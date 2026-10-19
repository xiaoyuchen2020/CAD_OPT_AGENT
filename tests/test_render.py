"""Rendering assertions use DXF geometry, not string snapshots of implementation."""
from pathlib import Path
from xml.etree import ElementTree as ET
import json
from hashlib import sha256

import ezdxf
from ezdxf.enums import TextEntityAlignment
import pytest

from cad_agent.cad.render import render_drawing, render_svg, RENDER_VERSION


def fixture(tmp_path):
    doc = ezdxf.new('R2010', setup=True)
    m = doc.modelspace()
    m.add_text('CENTER', dxfattribs={'height':5}).set_placement((100,80),align=TextEntityAlignment.MIDDLE_CENTER)
    m.add_text('WIDTH', dxfattribs={'height':5,'width':1,'insert':(0,100)})
    m.add_text('WIDTH', dxfattribs={'height':5,'width':.5,'insert':(0,120)})
    m.add_text('ROTATE', dxfattribs={'height':5,'rotation':90,'insert':(150,100)})
    m.add_mtext('ALPHA BETA GAMMA DELTA EPSILON ZETA ETA THETA',dxfattribs={'insert':(10,60),'char_height':5,'width':45})
    m.add_text('墙窗',dxfattribs={'height':5,'insert':(10,150)})
    m.add_text('<script>',dxfattribs={'height':5,'insert':(10,160)})
    doc.layers.new('OFF').off()
    doc.layers.new('FROZEN').freeze()
    m.add_line((0,0),(10,0),dxfattribs={'layer':'OFF'})
    m.add_text('HIDDEN',dxfattribs={'layer':'FROZEN','height':5})
    m.add_text('INVISIBLE',dxfattribs={'height':5,'invisible':1})
    block=doc.blocks.new('HIDDEN_BLOCK')
    block.add_text('HIDDEN_CHILD',dxfattribs={'height':5})
    m.add_blockref('HIDDEN_BLOCK',(0,0),dxfattribs={'layer':'OFF'})
    block=doc.blocks.new('BLOCK')
    block.add_line((0,0),(10,0),dxfattribs={'color':0})
    m.add_blockref('BLOCK',(100,30),dxfattribs={'rotation':90,'xscale':2,'yscale':2,'color':1})
    m.add_line((10,5),(70,5),dxfattribs={'color':1,'linetype':'DASHED','lineweight':50})
    m.add_solid([(150,20),(180,20),(150,40),(180,40)])
    m.add_hatch(color=2).paths.add_polyline_path([(150,45),(180,45),(180,65),(150,65)],is_closed=True)
    m.add_linear_dim(base=(0,-15),p1=(0,0),p2=(200,0),dimstyle='EZDXF').render()
    path=tmp_path/'fixture.dxf';doc.saveas(path)
    return path


@pytest.fixture
def drawing(tmp_path):
    path=fixture(tmp_path)
    return path, render_drawing(path,'sample')


def test_alignment_width_wrapping_and_rotation(drawing):
    _,r=drawing
    assert r['complete'],r['diagnostics']
    texts=[i for i in r['items'] if i['type']=='text']
    center=next(i['bbox'] for i in texts if i['text']=='CENTER')
    assert abs((center[0]+center[2])/2-100)<.3
    assert abs((center[1]+center[3])/2-80)<.01
    widths=[i['bbox'][2]-i['bbox'][0] for i in texts if i['text']=='WIDTH']
    assert widths[1] == pytest.approx(widths[0]*.5)
    rotated=next(i['bbox'] for i in texts if i['text']=='ROTATE')
    assert rotated[3]-rotated[1] > 4*(rotated[2]-rotated[0])
    lines=[i for i in texts if 'MTEXT' in i['kinds'] and 'DIMENSION' not in i['kinds']]
    assert [i['text'] for i in lines]==['ALPHA BETA','GAMMA','DELTA','EPSILON','ZETA ETA','THETA']
    assert all(i['bbox'][2]-i['bbox'][0]<=45 for i in lines)
    assert lines[0]['bbox'][3] == pytest.approx(60)
    assert all(a['bbox'][1]>b['bbox'][3] for a,b in zip(lines,lines[1:]))


