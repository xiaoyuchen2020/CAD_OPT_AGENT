"""Shared source heading grammar. Matches retain original character offsets."""
import re

FAMILIES = r'楼地面|内墙面|外墙面|内墙|外墙|顶棚|屋面|地面|墙面|踢脚|顶板|(?:架空|保温|地下室|室外|室内){0,3}楼板|雨篷|雨蓬|散水|台阶|坡道|墙裙'
FAMILY = re.compile(FAMILIES)
HEADING = re.compile(
    rf'^[ \t]*(?:(?:{FAMILIES})[ \t]*[:：][ \t]*)?'
    rf'({FAMILIES})[ \t]*(?P<code>[A-Za-z]?[0-9０-９]+[A-Za-z]?)[^\S\n]*'
    r'(?:[:：（(—–－-]|(?=\n|$))', re.MULTILINE)


def reading_columns(items, physical_key):
    """Split wide envelopes only when independent explicit heading anchors exist.

    These are reading streams, not proof of chapter/engineering ownership.
    Text remains verbatim; downstream boundaries and numbering are still checked.
    """
    headings = sorted((t for t in items if abs(t.rotation) < .1 and HEADING.match(t.text)),
                      key=lambda t: t.origin[0])
    anchors = []
    for t in headings:
        if not anchors or t.origin[0] - anchors[-1][0] > 12 * max(t.height, anchors[-1][1]):
            anchors.append((t.origin[0], t.height))
    if len(anchors) < 2:
        return [(physical_key, items)]
    cuts = [(a[0] + b[0]) / 2 for a, b in zip(anchors, anchors[1:])]
    groups = [[] for _ in anchors]
    for t in items:
        groups[sum(t.origin[0] >= x for x in cuts)].append(t)
    return [((physical_key, 'heading_stream', round(a[0], 3)), group)
            for a, group in zip(anchors, groups) if group]
