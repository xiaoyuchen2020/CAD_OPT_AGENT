"""Read native dimensions; never substitute text overrides for geometry."""
import math
import re
from itertools import islice

from shapely.geometry import Point, LineString, box

from cad_agent.geometry.source import native_parameters, UNIT_TO_M

TYPES = {0:"linear", 1:"aligned", 2:"angular", 3:"diameter", 4:"radius", 5:"angular_3point", 6:"ordinate"}


def read_dimensions(workspace, sid, layout_id, bounds, unit):
    from cad_agent.geometry.source import source_dimensions
    return source_dimensions(workspace,sid,layout_id,bounds,unit)


def read_dimensions_from_source(source, sid, layout_id, bounds, unit):
    """Worker-local source adapter; no workspace, Store or nested dispatch."""
    factor = UNIT_TO_M.get(unit)
    entries = []
    for eid, (e, path, lid, ext) in source["entities"].items():
        if lid != layout_id or e.dxftype() != "DIMENSION":
            continue
        if bounds and ext and not box(*bounds).intersects(box(*ext)):
            continue
        entries.append((eid,e,path,ext))
    items = []
    for eid,e,path,ext in entries[:5000]:
        native = native_parameters(e)
        kind = int(e.dxf.dimtype) & 15
        actual = native.get("actual_measurement")
        if isinstance(actual, float) and not math.isfinite(actual):
            actual = None
        override = e.override()
        lf = float(override.get("dimlfac", 1))
        text = e.dxf.get("text", "")
        issues = []
        points = [list(e.dxf.get(k)) for k in ("defpoint2", "defpoint3") if e.dxf.hasattr(k)]
        recomputed = None
        if kind in (0,1) and len(points)==2:
            a,b=points
            delta=[b[i]-a[i] for i in range(3)]
            if kind == 1:
                recomputed = math.sqrt(sum(v*v for v in delta))
            elif abs(delta[2]) < 1e-8 and tuple(e.dxf.get("extrusion", (0,0,1))) == (0,0,1):
                angle=math.radians(e.dxf.get("angle",0))
                recomputed=abs(delta[0]*math.cos(angle)+delta[1]*math.sin(angle))
        if recomputed is None:
            issues.append("independent_recompute_not_supported_for_this_dimension")
        linear = kind in (0,1,3,4)
        scaled = actual*lf if linear and isinstance(actual,(int,float)) and math.isfinite(lf) and lf>0 else None
        if linear and scaled is None:
            issues.append("style_scale_unresolved")
        explicit = text.strip() if text.strip() not in ("", "<>") else None
        numeric = float(explicit) if explicit and re.fullmatch(r"[+-]?\d+(?:\.\d+)?",explicit) else None
        conflict = numeric is not None and scaled is not None and not math.isclose(numeric,scaled,rel_tol=1e-6,abs_tol=1e-6)
        if conflict:
            issues.append("explicit_text_differs_from_scaled_measurement")
        if recomputed is not None and isinstance(actual,(int,float)) and not math.isclose(recomputed,actual,rel_tol=1e-7,abs_tol=1e-7):
            issues.append("native_and_recomputed_measurement_conflict")
            conflict = True
        rendered_texts=[]
        try:
            children=list(islice(e.virtual_entities(),257))
            if len(children)>256:issues.append("dimension_block_scan_truncated")
            for child in children[:256]:
                if child.dxftype() in ("TEXT","MTEXT"):
                    rendered_texts.append(child.plain_text())
        except (ValueError,TypeError,AttributeError,ZeroDivisionError):
            issues.append("dimension_display_text_unavailable")
        stored=e.dxf.get("actual_measurement",None)
        if stored is not None and not math.isfinite(stored):stored=None
        if factor is None:
            issues.append("unknown_units")
        if ext is None:
            issues.append("dimension_extent_unresolved")
        matches=[]
        # Endpoint proximity is evidence for a candidate association, never an
        # AutoCAD associative-dimension constraint or proof of wall identity.
        tolerance=.002/factor if factor else None
        if kind in (0,1) and tolerance is not None:
            for p in points:
                near=[]
                for i in source["tree"].query(Point(p[:2]).buffer(tolerance)):
                    target_id=source["ids"][int(i)]
                    target,_,target_lid,_=source["entities"][target_id]
                    if target_lid!=layout_id or target.dxftype()!="LINE":
                        continue
                    a,b=target.dxf.start,target.dxf.end
                    if abs(a.z-p[2])>tolerance or abs(b.z-p[2])>tolerance:
                        continue
                    if LineString([(a.x,a.y),(b.x,b.y)]).distance(Point(p[:2]))<=tolerance:
                        near.append(target_id)
                matches.append(dict(point=p,entity_ids=sorted(near)[:12],total=len(near),truncated=len(near)>12,
                                    state="candidate" if near else "unresolved"))
        items.append(dict(object_id=eid,kind="dimension",dimension_type=TYPES.get(kind,"unsupported"),
            layout_id=layout_id,entity_ids=[eid],entity_path=path,layer=e.dxf.get("layer","0"),bbox=ext,
            status="conflict" if conflict else "needs_review", text_override=text,
            text_state="suppressed" if text==" " else "explicit_override" if explicit else "automatic",
            actual_measurement=actual,measurement_unit="degrees" if kind in (2,5) else "drawing_units",
            actual_measurement_basis="ezdxf_get_measurement",stored_measurement=stored,
            rendered_texts=rendered_texts,rendered_text_basis="native_dimension_block_text_not_reformatted",
            style=dict(name=e.dxf.get("dimstyle","Standard"),dimlfac=lf if math.isfinite(lf) else None,
                       dimpost=override.get("dimpost",""),dimdec=override.get("dimdec",2),dimlunit=override.get("dimlunit",2)),
            expected_unformatted_text_value=scaled, geometry_recomputed=recomputed,
            geometry_length_m=recomputed*factor if recomputed is not None and factor else None,
            definition_points=points, dimension_line_point=list(e.dxf.defpoint), applied_unit=unit, target_candidates=matches,
            association_status="candidate_endpoint_proximity" if any(m["total"] for m in matches) else "unresolved",
            diagnostics=issues, note="标注文字、样式缩放值与几何值分别保留；实体邻近不证明被标注对象身份。未复现完整 CAD 文本格式和公差排版。"))
    return dict(dimensions=items, total_native_dimensions=len(entries), truncated=len(entries)>5000,
                diagnostics=[d for d in source["diagnostics"] if d.get("layout_id")==layout_id],
                source_diagnostics=[d for d in source["diagnostics"] if d.get("layout_id") in (None,layout_id)],
                coverage="native_dimensions_only; exploded_text_and_lines_not_reconstructed")
