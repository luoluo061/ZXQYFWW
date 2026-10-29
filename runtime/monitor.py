"""Local, evidence-first policy/tender ledger. Acquisition is performed separately."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TZ = timezone(timedelta(hours=8))
STAGES = {"open", "upcoming", "change", "terminated", "awarded", "historical", "unknown"}
REVIEW_STATES = {"关注", "待核", "推进", "暂不推进", "结束", "待人员判断", "参与", "先补资料", "不参与"}
REVIEW_DISPLAY = {"待核": "待人员判断", "关注": "待人员判断", "推进": "参与", "暂不推进": "不参与"}
TOOL_ROOT = Path(__file__).resolve().parent
COVERAGE = {"success": "成功", "partial": "部分完成", "auth_required": "需要登录", "failed": "采集失败"}
META_FIELDS = {"source_id", "source_url", "original_url", "evidence_path", "checked_at", "source_record_id", "item_id", "source_is_list", "original_is_list"}


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_json(path):
    return json.loads(Path(path).read_text("utf-8-sig"))


def canonical_url(url):
    if not url:
        return ""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"https", "http"} or not parts.netloc:
        raise ValueError(f"只接受完整 http/https 来源地址：{url}")
    if parts.username or parts.password:
        raise ValueError("来源地址不得包含账号或密码")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if any(k.lower() in {"token", "access_token", "refresh_token", "password", "authorization", "sessionid", "session_id"} for k, _ in pairs):
        raise ValueError("来源链接包含敏感会话参数，请改用不带凭据的业务地址")
    query = [(k, v) for k, v in pairs if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(sorted(query)), ""))


def moment(value):
    value = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.combine(datetime.fromisoformat(value).date(), time(23, 59, 59), TZ)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)


def validate_item(item):
    for name in ("kind", "source_id", "source_url", "title"):
        if not isinstance(item.get(name), str) or not item[name].strip():
            raise ValueError(f"线索缺少文本字段 {name}")
    if item["kind"] not in {"policy", "tender"}:
        raise ValueError("kind 必须为 policy 或 tender")
    canonical_url(item["source_url"])
    canonical_url(item.get("original_url"))
    if not isinstance(item.get("original_verified", False), bool):
        raise ValueError("original_verified 必须为 true/false")
    if item.get("original_verified") and not item.get("original_url"):
        raise ValueError("原文已核必须提供 original_url")
    if item.get("stage", "unknown") not in STAGES:
        raise ValueError("未知的 stage 值")
    if item.get("deadline_at"):
        moment(item["deadline_at"])
    if item.get("published_at"):
        datetime.strptime(item["published_at"], "%Y-%m-%d")
    for condition in item.get("hard_conditions", []):
        if condition.get("status", "unknown") not in {"pass", "fail", "unknown"}:
            raise ValueError("硬性条件状态必须为 pass/fail/unknown")
    for node in item.get("deadlines", []):
        if node.get("at"):
            moment(node["at"])


def aliases(item):
    scope = (item["kind"], str(item.get("lot") or ""), str(item.get("round") or ""))
    keys = []
    number = item.get("document_number") if item["kind"] == "policy" else item.get("project_id")
    if number:
        keys.append("natural:" + json_text((*scope, item.get("issuer", ""), str(number))))
        # A shared list/detail URL can contain several document or project IDs.
        # Known business identities must never be joined through a shared URL.
        return keys
    if item.get("original_url") and not item.get("original_is_list", False):
        return ["original:" + json_text((*scope, canonical_url(item["original_url"])))]
    if item.get("source_is_list") is False:
        return ["detail:" + json_text((*scope, canonical_url(item["source_url"])))]
    title = re.sub(r"\s+", "", item["title"]).casefold()
    keys.append("candidate:" + json_text((*scope, item.get("issuer", ""), title, item.get("published_at") or "")))
    return list(dict.fromkeys(keys))


def information_quality(item):
    conditions = item.get("hard_conditions", [])
    evidence_count = sum(bool(x.get("evidence")) for x in conditions)
    return (int(item.get("original_verified", False)), int(bool(item.get("body_text"))), evidence_count)


def lower_information(old, item):
    if old.get("original_verified") and not item.get("original_verified"):
        return True
    different_source = canonical_url(old["source_url"]) != canonical_url(item["source_url"])
    return different_source and information_quality(item) < information_quality(old)


def business_hash(item):
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in META_FIELDS}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else value
    return hashlib.sha256(json_text(clean(item)).encode()).hexdigest()


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
      PRAGMA foreign_keys=ON;
      CREATE TABLE IF NOT EXISTS records (
        id TEXT PRIMARY KEY, data TEXT NOT NULL, content_hash TEXT NOT NULL,
        version INTEGER NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        review_state TEXT NOT NULL DEFAULT '待核', review_note TEXT NOT NULL DEFAULT '',
        pending_source_update INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, record_id TEXT NOT NULL REFERENCES records(id));
      CREATE TABLE IF NOT EXISTS versions (
        record_id TEXT NOT NULL REFERENCES records(id), version INTEGER NOT NULL,
        checked_at TEXT NOT NULL, content_hash TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(record_id,version));
      CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL, profile_version TEXT,
        coverage TEXT NOT NULL, changes TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS observations (
        run_id TEXT NOT NULL, record_id TEXT NOT NULL REFERENCES records(id), source_id TEXT,
        source_url TEXT, data TEXT NOT NULL,
        PRIMARY KEY(run_id,record_id,source_id,source_url));
      CREATE TABLE IF NOT EXISTS review_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id TEXT NOT NULL REFERENCES records(id), previous_state TEXT NOT NULL,
        state TEXT NOT NULL, note TEXT NOT NULL, reviewed_at TEXT NOT NULL);
    """)
    if "pending_source_update" not in {r[1] for r in db.execute("PRAGMA table_info(records)")}:
        db.execute("ALTER TABLE records ADD COLUMN pending_source_update INTEGER NOT NULL DEFAULT 0")
        db.commit()
    return db


