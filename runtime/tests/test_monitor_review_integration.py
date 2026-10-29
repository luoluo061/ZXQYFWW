"""Integration checks for opportunity exports and the independent screening audit.

All databases, mappings, notes and exports live in a temporary TOOL_ROOT.
"""
import copy
import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SOURCE_ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load_module("monitor_review_integration", "monitor.py")
h = load_module("human_review_integration", "human_review.py")


class MonitorReviewIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.patch_root = patch.object(m, "TOOL_ROOT", self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.patch_import = patch.dict(sys.modules, {"human_review": h})
        self.patch_import.start()
        self.addCleanup(self.patch_import.stop)
        self.db = m.connect(self.root / "data" / "monitor.sqlite")
        self.addCleanup(self.db.close)
        self.review_db = None
        self.profile = {
            "company": {"name": "模拟软件企业"}, "version": "test-1",
            "confirmed_services": ["软件开发"],
            "discovery_keywords": {"policy": ["科技"], "tender": ["软件开发"]},
        }

    def item(self, kind, suffix="1", lot=""):
        url = f"https://official.example/{kind}/{suffix}"
        result = {
            "kind": kind, "source_id": "official", "source_url": url,
            "source_is_list": False, "original_url": url, "original_verified": True,
            "title": f"科技项目{suffix}" if kind == "policy" else f"软件开发项目{suffix}标包{lot}",
            "issuer": "模拟主管部门" if kind == "policy" else "模拟采购人",
            "stage": "open", "published_at": "2026-10-28", "deadline_at": "2026-11-20",
            "summary": "来源用途说明", "relevance_reason": "来源企业关联",
            "hard_conditions": [{"label": "主体条件", "status": "unknown", "evidence": "企业证明待补"}],
            "key_conditions": ["来源关键条件"], "body_text": "本次正式来源正文",
        }
        result["document_number" if kind == "policy" else "project_id"] = f"{kind}-{suffix}"
        if kind == "tender":
            result.update(lot=lot, round="首次")
        return result

    def ingest(self, items, run="run-1", checked="2026-10-29T09:00:00+08:00"):
        return m.ingest(self.db, {
            "run_id": run, "checked_at": checked,
            "coverage": [{"source_id": "official", "status": "partial", "queries": ["模拟正式来源"]}],
            "items": copy.deepcopy(items),
        }, self.profile)

    def raw_id(self, item):
        identity = m.aliases(item)[0]
        return self.db.execute("SELECT record_id FROM aliases WHERE alias=?", (identity,)).fetchone()[0]

    def write_json(self, relative, data):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
        return target

    def export(self):
        return m.export_files(self.db, self.profile, self.root / "exports", "2026-10-29T12:00:00+08:00")

    def rows(self, filename):
        with (self.root / "exports" / filename).open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def open_review_db(self):
        if self.review_db is None:
            self.review_db = h.connect(self.root / "data" / "human_review.sqlite")
            self.addCleanup(self.review_db.close)
        return self.review_db

    def observation(self, item, opportunity_id):
        result = {key: item[key] for key in (
            "kind", "source_id", "title", "issuer", "original_url", "original_verified",
            "published_at", "deadline_at", "body_text",
        )}
        result.update({key: item[key] for key in ("document_number", "project_id", "lot", "round") if key in item})
        result.update(
            list_url="https://official.example/notices", detail_url=item["source_url"],
            machine_bucket="待核", machine_reason="企业证明待补，保留人员复核",
            reason_code="未知", reading_level="原文", condition_evidence=copy.deepcopy(item["hard_conditions"]),
            opportunity_id=opportunity_id,
        )
        return result

    def ingest_review(self, observations, run="screening-1"):
        return h.ingest(self.open_review_db(), {
            "run_id": run, "checked_at": "2026-10-29T10:00:00+08:00", "scope": "both",
            "coverage": [{"source_id": "official", "status": "partial"}],
            "observations": observations,
        })

    def test_existing_display_ids_stay_fixed_and_new_ids_increment_per_kind(self):
        policy, tender = self.item("policy"), self.item("tender", lot="01")
        self.ingest([policy, tender])
        old_ids = {self.raw_id(policy): "P003", self.raw_id(tender): "T007"}
        mapping = self.write_json("data/display_ids.json", {"ids": old_ids, "note": "保留原编号"})
        self.export()
        self.assertEqual(m.read_json(mapping)["ids"], old_ids)

        new_policy, new_tender = self.item("policy", "2"), self.item("tender", "2", "01")
        self.ingest([new_tender, new_policy], "run-2", "2026-10-29T10:00:00+08:00")
        self.export()
        assigned = m.read_json(mapping)
        self.assertEqual(assigned["note"], "保留原编号")
        self.assertEqual({key: assigned["ids"][key] for key in old_ids}, old_ids)
        self.assertEqual(assigned["ids"][self.raw_id(new_policy)], "P004")
        self.assertEqual(assigned["ids"][self.raw_id(new_tender)], "T008")
        self.assertEqual(len(set(assigned["ids"].values())), 4)

        policy["summary"] = "原事项来源更新"
        self.ingest([policy], "run-3", "2026-10-29T11:00:00+08:00")
        self.export()
        self.assertEqual(m.read_json(mapping), assigned)

    def test_four_lots_at_one_official_url_remain_four_opportunities_and_review_cases(self):
        lots = [self.item("tender", "shared", f"{number:02}") for number in range(1, 5)]
        result = self.ingest(lots)
        self.assertEqual(len(result["new"]), 4)
        self.export()
        rows = self.rows("招标台账.csv")
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["标题"] for row in rows}, {item["title"] for item in lots})
        self.assertEqual(len({row["编号"] for row in rows}), 4)
        display = m.read_json(self.root / "data/display_ids.json")
        reviewed = self.ingest_review([self.observation(item, display[self.raw_id(item)]) for item in lots])
        self.assertEqual(len(reviewed["new"]), 4)
        self.assertEqual(len(h.pool_rows(self.review_db)), 4)
        self.export()
        self.assertEqual({row["人工筛选复核"] for row in self.rows("招标台账.csv")}, {"待人工复核"})

    def test_missing_review_database_is_not_created_and_exports_explicit_missing_pool_status(self):
        policy = self.item("policy")
        self.ingest([policy])
        review_path = self.root / "data/human_review.sqlite"
        self.assertFalse(review_path.exists())
        self.assertEqual(m.screening_status(), {})
        self.export()
        self.assertFalse(review_path.exists())
        row = self.rows("政策台账.csv")[0]
        self.assertEqual(row["人工筛选复核"], "尚未进入复核池")
        self.assertEqual(row["人员决定"], "待人员判断")
        for field in ("筛选复核人", "筛选复核时间", "关联复核编号"):
            self.assertEqual(row[field], "")
        self.assertEqual(self.rows("已决定参与.csv"), [])

    def test_linked_r_reviews_update_screening_columns_without_changing_participation(self):
        policy, tender = self.item("policy"), self.item("tender", lot="01")
        self.ingest([policy, tender])
        m.save_review(self.db, self.raw_id(tender), "参与", "模拟人员明确选择参与", "2026-10-29T09:30:00+08:00")
        self.export()
        before = [tuple(row) for row in self.db.execute("SELECT id,review_state,review_note FROM records ORDER BY id")]
        audit_before = [tuple(row) for row in self.db.execute("SELECT * FROM review_events ORDER BY id")]
        display = m.read_json(self.root / "data/display_ids.json")
        self.ingest_review([self.observation(item, display[self.raw_id(item)]) for item in (policy, tender)])
        self.export()
        self.assertEqual(self.rows("政策台账.csv")[0]["人工筛选复核"], "待人工复核")

        outcomes = ["恢复候选", "维持暂不推荐"]
        for case, outcome in zip(h.pool_rows(self.review_db), outcomes):
            result = h.review(self.review_db, case["复核编号"], "模拟审核员", outcome,
                              "模拟实际阅读后的筛选复核结论", case["正式原文"], "2026-10-29T11:00:00+08:00")
            self.assertFalse(result["participation_changed"])
        self.export()
        policy_row, tender_row = self.rows("政策台账.csv")[0], self.rows("招标台账.csv")[0]
        for row in (policy_row, tender_row):
            self.assertEqual(row["人工筛选复核"], "已复核")
            self.assertEqual(row["筛选复核人"], "模拟审核员")
            self.assertEqual(row["筛选复核时间"], "2026-10-29T11:00:00+08:00")
            self.assertRegex(row["关联复核编号"], r"^R\d{5,}$")
        self.assertNotEqual(policy_row["关联复核编号"], tender_row["关联复核编号"])
        self.assertEqual(policy_row["人员决定"], "待人员判断")
        self.assertEqual(tender_row["人员决定"], "参与")
        self.assertEqual([tuple(row) for row in self.db.execute("SELECT id,review_state,review_note FROM records ORDER BY id")], before)
        self.assertEqual([tuple(row) for row in self.db.execute("SELECT * FROM review_events ORDER BY id")], audit_before)
        self.assertEqual([row["编号"] for row in self.rows("已决定参与.csv")], [tender_row["编号"]])

    def test_stale_source_version_notes_fall_back_to_current_source_without_new_versions(self):
        policy = self.item("policy")
        self.ingest([policy])
        record_id = self.raw_id(policy)
        note = {"source_version": 1, "brief_interpretation": "旧版展示解读",
                "relevance_reason": "旧版展示关联", "key_conditions": ["旧版展示条件"]}
        notes_path = self.write_json("data/card_notes.json", {record_id: note})
        self.export()
        self.assertEqual(self.rows("政策台账.csv")[0]["一句话解读"], "旧版展示解读")

        policy.update(summary="新版来源用途", relevance_reason="新版来源企业关联", key_conditions=["新版来源关键条件"], body_text="正式来源正文发生变化")
        self.ingest([policy], "run-2", "2026-10-29T11:00:00+08:00")
        self.export()
        row = self.rows("政策台账.csv")[0]
        self.assertEqual(row["一句话解读"], "新版来源用途")
        self.assertEqual(row["企业关联"], "新版来源企业关联")
        self.assertEqual(row["关键条件（最多3条）"], "新版来源关键条件")
        self.assertEqual(m.read_json(notes_path)[record_id], note)
        self.assertEqual(self.db.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()[0], 2)

        note.update(source_version=2, brief_interpretation="已复核新版解读")
        self.write_json("data/card_notes.json", {record_id: note})
        self.export()
        self.assertEqual(self.rows("政策台账.csv")[0]["一句话解读"], "已复核新版解读")
        self.assertEqual(self.db.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM versions WHERE record_id=?", (record_id,)).fetchone()[0], 2)

    def test_chat_material_separates_policy_and_tender_sections_and_links(self):
        policy, tender = self.item("policy"), self.item("tender", lot="01")
        self.ingest([tender, policy])
        self.export()
        chat = (self.root / "exports/对话汇报素材.md").read_text("utf-8")
        policy_heading, tender_heading = chat.index("\n# 政策\n"), chat.index("\n# 招标\n")
        self.assertLess(policy_heading, chat.index("## P001 "))
        self.assertLess(chat.index("## P001 "), tender_heading)
        self.assertLess(tender_heading, chat.index("## T001 "))
        policy_section, tender_section = chat[policy_heading:tender_heading], chat[tender_heading:]
        self.assertIn(policy["original_url"], policy_section)
        self.assertNotIn(tender["original_url"], policy_section)
        self.assertIn(tender["original_url"], tender_section)
        self.assertNotIn(policy["original_url"], tender_section)
        self.assertIn("人工筛选复核：尚未进入复核池；人员决定：待人员判断", policy_section)
        self.assertIn("人工筛选复核：尚未进入复核池；人员决定：待人员判断", tender_section)


if __name__ == "__main__":
    unittest.main()
