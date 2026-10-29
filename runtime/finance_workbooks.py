"""Portable finance workbooks using standard-library OOXML only.

This is a local presentation exporter, not a collector or an accounting/review
engine. All source and human facts come from the supplied ledger views.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit
from xml.etree import ElementTree as ET
import zipfile

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT = "http://schemas.openxmlformats.org/package/2006/content-types"
TZ = timezone(timedelta(hours=8))
ET.register_namespace("", NS)
ET.register_namespace("r", REL)
DAILY_FIELDS = ["检索日期", "编号及政策名称", "检索词/地区", "能带来什么", "要核实什么", "办理时间", "资料下载", "人工意见及下一步"]
LEDGER_FIELDS = ["编号及政策名称", "政策类别", "能带来什么", "要核实/缺什么资料", "办理时间", "资料下载", "是否参与", "办理进展", "下一步/负责人", "最近跟进"]
DETAIL_FIELDS = ["政策编号", "资料名称", "用来做什么", "当前情况", "打开或下载", "需要注意"]


def element(tag, attrs=None, text=None, namespace=NS):
    item = ET.Element("{" + namespace + "}" + tag, attrs or {})
    if text is not None:
        item.text = clean(text)
    return item


def child(parent, tag, attrs=None, text=None, namespace=NS):
    item = element(tag, attrs, text, namespace)
    parent.append(item)
    return item


def xml(item):
    return ET.tostring(item, encoding="utf-8", xml_declaration=True)


def clean(value):
    value = "" if value is None else str(value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "�", value)
    return value[:32760] + ("…" if len(value) > 32760 else "")


def safe_url(value):
    """Use document links only; never publish credentials/session URLs."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        _ = parts.port
        for key, _ in parse_qsl(parts.query, keep_blank_values=True) + parse_qsl(parts.fragment, keep_blank_values=True):
            key = key.lower().replace("-", "_")
            if re.search(r"(?:^|_)(?:token|password|passwd|secret|api_?key|cookie|authorization|session_?id)$", key) or key in {"accesstoken", "refreshtoken"}:
                return None
        if any(ord(c) < 32 for c in value):
            return None
        normalized = urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, parts.fragment))
        return quote(normalized, safe=":/?#[]@!$&'()*+,;=%")
    except (ValueError, UnicodeError):
        return None


def identity(title):
    match = re.match(r"^(P\d{3,})\b", str(title or ""))
    if not match:
        raise ValueError("财务Excel记录缺少稳定P编号")
    return match.group(1)


def policy_title(value):
    value = str(value or "")
    first = value.splitlines()[0] if value else ""
    urls = re.findall(r"https?://[^\s]+", value)
    return clean(first), next((url for url in (safe_url(v) for v in urls) if url), None)


def date_number(value):
    if not value:
        return None
    try:
        text = str(value).strip()
        if "T" in text:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            when = moment.astimezone(TZ).date() if moment.tzinfo else moment.date()
        else:
            when = date.fromisoformat(text[:10])
        return (when - date(1899, 12, 30)).days
    except (ValueError, TypeError, OverflowError):
        return None


def record_items(records, ids):
    output = {}
    for record in records:
        value = record.get("data", {}) if isinstance(record, dict) else record["data"]
        data = json.loads(value) if isinstance(value, str) else dict(value)
        internal = record["id"]
        number = ids.get(internal)
        if number and re.fullmatch(r"P\d{3,}", str(number)):
            output[number] = (internal, data)
    return output


def verified_file(root, entry):
    if entry.get("status") != "downloaded":
        return False, "尚未取得"
    value = entry.get("local_path")
    if not value:
        return False, "本地文件待核"
    path = Path(value)
    path = path if path.is_absolute() else root / path
    try:
        path = path.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return False, "本地文件缺失或路径待核"
        expected = entry.get("sha256")
        if not expected or not re.fullmatch(r"[0-9a-fA-F]{64}", str(expected)):
            return False, "本地文件存在，哈希待核"
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest().lower() != str(expected).lower():
            return False, "本地文件已变化，需重新核验"
        if entry.get("size_bytes") is not None and path.stat().st_size != entry["size_bytes"]:
            return False, "本地文件大小已变化，需重新核验"
        return True, "已取得并核对留存文件"
    except (OSError, ValueError):
        return False, "本地文件无法核验"


