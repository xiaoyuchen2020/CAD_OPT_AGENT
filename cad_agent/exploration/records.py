"""Summaries point to actual archived records, never synthesize drawing facts."""

from hashlib import sha256

from .contracts import copied, digest


def _pointer(base, key):
    return base + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _artifact_refs(value, pointer):
    out = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = _pointer(pointer, key)
            if key.endswith("artifact_id") and isinstance(item, str):
                out.append({"artifact_id": item, "source_pointer": path, "materialization": "reference_only"})
            elif isinstance(item, (dict, list)):
                out.extend(_artifact_refs(item, path))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            out.extend(_artifact_refs(item, _pointer(pointer, i)))
    return out


def _reported_usage(run, result):
    # Choose one owning usage stream; the same usage is often repeated in result.
    usage = run.get("usage") if isinstance(run, dict) else None
    pointer = "/artifacts/run/usage"
    if usage is None and isinstance(result, dict):
        usage, pointer = result.get("usage"), "/result/usage"
    entries = usage if isinstance(usage, list) else [usage] if isinstance(usage, dict) else []
    values = [entry.get("total_tokens") if isinstance(entry, dict) else None for entry in entries]
    known = bool(values) and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in values)
    return {"known": known, "total_tokens": sum(values) if known else None,
            "source_pointer": pointer if usage is not None else None,
            "billing": "reported_usage; no inferred price"}


