"""Offline regression checks for the publisher's irreversible boundary."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import social_auto_upload_adapter as adapter
import test_p2cos_state as state_fixtures
from p2cos_runs import ensure_run
from p2cos_state import State, StateError


class AdapterTests(unittest.TestCase):
    def account_page(self, profile_text):
        page = Mock()
        page.goto = AsyncMock(return_value=Mock(status=200))
        navigation = Mock(wait_for=AsyncMock())
        profile = Mock()
        profile.first = profile
        loaded = False

        async def load_profile(**kwargs):
            nonlocal loaded
            loaded = True

        async def body_text():
            return profile_text if loaded else 'Test Creator\n笔记管理'

        profile.wait_for = AsyncMock(side_effect=load_profile)
        page.get_by_text.side_effect = lambda selector, **kw: navigation if isinstance(selector, str) else profile
        page.locator.return_value.inner_text = AsyncMock(side_effect=body_text)
        return page, profile

    def test_account_check_waits_for_delayed_visible_profile(self):
        page, profile = self.account_page('Test Creator\n小红书账号: 10000000001')
        config = {'publishing': {'xiaohongshu': {'account': 'Test Creator', 'account_id': '10000000001'}}}
        result = asyncio.run(adapter.verify_account(page, config))
        self.assertEqual(result['status'], 'account_verified')
        profile.wait_for.assert_awaited_once_with(state='visible', timeout=20000)

    def test_loaded_different_account_still_blocks(self):
        page, _ = self.account_page('Test Creator\n小红书账号: 10000000002')
        config = {'publishing': {'xiaohongshu': {'account': 'Test Creator', 'account_id': '10000000001'}}}
        with self.assertRaisesRegex(adapter.AdapterError, 'expected_account_id_not_visible'):
            asyncio.run(adapter.verify_account(page, config))

    def job(self, status='pending', paths=('old.png',)):
        return {'job_id': 'job', 'artwork_id': '123', 'page_count': len(paths),
                'metadata': {'artist': 'Artist'},
                'pages': [{'page_index': i, 'cos': {'path': p}} for i, p in enumerate(paths)],
                'publications': {'xiaohongshu': {'status': status, 'copy': {'title': 'Title', 'body': 'AI生成 Artist https://www.pixiv.net/artworks/123'}}}}

    def test_published_skips_without_opening_database_or_browser(self):
        with patch.object(adapter, 'connect_readonly', side_effect=AssertionError('must not inspect assets')):
            self.assertEqual(adapter.preflight({}, self.job('published'))['status'], 'skipped_already_published')

    def test_uncertain_attempt_requires_reconciliation_before_any_asset_work(self):
        for status in ('in_flight', 'reconciliation_required'):
            with patch.object(adapter, 'connect_readonly', side_effect=AssertionError('must not inspect assets')):
                self.assertEqual(adapter.preflight({}, self.job(status))['status'], 'reconciliation_required')

    def run_publish(self, *, click_error=None, declaration_error=None, freeze_new_job=False):
        original = self.job()
        frozen = self.job('in_flight', ('new-0.png', 'new-1.png')) if freeze_new_job else deepcopy(original)
        note_instances = []

        class Note:
            def __init__(self, images, body, *args, **kwargs):
                self.image_paths = images
                self.body = body
                self.validate_upload_args = AsyncMock()
                self.fill_meta = AsyncMock()
                note_instances.append(self)

        state = Mock()
        state.claim_publish.return_value = {'status': 'in_flight'}
        state.get_job.return_value = frozen
        state.publish_ready.return_value = {'ready': True}
        control = Mock()
        control.first = control
        control.wait_for = AsyncMock()
        control.set_input_files = AsyncMock()
        control.count = AsyncMock(return_value=1)
        control.click = AsyncMock(side_effect=click_error)
        page = Mock()
        page.locator.return_value = control
        page.get_by_role.return_value = control
        page.goto = AsyncMock()
        page.wait_for_url = AsyncMock()

        @asynccontextmanager
        async def browser(config):
            yield page

        config = {'storage': {'state_db': 'fake'}, 'schedule': {'publish_at': '10:00'},
                  'publishing': {'platforms': ['xiaohongshu']}}
        with (patch.object(adapter, 'preflight', return_value={'status': 'ready', 'ready': True, 'ordered_images': ['old.png']}),
              patch.object(adapter, 'upstream_class', return_value=Note),
              patch.object(adapter, 'State', return_value=state),
              patch.object(adapter, 'browser', browser),
              patch.object(adapter, 'verify_account', AsyncMock()),
              patch.object(adapter, 'verify_form', AsyncMock()) as verify_form,
              patch.object(adapter, 'apply_ai_declaration', AsyncMock(side_effect=declaration_error)),
              patch.object(adapter, 'published_candidates', AsyncMock(return_value=[]))):
            result = asyncio.run(adapter.publish(config, original))
        return result, state, control, note_instances, verify_form, frozen

    def test_click_timeout_is_unknown_and_never_retried(self):
        result, state, control, _, _, _ = self.run_publish(click_error=TimeoutError('simulated'))
        self.assertEqual(result['status'], 'reconciliation_required')
        self.assertEqual(control.click.await_count, 1)
        self.assertFalse(state.fail_publish.call_args.kwargs['definitely_not_published'])
        state.complete_publish.assert_not_called()
        self.assertFalse(result['fallback_allowed'])
        self.assertTrue(result['submit_attempted'])

    def test_missing_ai_declaration_blocks_before_submit(self):
        result, state, control, _, _, _ = self.run_publish(declaration_error=adapter.AdapterError('ai_missing'))
        self.assertEqual(result['status'], 'blocked_before_submit')
        control.click.assert_not_awaited()
        self.assertTrue(state.fail_publish.call_args.kwargs['definitely_not_published'])
        self.assertTrue(result['fallback_allowed'])
        self.assertTrue(state.fail_publish.call_args.kwargs['reason'].startswith(adapter.PRIMARY_SAFE_FAILURE))

    def test_claim_freezes_new_package_and_uploads_every_new_page_in_order(self):
        result, state, control, notes, verify_form, frozen = self.run_publish(freeze_new_job=True)
        self.assertEqual(notes[-1].image_paths, ['new-0.png', 'new-1.png'])
        control.set_input_files.assert_awaited_once_with(['new-0.png', 'new-1.png'])
        self.assertEqual(verify_form.await_args.args[1], frozen)
        self.assertEqual(control.click.await_count, 1)
        self.assertEqual(result['status'], 'reconciliation_required')
        state.complete_publish.assert_not_called()

    def test_confirmed_pre_submit_failure_allows_fresh_claim_for_legacy(self):
        failed = self.job()
        failed['publications']['xiaohongshu'].update(
            attempt_count=1, last_error=adapter.PRIMARY_SAFE_FAILURE + 'ai_declaration_not_confirmed')
        state = Mock()
        state.claim_publish.return_value = {'attempt_count': 2, 'idempotency_key': 'p2cos:123:xiaohongshu'}
        frozen = deepcopy(failed)
        frozen['publications']['xiaohongshu']['status'] = 'in_flight'
        frozen['pages'][0]['cos']['sha256'] = 'expected-hash'
        state.get_job.return_value = frozen
        config = {'storage': {'state_db': 'fake'}, 'schedule': {'publish_at': '10:00'},
                  'publishing': {'platforms': ['xiaohongshu'], 'xiaohongshu': {'account': 'Example Creator', 'account_id': '12345678900'}}}
        with (patch.object(adapter, 'preflight', return_value={'ready': True, 'status': 'ready'}),
              patch.object(adapter, 'State', return_value=state)):
            admission = adapter.fallback_preflight(config, failed)
            result = adapter.claim_legacy(config, failed)
        self.assertTrue(admission['fallback_allowed'])
        self.assertEqual(result['status'], 'legacy_claimed')
        state.claim_publish.assert_called_once_with('job', 'xiaohongshu')
        self.assertEqual(result['images'][0]['sha256'], 'expected-hash')

    def test_unknown_cannot_use_even_stale_safe_failure_marker_for_legacy(self):
        for status in ('in_flight', 'reconciliation_required'):
            job = self.job(status)
            job['publications']['xiaohongshu'].update(attempt_count=2, last_error=adapter.PRIMARY_SAFE_FAILURE + 'old')
            with patch.object(adapter, 'State') as state:
                result = adapter.claim_legacy({}, job)
            self.assertFalse(result['fallback_allowed'])
            state.assert_not_called()

    def test_published_job_never_claims_legacy(self):
        with patch.object(adapter, 'State') as state:
            result = adapter.claim_legacy({}, self.job('published'))
        self.assertEqual(result['status'], 'skipped_already_published')
        self.assertFalse(result['fallback_allowed'])
        state.assert_not_called()


class AdapterRunPolicyTests(unittest.TestCase):
    """Use real isolated run/state tables and fully validated synthetic media."""
    def setUp(self):
        self.fixture = state_fixtures.StateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.now = datetime(2026, 10, 9, 1, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.state = State(self.fixture.db, platforms=('xiaohongshu',), clock=lambda: self.now)
        self.fixture.state = self.state
        self.config = {'storage': {'state_db': str(self.fixture.db)},
            'schedule': {'publish_at': '10:00'}, 'publishing': {'platforms': ['xiaohongshu'],
                'xiaohongshu': {'account': 'Example Creator', 'account_id': '12345678900',
                    'observed_limits': {'max_images_per_post': 18,
                        'max_image_bytes': 33554432, 'formats': ['png', 'jpg', 'jpeg', 'webp']}}}}

    def ready(self, run_id, artwork, immediate=False):
        kind = run_id.split(':', 1)[0]
        with self.state._transaction() as db:
            ensure_run(db, run_id=run_id, kind=kind, day='2026-10-09',
                allow_immediate_publish=immediate,
                authorization='explicit_user_request' if kind == 'manual' else None)
        self.fixture.reserve = lambda artwork, pages=1: self.state.reserve_run(
            [{'id': artwork, 'page_count': pages, 'artist': 'Artist'}], run_id=run_id)
        job = self.fixture.ready(artwork)
        self.state.record_copy(job['job_id'], 'xiaohongshu', title='Test portrait',
            body=f'AI生成 Artist https://www.pixiv.net/artworks/{artwork}')
        return self.state.get_job(job['job_id'])

    def test_other_published_work_today_does_not_block_new_authorized_manual_run(self):
        old = self.ready('manual:earlier', 123, immediate=True)
        self.state.claim_publish(old['job_id'], 'xiaohongshu')
        self.state.complete_publish(old['job_id'], 'xiaohongshu', receipt_id='synthetic-old-receipt', verified=True)
        # Preserve an old daily-quota row to catch accidental legacy checks.
        with self.state._transaction() as db:
            db.execute('INSERT INTO publish_days(day,job_id) VALUES (?,?)', ('2026-10-09', old['job_id']))
        fresh = self.ready('manual:new-request', 456, immediate=True)
        result = adapter.preflight(self.config, fresh, clock=lambda: self.now)
        self.assertTrue(result['ready'], result['reasons'])
        self.assertEqual(result['earliest_publish_time'], '00:00')
        self.assertEqual(self.state.claim_publish(fresh['job_id'], 'xiaohongshu')['status'], 'in_flight')

    def test_one_am_requires_persisted_manual_immediate_authorization(self):
        manual = self.ready('manual:now', 123, immediate=True)
        ordinary = self.ready('manual:without-immediate', 456)
        scheduled = self.ready('scheduled:2026-10-09', 789)
        self.assertTrue(adapter.preflight(self.config, manual, clock=lambda: self.now)['ready'])
        self.assertEqual(self.state.claim_publish(manual['job_id'], 'xiaohongshu')['status'], 'in_flight')
        for job in (ordinary, scheduled):
            # Forging the input snapshot cannot replace persisted authorization.
            job['metadata']['run_context'] = {'kind': 'manual', 'allow_immediate_publish': True,
                                               'authorization': 'explicit_user_request'}
            result = adapter.preflight(self.config, job, clock=lambda: self.now)
            self.assertFalse(result['ready'])
            self.assertIn('before_publish_window_Asia_Shanghai', result['reasons'])
            with self.assertRaisesRegex(StateError, 'before 10:00'):
                self.state.claim_publish(job['job_id'], 'xiaohongshu')


if __name__ == '__main__':
    unittest.main()
