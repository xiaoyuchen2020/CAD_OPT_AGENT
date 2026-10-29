"""Bounded, read-only materialization of one Fleet run's generated artifacts."""
import hmac
import os
import re

from fastapi import APIRouter, HTTPException, Request

from cad_agent.extensions.table_recovery.bundle import digest, encoded


MAX_EXPORT_BYTES = 8 * 1024 * 1024
MAX_REFERENCES = 512
KINDS = frozenset({'input', 'code', 'execution', 'validation', 'program_input', 'program',
                  'program_execution', 'program_validation', 'program_diff',
                  'program_regression', 'program_kernel_input'})


class ExportLimit(ValueError):
    pass


def materialize(workspace, run):
    task_id = run.get('task_id')
    if not isinstance(task_id, str) or not task_id:
        raise ValueError('GENERATED_ARTIFACT_TASK_MISSING')
    task = workspace.store.get('agent_task', task_id)
    if task.get('task_id') != task_id or task.get('snapshot_id') != run.get('snapshot_id'):
        raise ValueError('GENERATED_ARTIFACT_TASK_BINDING_MISMATCH')
    allowed_sources = {run['snapshot_id']} | {
        item['snapshot_id'] for item in run.get('request', {}).get('related_drawings', [])}
    items, diagnostics, excluded, references = [], [], [], {}

    def issue(code, path, artifact_id=None):
        diagnostics.append(dict(code=code, path=path, **({'artifact_id': artifact_id} if artifact_id else {})))

    def exclude(value, reason, path):
        if isinstance(value, str) and value:
            excluded.append(dict(artifact_id=value, reason=reason, source_pointer=path))

    def attempts(record, path):
        values = record.get('attempts', [])
        if not isinstance(values, list):
            issue('INVALID_ATTEMPTS', path + '.attempts')
            return []
        return values

    def reference(record, field, kind, path, required=False):
        value = record.get(field)
        pointer = path + '.' + field
        if value is None:
            if required:
                issue('MISSING_REQUIRED_REFERENCE', pointer)
            return
        if not isinstance(value, str) or not re.fullmatch('ad_' + kind + '_[0-9a-f]{64}', value):
            issue('INVALID_ARTIFACT_REFERENCE', pointer)
            return
        if kind not in KINDS:
            raise ValueError('GENERATED_ARTIFACT_KIND_FORBIDDEN')
        references.setdefault(value, (kind, pointer))
        if len(references) > MAX_REFERENCES:
            raise ExportLimit('GENERATED_ARTIFACT_REFERENCE_LIMIT')

    methods = task.get('task_methods', {})
    programs = task.get('task_programs', {})
    results = task.get('program_results', {})
    routes = task.get('tool_version_routes', {})
    if any(not isinstance(value, dict) for value in (methods, programs, results, routes)):
        raise ValueError('GENERATED_ARTIFACT_LEDGER_INVALID')
    owned_programs = set()
    for ident, entry in methods.items():
        path = 'task_methods.' + ident
        if not isinstance(entry, dict) or entry.get('task_id') != task_id or entry.get('snapshot_id') != run['snapshot_id']:
            issue('FOREIGN_TASK_METHOD', path)
            continue
        reference(entry, 'input_artifact_id', 'input', path, required=True)
        entries = attempts(entry, path)
        for index, attempt in enumerate(entries):
            pointer = path + '.attempts.' + str(index)
            if not isinstance(attempt, dict):
                issue('INVALID_ATTEMPT', pointer)
                continue
            accepted = entry.get('status') == 'validated' and entry.get('accepted_attempt') == index + 1
            for field, kind in (('code_artifact_id', 'code'), ('execution_artifact_id', 'execution'), ('validation_artifact_id', 'validation')):
                reference(attempt, field, kind, pointer, required=accepted)
            exclude(attempt.get('metadata_artifact_id'), 'MODEL_METADATA', pointer + '.metadata_artifact_id')
        accepted_index = entry.get('accepted_attempt')
        if entry.get('status') == 'validated' and (type(accepted_index) is not int or not 1 <= accepted_index <= len(entries)):
            issue('ACCEPTED_ATTEMPT_MISSING', path)

    for ident, entry in programs.items():
        path = 'task_programs.' + ident
        if not isinstance(entry, dict) or entry.get('task_id') != task_id:
            issue('FOREIGN_TASK_PROGRAM', path)
            continue
        owned_programs.add(ident)
        if entry.get('imported_from'):
            exclude(entry.get('input_artifact_id'), 'IMPORTED_GENERATION_INPUT', path + '.input_artifact_id')
            if not _valid_registration(workspace, entry):
                issue('IMPORTED_PROGRAM_REGISTRATION_INVALID', path)
                owned_programs.discard(ident)
                continue
        else:
            reference(entry, 'input_artifact_id', 'program_input', path, required=True)
        reference(entry, 'package_artifact_id', 'program', path, required=entry.get('status') == 'validated')
        entries = attempts(entry, path)
        for index, attempt in enumerate(entries):
            pointer = path + '.attempts.' + str(index)
            if not isinstance(attempt, dict):
                issue('INVALID_ATTEMPT', pointer)
                continue
            accepted = entry.get('status') == 'validated' and entry.get('accepted_attempt') == index + 1
            for field, kind in (('package_artifact_id', 'program'), ('execution_artifact_id', 'program_execution'),
                                ('validation_artifact_id', 'program_validation'), ('diff_artifact_id', 'program_diff'),
                                ('regression_artifact_id', 'program_regression'), ('baseline_artifact_id', 'program_execution')):
                required = accepted and (field in {'package_artifact_id', 'execution_artifact_id', 'validation_artifact_id'}
                                         or entry.get('kind') == 'revision')
                reference(attempt, field, kind, pointer, required=required)
            exclude(attempt.get('model_artifact_id'), 'MODEL_METADATA', pointer + '.model_artifact_id')
        accepted_index = entry.get('accepted_attempt')
        if (entry.get('status') == 'validated' and not entry.get('imported_from')
                and (type(accepted_index) is not int or not 1 <= accepted_index <= len(entries))):
            issue('ACCEPTED_ATTEMPT_MISSING', path)

    for ident, result in results.items():
        path = 'program_results.' + ident
        if not isinstance(result, dict) or result.get('program_id') not in owned_programs:
            issue('FOREIGN_PROGRAM_RESULT', path)
            continue
        reference(result, 'input_artifact_id', 'program_input', path, required=True)
        for field, kind in (('execution_artifact_id', 'program_execution'), ('validation_artifact_id', 'program_validation')):
            reference(result, field, kind, path, required=result.get('status') == 'validated')

    for ident, route in routes.items():
        path = 'tool_version_routes.' + ident
        if (not isinstance(route, dict) or route.get('version_id') not in owned_programs
                or programs[route['version_id']].get('kind') != 'revision'
                or route.get('snapshot_id') not in allowed_sources):
            issue('FOREIGN_TOOL_ROUTE', path)
            continue
        reference(route, 'refs_artifact_id', 'program_input', path, required=True)
        executions = route.get('executions', {})
        if not isinstance(executions, dict):
            issue('INVALID_ROUTE_EXECUTIONS', path)
            continue
        for input_id, execution in executions.items():
            pointer = path + '.executions.' + input_id
            if not isinstance(execution, dict):
                issue('INVALID_ROUTE_EXECUTION', pointer)
                continue
            for field, kind in (('input_artifact_id', 'program_kernel_input'), ('execution_artifact_id', 'program_execution'),
                                ('validation_artifact_id', 'program_validation')):
                reference(execution, field, kind, pointer, required=True)

    total_bytes = 0
    for ident, (kind, path) in sorted(references.items()):
        try:
            value = workspace.store.get('adaptation_' + kind, ident)
        except KeyError:
            issue('ARTIFACT_MISSING', path, ident)
            continue
        except (TypeError, ValueError):
            issue('ARTIFACT_CORRUPT', path, ident)
            continue
        try:
            sha = digest(value)
        except (TypeError, ValueError):
            issue('ARTIFACT_CORRUPT', path, ident)
            continue
        if ident != 'ad_' + kind + '_' + sha:
            issue('ARTIFACT_CORRUPT', path, ident)
            continue
        if not _sources_allowed(kind, value, allowed_sources):
            issue('ARTIFACT_SOURCE_NOT_AUTHORIZED', path, ident)
            continue
        item = dict(artifact_id=ident, kind=kind, sha256=sha, value=value)
        total_bytes += len(encoded(item))
        if total_bytes > MAX_EXPORT_BYTES:
            raise ExportLimit('GENERATED_ARTIFACT_BYTE_LIMIT')
        items.append(item)
    result = dict(schema_version='generated-artifacts@1', run_id=run['id'], task_id=task_id,
                  task_revision=task.get('revision'), complete=not diagnostics, items=items,
                  diagnostics=diagnostics, excluded=excluded, referenced_count=len(references))
    if len(encoded(result)) > MAX_EXPORT_BYTES:
        raise ExportLimit('GENERATED_ARTIFACT_BYTE_LIMIT')
    return result


