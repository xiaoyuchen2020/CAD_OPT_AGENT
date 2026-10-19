from pathlib import Path
import json
import gzip
from hashlib import sha256
import ezdxf
from fastapi.testclient import TestClient
from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.cad.preview import ensure_preview,preview_file


def test_packets_reassemble_exact_render_and_keep_semantics(workspace,tmp_path):
    doc=ezdxf.new('R2010');m=doc.modelspace()
    m.add_line((0,0),(2000,2000))
    for i in range(850):
        m.add_text(f'墙体 {i}',dxfattribs={'height':2 if i%2 else 200,'insert':(i*3,100)})
    doc.layouts.new('Sheet').add_text('纸空间',dxfattribs={'height':5})
    source=tmp_path/'input.dxf';doc.saveas(source)
    drawing=workspace.ingest('input.dxf',source.read_bytes());sid=drawing['id']
    paths=[workspace.store.artifact(sid,n) for n in ('source.dxf','scene.json','entities.json','regions.json')]
    hashes=[sha256(p.read_bytes()).hexdigest() for p in paths]
    render=workspace.rendering(sid)
    rev,folder=ensure_preview(workspace,sid)
    manifest=json.loads((folder/'manifest.json').read_text());restored={}
    for name in list(manifest['bases'].values())+[c['file'] for c in manifest['chunks']]:
        path=preview_file(workspace,sid,rev,name)
        assert gzip.decompress(path.with_suffix('.json.gz').read_bytes())==path.read_bytes()
        for i,item in json.loads(path.read_text()):
            assert i not in restored;restored[i]=item
    assert [restored[i] for i in range(len(restored))]==render['items']
    assert all(c['count']<=400 for c in manifest['chunks'])
    assert len(manifest['bases'])==2 and manifest['texts']==[]
    assert hashes==[sha256(p.read_bytes()).hexdigest() for p in paths]
    stamp=(folder/'manifest.json').stat().st_mtime_ns
    assert ensure_preview(workspace,sid)==(rev,folder)
    assert (folder/'manifest.json').stat().st_mtime_ns==stamp
    assert not (folder/'meta.json').exists()
    meta=json.loads(preview_file(workspace,sid,rev,'meta').read_text())
    assert meta and all(set(row)=={'entity_id','entity_path'} for row in meta)
    # A changed display cache gets a distinct immutable URL.
    target=workspace.store.artifact(sid,'render.json');target.touch()
    assert ensure_preview(workspace,sid)[0]!=rev


def test_packet_cache_and_validation(tmp_path):
    app=create_app(Settings(data_dir=tmp_path/'data'))
    w=app.state.workspace
    doc=ezdxf.new();doc.modelspace().add_line((0,0),(1,1));p=tmp_path/'d.dxf';doc.saveas(p)
    d=w.ingest('d.dxf',p.read_bytes());prefix=f'/api/drawings/{d["id"]}'
    with TestClient(app) as c:
        index=c.get(prefix+'/preview');assert index.status_code==200
        assert index.headers['cache-control']=='no-store'
        manifest=index.json();url=prefix+'/preview/'+manifest['revision']+'/'+next(iter(manifest['bases'].values()))
        packet=c.get(url);assert packet.status_code==200 and packet.json()
        assert 'immutable' in packet.headers['cache-control'] and packet.headers['content-encoding']=='gzip'
        assert c.get(url,headers={'accept-encoding':'identity'}).json()==packet.json()
        assert c.get(prefix+'/preview/not-a-revision/base-0').status_code==404
        assert c.get(prefix+'/preview/'+manifest['revision']+'/source.dxf').status_code==404


def test_large_geometry_spatial_packets_are_bounded_and_lossless(workspace,tmp_path,monkeypatch):
    doc=ezdxf.new();doc.modelspace().add_line((0,0),(1,1));p=tmp_path/'large.dxf';doc.saveas(p)
    d=workspace.ingest('large.dxf',p.read_bytes());sid=d['id'];r=workspace.rendering(sid)
    template=r['items'][0]
    r['items']=[{**template,'bbox':[i%1000,(i//1000)*10,i%1000+1,(i//1000)*10+1]} for i in range(50001)]
    r['layouts'][0]['bbox']=[0,0,1000,501]
    monkeypatch.setattr(workspace,'rendering',lambda _:r)
    _,folder=ensure_preview(workspace,sid);m=json.loads((folder/'manifest.json').read_text())
    geometry=[c for c in m['chunks'] if c['kind']=='geometry']
    assert geometry and len(geometry)<100
    assert max(c['count'] for c in geometry)<=1600
    assert sum(c['count'] for c in geometry)==50001
    seen=set()
    for c in geometry:
        assert c['min_pixels']==.8
        for i,item in json.loads((folder/(c['file']+'.json')).read_text()):
            assert i not in seen;seen.add(i)
            assert item==r['items'][i]
            a=c['bbox'];b=item['bbox']
            assert a[0]<=b[0]<=b[2]<=a[2] and a[1]<=b[1]<=b[3]<=a[3]
    assert len(seen)==50001


def test_renderer_upgrade_rebuilds_preview_instead_of_reusing_sparse_cache(workspace,tmp_path,monkeypatch):
    import cad_agent.cad.render as render
    import cad_agent.cad.preview as preview
    doc=ezdxf.new();doc.modelspace().add_line((0,0),(100,100))
    p=tmp_path/'upgrade.dxf';doc.saveas(p)
    d=workspace.ingest('upgrade.dxf',p.read_bytes());sid=d['id']
    target=workspace.store.artifact(sid,'render.json')
    old=workspace.rendering(sid);old['items']=[]
    workspace.store.write_json(target,old)
    old_rev,old_folder=ensure_preview(workspace,sid)
    assert json.loads((old_folder/'manifest.json').read_text())['total_items']==0
    monkeypatch.setattr(render,'RENDER_VERSION',render.RENDER_VERSION+'-upgrade')
    monkeypatch.setattr(preview,'RENDER_VERSION',render.RENDER_VERSION)
    rev,folder=ensure_preview(workspace,sid)
    assert rev!=old_rev
    assert json.loads((folder/'manifest.json').read_text())['total_items']==1
    assert json.loads(target.read_text())['version']==render.RENDER_VERSION