def review_display(state):
    return REVIEW_DISPLAY.get(state, state if state in REVIEW_STATES else "待人员判断")


def save_review(db, record_id, state, note, reviewed_at=None):
    """Save a human decision separately from source updates, with an audit event."""
    if state not in REVIEW_STATES or not str(note).strip():
        raise ValueError("人工决定须使用有效状态并提供决定依据或备注")
    reviewed_at = moment(reviewed_at).isoformat(timespec="seconds") if reviewed_at else datetime.now(TZ).isoformat(timespec="seconds")
    with db:
        old = db.execute("SELECT review_state FROM records WHERE id=?", (record_id,)).fetchone()
        if old is None:
            raise ValueError("未找到编号；请使用台账的内部记录编号")
        db.execute("UPDATE records SET review_state=?,review_note=? WHERE id=?", (state, str(note).strip(), record_id))
        db.execute("INSERT INTO review_events(record_id,previous_state,state,note,reviewed_at) VALUES(?,?,?,?,?)",
                   (record_id, old["review_state"], state, str(note).strip(), reviewed_at))
    return {"id": record_id, "review_state": state, "person_decision": review_display(state),
            "note_saved": True, "reviewed_at": reviewed_at}


def ingest(db, payload, profile):
    checked = payload.get("checked_at")
    if not checked or "T" not in checked or moment(checked).utcoffset() is None:
        raise ValueError("checked_at 必须为 ISO 日期时间")
    if not re.search(r"(?:Z|[+-]\d\d:\d\d)$", checked):
        raise ValueError("checked_at 必须包含时区")
    checked = moment(checked).isoformat(timespec="seconds")
    run_id = payload.get("run_id")
    if not run_id:
        raise ValueError("缺少 run_id")
    coverage = payload.get("coverage", [])
    for source in coverage:
        if not source.get("source_id") or source.get("status") not in COVERAGE:
            raise ValueError("coverage 必须提供 source_id 和有效 status")
    items = payload.get("items", [])
    for item in items:
        validate_item(item)
    source_ids = {x["source_id"] for x in coverage}
    if any(x["source_id"] not in source_ids for x in items):
        raise ValueError("每个线索来源都必须列入 coverage")
    changes = {"new": [], "changed": [], "unchanged": [], "unverified_updates": [], "observations": len(items)}
    with db:
        for item in items:
            keys = aliases(item)
            matches = {row[0] for key in keys for row in db.execute("SELECT record_id FROM aliases WHERE alias=?", (key,))}
            if len(matches) > 1:
                raise ValueError("来源身份关联到多条旧记录，需人工合并，未自动丢弃任何记录")
            record_id = next(iter(matches), hashlib.sha256(keys[0].encode()).hexdigest()[:20])
            old = db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            digest = business_hash(item)
            version = old["version"] + 1 if old else 1
            data = json_text(item)
            downgraded = old and old["content_hash"] != digest and lower_information(json.loads(old["data"]), item)
            previously_observed = False
            if downgraded:
                previously_observed = any(business_hash(json.loads(row[0])) == digest for row in
                    db.execute("SELECT data FROM observations WHERE record_id=?", (record_id,)))
            if not old:
                db.execute("INSERT INTO records(id,data,content_hash,version,first_seen,last_seen) VALUES(?,?,?,?,?,?)",
                           (record_id, data, digest, version, checked, checked))
                action = "new"
            elif old["content_hash"] != digest and not downgraded:
                db.execute("UPDATE records SET data=?,content_hash=?,version=?,last_seen=? WHERE id=?",
                           (data, digest, version, checked, record_id))
                action = "changed"
            else:
                db.execute("UPDATE records SET last_seen=? WHERE id=?", (checked, record_id))
                action = "unchanged"
                if downgraded and not previously_observed:
                    changes["unverified_updates"].append(record_id)
            if downgraded:
                db.execute("UPDATE records SET pending_source_update=1 WHERE id=?", (record_id,))
            elif item.get("original_verified"):
                db.execute("UPDATE records SET pending_source_update=0 WHERE id=?", (record_id,))
            if action != "unchanged":
                db.execute("INSERT INTO versions VALUES(?,?,?,?,?)", (record_id, version, checked, digest, data))
            for key in keys:
                db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?)", (key, record_id))
            db.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?)",
                       (run_id, record_id, item["source_id"], item["source_url"], data))
            if record_id not in changes[action]:
                changes[action].append(record_id)
        # A record encountered twice is counted once at its strongest change level.
        changes["changed"] = [x for x in changes["changed"] if x not in changes["new"]]
        changes["unchanged"] = [x for x in changes["unchanged"] if x not in changes["new"] + changes["changed"]]
        db.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?)",
                   (run_id, checked, str(profile.get("version", "未标版本")), json_text(coverage), json_text(changes)))
    return changes


