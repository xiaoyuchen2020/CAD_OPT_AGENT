"""Frozen 60-run diagnostic packet replay; no model, source mutation or CAD run."""
import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import sqlite3

from cad_agent.adapters.store import Store
from cad_agent.analysis.efficiency import dumps
from cad_agent.analysis.error_feedback import build_feedback
from cad_agent.analysis.evidence_store import EvidenceRecordRead, read
from cad_agent.analysis.tasks import TaskLedger

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/bottleneck-round3-20261020"
INDEX = ROOT / "artifacts/designer-facts-unlimited-final-20261020/evaluated-results.json"


class ReplayTask(TaskLedger):
    def save(self):
        # Archive records persist separately; never update frozen task metadata.
        pass


def main():
    rows = json.loads(INDEX.read_text())
    assert len(rows) == 60
    store = Store(OUT / "incremental-error-archives")
    report = dict(
        scope="60 frozen real-model trajectories; deterministic error-packet replay only",
        limitation=("Original per-error full task packets were not stored. Historical response_chars are exact trace "
                    "measurements; after packets use the frozen terminal task/evidence registry, not reconstructed "
                    "historical state. This is no new model run, no claim of answer quality or end-to-end speedup."),
        original_failure_guard_sha256="85e975f240785b6887f4b9a0b918768e33c44d59edb52d46afc8b3ea3581ce25",
        archive_omissions=["task.evidence_records (existing durable reference index)", "task.execution_* (non-analysis execution controls, including lease tokens)"],
        source_read_batching=False, budgets_changed=False,
        rows=[], source_hashes={str(INDEX.relative_to(ROOT)): sha256(INDEX.read_bytes()).hexdigest()})
    for row in rows:
        folder = Path(row["evidence_path"])
        run_path, task_path = folder / "run.json", folder / "task.json"
        for path in (run_path, task_path):
            report["source_hashes"][str(path.relative_to(ROOT))] = sha256(path.read_bytes()).hexdigest()
        run, data = json.loads(run_path.read_text()), json.loads(task_path.read_text())
        task = ReplayTask.__new__(ReplayTask)
        task.data = deepcopy(data)
        task.ensure_coverage()
        snapshots = {run["snapshot_id"], *(x["snapshot_id"] for x in run["request"].get("related_drawings", []))}
        with sqlite3.connect(f"file:{folder / 'data/index.sqlite3'}?mode=ro", uri=True) as source:
            for sid in snapshots:
                record = source.execute("SELECT data FROM records WHERE kind='drawing' AND id=?", (sid,)).fetchone()
                assert record, sid
                store.put("drawing", sid, json.loads(record[0]))
        request = SimpleNamespace(**(run["request"] | {"source_read_batching": False}))
        session = SimpleNamespace(
            task=task, evidence=deepcopy(data["evidence"]), observations=deepcopy(data["observations"]),
            request=request, allowed_snapshots=snapshots, room_batches=deepcopy(data["batches"]),
            w=SimpleNamespace(store=store, settings=SimpleNamespace(provider="responses_subscription")),
            focused_enabled=False, answer_field_issues=[], visual_images={})
        result = dict(id=row["id"], failed_calls=0, before_response_chars=0, after_response_chars=0,
                      all_archives_roundtrip=True, errors=[])
        for number, entry in enumerate(run["trace"]):
            if entry["status"] != "failed" or not entry.get("error"):
                continue
            error = ValueError(entry["error"]["message"])
            error.issues = deepcopy(entry["error"].get("issues", []))
            packet = build_feedback(session, entry["tool"], entry.get("arguments", {}),
                                    {"error": deepcopy(entry["error"])}, error, entry.get("task_ids", []))
            ref, content, start = packet["error_record"], "", 0
            while True:
                page = read(session, EvidenceRecordRead(record_id=ref["record_id"], start=start, length=12000))
                content += page["content"]
                if not page["has_more"]:
                    break
                start = page["next_start"]
            restored = json.loads(content)
            assert sha256(content.encode()).hexdigest() == ref["sha256"]
            assert restored["evidence"] == data["evidence"]
            assert restored["observations"] == data["observations"]
            assert restored["exception_message"] == entry["error"]["message"]
            assert restored["field_issues"] == error.issues
            assert not any(k.startswith("execution_") for k in restored["task"])
            before = entry.get("efficiency", {}).get("response_chars")
            after = len(dumps(packet))
            result["failed_calls"] += 1
            if before is not None:
                result["before_response_chars"] += before
                result["after_response_chars"] += after
            result["errors"].append(dict(
                trace_index=number, tool=entry["tool"], before_response_chars=before,
                after_response_chars=after, archive_record_id=ref["record_id"],
                archive_sha256=ref["sha256"], archive_chars=ref["total_chars"],
                field_issue_count=len(error.issues), evidence_count=len(data["evidence"]),
                observation_count=len(data["observations"]), roundtrip_verified=True))
        requests = (run.get("result") or {}).get("model_requests", [])
        result["historical_request_chars"] = sum(r.get("request_chars", 0) for r in requests)
        result["max_historical_request_chars"] = max((r.get("request_chars", 0) for r in requests), default=0)
        result["max_before_error_chars"] = max((e["before_response_chars"] or 0 for e in result["errors"]), default=0)
        result["max_after_error_chars"] = max((e["after_response_chars"] for e in result["errors"]), default=0)
        report["rows"].append(result)
    before = sum(row["before_response_chars"] for row in report["rows"])
    after = sum(row["after_response_chars"] for row in report["rows"])
    report["totals"] = dict(
        runs=len(rows), failed_calls=sum(row["failed_calls"] for row in report["rows"]),
        before_response_chars=before, after_response_chars=after,
        reduction_percent=round(100*(1-after/before), 3) if before else None,
        max_after_packet_chars=max((e["after_response_chars"] for r in report["rows"] for e in r["errors"]), default=0),
        all_archives_roundtrip=True,
        frozen_source_files_unchanged=all(sha256((ROOT / p).read_bytes()).hexdigest() == value
                                        for p, value in report["source_hashes"].items()))
    report["production_file_hashes"] = {
        p: sha256((ROOT / p).read_bytes()).hexdigest() for p in (
            "cad_agent/analysis/error_feedback.py", "cad_agent/analysis/failure_guard.py",
            "cad_agent/analysis/tool_execution.py", "tests/test_incremental_error_feedback.py",
            "scripts/replay_incremental_errors.py")}
    report["regressions"] = [
        dict(log=name, result=(OUT / name).read_text().strip().splitlines()[-1])
        for name in ("incremental-errors-pytest-final.log", "incremental-errors-runtime-compat.log")
        if (OUT / name).is_file()]
    report["environment_limitation"] = (
        "The earlier bridge regression could not bind 127.0.0.1 in this sandbox (PermissionError); "
        "the final targeted suite excludes that one test. No production behavior was bypassed.")
    (OUT / "incremental-errors-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(dumps(report["totals"]))
    print(dumps(next(r for r in report["rows"] if r["id"] == "Q06-B-luna-high") | {"errors": "see report"}))


if __name__ == "__main__":
    main()
