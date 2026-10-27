"""Native-scene audit crops and source/result inventory for the corpus run."""
import argparse
from collections import Counter
import html
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/business-tables-corpus-20261026'
def read(path):return json.loads(path.read_text())
def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def intersects(a,b):return a[0]<=b[2] and a[2]>=b[0] and a[1]<=b[3] and a[3]>=b[1]


def crop(case_id,box,name,layout=None):
    folder=OUT/'cases'/case_id;r=read(folder/'result.json');sid=r['snapshot_id']
    scene=read(folder/'data/drawings'/sid/'scene.json')
    if layout is None:layout=r['drawing']['layouts'][0]['layout_id']
    x,y,z,w=box;pad=max(z-x,w-y)*.025;x-=pad;y-=pad;z+=pad;w+=pad
    scale=2200/max(z-x,w-y);width=(z-x)*scale;height=(w-y)*scale
    tx=lambda a:(a-x)*scale;ty=lambda b:(w-b)*scale
    body=[]
    for line in scene['lines']:
        if line['layout_id']!=layout or not intersects(line['bbox'],[x,y,z,w]):continue
        pts=' '.join(f'{tx(p[0]):.3f},{ty(p[1]):.3f}' for p in line['points'])
        body.append(f'<polyline points="{pts}" fill="none" stroke="#4c5965" stroke-width="0.7"/>')
    refs=[]
    for t in scene['texts']:
        if t['layout_id']!=layout or not intersects(t['bbox'],[x,y,z,w]):continue
        refs.append({k:t.get(k) for k in ('entity_id','text','bbox','origin','height','entity_path')})
        lines=t['text'].splitlines() or [''];size=max(.6,t['height']*scale)
        origin=t.get('origin',[t['bbox'][0],t['bbox'][1]])
        for i,text in enumerate(lines):
            body.append(f'<text x="{tx(origin[0]):.3f}" y="{ty(origin[1])+i*size*1.2:.3f}" font-size="{size:.3f}" fill="#17242d">{html.escape(text)}</text>')
    svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="{int(width)}" height="{int(height)}" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="white"/><g font-family="PingFang SC,Noto Sans CJK SC,sans-serif">'+''.join(body)+'</g></svg>'
    target=folder/'review';target.mkdir(exist_ok=True)
    (target/(name+'.svg')).write_text(svg)
    save(target/(name+'.json'),dict(type='native_scene_crop_not_extractor_output',layout_id=layout,bbox=box,source_refs=refs,
         limitation='Lightweight native text/line view; font layout approximated. Original DXF remains authoritative.'))
    import resvg_py
    (target/(name+'.png')).write_bytes(resvg_py.svg_to_bytes(svg_string=svg))
    print(target/(name+'.png'))


def inventory():
    overview=[]
    for folder in sorted((OUT/'cases').iterdir()):
        if not (folder/'result.json').exists():continue
        r=read(folder/'result.json');item=dict(case_id=folder.name,name=r['case']['name'],status=r['status'],methods=r['methods'],issues=r['issues'])
        if r.get('snapshot_id'):
            scene=read(folder/'data/drawings'/r['snapshot_id']/'scene.json')
            pattern=r'一览表|工程概况|项目特征|建筑概况|经济技术指标|技术经济指标|主要技术指标'
            hits=[{k:t.get(k) for k in ('entity_id','text','layout_id','bbox','origin','height','entity_path')}
                  for t in scene['texts'] if len(t['text'])<200 and re.search(pattern,re.sub(r'\s+','',t['text']))]
            save(folder/'review-signals.json',hits);item['review_signals']=hits
            tables=read(folder/'table-index.json') if (folder/'table-index.json').exists() else []
            ids={t['table_id'] for m in r['methods'].values() for t in m.get('tables',[])}
            item['candidate_raw_headers']=[dict(table_id=t['table_id'],bbox=t['bbox'],rows=t['row_count'],columns=t['column_count'],
                first_rows=[[c.get('text','')[:160] for c in row['cells'][:16]] for row in t['rows'][:5]]) for t in tables if t['table_id'] in ids]
        overview.append(item)
    save(OUT/'review-inventory.json',overview)
    print('Inventory',len(overview),'files')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--inventory',action='store_true');p.add_argument('--case');p.add_argument('--bbox',type=float,nargs=4);p.add_argument('--name',default='source-crop');p.add_argument('--layout')
    a=p.parse_args()
    if a.inventory:inventory()
    else:crop(a.case,a.bbox,a.name,a.layout)
