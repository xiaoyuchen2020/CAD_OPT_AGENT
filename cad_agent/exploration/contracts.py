"""Small JSON contracts shared by the store and Fleet adapter."""

from datetime import datetime, timezone
from hashlib import sha256
import json
from types import SimpleNamespace

from cad_agent.analysis.method_authorization import authorization

SCHEMA_VERSION = "exploration-e0@1"
TERMINAL = frozenset({"completed", "partial", "failed", "cancelled"})


class ExplorationError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


class ExplorationConflict(ExplorationError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(encoded(value).encode()).hexdigest()


def copied(value):
    return json.loads(encoded(value))


def original_authorization(original_user_input, dynamic_methods="off"):
    if not isinstance(original_user_input, str):
        raise ExplorationError("INVALID_ORIGINAL_INPUT", "原始用户输入必须是文本")
    if dynamic_methods not in ("off", "task_verified"):
        raise ExplorationError("INVALID_DYNAMIC_METHODS")
    result = authorization(SimpleNamespace(question=original_user_input, dynamic_methods=dynamic_methods))
    return dict(result, original_input_sha256=sha256(original_user_input.encode()).hexdigest(),
                requested_mode=dynamic_methods, source="original_user_input_and_explicit_switch")


def budget_contract(value=None):
    value = copied(value or {})
    allowed = {"max_case_starts", "max_concurrent_cases", "max_reported_tokens"}
    if set(value) - allowed:
        raise ExplorationError("UNSUPPORTED_BUDGET_FIELD", "E0 仅支持启动次数、并发数和已报告 token 的后续调度门槛")
    result = {"max_case_starts": 10, "max_concurrent_cases": 1, "max_reported_tokens": None} | value
    for key, number in result.items():
        if key == "max_reported_tokens" and number is None:
            continue
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ExplorationError("INVALID_BUDGET", f"{key} 必须为正整数")
    return result


CAPABILITIES = {
    "stage": "E0", "automatic_rule_evaluation": False, "automatic_tool_promotion": False,
    "max_case_starts": "enforced_before_dispatch; retries do not refund a started case",
    "max_concurrent_cases": "enforced_before_dispatch",
    "max_reported_tokens": "subsequent_dispatch_gate_only; unknown usage blocks admission when configured",
    "runtime_budget": "audited hot overlay applies at next model/tool boundary; usage is reported after generation, not an in-flight token ceiling",
    "live_controls": "pause/resume/guidance/configure with revisions, worker-fenced acknowledgement and effective-state readback",
    "external_supervision": "opt-in advisory or guidance; independent review/token budget; cannot grant permissions",
    "hot_sandbox": "subsequent sandbox invocation only; network, mounts and code authorization remain immutable",
    "hot_validator": "additional evidence gate only; mandatory provenance checks always enforced",
    "tool_artifacts": "generated-artifact bundle hashes are verified when present; missing or corrupt payloads remain explicit gaps",
}
