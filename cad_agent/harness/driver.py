"""Shared CAD step admission, tool dispatch and protocol settlement.

Transport loops retain streaming/wire formats. This driver owns the execution
boundary for both custom runtimes and never executes protocol-private tools.
"""
import json
from cad_agent.adapters.model import ModelError
from cad_agent.harness.state import StepContext, LoopPolicy


def error_output(code, message, **fields):
    return json.dumps({'error': {'code': code, 'message': message}, **fields}, ensure_ascii=False)


class HarnessDriver:
    def __init__(self, session, adapter, *, controls=None):
        self.session, self.adapter, self.controls = session, adapter, controls
        self.seen_calls = set()
        self.registered_calls = set()
        self.step = None
        self.phase = 'created'
        self.input_changed = False
        if not hasattr(session, 'runtime_diagnostics'):
            session.runtime_diagnostics = {}

    def emit(self, kind, **payload):
        callback = getattr(self.session, 'harness_event', None)
        if callback:
            callback(kind, payload)
        elif execution := getattr(self.session, 'execution', None):
            if hasattr(execution, 'emit'):
                execution.emit(kind, payload)

    def inputs(self, messages):
        from cad_agent.harness.inbox import apply_pending
        before = getattr(self.session, 'input_revision', 0)
        updated = apply_pending(self.session, messages)
        self.input_changed = getattr(self.session, 'input_revision', 0) != before
        if self.input_changed:
            # These are old-question completion/repair phases, not resource
            # limits. Global token/time/round/call limits are never reset.
            if self.session.policy.stop_reason in {'answer_repair', 'evidence_sufficient', 'partial_delivery'}:
                self.session.policy.stop_reason = None
            self.emit('request.series_changed', input_revision=self.session.input_revision)
        return updated

    def pending(self):
        from cad_agent.harness.inbox import has_pending
        return has_pending(self.session)

    def ready_for_delivery(self):
        from cad_agent.harness.inbox import prepare_delivery
        return prepare_delivery(self.session)

    def failure(self, code, message):
        # Public error codes are part of the transport compatibility contract.
        return ModelError(self.adapter.error_prefix + code, message)

    @staticmethod
    def recovery_kind(session, answer):
        """Classify missing provenance, not whether a source semantically answers the question."""
        from cad_agent.analysis.tool_registry import FinalAnswer
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

    def decision(self, ordinal, *, reason=None, repairing=False, budgeted=False):
        subscription = self.adapter.protocol == 'responses'
        if not subscription:
            reason = reason or self.session.policy.stop_reason
            if self.session.calls >= self.session.w.settings.agent_max_tool_calls:
                reason = reason or 'tool_call_budget'
        policy = LoopPolicy(reserve_rounds=2 if subscription and budgeted else 1,
                            repair_requires_final=subscription or self.session.policy.enabled,
                            require_successful_tool_for_final=subscription)
        decision = policy.decide(self.session, ordinal, reason=reason, repairing=repairing)
        if decision.final_only and policy.require_successful_tool_for_final and not any(
                t.get('status') == 'completed' for t in self.session.trace):
            self.session.runtime_diagnostics['stop_reason'] = reason or 'round_budget_without_tool_result'
            raise self.failure('EVIDENCE_NOT_OBTAINED', '探索预算已收束，但尚未成功读取图纸信息；停止调用，不生成无依据答案。')
        return decision

    def begin_step(self, ordinal, tools, final_only, *, admission_names=None):
        self.session._remaining()
        self.step = StepContext.capture(self.session, ordinal + 1, self.adapter.protocol, tools, final_only,
                                        admission_names=admission_names)
        self.session.harness_step = self.step
        self.phase = 'prepared'
        self.emit('step.prepared', context=self.step.public())
        return self.step

    def model_call(self, payload, call, **kwargs):
        self.phase = 'requesting'
        # The execution event store applies the same public-data redaction as
        # RunJournal. Model-private reasoning and image bytes never reach UI.
        self.emit('model.requested', step_id=self.step.step_id if self.step else None,
                  request=payload)
        try:
            result = self.session.journal.model_call(payload, call, **kwargs)
        except BaseException as error:
            self.phase = 'failed'
            self.emit('model.failed', code=getattr(error, 'code', type(error).__name__))
            raise
        self.phase = 'responded'
        journal_event = self.session.journal.last_model_event
        self.emit('model.completed', step_id=self.step.step_id if self.step else None,
                  response=result, journal_event_id=journal_event['id'] if journal_event else None,
                  sections=journal_event.get('sections', {}) if journal_event else {})
        return result

    def discard_for_steering(self, calls, messages):
        if not self.pending():
            return False
        for call in calls:
            value = error_output('STEERING_SUPERSEDED', '用户已补充要求；此调用未执行，下一步按新要求重新规划。')
            self.record_skipped(call, value)
            messages.append(self.adapter.result(call, value))
        self.emit('step.superseded', step_id=self.step.step_id if self.step else None)
        return True

    def register_call(self, call):
        execution = getattr(self.session, 'execution', None)
        key = f'{self.step.step_id if self.step else "step"}:{call.call_id}'
        if execution and hasattr(execution, 'begin_call'):
            if key not in self.registered_calls:
                execution.begin_call(call.name, call.arguments, call_id=key)
                self.registered_calls.add(key)
        return key

    def record_skipped(self, call, value, status='not_started'):
        execution = getattr(self.session, 'execution', None)
        key = self.register_call(call)
        if execution and hasattr(execution, 'settle_call'):
            execution.settle_call(key, status=status, result=json.loads(value))

    def execute_calls(self, calls, messages, *, allowed, advertised, final_only=False, eligible=None):
        """Validate the whole batch before dispatch; settle even skipped siblings."""
        s = self.session
        # Native historically accepts registered capabilities outside its
        # focused prompt subset. The frozen eligible catalog still filters
        # runtime support and authorization; domain handlers recheck both.
        catalog = eligible if self.adapter.protocol == 'chat' and eligible is not None else advertised
        if final_only:
            raise self.failure('TOOL_DENIED', '最终回答阶段禁止调用工具。')
        ids = [c.call_id for c in calls]
        if (any(not i for i in ids) or len(ids) != len(set(ids)) or
                (self.adapter.protocol == 'responses' and any(c.name not in advertised or c.namespace != 'cad' for c in calls))
                or (self.adapter.strict_global_call_ids and self.seen_calls.intersection(ids))):
            raise self.failure('TOOL_DENIED', '模型调用了未授权或重复编号的工具；已停止。')
        self.seen_calls.update(ids)
        # Register every declared call before the first handler runs. A crash
        # during that handler leaves the later siblings visibly not started.
        for call in calls:
            self.register_call(call)
        self.phase = 'tools'
        for position, call in enumerate(calls):
            invoked = False
            try:
                s._remaining()
                if self.pending():
                    value = error_output('STEERING_SUPERSEDED', '用户已补充要求；此调用未执行。')
                elif s.prepared_answer is not None:
                    value = error_output('DELIVERY_ALREADY_PREPARED', '答案已通过交付校验；此调用未执行。')
                elif call.name not in catalog:
                    # Native's historical recoverable schema-error behavior is
                    # retained; nothing reaches a tool implementation.
                    s.calls += 1
                    failure = {'code': 'INVALID_TOOL_INPUT', 'message': '工具未注册或未在本步骤开放。'}
                    s.trace.append({'tool': call.name, 'status': 'failed', 'error': failure})
                    value = json.dumps({'error': failure}, ensure_ascii=False)
                elif call.name not in allowed:
                    value = error_output('TOOL_STAGE_NOT_READY', '工具尚未满足当前阶段前置条件；请使用allowed_tools。', allowed_tools=sorted(allowed))
                    diagnostics = s.runtime_diagnostics
                    diagnostics['stage_rejections'] = diagnostics.get('stage_rejections', 0) + 1
                    if diagnostics['stage_rejections'] >= 2:
                        s.policy.stop_reason = 'repeated_stage_failure'
                elif s.calls >= s.w.settings.agent_max_tool_calls:
                    value = error_output('AGENT_TOOL_LIMIT', '探索额度已用完，请根据已读取证据回答。')
                else:
                    # Persisted key is step-scoped even when a provider reuses c1.
                    s.harness_call_id = self.register_call(call)
                    s.harness_call_registered = s.harness_call_id in self.registered_calls
                    invoked = True
                    value = s.invoke_tool(call.name, call.arguments)
                    if self.controls:
                        self.controls.remember_context(call.name, json.loads(value))
                if not invoked:
                    self.record_skipped(call, value, 'rejected' if call.name not in allowed else 'not_started')
                messages.append(self.adapter.result(call, value))
            except BaseException:
                # Do not retry an uncertain operation. A restored conversation
                # still has one result per model call and no dangling siblings.
                value = error_output('TOOL_OUTCOME_UNKNOWN' if invoked else 'TOOL_NOT_STARTED',
                                     '调用未获得完成回执；恢复时先核对执行记录，不自动重试。' if invoked else '调用尚未开始。')
                if not invoked:
                    try:
                        self.record_skipped(call, value)
                    except Exception:
                        pass
                messages.append(self.adapter.result(call, value))
                for remainder in calls[position + 1:]:
                    value = error_output('TOOL_NOT_STARTED', '前序调用中断，本调用未开始。')
                    try:
                        self.record_skipped(remainder, value)
                    except Exception:
                        pass  # Lost execution lease: durable recovery owns settlement.
                    messages.append(self.adapter.result(remainder, value))
                self.emit('step.interrupted', step_id=self.step.step_id if self.step else None)
                raise
            finally:
                s.harness_call_id = None
                s.harness_call_registered = False
        self.phase = 'tools_settled'
        self.emit('step.tools_settled', count=len(calls))
        # Steering can arrive while the last handler validates a delivery.
        # Let the next step apply it instead of returning the old answer and
        # making the publication fence turn accepted steering into a failure.
        if self.pending():
            self.emit('step.superseded', step_id=self.step.step_id if self.step else None)
            return None
        if s.prepared_answer is not None and not self.ready_for_delivery():
            return None
        return s.prepared_answer
