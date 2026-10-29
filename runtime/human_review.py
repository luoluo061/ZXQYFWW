"""Local screening audit, separate from opportunity participation and web collection.

Only imports evidence actually observed by the collector. Human reviews are append-only
events for a specific screening version; they never change monitor.sqlite.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TZ = timezone(timedelta(hours=8))
KINDS = {"policy", "tender"}
BUCKETS = {"推荐", "待核", "暂不推荐", "历史信息"}
OUTCOMES = {"恢复候选", "维持暂不推荐", "先补证据", "仅作储备"}
READING_LEVELS = {"标题", "摘要", "平台详情", "原文"}
READING_RANK = {"标题": 0, "摘要": 1, "平台详情": 2, "原文": 3}
REASONS = {"业务", "地区", "主体", "期限", "公告性质", "重复", "未知", "相关", "其他"}
STATUSES = {"success", "partial", "failed", "auth_required"}


def text_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(text_json(value).encode("utf-8")).hexdigest()


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def timestamp(value):
    if not isinstance(value, str) or not re.search(r"(?:Z|[+-]\d\d:\d\d)$", value) or "T" not in value:
        raise ValueError("时间须为含时区的ISO日期时间")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.astimezone(TZ).isoformat(timespec="seconds")


def date_or_timestamp(value):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
        datetime.strptime(value, "%Y-%m-%d")
        return value
    return timestamp(value)


def business_url(value, required=False):
    if not value and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError("来源须为http/https业务链接")
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
        raise ValueError("来源须为不含账号密码的完整http/https业务链接")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if any(k.casefold() in {"token", "access_token", "refresh_token", "password", "authorization", "sessionid", "session_id", "cookie"} for k, _ in pairs):
        raise ValueError("不保存含会话凭据的链接")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                       urlencode(sorted((k, v) for k, v in pairs if not k.lower().startswith("utm_"))), ""))


def required_text(item, field):
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("缺少文本字段：" + field)
    return value.strip()


def validate_observation(item, checked_at):
    if not isinstance(item, dict):
        raise ValueError("observation须为对象")
    result = dict(item)
    for field in ("kind", "source_id", "title", "machine_bucket", "machine_reason", "reason_code", "reading_level"):
        result[field] = required_text(item, field)
    if result["kind"] not in KINDS or result["machine_bucket"] not in BUCKETS:
        raise ValueError("未知的kind或machine_bucket")
    if result["reason_code"] not in REASONS or result["reading_level"] not in READING_LEVELS:
        raise ValueError("未知的reason_code或reading_level")
    result["list_url"] = business_url(item.get("list_url"), required=True)
    for field in ("detail_url", "original_url", "platform_url"):
        result[field] = business_url(item.get(field))
    if not isinstance(item.get("original_verified", False), bool):
        raise ValueError("original_verified须为布尔值")
    result["original_verified"] = item.get("original_verified", False)
    if result["original_verified"] and (not result["original_url"] or result["reading_level"] != "原文"):
        raise ValueError("原文已核须有原文URL且实际阅读层级为原文")
    result["observed_at"] = timestamp(item.get("observed_at") or checked_at)
    if item.get("published_at"):
        result["published_at"] = datetime.strptime(item["published_at"], "%Y-%m-%d").strftime("%Y-%m-%d")
    if item.get("deadline_at"):
        result["deadline_at"] = date_or_timestamp(item["deadline_at"])
    if item.get("opportunity_id") and not re.fullmatch(r"[PT]\d{3,}", str(item["opportunity_id"])):
        raise ValueError("opportunity_id须为P/T稳定编号")
    conditions = item.get("condition_evidence", [])
    if not isinstance(conditions, (str, list)):
        raise ValueError("condition_evidence须为文本或条件列表")
    unknown = result["reason_code"] == "未知"
    if isinstance(conditions, list):
        for condition in conditions:
            if not isinstance(condition, dict) or condition.get("status", "unknown") not in {"pass", "fail", "unknown"}:
                raise ValueError("条件证据的status须为pass/fail/unknown")
            unknown |= condition.get("status", "unknown") == "unknown"
            if condition.get("status") == "fail" and not condition.get("evidence"):
                raise ValueError("已证不符必须留证据；缺资料须unknown")
    if item.get("conclusion_strength") == "已证不符":
        if unknown or not result["original_verified"]:
            raise ValueError("未知条件或未核原文不得写已证不符")
    if unknown and re.search(r"已证不符|确定不符|明确不符", result["machine_reason"]):
        raise ValueError("unknown不能写成已证不符")
    return result


def identity(item):
    scope = [item["kind"], str(item.get("lot") or ""), str(item.get("round") or "")]
    number = item.get("document_number") if item["kind"] == "policy" else item.get("project_id")
    if number:
        return digest(scope + ["number", str(item.get("issuer") or ""), str(number)])
    detail = item.get("detail_url") or item.get("original_url")
    if detail and not item.get("detail_is_list", False) and not item.get("original_is_list", False):
        return digest(scope + ["detail", detail])
    # A list URL alone is never an opportunity identity.
    return digest(scope + ["title", re.sub(r"\s+", "", item["title"]).casefold(),
                           str(item.get("issuer") or ""), str(item.get("published_at") or "")])


def critical_fingerprint(item):
    # Collection timestamps, paths, page positions and queries do not invalidate a review.
    fields = ("kind", "title", "issuer", "published_at", "document_number", "project_id", "lot", "round",
              "detail_url", "original_url", "original_verified", "reading_level", "machine_bucket", "machine_reason",
              "reason_code", "condition_evidence", "conclusion_strength", "deadline_at", "source_content_sha256",
              "content_sha256", "source_quote", "body_text", "stage")
    return digest({field: item.get(field) for field in fields})


def lower_information(old, item):
    """A directory observation must not replace a previously read full notice."""
    if old.get("original_verified") and not item.get("original_verified"):
        return True
    old_rank = READING_RANK.get(old.get("reading_level"), 0)
    new_rank = READING_RANK.get(item.get("reading_level"), 0)
    if new_rank < old_rank:
        return True
    return new_rank <= old_rank and any(old.get(field) and not item.get(field)
                                       for field in ("body_text", "condition_evidence"))


def important_lower_hints(old, item):
    """Keep explicit new facts as unverified hints, not as replacement notice facts.

    Missing fields and unknown conditions are absence of information. Text hashes
    from different reading layers are not comparable content changes.
    """
    hints = {}
    for field in ("title", "deadline_at", "document_number", "project_id", "lot", "round", "stage"):
        value = item.get(field)
        if value in (None, "", "unknown"):
            continue
        normalize = lambda value: re.sub(r"\s+", " ", str(value or "")).strip()
        if normalize(value) != normalize(old.get(field)):
            hints[field] = value
    old_conditions = {str(c.get("label") or ""): c for c in old.get("condition_evidence", []) if isinstance(c, dict)} if isinstance(old.get("condition_evidence"), list) else {}
    for condition in item.get("condition_evidence", []) if isinstance(item.get("condition_evidence"), list) else []:
        if condition.get("status", "unknown") == "unknown" or not condition.get("evidence"):
            continue
        previous = old_conditions.get(str(condition.get("label") or ""))
        if previous != condition:
            hints.setdefault("condition_evidence", []).append(condition)
    for field in ("source_change_hint", "update_hint"):
        if item.get(field):
            hints[field] = item[field]
    return hints


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
      PRAGMA foreign_keys=ON;
      CREATE TABLE IF NOT EXISTS screening_runs (
        run_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL, scope TEXT NOT NULL,
        payload_hash TEXT NOT NULL, coverage TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS screening_cases (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, identity_key TEXT UNIQUE NOT NULL,
        kind TEXT NOT NULL, version INTEGER NOT NULL, fingerprint TEXT NOT NULL,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, current_data TEXT NOT NULL,
        pending_source_update INTEGER NOT NULL DEFAULT 0, pending_source_hint TEXT NOT NULL DEFAULT '');
      CREATE TABLE IF NOT EXISTS screening_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES screening_runs(run_id),
        case_seq INTEGER NOT NULL REFERENCES screening_cases(seq), case_version INTEGER NOT NULL,
        observed_at TEXT NOT NULL, data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS screening_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT, case_seq INTEGER NOT NULL REFERENCES screening_cases(seq),
        case_version INTEGER NOT NULL, reviewer TEXT NOT NULL, outcome TEXT NOT NULL,
        note TEXT NOT NULL, evidence_url TEXT NOT NULL, reviewed_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS independent_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, reviewer TEXT NOT NULL,
        url TEXT NOT NULL, title TEXT NOT NULL, note TEXT NOT NULL, read_at TEXT NOT NULL,
        reading_level TEXT NOT NULL DEFAULT '原文');
      CREATE TRIGGER IF NOT EXISTS observations_no_update BEFORE UPDATE ON screening_observations
        BEGIN SELECT RAISE(ABORT,'来源观察不可改写'); END;
      CREATE TRIGGER IF NOT EXISTS observations_no_delete BEFORE DELETE ON screening_observations
        BEGIN SELECT RAISE(ABORT,'来源观察不可删除'); END;
      CREATE TRIGGER IF NOT EXISTS reviews_no_update BEFORE UPDATE ON screening_reviews
        BEGIN SELECT RAISE(ABORT,'人工复核事件不可改写'); END;
      CREATE TRIGGER IF NOT EXISTS reviews_no_delete BEFORE DELETE ON screening_reviews
        BEGIN SELECT RAISE(ABORT,'人工复核事件不可删除'); END;
    """)
    if "reading_level" not in {row[1] for row in db.execute("PRAGMA table_info(independent_readings)")}:
        db.execute("ALTER TABLE independent_readings ADD COLUMN reading_level TEXT NOT NULL DEFAULT '原文'")
        db.commit()
    case_columns = {row[1] for row in db.execute("PRAGMA table_info(screening_cases)")}
    for field, declaration in [("pending_source_update", "INTEGER NOT NULL DEFAULT 0"),
                               ("pending_source_hint", "TEXT NOT NULL DEFAULT ''")]:
        if field not in case_columns:
            db.execute(f"ALTER TABLE screening_cases ADD COLUMN {field} {declaration}")
    db.commit()
    return db


