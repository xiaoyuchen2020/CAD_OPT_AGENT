"""Reviewed composition tried before asking a model to write code."""
FULL_WIDTH_ROWS = '''def adapt(bundle):
    import copy
    from sdk import recover
    b = copy.deepcopy(bundle)
    a, low, c, high = b['bbox']
    tol = b['tolerance']
    groups = []
    for s in sorted(b['segments'], key=lambda s: s['a'][1]):
        if abs(s['a'][1]-s['b'][1]) > tol:
            continue
        y = (s['a'][1]+s['b'][1])/2
        if not low+tol < y < high-tol:
            continue
        if groups and abs(groups[-1][0]-y) <= tol:
            groups[-1][1].append(s)
        else:
            groups.append((y,[s]))
    ys = list(b['ys'])
    for y, segs in groups:
        end = a; missing = 0
        for left,right in sorted((max(a,min(s['a'][0],s['b'][0])), min(c,max(s['a'][0],s['b'][0]))) for s in segs):
            if right <= left:
                continue
            missing += max(0,left-end); end = max(end,right)
        missing += max(0,c-end)
        if missing <= tol and all(abs(y-v)>tol for v in ys):
            ys.append(y)
    b['ys'] = sorted(ys,reverse=True)
    return recover(b)
'''