def test_visibility_style_fills_and_blocks(drawing):
    _,r=drawing
    assert not any('HIDDEN' in i.get('text','') or 'INVISIBLE' in i.get('text','') for i in r['items'])
    assert not any(i['layer'] in ('OFF','FROZEN') for i in r['items'])
    block=next(i for i in r['items'] if i['kinds']==['INSERT','LINE'])
    assert block['bbox']==pytest.approx([100,30,100,50])
    assert block['color']=='#ff0000'
    for kind in ('HATCH','SOLID'):
        assert any(kind in i['kinds'] and i.get('filled') for i in r['items'])
    assert any('DIMENSION' in i['kinds'] and i['type']=='text' for i in r['items'])
    dashed=next(i for i in r['items'] if i['kinds']==['LINE'] and i['color']=='#ff0000')
    assert dashed['d'].count('M')==1  # lightweight preview uses solid lines
    assert dashed['stroke_width']==pytest.approx(.5*96/25.4)


def test_shared_svg_native_unicode_text_strokes_and_escaping(drawing):
    _,r=drawing
    t=next(i for i in r['items'] if i.get('text')=='墙窗')
    assert t['width'] > 0 and 'glyphs' not in r and 'symbols' not in r
    layout=r['layouts'][0]
    svg=render_svg(r,layout['layout_id'],layout['bbox'])
    root=ET.fromstring(svg)
    texts=root.findall('.//{http://www.w3.org/2000/svg}text')
    assert any(e.text=='墙窗' for e in texts)
    assert all(float(e.get('textLength')) > 0 for e in texts)
    assert not root.findall('.//{http://www.w3.org/2000/svg}use')
    assert '&lt;script&gt;' in svg and '<script>' not in svg
    strokes=[e for e in root.iter() if e.get('stroke') not in (None,'none')]
    assert strokes and all(e.get('vector-effect')=='non-scaling-stroke' for e in strokes)
    assert all(float(e.get('stroke-width'))>=.65 for e in strokes)


def test_display_cache_does_not_rewrite_semantics(workspace,tmp_path,monkeypatch):
    path=fixture(tmp_path)
    d=workspace.ingest('fixture.dxf',path.read_bytes())
    files=[workspace.store.artifact(d['id'],name) for name in
           ('source.dxf','scene.json','regions.json','entities.json')]
    hashes=[sha256(p.read_bytes()).hexdigest() for p in files]
    before=workspace.store.get('drawing',d['id'])
    scene=workspace.preview_scene(d['id'])
    assert scene['render']['version']==RENDER_VERSION
    assert scene['lines']==[] and scene['texts']
    assert hashes==[sha256(p.read_bytes()).hexdigest() for p in files]
    assert workspace.store.get('drawing',d['id'])==before
    import cad_agent.cad.render as render
    def unexpected(*args):
        raise AssertionError('cache hit should not redraw')
    monkeypatch.setattr(render,'render_drawing',unexpected)
    assert workspace.rendering(d['id'])==scene['render']


def test_local_preview_does_not_silently_truncate():
    items=[dict(type='path',d=f'M{i} 0 L{i} 10',filled=False,bbox=[i,0,i,10],layout_id='L',
                color='#000000',stroke_width=1) for i in range(3000)]
    r=dict(items=items,symbols={},glyphs={},diagnostics=[])
    root=ET.fromstring(render_svg(r,'L',[0,0,3000,10]))
    assert len(root.findall('.//{http://www.w3.org/2000/svg}path'))==3000


def test_bigfont_fullwidth_text_does_not_overrun_authored_next_run(tmp_path):
    doc=ezdxf.new('R2010')
    doc.styles.new('CAD',dxfattribs={'font':'complex.shx','bigfont':'hztxt.shx'})
    m=doc.modelspace()
    # CAD full-width cells: 4 characters at height 10 and width .8 occupy 32.
    m.add_text('墙体材料',dxfattribs={'style':'CAD','height':10,'width':.8,'insert':(100,100)})
    m.add_text('C20',dxfattribs={'style':'CAD','height':8,'width':.6,'insert':(132,100)})
    p=tmp_path/'bigfont.dxf';doc.saveas(p)
    r=render_drawing(p,'test')
    runs={i['text']:i for i in r['items'] if i['type']=='text'}
    assert runs['墙体材料']['bbox'][2] <= 132
    assert runs['C20']['bbox'][0] >= 132
    assert r['complete']


