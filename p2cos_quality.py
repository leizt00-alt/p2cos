"""Offline schema and fail-closed checks for reference-to-Cos quality evidence.

Agent identifiers are audit provenance supplied by the orchestrator; this module
cannot infer visual agreement or verify agent isolation from image bytes.
"""

import hashlib
import json
from pathlib import Path
import re


SCHEMA_VERSION = 2
REQUIRED_CHECKS = (
    "composition.framing", "composition.perspective", "composition.background",
    "pose.head", "pose.torso", "pose.arms_and_hands", "pose.legs",
    "lighting.direction", "lighting.shadows",
    "clothing_structure.neckline", "clothing_structure.sleeves",
    "clothing_structure.torso_and_waist", "clothing_structure.layers", "clothing_structure.transparency",
    "hem_and_skin_gaps.hem_shape", "hem_and_skin_gaps.hem_length", "hem_and_skin_gaps.skin_gaps",
    "stocking_boundaries.left_leg", "stocking_boundaries.right_leg",
    "stocking_boundaries.opacity", "stocking_boundaries.upper_edges", "stocking_boundaries.knee_relative_boundaries",
    "accessories_and_markings.accessories", "accessories_and_markings.markings", "accessories_and_markings.trim",
    "identity.face", "identity.hair", "identity.outfit",
)
VERDICTS = {"pass", "fail", "unverifiable", "not_applicable"}
VISIBILITY = {"visible", "not_visible", "unverifiable"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} requires non-empty text")


def agent_id(value, label):
    nonempty(value, label)
    if value != value.strip():
        raise ValueError(f"{label} must not contain surrounding whitespace")
    if value.strip().lower() in {"unknown", "legacy", "legacy/unknown", "n/a"}:
        raise ValueError(f"{label} requires an actual fresh agent identifier")


def sha256(value, label):
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value):
        raise ValueError(f"{label} requires a lowercase SHA-256 digest")


def validate_features(report, raw_sha256):
    if not isinstance(report, dict) or report.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Original feature inventory requires schema_version=2")
    agent_id(report.get("analyst_agent_id"), "analyst_agent_id")
    if report.get("raw_sha256") != raw_sha256:
        raise ValueError("Original feature inventory raw hash does not match")
    checks = report.get("checks")
    if not isinstance(checks, dict) or not set(REQUIRED_CHECKS).issubset(checks):
        raise ValueError("Original feature inventory must cover every required detailed check")
    for key, item in checks.items():
        if not isinstance(item, dict) or item.get("visibility") not in VISIBILITY:
            raise ValueError(f"{key}: invalid source visibility")
        if type(item.get("critical")) is not bool:
            raise ValueError(f"{key}: critical must be a boolean")
        nonempty(item.get("expected"), f"{key} expected")
        nonempty(item.get("evidence"), f"{key} source evidence")
    return report


def validate_review(report, features, generation, page_hashes, page_count):
    if not isinstance(report, dict) or report.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Independent review requires schema_version=2")
    agent_id(report.get("reviewer_agent_id"), "reviewer_agent_id")
    if not isinstance(generation, dict):
        raise ValueError("Generation provenance is missing")
    agent_id(generation.get("generator_agent_id"), "generator_agent_id")
    roles = (features["analyst_agent_id"], generation["generator_agent_id"], report["reviewer_agent_id"])
    if len(set(roles)) != 3:
        raise ValueError("Source analyst, generator and reviewer must be different fresh agents")
    for key in ("raw_sha256", "cos_sha256", "features_sha256"):
        if report.get(key) != generation.get(key):
            raise ValueError(f"Independent review {key} does not match the generation")
    if report.get("generation_sha256") != digest(generation):
        raise ValueError("Independent review generation_sha256 does not match the generation version")
    if page_count > 1:
        if len(page_hashes) != page_count or report.get("identity_cos_sha256") != page_hashes:
            raise ValueError("Multi-page identity review must bind every current Cos page hash")
    checks = report.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(features["checks"]):
        raise ValueError("Independent review must cover exactly the original feature inventory")
    for key, item in checks.items():
        if not isinstance(item, dict) or item.get("status") not in VERDICTS:
            raise ValueError(f"{key}: invalid review status")
        nonempty(item.get("evidence"), f"{key} review evidence")
    return report


def verdict_reasons(features, review, page_count):
    reasons = []
    for key, feature in features["checks"].items():
        status = review["checks"][key]["status"]
        if status == "fail":
            reasons.append(f"{key} failed")
        elif feature["visibility"] == "visible" and status != "pass":
            reasons.append(f"{key} visible source feature not confirmed")
        elif feature["critical"] and feature["visibility"] == "unverifiable":
            reasons.append(f"{key} critical source feature unverifiable")
        elif feature["critical"] and status == "unverifiable":
            reasons.append(f"{key} critical feature unverifiable")
        elif feature["visibility"] != "visible" and status == "pass":
            reasons.append(f"{key} unseen source feature cannot be marked pass")
    if page_count > 1 and not any(key.startswith("identity.") and feature["visibility"] == "visible"
                                  for key, feature in features["checks"].items()):
        reasons.append("multi-page identity has no visible comparison evidence")
    return reasons


