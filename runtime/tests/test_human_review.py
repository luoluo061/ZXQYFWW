import copy
import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("human_review", Path(__file__).resolve().parents[1] / "human_review.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class HumanReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = h.connect(self.root / "review.sqlite")
        self.item = {"kind": "policy", "source_id": "official", "list_url": "https://example.gov.cn/list",
                     "detail_url": "https://example.gov.cn/1", "original_url": "https://example.gov.cn/1",
                     "original_verified": True, "title": "科技项目申请", "machine_bucket": "暂不推荐",
                     "machine_reason": "主体资格待核，暂不主动推荐", "reason_code": "未知", "reading_level": "原文",
                     "condition_evidence": [{"label": "主体", "status": "unknown", "evidence": "尚无企业证明"}],
                     "source_content_sha256": "abc", "opportunity_id": "P001"}

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def payload(self, items=None, run="r1", checked="2026-10-29T09:00:00+08:00"):
        return {"run_id": run, "checked_at": checked, "scope": "policy", "coverage": [{"source_id": "official", "status": "partial", "note": "仅第1页"}], "observations": [copy.deepcopy(self.item)] if items is None else items}

    def review(self, rid="R00001", outcome="先补证据"):
        return h.review(self.db, rid, "审核员甲", outcome, "已看条款，企业证明待补", "https://example.gov.cn/1", "2026-10-29T10:00:00+08:00")

    def test_shared_list_keeps_distinct_titles_and_known_lots(self):
        items = [{**self.item, "original_url": "", "original_verified": False, "detail_url": "", "title": f"政策{i}"} for i in range(2)]
        result = h.ingest(self.db, self.payload(items))
        self.assertEqual(len(result["new"]), 2)
        tender_items = [{**self.item, "kind": "tender", "project_id": "A1", "issuer": "甲", "lot": lot} for lot in ["01", "02"]]
        p = self.payload(tender_items, "r2"); p["scope"] = "tender"
        self.assertEqual(len(h.ingest(self.db, p)["new"]), 2)

    def test_same_business_number_different_title_is_version_not_duplicate(self):
        item = {**self.item, "document_number": "科2026-1", "issuer": "科技厅"}
        h.ingest(self.db, self.payload([item]))
        item["title"] = "科技项目申请更正"
        result = h.ingest(self.db, self.payload([item], "r2", "2026-10-29T11:00:00+08:00"))
        self.assertEqual(result["changed"], ["R00001"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM screening_cases").fetchone()[0], 1)

    def test_invalid_batch_is_atomic_and_unknown_cannot_be_failed(self):
        bad = {**self.item, "machine_reason": "已证不符", "conclusion_strength": "已证不符"}
        with self.assertRaises(ValueError):
            h.ingest(self.db, self.payload([self.item, bad]))
        self.assertEqual(self.db.execute("SELECT count(*) FROM screening_runs").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM screening_cases").fetchone()[0], 0)

    def test_original_verified_requires_actual_original_reading(self):
        with self.assertRaises(ValueError):
            h.ingest(self.db, self.payload([{**self.item, "reading_level": "标题"}]))

    def test_idempotent_runs_and_conflicting_run_id(self):
        payload = self.payload()
        h.ingest(self.db, payload)
        self.assertTrue(h.ingest(self.db, payload)["idempotent"])
        payload["observations"][0]["title"] = "别的政策"
        with self.assertRaises(ValueError):
            h.ingest(self.db, payload)
        self.assertEqual(self.db.execute("SELECT count(*) FROM screening_observations").fetchone()[0], 1)

    def test_review_survives_metadata_refresh_but_source_change_invalidates(self):
        h.ingest(self.db, self.payload())
        self.review()
        item = {**self.item, "page": "2", "evidence_path": "new-file", "query": "换检索词"}
        h.ingest(self.db, self.payload([item], "r2", "2026-10-29T11:00:00+08:00"))
        self.assertEqual(h.pool_rows(self.db)[0]["人工审核状态"], "已复核")
        item["source_content_sha256"] = "changed"
        h.ingest(self.db, self.payload([item], "r3", "2026-10-29T12:00:00+08:00"))
        row = h.pool_rows(self.db)[0]
        self.assertEqual(row["人工审核状态"], "待重新复核")
        self.assertEqual(row["审核人"], "审核员甲")
        self.assertEqual(row["人工审核结果"], "先补证据")
        self.assertEqual(self.db.execute("SELECT count(*) FROM screening_reviews").fetchone()[0], 1)

    def test_machine_reason_change_also_needs_new_review(self):
        h.ingest(self.db, self.payload())
        self.review()
        item = {**self.item, "machine_reason": "发现主体例外条款，改为待核", "machine_bucket": "待核"}
        h.ingest(self.db, self.payload([item], "r2", "2026-10-29T11:00:00+08:00"))
        self.assertEqual(h.opportunity_status(self.db)["P001"]["status"], "待重新复核")

    def test_any_unreviewed_linked_case_keeps_opportunity_pending(self):
        h.ingest(self.db, self.payload([self.item, {**self.item, "detail_url": "https://example.gov.cn/2", "original_url": "https://example.gov.cn/2"}]))
        self.review()
        self.assertEqual(h.opportunity_status(self.db)["P001"]["status"], "待人工复核")
        self.review("R00002")
        self.assertEqual(h.opportunity_status(self.db)["P001"]["status"], "已复核")

    def test_older_backfill_does_not_override_new_machine_screen(self):
        h.ingest(self.db, self.payload(run="new", checked="2026-10-29T11:00:00+08:00"))
        item = {**self.item, "machine_bucket": "推荐", "machine_reason": "早先资料"}
        h.ingest(self.db, self.payload([item], "old", "2026-10-28T09:00:00+08:00"))
        self.assertEqual(h.pool_rows(self.db)[0]["机器分流"], "暂不推荐")
        self.assertEqual(self.db.execute("SELECT count(*) FROM screening_observations").fetchone()[0], 2)

    def test_event_history_is_immutable(self):
        h.ingest(self.db, self.payload())
        self.review()
        for sql in ["DELETE FROM screening_reviews", "UPDATE screening_reviews SET note='伪造'", "DELETE FROM screening_observations", "UPDATE screening_observations SET observed_at='旧'"]:
            with self.assertRaises(Exception):
                self.db.execute(sql)
            self.db.rollback()

    def test_review_is_not_participation_and_old_review_timestamp_rejected(self):
        h.ingest(self.db, self.payload())
        result = self.review(outcome="恢复候选")
        self.assertFalse(result["participation_changed"])
        with self.assertRaises(ValueError):
            h.review(self.db, "R00001", "甲", "维持暂不推荐", "旧判断", "https://example.gov.cn/1", "2026-10-28T09:00:00+08:00")
        self.assertFalse(self.db.execute("SELECT name FROM sqlite_master WHERE name='records'").fetchall())

    def test_pool_never_hides_machine_discarded_and_does_not_fake_review(self):
        historical = {**self.item, "detail_url": "https://example.gov.cn/2", "machine_bucket": "历史信息", "machine_reason": "申报期已过", "reason_code": "期限"}
        h.ingest(self.db, self.payload([self.item, historical]))
        result = h.export(self.db, self.root / "exports", "2026-10-29T16:30:00+08:00")
        with (self.root / "exports/政策人工复核池.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["人工审核状态"] == "待人工复核" for row in rows))
        self.assertEqual(len(result["samples"]["政策"]), 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM screening_reviews").fetchone()[0], 0)

    def test_sampling_skips_reviewed_and_is_reproducible(self):
        items = [{**self.item, "detail_url": f"https://example.gov.cn/{i}", "original_url": f"https://example.gov.cn/{i}"} for i in range(6)]
        h.ingest(self.db, self.payload(items))
        self.review()
        rows = h.pool_rows(self.db)
        first = h.selection(rows, "2026-10-29")
        self.assertEqual(len(first), 3)
        self.assertNotIn("R00001", [row["复核编号"] for row in first])
        self.assertEqual(first, h.selection(rows, "2026-10-29"))

    def test_sampling_prioritizes_linked_borderline_and_reserves_low_relevance_third(self):
        items = [{**self.item, "detail_url": f"https://example.gov.cn/{i}", "original_url": f"https://example.gov.cn/{i}", "opportunity_id": ""} for i in range(8)]
        items[5]["opportunity_id"] = "P006"
        items[6]["opportunity_id"] = "P007"
        items[7].update({"machine_bucket": "历史信息", "machine_reason": "名单公示，仅供历史参考", "reason_code": "公告性质"})
        h.ingest(self.db, self.payload(items))
        selected = h.selection(h.pool_rows(self.db), "2026-10-29")
        self.assertEqual({row["关联机会编号"] for row in selected[:2]}, {"P006", "P007"})
        self.assertEqual(selected[2]["理由类别"], "公告性质")
        self.assertEqual(selected, h.selection(h.pool_rows(self.db), "2026-10-29"))

    def test_sampling_nearer_deadline_before_original_gap_with_same_other_priority(self):
        items = [{**self.item, "detail_url": "https://example.gov.cn/early", "deadline_at": "2026-10-30"},
                 {**self.item, "detail_url": "https://example.gov.cn/late", "original_verified": False, "reading_level": "标题", "deadline_at": "2026-11-30"}]
        h.ingest(self.db, self.payload(items))
        selected = h.selection(h.pool_rows(self.db), "2026-10-29")
        self.assertEqual(selected[0]["主要截止"], "2026-10-30")

    def test_linked_history_cannot_displace_unlinked_unknown_risk(self):
        items = [
            {**self.item, "detail_url": "https://example.gov.cn/history", "original_url": "https://example.gov.cn/history", "machine_bucket": "历史信息", "reason_code": "公告性质", "machine_reason": "成交公告，仅历史参考", "opportunity_id": "P001"},
            {**self.item, "detail_url": "https://example.gov.cn/risk1", "original_url": "https://example.gov.cn/risk1", "opportunity_id": ""},
            {**self.item, "detail_url": "https://example.gov.cn/risk2", "original_url": "https://example.gov.cn/risk2", "opportunity_id": ""},
        ]
        h.ingest(self.db, self.payload(items))
        selected = h.selection(h.pool_rows(self.db), "2026-10-29")
        self.assertTrue(all(row["理由类别"] == "未知" for row in selected[:2]))
        self.assertEqual(selected[2]["机器分流"], "历史信息")

    def test_packet_has_independent_source_entries_without_reading_or_review_events(self):
        h.export(self.db, self.root / "exports", "2026-10-29T16:30:00+08:00")
        packet = (self.root / "exports/每日人工复核包.md").read_text(encoding="utf-8")
        for url in ["https://kjt.yn.gov.cn/xwdt/tzgg/", "https://gxt.yn.gov.cn/zfxxgk/fdzdgknr/tzgg/", "https://kjj.km.gov.cn/", "https://chsme.qibiaoduo.com/result?key=project"]:
            self.assertIn(url, packet)
        self.assertIn("系统集成", packet)
        self.assertEqual(self.db.execute("SELECT count(*) FROM independent_readings").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM screening_reviews").fetchone()[0], 0)

    def test_independent_actual_reading_separate_from_case_review(self):
        h.ingest(self.db, self.payload())
        h.independent(self.db, "policy", "甲", "https://example.gov.cn/1", "实际原文", "读了适用对象条款，暂无适用项", "2026-10-29T14:00:00+08:00")
        self.assertEqual(self.db.execute("SELECT count(*) FROM independent_readings").fetchone()[0], 1)
        self.assertEqual(h.pool_rows(self.db)[0]["人工审核状态"], "待人工复核")

    def test_title_only_independent_reading_is_not_counted_as_original(self):
        h.independent(self.db, "policy", "甲", "https://example.gov.cn/list", "通知列表", "看10条标题暂无适用项", "2026-10-29T14:00:00+08:00", "标题")
        h.export(self.db, self.root / "exports", "2026-10-29T16:30:00+08:00")
        packet = (self.root / "exports/每日人工复核包.md").read_text(encoding="utf-8")
        self.assertIn("平台详情0个不同链接、正式原文0个不同链接", packet)
        with (self.root / "exports/每日人工查阅记录.csv").open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(list(csv.DictReader(handle))[0]["实际阅读层级"], "标题")

    def test_platform_detail_reading_is_saved_without_becoming_original(self):
        h.independent(self.db, "policy", "甲", "https://zcxm.chinasme.cn/policy/detail/observed", "实际平台政策页", "已读平台详情，申报条件须核正式原文", "2026-10-29T14:00:00+08:00", "平台详情")
        h.export(self.db, self.root / "exports", "2026-10-29T16:30:00+08:00")
        packet = (self.root / "exports/每日人工复核包.md").read_text(encoding="utf-8")
        self.assertIn("平台详情1个不同链接、正式原文0个不同链接", packet)
        with self.assertRaises(ValueError):
            h.ingest(self.db, self.payload([{**self.item, "reading_level": "平台详情"}]))

    def test_platform_page_is_first_and_official_link_remains_distinct(self):
        h.ingest(self.db, self.payload([{**self.item, "platform_url": "https://zcxm.chinasme.cn/policy/detail/observed"}]))
        line = h.md_line(h.pool_rows(self.db)[0])
        self.assertLess(line.index("[服务网页面]"), line.index("[正式原文]"))
        self.assertIn("https://zcxm.chinasme.cn/policy/detail/observed", line)
        self.assertIn("[正式原文](https://example.gov.cn/1)", line)

    def test_unknown_platform_detail_falls_back_to_list_and_full_title(self):
        item = {**self.item, "detail_url": "", "original_url": "", "original_verified": False, "reading_level": "标题", "platform_url": "https://chinasme.cn.evil.example/fake", "title": "必须保留的完整政策标题"}
        h.ingest(self.db, self.payload([item]))
        line = h.md_line(h.pool_rows(self.db)[0])
        self.assertIn("必须保留的完整政策标题", line)
        self.assertIn("https://www.chinasme.cn/policy", line)
        self.assertIn("详情未定位", line)
        self.assertNotIn("[正式原文]", line)
        self.assertNotIn("evil.example", line)

    def test_scope_and_secret_urls_rejected(self):
        for change in [{"kind": "tender"}, {"detail_url": "https://example.gov.cn/1?token=secret"}, {"list_url": "javascript:bad"}]:
            with self.assertRaises(ValueError):
                h.ingest(self.db, self.payload([{**self.item, **change}]))

    def test_lower_title_observation_is_retained_without_downgrade_or_review_reset(self):
        original = {**self.item, "body_text": "正式原文完整条款", "deadline_at": "2026-11-10"}
        h.ingest(self.db, self.payload([original]))
        self.review()
        title_only = {**self.item, "original_verified": False, "reading_level": "标题", "condition_evidence": [], "machine_bucket": "待核", "machine_reason": "目录标题，其他条件未知"}
        title_only.pop("source_content_sha256")
        result = h.ingest(self.db, self.payload([title_only], "r2", "2026-10-29T11:00:00+08:00"))
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["retained_lower_information"], ["R00001"])
        self.assertEqual(result["unverified_updates"], [])
        case = self.db.execute("SELECT * FROM screening_cases").fetchone()
        self.assertEqual(case["version"], 1)
        self.assertIn("正式原文完整条款", case["current_data"])
        row = h.pool_rows(self.db)[0]
        self.assertEqual(row["原文核验"], "已核正式原文")
        self.assertEqual(row["实际阅读层级"], "原文")
        self.assertEqual(row["人工审核状态"], "已复核")
        observation = self.db.execute("SELECT data FROM screening_observations ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertIn('"reading_level":"标题"', observation)

    def test_lower_explicit_new_deadline_needs_recheck_but_does_not_replace_official_fact(self):
        original = {**self.item, "deadline_at": "2026-11-10"}
        h.ingest(self.db, self.payload([original]))
        self.review()
        title_only = {**self.item, "original_verified": False, "reading_level": "标题", "condition_evidence": [], "deadline_at": "2026-11-20"}
        result = h.ingest(self.db, self.payload([title_only], "r2", "2026-10-29T11:00:00+08:00"))
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["unverified_updates"], ["R00001"])
        row = h.pool_rows(self.db)[0]
        self.assertEqual(row["主要截止"], "2026-11-10")
        self.assertEqual(row["人工审核状态"], "待重新复核")
        self.assertIn("2026-11-20", row["机器筛选理由"])
        # A fresh full-source read disproves the hint without manufacturing a version.
        result = h.ingest(self.db, self.payload([original], "r3", "2026-10-29T12:00:00+08:00"))
        self.assertEqual(result["changed"], [])
        self.assertEqual(h.pool_rows(self.db)[0]["人工审核状态"], "已复核")
        self.assertEqual(self.db.execute("SELECT version FROM screening_cases").fetchone()[0], 1)

    def test_lower_stage_or_title_change_is_not_ignored_and_verified_change_reopens_review(self):
        original = {**self.item, "stage": "open"}
        h.ingest(self.db, self.payload([original]))
        self.review()
        lower = {**self.item, "original_verified": False, "reading_level": "标题", "condition_evidence": [], "stage": "terminated", "title": "科技项目申请终止通知"}
        result = h.ingest(self.db, self.payload([lower], "r2", "2026-10-29T11:00:00+08:00"))
        self.assertEqual(result["unverified_updates"], ["R00001"])
        self.assertEqual(h.pool_rows(self.db)[0]["标题"], original["title"])
        verified = {**original, "stage": "terminated", "title": lower["title"]}
        result = h.ingest(self.db, self.payload([verified], "r3", "2026-10-29T12:00:00+08:00"))
        self.assertEqual(result["changed"], ["R00001"])
        self.assertEqual(h.pool_rows(self.db)[0]["人工审核状态"], "待重新复核")
        self.assertEqual(self.db.execute("SELECT version FROM screening_cases").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
