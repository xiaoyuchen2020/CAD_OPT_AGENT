from hashlib import sha256
import json
import ezdxf
import pytest

from cad_agent.cad.reader import entity_batches, walk_entities, read_drawing
from cad_agent.cad.reindex import reindex_drawing
from cad_agent.analysis.general import DrawingIndex
from cad_agent.geometry.source import native_source, build_geometry


def block_drawing():
    doc=ezdxf.new('R2010');doc.units=4
    b=doc.blocks.new('PAIR');b.add_text('恢复文字',dxfattribs={'height':1});b.add_line((0,0),(10,0))
    for i in range(8):doc.modelspace().add_blockref('PAIR',(i*100,0))
    doc.layouts.new('Sheet').add_text('最后布局',dxfattribs={'height':1})
    return doc


def test_root_batches_keep_every_instance_and_stable_paths():
    doc=block_drawing();m=doc.modelspace();stats={};issues=[]
    batches=list(entity_batches(m,issues,'Model',batch_size=3,entity_limit=4,stats=stats))
    got=[path for batch in batches for e,path in batch]
    reference=[path for e,path in walk_entities(m,[],'Model',entity_limit=100)]
    assert got==reference and len(got)==16 and max(map(len,batches))<=3
    assert not issues and stats['visited']==24 and stats['max_root_visited']==3


def test_root_limit_does_not_hide_later_root_or_layout(tmp_path,monkeypatch):
    from cad_agent.cad import reader
    doc=block_drawing();huge=doc.blocks.new('HUGE')
    for i in range(20):huge.add_text(f'too many {i}')
    doc.modelspace().add_blockref('HUGE',(1000,0))
    doc.modelspace().add_text('尾部文字',dxfattribs={'height':1})
    p=tmp_path/'roots.dxf';doc.saveas(p);monkeypatch.setattr(reader,'MAX_ENTITIES',4)
    texts,scene,_,_,issues=read_drawing(p,'snap_test')
    assert sum(t.text=='恢复文字' for t in texts)==8
    assert {'尾部文字','最后布局'}<={t.text for t in texts}
    assert any(d.code=='ENTITY_LIMIT' for d in issues)
    assert scene['parse_coverage']['limited_roots']==1


def test_geometry_uses_same_bounded_walker_without_global_cap(workspace,tmp_path,monkeypatch):
    from cad_agent.cad import reader
    doc=block_drawing();p=tmp_path/'roots.dxf';doc.saveas(p)
    monkeypatch.setattr(reader,'MAX_ENTITIES',4)
    d=workspace.ingest(p.name,p.read_bytes());source=native_source(workspace,d['id'])
    assert len(source['entities'])==8 and source['coverage']['max_root_visited']==3
    last=sorted(source['entities'],key=lambda eid:source['entities'][eid][3][0])[-1]
    data=build_geometry(workspace,d['id'],entity_ids=[last])
    assert data['entities'][0]['paths']==[[[700.,0.],[710.,0.]]]


def test_reindex_preserves_old_snapshot_and_does_not_reconvert(workspace,tmp_path,monkeypatch):
    import cad_agent.service as service
    monkeypatch.setattr(service,'PARSER_VERSION','old-test-parser')
    doc=block_drawing();p=tmp_path/'roots.dxf';doc.saveas(p)
    old=workspace.ingest(p.name,p.read_bytes());folder=workspace.store.artifact_dir(old['id'])
    hashes={p.name:sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
    monkeypatch.setattr(workspace.converter,'convert',lambda *a:pytest.fail('must not reconvert'))
    new=reindex_drawing(workspace,old['id'])
    assert new['id']!=old['id'] and new['reindexed_from']==old['id']
    assert new['sha256']==old['sha256'] and new['latest_fact_set_id'] is None
    assert new['text_count']==9 and new['index_coverage']['visited']>4
    assert workspace.store.get('drawing',old['id'])==old
    assert hashes=={p.name:sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
    assert reindex_drawing(workspace,old['id'])==new
    before=DrawingIndex(workspace,old['id']);after=DrawingIndex(workspace,new['id'])
    assert set(before.by_id).isdisjoint(after.by_id)
    assert after.search('最后布局')['total']==1
    workspace.store.artifact(old['id'],'source.dxf').write_bytes(b'bad')
    with pytest.raises(ValueError,match='校验失败'):reindex_drawing(workspace,old['id'])


def test_search_cache_is_lazy_and_invalidates_replaced_artifacts(workspace,demo_bytes):
    d=workspace.ingest('demo.dxf',demo_bytes);sid=d['id'];a=DrawingIndex(workspace,sid)
    assert a._data['scene'] is None
    a.search('混凝土');b=DrawingIndex(workspace,sid)
    assert b.regions is a.regions and b._data['scene'] is None
    assert a.scene is b.scene
    p=workspace.store.artifact(sid,'regions.json');rows=json.loads(p.read_text());rows[0]['text']='替换后的来源'
    p.write_text(json.dumps(rows,ensure_ascii=False))
    c=DrawingIndex(workspace,sid)
    assert c.regions is not a.regions and c.search('替换后的来源')['total']==1


@pytest.mark.parametrize('normal',[(0,0,1),(0,0,-1),(0,1,1)])
def test_fast_candidate_bounds_never_exclude_transformed_curve_points(normal):
    from cad_agent.geometry.source import candidate_bounds
    from ezdxf.path import make_path
    doc=ezdxf.new();m=doc.modelspace()
    entities=[m.add_arc((20,40,30),12,30,210,dxfattribs={'extrusion':normal}),
              m.add_circle((20,40,30),12,dxfattribs={'extrusion':normal}),
              m.add_ellipse((20,40,30),major_axis=(4,0,0),ratio=.4,dxfattribs={'extrusion':normal})]
    for e in entities:
        a,b,c,d=candidate_bounds(e)
        for p in make_path(e).flattening(.001):
            assert a-1e-6<=p.x<=c+1e-6 and b-1e-6<=p.y<=d+1e-6
