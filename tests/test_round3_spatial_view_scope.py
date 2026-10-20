"""Explicit view resolution uses source titles and borders, with no model calls."""
import io
import json
import shutil
from pathlib import Path

import ezdxf
import pytest
from pydantic import ValidationError

from cad_agent.analysis.fact_tools import ResolveScope, resolve_scope
from cad_agent.analysis.scope_workflow import selected_for_call
from cad_agent.analysis.spatial_evidence import BindSpatialObject, bind_spatial_object
from cad_agent.analysis.view_scope import title_key
from test_remediation_scope import session, invoke
from test_room_batch import room_fixture


def source_view(workspace, title='2-2剖面图1:150', extra_title=None, duplicate=False, paper=False):
    doc = ezdxf.new('R2018')
    doc.units = 4
    def build(layout, dx=0):
        layout.add_lwpolyline([(dx,0),(dx+10000,0),(dx+10000,10000),(dx,10000)], close=True)
        for text, x, y in [(title,500,9000),('结构标高 -6.600m',1000,1000),
                           ('结构标高 -3.000m',3000,3000),('构件A',5000,5000)]:
            layout.add_text(text,dxfattribs={'insert':(dx+x,y),'height':100})
        layout.add_line((dx+1000,1500),(dx+5000,1500))
        if extra_title:
            layout.add_text(extra_title,dxfattribs={'insert':(dx+500,7000),'height':100})
    build(doc.modelspace())
    if duplicate:
        build(doc.modelspace(),12000)
    if paper:
        build(doc.layouts.new('Second'))
    stream=io.StringIO();doc.write(stream)
    return workspace.ingest('views.dxf',stream.getvalue().encode())


@pytest.mark.parametrize('title,view', [('2-2剖面图1:150','section'),('节点图','detail')])
def test_native_explicit_view_can_bind_source_geometry(workspace,title,view):
    drawing=source_view(workspace,title)
    s=session(workspace,drawing)
    result=invoke(s,'resolve_drawing_scope',dict(view=view,floor_title=title_key(title),task_ids=['q1']))
    assert 'error' not in result,result
    scope=result['selected_scope']
    assert scope and scope['view_kind']==view,result
    assert scope['datum_status']==scope['role_status']==scope['view_equivalence_status']=='unconfirmed'
    assert not result['operation_receipts']
    native=selected_for_call(s,'inspect_scope_objects')
    assert native['view_kind']==view
    line=next(r for r in s.index.scene['lines'] if r['kind']=='LINE')
    labels=[r for r in s.index.regions if r.text.startswith('结构标高')]
    obj=bind_spatial_object(s,BindSpatialObject(scope_id=scope['scope_id'],
        entity_ids=[line['entity_id']],label_region_ids=[r.region_id for r in labels]))
    assert obj['object_id']


def test_default_plan_behavior_and_no_implicit_section_upgrade(workspace):
    _,_,drawing=room_fixture(workspace,units=4)
    result=invoke(session(workspace,drawing),'resolve_drawing_scope',dict(floor_title='三层平面图',task_ids=['q1']))
    assert result['selected_scope'] and 'view_kind' not in result['selected_scope']
    drawing=source_view(workspace)
    result=invoke(session(workspace,drawing),'resolve_drawing_scope',dict(floor_title='2-2剖面图1:150',task_ids=['q1']))
    assert not result['selected_scope']
    assert all('not_plan_title' in c['reasons'] for c in result['choices'])


@pytest.mark.parametrize('extra_title', ['1-1剖面图1:150','一层平面图','节点详图'])
def test_mixed_views_are_not_one_section(workspace,extra_title):
    drawing=source_view(workspace,extra_title=extra_title)
    result=invoke(session(workspace,drawing),'resolve_drawing_scope',dict(view='section',floor_title='2-2剖面图',task_ids=['q1']))
    assert not result['selected_scope']
    assert 'conflicting_view_titles' in result['choices'][0]['reasons']


def test_repeated_native_title_is_anchor_only_and_wrong_view_not_selected(workspace):
    drawing=source_view(workspace,extra_title='2-2剖面图1:150')
    s=session(workspace,drawing)
    result=invoke(s,'resolve_drawing_scope',dict(view='section',floor_title='2-2剖面图',task_ids=['q1']))
    assert result['selected_scope']['title_evidence_count']==2
    wrong=invoke(s,'resolve_drawing_scope',dict(view='detail',floor_title='2-2剖面图1:150',task_ids=['q1']))
    assert not wrong['selected_scope']
    assert 'requested_title_view_mismatch' in wrong['choices'][0]['reasons']
    with pytest.raises(ValueError,match='SCOPE_UNRESOLVED'):
        selected_for_call(s,'inspect_scope_objects')


