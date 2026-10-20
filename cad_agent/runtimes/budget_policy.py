"""Versioned, progress-aware subscription budgets; estimates != billed usage."""
from dataclasses import dataclass, asdict
import hashlib
import math
import time
from cad_agent.analysis.efficiency import dumps
from cad_agent.runtimes.token_budget import charged_tokens
from cad_agent.adapters.siwc import fail


@dataclass(frozen=True)
class BudgetPolicy:
    name: str = "standard"
    exploration_soft_tokens: int = 600000
    total_ceiling_tokens: int = 1000000
    reserved_finalization_tokens: int = 150000
    input_soft_tokens_per_request: int = 64000
    input_ceiling_tokens_per_request: int = 96000
    text_emergency_chars: int = 300000
    elapsed_soft_seconds: int = 300
    elapsed_ceiling_seconds: int = 600
    finalization_reserve_seconds: int = 120
    rounds_soft: int = 24
    rounds_ceiling: int = 32
    tool_calls_ceiling: int = 40
    max_output_tokens: int = 8192
    final_context_chars: int = 100000

    def public(self):
        return asdict(self) | {"version": "adaptive_v1"}


STANDARD = BudgetPolicy()
DEEP = BudgetPolicy(name="deep", exploration_soft_tokens=750000, total_ceiling_tokens=1200000,
                    elapsed_soft_seconds=420, elapsed_ceiling_seconds=900,
                    finalization_reserve_seconds=180,
                    rounds_soft=32, rounds_ceiling=48, tool_calls_ceiling=64, max_output_tokens=12288)


def policy_for(settings, runtime="responses_subscription"):
    if (getattr(settings, "subscription_budget_policy", "legacy") != "adaptive_v1"
            or settings.provider != "responses_subscription" or runtime != "responses_subscription"):
        return None
    return DEEP if settings.reasoning_effort == "high" else STANDARD


def for_session(session):
    return policy_for(session.w.settings, getattr(getattr(session, "request", None), "runtime", "responses_subscription"))


def text_limit(session):
    policy = for_session(session)
    return policy.text_emergency_chars if policy else 100000


_encoding = None
_encoding_failed = False


def estimate_input(payload):
    """Local tokenizer + 20% margin + envelope reserve; byte bound if unavailable.

    o200k_base is an estimate for this subscription, not an assertion of the
    server's tokenizer. Images use a separate conservative reserve.
    """
    global _encoding, _encoding_failed
    images = 0
    def strip(value):
        nonlocal images
        if isinstance(value, dict):
            if value.get("type") == "input_image":
                images += 1
                return {"type": "input_image"}
            # Encrypted state is opaque; include its bytes conservatively.
            return {k: strip(v) for k, v in value.items()}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    text = dumps(strip(payload))
    if _encoding is None and not _encoding_failed:
        try:
            import tiktoken
            _encoding = tiktoken.get_encoding("o200k_base")
        except Exception:
            _encoding_failed = True
    if _encoding is None:
        tokens, basis = len(text.encode()), "utf8_byte_bound"
    else:
        tokens, basis = math.ceil(len(_encoding.encode(text, disallowed_special=())) * 1.2), "o200k_base_estimate_20pct_margin"
    return tokens + 2048 + images * 8192, basis


def progress(session):
    from cad_agent.analysis.failure_guard import progress_signature
    return progress_signature(session)


def stop_reason(session):
    policy = for_session(session)
    if not policy:
        return None
    tokens = charged_tokens(session)
    elapsed = time.monotonic() - session.started
    requests = getattr(session, "runtime_diagnostics", {}).get("model_requests", [])
    if session.calls >= session.w.settings.agent_max_tool_calls:
        return "tool_call_budget"
    if tokens >= policy.total_ceiling_tokens - policy.reserved_finalization_tokens:
        return "finalization_token_reserve"
    if session._remaining() <= policy.finalization_reserve_seconds:
        return "finalization_time_reserve"
    soft = (tokens >= policy.exploration_soft_tokens or elapsed >= policy.elapsed_soft_seconds
            or len(requests) >= policy.rounds_soft)
    if soft:
        signature = progress(session)
        previous = getattr(session, "_budget_progress", None)
        session._budget_progress = signature
        # The first crossing must also show progress since the prior request.
        if previous is None or signature == previous:
            return "soft_budget_no_progress"
        session.runtime_diagnostics.setdefault("budget_extensions", []).append(
            dict(round=len(requests) + 1, charged_tokens=tokens, reason="new_evidence_or_operation",
                 progress_signature=signature))
    else:
        session._budget_progress = progress(session)
    return None


def preflight(session, payload, entry):
    policy = for_session(session)
    if not policy:
        return
    estimated, basis = estimate_input(payload)
    reserve = 0 if entry.get("final_only") else policy.reserved_finalization_tokens
    # First synthesis also leaves room for its one allowed repair.
    if entry.get("final_only") and not entry.get("answer_repair"):
        reserve = policy.reserved_finalization_tokens // 2
    output = payload.setdefault("max_output_tokens", policy.max_output_tokens)
    entry["budget_preflight"] = dict(input_estimate=estimated, estimate_basis=basis,
        output_ceiling=output, finalization_reserve=reserve, charged_before=charged_tokens(session),
        total_ceiling=policy.total_ceiling_tokens)
    if estimated > policy.input_ceiling_tokens_per_request:
        raise fail("CONTEXT_BUDGET", "单轮输入估算超过上下文上限；原文与几何记录已保留。")
    if charged_tokens(session) + estimated + output + reserve > policy.total_ceiling_tokens:
        raise fail("TOKEN_BUDGET", "请求前预算预留不足，已停止请求；不会通过重试重置预算。")


def prefix_diagnostics(payload):
    def digest(value):
        return hashlib.sha256(dumps(value).encode()).hexdigest()
    return dict(instructions_sha256=digest(payload["instructions"]),
                tools_sha256=digest(payload["tools"]),
                prefix_sha256=digest({k: payload[k] for k in ("model", "instructions", "tools", "reasoning")}))


def cache_summary(usage):
    known = [u for u in usage if type(u.get("prompt_cache_hit_tokens")) is int]
    input_tokens = sum(u.get("prompt_tokens", 0) for u in known)
    cached = sum(u["prompt_cache_hit_tokens"] for u in known)
    return dict(reported_requests=len(known), missing_cache_usage_requests=len(usage)-len(known),
                cached_input_tokens=cached, cache_reported_input_tokens=input_tokens,
                cached_input_ratio=cached / input_tokens if input_tokens else None,
                source="usage.input_tokens_details.cached_tokens",
                billing="subscription_no_dollar_estimate")
