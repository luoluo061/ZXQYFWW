"""Local finance policy logs and explicit human review; never a web collector.

Use after monitor.py and human_review.py have ingested the same real collection
round. Exporting views does not record a scan, a human review, or a benefit.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=8))
FINANCE_CATEGORIES = {"税费优惠", "奖补兑现", "人才用工稳岗", "融资增信贴息", "待核分类"}
REVIEW_STATES = {"待财务审核", "可推进准备", "待补齐", "不适用", "历史参考", "已到账", "已享受"}
PARTICIPATION = {"参与", "先补资料", "不参与"}
CLOSED = {"已到账", "已享受"}
FINANCE_FIELDS = {
    "category": "政策类别", "applicable_period": "适用财税期间", "support_type": "资金或税费类型",
    "benefit_description": "支持内容及口径", "payment_mode": "拨付或享受方式",
    "qualification_reward_link": "资质衍生奖励关联", "financial_requirements": "财务指标或条件",
    "company_evidence": "企业财务证据", "material_gaps": "材料缺口",
    "coordination_department": "需协同部门", "supplement_deadline": "补料期限",
    "nonduplication": "不得重复享受条款", "tax_notes": "涉税事项待核",
    "application_deadline": "企业填报截止", "local_acceptance_deadline": "本地受理节点",
    "recommendation_deadline": "主管部门推荐截止",
}
LEDGER_FIELDS = ["编号", "政策标题", *FINANCE_FIELDS.values(), "机器初筛", "关键条件（最多3条）",
    "正式原文", "原文核验", "发现来源", "下载资料", "当前来源版本", "财务审核状态", "上次财务结论",
    "财务审核人", "财务审核时间", "财务审核依据", "人员参与决定", "人员决定依据", "人工筛选复核",
    "关联复核编号", "人员补料缺口", "补料责任部门", "人员补料期限", "到账日期", "税惠享受日期",
    "已享受所属期", "会计凭证号", "闭环归档证据", "闭环对应来源版本", "闭环说明", "首次发现",
    "最近核查", "内部记录编号"]
DAILY_FIELDS = ["检索日期", "今日检索关键词", "检索地域（省/市）", "今日检索政策条数", "今日待办事项进展",
    "次日待检索关键词", "政策线索及人工判断", "备注"]


def load_core(name):
    spec = importlib.util.spec_from_file_location("finance_" + name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


monitor = load_core("monitor")
human_review = load_core("human_review")


def text_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(text_json(value).encode("utf-8")).hexdigest()


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def read_json(path):
    return json.loads(Path(path).read_text("utf-8-sig"))


def time_value(value):
    return human_review.timestamp(value)


def date_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期须为YYYY-MM-DD")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def safe_path(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("运行数据必须使用安全相对路径")
    target = root / rel
    if not target.resolve().is_relative_to(root):
        raise ValueError("工作目录中的链接指向目录外")
    return target


def workspace(value):
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ValueError("--workspace须为已安装财务工作目录的绝对路径")
    root = raw.resolve()
    if not root.is_dir():
        raise ValueError("工作目录不存在；先用bootstrap初始化")
    marker = safe_path(root, "data/install_manifest.json")
    profile_path = safe_path(root, "profile.json")
    if not marker.is_file() or not profile_path.is_file():
        raise ValueError("未找到安装清单和画像；未创建数据库或导出")
    record, profile = read_json(marker), read_json(profile_path)
    if record.get("name") not in {"sme-opportunity-monitor", "sme-finance-opportunities"}:
        raise ValueError("安装清单不属于本流程")
    install_path = safe_path(root, "data/installation.json")
    installed = read_json(install_path) if install_path.is_file() else {}
    mode = profile.get("workflow")
    finance = mode == "finance" or isinstance(mode, dict) and mode.get("kind") == "finance"
    if not (finance or installed.get("scope") == "finance" or installed.get("edition") == "finance"):
        raise ValueError("此目录未配置财务分支，未向通用政策或招标目录写入")
    return root, profile


@contextmanager
def ro_db(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError("缺少已初始化的数据库：" + path.name)
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


@contextmanager
def audit_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
      CREATE TABLE IF NOT EXISTS finance_runs (
        run_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL, payload_hash TEXT NOT NULL,
        input_json TEXT NOT NULL, screening_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
        snapshots_json TEXT NOT NULL DEFAULT '{}');
      CREATE TABLE IF NOT EXISTS finance_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL, source_version INTEGER NOT NULL,
        state TEXT NOT NULL, reviewer TEXT NOT NULL, note TEXT NOT NULL, reviewed_at TEXT NOT NULL,
        details TEXT NOT NULL, participation TEXT NOT NULL DEFAULT '');
      CREATE TRIGGER IF NOT EXISTS finance_runs_no_update BEFORE UPDATE ON finance_runs
        BEGIN SELECT RAISE(ABORT,'检索轮次不可改写'); END;
      CREATE TRIGGER IF NOT EXISTS finance_runs_no_delete BEFORE DELETE ON finance_runs
        BEGIN SELECT RAISE(ABORT,'检索轮次不可删除'); END;
      CREATE TRIGGER IF NOT EXISTS finance_reviews_no_update BEFORE UPDATE ON finance_reviews
        BEGIN SELECT RAISE(ABORT,'财务人审事件不可改写'); END;
      CREATE TRIGGER IF NOT EXISTS finance_reviews_no_delete BEFORE DELETE ON finance_reviews
        BEGIN SELECT RAISE(ABORT,'财务人审事件不可删除'); END;
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(finance_runs)")}
    if "snapshots_json" not in columns:
        db.execute("ALTER TABLE finance_runs ADD COLUMN snapshots_json TEXT NOT NULL DEFAULT '{}'")
    try:
        with db:
            yield db
    finally:
        db.close()


def finance_data(item):
    result = item.get("finance") or {}
    if not isinstance(result, dict):
        raise ValueError("finance须为对象")
    category = result.get("category") or "待核分类"
    if category not in FINANCE_CATEGORIES:
        raise ValueError("finance.category须为四类财务方向或待核分类")
    return dict(result, category=category)


def list_text(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return "；".join(list_text(part) for part in value)
    if isinstance(value, dict):
        return text_json(value)
    return str(value)


def csv_export(path, fields, rows):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".finance-export-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows({key: monitor.csv_value(row.get(key, "")) for key in fields} for row in rows)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def display_ids(root, records):
    target = safe_path(root, "data/display_ids.json")
    mapping = read_json(target) if target.is_file() else {}
    if isinstance(mapping, dict) and isinstance(mapping.get("ids"), dict):
        mapping = mapping["ids"]
    if not isinstance(mapping, dict):
        raise ValueError("display_ids格式错误")
    relevant = {row["id"]: mapping.get(row["id"], "") for row in records}
    if any(not re.fullmatch(r"P\d{3,}", value) for value in relevant.values()):
        raise ValueError("缺少政策稳定P编号；先运行monitor.py export，不临时占号")
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("display_ids包含重复编号")
    return relevant


def state_files(root):
    path = safe_path(root, "data/finance_workflow.sqlite")
    if not path.is_file():
        return [], []
    with ro_db(path) as db:
        runs = [dict(row) for row in db.execute("SELECT * FROM finance_runs ORDER BY checked_at,run_id")]
        reviews = [dict(row) for row in db.execute("SELECT * FROM finance_reviews ORDER BY reviewed_at,id")]
    return runs, reviews


def records_and_ids(root):
    with ro_db(safe_path(root, "data/monitor.sqlite")) as db:
        records = [dict(row) for row in db.execute("SELECT * FROM records ORDER BY first_seen,id")
                   if json.loads(row["data"])["kind"] == "policy"]
    return records, display_ids(root, records)


def record_for(root, requested):
    records, ids = records_and_ids(root)
    matches = [row for row in records if row["id"] == requested or ids.get(row["id"]) == requested]
    if len(matches) != 1:
        raise ValueError("未找到唯一财务政策编号，须用本工作目录的P号或内部编号")
    return matches[0], ids[matches[0]["id"]]


def source_snapshot(db, record_id, as_of):
    """Reconstruct source facts as known at a collection/review timestamp."""
    source = db.execute("SELECT * FROM versions WHERE record_id=? AND checked_at<=? ORDER BY version DESC LIMIT 1",
                        (record_id, as_of)).fetchone()
    first = db.execute("SELECT first_seen FROM records WHERE id=?", (record_id,)).fetchone()
    if source is None or first is None:
        return None
    pending, effective, last_seen = False, None, source["checked_at"]
    observations = db.execute("""SELECT o.data,r.checked_at FROM observations o
        JOIN runs r ON r.run_id=o.run_id WHERE o.record_id=? AND r.checked_at<=?
        ORDER BY r.checked_at,o.rowid""", (record_id, as_of))
    for observation in observations:
        item = json.loads(observation["data"])
        if effective is not None and monitor.business_hash(effective) != monitor.business_hash(item) and monitor.lower_information(effective, item):
            pending = True
        else:
            if effective is None or monitor.business_hash(effective) != monitor.business_hash(item):
                effective = item
            if item.get("original_verified"):
                pending = False
        last_seen = observation["checked_at"]
    return {"id": record_id, "version": source["version"], "data": source["data"],
            "pending_source_update": int(pending), "last_seen": last_seen, "first_seen": first[0]}


def log_round(workspace_path, payload, screening):
    root, _ = workspace(workspace_path)
    if not isinstance(payload, dict) or not isinstance(screening, dict):
        raise ValueError("候选与screening须为JSON对象")
    if payload.get("example_only") or screening.get("example_only"):
        raise ValueError("演示JSON不能登记为真实网站检索")
    run_id = payload.get("run_id")
    checked = time_value(payload.get("checked_at"))
    if not isinstance(run_id, str) or not run_id.strip() or screening.get("run_id") != run_id:
        raise ValueError("候选与screening须对应同一真实run_id")
    if time_value(screening.get("checked_at")) != checked:
        raise ValueError("同一轮候选和screening的checked_at必须一致")
    if screening.get("scope") != "policy":
        raise ValueError("财务日志只接受政策范围，不登记招标轮次")
    coverage = payload.get("coverage")
    if not isinstance(coverage, list) or not coverage:
        raise ValueError("必须保留实际检索coverage；失败也要登记具体范围")
    for source in coverage:
        if not isinstance(source, dict) or not source.get("source_id") or source.get("status") not in monitor.COVERAGE:
            raise ValueError("coverage须有source_id和真实采集状态")
    if not isinstance(screening.get("coverage"), list) or not screening["coverage"]:
        raise ValueError("screening须有实际覆盖与缺口")
    items = payload.get("items", [])
    observations = screening.get("observations", [])
    if not isinstance(items, list) or not isinstance(observations, list):
        raise ValueError("items/observations须为数组")
    suggestions = payload.get("next_keywords_suggested", [])
    if not isinstance(suggestions, list) or any(not isinstance(value, str) for value in suggestions):
        raise ValueError("next_keywords_suggested须为系统建议检索词数组")
    for item in items:
        monitor.validate_item(item)
        if item["kind"] != "policy":
            raise ValueError("财务分支不接收招标记录")
        finance_data(item)
    for item in observations:
        human_review.validate_observation(item, checked)
        if item["kind"] != "policy":
            raise ValueError("财务分支不接收招标观察")
    # Confirm the original run is already durable; opening nonexistent DBs must
    # never manufacture a successful check or a completed review.
    with ro_db(safe_path(root, "data/monitor.sqlite")) as db:
        known = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if known is None or known["checked_at"] != checked or json.loads(known["coverage"]) != coverage:
            raise ValueError("先将同一轮候选导入monitor；本日志不能代替真实采集和入库")
        stored = [json.loads(row[0]) for row in db.execute("SELECT data FROM observations WHERE run_id=?", (run_id,))]
        if {digest(item) for item in stored} != {digest(item) for item in items}:
            raise ValueError("本轮候选JSON与核心留存观察不一致，未登记")
        record_ids = {row[0] for row in db.execute("SELECT DISTINCT record_id FROM observations WHERE run_id=?", (run_id,))}
        snapshots = {record_id: source_snapshot(db, record_id, checked) for record_id in record_ids}
        if any(value is None for value in snapshots.values()):
            raise ValueError("本轮来源版本快照不完整，未将未来正文回填到旧日期")
    with ro_db(safe_path(root, "data/human_review.sqlite")) as db:
        known = db.execute("SELECT * FROM screening_runs WHERE run_id=?", (run_id,)).fetchone()
        if known is None or known["checked_at"] != checked or known["scope"] != "policy":
            raise ValueError("先将同轮screening导入human_review，未推荐也须留存")
        if json.loads(known["coverage"]) != screening["coverage"]:
            raise ValueError("screening覆盖与核心记录不一致")
        if known["payload_hash"] != human_review.digest(screening):
            raise ValueError("screening内容与已入库的同轮观察不一致")
        actual = db.execute("SELECT COUNT(*) FROM screening_observations WHERE run_id=?", (run_id,)).fetchone()[0]
        if actual != len(observations):
            raise ValueError("screening观察条数与核心留存不一致")
    content_hash = digest({"input": payload, "screening": screening})
    sidecar = safe_path(root, "data/finance_workflow.sqlite")
    with audit_db(sidecar) as db:
        old = db.execute("SELECT payload_hash FROM finance_runs WHERE run_id=?", (run_id,)).fetchone()
        if old:
            if old[0] != content_hash:
                raise ValueError("同run_id已存在不同内容；不能改写历史检索记录")
            return {"run_id": run_id, "status": "already_logged", "new_scan_recorded": False}
        db.execute("INSERT INTO finance_runs(run_id,checked_at,payload_hash,input_json,screening_json,recorded_at,snapshots_json) VALUES(?,?,?,?,?,?,?)",
                   (run_id, checked, content_hash, text_json(payload), text_json(screening), now(), text_json(snapshots)))
    result = export_views(root)
    return dict(result, run_id=run_id, status="logged", new_scan_recorded=True,
                note="仅登记该checked_at的既有真实采集，不是程序重新查网。")


def validate_evidence(root, supplied):
    entries = []
    for value in supplied or []:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = root / path
        for component in [path, *path.parents]:
            if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
                raise ValueError("闭环归档证据不能经过符号链接")
        path = path.resolve()
        if not path.is_file():
            raise ValueError("闭环证据文件未实际取得：" + str(value))
        entries.append({"path": path.as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "size_bytes": path.stat().st_size})
    return entries


def save_review(workspace_path, requested, state, reviewer, note, reviewed_at=None,
                date=None, period=None, voucher=None, evidence=None, material_gaps=None,
                department=None, due=None, participation=None, next_keywords=None):
    root, _ = workspace(workspace_path)
    if state not in REVIEW_STATES or not str(reviewer or "").strip() or not str(note or "").strip():
        raise ValueError("财务审核须提供真实审核人、有效状态及人员原话/依据")
    if participation is not None and participation not in PARTICIPATION:
        raise ValueError("参与决定须为参与/先补资料/不参与")
    checked = time_value(reviewed_at or now())
    record, display = record_for(root, requested)
    if checked < record["last_seen"]:
        raise ValueError("人审时间不得早于当前来源核查，旧意见不能确认新版本")
    details = {}
    if state == "待补齐":
        if not material_gaps or not any(str(x).strip() for x in material_gaps) or not str(department or "").strip() or not due:
            raise ValueError("待补齐必须明确材料缺口、责任部门和补齐期限")
        details.update(material_gaps=[str(x).strip() for x in material_gaps if str(x).strip()],
                       department=str(department).strip(), due=date_value(due))
    if state in CLOSED:
        if not date or not str(voucher or "").strip() or not evidence:
            raise ValueError("已到账/已享受必须有真实日期、会计凭证号及已取得的归档文件")
        date = date_value(date)
        if date > checked[:10]:
            raise ValueError("不能将未来到账或享受日期记为已完成")
        if state == "已享受" and not str(period or "").strip():
            raise ValueError("税惠已享受须记录适用所属期，不以现金到账日替代")
        details.update(date=date, voucher=str(voucher).strip(), evidence=validate_evidence(root, evidence))
        if state == "已享受":
            details["period"] = str(period).strip()
    if next_keywords is not None:
        details["next_keywords"] = [str(x).strip() for x in next_keywords if str(x).strip()]
    sidecar = safe_path(root, "data/finance_workflow.sqlite")
    with audit_db(sidecar):
        pass
    # ATTACH makes the optional explicit participation decision and finance audit
    # one transaction. monitor.save_review commits that same attached transaction.
    path = safe_path(root, "data/monitor.sqlite")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("ATTACH DATABASE ? AS finance_audit", (str(sidecar),))
        with db:
            current = db.execute("SELECT version,last_seen FROM records WHERE id=?", (record["id"],)).fetchone()
            if current["version"] != record["version"] or current["last_seen"] != record["last_seen"]:
                raise ValueError("审核期间来源已更新，须先查看当前版本再确认")
            cursor = db.execute("INSERT INTO finance_audit.finance_reviews(record_id,source_version,state,reviewer,note,reviewed_at,details,participation) VALUES(?,?,?,?,?,?,?,?)",
                (record["id"], record["version"], state, str(reviewer).strip(), str(note).strip(), checked,
                 text_json(details), participation or ""))
            event_id = cursor.lastrowid
            if participation is not None:
                monitor.save_review(db, record["id"], participation, f"{str(reviewer).strip()}：{str(note).strip()}", checked)
    finally:
        db.close()
    result = export_views(root)
    return dict(result, id=display, event_id=event_id, state=state,
                participation_changed=participation is not None, reviewed_at=checked)


def current_review(record, reviews):
    events = [row for row in reviews if row["record_id"] == record["id"]]
    latest = events[-1] if events else None
    stale = latest is not None and (latest["source_version"] != record["version"] or bool(record["pending_source_update"]))
    state = "待财务重新复核" if stale else latest["state"] if latest else "待财务审核"
    closed = next((row for row in reversed(events) if row["state"] in CLOSED), None)
    return latest, closed, state


def ledger_rows(root, profile, records, ids, reviews):
    manifest = safe_path(root, "materials/materials_manifest.json")
    attachments = read_json(manifest).get("attachments", []) if manifest.is_file() else []
    checks = {}
    pool = safe_path(root, "data/human_review.sqlite")
    if pool.is_file():
        with ro_db(pool) as db:
            checks = human_review.opportunity_status(db)
    # Core material helpers are parameterized by its runtime root. Restore after
    # export so an imported module cannot change a later unrelated call.
    previous = monitor.TOOL_ROOT
    monitor.TOOL_ROOT = root
    rows = []
    try:
        for record in records:
            item = json.loads(record["data"])
            finance = finance_data(item)
            latest, closed, state = current_review(record, reviews)
            detail = json.loads(latest["details"]) if latest else {}
            closing = json.loads(closed["details"]) if closed else {}
            entries = monitor.material_entries(record["id"], attachments)
            check = checks.get(ids[record["id"]], {})
            closed_valid = closed and latest["id"] == closed["id"] and closed["source_version"] == record["version"] and not record["pending_source_update"]
            if closed and latest["id"] != closed["id"]:
                closure_note = f"历史闭环已保留；已被后续人员结论“{latest['state']}”替代，不作为当前到账或享受确认"
            else:
                closure_note = "人员据归档证据确认" if closed_valid else "历史闭环已保留；当前来源变化须重新复核" if closed else "尚未由人员确认到账或享受"
            machine_decision = monitor.evaluate(item, profile, now())["decision"]
            if record["pending_source_update"] and machine_decision not in {"历史参考", "已过截止", "不符"}:
                machine_decision = "待核"
            row = {"编号": ids[record["id"]], "政策标题": item["title"],
                **{label: list_text(finance.get(field)) for field, label in FINANCE_FIELDS.items()},
                "机器初筛": machine_decision,
                "关键条件（最多3条）": "；".join(monitor.key_conditions(item)),
                "正式原文": item.get("original_url", ""), "原文核验": "已核正式原文" if item.get("original_verified") else "待核原文",
                "发现来源": item["source_url"], "下载资料": monitor.material_text(entries),
                "当前来源版本": record["version"], "财务审核状态": state,
                "上次财务结论": latest["state"] if latest else "", "财务审核人": latest["reviewer"] if latest else "",
                "财务审核时间": latest["reviewed_at"] if latest else "", "财务审核依据": latest["note"] if latest else "",
                "人员参与决定": monitor.review_display(record["review_state"]), "人员决定依据": record["review_note"],
                "人工筛选复核": check.get("status", "尚未进入复核池"), "关联复核编号": "、".join(check.get("case_ids", [])),
                "人员补料缺口": list_text(detail.get("material_gaps")), "补料责任部门": detail.get("department", ""),
                "人员补料期限": detail.get("due", ""),
                "到账日期": closing.get("date", "") if closed and closed["state"] == "已到账" else "",
                "税惠享受日期": closing.get("date", "") if closed and closed["state"] == "已享受" else "",
                "已享受所属期": closing.get("period", ""), "会计凭证号": closing.get("voucher", ""),
                "闭环归档证据": list_text(closing.get("evidence")), "闭环对应来源版本": closed["source_version"] if closed else "",
                "闭环说明": closure_note, "首次发现": record["first_seen"], "最近核查": record["last_seen"], "内部记录编号": record["id"]}
            rows.append(row)
    finally:
        monitor.TOOL_ROOT = previous
    return rows


def coverage_terms(source, names):
    result = []
    for name in names:
        value = source.get(name)
        values = value if isinstance(value, list) else [value]
        for part in values:
            if part is not None and str(part).strip():
                result.append(str(part).strip())
    return result


def business_identity(item):
    number = item.get("document_number")
    if number:
        return "business:" + text_json([item.get("issuer") or "", str(number), item.get("lot") or "", item.get("round") or ""])
    return ""


def identity_urls(item):
    """Only evidence-linked pages, never a generic list or an invented title join."""
    urls = set()
    scope = (str(item.get("lot") or ""), str(item.get("round") or ""))
    if item.get("original_verified") and item.get("original_url") and not item.get("original_is_list"):
        urls.add((monitor.canonical_url(item["original_url"]), *scope))
    if item.get("detail_url") and not item.get("detail_is_list"):
        urls.add((monitor.canonical_url(item["detail_url"]), *scope))
    if item.get("source_url") and item.get("source_is_list") is False:
        urls.add((monitor.canonical_url(item["source_url"]), *scope))
    if item.get("platform_url") and not item.get("platform_is_list"):
        urls.add((monitor.canonical_url(item["platform_url"]), *scope))
    return urls


def observed_keys(observations, snapshots, ids):
    """Deduplicate linked pages without merging distinct batches at one URL."""
    source_items = {record_id: json.loads(row["data"]) for record_id, row in snapshots.items()}
    primary = {record_id: business_identity(item) or "record:" + record_id for record_id, item in source_items.items()}
    reverse = {value: key for key, value in ids.items()}
    urls = {}
    for record_id, item in source_items.items():
        for url in identity_urls(item):
            urls.setdefault(url, set()).add(primary[record_id])
    for item in observations:
        key = business_identity(item)
        if key:
            for url in identity_urls(item):
                urls.setdefault(url, set()).add(key)
    result = set(primary.values())
    for item in observations:
        key = business_identity(item)
        linked = reverse.get(item.get("opportunity_id"))
        if not key and linked in primary:
            key = primary[linked]
        if not key:
            references = identity_urls(item)
            # A platform observation may only have followed a real href; using
            # that href to join an already verified official candidate does not
            # upgrade the observation's reading level or original_verified flag.
            if item.get("original_url") and not item.get("original_is_list"):
                reference = (monitor.canonical_url(item["original_url"]), str(item.get("lot") or ""), str(item.get("round") or ""))
                if reference in urls:
                    references.add(reference)
            matches = set().union(*(urls.get(url, set()) for url in references))
            if len(matches) == 1:
                key = next(iter(matches))
        if not key:
            verified = sorted(identity_urls(dict(item, detail_url="", source_url="", platform_url="")))
            key = "original:" + text_json(verified) if verified else "unlinked:" + human_review.identity(item)
        result.add(key)
    return result


def daily_rows(root, runs, reviews, records, ids, ledgers):
    days = sorted({row["checked_at"][:10] for row in runs} | {row["reviewed_at"][:10] for row in reviews}, reverse=True)
    results = []
    with ro_db(safe_path(root, "data/monitor.sqlite")) as core:
        for day in days:
            day_runs = [row for row in runs if row["checked_at"][:10] == day]
            day_reviews = [row for row in reviews if row["reviewed_at"][:10] == day]
            keywords, regions, seen, failures, candidates = set(), set(), set(), [], set()
            snapshots, observations = {}, []
            suggested_keywords = []
            any_success = False
            for row in day_runs:
                payload, screening = json.loads(row["input_json"]), json.loads(row["screening_json"])
                if "next_keywords_suggested" in payload:
                    suggested_keywords = payload["next_keywords_suggested"]
                for source in payload["coverage"]:
                    keywords.update(coverage_terms(source, ("queries", "query", "keywords")))
                    regions.update(coverage_terms(source, ("regions", "region", "region_filter", "province", "city")))
                    any_success |= source["status"] in {"success", "partial"}
                    if source["status"] != "success":
                        failures.append(source["source_id"] + "：" + monitor.COVERAGE[source["status"]] + ("（" + str(source["note"]) + "）" if source.get("note") else ""))
                for observed in screening["observations"]:
                    observations.append(observed)
                    keywords.update(coverage_terms(observed, ("query",)))
                    regions.update(coverage_terms(observed, ("region_filter",)))
                saved = json.loads(row.get("snapshots_json") or "{}")
                if not saved:
                    record_ids = {found[0] for found in core.execute("SELECT DISTINCT record_id FROM observations WHERE run_id=?", (row["run_id"],))}
                    saved = {record_id: source_snapshot(core, record_id, row["checked_at"]) for record_id in record_ids}
                snapshots.update({key: value for key, value in saved.items() if value is not None})
                candidates.update(saved)
            seen = observed_keys(observations, snapshots, ids)
            candidates.update(row["record_id"] for row in day_reviews)
            for event in day_reviews:
                snapshot = source_snapshot(core, event["record_id"], event["reviewed_at"])
                previous = snapshots.get(event["record_id"])
                # A morning human opinion refers to its then-current source.
                # It must not displace a later source change collected that day.
                newer = snapshot is not None and (previous is None or
                    (snapshot["last_seen"], snapshot["version"]) > (previous["last_seen"], previous["version"]))
                if newer:
                    snapshots[event["record_id"]] = snapshot
            progress = []
            next_keywords = []
            for row in day_reviews:
                detail = json.loads(row["details"])
                progress.append(f"{ids.get(row['record_id'], row['record_id'])} {row['reviewer']}：{row['state']}（来源v{row['source_version']}，{row['reviewed_at'][11:16]}审核）；{row['note']}")
                if detail.get("department"):
                    progress[-1] += "；补料：" + list_text(detail["material_gaps"]) + "；责任部门：" + detail["department"] + "；期限：" + detail["due"]
                if row["state"] in CLOSED:
                    progress[-1] += f"；{'到账' if row['state'] == '已到账' else '享受'}日期：{detail['date']}；凭证：{detail['voucher']}"
                if "next_keywords" in detail:
                    next_keywords = detail["next_keywords"]
            policy_lines = []
            for record_id in sorted(candidates, key=lambda value: ids.get(value, value)):
                snapshot = snapshots.get(record_id)
                if snapshot:
                    item = json.loads(snapshot["data"])
                    prior_reviews = [event for event in reviews if event["reviewed_at"][:10] <= day]
                    state = current_review(snapshot, prior_reviews)[2]
                    decision = core.execute("SELECT state FROM review_events WHERE record_id=? AND reviewed_at<=? ORDER BY reviewed_at DESC,id DESC LIMIT 1",
                                            (record_id, day + "T23:59:59+08:00")).fetchone()
                    participation = monitor.review_display(decision[0]) if decision else "待人员判断"
                    policy_lines.append(f"{ids.get(record_id, record_id)} {item['title']}；来源v{snapshot['version']}；{state}；参与决定：{participation}；{item.get('original_url') or item['source_url']}")
            if not day_runs:
                count = "未执行网站检索（仅记录人员进展）"
            elif not any_success and not seen:
                count = "未完成（已读0条；失败范围见备注）"
            else:
                count = str(len(seen))
            note = [f"本日实际登记{len(day_runs)}个检索轮次；条数为可辨认记录去重计数，不是全网总量；机器初筛不等于财务已审。",
                    "政策线索按本日来源快照及截至当天的人审、参与决定回放；以后更新不回灌历史日期。"]
            if failures:
                note.append("缺口：" + "；".join(dict.fromkeys(failures)))
            if not seen and day_runs and any_success:
                note.append("本轮已记录范围未读到可辨认新增标题；无新增不取消检索留痕。")
            if not next_keywords:
                note.append("次日关键词待人员确认；空白不表示无需检索。")
            plan = "人员确认：" + "、".join(next_keywords) if next_keywords else "系统建议（待人员确认）：" + "、".join(suggested_keywords) if suggested_keywords else "待人员确认"
            results.append(dict(zip(DAILY_FIELDS, [day, "、".join(sorted(keywords)), "、".join(sorted(regions)), count,
                "\n".join(progress) or "待人员更新；机器运行不代表人工已审核或事项已推进", plan,
                "\n".join(policy_lines) or "本日未形成正式候选；待核/未推荐及历史观察见R复核池", "\n".join(note)])))
    return results


def export_views(workspace_path):
    root, profile = workspace(workspace_path)
    records, ids = records_and_ids(root)
    runs, reviews = state_files(root)
    rows = ledger_rows(root, profile, records, ids, reviews)
    daily = daily_rows(root, runs, reviews, records, ids, rows)
    folder = safe_path(root, "exports")
    folder.mkdir(exist_ok=True)
    ledger_path = safe_path(root, "exports/财务政策资金台账.csv")
    daily_path = safe_path(root, "exports/每日检索政策台账.csv")
    csv_export(ledger_path, LEDGER_FIELDS, rows)
    csv_export(daily_path, DAILY_FIELDS, daily)
    return {"ledger": str(ledger_path), "daily_log": str(daily_path), "finance_opportunities": len(rows),
            "logged_runs": len(runs), "human_review_events": len(reviews), "web_collection_performed": False}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("export", "log", "review"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--workspace", required=True)
        if name == "log":
            cmd.add_argument("--input", required=True)
            cmd.add_argument("--screening", required=True)
        elif name == "review":
            cmd.add_argument("--id", required=True)
            cmd.add_argument("--state", choices=sorted(REVIEW_STATES), required=True)
            cmd.add_argument("--reviewer", required=True)
            cmd.add_argument("--note", required=True)
            cmd.add_argument("--reviewed-at")
            cmd.add_argument("--date")
            cmd.add_argument("--period")
            cmd.add_argument("--voucher")
            cmd.add_argument("--evidence", action="append")
            cmd.add_argument("--material-gap", action="append")
            cmd.add_argument("--department")
            cmd.add_argument("--due")
            cmd.add_argument("--participation", choices=sorted(PARTICIPATION))
            cmd.add_argument("--next-keyword", action="append")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            result = export_views(args.workspace)
        elif args.command == "log":
            result = log_round(args.workspace, read_json(args.input), read_json(args.screening))
        else:
            result = save_review(args.workspace, args.id, args.state, args.reviewer, args.note, args.reviewed_at,
                args.date, args.period, args.voucher, args.evidence, args.material_gap, args.department, args.due,
                args.participation, args.next_keyword)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
