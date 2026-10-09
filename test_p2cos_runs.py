"""Offline per-trigger run tests; synthetic state only and no remote calls."""
import concurrent.futures
from contextlib import redirect_stdout
from datetime import datetime, time
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from p2cos_state import State, StateError
from p2cos_runs import ensure_run, main, migrate_runs, publication_not_before, ranking_for_day, run_check


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='p2cos_runs_')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.now = datetime(2026,10,9,1,30,tzinfo=ZoneInfo('Asia/Shanghai'))
        self.state = State(self.path, platforms=('xiaohongshu',), clock=lambda:self.now)

    def manual(self, suffix, immediate=True):
        with self.state._transaction() as db:
            return ensure_run(db,run_id='manual:'+suffix,kind='manual',day='2026-10-09',
                              ranking_mode='month',allow_immediate_publish=immediate,
                              authorization='explicit_user_request')

    def reserve(self, suffix, artwork):
        return self.state.reserve_run([{'id':artwork,'page_count':1}],run_id='manual:'+suffix)

    def test_multiple_explicit_manual_runs_can_reserve_different_artworks_same_day(self):
        self.manual('one'); self.manual('two')
        one=self.reserve('one','123'); two=self.reserve('two','456')
        self.assertNotEqual(one['job_id'],two['job_id'])
        self.assertEqual(one['day'],two['day'])
        self.assertEqual((one['run_id'],two['run_id']),('manual:one','manual:two'))

    def test_same_run_retry_returns_same_job_without_selecting_another(self):
        self.manual('one')
        one=self.reserve('one','123')
        retry=self.reserve('one','456')
        self.assertEqual(one['job_id'],retry['job_id'])
        self.assertEqual(self.state.excluded_ids(),{'123'})

    def test_same_run_concurrent_calls_reserve_exactly_one_job(self):
        self.manual('one')
        def reserve(index):
            return State(self.path,platforms=('xiaohongshu',)).reserve_run([{'id':900+index,'page_count':1}],run_id='manual:one')
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            jobs=list(pool.map(reserve,range(10)))
        self.assertEqual(len({j['job_id'] for j in jobs}),1)
        self.assertEqual(len(self.state.excluded_ids()),1)

    def test_artwork_reservation_history_is_global_across_runs(self):
        self.manual('one'); self.manual('two')
        self.reserve('one','123')
        self.assertIsNone(self.reserve('two','123'))
        with self.state._transaction() as db:
            db.execute("INSERT INTO history(artwork_id,source) VALUES ('456','test_download')")
        self.assertIsNone(self.reserve('two','456'))
        self.assertEqual(self.reserve('two','789')['artwork_id'],'789')

    def test_candidate_ranking_source_cannot_change_persisted_run_mode(self):
        self.manual('one')
        with self.assertRaisesRegex(StateError,'ranking source'):
            self.state.reserve_run([{'id':123,'page_count':1,'selection_decision':{'source_mode':'day'}}],run_id='manual:one')
        self.assertEqual(self.state.excluded_ids(),set())
        job=self.state.reserve_run([{'id':123,'page_count':1,'selection_decision':{'source_mode':'month'}}],run_id='manual:one')
        self.assertEqual(job['metadata']['run_context']['ranking_mode'],'month')

    def test_scheduled_prepare_and_publish_share_one_run(self):
        a=self.state.reserve_daily([{'id':123,'page_count':1}],day='2026-10-09')
        b=self.state.reserve_daily([{'id':456,'page_count':1}],day='2026-10-09')
        self.assertEqual(a['job_id'],b['job_id'])
        self.assertEqual(a['run_id'],'scheduled:2026-10-09')
        c=self.state.reserve_daily([{'id':456,'page_count':1}],day='2026-10-10')
        self.assertNotEqual(c['job_id'],a['job_id'])

    def test_run_context_cannot_be_changed_on_retry(self):
        self.manual('one')
        with self.state._transaction() as db:
            with self.assertRaises(ValueError):
                ensure_run(db,run_id='manual:one',kind='manual',day='2026-10-09',ranking_mode='day',allow_immediate_publish=True,authorization='explicit_user_request')

    def test_immediate_policy_requires_explicit_manual_authorization(self):
        with self.state._transaction() as db:
            with self.assertRaises(ValueError):
                ensure_run(db,run_id='scheduled:2026-10-09',kind='scheduled',day='2026-10-09',allow_immediate_publish=True,authorization='explicit_user_request')
            with self.assertRaises(ValueError):
                ensure_run(db,run_id='manual:noauth',kind='manual',day='2026-10-09',allow_immediate_publish=True)
            with self.assertRaises(ValueError):
                ensure_run(db,run_id='scheduled:2026-10-09',kind='manual',day='2026-10-09',allow_immediate_publish=True,authorization='explicit_user_request')

    def test_manual_immediate_claim_and_scheduled_ten_oclock_policy(self):
        self.manual('immediate'); self.manual('normal',immediate=False)
        immediate=self.reserve('immediate','123')
        normal=self.reserve('normal','456')
        scheduled=self.state.reserve_daily([{'id':789,'page_count':1}],day='2026-10-09')
        # Quality correctness is tested separately; this fixture isolates timing.
        with patch.object(State,'_readiness',return_value={'ready':True,'reasons':[]}):
            self.assertEqual(self.state.claim_publish(immediate['job_id'],'xiaohongshu')['status'],'in_flight')
            for job in (normal,scheduled):
                with self.assertRaisesRegex(StateError,'before 10:00'):
                    self.state.claim_publish(job['job_id'],'xiaohongshu')

    def test_different_manual_runs_can_claim_same_day_without_quota(self):
        self.manual('one'); self.manual('two')
        one=self.reserve('one','123'); two=self.reserve('two','456')
        with patch.object(State,'_readiness',return_value={'ready':True,'reasons':[]}):
            self.state.claim_publish(one['job_id'],'xiaohongshu')
            self.state.claim_publish(two['job_id'],'xiaohongshu')
        with self.state._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM run_publish_days').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM publish_days').fetchone()[0],0)

    def test_only_completed_same_run_skips_other_run_can_select(self):
        self.manual('one'); self.manual('two')
        one=self.reserve('one','123')
        with self.state._transaction() as db:
            db.execute("UPDATE jobs SET status='complete' WHERE job_id=?",(one['job_id'],))
            db.execute("UPDATE publications SET status='published',receipt_id='test-receipt' WHERE job_id=?",(one['job_id'],))
        with self.state._connect() as db:
            self.assertEqual(run_check(db,'manual:one')['action'],'skip_run_already_published')
            self.assertEqual(run_check(db,'manual:two')['action'],'select_for_run')

    def test_ranking_alternates_by_shanghai_calendar_not_number_of_manual_runs(self):
        self.assertEqual(ranking_for_day('2026-10-09'),'month')
        self.assertEqual(ranking_for_day('2026-10-10'),'day')
        self.assertEqual(ranking_for_day('2026-10-11'),'month')

    def test_cli_uses_configured_calendar_for_scheduled_and_manual_runs(self):
        config = {'storage': {'state_db': str(self.path)}, 'schedule': {'publish_at': '10:00'},
                  'publishing': {'platforms': ['xiaohongshu']},
                  'selection': {'ranking_schedule': {'timezone': 'Asia/Shanghai',
                                'anchor_date': '2026-10-10', 'cycle': ['month', 'day']}}}
        config_path = Path(self.tmp.name) / 'workflow.json'
        config_path.write_text(json.dumps(config))
        cases = [(['ensure-scheduled', '--day', '2026-10-09'], 'day'),
                 (['ensure-scheduled', '--day', '2026-10-10'], 'month'),
                 (['ensure-scheduled', '--day', '2026-10-14'], 'month'),
                 (['create-manual', '--day', '2026-10-09', '--run-id', 'manual:calendar'], 'day'),
                 (['create-manual', '--day', '2026-10-09', '--run-id', 'manual:override', '--ranking-mode', 'month'], 'month')]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['--config', str(config_path)] + arguments), 0)
                self.assertEqual(json.loads(output.getvalue())['ranking_mode'], expected)
        self.assertEqual(json.loads(config_path.read_text()), config)

    def test_legacy_migration_preserves_rows_receipts_and_children(self):
        path=Path(self.tmp.name)/'legacy.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.executescript('''CREATE TABLE jobs(job_id TEXT PRIMARY KEY,day TEXT NOT NULL UNIQUE,artwork_id TEXT NOT NULL UNIQUE,status TEXT NOT NULL);
            CREATE TABLE publications(job_id TEXT NOT NULL REFERENCES jobs(job_id),receipt_id TEXT,status TEXT);
            CREATE TABLE publish_days(day TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(job_id));
            INSERT INTO jobs VALUES('old-job','2026-10-09','123456789','complete');
            INSERT INTO publications VALUES('old-job','test-existing-receipt','published');
            INSERT INTO publish_days VALUES('2026-10-09','old-job');''')
            before={table:db.execute('SELECT * FROM '+table).fetchall() for table in ('jobs','publications','publish_days')}
            migration=migrate_runs(db)
            self.assertTrue(migration['jobs_rebuilt'])
            self.assertEqual(db.execute('SELECT job_id,day,artwork_id,status FROM jobs').fetchall(),before['jobs'])
            self.assertEqual(db.execute('SELECT * FROM publications').fetchall(),before['publications'])
            self.assertEqual(db.execute('SELECT * FROM publish_days').fetchall(),before['publish_days'])
            self.assertEqual(db.execute('SELECT run_id FROM jobs').fetchone()[0],'legacy:old-job')
            self.assertEqual(db.execute('SELECT ranking_mode FROM workflow_runs').fetchone()[0],'day')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0],1)
            self.assertFalse(migrate_runs(db)['jobs_rebuilt'])


if __name__=='__main__':
    unittest.main()