def case_id(seq):
    return f"R{seq:05d}"


def ingest(db, payload):
    run_id = required_text(payload, "run_id")
    checked_at = timestamp(payload.get("checked_at"))
    scope = payload.get("scope")
    if scope not in KINDS | {"both"}:
        raise ValueError("scope须为policy/tender/both")
    coverage = payload.get("coverage")
    if not isinstance(coverage, list):
        raise ValueError("须保存本轮coverage")
    for source in coverage:
        required_text(source, "source_id")
        if source.get("status") not in STATUSES:
            raise ValueError("coverage状态无效")
    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise ValueError("observations须为数组；无结果也显式保存空数组及覆盖缺口")
    observations = [validate_observation(item, checked_at) for item in observations]
    if any(item["kind"] != scope for item in observations) and scope != "both":
        raise ValueError("输入事项超出本轮scope")
    if any(item["source_id"] not in {s["source_id"] for s in coverage} for item in observations):
        raise ValueError("每条观察的source_id须在coverage中")
    payload_hash = digest(payload)
    prior = db.execute("SELECT payload_hash FROM screening_runs WHERE run_id=?", (run_id,)).fetchone()
    if prior:
        if prior[0] != payload_hash:
            raise ValueError("run_id已存在且内容不同，不得覆盖历史观察")
        return {"run_id": run_id, "idempotent": True, "observations": 0}
    new, changed, seen, retained_lower, unverified_updates = [], [], [], [], []
    with db:
        db.execute("INSERT INTO screening_runs VALUES(?,?,?,?,?)", (run_id, checked_at, scope, payload_hash, text_json(coverage)))
        for item in observations:
            key, fingerprint = identity(item), critical_fingerprint(item)
            row = db.execute("SELECT * FROM screening_cases WHERE identity_key=?", (key,)).fetchone()
            if row is None:
                cursor = db.execute("INSERT INTO screening_cases(identity_key,kind,version,fingerprint,first_seen,last_seen,current_data) VALUES(?,?,1,?,?,?,?)",
                                    (key, item["kind"], fingerprint, item["observed_at"], item["observed_at"], text_json(item)))
                seq, version = cursor.lastrowid, 1
                new.append(case_id(seq))
            else:
                seq, version = row["seq"], row["version"]
                # An older backfill is evidence, not a replacement of newer screening.
                if item["observed_at"] >= row["last_seen"]:
                    previous = json.loads(row["current_data"])
                    if lower_information(previous, item):
                        retained_lower.append(case_id(seq))
                        hints = important_lower_hints(previous, item)
                        if hints:
                            unverified_updates.append(case_id(seq))
                            db.execute("UPDATE screening_cases SET last_seen=?,pending_source_update=1,pending_source_hint=? WHERE seq=?",
                                       (item["observed_at"], text_json(hints), seq))
                        else:
                            db.execute("UPDATE screening_cases SET last_seen=? WHERE seq=?", (item["observed_at"], seq))
                    else:
                        # Recompute both sides so a fingerprint algorithm extension
                        # does not itself manufacture a new source version.
                        if fingerprint != critical_fingerprint(previous):
                            version += 1
                            changed.append(case_id(seq))
                        db.execute("UPDATE screening_cases SET version=?,fingerprint=?,last_seen=?,current_data=?,pending_source_update=0,pending_source_hint='' WHERE seq=?",
                                   (version, fingerprint, item["observed_at"], text_json(item), seq))
            db.execute("INSERT INTO screening_observations(run_id,case_seq,case_version,observed_at,data) VALUES(?,?,?,?,?)",
                       (run_id, seq, version, item["observed_at"], text_json(item)))
            seen.append(case_id(seq))
    return {"run_id": run_id, "new": sorted(set(new)), "changed": sorted(set(changed)), "observed_ids": sorted(set(seen)),
            "retained_lower_information": sorted(set(retained_lower)), "unverified_updates": sorted(set(unverified_updates)), "observations": len(observations)}


