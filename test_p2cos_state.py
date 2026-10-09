import concurrent.futures
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from PIL import Image

from p2cos_state import NeedsReconciliation, State, StateError, inspect_image
from p2cos_quality import REQUIRED_CHECKS, digest, unresolved_checks
from recovery_test_fixtures import save_result, fail


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "state.sqlite3"
        self.now = datetime(2026, 10, 9, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.state = State(self.db, clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def picture(self, filename):
        path = self.root / filename
        Image.new("RGB", (12, 16), (50, 80, 120)).save(path)
        return path

    def reserve(self, artwork=123, pages=1, day="2026-10-09"):
        return self.state.reserve_daily([{"id": artwork, "page_count": pages}], day=day)

    def ready(self, artwork=123, pages=1):
        job = self.reserve(artwork, pages)
        for index in range(pages):
            self.state.record_raw(job["job_id"], index, self.picture(f"{artwork}_p{index}_raw.png"))
            self.state.record_features(job["job_id"], index, self.features(job["job_id"], index))
            cos = self.picture(f"{artwork}_p{index}_cos.png")
            self.state.record_cos(job["job_id"], index, cos, generator_agent_id="test-generator", generation_trace=self.trace(job["job_id"], index, cos))
        for index in range(pages):
            self.state.record_quality_review(job["job_id"], index, self.review(job["job_id"], index))
        self.state.record_research(job["job_id"], character="Test Character", sources=[{"url": "https://example.org/character"}], verified=True, personality="Reserved")
        for platform in job["publications"]:
            self.state.record_copy(job["job_id"], platform, title="Character portrait", body="A quiet moment.")
        return self.state.get_job(job["job_id"])

    def features(self, job_id, index=0):
        page = self.state.get_job(job_id)["pages"][index]
        return {"schema_version": 2, "analyst_agent_id": "test-analyst", "raw_sha256": page["raw"]["sha256"],
                "checks": {key: {"visibility": "visible", "critical": True,
                                  "expected": f"Synthetic fixture expectation: {key}",
                                  "evidence": f"Synthetic fixture source region: {key}"} for key in REQUIRED_CHECKS}}

    def review(self, job_id, index=0, reviewer="test-reviewer"):
        job = self.state.get_job(job_id)
        page = job["pages"][index]
        return {"schema_version": 2, "reviewer_agent_id": reviewer,
                "raw_sha256": page["raw"]["sha256"], "cos_sha256": page["cos"]["sha256"],
                "features_sha256": page["features_sha256"], "generation_sha256": page["generation_sha256"],
                "identity_cos_sha256": {str(p["page_index"]): p["cos"]["sha256"] for p in job["pages"] if p["cos"]},
                "checks": {key: {"status": "pass", "evidence": f"Synthetic fixture comparison: {key}"} for key in page["features"]["checks"]}}

    def trace(self, job_id, index, output, generator="test-generator", attempt=None):
        page = self.state.get_job(job_id)["pages"][index]
        output = inspect_image(output)
        prompt = "Synthetic test fixture only; no image generation was performed."
        if attempt:
            prompt += f" Optimization {attempt['attempt_number']}: preserve the visible original hem and skin gaps."
        return {"tool": "image_gen.imagegen", "generator_agent_id": generator,
                "actual_prompt": prompt, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "references": [{"role": "current_page_original", "path": page["raw"]["path"], "sha256": page["raw"]["sha256"]}],
                "output_path": output["path"], "output_sha256": output["sha256"], "returned_model": "undisclosed (test fixture)",
                "attempt": attempt["attempt_number"] if attempt else 0,
                "correction_attempt_id": attempt["attempt_id"] if attempt else None}

    def correction(self, job_id, index=0, number=1, changed=True):
        generator = f"test-editor-{number}"
        self.mark_quality_failure(job_id, index)
        attempt = self.state.begin_correction(job_id, index, generator_agent_id=generator, optimization=self.optimization(job_id, index))
        path = self.root / f"corrected-{index}-{number}.png"
        if changed:
            Image.new("RGB", (12 + number, 16), (10 * number, 50, 90)).save(path)
        else:
            path.write_bytes(Path(self.state.get_job(job_id)["pages"][index]["cos"]["path"]).read_bytes())
        save_result(self.state, self.root, job_id, index, attempt,
                    self.trace(job_id, index, path, generator, attempt))
        self.state.record_cos(job_id, index, path, generator_agent_id=generator, correction_attempt_id=attempt["attempt_id"], generation_trace=self.trace(job_id, index, path, generator, attempt))
        return attempt

    def mark_quality_failure(self, job_id, index=0):
        version = self.state.get_job(job_id)["pages"][index]["generation_sha256"]
        report = self.review(job_id, index, reviewer=f"failure-reviewer-{version}")
        report["checks"]["hem_and_skin_gaps.skin_gaps"].update(status="fail", evidence="Synthetic fixture skin-gap mismatch")
        self.state.record_quality_review(job_id, index, report)

    def optimization(self, job_id, index=0):
        page = self.state.get_job(job_id)["pages"][index]
        candidates = self.state.quality_candidates(job_id)["pages"][str(index)]
        candidate = next(c for c in candidates if c["generation_sha256"] == page["generation_sha256"])
        return {"based_on_generation_sha256": page["generation_sha256"], "review_sha256": candidate["review_sha256"],
                "target_checks": unresolved_checks(page["features"], candidate["review"]),
                "changes": "Specify exact original hem and exposed skin boundaries from the failure evidence"}

    def test_bootstrap_conservatively_deduplicates_all_history_sources(self):
        selection = self.root / "selection.json"
        selection.write_text(json.dumps([{"id": 101, "files": ["removed.jpg"]}, {"id": 109, "files": []}]))
        storage = self.root / "storage.json"
        storage.write_text(json.dumps({"google_drive": {"artworks": {"103": {"folder_id": "archived"}}}}))
        rawdir = self.root / "raw"
        rawdir.mkdir()
        Image.new("RGB", (2, 2)).save(rawdir / "102_p0_raw.jpg")
        Image.new("RGB", (2, 2)).save(rawdir / "999_p0_cos.jpg")
        result = self.state.bootstrap_history(selection, [rawdir], storage, known_archived=("104",))
        self.assertEqual(set(result["artwork_ids"]), {"101", "102", "103", "104"})
        self.assertEqual(self.state.bootstrap_history(selection, [rawdir], storage)["added"], 0)
        job = self.state.reserve_daily([{"id": i, "page_count": 1} for i in (101, 102, 103, 104, 109)])
        self.assertEqual(job["artwork_id"], "109")

    def test_concurrent_reservations_share_one_daily_work(self):
        def reserve(index):
            return State(self.db).reserve_daily([{"id": 900 + index, "page_count": 2}], day="2026-10-09")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(reserve, range(12)))
        self.assertEqual(len({j["job_id"] for j in jobs}), 1)
        self.assertEqual(len(self.state.excluded_ids()), 1)

    def test_resumes_only_the_requested_run_and_allows_another_run(self):
        previous = self.reserve(day="2026-10-08")
        resumed = self.state.reserve_run([], run_id=previous["run_id"])
        self.assertEqual(previous["job_id"], resumed["job_id"])
        self.assertEqual(resumed["day"], "2026-10-08")
        another = self.reserve(artwork=456, day="2026-10-09")
        self.assertNotEqual(previous["job_id"], another["job_id"])

    def test_selection_decision_survives_reservation_and_process_restart(self):
        decision = {"candidate_manifest": "/workspace/work/candidates.json", "fetched_at": "2026-10-09T00:00:00+08:00",
                    "approved_ids": ["123"], "reason": "Clear pose and detailed clothing", "visual_reviewed": True}
        job = self.state.reserve_daily([{"id": 123, "page_count": 2, "selection_decision": decision}], day="2026-10-09")
        reopened = State(self.db, clock=lambda: self.now)
        resumed = reopened.reserve_run([], run_id=job["run_id"])
        self.assertEqual(resumed["job_id"], job["job_id"])
        self.assertEqual(resumed["metadata"]["selection_decision"], decision)

    def test_partial_multipage_artwork_cannot_publish(self):
        job = self.reserve(pages=2)
        self.state.record_raw(job["job_id"], 0, self.picture("p0_raw.jpg"))
        self.state.record_cos(job["job_id"], 0, self.picture("p0_cos.png"), qa_passed=True)
        self.assertFalse(self.state.publish_ready(job["job_id"])["ready"])
        self.assertIn("p1 raw missing", self.state.publish_ready(job["job_id"])["reasons"])
        with self.assertRaises(StateError):
            self.state.claim_publish(job["job_id"], "douyin")

    def test_strict_decode_rejects_truncated_image_without_recording_success(self):
        job = self.reserve()
        path = self.picture("truncated.jpg")
        path.write_bytes(path.read_bytes()[:-20])
        with self.assertRaises((OSError, SyntaxError)):
            self.state.record_raw(job["job_id"], 0, path)
        self.assertIsNone(self.state.get_job(job["job_id"])["pages"][0]["raw"])
        self.assertNotIn("123", self.state.downloaded_ids())

    def test_full_decode_reports_real_format_dimensions_and_size(self):
        path = self.picture("decoded.png")
        details = inspect_image(path)
        self.assertEqual((details["format"], details["width"], details["height"]), ("PNG", 12, 16))
        self.assertEqual(details["bytes"], path.stat().st_size)
        self.assertTrue(details["decoded"])

    def test_changed_artifact_blocks_publication(self):
        job = self.ready()
        path = Path(job["pages"][0]["cos"]["path"])
        Image.new("RGB", (13, 18)).save(path)
        self.assertFalse(self.state.publish_ready(job["job_id"])["ready"])
        with self.assertRaises(StateError):
            self.state.claim_publish(job["job_id"], "douyin")

    def test_independent_platform_retry_never_republishes_successful_platform(self):
        job = self.ready(pages=2)
        job_id = job["job_id"]
        self.state.claim_publish(job_id, "douyin", day="2026-10-09")
        self.state.complete_publish(job_id, "douyin", receipt_id="dy-real-receipt", verified=True, day="2026-10-09")
        first = self.state.claim_publish(job_id, "xiaohongshu", day="2026-10-09")
        self.state.fail_publish(job_id, "xiaohongshu", reason="Rejected before creation", definitely_not_published=True)
        self.assertIsNone(self.state.claim_publish(job_id, "douyin"))
        second = self.state.claim_publish(job_id, "xiaohongshu", day="2026-10-09")
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertEqual(second["attempt_count"], 2)
        self.state.complete_publish(job_id, "xiaohongshu", receipt_id="xhs-real-receipt", verified=True, day="2026-10-09")
        self.assertEqual(self.state.get_job(job_id)["status"], "complete")
        self.assertEqual(self.reserve(456)["job_id"], job_id)
        next_job = self.state.reserve_daily([{"id": 123, "page_count": 2}, {"id": 456, "page_count": 1}], day="2026-10-10")
        self.assertEqual(next_job["artwork_id"], "456")

    def test_late_resume_does_not_consume_another_runs_publication_slot(self):
        job = self.ready()
        self.now = self.now.replace(day=10)
        for platform in ("douyin", "xiaohongshu"):
            self.state.claim_publish(job["job_id"], platform, day="2026-10-10")
            self.state.complete_publish(job["job_id"], platform, receipt_id=f"receipt-{platform}", verified=True, day="2026-10-10")
        same_day = self.reserve(artwork=456, day="2026-10-10")
        self.assertNotEqual(same_day["job_id"], job["job_id"])
        self.assertEqual(same_day["artwork_id"], "456")
        next_day = self.reserve(artwork=789, day="2026-10-11")
        self.assertEqual(next_day["artwork_id"], "789")

    def test_every_cos_page_needs_explicit_quality_approval(self):
        job = self.ready(pages=2)
        self.state.record_cos(job["job_id"], 1, job["pages"][1]["cos"]["path"])
        self.assertFalse(self.state.publish_ready(job["job_id"])["ready"])
        with self.assertRaises(StateError):
            self.state.claim_publish(job["job_id"], "xiaohongshu")

    def test_uncertain_publish_requires_remote_reconciliation(self):
        job = self.ready()
        self.state.claim_publish(job["job_id"], "douyin")
        restarted = State(self.db, clock=lambda: self.now)
        with self.assertRaises(NeedsReconciliation):
            restarted.claim_publish(job["job_id"], "douyin")
        restarted.fail_publish(job["job_id"], "douyin", reason="Response interrupted")
        with self.assertRaises(NeedsReconciliation):
            restarted.claim_publish(job["job_id"], "douyin")
        restarted.complete_publish(job["job_id"], "douyin", receipt_id="verified-after-query", verified=True)
        self.assertIsNone(restarted.claim_publish(job["job_id"], "douyin"))

    def test_receipt_and_quality_review_are_required(self):
        job = self.ready()
        self.state.claim_publish(job["job_id"], "douyin")
        with self.assertRaises(StateError):
            self.state.complete_publish(job["job_id"], "douyin", receipt_id="unverified")
        with self.assertRaises(StateError):
            self.state.complete_publish(job["job_id"], "douyin", receipt_id="", verified=True)
        with self.assertRaises(StateError):
            self.state.record_cos(job["job_id"], 0, self.picture("new_cos.png"), qa_passed=True)

    def test_publication_time_gate_blocks_before_ten_and_allows_at_ten(self):
        job = self.ready()
        self.now = self.now.replace(hour=9, minute=59, second=59)
        with self.assertRaisesRegex(StateError, "before 10:00 Asia/Shanghai"):
            self.state.claim_publish(job["job_id"], "douyin")
        self.assertEqual(self.state.get_job(job["job_id"])["publications"]["douyin"]["attempt_count"], 0)
        self.now = self.now.replace(hour=10, minute=0, second=0)
        self.assertEqual(self.state.claim_publish(job["job_id"], "douyin")["status"], "in_flight")

    def test_claim_day_is_an_assertion_and_cannot_override_actual_date(self):
        job = self.ready()
        for wrong_day in ("2026-10-08", "2026-10-10"):
            with self.assertRaisesRegex(StateError, "current Asia/Shanghai date"):
                self.state.claim_publish(job["job_id"], "douyin", day=wrong_day)
        self.now = self.now.replace(hour=9)
        with self.assertRaisesRegex(StateError, "before 10:00"):
            self.state.claim_publish(job["job_id"], "douyin", day="2026-10-09")

    def test_clock_is_normalized_to_shanghai(self):
        job = self.ready()
        self.now = datetime(2026, 10, 9, 2, 0, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(self.state.claim_publish(job["job_id"], "douyin", day="2026-10-09")["status"], "in_flight")

    def test_verified_previous_day_receipts_do_not_consume_today(self):
        job = self.ready()
        for platform in ("douyin", "xiaohongshu"):
            self.state.claim_publish(job["job_id"], platform)
        self.now = self.now.replace(day=10, hour=8)
        for platform in ("douyin", "xiaohongshu"):
            self.state.complete_publish(job["job_id"], platform, receipt_id=f"verified-{platform}", verified=True, day="2026-10-09")
        next_job = self.reserve(artwork=456, day="2026-10-10")
        self.assertEqual(next_job["artwork_id"], "456")

    def test_xiaohongshu_only_job_requires_no_douyin_copy_or_receipt(self):
        self.state = State(self.db, clock=lambda: self.now, platforms=("xiaohongshu",))
        job = self.ready(pages=2)
        self.assertEqual(job["required_platforms"], ["xiaohongshu"])
        self.assertEqual(job["metadata"]["required_platforms"], ["xiaohongshu"])
        self.assertNotIn("douyin", job["publications"])
        self.assertTrue(self.state.publish_ready(job["job_id"])["ready"])
        self.state.claim_publish(job["job_id"], "xiaohongshu")
        self.state.complete_publish(job["job_id"], "xiaohongshu", receipt_id="real-xhs-post", verified=True)
        self.assertEqual(self.state.get_job(job["job_id"])["status"], "complete")

    def test_unassigned_platform_cannot_receive_copy_claim_or_receipt(self):
        self.state = State(self.db, clock=lambda: self.now, platforms=("xiaohongshu",))
        job = self.ready()
        with self.assertRaisesRegex(StateError, "not assigned"):
            self.state.record_copy(job["job_id"], "douyin", title="unused", body="unused")
        with self.assertRaisesRegex(StateError, "not assigned"):
            self.state.claim_publish(job["job_id"], "douyin")
        with self.assertRaisesRegex(StateError, "not assigned"):
            self.state.complete_publish(job["job_id"], "douyin", receipt_id="unused", verified=True)
        with self.assertRaisesRegex(StateError, "not assigned"):
            self.state.fail_publish(job["job_id"], "douyin", reason="unused")

    def test_platform_assignment_persists_when_constructor_configuration_changes(self):
        self.state = State(self.db, clock=lambda: self.now, platforms=("xiaohongshu",))
        job = self.ready()
        reopened = State(self.db, clock=lambda: self.now)
        self.assertEqual(reopened.get_job(job["job_id"])["required_platforms"], ["xiaohongshu"])
        self.assertTrue(reopened.publish_ready(job["job_id"])["ready"])
        reopened.claim_publish(job["job_id"], "xiaohongshu")
        reopened.complete_publish(job["job_id"], "xiaohongshu", receipt_id="verified-xhs", verified=True)
        self.assertEqual(reopened.get_job(job["job_id"])["status"], "complete")
        dual = reopened.reserve_daily([{"id": 456, "page_count": 1}], day="2026-10-10")
        xhs_config = State(self.db, clock=lambda: self.now, platforms=("xiaohongshu",))
        self.assertEqual(set(xhs_config.get_job(dual["job_id"])["required_platforms"]), {"douyin", "xiaohongshu"})

    def test_invalid_platform_configuration_is_rejected(self):
        for platforms in ((), ("unsupported",), ("xiaohongshu", "xiaohongshu"), "xiaohongshu"):
            with self.assertRaises(ValueError):
                State(self.db, platforms=platforms)

    def test_verified_published_ui_evidence_completes_without_inventing_post_id(self):
        self.state = State(self.db, clock=lambda: self.now, platforms=("xiaohongshu",))
        job = self.ready()
        self.state.claim_publish(job["job_id"], "xiaohongshu")
        result = self.state.complete_publish_from_ui(job["job_id"], "xiaohongshu", account_id="test-account",
                    title="Character portrait", published_at=self.now, evidence_path=self.picture("published-ui.png"), verified=True)
        self.assertEqual(result["verification_kind"], "published_ui_evidence")
        self.assertTrue(result["receipt_id"].startswith("ui-evidence:"))
        self.assertIsNone(result["post_id"])
        self.assertIsNone(result["post_url"])
        self.assertIsNone(result["receipt_url"])
        self.assertEqual(self.state.get_job(job["job_id"])["status"], "complete")
        self.assertIsNone(self.state.claim_publish(job["job_id"], "xiaohongshu"))
        reopened = State(self.db, clock=lambda: self.now)
        evidence = reopened.get_job(job["job_id"])["publications"]["xiaohongshu"]["receipt_evidence"]
        self.assertEqual(evidence["evidence_sha256"], result["receipt_id"].split(":", 1)[1])

    def test_ui_evidence_cannot_complete_pending_or_unverified_publication(self):
        job = self.ready()
        params = {"account_id": "test-account", "title": "Character portrait", "published_at": self.now,
                  "evidence_path": self.picture("published-ui.png")}
        with self.assertRaisesRegex(StateError, "Claim the platform"):
            self.state.complete_publish_from_ui(job["job_id"], "xiaohongshu", **params, verified=True)
        self.state.claim_publish(job["job_id"], "xiaohongshu")
        with self.assertRaisesRegex(StateError, "requires verified"):
            self.state.complete_publish_from_ui(job["job_id"], "xiaohongshu", **params, verified=False)
        self.assertEqual(self.state.get_job(job["job_id"])["publications"]["xiaohongshu"]["status"], "in_flight")

    def test_ui_evidence_rejects_mismatched_title_naive_time_and_missing_file(self):
        job = self.ready()
        self.state.claim_publish(job["job_id"], "xiaohongshu")
        params = {"account_id": "test-account", "title": "Character portrait", "published_at": self.now,
                  "evidence_path": self.picture("published-ui.png"), "verified": True}
        with self.assertRaisesRegex(StateError, "does not match"):
            self.state.complete_publish_from_ui(job["job_id"], "xiaohongshu", **{**params, "title": "Different post"})
        with self.assertRaisesRegex(StateError, "timezone"):
            self.state.complete_publish_from_ui(job["job_id"], "xiaohongshu", **{**params, "published_at": "2026-10-09T10:00:00"})
        with self.assertRaises(FileNotFoundError):
            self.state.complete_publish_from_ui(job["job_id"], "xiaohongshu", **{**params, "evidence_path": self.root / "missing.png"})

    def test_legacy_boolean_and_database_boolean_cannot_bypass_structured_gate(self):
        job = self.reserve()
        self.state.record_raw(job["job_id"], 0, self.picture("raw.png"))
        self.state.record_cos(job["job_id"], 0, self.picture("cos.png"), qa_passed=True)
        self.assertFalse(self.state.get_job(job["job_id"])["pages"][0]["qa_passed"])
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE pages SET qa_passed=1")
        self.assertFalse(self.state.publish_ready(job["job_id"])["ready"])
        with self.assertRaisesRegex(StateError, "structured feature"):
            self.state.claim_publish(job["job_id"], "xiaohongshu")

    def test_all_required_detailed_features_and_review_checks_are_required(self):
        job = self.reserve()
        job_id = job["job_id"]
        self.state.record_raw(job_id, 0, self.picture("raw.png"))
        features = self.features(job_id)
        del features["checks"]["hem_and_skin_gaps.skin_gaps"]
        with self.assertRaisesRegex(StateError, "every required detailed check"):
            self.state.record_features(job_id, 0, features)
        features = self.features(job_id)
        features["checks"]["clothing_structure.custom_cuff"] = deepcopy(features["checks"]["clothing_structure.sleeves"])
        self.state.record_features(job_id, 0, features)
        cos = self.picture("cos.png")
        self.state.record_cos(job_id, 0, cos, generator_agent_id="test-generator", generation_trace=self.trace(job_id, 0, cos))
        review = self.review(job_id)
        del review["checks"]["clothing_structure.custom_cuff"]
        with self.assertRaisesRegex(StateError, "exactly the original feature"):
            self.state.record_quality_review(job_id, 0, review)

    def test_visible_failure_unverifiable_and_not_applicable_each_block_publish(self):
        job = self.ready()
        for verdict in ("fail", "unverifiable", "not_applicable"):
            with self.subTest(verdict=verdict):
                report = self.review(job["job_id"])
                report["checks"]["hem_and_skin_gaps.skin_gaps"] = {"status": verdict, "evidence": "Visible waistband/skin gap differs or cannot be confirmed"}
                result = self.state.record_quality_review(job["job_id"], 0, report)
                self.assertFalse(result["passed"])
                self.assertIn("hem_and_skin_gaps.skin_gaps", ";".join(result["reasons"]))
                with self.assertRaises(StateError):
                    self.state.claim_publish(job["job_id"], "xiaohongshu")
        self.assertEqual(self.state.get_job(job["job_id"])["publications"]["xiaohongshu"]["attempt_count"], 0)

    def test_hidden_noncritical_features_allow_na_or_unverifiable_but_never_false_pass(self):
        job = self.reserve()
        job_id = job["job_id"]
        self.state.record_raw(job_id, 0, self.picture("raw.png"))
        features = self.features(job_id)
        key = "stocking_boundaries.upper_edges"
        features["checks"][key].update(visibility="not_visible", critical=False, expected="Upper edges hidden by skirt", evidence="Skirt occludes both upper edges")
        self.state.record_features(job_id, 0, features)
        cos = self.picture("cos.png")
        self.state.record_cos(job_id, 0, cos, generator_agent_id="test-generator", generation_trace=self.trace(job_id, 0, cos))
        for verdict, passed in (("not_applicable", True), ("unverifiable", True), ("pass", False)):
            report = self.review(job_id)
            report["checks"][key]["status"] = verdict
            self.assertEqual(self.state.record_quality_review(job_id, 0, report)["passed"], passed)

    def test_critical_unverifiable_source_cannot_be_approved(self):
        job = self.reserve()
        job_id = job["job_id"]
        self.state.record_raw(job_id, 0, self.picture("raw.png"))
        features = self.features(job_id)
        features["checks"]["clothing_structure.layers"]["visibility"] = "unverifiable"
        self.state.record_features(job_id, 0, features)
        self.state.record_cos(job_id, 0, self.picture("cos.png"), generator_agent_id="test-generator")
        report = self.review(job_id)
        report["checks"]["clothing_structure.layers"]["status"] = "not_applicable"
        self.assertFalse(self.state.record_quality_review(job_id, 0, report)["passed"])

    def test_three_roles_must_be_distinct_including_across_pages(self):
        job = self.ready(pages=2)
        for identifier in ("test-generator", "test-analyst", "legacy/unknown"):
            with self.assertRaises(StateError):
                self.state.record_quality_review(job["job_id"], 0, self.review(job["job_id"], reviewer=identifier))
        self.mark_quality_failure(job["job_id"])
        with self.assertRaisesRegex(StateError, "different fresh agent"):
            self.state.begin_correction(job["job_id"], 0, generator_agent_id="test-reviewer", optimization=self.optimization(job["job_id"]))
        with self.assertRaisesRegex(StateError, "different fresh agent"):
            self.state.begin_correction(job["job_id"], 0, generator_agent_id="test-generator", optimization=self.optimization(job["job_id"]))

    def test_review_must_bind_raw_cos_features_and_generation_version(self):
        job = self.ready()
        for field in ("raw_sha256", "cos_sha256", "features_sha256", "generation_sha256"):
            with self.subTest(field=field):
                report = self.review(job["job_id"])
                report[field] = "0" * 64
                with self.assertRaisesRegex(StateError, "does not match"):
                    self.state.record_quality_review(job["job_id"], 0, report)

    def test_multipage_identity_requires_all_hashes_and_current_versions(self):
        job = self.ready(pages=2)
        report = self.review(job["job_id"], 0)
        del report["identity_cos_sha256"]["1"]
        with self.assertRaisesRegex(StateError, "every current Cos page hash"):
            self.state.record_quality_review(job["job_id"], 0, report)
        old_report = self.review(job["job_id"], 0)
        self.correction(job["job_id"], index=1)
        pages = self.state.get_job(job["job_id"])["pages"]
        self.assertTrue(all(page["quality_review"] is None for page in pages))
        with self.assertRaisesRegex(StateError, "every current Cos page hash"):
            self.state.record_quality_review(job["job_id"], 0, old_report)
        with self.assertRaisesRegex(StateError, "different fresh agent"):
            self.state.record_quality_review(job["job_id"], 0, self.review(job["job_id"], 0))
        for index in range(2):
            self.state.record_quality_review(job["job_id"], index, self.review(job["job_id"], index, reviewer="new-independent-reviewer"))
        self.assertTrue(self.state.publish_ready(job["job_id"])["ready"])

    def test_same_bytes_correction_invalidates_old_generation_review(self):
        job = self.ready()
        old = self.review(job["job_id"])
        self.correction(job["job_id"], changed=False)
        self.state.record_quality_review(job["job_id"], 0, old)
        self.assertIsNone(self.state.get_job(job["job_id"])["pages"][0]["quality_review"])
        self.assertFalse(self.state.publish_ready(job["job_id"])["ready"])

    def test_edit_requires_prior_attempt_and_matching_attempt_identity(self):
        job = self.ready()
        path = self.root / "changed.png"
        Image.new("RGB", (22, 33)).save(path)
        with self.assertRaisesRegex(StateError, "Reserve a correction"):
            self.state.record_cos(job["job_id"], 0, path)
        self.mark_quality_failure(job["job_id"])
        attempt = self.state.begin_correction(job["job_id"], 0, generator_agent_id="new-editor", optimization=self.optimization(job["job_id"]))
        with self.assertRaisesRegex(StateError, "reserved correction attempt"):
            self.state.record_cos(job["job_id"], 0, path, generator_agent_id="new-editor", correction_attempt_id="wrong")
        self.assertEqual(self.state.get_job(job["job_id"])["pages"][0]["pending_correction"], attempt)

    def test_two_corrections_survive_restart_third_rejects_without_erasing_valid_pass(self):
        job = self.ready()
        for number in (1, 2):
            self.correction(job["job_id"], number=number)
            self.state.record_quality_review(job["job_id"], 0, self.review(job["job_id"], reviewer=f"reviewer-correction-{number}"))
            self.state = State(self.db, clock=lambda: self.now)
            self.assertTrue(self.state.publish_ready(job["job_id"])["ready"])
        with self.assertRaisesRegex(StateError, "at most two"):
            self.state.begin_correction(job["job_id"], 0, generator_agent_id="third-editor")
        reopened = State(self.db, clock=lambda: self.now)
        page = reopened.get_job(job["job_id"])["pages"][0]
        self.assertEqual(page["correction_count"], 2)
        self.assertFalse(page["quality_blocked"])
        self.assertTrue(reopened.publish_ready(job["job_id"])["ready"])
        self.assertEqual(reopened.claim_publish(job["job_id"], "xiaohongshu")["status"], "in_flight")

    def test_failed_generation_still_consumes_correction_budget(self):
        job = self.ready()
        self.mark_quality_failure(job["job_id"])
        optimization = self.optimization(job["job_id"])
        first = self.state.begin_correction(job["job_id"], 0, generator_agent_id="failed-editor-1", optimization=optimization)
        with self.assertRaisesRegex(StateError, "pending correction"):
            self.state.begin_correction(job["job_id"], 0, generator_agent_id="failed-editor-2", optimization=optimization)
        fail(self.state, self.root, job["job_id"], 0, first, "Synthetic revised prompt one", "Synthetic terminal tool failure")
        self.state = State(self.db, clock=lambda: self.now)
        second = self.state.begin_correction(job["job_id"], 0, generator_agent_id="failed-editor-2", optimization=optimization)
        self.assertEqual(second["attempt_number"], 2)
        with sqlite3.connect(self.db) as db:
            status = db.execute("SELECT status FROM quality_corrections WHERE attempt_id=?", (first["attempt_id"],)).fetchone()[0]
        self.assertEqual(status, "failed")
        with self.assertRaises(StateError):
            self.state.begin_correction(job["job_id"], 0, generator_agent_id="failed-editor-3")

    def test_legacy_reregistration_cannot_erase_provenance_or_unfreeze_features(self):
        job = self.ready()
        old_page = job["pages"][0]
        self.state.record_cos(job["job_id"], 0, old_page["cos"]["path"], qa_passed=True)
        page = self.state.get_job(job["job_id"])["pages"][0]
        self.assertEqual(page["generation"], old_page["generation"])
        self.assertFalse(page["qa_passed"])
        report = deepcopy(page["features"])
        report["checks"]["hem_and_skin_gaps.skin_gaps"]["expected"] = "Changed after generation"
        with self.assertRaisesRegex(StateError, "frozen after generation"):
            self.state.record_features(job["job_id"], 0, report)

    def test_additive_migration_preserves_legacy_published_rows_and_claim_skips(self):
        job = self.ready()
        for platform in job["publications"]:
            self.state.claim_publish(job["job_id"], platform)
            self.state.complete_publish(job["job_id"], platform, receipt_id=f"legacy-{platform}", verified=True)
        with sqlite3.connect(self.db) as db:
            db.execute("DROP TABLE quality_agents")
            db.execute("DROP TABLE quality_corrections")
            for column in ("features", "generation", "quality_review", "correction_count", "quality_blocked", "pending_correction"):
                db.execute(f"ALTER TABLE pages DROP COLUMN {column}")
            before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in ("jobs", "publications", "history", "publish_days")}
        with self.state._connect() as db:
            self.assertEqual(State._get_job(db, job["job_id"])["status"], "complete")
        restarted = State(self.db, clock=lambda: self.now)
        with sqlite3.connect(self.db) as db:
            after = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in before}
        self.assertEqual(before, after)
        self.assertEqual(restarted.get_job(job["job_id"])["status"], "complete")
        for platform in job["publications"]:
            self.assertIsNone(restarted.claim_publish(job["job_id"], platform))

    def test_missing_or_mismatched_actual_generation_trace_blocks_otherwise_passing_package(self):
        for case in ("missing", "prompt", "reference", "output", "generator", "attempt", "tool"):
            with self.subTest(case=case):
                self.state = State(self.root / f"trace-{case}.sqlite3", clock=lambda: self.now)
                job = self.reserve()
                job_id = job["job_id"]
                self.state.record_raw(job_id, 0, self.picture(f"trace-{case}-raw.png"))
                self.state.record_features(job_id, 0, self.features(job_id))
                cos = self.picture(f"trace-{case}-cos.png")
                trace = self.trace(job_id, 0, cos)
                if case == "missing":
                    trace = None
                elif case == "prompt":
                    trace["actual_prompt"] = "Changed prompt without updating its digest"
                elif case == "reference":
                    trace["references"][0]["sha256"] = "0" * 64
                elif case == "output":
                    trace["output_sha256"] = "0" * 64
                elif case == "generator":
                    trace["generator_agent_id"] = "different-generator"
                elif case == "attempt":
                    trace["attempt"] = 1
                elif case == "tool":
                    trace["tool"] = "unrecorded-tool"
                self.state.record_cos(job_id, 0, cos, generator_agent_id="test-generator", generation_trace=trace)
                self.state.record_research(job_id, character="Fixture", sources=[{"url": "https://example.org/fixture"}], verified=True)
                for platform in job["publications"]:
                    self.state.record_copy(job_id, platform, title="Fixture", body="Fixture only")
                result = self.state.record_quality_review(job_id, 0, self.review(job_id))
                self.assertFalse(result["passed"])
                self.assertFalse(self.state.publish_ready(job_id)["ready"])
                with self.assertRaises(StateError):
                    self.state.claim_publish(job_id, "xiaohongshu")

    def test_critical_hidden_unverifiable_review_explicitly_blocks(self):
        from p2cos_quality import verdict_reasons
        job = self.ready()
        features = deepcopy(job["pages"][0]["features"])
        review = self.review(job["job_id"])
        key = "stocking_boundaries.upper_edges"
        features["checks"][key].update(visibility="not_visible", critical=True)
        review["checks"][key]["status"] = "unverifiable"
        self.assertIn(f"{key} critical feature unverifiable", verdict_reasons(features, review, 1))

    def test_final_selector_identity_failure_cannot_label_single_page_passed(self):
        job = self.ready()
        manifest = self.state.quality_candidates(job["job_id"])
        candidate = manifest["pages"]["0"][0]
        report = {"schema_version": 3, "selector_agent_id": "new-selector", "reason": "Synthetic independent final comparison",
                  "candidate_set_sha256": manifest["candidate_set_sha256"],
                  "candidates": {"0": [{"generation_sha256": candidate["generation_sha256"], "review_sha256": candidate["review_sha256"], "score": candidate["score"], "rank": 1, "reason": "Only candidate"}]},
                  "selected": {"0": {"generation_sha256": candidate["generation_sha256"], "cos_sha256": candidate["cos_sha256"], "reason": "Only candidate", "unresolved_deviations": []}},
                  "identity_review": {"cos_sha256": {"0": candidate["cos_sha256"]}, "checks": {
                      key: {"status": "fail" if key == "identity.face" else "pass", "evidence": "Synthetic final identity assessment"}
                      for key in ("identity.face", "identity.hair", "identity.outfit")}}}
        with self.assertRaisesRegex(StateError, "identity requires exhausted"):
            self.state.select_best_available(job["job_id"], report)


if __name__ == "__main__":
    unittest.main()
