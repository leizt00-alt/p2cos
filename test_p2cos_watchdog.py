"""Offline tests: no credentials, production database, image tools or publications."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone

from p2cos_watchdog import inspect_health


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'fixture.sqlite3'
        self.now = datetime(2026, 1, 2, 3, 0, tzinfo=timezone.utc)
        self.run = 'scheduled:2026-01-02'
        self.config = {'schedule': {'publish_at': '10:00'}}
        with sqlite3.connect(self.db) as db:
            db.executescript('''
                CREATE TABLE workflow_runs(run_id TEXT, kind TEXT, day TEXT);
                CREATE TABLE jobs(job_id TEXT, run_id TEXT, artwork_id TEXT, page_count INTEGER,
                    status TEXT, research TEXT, created_at TEXT, updated_at TEXT);
                CREATE TABLE pages(job_id TEXT, page_index INTEGER, raw TEXT, cos TEXT,
                    features TEXT, quality_review TEXT, correction_count INTEGER);
                CREATE TABLE publications(job_id TEXT, platform TEXT, status TEXT, copy TEXT,
                    attempt_count INTEGER, receipt_id TEXT, updated_at TEXT);
                CREATE TABLE quality_corrections(attempt_id TEXT, job_id TEXT, page_index INTEGER,
                    attempt_number INTEGER, generator_agent_id TEXT, status TEXT, created_at TEXT,
                    recovery TEXT, optimization_slot INTEGER, replacement_of TEXT);
                CREATE TABLE correction_recovery_events(attempt_id TEXT, event TEXT, created_at TEXT);
            ''')
            db.execute('INSERT INTO workflow_runs VALUES (?,?,?)', (self.run, 'scheduled', '2026-01-02'))
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)',
                       ('j', self.run, 'a', 1, 'ready', '{}', '2026-01-02 00:00:00', '2026-01-02 00:00:00'))
            db.execute('INSERT INTO pages VALUES (?,?,?,?,?,?,?)', ('j', 0, '{}', '{}', '{}', None, 1))
            db.execute('INSERT INTO publications VALUES (?,?,?,?,?,?,?)',
                       ('j', 'xiaohongshu', 'pending', '{}', 0, None, '2026-01-02 00:00:00'))

    def tearDown(self):
        self.temp.cleanup()

    def sql(self, query, args=()):
        with sqlite3.connect(self.db) as db:
            db.execute(query, args)

    def check(self, **kw):
        return inspect_health(self.db, config=self.config, _now=kw.pop('_now', self.now), **kw)

    def attempt(self, phase=None, *, status='pending', name='c1', number=1, replacement_of=None):
        self.sql('INSERT INTO quality_corrections VALUES (?,?,?,?,?,?,?,?,?,?)',
                 (name, 'j', 0, number, 'generator', status, '2026-01-02 00:00:00',
                  json.dumps({'phase': phase}) if phase else None, 1, replacement_of))

    def artifact(self, name, body):
        path = self.root / name
        path.write_text(json.dumps(body))
        return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def enable_policy(self):
        auth = self.artifact('authorization.json', {'kind': 'explicit_user_authorization',
            'scope': 'bounded_unknown_recovery_policy', 'source': 'offline fixture only',
            'user_message': 'synthetic authorization for isolated test'})
        policy = self.artifact('policy.json', {'kind': 'bounded_unknown_recovery_policy', 'enabled': True,
            'scope': 'all_runs', 'max_unknown_replacements_per_page': 1, 'generation_only': True,
            'unknown_publication_action': 'reconcile', 'user_authorization': auth})
        self.config['recovery'] = {'auto_unknown_recovery_enabled': True, 'policy': policy, 'user_authorization': auth}

    def stop(self, **changes):
        body = {'kind': 'executor_stopped', 'run_id': self.run, 'artwork_id': 'a', 'job_id': 'j',
                'page_index': 0, 'attempt_id': 'c1', 'generator_agent_id': 'generator',
                'local_request_id': None, 'request_sha256': None, 'outcome': 'stopped',
                'original_call_recoverable': False, 'statement': 'synthetic local stop observation; remote unknown'}
        body.update(changes)
        return self.artifact('stop.json', body)

    def test_missing_database_is_not_created(self):
        path = self.root / 'missing' / 'state.sqlite3'
        result = inspect_health(path, _now=self.now)
        self.assertEqual(result['stage'], 'database_missing')
        self.assertFalse(path.parent.exists())

    def test_shanghai_date_and_explicit_run(self):
        result = self.check(_now=datetime(2026, 1, 1, 16, 1, tzinfo=timezone.utc))
        self.assertEqual(result['run_id'], self.run)
        self.assertEqual(result['job_id'], 'j')
        self.assertEqual(self.check(run_id='scheduled:2026-01-01')['stage'], 'run_missing')

    def test_no_job_does_not_select(self):
        self.sql('DELETE FROM jobs')
        result = self.check()
        self.assertEqual(result['stage'], 'job_missing')
        self.assertEqual(result['next_action'], 'report_missing_job_do_not_select')

    def test_legacy_mapping_never_guessed(self):
        self.sql('ALTER TABLE jobs RENAME COLUMN run_id TO old_run_id')
        self.assertEqual(self.check()['stage'], 'legacy_run_mapping_missing')

    def test_legacy_schema_missing_recovery_column_remains_unknown(self):
        self.attempt()
        self.sql('ALTER TABLE quality_corrections DROP COLUMN recovery')
        self.sql('ALTER TABLE quality_corrections DROP COLUMN replacement_of')
        self.sql('ALTER TABLE quality_corrections DROP COLUMN optimization_slot')
        result = self.check()
        self.assertEqual(result['stage'], 'generation_unknown')
        self.assertEqual(result['attempts'][0]['optimization_slot'], 1)
        self.assertFalse(result['age_is_failure_evidence'])
        self.assertIsNone(result['completed_quality_slots'])

    def test_age_and_missing_heartbeat_do_not_authorize_replacement(self):
        self.attempt('submitted')
        self.enable_policy()
        result = self.check(_now=datetime(2030, 1, 2, tzinfo=timezone.utc), run_id=self.run)
        self.assertEqual(result['next_action'], 'recover_original_call_or_obtain_executor_stop_evidence')
        self.assertGreater(result['age_seconds'], 86400)
        self.assertIsNone(result['attempts'][0]['last_heartbeat_at'])

    def test_saved_result_then_recorded_are_not_new_generation(self):
        self.attempt('result_saved')
        self.assertEqual(self.check()['next_action'], 'verify_and_commit_saved_result_no_generation')
        self.sql("UPDATE quality_corrections SET status='recorded',recovery=?", (json.dumps({'phase': 'recorded'}),))
        self.assertEqual(self.check()['next_action'], 'fresh_independent_qa_then_selection')

    def test_unknown_needs_policy_and_bound_stop_and_unrecoverable_call(self):
        self.attempt()
        self.assertNotEqual(self.check(executor_evidence=[self.stop()])['next_action'], 'authorized_replacement_after_state_checks')
        self.enable_policy()
        self.assertEqual(self.check(executor_evidence=[self.stop()])['next_action'], 'authorized_replacement_after_state_checks')
        for change in ({'run_id': 'other'}, {'artwork_id': 'other'}, {'attempt_id': 'other'},
                       {'local_request_id': 'stale'}, {'original_call_recoverable': True}, {'outcome': 'running'}):
            with self.subTest(change=change):
                self.assertNotEqual(self.check(executor_evidence=[self.stop(**change)])['next_action'],
                                    'authorized_replacement_after_state_checks')

    def test_mutated_policy_or_boolean_alone_not_authorization(self):
        self.attempt()
        self.enable_policy()
        Path(self.config['recovery']['policy']['path']).write_text('{}')
        self.assertFalse(self.check()['policy']['valid'])
        self.config['recovery'] = {'auto_unknown_recovery_enabled': True}
        self.assertFalse(self.check()['policy']['valid'])

    def test_replacement_budget_is_page_wide_and_no_chain(self):
        self.attempt()
        self.enable_policy()
        self.attempt('failed', status='failed', name='c2', number=2, replacement_of='older')
        self.assertEqual(self.check(executor_evidence=[self.stop()])['next_action'],
                         'report_unknown_policy_or_replacement_budget_blocked')
        self.sql('DELETE FROM quality_corrections WHERE attempt_id=?', ('c2',))
        self.sql('UPDATE quality_corrections SET replacement_of=?', ('older',))
        self.assertEqual(self.check(executor_evidence=[self.stop()])['next_action'],
                         'report_unknown_policy_or_replacement_budget_blocked')

    def test_reassignment_only_unclaimed_normal_attempt(self):
        self.attempt('reserved')
        self.assertEqual(self.check()['next_action'], 'fresh_agent_reassign_if_original_unavailable_and_never_dispatched')
        self.sql('INSERT INTO correction_recovery_events VALUES (?,?,?)', ('c1', 'dispatch_claimed', '2026-01-02 00:01:00'))
        self.assertEqual(self.check()['next_action'], 'report_claim_history_do_not_reassign')
        self.sql('DELETE FROM correction_recovery_events')
        self.sql('UPDATE quality_corrections SET replacement_of=?', ('older',))
        self.assertEqual(self.check()['next_action'], 'recover_authorized_executor_or_manual_attention')

    def test_physical_correction_budget_cannot_exceed_three(self):
        self.attempt(number=3)
        self.enable_policy()
        self.assertEqual(self.check(executor_evidence=[self.stop()])['next_action'],
                         'report_unknown_policy_or_replacement_budget_blocked')

    def test_heartbeat_does_not_reset_progress_age(self):
        self.attempt('submitted')
        self.sql('INSERT INTO correction_recovery_events VALUES (?,?,?)', ('c1', 'submitted', '2026-01-02 00:01:00'))
        self.sql('INSERT INTO correction_recovery_events VALUES (?,?,?)', ('c1', 'heartbeat', '2026-01-02 02:59:00'))
        result = self.check()
        self.assertEqual(result['age_seconds'], 10740)
        self.assertEqual(result['attempts'][0]['heartbeat_age_seconds'], 60)

    def test_publication_unknown_always_wins_over_generation_replacement(self):
        self.attempt()
        self.enable_policy()
        for status in ('in_flight', 'reconciliation_required', 'unknown', 'under_review'):
            self.sql('UPDATE publications SET status=?', (status,))
            self.assertEqual(self.check(executor_evidence=[self.stop()])['next_action'],
                             'remote_reconcile_only_never_resubmit')

    def test_verified_complete_skip_but_missing_receipt_reconcile(self):
        self.sql("UPDATE publications SET status='published',receipt_id='verified-fixture'")
        self.assertEqual(self.check()['next_action'], 'skip_run_already_published')
        self.sql('UPDATE publications SET receipt_id=NULL')
        self.assertEqual(self.check()['next_action'], 'remote_reconcile_only_never_resubmit')

    def test_publish_preflight_is_not_quality_or_publication_authorization(self):
        self.sql("UPDATE pages SET quality_review='{}'")
        result = self.check()
        self.assertEqual(result['stage'], 'publish_preflight')
        self.assertFalse(result['publication_authorized_by_watchdog'])
        self.assertEqual(result['deadline_at'], '2026-01-02T10:00:00+08:00')
        early = self.check(_now=datetime(2026, 1, 2, 1, 15, tzinfo=timezone.utc))
        self.assertEqual(early['stage'], 'prepare_only_before_publish_time')

    def test_readonly_snapshot_preserves_schema_and_bytes(self):
        self.attempt()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        result = self.check()
        self.assertTrue(result['read_only'])
        self.assertEqual(result['total_changes'], 0)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.assertFalse(Path(str(self.db) + '-journal').exists())


if __name__ == '__main__':
    unittest.main()