def material_kind(entry):
    title, note = clean(entry.get("title")), clean(entry.get("note"))
    terms = title + " " + note
    try:
        path = urlsplit(str(entry.get("url") or "")).path.lower()
    except ValueError:
        path = ""
    document_extensions = {".doc", ".docx", ".xls", ".xlsx", ".pdf", ".csv", ".odt", ".ods", ".zip", ".rar", ".7z"}
    title_extension = Path(title.strip()).suffix.lower()
    local_extension = Path(str(entry.get("local_path") or "").strip()).suffix.lower()
    concrete_document = title_extension in document_extensions or local_extension in document_extensions
    if re.search(r"历史参考|旧年度|旧版|历史指引", terms):
        return "历史参考", "帮助理解政策背景和旧口径", "旧资料仅供参考，正式办理核现行政策和表样。"
    # Download handlers often end in .aspx or .html while returning a real
    # document. A known document filename must not become policy prose merely
    # because its download URL is a web endpoint.
    if title_extension in {".html", ".htm"} or (not concrete_document and
            (local_extension in {".html", ".htm"} or path.endswith((".html", ".htm", ".aspx")))):
        return "政策正文", "看支持对象、条件、时间和办理方式", "政策通知正文通常不是企业填写的申请表。"
    if re.search(r"银行.*财政|财政.*银行|银行汇总|财政汇总|资金管理表|非企业申请表|不是企业.{0,8}(?:申请|申报)表", terms):
        return "银行财政表", "了解银行或财政的资金管理流程", "按注明的填报主体使用；企业材料以经办要求为准。"
    if re.search(r"申请表|申报表|表样|填报说明", terms):
        return "表样", "核适用年度和填报主体后准备材料", "取得表样不代表符合条件、已填报或已享受。"
    return "政策附件", "核对通知要求和相关材料", "具体用途及适用期间按原文核实。"


def material_rows(root, numbers, records, ids, manifest):
    """Return display rows, real web links, and one anchor for every P number."""
    indexed = record_items(records, ids)
    entries = manifest.get("attachments", []) if isinstance(manifest, dict) else list(manifest or [])
    if not isinstance(entries, list):
        entries = []
    rows, links, anchors, summaries = [], [], {}, {}
    for number in sorted(numbers):
        anchors[number] = len(rows) + 5
        internal, data = indexed.get(number, (None, {}))
        source = safe_url(data.get("original_url") or data.get("source_url"))
        associated = [e for e in entries if isinstance(e, dict) and
                      ((internal is not None and e.get("item_id") == internal) or
                       (not e.get("item_id") and e.get("short_id") == number))]
        if source:
            rows.append([number, "政策来源页面", "阅读来源，核对支持对象、条件、时间和办理方式",
                         "采集记录：原文已核" if data.get("original_verified") else "来源阅读层级待核",
                         "打开政策原文" if data.get("original_verified") else "打开来源页面", "来源页面不是企业填写用的申请表；当前说明不代替办理材料清单。"])
            links.append(("E" + str(len(rows) + 4), source))
        # Keep current continuation rules and current forms visible beside the
        # base notice. A related link is not itself a newly downloaded file.
        seen_basis = {source} if source else set()
        seen_basis.update(safe_url(e.get("url")) for e in associated if verified_file(root, e)[0])
        for field in ("related_policy_documents", "related_items"):
            related = data.get(field) or []
            related = [related] if isinstance(related, dict) else related
            if not isinstance(related, list):
                continue
            for reference in related:
                if not isinstance(reference, dict):
                    continue
                target = safe_url(reference.get("url") or reference.get("original_url") or reference.get("source_url"))
                if not target or target in seen_basis:
                    continue
                seen_basis.add(target)
                section = clean(reference.get("verified_section"))
                state = "采集记录已读指定段落" if section else "采集记录：原文已核" if reference.get("original_verified") is True else "关联依据，阅读层级按采集记录核"
                note = "已读范围：" + section + "；其余条款按采集记录核。" if section else "请核适用期间和具体条款；此链接不表示已取得独立附件。"
                rows.append([number, clean(reference.get("title") or reference.get("document_number") or reference.get("number") or "关联政策依据"),
                             "核对政策衔接、适用期间或办理要求", state, "打开关联依据", note])
                links.append(("E" + str(len(rows) + 4), target))
        downloaded, missing, seen = 0, 0, set()
        for entry in associated:
            key = (entry.get("url"), entry.get("title"), entry.get("status"))
            if key in seen:
                continue
            seen.add(key)
            kind, purpose, caution = material_kind(entry)
            valid, state = verified_file(root, entry)
            target = safe_url(entry.get("url"))
            if valid:
                downloaded += 1
                action = "打开政策正文" if kind == "政策正文" else "下载历史参考" if kind == "历史参考" else "下载文件"
                if not target:
                    action = "来源链接待核"
                    caution += " 未提供安全的网页或文件来源链接。"
            else:
                missing += 1
                action = "暂无可下载文件"
                if entry.get("status") == "auth_required":
                    state = "需人员正常登录后取得"
                purpose = "核实企业要交哪些材料" if entry.get("status") != "downloaded" else purpose
                caution += " 先按属地或经办机构要求补核。"
            title = clean(entry.get("title") or "办理材料待核")
            rows.append([number, title, purpose, state, action, caution + (" " + clean(entry.get("note")) if entry.get("note") else "")])
            if valid and target:
                links.append(("E" + str(len(rows) + 4), target))
        if not associated:
            rows.append([number, "企业办理表样或材料清单", "先核企业实际要交哪些材料", "尚未取得独立企业表样", "暂无可下载文件", "按属地或经办机构要求核实；没有附件不代表政策不适用。"])
            missing += 1
        summaries[number] = "查看资料明细\n" + (f"已取得{downloaded}份" if downloaded else "办理材料待核") + ("；另有资料待核" if downloaded and missing else "")
    if not rows:
        rows.append(["", "本轮没有正式候选", "实际查询及失败范围见运行日志", "无资料待列", "", "此处不新增政策或人工审核记录。"])
    return rows, links, anchors, summaries