def review(db, record_id, reviewer, outcome, note, evidence_url, reviewed_at=None):
    """Record an explicit human reply only; packet creation is not a human reply."""
    if not re.fullmatch(r"R\d{5,}", str(record_id)):
        raise ValueError("须使用R稳定编号")
    if outcome not in OUTCOMES or not str(reviewer).strip() or not str(note).strip():
        raise ValueError("复核须提供审核人、有效结果及事实依据")
    evidence_url = business_url(evidence_url, required=True)
    reviewed_at = timestamp(reviewed_at or now())
    row = db.execute("SELECT * FROM screening_cases WHERE seq=?", (int(record_id[1:]),)).fetchone()
    if row is None:
        raise ValueError("未找到复核编号")
    if reviewed_at < row["last_seen"]:
        raise ValueError("复核时间不得早于当前来源观察；旧材料不能确认新版本")
    with db:
        db.execute("INSERT INTO screening_reviews(case_seq,case_version,reviewer,outcome,note,evidence_url,reviewed_at) VALUES(?,?,?,?,?,?,?)",
                   (row["seq"], row["version"], str(reviewer).strip(), outcome, str(note).strip(), evidence_url, reviewed_at))
    return {"id": record_id, "reviewed_version": row["version"], "outcome": outcome, "participation_changed": False}


