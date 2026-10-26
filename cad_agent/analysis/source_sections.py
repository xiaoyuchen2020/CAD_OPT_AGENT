"""Explicit chapter lineage in one parser reading column, with source evidence."""
from bisect import bisect_left
from cad_agent.cad.assembly_headings import CHAPTER, HEADING
from cad_agent.cad.source_vocabulary import identities, canonical, expand


def context(index, target, start=0):
    if getattr(target,'column_key',None) is None:return None
    def build():
        grouped={}
        for r in index.regions:
            key=(r.layout_id,r.frame_id,str(r.column_key))
            for m in CHAPTER.finditer(r.text):
                grouped.setdefault(key,[]).append(((-r.bbox[3],r.bbox[0],r.region_id,m.start()),r,m.start(),m.end()))
        for rows in grouped.values():rows.sort(key=lambda r:r[0])
        return grouped
    if hasattr(index,'w') and hasattr(index,'signature'):
        from cad_agent.cache import workspace_cache
        rows=workspace_cache(index.w,'_source_chapters',max_entries=4).get_or_build(index.signature,build)
    else:rows=build()
    candidates=rows.get((target.layout_id,target.frame_id,str(target.column_key)),[])
    order=(-target.bbox[3],target.bbox[0],target.region_id,start)
    at=bisect_left([r[0] for r in candidates],order)-1
    if at<0:return None
    _,r,a,b=candidates[at]
    # Any following chapter, including an unrelated one, ends the scope.
    families=identities(r.text[a:b])
    if not families:return None
    return dict(families=sorted(families),evidence=index.evidence(r,a,b),basis='explicit_preceding_chapter_same_column')


def narrowed_families(chapter, title):
    heading=HEADING.search(title)
    if not chapter or not heading:return []
    parent=canonical(heading[1])
    # A chapter can qualify a generic label, but never replace a conflicting
    # explicit child: 楼面 -> 楼地面1 is supported; 楼面 -> 地面1 is not.
    return sorted(set(chapter['families']) & {canonical(f) for f in expand(parent)})
