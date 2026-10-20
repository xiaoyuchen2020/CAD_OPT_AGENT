"""Explicit source view scopes; a sheet border never proves view equivalence."""
import re
from shapely.geometry import Polygon, box

from cad_agent.analysis.scope_workflow import contains


def title_key(text):
    """Only typography and a trailing printed scale are presentation differences."""
    text = re.sub(r'\s+', '', text).casefold()
    return re.sub(r'(?:比例)?[（(]?1[:：]\d+(?:\.\d+)?[）)]?$', '', text)


def view_kind(text):
    text = title_key(text)
    if len(text) > 80 or any(x in text for x in ('详见', '参见', '说明：', '。', '；')):
        return None
    if text.endswith('剖面图'):
        return 'section'
    if re.search(r'(?:详图|节点图|节点详图|大样(?:图)?[一二三四五六七八九十\d]*)(?:[（(][^）)]{0,20}[）)])?$', text):
        return 'detail'
    if text.endswith('平面图'):
        return 'plan'
    if text.endswith('立面图'):
        return 'elevation'
    return None


def diagnose_view_frame(index, frame, requested_view, requested_title, scene=None):
    """Validate an existing frame, never accept a caller-supplied extent.

    A native title must be inside the frame and attached to that same layout and
    frame. Every other view title inside the boundary is a conflict, including
    another view kind or a composite title. Repeated identical titles are anchors
    only; they do not establish that elevations share a datum or position.
    """
    scene = scene if scene is not None else index.scene
    bounds = frame['bbox']
    rows = [r for r in index.scope(frame['layout_id'], frame['frame_id'])
            if contains(bounds, r.bbox)]
    texts = {t['entity_id']: t for t in scene['texts']
             if t['layout_id'] == frame['layout_id']}
    spatial_rows = [r for r in index.scope(frame['layout_id'])
                    if contains(bounds, r.bbox) and view_kind(r.text)]
    def native_title(region):
        # CAD commonly stores the section number, title and scale as separate
        # TEXT entities. Require the complete ordered source text, never add a
        # prefix or infer a missing title from nearby labels.
        sources = [texts.get(eid) for eid in region.entity_ids]
        return (bool(sources) and all(t and contains(bounds,t['bbox']) for t in sources)
                and title_key(''.join(t['text'] for t in sources)) == title_key(region.text))
    native_titles = [r for r in spatial_rows if native_title(r)]
    key = title_key(requested_title)
    matching = [r for r in native_titles if view_kind(r.text) == requested_view
                and (not key or title_key(r.text) == key)]
    identities = {title_key(r.text) for r in matching}
    chosen_key = key or (next(iter(identities)) if len(identities) == 1 else '')
    titles = [r for r in matching if r.frame_id == frame['frame_id']
              and title_key(r.text) == chosen_key]
    others = [r for r in native_titles if title_key(r.text) != chosen_key]
    border_ids = []
    for line in scene['lines']:
        if line['layout_id'] != frame['layout_id']:
            continue
        pts = line['points']
        if len(pts) < 4 or pts[0] != pts[-1] or not line.get('entity_path'):
            continue
        bb = [min(p[0] for p in pts), min(p[1] for p in pts),
              max(p[0] for p in pts), max(p[1] for p in pts)]
        if not (bb[0] < bb[2] and bb[1] < bb[3]
                and all(abs(a-b) < 1e-5 for a, b in zip(bb, bounds))):
            continue
        polygon = Polygon([p[:2] for p in pts])
        if polygon.is_valid and polygon.equals(box(*bb)):
            border_ids.append(line['entity_id'])
    reasons = []
    if frame.get('method') != 'closed_border_and_title_candidate' or not border_ids:
        reasons.append('missing_native_closed_border')
    if requested_title and view_kind(requested_title) != requested_view:
        reasons.append('requested_title_view_mismatch')
    if not titles:
        reasons.append('missing_matching_native_title')
    if others:
        reasons.append('conflicting_view_titles')
    if any(r.frame_id != frame['frame_id'] for r in matching):
        reasons.append('title_frame_mismatch')
    return dict(
        frame_id=frame['frame_id'], eligible=not reasons, reasons=reasons,
        matching_title_count=len(titles), title_region_ids=[r.region_id for r in titles],
        conflicting_titles=[dict(region_id=r.region_id, text=r.text) for r in others],
        border_entity_ids=sorted(set(border_ids)), view_kind=requested_view,
        title_match='literal_title' if matching and requested_title else 'unfiltered',
        requested_title_found=bool(matching),
    ), rows, titles