def validate_trace(page):
    generation = page["generation"]
    trace = generation.get("trace")
    if not isinstance(trace, dict):
        raise ValueError("Actual generation trace is required")
    if trace.get("tool") != "image_gen.imagegen":
        raise ValueError("Generation trace must identify image_gen.imagegen")
    if trace.get("generator_agent_id") != generation["generator_agent_id"]:
        raise ValueError("Generation trace generator does not match")
    nonempty(trace.get("actual_prompt"), "Actual generation prompt")
    if trace.get("prompt_sha256") != hashlib.sha256(trace["actual_prompt"].encode()).hexdigest():
        raise ValueError("Actual generation prompt hash does not match")
    if (trace.get("output_sha256") != page["cos"]["sha256"]
            or trace.get("output_path") != page["cos"]["path"]):
        raise ValueError("Generation trace output does not match the decoded Cos file")
    nonempty(trace.get("returned_model"), "Returned model or explicit undisclosed label")
    if type(trace.get("attempt")) is not int or trace["attempt"] != generation["correction_attempt"]:
        raise ValueError("Generation trace attempt does not match")
    if trace.get("correction_attempt_id") != generation["correction_attempt_id"]:
        raise ValueError("Generation trace correction attempt ID does not match")
    references = trace.get("references")
    if not isinstance(references, list) or not references:
        raise ValueError("Generation trace requires the actual original image reference")
    originals = [ref for ref in references if isinstance(ref, dict) and ref.get("role") == "current_page_original"]
    if len(originals) != 1 or originals[0].get("sha256") != page["raw"]["sha256"] or originals[0].get("path") != page["raw"]["path"]:
        raise ValueError("Generation trace original reference does not match the decoded raw file")
    for ref in references:
        if not isinstance(ref, dict) or ref.get("role") not in {"current_page_original", "identity_anchor", "current_failed_output"}:
            raise ValueError("Generation trace reference has an invalid role")
        nonempty(ref.get("path"), "Generation reference path")
        sha256(ref.get("sha256"), "Generation reference hash")
        path = Path(ref["path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
            raise ValueError("Generation trace reference file is unavailable or changed")


def page_integrity_reasons(page, pages):
    """Non-visual prerequisites: these can never be waived by best selection."""
    if page.get("pending_correction"):
        return ["correction attempt is pending"]
    features, generation, review = (page.get(key) for key in ("features", "generation", "quality_review"))
    if not features or not generation or not review:
        return ["structured feature inventory, generation provenance and independent review required"]
    if not page.get("raw") or not page.get("cos"):
        return ["decoded Raw and Cos images required for quality review"]
    try:
        validate_features(features, page["raw"]["sha256"])
        if generation.get("features_sha256") != digest(features):
            raise ValueError("Original feature inventory changed after generation")
        if generation.get("raw_sha256") != page["raw"]["sha256"] or generation.get("cos_sha256") != page["cos"]["sha256"]:
            raise ValueError("Generation hashes do not match the current images")
        hashes = {str(p["page_index"]): p["cos"]["sha256"] for p in pages if p["cos"]}
        validate_review(review, features, generation, hashes, len(pages))
        contributors = set()
        for other in pages:
            if other.get("features"):
                contributors.add(other["features"]["analyst_agent_id"])
            if other.get("generation"):
                contributors.update(other["generation"].get("generator_history", []))
                contributors.add(other["generation"]["generator_agent_id"])
        if review["reviewer_agent_id"] in contributors:
            raise ValueError("Independent reviewer cannot be an analyst or generator on any page/version")
    except (ValueError, KeyError, TypeError) as error:
        return [str(error)]
    reasons = []
    try:
        validate_trace(page)
    except (ValueError, KeyError, TypeError, OSError) as error:
        reasons.append(str(error))
    return reasons


def page_quality_reasons(page, pages):
    reasons = page_integrity_reasons(page, pages)
    try:
        reasons.extend(verdict_reasons(page["features"], page["quality_review"], len(pages)))
    except (KeyError, TypeError, AttributeError):
        pass
    return reasons


def quality_score(features, review):
    """Integer 0..10000, equal weights over source-visible or critical checks."""
    keys = [key for key, item in features["checks"].items()
            if item["visibility"] != "not_visible" and (item["visibility"] == "visible" or item["critical"])]
    passed = sum(review["checks"][key]["status"] == "pass" for key in keys)
    return passed * 10000 // len(keys) if keys else 0


def unresolved_checks(features, review):
    return [key for key, item in features["checks"].items()
            if review["checks"][key]["status"] == "fail"
            or (item["visibility"] == "visible" and review["checks"][key]["status"] != "pass")
            or (item["critical"] and (item["visibility"] == "unverifiable"
                                       or review["checks"][key]["status"] == "unverifiable"))]