def _sources_allowed(kind, value, allowed):
    def authorized(snapshot):
        return isinstance(snapshot, str) and snapshot in allowed
    if kind == 'input':
        return isinstance(value, dict) and authorized(value.get('snapshot_id'))
    if kind != 'program_input':
        return True
    if not isinstance(value, dict) or not isinstance(value.get('refs'), list) or not value['refs']:
        return False
    for ref in value['refs']:
        if not isinstance(ref, dict) or not authorized(ref.get('snapshot_id')):
            return False
        bundle = ref.get('bundle', {})
        if not isinstance(bundle, dict) or bundle.get('snapshot_id') != ref['snapshot_id']:
            return False
    program_input = value.get('bundle')
    if not isinstance(program_input, dict):
        return False
    sources = program_input.get('sources', [])
    if not isinstance(sources, list):
        return False
    for bundle in sources:
        if not isinstance(bundle, dict) or not authorized(bundle.get('snapshot_id')):
            return False
    return True


def _valid_registration(workspace, entry):
    ident = entry['imported_from']
    if not isinstance(ident, str) or not re.fullmatch(r'ad_program_registration_[0-9a-f]{64}', ident):
        return False
    try:
        value = workspace.store.get('adaptation_program_registration', ident)
        return (ident == 'ad_program_registration_' + digest(value)
                and value['entry']['package_artifact_id'] == entry.get('package_artifact_id')
                and value['entry']['contract'] == entry.get('contract'))
    except (KeyError, TypeError, ValueError):
        return False


