"""Install the local ledger into an independent workspace; never register schedules.

Only Python's standard library is required. Website acquisition remains a Codex
browser/web-tool task. This script does not visit sites, authenticate, submit,
send messages, or create automation jobs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent
MANIFEST = "data/install_manifest.json"
INSTALLATION = "data/installation.json"
NAME = "sme-opportunity-monitor"
STATIC_FILES = ("monitor.py", "human_review.py", "profile.example.json",
                "sources.example.json", "version.json", "examples/incoming.example.json",
                "examples/screening.example.json", "tests/test_monitor.py",
                "tests/test_human_review.py", "tests/test_monitor_review_integration.py")
FORBIDDEN_KEYS = {"password", "passwd", "secret", "api_key", "apikey", "access_token",
                  "refresh_token", "cookie", "cookies", "authorization", "automation_id",
                  "automation_ids", "automations", "automation_setup", "notification_receipts",
                  "thread_id", "threadid", "targetthreadid"}


def read_json(path):
    return json.loads(Path(path).read_text("utf-8-sig"))


def sha(value):
    return hashlib.sha256(value).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")


def safe_target(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("安装清单包含不安全的相对路径")
    target = root / rel
    if not target.resolve().is_relative_to(root):
        raise ValueError("工作目录中的链接指向目录外，停止写入：" + relative)
    return target


def workspace_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("--workspace 必须是独立工作目录的绝对路径")
    path = path.resolve()
    package_root = RUNTIME.parent
    if path == package_root or path in package_root.parents or path.is_relative_to(package_root):
        raise ValueError("工作数据目录须与分发源代码目录分开")
    parts = [part.casefold() for part in path.parts]
    for index, part in enumerate(parts):
        if part in {".codex", ".agents"} and index + 1 < len(parts) and parts[index + 1] in {"skills", "plugins"}:
            raise ValueError("工作数据不能放入技能或插件缓存目录")
        if part == ".git":
            raise ValueError("工作数据不能放入.git目录")
    if path.exists() and not path.is_dir():
        raise ValueError("--workspace 已存在且不是目录")
    return path


def ensure_profile(profile):
    if not isinstance(profile, dict) or not isinstance(profile.get("company"), dict):
        raise ValueError("画像须为含company对象的JSON，不能用历史库或自动化配置代替")
    if not isinstance(profile["company"].get("name"), str) or not profile["company"]["name"].strip():
        raise ValueError("画像company.name须为非空文本")
    for field in ("confirmed_services", "confirmed_qualifications", "pending_verification"):
        if field in profile and not isinstance(profile[field], list):
            raise ValueError("画像字段须为列表：" + field)
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).casefold().replace("-", "_")
                if normalized in FORBIDDEN_KEYS or re.search(r"(?:^|_)(?:password|passwd|token|secret|api_key|cookie|cookies)$", normalized):
                    raise ValueError("画像不可包含凭据或其他人员的自动化/对话标识：" + str(key))
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(profile)
    return profile


def source_files(root, scope, operator):
    contents = {}
    for relative in STATIC_FILES:
        source = RUNTIME / relative
        if not source.is_file() or source.is_symlink():
            raise ValueError("分发包缺少必要文件：" + relative)
        contents[relative] = source.read_bytes()
    templates = RUNTIME.parent / "templates" / "workspace"
    # Templates are maintained in the repository (and bundled beside installed
    # skills). Explicit extensions and path checks keep this an allowlist.
    if templates.is_dir():
        for source in sorted(templates.rglob("*")):
            if source.is_dir():
                continue
            if source.is_symlink() or source.suffix.casefold() not in {".txt", ".md", ".json"}:
                raise ValueError("模板包含不支持的文件：" + source.name)
            relative = source.relative_to(templates).as_posix()
            if relative.startswith(("data/", "materials/", "incoming/", "exports/", "inputs/")):
                raise ValueError("分发模板不得携带运行数据：" + relative)
            if relative in contents:
                raise ValueError("重复的安装文件：" + relative)
            value = source.read_text("utf-8-sig")
            for key, replacement in (("WORKSPACE", root.as_posix()), ("SCOPE", scope), ("OPERATOR", operator)):
                value = value.replace("{{" + key + "}}", replacement)
            if re.search(r"\{\{[A-Z_]+\}\}", value):
                raise ValueError("未识别的安装模板占位符：" + relative)
            contents[relative] = value.encode("utf-8")
    return contents


def owned_manifest(root, required=False):
    target = safe_target(root, MANIFEST)
    if not target.exists():
        if required:
            raise ValueError("未找到本工具安装清单；先选择新的独立目录执行init")
        return None
    manifest = read_json(target)
    if manifest.get("name") != NAME or not isinstance(manifest.get("files"), dict):
        raise ValueError("安装清单不属于本工具，未覆盖任何文件")
    for relative in manifest["files"]:
        safe_target(root, relative)
    return manifest


def next_pending(root, relative, content):
    for suffix in (".new", *(f".new.{i}" for i in range(2, 1001))):
        target = safe_target(root, relative + suffix)
        if not target.exists() or target.is_file() and target.read_bytes() == content:
            return relative + suffix
    raise ValueError("过多尚未处理的升级副本：" + relative)


def install_files(root, contents, prior):
    old_files = prior.get("files", {}) if prior else {}
    plan, result, conflicts = [], {}, []
    # Preflight every target before any code write, so an unowned filename never
    # gets overwritten, even if discovered near the end of an upgrade.
    for relative, content in contents.items():
        target = safe_target(root, relative)
        previous = old_files.get(relative)
        if target.exists() and (not target.is_file() or previous is None):
            raise ValueError("存在不属于本安装的文件，未覆盖：" + relative)
        expected = previous.get("installed_sha256") if isinstance(previous, dict) else previous
        current = sha(target.read_bytes()) if target.is_file() else None
        if current is not None and current != expected and current != sha(content):
            pending = next_pending(root, relative, content)
            plan.append((pending, content))
            result[relative] = {"installed_sha256": expected, "package_sha256": sha(content), "pending_update": pending}
            conflicts.append({"path": relative, "preserved": True, "new_version": pending})
        else:
            plan.append((relative, content))
            result[relative] = {"installed_sha256": sha(content), "package_sha256": sha(content)}
    for relative, content in plan:
        target = safe_target(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    # Previously managed files removed from a release remain locally preserved.
    for relative, previous in old_files.items():
        if relative not in result:
            result[relative] = previous
    return result, conflicts


def initial_values(scope, operator, root):
    schedules = []
    if scope == "both":
        schedules.append({"key": "morning", "scope": "both", "local_time": "09:00", "review_opening": True})
    if scope == "policy":
        schedules.append({"key": "policy_morning", "scope": "policy", "local_time": "09:00", "review_opening": True})
    if scope == "tender":
        schedules.append({"key": "tender_morning", "scope": "tender", "local_time": "09:00", "review_opening": False})
    schedules.append({"key": "afternoon", "scope": scope, "local_time": "16:30", "review_opening": False})
    return {
        INSTALLATION: {"name": NAME, "workspace": root.as_posix(), "operator": operator, "scope": scope,
                       "timezone": "Asia/Shanghai", "initialized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "profile_ready": False, "collection_performed": False},
        "data/automation_setup.json": {"status": "not_registered", "timezone": "Asia/Shanghai",
             "delivery": "recipient_current_codex_conversation", "automations": [],
             "requested_schedule": schedules, "daily_summary_time": None,
             "daily_summary_status": "待团队确认", "participation": "human_only", "default_review_state": "待人员判断",
             "note": "安装器未建立定时任务；在接收人员自己的Codex对话中用自动化工具登记实际返回的ID。"},
        "data/display_ids.json": {}, "data/notification_receipts.json": {},
        "data/card_notes.json": {},
        "data/review_navigation.json": {"primary_entry": "https://www.chinasme.cn/policy",
             "project_entry": "https://zcxm.chinasme.cn/policy", "focus_pages": {"min": 2, "max": 3},
             "reuse_existing_tabs": True, "platform_first": True, "opening_is_human_review": False},
        "materials/materials_manifest.json": {"attachments": []},
    }


def export_empty(root):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    commands = [
        ["human_review.py", "export", "--db", "data/human_review.sqlite", "--out", "exports"],
        ["monitor.py", "export", "--profile", "profile.json", "--db", "data/monitor.sqlite", "--out", "exports"],
    ]
    for command in commands:
        completed = subprocess.run([sys.executable, "-B", *command], cwd=root, env=env,
                                   text=True, encoding="utf-8", capture_output=True)
        if completed.returncode:
            raise ValueError("初始化空台账失败：" + completed.stderr.strip())


def initialize(args):
    root = workspace_path(args.workspace)
    prior = owned_manifest(root)
    if prior:
        if args.profile_import:
            raise ValueError("已安装目录不自动替换画像；先由人员审查后在本地单独编辑")
        existing = read_json(safe_target(root, INSTALLATION))
        if existing["scope"] != args.scope or existing["operator"] != args.operator:
            raise ValueError("已安装目录的scope/operator不同；升级保留原设置，更换人员请使用独立目录")
        return upgrade(args)
    if not args.operator.strip():
        raise ValueError("--operator 不能为空")
    profile = ensure_profile(read_json(args.profile_import) if args.profile_import else read_json(RUNTIME / "profile.example.json"))
    values = initial_values(args.scope, args.operator.strip(), root)
    protected = ["profile.json", "sources.json", "data/monitor.sqlite", "data/human_review.sqlite", MANIFEST, *values]
    for relative in protected:
        if safe_target(root, relative).exists():
            raise ValueError("目录已有未登记的配置或运行数据，请使用新的独立目录：" + relative)
    contents = source_files(root, args.scope, args.operator.strip())
    # Code-file preflight happens before creating configuration or empty ledgers.
    for relative in contents:
        if safe_target(root, relative).exists():
            raise ValueError("目录已有同名文件，未覆盖：" + relative)
    root.mkdir(parents=True, exist_ok=True)
    files, conflicts = install_files(root, contents, None)
    for folder in ("data", "incoming", "inputs/evidence", "materials", "exports/history"):
        safe_target(root, folder).mkdir(parents=True, exist_ok=True)
    write_json(root / "profile.json", profile)
    write_json(root / "sources.json", read_json(RUNTIME / "sources.example.json"))
    for relative, value in values.items():
        write_json(safe_target(root, relative), value)
    version = read_json(RUNTIME / "version.json")["version"]
    write_json(root / MANIFEST, {"name": NAME, "version": version, "files": files, "conflicts": conflicts})
    export_empty(root)
    return {"status": "initialized", "workspace": root.as_posix(), "scope": args.scope,
            "operator": args.operator.strip(), "version": version, "profile_imported": bool(args.profile_import),
            "automations_registered": False, "collection_performed": False,
            "next": "由人员核对画像及来源；Codex在本人的当前对话真实试跑后，通过自动化工具登记每日09:00和16:30。"}


def upgrade(args):
    root = workspace_path(args.workspace)
    prior = owned_manifest(root, required=True)
    settings = read_json(safe_target(root, INSTALLATION))
    contents = source_files(root, settings["scope"], settings["operator"])
    files, conflicts = install_files(root, contents, prior)
    version = read_json(RUNTIME / "version.json")["version"]
    write_json(root / MANIFEST, {"name": NAME, "version": version, "files": files, "conflicts": conflicts})
    return {"status": "upgraded", "workspace": root.as_posix(), "version": version,
            "conflicts": conflicts, "profile_preserved": True, "databases_preserved": True,
            "automation_registration_preserved": True, "collection_performed": False}


def doctor(args):
    root = workspace_path(args.workspace)
    manifest = owned_manifest(root, required=True)
    errors, warnings, counts = [], [], {}
    if sys.version_info < (3, 10):
        errors.append("需要Python 3.10或以上")
    for relative, entry in manifest["files"].items():
        path = safe_target(root, relative)
        expected = entry.get("installed_sha256") if isinstance(entry, dict) else entry
        if not path.is_file():
            errors.append("缺少安装文件：" + relative)
        elif sha(path.read_bytes()) != expected:
            warnings.append("本地已修改，升级将保留：" + relative)
        if isinstance(entry, dict) and entry.get("pending_update"):
            warnings.append("升级副本待合并：" + entry["pending_update"])
    try:
        profile = ensure_profile(read_json(safe_target(root, "profile.json")))
        if profile["company"]["name"].startswith("待补充") or not profile.get("confirmed_services"):
            warnings.append("企业画像尚未完成，不能据默认模板认定企业符合条件")
        for document in profile.get("source_documents", []):
            if not isinstance(document, dict) or not document.get("path"):
                continue
            candidate = Path(document["path"])
            if not candidate.is_absolute():
                candidate = root / candidate
            if not candidate.is_file():
                warnings.append("画像证据文件未迁入或路径失效；不能声称已查阅：" + str(document["path"]))
    except (ValueError, OSError, KeyError) as error:
        errors.append("画像无法读取：" + str(error))
    for filename, table in (("monitor.sqlite", "records"), ("human_review.sqlite", "screening_cases")):
        target = safe_target(root, "data/" + filename)
        if not target.is_file():
            errors.append("缺少数据库：" + filename)
            continue
        try:
            db = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                result = db.execute("PRAGMA quick_check").fetchone()[0]
                if result != "ok":
                    errors.append("数据库校验失败：" + filename)
                counts[table] = db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            finally:
                db.close()
        except sqlite3.Error as error:
            errors.append("数据库无法读取：" + filename + " " + str(error))
    try:
        registration = read_json(safe_target(root, "data/automation_setup.json"))
        if not registration.get("automations"):
            warnings.append("尚未在接收人员自己的Codex对话登记自动化任务")
    except (ValueError, OSError) as error:
        errors.append("自动化登记无法读取：" + str(error))
    return {"status": "healthy" if not errors else "failed", "workspace": root.as_posix(),
            "python": sys.executable, "python_version": sys.version.split()[0], "counts": counts,
            "errors": errors, "warnings": warnings, "web_access_checked": False,
            "note": "doctor只检查本地安装；网站访问、登录和定时工具权限仍需由本机Codex真实试跑。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("init")
    command.add_argument("--workspace", required=True)
    command.add_argument("--scope", choices=("policy", "tender", "both"), required=True)
    command.add_argument("--operator", required=True)
    command.add_argument("--profile-import")
    for name in ("doctor", "upgrade"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", required=True)
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 10):
            raise ValueError("需要Python 3.10或以上；可让Codex查询其bundled Python路径")
        result = {"init": initialize, "upgrade": upgrade, "doctor": doctor}[args.command](args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("errors") else 0
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
