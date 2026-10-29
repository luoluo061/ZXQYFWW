import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("skill_install", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class SkillInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skills = self.root / "skills"
        self.target = self.skills / installer.SKILL_NAME

    def tearDown(self):
        self.temp.cleanup()

    def test_full_payload_and_idempotent_install_no_user_data(self):
        installer.install(self.skills)
        self.assertTrue(installer.doctor(self.skills)["ok"])
        self.assertTrue((self.target / "runtime/bootstrap.py").is_file())
        self.assertTrue((self.target / "templates/workspace/人工每日审核说明.txt").is_file())
        self.assertTrue((self.target / "tools/profile_bundle.py").is_file())
        self.assertFalse((self.target / "data").exists())
        self.assertEqual(installer.install(self.skills)["conflicts"], [])

    def test_changed_user_skill_preserved_with_new_copy(self):
        installer.install(self.skills)
        path = self.target / "SKILL.md"
        path.write_text("user's local guidance", "utf-8")
        result = installer.install(self.skills)
        self.assertIn("SKILL.md", result["conflicts"])
        self.assertEqual(path.read_text("utf-8"), "user's local guidance")
        self.assertTrue(path.with_suffix(".md.new").is_file())
        self.assertFalse(installer.doctor(self.skills)["ok"])

    def test_unmanaged_skill_not_overwritten(self):
        self.target.mkdir(parents=True)
        path = self.target / "SKILL.md"
        path.write_text("existing", "utf-8")
        with self.assertRaises(ValueError):
            installer.install(self.skills)
        self.assertEqual(path.read_text("utf-8"), "existing")

    def test_conflict_preflight_before_other_files_written(self):
        installer.install(self.skills)
        (self.target / "SKILL.md").write_text("changed", "utf-8")
        (self.target / "SKILL.md.new").write_text("do not replace", "utf-8")
        missing = self.target / "runtime/profile.example.json"
        missing.unlink()
        with self.assertRaises(ValueError):
            installer.install(self.skills)
        self.assertFalse(missing.exists())

    def test_nested_runtime_data_rejected(self):
        source = self.root / "source"
        (source / "skills" / installer.SKILL_NAME).mkdir(parents=True)
        (source / "runtime/data").mkdir(parents=True)
        (source / "runtime/data/history.json").write_text("{}", "utf-8")
        with self.assertRaises(ValueError):
            installer.payload(source)

    def test_marker_escape_rejected_by_doctor(self):
        installer.install(self.skills)
        import json
        path = self.target / installer.MARKER
        state = json.loads(path.read_text("utf-8"))
        state["files"]["../outside"] = "not a hash"
        path.write_text(json.dumps(state), "utf-8")
        with self.assertRaises(ValueError):
            installer.doctor(self.skills)

    def test_symlink_parent_rejected(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation is unavailable")
        with self.assertRaises(ValueError):
            installer.install(link / "skills")
        self.assertEqual(list(real.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
