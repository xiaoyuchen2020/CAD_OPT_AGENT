"""Official token usage and conservative unknown-usage reservations stay separate."""


def charged_tokens(session):
    methods = getattr(getattr(session, 'task', None), 'data', {}).get('task_methods', {})
    reserved = sum(m.get('reserved_tokens', 0) for m in methods.values())
    return reserved + sum(u.get('total_tokens', 0) for u in session.usage if u.get('source') != 'task_method') + getattr(
        session, 'runtime_diagnostics', {}).get('reserved_unknown_usage_tokens', 0)