def independent(db, kind, reviewer, url, title, note, read_at=None, reading_level="原文"):
    """Record actual human reading, never inferred from a generated recommendation."""
    if kind not in KINDS or not str(reviewer).strip() or not str(title).strip() or not str(note).strip():
        raise ValueError("主动查阅须提供类型、审核人、标题及真实阅读结论")
    url = business_url(url, required=True)
    if reading_level not in READING_LEVELS:
        raise ValueError("主动查阅阅读层级须为标题/摘要/平台详情/原文")
    read_at = timestamp(read_at or now())
    with db:
        cursor = db.execute("INSERT INTO independent_readings(kind,reviewer,url,title,note,read_at,reading_level) VALUES(?,?,?,?,?,?,?)",
                            (kind, str(reviewer).strip(), url, str(title).strip(), str(note).strip(), read_at, reading_level))
    return {"reading_id": cursor.lastrowid, "read_at": read_at, "participation_changed": False}


def csv_text(value):
    value = "" if value is None else str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


POOL_FIELDS = ["复核编号", "类型", "标题", "机器分流", "机器筛选理由", "理由类别", "实际阅读层级", "原文核验", "正式原文", "详情链接", "列表来源", "来源页码或检索词", "企业条件证据", "当前版本", "人工审核状态", "人工审核结果", "审核人", "审核时间", "审核依据", "审核引用", "关联机会编号", "主要截止", "首次观察", "最近观察", "证据路径"]
READ_FIELDS = ["记录编号", "类型", "查阅人", "原文标题", "原文链接", "阅读时间", "阅读结论", "实际阅读层级"]


