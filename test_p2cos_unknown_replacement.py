"""Bounded recovery tests: synthetic files and isolated SQLite only."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from p2cos_recovery import inspect_recovery
from p2cos_state import StateError
from recovery_test_fixtures import fail, save_result
import test_p2cos_recovery as recovery_fixtures


class UnknownReplacementTests(unittest.TestCase):
    for _name in ('setUp', 'picture', 'page', 'trace', 'fixture', 'add_package', 'review',
                  'optimization', 'begin', 'selection', 'start', 'prepare', 'restart'):
        locals()[_name] = getattr(recovery_fixtures.RecoveryTests, _name)

    def artifact(self, name, body):
        path = self.root / (name + '.json')
        raw = json.dumps(body).encode()
        path.write_bytes(raw)
        return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}

    def authorization(self, job, old, new='synthetic-replacement', *, mode='manual_requested'):
        detail = self.state.get_job(job)
        item = next(a for a in inspect_recovery(self.db, job)['attempts'] if a['attempt_id'] == old['attempt_id'])
        target = {'run_id': detail['run_id'], 'job_id': job, 'artwork_id': detail['artwork_id'],
                  'page_index': 0, 'attempt_id': old['attempt_id']}
        source = self.artifact('user', {'kind': 'explicit_user_authorization', 'source': 'Synthetic test conversation',
            'user_message': 'Synthetic explicit recovery authorization; not actual user evidence.',
            'scope': 'bounded_unknown_recovery_policy' if mode == 'explicit_policy' else 'one_unknown_replacement',
            **(target if mode == 'manual_requested' else {})})
        stopped = self.artifact('stopped', {'kind': 'executor_stopped', 'generator_agent_id': old['generator_agent_id'],
            'outcome': 'stopped', 'statement': 'Synthetic local executor stopped; remote outcome unknown.',
            'local_request_id': item['local_request_id'], 'request_sha256': item['request_sha256'], **target})
        body = {'kind': 'authorized_unknown_replacement', 'mode': mode, 'run_id': detail['run_id'],
            'job_id': job, 'artwork_id': detail['artwork_id'], 'page_index': 0,
            'attempt_id': old['attempt_id'], 'generator_agent_id': old['generator_agent_id'],
            'new_generator_agent_id': new, 'operator_agent_id': 'synthetic-operator',
            'local_request_id': item['local_request_id'], 'request_sha256': item['request_sha256'],
            'accept_unknown_outcome': True, 'original_executor_stopped': True,
            'statement': 'Synthetic authorized replacement, retaining uncertainty.',
            'user_authorization': source, 'executor_stop_evidence': stopped}
        policy = self.artifact('policy', {'kind': 'bounded_unknown_recovery_policy', 'enabled': True,
            'max_unknown_replacements_per_page': 1, 'generation_only': True,
            'unknown_publication_action': 'reconcile', 'scope': 'all_runs', 'user_authorization': source}) if mode == 'explicit_policy' else None
        return self.artifact('authorization', body), policy

    def replace(self, job, old, *, new='synthetic-replacement', mode='manual_requested', authorization=None, policy=None, optimization=None):
        if authorization is None:
            authorization, policy = self.authorization(job, old, new, mode=mode)
        return self.state.supersede_unknown_with_authorization(job, 0,
            attempt_id=old['attempt_id'], generator_agent_id=old['generator_agent_id'],
            new_generator_agent_id=new, operator_agent_id='synthetic-operator',
            optimization=optimization or self.optimization(job), authorization=authorization,
            mode=mode, policy=policy)

    def unknown(self):
        job, old, args, trace = self.start()
        self.prepare(job, args, trace)
        self.state.claim_correction_dispatch(job, 0, **args)
        return job, old, args, trace

    def test_unknown_replacement_then_normal_retry_keeps_two_real_slots(self):
        job, old, args, trace = self.unknown()
        replacement = self.replace(job, old)
        self.assertEqual((replacement['attempt_number'], replacement['optimization_slot']), (2, 1))
        self.assertEqual(self.page(job)['correction_count'], 1)
        rows = inspect_recovery(self.db, job)['attempts']
        self.assertEqual([r['phase'] for r in rows], ['superseded_unknown', 'reserved'])
        self.assertEqual(rows[0]['superseded_by'], replacement['attempt_id'])
        self.assertEqual(rows[1]['replacement_of'], old['attempt_id'])
        with self.assertRaises(StateError):
            self.state.claim_correction_dispatch(job, 0, **args)
        fail(self.state, self.root, job, 0, replacement, 'Synthetic replacement optimized prompt', 'Synthetic failure')
        with self.assertRaises(StateError):
            self.state.select_best_available(job, self.selection(job))
        final = self.begin(job)
        self.assertEqual((final['attempt_number'], final['optimization_slot']), (3, 2))
        fail(self.state, self.root, job, 0, final, 'Synthetic final optimized prompt', 'Synthetic failure')
        self.state.select_best_available(job, self.selection(job))
        self.assertTrue(self.state.publish_ready(job)['ready'])
        self.assertEqual(self.page(job)['correction_count'], 2)
        with self.assertRaises(StateError):
            self.begin(job)

    def test_recorded_replacement_trace_uses_physical_number_and_requires_new_qa(self):
        job, old, _, _ = self.unknown()
        replacement = self.replace(job, old)
        output = self.picture('physical-v2.png', 10)
        trace = self.trace(job, 0, output, replacement['generator_agent_id'], replacement)
        save_result(self.state, self.root, job, 0, replacement, trace)
        args = {k: replacement[k] for k in ('attempt_id', 'generator_agent_id')}
        self.restart()
        self.state.commit_correction_result(job, 0, **args)
        generation = self.page(job)['generation']
        self.assertEqual((generation['correction_attempt'], generation['optimization_slot']), (2, 1))
        self.assertEqual(generation['replacement_of'], old['attempt_id'])
        self.assertEqual(generation['trace']['attempt'], 2)
        self.assertFalse(self.state.publish_ready(job)['ready'])
        self.review(job, failed=(), reviewer='synthetic-new-reviewer')
        self.assertTrue(self.state.publish_ready(job)['ready'])
        self.assertEqual(self.state.commit_correction_result(job, 0, **args)['path'], str(output))

    def test_only_one_concurrent_replacement_is_reserved(self):
        job, old, _, _ = self.unknown()
        authorization, policy = self.authorization(job, old)
        def reserve(_):
            try:
                return self.replace(job, old, authorization=authorization, policy=policy)
            except StateError:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(reserve, range(4)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(inspect_recovery(self.db, job)['attempts']), 2)

    def test_physical_three_records_and_tie_prefers_earlier_physical_version(self):
        job, old, _, _ = self.unknown()
        replacement = self.replace(job, old)
        for attempt, variation in ((replacement, 9), (None, 10)):
            attempt = attempt or self.begin(job)
            path = self.picture(f"physical-{attempt['attempt_number']}.png", variation)
            trace = self.trace(job, 0, path, attempt['generator_agent_id'], attempt)
            save_result(self.state, self.root, job, 0, attempt, trace)
            args = {k: attempt[k] for k in ('attempt_id', 'generator_agent_id')}
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda _: self.state.commit_correction_result(job, 0, **args), range(3)))
            self.assertEqual(results, [results[0]] * 3)
            self.assertEqual(self.page(job)['generation']['trace']['attempt'], attempt['attempt_number'])
            self.review(job, reviewer=f"synthetic-physical-reviewer-{attempt['attempt_number']}")
        self.assertEqual(self.page(job)['generation']['correction_attempt'], 3)
        self.state.select_best_available(job, self.selection(job))
        self.assertTrue(self.state.publish_ready(job)['ready'])
        # Equal fixture QA scores retain the established physical-attempt sort.
        versions = self.state.quality_candidates(job)['pages']['0']
        ordered = sorted(versions, key=lambda c: (not c['visual_passed'], -c['score'], c['attempt']))
        self.assertEqual([v['attempt'] for v in ordered], [0, 2, 3])
        self.assertEqual(self.selection(job)['selected']['0']['generation_sha256'], ordered[0]['generation_sha256'])

    def test_legacy_unknown_snapshot_and_late_result_remain_audit_only(self):
        job, old, args, trace = self.start()
        with sqlite3.connect(self.db) as db:
            db.execute('UPDATE quality_corrections SET recovery=NULL WHERE attempt_id=?', (old['attempt_id'],))
        replacement = self.replace(job, old)
        with self.assertRaises(StateError):
            self.state.reassign_unsubmitted_correction(job, 0,
                attempt_id=replacement['attempt_id'], generator_agent_id=replacement['generator_agent_id'],
                new_generator_agent_id='synthetic-unapproved-third-generator', operator_agent_id='synthetic-operator',
                reason='Cannot alter the authorized replacement executor')
        with sqlite3.connect(self.db) as db:
            event = json.loads(db.execute("SELECT details FROM correction_recovery_events WHERE event='authorized_unknown_superseded'").fetchone()[0])
        self.assertIsNone(event['prior_journal'])
        from recovery_test_fixtures import receipt
        evidence = receipt(self.root, job, 0, old, 'tool_result', state=self.state,
            tool_result={'fixture': 'synthetic late return'}, output_sha256=trace['output_sha256'], prompt_sha256=trace['prompt_sha256'])
        baseline_hash = self.page(job)['generation_sha256']
        saved = self.state.audit_late_superseded_result(job, 0, **args, generation_trace=trace, evidence=evidence)
        self.assertEqual(saved['phase'], 'superseded_unknown')
        self.assertEqual(self.state.audit_late_superseded_result(job, 0, **args, generation_trace=trace, evidence=evidence), saved)
        self.assertEqual(self.page(job)['pending_correction']['attempt_id'], replacement['attempt_id'])
        self.assertEqual(self.page(job)['generation_sha256'], baseline_hash)
        self.assertEqual(self.page(job)['correction_count'], 1)
        with self.assertRaises(StateError):
            self.state.commit_correction_result(job, 0, **args)

    def test_replacement_unknown_cannot_be_replaced_or_count_as_completed(self):
        job, old, _, _ = self.unknown()
        replacement = self.replace(job, old)
        args = {k: replacement[k] for k in ('attempt_id', 'generator_agent_id')}
        trace = self.trace(job, 0, self.picture('extra.png', 7), replacement['generator_agent_id'], replacement)
        self.prepare(job, args, trace)
        self.state.claim_correction_dispatch(job, 0, **args)
        with self.assertRaises(StateError):
            self.replace(job, replacement, new='synthetic-second-replacement')
        with self.assertRaises(StateError):
            self.state.select_best_available(job, self.selection(job))
        with self.assertRaises(StateError):
            self.begin(job)

    def test_manual_and_policy_authorization_are_required_and_hash_bound(self):
        job, old, _, _ = self.unknown()
        evidence, policy = self.authorization(job, old, mode='explicit_policy')
        with self.assertRaises(StateError):
            self.replace(job, old, mode='explicit_policy', authorization=evidence, policy=None)
        body = json.loads(Path(evidence['path']).read_text())
        body['mode'] = 'manual_requested'
        with self.assertRaises(StateError):
            self.replace(job, old, authorization=self.artifact('manual-policy-bypass', body))
        source = json.loads(Path(evidence['path']).read_text())['user_authorization']
        Path(source['path']).write_text('{}')
        with self.assertRaises(StateError):
            self.replace(job, old, mode='explicit_policy', authorization=evidence, policy=policy)
        self.assertEqual(len(inspect_recovery(self.db, job)['attempts']), 1)
        evidence, policy = self.authorization(job, old, mode='explicit_policy')
        self.replace(job, old, mode='explicit_policy', authorization=evidence, policy=policy)

    def test_binding_scope_fresh_executor_and_all_unresolved_checks_enforced(self):
        for field, value in [('run_id', 'scheduled:wrong'), ('artwork_id', 'wrong'),
                ('original_executor_stopped', False), ('accept_unknown_outcome', False),
                ('request_sha256', '0' * 64), ('new_generator_agent_id', 'wrong')]:
            with self.subTest(field=field):
                self.setUp()
                job, old, _, _ = self.unknown()
                evidence, _ = self.authorization(job, old)
                body = json.loads(Path(evidence['path']).read_text())
                body[field] = value
                evidence = self.artifact('invalid', body)
                with self.assertRaises(StateError):
                    self.replace(job, old, authorization=evidence)
                self.assertEqual(self.page(job)['pending_correction']['attempt_id'], old['attempt_id'])
        for new in ('synthetic-analyst', old['generator_agent_id'], 'synthetic-reviewer-0'):
            with self.assertRaises(StateError):
                self.replace(job, old, new=new)
        optimization = self.optimization(job)
        optimization['target_checks'] = []
        with self.assertRaises(StateError):
            self.replace(job, old, optimization=optimization)

    def test_predispatch_and_saved_result_cannot_use_unknown_replacement(self):
        job, old, args, trace = self.start()
        with self.assertRaises(StateError):
            self.replace(job, old)
        self.prepare(job, args, trace)
        with self.assertRaises(StateError):
            self.replace(job, old)
        self.state.claim_correction_dispatch(job, 0, **args)
        from recovery_test_fixtures import receipt
        evidence = receipt(self.root, job, 0, old, 'tool_result', state=self.state,
            tool_result={'fixture': 'synthetic'}, output_sha256=trace['output_sha256'], prompt_sha256=trace['prompt_sha256'])
        self.state.save_correction_result(job, 0, **args, generation_trace=trace, evidence=evidence)
        with self.assertRaises(StateError):
            self.replace(job, old)

    def test_atomic_crash_does_not_abandon_unknown_or_consume_extra_budget(self):
        job, old, _, _ = self.unknown()
        with patch.object(self.state, '_reserve_correction_recovery', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):
                self.replace(job, old)
        self.assertEqual(len(inspect_recovery(self.db, job)['attempts']), 1)
        self.assertEqual(inspect_recovery(self.db, job)['attempts'][0]['phase'], 'dispatching')
        self.assertEqual(self.page(job)['pending_correction']['attempt_id'], old['attempt_id'])
        self.replace(job, old)

    def test_migration_preserves_legacy_rows_events_publication_and_unknown(self):
        job, old, _, _ = self.unknown()
        with sqlite3.connect(self.db) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('UPDATE quality_corrections SET recovery=NULL')
            db.execute("UPDATE publications SET status='published',receipt_id='synthetic-historic-receipt'")
            names = [r[1] for r in db.execute('PRAGMA table_info(quality_corrections)')
                     if r[1] not in ('optimization_slot', 'replacement_of', 'replacement_authorization')]
            old_rows = db.execute('SELECT ' + ','.join(names) + ' FROM quality_corrections').fetchall()
            events = db.execute('SELECT * FROM correction_recovery_events').fetchall()
            publications = db.execute('SELECT * FROM publications').fetchall()
            pages = db.execute('SELECT * FROM pages').fetchall()
            db.execute('''CREATE TABLE legacy_copy(attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                page_index INTEGER NOT NULL, attempt_number INTEGER NOT NULL CHECK(attempt_number BETWEEN 1 AND 2),
                generator_agent_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, optimization TEXT, failure_details TEXT, recovery TEXT,
                UNIQUE(job_id,page_index,attempt_number), FOREIGN KEY(job_id,page_index) REFERENCES pages(job_id,page_index))''')
            db.execute('INSERT INTO legacy_copy(' + ','.join(names) + ') SELECT ' + ','.join(names) + ' FROM quality_corrections')
            db.execute('DROP TABLE quality_corrections')
            db.execute('ALTER TABLE legacy_copy RENAME TO quality_corrections')
        before = self.db.read_bytes()
        self.assertEqual(inspect_recovery(self.db, job)['attempts'][0]['phase'], 'unknown')
        self.assertEqual(self.db.read_bytes(), before)
        self.restart()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT ' + ','.join(names) + ' FROM quality_corrections').fetchall(), old_rows)
            self.assertEqual(db.execute('SELECT * FROM correction_recovery_events').fetchall(), events)
            self.assertEqual(db.execute('SELECT * FROM publications').fetchall(), publications)
            self.assertEqual(db.execute('SELECT * FROM pages').fetchall(), pages)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute('SELECT optimization_slot FROM quality_corrections').fetchone()[0], 1)
        self.assertEqual(inspect_recovery(self.db, job)['attempts'][0]['phase'], 'unknown')
        with self.assertRaises(StateError):
            self.replace(job, old)


if __name__ == '__main__':
    unittest.main()
