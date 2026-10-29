"""Small browser delivery; persisted runs and downloadable archives stay full."""


def answer_view(job):
    view = dict(job)
    if isinstance(job.get('result'), dict):
        view['result'] = {k: v for k, v in job['result'].items() if k not in {
            'trace', 'task', 'usage', 'model_context_codec', 'efficiency',
        }}
    return view
