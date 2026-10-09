"""Offline policy tests using tiny synthetic images and a temporary SQLite DB.

These records describe test fixtures only. No image generation, network calls,
real publication, real credentials, or production state are involved.
"""

from copy import deepcopy
from datetime import datetime
import hashlib
import sqlite3
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from PIL import Image

from p2cos_quality import REQUIRED_CHECKS, digest, unresolved_checks, verdict_reasons
from p2cos_state import State, StateError, inspect_image
from recovery_test_fixtures import save_result, fail


FAILURES = ("clothing_structure.sleeves", "pose.arms_and_hands", "lighting.shadows")


class BestAvailableSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "synthetic-state.sqlite3"
        self.now = datetime(2026, 10, 9, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.state = State(self.db, clock=lambda: self.now)
        self.reports = {}

    def picture(self, filename, variation=0):
        path = self.root / filename
        Image.new("RGB", (12 + variation, 16), (20 + variation, 70, 120)).save(path)
        return path

    def page(self, job_id, index=0):
        return self.state.get_job(job_id)["pages"][index]

    def trace(self, job_id, index, path, generator, attempt=None, prompt=None):
        page = self.page(job_id, index)
        output = inspect_image(path)
        number = attempt["attempt_number"] if attempt else 0
        prompt = prompt or (
            f"Synthetic fixture only, page {index}, version {number}; no image generation occurred. "
            f"Correct sleeves, hand pose and shadow direction; revision {number}."
        )
        return {
            "tool": "image_gen.imagegen", "generator_agent_id": generator,
            "actual_prompt": prompt, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "references": [{"role": "current_page_original", "path": page["raw"]["path"],
                            "sha256": page["raw"]["sha256"]}],
            "output_path": output["path"], "output_sha256": output["sha256"],
            "returned_model": "undisclosed (synthetic fixture)", "attempt": number,
            "correction_attempt_id": attempt["attempt_id"] if attempt else None,
        }

    def review(self, job_id, index=0, failed=FAILURES[:1], reviewer=None, *, provisional=False):
        job = self.state.get_job(job_id)
        page = job["pages"][index]
        reviewer = reviewer or f"synthetic-reviewer-{page['correction_count']}"
        report = {
            "schema_version": 2, "reviewer_agent_id": reviewer,
            "raw_sha256": page["raw"]["sha256"], "cos_sha256": page["cos"]["sha256"],
            "features_sha256": page["features_sha256"],
            "generation_sha256": page["generation_sha256"],
            "identity_cos_sha256": {str(p["page_index"]): p["cos"]["sha256"]
                                     for p in job["pages"] if p["cos"]},
            "checks": {key: {"status": "fail" if key in failed else "pass",
                              "evidence": f"Synthetic comparison only: {key}"}
                       for key in page["features"]["checks"]},
        }
        self.state.record_quality_review(job_id, index, report, provisional=provisional)
        self.reports[page["generation_sha256"]] = deepcopy(report)
        return report

    def fixture(self, *, pages=1, failed=FAILURES[:1], package=True, unknown_trace=False):
        job = self.state.reserve_daily([{"id": 424242, "page_count": pages}], day="2026-10-09")
        job_id = job["job_id"]
        for index in range(pages):
            self.state.record_raw(job_id, index, self.picture(f"raw-{index}.png", index))
            page = self.page(job_id, index)
            features = {
                "schema_version": 2, "analyst_agent_id": "synthetic-analyst",
                "raw_sha256": page["raw"]["sha256"],
                "checks": {key: {"visibility": "visible", "critical": True,
                                  "expected": f"Synthetic source expectation: {key}",
                                  "evidence": f"Synthetic source region: {key}"}
                           for key in REQUIRED_CHECKS},
            }
            self.state.record_features(job_id, index, features)
            path = self.picture(f"cos-{index}-0.png", 3 + index)
            trace = self.trace(job_id, index, path, "synthetic-generator")
            if unknown_trace:
                trace["tool"] = "unknown"
            self.state.record_cos(job_id, index, path, generator_agent_id="synthetic-generator",
                                  generation_trace=trace)
        for index in range(pages):
            self.review(job_id, index, failed)
        if package:
            self.add_package(job_id)
        return job_id

    def add_package(self, job_id):
        self.state.record_research(job_id, character="Synthetic Fixture Character",
                                   sources=[{"url": "https://example.invalid/synthetic-source"}],
                                   verified=True, personality="Synthetic test description")
        for platform in self.state.get_job(job_id)["publications"]:
            self.state.record_copy(job_id, platform, title="Synthetic fixture", body="Offline test copy.")

    def optimization(self, job_id, index=0):
        page = self.page(job_id, index)
        report = self.reports[page["generation_sha256"]]
        return {
            "based_on_generation_sha256": page["generation_sha256"],
            "review_sha256": digest(report),
            "target_checks": unresolved_checks(page["features"], report),
            "changes": "Synthetic optimization: specify sleeve cut, hand orientation and shadow direction.",
        }

    def begin(self, job_id, index=0):
        number = self.page(job_id, index)["correction_count"] + 1
        return self.state.begin_correction(
            job_id, index, generator_agent_id=f"synthetic-editor-{index}-{number}",
            optimization=self.optimization(job_id, index),
        )

    def correct(self, job_id, index=0, failed=FAILURES[:1], *, provisional=False):
        attempt = self.begin(job_id, index)
        number = attempt["attempt_number"]
        generator = attempt["generator_agent_id"]
        path = self.picture(f"cos-{index}-{number}.png", 5 + index + number)
        save_result(self.state, self.root, job_id, index, attempt,
                    self.trace(job_id, index, path, generator, attempt))
        self.state.record_cos(job_id, index, path, generator_agent_id=generator,
                              correction_attempt_id=attempt["attempt_id"],
                              generation_trace=self.trace(job_id, index, path, generator, attempt))
        self.review(job_id, index, failed, provisional=provisional)
        return attempt

    def exhausted(self, *, package=True):
        job_id = self.fixture(package=package)
        # Later versions deliberately worsen, so the original must remain selectable.
        self.correct(job_id, failed=FAILURES[:2])
        self.correct(job_id, failed=FAILURES)
        return job_id

    def selection(self, job_id, selector="synthetic-independent-selector"):
        manifest = self.state.quality_candidates(job_id)
        report = {
            "schema_version": 3, "selector_agent_id": selector,
            "candidate_set_sha256": manifest["candidate_set_sha256"],
            "candidates": {}, "selected": {},
            "identity_review": {"cos_sha256": {}, "checks": {
                key: {"status": "pass", "evidence": f"Synthetic selected-page comparison: {key}"}
                for key in ("identity.face", "identity.hair", "identity.outfit")}},
            "reason": "Synthetic complete comparison; passing versions first, then highest recorded score.",
        }
        for index, candidates in manifest["pages"].items():
            eligible = sorted((c for c in candidates if c["eligible"]),
                              key=lambda c: (not c["visual_passed"], -c["score"], c["attempt"]))
            report["candidates"][index] = [
                {"generation_sha256": c["generation_sha256"], "review_sha256": c["review_sha256"],
                 "score": c["score"], "rank": rank,
                 "reason": f"Synthetic evidence comparison, deterministic rank {rank}."}
                for rank, c in enumerate(eligible, 1)
            ]
            if not eligible:
                continue
            best = eligible[0]
            qa = self.reports[best["generation_sha256"]]
            residuals = verdict_reasons(self.page(job_id, int(index))["features"], qa,
                                       self.state.get_job(job_id)["page_count"])
            report["selected"][index] = {
                "generation_sha256": best["generation_sha256"], "cos_sha256": best["cos_sha256"],
                "reason": "Best eligible synthetic candidate after comparing every recorded version.",
                "unresolved_deviations": residuals,
            }
            report["identity_review"]["cos_sha256"][index] = best["cos_sha256"]
        return report

    def test_visual_failure_cannot_select_before_two_terminal_retries(self):
        job_id = self.fixture()
        for completed in (0, 1):
            with self.subTest(completed=completed):
                with self.assertRaises(StateError):
                    self.state.select_best_available(job_id, self.selection(job_id))
                self.assertFalse(self.state.publish_ready(job_id)["ready"])
            if completed == 0:
                self.correct(job_id)
        pending = self.begin(job_id)
        self.assertEqual(pending["attempt_number"], 2)
        with self.assertRaises(StateError):
            self.state.select_best_available(job_id, self.selection(job_id))
        with self.assertRaises(StateError):
            self.begin(job_id)

    def test_optimization_must_bind_failure_and_describe_changes(self):
        job_id = self.fixture()
        original = self.optimization(job_id)
        changes = (
            {"based_on_generation_sha256": "0" * 64},
            {"review_sha256": "0" * 64},
            {"target_checks": []},
            {"target_checks": ["composition.framing"]},
            {"changes": "   "},
        )
        for invalid in changes:
            with self.subTest(invalid=invalid):
                optimization = dict(original, **invalid)
                with self.assertRaises(StateError):
                    self.state.begin_correction(job_id, 0, generator_agent_id="synthetic-new-editor",
                                                optimization=optimization)
                self.assertEqual(self.page(job_id)["correction_count"], 0)

    def test_unchanged_prompt_is_not_a_valid_retry_or_failed_attempt(self):
        job_id = self.fixture()
        old_prompt = self.page(job_id)["generation"]["trace"]["actual_prompt"]
        attempt = self.begin(job_id)
        generator = attempt["generator_agent_id"]
        path = self.picture("changed-image-unchanged-prompt.png", 11)
        with self.assertRaises(StateError):
            self.state.record_cos(job_id, 0, path, generator_agent_id=generator,
                                  correction_attempt_id=attempt["attempt_id"],
                                  generation_trace=self.trace(job_id, 0, path, generator, attempt, old_prompt))
        with self.assertRaises(StateError):
            self.state.fail_correction(job_id, 0, attempt_id=attempt["attempt_id"],
                                       actual_prompt=old_prompt, reason="Synthetic generation tool failure.")
        self.assertEqual(self.page(job_id)["pending_correction"]["attempt_id"], attempt["attempt_id"])

    def test_failed_generation_attempts_require_evidence_and_are_terminal(self):
        job_id = self.fixture()
        for number in (1, 2):
            attempt = self.begin(job_id)
            prompt = f"Synthetic optimized prompt {number}: repair sleeve outline and hand orientation."
            with self.assertRaises(StateError):
                self.state.fail_correction(job_id, 0, attempt_id=attempt["attempt_id"],
                                           actual_prompt=prompt, reason="")
            fail(self.state, self.root, job_id, 0, attempt, prompt, "Synthetic terminal tool failure.")
            self.assertIsNone(self.page(job_id)["pending_correction"])
            self.state = State(self.db, clock=lambda: self.now)
        self.state.select_best_available(job_id, self.selection(job_id))
        self.assertTrue(self.state.publish_ready(job_id)["ready"])
        self.assertFalse(self.page(job_id)["qa_passed"])
        with sqlite3.connect(self.db) as connection:
            statuses = connection.execute("SELECT status FROM quality_corrections ORDER BY attempt_number").fetchall()
        self.assertEqual(statuses, [("failed",), ("failed",)])

    def test_third_retry_is_rejected_without_erasing_three_versions(self):
        job_id = self.exhausted()
        before = self.state.quality_candidates(job_id)
        with self.assertRaises(StateError):
            self.begin(job_id)
        self.state = State(self.db, clock=lambda: self.now)
        after = self.state.quality_candidates(job_id)
        self.assertEqual(len(after["pages"]["0"]), 3)
        self.assertEqual({c["generation_sha256"] for c in before["pages"]["0"]},
                         {c["generation_sha256"] for c in after["pages"]["0"]})
        self.assertEqual(self.page(job_id)["correction_count"], 2)

    def test_repeated_failed_prompt_cannot_exhaust_retry_budget(self):
        job_id = self.fixture()
        prompt = "Synthetic revised prompt: specify sleeve geometry and hand orientation."
        first = self.begin(job_id)
        fail(self.state, self.root, job_id, 0, first, prompt, "Synthetic terminal tool failure.")
        second = self.begin(job_id)
        with self.assertRaises(StateError):
            fail(self.state, self.root, job_id, 0, second, prompt, "Synthetic second terminal tool failure.")
        self.assertEqual(self.page(job_id)["pending_correction"]["attempt_id"], second["attempt_id"])
        with self.assertRaises(StateError):
            self.state.select_best_available(job_id, self.selection(job_id))

    def test_provisional_retry_review_cannot_satisfy_final_quality_requirements(self):
        job_id = self.fixture()
        self.correct(job_id, provisional=True)
        provisional_version = self.page(job_id)["generation_sha256"]
        self.correct(job_id)
        candidates = self.state.quality_candidates(job_id)["pages"]["0"]
        provisional = next(c for c in candidates if c["generation_sha256"] == provisional_version)
        self.assertFalse(provisional["eligible"])
        with self.assertRaises(StateError):
            self.state.select_best_available(job_id, self.selection(job_id))
        self.assertFalse(self.state.publish_ready(job_id)["ready"])

    def test_historical_best_is_selected_without_rewriting_failed_qa(self):
        job_id = self.exhausted()
        report = self.selection(job_id)
        chosen = report["selected"]["0"]
        current_hash = self.page(job_id)["cos"]["sha256"]
        self.assertNotEqual(chosen["cos_sha256"], current_hash)
        manifest_before = self.state.quality_candidates(job_id)
        self.state.select_best_available(job_id, report)
        self.state = State(self.db, clock=lambda: self.now)
        readiness = self.state.publish_ready(job_id)
        page = self.page(job_id)
        self.assertTrue(readiness["ready"], readiness)
        self.assertFalse(page["qa_passed"])
        self.assertEqual(page["cos"]["sha256"], chosen["cos_sha256"])
        self.assertEqual(page["quality_review"]["checks"][FAILURES[0]]["status"], "fail")
        saved = self.state.get_job(job_id)["quality_selection"]
        self.assertEqual(saved["quality_override"], "best_available_after_max_retries")
        self.assertIs(saved["quality_passed"], False)
        self.assertEqual(saved["selected"]["0"]["unresolved_deviations"], chosen["unresolved_deviations"])
        manifest_after = self.state.quality_candidates(job_id)
        self.assertEqual({c["generation_sha256"] for c in manifest_before["pages"]["0"]},
                         {c["generation_sha256"] for c in manifest_after["pages"]["0"]})

    def test_passing_retry_is_preferred_and_does_not_need_budget_exhaustion(self):
        job_id = self.fixture()
        self.correct(job_id, failed=())
        passed_hash = self.page(job_id)["generation_sha256"]
        report = self.selection(job_id)
        self.assertEqual(report["selected"]["0"]["generation_sha256"], passed_hash)
        wrong = deepcopy(report)
        wrong["candidates"]["0"].reverse()
        for rank, candidate in enumerate(wrong["candidates"]["0"], 1):
            candidate["rank"] = rank
        with self.assertRaises(StateError):
            self.state.select_best_available(job_id, wrong)
        self.state.select_best_available(job_id, report)
        self.assertTrue(self.state.publish_ready(job_id)["ready"])
        self.assertTrue(self.page(job_id)["qa_passed"])
        self.assertEqual(self.page(job_id)["correction_count"], 1)

    def test_selector_must_cover_every_candidate_and_bind_exact_evidence(self):
        job_id = self.exhausted()
        report = self.selection(job_id)
        mutations = {}
        omitted = deepcopy(report)
        omitted["candidates"]["0"].pop()
        mutations["omitted eligible candidate"] = omitted
        changed = deepcopy(report)
        changed["selected"]["0"]["cos_sha256"] = "0" * 64
        mutations["changed selected hash"] = changed
        stale = deepcopy(report)
        stale["candidate_set_sha256"] = "0" * 64
        mutations["stale candidate set"] = stale
        altered = deepcopy(report)
        altered["candidates"]["0"][0]["score"] = 10000
        mutations["invented score"] = altered
        review = deepcopy(report)
        review["candidates"]["0"][0]["review_sha256"] = "0" * 64
        mutations["changed independent review"] = review
        for label, invalid in mutations.items():
            with self.subTest(label=label):
                with self.assertRaises(StateError):
                    self.state.select_best_available(job_id, invalid)
        self.assertIsNone(self.state.get_job(job_id)["quality_selection"])

    def test_selector_cannot_reuse_analyst_generator_or_reviewer(self):
        job_id = self.exhausted()
        for selector in ("synthetic-analyst", "synthetic-generator", "synthetic-editor-0-1",
                         "synthetic-reviewer-2", "legacy/unknown"):
            with self.subTest(selector=selector):
                with self.assertRaises(StateError):
                    self.state.select_best_available(job_id, self.selection(job_id, selector))

    def test_final_identity_must_cover_selected_hashes_for_every_page(self):
        job_id = self.fixture(pages=2, failed=())
        report = self.selection(job_id)
        missing_page = deepcopy(report)
        del missing_page["selected"]["1"]
        missing_identity = deepcopy(report)
        del missing_identity["identity_review"]["cos_sha256"]["1"]
        wrong_identity = deepcopy(report)
        wrong_identity["identity_review"]["cos_sha256"]["0"] = "0" * 64
        for invalid in (missing_page, missing_identity, wrong_identity):
            with self.subTest(invalid=invalid):
                with self.assertRaises(StateError):
                    self.state.select_best_available(job_id, invalid)

    def test_override_does_not_waive_research_or_platform_copy(self):
        job_id = self.exhausted(package=False)
        self.state.select_best_available(job_id, self.selection(job_id))
        readiness = self.state.publish_ready(job_id)
        self.assertFalse(readiness["ready"])
        self.assertTrue(any("research" in reason for reason in readiness["reasons"]))
        self.assertTrue(any("copy" in reason for reason in readiness["reasons"]))
        self.add_package(job_id)
        self.assertTrue(self.state.publish_ready(job_id)["ready"])

    def test_selected_image_missing_or_changed_still_blocks_publication(self):
        job_id = self.exhausted()
        self.state.select_best_available(job_id, self.selection(job_id))
        path = Path(self.page(job_id)["cos"]["path"])
        original = path.read_bytes()
        for replacement in (None, b"synthetic corrupted PNG"):
            with self.subTest(replacement=replacement):
                if replacement is None:
                    path.unlink()
                else:
                    path.write_bytes(replacement)
                self.assertFalse(self.state.publish_ready(job_id)["ready"])
                with self.assertRaises(StateError):
                    self.state.claim_publish(job_id, "xiaohongshu")
                path.write_bytes(original)

    def test_unknown_generation_trace_never_becomes_eligible(self):
        job_id = self.fixture(unknown_trace=True)
        for number in (1, 2):
            attempt = self.begin(job_id)
            fail(self.state, self.root, job_id, 0, attempt,
                 f"Synthetic optimized failed prompt {number}: repair sleeve outline.", "Synthetic terminal tool failure.")
        manifest = self.state.quality_candidates(job_id)
        self.assertTrue(manifest["pages"]["0"])
        self.assertTrue(all(not candidate["eligible"] for candidate in manifest["pages"]["0"]))
        with self.assertRaises(StateError):
            self.state.select_best_available(job_id, self.selection(job_id))
        self.assertFalse(self.state.publish_ready(job_id)["ready"])

    def test_published_history_and_receipts_are_not_rewritten_by_selection(self):
        job_id = self.exhausted()
        self.state.select_best_available(job_id, self.selection(job_id))
        for platform in self.state.get_job(job_id)["publications"]:
            self.state.claim_publish(job_id, platform)
            self.state.complete_publish(job_id, platform, receipt_id=f"synthetic-receipt-{platform}",
                                        verified=True)
        with sqlite3.connect(self.db) as connection:
            before = {table: connection.execute(f"SELECT * FROM {table}").fetchall()
                      for table in ("jobs", "publications", "history", "publish_days", "quality_selections")}
        with self.assertRaises(StateError):
            self.state.select_best_available(job_id, self.selection(job_id, "synthetic-another-selector"))
        self.state = State(self.db, clock=lambda: self.now)
        for platform in self.state.get_job(job_id)["publications"]:
            self.assertIsNone(self.state.claim_publish(job_id, platform))
        with sqlite3.connect(self.db) as connection:
            after = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in before}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
