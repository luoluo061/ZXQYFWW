"""The finance edition must install separately and preserve company/human facts."""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


bootstrap = module("finance_bootstrap", ROOT / "runtime/bootstrap.py")
installer = module("finance_installer", ROOT / "install.py")


class FinanceInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sme-finance-install-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / "财务独立工作区"

    def args(self, **changes):
        values = dict(workspace=str(self.workspace), scope="policy", operator="模拟财务负责人",
                      edition="finance", profile_import=None)
        values.update(changes)
        return argparse.Namespace(**values)

    def read(self, name):
        return json.loads((self.workspace / name).read_text("utf-8"))

    def test_finance_init_has_two_schedules_and_empty_finance_exports(self):
        result = bootstrap.initialize(self.args())
        self.assertEqual(result["edition"], "finance")
        self.assertEqual(self.read("profile.json")["workflow"], "finance")
        self.assertEqual(self.read("data/installation.json")["edition"], "finance")
        setup = self.read("data/automation_setup.json")
        self.assertEqual([x["local_time"] for x in setup["requested_schedule"]], ["09:00", "16:30"])
        self.assertTrue(all(x["edition"] == "finance" for x in setup["requested_schedule"]))
        self.assertEqual(setup["automations"], [])
        for name in ["财务政策资金台账.csv", "每日检索政策台账.csv", "财务政策资金台账.xlsx", "每日检索政策台账.xlsx"]:
            self.assertTrue((self.workspace / "exports" / name).is_file())
        self.assertTrue((self.workspace / "finance_workbooks.py").is_file())
        self.assertTrue((self.workspace / "tests/test_finance_workbooks.py").is_file())
        for name in ["财务政策资金台账.xlsx", "每日检索政策台账.xlsx"]:
            with zipfile.ZipFile(self.workspace / "exports" / name) as archive:
                self.assertIsNone(archive.testzip())
                document = ET.fromstring(archive.read("xl/workbook.xml"))
                namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                sheets = document.findall("s:sheets/s:sheet", namespace)
                self.assertEqual(len(sheets), 2)
                self.assertIn("资料明细", [sheet.attrib["name"] for sheet in sheets])
        self.assertEqual(bootstrap.doctor(self.args())["status"], "healthy")

    def test_import_preserves_facts_but_changes_department_search_directions(self):
        company = {"name": "Fixture Finance Ltd", "registered_province": None}
        qualifications = [{"name": "示例已核资格", "status": "原证据仍待外部核验"}]
        profile = {"company": company, "confirmed_services": ["示例真实业务"],
                   "confirmed_qualifications": qualifications,
                   "discovery_keywords": {"policy": ["原业务检索词"]},
                   "policy_scope": {"directions": ["资质评价认定"], "priority_regions": ["示例地区"]}}
        path = self.root / "private_profile.json"
        path.write_text(json.dumps(profile, ensure_ascii=False), "utf-8")
        bootstrap.initialize(self.args(profile_import=str(path)))
        actual = self.read("profile.json")
        self.assertEqual(actual["company"], company)
        self.assertEqual(actual["confirmed_qualifications"], qualifications)
        self.assertEqual(actual["policy_scope"]["priority_regions"], ["示例地区"])
        self.assertEqual(actual["finance_scope"]["previous_policy_keywords"], ["原业务检索词"])
        self.assertNotIn("资质评价认定", actual["policy_scope"]["directions"])
        self.assertIn("研发费用加计扣除", actual["discovery_keywords"]["policy"])

    def test_after_private_import_configure_is_backed_up_and_idempotent(self):
        bootstrap.initialize(self.args())
        private = {"company": {"name": "After Import Ltd"}, "confirmed_services": ["真实业务"],
                   "finance_profile": {"taxpayer_identity": {"value": "原资料值", "status": "unknown"}}}
        path = self.workspace / "profile.json"
        path.write_text(json.dumps(private, ensure_ascii=False), "utf-8")
        original = path.read_bytes()
        bootstrap.configure_finance(self.args())
        converted = path.read_bytes()
        self.assertEqual(self.read("profile.json")["company"], private["company"])
        self.assertEqual(self.read("profile.json")["finance_profile"]["taxpayer_identity"]["value"], "原资料值")
        backups = list((self.workspace / "inputs").glob("profile_before_finance_config_*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        bootstrap.configure_finance(self.args())
        self.assertEqual(path.read_bytes(), converted)
        self.assertEqual(len(list((self.workspace / "inputs").glob("profile_before_finance_config_*.json"))), 1)

    def test_finance_rejects_tender_scope_before_any_write(self):
        with self.assertRaisesRegex(ValueError, "scope policy"):
            bootstrap.initialize(self.args(scope="tender"))
        self.assertFalse(self.workspace.exists())

    def test_upgrade_preserves_finance_store_profile_and_registration(self):
        bootstrap.initialize(self.args())
        finance_store = self.workspace / "data/finance_workflow.sqlite"
        finance_store.write_bytes(b"opaque-preserved-finance-store")
        paths = [finance_store, self.workspace / "profile.json", self.workspace / "data/monitor.sqlite",
                 self.workspace / "data/human_review.sqlite", self.workspace / "data/automation_setup.json"]
        snapshots = {path: path.read_bytes() for path in paths}
        bootstrap.upgrade(self.args())
        for path, expected in snapshots.items():
            self.assertEqual(path.read_bytes(), expected)

    def test_finance_and_general_user_skills_coexist(self):
        target = self.root / "skills"
        finance = installer.install(target, edition="finance")
        general = installer.install(target, edition="general")
        self.assertNotEqual(finance["skill_directory"], general["skill_directory"])
        self.assertTrue(installer.doctor(target, "finance")["ok"])
        self.assertTrue(installer.doctor(target, "general")["ok"])

    def test_finance_skill_payload_carries_portable_workbook_module_and_tests(self):
        files = installer.payload(edition="finance")
        self.assertIn("runtime/finance_workbooks.py", files)
        self.assertIn("runtime/tests/test_finance_workbooks.py", files)

    def test_known_office_region_enables_discovery_without_tax_eligibility(self):
        company = {"name": "Fixture Local Ltd", "located_province": "云南", "located_city": "昆明",
                   "registered_province": None, "registered_city": None}
        path = self.root / "private_local_profile.json"
        path.write_text(json.dumps({"company": company}, ensure_ascii=False), "utf-8")
        bootstrap.initialize(self.args(profile_import=str(path)))
        sources = self.read("sources.json")
        regional = [source for source in sources["sources"] if source.get("activation", {}).get("province") == "云南"]
        self.assertTrue(regional)
        self.assertTrue(all(source["enabled"] for source in regional))
        self.assertTrue(all(source["activation_decision"]["eligibility"] == "unknown" for source in regional))
        self.assertEqual(self.read("profile.json")["company"], company)
        self.assertIsNone(self.read("profile.json")["finance_profile"]["tax_jurisdiction"]["province"])
        regional[0]["enabled"] = False
        bootstrap.write_json(self.workspace / "sources.json", sources)
        bootstrap.configure_finance(self.args())
        self.assertFalse(next(source for source in self.read("sources.json")["sources"] if source["id"] == regional[0]["id"])["enabled"])

    def test_no_known_region_does_not_invent_yunnan_search_target(self):
        bootstrap.initialize(self.args())
        regional = [source for source in self.read("sources.json")["sources"] if source.get("activation")]
        self.assertTrue(regional)
        self.assertTrue(all(source["enabled"] is False for source in regional))


if __name__ == "__main__":
    unittest.main()
