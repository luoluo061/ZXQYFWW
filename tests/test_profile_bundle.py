import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import warnings
import zipfile

SPEC = importlib.util.spec_from_file_location("profile_bundle", Path(__file__).resolve().parents[1] / "tools" / "profile_bundle.py")
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


class ProfileBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "private source"
        self.source.mkdir()
        self.evidence = self.source / "inputs" / "说明.pdf"
        self.evidence.parent.mkdir()
        self.evidence.write_bytes(b"private evidence bytes")
        self.profile = {"company": {"name": "Fixture Ltd"},
                        "source_documents": [{"path": "inputs/说明.pdf", "sha256": bundle.sha256(self.evidence.read_bytes())}]}
        self.profile_path = self.source / "profile.json"
        self.write_profile()
        self.archive = self.root / "private.zip"
        self.workspace = self.root / "recipient workspace"

    def tearDown(self):
        self.tmp.cleanup()

    def write_profile(self):
        self.profile_path.write_text(json.dumps(self.profile, ensure_ascii=False), encoding="utf-8")

    def pack(self):
        return bundle.pack(self.profile_path, self.archive)

    def rewrite_zip(self, mutator):
        with zipfile.ZipFile(self.archive) as source:
            contents = {info.filename: source.read(info) for info in source.infolist()}
        mutator(contents)
        with zipfile.ZipFile(self.archive, "w") as target:
            for name, content in contents.items():
                target.writestr(name, content)

    def test_relative_paths_import_and_hash_verification(self):
        packed = self.pack()
        self.assertFalse(packed["uploaded"])
        self.assertEqual(1, packed["evidence_files"])
        with zipfile.ZipFile(self.archive) as archive:
            profile = json.loads(archive.read("profile.json"))
            self.assertTrue(profile["source_documents"][0]["path"].startswith("evidence/"))
            self.assertNotIn(str(self.source), archive.read("profile.json").decode("utf-8"))
        result = bundle.unpack(self.archive, self.workspace)
        self.assertFalse(result["reused_evidence"])
        imported = json.loads((self.workspace / "profile.json").read_text("utf-8"))
        evidence = Path(imported["source_documents"][0]["path"])
        self.assertTrue(evidence.is_relative_to(self.workspace))
        self.assertEqual(self.evidence.read_bytes(), evidence.read_bytes())
        self.assertTrue(bundle.doctor(workspace_path=self.workspace)["ok"])
        self.assertTrue(bundle.doctor(bundle_path=self.archive)["ok"])
        self.assertTrue(bundle.unpack(self.archive, self.workspace)["reused_evidence"])

    def test_only_explicit_documents_no_scheduler_or_db(self):
        (self.source / "monitor.sqlite").write_bytes(b"ledger")
        (self.source / "automation_setup.json").write_text('{"id":"old"}', encoding="utf-8")
        (self.source / "cookie.txt").write_text("private login", encoding="utf-8")
        self.pack()
        with zipfile.ZipFile(self.archive) as archive:
            names = archive.namelist()
        self.assertEqual(4, len(names))
        self.assertFalse(any("automation" in name or ".sqlite" in name or "cookie" in name for name in names))

    def test_database_reference_is_rejected(self):
        database = self.source / "monitor.sqlite"
        database.write_bytes(b"ledger")
        self.profile["source_documents"] = [{"path": "monitor.sqlite"}]
        self.write_profile()
        with self.assertRaisesRegex(ValueError, "文档证据"):
            self.pack()

    def test_source_sha_mismatch_rejected_without_output(self):
        self.profile["source_documents"][0]["sha256"] = "0" * 64
        self.write_profile()
        with self.assertRaisesRegex(ValueError, "SHA256"):
            self.pack()
        self.assertFalse(self.archive.exists())

    def test_external_evidence_requires_explicit_flag(self):
        outside = self.root / "outside.pdf"
        outside.write_bytes(b"explicit external")
        self.profile["source_documents"] = [{"path": str(outside)}]
        self.write_profile()
        with self.assertRaisesRegex(ValueError, "allow-external-evidence"):
            self.pack()
        bundle.pack(self.profile_path, self.archive, allow_external_evidence=True)
        self.assertTrue(bundle.doctor(bundle_path=self.archive)["ok"])

    def test_no_external_path_search_when_reference_missing(self):
        self.evidence.unlink()
        # A file with the same basename elsewhere must not be discovered.
        (self.root / "说明.pdf").write_bytes(b"wrong document")
        with self.assertRaises(FileNotFoundError):
            self.pack()

    def test_tamper_rejected_before_workspace_is_created(self):
        self.pack()
        def tamper(contents):
            evidence = next(name for name in contents if name.startswith("evidence/"))
            contents[evidence] = b"tampered"
        self.rewrite_zip(tamper)
        with self.assertRaises(ValueError):
            bundle.unpack(self.archive, self.workspace)
        self.assertFalse(self.workspace.exists())

    def test_traversal_and_absolute_windows_paths_rejected(self):
        for dangerous in ["../escape.txt", "/absolute.txt", "C:/escape.txt", "evidence/../x.pdf", "evidence\\x.pdf", "evidence/CON.pdf", "evidence/file?.pdf"]:
            with self.subTest(path=dangerous):
                self.pack()
                self.rewrite_zip(lambda contents: contents.update({dangerous: b"escape"}))
                with self.assertRaises(ValueError):
                    bundle.unpack(self.archive, self.workspace)
                self.archive.unlink()
        self.assertFalse(self.workspace.exists())

    def test_source_symlink_not_followed(self):
        linked = self.source / "linked.pdf"
        try:
            linked.symlink_to(self.evidence)
        except (OSError, NotImplementedError):
            self.skipTest("当前Windows会话没有创建符号链接权限")
        self.profile["source_documents"] = [{"path": "linked.pdf"}]
        self.write_profile()
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.pack()

    def test_duplicate_case_conflicting_and_symlink_members_rejected(self):
        for mode in ("duplicate", "case", "symlink"):
            with self.subTest(mode=mode):
                self.pack()
                with zipfile.ZipFile(self.archive, "a") as archive:
                    if mode == "duplicate":
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            archive.writestr("profile.json", b"{}")
                    elif mode == "case":
                        archive.writestr("PROFILE.JSON", b"{}")
                    else:
                        entry = zipfile.ZipInfo("evidence/link.pdf")
                        entry.create_system = 3
                        entry.external_attr = 0o120777 << 16
                        archive.writestr(entry, "../../outside")
                with self.assertRaises(ValueError):
                    bundle.unpack(self.archive, self.workspace)
                self.archive.unlink()

    def test_existing_profile_requires_replace_and_backup_keeps_db_untouched(self):
        self.pack()
        self.workspace.mkdir()
        old_profile = b'{"company":{"name":"Other"}}'
        (self.workspace / "profile.json").write_bytes(old_profile)
        (self.workspace / "data").mkdir()
        database = self.workspace / "data" / "human_review.sqlite"
        database.write_bytes(b"human history must stay")
        automation = self.workspace / "data" / "automation_setup.json"
        automation.write_bytes(b"real recipient registration")
        with self.assertRaisesRegex(ValueError, "replace-profile"):
            bundle.unpack(self.archive, self.workspace)
        self.assertFalse((self.workspace / "inputs").exists())
        result = bundle.unpack(self.archive, self.workspace, replace_profile=True)
        self.assertEqual(old_profile, Path(result["backup"]).read_bytes())
        self.assertEqual(b"human history must stay", database.read_bytes())
        self.assertEqual(b"real recipient registration", automation.read_bytes())

    def test_existing_evidence_conflict_not_overwritten(self):
        self.pack()
        bundle.unpack(self.archive, self.workspace)
        profile = json.loads((self.workspace / "profile.json").read_text("utf-8"))
        evidence = Path(profile["source_documents"][0]["path"])
        evidence.write_bytes(b"recipient changed file")
        with self.assertRaisesRegex(ValueError, "冲突"):
            bundle.unpack(self.archive, self.workspace)
        self.assertEqual(b"recipient changed file", evidence.read_bytes())

    def test_no_silent_overwrite_of_output_bundle(self):
        self.pack()
        before = self.archive.read_bytes()
        with self.assertRaisesRegex(ValueError, "已存在"):
            self.pack()
        self.assertEqual(before, self.archive.read_bytes())

    def test_secret_fields_rejected_and_duplicate_evidence_deduplicated(self):
        self.profile["access_token"] = "must not package"
        self.write_profile()
        with self.assertRaisesRegex(ValueError, "凭据"):
            self.pack()
        del self.profile["access_token"]
        self.profile["source_documents"].append(copy.deepcopy(self.profile["source_documents"][0]))
        self.write_profile()
        self.assertEqual(1, self.pack()["evidence_files"])
        bundle.unpack(self.archive, self.workspace)
        self.assertEqual(2, bundle.doctor(workspace_path=self.workspace)["document_references"])

    def test_scheduler_identifiers_rejected_in_pack_and_import(self):
        for key in ("automations", "ThreadId", "AUTOMATION-SETUP", "targetThreadId",
                    "notification-receipts", "service-access-token"):
            with self.subTest(key=key):
                self.profile["nested"] = {key: "old registration must not move"}
                self.write_profile()
                with self.assertRaisesRegex(ValueError, "凭据|自动化|对话"):
                    self.pack()
                self.assertFalse(self.archive.exists())
        del self.profile["nested"]
        self.write_profile()
        self.pack()
        # A validly hashed archive is still rejected when its profile carries
        # another person's scheduler state; integrity does not grant scope.
        def inject(contents):
            profile = json.loads(contents["profile.json"])
            profile["nested"] = {"AUTOMATION-SETUP": {"automations": []}}
            contents["profile.json"] = bundle.json_bytes(profile)
            manifest = json.loads(contents["manifest.json"])
            manifest["files"] = [{"path": name, "sha256": bundle.sha256(content), "size": len(content)}
                                 for name, content in sorted(contents.items()) if name != "manifest.json"]
            manifest["bundle_id"] = bundle.sha256(bundle.json_bytes(manifest["files"]))
            contents["manifest.json"] = bundle.json_bytes(manifest)
        self.rewrite_zip(inject)
        with self.assertRaisesRegex(ValueError, "自动化|对话"):
            bundle.unpack(self.archive, self.workspace)
        self.assertFalse(self.workspace.exists())

    def test_size_limit_is_enforced_from_zip_headers(self):
        self.pack()
        old_limit = bundle.MAX_FILE_BYTES
        try:
            bundle.MAX_FILE_BYTES = 4
            with self.assertRaisesRegex(ValueError, "上限|超限"):
                bundle.load_bundle(self.archive)
        finally:
            bundle.MAX_FILE_BYTES = old_limit


if __name__ == "__main__":
    unittest.main()
