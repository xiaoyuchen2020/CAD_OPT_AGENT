"""Source-only separator oracle; never use candidate axes as the search space."""
from shapely.geometry import LineString
from shapely.ops import unary_union


def rails(bundle, bounds, axis):
    a, b, c, d = bounds
    lo, hi = (a, c) if axis == 0 else (b, d)
    low, high = (b, d) if axis == 0 else (a, c)
    tol = bundle['tolerance']; other = 1 - axis; groups = []
    for s in sorted(bundle['segments'], key=lambda s: s['a'][other]):
        if abs(s['a'][other] - s['b'][other]) > tol:
            continue
        at = (s['a'][other] + s['b'][other]) / 2
        if not low + tol < at < high - tol:
            continue
        if groups and abs(groups[-1][0] - at) <= tol:
            groups[-1][1].append(s)
        else:
            groups.append((at, [s]))
    result = []
    for at, segs in groups:
        pieces = []; ids = []
        for s in segs:
            left = max(lo, min(s['a'][axis], s['b'][axis]))
            right = min(hi, max(s['a'][axis], s['b'][axis]))
            if right > left:
                pieces.append(LineString([(left, 0), (right, 0)]))
                ids.append(s['segment_id'])
        gap = LineString([(lo, 0), (hi, 0)]).difference(unary_union(pieces)).length
        if gap <= tol:
            result.append(dict(axis=axis, coordinate=at, segment_ids=sorted(ids)))
    return result