def router(workspace):
    api = APIRouter()

    @api.get('/api/fleet-runs/{run_id}/generated-artifacts')
    def generated_artifacts(run_id: str, request: Request):
        secret = os.environ.get('CAD_FLEET_TOKEN', '')
        if not secret or not hmac.compare_digest(request.headers.get('authorization', '').encode(), ('Bearer ' + secret).encode()):
            raise HTTPException(403, '仅接受已授权的本机 Worker。')
        if request.query_params or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', run_id):
            raise HTTPException(400, '此接口只接受运行编号，不接受路径或工件选择参数。')
        run = workspace.store.get('chat_run', run_id)
        if run.get('id') != run_id:
            raise HTTPException(409, detail=dict(code='GENERATED_ARTIFACT_RUN_BINDING_MISMATCH'))
        key = request.headers.get('x-fleet-execution', '')
        if not key or not run.get('fleet_execution_key') or not hmac.compare_digest(key.encode(), run['fleet_execution_key'].encode()):
            raise HTTPException(403, '执行编号不属于此运行。')
        try:
            return materialize(workspace, run)
        except ExportLimit as error:
            raise HTTPException(413, detail=dict(code=str(error), max_bytes=MAX_EXPORT_BYTES)) from None
        except ValueError as error:
            raise HTTPException(409, detail=dict(code=str(error))) from None

    return api