def evaluate(item, profile, as_of):
    as_of = moment(as_of) if isinstance(as_of, str) else as_of
    stage = item.get("stage", "unknown")
    deadline = moment(item["deadline_at"]) if item.get("deadline_at") else None
    days = (deadline.date() - as_of.astimezone(TZ).date()).days if deadline else None
    reasons = []
    conditions = item.get("hard_conditions", [])
    failed = [c for c in conditions if c.get("status") == "fail" and c.get("evidence")]
    unknown = [c for c in conditions if c.get("status", "unknown") == "unknown" or not c.get("evidence")]
    missed_prerequisites = [node for node in item.get("deadlines", [])
        if node.get("at") and moment(node["at"]) < as_of and node.get("completed") is not True
        and re.search(r"报名|获取.{0,8}文件|领取.{0,8}文件|取文件|购买.{0,8}文件|文件.{0,8}获取", node.get("label", ""))]
    if stage in {"terminated", "awarded", "historical"}:
        decision = "历史参考"
        reasons.append({"terminated": "项目已终止", "awarded": "已成交或中标", "historical": "历史记录"}[stage])
    elif deadline and deadline < as_of:
        decision = "已过截止"
        reasons.append("主办理截止时间已过；后续如有更正须重新核验")
    elif not item.get("original_verified"):
        decision = "待核"
        reasons.append("正式原文尚未核验")
    elif failed:
        decision = "不符"
        reasons.extend("不满足：" + c.get("label", "未命名硬性条件") for c in failed)
    elif stage == "upcoming":
        decision = "储备跟踪"
        reasons.append("尚未开放办理，不作为当前可申报或可投机会")
    elif stage == "change":
        decision = "变更待核"
        reasons.append("应关联原项目核对最新文件及各办理期限，不新增为一项招标")
    elif stage != "open":
        decision = "待核"
        reasons.append("当前办理阶段未核清")
    elif not conditions or unknown or not deadline or missed_prerequisites:
        decision = "待核"
        if not conditions:
            reasons.append("尚未完成硬性条件逐条核对")
        reasons.extend("待核：" + c.get("label", "未命名硬性条件") for c in unknown)
        if not deadline:
            reasons.append("主办理截止时间未核清")
        reasons.extend(f"{node.get('label', '前置节点')}已过，企业是否已完成待核" for node in missed_prerequisites)
    elif item["kind"] == "tender" and not profile.get("confirmed_services"):
        decision = "待核"
        reasons.append("企业真实可交付业务尚未确认")
    else:
        decision = "可推进准备"
        reasons.append("已核条件可进入人工复核及准备，不代表认定、受理或投标批准")
    keywords = profile.get("discovery_keywords", {}).get(item["kind"], [])
    haystack = " ".join(str(item.get(k) or "") for k in ("title", "summary", "body_text"))
    hits = [k for k in keywords if k and k.casefold() in haystack.casefold()]
    return {"decision": decision, "reasons": reasons, "keyword_hits": hits,
            "candidate_score": min(len(hits) * 10, 100), "deadline_days": days}


