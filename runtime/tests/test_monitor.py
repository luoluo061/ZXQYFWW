import copy
import csv
import importlib.util
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "monitor.py"
spec = importlib.util.spec_from_file_location("monitor", MODULE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.resource_patch = patch.object(m, "TOOL_ROOT", self.root)
        self.resource_patch.start()
        self.addCleanup(self.resource_patch.stop)
        self.db = m.connect(self.root / "monitor.sqlite")
        self.profile = {"company": {"name": "测试企业"}, "version": "1", "confirmed_services": [],
                        "discovery_keywords": {"policy": ["科技"], "tender": ["软件开发"]}}
        self.item = {"kind": "tender", "source_id": "A", "source_url": "https://mirror.example/a?id=1",
                     "original_url": "https://official.example/notice?id=1", "original_verified": True,
                     "title": "软件开发服务采购", "issuer": "采购人甲", "project_id": "P1", "lot": "01", "round": "1",
                     "stage": "open", "deadline_at": "2026-11-10", "hard_conditions": [{"label": "业绩", "status": "unknown", "evidence": "未提供证明"}],
                     "summary": "系统软件开发", "next_action": "补核同类项目业绩"}

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def payload(self, items=None, run_id="r1", checked="2026-10-28T15:00:00+08:00", status="success"):
        return {"run_id": run_id, "checked_at": checked, "mode": "manual",
                "coverage": [{"source_id": "A", "status": status, "queries": ["软件开发"]}],
                "items": [copy.deepcopy(self.item)] if items is None else items}

    def test_unknown_and_missing_do_not_become_failed_or_passed(self):
        self.assertEqual(m.evaluate(self.item, self.profile, "2026-10-28")['decision'], "待核")
        no_conditions = {**self.item, "hard_conditions": []}
        self.assertEqual(m.evaluate(no_conditions, self.profile, "2026-10-28")['decision'], "待核")
        no_evidence = {**self.item, "hard_conditions": [{"label": "资格", "status": "pass"}]}
        self.assertEqual(m.evaluate(no_evidence, self.profile, "2026-10-28")['decision'], "待核")

    def test_verified_fail_and_unverified_are_different(self):
        item = {**self.item, "hard_conditions": [{"label": "资格", "status": "fail", "evidence": "已核缺少所需证书"}]}
        self.assertEqual(m.evaluate(item, self.profile, "2026-10-28")['decision'], "不符")
        item['original_verified'] = False
        self.assertEqual(m.evaluate(item, self.profile, "2026-10-28")['decision'], "待核")

    def test_closed_stages_and_expired_are_not_open_opportunities(self):
        for stage in ("awarded", "terminated", "historical"):
            with self.subTest(stage=stage):
                self.assertEqual(m.evaluate({**self.item, "stage": stage}, self.profile, "2026-10-28")['decision'], "历史参考")
        self.assertEqual(m.evaluate(self.item, self.profile, "2026-11-11")['decision'], "已过截止")
        self.assertNotEqual(m.evaluate(self.item, self.profile, "2026-11-10T12:00:00+08:00")['decision'], "已过截止")

    def test_no_confirmed_services_prevents_tender_promotion(self):
        item = {**self.item, "hard_conditions": [{"label": "资格", "status": "pass", "evidence": "已核文件与企业材料"}]}
        self.assertEqual(m.evaluate(item, self.profile, "2026-10-28")['decision'], "待核")
        self.profile['confirmed_services'] = ['软件开发']
        self.assertEqual(m.evaluate(item, self.profile, "2026-10-28")['decision'], "可推进准备")

    def test_earlier_enrollment_is_pending_not_historical(self):
        self.profile['confirmed_services'] = ['软件开发']
        item = {**self.item, "hard_conditions": [{"label": "资格", "status": "pass", "evidence": "材料已核"}],
                "deadlines": [{"label": "报名截止", "at": "2026-10-27", "source_quote": "报名期限"}]}
        result = m.evaluate(item, self.profile, "2026-10-28")
        self.assertEqual(result['decision'], "待核")
        self.assertIn("企业是否已完成待核", " ".join(result['reasons']))
        item['deadlines'][0]['completed'] = True
        self.assertEqual(m.evaluate(item, self.profile, "2026-10-28")['decision'], "可推进准备")

    def test_unspecified_condition_status_stays_unknown(self):
        self.profile['confirmed_services'] = ['软件开发']
        item = {**self.item, "hard_conditions": [{"label": "资格", "evidence": "尚待业务核查"}]}
        self.assertEqual(m.evaluate(item, self.profile, "2026-10-28")['decision'], "待核")

    def test_url_canonicalization_keeps_real_ids(self):
        self.assertEqual(m.canonical_url("https://EXAMPLE.com/a?id=1&utm_source=x#part"), "https://example.com/a?id=1")
        self.assertNotEqual(m.canonical_url("https://example.com/a?id=1"), m.canonical_url("https://example.com/a?id=2"))

    def test_repeat_metadata_and_mirror_do_not_create_versions(self):
        first = m.ingest(self.db, self.payload(), self.profile)
        mirror = copy.deepcopy(self.item)
        mirror.update(source_url="https://other.example/item?id=3&utm_campaign=test", evidence_path="new-proof.txt")
        second = m.ingest(self.db, self.payload([mirror], "r2", "2026-10-29T15:00:00+08:00"), self.profile)
        self.assertEqual(len(first['new']), 1)
        self.assertEqual(len(second['unchanged']), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM versions").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM observations").fetchone()[0], 2)

    def test_real_change_once_and_manual_notes_survive(self):
        changes = m.ingest(self.db, self.payload(), self.profile)
        record_id = changes['new'][0]
        self.db.execute("UPDATE records SET review_state='关注', review_note='待技术核业绩' WHERE id=?", (record_id,))
        self.db.commit()
        updated = {**self.item, "stage": "change", "deadline_at": "2026-11-15", "notice_type": "更正"}
        changed = m.ingest(self.db, self.payload([updated], "r2"), self.profile)
        repeated = m.ingest(self.db, self.payload([updated], "r3"), self.profile)
        self.assertEqual(changed['changed'], [record_id])
        self.assertEqual(repeated['changed'], [])
        row = self.db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        self.assertEqual(row['version'], 2)
        self.assertEqual(row['review_note'], "待技术核业绩")
        self.assertEqual(row['review_state'], "关注")
        self.assertEqual(m.evaluate(updated, self.profile, "2026-10-28")['decision'], "变更待核")

    def test_lots_and_rounds_stay_separate(self):
        items = [self.item, {**self.item, "lot": "02"}, {**self.item, "round": "2"}]
        changes = m.ingest(self.db, self.payload(items), self.profile)
        self.assertEqual(len(changes['new']), 3)

    def test_shared_list_url_different_business_numbers_stay_separate(self):
        items = [{**self.item, "source_url": "https://mirror.example/list", "original_url": None, "original_verified": False},
                 {**self.item, "source_url": "https://mirror.example/list", "original_url": None, "original_verified": False,
                  "project_id": "P2", "title": "第二个软件项目"}]
        result = m.ingest(self.db, self.payload(items), self.profile)
        self.assertEqual(len(result['new']), 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 2)

    def test_shared_list_without_numbers_uses_title_and_publication(self):
        first = {**self.item, "source_url": "https://mirror.example/list", "project_id": None,
                 "original_url": None, "original_verified": False, "published_at": "2026-10-28"}
        second = {**first, "title": "另一个项目"}
        third = {**first, "published_at": "2026-10-27"}
        result = m.ingest(self.db, self.payload([first, second, third]), self.profile)
        self.assertEqual(len(result['new']), 3)
        again = m.ingest(self.db, self.payload([first, second, third], "r2"), self.profile)
        self.assertEqual(len(again['unchanged']), 3)

    def test_shared_url_different_ids_not_merged_even_if_original_present(self):
        result = m.ingest(self.db, self.payload([self.item, {**self.item, "project_id": "P2"}]), self.profile)
        self.assertEqual(len(result['new']), 2)

    def test_no_number_mirrors_can_share_trusted_original(self):
        first = {**self.item, "project_id": None}
        second = {**first, "source_url": "https://other.example/mirror?id=99"}
        result = m.ingest(self.db, self.payload([first, second]), self.profile)
        self.assertEqual(len(result['new']), 1)

    def test_low_information_mirror_cannot_replace_verified_body(self):
        official = {**self.item, "body_text": "已核正式正文及完整条件", "summary": "正式摘要"}
        first = m.ingest(self.db, self.payload([official]), self.profile)
        record_id = first['new'][0]
        mirror = {**official, "source_url": "https://other.example/short", "original_verified": False,
                  "body_text": "", "hard_conditions": [], "summary": "列表摘要"}
        second = m.ingest(self.db, self.payload([mirror], "r2"), self.profile)
        row = self.db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        self.assertEqual(json.loads(row['data'])['body_text'], official['body_text'])
        self.assertEqual(row['version'], 1)
        self.assertEqual(second['unverified_updates'], [record_id])
        third = m.ingest(self.db, self.payload([mirror], "r3"), self.profile)
        self.assertEqual(third['unverified_updates'], [])

    def test_unverified_source_update_remains_pending_until_rechecked(self):
        self.profile['confirmed_services'] = ['软件开发']
        official = {**self.item, "body_text": "完整的正式正文", "hard_conditions": [{"label": "资格", "status": "pass", "evidence": "证据已核"}]}
        m.ingest(self.db, self.payload([official]), self.profile)
        mirror = {**official, "source_url": "https://other.example/update", "original_verified": False,
                  "stage": "terminated", "body_text": ""}
        m.ingest(self.db, self.payload([mirror], "r2"), self.profile)
        result = m.export_files(self.db, self.profile, self.root / 'out', "2026-10-28T15:00:00+08:00")
        with Path(result['files']['master']).open(encoding='utf-8-sig', newline='') as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row['判断'], "待核")
        self.assertEqual(row['待核来源更新'], "是")
        m.ingest(self.db, self.payload([official], "r3"), self.profile)
        self.assertEqual(self.db.execute("SELECT pending_source_update FROM records").fetchone()[0], 0)

    def test_partial_capture_is_not_healthy_empty(self):
        m.ingest(self.db, self.payload([], status="partial"), self.profile)
        result = m.export_files(self.db, self.profile, self.root / "out", "2026-10-28T15:00:00+08:00")
        brief = Path(result['files']['brief']).read_text('utf-8')
        self.assertIn("不能解释为这些来源没有新信息", brief)
        self.assertEqual(result['counts']['incomplete_sources'], 1)
        self.assertEqual(result['counts']['new'], 0)

    def test_failed_validation_does_not_partially_ingest_batch(self):
        bad = {**self.item, "kind": "other"}
        with self.assertRaises(ValueError):
            m.ingest(self.db, self.payload([self.item, bad]), self.profile)
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 0)

    def test_exports_are_utf8_bom_and_keep_review_independent(self):
        m.ingest(self.db, self.payload(), self.profile)
        result = m.export_files(self.db, self.profile, self.root / "out", "2026-10-28T15:00:00+08:00")
        raw = Path(result['files']['master']).read_bytes()
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        self.assertIn("待核", raw.decode('utf-8-sig'))
        self.assertEqual(result['counts']['todo'], 1)

    def test_summary_and_profile_relevance_are_kept_without_inference(self):
        item = {**self.item, "summary": "采购系统开发及维护服务", "relevance_reason": "企业已确认软件开发业务，具体业绩仍待核"}
        m.ingest(self.db, self.payload([item]), self.profile)
        result = m.export_files(self.db, self.profile, self.root / 'out', "2026-10-28T15:00:00+08:00")
        with Path(result['files']['master']).open(encoding='utf-8-sig', newline='') as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row['事项摘要'], item['summary'])
        self.assertEqual(row['匹配理由'], item['relevance_reason'])
        brief = Path(result['files']['brief']).read_text('utf-8')
        self.assertIn(item['relevance_reason'], brief)
        self.assertIn("不能作为资格分或可投结论", brief)
        human = self.read_export(result, 'tender_ledger')[0]
        self.assertEqual(human['一句话解读'], item['summary'])
        self.assertEqual(human['企业关联'], item['relevance_reason'])

    def test_file_acquisition_due_before_primary_is_exported_and_alerted(self):
        item = {**self.item, "deadline_at": "2026-11-17T09:00:00+08:00",
                "deadlines": [{"label": "取文件截止", "at": "2026-11-02", "completed": None, "source_quote": "获取期限11月2日"}]}
        m.ingest(self.db, self.payload([item]), self.profile)
        result = m.export_files(self.db, self.profile, self.root / "out", "2026-10-28T15:00:00+08:00")
        changed = json.loads(Path(result['files']['changes']).read_text('utf-8'))
        self.assertEqual(len(changed['deadline_alerts']), 1)
        alert = changed['deadline_alerts'][0]
        self.assertEqual(alert['label'], "取文件截止")
        self.assertEqual(alert['days'], 5)
        self.assertEqual(alert['at'], "2026-11-02")
        with Path(result['files']['master']).open(encoding='utf-8-sig', newline='') as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(len(json.loads(row['各办理节点'])), 2)
        brief = Path(result['files']['brief']).read_text('utf-8')
        self.assertIn("取文件截止", brief)
        self.assertIn("新增为首次入库，不等于当天新发布", brief)
        self.assertIn("非全网完整扫描", brief)

    def test_completed_nodes_are_preserved_but_not_alerted(self):
        item = {**self.item, "deadlines": [{"label": "报名截止", "at": "2026-10-30", "completed": True}]}
        m.ingest(self.db, self.payload([item]), self.profile)
        result = m.export_files(self.db, self.profile, self.root / "out", "2026-10-28T15:00:00+08:00")
        changed = json.loads(Path(result['files']['changes']).read_text('utf-8'))
        self.assertEqual(changed['deadline_alerts'], [])

    def test_date_only_node_is_valid_and_alerted_until_end_of_day(self):
        self.profile['reminder_days'] = [7, 3, 1]
        item = {**self.item, "deadlines": [{"label": "取文件截止", "at": "2026-10-28", "completed": False}]}
        m.ingest(self.db, self.payload([item]), self.profile)
        result = m.export_files(self.db, self.profile, self.root / "out", "2026-10-28T20:00:00+08:00")
        changed = json.loads(Path(result['files']['changes']).read_text('utf-8'))
        self.assertEqual(changed['deadline_alerts'][0]['days'], 0)
        self.assertEqual(changed['deadline_alerts'][0]['at'], "2026-10-28")
        self.assertEqual(changed['deadline_alerts'][0]['completed'], False)

    def read_export(self, result, key):
        with Path(result['files'][key]).open(encoding='utf-8-sig', newline='') as stream:
            return list(csv.DictReader(stream))

    def test_unknown_candidate_and_positive_initial_check_do_not_decide_participation(self):
        m.ingest(self.db, self.payload(), self.profile)
        result = m.export_files(self.db, self.profile, self.root / 'out', '2026-10-28T15:00:00+08:00')
        candidates = self.read_export(result, 'awaiting_decision')
        self.assertEqual(candidates[0]['初筛情况'], '待核')
        self.assertEqual(candidates[0]['人员决定'], '待人员判断')
        self.assertEqual(self.read_export(result, 'participating'), [])
        self.profile['confirmed_services'] = ['软件开发']
        verified = {**self.item, 'hard_conditions': [{'label': '完整资格', 'status': 'pass', 'evidence': '已核材料'}]}
        m.ingest(self.db, self.payload([verified], 'r2'), self.profile)
        result = m.export_files(self.db, self.profile, self.root / 'out', '2026-10-28T15:00:00+08:00')
        self.assertEqual(self.read_export(result, 'awaiting_decision')[0]['初筛情况'], '可推进准备')
        self.assertEqual(result['counts']['participating'], 0)

    def test_human_participation_survives_source_change_and_expiry_for_result_followup(self):
        record_id = m.ingest(self.db, self.payload(), self.profile)['new'][0]
        decision = m.save_review(self.db, record_id, '参与', '用户明确选择参与，准备资料', '2026-10-28T16:00:00+08:00')
        changed = {**self.item, 'summary': '更正了文件内容', 'deadline_at': '2026-11-09'}
        m.ingest(self.db, self.payload([changed], 'r2', '2026-10-29T15:00:00+08:00'), self.profile)
        result = m.export_files(self.db, self.profile, self.root / 'out', '2026-11-11T15:00:00+08:00')
        execution = self.read_export(result, 'participating')
        self.assertEqual(len(execution), 1)
        self.assertEqual(execution[0]['人员决定'], '参与')
        self.assertEqual(execution[0]['初筛情况'], '已过截止')
        self.assertEqual(execution[0]['决定备注'], '用户明确选择参与，准备资料')
        self.assertEqual(execution[0]['决定时间'], decision['reviewed_at'])
        self.assertEqual(self.read_export(result, 'awaiting_decision'), [])
        audit = self.db.execute('SELECT * FROM review_events').fetchone()
        self.assertEqual(audit['previous_state'], '待核')
        self.assertEqual(audit['state'], '参与')
        m.save_review(self.db, record_id, '结束', '结果跟进完毕')
        result = m.export_files(self.db, self.profile, self.root / 'out', '2026-11-11T15:00:00+08:00')
        self.assertEqual(self.read_export(result, 'participating'), [])

    def test_non_participation_excludes_candidates_but_keeps_long_term_ledger(self):
        record_id = m.ingest(self.db, self.payload(), self.profile)['new'][0]
        m.save_review(self.db, record_id, '不参与', '人员确认资源不合适')
        result = m.export_files(self.db, self.profile, self.root / 'out', '2026-10-28T15:00:00+08:00')
        self.assertEqual(self.read_export(result, 'awaiting_decision'), [])
        self.assertEqual(self.read_export(result, 'participating'), [])
        ledger = self.read_export(result, 'tender_ledger')
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]['人员决定'], '不参与')
        self.assertEqual(result['counts']['todo'], 0)

    def test_display_ids_material_status_and_three_conditions_are_truthful(self):
        conditions = [{'label': f'门槛{i}', 'status': 'unknown', 'evidence': '待企业提供'} for i in range(1, 5)]
        record_id = m.ingest(self.db, self.payload([{**self.item, 'hard_conditions': conditions}]), self.profile)['new'][0]
        (self.root / 'data').mkdir()
        (self.root / 'data/display_ids.json').write_text(json.dumps({'ids': {record_id: 'T001'}}), 'utf-8')
        (self.root / 'materials').mkdir()
        actual = self.root / 'materials/真实附件.docx'
        actual.write_bytes(b'test fixture')
        unavailable = self.root / 'materials/不可用附件.docx'
        unavailable.write_bytes(b'present but not downloaded status')
        entries = [
            {'item_id': record_id, 'title': '已取得指南', 'url': 'https://official.example/guide.docx', 'status': 'downloaded', 'local_path': str(actual)},
            {'item_id': record_id, 'title': '缺失指南', 'url': 'https://official.example/missing.docx', 'status': 'downloaded', 'local_path': str(self.root / 'missing.docx')},
            {'item_id': record_id, 'title': '购标资料', 'url': 'https://official.example/files', 'status': 'purchase_or_login_required', 'local_path': str(unavailable)}]
        (self.root / 'materials/materials_manifest.json').write_text(json.dumps({'attachments': entries}), 'utf-8')
        result = m.export_files(self.db, self.profile, self.root / 'out', '2026-10-28T15:00:00+08:00')
        ledger = self.read_export(result, 'tender_ledger')[0]
        self.assertEqual(ledger['编号'], 'T001')
        self.assertIn('门槛3', ledger['关键条件（最多3条）'])
        self.assertNotIn('门槛4', ledger['关键条件（最多3条）'])
        self.assertIn('本地文件缺失', ledger['下载资料'])
        self.assertIn('需登录或购标后获取', ledger['下载资料'])
        cards = Path(result['files']['chat_material']).read_text('utf-8')
        self.assertIn(actual.as_posix(), cards)
        self.assertNotIn(unavailable.as_posix(), cards)
        self.assertNotIn((self.root / 'missing.docx').as_posix(), cards)
        self.assertIn(self.item['original_url'], cards)

    def test_legacy_review_states_map_for_display_and_cannot_self_promote(self):
        record_id = m.ingest(self.db, self.payload(), self.profile)['new'][0]
        for old, displayed in [('待核', '待人员判断'), ('关注', '待人员判断'), ('推进', '参与'), ('暂不推进', '不参与')]:
            m.save_review(self.db, record_id, old, '旧版人工状态保留')
            result = m.export_files(self.db, self.profile, self.root / 'out', '2026-10-28T15:00:00+08:00')
            self.assertEqual(self.read_export(result, 'tender_ledger')[0]['人员决定'], displayed)
        self.assertEqual(self.db.execute('SELECT count(*) FROM review_events').fetchone()[0], 4)

    def test_review_audit_requires_existing_record_and_note(self):
        with self.assertRaises(ValueError):
            m.save_review(self.db, 'missing', '参与', '用户选择')
        record_id = m.ingest(self.db, self.payload(), self.profile)['new'][0]
        with self.assertRaises(ValueError):
            m.save_review(self.db, record_id, '参与', ' ')
        self.assertEqual(self.db.execute('SELECT count(*) FROM review_events').fetchone()[0], 0)

    def test_review_command_saves_explicit_request_and_audit_time(self):
        record_id = m.ingest(self.db, self.payload(), self.profile)['new'][0]
        with patch('sys.stdout'):
            code = m.main(['review', '--db', str(self.root / 'monitor.sqlite'), '--id', record_id,
                           '--state', '先补资料', '--note', '人员回复先补合同再决定'])
        self.assertEqual(code, 0)
        audit = self.db.execute('SELECT * FROM review_events').fetchone()
        self.assertEqual(audit['state'], '先补资料')
        self.assertEqual(audit['note'], '人员回复先补合同再决定')
        self.assertRegex(audit['reviewed_at'], r'T\d\d:\d\d:\d\d\+08:00$')
        result = m.export_files(self.db, self.profile, self.root / 'out', '2026-10-28T15:00:00+08:00')
        self.assertEqual(self.read_export(result, 'awaiting_decision')[0]['人员决定'], '先补资料')
        self.assertEqual(self.read_export(result, 'participating'), [])

    def test_closed_and_expired_records_do_not_become_chat_candidates(self):
        closed = {**self.item, 'stage': 'awarded', 'project_id': 'closed'}
        expired = {**self.item, 'deadline_at': '2026-10-01', 'project_id': 'expired'}
        m.ingest(self.db, self.payload([closed, expired]), self.profile)
        result = m.export_files(self.db, self.profile, self.root / 'out', '2026-10-28T15:00:00+08:00')
        self.assertEqual(self.read_export(result, 'awaiting_decision'), [])
        self.assertEqual(len(self.read_export(result, 'tender_ledger')), 2)
        self.assertNotIn('## ', Path(result['files']['chat_material']).read_text('utf-8'))

    def test_card_notes_override_display_without_source_versions_or_decisions_changing(self):
        original = {**self.item, 'summary': '来源中的采购用途', 'relevance_reason': '来源中的关联说明'}
        record_id = m.ingest(self.db, self.payload([original]), self.profile)['new'][0]
        before = dict(self.db.execute('SELECT * FROM records WHERE id=?', (record_id,)).fetchone())
        (self.root / 'data').mkdir()
        notes = {record_id: {'brief_interpretation': '用于开发业务系统。', 'relevance_reason': '与已确认的软件开发业务有关。',
                            'key_conditions': ['展示条件一', '展示条件二', '展示条件三', '不应展示的第四项'],
                            'review_state': '参与', 'original_url': 'https://wrong.example/override'}}
        (self.root / 'data/card_notes.json').write_text(json.dumps(notes, ensure_ascii=False), 'utf-8')
        result = m.export_files(self.db, self.profile, self.root / 'out', '2026-10-28T15:00:00+08:00')
        human = self.read_export(result, 'tender_ledger')[0]
        self.assertEqual(human['一句话解读'], '用于开发业务系统。')
        self.assertEqual(human['企业关联'], '与已确认的软件开发业务有关。')
        self.assertEqual(human['关键条件（最多3条）'], '展示条件一；展示条件二；展示条件三')
        self.assertEqual(human['正式原文'], original['original_url'])
        self.assertEqual(human['人员决定'], '待人员判断')
        cards = Path(result['files']['chat_material']).read_text('utf-8')
        self.assertIn('用于开发业务系统。', cards)
        self.assertIn('与企业关联：与已确认的软件开发业务有关。', cards)
        self.assertNotIn('不应展示的第四项', cards)
        after = dict(self.db.execute('SELECT * FROM records WHERE id=?', (record_id,)).fetchone())
        self.assertEqual(before, after)
        self.assertEqual(self.db.execute('SELECT count(*) FROM versions').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM review_events').fetchone()[0], 0)
        master = self.read_export(result, 'master')[0]
        self.assertEqual(master['事项摘要'], original['summary'])
        self.assertEqual(master['匹配理由'], original['relevance_reason'])


if __name__ == '__main__':
    unittest.main()
