"""Exact local source-pair measurements; no object, layer or rule decisions."""
import math

VERSION = 'local-source-pair-v1'


def _point(p):
    if len(p) < 2 or not all(math.isfinite(v) for v in p):
        raise ValueError('NONFINITE_POINT')
    return list(p[:2])


def _intervals(start, end):
    sweep = (end-start) % 360
    if sweep <= 1e-10:
        raise ValueError('DEGENERATE_ARC')
    a = start % 360
    return [(a,min(a+sweep,360))] + ([(0,a+sweep-360)] if a+sweep>360 else [])


def measure_pair(a, b, basis, relative_tolerance=1e-9):
    """Records come from geometry/source after native instance transforms.

    Rejections are local diagnostics, not an instruction to abort analysis.
    Numerical tolerances are relative to primitive size, never WCS translation.
    """
    out = dict(status='unsupported', basis=basis, source_entity_ids=[a['entity_id'],b['entity_id']],
               measurements={}, diagnostics=[], global_minimum_proven=False,
               identity_status='candidate', surface_role='unconfirmed', unit='drawing_unit',
               algorithm_version=VERSION)
    try:
        if not math.isfinite(relative_tolerance) or relative_tolerance<=0:raise ValueError('INVALID_TOLERANCE')
        if a['entity_id']==b['entity_id']:
            raise ValueError('DISTINCT_SOURCES_REQUIRED')
        if a.get('layout_id') != b.get('layout_id'):
            raise ValueError('DIFFERENT_LAYOUTS')
        if any(r.get('status') != 'ready' for r in (a,b)):
            raise ValueError('SOURCE_GEOMETRY_UNAVAILABLE')
        na, nb = a['native'], b['native']
        if basis=='parallel_normal':
            if any(r['kind']!='LINE' for r in (a,b)):
                raise ValueError('NATIVE_LINES_REQUIRED')
            p,q,r,s = [_point(v) for v in (na['start'],na['end'],nb['start'],nb['end'])]
            if any(abs(v[2]-na['start'][2])>relative_tolerance for v in (na['end'],nb['start'],nb['end']) if len(v)>2):
                raise ValueError('DIFFERENT_PLANES')
            d=[q[i]-p[i] for i in (0,1)]; e=[s[i]-r[i] for i in (0,1)]
            length=math.hypot(*d); other=math.hypot(*e)
            if min(length,other)<=0: raise ValueError('DEGENERATE_LINE')
            u=[v/length for v in d]; n=[-u[1],u[0]]
            roundoff=8*max(math.ulp(v) for point in (p,q,r,s) for v in point)
            if abs(u[0]*e[1]-u[1]*e[0])>relative_tolerance*other+roundoff*(1+other/length):
                raise ValueError('NONPARALLEL_LINES')
            project=lambda v: sum((v[i]-p[i])*u[i] for i in (0,1))
            lo=max(0,min(project(r),project(s))); hi=min(length,max(project(r),project(s)))
            if hi-lo<=relative_tolerance*max(length,other): raise ValueError('NO_FINITE_OVERLAP')
            t=(lo+hi)/2
            x=[p[i]+t*u[i] for i in (0,1)]
            # Intersect the normal with the actual second finite line, rather than
            # projecting onto an infinite line and silently ignoring its extent.
            alpha=(t-project(r))/(project(s)-project(r))
            y=[r[i]+alpha*e[i] for i in (0,1)]
            width=math.dist(x,y)
            out.update(measurement_line=[x,y], domain=dict(kind='linear',origin=p,direction=u,
                interval=[lo,hi],overlap_length=hi-lo), error_bound=relative_tolerance*max(length,other)+roundoff,
                measurements=dict(width_drawing_units=width))
        elif basis=='concentric_radial':
            if any(r['kind']!='ARC' for r in (a,b)):
                raise ValueError('NATIVE_ARCS_REQUIRED')
            # ezdxf ARC parameters are OCS. A tilted/reflected OCS must first be
            # normalized by an appropriate exact kernel, never treated as XY.
            for native in (na,nb):
                if list(native.get('extrusion',[0,0,1])) != [0,0,1]:
                    raise ValueError('UNSUPPORTED_ARC_OCS')
            ca,cb = na['center'],nb['center']
            ra,rb = na['radius'],nb['radius']
            if not all(math.isfinite(v) for v in [*ca,*cb,ra,rb,na['start_angle'],na['end_angle'],nb['start_angle'],nb['end_angle']]):
                raise ValueError('NONFINITE_ARC')
            if min(ra,rb)<=0:raise ValueError('DEGENERATE_RADIUS')
            tol=relative_tolerance*max(ra,rb)
            center_error=math.dist(ca,cb)
            if center_error>tol:raise ValueError('NONCONCENTRIC_ARCS')
            overlaps=[]
            for x,y in _intervals(na['start_angle'],na['end_angle']):
                for u,v in _intervals(nb['start_angle'],nb['end_angle']):
                    if min(y,v)>max(x,u)+1e-10:overlaps.append([max(x,u),min(y,v)])
            if not overlaps:raise ValueError('NO_ANGULAR_OVERLAP')
            interval=max(overlaps,key=lambda z:z[1]-z[0]); angle=math.radians(sum(interval)/2)
            unit=[math.cos(angle),math.sin(angle)]
            x=[ca[i]+ra*unit[i] for i in (0,1)]; y=[cb[i]+rb*unit[i] for i in (0,1)]
            out.update(measurement_line=[x,y],domain=dict(kind='angular',center=list(ca),
                radius_values=[ra,rb],intervals_degrees=sorted(overlaps)),error_bound=center_error,
                measurements=dict(width_drawing_units=abs(rb-ra)),center_residual=center_error)
        else:raise ValueError('UNSUPPORTED_MEASUREMENT_BASIS')
        out['status']='local_pair_measured'
        out['diagnostics'].append(dict(code='IDENTITY_SURFACE_AND_OBSTACLES_UNCONFIRMED'))
    except (ValueError,KeyError,TypeError,IndexError) as ex:
        out['diagnostics'].append(dict(code=str(ex)))
    return out
