"""Current-channel admission and accounting for task-local code generation."""


API_PROVIDERS = frozenset({'custom', 'deepseek', 'qwen'})


def runtime_supported(runtime, provider, *, protocol=None, model=None):
    if runtime == 'responses_subscription':
        return (provider == 'responses_subscription'
                and protocol in (None, 'responses')
                and model in (None, 'gpt-6.1-sol'))
    return (runtime == 'native' and provider in API_PROVIDERS
            and protocol in (None, 'openai') and (model is None or bool(model)))


def session_supported(session):
    settings = session.w.settings
    return runtime_supported(session.request.runtime, settings.provider,
                             protocol=settings.protocol, model=settings.model)


def generation_slot(session, error_code):
    from cad_agent.harness.live_controls import before_model
    before_model(session)
    from cad_agent.harness.controls import BudgetController
    from cad_agent.runtimes.budget_policy import for_session
    from cad_agent.runtimes.token_budget import charged_tokens
    policy = for_session(session)
    reserve = policy.reserved_finalization_tokens if policy else 24000
    slot = min(32000, BudgetController().total_budget(session) - charged_tokens(session) - reserve)
    if slot < 8000:
        raise ValueError(error_code)
    return slot


def generation_config(settings):
    subscription = settings.provider == 'responses_subscription'
    return dict(model=settings.model, profile=settings.model_profile,
                provider=settings.provider, protocol=settings.protocol,
                transport='responses_subscription' if subscription else 'chat_completions',
                billing='subscription' if subscription else 'api')


def reported_usage(value):
    keys = {'prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_cache_hit_tokens',
            'prompt_cache_miss_tokens', 'reasoning_tokens'}
    return {key: count for key, count in (value.items() if isinstance(value, dict) else ())
            if key in keys and type(count) is int and count >= 0}


def usage_record(settings, value, **identity):
    usage = reported_usage(value)
    return usage | generation_config(settings) | identity | {
        'usage_status': 'reported' if 'total_tokens' in usage else 'unknown'}


def additional_charge(meta, slot):
    """Keep reserved estimates separate from usage, including unknown retries."""
    actual = reported_usage(meta.get('usage')).get('total_tokens', 0)
    unknown = meta.get('transport_diagnostics', {}).get('reserved_unknown_usage_tokens', 0)
    if type(unknown) is not int or unknown < 0:
        unknown = 0
    return max(0, actual + unknown - slot)
