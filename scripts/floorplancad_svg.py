"""FloorPlanCAD SVG adapter. Labels returned separately from typed geometry.

Raw SVG IDs use the pre-remap vocabulary documented in CADTransformer's
config/anno_config.py RemapDict: 1=wall, 3..8=doors, 9..11=windows.
No inferred physical unit: SVG coordinates remain arbitrary dataset units.
"""
from collections import Counter
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET

from cad_agent.geometry.vector_recognition import Primitive

SCHEMA_SOURCE='https://github.com/VITA-Group/CADTransformer/blob/main/config/anno_config.py'
NUMBER=r'[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?'
TOKEN=re.compile(r'[a-zA-Z]|'+NUMBER)
SHAPES={'path','line','polyline','polygon','circle','ellipse','rect'}


def category(raw):
    if raw == 1:return 'wall'
    if 3 <= raw <= 8:return 'door'
    if 9 <= raw <= 11:return 'window'
    return 'other'  # Curtain wall, opening symbol and railing are separate classes.


def matmul(a,b):
    return tuple(tuple(sum(a[i][k]*b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


IDENTITY=((1,0,0),(0,1,0),(0,0,1))


def transform_matrix(value):
    out=IDENTITY
    parts=list(re.finditer(r'([a-zA-Z]+)\s*\(([^)]*)\)',value))
    if re.sub(r'([a-zA-Z]+)\s*\(([^)]*)\)','',value).strip(' ,\t\n'):
        raise ValueError('unsupported transform')
    for match in parts:
        name=match[1];v=[float(s) for s in re.findall(NUMBER,match[2])]
        if name=='matrix' and len(v)==6:
            a,b,c,d,e,f=v;m=((a,c,e),(b,d,f),(0,0,1))
        elif name=='translate' and len(v) in (1,2):
            m=((1,0,v[0]),(0,1,v[1] if len(v)==2 else 0),(0,0,1))
        elif name=='scale' and len(v) in (1,2):
            m=((v[0],0,0),(0,v[-1],0),(0,0,1))
        elif name=='rotate' and len(v) in (1,3):
            ang=math.radians(v[0]);c=math.cos(ang);s=math.sin(ang)
            x,y=v[1:] if len(v)==3 else (0,0)
            m=((c,-s,x-c*x+s*y),(s,c,y-s*x-c*y),(0,0,1))
        else:raise ValueError('unsupported transform')
        out=matmul(out,m)
    return out


def point(m,p):
    return (m[0][0]*p[0]+m[0][1]*p[1]+m[0][2],m[1][0]*p[0]+m[1][1]*p[1]+m[1][2])


def path_geometry(value, m, eid):
    tokens=TOKEN.findall(value)
    if re.sub(TOKEN,'',value).strip(' ,\t\r\n'):
        raise ValueError('invalid path data')
    # Released files predominantly contain one M followed by one L or A.
    # Other commands are explicitly unsupported, never silently dropped.
    if len(tokens)==6 and tokens[0]=='M' and tokens[3]=='L':
        return Primitive(eid,'line',(point(m,(float(tokens[1]),float(tokens[2]))),
                                     point(m,(float(tokens[4]),float(tokens[5])))))
    if len(tokens)!=11 or tokens[0]!='M' or tokens[3]!='A':
        raise ValueError('unsupported path command sequence')
    x,y=map(float,tokens[1:3]);rx,ry,rotation,large,sweep,ex,ey=map(float,tokens[4:])
    rx,ry=abs(rx),abs(ry)
    if rx==0 or ry==0 or (x==ex and y==ey) or large not in (0,1) or sweep not in (0,1):
        raise ValueError('degenerate arc')
    phi=math.radians(rotation);c=math.cos(phi);s=math.sin(phi)
    xp=c*(x-ex)/2+s*(y-ey)/2;yp=-s*(x-ex)/2+c*(y-ey)/2
    scale=max(1,math.sqrt(xp*xp/(rx*rx)+yp*yp/(ry*ry)));rx*=scale;ry*=scale
    denom=rx*rx*yp*yp+ry*ry*xp*xp
    ratio=max(0,(rx*rx*ry*ry-denom)/denom)
    f=(-1 if large==sweep else 1)*math.sqrt(ratio)
    cxp=f*rx*yp/ry;cyp=-f*ry*xp/rx
    center=(c*cxp-s*cyp+(x+ex)/2,s*cxp+c*cyp+(y+ey)/2)
    u=((xp-cxp)/rx,(yp-cyp)/ry);v=((-xp-cxp)/rx,(-yp-cyp)/ry)
    delta=math.atan2(u[0]*v[1]-u[1]*v[0],u[0]*v[0]+u[1]*v[1])
    if not sweep and delta>0:delta-=2*math.pi
    if sweep and delta<0:delta+=2*math.pi
    sx=math.hypot(m[0][0],m[1][0]);sy=math.hypot(m[0][1],m[1][1])
    circular=(abs(rx-ry)<=max(rx,ry)*1e-6 and abs(sx-sy)<=max(sx,sy)*1e-6
              and abs(m[0][0]*m[0][1]+m[1][0]*m[1][1])<=max(sx*sy,1e-12)*1e-6)
    return Primitive(eid,'arc' if circular else 'elliptic_arc',
        (point(m,(x,y)),point(m,(ex,ey))), point(m,center),rx*sx if circular else None,
        math.degrees(delta))


def parse_svg(raw):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('DTD/entity definitions forbidden')
    root=ET.fromstring(raw);primitives=[];truth={};diagnostics=Counter();fingerprint=[];counter=0
    def visit(node,parent,blocked=False):
        nonlocal counter
        tag=node.tag.rsplit('}',1)[-1]
        try:m=matmul(parent,transform_matrix(node.get('transform','')))
        except ValueError:m=parent;blocked=True
        if tag in SHAPES:
            eid=f'p{counter}';counter+=1
            rawid=int(node.get('semantic-id','0'))
            truth[eid]=dict(raw_semantic_id=rawid,category=category(rawid),instance_id=node.get('instance-id','-1'))
            # Fingerprint excludes labels, colors, layer names and source IDs.
            geom={k:node.get(k) for k in ('d','x','y','x1','y1','x2','y2','cx','cy','r','rx','ry','width','height','points') if node.get(k) is not None}
            fingerprint.append((tag,geom,m))
            try:
                if blocked:raise ValueError('unsupported ancestor transform')
                if tag=='path':p=path_geometry(node.get('d',''),m,eid)
                elif tag=='line':p=Primitive(eid,'line',(point(m,(float(node.get('x1','0')),float(node.get('y1','0')))),point(m,(float(node.get('x2','0')),float(node.get('y2','0'))))))
                elif tag in ('circle','ellipse'):
                    # Not converted to line segments; curves remain non-linear distractors.
                    p=Primitive(eid,tag,(point(m,(float(node.get('cx','0')),float(node.get('cy','0')))),))
                else:raise ValueError('unsupported shape')
                if any(not math.isfinite(v) for pt in p.points for v in pt):raise ValueError('nonfinite geometry')
                primitives.append(p)
            except (ValueError,ZeroDivisionError) as exc:
                diagnostics[str(exc)]+=1
        for child in node:visit(child,m,blocked)
    visit(root,IDENTITY)
    # Order-independent exact geometry check across splits (not a near-duplicate proof).
    items=sorted(json.dumps(v,sort_keys=True,separators=(',',':')) for v in fingerprint)
    return primitives,truth,dict(diagnostics),hashlib.sha256('\n'.join(items).encode()).hexdigest()
