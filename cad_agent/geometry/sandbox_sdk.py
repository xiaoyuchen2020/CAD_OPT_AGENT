"""Stable, dependency-light SDK for the existing networkless Docker runtime.

The host owns input hashes and promotes results only through source tools. This
SDK writes auditable candidates, never production observations or confirmations.
"""
from pathlib import Path
import hashlib
import json
import re
from cad_agent.geometry.local_measurement import measure_pair
from cad_agent.geometry.investigation_topology import network, primitives

SDK_VERSION='geometry-sandbox-sdk-v1'


def load_bundle(bundle_id, expected_source_sha256, input_root='/input'):
    if not re.fullmatch(r'gb_[a-f0-9]+',bundle_id):raise ValueError('INVALID_BUNDLE_ID')
    root=Path(input_root).resolve();path=(root/'bundles'/f'{bundle_id}.json').resolve()
    if not path.is_relative_to(root):raise ValueError('BUNDLE_PATH_OUTSIDE_INPUT')
    data=json.loads(path.read_text())
    if data.get('bundle_id')!=bundle_id or data.get('source_sha256')!=expected_source_sha256:
        raise ValueError('BUNDLE_SOURCE_MISMATCH')
    if hashlib.sha256((root/'drawing.dxf').read_bytes()).hexdigest()!=expected_source_sha256:
        raise ValueError('INPUT_SOURCE_CHANGED')
    return data


def write_candidate(filename, result, source_sha256, code_sha256, output_root='/output'):
    root=Path(output_root).resolve();path=(root/filename).resolve()
    if not path.is_relative_to(root) or path==root:raise ValueError('OUTPUT_PATH_OUTSIDE_SANDBOX')
    if not all(re.fullmatch(r'[a-f0-9]{64}',v) for v in (source_sha256,code_sha256)):
        raise ValueError('SHA256_REQUIRED')
    payload=dict(sdk_version=SDK_VERSION,source_sha256=source_sha256,code_sha256=code_sha256,
        status='sandbox_candidate_requires_host_replay',engineering_confirmed=False,result=result)
    path.parent.mkdir(parents=True,exist_ok=True)
    # Do not destroy a prior result on an attempted rerun.
    with path.open('x') as f:json.dump(payload,f,ensure_ascii=False,allow_nan=False,indent=2)
    return dict(artifact=filename,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),status=payload['status'])


def classify_failure(stderr,exit_code):
    if exit_code==0:return dict(category='success',retry='none')
    for marker,category,repair in [
        ('ModuleNotFoundError','dependency','Use the frozen cad_agent.geometry.sandbox_sdk; inspect SDK capabilities before retry.'),
        ('NameError','python_name','Use json.load/json.loads for JSON, not Python source literals.'),
        ('BUNDLE_SOURCE_MISMATCH','source_mismatch','Reload the bundle from the host; do not bypass identity checking.'),
        ('INPUT_SOURCE_CHANGED','source_mismatch','Stop using this input snapshot and re-import its actual version.'),
        ('PermissionError','filesystem','Write only /output; inputs and runtime are read-only.'),
        ('FileExistsError','existing_artifact','Read the existing result or choose a new artifact name; do not overwrite evidence.')]:
        if marker in stderr:return dict(category=category,retry='after_repair_only',repair=repair)
    return dict(category='execution_error',retry='after_diagnosis_only',repair='Inspect stderr and preserve the failed script; do not blindly rerun.')


def capabilities():
    return dict(sdk_version=SDK_VERSION,imports='from cad_agent.geometry.sandbox_sdk import load_bundle, measure_pair, primitives, network, write_candidate',
        inputs_read_only=['/input/drawing.dxf','/input/rule.json','/input/bundles'],outputs='/output',
        result_state='candidate',promotion='Replay source IDs and operations through host geometry tools',
        units='drawing units unless independently established',network=False)
