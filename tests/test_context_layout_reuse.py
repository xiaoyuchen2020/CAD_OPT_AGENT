import ezdxf
from cad_agent.analysis.general import DrawingIndex
from cad_agent.components.context import build_context,cached_context


def test_layout_cache_reconstructs_frame_projection_without_cross_owner_leak(workspace,tmp_path,monkeypatch):
    d=ezdxf.new();m=d.modelspace()
    m.add_text('首层平面图',dxfattribs={'insert':(10,20),'height':1})
    m.add_text('二层平面图',dxfattribs={'insert':(60,20),'height':1})
    p=tmp_path/'context.dxf';d.saveas(p);drawing=workspace.ingest('context.dxf',p.read_bytes())
    reading=DrawingIndex(workspace,drawing['snapshot_id']);lid=drawing['layouts'][0]['layout_id']
    frames=[dict(frame_id='left',name='left',bbox=[0,0,40,40],method='test'),dict(frame_id='right',name='right',bbox=[50,0,100,40],method='test')]
    expected=[build_context(reading,lid,group) for group in ([frames[0]],[frames[1]],frames,[])]
    calls=[]
    import cad_agent.components.context as module
    def counted(*args):calls.append(1);return build_context(*args)
    monkeypatch.setattr(module,'build_context',counted)
    for group,wanted in zip(([frames[0]],[frames[1]],frames,[]),expected):
        result=cached_context(reading,lid,group)
        assert result==wanted
        result['diagnostics'].append('caller_mutation')
    assert len(calls)==1
    # A changed parsed-source signature must not reuse an older base.
    reading.signature=reading.signature+('new_source_revision',)
    assert cached_context(reading,lid,frames)==expected[2]
    assert len(calls)==2
