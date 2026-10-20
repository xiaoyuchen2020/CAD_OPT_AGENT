import {clientToView, visibleBounds} from './render-viewport.mjs';

// CAD world Y points up, while SVG viewBox Y points down. Respect SVG letterboxing.
export function selectionBox(view, rect, start, end) {
  if (Math.hypot(end[0] - start[0], end[1] - start[1]) < 5) return null;
  const a = clientToView(view, rect, ...start), b = clientToView(view, rect, ...end);
  const box = [Math.min(a[0], b[0]), 0 - Math.max(a[1], b[1]),
    Math.max(a[0], b[0]), 0 - Math.min(a[1], b[1])];
  return box.every(Number.isFinite) && box[2] > box[0] && box[3] > box[1] ? box : null;
}

export function similarityScope(view, rect, layoutId, frameId = '') {
  return {layout_id:layoutId, frame_id:frameId,
    ...(frameId ? {} : {search_bbox:visibleBounds(view, rect.width, rect.height).box})};
}

export function similarityPath(candidate) {
  const paths=candidate.highlight_paths;
  if (!Array.isArray(paths) || paths.reduce((n,p)=>n+(Array.isArray(p) ? p.length : 4097),0)>4096) return '';
  if (paths.some(p=>!Array.isArray(p) || p.length<2 ||
      p.some(v=>!Array.isArray(v) || v.length!==2 || !v.every(Number.isFinite)))) return '';
  return paths.map(p=>p.map(([x,y],i)=>`${i?'L':'M'}${x},${0-y}`).join(' ')).join(' ');
}
