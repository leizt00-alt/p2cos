#!/usr/bin/env python3
"""Historical audits, isolated real-image QA validation, and recovery backups.

This entry point never imports browser, image generation, or Pixiv clients.
It does not read environment variables or credentials. Historical reviews are
isolated acceptance evidence and do not rewrite published production jobs.
New manual and scheduled work uses p2cos_runs.py and a stable per-trigger run ID.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import zipfile
from zoneinfo import ZoneInfo


DEFAULT_CONFIG = Path(__file__).with_name("workflow.json")
DEFAULT_REPORTS = Path("/workspace/work/quality_v2_trial")
BACKUP_SOURCES = (
    "p2cos_state.py", "p2cos_quality.py", "pixiv_pipeline.py", "creator_browser.py",
    "p2cos_runs.py", "test_p2cos_runs.py", "test_p2cos_selection.py",
    "run_quality_validation.py", "test_p2cos_state.py", "test_pixiv_pipeline.py",
    "test_run_quality_validation.py", "workflow.json", "prepare_task.txt",
    "publish_task.txt", "generation_prompt.txt", "QUALITY_TASKS.txt", "RUNBOOK.txt",
    "BROWSER.txt",
    "SOCIAL_AUTO_UPLOAD.md", "social_auto_upload_adapter.py", "social_auto_upload.install.json",
    "social_auto_upload.requirements.lock", "test_social_auto_upload_adapter.py",
)
BACKUP_REPORTS = (
    "media_manifest.json", "reports_manifest.json",
    "run_report.json", "run_report.txt", "acceptance_core.json",
    "migration_report.json", "migration_report_stage1.json", "test_results.json", "test_results.txt",
    "test_results_stage1.json", "test_results_stage1.txt", "acceptance_core_stage1.json",
    "daily_check.json", "existing_validation.json", "social_adapter_dry_run.json",
    "social_adapter_doctor.json", "social_adapter_check_login.json",
    "social_adapter_fallback_preflight.json",
    "social_test_results.json", "social_test_results.txt",
    "manual_run_context.json", "manual_job_snapshot.json", "historical_preservation.json",
    "monthly_original_download_status.json", "current_account_verification.json", "publish_result.json",
    "legacy_run_annotation_fix.json",
    "automation_update.json", "RESTORE.txt",
)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def historical_reports(reports_dir):
    """Read an installation's explicit audit allowlist, confined to its directory."""
    reports_dir = Path(reports_dir)
    manifest = read_json(reports_dir / "reports_manifest.json")
    reports = {}
    for item in manifest["artworks"]:
        artwork_id = str(item["artwork_id"])
        if not artwork_id.isascii() or not artwork_id.isdecimal() or int(artwork_id) < 1 or artwork_id in reports:
            raise ValueError("QA manifest must name unique positive artwork IDs")
        paths = {}
        for key in ("features_path", "review_path"):
            path = Path(item[key])
            if not path.is_absolute():
                path = reports_dir / path
            if path.parent.resolve() != reports_dir.resolve() or path.suffix != ".json" or path.is_symlink():
                raise ValueError("QA manifest must name regular JSON reports in its own directory")
            paths[key] = path
        reports[artwork_id] = paths
    if not reports:
        raise ValueError("QA manifest must include at least one artwork")
    return reports


