"""Native finance workbook invariants, using temporary fictional records only."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
import zipfile

SOURCE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("finance_workbooks_tested", SOURCE / "finance_workbooks.py")
w = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w)
N = {"s": w.NS, "r": w.REL, "p": w.PKG}


class FinanceWorkbookTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="finance-xlsx-test-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.ids = {"fixture1": "P001", "fixture2": "P002"}
        self.records = [{"id": key, "data": json.dumps({"title": "模拟政策" + key, "source_url": "https://official.example/" + key,
            "original_url": "https://official.example/" + key, "original_verified": True})} for key in self.ids]
        self.daily = [{field: "" for field in w.DAILY_FIELDS} for _ in self.ids]
        self.ledger = [{field: "" for field in w.LEDGER_FIELDS} for _ in self.ids]
        for number, (key, display) in enumerate(self.ids.items()):
            title = display + " 模拟政策\n原文：https://official.example/" + key
            self.daily[number].update({"检索日期": "2026-10-29", "编号及政策名称": title, "人工意见及下一步": "待财务审核；待人员判断"})
            self.ledger[number].update({"编号及政策名称": title, "最近跟进": "2026-10-29T16:15:45+08:00", "是否参与": "待人员判断", "办理进展": "待财务审核"})
        self.manifest = {"attachments": []}

    def export(self, daily=None, ledger=None, manifest=None):
        return w.export_workbooks(self.root, self.daily if daily is None else daily, self.ledger if ledger is None else ledger,
            self.records, self.ids, self.manifest if manifest is None else manifest)

    def parts(self, result, kind="daily_workbook"):
        with zipfile.ZipFile(result[kind]) as archive:
            self.assertIsNone(archive.testzip())
            return {name: archive.read(name) for name in archive.namelist()}

    def sheet(self, parts, index=1):
        return ET.fromstring(parts[f"xl/worksheets/sheet{index}.xml"])

    def text(self, sheet, address):
        cell = sheet.find(f".//s:c[@r='{address}']", N)
        return "".join(cell.itertext()) if cell is not None else None

    def attachment(self, title="模拟申报表.doc", **kwargs):
        relative = "materials/fixture.doc"
        data = b"fictional attachment bytes"
        target = self.root / relative
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)
        entry = {"item_id": "fixture1", "short_id": "P001", "title": title,
                 "url": "https://official.example/form.doc", "source_url": "https://official.example/fixture1",
                 "status": "downloaded", "local_path": relative,
                 "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
        entry.update(kwargs)
        return entry

    def test_two_native_workbooks_columns_dates_filters_and_anchors(self):
        result = self.export()
        for kind, last_col, title_col, material_col in [("daily_workbook", "H", "B", "G"), ("ledger_workbook", "J", "A", "F")]:
            parts = self.parts(result, kind)
            root = self.sheet(parts)
            self.assertEqual(root.find("s:autoFilter", N).get("ref"), f"A4:{last_col}6")
            self.assertEqual(root.find("s:sheetViews/s:sheetView/s:pane", N).get("state"), "frozen")
            book = ET.fromstring(parts["xl/workbook.xml"])
            self.assertEqual(len(book.find("s:sheets", N)), 2)
            self.assertEqual(self.text(root, title_col + "5"), "P001 模拟政策")
            self.assertEqual(self.text(root, material_col + "4"), "资料与用途")
            date_cell = root.find(".//s:c[@r='" + ("A5" if kind == "daily_workbook" else "J5") + "']", N)
            self.assertNotEqual(date_cell.get("t"), "inlineStr")
            self.assertEqual(date_cell.find("s:v", N).text, "46324")
            detail = self.sheet(parts, 2)
            anchors = root.find("s:hyperlinks", N)
            for number in (1, 2):
                link = next(x for x in anchors if x.get("ref") == material_col + str(number + 4))
                self.assertIsNone(link.get("{" + w.REL + "}id"))
                self.assertEqual(self.text(detail, link.get("location").split("!")[1]), f"P{number:03d}")
            for name, content in parts.items():
                if name.endswith(".rels"):
                    for relationship in ET.fromstring(content):
                        if relationship.get("Type") == w.REL + "/hyperlink":
                            self.assertTrue(relationship.get("Target").startswith("https://"))
            self.assertNotIn(b"HYPERLINK(", parts["xl/worksheets/sheet1.xml"])

    def test_no_attachment_still_has_separate_missing_material_row(self):
        parts = self.parts(self.export())
        detail = self.sheet(parts, 2)
        self.assertIn("尚未取得独立企业表样", self.text(detail, "D6"))
        self.assertEqual(self.text(detail, "E6"), "暂无可下载文件")
        links = detail.find("s:hyperlinks", N)
        self.assertFalse(any(link.get("ref") == "E6" for link in links))
        self.assertEqual(self.text(detail, "A7"), "P002")

    def test_download_requires_existing_hash_and_size_matches(self):
        good = self.attachment()
        invalid = self.attachment("文件已变化.doc", sha256="0" * 64)
        missing = self.attachment("不存在.doc", local_path="materials/absent.doc")
        unhashed = self.attachment("没有哈希.doc", sha256=None)
        bad_size = self.attachment("大小变化.doc", size_bytes=99999)
        manifest = {"attachments": [good, invalid, missing, unhashed, bad_size]}
        detail = self.sheet(self.parts(self.export(manifest=manifest)), 2)
        self.assertEqual(self.text(detail, "D6"), "已取得并核对留存文件")
        self.assertEqual(self.text(detail, "E6"), "下载文件")
        actual_links = {x.get("ref") for x in detail.find("s:hyperlinks", N)}
        self.assertIn("E6", actual_links)
        for row in (7, 8, 9, 10):
            self.assertNotIn("E" + str(row), actual_links)
            self.assertEqual(self.text(detail, "E" + str(row)), "暂无可下载文件")

    def test_material_types_distinguish_purpose_without_assuming_all_excel_bank(self):
        entries = [self.attachment("正文.html", url="https://official.example/notice.html"),
                   self.attachment("历史参考_指南.pdf"),
                   self.attachment("企业申请表.xls", note="企业自行准备本表"),
                   self.attachment("贴息资金审核表.xls", note="银行财政汇总，不是企业直接申请表")]
        detail = self.sheet(self.parts(self.export(manifest={"attachments": entries})), 2)
        self.assertIn("看支持对象", self.text(detail, "C6"))
        self.assertIn("旧口径", self.text(detail, "C7"))
        self.assertIn("填报主体", self.text(detail, "C8"))
        self.assertIn("银行或财政", self.text(detail, "C9"))
        self.assertNotIn("银行或财政", self.text(detail, "C8"))

    def test_unacquired_and_login_materials_never_offer_downloads(self):
        entries = [self.attachment("企业表样待核", status="inline_notice_verified", local_path=None),
                   self.attachment("需登录企业表", status="auth_required", local_path=None)]
        detail = self.sheet(self.parts(self.export(manifest={"attachments": entries})), 2)
        self.assertEqual(self.text(detail, "D7"), "需人员正常登录后取得")
        self.assertFalse(any(x.get("ref") in {"E6", "E7"} for x in detail.find("s:hyperlinks", N)))

    def test_unsafe_links_and_formula_xml_injection_are_not_executable(self):
        self.daily[0]["能带来什么"] = '=SUM(1,2)<node>&\x00'
        self.daily[0]["编号及政策名称"] = "P001 =DANGEROUS()\n原文：https://user:password@official.example/private"
        self.records[0]["data"] = json.dumps({"source_url": "javascript:alert(1)"})
        malicious = self.attachment(url="https://official.example/file?access_token=private-value")
        parts = self.parts(self.export(manifest={"attachments": [malicious]}))
        for name in ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"):
            root = ET.fromstring(parts[name])
            self.assertEqual(root.findall(".//s:f", N), [])
        all_rels = b"".join(data for name, data in parts.items() if name.endswith(".rels"))
        self.assertNotIn(b"private-value", all_rels)
        self.assertNotIn(b"password@", all_rels)
        self.assertNotIn(b"javascript:", all_rels)
        self.assertIn("=SUM(1,2)<node>&�", self.text(self.sheet(parts), "D5"))

    def test_unsafe_path_cannot_claim_material_download(self):
        outside = self.root.parent / (self.root.name + "-outside")
        outside.write_bytes(b"outside")
        self.addCleanup(outside.unlink)
        entry = self.attachment(local_path=str(outside), sha256=hashlib.sha256(b"outside").hexdigest())
        detail = self.sheet(self.parts(self.export(manifest={"attachments": [entry]})), 2)
        self.assertIn("路径待核", self.text(detail, "D6"))

    def test_empty_results_are_valid_and_do_not_invent_policy(self):
        parts = self.parts(self.export(daily=[], ledger=[]))
        main = self.sheet(parts)
        self.assertEqual(len(main.find("s:sheetData", N)), 4)
        self.assertIsNone(main.find("s:autoFilter", N))
        self.assertIn("本轮没有正式候选", self.text(self.sheet(parts, 2), "B5"))
        self.assertFalse((self.root / "data").exists())

    def test_locked_primary_uses_alternative_and_preserves_old_file(self):
        target = self.root / "exports/每日检索政策台账.xlsx"
        target.parent.mkdir()
        target.write_bytes(b"existing open file")
        original = w.os.replace
        def replace(source, destination):
            if Path(destination) == target:
                raise PermissionError("fixture lock")
            return original(source, destination)
        with patch.object(w.os, "replace", side_effect=replace):
            result = self.export()
        self.assertEqual(target.read_bytes(), b"existing open file")
        self.assertTrue(result["daily_workbook"].endswith("_新版.xlsx"))
        self.assertTrue(result["workbook_export_notes"])
        self.parts(result)
        self.assertFalse(list(target.parent.glob(".finance-workbook-*")))

    def test_both_locked_files_fail_clearly_and_cleanup(self):
        with patch.object(w.os, "replace", side_effect=PermissionError("fixture lock")):
            with self.assertRaisesRegex(ValueError, "都无法写入"):
                self.export()
        self.assertFalse(list((self.root / "exports").glob(".finance-workbook-*")))

    def test_timestamp_uses_beijing_calendar_and_material_override_is_ignored(self):
        self.ledger[0]["最近跟进"] = "2026-10-28T18:00:00Z"
        self.daily[0]["资料下载"] = "已下载且已申报（假的显示文字）"
        before = copy.deepcopy((self.daily, self.ledger, self.manifest, self.records))
        result = self.export()
        self.assertEqual(self.text(self.sheet(self.parts(result, "ledger_workbook")), "J5"), "46324")
        self.assertNotIn("已申报", self.text(self.sheet(self.parts(result)), "G5"))
        self.assertEqual((self.daily, self.ledger, self.manifest, self.records), before)

    def test_invalid_source_is_never_called_original_and_zero_text_survives(self):
        item = json.loads(self.records[0]["data"])
        item["original_verified"] = False
        self.records[0]["data"] = json.dumps(item)
        detail = self.sheet(self.parts(self.export()), 2)
        self.assertEqual(self.text(detail, "E5"), "打开来源页面")
        self.assertEqual(w.clean(0), "0")
        for url in ["file:///tmp/form", "https://official.example/#token=secret", "https://official.example/?session_id=secret", "https://official.example:abc/file"]:
            self.assertIsNone(w.safe_url(url))

    def test_related_current_rules_and_forms_are_visible_and_deduplicated(self):
        item = json.loads(self.records[0]["data"])
        item["related_policy_documents"] = [
            {"title": "模拟现行续行公告", "url": "https://official.example/current", "verified_section": "第五条第4项"},
            {"title": "重复续行公告", "url": "https://official.example/current"},
            {"title": "重复基础文", "url": item["original_url"]},
            {"title": "不安全链接", "url": "https://official.example/?token=secret"}]
        item["related_items"] = [{"title": "模拟当前申报表依据", "source_url": "https://official.example/form-policy"}]
        self.records[0]["data"] = json.dumps(item)
        parts = self.parts(self.export())
        detail = self.sheet(parts, 2)
        self.assertEqual(self.text(detail, "B6"), "模拟现行续行公告")
        self.assertEqual(self.text(detail, "D6"), "采集记录已读指定段落")
        self.assertIn("第五条第4项", self.text(detail, "F6"))
        self.assertEqual(self.text(detail, "B7"), "模拟当前申报表依据")
        self.assertEqual(self.text(detail, "D7"), "关联依据，阅读层级按采集记录核")
        rels = ET.fromstring(parts["xl/worksheets/_rels/sheet2.xml.rels"])
        targets = [node.get("Target") for node in rels]
        self.assertEqual(targets.count("https://official.example/current"), 1)
        self.assertEqual(targets.count("https://official.example/fixture1"), 1)
        self.assertNotIn("secret", " ".join(targets))

    def test_download_handler_does_not_override_real_document_name(self):
        form = self.attachment("企业申请表.doc", url="https://official.example/download.aspx?id=1")
        self.assertEqual(w.material_kind(form)[0], "表样")
        local_form = self.attachment("材料附件", url="https://official.example/download.aspx?id=2",
                                     local_path="materials/企业申请表.xls", note="企业按要求填写申请表")
        self.assertEqual(w.material_kind(local_form)[0], "表样")
        ordinary_pdf = self.attachment("政策研究材料.pdf", url="https://official.example/download.aspx?id=3")
        self.assertEqual(w.material_kind(ordinary_pdf)[0], "政策附件")
        result = self.export(manifest={"attachments": [form]})
        detail = self.sheet(self.parts(result), 2)
        self.assertIn("填报主体", self.text(detail, "C6"))
        self.assertEqual(self.text(detail, "E6"), "下载文件")

    def test_long_cjk_content_expands_height_without_truncation_or_tiny_font(self):
        text = "待核具体财务资料和期间。" * 35
        self.daily[0]["要核实什么"] = text
        parts = self.parts(self.export())
        main = self.sheet(parts)
        row = main.find("s:sheetData/s:row[@r='5']", N)
        self.assertGreater(float(row.get("ht")), 74)
        self.assertLessEqual(float(row.get("ht")), 409)
        self.assertEqual(self.text(main, "E5"), text)
        fonts = ET.fromstring(parts["xl/styles.xml"]).find("s:fonts", N)
        self.assertTrue(all(float(font.find("s:sz", N).get("val")) >= 10 for font in fonts))
        self.assertEqual(w.wrapped_row_height(["很长的材料" * 5000], [10], 68), 409)


if __name__ == "__main__":
    unittest.main()
