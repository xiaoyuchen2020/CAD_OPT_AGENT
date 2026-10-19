"""Service entrypoints must not accidentally return to parent geometry work."""
import io
import json
import ezdxf
import pytest
from cad_agent.geometry.service import GeometryService
from cad_agent.geometry.contracts import GeometryMeasure, SpatialQuery
from cad_agent.geometry import source


def test_source_query_and_measure_run_outside_parent(workspace, monkeypatch):
    doc=ezdxf.new();doc.units=4
    doc.modelspace().add_lwpolyline([(0,0),(6000,0),(6000,4000),(0,4000)],close=True)
    text=io.StringIO();doc.write(text)
    drawing=workspace.ingest('isolation.dxf',text.getvalue().encode())
    service=GeometryService(workspace,drawing['id'])
    events=[];execute=workspace.geometry_executor.compute
    def track(op,payload,timeout=None):
        json.dumps(payload)
        events.append(op)
        return execute(op,payload,timeout)
    monkeypatch.setattr(workspace.geometry_executor,'compute',track)
    def forbidden(*args,**kwargs):raise AssertionError('Geometry executed in parent')
    monkeypatch.setattr(source.ezdxf,'readfile',forbidden)
    monkeypatch.setattr(source,'LineString',forbidden)
    entity=service.index.data['entities'][0]
    page=service.query(SpatialQuery(layout_id=entity['layout_id'],bbox=[-1,-1,6001,4001]))
    result=service.measure(GeometryMeasure(operation='area',entity_ids=[entity['entity_id']]))
    assert page['total']==1 and result['value']==24
    assert {'source_geometry','spatial_query','geometry_measure'}<=set(events)
    assert workspace.store.get('measurement',result['measurement_id'])['value']==24


def test_changed_source_stamp_is_rejected_before_geometry(workspace,demo_bytes):
    d=workspace.ingest('source.dxf',demo_bytes)
    path=workspace.store.artifact(d['id'],'source.dxf')
    payload=dict(source_path=str(path),source_stamp=['stale'],sid=d['id'],
        drawing={k:d[k] for k in ('sha256','unit')},selection={})
    with pytest.raises(ValueError,match='来源文件已变化'):
        workspace.geometry_executor.compute('source_geometry',payload)
    assert workspace.store.count('measurement')==0
