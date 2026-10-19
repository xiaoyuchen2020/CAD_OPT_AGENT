"""Original bounded tool loop, retained as the default runtime."""

import json
from pydantic import ValidationError
from cad_agent.adapters.model import ModelError
from cad_agent.analysis.session import tool_definitions, validation_feedback


class NativeRuntime:
    name = "native"

    def run(self, session, history=None):
        messages = session.build_messages(history)
        final_errors = 0

        # Reserve the last normal round for synthesis, plus one citation repair.
        # Reaching the exploration budget must not discard already-read sources.
        for round_no in range(max(20,session.round_limit) + 1):
            if round_no>session.round_limit:break
            correction_only = round_no == session.round_limit
            final_only = (
                round_no >= session.round_limit - 1
                or session.calls >= session.w.settings.agent_max_tool_calls
                or bool(session.policy.stop_reason)
                or (session.policy.enabled and final_errors > 0)
            )
            if final_only and session.policy.enabled and not session.policy.stop_reason:
                session.policy.stop_reason = "answer_repair" if final_errors else "exploration_budget"
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
                msg, usage = session.w.gateway.chat_turn(
                    session.model_messages(messages, final_only),
                    [] if final_only else session.offered_tools(planning_only=True),
                    timeout=session._remaining(),
                )
            session.usage.append(usage)
            session._remaining()
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            if calls:
                if final_only:
                    break
                for call in calls:
                    session._remaining()
                    if session.calls >= session.w.settings.agent_max_tool_calls:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": json.dumps(
                                    {
                                        "error": {
                                            "code": "AGENT_TOOL_LIMIT",
                                            "message": "探索额度已用完，请根据已读取证据回答。",
                                        }
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        )
                        continue
                    encoded = session.invoke_tool(
                        call["function"]["name"], call["function"]["arguments"]
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": encoded}
                    )
                    if session.prepared_answer is not None:return session.prepared_answer
                continue
            try:
                return session.finish(msg.get("content") or "")
            except (ValueError, ValidationError) as error:
                final_errors += 1
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
