"""Credential-free correction journal and read-only recovery inspection.

The external image tool has no query/idempotency API. A dispatch claim is a
write-ahead intent, NOT proof of submission. Losing its return or the tool's
return leaves an unknown outcome; never retry that claim. Evidence supplied by
an operator is audit provenance, not cryptographically authenticated by us.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from p2cos_quality import agent_id, digest, nonempty, validate_trace


def _reject(message):
    from p2cos_state import StateError
    raise StateError(message)


def migrate_recovery(db):
    columns = {r[1] for r in db.execute("PRAGMA table_info(quality_corrections)")}
    if "recovery" not in columns:
        # NULL deliberately means legacy/unknown, never reserved or not called.
        db.execute("ALTER TABLE quality_corrections ADD COLUMN recovery TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS correction_recovery_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id TEXT NOT NULL REFERENCES quality_corrections(attempt_id),
        event TEXT NOT NULL, details TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")


def migrate_correction_slots(db):
    """Separate two optimization slots from at most three physical corrections.

    Rebuild outside a transaction, preserving every old column, row, journal and
    event. NULL old journals remain unknown. This never changes page counts or
    publication facts, and is never called by read-only inspection.
    """
    if db.in_transaction:
        raise RuntimeError("Correction migration requires a connection outside a transaction")
    columns = [r[1] for r in db.execute("PRAGMA table_info(quality_corrections)")]
    if "optimization_slot" in columns:
        return
    foreign_keys = db.execute("PRAGMA foreign_keys").fetchone()[0]
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN IMMEDIATE")
        columns = [r[1] for r in db.execute("PRAGMA table_info(quality_corrections)")]
        if "optimization_slot" in columns:
            db.commit()
            return
        schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='quality_corrections'").fetchone()[0]
        schema, count = re.subn(r"CHECK\s*\(\s*attempt_number\s+BETWEEN\s+1\s+AND\s+2\s*\)",
                               "CHECK(attempt_number BETWEEN 1 AND 3)", schema, flags=re.I)
        if count != 1:
            raise RuntimeError("Unrecognized correction schema; migration refused")
        schema = re.sub(r"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`\[]?quality_corrections[\"`\]]?",
                        "CREATE TABLE quality_corrections_slot_migration", schema, count=1, flags=re.I)
        schema = schema.replace("(", "(optimization_slot INTEGER NOT NULL CHECK(optimization_slot BETWEEN 1 AND 2), replacement_of TEXT REFERENCES quality_corrections(attempt_id), replacement_authorization TEXT, ", 1)
        auxiliary = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='quality_corrections' AND type IN ('index','trigger') AND sql IS NOT NULL")]
        db.execute(schema)
        names = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
        db.execute(f"INSERT INTO quality_corrections_slot_migration({names},optimization_slot) SELECT {names},attempt_number FROM quality_corrections")
        db.execute("DROP TABLE quality_corrections")
        db.execute("ALTER TABLE quality_corrections_slot_migration RENAME TO quality_corrections")
        for statement in auxiliary:
            db.execute(statement)
        db.execute("CREATE UNIQUE INDEX quality_correction_replacement_once ON quality_corrections(replacement_of) WHERE replacement_of IS NOT NULL")
        db.execute("CREATE UNIQUE INDEX quality_correction_page_replacement_once ON quality_corrections(job_id,page_index) WHERE replacement_of IS NOT NULL")
        db.execute("CREATE UNIQUE INDEX quality_correction_normal_slot ON quality_corrections(job_id,page_index,optimization_slot) WHERE replacement_of IS NULL")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Correction migration failed foreign-key validation")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys=" + str(int(bool(foreign_keys))))


def _authorization_artifact(value, kind):
    """Read small immutable evidence, never inline image payloads or credentials."""
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        _reject("Authorization requires an artifact path and SHA-256")
    try:
        path = Path(value["path"]).resolve(strict=True)
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != value["sha256"]:
            raise ValueError("Authorization artifact size or SHA-256 mismatch")
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get("kind") != kind or _contains_inline_binary(body):
            raise ValueError("Authorization artifact kind or content invalid")
    except (OSError, ValueError, TypeError) as error:
        _reject(str(error))
    return {"path": str(path), "sha256": value["sha256"], "content": body}


def _save(db, attempt_id, journal, event, details=None):
    db.execute("UPDATE quality_corrections SET recovery=? WHERE attempt_id=?",
               (json.dumps(journal, ensure_ascii=False), attempt_id))
    db.execute("INSERT INTO correction_recovery_events(attempt_id,event,details) VALUES (?,?,?)",
               (attempt_id, event, json.dumps(details if details is not None else journal, ensure_ascii=False)))


def _journal(row):
    return json.loads(row["recovery"]) if row["recovery"] else {"schema_version": 1, "phase": "unknown"}


def _contains_inline_binary(value):
    if isinstance(value, str):
        return value.startswith("data:") and ";base64," in value
    if isinstance(value, dict):
        return any(_contains_inline_binary(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_inline_binary(item) for item in value)
    return False


def _evidence(value, row, kind):
    """Snapshot a hash-bound JSON receipt/attestation, including its payload."""
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        _reject("Evidence requires a JSON artifact path and its SHA-256")
    path = Path(value["path"]).resolve(strict=True)
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        _reject("Evidence receipt too large; store binary tool output in a separate hashed artifact")
    if hashlib.sha256(raw).hexdigest() != value["sha256"]:
        _reject("Evidence artifact SHA-256 mismatch")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("Evidence must be an object")
        if _contains_inline_binary(body):
            raise ValueError("Inline base64 output is not allowed in evidence; use a hashed raw_result_artifact")
        for key in ("job_id", "page_index", "attempt_id", "generator_agent_id"):
            if body.get(key) != row[key]:
                raise ValueError(f"Evidence {key} mismatch")
        journal = _journal(row)
        for key, expected in (("local_request_id", journal.get("local_request_id")),
                              ("request_sha256", journal.get("request", {}).get("request_sha256"))):
            if key not in body or body[key] != expected:
                raise ValueError(f"Evidence {key} must bind this dispatch incarnation (null for legacy)")
        if body.get("kind") != kind:
            raise ValueError("Evidence kind mismatch")
        agent_id(body.get("operator_agent_id"), "Evidence operator_agent_id")
        nonempty(body.get("statement"), "Evidence statement")
        if kind in {"tool_result", "terminal_tool_failure"}:
            if (not isinstance(body.get("tool_result"), dict)
                    or (not body["tool_result"] and not body.get("raw_result_artifact"))):
                raise ValueError("Evidence requires actual nonbinary tool fields or a hashed raw result artifact")
        if kind == "terminal_tool_failure" and (body.get("terminal") is not True or body.get("outcome") != "failed"):
            raise ValueError("Only an explicit terminal failure receipt proves failure; timeouts are unknown")
        if kind == "authoritative_not_submitted":
            if body.get("outcome") != "not_submitted" or body.get("original_executor_stopped") is not True:
                raise ValueError("Not-submitted evidence must establish no call and a stopped original executor")
            nonempty(body.get("authority"), "Not-submitted evidence authority")
        if kind == "operator_abandon_unknown" and body.get("accept_unknown_outcome") is not True:
            raise ValueError("Abandonment must explicitly retain the unknown outcome")
        artifact = body.get("raw_result_artifact")
        if artifact is not None:
            if (not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}
                    or hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest() != artifact["sha256"]):
                raise ValueError("Raw tool result artifact hash mismatch")
    except (ValueError, TypeError) as error:
        _reject(str(error))
    return {"path": str(path), "sha256": value["sha256"], "content": body}


class CorrectionRecoveryMixin:
    @staticmethod
    def _reserve_correction_recovery(db, attempt_id):
        _save(db, attempt_id, {"schema_version": 1, "phase": "reserved"}, "reserved")

    def supersede_unknown_with_authorization(self, job_id, page_index, *, attempt_id,
            generator_agent_id, new_generator_agent_id, operator_agent_id, optimization,
            authorization, mode="manual_requested", policy=None):
        """Reserve one extra physical call, preserving an unknown logical slot.

        This is an explicit audited authorization, not evidence of remote
        failure/non-submission. It permits no publication action. A subsequent
        ordinary prepare/claim is still required before any actual tool call.
        """
        if mode not in {"manual_requested", "explicit_policy"}:
            _reject("Replacement requires a manual request or an explicit authorized policy")
        for value, label in ((new_generator_agent_id, "new_generator_agent_id"),
                             (operator_agent_id, "operator_agent_id")):
            self._quality_input(agent_id, value, label)
        with self._transaction() as db:
            row, journal = self._correction(db, job_id, page_index, attempt_id, generator_agent_id)
            if db.execute("SELECT 1 FROM publications WHERE job_id=? AND status!='pending'", (job_id,)).fetchone():
                _reject("Publication is no longer pending; unknown generation replacement cannot mutate this package")
            if journal["phase"] not in {"unknown", "dispatching", "submitted"} or row["replacement_of"]:
                _reject("Only an unresolved normal generation invocation may receive one replacement")
            if db.execute("SELECT 1 FROM quality_corrections WHERE job_id=? AND page_index=? AND replacement_of IS NOT NULL",
                          (job_id, page_index)).fetchone():
                _reject("The page's single unknown replacement budget is already reserved")
            job = db.execute("SELECT run_id,artwork_id FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            receipt = _authorization_artifact(authorization, "authorized_unknown_replacement")
            body = receipt["content"]
            expected = {"run_id": job["run_id"], "job_id": job_id, "artwork_id": job["artwork_id"],
                        "page_index": page_index, "attempt_id": attempt_id,
                        "generator_agent_id": generator_agent_id, "new_generator_agent_id": new_generator_agent_id,
                        "operator_agent_id": operator_agent_id, "mode": mode,
                        "local_request_id": journal.get("local_request_id"),
                        "request_sha256": journal.get("request", {}).get("request_sha256")}
            if any(key not in body or body[key] != value for key, value in expected.items()):
                _reject("Replacement authorization must bind this run, artwork, page, executor and dispatch incarnation")
            if body.get("accept_unknown_outcome") is not True or body.get("original_executor_stopped") is not True:
                _reject("Replacement must retain remote uncertainty and establish the original local executor stopped")
            self._quality_input(nonempty, body.get("statement"), "Replacement authorization statement")
            stopped = _authorization_artifact(body.get("executor_stop_evidence"), "executor_stopped")
            stop_keys = ("run_id", "job_id", "artwork_id", "page_index", "attempt_id", "generator_agent_id",
                         "local_request_id", "request_sha256")
            if (stopped["content"].get("outcome") != "stopped"
                    or any(k not in stopped["content"] or stopped["content"][k] != expected[k] for k in stop_keys)):
                _reject("Executor stop evidence must bind the original generator and exact dispatch incarnation")
            self._quality_input(nonempty, stopped["content"].get("statement"), "Executor stop evidence statement")
            source = _authorization_artifact(body.get("user_authorization"), "explicit_user_authorization")
            for field in ("source", "user_message"):
                self._quality_input(nonempty, source["content"].get(field), "User authorization " + field)
            allowed_scopes = {"one_unknown_replacement", "bounded_unknown_recovery_policy"}
            if source["content"].get("scope") not in allowed_scopes:
                _reject("User authorization does not authorize unknown generation replacement")
            if mode == "manual_requested" and source["content"].get("scope") != "one_unknown_replacement":
                _reject("A manual replacement requires its own targeted one-time user authorization")
            if source["content"].get("scope") == "one_unknown_replacement":
                if any(source["content"].get(k) != expected[k] for k in ("run_id", "job_id", "artwork_id", "page_index", "attempt_id")):
                    _reject("One-time user authorization must bind the original run and target attempt")
            policy_receipt = None
            if mode == "explicit_policy":
                policy_receipt = _authorization_artifact(policy, "bounded_unknown_recovery_policy")
                policy_body = policy_receipt["content"]
                if (policy_body.get("enabled") is not True
                        or type(policy_body.get("max_unknown_replacements_per_page")) is not int
                        or policy_body["max_unknown_replacements_per_page"] != 1
                        or policy_body.get("generation_only") is not True
                        or policy_body.get("unknown_publication_action") != "reconcile"
                        or (policy_body.get("scope") != "all_runs" and policy_body.get("run_id") != job["run_id"])
                        or policy_body.get("user_authorization") != body["user_authorization"]
                        or source["content"].get("scope") != "bounded_unknown_recovery_policy"):
                    _reject("Automatic recovery policy must be explicitly authorized, bounded to one and generation-only")
            elif policy is not None:
                _reject("A manual replacement must not implicitly activate an automatic policy")
            page = self._page(db, job_id, page_index)
            self._validate_correction_optimization(db, job_id, page_index, page, optimization)
            if not row["optimization"] or optimization != json.loads(row["optimization"]):
                _reject("A replacement inherits the original logical slot's complete optimization contract")
            self._assign_agent(db, job_id, new_generator_agent_id, "generator", fresh=True)
            number = db.execute("SELECT COALESCE(MAX(attempt_number),0)+1 FROM quality_corrections WHERE job_id=? AND page_index=?",
                                (job_id, page_index)).fetchone()[0]
            if number > 3:
                _reject("Physical correction budget exceeded")
            audit = {"authorization": receipt, "user_authorization": source,
                     "executor_stop_evidence": stopped, "policy": policy_receipt}
            attempt = {"attempt_id": uuid4().hex, "attempt_number": number,
                       "optimization_slot": row["optimization_slot"], "replacement_of": attempt_id,
                       "generator_agent_id": new_generator_agent_id, "optimization": optimization}
            db.execute("""INSERT INTO quality_corrections(attempt_id,job_id,page_index,attempt_number,
                generator_agent_id,optimization,optimization_slot,replacement_of,replacement_authorization)
                VALUES (?,?,?,?,?,?,?,?,?)""", (attempt["attempt_id"], job_id, page_index, number,
                new_generator_agent_id, json.dumps(optimization, ensure_ascii=False), row["optimization_slot"],
                attempt_id, json.dumps(audit, ensure_ascii=False)))
            prior_journal = json.loads(row["recovery"]) if row["recovery"] else None
            journal.update(superseded_from_phase=journal["phase"], phase="superseded_unknown", outcome="unknown", superseded_by=attempt["attempt_id"],
                           replacement_authorization=audit)
            _save(db, attempt_id, journal, "authorized_unknown_superseded", {"prior_journal": prior_journal, "journal": journal})
            db.execute("UPDATE quality_corrections SET status='superseded_unknown' WHERE attempt_id=?", (attempt_id,))
            self._reserve_correction_recovery(db, attempt["attempt_id"])
            # correction_count retains the number of normal logical slots.
            db.execute("UPDATE pages SET pending_correction=?,quality_blocked=0 WHERE job_id=? AND page_index=?",
                       (json.dumps(attempt, ensure_ascii=False), job_id, page_index))
            self._invalidate_reviews(db, job_id)
            self._refresh(db, job_id)
            return attempt

    def audit_late_superseded_result(self, job_id, page_index, *, attempt_id,
                                    generator_agent_id, generation_trace, evidence):
        """Retain a late real return without selecting it or changing any budget.

        The superseded unknown decision remains historical fact. This audit is
        not a new candidate, a replacement cancellation, or permission to call.
        """
        from p2cos_state import inspect_image
        with self._transaction() as db:
            row, journal = self._correction(db, job_id, page_index, attempt_id, generator_agent_id, terminal=True)
            if row["status"] != "superseded_unknown" or journal["phase"] != "superseded_unknown":
                _reject("Late audit only accepts the original superseded unknown attempt")
            receipt = _evidence(evidence, row, "tool_result")
            if not isinstance(generation_trace, dict):
                _reject("Late result requires its original real generation trace")
            output = inspect_image(generation_trace["output_path"])
            result = {"output": output, "generation_trace": generation_trace, "evidence": receipt}
            self._validate_saved_trace(db, row, result)
            request = journal.get("request")
            if request and any(generation_trace[k] != request[k] for k in ("actual_prompt", "prompt_sha256", "references")):
                _reject("Late result trace does not match the original request")
            if (receipt["content"].get("output_sha256") != output["sha256"]
                    or receipt["content"].get("prompt_sha256") != generation_trace["prompt_sha256"]):
                _reject("Late result evidence hash mismatch")
            if "late_result" in journal:
                if journal["late_result"] != result:
                    _reject("Late result audit is immutable")
                return journal
            paths = [json.loads(r[0])["cos"]["path"] for r in db.execute(
                "SELECT snapshot FROM quality_versions WHERE job_id=? AND page_index=?", (job_id, page_index))]
            raw = json.loads(self._page(db, job_id, page_index)["raw"])
            if output["path"] == raw["path"] or output["path"] in paths:
                _reject("Late output must retain its own immutable artifact path")
            journal["late_result"] = result
            _save(db, attempt_id, journal, "late_superseded_result_saved")
            return journal

    def _correction(self, db, job_id, page_index, attempt_id, generator_agent_id, *, terminal=False):
        row = db.execute("SELECT * FROM quality_corrections WHERE job_id=? AND page_index=? AND attempt_id=?",
                         (job_id, page_index, attempt_id)).fetchone()
        if row is None or row["generator_agent_id"] != generator_agent_id:
            _reject("Correction must match its job, page, attempt and original generator")
        if not terminal or row["status"] == "pending":
            self._assert_mutable(db, job_id)
            page = self._page(db, job_id, page_index)
            pending = json.loads(page["pending_correction"]) if page["pending_correction"] else None
            if row["status"] != "pending" or not pending or pending["attempt_id"] != attempt_id:
                _reject("Correction is not the current pending attempt")
        return row, _journal(row)

    def _request(self, db, row, actual_prompt, references, tool_arguments=None):
        from p2cos_state import inspect_image
        self._validate_optimized_prompt(db, row["job_id"], row["page_index"], actual_prompt)
        page = self._page(db, row["job_id"], row["page_index"])
        raw, cos = json.loads(page["raw"]), json.loads(page["cos"])
        features_hash = digest(json.loads(page["features"]))
        generation = json.loads(page["generation"])
        try:
            inspected_raw, inspected_cos = inspect_image(raw["path"]), inspect_image(cos["path"])
        except (OSError, SyntaxError, ValueError) as error:
            _reject(f"Correction baseline image is unavailable or cannot be decoded: {error}")
        if (inspected_raw != raw or inspected_cos != cos
                or generation["features_sha256"] != features_hash
                or generation["cos_sha256"] != cos["sha256"]
                or json.loads(row["optimization"])["based_on_generation_sha256"] != digest(generation)):
            _reject("Correction baseline images, feature inventory or generation changed")
        baseline = {"raw": raw, "cos": cos, "features_sha256": features_hash,
                    "generation_sha256": digest(generation)}
        prompt_hash = hashlib.sha256(actual_prompt.encode()).hexdigest()
        trace = {"tool": "image_gen.imagegen", "generator_agent_id": row["generator_agent_id"],
                 "actual_prompt": actual_prompt, "prompt_sha256": prompt_hash,
                 "references": references, "output_path": cos["path"], "output_sha256": cos["sha256"],
                 "returned_model": "not invoked", "attempt": row["attempt_number"],
                 "correction_attempt_id": row["attempt_id"]}
        generation = {"generator_agent_id": row["generator_agent_id"], "correction_attempt": row["attempt_number"],
                      "correction_attempt_id": row["attempt_id"], "trace": trace}
        self._quality_input(validate_trace, {"raw": raw, "cos": cos, "generation": generation})
        if any(ref["role"] == "current_failed_output" and (ref["path"] != cos["path"] or ref["sha256"] != cos["sha256"])
               for ref in references):
            _reject("Current failed output reference must match the correction baseline")
        if tool_arguments is None:
            tool_arguments = {"prompt": actual_prompt, "referenced_image_paths": [r["path"] for r in references],
                              "transparent_background": False}
        if (not isinstance(tool_arguments, dict)
                or set(tool_arguments) != {"prompt", "referenced_image_paths", "transparent_background"}
                or tool_arguments["prompt"] != actual_prompt
                or tool_arguments["referenced_image_paths"] != [r["path"] for r in references]
                or type(tool_arguments["transparent_background"]) is not bool):
            _reject("Tool arguments must exactly bind the saved prompt, ordered references and transparency")
        request = {"tool": "image_gen.imagegen", "actual_prompt": actual_prompt, "prompt_sha256": prompt_hash,
                   "references": references, "tool_arguments": tool_arguments, "baseline": baseline}
        request["request_sha256"] = digest(request)
        return request

    def prepare_correction(self, job_id, page_index, *, attempt_id, generator_agent_id,
                           actual_prompt, references, tool_arguments=None):
        with self._transaction() as db:
            row, journal = self._correction(db, job_id, page_index, attempt_id, generator_agent_id)
            request = self._request(db, row, actual_prompt, references, tool_arguments)
            if journal["phase"] == "prepared" and journal["request"] == request:
                return journal
            if journal["phase"] not in {"reserved", "confirmed_not_submitted"}:
                _reject("Only a reserved or authoritatively unsubmitted attempt can be prepared")
            # Earlier submission IDs/resolutions stay in append-only events.
            # They must never be presented as evidence about this new dispatch.
            journal.pop("submission", None)
            journal.pop("resolution", None)
            journal.update(phase="prepared", request=request, local_request_id=uuid4().hex)
            _save(db, attempt_id, journal, "prepared")
            return journal

    def reassign_unsubmitted_correction(self, job_id, page_index, *, attempt_id, generator_agent_id,
                                        new_generator_agent_id, operator_agent_id, reason):
        """Reassign an unused reservation, never an invocation with uncertain outcome."""
        for identifier, label in ((new_generator_agent_id, "new_generator_agent_id"),
                                  (operator_agent_id, "operator_agent_id")):
            self._quality_input(agent_id, identifier, label)
        self._quality_input(nonempty, reason, "Reassignment reason")
        with self._transaction() as db:
            row, journal = self._correction(db, job_id, page_index, attempt_id, generator_agent_id)
            claimed = db.execute("SELECT 1 FROM correction_recovery_events WHERE attempt_id=? AND event='dispatch_claimed'",
                                 (attempt_id,)).fetchone()
            if journal["phase"] not in {"reserved", "prepared"} or claimed or row["replacement_of"]:
                _reject("Only a journaled reservation that has never claimed dispatch can be reassigned")
            self._assign_agent(db, job_id, new_generator_agent_id, "generator", fresh=True)
            page = self._page(db, job_id, page_index)
            pending = json.loads(page["pending_correction"])
            pending["generator_agent_id"] = new_generator_agent_id
            details = {"old_generator_agent_id": generator_agent_id, "new_generator_agent_id": new_generator_agent_id,
                       "operator_agent_id": operator_agent_id, "reason": reason, "prior_journal": journal}
            # The actual new executor must explicitly prepare its own request.
            journal = {"schema_version": 1, "phase": "reserved"}
            db.execute("UPDATE quality_corrections SET generator_agent_id=? WHERE attempt_id=?", (new_generator_agent_id, attempt_id))
            db.execute("UPDATE pages SET pending_correction=? WHERE job_id=? AND page_index=?",
                       (json.dumps(pending, ensure_ascii=False), job_id, page_index))
            _save(db, attempt_id, journal, "unsubmitted_executor_reassigned", details)
            return pending

    def claim_correction_dispatch(self, job_id, page_index, *, attempt_id, generator_agent_id):
        """Call once immediately before the tool. A repeated claim NEVER authorizes a call."""
        with self._transaction() as db:
            row, journal = self._correction(db, job_id, page_index, attempt_id, generator_agent_id)
            if journal["phase"] != "prepared":
                _reject("Dispatch already claimed or unprepared; inspect/reconcile, do not call the tool again")
            request = journal["request"]
            checked = self._request(db, row, request["actual_prompt"], request["references"], request["tool_arguments"])
            if checked != request:
                _reject("Prepared request or baseline changed before dispatch")
            journal["phase"] = "dispatching"
            _save(db, attempt_id, journal, "dispatch_claimed")
            return {"local_request_id": journal["local_request_id"], "request_sha256": request["request_sha256"],
                    "tool_arguments": request["tool_arguments"]}

    def note_correction_submission(self, job_id, page_index, *, attempt_id, generator_agent_id,
                                   cell_id=None, provider_request_id=None):
        identifiers = {k: v for k, v in {"cell_id": cell_id, "provider_request_id": provider_request_id}.items() if v is not None}
        if not identifiers:
            _reject("Record only a real returned cell ID or provider request ID")
        for key, value in identifiers.items():
            self._quality_input(nonempty, value, key)
        with self._transaction() as db:
            _, journal = self._correction(db, job_id, page_index, attempt_id, generator_agent_id)
            if journal["phase"] not in {"dispatching", "submitted"}:
                _reject("Submission identifiers require a dispatch claim")
            previous = journal.get("submission", {})
            if any(k in previous and previous[k] != v for k, v in identifiers.items()):
                _reject("Submission identifiers cannot be replaced")
            if journal["phase"] == "submitted" and all(previous.get(k) == v for k, v in identifiers.items()):
                return journal
            journal.update(phase="submitted", submission={**previous, **identifiers})
            _save(db, attempt_id, journal, "submission_observed")
            return journal

    def save_correction_result(self, job_id, page_index, *, attempt_id, generator_agent_id,
                               generation_trace, evidence):
        """Persist a real result before mutating the selected page; also imports legacy results."""
        from p2cos_state import inspect_image
        with self._transaction() as db:
            row, journal = self._correction(db, job_id, page_index, attempt_id, generator_agent_id, terminal=True)
            receipt = _evidence(evidence, row, "tool_result")
            if not isinstance(generation_trace, dict):
                _reject("Generation trace is required")
            output = inspect_image(generation_trace["output_path"])
            result = {"output": output, "generation_trace": generation_trace, "evidence": receipt}
            if journal["phase"] in {"result_saved", "recorded"}:
                if journal["result"] != result:
                    _reject("Saved result is immutable")
                return journal
            if row["status"] != "pending" or journal["phase"] not in {"unknown", "dispatching", "submitted"}:
                _reject("A result requires a claimed dispatch or a bound legacy recovery receipt")
            page = self._page(db, job_id, page_index)
            if output["path"] == json.loads(page["raw"])["path"]:
                _reject("Raw and Cos must be separate files")
            paths = [json.loads(r[0])["cos"]["path"] for r in db.execute(
                "SELECT snapshot FROM quality_versions WHERE job_id=? AND page_index=?", (job_id, page_index))]
            if output["path"] in paths:
                _reject("Every generated version must retain a separate immutable output path")
            generation = {"generator_agent_id": generator_agent_id, "correction_attempt": row["attempt_number"],
                          "correction_attempt_id": attempt_id, "trace": generation_trace}
            self._quality_input(validate_trace, {"raw": json.loads(page["raw"]), "cos": output, "generation": generation})
            self._validate_optimized_prompt(db, job_id, page_index, generation_trace["actual_prompt"])
            request = journal.get("request")
            if request and any(generation_trace[k] != request[k] for k in ("actual_prompt", "prompt_sha256", "references")):
                _reject("Result trace does not match the prepared request")
            if receipt["content"].get("output_sha256") != output["sha256"]:
                _reject("Tool result evidence output hash mismatch")
            if receipt["content"].get("prompt_sha256") != generation_trace["prompt_sha256"]:
                _reject("Tool result evidence prompt hash mismatch")
            journal.update(phase="result_saved", result=result)
            _save(db, attempt_id, journal, "result_saved")
            return journal

    def _require_saved_correction_result(self, db, job_id, page_index, pending, output, trace):
        row = db.execute("SELECT * FROM quality_corrections WHERE attempt_id=?", (pending["attempt_id"],)).fetchone()
        journal = _journal(row)
        result = journal.get("result", {})
        if journal["phase"] != "result_saved" or result.get("output") != output or result.get("generation_trace") != trace:
            _reject("Persist the matching real tool result with save_correction_result before record_cos")
        request = journal.get("request")
        if request and self._request(db, row, request["actual_prompt"], request["references"], request["tool_arguments"]) != request:
            _reject("Prepared correction baseline changed before result commit")
        page = self._page(db, job_id, page_index)
        generation = {"generator_agent_id": row["generator_agent_id"], "correction_attempt": row["attempt_number"],
                      "correction_attempt_id": row["attempt_id"], "trace": trace}
        self._quality_input(validate_trace, {"raw": json.loads(page["raw"]), "cos": output, "generation": generation})

    @staticmethod
    def _mark_correction_recorded(db, attempt_id):
        row = db.execute("SELECT * FROM quality_corrections WHERE attempt_id=?", (attempt_id,)).fetchone()
        journal = _journal(row)
        journal["phase"] = "recorded"
        _save(db, attempt_id, journal, "recorded", {"output_sha256": journal["result"]["output"]["sha256"]})

    def commit_correction_result(self, job_id, page_index, *, attempt_id, generator_agent_id):
        from p2cos_state import inspect_image
        # record_cos checks the same saved result and commits page/version/journal
        # together. The read is not a claim and authorizes no external side effect.
        with self._connect() as db:
            row, journal = self._correction(db, job_id, page_index, attempt_id, generator_agent_id, terminal=True)
            if journal["phase"] not in {"result_saved", "recorded"}:
                _reject("No saved result to commit")
            result = journal["result"]
            if inspect_image(result["output"]["path"]) != result["output"]:
                _reject("Saved result file changed")
            self._validate_saved_trace(db, row, result)
            if row["status"] == "recorded":
                return result["output"]
        from p2cos_state import StateError
        try:
            return self.record_cos(job_id, page_index, result["output"]["path"],
                generator_agent_id=generator_agent_id, correction_attempt_id=attempt_id,
                generation_trace=result["generation_trace"])
        except StateError:
            # Another worker may have committed this exact immutable result.
            # Never retry a write or mask a different validation failure.
            with self._connect() as db:
                row, latest = self._correction(db, job_id, page_index, attempt_id, generator_agent_id, terminal=True)
                if (row["status"] == "recorded" and latest.get("result") == result
                        and inspect_image(result["output"]["path"]) == result["output"]):
                    self._validate_saved_trace(db, row, result)
                    return result["output"]
            raise

    def _validate_saved_trace(self, db, row, result):
        page = self._page(db, row["job_id"], row["page_index"])
        generation = {"generator_agent_id": row["generator_agent_id"], "correction_attempt": row["attempt_number"],
                      "correction_attempt_id": row["attempt_id"], "trace": result["generation_trace"]}
        self._quality_input(validate_trace, {"raw": json.loads(page["raw"]), "cos": result["output"], "generation": generation})

    def _fail_correction_with_evidence(self, job_id, page_index, *, attempt_id, generator_agent_id,
                                       actual_prompt, reason, evidence):
        self._quality_input(nonempty, reason, "Generation failure reason")
        self._quality_input(nonempty, actual_prompt, "Actual optimized prompt")
        with self._transaction() as db:
            row, journal = self._correction(db, job_id, page_index, attempt_id, generator_agent_id, terminal=True)
            receipt = _evidence(evidence, row, "terminal_tool_failure")
            details = {"actual_prompt": actual_prompt, "prompt_sha256": hashlib.sha256(actual_prompt.encode()).hexdigest(),
                       "reason": reason, "evidence": receipt}
            if row["status"] == "failed" and json.loads(row["failure_details"]) == details:
                return journal
            if row["status"] != "pending" or journal["phase"] not in {"unknown", "dispatching", "submitted"}:
                _reject("Terminal failure requires a claimed dispatch or an authoritative legacy receipt")
            self._validate_optimized_prompt(db, job_id, page_index, actual_prompt)
            if journal.get("request", {}).get("actual_prompt", actual_prompt) != actual_prompt:
                _reject("Failure prompt does not match the prepared request")
            if receipt["content"].get("prompt_sha256") != details["prompt_sha256"]:
                _reject("Failure receipt prompt hash mismatch")
            journal.update(phase="failed", failure=details)
            _save(db, attempt_id, journal, "terminal_tool_failure")
            db.execute("UPDATE quality_corrections SET status='failed',failure_details=? WHERE attempt_id=?",
                       (json.dumps(details, ensure_ascii=False), attempt_id))
            db.execute("UPDATE pages SET pending_correction=NULL WHERE job_id=? AND page_index=?", (job_id, page_index))
            self._refresh(db, job_id)
            return journal

    def resolve_correction_unknown(self, job_id, page_index, *, attempt_id, generator_agent_id,
                                   resolution, evidence):
        kinds = {"not_submitted": "authoritative_not_submitted", "abandon_unknown": "operator_abandon_unknown"}
        if resolution not in kinds:
            _reject("Unknown resolution must be not_submitted or abandon_unknown")
        with self._transaction() as db:
            row, journal = self._correction(db, job_id, page_index, attempt_id, generator_agent_id, terminal=True)
            receipt = _evidence(evidence, row, kinds[resolution])
            phase = "confirmed_not_submitted" if resolution == "not_submitted" else "abandoned_unknown"
            if journal["phase"] == phase and journal.get("resolution") == receipt:
                return journal
            if row["status"] != "pending" or journal["phase"] not in {"unknown", "dispatching", "submitted"}:
                _reject("Only an unresolved invocation can use the audited unknown-resolution channel")
            # Preserve all prior request, receipt and event history. No budget reset.
            journal.update(phase=phase, resolution=receipt)
            _save(db, attempt_id, journal, kinds[resolution])
            if resolution == "abandon_unknown":
                db.execute("UPDATE quality_corrections SET status='abandoned_unknown' WHERE attempt_id=?", (attempt_id,))
                db.execute("UPDATE pages SET pending_correction=NULL WHERE job_id=? AND page_index=?", (job_id, page_index))
                self._refresh(db, job_id)
            return journal


def inspect_recovery(db_path, job_id, page_index=None):
    """No State constructor, migrations, file timestamp heuristics or writes."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        query = "SELECT * FROM quality_corrections WHERE job_id=?"
        args = [job_id]
        if page_index is not None:
            query += " AND page_index=?"
            args.append(page_index)
        rows = db.execute(query + " ORDER BY page_index,attempt_number", args).fetchall()
        items = []
        actions = {
            "reserved": "prepare_correction; no dispatch has been claimed by this protocol",
            "prepared": "claim_correction_dispatch once, then call tool with exactly returned arguments",
            "dispatching": "outcome unknown: recover original call/result; never claim or submit again",
            "submitted": "await/recover the original call using recorded identifiers; never resubmit",
            "unknown": "legacy outcome unknown: recover real result or authoritative non-submission evidence",
            "result_saved": "commit_correction_result; do not generate again",
            "recorded": "resume independent QA/selection; do not generate again for this attempt",
            "failed": "real terminal failure recorded; remaining retry still requires optimized prompt and budget",
            "legacy_failure_unverified": "legacy reason-only failure is unverified and does not qualify for completed retry fallback",
            "confirmed_not_submitted": "prepare same attempt after original executor is stopped; budget unchanged",
            "abandoned_unknown": "unknown remains audited and budget consumed; does not qualify as completed retry",
            "superseded_unknown": "remote outcome remains unknown; follow the separately authorized replacement, never resubmit this attempt",
        }
        for row in rows:
            item = dict(row)
            legacy_phase = {"pending": "unknown", "failed": "legacy_failure_unverified"}.get(item["status"], item["status"])
            journal = json.loads(item["recovery"]) if item.get("recovery") else {"phase": legacy_phase}
            saved_evidence = journal.get("result", {}).get("evidence", {})
            items.append({"attempt_id": item["attempt_id"], "page_index": item["page_index"],
                          "attempt_number": item["attempt_number"], "generator_agent_id": item["generator_agent_id"],
                          "optimization_slot": item.get("optimization_slot", item["attempt_number"]),
                          "replacement_of": item.get("replacement_of"), "superseded_by": journal.get("superseded_by"),
                          "status": item["status"], "phase": journal["phase"],
                          "local_request_id": journal.get("local_request_id"), "submission": journal.get("submission"),
                          "request_sha256": journal.get("request", {}).get("request_sha256"),
                          "result_sha256": journal.get("result", {}).get("output", {}).get("sha256"),
                          "result_path": journal.get("result", {}).get("output", {}).get("path"),
                          "evidence_artifact": {k: saved_evidence[k] for k in ("path", "sha256") if k in saved_evidence} or None,
                          "raw_result_artifact": saved_evidence.get("content", {}).get("raw_result_artifact"),
                          "next_action": actions.get(journal["phase"], "unrecognized phase: stop and inspect")})
        return {"job_id": job_id, "read_only": True, "total_changes": db.total_changes,
                "attempts": items, "limitation": "No external tool query or idempotency API; dispatch intent cannot establish remote outcome."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--page-index", type=int)
    args = parser.parse_args(argv)
    print(json.dumps(inspect_recovery(args.db, args.job_id, args.page_index), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
