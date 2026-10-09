"""Read-only run health. Advice never dispatches, creates a run or authorizes publication."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
UNKNOWN = {"unknown", "dispatching", "submitted"}
PUBLICATION_UNKNOWN = {"in_flight", "reconciliation_required", "unknown", "under_review"}


def _object(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _stamp(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
    except (TypeError, ValueError):
        return None


def _age(value, now):
    stamp = _stamp(value)
    return max(0, int((now - stamp).total_seconds())) if stamp else None


def _artifact(ref, kind):
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise ValueError("artifact reference requires path and sha256")
    raw = Path(ref["path"]).read_bytes()
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise ValueError("artifact size or hash mismatch")
    body = json.loads(raw)
    if not isinstance(body, dict) or body.get("kind") != kind:
        raise ValueError("artifact kind mismatch")
    return body


def _policy(config, run_id):
    settings = config.get("recovery", {})
    if settings.get("auto_unknown_recovery_enabled") is not True:
        return {"valid": False, "reason": "automatic_policy_disabled"}
    try:
        policy_ref = settings["policy"]
        policy = _artifact(policy_ref, "bounded_unknown_recovery_policy")
        source_ref = settings["user_authorization"]
        source = _artifact(source_ref, "explicit_user_authorization")
        valid = (policy.get("enabled") is True
                 and type(policy.get("max_unknown_replacements_per_page")) is int
                 and policy["max_unknown_replacements_per_page"] == 1
                 and policy.get("generation_only") is True
                 and policy.get("unknown_publication_action") == "reconcile"
                 and (policy.get("scope") == "all_runs" or policy.get("run_id") == run_id)
                 and policy.get("user_authorization") == source_ref
                 and source.get("scope") == "bounded_unknown_recovery_policy"
                 and isinstance(source.get("source"), str) and bool(source["source"].strip())
                 and isinstance(source.get("user_message"), str) and bool(source["user_message"].strip()))
        return {"valid": bool(valid), "reason": "valid" if valid else "policy_contract_mismatch",
                "policy_sha256": policy_ref["sha256"], "authorization_sha256": source_ref["sha256"]}
    except (OSError, ValueError, TypeError, KeyError):
        return {"valid": False, "reason": "policy_evidence_missing_or_invalid"}


def _stopped(attempt, journal, evidence):
    for ref in evidence:
        try:
            body = _artifact(ref, "executor_stopped")
            binding = {key: attempt.get(key) for key in
                       ("run_id", "artwork_id", "job_id", "page_index", "attempt_id", "generator_agent_id")}
            binding.update(local_request_id=journal.get("local_request_id"),
                           request_sha256=journal.get("request", {}).get("request_sha256"))
            if (all(key in body and body[key] == value for key, value in binding.items())
                    and body.get("outcome") == "stopped"
                    and body.get("original_call_recoverable") is False
                    and isinstance(body.get("statement"), str) and body["statement"].strip()):
                return True
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return False


def inspect_health(db_path, *, config=None, run_id=None, executor_evidence=(), _now=None):
    """Use a read-only SQLite snapshot, including legacy schemas; never construct State.

    _now is an offline test seam. The CLI always uses the actual UTC clock.
    Executor observations are hash-bound local artifacts, never timeout inferences.
    """
    config = config or {}
    now = _now or datetime.now(timezone.utc)
    local = now.astimezone(SHANGHAI)
    run_id = run_id or "scheduled:" + local.date().isoformat()
    out = {"run_id": run_id, "job_id": None, "artwork_id": None,
           "checked_at": now.isoformat(), "shanghai_date": local.date().isoformat(),
           "read_only": True, "total_changes": 0, "stage": "unknown", "age_seconds": None,
           "next_action": "inspect", "needs_attention": True, "attempts": [], "deadline_at": None,
           "completed_quality_slots": None, "completed_quality_slots_requires_state_validation": True,
           "age_is_failure_evidence": False, "publication_authorized_by_watchdog": False,
           "policy": _policy(config, run_id)}

    def finish(stage, action, attention=True, progress=None):
        out.update(stage=stage, next_action=action, needs_attention=attention,
                   health="needs_attention" if attention else ("complete" if stage == "verified_complete" else "waiting"),
                   last_progress_at=progress, age_seconds=_age(progress, now), stage_age_seconds=_age(progress, now))
        return out

    path = Path(db_path).resolve()
    if not path.is_file():
        return finish("database_missing", "report_missing_database_do_not_create")
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

            def rows(table, key, value):
                if table not in tables:
                    return []
                return [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,))]

            if "jobs" not in tables:
                return finish("schema_unavailable", "report_schema_no_migration_from_watchdog")
            columns = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            if "run_id" not in columns:
                return finish("legacy_run_mapping_missing", "report_schema_do_not_guess_job_by_day")
            jobs = rows("jobs", "run_id", run_id)
            runs = rows("workflow_runs", "run_id", run_id)
            if not jobs:
                return finish("job_missing" if runs else "run_missing", "report_missing_job_do_not_select")
            if len(jobs) != 1:
                return finish("ambiguous_run", "report_multiple_jobs_do_not_choose")
            job = jobs[0]
            out.update(job_id=job["job_id"], artwork_id=job.get("artwork_id"),
                       run_kind=runs[0].get("kind") if runs else None,
                       job_status=job.get("status"), created_at=job.get("created_at"))
            if runs and runs[0].get("kind") == "scheduled":
                out["deadline_at"] = runs[0].get("day", local.date().isoformat()) + "T" + config.get(
                    "schedule", {}).get("publish_at", "10:00") + ":00+08:00"
            progress = job.get("updated_at") or job.get("created_at")
            pubs = rows("publications", "job_id", job["job_id"])
            out["publications"] = [{k: p.get(k) for k in ("platform", "status", "attempt_count", "updated_at")}
                                   for p in pubs]
            if any(p.get("status") in PUBLICATION_UNKNOWN or
                   (p.get("status") == "published" and not p.get("receipt_id")) for p in pubs):
                return finish("publication_reconciliation", "remote_reconcile_only_never_resubmit", True,
                              max((p.get("updated_at") or "" for p in pubs), default=None) or progress)
            if pubs and all(p.get("status") == "published" and p.get("receipt_id") for p in pubs):
                return finish("verified_complete", "skip_run_already_published", False, progress)
            if job.get("status") == "complete":
                return finish("completion_inconsistent", "remote_reconcile_only_never_resubmit", True, progress)
            pages = rows("pages", "job_id", job["job_id"])
            attempts = rows("quality_corrections", "job_id", job["job_id"])
            replacement_counts = {p["page_index"]: sum(bool(a.get("replacement_of")) for a in attempts
                                                       if a["page_index"] == p["page_index"]) for p in pages}
            for attempt in sorted(attempts, key=lambda a: (a["page_index"], a["attempt_number"])):
                attempt.update(run_id=run_id, artwork_id=job.get("artwork_id"))
                journal = _object(attempt.get("recovery"))
                phase = journal.get("phase") or {"pending": "unknown", "failed": "legacy_failure_unverified"}.get(
                    attempt.get("status"), attempt.get("status", "unknown"))
                events = rows("correction_recovery_events", "attempt_id", attempt["attempt_id"])
                timestamps = [e.get("created_at") for e in events if e.get("event") != "heartbeat" and e.get("created_at")]
                last_progress = max(timestamps, default=attempt.get("created_at"))
                heartbeat = max((e.get("created_at") for e in events
                                 if e.get("event") == "heartbeat" and e.get("created_at")), default=None)
                claimed = bool(journal.get("local_request_id") and phase in UNKNOWN) or any(
                    e.get("event") == "dispatch_claimed" for e in events)
                stopped = _stopped(attempt, journal, executor_evidence)
                replacements = replacement_counts.get(attempt["page_index"], 0)
                actions = {"result_saved": "verify_and_commit_saved_result_no_generation",
                           "recorded": "fresh_independent_qa_then_selection",
                           "failed": "continue_quality_with_remaining_normal_budget",
                           "legacy_failure_unverified": "report_unverified_failure_evidence",
                           "superseded_unknown": "retain_original_unknown_follow_replacement",
                           "abandoned_unknown": "report_unknown_not_completed_quality_slot",
                           "confirmed_not_submitted": "verify_evidence_then_prepare_same_attempt"}
                action = actions.get(phase, "inspect_unrecognized_phase")
                if phase in {"reserved", "prepared"}:
                    action = ("recover_authorized_executor_or_manual_attention" if attempt.get("replacement_of") else
                              "report_claim_history_do_not_reassign" if claimed or "correction_recovery_events" not in tables else
                              "fresh_agent_reassign_if_original_unavailable_and_never_dispatched")
                elif phase in UNKNOWN:
                    physical_used = max((a["attempt_number"] for a in attempts
                                         if a["page_index"] == attempt["page_index"]), default=0)
                    if (out["policy"]["valid"] and stopped and not attempt.get("replacement_of")
                            and replacements == 0 and physical_used < 3):
                        action = "authorized_replacement_after_state_checks"
                    elif stopped:
                        action = "report_unknown_policy_or_replacement_budget_blocked"
                    else:
                        action = "recover_original_call_or_obtain_executor_stop_evidence"
                item = {k: attempt.get(k) for k in ("attempt_id", "page_index", "attempt_number", "generator_agent_id",
                                                   "status", "optimization_slot", "replacement_of")}
                item.update(phase=phase, next_action=action, last_progress_at=last_progress,
                            age_seconds=_age(last_progress, now), last_heartbeat_at=heartbeat,
                            heartbeat_age_seconds=_age(heartbeat, now), executor_stopped_evidence=stopped,
                            replacement_budget_used=replacements, superseded_by=journal.get("superseded_by"))
                item["optimization_slot"] = attempt.get("optimization_slot") or (
                    attempt["attempt_number"] if not attempt.get("replacement_of") else None)
                out["attempts"].append(item)
            out["pages"] = [{"page_index": p["page_index"], "normal_budget_used": p.get("correction_count"),
                             "replacement_budget_used": replacement_counts.get(p["page_index"], 0),
                             "completed_quality_slots": None} for p in pages]
            out["unknown_attempts"] = [a["attempt_id"] for a in out["attempts"]
                                       if a["phase"] in UNKNOWN | {"superseded_unknown", "abandoned_unknown"}]
            pending = [a for a in out["attempts"] if a["status"] == "pending"]
            if pending:
                current = min(pending, key=lambda a: (a["phase"] != "result_saved", a["page_index"]))
                return finish("generation_" + current["phase"], current["next_action"], True, current["last_progress_at"])
            if len(pages) != job.get("page_count") or any(not p.get("raw") for p in pages):
                return finish("originals_incomplete", "resume_existing_job_download_no_reselection", True, progress)
            if any(not p.get("features") for p in pages):
                return finish("original_analysis", "fresh_original_analysis_agent", True, progress)
            if any(not p.get("cos") for p in pages):
                return finish("generation_incomplete", "inspect_initial_generation_before_any_new_call", True, progress)
            if any(not p.get("quality_review") for p in pages):
                return finish("quality_review", "fresh_independent_qa_then_selection", True, progress)
            if any(a["phase"] in {"abandoned_unknown", "legacy_failure_unverified"} for a in out["attempts"]):
                return finish("quality_evidence_unresolved", "validate_quality_unknown_is_not_completed_slot", True, progress)
            if not job.get("research") or not pubs or any(not p.get("copy") for p in pubs):
                return finish("research_or_copy", "complete_existing_job_research_and_copy", True, progress)
            if not runs:
                return finish("run_authorization_missing", "report_missing_run_context", True, progress)
            publish_at = config.get("schedule", {}).get("publish_at", "10:00")
            if runs[0].get("kind") == "scheduled" and local.strftime("%H:%M") < publish_at:
                return finish("prepare_only_before_publish_time", "validate_preparation_and_wait_for_publish_window", False, progress)
            return finish("publish_preflight", "fresh_executor_validate_publish_ready_and_original_gates", True, progress)
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return finish("inspection_error", "report_schema_or_evidence_error_no_mutation")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/workspace/p2cos/workflow.json"))
    parser.add_argument("--db", type=Path, help="Override configured database; never creates it")
    parser.add_argument("--run-id", help="Existing run only; default is today's real Asia/Shanghai scheduled run")
    parser.add_argument("--evidence-manifest", type=Path,
                        help="Private JSON with executor_stop_evidence: [{path, sha256}]; no evidence is inferred")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text()) if args.config.is_file() else {}
        db = args.db or Path(config["storage"]["state_db"])
        evidence = json.loads(args.evidence_manifest.read_text())["executor_stop_evidence"] if args.evidence_manifest else []
        if not isinstance(evidence, list):
            raise ValueError("evidence must be a list")
        result = inspect_health(db, config=config, run_id=args.run_id, executor_evidence=evidence)
    except (OSError, ValueError, TypeError, KeyError):
        result = {"read_only": True, "stage": "configuration_error", "needs_attention": True,
                  "next_action": "report_configuration_error_no_mutation"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