def write_csv(path, fields, rows):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: csv_text(row.get(field, "")) for field in fields} for row in rows)


def pool_rows(db):
    result = []
    for row in db.execute("SELECT * FROM screening_cases ORDER BY seq"):
        item = json.loads(row["current_data"])
        event = db.execute("SELECT * FROM screening_reviews WHERE case_seq=? ORDER BY id DESC LIMIT 1", (row["seq"],)).fetchone()
        state = "待人工复核" if not event else "已复核" if event["case_version"] == row["version"] else "待重新复核"
        pending_hint = row["pending_source_hint"] if "pending_source_hint" in row.keys() else ""
        if "pending_source_update" in row.keys() and row["pending_source_update"]:
            state = "待重新复核"
        result.append(dict(zip(POOL_FIELDS, [case_id(row["seq"]), "政策" if row["kind"] == "policy" else "招标", item["title"], item["machine_bucket"], item["machine_reason"], item["reason_code"], item["reading_level"], "已核正式原文" if item["original_verified"] else "未核正式原文", item.get("original_url", ""), item.get("detail_url", ""), item["list_url"], " / ".join(str(item.get(k) or "") for k in ("page", "query")), text_json(item.get("condition_evidence", [])), row["version"], state, event["outcome"] if event else "", event["reviewer"] if event else "", event["reviewed_at"] if event else "", event["note"] if event else "", event["evidence_url"] if event else "", item.get("opportunity_id", ""), item.get("deadline_at", ""), row["first_seen"], row["last_seen"], item.get("evidence_path", "")])) )
        # Navigation metadata is exposed to Markdown only; existing CSV columns stay compatible.
        result[-1]["平台页面"] = item.get("platform_url", "")
        if pending_hint:
            result[-1]["机器筛选理由"] += "；低信息来源重要提示待重核（保留已核正文）：" + pending_hint
    return result


def opportunity_status(db):
    """Read-only P/T linkage for monitor exports; this is not a participation state."""
    grouped = {}
    for row in pool_rows(db):
        if row["关联机会编号"]:
            grouped.setdefault(row["关联机会编号"], []).append(row)
    result = {}
    for opportunity, rows in grouped.items():
        pending = [row for row in rows if row["人工审核状态"] != "已复核"]
        latest = max(rows, key=lambda row: row["审核时间"] or "")
        result[opportunity] = {
            "status": "待重新复核" if any(row["人工审核状态"] == "待重新复核" for row in pending) else "待人工复核" if pending else "已复核",
            "reviewer": latest["审核人"], "reviewed_at": latest["审核时间"],
            "case_ids": [row["复核编号"] for row in rows],
            "outcomes": [row["人工审核结果"] for row in rows if row["人工审核结果"]],
        }
    return result


