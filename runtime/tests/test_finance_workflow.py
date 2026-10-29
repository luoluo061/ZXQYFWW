"""Finance review and daily-log invariants, using temporary mock data only."""
import copy
from contextlib import closing
import csv
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("finance_workflow_tested", SOURCE / "finance_workflow.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


class FinanceWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="finance-workflow-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.profile = {"workflow": "finance", "version": "fixture-1", "company": {"name": "模拟企业"},
            "confirmed_services": [], "discovery_keywords": {"policy": ["研发", "奖补"]}}
        self.write("profile.json", self.profile)
        self.write("data/install_manifest.json", {"name": "sme-opportunity-monitor"})
        self.write("data/installation.json", {"scope": "policy", "edition": "finance"})
        self.write("data/display_ids.json", {})
        self.write("materials/materials_manifest.json", {"attachments": []})
        self.main = f.monitor.connect(self.root / "data/monitor.sqlite")
        self.addCleanup(self.main.close)
        self.pool = f.human_review.connect(self.root / "data/human_review.sqlite")
        self.addCleanup(self.pool.close)
        patch_root = patch.object(f.monitor, "TOOL_ROOT", self.root)
        patch_root.start()
        self.addCleanup(patch_root.stop)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
        return path

    def rows(self, name):
        with (self.root / "exports" / name).open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def item(self, suffix="1", category="奖补兑现"):
        return {"kind": "policy", "source_id": "fixture_official", "source_url": "https://official.example/policy/" + suffix,
            "source_is_list": False, "original_url": "https://official.example/policy/" + suffix,
            "original_verified": True, "title": "模拟研发奖补政策" + suffix,
            "issuer": "模拟主管部门", "document_number": "FIXTURE-" + suffix,
            "published_at": "2026-10-28", "stage": "open", "deadline_at": "2026-11-30",
            "summary": "模拟规则仅供测试，无实际政策或金额。", "regions": ["模拟地区"],
            "hard_conditions": [{"label": "财务指标", "status": "unknown", "evidence": "企业证明待补"}],
            "body_text": "测试正式原文。", "finance": {"category": category, "applicable_period": "模拟所属期",
                "payment_mode": "测试先申后拨", "financial_requirements": ["财务指标待据正式材料比对"],
                "company_evidence": ["待提供"], "material_gaps": ["归集辅助账"]}}

    def observation(self, item, bucket="待核"):
        return {key: copy.deepcopy(item[key]) for key in ("kind", "source_id", "title", "issuer", "document_number", "published_at", "lot", "round") if key in item} | {
            "list_url": "https://official.example/list", "detail_url": item["original_url"],
            "original_url": item["original_url"], "original_verified": True,
            "reading_level": "原文", "machine_bucket": bucket, "machine_reason": "财务证据待补，保留人员核查。",
            "reason_code": "未知", "condition_evidence": copy.deepcopy(item["hard_conditions"]),
            "query": "研发奖补", "region_filter": "模拟省/市", "page": 1}

    def collect(self, items=None, run_id="test-run-1", checked="2026-10-29T09:00:00+08:00", status="success", extra_observations=None):
        items = [self.item()] if items is None else items
        coverage = [{"source_id": "fixture_official", "status": status, "queries": ["研发奖补"],
                     "regions": ["模拟省", "模拟市"], "note": "测试来源覆盖；不代表真实网站检索。"}]
        payload = {"run_id": run_id, "checked_at": checked, "coverage": coverage, "items": copy.deepcopy(items),
                   "next_keywords_suggested": ["研发后补助"]}
        screening = {"run_id": run_id, "checked_at": checked, "scope": "policy", "coverage": coverage,
                     "observations": [self.observation(item) for item in items] + (extra_observations or [])}
        f.monitor.ingest(self.main, payload, self.profile)
        f.monitor.ensure_display_ids(self.main)
        f.human_review.ingest(self.pool, screening)
        return payload, screening

    def count_events(self):
        path = self.root / "data/finance_workflow.sqlite"
        if not path.exists():
            return 0
        with closing(sqlite3.connect(path)) as db:
            return db.execute("SELECT COUNT(*) FROM finance_reviews").fetchone()[0]

    def review(self, state, **kwargs):
        return f.save_review(self.root, "P001", state, "模拟财务人员", "人员测试原话及依据", reviewed_at="2026-10-29T10:00:00+08:00", **kwargs)

    def test_empty_export_does_not_create_scan_or_human_history(self):
        result = f.export_views(self.root)
        self.assertEqual(result["finance_opportunities"], 0)
        self.assertFalse((self.root / "data/finance_workflow.sqlite").exists())
        self.assertEqual(self.rows("每日检索运行日志.csv"), [])
        self.assertEqual(self.main.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0)

    def test_wrong_workspace_never_creates_database_or_output(self):
        unknown = self.root / "not-installed"
        with self.assertRaises(ValueError):
            f.export_views(unknown)
        self.assertFalse(unknown.exists())
        self.profile["workflow"] = "general"
        self.write("profile.json", self.profile)
        self.write("data/installation.json", {"scope": "policy"})
        with self.assertRaises(ValueError):
            f.export_views(self.root)
        self.assertFalse((self.root / "exports").exists())

    def test_four_categories_and_financial_fields_without_human_approval(self):
        categories = ["税费优惠", "奖补兑现", "人才用工稳岗", "融资增信贴息"]
        payload, screening = self.collect([self.item(str(index), category) for index, category in enumerate(categories)])
        f.log_round(self.root, payload, screening)
        rows = self.rows("财务政策资金明细.csv")
        self.assertEqual({row["政策类别"] for row in rows}, set(categories))
        self.assertTrue(all(row["财务审核状态"] == "待财务审核" and row["人员参与决定"] == "待人员判断" for row in rows))
        self.assertTrue(all(row["适用财税期间"] == "模拟所属期" for row in rows))
        daily = self.rows("每日检索运行日志.csv")[0]
        self.assertEqual(list(daily), f.DAILY_FIELDS)
        self.assertIn("待财务审核", daily["政策线索及人工判断"])
        self.assertNotIn("符合条件", daily["政策线索及人工判断"])
        self.assertIn("待人员更新", daily["今日待办事项进展"])
        self.assertIn("系统建议（待人员确认）", daily["次日待检索关键词"])
        self.assertEqual(self.count_events(), 0)

    def test_log_round_is_idempotent_and_conflict_is_not_overwritten(self):
        payload, screening = self.collect()
        self.assertEqual(f.log_round(self.root, payload, screening)["status"], "logged")
        self.assertEqual(f.log_round(self.root, payload, screening)["status"], "already_logged")
        changed = copy.deepcopy(payload)
        changed["next_keywords_suggested"] = ["changed"]
        with self.assertRaises(ValueError):
            f.log_round(self.root, changed, screening)
        with closing(sqlite3.connect(self.root / "data/finance_workflow.sqlite")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM finance_runs").fetchone()[0], 1)

    def test_screening_content_must_match_ingested_round_not_just_count(self):
        payload, screening = self.collect()
        screening["observations"][0]["title"] = "不同的观察标题"
        with self.assertRaises(ValueError):
            f.log_round(self.root, payload, screening)
        self.assertFalse((self.root / "data/finance_workflow.sqlite").exists())

    def test_two_rounds_same_day_merge_and_deduplicate_observed_policy(self):
        first = self.collect()
        f.log_round(self.root, *first)
        second = self.collect([self.item(), self.item("2")], "test-run-2", "2026-10-29T16:30:00+08:00")
        f.log_round(self.root, *second)
        daily = self.rows("每日检索运行日志.csv")
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0]["今日检索政策条数"], "2")
        self.assertIn("2个检索轮次", daily[0]["备注"])
        self.assertEqual(self.count_events(), 0)

    def test_unrecommended_observations_count_and_survive_in_core_pool(self):
        extra = self.observation(self.item("2"), "暂不推荐")
        payload, screening = self.collect(extra_observations=[extra])
        f.log_round(self.root, payload, screening)
        self.assertEqual(self.rows("每日检索运行日志.csv")[0]["今日检索政策条数"], "2")
        self.assertEqual(self.pool.execute("SELECT COUNT(*) FROM screening_cases").fetchone()[0], 2)
        self.assertEqual(self.pool.execute("SELECT COUNT(*) FROM screening_reviews").fetchone()[0], 0)

    def test_no_results_is_logged_but_failed_coverage_is_not_zero_complete_scan(self):
        payload, screening = self.collect([], status="failed")
        f.log_round(self.root, payload, screening)
        daily = self.rows("每日检索运行日志.csv")[0]
        self.assertIn("未完成", daily["今日检索政策条数"])
        self.assertIn("采集失败", daily["备注"])
        f.log_round(self.root, *self.collect([], "test-run-2", "2026-10-30T09:00:00+08:00", "success"))
        daily = self.rows("每日检索运行日志.csv")
        self.assertEqual(len(daily), 2)
        self.assertEqual(daily[0]["今日检索政策条数"], "0")
        self.assertIn("无新增不取消检索留痕", daily[0]["备注"])

    def test_example_or_uningested_json_is_not_a_scan(self):
        payload, screening = self.collect()
        payload["example_only"] = True
        with self.assertRaises(ValueError):
            f.log_round(self.root, payload, screening)
        payload.pop("example_only")
        payload["run_id"] = screening["run_id"] = "not-ingested"
        with self.assertRaises(ValueError):
            f.log_round(self.root, payload, screening)
        self.assertFalse((self.root / "data/finance_workflow.sqlite").exists())

    def test_material_gaps_require_department_and_due_and_human_record(self):
        f.log_round(self.root, *self.collect())
        with self.assertRaises(ValueError):
            self.review("待补齐", material_gaps=["归集辅助账"])
        self.assertEqual(self.count_events(), 0)
        self.review("待补齐", material_gaps=["归集辅助账"], department="研发及财务", due="2026-11-02", next_keywords=["研发补助"])
        row = self.rows("财务政策资金明细.csv")[0]
        self.assertEqual(row["补料责任部门"], "研发及财务")
        self.assertEqual(row["人员补料期限"], "2026-11-02")
        daily = self.rows("每日检索运行日志.csv")[0]
        self.assertIn("责任部门：研发及财务", daily["今日待办事项进展"])
        self.assertEqual(daily["次日待检索关键词"], "人员确认：研发补助")

    def test_tax_benefit_closure_requires_period_date_voucher_and_actual_proof(self):
        self.collect([self.item(category="税费优惠")])
        for fields in ({}, {"date": "2026-10-28", "voucher": "TEST-V1", "evidence": ["not-there.pdf"]},
                       {"date": "2026-10-28", "period": "2026第三季度", "voucher": "TEST-V1", "evidence": ["not-there.pdf"]}):
            with self.assertRaises(ValueError):
                self.review("已享受", **fields)
        self.assertEqual(self.count_events(), 0)
        proof = self.root / "inputs/mock-receipt.txt"
        proof.parent.mkdir()
        proof.write_text("Only a mock receipt for testing.", "utf-8")
        self.review("已享受", date="2026-10-28", period="模拟所属期", voucher="TEST-V1", evidence=[str(proof)])
        row = self.rows("财务政策资金明细.csv")[0]
        self.assertEqual(row["到账日期"], "")
        self.assertEqual(row["税惠享受日期"], "2026-10-28")
        self.assertEqual(row["已享受所属期"], "模拟所属期")
        self.assertIn("sha256", row["闭环归档证据"])
        self.assertEqual(row["人员参与决定"], "待人员判断")

    def test_cash_arrival_is_distinct_from_tax_benefit_and_no_future_completion(self):
        self.collect()
        proof = self.root / "receipt.txt"
        proof.write_text("mock", "utf-8")
        with self.assertRaises(ValueError):
            self.review("已到账", date="2026-11-01", voucher="TEST-V2", evidence=[str(proof)])
        self.review("已到账", date="2026-10-28", voucher="TEST-V2", evidence=[str(proof)])
        row = self.rows("财务政策资金明细.csv")[0]
        self.assertEqual(row["到账日期"], "2026-10-28")
        self.assertEqual(row["税惠享受日期"], "")

    def test_review_does_not_participate_unless_explicit_and_events_are_append_only(self):
        self.collect()
        self.review("可推进准备")
        self.assertEqual(self.main.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0)
        self.assertEqual(self.rows("财务政策资金明细.csv")[0]["人员参与决定"], "待人员判断")
        self.review("可推进准备", participation="参与")
        self.assertEqual(self.count_events(), 2)
        self.assertEqual(self.rows("财务政策资金明细.csv")[0]["人员参与决定"], "参与")
        with closing(sqlite3.connect(self.root / "data/finance_workflow.sqlite")) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE finance_reviews SET note='changed'")

    def test_failed_participation_bridge_does_not_leave_fake_finance_event(self):
        self.collect()
        with patch.object(f.monitor, "save_review", side_effect=ValueError("bridge blocked for test")):
            with self.assertRaises(ValueError):
                self.review("可推进准备", participation="参与")
        self.assertEqual(self.count_events(), 0)
        self.assertEqual(self.main.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0)

    def test_source_change_reopens_review_and_preserves_receipt_and_participation(self):
        original = self.item()
        self.collect([original])
        proof = self.root / "receipt.txt"
        proof.write_text("mock", "utf-8")
        self.review("已到账", date="2026-10-28", voucher="TEST-CLOSED", evidence=[str(proof)], participation="参与")
        changed = copy.deepcopy(original)
        changed["body_text"] += "现已更新条件。"
        self.collect([changed], "changed-run", "2026-10-29T16:30:00+08:00")
        f.export_views(self.root)
        row = self.rows("财务政策资金明细.csv")[0]
        self.assertEqual(row["财务审核状态"], "待财务重新复核")
        self.assertEqual(row["上次财务结论"], "已到账")
        self.assertEqual(row["会计凭证号"], "TEST-CLOSED")
        self.assertEqual(row["人员参与决定"], "参与")
        self.assertIn("历史闭环", row["闭环说明"])
        self.assertEqual(self.count_events(), 1)

    def test_history_reference_shows_real_status_not_eligible_claim(self):
        f.log_round(self.root, *self.collect())
        self.review("历史参考")
        daily = self.rows("每日检索运行日志.csv")[0]
        self.assertIn("历史参考", daily["政策线索及人工判断"])
        self.assertNotIn("符合条件", daily["政策线索及人工判断"])

    def test_csv_formula_injection_is_escaped(self):
        item = self.item()
        item["title"] = "=HYPERLINK(\"https://invalid.example\")"
        self.collect([item])
        f.export_views(self.root)
        self.assertTrue(self.rows("财务政策资金明细.csv")[0]["政策标题"].startswith("'="))

    def test_daily_snapshot_not_changed_by_next_day_source_human_or_participation(self):
        original = self.item()
        f.log_round(self.root, *self.collect([original]))
        self.review("可推进准备", participation="参与")
        first = self.rows("每日检索运行日志.csv")[0]
        changed = copy.deepcopy(original)
        changed["title"] = "次日变更的政策标题"
        changed["body_text"] += "次日变更条件。"
        f.log_round(self.root, *self.collect([changed], "next-day", "2026-10-30T09:00:00+08:00"))
        f.save_review(self.root, "P001", "不适用", "模拟财务人员", "次日真实更正依据",
                      reviewed_at="2026-10-30T10:00:00+08:00", participation="不参与")
        by_day = {row["检索日期"]: row for row in self.rows("每日检索运行日志.csv")}
        self.assertEqual(by_day["2026-10-29"]["政策线索及人工判断"], first["政策线索及人工判断"])
        self.assertEqual(by_day["2026-10-29"]["今日待办事项进展"], first["今日待办事项进展"])
        self.assertIn(original["title"], by_day["2026-10-29"]["政策线索及人工判断"])
        self.assertIn("参与决定：参与", by_day["2026-10-29"]["政策线索及人工判断"])
        self.assertIn("次日变更的政策标题", by_day["2026-10-30"]["政策线索及人工判断"])
        self.assertIn("不适用", by_day["2026-10-30"]["政策线索及人工判断"])
        self.assertIn("不参与", self.rows("财务政策资金明细.csv")[0]["人员参与决定"])

    def test_no_document_number_linked_platform_and_official_pages_count_once(self):
        item = self.item()
        item.pop("document_number")
        coverage = [{"source_id": "fixture_official", "status": "success", "queries": ["奖补"]}]
        payload = {"run_id": "linked-pages", "checked_at": "2026-10-29T09:00:00+08:00", "coverage": coverage, "items": [item]}
        observed = {"kind": "policy", "source_id": "fixture_official", "title": item["title"],
            "list_url": "https://www.chinasme.cn/policy", "detail_url": "https://www.chinasme.cn/detail/mock-item",
            "original_url": item["original_url"], "original_verified": False, "reading_level": "平台详情",
            "machine_bucket": "待核", "machine_reason": "待财务审核", "reason_code": "未知"}
        screening = {"run_id": payload["run_id"], "checked_at": payload["checked_at"], "scope": "policy", "coverage": coverage, "observations": [observed]}
        f.monitor.ingest(self.main, payload, self.profile)
        f.monitor.ensure_display_ids(self.main)
        f.human_review.ingest(self.pool, screening)
        f.log_round(self.root, payload, screening)
        self.assertEqual(self.rows("每日检索运行日志.csv")[0]["今日检索政策条数"], "1")
        stored = json.loads(self.pool.execute("SELECT data FROM screening_observations").fetchone()[0])
        self.assertFalse(stored["original_verified"])
        self.assertEqual(stored["reading_level"], "平台详情")

    def test_later_same_day_source_change_is_not_displaced_by_morning_review(self):
        original = self.item()
        f.log_round(self.root, *self.collect([original]))
        self.review("可推进准备", participation="参与")
        changed = copy.deepcopy(original)
        changed["title"] = "下午更新的政策标题"
        changed["body_text"] += "下午新增重要条件。"
        f.log_round(self.root, *self.collect([changed], "same-day-afternoon", "2026-10-29T16:30:00+08:00"))
        daily = self.rows("每日检索运行日志.csv")[0]
        self.assertIn("下午更新的政策标题", daily["政策线索及人工判断"])
        self.assertIn("待财务重新复核", daily["政策线索及人工判断"])
        self.assertIn("来源v2", daily["政策线索及人工判断"])
        self.assertNotIn(original["title"], daily["政策线索及人工判断"])
        self.assertIn("可推进准备", daily["今日待办事项进展"])
        self.assertIn("来源v1，10:00审核", daily["今日待办事项进展"])
        self.assertIn("人员测试原话及依据", daily["今日待办事项进展"])
        self.assertEqual(self.count_events(), 1)
        self.assertEqual(self.rows("财务政策资金明细.csv")[0]["财务审核状态"], "待财务重新复核")
        simple = self.rows("每日检索政策台账.csv")[0]
        self.assertIn("下午更新的政策标题", simple["编号及政策名称"])
        self.assertIn("待财务重新复核", simple["人工意见及下一步"])
        self.assertIn("人员测试原话及依据", simple["人工意见及下一步"])

    def test_same_title_and_url_different_document_batches_are_not_merged(self):
        first, second = self.item("1"), self.item("2")
        second["title"] = first["title"]
        second["original_url"] = second["source_url"] = first["original_url"]
        f.log_round(self.root, *self.collect([first, second]))
        self.assertEqual(self.rows("每日检索运行日志.csv")[0]["今日检索政策条数"], "2")
        self.assertEqual(len(self.rows("财务政策资金明细.csv")), 2)

    def test_same_title_no_number_distinct_rounds_are_not_merged(self):
        first, second = self.item(), self.item()
        first.pop("document_number")
        second.pop("document_number")
        first["round"], second["round"] = "模拟第一批", "模拟第二批"
        payload, screening = self.collect([first, second])
        f.log_round(self.root, payload, screening)
        self.assertEqual(self.rows("每日检索运行日志.csv")[0]["今日检索政策条数"], "2")

    def test_human_correction_preserves_receipt_only_as_history(self):
        self.collect()
        proof = self.root / "receipt.txt"
        proof.write_text("mock", "utf-8")
        self.review("已到账", date="2026-10-28", voucher="TEST-CORRECTED", evidence=[str(proof)])
        f.save_review(self.root, "P001", "待财务审核", "模拟财务人员", "人员更正到账记录，待重新核实",
                      reviewed_at="2026-10-29T11:00:00+08:00")
        row = self.rows("财务政策资金明细.csv")[0]
        self.assertEqual(row["财务审核状态"], "待财务审核")
        self.assertEqual(row["会计凭证号"], "TEST-CORRECTED")
        self.assertIn("历史闭环", row["闭环说明"])
        self.assertIn("后续人员结论", row["闭环说明"])
        self.assertNotIn("人员据归档证据确认", row["闭环说明"])

    def test_screening_status_uses_real_opportunity_status_mapping(self):
        self.collect()
        payload, screening = self.collect(run_id="linked-screening", checked="2026-10-29T10:00:00+08:00")
        # The current linked observation is new metadata, not a completed review.
        screening["run_id"] = "linked-screening-human"
        screening["observations"][0]["opportunity_id"] = "P001"
        f.human_review.ingest(self.pool, screening)
        f.human_review.review(self.pool, "R00001", "模拟复核人员", "先补证据", "需要补齐材料", payload["items"][0]["original_url"], "2026-10-29T10:10:00+08:00")
        f.export_views(self.root)
        row = self.rows("财务政策资金明细.csv")[0]
        self.assertEqual(row["人工筛选复核"], "已复核")
        self.assertEqual(row["关联复核编号"], "R00001")
        self.assertEqual(row["财务审核状态"], "待财务审核")

    def test_finance_cli_works_under_ascii_stdio(self):
        completed = subprocess.run([sys.executable, "-B", str(SOURCE / "finance_workflow.py"), "export", "--workspace", str(self.root)],
            env=dict(os.environ, PYTHONIOENCODING="ascii", PYTHONUTF8="0"), capture_output=True, encoding="utf-8")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["finance_opportunities"], 0)

    def test_lower_information_pending_hint_keeps_machine_screen_pending(self):
        original = self.item()
        original["hard_conditions"] = [{"label": "测试财务指标", "status": "pass", "evidence": "仅测试的已核证明"}]
        self.collect([original])
        self.review("可推进准备", participation="参与")
        lower = copy.deepcopy(original)
        lower["source_url"] = "https://www.chinasme.cn/detail/mock-new-hint"
        lower["original_verified"] = False
        lower["body_text"] = ""
        lower["deadline_at"] = "2026-10-30"
        f.monitor.ingest(self.main, {"run_id": "lower-new-hint", "checked_at": "2026-10-29T16:30:00+08:00",
            "coverage": [{"source_id": "fixture_official", "status": "partial", "queries": ["测试新期限提示"]}],
            "items": [lower]}, self.profile)
        f.export_views(self.root)
        row = self.rows("财务政策资金明细.csv")[0]
        self.assertEqual(row["机器初筛"], "待核")
        self.assertEqual(row["财务审核状态"], "待财务重新复核")
        self.assertEqual(row["人员参与决定"], "参与")
        self.assertEqual(self.count_events(), 1)

    def test_simple_views_have_eight_and_ten_columns_and_one_policy_per_day(self):
        f.log_round(self.root, *self.collect([self.item(), self.item("2")]))
        f.log_round(self.root, *self.collect([self.item()], "afternoon", "2026-10-29T16:30:00+08:00"))
        daily = self.rows("每日检索政策台账.csv")
        ledger = self.rows("财务政策资金台账.csv")
        self.assertEqual(len(daily), 2)
        self.assertEqual(len(ledger), 2)
        self.assertEqual(list(daily[0]), f.SIMPLE_DAILY_FIELDS)
        self.assertEqual(list(ledger[0]), f.SIMPLE_LEDGER_FIELDS)
        self.assertEqual(len(daily[0]), 8)
        self.assertEqual(len(ledger[0]), 10)
        self.assertTrue(all(row["是否参与"] == "待人员判断" and row["办理进展"] == "待财务审核" for row in ledger))
        self.assertTrue(all(row["编号及政策名称"].count("https://") == 1 for row in daily))
        self.assertTrue(all("模拟省" in row["检索词/地区"] for row in daily))
        self.assertEqual(len(self.rows("每日检索运行日志.csv")), 1)
        self.assertEqual(self.count_events(), 0)

    def test_simple_zero_results_keeps_run_log_without_fake_policy_row(self):
        f.log_round(self.root, *self.collect([], status="failed"))
        self.assertEqual(self.rows("每日检索政策台账.csv"), [])
        self.assertEqual(self.rows("财务政策资金台账.csv"), [])
        log = self.rows("每日检索运行日志.csv")
        self.assertEqual(len(log), 1)
        self.assertIn("未完成", log[0]["今日检索政策条数"])

    def test_locked_csv_exports_use_new_names_and_preserve_open_original_files(self):
        self.collect()
        folder = self.root / "exports"
        folder.mkdir()
        files = {"ledger": "财务政策资金台账.csv", "daily_log": "每日检索政策台账.csv",
                 "ledger_details": "财务政策资金明细.csv", "run_log": "每日检索运行日志.csv"}
        for filename in files.values():
            (folder / filename).write_text("original-open-file", "utf-8")
        real_replace = os.replace
        def replace(source, target):
            if Path(target).name in files.values():
                raise PermissionError("mock Windows file lock")
            return real_replace(source, target)
        with patch.object(f.os, "replace", side_effect=replace):
            result = f.export_views(self.root)
        for key, filename in files.items():
            original = folder / filename
            actual = original.with_name(original.stem + "_新版.csv")
            self.assertEqual(result[key], str(actual))
            self.assertTrue(actual.is_file())
            self.assertEqual(original.read_text("utf-8"), "original-open-file")
        self.assertEqual(len(result["export_notes"]), 4)
        self.assertIn("可能正在被Excel占用", result["note"])
        self.assertEqual(self.count_events(), 0)
        self.assertEqual(self.main.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], 0)
        self.assertFalse(list(folder.glob(".finance-export-*")))

    def test_export_uses_default_paths_when_not_locked(self):
        result = f.export_views(self.root)
        self.assertEqual(result["ledger"], str(self.root / "exports/财务政策资金台账.csv"))
        self.assertEqual(result["daily_log"], str(self.root / "exports/每日检索政策台账.csv"))
        self.assertEqual(result["export_notes"], [])

    def test_both_csv_names_locked_raise_clear_error_and_remove_temporary_file(self):
        folder = self.root / "exports"
        folder.mkdir()
        target = folder / "台账.csv"
        with patch.object(f.os, "replace", side_effect=PermissionError("mock locked")):
            with self.assertRaisesRegex(PermissionError, "原台账和新版文件均无法写入"):
                f.csv_export(target, ["标题"], [{"标题": "模拟"}])
        self.assertFalse(list(folder.glob(".finance-export-*")))
        self.assertFalse(target.exists())

    def test_simple_daily_prefers_linked_policy_context_over_shared_source_queries(self):
        first, second = self.item(), self.item("2")
        payload, screening = self.collect([first, second])
        ids = f.display_ids(self.root, f.records_and_ids(self.root)[0])
        records = {json.loads(row["data"])["title"]: row["id"] for row in self.main.execute("SELECT id,data FROM records")}
        screening["run_id"] = payload["run_id"] = "linked-context"
        screening["observations"][0].update(opportunity_id=ids[records[first["title"]]], query="本政策检索词", region_filter="本政策地区")
        screening["observations"][1].update(opportunity_id=ids[records[second["title"]]], query="另一政策词", region_filter="另一政策地区")
        payload["coverage"][0]["queries"] = ["共享来源全量词1", "共享来源全量词2"]
        screening["coverage"] = copy.deepcopy(payload["coverage"])
        f.monitor.ingest(self.main, payload, self.profile)
        f.human_review.ingest(self.pool, screening)
        f.log_round(self.root, payload, screening)
        rows = self.rows("每日检索政策台账.csv")
        first_row = next(row for row in rows if first["title"] in row["编号及政策名称"])
        self.assertIn("本政策检索词", first_row["检索词/地区"])
        self.assertIn("本政策地区", first_row["检索词/地区"])
        self.assertNotIn("共享来源全量", first_row["检索词/地区"])
        self.assertNotIn("另一政策", first_row["检索词/地区"])

    def test_simple_daily_preserves_old_source_and_human_decisions(self):
        original = self.item()
        f.log_round(self.root, *self.collect([original]))
        self.review("可推进准备", participation="参与")
        before = self.rows("每日检索政策台账.csv")[0]
        changed = copy.deepcopy(original)
        changed["title"] = "次日新的标题"
        changed["body_text"] += "次日条件变更"
        f.log_round(self.root, *self.collect([changed], "next-day", "2026-10-30T09:00:00+08:00"))
        f.save_review(self.root, "P001", "不适用", "模拟财务人员", "次日明确更正",
                      reviewed_at="2026-10-30T10:00:00+08:00", participation="不参与")
        by_day = {row["检索日期"]: row for row in self.rows("每日检索政策台账.csv")}
        self.assertEqual(by_day["2026-10-29"], before)
        self.assertIn("次日新的标题", by_day["2026-10-30"]["编号及政策名称"])
        self.assertEqual(self.rows("财务政策资金台账.csv")[0]["是否参与"], "不参与")

    def test_display_notes_require_current_version_and_do_not_rewrite_prior_days(self):
        f.log_round(self.root, *self.collect())
        record_id = self.main.execute("SELECT id FROM records").fetchone()[0]
        note = {"source_version": 1, "updated_at": "2026-10-30T11:00:00+08:00",
                "title": "通俗标题", "benefit": "符合条件可减少税费", "checks": "先核辅助账"}
        self.write("data/finance_display_notes.json", {record_id: note})
        f.export_views(self.root)
        self.assertIn("通俗标题", self.rows("财务政策资金台账.csv")[0]["编号及政策名称"])
        self.assertNotIn("通俗标题", self.rows("每日检索政策台账.csv")[0]["编号及政策名称"])
        note["source_version"] = 2
        self.write("data/finance_display_notes.json", {record_id: note})
        f.export_views(self.root)
        self.assertNotIn("通俗标题", self.rows("财务政策资金台账.csv")[0]["编号及政策名称"])
        self.assertEqual(self.count_events(), 0)
        self.assertEqual(self.main.execute("SELECT version FROM records").fetchone()[0], 1)

    def test_source_checked_display_notes_accept_three_checks_without_changing_human_next_step(self):
        f.log_round(self.root, *self.collect())
        record_id = self.main.execute("SELECT id FROM records").fetchone()[0]
        self.write("data/finance_display_notes.json", {record_id: {"source_version": 1,
            "source_checked_at": "2026-10-29T09:00:00+08:00", "title": "通俗标题",
            "checks": ["核辅助账", "核所属期", "核已享受"], "next_action": "机器建议补材料"}})
        f.export_views(self.root)
        daily = self.rows("每日检索政策台账.csv")[0]
        self.assertIn("通俗标题", daily["编号及政策名称"])
        self.assertEqual(daily["要核实什么"], "核辅助账；核所属期；核已享受")
        self.assertIn("原文：https://", daily["编号及政策名称"])
        self.review("可推进准备")
        self.assertEqual(self.rows("财务政策资金台账.csv")[0]["下一步/负责人"], "人员测试原话及依据")

    def test_policy_execution_end_is_not_written_as_enterprise_deadline(self):
        item = self.item()
        item["deadline_at"] = "2026-12-31"
        item["deadline_kind"] = "policy_execution_end"
        item["finance"]["applicable_period"] = "2026年度政策执行期"
        f.log_round(self.root, *self.collect([item]))
        timing = self.rows("财务政策资金台账.csv")[0]["办理时间"]
        self.assertIn("企业具体办理时间待核", timing)
        self.assertNotIn("企业办理截止：2026-12-31", timing)
        self.assertNotIn("主要办理节点：2026-12-31", timing)

    def test_simple_view_real_review_updates_finance_without_automatic_participation(self):
        f.log_round(self.root, *self.collect())
        self.review("待补齐", material_gaps=["辅助账"], department="研发", due="2026-11-02")
        row = self.rows("财务政策资金台账.csv")[0]
        self.assertEqual(row["是否参与"], "待人员判断")
        self.assertEqual(row["办理进展"], "待补齐")
        self.assertIn("研发", row["下一步/负责人"])
        self.assertIn("2026-11-02", row["下一步/负责人"])
        self.assertIn("辅助账", row["下一步/负责人"])

    def test_participation_only_feedback_has_a_daily_row_without_a_new_scan(self):
        f.log_round(self.root, *self.collect())
        record_id = self.main.execute("SELECT id FROM records").fetchone()[0]
        f.monitor.save_review(self.main, record_id, "不参与", "人员明确暂不参与", "2026-10-30T10:00:00+08:00")
        result = f.export_views(self.root)
        rows = {row["检索日期"]: row for row in self.rows("每日检索政策台账.csv")}
        self.assertIn("参与决定：不参与", rows["2026-10-30"]["人工意见及下一步"])
        self.assertIn("仅记录人员进展", rows["2026-10-30"]["检索词/地区"])
        self.assertEqual(result["logged_runs"], 1)
        self.assertEqual(self.count_events(), 0)

    def test_display_notes_cannot_claim_materials_or_mask_a_pending_source_update(self):
        f.log_round(self.root, *self.collect())
        record_id = self.main.execute("SELECT id FROM records").fetchone()[0]
        self.write("data/finance_display_notes.json", {record_id: {"source_version": 1,
            "source_checked_at": "2026-10-29T09:00:00+08:00", "title": "通俗旧标题",
            "materials_text": "已下载并且申报材料齐全", "timing": "所有截止已确定"}})
        f.export_views(self.root)
        self.assertNotIn("申报材料齐全", self.rows("财务政策资金台账.csv")[0]["资料下载"])
        self.main.execute("UPDATE records SET pending_source_update=1 WHERE id=?", (record_id,))
        self.main.commit()
        f.export_views(self.root)
        row = self.rows("财务政策资金台账.csv")[0]
        self.assertNotIn("通俗旧标题", row["编号及政策名称"])
        self.assertNotIn("所有截止已确定", row["办理时间"])


if __name__ == "__main__":
    unittest.main()