def test_gbcbig_metrics_match_authored_dxf_fragment_gap(tmp_path):
    from cad_agent.cad.render import font_directory
    if not (font_directory()/'gbcbig.shx').exists():
        pytest.skip('Optional local GBCBIG resource is not installed')
    doc=ezdxf.new('R2010')
    doc.styles.get('Standard').dxf.update({'font':'gbenor.shx','bigfont':'gbcbig.shx'})
    m=doc.modelspace()
    m.add_text('钢筋混凝土楼板',dxfattribs={'insert':(751811.6753437505,21138.60951760301),'height':385.8156028368794})
    m.add_text('+',dxfattribs={'insert':(753777.632790559,21138.60951760301),'height':400})
    p=tmp_path/'fragment.dxf';doc.saveas(p)
    r=render_drawing(p,'test')
    runs={i['text']:i for i in r['items'] if i['type']=='text'}
    assert runs['钢筋混凝土楼板']['bbox'][2]<=runs['+']['bbox'][0]
    assert r['font_sources']['Standard']['bigfont_metrics']=='gbcbig.shx'


def test_dense_pattern_keeps_boundary_without_expanding_hatch_lines(tmp_path):
    doc=ezdxf.new('R2010')
    hatch=doc.modelspace().add_hatch()
    hatch.set_pattern_fill('ANSI31',scale=.001)
    hatch.paths.add_polyline_path([(0,0),(1000,0),(1000,1000),(0,1000)],is_closed=True)
    path=tmp_path/'dense.dxf';doc.saveas(path)
    r=render_drawing(path,'dense')
    assert r['complete'] and r['entered_types']['HATCH']==1
    paths=[i for i in r['items'] if 'HATCH' in i['kinds']]
    assert paths and all(not i['filled'] for i in paths)
    assert sum(len(i['d']) for i in paths)<500
    assert r['layouts'][0]['bbox']==pytest.approx([0,0,1000,1000])


def test_dense_polyline_simplification_preserves_closed_shape(tmp_path):
    doc=ezdxf.new('R2010')
    points=([(i,0) for i in range(1000)]+[(1000,i) for i in range(1000)]
            +[(1000-i,1000) for i in range(1000)]+[(0,1000-i) for i in range(1000)])
    doc.modelspace().add_lwpolyline(points,close=True)
    path=tmp_path/'dense-line.dxf';doc.saveas(path)
    r=render_drawing(path,'dense-line')
    assert r['complete'] and len(r['items'])==1
    i=r['items'][0]
    assert i['bbox']==pytest.approx([0,0,1000,1000])
    assert i['d'].count('L')==4 and i['d'].endswith('L0 0')


def test_expansion_budget_is_per_root_and_does_not_drop_later_layout(tmp_path, monkeypatch):
    import cad_agent.cad.render as renderer
    monkeypatch.setattr(renderer, 'MAX_ENTITIES', 4)
    doc=ezdxf.new('R2010');m=doc.modelspace()
    block=doc.blocks.new('NORMAL');block.add_line((0,0),(1,0));block.add_line((0,1),(1,1))
    for x in range(10):m.add_blockref('NORMAL',(x*10,0))
    doc.layouts.new('Sheet').add_line((100,100),(101,101))
    p=tmp_path/'roots.dxf';doc.saveas(p);r=render_drawing(p,'roots')
    assert r['complete'],r['diagnostics']
    assert len(r['items'])==21 and len(r['layouts'])==2


def test_rejected_block_does_not_poison_later_entity_state(tmp_path,monkeypatch):
    import cad_agent.cad.render as renderer
    monkeypatch.setattr(renderer,'MAX_ENTITIES',4)
    doc=ezdxf.new('R2010');block=doc.blocks.new('HUGE')
    for x in range(20):block.add_line((x,0),(x,1),dxfattribs={'color':0})
    doc.modelspace().add_blockref('HUGE',(0,0),dxfattribs={'color':1})
    doc.modelspace().add_line((100,100),(200,200),dxfattribs={'color':3})
    p=tmp_path/'limited-root.dxf';doc.saveas(p);r=render_drawing(p,'limited')
    assert not r['complete'] and any('单个源实体' in d for d in r['diagnostics'])
    last=r['items'][-1]
    assert last['bbox']==[100,100,200,200] and last['kinds']==['LINE'] and last['color']=='#00ff00'


