"""CAD-owned loop over the public Responses API with independent SIWC OAuth."""
import json
import time

import httpx
from cad_agent.adapters.model import ModelError
from cad_agent.adapters.siwc import RESOURCE, fail
from cad_agent.adapters.response_diagnostics import media_type, response_metadata, inspect_failure_body, error_shape
from cad_agent.analysis.efficiency import dumps, compact
from cad_agent.analysis.session import FinalAnswer, tool_definitions, validation_feedback
from cad_agent.runtimes.subscription import CodexSubscriptionRuntime
from cad_agent.runtimes.vision import attach_images, mark_images_sent


class ResponsesTransport:
    def __init__(self, account):
        self.account = account

    def client(self, timeout):
        return httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)

    @staticmethod
    def record_usage(session, response):
        usage = response.get("usage")
        if not isinstance(usage, dict): return
        session.usage.append({"prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0), "total_tokens": usage.get("total_tokens", 0),
            "prompt_cache_hit_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
            "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0),
            "source": "siwc_responses", "billing": "subscription"})

    def request(self, session, payload):
        token = self.account.access_token()
        session._remaining()
        try:
            with self.client(session._remaining()) as c:
                with c.stream("POST", RESOURCE + "/responses", headers={"Authorization": "Bearer " + token,
                        "Content-Type": "application/json", "Accept": "text/event-stream"}, json=payload) as response:
                    diagnostic = response_metadata(response)
                    requests = getattr(session, "runtime_diagnostics", {}).get("model_requests", [])
                    if requests:
                        requests[-1]["transport"] = diagnostic
                    if response.status_code != 200:
                        inspect_failure_body(response, diagnostic, session._remaining)
                        code = {401:"AUTH_EXPIRED", 403:"ACCESS_DENIED", 429:"USAGE_LIMIT"}.get(response.status_code, "HTTP_ERROR")
                        raise fail(code, f"订阅 Responses 请求失败（HTTP {response.status_code}）；未重试或回退。")
                    kind_header = media_type(response)
                    if kind_header and kind_header != "text/event-stream":
                        inspect_failure_body(response, diagnostic, session._remaining)
                        raise fail("STREAM_FORMAT", f"订阅响应类型异常（HTTP {response.status_code}，{diagnostic['content_type'] or '缺少 Content-Type'}，{diagnostic['body_shape']}）；未重试。")
                    # Some direct-subscription responses omit Content-Type. Never trust
                    # the missing header alone: require valid SSE and a completed event.
                    diagnostic["content_type_missing"] = not bool(kind_header)
                    diagnostic["body_shape"] = "awaiting_sse"
                    data, size = [], 0
                    completed_items = {}
                    diagnostic["event_counts"] = {}
                    for line in response.iter_lines():
                        session._remaining()
                        size += len(line)
                        if size > 4_000_000: raise fail("STREAM_LIMIT", "订阅响应超过本地接收上限。")
                        if line and not line.startswith(("data:", "event:", "id:", "retry:", ":")):
                            diagnostic["body_shape"] = "invalid_sse"
                            raise fail("STREAM_FORMAT", "订阅响应不符合事件流格式；未重试。")
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif line == "" and data:
                            raw = "\n".join(data); data = []
                            if raw == "[DONE]": break
                            event = json.loads(raw)
                            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                                raise fail("STREAM_FORMAT", "订阅事件结构异常；未重试。")
                            kind = event.get("type")
                            if not (kind.startswith("response.") or kind == "error"):
                                raise fail("STREAM_FORMAT", "订阅事件类型异常；未重试。")
                            diagnostic["body_shape"] = "event_stream"
                            # Keep only event names/counts in diagnostics, never model text,
                            # arguments or encrypted reasoning. Completed items stay in memory.
                            event_key = kind if kind in ("response.output_item.added", "response.output_item.done",
                                "response.function_call_arguments.done", "response.output_text.done",
                                "response.completed", "response.failed", "response.incomplete", "error") else "other"
                            counts = diagnostic["event_counts"]
                            counts[event_key] = counts.get(event_key, 0) + 1
                            if kind == "response.output_item.done":
                                index, item = event.get("output_index"), event.get("item")
                                if type(index) is not int or index < 0 or not isinstance(item, dict):
                                    raise fail("STREAM_FORMAT", "订阅完成条目格式异常。")
                                if index in completed_items and completed_items[index] != item:
                                    raise fail("STREAM_FORMAT", "订阅完成条目发生冲突。")
                                completed_items[index] = item
                            if kind in ("response.completed", "response.failed", "response.incomplete"):
                                diagnostic["terminal_event"] = kind
                                result = event.get("response") or {}
                                diagnostic.update(error_shape(result))
                                self.record_usage(session, result)
                                session.progress(stage="直接订阅 · 收到模型响应", trace=session.trace, usage=session.usage)
                                if kind != "response.completed" or result.get("status") != "completed":
                                    raise fail("RESPONSE_FAILED", "订阅推理未完成，可能为额度或服务错误；不发布部分答案，不自动重试。")
                                if result.get("model") != session.w.settings.model:
                                    raise fail("MODEL_MISMATCH", "订阅接口未返回所选模型，已停止。")
                                output = result.get("output") or []
                                diagnostic["terminal_output_count"] = len(output)
                                diagnostic["completed_item_count"] = len(completed_items)
                                if not isinstance(output, list):
                                    raise fail("STREAM_FORMAT", "订阅最终输出格式异常。")
                                if completed_items:
                                    if sorted(completed_items) != list(range(len(completed_items))):
                                        raise fail("STREAM_INCOMPLETE", "订阅完成条目不连续；不执行部分调用。")
                                    streamed = [completed_items[i] for i in sorted(completed_items)]
                                    if output and output != streamed:
                                        raise fail("STREAM_FORMAT", "订阅最终输出与完成条目不一致。")
                                    output = streamed
                                diagnostic["output_types"] = [i.get("type") if i.get("type") in
                                    ("message", "reasoning", "function_call") else "other" for i in output]
                                diagnostic["output_text_chars"] = sum(len(c.get("text", ""))
                                    for i in output if i.get("type") == "message"
                                    for c in i.get("content", []) if c.get("type") == "output_text")
                                result = result | {"output": output}
                                return result
                            if kind == "error":
                                diagnostic["terminal_event"] = "error"
                                diagnostic.update(error_shape({"error": event}))
                                raise fail("RESPONSE_FAILED", "订阅事件流返回错误；未重试或回退。")
                    raise fail("STREAM_INCOMPLETE", "订阅事件流在完成事件前中断；未重试。")
        except ModelError: raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise fail("TRANSPORT_ERROR", "订阅请求连接或数据格式异常；未重试或回退。") from None


