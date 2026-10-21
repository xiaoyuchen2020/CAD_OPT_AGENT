"""Explicit, single-request subscription probe. No DXF, CAD tools, retries or API keys."""
import argparse
import json
import time
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cad_agent.adapters.model import ModelError
from cad_agent.adapters.siwc import SiwcAccount
from cad_agent.runtimes.responses_subscription import ResponsesTransport


PAYLOAD = {"model": "gpt-6.1-sol", "input": [{"role": "user", "content": "Reply exactly: OK"}],
    "tools": [], "tool_choice": "none", "store": False, "stream": True,
    "reasoning": {"effort": "low"}, "include": ["reasoning.encrypted_content"],
    "parallel_tool_calls": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Actually spend subscription usage on one request")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"dry_run": True, "payload": PAYLOAD, "model_requests": 0}, ensure_ascii=False))
        return
    args.output.mkdir(parents=True, exist_ok=True)
    # Refuse repeated execution even if an earlier submission's outcome is unknown.
    with (args.output / "submission.json").open("x") as file:
        json.dump({"payload": PAYLOAD, "request_attempt_budget": 1, "automatic_retries": 0,
                   "paid_api_fallback": False, "submission_attempted": True}, file, indent=2)
    started = time.monotonic()
    def remaining():
        seconds = 30 - (time.monotonic() - started)
        if seconds <= 0: raise ModelError("PROBE_TIMEOUT", "简短订阅探测已超时，未重试。")
        return seconds
    account = SiwcAccount(args.data_dir)
    session = SimpleNamespace(w=SimpleNamespace(settings=SimpleNamespace(model=PAYLOAD["model"])),
        task=SimpleNamespace(data={'subtasks': []}), system_prompt='',
        _remaining=remaining, usage=[], trace=[], progress=lambda **kwargs: None,
        runtime_diagnostics={"model_requests": [{"round": 1, "probe": True}]})
    result = {"status": "not_started", "tool_calls": 0, "paid_api_fallback": False}
    try:
        with account.lease():
            response = ResponsesTransport(account).request(session, PAYLOAD, max_attempts=1)
        result.update(status="completed", model=response["model"], model_access_verified=True)
    except ModelError as error:
        result.update(status="failed", error={"code": error.code, "message": error.message})
    finally:
        result.update(elapsed_seconds=round(time.monotonic()-started, 3), usage=session.usage,
                      diagnostics=session.runtime_diagnostics)
        (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        account.close()
    return 0 if result['status'] == 'completed' else 1


if __name__ == "__main__":
    raise SystemExit(main())
