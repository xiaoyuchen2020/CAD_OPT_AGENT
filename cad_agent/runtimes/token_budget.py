"""Official token usage and conservative unknown-usage reservations stay separate."""


def charged_tokens(session):
    return sum(u.get('total_tokens', 0) for u in session.usage) + getattr(
        session, 'runtime_diagnostics', {}).get('reserved_unknown_usage_tokens', 0)
