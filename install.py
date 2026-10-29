"""Install a complete local Codex skill; never copy scheduler or runtime user data."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILL_NAME = "sme-opportunity-monitor"
MARKER = ".zxqyfww-skill-install.json"
DATA_DIRS = {"data", "inputs", "exports", "incoming", "materials", "private", "workspaces"}


def reject_links(path):
    for candidate in [path.absolute(), *path.absolute().parents]:
        if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
            raise ValueError("技能路径不能经过符号链接或目录联接：" + str(candidate))


def safe_relative(value):
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or ":" in str(value):
        raise ValueError("安装清单包含不安全路径")
    return path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def payload(root=ROOT):
    """Use an explicit source allowlist, not a repository-wide recursive copy."""
    skill = root / "skills" / SKILL_NAME
    if not skill.is_dir():
        raise ValueError("请从完整发行包或仓库运行 install.py，不能只下载这一个脚本。")
    files = {}
    for source, prefix, suffixes in (
        (skill, "", {".md", ".yaml", ".yml"}),
        (root / "runtime", "runtime", {".py", ".json", ".md", ".txt"}),
        (root / "templates", "templates", {".json", ".md", ".txt"}),
        (root / "tools", "tools", {".py"}),
    ):
        if not source.is_dir():
            raise ValueError("发行包缺少目录：" + str(source))
        for path in sorted(source.rglob("*")):
            reject_links(path)
            if set(path.relative_to(source).parts) & DATA_DIRS:
                raise ValueError("发行包资源中混入运行数据目录")
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if prefix == "tools" and path.relative_to(source).as_posix() != "profile_bundle.py":
                continue
            if path.suffix not in suffixes:
                continue
            rel = (Path(prefix) / path.relative_to(source)).as_posix()
            if rel.split("/")[0] in {"data", "inputs", "exports", "incoming", "materials"}:
                raise ValueError("发行包混入运行数据")
            files[rel] = path.read_bytes()
    for name in ("VERSION", "INSTALL.md"):
        if not (root / name).is_file():
            raise ValueError("发行包缺少 " + name)
        files[name] = (root / name).read_bytes()
    if "SKILL.md" not in files or "runtime/bootstrap.py" not in files:
        raise ValueError("发行包不完整")
    return files


def install(skills_dir, root=ROOT):
    skills_dir = Path(skills_dir).expanduser().absolute()
    reject_links(skills_dir)
    skills_dir = skills_dir.resolve()
    target = skills_dir / SKILL_NAME
    reject_links(target)
    marker = target / MARKER
    reject_links(marker)
    old = {}
    if target.exists():
        if not marker.is_file():
            raise ValueError("目标已存在且不是本安装器管理的技能，请先人工检查：" + str(target))
        old = json.loads(marker.read_text("utf-8"))
        if old.get("name") != SKILL_NAME:
            raise ValueError("安装标记不匹配")
        for rel in old.get("files", {}):
            safe_relative(rel)
    files = payload(root)
    conflicts = []
    managed = dict(old.get("files", {}))
    # Plan all writes, including conflict copies, before changing any file.
    plan = []
    for rel in files:
        dest = target / safe_relative(rel)
        reject_links(dest)
        if dest.exists() and not dest.is_file():
            raise ValueError("目标文件位置已有目录：" + rel)
        data = files[rel]
        expected = sha(data)
        current = sha(dest.read_bytes()) if dest.exists() else None
        previous = old.get("files", {}).get(rel)
        if current not in (None, previous, expected):
            new = dest.with_name(dest.name + ".new")
            reject_links(new)
            if new.exists() and (not new.is_file() or new.read_bytes() != data):
                raise ValueError("升级冲突旁路文件已被修改，请人工合并：" + str(new))
            plan.append((new, data))
            conflicts.append(rel)
            continue
        plan.append((dest, data))
        managed[rel] = expected
    target.mkdir(parents=True, exist_ok=True)
    for dest, data in plan:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    record = {"name": SKILL_NAME, "version": (root / "VERSION").read_text("utf-8").strip(),
              "files": managed, "pending_conflicts": conflicts,
              "scheduler_registered": False}
    marker.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return {"skill_directory": str(target), "conflicts": conflicts,
            "automation_status": "未创建；需要接收人的 Codex 在当前对话中登记。"}


def doctor(skills_dir):
    skills_dir = Path(skills_dir).expanduser().absolute()
    reject_links(skills_dir)
    target = skills_dir.resolve() / SKILL_NAME
    marker = target / MARKER
    reject_links(marker)
    if not marker.is_file():
        raise ValueError("没有安装标记")
    state = json.loads(marker.read_text("utf-8"))
    problems = list(state.get("pending_conflicts", []))
    for rel, expected in state.get("files", {}).items():
        path = target / safe_relative(rel)
        reject_links(path)
        if not path.is_file() or path.is_symlink() or sha(path.read_bytes()) != expected:
            problems.append(rel)
    return {"skill_directory": str(target), "version": state.get("version"),
            "ok": not problems, "modified_or_missing": sorted(set(problems)),
            "checks": "仅检查技能文件完整性；网页登录和实际定时触发另行验收。"}


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "doctor"], nargs="?", default="install")
    parser.add_argument("--skills-dir", type=Path, default=Path.home() / ".agents" / "skills")
    args = parser.parse_args()
    try:
        result = install(args.skills_dir) if args.command == "install" else doctor(args.skills_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result.get("conflicts") and result.get("ok", True) else 2
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print("安装检查失败：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