FIELDS = ["编号", "类型", "标题", "事项摘要", "匹配理由", "机关或采购人", "阶段", "判断", "判断依据", "候选相关度非资格分", "命中词",
          "地区", "文号或项目编号", "标段", "轮次", "主截止", "距截止天数", "各办理节点", "原文已核", "原文", "平台来源",
          "关联项目", "当前版本", "首次发现", "最近看到", "待核来源更新", "人工跟进状态", "人工备注", "下一动作", "证据文件"]


def csv_value(value):
    result = "" if value is None else str(value)
    return "'" + result if result.lstrip().startswith(("=", "+", "-", "@")) else result


def write_csv(path, rows):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows({k: csv_value(v) for k, v in row.items()} for row in rows)


HUMAN_FIELDS = ["编号", "类型", "标题", "一句话解读", "企业关联", "关键条件（最多3条）", "主要截止", "各办理节点",
                "初筛情况", "人员决定", "决定备注", "决定时间", "正式原文", "原文核验", "发现或转发来源",
                "下载资料", "首次记录", "最近复核", "下一步（供人员决定）", "内部记录编号",
                "人工筛选复核", "筛选复核人", "筛选复核时间", "关联复核编号"]


def write_human_csv(path, rows):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=HUMAN_FIELDS)
        writer.writeheader()
        writer.writerows({k: csv_value(row.get(k, "")) for k in HUMAN_FIELDS} for row in rows)


def short_text(value, limit=120):
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value if len(value) <= limit else value[:limit - 1] + "…"


def key_conditions(item):
    curated = item.get("key_conditions")
    if isinstance(curated, list) and curated:
        return [short_text(x.get("label", "") if isinstance(x, dict) else x, 90) for x in curated[:3]]
    labels = {"pass": "证据已核", "fail": "已核不符", "unknown": "待核"}
    return [short_text(c.get("label", "未命名条件"), 80) + "（" + labels.get(c.get("status"), "待核") + "）"
            for c in item.get("hard_conditions", [])[:3]]


def presentation_resources():
    display_file = TOOL_ROOT / "data" / "display_ids.json"
    display = read_json(display_file) if display_file.is_file() else {}
    if isinstance(display, dict) and isinstance(display.get("ids"), dict):
        display = display["ids"]
    display = {str(k): v for k, v in display.items() if isinstance(v, str)} if isinstance(display, dict) else {}
    manifest = TOOL_ROOT / "materials" / "materials_manifest.json"
    materials = read_json(manifest).get("attachments", []) if manifest.is_file() else []
    return display, materials if isinstance(materials, list) else []


def ensure_display_ids(db):
    """Append stable presentation IDs; source updates never reorder existing IDs."""
    target = TOOL_ROOT / "data" / "display_ids.json"
    document = read_json(target) if target.is_file() else {}
    wrapped = isinstance(document.get("ids"), dict)
    ids = document["ids"] if wrapped else document
    if not isinstance(ids, dict) or any(not isinstance(v, str) for v in ids.values()):
        raise ValueError("display_ids格式错误，未重排或覆盖原编号")
    if len(ids.values()) != len(set(ids.values())):
        raise ValueError("display_ids存在重复编号，须先核实")
    for row in db.execute("SELECT id,data FROM records ORDER BY first_seen,id"):
        prefix = "P" if json.loads(row["data"])["kind"] == "policy" else "T"
        if row["id"] in ids:
            if not re.fullmatch(prefix + r"\d{3,}", ids[row["id"]]):
                raise ValueError("display_ids编号与事项类别不一致")
            continue
        maximum = max((int(value[1:]) for value in ids.values() if re.fullmatch(prefix + r"\d{3,}", value)), default=0)
        ids[row["id"]] = f"{prefix}{maximum + 1:03d}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if wrapped:
        document["ids"] = ids
    else:
        document = ids
    target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return ids


