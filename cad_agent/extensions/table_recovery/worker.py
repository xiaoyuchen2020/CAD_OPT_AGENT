"""Image entry point, standard library only. This is a fixed, reviewed method.

No dynamic source/import/exec endpoint. Probes are maintenance-only operations,
never exposed by the application API.
"""
import json
import math
import os
import resource
import socket
import sys
from bisect import bisect_left


def recover(bundle):
    def unresolved(reason):
        return dict(input_id=bundle['input_id'], status='unresolved', reason=reason)
    if bundle['coverage']['blocking_diagnostics']:
        return unresolved('INPUT_INCOMPLETE_OR_UNSUPPORTED')
    if bundle['baseline'].get('code') == 'TABLE_GRAPHICS_DOMINATE':
        return unresolved('TABLE_IDENTITY_REQUIRES_REVIEW')
    xs, ys, tol = bundle['xs'], bundle['ys'], bundle['tolerance']
    n, m = len(ys)-1, len(xs)-1
    if n < 1 or m < 1 or n*m > 4000:
        return unresolved('ANALYSIS_LIMIT_EXCEEDED')
    rails={0:{y:[] for y in ys},1:{x:[] for x in xs}}
    for s in bundle['segments']:
        for axis,anchors in ((0,sorted(ys)),(1,xs)):
            other=1-axis
            pos=bisect_left(anchors,s['a'][other])
            for anchor in anchors[max(0,pos-1):pos+1]:
                if max(abs(s['a'][other]-anchor),abs(s['b'][other]-anchor))<=tol:
                    rails[axis][anchor].append(s)
    def supports(p, q):
        horizontal = p[1] == q[1]
        axis = 0 if horizontal else 1
        other = 1-axis
        lo, hi = sorted((p[axis], q[axis]))
        intervals = []
        for s in rails[axis][p[other]]:
            a, b = s['a'], s['b']
            if max(abs(a[other]-p[other]), abs(b[other]-p[other])) > tol:
                continue
            left, right = max(lo,min(a[axis],b[axis])), min(hi,max(a[axis],b[axis]))
            if right > left:
                intervals.append((left,right,s['segment_id']))
        end, gaps, covered = lo, [], 0.
        for left,right,_ in sorted(intervals):
            if left > end:
                gaps.append(left-end)
            covered += max(0., right-max(end,left))
            end = max(end,right)
        gaps.append(max(0.,hi-end))
        allowance = max(tol, (hi-lo)*bundle['max_gap_ratio'])
        supported = covered >= (hi-lo)-allowance and max(gaps) <= allowance
        return supported, covered/(hi-lo), sorted({s[2] for s in intervals})
    parent = list(range(n*m))
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for r in range(n):
        for c in range(m):
            for neighbor,p,q in (
                (r*m+c+1 if c+1<m else None,[xs[c+1],ys[r+1]],[xs[c+1],ys[r]]),
                ((r+1)*m+c if r+1<n else None,[xs[c],ys[r+1]],[xs[c+1],ys[r+1]])):
                if neighbor is None:
                    continue
                supported, fraction, _ = supports(p,q)
                if not supported:
                    if fraction > .05:
                        return unresolved('PARTIAL_SEPARATOR_AMBIGUOUS')
                    parent[root(neighbor)] = root(r*m+c)
    groups = {}
    for i in range(n*m):
        groups.setdefault(root(i), []).append(divmod(i,m))
    cells = []
    for coords in groups.values():
        r0,r1 = min(r for r,c in coords), max(r for r,c in coords)+1
        c0,c1 = min(c for r,c in coords), max(c for r,c in coords)+1
        if len(coords) != (r1-r0)*(c1-c0):
            return unresolved('NON_RECTANGULAR_CELL_COMPONENT')
        a,b,c,d = xs[c0],ys[r1],xs[c1],ys[r0]
        edges = [supports(p,q) for p,q in (([a,b],[c,b]),([c,b],[c,d]),([c,d],[a,d]),([a,d],[a,b]))]
        if not all(s[0] for s in edges):
            return unresolved('OUTER_EDGE_UNSUPPORTED')
        texts = [t['entity_id'] for t in bundle['texts'] if a<=t['origin'][0]<c and b<=t['origin'][1]<d]
        cells.append(dict(row_index=r0,column_index=c0,row_span=r1-r0,column_span=c1-c0,
                          text_entity_ids=texts,edge_segment_ids=[s[2] for s in edges]))
    return dict(input_id=bundle['input_id'],status='candidate',xs=xs,ys=ys,cells=cells)


def probe():
    result = dict(uid=os.getuid(), cpu_limit=list(resource.getrlimit(resource.RLIMIT_CPU)),
                  file_limit=list(resource.getrlimit(resource.RLIMIT_FSIZE)))
    for path,key in (('/app/forbidden','root_write_denied'),('/host-sentinel','host_file_unavailable')):
        try:
            with open(path, 'w' if key=='root_write_denied' else 'r') as f:
                f.write('x') if key=='root_write_denied' else f.read()
            result[key] = False
        except OSError:
            result[key] = True
    s=socket.socket();s.settimeout(.3)
    try:
        s.connect(('1.1.1.1',80));result['network_denied']=False
    except OSError:
        result['network_denied']=True
    finally:
        s.close()
    result['sensitive_env_absent'] = not any(k in os.environ for k in ('DEEPSEEK_API_KEY','AWS_SECRET_ACCESS_KEY','CAD_ODA_API_KEY'))
    return result


if __name__ == '__main__':
    request=json.load(sys.stdin)
    op=request.get('operation','recover')
    if op=='probe':
        output=probe()
    elif op=='timeout_probe':
        while True:
            math.sqrt(2)
    elif op=='output_probe':
        sys.stdout.write('x'*(3*1024*1024));sys.stdout.flush();sys.exit(0)
    elif op=='recover':
        output=recover(request['bundle'])
    else:
        raise ValueError('UNKNOWN_FIXED_OPERATION')
    json.dump(output,sys.stdout,ensure_ascii=False,allow_nan=False)
