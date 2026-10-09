"""Synthetic recovery receipts for isolated tests only; never real tool evidence."""

import hashlib
import json
from pathlib import Path
from p2cos_recovery import inspect_recovery


def receipt(root, job_id, index, attempt, kind, *, state=None, **fields):
    binding = {}
    if state:
        binding = next(a for a in inspect_recovery(state.db_path, job_id, index)["attempts"]
                       if a["attempt_id"] == attempt["attempt_id"])
    body = {"job_id": job_id, "page_index": index, "attempt_id": attempt["attempt_id"],
            "generator_agent_id": attempt["generator_agent_id"], "kind": kind,
            "local_request_id": binding.get("local_request_id"), "request_sha256": binding.get("request_sha256"),
            "operator_agent_id": "synthetic-receipt-operator", "statement": "Offline synthetic test fixture only.",
            **fields}
    path = Path(root) / f"{attempt['attempt_id']}-{kind}.json"
    raw = json.dumps(body).encode()
    path.write_bytes(raw)
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def dispatch(state, job_id, index, attempt, prompt, references):
    args = {"attempt_id": attempt["attempt_id"], "generator_agent_id": attempt["generator_agent_id"]}
    state.prepare_correction(job_id, index, **args, actual_prompt=prompt, references=references)
    state.claim_correction_dispatch(job_id, index, **args)
    return args


def save_result(state, root, job_id, index, attempt, trace):
    args = dispatch(state, job_id, index, attempt, trace["actual_prompt"], trace["references"])
    evidence = receipt(root, job_id, index, attempt, "tool_result", state=state, tool_result={"fixture": "synthetic successful return"},
                       prompt_sha256=trace["prompt_sha256"], output_sha256=trace["output_sha256"])
    state.save_correction_result(job_id, index, **args, generation_trace=trace, evidence=evidence)


def fail(state, root, job_id, index, attempt, prompt, reason):
    page = state.get_job(job_id)["pages"][index]
    refs = [{"role": "current_page_original", "path": page["raw"]["path"], "sha256": page["raw"]["sha256"]}]
    args = dispatch(state, job_id, index, attempt, prompt, refs)
    evidence = receipt(root, job_id, index, attempt, "terminal_tool_failure", state=state,
                       tool_result={"fixture": "synthetic explicit terminal tool failure"},
                       terminal=True, outcome="failed", prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
    return state.fail_correction(job_id, index, **args, actual_prompt=prompt, reason=reason, evidence=evidence)
