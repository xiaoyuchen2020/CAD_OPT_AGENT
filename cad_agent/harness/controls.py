"""Shared CAD budget and context controls, independent of CLI or transport."""

class BudgetController:
    # Usage arrives after generation, so these are soft thresholds, not a
    # per-request token cap. Reserve room for one bounded, tool-free answer.
    EVIDENCE_STOP_TOKENS = 30000
    EXPLORATION_STOP_TOKENS = 40000
    TOTAL_SOFT_TOKENS = 50000
    FINAL_CONTEXT_CHARS = 24000

    def __init__(self):
        self.source_contexts = []

    def exploration_budget(self, session):
        from cad_agent.runtimes.budget_policy import for_session
        policy = for_session(session)
        if policy:return policy.exploration_soft_tokens
        operations={op for s in session.task.data['subtasks'] for op in s.get('operations',[])}
        return min(120000,self.EXPLORATION_STOP_TOKENS+10000*len(operations)+8000*max(0,len(session.task.data["subtasks"])-1))

    def total_budget(self, session):
        from cad_agent.runtimes.budget_policy import for_session
        policy = for_session(session)
        if policy:return policy.total_ceiling_tokens
        # Stateless final synthesis replays a bounded evidence packet. Reserve
        # BOTH final delivery and its one allowed correction before exploration
        # starts; a large last tool request must not silently erase correction.
        # Usage is reported after generation, so this remains a soft threshold.
        return self.exploration_budget(session)+2*(self.FINAL_CONTEXT_CHARS+len(session.system_prompt))

    def stop_reason(self, session):
        from cad_agent.runtimes.token_budget import charged_tokens
        tokens = charged_tokens(session)
        if session.policy.stop_reason:
            return session.policy.stop_reason
        from cad_agent.runtimes.budget_policy import for_session, stop_reason
        if for_session(session):
            return stop_reason(session)
        pending=session.task.pending_work()
        exploration_limit=self.exploration_budget(session)
        pending_images=set(getattr(session,'visual_images',{}))-set(getattr(session,'reviewed_image_ids',set()))
        # Fetching a source image creates an atomic review obligation. Reserve
        # one bounded decision turn instead of discarding an unseen image at
        # the exploration threshold. The total/time/tool limits still apply.
        prepared={'prepare_space_connections','prepare_discovery_review','prepare_room_boundaries','render_region'}
        last_index=next((i for i in range(len(session.trace)-1,-1,-1) if session.trace[i].get('status')=='completed'),-1)
        last=session.trace[last_index] if last_index>=0 else {}
        # One decision and one input repair only. A failed review must not
        # repeatedly inherit the same preparation's budget exemption.
        attempts=len(session.trace)-last_index-1
        discovery_ready=last.get('tool')=='discover_rooms' and any(o['tool']=='discover_rooms' and o['data'].get('total',0)>0 for o in getattr(session,'observations',{}).values())
        if (pending_images and last.get('tool') in prepared or discovery_ready) and attempts<2 and tokens < self.total_budget(session)-self.FINAL_CONTEXT_CHARS and session.calls<session.w.settings.agent_max_tool_calls and session._remaining()>45:
            return None
        if tokens >= exploration_limit:
            return "subscription_exploration_budget"
        has_evidence = session.evidence or any(o['tool'] in ('measure_geometry', 'measure_room', 'get_measurement_evidence') for o in getattr(session, 'observations', {}).values())
        # An empty operation queue often means an unplanned lookup, not a
        # complete answer. Preserve independent/object-field work until actual
        # delivery or a concrete block, without increasing any budget.
        if (has_evidence and tokens >= self.EVIDENCE_STOP_TOKENS and not pending
                and session.task.coverage_ready() and not session.coverage.pending()
                and (not session.focused_enabled or session.focus.review_ready())):
            return "subscription_answer_reserve"
        if session.calls >= session.w.settings.agent_max_tool_calls:
            return "tool_call_budget"
        if has_evidence and session._remaining() <= (90 if session.task.complex else 45):
            return "subscription_answer_time_reserve"
        return None

    def remember_context(self, tool, value):
        if tool == 'batch_read_sources':
            for item in value.get('items', []):
                self.remember_context(item['tool'], item['result'])
            return
        if tool != "read_source_context" or not value.get("target_region_id"):
            return
        self.source_contexts.append({
            "target_region_id": value["target_region_id"],
            "items": [{k: item[k] for k in (
                "region_id", "relationship", "relation_kind", "relation_basis",
                "relation_state", "position_relative_to_target",
                "parser_context_region_ids", "truncated", "next_start",
            ) if k in item} for item in value.get("items", [])],
        })

