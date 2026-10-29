"""Portable install tests use isolated package copies and temporary workspaces."""
import argparse
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("portable_bootstrap", ROOT / "runtime" / "bootstrap.py")
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class PortableRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sme-install-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "package"
        self.runtime = self.package / "runtime"
        for relative in b.STATIC_FILES:
            target = self.runtime / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / "runtime" / relative, target)
        templates = self.package / "templates" / "workspace"
        templates.mkdir(parents=True)
        (templates / "安装说明.txt").write_text("位置={{WORKSPACE}}；范围={{SCOPE}}；人员={{OPERATOR}}", "utf-8")
        self.target = self.root / "工作 空间"
        runtime_patch = patch.object(b, "RUNTIME", self.runtime)
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)

    def args(self, scope="policy", operator="模拟审核人", profile_import=None):
        return argparse.Namespace(workspace=str(self.target), scope=scope, operator=operator,
                                  profile_import=str(profile_import) if profile_import else None)

    def read(self, relative):
        return b.read_json(self.target / relative)

    def test_fresh_install_is_empty_and_does_not_register_automations(self):
        result = b.initialize(self.args())
        self.assertFalse(result["collection_performed"])
        self.assertFalse(result["automations_registered"])
        self.assertTrue((self.target / "exports" / "政策台账.csv").is_file())
        report = b.doctor(self.args())
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["counts"], {"records": 0, "screening_cases": 0})
        self.assertEqual(self.read("data/automation_setup.json")["automations"], [])
        self.assertIsNone(self.read("data/automation_setup.json")["daily_summary_time"])
        self.assertEqual(self.read("data/notification_receipts.json"), {})
        self.assertEqual(self.read("materials/materials_manifest.json"), {"attachments": []})
        self.assertFalse((self.runtime / "data").exists())
        rendered = (self.target / "安装说明.txt").read_text("utf-8")
        self.assertIn(self.target.as_posix(), rendered)
        self.assertNotIn("{{", rendered)

    def test_each_scope_has_only_its_requested_schedule_templates(self):
        for scope, expected in (("policy", ["policy_morning", "afternoon"]),
                                ("tender", ["tender_morning", "afternoon"]),
                                ("both", ["morning", "afternoon"])):
            self.target = self.root / scope
            b.initialize(self.args(scope))
            setup = self.read("data/automation_setup.json")
            self.assertEqual([entry["key"] for entry in setup["requested_schedule"]], expected)
            self.assertEqual(setup["requested_schedule"][-1]["scope"], scope)
            self.assertEqual(setup["requested_schedule"][-1]["local_time"], "16:30")
            self.assertEqual(len(setup["requested_schedule"]), 2)
            self.assertEqual(setup["requested_schedule"][0]["scope"], scope)
            self.assertEqual(setup["requested_schedule"][0]["review_opening"], scope in {"policy", "both"})

    def test_existing_unowned_file_is_never_overwritten(self):
        self.target.mkdir()
        filename = self.target / "human_review.py"
        filename.write_text("用户原文件", "utf-8")
        with self.assertRaisesRegex(ValueError, "同名"):
            b.initialize(self.args())
        self.assertEqual(filename.read_text("utf-8"), "用户原文件")
        self.assertFalse((self.target / "profile.json").exists())
        self.assertFalse((self.target / "monitor.py").exists())

    def test_existing_unregistered_runtime_data_requires_new_directory(self):
        (self.target / "data").mkdir(parents=True)
        (self.target / "data/monitor.sqlite").write_bytes(b"existing database")
        with self.assertRaisesRegex(ValueError, "未登记"):
            b.initialize(self.args())
        self.assertFalse((self.target / "profile.json").exists())

    def test_profile_import_does_not_import_automation_or_other_history(self):
        source = self.root / "private-profile.json"
        profile = {"company": {"name": "模拟企业"}, "confirmed_services": ["软件开发"],
                   "source_documents": [{"path": "inputs/not-transferred.pdf"}]}
        b.write_json(source, profile)
        result = b.initialize(self.args(profile_import=source))
        self.assertTrue(result["profile_imported"])
        self.assertEqual(self.read("profile.json"), profile)
        self.assertFalse((self.target / "inputs/not-transferred.pdf").exists())
        report = b.doctor(self.args())
        self.assertTrue(any("证据文件未迁入" in warning for warning in report["warnings"]))
        self.assertEqual(report["counts"]["records"], 0)

    def test_profile_with_credential_or_foreign_automation_is_rejected_before_write(self):
        for field in ("access_token", "automation_id", "thread_id", "cookies"):
            source = self.root / "invalid.json"
            b.write_json(source, {"company": {"name": "模拟企业"}, "nested": {field: "fake-test-value"}})
            with self.assertRaisesRegex(ValueError, "凭据"):
                b.initialize(self.args(profile_import=source))
            self.assertFalse(self.target.exists())

    def test_init_again_preserves_data_and_rejects_scope_switch(self):
        b.initialize(self.args())
        b.write_json(self.target / "data/notification_receipts.json", {"test-receipt": True})
        result = b.initialize(self.args())
        self.assertEqual(result["status"], "upgraded")
        self.assertEqual(self.read("data/notification_receipts.json"), {"test-receipt": True})
        with self.assertRaisesRegex(ValueError, "scope/operator"):
            b.initialize(self.args("both"))

    def test_upgrade_preserves_company_review_events_and_schedule_registration(self):
        b.initialize(self.args())
        spec = importlib.util.spec_from_file_location("installed_monitor_test", self.target / "monitor.py")
        monitor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(monitor)
        db = monitor.connect(self.target / "data/monitor.sqlite")
        try:
            item = {"kind": "policy", "source_id": "test", "source_url": "https://example.invalid/notice",
                    "source_is_list": False, "title": "模拟科技项目", "original_verified": False}
            changes = monitor.ingest(db, {"run_id": "test", "checked_at": "2026-10-29T09:00:00+08:00",
                "coverage": [{"source_id": "test", "status": "partial"}], "items": [item]}, {})
            monitor.save_review(db, changes["new"][0], "先补资料", "模拟人员测试反馈")
        finally:
            db.close()
        profile = self.read("profile.json")
        profile["company"]["name"] = "安装后本地确认的模拟企业"
        b.write_json(self.target / "profile.json", profile)
        registration = self.read("data/automation_setup.json")
        registration["automations"] = [{"id": "recipient-test-id", "key": "policy_morning"}]
        b.write_json(self.target / "data/automation_setup.json", registration)
        immutable_snapshots = {name: (self.target / name).read_bytes() for name in
            ("profile.json", "data/monitor.sqlite", "data/human_review.sqlite", "data/automation_setup.json")}
        source = self.runtime / "monitor.py"
        source.write_text(source.read_text("utf-8") + "\n# simulated next release\n", "utf-8")
        result = b.upgrade(self.args())
        self.assertEqual(result["conflicts"], [])
        self.assertIn("next release", (self.target / "monitor.py").read_text("utf-8"))
        for name, original in immutable_snapshots.items():
            self.assertEqual((self.target / name).read_bytes(), original, name)
        db = sqlite3.connect(self.target / "data/monitor.sqlite")
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT review_state FROM records").fetchone()[0], "先补资料")
        finally:
            db.close()

    def test_customized_rules_and_code_get_pending_copy_without_overwrite(self):
        b.initialize(self.args())
        (self.target / "安装说明.txt").write_text("自定义规则", "utf-8")
        filename = self.target / "monitor.py"
        original_code = filename.read_text("utf-8") + "\n# employee customization\n"
        filename.write_text(original_code, "utf-8")
        result = b.upgrade(self.args())
        self.assertEqual({entry["path"] for entry in result["conflicts"]}, {"安装说明.txt", "monitor.py"})
        self.assertEqual((self.target / "安装说明.txt").read_text("utf-8"), "自定义规则")
        self.assertEqual(filename.read_text("utf-8"), original_code)
        self.assertTrue((self.target / "monitor.py.new").exists())
        self.assertTrue((self.target / "安装说明.txt.new").exists())
        b.upgrade(self.args())
        self.assertFalse((self.target / "monitor.py.new.2").exists())

    def test_tampered_manifest_cannot_escape_workspace(self):
        b.initialize(self.args())
        manifest = self.read(b.MANIFEST)
        manifest["files"]["../outside.txt"] = {"installed_sha256": "fake"}
        b.write_json(self.target / b.MANIFEST, manifest)
        with self.assertRaisesRegex(ValueError, "不安全"):
            b.upgrade(self.args())
        self.assertFalse((self.root / "outside.txt").exists())

    def test_source_or_cache_cannot_be_a_workspace(self):
        for path in (self.runtime, self.runtime.parent, self.runtime / "nested", self.package / "some-other-subdirectory",
                     self.root / ".codex/plugins/cache/local-plugin/work",
                     self.root / ".agents/skills/sme-opportunity-monitor/work"):
            args = self.args()
            args.workspace = str(path)
            with self.assertRaises(ValueError):
                b.initialize(args)

    def test_actual_distribution_templates_render_and_empty_exports_succeed(self):
        with patch.object(b, "RUNTIME", ROOT / "runtime"):
            result = b.initialize(self.args("both", "安装验收测试"))
            self.assertEqual(result["status"], "initialized")
            for filename in ("README.md", "日常监测与人工判断规则.txt", "人工每日审核说明.txt",
                             "Codex定时监测提示词.txt", "手动试跑提示词.txt"):
                text = (self.target / filename).read_text("utf-8")
                self.assertNotIn("{{WORKSPACE}}", text)
                self.assertNotIn("{{SCOPE}}", text)
                self.assertNotIn("{{OPERATOR}}", text)
            report = b.doctor(self.args())
            self.assertEqual(report["status"], "healthy")
            self.assertEqual(report["counts"]["screening_cases"], 0)

    def test_doctor_runs_from_package_runtime_for_existing_independent_workspace(self):
        # bootstrap belongs to the installable package, not the mutable workspace.
        # Its CLI must inspect the workspace successfully from any current dir.
        with patch.object(b, "RUNTIME", ROOT / "runtime"):
            b.initialize(self.args("both", "安装验收测试"))
        self.assertFalse((self.target / "bootstrap.py").exists())
        completed = subprocess.run([sys.executable, "-B", str(ROOT / "runtime/bootstrap.py"),
                                    "doctor", "--workspace", str(self.target)],
                                   cwd=self.target, capture_output=True, encoding="utf-8")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