def letters(number):
    value = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        value = chr(65 + remainder) + value
    return value


def wrapped_row_height(values, widths, minimum):
    """Estimate wrapped CJK lines in Excel character-width units, in points."""
    maximum_lines = 1
    for index, value in enumerate(values):
        width = max(4, float(widths[index] if index < len(widths) else 16) - 1)
        lines = 0
        for paragraph in clean(value).split("\n"):
            weight = sum(2 if unicodedata.east_asian_width(char) in {"W", "F", "A"}
                         else 4 if char == "\t" else 1 for char in paragraph)
            lines += max(1, math.ceil(weight / width))
        maximum_lines = max(maximum_lines, lines)
    return min(409, max(minimum, maximum_lines * 14 + 6))


def styles():
    root = element("styleSheet")
    formats = child(root, "numFmts", {"count": "1"})
    child(formats, "numFmt", {"numFmtId": "164", "formatCode": "yyyy-mm-dd"})
    fonts = child(root, "fonts", {"count": "5"})
    for size, color, bold in [(11, "FF202D3A", False), (16, "FF183B56", True), (11, "FFFFFFFF", True), (11, "FF1F609B", False), (10, "FF667585", False)]:
        font = child(fonts, "font")
        child(font, "sz", {"val": str(size)})
        child(font, "color", {"rgb": color})
        child(font, "name", {"val": "Arial"})
        if bold:
            child(font, "b")
    fills = child(root, "fills", {"count": "4"})
    for pattern, color in [("none", None), ("gray125", None), ("solid", "FF234966"), ("solid", "FFFFF2CC")]:
        fill = child(child(fills, "fill"), "patternFill", {"patternType": pattern})
        if color:
            child(fill, "fgColor", {"rgb": color})
            child(fill, "bgColor", {"indexed": "64"})
    borders = child(root, "borders", {"count": "2"})
    for line in (False, True):
        border = child(borders, "border")
        for side in ("left", "right", "top", "bottom", "diagonal"):
            part = child(border, side, {"style": "thin"} if line and side == "bottom" else {})
            if line and side == "bottom":
                child(part, "color", {"rgb": "FFDCE5EB"})
    base = child(root, "cellStyleXfs", {"count": "1"})
    child(base, "xf", {"numFmtId": "0", "fontId": "0", "fillId": "0", "borderId": "0"})
    xfs = child(root, "cellXfs", {"count": "7"})
    for font, fill, border, fmt, horizontal in [(0, 0, 1, 0, "left"), (1, 0, 0, 0, "left"), (4, 0, 0, 0, "left"), (2, 2, 1, 0, "center"), (3, 0, 1, 0, "left"), (0, 0, 1, 164, "center"), (0, 3, 1, 0, "left")]:
        xf = child(xfs, "xf", {"numFmtId": str(fmt), "fontId": str(font), "fillId": str(fill), "borderId": str(border), "xfId": "0", "applyAlignment": "1", "applyNumberFormat": "1"})
        child(xf, "alignment", {"horizontal": horizontal, "vertical": "top", "wrapText": "1"})
    named = child(root, "cellStyles", {"count": "1"})
    child(named, "cellStyle", {"name": "Normal", "xfId": "0", "builtinId": "0"})
    return xml(root)