def summarize_archive(archive, *, archive_ref=None, drawings=None):
    """Accept Fleet's real archive shape and retain source/semantic uncertainty."""
    archive = copied(archive)
    artifacts = archive.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    task = artifacts.get("task")
    task = task if isinstance(task, dict) else {}
    run = artifacts.get("run")
    run = run if isinstance(run, dict) else {}
    issues, evidence, tools, failures = [], [], [], []
    generated = artifacts.get("generated_artifacts")
    generated_items = generated.get("items", []) if isinstance(generated, dict) else []
    exclusions = {item["artifact_id"]: item for item in generated.get("excluded", [])
                  if isinstance(item, dict) and item.get("artifact_id")} if isinstance(generated, dict) else {}
    artifact_index = {}
    if not isinstance(generated_items, list):
        issues.append({"code": "INVALID_GENERATED_ARTIFACT_BUNDLE", "source_pointer": "/artifacts/generated_artifacts"})
        generated_items = []
    for i, item in enumerate(generated_items):
        pointer = f"/artifacts/generated_artifacts/items/{i}"
        if not isinstance(item, dict) or not item.get("artifact_id"):
            issues.append({"code": "INVALID_GENERATED_ARTIFACT", "source_pointer": pointer})
            continue
        artifact_id = item["artifact_id"]
        valid = "value" in item and digest(item["value"]) == item.get("sha256")
        if artifact_id in artifact_index:
            valid = False
            artifact_index[artifact_id]["integrity_verified"] = False
            issues.append({"code": "DUPLICATE_GENERATED_ARTIFACT", "source_pointer": pointer})
        else:
            artifact_index[artifact_id] = {"artifact_id": artifact_id, "kind": item.get("kind"),
                                           "sha256": item.get("sha256"), "source_pointer": pointer,
                                           "integrity_verified": valid}
        if not valid:
            issues.append({"code": "GENERATED_ARTIFACT_INTEGRITY_MISMATCH", "source_pointer": pointer})
    if isinstance(generated, dict):
        for diagnostic in generated.get("diagnostics", []):
            issues.append({"code": "GENERATED_ARTIFACT_EXPORT_DIAGNOSTIC", "diagnostic": copied(diagnostic)})
    imported = artifacts.get("drawings", [])
    imported = imported if isinstance(imported, list) else []
    source_hashes = {item.get("id"): item.get("sha256") for item in imported if isinstance(item, dict) and item.get("id")}
    if drawings is not None:
        if len(imported) != len(drawings):
            issues.append({"code": "DRAWING_ARCHIVE_SCOPE_MISMATCH", "source_pointer": "/artifacts/drawings"})
        for i, (source, frozen) in enumerate(zip(imported, drawings)):
            actual = source.get("original_sha256") or source.get("sha256") if isinstance(source, dict) else None
            if actual != frozen["sha256"]:
                issues.append({"code": "DRAWING_SOURCE_HASH_MISMATCH", "source_pointer": f"/artifacts/drawings/{i}"})
    records = artifacts.get("evidence_records", [])
    if not isinstance(records, list):
        issues.append({"code": "INVALID_EVIDENCE_RECORDS", "source_pointer": "/artifacts/evidence_records"})
        records = []
    for i, record in enumerate(records):
        pointer = f"/artifacts/evidence_records/{i}"
        if not isinstance(record, dict) or not isinstance(record.get("ref"), dict):
            issues.append({"code": "INVALID_EVIDENCE_RECORD", "source_pointer": pointer})
            continue
        ref, content, sources = record["ref"], record.get("content"), record.get("sources")
        content_ok = isinstance(content, str) and sha256(content.encode()).hexdigest() == ref.get("sha256")
        sources_ok = isinstance(sources, dict) and bool(sources) and all(
            sid in source_hashes and isinstance(value, str) and bool(value) and source_hashes[sid] == value
            for sid, value in sources.items())
        if not content_ok or not sources_ok:
            issues.append({"code": "EVIDENCE_INTEGRITY_OR_SOURCE_MISMATCH", "source_pointer": pointer})
        evidence.append({"record_id": ref.get("record_id"), "sha256": ref.get("sha256"),
                         "total_chars": ref.get("total_chars"), "sources": copied(sources),
                         "source_pointer": pointer, "integrity_verified": content_ok and sources_ok,
                         "fact_validation": "not_performed", "coverage_credit": False})
    for collection in ("task_methods", "task_programs", "program_results", "tool_version_routes"):
        values = task.get(collection, {})
        if not isinstance(values, dict):
            continue
        for key, item in values.items():
            if not isinstance(item, dict):
                continue
            pointer = _pointer("/artifacts/task/" + collection, key)
            artifact_refs = _artifact_refs(item, pointer)
            for ref in artifact_refs:
                exclusion = exclusions.get(ref["artifact_id"])
                if exclusion is not None:
                    ref.update(materialization="explicitly_excluded", exclusion=copied(exclusion))
                    continue
                if ref["source_pointer"].rsplit("/", 1)[-1] in ("metadata_artifact_id", "model_artifact_id"):
                    ref["materialization"] = "excluded_model_metadata"
                    continue
                materialized = artifact_index.get(ref["artifact_id"])
                if materialized is not None:
                    ref.update(materialization="archived_verified" if materialized["integrity_verified"] else "archive_integrity_failed",
                               payload_ref=copied(materialized))
                elif generated is not None:
                    ref["materialization"] = "missing_from_archive"
                    issues.append({"code": "REFERENCED_GENERATED_ARTIFACT_MISSING", "artifact_id": ref["artifact_id"], "source_pointer": ref["source_pointer"]})
            tools.append({"kind": collection, "record_id": key, "status": item.get("status"),
                          "version": item.get("version"), "source_pointer": pointer,
                          "record_sha256": digest(item), "artifact_refs": artifact_refs,
                          "automatic_registration": False, "promotion_status": "not_evaluated"})
            if item.get("error"):
                failures.append({"kind": collection, "error": copied(item["error"]), "source_pointer": pointer})
            for i, attempt in enumerate(item.get("attempts", [])):
                if isinstance(attempt, dict) and (attempt.get("error") or attempt.get("status") in ("rejected", "failed", "abstained")):
                    failures.append({"kind": collection + "_attempt", "status": attempt.get("status"),
                                     "error": copied(attempt.get("error")), "source_pointer": pointer + f"/attempts/{i}"})
    if archive.get("error"):
        failures.append({"kind": "execution", "error": copied(archive["error"]), "source_pointer": "/error"})
    required_refs = [ref for tool in tools for ref in tool["artifact_refs"]
                     if ref["materialization"] not in ("excluded_model_metadata", "explicitly_excluded")]
    if generated is None and required_refs:
        issues.append({"code": "GENERATED_ARTIFACT_BUNDLE_UNAVAILABLE", "source_pointer": "/artifacts/generated_artifacts"})
    if isinstance(generated, dict) and generated.get("complete") is False:
        issues.append({"code": "GENERATED_ARTIFACT_EXPORT_INCOMPLETE", "source_pointer": "/artifacts/generated_artifacts"})
    result = archive.get("result") if archive.get("result") is not None else run.get("result")
    return {
        "archive_sha256": digest(archive), "archive_ref": archive_ref,
        "job_id": archive.get("job_id"), "attempt_id": archive.get("attempt_id"),
        "task_id": task.get("task_id") or run.get("task_id"),
        "execution_status": archive.get("execution_status"), "delivery_status": archive.get("delivery_status"),
        "fact_package": {"kind": "archived_evidence_references", "records": evidence,
                         "semantic_validation": "not_performed", "rule_completion_credit": False},
        "tool_artifacts": tools, "failures": failures, "validation_issues": issues,
        "generated_artifact_manifest": list(artifact_index.values()),
        "generated_artifact_summary": {"bundle_present": generated is not None,
                                       "referenced": len({r["artifact_id"] for r in required_refs}),
                                       "verified": len({r["artifact_id"] for r in required_refs if r["materialization"] == "archived_verified"})},
        "usage": _reported_usage(run, result),
        "result_ref": {"source_pointer": "/result" if archive.get("result") is not None else "/artifacts/run/result",
                       "sha256": digest(result), "kind": "unvalidated_exploration_output"} if result is not None else None,
        "rule_completed": False, "rule_assessment_status": "not_evaluated",
    }
