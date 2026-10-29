"""Validate the public source package, without reading private runtime workspaces."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
TOP_FILES = {".gitignore", "README.md", "INSTALL.md", "VALIDATION.md", "AGENTS.md", "VERSION", "install.py", "plugin.json"}
TOP_DIRS = {".agents", ".github", "runtime", "skills", "templates", "tests", "tools"}
SKIP = {".git", "__pycache__"}
PRIVATE_DIRS = {"data", "incoming", "inputs", "exports", "materials", "private", "workspaces", "releases"}


def public_files(root=ROOT):
    result = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP for part in rel.parts):
            continue
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("发布包不能包含链接：" + rel.as_posix())
        if set(rel.parts) & PRIVATE_DIRS:
            raise ValueError("发布目录混入运行或私有资料：" + rel.as_posix())
        if not path.is_file():
            continue
        if rel.parts[0] not in TOP_DIRS and rel.as_posix() not in TOP_FILES:
            raise ValueError("未经审核的顶层文件：" + rel.as_posix())
        if path.suffix.lower() in {".sqlite", ".db", ".zip", ".pdf", ".docx", ".pptx", ".xlsx", ".key", ".pem"}:
            raise ValueError("不应公开的二进制/运行数据：" + rel.as_posix())
        result.append(path)
    return result


def check(root=ROOT):
    files = public_files(root)
    token = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-proj-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----)")
    personal_path = re.compile(r"[A-Za-z]:[/\\](?:Users[/\\][^/\\\s]+|中国中小企业服务网)")
    for path in files:
        text = path.read_text("utf-8-sig")
        if token.search(text) or personal_path.search(text):
            raise ValueError("疑似凭据或作者本机绝对路径：" + path.relative_to(root).as_posix())
        if path.suffix == ".json":
            json.loads(text)
    version = (root / "VERSION").read_text("utf-8").strip()
    manifest = json.loads((root / "plugin.json").read_text("utf-8"))
    if manifest["version"] != version or json.loads((root / "runtime/version.json").read_text("utf-8"))["version"] != version:
        raise ValueError("发行版本不一致")
    name = manifest["name"]
    if name not in {"sme-opportunity-monitor", "sme-finance-opportunities"}:
        raise ValueError("未知的发行技能")
    skill = root / "skills" / name / "SKILL.md"
    if not skill.read_text("utf-8").startswith("---\nname: " + name + "\n"):
        raise ValueError("技能名称或frontmatter不正确")
    onboarding = manifest["extensions"]["com.openai"]["onboardingSkill"]
    if (root / onboarding).resolve() != skill.resolve():
        raise ValueError("插件安装入口未指向现有技能")
    return {"ok": True, "version": version, "public_file_count": len(files),
            "note": "公开文件白名单/JSON/版本/凭据模式/作者路径检查通过；仍需发布前人工确认文件列表。"}


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(check(), ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
