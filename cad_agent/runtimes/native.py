"""Original bounded tool loop, retained as the default runtime."""

from pydantic import ValidationError
from cad_agent.adapters.model import ModelError
from cad_agent.analysis.session import tool_definitions, validation_feedback
from cad_agent.harness.driver import HarnessDriver
from cad_agent.harness.model_adapter import ChatAdapter


class NativeRuntime:
    name = "native"

    def run(self, session, history=None):
        messages = session.build_messages(history)
        final_errors = 0
        repairing = False
        driver = HarnessDriver(session, ChatAdapter())

        # Reserve the last normal round for synthesis, plus one citation repair.
        # Reaching the exploration budget must not discard already-read sources.
        for round_no in range(max(20,session.round_limit) + 1):
            if round_no>session.round_limit:break
            messages = driver.inputs(messages)
            if driver.input_changed:
                repairing = False
            decision = driver.decision(round_no, repairing=repairing)
            correction_only, final_only = decision.correction_only, decision.final_only
            if final_only and session.policy.enabled and not session.policy.stop_reason:
                session.policy.stop_reason = "answer_repair" if repairing else "exploration_budget"
            if correction_only:
                if final_errors != 1:
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": "这是最后一次答案纠正机会。请按校验反馈修正已读取来源的引用，只返回最终 JSON，不再调用工具。",
                    }
                )
            elif final_only:
                messages.append(
                    {
                        "role": "user",
                        "content": "工具探索额度已用完，现在必须回答。仅依据已读取的证据返回最终 JSON；能确定的先回答，不足部分标 unknown，不再调用工具。不要把工具上限作为用户问题过宽的依据。",
                    }
                )
            session.progress(
                stage=(
                    "正在整理已有证据并回答"
                    if final_only
                    else f"模型分析与工具选择 · 第 {round_no+1} 轮"
                ),
                trace=session.trace,
                usage=session.usage,
            )
            with session.metrics.measure("model_wait"):
                model_messages = session.model_messages(messages, final_only)
                offered = [] if final_only else session.offered_tools(planning_only=True)
                eligible = {t['function']['name'] for t in tool_definitions(session.policy.enabled, session)} if not final_only else set()
                driver.begin_step(round_no, offered, final_only, admission_names=eligible)
                msg, usage = driver.model_call(
                    {'messages': model_messages, 'tools': offered},
                    lambda: session.w.gateway.chat_turn(model_messages, offered, timeout=session._remaining()),
                    title=f'模型请求 · 第 {round_no+1} 轮')
            session.usage.append(usage)
            session._remaining()
            messages.append(msg)
            calls = driver.adapter.calls(msg)
            if driver.discard_for_steering(calls, messages):
                repairing = False
                continue
            if calls:
                if final_only:
                    break
                names = {t['function']['name'] for t in offered}
                prepared = driver.execute_calls(calls, messages, allowed=eligible, advertised=names, eligible=eligible)
                if prepared is not None:
                    return prepared
                continue
            try:
                result = session.finish(msg.get("content") or "")
                if not driver.ready_for_delivery():
                    continue
                return result
            except (ValueError, ValidationError) as error:
                if driver.pending():
                    continue
                final_errors += 1
                repairing = True
                category, needs_tools = driver.recovery_kind(session, msg.get("content") or "")
                session.runtime_diagnostics.setdefault("answer_validation", []).append({
                    "category": category, "needs_tools": needs_tools,
                    "action": "repair_answer" if final_errors < 2 else "stop",
                    "policy": "native_bounded_repair"})
                issue = validation_feedback(error)
                session.progress(
                    stage="答案引用校验未通过，正在纠正", validation_issue=issue
                )
                if final_errors >= 2:
                    raise ModelError(
                        "ANSWER_VALIDATION_FAILED",
                        "模型答案未通过引用或格式校验，未发布该答案；请查看工具记录或调整问题。",
                    ) from None
                messages.append(
                    {
                        "role": "user",
                        "content": "答案未通过服务端校验："
                        + issue
                        + " 请返回约定 JSON；所有非 unknown 结论引用已读取 evidence_ids，所有数值来自引用原文。未知信息明确标记 unknown。",
                    }
                )
        raise ModelError(
            "AGENT_ROUND_LIMIT",
            "模型在轮数上限内未按要求完成回答；已保留工具记录，可重试本问题。",
        )