def test_duplicate_title_frames_and_layout_identity(workspace):
    drawing=source_view(workspace,duplicate=True)
    r=invoke(session(workspace,drawing),'resolve_drawing_scope',dict(view='section',floor_title='2-2剖面图',task_ids=['q1']))
    assert len([c for c in r['choices'] if c['eligible']])==2 and not r['selected_scope']
    drawing=source_view(workspace,paper=True)
    s=session(workspace,drawing)
    r=invoke(s,'resolve_drawing_scope',dict(view='section',floor_title='2-2剖面图',task_ids=['q1']))
    assert not r['selected_scope']
    lid=drawing['layouts'][0]['layout_id']
    r=invoke(s,'resolve_drawing_scope',dict(view='section',floor_title='2-2剖面图',layout_id=lid,task_ids=['q1']))
    assert r['selected_scope']['layout_id']==lid


@pytest.mark.parametrize('damage',['border','native_title','title_layout'])
def test_missing_source_proofs_cannot_be_overridden(workspace,damage):
    drawing=source_view(workspace)
    path=workspace.store.artifact(drawing['id'],'scene.json')
    data=json.loads(path.read_text())
    if damage=='border':
        data['lines']=[l for l in data['lines'] if l['kind']=='LINE']
    else:
        t=next(t for t in data['texts'] if '剖面图' in t['text'])
        t['text' if damage=='native_title' else 'layout_id']='other'
    path.write_text(json.dumps(data))
    result=invoke(session(workspace,drawing),'resolve_drawing_scope',
        dict(view='section',floor_title='2-2剖面图1:150',task_ids=['q1']))
    assert not result['selected_scope']
    assert ('missing_native_closed_border' if damage=='border' else 'missing_matching_native_title') in result['choices'][0]['reasons']


def test_model_cannot_invent_scope_bbox_or_frame_id():
    for extra in ({'bbox':[0,0,10,10]},{'frame_id':'invented'},{'view':'elevation'}):
        with pytest.raises(ValidationError):
            ResolveScope.model_validate(dict(floor_title='2-2剖面图')|extra)


def replay_real_q16(workspace):
    root=Path(__file__).resolve().parents[1]/'artifacts/designer-facts-unlimited-final-20261020'
    if not (root/'evaluated-results.json').exists():
        pytest.skip('frozen Q16 inputs unavailable')
    source=next(r for r in json.loads((root/'evaluated-results.json').read_text()) if r['id']=='Q16-B-sol-high')
    evidence=Path(source['evidence_path'])
    frozen_task=json.loads((evidence/'task.json').read_text())
    sid=frozen_task['snapshot_id']
    inputs=root/'inputs'/sid
    drawing=json.loads((inputs/'drawing.json').read_text())
    workspace.store.put('drawing',sid,drawing)
    for name in ('source.dxf','regions.json','scene.json','entities.json'):
        dest=workspace.store.artifact(sid,name);dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(inputs/name,dest)
    s=session(workspace,drawing)
    queries=[('section','2-2剖面图'),('section','J1-J1剖面图'),
             ('detail','集水坑大样一'),('section','单轨防火卷帘卷筒安装剖面图')]
    results=[]
    for view,title in queries:
        r=resolve_scope(s,ResolveScope(view=view,floor_title=title))
        assert r['selected_scope'] is None,r
        assert r['choices'] and all('conflicting_view_titles' in c['reasons'] for c in r['choices'])
        results.append(dict(request=dict(view=view,floor_title=title),result=r))
    return dict(evidence_class='frozen_source_deterministic_replay',model_calls=0,
        selected_evidence_path=str(evidence),source_snapshot_id=sid,source_sha256=drawing['sha256'],
        results=results,conclusion='Q16 source has real closed sheet borders and titles, but multiple view titles conflict within each requested frame. No active view scope is fabricated; datum, surface roles and view equivalence remain unconfirmed.')


def test_real_q16_source_keeps_mixed_view_conflicts(workspace):
    replay_real_q16(workspace)
