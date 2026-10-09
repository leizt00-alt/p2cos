"""Durable, credential-free state for the p2cos daily workflow.

This module performs no network requests and never reads environment variables.
Adapters must provide ranked/eligible candidates, real image files, and verified
publication receipts. A reservation is not a successful download or publication.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable, Iterable
from uuid import uuid4
from zoneinfo import ZoneInfo

from p2cos_quality import (
    agent_id, digest, nonempty, page_integrity_reasons, page_quality_reasons,
    quality_score, unresolved_checks, validate_features, validate_review, verdict_reasons,
)
from p2cos_runs import migrate_runs, reserve_for_run, claim_run_publication, publication_not_before
from p2cos_recovery import CorrectionRecoveryMixin, migrate_recovery, migrate_correction_slots


PLATFORMS = ("douyin", "xiaohongshu")


class StateError(RuntimeError):
    pass


class NeedsReconciliation(StateError):
    """A previous publish may have succeeded; query its receipt before retrying."""


def shanghai_day() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def inspect_image(path: str | Path) -> dict:
    """Verify the container, fully decode every frame, and hash actual bytes."""
    from PIL import Image, ImageFile

    if ImageFile.LOAD_TRUNCATED_IMAGES:
        raise StateError("Strict image validation requires LOAD_TRUNCATED_IMAGES=False")
    path = Path(path).resolve(strict=True)
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        details = {"format": image.format, "width": image.width,
                   "height": image.height, "mode": image.mode,
                   "frames": getattr(image, "n_frames", 1)}
        for frame in range(details["frames"]):
            image.seek(frame)
            image.load()
            image.tobytes()
    details.update(path=str(path), bytes=path.stat().st_size,
                   sha256=hashlib.sha256(path.read_bytes()).hexdigest(), decoded=True)
    return details


class State(CorrectionRecoveryMixin):
    def __init__(self, db_path: str | Path, *, publish_not_before: str = "10:00",
                 clock: Callable[[], datetime] | None = None,
                 platforms: Iterable[str] = PLATFORMS):
        if not isinstance(publish_not_before, str) or not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", publish_not_before):
            raise ValueError("publish_not_before must be a valid HH:MM time")
        self.publish_not_before = time.fromisoformat(publish_not_before)
        self._clock = clock
        if isinstance(platforms, str):
            raise ValueError("platforms must be a non-empty sequence of supported platform names")
        self.platforms = tuple(platforms)
        if (not self.platforms or len(set(self.platforms)) != len(self.platforms)
                or any(platform not in PLATFORMS for platform in self.platforms)):
            raise ValueError("platforms must contain unique supported platform names")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS history (
                    artwork_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    day TEXT NOT NULL UNIQUE,
                    artwork_id TEXT NOT NULL UNIQUE,
                    page_count INTEGER NOT NULL CHECK(page_count > 0),
                    metadata TEXT NOT NULL,
                    research TEXT,
                    status TEXT NOT NULL DEFAULT 'reserved',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS pages (
                    job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    page_index INTEGER NOT NULL CHECK(page_index >= 0),
                    raw TEXT,
                    cos TEXT,
                    qa_passed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(job_id, page_index)
                );
                CREATE TABLE IF NOT EXISTS publications (
                    job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    platform TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    idempotency_key TEXT NOT NULL UNIQUE,
                    copy TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    receipt_id TEXT,
                    receipt_url TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(job_id, platform)
                );
                CREATE TABLE IF NOT EXISTS publish_days (
                    day TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id)
                );
                CREATE TABLE IF NOT EXISTS publication_evidence (
                    job_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    verification_kind TEXT NOT NULL,
                    details TEXT NOT NULL,
                    PRIMARY KEY(job_id, platform),
                    FOREIGN KEY(job_id, platform) REFERENCES publications(job_id, platform)
                );
            """)
        # Additive migration only: old publication receipts, status and boolean
        # QA records are retained as history, never promoted to structured QA.
        with self._transaction() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(pages)")}
            for name, definition in (
                ("features", "TEXT"), ("generation", "TEXT"), ("quality_review", "TEXT"),
                ("correction_count", "INTEGER NOT NULL DEFAULT 0"),
                ("quality_blocked", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_correction", "TEXT"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE pages ADD COLUMN {name} {definition}")
            db.execute("""CREATE TABLE IF NOT EXISTS quality_corrections (
                attempt_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                page_index INTEGER NOT NULL,
                attempt_number INTEGER NOT NULL CHECK(attempt_number BETWEEN 1 AND 2),
                generator_agent_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(job_id,page_index,attempt_number),
                FOREIGN KEY(job_id,page_index) REFERENCES pages(job_id,page_index)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS quality_agents (
                job_id TEXT NOT NULL REFERENCES jobs(job_id),
                agent_id TEXT NOT NULL,
                role TEXT NOT NULL,
                review_context TEXT,
                PRIMARY KEY(job_id,agent_id)
            )""")
            correction_columns = {row[1] for row in db.execute("PRAGMA table_info(quality_corrections)")}
            for name in ("optimization", "failure_details"):
                if name not in correction_columns:
                    db.execute(f"ALTER TABLE quality_corrections ADD COLUMN {name} TEXT")
            migrate_recovery(db)
            db.execute("""CREATE TABLE IF NOT EXISTS quality_versions (
                job_id TEXT NOT NULL REFERENCES jobs(job_id), page_index INTEGER NOT NULL,
                generation_sha256 TEXT NOT NULL, snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(job_id,page_index,generation_sha256)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS quality_version_reviews (
                review_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                page_index INTEGER NOT NULL, generation_sha256 TEXT NOT NULL,
                review_sha256 TEXT NOT NULL, report TEXT NOT NULL, context TEXT NOT NULL,
                score INTEGER NOT NULL, visual_passed INTEGER NOT NULL,
                provisional INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(job_id,page_index,generation_sha256,review_sha256,provisional),
                FOREIGN KEY(job_id,page_index,generation_sha256)
                    REFERENCES quality_versions(job_id,page_index,generation_sha256)
            )""")
            review_columns = {row[1] for row in db.execute("PRAGMA table_info(quality_version_reviews)")}
            if "provisional" not in review_columns:
                db.execute("ALTER TABLE quality_version_reviews ADD COLUMN provisional INTEGER NOT NULL DEFAULT 0")
            db.execute("""CREATE TABLE IF NOT EXISTS quality_selections (
                selection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(job_id), report TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS quality_selection_current (
                job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
                selection_id INTEGER NOT NULL REFERENCES quality_selections(selection_id)
            )""")
        with self._connect() as db:
            migrate_runs(db)
            migrate_correction_slots(db)

    def _now(self) -> datetime:
        now = self._clock() if self._clock is not None else datetime.now(ZoneInfo("Asia/Shanghai"))
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise StateError("The publishing clock must return a timezone-aware datetime")
        return now.astimezone(ZoneInfo("Asia/Shanghai"))

    def _connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def _transaction(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _id(value) -> str:
        result = str(value)
        if not re.fullmatch(r"[1-9][0-9]*", result):
            raise ValueError("Artwork ID must be a positive decimal integer")
        return result

    def bootstrap_history(
        self,
        selection_path: str | Path = "/workspace/output/selection.json",
        raw_roots: Iterable[str | Path] = ("/workspace/output/originals", "/workspace/output/artworks"),
        storage_path: str | Path = "/workspace/work/pixiv_storage.json",
        known_archived: Iterable[str] = (),
    ) -> dict:
        """Import downloaded history only; never treat historical files as new jobs.

        Selection rows count as download evidence only if they contain file paths.
        Drive metadata and known archive IDs are conservative historical evidence,
        even if the corresponding image has since disappeared from local storage.
        """
        seen: dict[str, str] = {}
        selection = Path(selection_path)
        if selection.is_file():
            data = json.loads(selection.read_text())
            for row in data if isinstance(data, list) else data.get("selected", []):
                if row.get("files"):
                    seen[self._id(row["id"])] = "selection_metadata"
        for root in raw_roots:
            root = Path(root)
            if root.is_dir():
                for path in root.rglob("*"):
                    match = re.fullmatch(r"([1-9][0-9]*)_p[0-9]+(?:_raw)?\.(?:jpg|jpeg|png|webp|gif)", path.name, re.I)
                    if path.is_file() and match:
                        seen[match.group(1)] = "existing_raw_file"
        storage = Path(storage_path)
        if storage.is_file():
            data = json.loads(storage.read_text())
            for artwork_id in data.get("google_drive", {}).get("artworks", {}):
                seen[self._id(artwork_id)] = "drive_archive_metadata"
        for artwork_id in known_archived:
            seen[self._id(artwork_id)] = "known_drive_archive"
        with self._transaction() as db:
            before = db.total_changes
            db.executemany("INSERT OR IGNORE INTO history(artwork_id, source) VALUES (?,?)", seen.items())
            added = db.total_changes - before
        return {"added": added, "artwork_ids": sorted(seen, key=int)}

    def downloaded_ids(self) -> set[str]:
        with self._connect() as db:
            return {r[0] for r in db.execute("SELECT artwork_id FROM history")}

    def excluded_ids(self) -> set[str]:
        with self._connect() as db:
            return {r[0] for r in db.execute("SELECT artwork_id FROM history UNION SELECT artwork_id FROM jobs")}

    def reserve_daily(self, candidates: Iterable[dict], day: str | None = None) -> dict | None:
        """Compatibility entry point for the scheduled run on this Shanghai date.

        Other runs, including manual runs on the same date, have their own slot.
        """
        day = day or shanghai_day()
        return self.reserve_run(candidates, run_id=f"scheduled:{day}", day=day)

    def reserve_run(self, candidates: Iterable[dict], *, run_id: str, day: str | None = None) -> dict | None:
        """Reserve or resume exactly one work for an explicit, durable run."""
        return reserve_for_run(self, candidates, run_id, day)

    @staticmethod
    def _get_job(db, job_id):
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        result["research"] = json.loads(result["research"]) if result["research"] else None
        result["pages"] = []
        for page in db.execute("SELECT * FROM pages WHERE job_id=? ORDER BY page_index", (job_id,)):
            page = dict(page)
            # Read-only callers can inspect an unmigrated legacy database.
            for key in ("features", "generation", "quality_review", "pending_correction"):
                page.setdefault(key, None)
            page.setdefault("correction_count", 0)
            page.setdefault("quality_blocked", 0)
            for kind in ("raw", "cos", "features", "generation", "quality_review", "pending_correction"):
                page[kind] = json.loads(page[kind]) if page[kind] else None
            page["qa_passed"] = bool(page["qa_passed"])
            page["quality_blocked"] = bool(page["quality_blocked"])
            page["features_sha256"] = digest(page["features"]) if page["features"] else None
            page["generation_sha256"] = digest(page["generation"]) if page["generation"] else None
            result["pages"].append(page)
        result["publications"] = {}
        for publication in db.execute("SELECT * FROM publications WHERE job_id=?", (job_id,)):
            publication = dict(publication)
            publication["copy"] = json.loads(publication["copy"]) if publication["copy"] else None
            evidence = db.execute("SELECT verification_kind,details FROM publication_evidence WHERE job_id=? AND platform=?", (job_id, publication["platform"])).fetchone()
            publication["verification_kind"] = evidence[0] if evidence else ("remote_receipt" if publication["status"] == "published" else None)
            publication["receipt_evidence"] = json.loads(evidence[1]) if evidence else None
            publication["post_id"] = None if evidence else publication["receipt_id"]
            publication["post_url"] = None if evidence else publication["receipt_url"]
            result["publications"][publication["platform"]] = publication
        # Publication rows are the durable per-job assignment, including older
        # jobs that predate required_platforms metadata. A new State configuration
        # never silently expands or shrinks an already reserved job's targets.
        result["required_platforms"] = list(result["publications"])
        selection_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='quality_selection_current'").fetchone()
        selection = db.execute("SELECT report FROM quality_selections JOIN quality_selection_current USING(selection_id) WHERE quality_selection_current.job_id=?", (job_id,)).fetchone() if selection_table else None
        result["quality_selection"] = json.loads(selection[0]) if selection else None
        return result

    def get_job(self, job_id) -> dict:
        with self._connect() as db:
            return self._get_job(db, job_id)

    def latest_job(self) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT job_id FROM jobs ORDER BY day DESC LIMIT 1").fetchone()
            return self._get_job(db, row[0]) if row else None

    @staticmethod
    def _assert_mutable(db, job_id):
        if db.execute("SELECT 1 FROM publications WHERE job_id=? AND attempt_count>0", (job_id,)).fetchone():
            raise StateError("Publication has started; the artwork package is frozen")

    @staticmethod
    def _page(db, job_id, page_index):
        page = db.execute("SELECT * FROM pages WHERE job_id=? AND page_index=?", (job_id, page_index)).fetchone()
        if page is None:
            raise StateError("Page is outside the artwork's declared page_count")
        return page

    @staticmethod
    def _quality_input(function, *args):
        try:
            return function(*args)
        except (ValueError, TypeError, KeyError) as error:
            raise StateError(str(error)) from error

    @staticmethod
    def _invalidate_reviews(db, job_id):
        # A correction on one page also invalidates every cross-page identity
        # decision. Persisted old reports cannot approve a newly edited image.
        db.execute("UPDATE pages SET quality_review=NULL,qa_passed=0 WHERE job_id=?", (job_id,))
        db.execute("DELETE FROM quality_selection_current WHERE job_id=?", (job_id,))

    @staticmethod
    def _assign_agent(db, job_id, identifier, role, *, fresh=False, review_context=None):
        existing = db.execute("SELECT role,review_context FROM quality_agents WHERE job_id=? AND agent_id=?", (job_id, identifier)).fetchone()
        if existing and (fresh or existing["role"] != role or existing["review_context"] != review_context):
            raise StateError("A different fresh agent is required; agent roles and review versions cannot be reused")
        db.execute("INSERT OR IGNORE INTO quality_agents(job_id,agent_id,role,review_context) VALUES (?,?,?,?)",
                   (job_id, identifier, role, review_context))

    def _record_image(self, job_id, page_index, kind, path, qa_passed=False,
                      generator_agent_id=None, correction_attempt_id=None, generation_trace=None) -> dict:
        details = inspect_image(path)
        with self._transaction() as db:
            self._assert_mutable(db, job_id)
            page = self._page(db, job_id, page_index)
            if kind == "cos" and not page["raw"]:
                raise StateError("A decoded raw image is required before its Cos image")
            if kind == "raw":
                if page["cos"] and json.loads(page["raw"])["sha256"] != details["sha256"]:
                    raise StateError("Cannot replace the raw reference after generating its Cos image")
                if page["raw"] and json.loads(page["raw"])["sha256"] != details["sha256"]:
                    db.execute("UPDATE pages SET features=NULL,generation=NULL WHERE job_id=? AND page_index=?", (job_id, page_index))
                    self._invalidate_reviews(db, job_id)
                db.execute("UPDATE pages SET raw=? WHERE job_id=? AND page_index=?", (json.dumps(details), job_id, page_index))
                db.execute("INSERT OR IGNORE INTO history(artwork_id,source) SELECT artwork_id,'decoded_download' FROM jobs WHERE job_id=?", (job_id,))
            else:
                raw = json.loads(page["raw"])
                if details["path"] == raw["path"]:
                    raise StateError("Raw and Cos must be separate files")
                if inspect_image(raw["path"])["sha256"] != raw["sha256"]:
                    raise StateError("Raw reference changed after validation")
                previous = json.loads(page["cos"]) if page["cos"] else None
                pending = json.loads(page["pending_correction"]) if page["pending_correction"] else None
                changed = previous is not None and previous["sha256"] != details["sha256"]
                if changed and not pending:
                    raise StateError("Reserve a correction attempt before replacing a Cos image")
                if pending and (correction_attempt_id != pending["attempt_id"] or generator_agent_id != pending["generator_agent_id"]):
                    raise StateError("Cos image must match its reserved correction attempt and generator")
                if pending:
                    self._require_saved_correction_result(db, job_id, page_index, pending, details, generation_trace)
                if correction_attempt_id and not pending:
                    raise StateError("Correction attempt is not pending")
                # Compatibility re-registration may invalidate QA but cannot
                # erase provenance or unfreeze a generated feature inventory.
                generation = json.loads(page["generation"]) if page["generation"] else None
                if generator_agent_id is not None:
                    self._quality_input(agent_id, generator_agent_id, "generator_agent_id")
                    if not page["features"]:
                        raise StateError("Record the original feature inventory before generating Cos")
                    features = json.loads(page["features"])
                    self._quality_input(validate_features, features, raw["sha256"])
                    analysts = {json.loads(p[0])["analyst_agent_id"] for p in db.execute("SELECT features FROM pages WHERE job_id=? AND features IS NOT NULL", (job_id,))}
                    if generator_agent_id in analysts:
                        raise StateError("Source analyst and generator must be different fresh agents")
                    if previous and not pending:
                        raise StateError("Reserve a correction attempt before recording new generation provenance")
                    if pending:
                        if not isinstance(generation_trace, dict):
                            raise StateError("A correction requires its actual optimized prompt trace")
                        self._validate_optimized_prompt(db, job_id, page_index, generation_trace.get("actual_prompt"))
                        paths = [json.loads(r[0])["cos"]["path"] for r in db.execute("SELECT snapshot FROM quality_versions WHERE job_id=? AND page_index=?", (job_id, page_index))]
                        if details["path"] in paths:
                            raise StateError("Every generated version must retain a separate immutable output path")
                    self._assign_agent(db, job_id, generator_agent_id, "generator")
                    previous_generation = json.loads(page["generation"]) if page["generation"] else {}
                    generator_history = set(previous_generation.get("generator_history", []))
                    if previous_generation.get("generator_agent_id"):
                        generator_history.add(previous_generation["generator_agent_id"])
                    generator_history.update(p[0] for p in db.execute("SELECT generator_agent_id FROM quality_corrections WHERE job_id=? AND page_index=?", (job_id, page_index)))
                    generator_history.add(generator_agent_id)
                    generation = {"generator_agent_id": generator_agent_id, "generator_history": sorted(generator_history), "raw_sha256": raw["sha256"],
                                  "cos_sha256": details["sha256"], "features_sha256": digest(features),
                                  "correction_attempt": pending["attempt_number"] if pending else 0,
                                  "correction_attempt_id": correction_attempt_id,
                                  "optimization": pending.get("optimization") if pending else None,
                                  "trace": generation_trace}
                    if pending and pending.get("optimization_slot") is not None:
                        generation["optimization_slot"] = pending["optimization_slot"]
                        generation["replacement_of"] = pending.get("replacement_of")
                self._invalidate_reviews(db, job_id)
                db.execute("UPDATE pages SET cos=?,generation=?,qa_passed=0,pending_correction=NULL WHERE job_id=? AND page_index=?",
                           (json.dumps(details), json.dumps(generation) if generation else None, job_id, page_index))
                if pending:
                    db.execute("UPDATE quality_corrections SET status='recorded' WHERE attempt_id=?", (pending["attempt_id"],))
                    self._mark_correction_recorded(db, pending["attempt_id"])
                if generation:
                    self._save_version(db, job_id, page_index)
            self._refresh(db, job_id)
        return details

    def record_raw(self, job_id, page_index, path) -> dict:
        return self._record_image(job_id, page_index, "raw", path)

    def record_cos(self, job_id, page_index, path, *, qa_passed=False,
                   generator_agent_id=None, correction_attempt_id=None, generation_trace=None) -> dict:
        """Record decoded media. Legacy qa_passed is accepted but grants no approval.

        Strict generation provenance requires a prior feature inventory and an
        actual fresh generator agent ID. Corrections must be reserved first.
        """
        return self._record_image(job_id, page_index, "cos", path, qa_passed,
                                  generator_agent_id, correction_attempt_id, generation_trace)

    def record_features(self, job_id, page_index, report: dict) -> dict:
        """Store the source analyst's visible-feature inventory before generation."""
        with self._transaction() as db:
            self._assert_mutable(db, job_id)
            page = self._page(db, job_id, page_index)
            if not page["raw"]:
                raise StateError("A decoded raw image is required before feature analysis")
            raw = json.loads(page["raw"])
            if inspect_image(raw["path"])["sha256"] != raw["sha256"]:
                raise StateError("Raw reference changed after validation")
            self._quality_input(validate_features, report, raw["sha256"])
            generators = set()
            for other in self._get_job(db, job_id)["pages"]:
                if other["generation"]:
                    generators.update(other["generation"].get("generator_history", []))
                    generators.add(other["generation"]["generator_agent_id"])
            generators.update(p[0] for p in db.execute("SELECT generator_agent_id FROM quality_corrections WHERE job_id=?", (job_id,)))
            if report["analyst_agent_id"] in generators:
                raise StateError("Source analyst and generator must be different fresh agents")
            if page["generation"] and json.loads(page["generation"])["features_sha256"] != digest(report):
                raise StateError("Original feature inventory is frozen after generation")
            self._assign_agent(db, job_id, report["analyst_agent_id"], "analyst")
            db.execute("UPDATE pages SET features=? WHERE job_id=? AND page_index=?", (json.dumps(report, ensure_ascii=False), job_id, page_index))
            self._invalidate_reviews(db, job_id)
            self._refresh(db, job_id)
            return {"features_sha256": digest(report)}

    @classmethod
    def _save_version(cls, db, job_id, page_index):
        page = next(p for p in cls._get_job(db, job_id)["pages"] if p["page_index"] == page_index)
        snapshot = {key: page[key] for key in ("page_index", "raw", "cos", "features", "generation")}
        db.execute("INSERT OR IGNORE INTO quality_versions(job_id,page_index,generation_sha256,snapshot) VALUES (?,?,?,?)",
                   (job_id, page_index, digest(page["generation"]), json.dumps(snapshot, ensure_ascii=False)))

    @classmethod
    def _validate_optimized_prompt(cls, db, job_id, page_index, prompt):
        cls._quality_input(nonempty, prompt, "Actual optimized prompt")
        prompts = []
        for row in db.execute("SELECT snapshot FROM quality_versions WHERE job_id=? AND page_index=?", (job_id, page_index)):
            trace = json.loads(row[0])["generation"].get("trace") or {}
            prompts.append(trace.get("actual_prompt", "").strip())
        for row in db.execute("SELECT failure_details FROM quality_corrections WHERE job_id=? AND page_index=? AND failure_details IS NOT NULL", (job_id, page_index)):
            prompts.append(json.loads(row[0])["actual_prompt"].strip())
        if prompt.strip() in prompts:
            raise StateError("Optimize the prompt using the review findings; a previous prompt cannot be retried unchanged")

    def _validate_correction_optimization(self, db, job_id, page_index, page, optimization):
        generation = json.loads(page["generation"]) if page["generation"] else None
        version_hash = digest(generation) if generation else None
        latest = db.execute("SELECT report,review_sha256 FROM quality_version_reviews WHERE job_id=? AND page_index=? AND generation_sha256=? ORDER BY review_id DESC LIMIT 1", (job_id, page_index, version_hash)).fetchone()
        if latest is None:
            raise StateError("An independent review of the current generation is required before optimization")
        review = json.loads(latest["report"])
        targets = unresolved_checks(json.loads(page["features"]), review)
        if not targets:
            raise StateError("The current generation has no unresolved quality checks to optimize")
        if (not isinstance(optimization, dict) or optimization.get("based_on_generation_sha256") != version_hash
                or optimization.get("review_sha256") != latest["review_sha256"]
                or not isinstance(optimization.get("target_checks"), list)
                or set(optimization["target_checks"]) != set(targets)):
            raise StateError("Prompt optimization must bind the current generation, latest review and every unresolved check")
        self._quality_input(nonempty, optimization.get("changes"), "Prompt optimization explanation")

    def begin_correction(self, job_id, page_index, *, generator_agent_id: str, optimization: dict | None = None) -> dict:
        """Reserve an edit BEFORE generation, including attempts that later fail.

        At most two normal optimization slots follow the initial generation.
        One separately authorized unknown replacement can share its original
        slot. Every call retains its own monotonically increasing attempt.
        """
        self._quality_input(agent_id, generator_agent_id, "generator_agent_id")
        with self._transaction() as db:
            self._assert_mutable(db, job_id)
            page = self._page(db, job_id, page_index)
            if not page["cos"] or not page["features"]:
                raise StateError("Corrections require a recorded Cos image and feature inventory")
            analysts = {json.loads(p[0])["analyst_agent_id"] for p in db.execute("SELECT features FROM pages WHERE job_id=? AND features IS NOT NULL", (job_id,))}
            if generator_agent_id in analysts:
                raise StateError("Source analyst and generator must be different fresh agents")
            if page["correction_count"] >= 2:
                raise StateError("Correction limit exceeded; at most two correction attempts are allowed; select the best eligible version")
            if page["pending_correction"]:
                raise StateError("Resolve the pending correction before reserving another attempt")
            self._validate_correction_optimization(db, job_id, page_index, page, optimization)
            self._assign_agent(db, job_id, generator_agent_id, "generator", fresh=True)
            self._invalidate_reviews(db, job_id)
            number = db.execute("SELECT COALESCE(MAX(attempt_number),0)+1 FROM quality_corrections WHERE job_id=? AND page_index=?", (job_id, page_index)).fetchone()[0]
            attempt = {"attempt_id": uuid4().hex, "attempt_number": number,
                       "optimization_slot": page["correction_count"] + 1, "replacement_of": None,
                       "generator_agent_id": generator_agent_id, "optimization": optimization}
            db.execute("INSERT INTO quality_corrections(attempt_id,job_id,page_index,attempt_number,generator_agent_id,optimization,optimization_slot) VALUES (?,?,?,?,?,?,?)",
                       (attempt["attempt_id"], job_id, page_index, number, generator_agent_id, json.dumps(optimization, ensure_ascii=False), attempt["optimization_slot"]))
            self._reserve_correction_recovery(db, attempt["attempt_id"])
            db.execute("UPDATE pages SET correction_count=?,pending_correction=?,quality_blocked=0 WHERE job_id=? AND page_index=?",
                       (attempt["optimization_slot"], json.dumps(attempt), job_id, page_index))
            self._refresh(db, job_id)
        return attempt

    def fail_correction(self, job_id, page_index, *, attempt_id: str, actual_prompt: str,
                        reason: str, generator_agent_id=None, evidence=None):
        """Only a bound, explicit terminal tool receipt can complete a failed retry."""
        return self._fail_correction_with_evidence(job_id, page_index, attempt_id=attempt_id,
            generator_agent_id=generator_agent_id, actual_prompt=actual_prompt,
            reason=reason, evidence=evidence)

    def record_quality_review(self, job_id, page_index, report: dict, *, provisional=False) -> dict:
        """Store independent pass/fail evidence bound to the exact generated version."""
        with self._transaction() as db:
            self._assert_mutable(db, job_id)
            job = self._get_job(db, job_id)
            self._page(db, job_id, page_index)
            page = next(p for p in job["pages"] if p["page_index"] == page_index)
            current_version = page["generation_sha256"]
            requested_version = report.get("generation_sha256")
            if requested_version != current_version:
                saved = db.execute("SELECT snapshot FROM quality_versions WHERE job_id=? AND page_index=? AND generation_sha256=?", (job_id, page_index, requested_version)).fetchone()
                if saved is None:
                    raise StateError("Independent review generation_sha256 does not match any recorded generation")
                page.update(json.loads(saved[0]))
                page["generation_sha256"] = requested_version
                page["features_sha256"] = digest(page["features"])
            if not page["features"] or not page["generation"]:
                raise StateError("Structured source inventory and generation provenance required")
            if page["pending_correction"]:
                raise StateError("Cannot review a pending correction")
            for kind in ("raw", "cos"):
                if inspect_image(page[kind]["path"])["sha256"] != page[kind]["sha256"]:
                    raise StateError(f"{kind} file changed after validation")
            hashes = {str(p["page_index"]): p["cos"]["sha256"] for p in job["pages"] if p["cos"]}
            context_pages = [page] if provisional else job["pages"]
            self._quality_input(validate_review, report, page["features"], page["generation"], hashes, len(context_pages))
            context = digest({"versions": {str(p["page_index"]): digest(p["generation"]) if p["generation"] else None for p in context_pages}, "provisional": bool(provisional)})
            self._assign_agent(db, job_id, report["reviewer_agent_id"], "reviewer", review_context=context)
            page["quality_review"] = report
            reasons = page_quality_reasons(page, context_pages)
            score = quality_score(page["features"], report)
            visual_passed = not verdict_reasons(page["features"], report, len(context_pages))
            if requested_version == current_version:
                self._save_version(db, job_id, page_index)
            db.execute("INSERT OR IGNORE INTO quality_version_reviews(job_id,page_index,generation_sha256,review_sha256,report,context,score,visual_passed,provisional) VALUES (?,?,?,?,?,?,?,?,?)",
                       (job_id, page_index, requested_version, digest(report), json.dumps(report, ensure_ascii=False), json.dumps(context_pages, ensure_ascii=False), score, int(visual_passed), int(bool(provisional))))
            db.execute("DELETE FROM quality_selection_current WHERE job_id=?", (job_id,))
            if requested_version == current_version and not provisional:
                db.execute("UPDATE pages SET quality_review=?,qa_passed=? WHERE job_id=? AND page_index=?",
                           (json.dumps(report, ensure_ascii=False), int(not reasons), job_id, page_index))
            self._refresh(db, job_id)
            return {"passed": not reasons, "visual_passed": visual_passed, "score": score,
                    "provisional": bool(provisional), "review_sha256": digest(report), "reasons": reasons}

    @classmethod
    def _quality_candidates(cls, db, job_id):
        job = cls._get_job(db, job_id)
        pages = {str(p["page_index"]): [] for p in job["pages"]}
        for row in db.execute("SELECT * FROM quality_versions WHERE job_id=? ORDER BY page_index,created_at,generation_sha256", (job_id,)):
            snapshot = json.loads(row["snapshot"])
            latest = db.execute("SELECT * FROM quality_version_reviews WHERE job_id=? AND page_index=? AND generation_sha256=? AND provisional=0 ORDER BY review_id DESC LIMIT 1", (job_id, row["page_index"], row["generation_sha256"])).fetchone()
            reasons, report, context = [], None, None
            for kind in ("raw", "cos"):
                try:
                    if inspect_image(snapshot[kind]["path"])["sha256"] != snapshot[kind]["sha256"]:
                        reasons.append(f"{kind} version file changed")
                except (OSError, ValueError, SyntaxError) as error:
                    reasons.append(f"{kind} version file cannot be decoded: {type(error).__name__}")
            if latest is None:
                reasons.append("Independent final review missing for this generation version")
            else:
                report, context = json.loads(latest["report"]), json.loads(latest["context"])
                snapshot["quality_review"] = report
                reasons.extend(page_integrity_reasons(snapshot, context))
            deviations = verdict_reasons(snapshot["features"], report, job["page_count"]) if report else []
            candidate = {"generation_sha256": row["generation_sha256"], "cos_sha256": snapshot["cos"]["sha256"],
                         "review_sha256": latest["review_sha256"] if latest else None,
                         "score": quality_score(snapshot["features"], report) if report else None,
                         "visual_passed": bool(report) and not deviations, "eligible": not reasons,
                         "integrity_reasons": reasons, "unresolved_deviations": deviations,
                         "attempt": snapshot["generation"]["correction_attempt"],
                         "snapshot": snapshot, "review": report, "review_context": context}
            pages[str(row["page_index"])].append(candidate)
        identity = {index: [{key: candidate[key] for key in ("generation_sha256", "cos_sha256", "review_sha256", "score", "visual_passed", "eligible", "integrity_reasons")}
                            for candidate in candidates] for index, candidates in pages.items()}
        return {"candidate_set_sha256": digest(identity), "pages": pages}

    def quality_candidates(self, job_id) -> dict:
        """Read every immutable version and its latest independent final review."""
        with self._connect() as db:
            return self._quality_candidates(db, job_id)

    @classmethod
    def _retry_budget_completed(cls, db, job_id, page_index, candidates):
        attempts = list(db.execute("SELECT * FROM quality_corrections WHERE job_id=? AND page_index=? ORDER BY attempt_number", (job_id, page_index)))
        roots = [a for a in attempts if not a["replacement_of"]]
        if len(roots) != 2 or [a["optimization_slot"] for a in roots] != [1, 2]:
            return False
        for root in roots:
            replacements = [a for a in attempts if a["replacement_of"] == root["attempt_id"]]
            if replacements:
                if (len(replacements) != 1 or root["status"] != "superseded_unknown"
                        or replacements[0]["optimization_slot"] != root["optimization_slot"]
                        or not replacements[0]["replacement_authorization"]):
                    return False
                attempt = replacements[0]
            else:
                attempt = root
            if not attempt["optimization"]:
                return False
            if attempt["status"] == "failed":
                if not attempt["failure_details"]:
                    return False
                recovery = json.loads(attempt["recovery"]) if "recovery" in attempt.keys() and attempt["recovery"] else {}
                if recovery.get("phase") != "failed" or not recovery.get("failure", {}).get("evidence"):
                    # Old reason-only failures are history, not proof that two
                    # genuine calls failed. Unknown outcomes never grant a waiver.
                    return False
            elif attempt["status"] == "recorded":
                matches = [c for c in candidates if c["snapshot"]["generation"]["correction_attempt_id"] == attempt["attempt_id"]]
                if len(matches) != 1 or not matches[0]["eligible"]:
                    return False
            else:
                return False
        return True

    @classmethod
    def _validate_selection(cls, db, job, report, *, assign_agent=False):
        if not isinstance(report, dict) or report.get("schema_version") != 3:
            raise StateError("Best-version selection requires schema_version=3")
        cls._quality_input(agent_id, report.get("selector_agent_id"), "selector_agent_id")
        cls._quality_input(nonempty, report.get("reason"), "Overall independent selection reason")
        manifest = cls._quality_candidates(db, job["job_id"])
        if report.get("candidate_set_sha256") != manifest["candidate_set_sha256"]:
            raise StateError("Selection must bind all current candidate versions and reviews")
        if (not isinstance(report.get("candidates"), dict) or set(report["candidates"]) != set(manifest["pages"])
                or not isinstance(report.get("selected"), dict) or set(report["selected"]) != set(manifest["pages"])):
            raise StateError("Selection must cover every page and every valid candidate")
        chosen, degraded = {}, False
        for index, versions in manifest["pages"].items():
            current = job["pages"][int(index)]
            if current["pending_correction"]:
                raise StateError("Pending generation cannot be bypassed by selection")
            eligible = sorted((c for c in versions if c["eligible"]), key=lambda c: (not c["visual_passed"], -c["score"], c["attempt"], c["generation_sha256"]))
            comparisons = report["candidates"][index]
            if not eligible or not isinstance(comparisons, list) or len(comparisons) != len(eligible):
                raise StateError("Every page needs valid independently reviewed candidates and complete comparisons")
            for rank, (expected, supplied) in enumerate(zip(eligible, comparisons), 1):
                if (not isinstance(supplied, dict) or supplied.get("generation_sha256") != expected["generation_sha256"]
                        or supplied.get("review_sha256") != expected["review_sha256"] or supplied.get("score") != expected["score"]
                        or type(supplied.get("score")) is not int or type(supplied.get("rank")) is not int
                        or supplied.get("rank") != rank):
                    raise StateError("Candidate ranks and scores must match the deterministic quality comparison")
                cls._quality_input(nonempty, supplied.get("reason"), "Independent candidate comparison reason")
            best, supplied = eligible[0], report["selected"][index]
            if not isinstance(supplied, dict) or supplied.get("generation_sha256") != best["generation_sha256"] or supplied.get("cos_sha256") != best["cos_sha256"]:
                raise StateError("Select the highest-ranked candidate, preferring every valid passing version")
            cls._quality_input(nonempty, supplied.get("reason"), "Selected version reason")
            deviations = supplied.get("unresolved_deviations")
            if not isinstance(deviations, list) or deviations != best["unresolved_deviations"]:
                raise StateError("Selected unresolved deviations must match the independent review")
            if not best["visual_passed"]:
                if not cls._retry_budget_completed(db, job["job_id"], int(index), versions):
                    raise StateError("Visual quality override requires two completed optimized correction attempts")
                degraded = True
            chosen[index] = best
        identity = report.get("identity_review")
        expected_hashes = {index: item["cos_sha256"] for index, item in chosen.items()}
        if not isinstance(identity, dict) or identity.get("cos_sha256") != expected_hashes:
            raise StateError("Final group identity review must bind every selected output hash")
        checks = identity.get("checks")
        if not isinstance(checks, dict) or set(checks) != {"identity.face", "identity.hair", "identity.outfit"}:
            raise StateError("Final group identity review must cover face, hair and outfit")
        for key, check in checks.items():
            if not isinstance(check, dict) or check.get("status") not in {"pass", "fail", "unverifiable", "not_applicable"}:
                raise StateError("Invalid final group identity verdict")
            cls._quality_input(nonempty, check.get("evidence"), "Final group identity evidence")
            visible = any(item["snapshot"]["features"]["checks"][key]["visibility"] == "visible" for item in chosen.values())
            critical = any(item["snapshot"]["features"]["checks"][key]["critical"] for item in chosen.values())
            if (check["status"] == "fail" or (visible and check["status"] != "pass")
                    or (critical and check["status"] == "unverifiable")):
                if not all(cls._retry_budget_completed(db, job["job_id"], int(index), manifest["pages"][index]) for index in chosen):
                    raise StateError("Unresolved group identity requires exhausted retry budgets on all pages")
                degraded = True
        if assign_agent:
            cls._assign_agent(db, job["job_id"], report["selector_agent_id"], "selector", fresh=True)
        else:
            role = db.execute("SELECT role FROM quality_agents WHERE job_id=? AND agent_id=?", (job["job_id"], report["selector_agent_id"])).fetchone()
            if role is None or role[0] != "selector":
                raise StateError("Selection requires an independent registered selector")
        return chosen, degraded

    def select_best_available(self, job_id, report: dict) -> dict:
        """Select a complete independently compared set; waive only visual defects."""
        with self._transaction() as db:
            self._assert_mutable(db, job_id)
            job = self._get_job(db, job_id)
            chosen, degraded = self._validate_selection(db, job, report, assign_agent=True)
            saved = {**report, "quality_override": "best_available_after_max_retries" if degraded else None,
                     "quality_passed": not degraded, "quality_status": "degraded" if degraded else "passed"}
            for index, selected in chosen.items():
                snapshot = selected["snapshot"]
                db.execute("UPDATE pages SET cos=?,generation=?,quality_review=?,qa_passed=?,quality_blocked=0 WHERE job_id=? AND page_index=?",
                           (json.dumps(snapshot["cos"]), json.dumps(snapshot["generation"], ensure_ascii=False), json.dumps(selected["review"], ensure_ascii=False), int(selected["visual_passed"]), job_id, int(index)))
            cursor = db.execute("INSERT INTO quality_selections(job_id,report) VALUES (?,?)", (job_id, json.dumps(saved, ensure_ascii=False)))
            db.execute("INSERT INTO quality_selection_current(job_id,selection_id) VALUES (?,?) ON CONFLICT(job_id) DO UPDATE SET selection_id=excluded.selection_id", (job_id, cursor.lastrowid))
            self._refresh(db, job_id)
            return self._readiness(db, job_id)

    def record_research(self, job_id, *, character: str, sources: list[dict], verified=False, **findings):
        if not character.strip() or not sources or verified is not True:
            raise StateError("Verified character research with at least one source is required")
        if any(not source.get("url", "").startswith(("https://", "http://")) for source in sources):
            raise ValueError("Research sources require source URLs")
        research = {"character": character, "sources": sources, "verified": True, "findings": findings}
        with self._transaction() as db:
            self._get_job(db, job_id)
            self._assert_mutable(db, job_id)
            db.execute("UPDATE jobs SET research=? WHERE job_id=?", (json.dumps(research, ensure_ascii=False), job_id))
            self._refresh(db, job_id)

    def record_copy(self, job_id, platform, *, title: str, body: str):
        self._platform(platform)
        if not body.strip():
            raise ValueError("Platform copy cannot be empty")
        with self._transaction() as db:
            job = self._get_job(db, job_id)
            self._publication(job, platform)
            self._assert_mutable(db, job_id)
            if not job["research"]:
                raise StateError("Research the character before drafting platform copy")
            db.execute("UPDATE publications SET copy=? WHERE job_id=? AND platform=?", (json.dumps({"title": title, "body": body}, ensure_ascii=False), job_id, platform))
            self._refresh(db, job_id)

    @staticmethod
    def _platform(platform):
        if platform not in PLATFORMS:
            raise ValueError(f"Unsupported platform: {platform}")

    @staticmethod
    def _publication(job, platform):
        if platform not in job["publications"]:
            raise StateError(f"Platform {platform} was not assigned to this job")
        return job["publications"][platform]

    @classmethod
    def _refresh(cls, db, job_id):
        job = cls._get_job(db, job_id)
        publications = list(job["publications"].values())
        if all(p["status"] == "published" for p in publications):
            status = "complete"
        elif any(p["attempt_count"] > 0 for p in publications):
            status = "publishing"
        elif all(p["raw"] and p["cos"] for p in job["pages"]) and not cls._quality_gate(db, job)["reasons"]:
            status = "ready" if job["research"] and all(p["copy"] for p in publications) else "generated"
        elif all(p["raw"] for p in job["pages"]):
            status = "downloaded"
        elif any(p["raw"] for p in job["pages"]):
            status = "downloading"
        else:
            status = "reserved"
        db.execute("UPDATE jobs SET status=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=?", (status, job_id))

    @classmethod
    def _quality_gate(cls, db, job):
        selection = job.get("quality_selection")
        if selection:
            try:
                chosen, degraded = cls._validate_selection(db, job, selection)
                for index, candidate in chosen.items():
                    page = job["pages"][int(index)]
                    if page["generation_sha256"] != candidate["generation_sha256"] or page["cos"]["sha256"] != candidate["cos_sha256"]:
                        raise StateError("Current output no longer matches the independently selected version")
                return {"reasons": [], "quality_status": "degraded" if degraded else "passed",
                        "quality_passed": not degraded,
                        "quality_override": "best_available_after_max_retries" if degraded else None}
            except (StateError, KeyError, TypeError) as error:
                return {"reasons": [str(error)], "quality_status": "unapproved", "quality_passed": False, "quality_override": None}
        reasons = [f"p{page['page_index']} {reason}" for page in job["pages"] for reason in page_quality_reasons(page, job["pages"])]
        return {"reasons": reasons, "quality_status": "unapproved" if reasons else "passed",
                "quality_passed": not reasons, "quality_override": None}

    @classmethod
    def _readiness(cls, db, job_id) -> dict:
        job = cls._get_job(db, job_id)
        reasons = []
        for page in job["pages"]:
            for kind in ("raw", "cos"):
                artifact = page[kind]
                label = f"p{page['page_index']} {kind}"
                if artifact is None:
                    reasons.append(f"{label} missing")
                    continue
                path = Path(artifact["path"])
                if not path.is_file():
                    reasons.append(f"{label} file unavailable")
                elif hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
                    reasons.append(f"{label} file changed after validation")
        quality = cls._quality_gate(db, job)
        reasons.extend(quality["reasons"])
        if not job["research"]:
            reasons.append("character research missing")
        for platform, publication in job["publications"].items():
            if not publication["copy"]:
                reasons.append(f"{platform} copy missing")
        return {"ready": not reasons, "reasons": reasons, "page_count": job["page_count"],
                **{key: quality[key] for key in ("quality_status", "quality_passed", "quality_override")}}

    def publish_ready(self, job_id) -> dict:
        with self._connect() as db:
            return self._readiness(db, job_id)

    @staticmethod
    def _claim_publish_day(db, job_id, day):
        claim_run_publication(db, job_id, day)

    def claim_publish(self, job_id, platform, *, day=None) -> dict | None:
        """Claim one platform. On a crash, reconcile the remote post before retrying.

        Return None for an already-published platform. Include the stable
        idempotency_key in adapters that support it; do not assume all APIs do.
        A claim always uses the actual Shanghai date and obeys the configured
        earliest publication time, with immediate publication for explicitly
        authorized manual runs. The optional day asserts the current date.
        """
        self._platform(platform)
        with self._transaction() as db:
            job = self._get_job(db, job_id)
            publication = self._publication(job, platform)
            if publication["status"] == "published":
                return None
            now = self._now()
            actual_day = now.date().isoformat()
            if day is not None and day != actual_day:
                raise StateError("Publication claim day must match the current Asia/Shanghai date")
            earliest = publication_not_before(db, job_id, self.publish_not_before)
            if now.time() < earliest:
                raise StateError(f"Publication is not allowed before {earliest.strftime('%H:%M')} Asia/Shanghai")
            if publication["status"] in ("in_flight", "reconciliation_required"):
                raise NeedsReconciliation(f"{platform}: resolve the previous attempt using its receipt before retrying")
            readiness = self._readiness(db, job_id)
            if not readiness["ready"]:
                raise StateError("Artwork package not publishable: " + "; ".join(readiness["reasons"]))
            self._claim_publish_day(db, job_id, actual_day)
            db.execute("UPDATE publications SET status='in_flight',attempt_count=attempt_count+1,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND platform=?", (job_id, platform))
            self._refresh(db, job_id)
            return self._get_job(db, job_id)["publications"][platform]

    def fail_publish(self, job_id, platform, *, reason: str, definitely_not_published=False):
        """Only positive evidence of no remote post permits a fresh attempt.

        Do not include API tokens, request headers, or credential-bearing response
        bodies in reason; use a short public error description.
        """
        self._platform(platform)
        with self._transaction() as db:
            publication = self._publication(self._get_job(db, job_id), platform)
            if publication["status"] not in ("in_flight", "reconciliation_required"):
                raise StateError("Only a claimed or uncertain publication can fail")
            status = "pending" if definitely_not_published is True else "reconciliation_required"
            db.execute("UPDATE publications SET status=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND platform=?", (status, reason, job_id, platform))
            self._refresh(db, job_id)

    def complete_publish(self, job_id, platform, *, receipt_id: str, verified=False, receipt_url: str | None = None, day=None):
        """Record real success only after the adapter verifies the remote post.

        For reconciliation, pass day only when the remote receipt's verified
        publication timestamp establishes its Asia/Shanghai date. Otherwise the
        current local date is used. This records evidence, not a new publish call,
        and therefore can be invoked before today's publication window opens.
        """
        self._platform(platform)
        if verified is not True or not isinstance(receipt_id, str) or not receipt_id.strip():
            raise StateError("Publication success requires a verified, non-empty receipt ID")
        if receipt_id.startswith("ui-evidence:"):
            raise StateError("Use complete_publish_from_ui to record UI evidence without a remote post ID")
        with self._transaction() as db:
            self._record_publication_success(db, job_id, platform, receipt_id, receipt_url, day or self._now().date().isoformat())

    def _record_publication_success(self, db, job_id, platform, receipt_id, receipt_url, day):
        publication = self._publication(self._get_job(db, job_id), platform)
        if publication["status"] == "published":
            if publication["receipt_id"] != receipt_id:
                raise StateError("Conflicting publication receipt")
            return
        if publication["status"] not in ("in_flight", "reconciliation_required"):
            raise StateError("Claim the platform before recording publication success")
        self._claim_publish_day(db, job_id, day)
        db.execute("UPDATE publications SET status='published',receipt_id=?,receipt_url=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND platform=?", (receipt_id, receipt_url, job_id, platform))
        self._refresh(db, job_id)

    def complete_publish_from_ui(self, job_id, platform, *, account_id: str, title: str,
                                 published_at: str | datetime, evidence_path: str | Path,
                                 verified=False) -> dict:
        """Record a visually verified published post when its remote ID is unavailable.

        The caller must inspect the real account's published-post view and verify
        the account, title, exact media, publication state and timestamp. This
        method validates the saved evidence file and stored title; it does not
        claim to infer publication from arbitrary screenshot pixels. The receipt
        is a LOCAL evidence identifier, never an invented platform post ID.
        """
        self._platform(platform)
        if verified is not True or not isinstance(account_id, str) or not account_id.strip():
            raise StateError("UI completion requires verified published-post evidence and an account ID")
        timestamp = datetime.fromisoformat(published_at) if isinstance(published_at, str) else published_at
        if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
            raise StateError("UI publication timestamp must include its timezone")
        timestamp = timestamp.astimezone(ZoneInfo("Asia/Shanghai"))
        image = inspect_image(evidence_path)
        receipt_id = "ui-evidence:" + image["sha256"]
        evidence = {"verification_kind": "published_ui_evidence", "account_id": account_id,
                    "title": title, "published_at": timestamp.isoformat(),
                    "evidence_path": image["path"], "evidence_sha256": image["sha256"],
                    "post_id": None, "post_url": None, "local_receipt_id": receipt_id}
        with self._transaction() as db:
            publication = self._publication(self._get_job(db, job_id), platform)
            if not publication["copy"] or title != publication["copy"]["title"]:
                raise StateError("Published UI title does not match the stored platform copy")
            existing = publication["receipt_evidence"]
            if existing is not None and existing != evidence:
                raise StateError("Conflicting published UI evidence")
            self._record_publication_success(db, job_id, platform, receipt_id, None, timestamp.date().isoformat())
            db.execute("INSERT OR IGNORE INTO publication_evidence(job_id,platform,verification_kind,details) VALUES (?,?,?,?)",
                       (job_id, platform, "published_ui_evidence", json.dumps(evidence, ensure_ascii=False)))
            return self._get_job(db, job_id)["publications"][platform]
