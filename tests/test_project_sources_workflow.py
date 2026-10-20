import json
import pytest
from cad_agent.contracts import ChatRequest
from cad_agent.analysis.session import CadAgentSession
from cad_agent.analysis.project_sources_workflow import early_context


@pytest.fixture
def sources(workspace, demo_bytes):
    current = workspace.ingest('current.dxf', demo_bytes)
    selected = workspace.ingest('same-project-revision-B.dxf', demo_bytes.replace(b'1.4.3', b'1.4.4'))
    unselected = workspace.ingest('same-project-revision-C.dxf', demo_bytes.replace(b'1.4.3', b'1.4.5'))
    session = CadAgentSession(workspace, ChatRequest(snapshot_id=current['id'], question='跨文件核对计算书与图内节能专篇',
        related_drawings=[dict(snapshot_id=selected['id'], project='same project', floor='一层', revision='B')]))
    return session, current, selected, unselected


@pytest.mark.parametrize('action', ['catalog', 'search', 'read'])
def test_only_selected_sources_are_accessed_even_for_empty_catalog_query(sources, action):
    s, current, selected, unselected = sources
    cat = s.execute('read_project_sources', dict(action='catalog'))
    assert {i['snapshot_id'] for i in cat['items']} == {current['id'], selected['id']}
    with pytest.raises(ValueError, match='本轮关联资料'):
        s.execute('read_project_sources', dict(action=action, snapshot_id=unselected['id'], query='', region_id='anything'))
    assert not s.evidence and cat['operation_receipts'] == []


def test_labels_and_reads_never_confirm_cross_version_relationship(sources):
    s, _, selected, _ = sources
    cat = s.execute('read_project_sources', dict(action='catalog'))
    assert cat['items'][1]['read_state'] == 'selected_not_read'
    assert cat['items'][1]['metadata']['revision'] == 'B'
    search = s.execute('read_project_sources', dict(action='search', snapshot_id=selected['id'], query='', limit=1))
    assert not s.evidence
    assert search['next_calls'][0]['arguments']['snapshot_id'] == selected['id']
    assert search['next_call']['tool'] == 'read_project_sources'
    read = s.execute(search['next_calls'][0]['tool'], search['next_calls'][0]['arguments'])
    assert all(c['tool'] == 'read_project_sources' and c['arguments']['snapshot_id'] == selected['id'] for c in read['next_actions'])
    cat = s.execute('read_project_sources', dict(action='catalog'))
    assert cat['items'][1]['read_state'] == 'source_spans_read'
    assert cat['items'][1]['relationship_state'] == 'project_floor_revision_unconfirmed'
    assert cat['items'][1]['whole_file_read'] is False
    assert cat['cross_file_comparison_complete'] is False


def test_source_reference_is_bound_but_does_not_make_referenced_file_available(sources):
    s, current, _, _ = sources
    # The reference is already in the read ledger; these controlled source spans
    # isolate catalog classification from parser behavior.
    s.evidence['reference'] = dict(evidence_id='reference', snapshot_id=current['id'], quote='保温厚度详见节能计算书。')
    cat = s.execute('read_project_sources', dict(action='catalog', query='节能计算书', reference_kind='external_file',
        reference_evidence_id='reference', reference_quote='详见节能计算书'))
    request = cat['requested_source']
    assert len(s.evidence) == 1 and s.evidence['reference']['quote'] == '保温厚度详见节能计算书。'
    assert request['reference']['state'] == 'source_reference_only'
    assert request['reference']['file_availability_proven'] is False
    assert request['state'] == 'selected_file_match_unconfirmed'
    assert request['identity_confirmed'] is False
    with pytest.raises(ValueError, match='逐字片段'):
        s.execute('read_project_sources', dict(action='catalog', query='计算书', reference_evidence_id='reference', reference_quote='已提供计算书'))