def screening_status():
    """Read the separate human-review ledger without creating or changing it."""
    source = TOOL_ROOT / "data" / "human_review.sqlite"
    if not source.is_file():
        return {}
    import human_review
    db = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        return human_review.opportunity_status(db)
    finally:
        db.close()


def load_card_notes():
    """Optional display notes never enter source data or the version hash."""
    note_file = TOOL_ROOT / "data" / "card_notes.json"
    notes = read_json(note_file) if note_file.is_file() else {}
    if isinstance(notes, dict) and isinstance(notes.get("notes"), dict):
        notes = notes["notes"]
    return notes if isinstance(notes, dict) else {}


def material_entries(record_id, materials):
    result = []
    for entry in materials:
        if not isinstance(entry, dict) or entry.get("item_id") != record_id:
            continue
        source = entry.get("url") or ""
        try:
            canonical_url(source)
        except ValueError:
            source = ""
        candidate = Path(entry["local_path"]) if entry.get("local_path") else None
        if candidate is not None and not candidate.is_absolute():
            candidate = TOOL_ROOT / candidate
        downloaded = entry.get("status") == "downloaded" and candidate is not None and candidate.is_file()
        status = "已下载（本地文件存在）" if downloaded else {
            "downloaded": "记录称已下载，但本地文件缺失",
            "inline_notice_verified": "已核正文，独立下载资料尚未核得",
            "purchase_or_login_required": "需登录或购标后获取，尚未取得",
            "auth_required": "需登录后获取，尚未取得",
            "failed": "下载未完成",
        }.get(entry.get("status"), "尚未取得下载文件")
        result.append({"title": short_text(entry.get("title") or entry.get("filename") or "相关资料", 100),
                       "status": status, "url": source, "local_path": candidate.as_posix() if downloaded else "",
                       "source_label": "原下载链接" if entry.get("status") == "downloaded" else "资料来源／获取入口",
                       "note": short_text(entry.get("note"), 160)})
    return result


def material_text(entries):
    return "\n".join("；".join(x for x in [e["title"], e["status"], e["url"], e["local_path"], e["note"]] if x)
                     for e in entries) or "尚未核得下载资料"


