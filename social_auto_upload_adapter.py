"""Guarded social-auto-upload integration, using the existing browser profile.

No credential files, cookie API, storage API, request capture, or login export.
The upstream XiaoHongShuNote supplies file validation and text entry. This module
replaces its credential-file lifecycle and unbounded final-submit loop.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, time
import importlib.metadata
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from p2cos_state import State
from p2cos_runs import publication_not_before

ROOT = Path(__file__).resolve().parent
VENDOR = Path('/workspace/work/vendor/social-auto-upload')
COMMIT = '0012d2c355f88f683cc38dde2a2db209e14091bc'
PLATFORM = 'xiaohongshu'
HOME_URL = 'https://creator.xiaohongshu.com/'
PUBLISH_URL = HOME_URL + 'publish/publish?from=homepage&target=image'


class AdapterError(RuntimeError):
    """Contains only a static public diagnostic, never a browser exception."""


def read_config(path):
    config = json.loads(Path(path).read_text())
    if PLATFORM not in config['publishing']['platforms']:
        raise AdapterError('xiaohongshu_not_enabled')
    account = config['publishing'][PLATFORM]
    if not str(account.get('account_id', '')).isdigit() or not account.get('account'):
        raise AdapterError('expected_account_missing')
    return config


def connect_readonly(config):
    path = Path(config['storage']['state_db']).resolve(strict=True)
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def read_job(config, job_id=None):
    if not job_id:
        raise AdapterError('explicit_job_id_required_for_run_routing')
    with connect_readonly(config) as db:
        return State._get_job(db, job_id)


def preflight(config, job, *, owned_claim=False, clock=None):
    """Read-only, with published/unknown checks before image or quality work."""
    publication = job['publications'].get(PLATFORM)
    result = {'job_id': job['job_id'], 'artwork_id': job['artwork_id'],
              'platform': PLATFORM, 'remote_side_effects': False}
    if publication is None:
        return dict(result, status='blocked', reasons=['platform_not_assigned'])
    if publication['status'] == 'published':
        return dict(result, status='skipped_already_published', ready=False,
                    receipt_verification=publication.get('verification_kind'))
    if publication['status'] in ('in_flight', 'reconciliation_required') and not owned_claim:
        return dict(result, status='reconciliation_required', ready=False,
                    reasons=['resolve_previous_attempt_before_retry'])
    reasons = []
    with connect_readonly(config) as db:
        ready = State._readiness(db, job['job_id'])
        reasons.extend(ready['reasons'])
        # State.claim_publish uses this same persisted run authorization. Never
        # infer immediate permission from the caller's job/metadata snapshot.
        earliest = publication_not_before(db, job['job_id'], time.fromisoformat(config['schedule']['publish_at']))
    limits = config['publishing'][PLATFORM].get('observed_limits', {})
    count = job['page_count']
    if len(job['pages']) != count or [p['page_index'] for p in job['pages']] != list(range(count)):
        reasons.append('incomplete_or_reordered_pages')
    if not limits.get('max_images_per_post') or count > limits['max_images_per_post']:
        reasons.append('image_count_exceeds_verified_limit_no_truncation_allowed')
    for page in job['pages']:
        item = page['cos']
        if item:
            if item.get('bytes', 0) > limits.get('max_image_bytes', 0):
                reasons.append('image_size_exceeds_verified_limit')
            if Path(item['path']).suffix.lower().lstrip('.') not in limits.get('formats', []):
                reasons.append('image_format_not_verified')
    copy = publication.get('copy') or {}
    title, body = copy.get('title', ''), copy.get('body', '')
    if not title.strip() or len(title) > 20:
        reasons.append('title_must_be_1_to_20_characters_no_truncation_allowed')
    if not re.search(r'AI\s*生成', body, re.I):
        reasons.append('explicit_ai_generated_text_missing')
    artist = job['metadata'].get('artist', '')
    if isinstance(artist, dict):
        artist = artist.get('name', '')
    source = 'https://www.pixiv.net/artworks/' + job['artwork_id']
    if not artist or artist not in body or source not in body:
        reasons.append('original_artist_and_pixiv_source_credit_required')
    # clock is for offline fixtures only; no CLI/config clock override exists.
    now = clock() if clock is not None else datetime.now(ZoneInfo('Asia/Shanghai'))
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise AdapterError('timezone_aware_clock_required')
    now = now.astimezone(ZoneInfo('Asia/Shanghai'))
    if now.time() < earliest:
        reasons.append('before_publish_window_Asia_Shanghai')
    return dict(result, status='ready' if not reasons else 'blocked', ready=not reasons,
                reasons=reasons, page_count=count,
                run_id=job.get('run_id'), earliest_publish_time=earliest.strftime('%H:%M'),
                ordered_images=[p['cos']['path'] for p in job['pages'] if p['cos']])


def upstream_class():
    actual = subprocess.check_output(['git', '-C', str(VENDOR), 'rev-parse', 'HEAD'], text=True).strip()
    changed = subprocess.run(['git', '-C', str(VENDOR), 'diff', '--quiet', 'HEAD'], check=False)
    if actual != COMMIT or changed.returncode:
        raise AdapterError('upstream_revision_or_tracked_source_changed')
    from uploader.xiaohongshu_uploader.main import XiaoHongShuNote
    from loguru import logger
    logger.remove()  # Upstream debug handlers can serialize browser exceptions.
    return XiaoHongShuNote


@asynccontextmanager
async def browser(config):
    from patchright.async_api import async_playwright
    profile = Path(config['publishing']['browser_profile']).resolve(strict=True)
    if not profile.is_dir():
        raise AdapterError('existing_browser_profile_unavailable')
    policy = json.loads(Path('/etc/codex/network-policy.json').read_text())
    if policy.get('version') != 1 or not policy.get('http_proxy'):
        raise AdapterError('network_proxy_policy_unavailable')
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile), executable_path=config['publishing']['browser_executable'],
            headless=True, proxy={'server': policy['http_proxy']},
            viewport={'width': 1440, 'height': 1100}, locale='zh-CN',
            timezone_id='Asia/Shanghai',
            args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            page = await context.new_page()
            page.set_default_timeout(15000)
            yield page
        finally:
            await context.close()


async def verify_account(page, config):
    response = await page.goto(HOME_URL, wait_until='domcontentloaded', timeout=45000)
    if response and response.status >= 400:
        raise AdapterError('creator_http_error_' + str(response.status))
    account = config['publishing'][PLATFORM]
    try:
        await page.get_by_text('笔记管理', exact=True).wait_for(timeout=20000)
    except Exception:
        body = await page.locator('body').inner_text(timeout=5000)
        if 'upstream connect error' in body or 'envoy' in body or 'reset reason' in body:
            raise AdapterError('creator_network_proxy_error') from None
        if '/login' in urlsplit(page.url).path or '扫一扫登录' in body:
            raise AdapterError('interactive_login_required_in_existing_profile') from None
        raise AdapterError('creator_page_not_ready_login_unconfirmed') from None
    # Sidebar navigation renders before the asynchronously loaded profile card.
    # Wait for a visible account ID label, then keep the exact identity checks.
    try:
        await page.get_by_text(re.compile(r'小红书账号\s*[:：]\s*\d+')).first.wait_for(
            state='visible', timeout=20000)
    except Exception:
        raise AdapterError('creator_account_details_not_ready') from None
    body = await page.locator('body').inner_text()
    if not re.search(r'(?<!\d)' + re.escape(str(account['account_id'])) + r'(?!\d)', body):
        raise AdapterError('expected_account_id_not_visible')
    if account['account'] not in body:
        raise AdapterError('expected_account_name_not_visible')
    return {'status': 'account_verified', 'account': account['account'],
            'account_id': str(account['account_id']), 'credential_export': False}


async def apply_ai_declaration(page):
    """Require a positive visible declaration; absence never silently passes."""
    pattern = re.compile(r'^(笔记含AI合成内容|内容由AI生成|含AI生成内容|AI生成内容|AI生成)$')
    selected = page.get_by_text('笔记含AI合成内容', exact=True)
    if await selected.count() == 1 and await selected.is_visible():
        return
    trigger = page.get_by_text('添加内容类型声明', exact=False).first
    await trigger.click()
    option = page.get_by_text(pattern).last
    await option.wait_for(state='visible')
    await option.click()
    confirms = page.get_by_role('button', name='确认', exact=True)
    if await confirms.count() == 1 and await confirms.is_visible():
        await confirms.click()
    await trigger.wait_for(state='hidden', timeout=8000)
    label = page.get_by_text(pattern)
    if not await label.count() or not await label.first.is_visible():
        raise AdapterError('ai_declaration_not_confirmed')


async def verify_form(page, job):
    copy = job['publications'][PLATFORM]['copy']
    title = page.locator('input[placeholder*="填写标题"]').first
    if await title.input_value() != copy['title']:
        raise AdapterError('title_readback_mismatch')
    editor = page.locator('[contenteditable="true"]').filter(visible=True)
    if await editor.count() != 1 or (await editor.inner_text()).strip() != copy['body'].strip():
        raise AdapterError('body_readback_mismatch')
    # The site exposes the current count as 图片编辑 n/m. Do not infer success
    # merely because the title input appeared (the upstream default).
    text = await page.locator('body').inner_text()
    counts = re.findall(r'(?:图片编辑|编辑图片)\s*[（(]?\s*(\d+)\s*/\s*(\d+)', text)
    if not counts or any(int(n) != job['page_count'] for n, _ in counts):
        raise AdapterError('all_uploaded_image_count_not_confirmed')
    if re.search(r'上传失败|上传中|上传错误', text):
        raise AdapterError('image_upload_not_complete')


async def published_candidates(page, job):
    await page.goto(HOME_URL, wait_until='domcontentloaded', timeout=45000)
    await page.get_by_text('笔记管理', exact=True).click()
    await page.get_by_text('已发布', exact=True).click()
    await page.locator('.note-card').first.wait_for(timeout=20000)
    title = job['publications'][PLATFORM]['copy']['title']
    cards = page.locator('.note-card').filter(has=page.get_by_text(title, exact=True))
    results = []
    for index in range(await cards.count()):
        card = cards.nth(index)
        # Only rendered public IDs/links and UI text. No framework internals,
        # request bodies, authorization headers or hidden application storage.
        ids = await card.evaluate('''el => {
            const ids = new Set();
            for (const node of [el, ...el.querySelectorAll('[data-note-id],[note-id],a[href]')]) {
                for (const key of ['data-note-id','note-id']) {
                    const value = node.getAttribute(key);
                    if (/^[a-f0-9]{24}$/i.test(value || '')) ids.add(value);
                }
                if (node.tagName === 'A') {
                    try { const u = new URL(node.href); const m = u.pathname.match(/^\\/(?:explore|discovery\\/item)\\/([a-f0-9]{24})$/i);
                        if (m && /(^|\\.)xiaohongshu\\.com$/.test(u.hostname)) ids.add(m[1]); } catch {}
                }
            }
            return [...ids];
        }''')
        results.append({'title': title, 'public_note_ids': ids,
                        'visible_card_text': (await card.inner_text())[:700]})
    return results


PRIMARY_SAFE_FAILURE = 'social_auto_upload:confirmed_pre_submit:'


def fallback_preflight(config, job):
    """Only a durable, positively pre-submit primary failure permits fallback."""
    check = preflight(config, job)
    publication = job['publications'].get(PLATFORM, {})
    evidence = publication.get('last_error') or ''
    allowed = (check.get('ready') is True and publication.get('status') == 'pending'
               and publication.get('attempt_count', 0) > 0
               and evidence.startswith(PRIMARY_SAFE_FAILURE))
    status = 'fallback_ready' if allowed else ('fallback_blocked' if check['status'] == 'ready' else check['status'])
    return dict(check, status=status, fallback_allowed=allowed, submit_attempted=False if allowed else None,
                fallback_evidence=evidence if evidence.startswith(PRIMARY_SAFE_FAILURE) else None,
                legacy_claim_required=allowed,
                fallback_reason='confirmed_pre_submit_failure_and_fresh_gate' if allowed
                else 'published_uncertain_not_ready_or_no_safe_primary_failure_evidence')


def claim_legacy(config, job):
    admission = fallback_preflight(config, job)
    if not admission['fallback_allowed']:
        return admission
    state = State(config['storage']['state_db'], publish_not_before=config['schedule']['publish_at'],
                  platforms=config['publishing']['platforms'])
    claim = state.claim_publish(job['job_id'], PLATFORM)
    if claim is None:
        return {'status': 'skipped_already_published', 'fallback_allowed': False,
                'remote_side_effects': False}
    try:
        frozen = state.get_job(job['job_id'])
        checked = preflight(config, frozen, owned_claim=True)
        if not checked['ready']:
            raise AdapterError('legacy_frozen_package_preflight_failed')
        return {'status': 'legacy_claimed', 'job_id': frozen['job_id'],
                'artwork_id': frozen['artwork_id'], 'fallback_allowed': True,
                'submit_attempted': False, 'remote_side_effects': False,
                'state_modified': True, 'attempt_count': claim['attempt_count'],
                'idempotency_key': claim['idempotency_key'],
                'account': config['publishing'][PLATFORM]['account'],
                'account_id': str(config['publishing'][PLATFORM]['account_id']),
                'images': [{'page_index': p['page_index'], 'path': p['cos']['path'],
                            'sha256': p['cos']['sha256']} for p in frozen['pages']],
                'copy': frozen['publications'][PLATFORM]['copy'],
                'requirements': ['verify_live_account', 'all_images_in_order',
                    'recheck_hashes_and_quality_before_click', 'verify_copy_ai_label_and_credit',
                    'click_publish_at_most_once', 'unknown_requires_reconciliation',
                    'verify_real_published_receipt_before_completion']}
    except Exception:
        # This command has no browser and cannot have submitted. Do not retain
        # primary admission evidence as though an unsuccessful legacy claim
        # were a new primary failure.
        state.fail_publish(job['job_id'], PLATFORM, reason='legacy_claim_preflight_failed',
                           definitely_not_published=True)
        raise


async def publish(config, job):
    audit = {'upload_attempt_count': 0, 'upload_completed_count': 0,
             'all_uploaded_images_verified': False,
             'publish_click_attempt_count': 0, 'last_stage': 'preflight'}
    check = preflight(config, job)
    if check['status'] != 'ready':
        return dict(check, fallback_allowed=False, submit_attempted=None, audit=audit)
    state = State(config['storage']['state_db'], publish_not_before=config['schedule']['publish_at'],
                  platforms=config['publishing']['platforms'])
    # Record a claim before loading the publisher or browser. Even an early
    # failure now has durable, attributable evidence for fallback admission.
    claim = state.claim_publish(job['job_id'], PLATFORM)
    if claim is None:
        return {'status': 'skipped_already_published', 'remote_side_effects': False,
                'fallback_allowed': False, 'submit_attempted': False, 'audit': audit}
    submitted = False
    try:
        job = state.get_job(job['job_id'])
        frozen_check = preflight(config, job, owned_claim=True)
        if not frozen_check['ready']:
            raise AdapterError('frozen_package_preflight_failed')
        Note = upstream_class()

        class ExistingProfileNote(Note):
            async def validate_base_args(self):
                if self.publish_strategy != 'immediate':
                    raise AdapterError('scheduler_owns_publish_time')

        copy = job['publications'][PLATFORM]['copy']
        uploader = ExistingProfileNote([p['cos']['path'] for p in job['pages']],
            copy['body'], [], 0, account_file='', title=copy['title'], debug=False)
        await uploader.validate_upload_args()
        async with browser(config) as page:
            audit['last_stage'] = 'verify_live_account'
            await verify_account(page, config)
            audit['last_stage'] = 'load_publish_page'
            await page.goto(PUBLISH_URL, wait_until='domcontentloaded', timeout=45000)
            upload = page.locator('input[type="file"][accept*="image"]').first
            await upload.wait_for(state='attached')
            audit['last_stage'] = 'upload_images'
            audit['upload_attempt_count'] += 1
            await upload.set_input_files(uploader.image_paths)
            audit['upload_completed_count'] += 1
            await page.locator('input[placeholder*="填写标题"]').first.wait_for(timeout=90000)
            audit['last_stage'] = 'fill_and_verify_copy_and_image_count'
            await uploader.fill_meta(page)
            await verify_form(page, job)
            audit['all_uploaded_images_verified'] = True
            audit['last_stage'] = 'apply_ai_declaration'
            await apply_ai_declaration(page)
            audit['last_stage'] = 'final_quality_and_publish_control'
            readiness = state.publish_ready(job['job_id'])
            if not readiness['ready']:
                raise AdapterError('quality_or_artifacts_changed_after_claim')
            if await page.get_by_role('button', name='发布', exact=True).count() != 1:
                raise AdapterError('unambiguous_publish_button_unavailable')
            # A failed click can still have reached the server: set BEFORE click.
            submitted = True
            audit['last_stage'] = 'click_publish'
            audit['publish_click_attempt_count'] += 1
            await page.get_by_role('button', name='发布', exact=True).click()
            audit['last_stage'] = 'wait_for_publish_success'
            await page.wait_for_url('**/publish/success?**', timeout=30000)
            audit['last_stage'] = 'read_published_candidates'
            candidates = await published_candidates(page, job)
        state.fail_publish(job['job_id'], PLATFORM, reason='submitted_once_verify_exact_published_media_and_receipt')
        return {'status': 'reconciliation_required', 'submitted_once': True,
                'submit_attempted': True, 'fallback_allowed': False,
                'candidates': candidates, 'requires_exact_media_review': True, 'audit': audit}
    except Exception as exc:
        error = str(exc) if isinstance(exc, AdapterError) else type(exc).__name__
        reason = PRIMARY_SAFE_FAILURE + error if not submitted else error
        state.fail_publish(job['job_id'], PLATFORM, reason=reason,
                           definitely_not_published=not submitted)
        return {'status': 'reconciliation_required' if submitted else 'blocked_before_submit',
                'submitted_once': submitted, 'submit_attempted': submitted,
                'fallback_allowed': not submitted, 'legacy_claim_required': not submitted,
                'fallback_evidence': reason if not submitted else None, 'error': error, 'audit': audit}


async def run(args):
    config = read_config(args.config)
    if args.command == 'doctor':
        Note = upstream_class()
        return {'status': 'installed', 'repository': 'https://github.com/dreammis/social-auto-upload',
                'commit': COMMIT, 'version': importlib.metadata.version('social-auto-upload'),
                'patchright_version': importlib.metadata.version('patchright'),
                'upstream_class': Note.__module__ + '.' + Note.__name__, 'multi_image_note': True,
                'browser_executable': config['publishing']['browser_executable'],
                'native_cookie_cli_allowed': False}
    if args.command == 'check-login':
        upstream_class()
        async with browser(config) as page:
            return await verify_account(page, config)
    job = read_job(config, args.job_id)
    if args.command == 'dry-run':
        return preflight(config, job)
    if args.command == 'publish':
        return await publish(config, job)
    if args.command == 'fallback-preflight':
        return fallback_preflight(config, job)
    if args.command == 'claim-legacy':
        return claim_legacy(config, job)
    if args.command == 'reconcile':
        upstream_class()
        async with browser(config) as page:
            await verify_account(page, config)
            return {'status': 'reconciliation_review_required', 'job_id': job['job_id'],
                    'candidates': await published_candidates(page, job),
                    'state_modified': False, 'publishes': 0,
                    'requires': 'verify exact media/account/title/time, then record real receipt or State.complete_publish_from_ui'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(ROOT / 'workflow.json'))
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('doctor', 'check-login', 'dry-run', 'publish', 'reconcile', 'fallback-preflight', 'claim-legacy'):
        cmd = sub.add_parser(name)
        if name in ('dry-run', 'publish', 'reconcile', 'fallback-preflight', 'claim-legacy'):
            cmd.add_argument('--job-id', required=True, help='Exact job in the intended persisted run; no latest-job fallback')
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        result = {'status': 'blocked', 'error': str(exc) if isinstance(exc, AdapterError) else type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('status') in ('installed', 'account_verified', 'ready', 'skipped_already_published', 'fallback_ready', 'legacy_claimed') else 2


if __name__ == '__main__':
    raise SystemExit(main())
