"""Public, credential-free model identity for frozen exploration runs."""
from hashlib import sha256

from cad_agent.config import Settings


class SnapshotMismatch(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def model_snapshot(profile, settings=None):
    if profile not in ('api_sol', 'api_astra'):
        raise SnapshotMismatch('EXPLORATION_MODEL_UNSUPPORTED')
    model = (settings or Settings.load()).model_catalog()[profile]
    return {
        'runtime': 'native', 'model_profile': profile, 'allow_fallback': False,
        'provider': model.provider, 'protocol': model.protocol, 'model': model.model,
        # Endpoint identity matters for reproducibility. Never expose its URL,
        # credentials, query parameters or other environment values.
        'endpoint_sha256': sha256(model.api_base.encode()).hexdigest(),
    }


def check_model_snapshot(snapshot, settings=None):
    if snapshot != model_snapshot(snapshot.get('model_profile'), settings):
        raise SnapshotMismatch('EXPLORATION_MODEL_VERSION_MISMATCH')