def selection(rows, day):
    pending = [row for row in rows if row["人工审核状态"] != "已复核" and row["机器分流"] != "推荐"]
    def risk(row):
        return (0 if row["人工审核状态"] == "待重新复核" else 1,
                0 if row["关联机会编号"] else 1,
                0 if row["机器分流"] == "待核" or row["理由类别"] in {"未知", "主体", "地区", "期限"} else 1,
                row["主要截止"] or "9999",
                0 if row["原文核验"] == "未核正式原文" else 1, row["复核编号"])
    risk_pool = [row for row in pending if row["人工审核状态"] == "待重新复核"
                 or row["机器分流"] == "待核" or row["理由类别"] in {"未知", "主体", "地区", "期限"}]
    priority = sorted(risk_pool, key=risk)[:2]
    if len(priority) < 2:
        others = [row for row in pending if row not in priority]
        priority += sorted(others, key=risk)[:2 - len(priority)]
    remaining = [row for row in pending if row not in priority]
    low_relevance = [row for row in remaining if row["机器分流"] in {"暂不推荐", "历史信息"}
                     and row["理由类别"] not in {"未知", "主体", "地区", "期限"}]
    # Prefer a different exclusion class for the third sample. The fallback avoids
    # inventing an unavailable low-relevance item in a small pool.
    rotating = sorted(low_relevance or remaining, key=lambda row: digest([day, row["复核编号"]]))[:1]
    return priority + rotating


def independent_entries(kind):
    """Previously verified public entry points; listing them does not claim a visit."""
    if kind == "政策":
        return [
            "人工主入口（本包仅提供链接，未代人员阅读）：[中国中小企业服务网政策库](https://www.chinasme.cn/policy)；[申报项目库](https://zcxm.chinasme.cn/policy)。可在服务网自主浏览、打开重点页，同步对照下方机器分析，不要求先单独看官网。",
            "原文与附件核验入口（云南/昆明地区示例，须先由企业画像确认适用，否则替换本地区主管部门）：[云南省科技厅通知公告](https://kjt.yn.gov.cn/xwdt/tzgg/)；[云南省工信厅通知公告](https://gxt.yn.gov.cn/zfxxgk/fdzdgknr/tzgg/)；[昆明市科技局](https://kjj.km.gov.cn/)。涉及实际申报条件、期限或参与决定时核正式原文及适用附件；平台详情不能冒充正式原文。",
        ]
    return [
        "人工主入口（本包仅提供链接，未代人员阅读）：[中小企业服务网找订单：软件开发](https://chsme.qibiaoduo.com/result?key=project&bValue=&value=%E8%BD%AF%E4%BB%B6%E5%BC%80%E5%8F%91)。可边浏览边看机器分析；这是发现入口，涉及参与判断须进入采购人或法定发布平台核正式原文。",
        "招标主动查阅可轮换检索词：软件开发、系统建设、系统升级、系统集成、平台开发；先核画像已确认的真实业务，再核企业可履约地区、驻场及成本，纯硬件或单纯许可供货保留筛选理由。",
    ]


def is_service_url(url):
    host = (urlsplit(str(url or "")).hostname or "").casefold()
    return host == "chinasme.cn" or host.endswith(".chinasme.cn")


