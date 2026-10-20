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
from cad_agent.runtimes.token_budget import charged_tokens
from cad_agent.runtimes.deadline_io import DeadlineIO, DeadlineResponse
from cad_agent.execution import ExecutionCancelled
from cad_agent.runtimes.budget_policy import (for_session, text_limit, preflight,
    estimate_input, prefix_diagnostics, cache_summary)


class ResponsesTransport:
    def __init__(self, account):
        self.account = account

    def client(self, timeout):
        return httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)

    @staticmethod
    def record_usage(session, response):
        usage = response.get("usage")
        if not isinstance(usage, dict) or type(usage.get('total_tokens')) is not int or usage['total_tokens'] < 0: return
        session.usage.append({"prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0), "total_tokens": usage.get("total_tokens", 0),
            "prompt_cache_hit_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens"),
            "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0),
            "source": "siwc_responses", "billing": "subscription"})
        requests = getattr(session, "runtime_diagnostics", {}).get("model_requests", [])
        if requests:
            cached = session.usage[-1]["prompt_cache_hit_tokens"]
            requests[-1]["cache_usage"] = dict(input_tokens=usage.get("input_tokens"),
                cached_tokens=cached, cache_hit=(cached > 0 if type(cached) is int else None),
                source="usage.input_tokens_details.cached_tokens")

    def request(self, session, payload):
        """At most one retry of an explicit pre-stream service rejection.

        Unknown billing is a reservation, never reported usage. Retry attempts
        consume the same request budget and deadline; partial SSE is not replayed.
        """
        runtime = getattr(session, 'runtime_diagnostics', None)
        if runtime is None:
            runtime = session.runtime_diagnostics = {}
        requests = runtime.setdefault('model_requests', [])
        if not requests:
            requests.append({})
        entry = requests[-1]
        attempts = entry.setdefault('transport_attempts', [])
        controls = CodexSubscriptionRuntime()
        for attempt_no in range(2):
            session._remaining()
            preflight(session, payload, entry)
            # Estimates reserve UNKNOWN failed-attempt usage only. They must
            # never reject an otherwise valid initial request. In particular,
            # base64 image bytes are not text tokens: an image attempt with
            # unknown billing reserves the remaining budget and cannot retry.
            def has_image(value):
                if isinstance(value,dict):
                    return value.get('type')=='input_image' or any(has_image(v) for v in value.values())
                return isinstance(value,list) and any(has_image(v) for v in value)
            image_input = has_image(payload)
            input_reserve = (entry.get("budget_preflight", {}).get("input_estimate")
                             or (0 if image_input else len(dumps(payload).encode('utf-8'))))
            output_reserve = payload.get("max_output_tokens", controls.FINAL_CONTEXT_CHARS)
            remaining_tokens = controls.total_budget(session)-charged_tokens(session)
            if remaining_tokens < 1:
                raise fail('TOKEN_BUDGET', '本次订阅剩余预算不足，未继续请求或重试。')
            diagnostic = dict(attempt=attempt_no+1, status='started', output_started=False,
                tools_executed=False, request_started=False, unknown_usage=False, reserved_tokens=0)
            attempts.append(diagnostic)
            tick = time.monotonic()
            usage_count = len(session.usage)
            try:
                result = self._request_once(session, payload, diagnostic)
                diagnostic['status'] = 'completed'
                return result
            except ModelError as error:
                diagnostic.update(status='failed', failure_code=error.code)
                retryable = (attempt_no == 0 and diagnostic.get('http_status') == 503
                    and diagnostic.get('body_shape') == 'json_object'
                    and not diagnostic.get('body_truncated')
                    and error.code == 'SIWC_HTTP_ERROR'
                    and diagnostic.get('error_code_from_body') == 'server_error'
                    and not set(diagnostic.get('recognized_fields', [])) & {'output','usage','status','object'}
                    and not diagnostic['output_started'] and not image_input)
                # Reserve the failed attempt before evaluating the retry below.
                diagnostic['retry_candidate'] = retryable
                if not retryable:
                    raise
            except ExecutionCancelled:
                diagnostic.update(status='cancelled', failure_code='EXECUTION_CANCELLED', retry_candidate=False)
                raise
            finally:
                diagnostic['elapsed_seconds'] = round(time.monotonic()-tick,3)
                if diagnostic['request_started'] and len(session.usage) == usage_count:
                    reserve = input_reserve+output_reserve if diagnostic.get('retry_candidate') else remaining_tokens
                    diagnostic.update(unknown_usage=True, reserved_tokens=reserve,
                        reservation_basis='text_input_estimate_plus_output_policy_reserve_not_hard_cap' if diagnostic.get('retry_candidate') else 'remaining_budget_unknown_usage')
                    runtime['reserved_unknown_usage_tokens'] = runtime.get('reserved_unknown_usage_tokens',0)+reserve
                runtime['unknown_usage'] = bool(runtime.get('reserved_unknown_usage_tokens',0))
                diagnostic['usage_status'] = ('reported' if len(session.usage) > usage_count else
                    'unknown' if diagnostic['request_started'] else 'not_requested')
                entry['transport'] = diagnostic
            if (session._remaining() < 15 or charged_tokens(session)+input_reserve+output_reserve >= controls.total_budget(session)):
                diagnostic['retry_blocked'] = 'remaining_time_or_token_budget'
                raise fail('HTTP_ERROR', '订阅服务暂时不可用，剩余预算不足以重试。')
            diagnostic['retry_scheduled'] = True
            runtime['automatic_retries'] = runtime.get('automatic_retries',0)+1
        raise fail('HTTP_ERROR', '订阅服务暂时不可用，有限重试已结束。')

    def _request_once(self, session, payload, diagnostic):
        try:
            with DeadlineIO(session._remaining, diagnostic) as io:
                token = io.call(self.account.access_token, "authentication")
                timeout = (httpx.Timeout(connect=min(15, session._remaining()),
                    read=min(120, session._remaining()), write=min(120, session._remaining()),
                    pool=min(15, session._remaining())) if for_session(session) else session._remaining())
                c = io.enter(self.client(timeout), "client_open")
                diagnostic['request_started'] = True
                response = io.enter(c.stream("POST", RESOURCE + "/responses", headers={"Authorization": "Bearer " + token,
                    "Content-Type": "application/json", "Accept": "text/event-stream"}, json=payload), "response_headers", response=True)
                diagnostic.update(response_metadata(response))
                requests = getattr(session, "runtime_diagnostics", {}).get("model_requests", [])
                if requests:
                    requests[-1]["transport"] = diagnostic
                if response.status_code != 200:
                    inspect_failure_body(DeadlineResponse(response, io), diagnostic, session._remaining)
                    session._remaining()
                    diagnostic['error_code_from_body'] = diagnostic.get('error_code')
                    code = {401:"AUTH_EXPIRED", 403:"ACCESS_DENIED", 429:"USAGE_LIMIT"}.get(response.status_code, "HTTP_ERROR")
                    raise fail(code, f"订阅 Responses 请求失败（HTTP {response.status_code}）；不会回退模型。")
                kind_header = media_type(response)
                if kind_header and kind_header != "text/event-stream":
                    inspect_failure_body(DeadlineResponse(response, io), diagnostic, session._remaining)
                    session._remaining()
                    raise fail("STREAM_FORMAT", f"订阅响应类型异常（HTTP {response.status_code}，{diagnostic['content_type'] or '缺少 Content-Type'}，{diagnostic['body_shape']}）；未重试。")
                # Some direct-subscription responses omit Content-Type. Never trust
                # the missing header alone: require valid SSE and a completed event.
                diagnostic["content_type_missing"] = not bool(kind_header)
                diagnostic["body_shape"] = "awaiting_sse"
                data, size = [], 0
                completed_items = {}
                diagnostic["event_counts"] = {}
                for line in io.iterate(response.iter_lines(), "stream_read"):
                    session._remaining()
                    size += len(line)
                    if size > 4_000_000: raise fail("STREAM_LIMIT", "订阅响应超过本地接收上限。")
                    if line and not line.startswith(("data:", "event:", "id:", "retry:", ":")):
                        diagnostic["body_shape"] = "invalid_sse"
                        raise fail("STREAM_FORMAT", "订阅响应不符合事件流格式；未重试。")
                    if line.startswith("data:"):
                        diagnostic['output_started'] = True
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
                            if not isinstance(result, dict):
                                raise fail("STREAM_FORMAT", "订阅终止事件格式异常；未重试。")
                            status = result.get('status')
                            diagnostic['response_status'] = status if isinstance(status, str) and status in {
                                'completed', 'failed', 'incomplete', 'cancelled', 'in_progress', 'queued'} else 'unknown'
                            details = result.get('incomplete_details')
                            reason = details.get('reason') if isinstance(details, dict) else None
                            diagnostic['incomplete_reason'] = reason if isinstance(reason, str) and reason in {
                                'max_output_tokens', 'content_filter'} else 'other_redacted' if reason is not None else 'unknown'
                            diagnostic['completed_item_count'] = len(completed_items)
                            terminal_output = result.get('output')
                            diagnostic['terminal_output_count'] = len(terminal_output) if isinstance(terminal_output, list) else None
                            diagnostic.update(error_shape(result))
                            self.record_usage(session, result)
                            session.progress(stage="直接订阅 · 收到模型响应", trace=session.trace, usage=session.usage)
                            if kind != "response.completed" or result.get("status") != "completed":
                                raise fail("RESPONSE_FAILED", "订阅推理未完成；原因以安全诊断字段为准，不发布部分答案，不自动重试。")
                            if result.get("model") != session.w.settings.model:
                                raise fail("MODEL_MISMATCH", "订阅接口未返回所选模型，已停止。")
                            output = result.get("output") or []
                            if not isinstance(output, list):
                                raise fail("STREAM_FORMAT", "订阅最终输出格式异常。")
                            diagnostic["terminal_output_count"] = len(output)
                            diagnostic["completed_item_count"] = len(completed_items)
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
        if any(item.get('blocking', True) for item in session.task.pending_work()):
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
        if (session.w.settings.provider != self.name or session.w.settings.model not in {"gpt-6.1-sol", "gpt-6-luna"}
                or session.w.settings.reasoning_effort not in {"low", "high"}):
            raise fail("MODEL_MISMATCH", "直接订阅模型或推理档位不在已配置范围内。")
        transport = ResponsesTransport(session.w.siwc_account)
        controls = CodexSubscriptionRuntime()  # Shared budget thresholds/context metadata only; no CLI calls.
        budget = for_session(session)
        if budget:
            controls.FINAL_CONTEXT_CHARS = budget.final_context_chars
            session.policy.addressable_context = True
        messages = [{"role": m["role"], "content": m["content"]}
                    for m in session.build_messages(history) if m["role"] != "system"]
        request_log = []
        previous_diagnostics = getattr(session, "runtime_diagnostics", {})
        session.runtime_diagnostics = {"runtime": self.name, "model_requests": request_log, "automatic_retries": 0,
            "reserved_unknown_usage_tokens":previous_diagnostics.get("reserved_unknown_usage_tokens", 0),
            "unknown_usage":previous_diagnostics.get("unknown_usage", False)}
        if budget:session.runtime_diagnostics["budget_policy"] = budget.public()
        stable = session.stable_prefix_enabled
        from cad_agent.runtimes.stable_prefix import StableTools
        stable_tools = StableTools(session) if stable else None
        instructions = session.system_prompt
        if stable:
            instructions += ("\n系统指令固定；注册工具目录只在确有新能力需求时追加，不随任务阶段删换。每轮末尾的工作状态列出当前允许的工具；"
                "即使工具已在清单中，也须满足当前阶段和来源前置条件。完整原文、几何或工具结果可按已返回"
                "record_id调用read_evidence_record分页回读。存储引用不等于已读或完成；相同失败两次后须修正参数或补证据，不能重复提交。")
        issue, recovery = None, None
        admission_stop = None
        evidence_recoveries = 0
        seen_calls = set()
        execution = getattr(session, "execution", None)
        session.progress(stage="直接订阅 · 等待账号并发名额")
        concurrency = session.runtime_diagnostics.setdefault("concurrency", {})
        with session.w.siwc_account.queued_lease(
                session.w.settings.siwc_queue_timeout_seconds,
                execution.check_cancelled if execution else None, concurrency) as lease:
            # Preserve preparation/tool time; exclude only time waiting for a slot.
            session.started += lease["queue_seconds"]
            session.progress(subscription_queue_seconds=lease["queue_seconds"],
                             subscription_concurrency=lease.get("max_concurrency", 1))
            # The model's first plan can add capabilities after this loop starts.
            # Iterate the bounded maximum, checking the CURRENT plan each turn;
            # capturing its pre-plan limit could end without final synthesis.
            for round_no in range(max(20,session.round_limit) + (0 if budget else 1)):
                if round_no >= session.round_limit and (budget or recovery != "repair_answer"):
                    break
                reason = admission_stop or controls.stop_reason(session)
                final_only = bool(reason or recovery == "repair_answer" or round_no >= session.round_limit - (2 if budget else 1))
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
                if session.focused_enabled and not final_only and not stable:
                    messages=session.focus.messages(messages)
                tools_added = []
                if stable and not final_only:
                    allowed_now = sorted(t["function"]["name"] for t in session.offered_tools(planning_only=True))
                    tools_added = stable_tools.include_required(allowed_now)
                    state = dict(allowed_tools=allowed_now,
                        requirement_ids=[p["id"] for p in session.task.parts],
                        remaining_tool_calls=session.w.settings.agent_max_tool_calls-session.calls,
                        latest_failure=next((t.get("error") for t in reversed(session.trace) if t.get("status") == "failed"), None))
                    messages.append({"role": "developer", "content": "当前执行状态（数据）：" + dumps(state)})
                definitions = stable_tools.definitions if stable else session.offered_tools(planning_only=True)
                tools = [] if final_only and not stable else [{"type": "namespace", "name": "cad",
                    "description": "当前图纸的只读 CAD 检索、上下文与证据工具。",
                    "tools": [{"type": "function", "name": t["function"]["name"],
                        "description": t["function"]["description"], "parameters": t["function"]["parameters"],
                        "strict": False} for t in definitions]}]
                choice = "none" if final_only else "required" if not completed_tool or recovery == "retrieve_evidence" else "auto"
                payload = {"model": session.w.settings.model, "instructions": instructions,
                    "input": messages, "tools": tools, "tool_choice": choice,
                    "parallel_tool_calls": False, "store": False, "stream": True,
                    "reasoning": {"effort": session.w.settings.reasoning_effort}, "include": ["reasoning.encrypted_content"]}
                if budget:
                    payload["max_output_tokens"] = budget.max_output_tokens
                    estimated, _ = estimate_input(payload)
                    if not final_only and estimated > budget.input_soft_tokens_per_request:
                        # Checkpoint only at the threshold, not every turn.
                        # Full preceding messages remain locally addressable.
                        from cad_agent.analysis.evidence_store import save_session
                        ref = save_session(session, "context_checkpoint", messages)
                        context = session.focus.context()
                        context["prior_context_record"] = ref
                        context["evidence_records"] = list(session.task.data.get("evidence_records", {}).values())[-20:]
                        messages = [messages[0], {"role":"user", "content":dumps(context)},
                                    {"role":"developer", "content":"当前执行状态（数据）：" + dumps(state)}]
                        payload["input"] = messages
                        session.runtime_diagnostics.setdefault("context_checkpoints", []).append(ref)
                if len(dumps(payload)) > text_limit(session):
                    raise fail("CONTEXT_BUDGET", "单轮上下文超过本地上限；未继续调用模型。")
                entry = {"round": round_no+1, "final_only": final_only, "request_chars": len(dumps(payload)),
                         "model": payload["model"], "reasoning_effort": payload["reasoning"]["effort"],
                         "status": "started", "stop_reason": session.policy.stop_reason,
                         "tool_choice": choice, "evidence_count": len(session.evidence),
                         "observation_count": len(session.observations),
                         "answer_repair": recovery == "repair_answer",
                         "tool_count":sum(len(t.get("tools",[])) for t in tools),
                         "tool_schema_chars":len(dumps(tools)),
                         "delivery_gate":session.focus.review_ready(),
                         "evidence_coverage": session.coverage.summary()}
                entry.update(prefix_diagnostics(payload))
                entry["tool_catalog_change"] = "initial" if not request_log else "required_capability_expansion" if tools_added else "none"
                entry["tools_added"] = tools_added
                payload = attach_images(session, payload, entry)
                try:
                    preflight(session, payload, entry)
                except ModelError as error:
                    if budget and not final_only and completed_tool and error.code == "SIWC_TOKEN_BUDGET":
                        admission_stop = "finalization_request_reserve"
                        session.runtime_diagnostics.setdefault("admission_rejections", []).append(
                            {"round": round_no + 1, "error_code": error.code,
                             "budget_preflight": entry.get("budget_preflight"), "sent": False})
                        continue
                    raise
                request_log.append(entry)
                session.policy.model_requests.append(dict(entry))
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
                        if (call.get("namespace") != "cad" or call.get("name") not in {t["function"]["name"] for t in definitions}
                                or not call.get("call_id") or call["call_id"] in seen_calls):
                            raise fail("TOOL_DENIED", "模型调用了未授权或重复编号的工具；已停止。")
                        seen_calls.add(call["call_id"])
                    messages.extend(output)
                    for call in calls:
                        if call["name"] not in allowed:
                            value = dumps({"error":{"code":"TOOL_STAGE_NOT_READY",
                                "message":"工具尚未满足当前阶段前置条件；请使用allowed_tools。"},
                                "allowed_tools":sorted(allowed)})
                            messages.append({"type":"function_call_output","call_id":call["call_id"],"output":value})
                            session.runtime_diagnostics["stage_rejections"] = session.runtime_diagnostics.get("stage_rejections",0)+1
                            if session.runtime_diagnostics["stage_rejections"] >= 2:
                                session.policy.stop_reason = "repeated_stage_failure"
                            continue
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
                    # Replay only a recognized answer protocol with read sources
                    # and a field failure. Free replies and transport-like JSON
                    # never enter persisted diagnostics, even after a source read.
                    rejected = {'round': round_no + 1, 'issue': validation_feedback(error)[:4000]}
                    from cad_agent.analysis.delivery import AnswerValidationError
                    from cad_agent.analysis.tool_registry import Claim
                    try:
                        protocol = json.loads(answer)
                    except (ValueError, TypeError):
                        protocol = None
                    has_read = bool(session.evidence) and any(
                        t.get('status') == 'completed' and t.get('tool') not in
                        ('plan_task', 'request_toolset', 'finish_analysis') for t in session.trace)
                    field_failure = isinstance(error, AnswerValidationError) and any(
                        e.get('code') != 'grounding_validation' for e in error.field_issues)
                    protocol_shape = (isinstance(protocol, dict)
                        and set(protocol) <= set(FinalAnswer.model_fields)
                        and isinstance(protocol.get('claims'), list) and bool(protocol['claims'])
                        and all(isinstance(c, dict) and set(c) <= set(Claim.model_fields)
                                and isinstance(c.get('text'), str) and 'state' in c
                                for c in protocol['claims']))
                    if has_read and field_failure and protocol_shape:
                        rejected.update(answer=answer[:60000], answer_truncated=len(answer) > 60000)
                    session.runtime_diagnostics.setdefault('rejected_answers', []).append(rejected)
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
                        "cache_usage":cache_summary(session.usage),
                        "budget_policy":for_session(session).public() if for_session(session) else {"version":"legacy"},
                        "model_access_verified": True, "reasoning_effort": session.w.settings.reasoning_effort, "model_requests": request_log,
                        "subscription_budget": {"total_soft_tokens": controls.total_budget(session),
                            "reported_tokens": sum(u.get("total_tokens", 0) for u in session.usage),
                            "reserved_unknown_usage_tokens": session.runtime_diagnostics.get('reserved_unknown_usage_tokens',0),
                            "unknown_usage": session.runtime_diagnostics.get('unknown_usage',False),
                            "charged_tokens": charged_tokens(session)},
                        "usage_note": "官方 Responses 报告的订阅 Token；不等于订阅额度百分比或 API 美元费用。"}
