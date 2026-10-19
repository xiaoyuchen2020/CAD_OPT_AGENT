"""Deterministic analysis-only visual cases and exact linework diagnostics."""
import io,json,math
from collections import Counter,defaultdict
from pathlib import Path
import xml.etree.ElementTree as ET
from scripts.floorplancad_failure_audit import OUT,ROOT,roster,guard,read,save,sha,pred_path

def choices(samples):
    pools=defaultdict(list)
    for r in samples:
        for obj in r['instances']:
            if obj['stage']=='generation_no_target_candidate' and obj['bbox']:
                pools['generation_'+obj['category']].append(dict(sample_id=r['sample_id'],group_id=r['group_id'],focus_ids=obj['entity_ids'],bbox=obj['bbox'],instance=obj))
        for c in r['candidates']:
            if c['kind']=='window' and c['target_parts'] and c['category_histogram'].get('wall',0):
                pools['ownership_mixed_window'].append(dict(sample_id=r['sample_id'],group_id=r['group_id'],focus_ids=c['entity_ids'],bbox=c['bbox'],candidate=c))
        grown=defaultdict(list)
        for g in r['door_growth']:
            if g['raw_semantic_id']==0:grown[g['candidate_index']].append(g['entity_id'])
        for i,ids in grown.items():
            c=r['candidates'][i];pools['label_zero_growth'].append(dict(sample_id=r['sample_id'],group_id=r['group_id'],focus_ids=c['entity_ids'],bbox=c['bbox'],candidate=c,added_zero_ids=ids))
        for g in r['coincident_groups']:
            if g['wall_window']:
                # Use a complete touching window candidate when available, so a
                # tiny shared edge is reviewed with its actual object context.
                cs=[c for c in r['candidates'] if c['kind']=='window' and set(c['entity_ids'])&set(g['entity_ids'])]
                if cs:
                    c=min(cs,key=lambda c:len(c['entity_ids']));pools['label_coincident_wall_window'].append(dict(sample_id=r['sample_id'],group_id=r['group_id'],focus_ids=c['entity_ids'],bbox=c['bbox'],candidate=c,coincident=g))
        for axis in r['opening_axis_proxies']:
            if axis['host_support_ids']:continue
            cs=[r['candidates'][i] for i in axis['candidate_indices'] if r['candidates'][i]['target_parts']]
            if cs:
                c=min(cs,key=lambda c:len(c['entity_ids']));pools['topology_unverified_opening_axis'].append(dict(sample_id=r['sample_id'],group_id=r['group_id'],focus_ids=c['entity_ids'],bbox=c['bbox'],candidate=c,axis=axis))
    selected=[];used=set()
    order=['generation_door','generation_window','ownership_mixed_window','label_zero_growth','label_coincident_wall_window','topology_unverified_opening_axis']
    for name in order:
        candidates=sorted(pools[name],key=lambda c:sha((name+':'+c['sample_id']+':'+','.join(sorted(c['focus_ids']))).encode()))
        n=0
        for c in candidates:
            if c['group_id'] in used:continue
            used.add(c['group_id']);selected.append(c|dict(case_id=f'A{len(selected)+1:02}',stratum=name));n+=1
            if n==2:break
    return selected,{k:len(v) for k,v in pools.items()}

def linework(ps,ids,bbox):
    from shapely.geometry import LineString,box
    from shapely.ops import unary_union,polygonize_full
    from shapely.strtree import STRtree
    crop=box(*bbox);lines=[];source_count=0
    for p in ps:
        if p.entity_id not in ids or p.kind!='line':continue
        g=LineString(p.points).intersection(crop)
        if g.geom_type=='LineString' and g.length>0:lines.append(g);source_count+=1
    # Exact source geometry only, no snapping/closing/width assumptions.
    exact={tuple(sorted(tuple(pt) for pt in g.coords)):g for g in lines};lines=list(exact.values())
    out=dict(source_lines=source_count,unique_clipped_lines=len(lines),snapping=False,synthetic_edges=0,complete=True)
    if len(lines)>5000:return out|dict(complete=False,reason='existing_room_segment_budget')
    tree=STRtree(lines);pairs=0
    for i,g in enumerate(lines):
        pairs+=sum(int(j)>i for j in tree.query(g,predicate='intersects'))
        if pairs>20000:return out|dict(complete=False,reason='existing_room_intersection_budget')
    if not lines:return out|dict(polygon_faces=0,cut_edges=0,dangling_edges=0,invalid_rings=0)
    noded=unary_union(lines);faces,cuts,dangles,invalid=polygonize_full(noded)
    return out|dict(polygon_faces=len(faces.geoms),cut_edges=len(cuts.geoms),dangling_edges=len(dangles.geoms),invalid_rings=len(invalid.geoms),
        note='Faces may be wall strips, symbols, crop artifacts or rooms. These counts do not measure room accuracy; crop clips can introduce dangling endpoints.')

def crop_box(b):
    x0,y0,x1,y1=b;span=max(x1-x0,y1-y0,1e-6);pad=span*.35
    return [x0-pad,y0-pad,x1+pad,y1+pad]