def worksheet(name, title, notes, headers, rows, widths, date_columns=(), frozen_columns=1, links=(), internal=(), state_columns=()):
    sheet = element("worksheet")
    total = max(4, len(rows) + 4)
    child(sheet, "dimension", {"ref": f"A1:{letters(len(headers))}{total}"})
    view = child(child(sheet, "sheetViews"), "sheetView", {"showGridLines": "0", "workbookViewId": "0"})
    cell = letters(frozen_columns + 1) + "5"
    child(view, "pane", {"xSplit": str(frozen_columns), "ySplit": "4", "topLeftCell": cell, "activePane": "bottomRight", "state": "frozen"})
    child(view, "selection", {"pane": "bottomRight", "activeCell": cell, "sqref": cell})
    child(sheet, "sheetFormatPr", {"defaultRowHeight": "18"})
    columns = child(sheet, "cols")
    for index, width in enumerate(widths, 1):
        child(columns, "col", {"min": str(index), "max": str(index), "width": str(width), "customWidth": "1"})
    data = child(sheet, "sheetData")
    hyperlink_cells = {address for address, _ in links} | {address for address, _ in internal}
    all_rows = [[title], [notes[0]], [notes[1]], headers, *rows]
    for number, values in enumerate(all_rows, 1):
        height = 24 if number == 1 else 18 if number < 4 else 32 if number == 4 else wrapped_row_height(values, widths, 74 if name != "资料明细" else 68)
        row = child(data, "row", {"r": str(number), "ht": str(height), "customHeight": "1"})
        for column, value in enumerate(values, 1):
            address = letters(column) + str(number)
            style = 1 if number == 1 else 2 if number < 4 else 3 if number == 4 else 4 if address in hyperlink_cells else 6 if column - 1 in state_columns and "待" in str(value) else 0
            serial = date_number(value) if number > 4 and column - 1 in date_columns else None
            attrs = {"r": address, "s": str(5 if serial is not None else style)}
            if serial is not None:
                child(child(row, "c", attrs), "v", text=str(serial))
            else:
                attrs["t"] = "inlineStr"
                text = child(child(child(row, "c", attrs), "is"), "t", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"}, clean(value))
    if rows:
        child(sheet, "autoFilter", {"ref": f"A4:{letters(len(headers))}{total}"})
    rels = element("Relationships", namespace=PKG)
    if links or internal:
        hyperlinks = child(sheet, "hyperlinks")
        for index, (address, target) in enumerate(links, 1):
            relation = "rIdLink" + str(index)
            child(hyperlinks, "hyperlink", {"ref": address, "{" + REL + "}id": relation, "tooltip": "打开政策网页或具体资料文件"})
            child(rels, "Relationship", {"Id": relation, "Type": REL + "/hyperlink", "Target": target, "TargetMode": "External"}, namespace=PKG)
        for address, location in internal:
            child(hyperlinks, "hyperlink", {"ref": address, "location": location, "tooltip": "查看资料明细或返回主表"})
    child(sheet, "pageMargins", {"left": "0.25", "right": "0.25", "top": "0.5", "bottom": "0.5", "header": "0.2", "footer": "0.2"})
    return xml(sheet), xml(rels)


def package(sheets):
    book = element("workbook")
    view = child(book, "bookViews")
    child(view, "workbookView", {"activeTab": "0"})
    listing = child(book, "sheets")
    rels = element("Relationships", namespace=PKG)
    types = element("Types", namespace=CONTENT)
    child(types, "Default", {"Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"}, namespace=CONTENT)
    child(types, "Default", {"Extension": "xml", "ContentType": "application/xml"}, namespace=CONTENT)
    child(types, "Override", {"PartName": "/xl/workbook.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"}, namespace=CONTENT)
    child(types, "Override", {"PartName": "/xl/styles.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"}, namespace=CONTENT)
    files = {"xl/styles.xml": styles()}
    for index, (name, content, relationships) in enumerate(sheets, 1):
        child(listing, "sheet", {"name": name, "sheetId": str(index), "{" + REL + "}id": "rId" + str(index)})
        child(rels, "Relationship", {"Id": "rId" + str(index), "Type": REL + "/worksheet", "Target": f"worksheets/sheet{index}.xml"}, namespace=PKG)
        child(types, "Override", {"PartName": f"/xl/worksheets/sheet{index}.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"}, namespace=CONTENT)
        files[f"xl/worksheets/sheet{index}.xml"] = content
        files[f"xl/worksheets/_rels/sheet{index}.xml.rels"] = relationships
    child(rels, "Relationship", {"Id": "rIdStyles", "Type": REL + "/styles", "Target": "styles.xml"}, namespace=PKG)
    root_rels = element("Relationships", namespace=PKG)
    child(root_rels, "Relationship", {"Id": "rId1", "Type": REL + "/officeDocument", "Target": "xl/workbook.xml"}, namespace=PKG)
    files.update({"[Content_Types].xml": xml(types), "_rels/.rels": xml(root_rels), "xl/workbook.xml": xml(book), "xl/_rels/workbook.xml.rels": xml(rels)})
    return files


def save_workbook(path, parts):
    def write(target):
        handle, temporary = tempfile.mkstemp(prefix=".finance-workbook-", suffix=".xlsx", dir=target.parent)
        os.close(handle)
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, content in parts.items():
                    archive.writestr(name, content)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    try:
        write(path)
        return path, None
    except PermissionError:
        alternative = path.with_name(path.stem + "_新版" + path.suffix)
        try:
            write(alternative)
        except PermissionError as exc:
            raise ValueError(f"{path.name}和{alternative.name}都无法写入，请关闭相应表格后重试。") from exc
        return alternative, f"{path.name}可能正在被WPS或Excel占用，原文件保留；新版保存为{alternative.name}。"


def export_workbooks(root, daily_rows, ledger_rows, records, ids, manifest):
    """Write two user-facing workbooks; never alter ledgers or review state."""
    root = Path(root).resolve()
    folder = root / "exports"
    if not folder.resolve().is_relative_to(root):
        raise ValueError("Excel导出目录必须位于当前工作目录内")
    folder.mkdir(parents=True, exist_ok=True)
    result = {"workbook_export_notes": []}
    setups = [("daily_workbook", "每日检索政策台账.xlsx", "每日检索政策", DAILY_FIELDS, daily_rows, 1, 6, [14, 31, 23, 30, 34, 28, 24, 40], (0,), ()),
              ("ledger_workbook", "财务政策资金台账.xlsx", "长期财务台账", LEDGER_FIELDS, ledger_rows, 0, 5, [32, 15, 30, 34, 28, 24, 17, 19, 34, 16], (9,), (6, 7))]
    for key, filename, name, fields, supplied, title_col, material_col, widths, dates, states in setups:
        supplied = list(supplied)
        numbers = {identity(row.get("编号及政策名称")) for row in supplied}
        details, detail_links, anchors, summaries = material_rows(root, numbers, records, ids, manifest)
        headers = list(fields)
        headers[material_col] = "资料与用途"
        rows, links, internal = [], [], []
        for index, item in enumerate(supplied, 5):
            row = [item.get(field, "") for field in fields]
            number = identity(item.get("编号及政策名称"))
            title, target = policy_title(row[title_col])
            row[title_col] = title
            if target:
                links.append((letters(title_col + 1) + str(index), target))
            row[material_col] = summaries[number]
            internal.append((letters(material_col + 1) + str(index), "'资料明细'!A" + str(anchors[number])))
            rows.append(row)
        notes = ["点政策名称看来源；点“资料与用途”查看资料明细，再打开正文或下载具体文件。",
                 "审核意见按编号在Codex回复，由系统回写；直接改Excel不会自动回写。"]
        main, main_rels = worksheet(name, "每日检索政策台账" if key == "daily_workbook" else "长期财务政策资金台账", notes, headers, rows, widths, dates, title_col + 1, links, internal, states)
        detail, detail_rels = worksheet("资料明细", "资料明细与使用说明",
            ["正文看政策条件；表样准备材料；历史指南、银行财政表按用途使用。",
             "返回政策台账（点击这里）。本页资料现状对应本次导出，历史检查事实以当日记录为准。"],
            DETAIL_FIELDS, details, [13, 45, 34, 27, 23, 58], links=detail_links,
            internal=[("A3", "'" + name + "'!A1")])
        written, note = save_workbook(folder / filename, package([(name, main, main_rels), ("资料明细", detail, detail_rels)]))
        result[key] = str(written)
        if note:
            result["workbook_export_notes"].append(note)
    return result
