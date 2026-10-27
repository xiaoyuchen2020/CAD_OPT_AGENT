"""Official token usage and conservative unknown-usage reservations stay separate."""


def charged_tokens(session):
    methods = getattr(getattr(session, 'task', None), 'data', {}).get('task_methods', {})
    programs = getattr(getattr(session, 'task', None), 'data', {}).get('task_programs', {})
    reserved = sum(m.get('reserved_tokens', 0) for m in [*methods.values(),*programs.values()])
    return reserved + sum(u.get('total_tokens', 0) for u in session.usage if u.get('source') not in ('task_method','task_program')) + getattr(
        session, 'runtime_diagnostics', {}).get('reserved_unknown_usage_tokens', 0)
