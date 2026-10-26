"""Shared source heading grammar. Matches retain original character offsets."""
import re

from cad_agent.cad.source_vocabulary import FAMILIES, FAMILY, FACET

CODE = r'[A-Za-zＡ-Ｚａ-ｚ]?[0-9０-９]+[A-Za-zＡ-Ｚａ-ｚ]?'
HEADING = re.compile(
    rf'^[ \t]*(?!(?:同|详|见|参照|参见|基层|采用))(?!(?:[^\n0-9０-９]{{0,24}})(?:选用|型号|材料同|采用|用于|这是|此为|该为))(?:(?:{FAMILIES})[ \t]*[:：][ \t]*)?'
    rf'(?:[\u4e00-\u9fff]{{0,20}}?)({FAMILIES})[ \t]*[—–－-]?[ \t]*(?P<code>{CODE})[^\S\n]*'
    r'(?:[:：（(—–－-]|(?=\n|$))', re.MULTILINE)

CHAPTER = re.compile(r'^[ \t]*[一二三四五六七八九十百]+[、．.][ \t]*[^\n]{1,60}$', re.MULTILINE)


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


def source_titles(text):
    """Numbered identities, explicit chapter titles, and short standalone labels.

    Short labels are discovery candidates; downstream geometry and a closing
    boundary still have to establish ownership.
    """
    numbered=list(HEADING.finditer(text))
    chapters=[m for m in CHAPTER.finditer(text) if FAMILY.search(m[0]) or FACET.search(m[0])]
    plain=[]
    for m in re.finditer(r'^[ \t]*[^\n]{1,35}$',text,re.MULTILINE) if '\n' not in text.strip() else []:
        value=m[0].strip()
        if (not re.search(r'[0-9０-９、，,；;。:：()（）]|用于|采用|见|同|详|按|应|为|的|厚|基层',value)
                and (FAMILY.search(value) or FACET.search(value)) and re.fullmatch(r'[\u4e00-\u9fff \t]+',value)):
            plain.append(m)
    return sorted({m.start():m for m in plain+chapters+numbered}.values(),key=lambda m:m.start())