def export_human_views(db, rows, folder, changes, alerts, checked):
    ensure_display_ids(db)
    display, materials = presentation_resources()
    screening = screening_status()
    notes = load_card_notes()
    human_rows = []
    cards = {}
    for row in rows:
        record = db.execute("SELECT data,version FROM records WHERE id=?", (row["编号"],)).fetchone()
        item = json.loads(record["data"])
        note = notes.get(row["编号"], {})
        if isinstance(note, dict) and note.get("source_version", 1) == record["version"]:
            # Only these three presentation fields may be overridden; source facts and human decisions stay untouched.
            for field in ("brief_interpretation", "relevance_reason"):
                if isinstance(note.get(field), str) and note[field].strip():
                    item[field] = note[field]
            if isinstance(note.get("key_conditions"), list) and note["key_conditions"]:
                item["key_conditions"] = note["key_conditions"]
        event = db.execute("SELECT reviewed_at FROM review_events WHERE record_id=? ORDER BY id DESC LIMIT 1", (row["编号"],)).fetchone()
        entries = material_entries(row["编号"], materials)
        critical = key_conditions(item)
        human = dict(zip(HUMAN_FIELDS, [display.get(row["编号"], row["编号"]), row["类型"], row["标题"],
            short_text(item.get("brief_interpretation") or item.get("summary") or "用途待核，先查原文与资料。"),
            short_text(item.get("relevance_reason") or "与企业的具体关联尚待核实。"),
            "；".join(critical) or "关键条件尚未核清", row["主截止"], row["各办理节点"], row["判断"],
            review_display(row["人工跟进状态"]), row["人工备注"], event["reviewed_at"] if event else "",
            row["原文"], row["原文已核"], row["平台来源"], material_text(entries), row["首次发现"],
            row["最近看到"], row["下一动作"], row["编号"]]))
        human_rows.append(human)
        check = screening.get(display.get(row["编号"], ""), {})
        human.update({"人工筛选复核": check.get("status", "尚未进入复核池"),
                      "筛选复核人": check.get("reviewer", ""), "筛选复核时间": check.get("reviewed_at", ""),
                      "关联复核编号": "、".join(check.get("case_ids", []))})
        cards[row["编号"]] = (human, entries, critical)
    awaiting = [r for r in human_rows if r["人员决定"] in {"待人员判断", "先补资料"}
                and r["初筛情况"] not in {"历史参考", "已过截止", "不符"}]
    # Participation is a human decision: expiry changes what to follow, not that decision.
    participating = [r for r in human_rows if r["人员决定"] == "参与"]
    paths = {"policy_ledger": str(folder / "政策台账.csv"), "tender_ledger": str(folder / "招标台账.csv"),
             "awaiting_decision": str(folder / "待人员判断.csv"), "participating": str(folder / "已决定参与.csv"),
             "chat_material": str(folder / "对话汇报素材.md")}
    write_human_csv(paths["policy_ledger"], [r for r in human_rows if r["类型"] == "政策"])
    write_human_csv(paths["tender_ledger"], [r for r in human_rows if r["类型"] == "招标"])
    write_human_csv(paths["awaiting_decision"], awaiting)
    write_human_csv(paths["participating"], participating)
    selected = set(changes["new"] + changes["changed"] + changes.get("unverified_updates", [])) | {a["record_id"] for a in alerts}
    eligible = {r["内部记录编号"] for r in awaiting + participating}
    lines = ["# 政策与招标机会", "", f"评估时间：{checked}。以下按最近一次采集的新增、变化和临期事项选出，不代表全网完整扫描。",
             "由人员选择“参与／先补资料／不参与”。初筛和线索入库都不会自动决定参与。", ""]
    current_kind = None
    for record_id in sorted(cards, key=lambda rid: (0 if cards[rid][0]["类型"] == "政策" else 1, cards[rid][0]["编号"])):
        if record_id not in selected or record_id not in eligible:
            continue
        row, entries, critical = cards[record_id]
        if current_kind != row["类型"]:
            current_kind = row["类型"]
            lines += [f"# {current_kind}", ""]
        lines += [f"## {row['编号']} {row['标题']}", "", row["一句话解读"], "",
                  "与企业关联：" + row["企业关联"], "",
                  f"- 初筛：{row['初筛情况']}；人工筛选复核：{row['人工筛选复核']}；人员决定：{row['人员决定']}。",
                  f"- 主要截止：{row['主要截止'] or '待核'}。"]
        if row["正式原文"]:
            lines.append(f"- [正式原文]({row['正式原文']})；原文已核：{row['原文核验']}。")
        else:
            lines.append("- 正式原文：尚未取得。")
        if row["发现或转发来源"] and row["发现或转发来源"] != row["正式原文"]:
            lines.append(f"- [发现或官方转发页面]({row['发现或转发来源']})。")
        lines.append("- 关键条件：" + ("；".join(critical) if critical else "尚未核清，先看原文和资料") + "。")
        if not entries:
            lines.append("- 下载资料：尚未核得。")
        for entry in entries:
            links = ([f"[{entry['source_label']}]({entry['url']})"] if entry["url"] else []) + ([f"[本地文件](<{entry['local_path']}>)"] if entry["local_path"] else [])
            lines.append(f"- 资料：{entry['title']}；{entry['status']}。" + "、".join(links))
        if row["人员决定"] == "参与" and row["初筛情况"] in {"历史参考", "已过截止"}:
            lines.append("- 本事项保留用于追踪已办事项和结果，不作为新的申报或投标机会。")
        lines += ["- 人员反馈：请用事项编号回复“参与／先补资料／不参与”，可附原因。", ""]
    if not selected.intersection(eligible):
        lines += ["本次已记录范围内没有需要新展示的候选、变化或临期事项；完整存量见两个长期台账。", ""]
    Path(paths["chat_material"]).write_text("\n".join(lines), "utf-8")
    return paths, {"awaiting_decision": len(awaiting), "participating": len(participating)}


