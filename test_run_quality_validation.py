"""Offline tests for the daily preflight; no live services or credentials."""

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from zoneinfo import ZoneInfo

from PIL import Image

from p2cos_quality import REQUIRED_CHECKS, digest
from p2cos_state import State, inspect_image
from run_quality_validation import consistent_backup, historical_day_audit as daily_check, historical_reports, historical_rows_preserved, logical_snapshot, migrate_production, validate_existing


class DailyCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="quality_preflight_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.sqlite3"
        self.state = State(self.db_path, platforms=("xiaohongshu",))
        self.config_path = self.root / "workflow.json"
        self.config_path.write_text(json.dumps({"storage": {"state_db": str(self.db_path)},
                                                "schedule": {"publish_at": "10:00"},
                                                "publishing": {"platforms": ["xiaohongshu"]}}))
        self.now = datetime(2026, 10, 9, 1, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def check(self):
        return daily_check(self.config_path, now=self.now)

    def test_no_existing_job_allows_selection_without_reserving(self):
        self.assertEqual(self.check()["action"], "select_new_artwork")
        self.assertIsNone(self.state.latest_job())

    def test_published_today_skips_before_publish_window_and_preserves_state(self):
        job = self.state.reserve_daily([{"id": "123", "page_count": 1}], day="2026-10-09")
        # Persisted fixture: this test checks preflight, not receipt verification.
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE jobs SET status='complete' WHERE job_id=?", (job["job_id"],))
            db.execute("UPDATE publications SET status='published',attempt_count=1,receipt_id='test-verified-receipt' WHERE job_id=?", (job["job_id"],))
            db.execute("INSERT INTO publish_days(day,job_id) VALUES (?,?)", ("2026-10-09", job["job_id"]))
        before = self.state.get_job(job["job_id"])
        result = self.check()
        self.assertEqual(result["action"], "skip_today_already_published")
        self.assertEqual(result["external_calls"], {"pixiv_download": 0, "image_generation": 0, "social_publication": 0})
        self.assertEqual(self.state.get_job(job["job_id"]), before)

    def test_completed_publication_day_can_refer_to_older_reservation(self):
        job = self.state.reserve_daily([{"id": "123", "page_count": 1}], day="2026-10-08")
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE jobs SET status='complete' WHERE job_id=?", (job["job_id"],))
            db.execute("UPDATE publications SET status='published',receipt_id='test-receipt' WHERE job_id=?", (job["job_id"],))
            db.execute("INSERT INTO publish_days(day,job_id) VALUES (?,?)", ("2026-10-09", job["job_id"]))
        self.assertEqual(self.check()["action"], "skip_today_already_published")

    def test_prior_unfinished_job_resumes_instead_of_new_selection(self):
        job = self.state.reserve_daily([{"id": "123", "page_count": 1}], day="2026-10-08")
        result = self.check()
        self.assertEqual(result["action"], "resume_existing_job")
        self.assertEqual(result["job"]["job_id"], job["job_id"])

    def test_complete_job_without_receipt_is_blocked(self):
        job = self.state.reserve_daily([{"id": "123", "page_count": 1}], day="2026-10-09")
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE jobs SET status='complete' WHERE job_id=?", (job["job_id"],))
        self.assertEqual(self.check()["action"], "blocked_inconsistent_state")

    def test_consistent_backup_contains_committed_wal_and_is_standalone(self):
        self.state.reserve_daily([{"id": "123", "page_count": 1}], day="2026-10-09")
        destination = self.root / "recovery.sqlite3"
        before = logical_snapshot(self.db_path)
        result = consistent_backup(self.db_path, destination)
        self.assertEqual(result["integrity_check"], "ok")
        self.assertEqual(logical_snapshot(destination), before)
        with sqlite3.connect(destination) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertFalse(Path(str(destination) + "-wal").exists())
        with self.assertRaises(FileExistsError):
            consistent_backup(self.db_path, destination)

    def test_repeated_compatible_migrations_keep_separate_backups_and_old_values(self):
        self.state.reserve_daily([{"id": "123", "page_count": 1}], day="2026-10-09")
        reports = self.root / "reports"
        reports.mkdir()
        baseline = logical_snapshot(self.db_path)
        first = migrate_production(self.config_path, reports)
        second = migrate_production(self.config_path, reports)
        self.assertEqual(first["migration_sequence"], 1)
        self.assertEqual(second["migration_sequence"], 2)
        self.assertNotEqual(first["backup_before"]["path"], second["backup_before"]["path"])
        self.assertEqual(len(second["previous_migrations"]), 1)
        self.assertEqual(logical_snapshot(self.db_path), baseline)
        self.assertEqual(logical_snapshot(first["backup_before"]["path"]), baseline)
        self.assertEqual(logical_snapshot(second["backup_before"]["path"]), baseline)

    def test_historical_comparison_allows_new_work_but_detects_old_row_changes(self):
        old = self.state.reserve_daily([{"id": "123", "page_count": 1}], day="2026-10-09")
        baseline = self.root / "baseline.sqlite3"
        consistent_backup(self.db_path, baseline)
        self.state.reserve_daily([{"id": "456", "page_count": 1}], day="2026-10-10")
        self.assertTrue(historical_rows_preserved(baseline, self.db_path)["all_historical_cells_preserved"])
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE jobs SET status='changed-fixture' WHERE job_id=?", (old["job_id"],))
        result = historical_rows_preserved(baseline, self.db_path)
        self.assertFalse(result["all_historical_cells_preserved"])
        self.assertEqual(result["changed_or_missing_rows"], [{"table": "jobs", "primary_key": [old["job_id"]]}])

    def test_existing_audit_uses_manifest_artwork_without_certifying_legacy_generation(self):
        media_path = self.root / "synthetic.png"
        Image.new("RGB", (12, 16), "blue").save(media_path)
        image = inspect_image(media_path)
        cos_path = self.root / "synthetic_cos.png"
        Image.new("RGB", (12, 16), "green").save(cos_path)
        cos_image = inspect_image(cos_path)
        features = {"schema_version": 2, "analyst_agent_id": "test-analyst", "raw_sha256": image["sha256"],
                    "checks": {key: {"visibility": "visible", "critical": True,
                                     "expected": "Synthetic expected feature", "evidence": "Synthetic source region"}
                               for key in REQUIRED_CHECKS}}
        review = {"schema_version": 2, "reviewer_agent_id": "test-reviewer",
                  "raw_sha256": image["sha256"], "cos_sha256": cos_image["sha256"],
                  "features_sha256": digest(features),
                  "checks": {key: {"status": "pass", "evidence": "Synthetic comparison"} for key in REQUIRED_CHECKS}}
        (self.root / "features.json").write_text(json.dumps(features))
        (self.root / "review.json").write_text(json.dumps(review))
        (self.root / "reports_manifest.json").write_text(json.dumps({"artworks": [
            {"artwork_id": "789", "features_path": "features.json", "review_path": str(self.root / "review.json")}]}))
        (self.root / "media_manifest.json").write_text(json.dumps({"789": {"raw": image, "cos": cos_image}}))
        before = logical_snapshot(self.db_path)
        result = validate_existing(self.root)
        self.assertEqual([row["artwork_id"] for row in result["artworks"]], ["789"])
        self.assertFalse(result["historical_generation_certified"])
        self.assertFalse(result["artworks"][0]["legacy_publish_ready"]["ready"])
        self.assertEqual(result["publications_created"], 0)
        self.assertEqual(logical_snapshot(self.db_path), before)

    def test_audit_manifest_cannot_select_reports_outside_its_directory(self):
        manifest = self.root / "reports_manifest.json"
        manifest.write_text(json.dumps({"artworks": [
            {"artwork_id": "789", "features_path": "../outside.json", "review_path": "review.json"}]}))
        with self.assertRaisesRegex(ValueError, "own directory"):
            historical_reports(self.root)
        manifest.write_text(json.dumps({"artworks": []}))
        with self.assertRaisesRegex(ValueError, "at least one artwork"):
            historical_reports(self.root)


if __name__ == "__main__":
    unittest.main()