def md_line(row):
    title = re.sub(r"\s+", " ", row["标题"])
    platform = row.get("平台页面") or (row["详情链接"] if is_service_url(row["详情链接"]) else "")
    if is_service_url(platform):
        links = [f"[服务网页面]({platform})"]
    else:
        entry = row["列表来源"] if is_service_url(row["列表来源"]) else "https://www.chinasme.cn/policy" if row["类型"] == "政策" else "https://chsme.qibiaoduo.com/result?key=project&bValue=&value=%E8%BD%AF%E4%BB%B6%E5%BC%80%E5%8F%91"
        links = [f"[服务网列表：按上述完整标题定位（详情未定位）]({entry})"]
    original = row["正式原文"]
    if original:
        label = "正式原文" if row["原文核验"] == "已核正式原文" else "待核原文链接"
        links.append(f"[{label}]({original})")
    elif row["详情链接"] and not is_service_url(row["详情链接"]):
        links.append(f"[来源详情（尚未核正式原文）]({row['详情链接']})")
    if row["列表来源"] and not is_service_url(row["列表来源"]):
        links.append(f"[发现列表（并非原文）]({row['列表来源']})")
    return f"- {row['复核编号']}｜{title}｜{row['机器分流']}；{row['机器筛选理由']}；阅读到{row['实际阅读层级']}。{'；'.join(links)}；{row['人工审核状态']}；关联机会：{row['关联机会编号'] or '尚未转为机会'}。"