def readonly_database(path):
    path = Path(path).resolve(strict=True)
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def logical_snapshot(path, *, existing_columns=None):
    """Hash complete table rows without disclosing their contents in a report."""
    with readonly_database(path) as db:
        tables = existing_columns or {row[0]: None for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")}
        result = {}
        for table, requested_columns in tables.items():
            table_sql = '"' + table.replace('"', '""') + '"'
            columns = requested_columns or [row[1] for row in db.execute(f"PRAGMA table_info({table_sql})")]
            column_sql = ",".join('"' + value.replace('"', '""') + '"' for value in columns)
            rows = [list(row) for row in db.execute(f"SELECT {column_sql} FROM {table_sql}")]
            encoded_rows = sorted(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows)
            result[table] = {"columns": columns, "row_count": len(rows),
                             "rows_sha256": hashlib.sha256("\n".join(encoded_rows).encode()).hexdigest()}
        return result


def consistent_backup(source, destination):
    """Use SQLite's backup API so WAL contents are included consistently."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = readonly_database(source)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        # Store a standalone recovery file, with no dependency on WAL sidecars.
        dst.execute("PRAGMA journal_mode=DELETE")
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite backup integrity check failed")
        dst.commit()
    finally:
        dst.close()
        src.close()
    return {"path": str(destination), "sha256": file_hash(destination),
            "integrity_check": "ok"}


def historical_rows_preserved(baseline_path, current_path):
    """Verify old cells by primary key while permitting newly authorized work."""
    result = {"all_historical_cells_preserved": True, "tables": {}, "changed_or_missing_rows": []}
    with readonly_database(baseline_path) as old, readonly_database(current_path) as current:
        tables = [row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for table in tables:
            quote = lambda value: '"' + value.replace('"', '""') + '"'
            info = old.execute(f"PRAGMA table_info({quote(table)})").fetchall()
            columns = [row[1] for row in info]
            primary = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            if not primary:
                raise RuntimeError("Historical comparison requires durable primary keys")
            selected = ','.join(quote(name) for name in columns)
            rows = old.execute(f"SELECT {selected} FROM {quote(table)}").fetchall()
            matched = 0
            for row in rows:
                keys = [row[name] for name in primary]
                where = ' AND '.join(quote(name) + '=?' for name in primary)
                now = current.execute(f"SELECT {selected} FROM {quote(table)} WHERE {where}", keys).fetchone()
                if now is not None and tuple(now) == tuple(row):
                    matched += 1
                else:
                    result["all_historical_cells_preserved"] = False
                    result["changed_or_missing_rows"].append({"table": table, "primary_key": keys})
            result["tables"][table] = {"baseline_rows": len(rows), "identical_rows": matched}
    return result


def migrate_production(config_path, reports_dir):
    """Add the quality schema, preserving every preexisting cell and receipt.

    The integration operator must run the complete offline suite first. This is
    an additive schema migration only; it does not retroactively approve images.
    """
    from p2cos_state import State

    config = read_json(config_path)
    source = Path(config["storage"]["state_db"])
    reports_dir = Path(reports_dir)
    previous_report_path = reports_dir / "migration_report.json"
    previous = read_json(previous_report_path) if previous_report_path.is_file() else None
    sequence = previous.get("migration_sequence", 1) + 1 if previous else 1
    previous_migrations = previous.get("previous_migrations", []) + [
        {key: value for key, value in previous.items() if key != "previous_migrations"}
    ] if previous else []
    if previous and sequence == 2:
        (reports_dir / "migration_report_stage1.json").write_text(json.dumps(previous, ensure_ascii=False, indent=2) + "\n")
    before = logical_snapshot(source)
    backup_name = "production_before_quality_v2.sqlite3" if sequence == 1 else f"production_before_quality_v2_migration{sequence}.sqlite3"
    backup = consistent_backup(source, reports_dir / backup_name)
    State(source, publish_not_before=config["schedule"]["publish_at"],
          platforms=config["publishing"]["platforms"])
    after_old = logical_snapshot(source, existing_columns={key: item["columns"] for key, item in before.items()})
    if after_old != before:
        raise RuntimeError("Migration changed preexisting data; stop and inspect the preserved pre-migration backup")
    with readonly_database(source) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [list(row) for row in db.execute("PRAGMA foreign_key_check")]
    if integrity != "ok" or foreign_keys:
        raise RuntimeError("Migrated production database failed consistency checks")
    result = {"migration_sequence": sequence, "previous_migrations": previous_migrations,
              "backup_before": backup, "legacy_tables_before": before,
              "legacy_tables_after": after_old, "all_preexisting_values_preserved": True,
              "integrity_check": integrity, "foreign_key_violations": foreign_keys,
              "schema_after": logical_snapshot(source)}
    (reports_dir / "migration_report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def validate_existing(reports_dir=DEFAULT_REPORTS):
    """Validate freshly reviewed existing media without altering published jobs.

    Main results use the original honest legacy provenance. A separate, clearly
    labelled fixture substitutes only test provenance to exercise the critical
    visual verdict through the full publish gate. It never certifies generation.
    """
    from p2cos_quality import digest, validate_features, verdict_reasons
    from p2cos_state import State, StateError, inspect_image

    reports_dir = Path(reports_dir)
    media = read_json(reports_dir / "media_manifest.json")
    reports = historical_reports(reports_dir)
    results = {"mode": "read_only_historical_audit", "new_images_generated": 0,
               "original_images_downloaded": 0, "publications_created": 0,
               "correction_attempts_triggered": 0, "best_available_override_applied": False,
               "production_state_writes": 0, "historical_generation_certified": False,
               "artworks": [], "supplemental_synthetic_test_only": []}
    fixture_clock = datetime(2026, 10, 9, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    for artwork_id in reports:
        features_path = Path(reports[artwork_id]["features_path"])
        review_path = Path(reports[artwork_id]["review_path"])
        features, review = read_json(features_path), read_json(review_path)
        if features["analyst_agent_id"] == review["reviewer_agent_id"]:
            raise RuntimeError("Fresh visual reviewer must be independent from the source inventory analyst")
        decoded = {}
        for kind in ("raw", "cos"):
            decoded[kind] = inspect_image(media[artwork_id][kind]["path"])
            if decoded[kind]["sha256"] != media[artwork_id][kind]["sha256"]:
                raise RuntimeError(f"{artwork_id} {kind} no longer matches fresh visual audit")
        validate_features(features, decoded["raw"]["sha256"])
        if review["raw_sha256"] != decoded["raw"]["sha256"] or review["cos_sha256"] != decoded["cos"]["sha256"]:
            raise RuntimeError("Visual review image hash mismatch")
        if review["features_sha256"] != digest(features):
            raise RuntimeError("Visual review source feature hash mismatch")
        visual_reasons = verdict_reasons(features, review, 1)
        result = {"artwork_id": artwork_id, "files_fully_decoded_and_hash_matched": True,
                  "raw_sha256": decoded["raw"]["sha256"], "cos_sha256": decoded["cos"]["sha256"],
                  "feature_report_sha256": file_hash(features_path), "review_report_sha256": file_hash(review_path),
                  "source_analyst_agent_id": features["analyst_agent_id"],
                  "independent_reviewer_agent_id": review["reviewer_agent_id"],
                  "fresh_review_independent_from_source_analyst": True,
                  "visual_verdict": "fail" if visual_reasons else "pass", "visual_reasons": visual_reasons,
                  "critical_failures": [key for key, item in features["checks"].items()
                                        if item["critical"] and review["checks"][key]["status"] == "fail"],
                  "legacy_generation_provenance": "unknown",
                  "historical_published_status_changed": False}
        with tempfile.TemporaryDirectory(prefix=f"legacy_{artwork_id}_", dir=reports_dir) as scratch:
            state = State(Path(scratch) / "state.sqlite3", clock=lambda: fixture_clock,
                          platforms=("xiaohongshu",))
            job = state.reserve_daily([{"id": artwork_id, "page_count": 1}], day="2026-10-09")
            job_id = job["job_id"]
            state.record_raw(job_id, 0, decoded["raw"]["path"])
            state.record_features(job_id, 0, features)
            state.record_cos(job_id, 0, decoded["cos"]["path"])
            try:
                state.record_quality_review(job_id, 0, review)
            except StateError as error:
                result["legacy_review_rejected_reason"] = str(error)
            else:
                raise RuntimeError("Legacy unknown generation was incorrectly accepted")
            result["legacy_publish_ready"] = state.publish_ready(job_id)
            if result["legacy_publish_ready"]["ready"]:
                raise RuntimeError("Legacy artwork became publishable without generation provenance")
        results["artworks"].append(result)

        if not visual_reasons:
            # A fresh reviewer determines the verdict. Do not manufacture a
            # failing case or give legacy media synthetic positive certification.
            continue

        # Only provenance is synthetic. Every check status/evidence below comes
        # byte-for-byte from the independent fresh historical visual audit.
        with tempfile.TemporaryDirectory(prefix=f"synthetic_test_only_{artwork_id}_", dir=reports_dir) as scratch:
            state = State(Path(scratch) / "state.sqlite3", clock=lambda: fixture_clock,
                          platforms=("xiaohongshu",))
            job = state.reserve_daily([{"id": artwork_id, "page_count": 1}], day="2026-10-09")
            job_id = job["job_id"]
            fixture_features = deepcopy(features)
            fixture_features["analyst_agent_id"] = "synthetic_test_only:source_analyst"
            state.record_raw(job_id, 0, decoded["raw"]["path"])
            state.record_features(job_id, 0, fixture_features)
            state.record_cos(job_id, 0, decoded["cos"]["path"],
                             generator_agent_id="synthetic_test_only:generator",
                             generation_trace={"synthetic_test_only": True,
                                               "note": "Existing image reused for offline gate mechanics; no generation occurred"})
            generation = state.get_job(job_id)["pages"][0]["generation"]
            fixture_review = deepcopy(review)
            fixture_review.update(reviewer_agent_id="synthetic_test_only:reviewer",
                                  features_sha256=digest(fixture_features), generation_sha256=digest(generation))
            gate = state.record_quality_review(job_id, 0, fixture_review)
            readiness = state.publish_ready(job_id)
            try:
                state.claim_publish(job_id, "xiaohongshu")
            except StateError as error:
                claim_reason = str(error)
            else:
                raise RuntimeError("Negative visual evidence was incorrectly claimable")
            critical_failures = result["critical_failures"]
            if gate["passed"] or readiness["ready"]:
                raise RuntimeError("Negative quality gate unexpectedly passed")
            for key in critical_failures:
                if not any(key in reason for reason in readiness["reasons"]):
                    raise RuntimeError("Critical visual failure did not reach publish_ready")
            if state.get_job(job_id)["publications"]["xiaohongshu"]["attempt_count"] != 0:
                raise RuntimeError("Rejected synthetic fixture consumed a publication attempt")
            results["supplemental_synthetic_test_only"].append({
                "synthetic_test_only": True, "artwork_id": artwork_id,
                "not_generation_certification": True, "provenance_fields_substituted_only_in_isolated_test": True,
                "visual_check_status_and_evidence_unchanged": fixture_review["checks"] == review["checks"],
                "quality_gate": gate, "publish_ready": readiness,
                "claim_publish_rejected_reason": claim_reason, "publication_attempt_count": 0,
                "isolated_database_removed": True,
                "note": "Research/copy were intentionally not fabricated; readiness can also list their absence."})
        if file_hash(features_path) != result["feature_report_sha256"] or file_hash(review_path) != result["review_report_sha256"]:
            raise RuntimeError("Original audit reports changed during isolated validation")
    (reports_dir / "existing_validation.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    return results


def write_run_report(run_id, config_path=DEFAULT_CONFIG, reports_dir=DEFAULT_REPORTS, *, outcome_path=None):
    """Refresh the actual per-run report after production work advances."""
    from p2cos_runs import run_check
    from p2cos_state import State

    config_path, reports_dir = Path(config_path), Path(reports_dir)
    config = read_json(config_path)
    with readonly_database(config["storage"]["state_db"]) as db:
        check = run_check(db, run_id)
        if not check.get("job"):
            raise RuntimeError("A run report requires a reserved artwork")
        job = State._get_job(db, check["job"]["job_id"])
        readiness = State._readiness(db, job["job_id"])
        versions = db.execute("SELECT COUNT(*) FROM quality_versions WHERE job_id=?", (job["job_id"],)).fetchone()[0]
    outcome = read_json(outcome_path) if outcome_path else None
    historical = historical_rows_preserved(reports_dir / "production_before_quality_v2.sqlite3", config["storage"]["state_db"])
    if not historical["all_historical_cells_preserved"]:
        raise RuntimeError("Previously published historical cells changed unexpectedly")
    status = "complete" if job["status"] == "complete" else "in_progress"
    if outcome and outcome.get("status") in {"blocked", "reconciliation_required", "reconciliation_review_required"}:
        status = "blocked" if job["status"] != "complete" else status
    report = {
        "schema_version": 3, "status": status,
        "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "main_run": check, "job_status": job["status"], "current_publish_readiness": readiness,
        "actual_artifacts": {"page_count": job["page_count"],
                             "decoded_raw_pages": sum(bool(page["raw"]) for page in job["pages"]),
                             "recorded_cos_pages": sum(bool(page["cos"]) for page in job["pages"]),
                             "retained_cos_versions": versions,
                             "corrections_reserved": sum(page["correction_count"] for page in job["pages"])},
        "publication_records": job["publications"], "publication_outcome": outcome,
        "quality_selection": job.get("quality_selection"),
        "runtime_semantics": {"one_artwork_per_run": True, "daily_publication_quota": False,
                              "same_run_retries_idempotent": True, "global_artwork_history_deduplication": True,
                              "manual_immediate_publish_requires_persisted_explicit_authorization": True,
                              "scheduled_publish_not_before": "10:00 Asia/Shanghai"},
        "quality_policy": {"max_corrections": config["generation"]["max_correction_attempts_per_page"],
                           "after_exhaustion": "Independent best-available selection may waive visual failures only",
                           "nonvisual_gates_waived": False},
        "fallback_policy": "Legacy browser method only after definite pre-submit failure; uncertainty requires reconciliation",
        "test_suite": read_json(reports_dir / "test_results.json"),
        "automation_update": read_json(reports_dir / "automation_update.json"),
        "schema_migrations": read_json(reports_dir / "migration_report.json"),
        "historical_preservation": historical,
        "historical_visual_audits": read_json(reports_dir / "existing_validation.json"),
        "code_sha256": {name: file_hash(config_path.parent / name) for name in BACKUP_SOURCES
                        if (config_path.parent / name).is_file()},
        "limitations": ["A local publication attempt_count records a claim, not proof that a remote submit occurred.",
                        "Historical legacy/unknown generation provenance was never recertified by test fixtures."],
    }
    for filename, key in (("monthly_original_download_status.json", "original_download"),
                          ("current_account_verification.json", "current_account_verification"),
                          ("legacy_run_annotation_fix.json", "legacy_run_annotation_fix")):
        if (reports_dir / filename).is_file():
            report[key] = read_json(reports_dir / filename)
    (reports_dir / "manual_job_snapshot.json").write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n")
    (reports_dir / "historical_preservation.json").write_text(json.dumps(historical, ensure_ascii=False, indent=2) + "\n")
    (reports_dir / "run_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = ["p2cos 实际运行报告", "更新时间：" + report["updated_at"],
             "运行结果：" + status + "；作品 " + job["artwork_id"] + "；本地阶段 " + job["status"],
             "Run：" + run_id, "真实定时任务先完成更新与回读，随后才启动此manual月榜运行。",
             "最新完整离线测试：" + str(report["test_suite"]["tests_run"]) + " 项；通过=" + str(report["test_suite"]["success"]),
             "实际媒体状态：" + json.dumps(report["actual_artifacts"], ensure_ascii=False),
             "每日次数上限已移除；只对同run重试去重，不同明确manual触发各自独立；作品历史仍全局去重。",
             "默认最多2次修正，耗尽后独立择优只可豁免视觉失败；媒体、来源、哈希、完整性和去重等门禁仍强制。",
             "只有明确未提交失败才允许备用浏览器方法；不确定结果先核对。",
             "历史记录均按基线逐主键核对，原有单元格全部保持。",
             "详情见run_report.json；旧图QA/synthetic_test_only只是历史审计和隔离门禁测试，不是旧图的新生成认证。"]
    if outcome:
        lines.append("实际发布器结果：" + json.dumps(outcome, ensure_ascii=False))
    (reports_dir / "run_report.txt").write_text("\n".join(lines) + "\n")
    return {"report": str(reports_dir / "run_report.json"), "status": status,
            "run_id": run_id, "artwork_id": job["artwork_id"], "job_status": job["status"]}


def package_backup(config_path=DEFAULT_CONFIG, reports_dir=DEFAULT_REPORTS, *, additional_sources=()):
    """Package an explicit allowlist; never traverse profiles or dependencies."""
    config_path, reports_dir = Path(config_path), Path(reports_dir)
    project_root = config_path.parent
    config = read_json(config_path)
    state_copy = reports_dir / "production_state_for_restore.sqlite3"
    if state_copy.exists():
        state_copy.unlink()
    consistent_backup(config["storage"]["state_db"], state_copy)
    files = [(project_root / name, "p2cos/" + name) for name in BACKUP_SOURCES
             if (project_root / name).is_file()]
    for name in additional_sources:
        source = project_root / name
        if (source.parent != project_root or source.suffix not in {".py", ".txt", ".json", ".lock", ".md", ".sh"}
                or not source.is_file()):
            raise ValueError("Additional backup source must be an explicit top-level source or lock file")
        files.append((source, "p2cos/" + name))
    # Reports are generated under this controlled directory. Isolated databases
    # contain test fixtures and are deliberately excluded from restore artifacts.
    for name in BACKUP_REPORTS:
        source = reports_dir / name
        if source.is_file():
            files.append((source, "quality_v2_trial/" + source.name))
    if (reports_dir / "reports_manifest.json").is_file():
        for reports in historical_reports(reports_dir).values():
            for source in reports.values():
                if not source.is_file():
                    raise ValueError("A manifest-listed QA report is missing")
                entry = (source, "quality_v2_trial/" + source.name)
                if entry not in files:
                    files.append(entry)
    files.append((state_copy, "p2cos/state.sqlite3"))
    forbidden = ("browser-profile", ".env", "cookies", "credential", ".venv", "site-packages", "third_party")
    for source, member in files:
        if source.is_symlink():
            raise ValueError("Backup allowlist must contain regular files, not symlinks")
        if any(token in member.lower() for token in forbidden):
            raise ValueError("Backup allowlist contains a forbidden filename")
    archive = Path("/workspace/work/p2cos_workflow_backup.zip")
    manifest = {member: {"sha256": file_hash(source), "bytes": source.stat().st_size} for source, member in files}
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for source, member in files:
            bundle.write(source, member)
        bundle.writestr("BACKUP_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise RuntimeError("Backup ZIP integrity check failed")
    return {"path": str(archive), "sha256": file_hash(archive),
            "bytes": archive.stat().st_size, "entry_count": len(files) + 1,
            "allowlist": list(manifest), "integrity_check": "ok"}


def historical_day_audit(config_path=DEFAULT_CONFIG, *, now=None):
    """Audit legacy day records; this is never a gate for creating a new run."""
    config = read_json(config_path)
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if now.utcoffset() is None:
        raise ValueError("daily-check clock must include a timezone")
    now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    day = now.date().isoformat()
    db_path = Path(config["storage"]["state_db"])
    result = {"checked_at": now.isoformat(), "day": day,
              "state_db": str(db_path), "read_only": True,
              "historical_audit_only_not_a_run_gate": True,
              "external_calls": {"pixiv_download": 0, "image_generation": 0,
                                 "social_publication": 0},
              "action": None, "reason": None, "job": None}
    with readonly_database(db_path) as db:
        slot = db.execute("SELECT job_id FROM publish_days WHERE day=?", (day,)).fetchone()
        job = None
        if slot:
            job = db.execute("SELECT * FROM jobs WHERE job_id=?", (slot[0],)).fetchone()
            if job is None:
                result.update(action="blocked_inconsistent_state", reason="publish_day_job_missing")
                return result
        if job is None:
            job = db.execute("SELECT * FROM jobs WHERE day=?", (day,)).fetchone()
        if job is not None:
            publications = [dict(row) for row in db.execute(
                "SELECT platform,status,attempt_count,receipt_id,receipt_url FROM publications WHERE job_id=? ORDER BY platform",
                (job["job_id"],))]
            result["job"] = {key: job[key] for key in ("job_id", "day", "artwork_id", "status", "page_count")}
            result["job"]["publications"] = publications
            if job["status"] == "complete":
                if publications and all(p["status"] == "published" and p["receipt_id"] for p in publications):
                    result.update(action="skip_today_already_published",
                                  reason="today_has_completed_job_with_verified_publication_receipts")
                else:
                    result.update(action="blocked_inconsistent_state",
                                  reason="complete_job_missing_published_receipt")
                return result
        # Match State.reserve_daily's recovery priority after checking today's
        # completed publication: any earlier unfinished job must be resumed.
        unfinished = db.execute("SELECT * FROM jobs WHERE status != 'complete' ORDER BY day LIMIT 1").fetchone()
        if unfinished is not None:
            result["job"] = {key: unfinished[key] for key in ("job_id", "day", "artwork_id", "status", "page_count")}
            result.update(action="resume_existing_job", reason="unfinished_artwork_must_be_resumed")
        elif job is None:
            result.update(action="select_new_artwork", reason="no_completed_or_unfinished_job_for_today")
        else:
            result.update(action="blocked_inconsistent_state", reason="unrecognized_daily_state")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    daily = commands.add_parser("historical-day-audit", help="Inspect legacy day records; never gates new run creation")
    daily.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    validate = commands.add_parser("validate-existing", help="Validate fresh historical image reports with isolated gate checks")
    validate.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS)
    report = commands.add_parser("report-run", help="Refresh the actual per-trigger run report")
    report.add_argument("--run-id", required=True)
    report.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    report.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS)
    report.add_argument("--outcome", type=Path)
    backup = commands.add_parser("package-backup", help="Package explicit source/report allowlist and consistent SQLite backup")
    backup.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    backup.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS)
    backup.add_argument("--additional-source", action="append", default=[])
    args = parser.parse_args(argv)
    if args.command == "historical-day-audit":
        result = historical_day_audit(args.config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result["action"] == "blocked_inconsistent_state" else 0
    if args.command == "package-backup":
        result = package_backup(args.config, args.reports_dir, additional_sources=args.additional_source)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate-existing":
        print(json.dumps(validate_existing(args.reports_dir), ensure_ascii=False, indent=2))
        return 0
    if args.command == "report-run":
        print(json.dumps(write_run_report(args.run_id, args.config, args.reports_dir, outcome_path=args.outcome), ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
