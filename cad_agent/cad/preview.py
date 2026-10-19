"""Versioned, precompressed display packets; semantic artifacts stay untouched."""
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
import gzip
import json
import math
import re
from uuid import uuid4

from cad_agent.cad.render import RENDER_VERSION, font_signature, union

PACK_VERSION = 'spatial-v2'


def spatial_batches(entries, limit=1600):
    """Split by median on the widest centroid axis, avoiding tiny grid files."""
    if len(entries)<=limit:
        yield entries
        return
    centers=[((i['bbox'][0]+i['bbox'][2])/2,(i['bbox'][1]+i['bbox'][3])/2) for _,i in entries]
    axis=max(range(2),key=lambda a:max(p[a] for p in centers)-min(p[a] for p in centers))
    ordered=sorted(entries,key=lambda pair:pair[1]['bbox'][axis]+pair[1]['bbox'][axis+2])
    middle=len(ordered)//2
    yield from spatial_batches(ordered[:middle],limit)
    yield from spatial_batches(ordered[middle:],limit)


def packet(folder, name, data):
    raw=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()
    for suffix,content in [('.json',raw),('.json.gz',gzip.compress(raw,compresslevel=3,mtime=0))]:
        target=folder/(name+suffix)
        temp=folder/(name+'.'+uuid4().hex+'.tmp')
        temp.write_bytes(content);temp.replace(target)


def revision(target):
    stat=target.stat()
    return sha256(repr((PACK_VERSION,RENDER_VERSION,stat.st_size,stat.st_mtime_ns,font_signature())).encode()).hexdigest()[:20]


def ensure_preview(workspace, sid):
    drawing=workspace.store.get('drawing',sid)
    target=workspace.store.artifact(sid,'render.json')
    with workspace.lock:
        if target.exists():
            rev=revision(target);folder=target.parent/'preview'/rev
            if (folder/'manifest.json').exists():
                return rev,folder
        render=workspace.rendering(sid)
        rev=revision(target);folder=target.parent/'preview'/rev
        folder.mkdir(parents=True,exist_ok=True)
        groups=defaultdict(list);bases=defaultdict(list)
        layouts={l['layout_id']:l for l in render['layouts']}
        counts=defaultdict(int)
        for item in render['items']:
            counts[item['layout_id']]+=1
        frames=[f for f in drawing['frames'] if '未确认图框' not in f['name']]
        for index,item in enumerate(render['items']):
            lid=item['layout_id']
            geometry=item['type']!='text'
            if geometry and counts[lid]<50000:
                bases[lid].append([index,item]);continue
            b=item['bbox'];x=(b[0]+b[2])/2;y=(b[1]+b[3])/2
            matches=[f for f in frames if f['layout_id']==lid and f['bbox'][0]<=x<=f['bbox'][2] and f['bbox'][1]<=y<=f['bbox'][3]]
            if matches and not geometry:
                frame=min(matches,key=lambda f:(f['bbox'][2]-f['bbox'][0])*(f['bbox'][3]-f['bbox'][1]))
                area=frame['frame_id']
            else:
                box=layouts[lid]['bbox'];w=max(box[2]-box[0],1);h=max(box[3]-box[1],1)
                area=f'cell-{int((x-box[0])/w*24)}-{int((y-box[1])/h*24)}'
            height=(max(b[2]-b[0],b[3]-b[1]) if geometry else
                    math.hypot(item['matrix'][2],item['matrix'][3]))
            # Points have no extent but remain visible at every scale.
            if item['type']=='point':
                height=1e30
            # Split differently sized text so a large heading does not force all
            # tiny text in its frame to load with the overview.
            band=math.floor(math.log2(max(height,1e-12)))
            if geometry:
                area='spatial'
                band=band//4
            groups[lid,area,band,'geometry' if geometry else 'text'].append([index,item])
        base_files={}
        for i,lid in enumerate(layouts):
            name=f'base-{i}';packet(folder,name,bases[lid]);base_files[lid]=name
        chunks=[]
        for key,entries in groups.items():
            # Bound each request without changing item order or geometry.
            batches=spatial_batches(entries) if key[3]=='geometry' else (entries[s:s+400] for s in range(0,len(entries),400))
            for batch in batches:
                name=f'{"geom" if key[3]=="geometry" else "text"}-{len(chunks)}'
                packet(folder,name,batch)
                chunks.append(dict(file=name,layout_id=key[0],area=key[1],count=len(batch),kind=key[3],
                    min_pixels=.8 if key[3]=='geometry' else 2,
                    bbox=union(item['bbox'] for _,item in batch),
                    max_height=max((1e30 if item['type']=='point' else max(item['bbox'][2]-item['bbox'][0],item['bbox'][3]-item['bbox'][1]))
                        if key[3]=='geometry' else math.hypot(item['matrix'][2],item['matrix'][3]) for _,item in batch)))
        manifest=dict(snapshot_id=sid,revision=rev,pack_version=PACK_VERSION,
            unit=drawing['unit'],frames=drawing['frames'],texts=[],lines=[],layouts=render['layouts'],
            render={k:v for k,v in render.items() if k not in ('items','font_sources')},
            total_items=len(render['items']),bases=base_files,chunks=chunks)
        # The manifest is the commit marker, written last.
        packet(folder,'manifest',manifest)
        return rev,folder


def preview_file(workspace,sid,rev,name):
    workspace.store.get('drawing',sid)
    if not re.fullmatch(r'[a-f0-9]{20}',rev) or not re.fullmatch(r'(base|text|geom)-\d+|meta',name):
        raise KeyError('Invalid preview packet')
    folder=workspace.store.artifact_dir(sid)/'preview'/rev
    if not (folder/'manifest.json').is_file():
        raise KeyError('Preview revision unavailable')
    if name=='meta' and not (folder/'meta.json').exists():
        with workspace.lock:
            if not (folder/'meta.json').exists():
                scene=json.loads(workspace.store.artifact(sid,'scene.json').read_text())
                packet(folder,'meta',[dict(entity_id=t['entity_id'],entity_path=t['entity_path']) for t in scene['texts']])
    path=folder/(name+'.json')
    if not path.is_file():
        raise KeyError('Preview packet unavailable')
    return path
