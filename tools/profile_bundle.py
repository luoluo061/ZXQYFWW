"""Move a private company profile and its explicit document evidence locally.

This program has no network calls and never reads ledger databases, scheduler
configuration, notification receipts, browser storage, or surrounding folders.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timezone
import unicodedata
import uuid
import zipfile

FORMAT = "sme-private-profile-v1"
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_MEMBERS = 2000
DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".png", ".jpg", ".jpeg",
    ".webp", ".txt", ".md", ".wps",
}
SECRET_KEYS = {
    "password", "passwd", "secret", "api_key", "apikey", "access_token",
    "refresh_token", "authorization", "cookie", "cookies", "sessionid",
    "session_id", "browser_storage", "credentials", "automation_id", "thread_id",
}
RESERVED_STEMS = {
    "automation_setup", "notification_receipts", "review_opening_receipts",
    "human_review", "monitor", "display_ids", "latest_run",
}
PRIVACY = (
    "私有企业画像迁移包。包含企业画像及画像显式引用的证据文件。\n"
    "请仅交给获授权的同事，不要上传到公开 GitHub、公开链接或群共享。\n"
    "本包不包含台账、人员审核历史、定时任务注册信息或登录会话。\n"
    "接收人需在自己的 Codex 对话试跑后单独建立定时任务。\n"
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def no_links(path: Path) -> None:
    """Reject symlink/junction components before resolving user paths."""
    absolute = path.absolute()
    for part in (absolute, *absolute.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("路径中存在符号链接或目录联接，未读取：" + str(part))


def regular_file(path: Path, limit=MAX_FILE_BYTES) -> Path:
    no_links(path)
    result = path.resolve(strict=True)
    if not result.is_file() or not stat.S_ISREG(result.stat().st_mode):
        raise ValueError("需要普通文件：" + str(path))
    if result.stat().st_size > limit:
        raise ValueError("文件超过大小上限：" + str(path))
    return result


def read_profile(path: Path):
    source = regular_file(path, MAX_JSON_BYTES)
    profile = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(profile, dict) or not isinstance(profile.get("company"), dict):
        raise ValueError("画像需要 company 对象")
    reject_secret_keys(profile)
    return source, profile


def reject_secret_keys(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in SECRET_KEYS:
                raise ValueError("画像包含登录或凭据字段，请移除后再迁移：" + str(key))
            reject_secret_keys(item)
    elif isinstance(value, list):
        for item in value:
            reject_secret_keys(item)


def document_refs(profile):
    """Only traverse explicit source_documents, never arbitrary profile paths."""
    def visit(value):
        if isinstance(value, dict):
            if "path" in value:
                if not isinstance(value["path"], str) or not value["path"].strip():
                    raise ValueError("source_documents 的 path 须为非空文本")
                yield value
            for key, item in value.items():
                if key != "path":
                    yield from visit(item)
        elif isinstance(value, list):
            for item in value:
                yield from visit(item)
    documents = profile.get("source_documents", [])
    if not isinstance(documents, list):
        raise ValueError("source_documents 须为数组")
    yield from visit(documents)


def safe_member(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("迁移包中有不安全路径")
    parts = name.split("/")
    if name.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("迁移包中有不安全路径：" + name)
    if any(part.endswith((".", " ")) for part in parts):
        raise ValueError("迁移包路径不能以点或空格结尾")
    for part in parts:
        if any(ord(character) < 32 or character in '<>"|?*' for character in part):
            raise ValueError("迁移包路径含非法字符")
        if re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])", part.split(".", 1)[0], re.IGNORECASE):
            raise ValueError("迁移包路径使用系统保留文件名")
    if PurePosixPath(name).is_absolute():
        raise ValueError("迁移包中有绝对路径")
    return name


def member_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def atomic_write(path: Path, content: bytes) -> None:
    no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".profile-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def pack(profile_path, output_path, allow_external_evidence=False):
    source, profile = read_profile(Path(profile_path))
    portable = copy.deepcopy(profile)
    payload = {"PRIVACY.txt": PRIVACY.encode("utf-8")}
    by_hash = {}
    total = len(payload["PRIVACY.txt"])
    for document in document_refs(portable):
        raw_path = Path(document["path"])
        evidence = regular_file(raw_path if raw_path.is_absolute() else source.parent / raw_path)
        if not evidence.is_relative_to(source.parent) and not allow_external_evidence:
            raise ValueError("证据位于画像目录外；确认该引用后可加 --allow-external-evidence")
        if evidence.suffix.casefold() not in DOCUMENT_EXTENSIONS or evidence.stem.casefold() in RESERVED_STEMS:
            raise ValueError("不是可迁移的文档证据：" + evidence.name)
        content = evidence.read_bytes()
        digest = sha256(content)
        expected = document.get("sha256")
        if expected and str(expected).casefold() != digest:
            raise ValueError("证据 SHA256 与画像记录不一致：" + evidence.name)
        if digest not in by_hash:
            name = "evidence/" + digest + evidence.suffix.casefold()
            payload[name] = content
            by_hash[digest] = name
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise ValueError("证据合计超过迁移包大小上限")
        document["path"] = by_hash[digest]
        document["sha256"] = digest
    payload["profile.json"] = json_bytes(portable)
    entries = [{"path": name, "sha256": sha256(content), "size": len(content)}
               for name, content in sorted(payload.items())]
    bundle_id = sha256(json_bytes(entries))
    manifest = {"format": FORMAT, "bundle_id": bundle_id,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "files": entries}
    payload["manifest.json"] = json_bytes(manifest)
    if len(payload) > MAX_MEMBERS or sum(map(len, payload.values())) > MAX_TOTAL_BYTES:
        raise ValueError("迁移包超过数量或大小上限")
    output = Path(output_path).absolute()
    no_links(output)
    if output.exists():
        raise ValueError("输出文件已存在，未覆盖：" + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".profile-bundle-", suffix=".zip", dir=output.parent)
    os.close(handle)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in payload.items():
                archive.writestr(name, content)
        # Re-read before publishing the completed local artifact.
        load_bundle(temporary)
        # Exclusive creation also prevents an unexpected existing output being replaced.
        with output.open("xb") as target, open(temporary, "rb") as stream:
            shutil.copyfileobj(stream, target)
    finally:
        os.unlink(temporary)
    return {"bundle_id": bundle_id, "bundle": str(output),
            "evidence_files": len(payload) - 3, "uploaded": False}


def load_bundle(bundle_path):
    bundle = regular_file(Path(bundle_path), MAX_TOTAL_BYTES)
    payload = {}
    total = 0
    with zipfile.ZipFile(bundle) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ValueError("迁移包文件数超限")
        seen = set()
        for info in infos:
            name = safe_member(info.filename)
            key = member_key(name)
            if key in seen:
                raise ValueError("迁移包有重复或大小写冲突路径：" + name)
            seen.add(key)
            mode = info.external_attr >> 16
            if info.is_dir() or stat.S_ISLNK(mode) or (mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG}):
                raise ValueError("迁移包只允许普通文件")
            if info.flag_bits & 1:
                raise ValueError("不接受加密 ZIP；请通过私有渠道传递原始迁移包")
            if info.file_size > MAX_FILE_BYTES:
                raise ValueError("迁移包单文件超限")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("迁移包解压后大小超限")
            with archive.open(info) as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
            if len(content) != info.file_size or len(content) > MAX_FILE_BYTES:
                raise ValueError("迁移包文件大小与声明不符")
            payload[name] = content
    if not {"manifest.json", "profile.json", "PRIVACY.txt"}.issubset(payload):
        raise ValueError("缺少迁移包清单、画像或隐私说明")
    if max(len(payload["manifest.json"]), len(payload["profile.json"])) > MAX_JSON_BYTES:
        raise ValueError("迁移包 JSON 过大")
    manifest = json.loads(payload["manifest.json"].decode("utf-8-sig"))
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or not isinstance(manifest.get("files"), list):
        raise ValueError("不支持的迁移包格式")
    entries = manifest["files"]
    expected_names = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("迁移包清单条目无效")
        name = safe_member(entry.get("path"))
        if name in expected_names or name == "manifest.json":
            raise ValueError("迁移包清单有重复路径")
        expected_names.add(name)
        if name not in payload or type(entry.get("size")) is not int or entry["size"] != len(payload[name]):
            raise ValueError("迁移包文件缺失或长度不符：" + name)
        if entry.get("sha256") != sha256(payload[name]):
            raise ValueError("迁移包 SHA256 校验失败：" + name)
    if expected_names != set(payload) - {"manifest.json"}:
        raise ValueError("迁移包包含未列入清单的文件")
    if manifest.get("bundle_id") != sha256(json_bytes(sorted(entries, key=lambda value: value["path"]))):
        raise ValueError("迁移包标识校验失败")
    profile = json.loads(payload["profile.json"].decode("utf-8-sig"))
    if not isinstance(profile, dict) or not isinstance(profile.get("company"), dict):
        raise ValueError("迁移画像缺少 company 对象")
    reject_secret_keys(profile)
    referenced = set()
    for document in document_refs(profile):
        name = safe_member(document["path"])
        if not name.startswith("evidence/") or name not in payload:
            raise ValueError("画像引用了包外证据")
        if PurePosixPath(name).suffix.casefold() not in DOCUMENT_EXTENSIONS:
            raise ValueError("画像证据类型不受支持")
        if document.get("sha256") != sha256(payload[name]):
            raise ValueError("画像引用的证据哈希不一致")
        referenced.add(name)
    if set(payload) != referenced | {"manifest.json", "profile.json", "PRIVACY.txt"}:
        raise ValueError("迁移包有画像未显式引用的额外文件")
    return manifest, profile, payload


def unpack(bundle_path, workspace_path, replace_profile=False):
    manifest, profile, payload = load_bundle(bundle_path)
    workspace = Path(workspace_path).absolute()
    no_links(workspace)
    if workspace.exists() and not workspace.is_dir():
        raise ValueError("workspace 须为文件夹")
    workspace = workspace.resolve()
    target = workspace / "inputs" / ("company_profile_" + manifest["bundle_id"][:16])
    no_links(target)
    local_profile = copy.deepcopy(profile)
    for document in document_refs(local_profile):
        document["path"] = (target / document["path"]).as_posix()
    final_profile = json_bytes(local_profile)
    profile_path = workspace / "profile.json"
    no_links(profile_path)
    if profile_path.exists():
        current_profile = regular_file(profile_path, MAX_JSON_BYTES).read_bytes()
        if current_profile != final_profile and not replace_profile:
            raise ValueError("workspace 已有不同画像；确认替换后加 --replace-profile（会先备份）")
    else:
        current_profile = None
    reused = target.exists()
    if reused:
        if not target.is_dir():
            raise ValueError("证据导入目标不是文件夹")
        actual = set()
        for existing in target.rglob("*"):
            no_links(existing)
            if existing.is_file():
                name = existing.relative_to(target).as_posix()
                actual.add(name)
                if name not in payload or sha256(existing.read_bytes()) != sha256(payload[name]):
                    raise ValueError("已存在的证据导入目录有文件冲突，未覆盖")
            elif not existing.is_dir():
                raise ValueError("证据导入目录含非普通文件")
        if actual != set(payload):
            raise ValueError("已存在的证据导入目录不完整，未合并")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".profile-import-", dir=target.parent))
        try:
            for name, content in payload.items():
                destination = staging / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            staging.rename(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    backup = None
    if current_profile is not None and current_profile != final_profile:
        backup = workspace / "inputs" / "profile_backups" / (
            datetime.now(timezone.utc).strftime("profile-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8] + ".json")
        atomic_write(backup, current_profile)
    if current_profile != final_profile:
        atomic_write(profile_path, final_profile)
    return {"bundle_id": manifest["bundle_id"], "workspace": str(workspace),
            "profile": str(profile_path), "backup": str(backup) if backup else None,
            "evidence_files": len(payload) - 3, "reused_evidence": reused}


def doctor(bundle_path=None, workspace_path=None):
    if bool(bundle_path) == bool(workspace_path):
        raise ValueError("doctor 请选择 --bundle 或 --workspace 其中一个")
    if bundle_path:
        manifest, _, payload = load_bundle(bundle_path)
        return {"ok": True, "bundle_id": manifest["bundle_id"], "evidence_files": len(payload) - 3}
    workspace = Path(workspace_path).absolute()
    source, profile = read_profile(workspace / "profile.json")
    count = 0
    for document in document_refs(profile):
        evidence = Path(document["path"])
        evidence = regular_file(evidence if evidence.is_absolute() else source.parent / evidence)
        if not evidence.is_relative_to(workspace.resolve()):
            raise ValueError("证据仍指向 workspace 外部，未完成便携导入")
        if not re.fullmatch(r"[a-f0-9]{64}", str(document.get("sha256", ""))):
            raise ValueError("证据缺少 SHA256")
        if sha256(evidence.read_bytes()) != document["sha256"]:
            raise ValueError("本地证据 SHA256 不一致：" + evidence.name)
        count += 1
    return {"ok": True, "workspace": str(workspace), "document_references": count}


def main(argv=None):
    parser = argparse.ArgumentParser(description="私有企业画像及显式证据迁移；不会联网或注册定时任务")
    commands = parser.add_subparsers(dest="command", required=True)
    packing = commands.add_parser("pack")
    packing.add_argument("--profile", required=True)
    packing.add_argument("--out", required=True)
    packing.add_argument("--allow-external-evidence", action="store_true")
    importing = commands.add_parser("unpack")
    importing.add_argument("--bundle", required=True)
    importing.add_argument("--workspace", required=True)
    importing.add_argument("--replace-profile", action="store_true")
    checking = commands.add_parser("doctor")
    group = checking.add_mutually_exclusive_group(required=True)
    group.add_argument("--bundle")
    group.add_argument("--workspace")
    args = parser.parse_args(argv)
    try:
        if args.command == "pack":
            result = pack(args.profile, args.out, args.allow_external_evidence)
        elif args.command == "unpack":
            result = unpack(args.bundle, args.workspace, args.replace_profile)
        else:
            result = doctor(args.bundle, args.workspace)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, zipfile.BadZipFile, json.JSONDecodeError, RuntimeError) as exc:
        print("画像迁移未完成：" + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
