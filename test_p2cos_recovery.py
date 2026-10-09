"""Crash/replay tests use only isolated temporary DBs and synthetic receipts."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from p2cos_recovery import inspect_recovery
from p2cos_state import State, StateError
from recovery_test_fixtures import receipt, fail
import test_p2cos_selection as selection_fixtures


class RecoveryTests(unittest.TestCase):
    # Reuse only fixture builders, not the other class's test cases.
    setUp = selection_fixtures.BestAvailableSelectionTests.setUp
    picture = selection_fixtures.BestAvailableSelectionTests.picture
    page = selection_fixtures.BestAvailableSelectionTests.page
    trace = selection_fixtures.BestAvailableSelectionTests.trace
    fixture = selection_fixtures.BestAvailableSelectionTests.fixture
    add_package = selection_fixtures.BestAvailableSelectionTests.add_package
    review = selection_fixtures.BestAvailableSelectionTests.review
    optimization = selection_fixtures.BestAvailableSelectionTests.optimization
    begin = selection_fixtures.BestAvailableSelectionTests.begin
    selection = selection_fixtures.BestAvailableSelectionTests.selection

    def start(self):
        job = self.fixture()
        attempt = self.begin(job)
        args = {"attempt_id": attempt["attempt_id"], "generator_agent_id": attempt["generator_agent_id"]}
        path = self.picture("recovered-result.png", 9)
        trace = self.trace(job, 0, path, attempt["generator_agent_id"], attempt)
        return job, attempt, args, trace

    def prepare(self, job, args, trace):
        return self.state.prepare_correction(job, 0, **args, actual_prompt=trace["actual_prompt"], references=trace["references"])

    def result_receipt(self, job, attempt, trace):
        return receipt(self.root, job, 0, attempt, "tool_result", state=self.state,
                       tool_result={"fixture": "synthetic successful tool return"},
                       prompt_sha256=trace["prompt_sha256"], output_sha256=trace["output_sha256"])

    def phase(self, job):
        return inspect_recovery(self.db, job)["attempts"][0]["phase"]

    def restart(self):
        self.state = State(self.db, clock=lambda: self.now)

    def make_legacy(self, attempt):
        # An isolated pre-journal DB, not fabricated production evidence.
        with sqlite3.connect(self.db) as db:
            db.execute("DROP TABLE correction_recovery_events")
            db.execute("ALTER TABLE quality_corrections DROP COLUMN recovery")

    def test_reserved_prepared_restart_and_concurrent_dispatch_claim(self):
        job, attempt, args, trace = self.start()
        self.restart()
        self.assertEqual(self.phase(job), "reserved")
        first = self.prepare(job, args, trace)
        self.restart()
        self.assertEqual(self.prepare(job, args, trace), first)
        self.assertEqual(self.phase(job), "prepared")
        def claim(_):
            try:
                return self.state.claim_correction_dispatch(job, 0, **args)
            except StateError:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(claim, range(4)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(next(r for r in results if r)["tool_arguments"]["prompt"], trace["actual_prompt"])
        self.restart()
        self.assertEqual(self.phase(job), "dispatching")
        with self.assertRaises(StateError):
            self.state.claim_correction_dispatch(job, 0, **args)
        self.assertEqual(self.page(job)["correction_count"], 1)
        self.assertFalse(self.state.publish_ready(job)["ready"])

    def test_prepared_references_are_rechecked_at_claim(self):
        job, attempt, args, trace = self.start()
        self.prepare(job, args, trace)
        Path(trace["references"][0]["path"]).write_bytes(b"changed source")
        with self.assertRaises(StateError):
            self.state.claim_correction_dispatch(job, 0, **args)
        self.assertEqual(self.phase(job), "prepared")

    def test_baseline_candidate_and_metadata_are_rechecked_before_dispatch(self):
        for target in ("candidate_file", "features", "generation", "cos"):
            with self.subTest(target=target):
                self.setUp()
                job, attempt, args, trace = self.start()
                self.prepare(job, args, trace)
                if target == "candidate_file":
                    from PIL import Image
                    Image.new("RGB", (9, 9), (1, 2, 3)).save(self.page(job)["cos"]["path"])
                else:
                    with sqlite3.connect(self.db) as db:
                        value = json.loads(db.execute(f"SELECT {target} FROM pages WHERE job_id=?", (job,)).fetchone()[0])
                        if target == "cos":
                            value["sha256"] = "0" * 64
                        else:
                            value["tampered_fixture"] = True
                        db.execute(f"UPDATE pages SET {target}=? WHERE job_id=?", (json.dumps(value), job))
                with self.assertRaises(StateError):
                    self.state.claim_correction_dispatch(job, 0, **args)
                self.assertEqual(self.phase(job), "prepared")

    def test_fresh_executor_reassignment_is_only_for_never_dispatched_reservations(self):
        job, attempt, args, trace = self.start()
        self.prepare(job, args, trace)
        with self.assertRaises(StateError):
            self.state.reassign_unsubmitted_correction(job, 0, **args, new_generator_agent_id="synthetic-analyst",
                operator_agent_id="synthetic-recovery-operator", reason="Synthetic executor change")
        reassigned = self.state.reassign_unsubmitted_correction(job, 0, **args,
            new_generator_agent_id="synthetic-fresh-generator", operator_agent_id="synthetic-recovery-operator",
            reason="Original synthetic executor stopped before any dispatch claim")
        self.assertEqual(reassigned["attempt_id"], attempt["attempt_id"])
        self.assertEqual(self.page(job)["correction_count"], 1)
        self.assertEqual(self.phase(job), "reserved")
        with self.assertRaises(StateError):
            self.prepare(job, args, trace)
        args["generator_agent_id"] = reassigned["generator_agent_id"]
        self.prepare(job, args, trace)
        self.state.claim_correction_dispatch(job, 0, **args)
        with self.assertRaises(StateError):
            self.state.reassign_unsubmitted_correction(job, 0, **args,
                new_generator_agent_id="synthetic-another-generator", operator_agent_id="synthetic-recovery-operator",
                reason="Cannot reassign dispatched fixture")

    def test_submission_and_unknown_do_not_authorize_failure_or_retry(self):
        job, attempt, args, trace = self.start()
        self.prepare(job, args, trace)
        self.state.claim_correction_dispatch(job, 0, **args)
        self.state.note_correction_submission(job, 0, **args, cell_id="synthetic-running-cell")
        self.restart()
        self.assertEqual(self.phase(job), "submitted")
        with self.assertRaises(StateError):
            self.state.fail_correction(job, 0, **args, actual_prompt=trace["actual_prompt"], reason="timeout/no files")
        with self.assertRaises(StateError):
            self.state.claim_correction_dispatch(job, 0, **args)
        self.assertFalse(self.state.publish_ready(job)["ready"])

    def test_result_saved_crash_and_concurrent_commits_are_idempotent(self):
        job, attempt, args, trace = self.start()
        self.prepare(job, args, trace)
        self.state.claim_correction_dispatch(job, 0, **args)
        evidence = self.result_receipt(job, attempt, trace)
        # Before save, even an existing image is not evidence of a completed call.
        self.restart()
        self.assertEqual(self.phase(job), "dispatching")
        saved = self.state.save_correction_result(job, 0, **args, generation_trace=trace, evidence=evidence)
        original_version = self.page(job)["generation_sha256"]
        self.restart()
        self.assertEqual(self.phase(job), "result_saved")
        self.assertEqual(self.state.save_correction_result(job, 0, **args, generation_trace=trace, evidence=evidence), saved)
        # A crash inside the page/version transaction rolls back the page and journal.
        with patch.object(self.state, "_save_version", side_effect=RuntimeError("synthetic process crash")):
            with self.assertRaises(RuntimeError):
                self.state.commit_correction_result(job, 0, **args)
        # A real process exit must also roll back SQLite's uncommitted page and
        # journal updates, including when Python cannot run context cleanup.
        crashed = subprocess.run([sys.executable, "-c",
            "import os,sys,json; from p2cos_state import State; s=State(sys.argv[1]); "
            "s._save_version=lambda *a,**k: os._exit(73); "
            "s.commit_correction_result(sys.argv[2],0,**json.loads(sys.argv[3]))",
            str(self.db), job, json.dumps(args)], capture_output=True)
        self.assertEqual(crashed.returncode, 73, crashed.stderr.decode())
        self.restart()
        self.assertEqual(self.page(job)["generation_sha256"], original_version)
        self.assertEqual(self.phase(job), "result_saved")
        with ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(pool.map(lambda _: self.state.commit_correction_result(job, 0, **args), range(4)))
        self.assertTrue(all(o == outputs[0] for o in outputs))
        self.restart()
        self.assertEqual(self.state.commit_correction_result(job, 0, **args), outputs[0])
        self.assertEqual(self.phase(job), "recorded")
        self.assertEqual(self.page(job)["correction_count"], 1)
        self.assertIsNone(self.page(job)["quality_review"])
        self.assertFalse(self.state.publish_ready(job)["ready"])
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM quality_versions WHERE job_id=?", (job,)).fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM correction_recovery_events WHERE event='recorded'").fetchone()[0], 1)
        Path(trace["references"][0]["path"]).write_bytes(b"changed after commit")
        with self.assertRaises(StateError):
            self.state.commit_correction_result(job, 0, **args)

    def test_legacy_migration_read_only_cli_and_real_result_import(self):
        job, attempt, args, trace = self.start()
        self.make_legacy(attempt)
        before = self.db.read_bytes()
        inspected = inspect_recovery(self.db, job)
        self.assertTrue(inspected["read_only"])
        self.assertEqual(inspected["attempts"][0]["phase"], "unknown")
        subprocess.run([sys.executable, "p2cos_recovery.py", "--db", str(self.db), "--job-id", job],
                       check=True, capture_output=True)
        self.assertEqual(self.db.read_bytes(), before)
        self.restart()
        self.assertEqual(self.phase(job), "unknown")
        with self.assertRaises(StateError):
            self.state.reassign_unsubmitted_correction(job, 0, **args,
                new_generator_agent_id="synthetic-replacement", operator_agent_id="synthetic-recovery-operator",
                reason="Legacy unknown must not be reassigned")
        with self.assertRaises(StateError):
            self.prepare(job, args, trace)
        evidence = self.result_receipt(job, attempt, trace)
        self.state.save_correction_result(job, 0, **args, generation_trace=trace, evidence=evidence)
        self.state.commit_correction_result(job, 0, **args)
        page = self.page(job)
        self.assertEqual(page["generation"]["generator_agent_id"], attempt["generator_agent_id"])
        with sqlite3.connect(self.db) as db:
            journal = json.loads(db.execute("SELECT recovery FROM quality_corrections").fetchone()[0])
        self.assertEqual(journal["result"]["evidence"]["content"]["operator_agent_id"], "synthetic-receipt-operator")
        self.assertFalse(self.state.publish_ready(job)["ready"])

    def test_evidence_agent_and_hash_mismatch_and_direct_bypass_rejected(self):
        job, attempt, args, trace = self.start()
        self.prepare(job, args, trace)
        self.state.claim_correction_dispatch(job, 0, **args)
        evidence = self.result_receipt(job, attempt, trace)
        for bad_args, bad_evidence, bad_trace in (
            ({**args, "generator_agent_id": "wrong-agent"}, evidence, trace),
            (args, {**evidence, "sha256": "0" * 64}, trace),
            (args, evidence, {**trace, "output_sha256": "0" * 64}),
            (args, evidence, {**trace, "generator_agent_id": "wrong-agent"}),
        ):
            with self.assertRaises(StateError):
                self.state.save_correction_result(job, 0, **bad_args, generation_trace=bad_trace, evidence=bad_evidence)
        with self.assertRaises(StateError):
            self.state.record_cos(job, 0, trace["output_path"], generator_agent_id=args["generator_agent_id"],
                                  correction_attempt_id=args["attempt_id"], generation_trace=trace)
        self.assertEqual(self.phase(job), "dispatching")

    def test_binary_result_is_separate_hash_bound_artifact(self):
        job, attempt, args, trace = self.start()
        self.prepare(job, args, trace)
        self.state.claim_correction_dispatch(job, 0, **args)
        fields = {"prompt_sha256": trace["prompt_sha256"], "output_sha256": trace["output_sha256"]}
        evidence = receipt(self.root, job, 0, attempt, "tool_result", state=self.state,
                           tool_result={"image_url": "data:image/png;base64,c3ludGhldGljLWZpeHR1cmU="}, **fields)
        with self.assertRaises(StateError):
            self.state.save_correction_result(job, 0, **args, generation_trace=trace, evidence=evidence)
        artifact = self.root / "synthetic-tool-result.json"
        artifact.write_text('{"fixture": "synthetic raw tool result"}')
        artifact_ref = {"path": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
        evidence = receipt(self.root, job, 0, attempt, "tool_result", state=self.state,
                           tool_result={}, raw_result_artifact={**artifact_ref, "sha256": "0" * 64}, **fields)
        with self.assertRaises(StateError):
            self.state.save_correction_result(job, 0, **args, generation_trace=trace, evidence=evidence)
        evidence = receipt(self.root, job, 0, attempt, "tool_result", state=self.state,
                           tool_result={}, raw_result_artifact=artifact_ref, **fields)
        self.state.save_correction_result(job, 0, **args, generation_trace=trace, evidence=evidence)
        self.assertEqual(self.phase(job), "result_saved")

    def test_changed_output_or_reference_between_save_and_commit_is_blocked(self):
        for target in ("output", "reference"):
            with self.subTest(target=target):
                self.setUp()
                job, attempt, args, trace = self.start()
                self.prepare(job, args, trace)
                self.state.claim_correction_dispatch(job, 0, **args)
                self.state.save_correction_result(job, 0, **args, generation_trace=trace,
                                                  evidence=self.result_receipt(job, attempt, trace))
                filename = trace["output_path"] if target == "output" else trace["references"][0]["path"]
                from PIL import Image
                Image.new("RGB", (9, 9), (2, 4, 8)).save(filename)
                with self.assertRaises(StateError):
                    self.state.commit_correction_result(job, 0, **args)
                self.assertEqual(self.phase(job), "result_saved")

    def test_non_submission_evidence_is_not_replayable_across_dispatches(self):
        job, attempt, args, trace = self.start()
        self.prepare(job, args, trace)
        first = self.state.claim_correction_dispatch(job, 0, **args)
        self.state.note_correction_submission(job, 0, **args, cell_id="synthetic-cell-old")
        evidence = receipt(self.root, job, 0, attempt, "authoritative_not_submitted", state=self.state,
                           outcome="not_submitted", original_executor_stopped=True,
                           authority="Synthetic authoritative fixture; no real tool involved.")
        self.state.resolve_correction_unknown(job, 0, **args, resolution="not_submitted", evidence=evidence)
        self.assertEqual(self.page(job)["correction_count"], 1)
        self.prepare(job, args, trace)
        with self.assertRaises(StateError):
            self.state.reassign_unsubmitted_correction(job, 0, **args,
                new_generator_agent_id="synthetic-replacement", operator_agent_id="synthetic-recovery-operator",
                reason="A previous dispatch claim remains in immutable history")
        second = self.state.claim_correction_dispatch(job, 0, **args)
        self.assertNotEqual(first["local_request_id"], second["local_request_id"])
        self.assertIsNone(inspect_recovery(self.db, job)["attempts"][0]["submission"])
        with self.assertRaises(StateError):
            self.state.resolve_correction_unknown(job, 0, **args, resolution="not_submitted", evidence=evidence)
        self.state.note_correction_submission(job, 0, **args, cell_id="synthetic-cell-new")

    def test_abandonment_cannot_exhaust_real_retry_budget_or_waive_qa(self):
        job, attempt, args, trace = self.start()
        self.prepare(job, args, trace)
        self.state.claim_correction_dispatch(job, 0, **args)
        evidence = receipt(self.root, job, 0, attempt, "operator_abandon_unknown", state=self.state,
                           accept_unknown_outcome=True)
        saved = self.state.resolve_correction_unknown(job, 0, **args, resolution="abandon_unknown", evidence=evidence)
        self.assertEqual(self.state.resolve_correction_unknown(job, 0, **args, resolution="abandon_unknown", evidence=evidence), saved)
        second = self.begin(job)
        fail(self.state, self.root, job, 0, second, "Synthetic second optimized prompt", "Synthetic terminal failure")
        self.assertEqual(self.page(job)["correction_count"], 2)
        with self.assertRaises(StateError):
            self.state.select_best_available(job, self.selection(job))
        with self.assertRaises(StateError):
            self.begin(job)
        self.assertFalse(self.state.publish_ready(job)["ready"])

    def test_legacy_reason_only_failures_do_not_grant_degraded_selection(self):
        job = self.fixture()
        for number in (1, 2):
            attempt = self.begin(job)
            fail(self.state, self.root, job, 0, attempt, f"Synthetic optimized failed prompt {number}", "Synthetic terminal failure")
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE quality_corrections SET recovery=NULL")
        with self.assertRaises(StateError):
            self.state.select_best_available(job, self.selection(job))
        self.assertFalse(self.state.publish_ready(job)["ready"])


if __name__ == "__main__":
    unittest.main()