def export(db, out, as_of=None):
    checked_at = timestamp(as_of or now())
    day = checked_at[:10]
    folder = Path(out)
    folder.mkdir(parents=True, exist_ok=True)
    rows = pool_rows(db)
    readings = [dict(zip(READ_FIELDS, [f"I{row['id']:05d}", "政策" if row["kind"] == "policy" else "招标", row["reviewer"], row["title"], row["url"], row["read_at"], row["note"], row["reading_level"]])) for row in db.execute("SELECT * FROM independent_readings ORDER BY read_at DESC,id DESC")]
    paths = {}
    for kind, name in [("政策", "policy_pool"), ("招标", "tender_pool")]:
        target = folder / f"{kind}人工复核池.csv"
        write_csv(target, POOL_FIELDS, [row for row in rows if row["类型"] == kind])
        paths[name] = str(target)
    target = folder / "每日人工查阅记录.csv"
    write_csv(target, READ_FIELDS, readings)
    paths["independent_readings"] = str(target)
    lines = ["# 每日人工复核包", "", f"生成时间：{checked_at}。这里依据已留存观察安排复核，未重新访问网站；不等于全量原始公告库。", "", "直接打开中国中小企业服务网，可先打开下列选取的2—3条重点核查页面，边浏览网站边对照机器分析。人员可随时自主选择其他政策与未推荐内容，不要求先脱离机器另读官网。机器暂不推荐保留原始出处；没有推荐不能代替人员审查。", "人工复核结果与参与决定分开：恢复候选只回到机会判断，不表示参与；是否参与仍须另用P/T编号明确回复。", "复核状态按来源与机器筛选版本保存。已审核且版本未变的事项不反复抽查；新证据或关键理由变化须重新复核。", ""]
    samples = {}
    for kind in ("政策", "招标"):
        scoped = [row for row in rows if row["类型"] == kind]
        today_readings = [row for row in readings if row["类型"] == kind and row["阅读时间"][:10] == day]
        recommended = sorted([row for row in scoped if row["机器分流"] == "推荐" and row["人工审核状态"] != "已复核"], key=lambda row: (row["主要截止"] or "9999", row["复核编号"]))
        chosen = selection(scoped, day)
        samples[kind] = [row["复核编号"] for row in chosen]
        platform_count = len({row["原文链接"] for row in today_readings if row["实际阅读层级"] == "平台详情"})
        original_count = len({row["原文链接"] for row in today_readings if row["实际阅读层级"] == "原文"})
        lines += [f"## {kind}", "", f"自主查阅：建议每天在服务网浏览并细看约2条{kind}，平台详情即可，可同步查看重点分析；数量按新增与工作量调整，并保留自主选择和未推荐抽查。当前已记录平台详情{platform_count}个不同链接、正式原文{original_count}个不同链接；标题/摘要另行留痕，不充作详细阅读。这不是实际阅读次数的自动识别，没有适用新增时如实记录浏览范围，不为凑数编写阅读。", ""]
        lines += independent_entries(kind)
        focus_pool = {row["复核编号"]: row for row in recommended + chosen if row["机器分流"] != "历史信息"}
        focus = sorted(focus_pool.values(), key=lambda row: (row["主要截止"] or "9999", row["复核编号"]))[:3]
        lines += ["", f"边看边审重点：本包选取{len(focus)}条（通常2—3条，临期优先；不足按实际数量）。"]
        lines.extend(md_line(row) for row in focus)
        if not focus:
            lines.append("- 当前没有可选重点，仍可自主浏览服务网，不据此认定没有机会。")
        lines += ["", f"机器推荐待复核：{len(recommended)}条（按已知截止先后；日期待核排后）。"]
        lines.extend(md_line(row) for row in recommended)
        if not recommended:
            lines.append("- 当前留存池没有待审的新推荐；仍需主动查阅与抽查。")
        lines += ["", f"未推荐抽查：本包{len(chosen)}条，前2条按重审、已有P/T关联、待核/疑似误筛、近期截止及原文缺口排序；第3条优先从明确低相关/历史且非未知主体地区期限理由的剩余项按日期轮换，无此类再从剩余项抽取；不足3条按实际数量，不重复凑数。"]
        lines.extend(md_line(row) for row in chosen)
        if not chosen:
            lines.append("- 没有尚待复核的已留存未推荐项。可能已审完，也可能未留存；不可据此认定不存在漏筛。")
        lines += ["", "人员回话示例：R00001恢复候选：原文允许软件企业，引用条款……；R00002维持暂不推荐：地域限定……；R00003先补证据：主体条件待核。请附审核人、实际查阅链接和依据。", ""]
    recent_runs = list(db.execute("SELECT * FROM screening_runs ORDER BY checked_at DESC LIMIT 3"))
    lines += ["## 覆盖与缺口", "", "只对观察池内实际看见的条目负责。未访问来源、未翻页、登录失败及尚未进入池的内容不能当作已排除或已审核。"]
    for run in recent_runs:
        lines.append(f"- {run['run_id']}｜{run['checked_at']}｜{run['scope']}；" + "；".join(f"{c['source_id']}:{c['status']} {c.get('note', '')}" for c in json.loads(run["coverage"])))
    if not recent_runs:
        lines.append("- 尚无来源观察，不能生成真实未推荐抽查样本。")
    target = folder / "每日人工复核包.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    paths["daily_packet"] = str(target)
    return {"files": paths, "case_count": len(rows), "pending_count": sum(row["人工审核状态"] != "已复核" for row in rows), "samples": samples, "participation_changed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("ingest", "export", "review", "independent"):
        command = commands.add_parser(name)
        command.add_argument("--db", default="data/human_review.sqlite")
        if name == "ingest":
            command.add_argument("--input", required=True)
        elif name == "export":
            command.add_argument("--out", default="exports")
            command.add_argument("--as-of")
        elif name == "review":
            command.add_argument("--id", required=True)
            command.add_argument("--reviewer", required=True)
            command.add_argument("--outcome", required=True, choices=sorted(OUTCOMES))
            command.add_argument("--note", required=True)
            command.add_argument("--evidence-url", required=True)
        else:
            command.add_argument("--kind", required=True, choices=sorted(KINDS))
            command.add_argument("--reviewer", required=True)
            command.add_argument("--url", required=True)
            command.add_argument("--title", required=True)
            command.add_argument("--note", required=True)
            command.add_argument("--read-at")
            command.add_argument("--reading-level", choices=sorted(READING_LEVELS), default="原文")
    args = parser.parse_args(argv)
    db = connect(args.db)
    try:
        if args.command == "ingest":
            result = ingest(db, json.loads(Path(args.input).read_text("utf-8-sig")))
        elif args.command == "export":
            result = export(db, args.out, args.as_of)
        elif args.command == "review":
            result = review(db, args.id, args.reviewer, args.outcome, args.note, args.evidence_url)
        else:
            result = independent(db, args.kind, args.reviewer, args.url, args.title, args.note, args.read_at, args.reading_level)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, KeyError, TypeError, sqlite3.Error) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