def project_key(item):
    if item["kind"] != "tender" or not item.get("project_id"):
        return ""
    return " / ".join(str(item.get(k) or "未标") for k in ("issuer", "project_id", "lot", "round"))


def all_deadlines(item):
    nodes = [dict(node, completed=node.get("completed")) for node in item.get("deadlines", [])]
    primary = item.get("deadline_at")
    if primary and not any(node.get("at") == primary for node in nodes):
        nodes.append({"label": "主响应或办理截止", "at": primary, "completed": None})
    return nodes


def node_alerts(todo, profile, checked):
    configured = {int(x) for x in profile.get("reminder_days", [7, 3, 1]) if isinstance(x, (int, float)) and x >= 0}
    horizon = max(configured, default=7)
    as_of = moment(checked)
    alerts = []
    for row in todo:
        seen = set()
        for node in json.loads(row["各办理节点"]):
            if not node.get("at") or node.get("completed") is True:
                continue
            due = moment(node["at"])
            days = (due.date() - as_of.date()).days
            key = (node.get("label", "办理节点"), node["at"])
            if key in seen or due < as_of or not 0 <= days <= horizon:
                continue
            seen.add(key)
            alerts.append({"record_id": row["编号"], "title": row["标题"], "label": key[0], "at": node["at"],
                           "completed": node.get("completed"), "days": days, "configured_reminder": days in configured,
                           "next_action": row["下一动作"]})
    return horizon, sorted(alerts, key=lambda x: (moment(x["at"]), x["record_id"], x["label"]))