def test_internal_chapter_is_not_a_missing_file_and_stays_on_source_snapshot(sources):
    s, _, selected, _ = sources
    s.evidence['chapter'] = dict(evidence_id='chapter', snapshot_id=selected['id'], quote='窗参数详见本图节能专篇。')
    cat = s.execute('read_project_sources', dict(action='catalog', query='节能专篇', reference_kind='external_file',
        reference_evidence_id='chapter', reference_quote='详见本图节能专篇'))
    request = cat['requested_source']
    assert request['state'] == 'internal_section_lookup_pending'
    assert cat['next_calls'][0]['arguments']['snapshot_id'] == selected['id']
    assert request['current_file_content_absence_proven'] is False
    s.evidence['calcbook'] = dict(evidence_id='calcbook', snapshot_id=selected['id'], quote='本图引用节能计算书。')
    other = s.execute('read_project_sources', dict(action='catalog', query='节能计算书', reference_kind='external_file',
        reference_evidence_id='calcbook', reference_quote='本图引用节能计算书'))
    assert other['requested_source']['reference_kind'] == 'external_file'


def test_single_selected_calcbook_cannot_be_declared_missing_by_filename_or_count(workspace, demo_bytes):
    drawing = workspace.ingest('节能计算书.dxf', demo_bytes)
    s = CadAgentSession(workspace, ChatRequest(snapshot_id=drawing['id'], question='核对节能计算书'))
    result = s.execute('read_project_sources', dict(action='catalog', query='节能计算书', reference_kind='external_file'))
    request = result['requested_source']
    assert request['state'] == 'no_additional_file_selected'
    assert request['requested_file_availability'] == request['current_file_match'] == 'unconfirmed'
    assert result['next_calls'][0]['arguments']['snapshot_id'] == drawing['id']
    internal = s.execute('read_project_sources', dict(action='catalog', query='本图节能专篇', reference_kind='external_file'))
    assert internal['requested_source']['state'] == 'internal_section_lookup_pending'
    assert result['independent_current_file_work'] == 'continue'
    read = s.execute('read_project_sources', dict(action='read', snapshot_id=drawing['id'], region_id=s.index.regions[0].region_id))
    assert read['items'] and s.evidence


def test_initial_model_prompt_has_executable_catalog_but_no_read_credit(workspace, demo_bytes):
    drawing = workspace.ingest('地下室.dxf', demo_bytes)
    s = CadAgentSession(workspace, ChatRequest(snapshot_id=drawing['id'], question='共同轴网对应首层功能'))
    messages = [{'role':'user', 'content':s.request.question}]
    out = s.focus.messages(messages)
    packet = json.loads(out[-1]['content'])['project_sources']
    assert packet['next_calls'] == [dict(tool='read_project_sources', arguments=dict(action='catalog'))]
    assert packet['items'][0]['read_state'] == 'selected_not_read'
    assert s.calls == 0 and not s.evidence and not s.observations
    assert len(s.focus.messages(out)) == len(out)
    single = CadAgentSession(workspace, ChatRequest(snapshot_id=drawing['id'], question='查墙体材料'))
    assert early_context(single) is None
    assert single.focus.messages(messages) == messages


def test_catalog_search_read_use_existing_budget_and_only_read_credits_source(workspace, demo_bytes):
    drawing = workspace.ingest('current.dxf', demo_bytes)
    s = CadAgentSession(workspace, ChatRequest(snapshot_id=drawing['id'], question='查墙体材料'))
    limit = s.w.settings.agent_max_tool_calls
    cat = json.loads(s.invoke_tool('read_project_sources', dict(action='catalog')))
    assert 'error' not in cat and s.calls == 1
    assert not s.task.data['subtasks'][0].get('source_read', False)
    search = json.loads(s.invoke_tool('read_project_sources', dict(action='search', snapshot_id=drawing['id'], query='', limit=1)))
    assert 'error' not in search and s.calls == 2
    assert not s.task.data['subtasks'][0].get('source_read', False)
    call = search['next_calls'][0]
    read = json.loads(s.invoke_tool(call['tool'], call['arguments']))
    assert 'error' not in read and s.calls == 3
    assert s.task.data['subtasks'][0]['source_read'] is True
    assert s.w.settings.agent_max_tool_calls == limit
    assert all(not r.get('fulfilled') for r in s.task.data['subtasks'][0].get('operation_receipts', []))
