"""Explicit selection and observable fallback; benchmarks disable fallback."""

import time
from cad_agent.adapters.model import ModelError
from cad_agent.runtimes.native import NativeRuntime


def get_runtime(name):
    if name == "native":
        return NativeRuntime()
    if name == "responses_subscription":
        from cad_agent.runtimes.responses_subscription import ResponsesSubscriptionRuntime
        return ResponsesSubscriptionRuntime()
    if name == "codex_subscription":
        from cad_agent.runtimes.subscription import CodexSubscriptionRuntime
        return CodexSubscriptionRuntime()
    if name in ("deepseek_harness", "codex"):
        from cad_agent.runtimes.external import ExternalRuntime

        return ExternalRuntime(name)
    raise ValueError("未知 AgentRuntime。")


def run_agent(session, history=None):
    name = session.request.runtime
    subscription_runtimes = {"codex_subscription", "responses_subscription"}
    subscription = session.w.settings.provider in subscription_runtimes
    siwc_external = session.w.settings.provider == "responses_subscription" and name in ("deepseek_harness", "codex")
    if ((subscription and name != session.w.settings.provider and not siwc_external)
            or (not subscription and name in subscription_runtimes)):
        raise ModelError("MODEL_RUNTIME_MISMATCH", "订阅模型需搭配对应的订阅运行时；请区分 Codex 订阅与 ChatGPT 直接订阅。")
    if subscription and session.request.allow_fallback:
        raise ModelError("SUBSCRIPTION_FALLBACK_DISABLED", "订阅问答不自动回退到付费 API，请关闭失败回退选项。")
    attempts = []
    try:
        with session.metrics.measure("runtime_total"):
            from cad_agent.analysis.query_intent import enabled
            result = None
            if not enabled(session):
                from cad_agent.tables.workflow import try_run as try_table
                result = try_table(session, history)
                if result is None:
                    from cad_agent.analysis.source_record_workflow import try_run
                    result = try_run(session, history)
            if result is None:
                result = get_runtime(name).run(session, history)
                if getattr(session, 'source_record_fallback', None):
                    result['source_record_fallback'] = session.source_record_fallback
    except ModelError as error:
        if error.code == "SOURCE_CHANGED" or name == "native" or name in subscription_runtimes or not session.request.allow_fallback:
            raise
        attempts.append(
            {
                "runtime": name,
                "status": "failed",
                "error_code": error.code,
                "trace": list(session.trace),
                "usage": list(session.usage),
            }
        )
        session.progress(
            stage="所选运行时失败，明确回退至原生运行时", runtime_attempts=attempts
        )
        # Fallback changes transport, not task identity, source provenance or budget.
        # Re-read source packets because a fresh model history has not seen the old
        # tool messages; authoritative evidence/receipts remain on the same task.
        original_request = session.request
        session.request = original_request.model_copy(update={"runtime": "native"})
        session.policy.cache.clear()
        session.policy.seen_evidence.clear()
        session.policy.stalled = 0
        if session.policy.stop_reason == "no_new_information":
            session.policy.stop_reason = None
        session.prepared_answer = None
        try:
            session._remaining()
            with session.metrics.measure("fallback_runtime_total"):
                result = NativeRuntime().run(session, history)
        finally:
            session.request = original_request
        result["elapsed_seconds"] = round(time.monotonic() - session.started, 3)
        name = "native"
    return result | {
        "metrics": session.metrics.snapshot(),
        "runtime_requested": session.request.runtime,
        "runtime_used": name,
        "fallback_used": bool(attempts),
        "runtime_attempts": attempts,
    }