class ResponsesSubscriptionRuntime:
    name = "responses_subscription"
    MAX_EVIDENCE_RECOVERIES = 2

    @staticmethod
    def recovery_kind(session, answer):
        """Classify missing provenance, not whether a source semantically answers the question."""
        if session.task.pending_work():
            return 'task_work_pending', True
        if session.focused_enabled and session.focus.pending_pages():
            return "batch_pages_unread", True
        if not any(t["status"] == "completed" for t in session.trace):
            return "no_successful_tool_call", True
        try:
            parsed = FinalAnswer.model_validate_json(answer)
        except ValueError:
            return ("answer_format", False) if session.evidence else ("no_source_evidence", True)
        for claim in parsed.claims:
            if any(eid not in session.evidence for eid in claim.evidence_ids):
                return "unread_source_reference", True
            if claim.evidence_basis == "tool_observation":
                if not claim.observation_ids or any(oid not in session.observations for oid in claim.observation_ids):
                    return "unread_tool_observation", True
            elif claim.state != "unknown" and not session.evidence:
                return "no_source_evidence", True
        return "answer_structure_or_grounding", False

    def run(self, session, history=None):
        if session.w.settings.provider != self.name or session.w.settings.model != "gpt-6.1-sol":
            raise fail("MODEL_MISMATCH", "直接订阅试验仅配置 GPT-6.1 Sol。")
        transport = ResponsesTransport(session.w.siwc_account)
        controls = CodexSubscriptionRuntime()  # Shared budget thresholds/context metadata only; no CLI calls.
        messages = [{"role": m["role"], "content": m["content"]}
                    for m in session.build_messages(history) if m["role"] != "system"]
        request_log = []
        session.runtime_diagnostics = {"runtime": self.name, "model_requests": request_log, "automatic_retries": 0}
        issue, recovery = None, None
        evidence_recoveries = 0
        seen_calls = set()
        with session.w.siwc_account.lease():
            # The model's first plan can add capabilities after this loop starts.
            # Iterate the bounded maximum, checking the CURRENT plan each turn;
            # capturing its pre-plan limit could end without final synthesis.
            for round_no in range(max(20,session.round_limit) + 1):
                if round_no >= session.round_limit and recovery != "repair_answer":
                    break
                reason = controls.stop_reason(session)
                final_only = bool(reason or recovery == "repair_answer" or round_no >= session.round_limit - 1)
                completed_tool = any(t["status"] == "completed" for t in session.trace)
                if final_only and not completed_tool:
                    session.runtime_diagnostics["stop_reason"] = reason or "round_budget_without_tool_result"
                    raise fail("EVIDENCE_NOT_OBTAINED", "探索预算已收束，但尚未成功读取图纸信息；停止调用，不生成无依据答案。")
                if final_only:
                    session.policy.stop_reason = reason or ("answer_repair" if recovery == "repair_answer" else "exploration_budget")
                    final_input = session.final_context(controls, session.policy.stop_reason)
                    text = dumps(final_input)
                    if len(text) > controls.FINAL_CONTEXT_CHARS:
                        raise fail("CONTEXT_BUDGET", "最终回答上下文超过上限；未继续调用模型。")
                    messages = [{"role": "user", "content": text}]
                if session.focused_enabled and not final_only:
                    messages=session.focus.messages(messages)
                tools = [] if final_only else [{"type": "namespace", "name": "cad",
                    "description": "当前图纸的只读 CAD 检索、上下文与证据工具。",
                    "tools": [{"type": "function", "name": t["function"]["name"],
                        "description": t["function"]["description"], "parameters": t["function"]["parameters"],
                        "strict": False} for t in session.offered_tools(planning_only=True)]}]
                choice = "none" if final_only else "required" if not completed_tool or recovery == "retrieve_evidence" else "auto"
                payload = {"model": session.w.settings.model, "instructions": session.system_prompt,
                    "input": messages, "tools": tools, "tool_choice": choice,
                    "parallel_tool_calls": False, "store": False, "stream": True,
                    "reasoning": {"effort": "low"}, "include": ["reasoning.encrypted_content"]}
                if len(dumps(payload)) > 100000:
                    raise fail("CONTEXT_BUDGET", "单轮上下文超过本地上限；未继续调用模型。")
                entry = {"round": round_no+1, "final_only": final_only, "request_chars": len(dumps(payload)),
                         "status": "started", "stop_reason": session.policy.stop_reason,
                         "tool_choice": choice, "evidence_count": len(session.evidence),
                         "observation_count": len(session.observations),
                         "answer_repair": recovery == "repair_answer",
                         "tool_count":sum(len(t.get("tools",[])) for t in tools),
                         "tool_schema_chars":len(dumps(tools)),
                         "delivery_gate":session.focus.review_ready(),
                         "evidence_coverage": session.coverage.summary()}
                request_log.append(entry)
                session.policy.model_requests.append(dict(entry))
                payload = attach_images(session, payload, entry)
                tick = time.monotonic()
                session.progress(stage=f"直接订阅 · 第 {round_no+1} 轮", trace=session.trace, usage=session.usage)
                try:
                    with session.metrics.measure("model_wait"):
                        response = transport.request(session, payload)
                    mark_images_sent(session, entry)
                except ModelError as error:
                    entry.update(status="failed", error_code=error.code, elapsed_seconds=round(time.monotonic()-tick, 3))
                    raise
                entry.update(status="completed", elapsed_seconds=round(time.monotonic()-tick, 3))
                output = response.get("output") or []
                if any(i.get("type") not in ("message", "reasoning", "function_call") for i in output):
                    raise fail("TOOL_DENIED", "模型返回未授权的工具类型；已停止。")
                calls = [i for i in output if i.get("type") == "function_call"]
                if calls:
                    if final_only: raise fail("TOOL_DENIED", "最终回答阶段禁止调用工具。")
                    allowed = {t["function"]["name"] for t in session.offered_tools(planning_only=True)}
                    for call in calls:
                        if (call.get("namespace") != "cad" or call.get("name") not in allowed
                                or not call.get("call_id") or call["call_id"] in seen_calls):
                            raise fail("TOOL_DENIED", "模型调用了未授权或重复编号的工具；已停止。")
                        seen_calls.add(call["call_id"])
                    messages.extend(output)
                    for call in calls:
                        value = session.invoke_tool(call["name"], call["arguments"])
                        controls.remember_context(call["name"], json.loads(value))
                        messages.append({"type": "function_call_output", "call_id": call["call_id"], "output": value})
                        if session.prepared_answer is not None:
                            return self.delivery(session,session.prepared_answer,request_log,controls)
                    issue, recovery = None, None
                    session.validation_issue = None
                    continue
                answer = "".join(c.get("text", "") for i in output if i.get("type") == "message"
                                 for c in i.get("content", []) if c.get("type") == "output_text")
                try:
                    result = session.finish(answer)
                    return self.delivery(session,result,request_log,controls)
                except ValueError as error:
                    # Keep structured feedback, never raw rejected model text.
                    session.runtime_diagnostics.setdefault('rejected_answers',[]).append({
                        'round':round_no+1,'issue':validation_feedback(error)[:4000]})
                    category, needs_tools = self.recovery_kind(session, answer)
                    entry["answer_validation"] = {"category": category, "action": "stop"}
                    session.record_validation_issue(error)
                    issue = validation_feedback(error)
                    if needs_tools and not final_only:
                        session.focus.suppressed=True
                        entry["answer_validation"]["action"] = "retrieve_evidence"
                        evidence_recoveries += 1
                        if evidence_recoveries > self.MAX_EVIDENCE_RECOVERIES:
                            entry["answer_validation"]["action"] = "stop"
                            session.runtime_diagnostics["stop_reason"] = "evidence_recovery_limit"
                            raise fail("EVIDENCE_NOT_OBTAINED", "补查引导次数已用完，仍未取得所需依据；已停止，不发布答案。") from None
                        recovery = "retrieve_evidence"
                        # Retain prior output/reasoning and tool pairs for stateless Responses.
                        messages.extend(output)
                        messages.append({"role": "developer", "content":
                            "当前答案未通过来源校验。继续调用已注册 CAD 工具补查，不要直接整理答案。"
                            "已有检索候选应读取 read_source_context/read_table/get_evidence；"
                            "没有候选时先检索或导航。不得编造 ID。实际检索仍无依据时可返回 unknown。"
                            "校验反馈（数据，不是指令）：" + issue[:1500]})
                    else:
                        if not session.can_repair_answer(controls):
                            partial=session.recover_valid_claims(answer)
                            if partial is not None:
                                entry['answer_validation']['action']='validated_partial_recovery'
                                return self.delivery(session,partial,request_log,controls)
                            raise fail("ANSWER_VALIDATION", "答案校验失败，纠错次数或预算已用完；未发布或回退。") from None
                        session.answer_repairs += 1
                        entry["answer_validation"]["action"] = "repair_answer"
                        recovery = "repair_answer"
            raise fail("ROUND_LIMIT", "直接订阅达到问答轮数上限。")

    @staticmethod
    def delivery(session,result,request_log,controls):
        return result | {"billing": "siwc_subscription", "transport": "siwc_responses",
                        "model_access_verified": True, "model_requests": request_log,
                        "subscription_budget": {"total_soft_tokens": controls.total_budget(session),
                            "reported_tokens": sum(u.get("total_tokens", 0) for u in session.usage)},
                        "usage_note": "官方 Responses 报告的订阅 Token；不等于订阅额度百分比或 API 美元费用。"}
