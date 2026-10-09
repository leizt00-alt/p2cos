#!/usr/bin/env python3
"""Durable per-trigger runs. No network, browser, credential, or image generation calls."""
from __future__ import annotations

import argparse
from datetime import datetime, time
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

DEFAULT_CONFIG = Path(__file__).with_name('workflow.json')
ANCHOR_DAY = datetime(2026, 10, 9).date()


def current_day():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()


def validate_day(day):
    if datetime.strptime(day, '%Y-%m-%d').date().isoformat() != day:
        raise ValueError('Run day must be YYYY-MM-DD')
    return day


def ranking_for_day(day):
    return 'month' if (datetime.strptime(validate_day(day), '%Y-%m-%d').date() - ANCHOR_DAY).days % 2 == 0 else 'day'


def migrate_runs(db):
    """Remove the old daily uniqueness constraint, preserving all historic cells.

    Must be called outside an active transaction. Rebuilds only jobs and adds
    run metadata tables; old publish_days rows remain unchanged as history.
    """
    if db.in_transaction:
        raise RuntimeError('Run schema migration requires a connection outside a transaction')
    foreign_keys = db.execute('PRAGMA foreign_keys').fetchone()[0]
    db.execute('PRAGMA foreign_keys=OFF')
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS workflow_runs (
            run_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('manual','scheduled','legacy')),
            day TEXT NOT NULL,
            ranking_mode TEXT NOT NULL CHECK(ranking_mode IN ('month','day')),
            allow_immediate_publish INTEGER NOT NULL DEFAULT 0 CHECK(allow_immediate_publish IN (0,1)),
            authorization TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''')
        columns = [row[1] for row in db.execute('PRAGMA table_info(jobs)')]
        schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()[0]
        rebuilt = 'run_id' not in columns or bool(re.search(r'\bday\s+TEXT\s+NOT\s+NULL\s+UNIQUE', schema, re.I))
        if rebuilt:
            legacy = db.execute('SELECT job_id,day FROM jobs').fetchall()
            legacy_modes = {}
            if 'metadata' in columns:
                for job_id, encoded in db.execute('SELECT job_id,metadata FROM jobs'):
                    metadata = json.loads(encoded)
                    mode = (metadata.get('selection_decision') or {}).get('source_mode') or metadata.get('source_mode')
                    if mode in {'month', 'day'}:
                        legacy_modes[job_id] = mode
            for row in legacy:
                # The pre-run workflow supported day ranking only. Never label
                # a historical job using the newly introduced alternating date.
                mode = legacy_modes.get(row[0], 'day')
                db.execute('INSERT OR IGNORE INTO workflow_runs(run_id,kind,day,ranking_mode,authorization) VALUES (?,?,?,?,?)',
                           ('legacy:' + row[0], 'legacy', row[1], mode, 'preserved_pre_run_history'))
            schema = re.sub(r'^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`\[]?jobs["`\]]?', 'CREATE TABLE jobs_run_migration', schema, count=1, flags=re.I)
            schema = re.sub(r'(\bday\s+TEXT\s+NOT\s+NULL)\s+UNIQUE', r'\1', schema, flags=re.I)
            if 'run_id' not in columns:
                schema = schema.rstrip().rstrip(';').rstrip()
                schema = schema[:-1] + ', run_id TEXT NOT NULL UNIQUE REFERENCES workflow_runs(run_id))'
            db.execute(schema)
            names = ','.join('"' + name.replace('"', '""') + '"' for name in columns)
            if 'run_id' not in columns:
                db.execute(f"INSERT INTO jobs_run_migration({names},run_id) SELECT {names},'legacy:'||job_id FROM jobs")
            else:
                db.execute(f'INSERT INTO jobs_run_migration({names}) SELECT {names} FROM jobs')
            db.execute('DROP TABLE jobs')
            db.execute('ALTER TABLE jobs_run_migration RENAME TO jobs')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS jobs_run_id_unique ON jobs(run_id)')
        db.execute('''CREATE TABLE IF NOT EXISTS run_publish_days (
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            day TEXT NOT NULL,
            job_id TEXT NOT NULL REFERENCES jobs(job_id),
            PRIMARY KEY(run_id,day)
        )''')
        violations = db.execute('PRAGMA foreign_key_check').fetchall()
        if violations:
            raise RuntimeError('Run schema migration failed foreign-key validation')
        db.commit()
        return {'jobs_rebuilt': rebuilt, 'daily_publication_quota': False, 'historic_cells_preserved': True}
    except BaseException:
        db.rollback()
        raise
    finally:
        db.execute('PRAGMA foreign_keys=' + str(int(bool(foreign_keys))))


def ensure_run(db, *, run_id, kind, day, ranking_mode=None, allow_immediate_publish=False, authorization=None):
    validate_day(day)
    ranking_mode = ranking_mode or ranking_for_day(day)
    if kind not in {'manual', 'scheduled', 'legacy'} or ranking_mode not in {'month', 'day'}:
        raise ValueError('Invalid run kind or ranking mode')
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError('A stable run_id is required')
    if not run_id.startswith(kind + ':'):
        raise ValueError('Run ID prefix must match its persisted kind')
    if allow_immediate_publish and (kind != 'manual' or authorization != 'explicit_user_request'):
        raise ValueError('Immediate publication requires an explicitly authorized manual run')
    if kind == 'scheduled' and run_id != 'scheduled:' + day:
        raise ValueError('Scheduled preparation and publication must share scheduled:YYYY-MM-DD')
    expected = (kind, day, ranking_mode, int(allow_immediate_publish), authorization)
    row = db.execute('SELECT kind,day,ranking_mode,allow_immediate_publish,authorization FROM workflow_runs WHERE run_id=?', (run_id,)).fetchone()
    if row is not None and tuple(row) != expected:
        raise ValueError('Run context is immutable; resume with its original persisted context')
    db.execute('INSERT OR IGNORE INTO workflow_runs(run_id,kind,day,ranking_mode,allow_immediate_publish,authorization) VALUES (?,?,?,?,?,?)', (run_id,) + expected)
    return {'run_id': run_id, 'kind': kind, 'day': day, 'ranking_mode': ranking_mode,
            'allow_immediate_publish': bool(allow_immediate_publish), 'authorization': authorization}


def reserve_for_run(state, candidates, run_id, day=None):
    """Reserve one never-used artwork for this run; never borrow another run."""
    from p2cos_state import StateError
    with state._transaction() as db:
        run = db.execute('SELECT * FROM workflow_runs WHERE run_id=?', (run_id,)).fetchone()
        if run is None:
            if run_id.startswith('scheduled:'):
                scheduled_day = validate_day(run_id.split(':', 1)[1])
                ensure_run(db, run_id=run_id, kind='scheduled', day=scheduled_day)
                run = db.execute('SELECT * FROM workflow_runs WHERE run_id=?', (run_id,)).fetchone()
            else:
                raise StateError('Create an explicit manual run before reserving its artwork')
        if day is not None and day != run['day']:
            raise StateError('Reservation day must match the persisted run context')
        existing = db.execute('SELECT job_id FROM jobs WHERE run_id=?', (run_id,)).fetchone()
        if existing:
            return state._get_job(db, existing[0])
        for candidate in candidates:
            decision = candidate.get('selection_decision') or {}
            if decision.get('source_mode') and decision['source_mode'] != run['ranking_mode']:
                raise StateError('Candidate ranking source must match the persisted run context')
            artwork_id = state._id(candidate['id'])
            if db.execute('SELECT 1 FROM history WHERE artwork_id=? UNION SELECT 1 FROM jobs WHERE artwork_id=?', (artwork_id, artwork_id)).fetchone():
                continue
            count = candidate.get('page_count')
            if type(count) is not int or count < 1:
                raise ValueError('Candidate requires a positive integer page_count')
            job_id = uuid4().hex
            metadata = {key: candidate[key] for key in ('id','title','artist','url','rank','candidate_rank','page_count','reason','selection_decision') if key in candidate}
            metadata['required_platforms'] = list(state.platforms)
            metadata['run_context'] = {key: run[key] for key in ('run_id','kind','day','ranking_mode','allow_immediate_publish','authorization')}
            db.execute('INSERT INTO jobs(job_id,day,artwork_id,page_count,metadata,run_id) VALUES (?,?,?,?,?,?)',
                       (job_id, run['day'], artwork_id, count, json.dumps(metadata, ensure_ascii=False), run_id))
            db.executemany('INSERT INTO pages(job_id,page_index) VALUES (?,?)', ((job_id,i) for i in range(count)))
            db.executemany('INSERT INTO publications(job_id,platform,idempotency_key) VALUES (?,?,?)',
                           ((job_id, platform, f'p2cos:{artwork_id}:{platform}') for platform in state.platforms))
            return state._get_job(db, job_id)
    return None


def claim_run_publication(db, job_id, day):
    validate_day(day)
    row = db.execute('SELECT run_id FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    if row is None or not row[0]:
        raise RuntimeError('Publication requires a persisted run')
    db.execute('INSERT OR IGNORE INTO run_publish_days(run_id,day,job_id) VALUES (?,?,?)', (row[0], day, job_id))


def publication_not_before(db, job_id, default_time):
    row = db.execute('SELECT r.kind,r.allow_immediate_publish,r.authorization FROM jobs j JOIN workflow_runs r ON r.run_id=j.run_id WHERE j.job_id=?', (job_id,)).fetchone()
    if row and row[0] == 'manual' and row[1] == 1 and row[2] == 'explicit_user_request':
        return time(0, 0)
    return default_time


def run_check(db, run_id):
    row = db.execute('SELECT * FROM workflow_runs WHERE run_id=?', (run_id,)).fetchone()
    if row is None:
        return {'action': 'unknown_run', 'run_id': run_id, 'read_only': True}
    context = dict(row)
    job = db.execute('SELECT job_id,artwork_id,day,status,page_count FROM jobs WHERE run_id=?', (run_id,)).fetchone()
    result = {'run': context, 'run_id': run_id, 'read_only': True, 'daily_publication_quota': False,
              'job': dict(job) if job else None, 'action': 'select_for_run'}
    if job:
        publications = [dict(p) for p in db.execute('SELECT platform,status,attempt_count,receipt_id,receipt_url FROM publications WHERE job_id=?', (job['job_id'],))]
        result['job']['publications'] = publications
        if job['status'] == 'complete' and publications and all(p['status']=='published' and p['receipt_id'] for p in publications):
            result['action'] = 'skip_run_already_published'
        elif job['status'] == 'complete':
            result['action'] = 'blocked_inconsistent_run'
        else:
            result['action'] = 'resume_run'
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=DEFAULT_CONFIG)
    subs=parser.add_subparsers(dest='command',required=True)
    manual=subs.add_parser('create-manual')
    manual.add_argument('--day',default=None)
    manual.add_argument('--ranking-mode',choices=('month','day'))
    manual.add_argument('--publish-immediately',action='store_true')
    manual.add_argument('--run-id',help='Reuse only when retrying the same explicit user trigger')
    scheduled=subs.add_parser('ensure-scheduled')
    scheduled.add_argument('--day',default=None)
    check=subs.add_parser('check')
    check.add_argument('--run-id',required=True)
    args=parser.parse_args(argv)
    config=json.loads(args.config.read_text())
    path=Path(config['storage']['state_db'])
    if args.command=='check':
        with sqlite3.connect(path.resolve(strict=True).as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            result=run_check(db,args.run_id)
    else:
        from p2cos_state import State
        from pixiv_pipeline import resolve_ranking_mode
        day=validate_day(args.day or current_day())
        kind='manual' if args.command=='create-manual' else 'scheduled'
        override=args.ranking_mode if kind=='manual' else None
        ranking_mode=(resolve_ranking_mode(config,day,override) if 'selection' in config
                      else override or ranking_for_day(day))
        state=State(path,publish_not_before=config['schedule']['publish_at'],platforms=config['publishing']['platforms'])
        run_id=(args.run_id or 'manual:'+uuid4().hex) if kind=='manual' else 'scheduled:'+day
        with state._transaction() as db:
            result=ensure_run(db,run_id=run_id,kind=kind,day=day,
                ranking_mode=ranking_mode,
                allow_immediate_publish=args.publish_immediately if kind=='manual' else False,
                authorization='explicit_user_request' if kind=='manual' else None)
        result['action']='run_created_or_resumed'
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['action'] not in {'unknown_run','blocked_inconsistent_run'} else 2


if __name__=='__main__':
    raise SystemExit(main())