def render(raw,ps,truth,roles,bbox,mode,focus,axis=None):
    import resvg_py
    ns='http://www.w3.org/2000/svg';ET.register_namespace('',ns);root=ET.fromstring(raw)
    if any(a.rsplit('}',1)[-1]=='href' and v and not v.startswith('#') for e in root.iter() for a,v in e.attrib.items()):raise ValueError('External SVG resources are forbidden')
    root.set('style','background-color: #fff;')
    for node in root.iter():
        if 'stroke' in node.attrib and node.get('stroke')!='none':node.set('stroke','#626d79' if mode=='source' else '#c1c7ce')
        if 'fill' in node.attrib and node.get('fill')!='none':node.set('fill','#37434c' if mode=='source' else '#a1aab4')
    x0,y0,x1,y1=bbox;root.set('viewBox',f'{x0} {y0} {x1-x0} {y1-y0}');root.set('width','640');root.set('height','520');root.set('preserveAspectRatio','xMidYMid meet')
    colors={'door':'#00886d','window':'#146cc9','wall':'#e27616','ambiguous':'#af239b'}
    if mode!='source':
        g=ET.SubElement(root,'{'+ns+'}g',{'fill':'none','stroke-width':str(max(x1-x0,y1-y0)/260),'stroke-linecap':'round'})
        for p in ps:
            kind=truth[p.entity_id]['category'] if mode=='labels' else roles.get(p.entity_id)
            if kind not in colors or p.kind not in ('line','arc'):continue
            if p.kind=='line':d='M '+' L '.join(f'{x},{y}' for x,y in p.points)
            else:
                if not p.radius or not p.sweep_degrees:continue
                a,b=p.points;d=f'M {a[0]},{a[1]} A {p.radius},{p.radius} 0 {int(abs(p.sweep_degrees)>180)} {int(p.sweep_degrees>0)} {b[0]},{b[1]}'
            ET.SubElement(g,'{'+ns+'}path',{'d':d,'stroke':colors[kind]})
        if axis and mode=='assigned':
            a,b=axis['hinge'],axis['closed_tip'];ET.SubElement(g,'{'+ns+'}path',{'d':f'M {a[0]},{a[1]} L {b[0]},{b[1]}','stroke':'#d11ca3','stroke-dasharray':f'{(x1-x0)/70} {(x1-x0)/90}'})
    return resvg_py.svg_to_bytes(svg_string=ET.tostring(root,encoding='unicode'),width=640,height=520,background='white',skip_system_fonts=True,font_files=['/System/Library/Fonts/STHeiti Medium.ttc'],sans_serif_family='Heiti SC')

def run():
    rows=roster();samples=[json.loads((OUT/'samples'/(r['sample_id'].replace('/','__')+'.json')).read_text()) for r in rows]
    selected,pools=choices(samples);save(OUT/'case_selection.json',dict(policy='Two hash-order eligible cases per stratum, distinct families globally; selected before visualization. No metric ranking.',pool_sizes=pools,cases=selected))
    mapping={r['sample_id']:r for r in rows};samplemap={r['sample_id']:r for r in samples};extras=[pred_path(mapping[c['sample_id']],'v5_full') for c in selected]
    accesses=guard([mapping[c['sample_id']] for c in selected],extras)
    from PIL import Image,ImageDraw,ImageFont
    import matplotlib
    font=ImageFont.truetype(str(Path(matplotlib.get_data_path())/'fonts/ttf/DejaVuSans.ttf'),20)
    sheets=[];records=[]
    for c in selected:
        row=mapping[c['sample_id']];raw,(ps,t,di,gh)=read(row);pred=json.loads(pred_path(row,'v5_full').read_text());bbox=crop_box(c['bbox']);roles=pred['entity_roles'];axis=c.get('axis')
        panel=Image.new('RGB',(1920,590),'#ffffff');draw=ImageDraw.Draw(panel)
        for j,mode in enumerate(('source','labels','assigned')):
            im=Image.open(io.BytesIO(render(raw,ps,t,roles,bbox,mode,c['focus_ids'],axis))).convert('RGB');panel.paste(im,(j*640,65))
            draw.text((j*640+14,10),f'{c["case_id"]} {c["sample_id"]} | {mode}',font=font,fill='#142e44')
            draw.text((j*640+14,35),c['stratum'],font=font,fill='#476172')
        dest=OUT/'cases'/(c['case_id']+'.png');dest.parent.mkdir(exist_ok=True);panel.save(dest);sheets.append(panel)
        idsall={p.entity_id for p in ps};wallgt={e for e,v in t.items() if v['category']=='wall'};wallpred={e for e,v in roles.items() if v=='wall'}
        topology={name:linework(ps,ids,bbox) for name,ids in [('all_parsed_lines',idsall),('annotated_wall_oracle',wallgt),('predicted_wall',wallpred)]}
        record=c|dict(crop_bbox=bbox,source_sha256=row['sha256'],geometry_sha256=gh,image='cases/'+c['case_id']+'.png',linework_topology=topology,
            focus_sources=[dict(entity_id=p.entity_id,kind=p.kind,points=p.points,center=p.center,radius=p.radius,sweep_degrees=p.sweep_degrees,annotation=t[p.entity_id],assigned_role=roles.get(p.entity_id)) for p in ps if p.entity_id in c['focus_ids']],
            room_ground_truth_available=False,room_accuracy_evaluated=False,review_status='awaiting_assistant_visual_review')
        save(OUT/'cases'/(c['case_id']+'.json'),record);records.append(record)
    for start in range(0,len(sheets),3):
        page=Image.new('RGB',(1920,590*len(sheets[start:start+3])),'white')
        for j,im in enumerate(sheets[start:start+3]):page.paste(im,(0,j*590))
        page.save(OUT/f'contact_sheet_{start//3+1}.png')
    save(OUT/'case_index.json',dict(cases=records,actual_data_reads=sorted(set(accesses)),new_check_reads=0,note='Source SVG geometry retained with grayscale styling; separate annotation/prediction overlays. Original colors are not interpreted as semantics. No annotation corrections, recognition changes, physical units or room claims.'))
    print('Rendered',len(records),'analysis cases')
if __name__=='__main__':run()