def export_files(db, profile, folder, as_of=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    latest = db.execute("SELECT * FROM runs ORDER BY checked_at DESC, rowid DESC LIMIT 1").fetchone()
    checked = as_of or datetime.now(TZ).isoformat(timespec="seconds")
    coverage = json.loads(latest["coverage"]) if latest else []
    changes = json.loads(latest["changes"]) if latest else {"new": [], "changed": [], "unchanged": [], "unverified_updates": []}
    rows = []
    for record in db.execute("SELECT * FROM records ORDER BY first_seen DESC,id"):
        item = json.loads(record["data"])
        match = evaluate(item, profile, checked)
        if record["pending_source_update"] and match["decision"] not in {"历史参考", "已过截止", "不符"}:
            match["decision"] = "待核"
            match["reasons"].append("有较低信息来源的新提示待复核；未核清前不据旧正文推进")
        rows.append(dict(zip(FIELDS, [record["id"], "政策" if item["kind"] == "policy" else "招标", item["title"], item.get("summary", ""), item.get("relevance_reason", ""),
            item.get("issuer", ""), item.get("stage", "unknown"), match["decision"], "；".join(match["reasons"]),
            match["candidate_score"], "、".join(match["keyword_hits"]), "、".join(item.get("regions", [])),
            item.get("document_number") or item.get("project_id") or "", item.get("lot", ""), item.get("round", ""),
            item.get("deadline_at", ""), match["deadline_days"], json_text(all_deadlines(item)), "是" if item.get("original_verified") else "否",
            item.get("original_url", ""), item["source_url"], project_key(item), record["version"], record["first_seen"],
            record["last_seen"], "是" if record["pending_source_update"] else "否", record["review_state"], record["review_note"], item.get("next_action", ""), item.get("evidence_path", "") ])))
    todo = [r for r in rows if r["判断"] not in {"历史参考", "已过截止", "不符"} and r["人工跟进状态"] not in {"结束", "暂不推进", "不参与"}]
    paths = {"master": str(folder / "政策招标总台账.csv"), "todo": str(folder / "当前待办.csv"),
             "brief": str(folder / "每日简报.md"), "changes": str(folder / "本次变化.json")}
    write_csv(paths["master"], rows)
    write_csv(paths["todo"], todo)
    incomplete = [s for s in coverage if s["status"] != "success"]
    reminder_horizon, alerts = node_alerts(todo, profile, checked)
    human_paths, human_counts = export_human_views(db, rows, folder, changes, alerts, checked)
    paths.update(human_paths)
    change_rows = [r for r in rows if r["编号"] in changes["new"] + changes["changed"]]
    lines = ["# 政策招标手动试跑简报", "", f"企业：{profile.get('company', {}).get('name', '待确认')}；画像版本：{profile.get('version', '未标版本')}",
             f"评估时间：{checked}；最近采集：{latest['checked_at'] if latest else '尚未采集'}", "",
             f"本次新增 {len(changes['new'])} 条，变化 {len(changes['changed'])} 条，未变化 {len(changes['unchanged'])} 条；累计 {len(rows)} 条。",
             "新增为首次入库，不等于当天新发布；本次为手动采集，非全网完整扫描。",
             "候选相关度只统计检索词命中，不能作为资格分或可投结论。", "", "## 本次采集覆盖"]
    if not coverage:
        lines.append("未提供采集覆盖记录，无法认定已完成监测。")
    for source in coverage:
        queries = "、".join(str(x) for x in source.get("queries", []))
        lines.append(f"- {source['source_id']}：{COVERAGE[source['status']]}；查询：{queries or '未记录'}；{source.get('note', '')}")
    if incomplete:
        lines += ["", "存在未完整采集的来源；本次无新增不能解释为这些来源没有新信息。"]
    lines += ["", "## 新增与变化"]
    for row in change_rows:
        lines += [f"- {row['编号']}｜{row['类型']}｜{row['标题']}：{row['判断']}。{row['判断依据']}",
                  f"  画像匹配理由：{row['匹配理由'] or '尚未记录，需补核企业业务与事项的关联'}",
                  f"  下一步：{row['下一动作'] or '补核条件和原文'}；来源：{row['原文'] or row['平台来源']}"]
    if not change_rows:
        lines.append("本次已记录范围内无新增或内容变化；是否完成覆盖见上节。")
    if changes.get("unverified_updates"):
        lines += ["", "## 较低信息来源提示待核更新", "下列来源观察未覆盖已核的较完整正文，原有正式内容与判断已保留。须核正式原文再判断是否更新："]
        lines.extend("- " + record_id for record_id in changes["unverified_updates"])
    lines += ["", f"## {reminder_horizon}日内办理节点"]
    for node in alerts:
        completed = "未完成" if node["completed"] is False else "是否完成待核"
        mark = "设定提醒日" if node["configured_reminder"] else "临期跟踪"
        lines.append(f"- {node['title']}｜{node['label']}：{node['at']}，剩余 {node['days']} 天；{completed}；{mark}。{node['next_action']}")
    if not alerts:
        lines.append(f"已登记待办中没有{reminder_horizon}日内尚未明确完成的已知办理节点；未知日期仍需补核。")
    lines += ["", "## 人工跟进", f"当前待办 {len(todo)} 条。人工跟进状态与自动判断分列，人工备注会保留；申请、投标和对外联系均未自动执行。"]
    Path(paths["brief"]).write_text("\n".join(lines) + "\n", "utf-8")
    Path(paths["changes"]).write_text(json.dumps({"run_id": latest["run_id"] if latest else None, "checked_at": checked,
        "changes": changes, "coverage_incomplete": incomplete, "deadline_alerts": alerts,
        "deadline_alert_ids": sorted({node["record_id"] for node in alerts})}, ensure_ascii=False, indent=2), "utf-8")
    return {"files": paths, "counts": {"total": len(rows), "todo": len(todo), "new": len(changes["new"]), **human_counts,
        "changed": len(changes["changed"]), "unchanged": len(changes["unchanged"]),
        "unverified_updates": len(changes.get("unverified_updates", [])), "incomplete_sources": len(incomplete)}}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "export", "review"):
        command = commands.add_parser(name)
        command.add_argument("--db", default="data/monitor.sqlite")
        if name != "review":
            command.add_argument("--profile", required=True)
            command.add_argument("--out", default="exports")
            command.add_argument("--as-of", help="Optional ISO time; defaults to checked_at for run, current time for export")
        if name == "run":
            command.add_argument("--input", required=True)
        if name == "review":
            command.add_argument("--id", required=True)
            command.add_argument("--state", required=True, choices=sorted(REVIEW_STATES))
            command.add_argument("--note", required=True)
    args = parser.parse_args(argv)
    db = connect(args.db)
    try:
        if args.command == "review":
            result = save_review(db, args.id, args.state, args.note)
        else:
            profile = read_json(args.profile)
            if args.command == "run":
                payload = read_json(args.input)
                ingest(db, payload, profile)
                as_of = args.as_of or payload["checked_at"]
            else:
                as_of = args.as_of
            result = export_files(db, profile, args.out, as_of)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