@pytest.mark.parametrize('normal',[(0,0,-1),(0,1,1)])
def test_pattern_outline_uses_world_coordinates_and_elevation(tmp_path,normal):
    from ezdxf.path import from_hatch
    from cad_agent.cad.render import bounds
    doc=ezdxf.new('R2010');h=doc.modelspace().add_hatch(dxfattribs={'extrusion':normal,'elevation':(0,0,30)})
    h.set_pattern_fill('ANSI31',scale=.001)
    h.paths.add_polyline_path([(100,200),(120,200),(120,210),(100,210)],is_closed=True)
    expected=bounds(v for path in from_hatch(h) for v in path.control_vertices())
    p=tmp_path/'ocs.dxf';doc.saveas(p);r=render_drawing(p,'ocs')
    assert r['complete']
    assert r['items'][0]['bbox']==pytest.approx(expected)


@pytest.mark.parametrize('frame',[0,1,2])
def test_xclip_scope_preserves_siblings_attributes_and_later_layout(tmp_path,frame):
    from ezdxf.xclip import XClip
    doc=ezdxf.new('R2010');doc.header['$XCLIPFRAME']=frame
    child=doc.blocks.new('CHILD');child.add_line((-10,2),(20,2))
    parent=doc.blocks.new('PARENT')
    ref=parent.add_blockref('CHILD',(0,0))
    XClip(ref).set_block_clipping_path([(0,0),(5,5)])
    ref.add_attrib('LABEL','ATTRIBUTE',(8,3),dxfattribs={'height':1})
    parent.add_line((0,8),(20,8))
    root=doc.modelspace().add_blockref('PARENT',(100,100))
    XClip(root).set_block_clipping_path([(0,0),(10,10)])
    doc.modelspace().add_line((200,200),(220,200))
    doc.modelspace().add_text('AFTER',dxfattribs={'insert':(200,210),'height':2})
    doc.layouts.new('Sheet').add_line((300,300),(310,300))
    p=tmp_path/'clips.dxf';doc.saveas(p);r=render_drawing(p,'clips')
    assert r['complete'],r['diagnostics']
    boxes=[i['bbox'] for i in r['items'] if 'LINE' in i['kinds']]
    for expected in ([100,102,105,102],[100,108,110,108],[200,200,220,200],[300,300,310,300]):
        assert any(b==pytest.approx(expected) for b in boxes), (expected,boxes)
    # ATTRIB lies outside its child's clip, but inside the parent's clip.
    assert any('ATTRIB' in i['kinds'] for i in r['items'])
    assert any(i.get('text')=='AFTER' for i in r['items'])


def test_xclip_minsert_cells_restore_independent_clip(tmp_path):
    from ezdxf.xclip import XClip
    doc=ezdxf.new('R2010');doc.header['$XCLIPFRAME']=0
    block=doc.blocks.new('CELL');block.add_line((-10,2),(20,2))
    ref=doc.modelspace().add_blockref('CELL',(0,0),dxfattribs={'column_count':2,'column_spacing':20})
    XClip(ref).set_block_clipping_path([(0,0),(5,5)])
    p=tmp_path/'cells.dxf';doc.saveas(p);r=render_drawing(p,'cells')
    assert r['complete'],r['diagnostics']
    assert [i['bbox'] for i in r['items']]==[[0,2,5,2],[20,2,25,2]]


@pytest.mark.parametrize('extent',[100,10000])
def test_raster_export_keeps_screen_linewidth_at_different_world_scales(extent):
    import io
    import resvg_py
    from PIL import Image
    r=dict(items=[dict(type='path',d=f'M{extent*.1} {extent/2} L{extent*.9} {extent/2}',
        filled=False,bbox=[extent*.1,extent/2,extent*.9,extent/2],layout_id='L',
        color='#000000',stroke_width=2)],diagnostics=[])
    svg=render_svg(r,'L',[0,0,extent,extent],width=128,height=128,raster=True)
    im=Image.open(io.BytesIO(resvg_py.svg_to_bytes(svg_string=svg))).convert('RGB')
    assert sum(im.getpixel((64,y))[0]<128 for y in range(128))==2
