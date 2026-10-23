import {publishedCitations} from './evidence-selection.mjs';
import {spatialRows,spatialHighlightBounds} from './spatial-results.mjs';
import {RunInspector} from './run-inspector.mjs';
import {propertyRows} from './property-evidence.mjs';
import {selectionBox, similarityScope, similarityPath} from './similarity-selection.mjs';
import {PanBuffer} from "./pan-buffer.mjs";
import {renderHighlight} from "./highlight.mjs";
import {roomBatchPresentation,roomAreaText,combinedBatchHighlights,displayedBatches} from "./room-batch.mjs";
import {PreviewStream} from "./preview-stream.mjs";
import {componentPageNavigation} from "./component-pages.mjs";
import {SvgViewport, clientToView, panView, zoomView, wheelFactor} from "./render-viewport.mjs";
const $ = (id) => document.getElementById(id);
const S = {
  drawings: [],
  relatedDrawings: new Map(),
  drawing: null,
  scene: null,
  preview: null,
  facts: null,
  factSet: null,
  tab: "facts",
  filter: "all",
  selected: null,
  layout: null,
  view: null,
  fullView: null,
  limit: 100,
  loading: 0,
  generation: 0,
  listGeneration: 0,
  busy: false,
  chatBusy: false,
  parentRun: null,
  retrievalSupported: false,
  roomPick: false,
  similarityPick: false,
  similarityRequest: 0,
  measurement: null,
  adaptationPolicy: null,
};
const labels = {
  inner: "内墙",
  outer: "外墙",
  above_ground: "地上",
  below_ground: "地下",
};
const fieldLabels = {
  wall_role: "墙体类型",
  elevation_scope: "地上/地下",
  strength_grade: "强度等级",
  density_grade: "密度等级",
};
const NS = "http://www.w3.org/2000/svg";
let toastTimer;
function node(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function svg(tag, attrs) {
  const e = document.createElementNS(NS, tag);
  Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v));
  return e;
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), 6500);
}
async function api(path, options = {}) {
  const r = await fetch(path, options);
  let data;
  try {
    data = await r.json();
  } catch {
    throw Error("服务响应无法读取，请检查本地服务是否运行。");
  }
  if (!r.ok)
    throw Error(
      data.error?.message ||
        (typeof data.detail === "string"
          ? data.detail
          : "请求未通过参数校验。"),
    );
  return data;
}
const post = (path, data) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
function busy(on, text = "正在处理图纸…") {
  S.busy = on;
  $("taskbar").hidden = !on;
  $("task-message").textContent = text;
  ["upload-button", "demo-button", "empty-demo"].forEach((id) => {
    if ($(id)) $(id).disabled = on;
  });
  $("extract-button").disabled = on || !S.drawing;
}
function attempt(fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (e) {
      toast(e.message);
    }
  };
}
function option(value, label) {
  const e = node("option", null, label);
  e.value = value;
  return e;
}
function showDialog(title, children) {
  $("dialog-title").textContent = title;
  $("dialog-content").replaceChildren(...children);
  $("details-dialog").showModal();
}
async function refreshDrawings() {
  S.drawings = (await api("/api/drawings")).items;
  $("drawing-select").replaceChildren(
    ...(S.drawings.length
      ? S.drawings.map((d) => option(d.id, d.name + (d.reindexed_from ? " · 索引更新" : "")))
      : [option("", "尚未导入图纸")]),
  );
  if (S.drawing) $("drawing-select").value = S.drawing.id;
}
async function selectDrawing(id) {
  if (!id) return;
  runInspector.reset(id);
  const gen = ++S.generation;
  cancelSimilaritySelection();
  S.roomPick = false;
  S.measurement = null;
  $("room-measure-button").setAttribute("aria-pressed", "false");
  $("cad-canvas").classList.remove("room-pick");
  $("drawing-measurement").replaceChildren();
  S.preview?.dispose();
  const [drawing, scene] = await Promise.all([
    api(`/api/drawings/${id}`),
    api(`/api/drawings/${id}/preview`),
  ]);
  if (gen !== S.generation) return;
  S.drawing = drawing;
  $("reindex-drawing").disabled = false;
  S.scene = scene;
  const fetchPacket = async (url, options) => {
    const r = await fetch(url, {...options, cache: new URLSearchParams(location.search).has('loadPerf') ? 'no-store' : 'default'});
    if (!r.ok) throw Error("图纸分块加载失败，请重试。");
    return r.json();
  };
  const stream = new PreviewStream(scene, fetchPacket, () => {
    if (S.preview !== stream) return;
    if (viewportRenderer) viewportRenderer.query = null;
    if (S.view && !viewFrame) viewFrame = requestAnimationFrame(commitView);
  });
  S.preview = stream;
  scene.render.items = stream.items;
  S.factSet = drawing.latest_fact_set_id;
  S.facts = null;
  S.selected = null;
  S.limit = 100;
  S.filter = "all";
  S.tab = "facts";
  $("search").value = "";
  $("frame-select").replaceChildren(
    option("", "全部来源范围"),
    ...drawing.frames.map((f) => option(f.frame_id, f.name)),
  );
  $("layout-select").replaceChildren(
    ...scene.layouts.map((l) => option(l.layout_id, l.name)),
  );
  S.layout = (scene.layouts.find((l) => l.name === "Model") || scene.layouts[0])?.layout_id || null;
  $("layout-select").value = S.layout || "";
  $("drawing-select").value = id;
  $("drawing-name").textContent = drawing.name;
  $("version-pill").textContent = drawing.synthetic
    ? "合成示例"
    : drawing.sha256.slice(0, 7);
  $("unit-label").textContent = `坐标单位 ${drawing.unit}`;
  $("download-source").href = `/api/drawings/${id}/source`;
  $("download-source").hidden = false;
  $("download-source").title = `下载原始 ${drawing.source_format || "DXF"}`;
  $("download-source").setAttribute("aria-label", $("download-source").title);
  $("drawing-format").textContent = drawing.source_format || "DXF";
  $("download-dxf").href = `/api/drawings/${id}/dxf`;
  $("download-oda-json").href = `/api/drawings/${id}/oda-json`;
  $("download-dxf").hidden = !drawing.conversion;
  $("download-oda-json").hidden = !drawing.conversion;
  $("canvas-empty").hidden = !!scene.layouts.length;
  $("canvas-label").hidden = !scene.layouts.length;
  $("view-status").textContent = drawing.synthetic
    ? "合成示例 · 非工程图"
    : (drawing.conversion ? "DWG 经内网转换 · DXF 快照" : "原始 DXF 快照");
  $("source-note").textContent = scene.render
    ? (scene.render.complete ? "轻量预览 · 远景细节简化，放大恢复" : "部分绘制 · 请查看诊断")
    : "预览服务需要重启以启用新版绘制";
  clearSelection();
  clearChat(false);
  // Restoring a saved task is read-only; inference only follows an explicit click.
  const saved=await api(`/api/drawings/${id}/tasks`);
  if(gen!==S.generation)return;
  if(saved.items.length) {
    const history=node('div','message');history.append(node('strong',null,'已保存任务'));
    for(const task of saved.items) {
      const b=node('button','button',task.question);
      b.onclick=attempt(async()=>{
        const current=await api(`/api/tasks/${task.task_id}`);
        if(gen!==S.generation)return;
        $('chat-messages').replaceChildren(history);
        const panel=node('section','room-batch-panel');history.replaceChildren(panel);renderTask(panel,current,'恢复任务记录，不调用模型。');
        $('chat-messages').scrollTop=0;
        const context={task:current,panel};const restoredBatches=[];
        for(const ref of current.batch_refs) {
          const batch=await api(ref.evidence_url);if(gen!==S.generation)return;
          restoredBatches.push(batch);

        }
        const restored={room_batches:restoredBatches,highlight_action:current.highlight_action};
        for(const batch of displayedBatches(restored)) {
          const container=node('section','room-batch-panel');history.append(container);renderRoomBatch(container,batch,context);
        }
        renderSpatialResults(history,current);
        applyResultHighlights(restored);
        if(current.publication_status==='published')S.parentRun=current.run_ids?.at(-1) || null;
      });history.append(b);
    }
    $('chat-messages').append(history);
  }
  setTabUI();
  renderScene();
  await loadFacts();
  if (gen !== S.generation) return;
  $("extract-button").disabled = S.busy;
}
async function loadFacts() {
  if (!S.drawing) return;
  const gen = ++S.listGeneration;
  const snapshot = S.drawing.id;
  const params = new URLSearchParams({
    q: $("search").value,
    limit: String(S.limit),
    frame_id: $("frame-select").value,
  });
  let data;
  if (S.tab === "sources") {
    data = await api(`/api/drawings/${snapshot}/sources?${params}`);
  } else if (S.factSet) {
    if (S.filter === "inner" || S.filter === "outer")
      params.set("wall_role", S.filter);
    if (S.filter === "needs_review")
      params.set("review_status", "needs_review");
    data = await api(`/api/fact-sets/${S.factSet}?${params}`);
  } else {
    data = { items: [], total: 0 };
  }
  if (gen !== S.listGeneration || snapshot !== S.drawing?.id) return;
  if (S.tab === "facts") S.facts = data;
  renderList(data);
}
function attr(p, key) {
  const a = p.attributes[key];
  return a?.value;
}
function friendly(value) {
  return Array.isArray(value)
    ? value.map(friendly).join(" / ")
    : labels[value] || value || "";
}
function title(p) {
  return `${friendly(attr(p, "elevation_scope"))}${friendly(attr(p, "wall_role")) || "范围待核查"} · ${friendly(attr(p, "material"))}`;
}
function renderList(data) {
  $("fact-count").textContent = data.total;
  const list = $("fact-list");
  list.replaceChildren();
  if (!data.items.length) {
    const empty = node("div", "empty-state");
    empty.append(
      node("div", "empty-icon", "⌕"),
      node(
        "h3",
        null,
        S.factSet || S.tab === "sources" ? "没有匹配记录" : "尚未提取事实",
      ),
      node(
        "p",
        null,
        S.factSet
          ? "可调整筛选条件，或切换原文检索核查。"
          : "点击右下方“重新提取”，整理当前图纸做法。",
      ),
    );
    list.append(empty);
  }
  data.items.forEach((p, i) => {
    if (S.tab === "sources") {
      const b = node("button", "fact-card source-card");
      const head = node("div", "card-heading");
      head.append(
        node("span", null, p.kind === "table_row" ? "表格来源" : "文字来源"),
        node("span", null, `S${String(i + 1).padStart(2, "0")}`),
      );
      b.append(
        head,
        node("h3", null, p.text),
        node("div", "card-location", p.frame_name),
      );
      b.addEventListener("click", () => {
        clearSelection();
        showSource(p);
      });
      list.append(b);
      return;
    }
    const b = node(
      "button",
      "fact-card" +
        (S.selected?.practice_id === p.practice_id ? " active" : ""),
    );
    b.dataset.practice = p.practice_id;
    const head = node("div", "card-heading");
    head.append(
      node("span", "record-number", `F${String(i + 1).padStart(2, "0")}`),
      node(
        "span",
        "state-label" + (p.review_status === "needs_review" ? " review" : ""),
        p.review_status === "needs_review" ? "◦ 待核查" : "✓ 原文已绑定",
      ),
    );
    b.append(head, node("h3", null, title(p)));
    const tags = node("div", "tags");
    const kind = attr(p, "statement_kind");
    if (kind && kind !== "specification")
      tags.append(
        node(
          "span",
          "tag warning",
          {
            prohibition: "禁用条款",
            reference: "引用材料",
            material_mention: "材料提及",
          }[kind] || kind,
        ),
      );
    for (const k of ["density_grade", "strength_grade", "mortar_grade"]) {
      if (attr(p, k)) tags.append(node("span", "tag", friendly(attr(p, k))));
    }
    if (attr(p, "thickness_mm")?.length)
      tags.append(
        node("span", "tag", `${attr(p, "thickness_mm").join(" / ")} mm`),
      );
    if (attr(p, "special_uses")?.length)
      tags.append(node("span", "tag", attr(p, "special_uses").join(" · ")));
    if (p.missing_fields.length)
      tags.append(
        node(
          "span",
          "tag warning",
          `缺 ${p.missing_fields.map((x) => fieldLabels[x] || x).join("、")}`,
        ),
      );
    if (p.conflicts.length)
      tags.append(node("span", "tag warning", "范围/取值待核查"));
    b.append(tags);
    if (attr(p, "application_scope"))
      b.append(
        node(
          "p",
          "fact-scope",
          `适用范围：${friendly(attr(p, "application_scope"))}`,
        ),
      );
    if (attr(p, "role_parameters"))
      b.append(
        node(
          "p",
          "fact-scope",
          `分角色参数（待核查）：${Object.entries(attr(p, "role_parameters"))
            .map(
              ([role, fields]) =>
                `${friendly(role)}：${Object.entries(fields)
                  .map(
                    ([key, val]) =>
                      `${fieldLabels[key] || key} ${friendly(val)}`,
                  )
                  .join("，")}`,
            )
            .join("；")}`,
        ),
      );
    b.append(node("p", null, p.original_text));
    const location = node("div", "card-location");
    location.append(
      node("span", null, p.frame_name.slice(0, 20)),
      node("span", null, "定位原文 ↗"),
    );
    b.append(location);
    b.addEventListener(
      "click",
      attempt(() => selectPractice(p)),
    );
    list.append(b);
  });
  if (data.total > data.items.length && S.limit < 500) {
    const more = node(
      "button",
      "load-more",
      `加载更多 · 已显示 ${data.items.length}/${data.total}`,
    );
    more.onclick = attempt(async () => {
      S.limit = Math.min(500, S.limit + 100);
      await loadFacts();
    });
    list.append(more);
  } else if (data.total > 500)
    list.append(
      node(
        "p",
        "answer-note",
        `显示前 500 条，请缩小搜索范围（共 ${data.total} 条）。`,
      ),
    );
  if (S.factSet && S.tab === "facts") {
    const a = node("a", "export-link", "导出完整事实与证据 JSON ↗");
    a.href = `/api/fact-sets/${S.factSet}/export`;
    a.target = "_blank";
    a.rel = "noopener";
    list.append(a);
  }
  const coverage = data.coverage;
  $("coverage-label").textContent = coverage
    ? `${coverage.completeness === "partial" ? "部分信息待核查" : "限定范围已处理"} · ${coverage.source_units_seen} 个来源区域`
    : `${data.total} 个${S.tab === "sources" ? "来源区域" : "事实"}`;
  $("review-count").textContent = "";
}
let viewportRenderer = null;
const panBuffer = new PanBuffer($("cad-canvas"), () => { if (S.view) setView([...S.view]); });
let viewFrame = 0;
function renderScene() {
  panBuffer.reset();
  $("drawing-measurement").replaceChildren();
  viewportRenderer = null;
  const geometry = $("drawing-geometry"),
    texts = $("drawing-text");
  geometry.replaceChildren();
  texts.replaceChildren();
  $("drawing-highlight").replaceChildren();
  if (!S.scene || !S.layout) return;
  if (S.scene.render) {
    const render = S.scene.render;
    const items = S.preview ? S.preview.items : render.items.filter((item) => item.layout_id === S.layout);
    const content = svg("g", { transform: "scale(1 -1)", "data-render-version": render.version });
    const createNode = (item) => {
      let el;
      if (item.type === "text") {
        const [a, b, c, d, e, f] = item.matrix;
        el = svg("text", { transform: `matrix(${[a,b,-c,-d,e,f].join(" ")})`,
          fill: item.color, stroke: "none", "data-text": item.text,
          "font-family": 'Arial, "Noto Sans CJK SC", "PingFang SC", "Microsoft YaHei", sans-serif',
          "font-size": item.font_size, textLength: item.width, lengthAdjust: "spacingAndGlyphs",
          "xml:space": "preserve" });
        el.textContent = item.text;
      } else {
        el = svg("path", { d: item.d || `M${item.x} ${item.y} l0.000001 0`,
          fill: item.filled ? item.color : "none", "fill-rule": "evenodd",
          stroke: item.filled ? "none" : item.color, "stroke-width": item.stroke_width,
          "vector-effect": "non-scaling-stroke", "stroke-linecap": "round" });
      }
      el.setAttribute("data-handle", item.handle || "");
      el.setAttribute("data-kinds", item.kinds.join(" "));
      return el;
    };
    geometry.append(content);
    viewportRenderer = new SvgViewport(content, items, createNode, S.preview ? S.layout : null);
    fit();
    return;
  }
  for (const line of S.scene.lines.filter((l) => l.layout_id === S.layout)) {
    geometry.append(
      svg("polyline", {
        points: line.points.map(([x, y]) => `${x},${-y}`).join(" "),
        "vector-effect": "non-scaling-stroke",
      }),
    );
  }
  for (const t of S.scene.texts.filter((t) => t.layout_id === S.layout)) {
    const [x, y] = t.origin;
    const e = svg("text", {
      x,
      y: -y,
      "font-size": t.height,
      transform: `rotate(${-t.rotation} ${x} ${-y})`,
    });
    const lines = t.text.split("\n");
    lines.forEach((line, i) => {
      const span = svg("tspan", { x, dy: i ? t.height * 1.35 : 0 });
      span.textContent = line;
      e.append(span);
    });
    texts.append(e);
  }
  fit();
}
function boxView(box, pad = 0.08) {
  const [x, y, xx, yy] = box;
  const w = Math.max(xx - x, 1),
    h = Math.max(yy - y, 1);
  return [x - w * pad, -yy - h * pad, w * (1 + pad * 2), h * (1 + pad * 2)];
}
function commitView() {
  viewFrame = 0;
  if (!S.view) return;
  const v = S.view;
  const canvas = $("cad-canvas");
  // Read dimensions before mutating SVG, once per animation frame.
  const width = $("canvas-wrap").clientWidth, height = $("canvas-wrap").clientHeight;
  const display = panBuffer.update(v, S.fullView, width, height, !!viewportRenderer);
  S.preview?.setTarget(S.layout, display.view, display.width, display.height);
  viewportRenderer?.update(display.view, display.width, display.height);
  if (S.preview) {
    canvas.dataset.previewReady = String(S.preview.ready);
    $("source-note").textContent = S.preview.error ? "当前范围加载失败，请重试" : !S.preview.ready
      ? "正在载入当前范围…" : S.scene.render.complete ? "轻量预览 · 远景细节简化，放大恢复" : "部分绘制 · 请查看诊断";
    $("retry-preview").hidden = !S.preview.error;
  }
  const box = display.view.join(" ");
  if (canvas.getAttribute("viewBox") !== box) canvas.setAttribute("viewBox", box);
  $("zoom-label").textContent = `${Math.round(((S.fullView?.[2] || v[2]) / v[2]) * 100)}%`;
}
function setView(v) {
  if (!v || !v.every(Number.isFinite) || v[2] <= 0 || v[3] <= 0) return;
  S.view = v;
  if (!viewFrame) viewFrame = requestAnimationFrame(commitView);
}
function fit() {
  const l = S.scene?.layouts.find((l) => l.layout_id === S.layout);
  if (!l) return;
  S.fullView = boxView(l.bbox);
  setView([...S.fullView]);
}
function zoom(factor, center) {
  if (!S.view || !S.fullView) return;
  const v = zoomView(S.view, S.fullView, factor, center);
  if (v) setView(v);
}
function locate(source) {
  if (S.layout !== source.layout_id) {
    S.layout = source.layout_id;
    $("layout-select").value = S.layout;
    renderScene();
  }
  const highlight = renderHighlight($("drawing-highlight"), source);
  if (!highlight.bbox) return;
  const [x, y, xx, yy] = highlight.bbox;
  const full = S.scene.layouts.find((l) => l.layout_id === S.layout)?.bbox;
  if (full) {
    const targetWidth = Math.min(
      full[2] - full[0],
      Math.max((xx - x) * 1.5, (yy - y) * 6, 1),
    );
    const ratio =
      $("canvas-wrap").clientHeight / Math.max(1, $("canvas-wrap").clientWidth);
    const targetHeight = Math.max((yy - y) * 3, targetWidth * ratio);
    setView([
      (x + xx - targetWidth) / 2,
      -(y + yy + targetHeight) / 2,
      targetWidth,
      targetHeight,
    ]);
  }
}
let sourceDisplayEpoch = 0;
function showSource(source) {
  const epoch = ++sourceDisplayEpoch;
  $("evidence-drawer").hidden = false;
  const preview = $("region-preview-link");
  preview.href = `/api/drawings/${encodeURIComponent(S.drawing.snapshot_id)}/sources/${encodeURIComponent(source.region_id)}/preview.svg`;

  locate(source);
  $("evidence-quote").textContent = source.quote || source.text;
  $("evidence-precision").textContent =
    source.precision === "entity"
      ? "实体级估算"
      : source.kind === "table_row"
        ? "表格行级"
        : "区域级估算";
  if ($("drawing-highlight").dataset.highlightFallback)
    $("evidence-precision").textContent += " · 轮廓不可显示，使用范围框";
  const metadata = [
    `来源 ${source.region_id}`,
    `原始坐标 ${source.bbox.map((v) => v.toFixed(1)).join(", ")}`,
    `${source.entity_ids.length} 个原始实体`,
  ];
  if (source.text_span)
    metadata.push(`字符范围 [${source.text_span.join(", ")})`);
  $("evidence-meta").replaceChildren(...metadata.map((t) => node("span", null, t)));
  const measure = node("button", "text-button", "以此房间名称测量净面积 ↗");
  const sourceSnapshot = S.drawing.id;
  measure.onclick = attempt(() => sourceSnapshot === S.drawing?.id && runRoomMeasurement({region_id: source.region_id}));
  $("evidence-meta").append(measure);
  const drawer = $("evidence-drawer");
  drawer.dataset.regionId = source.region_id;
  const generation = S.generation;
  const appendPaths = (texts) => {
    if (epoch !== sourceDisplayEpoch || generation !== S.generation || drawer.hidden || drawer.dataset.regionId !== source.region_id) return;
    const entities = texts.filter(t => source.entity_ids.includes(t.entity_id));
    if (entities.length) $("evidence-meta").append(node("span", null,
      `实体路径 ${entities.slice(0,5).map(t=>t.entity_path.join(" / ")).join("；")}${entities.length>5?"…":""}`));
  };
  if (S.preview) S.preview.metadata().then(appendPaths).catch(() => {});
  else appendPaths(S.scene.texts);

}
async function selectPractice(p, eid) {
  const fid = S.factSet,
    snapshot = S.drawing.id;
  const evidence = await api(
    `/api/fact-sets/${fid}/evidence/${eid || p.evidence_ids[0]}`,
  );
  if (fid !== S.factSet || snapshot !== S.drawing?.id) return;
  S.selected = p;
  document
    .querySelectorAll(".fact-card")
    .forEach((e) =>
      e.classList.toggle("active", e.dataset.practice === p.practice_id),
    );
  $("selection-context").hidden = false;
  $("selection-text").textContent = `已选做法 · ${title(p)}`;
  showSource(evidence);
  if (p.evidence_ids.length > 1) {
    const b = node("button", "text-button", "查看关联上下文 ↗");
    b.onclick = attempt(async () => {
      const records = await Promise.all(
        p.evidence_ids.map((id) => api(`/api/fact-sets/${fid}/evidence/${id}`)),
      );
      showDialog(
        "做法与上下文证据",
        records.map((r) => {
          const div = node("div", "diagnostic");
          div.append(
            node("p", null, r.quote),
            node("code", null, r.evidence_id),
          );
          const go = node("button", "text-button", "定位此来源 ↗");
          go.onclick = () => {
            $("details-dialog").close();
            showSource(r);
          };
          div.append(go);
          return div;
        }),
      );
    });
    $("evidence-meta").append(b);
  }
}
function clearSelection() {
  S.selected = null;
  $("selection-context").hidden = true;
  $("evidence-drawer").hidden = true;
  $("drawing-highlight").replaceChildren();
  document
    .querySelectorAll(".fact-card.active")
    .forEach((e) => e.classList.remove("active"));
}
function setTabUI() {
  for (const tab of ["facts", "sources"]) {
    $(`${tab}-tab`).setAttribute("aria-selected", S.tab === tab);
    $(`${tab}-tab`).classList.toggle("selected", S.tab === tab);
  }
  document.querySelector(".filters").hidden = S.tab === "sources";
  document
    .querySelectorAll("[data-filter]")
    .forEach((b) =>
      b.classList.toggle("active", b.dataset.filter === S.filter),
    );
}
async function waitJob(job, snapshot) {
  while (job.status === "queued" || job.status === "running") {
    busy(true, job.stage);
    await new Promise((r) => setTimeout(r, 900));
    job = await api(`/api/jobs/${job.id}`);
  }
  if (job.status === "failed") throw Error(job.error?.message || "提取未完成");
  await refreshDrawings();
  if (S.drawing?.id === snapshot) {
    S.drawing = await api(`/api/drawings/${snapshot}`);
    S.factSet = job.result.fact_set_id;
    S.selected = null;
    clearSelection();
    await loadFacts();
  }
  toast(
    `已整理 ${job.result.practice_count} 条砌体做法${job.cached ? " · 使用同版本缓存" : ""}`,
  );
}
async function demo() {
  busy(true, "生成合成示例图纸…");
  try {
    const data = await post("/api/demo", {});
    await refreshDrawings();
    await selectDrawing(data.drawing.id);
    await waitJob(data.job, data.drawing.id);
  } finally {
    busy(false);
  }
}
async function extractCurrent() {
  if (!S.drawing || S.busy) return;
  const id = S.drawing.id;
  busy(true, "开始提取来源…");
  try {
    const frame = $("frame-select").value;
    const job = await post("/api/extractions", {
      snapshot_id: id,
      mode: $("extract-mode").value,
      frame_ids: frame ? [frame] : [],
    });
    await waitJob(job, id);
  } finally {
    busy(false);
  }
}
const welcome = $("chat-messages").innerHTML;
const runInspector = new RunInspector({
  workspace: document.querySelector('.workspace'), api,
  onError: toast,
  onOpen: run => {
    if(S.chatBusy||run.snapshot_id!==S.drawing?.id)return;
    const result=run.result,answer=node('div','message');
    answer.append(node('div','message-label','✳ 已保存的模型问答'),
      node('p',null,result?.answer||run.error?.message||run.stage),runDetailsButton(run.id));
    for(const {source:c,number} of publishedCitations(result||{})){const b=node('button','citation',`[${number}] ${c.quote}`);b.type='button';
      b.append(node('small',null,`${c.source_context?.frame_name ? c.source_context.frame_name+'（解析图框） · ' : ''}${c.label||''} · 定位原文 ↗`));
      b.onclick=attempt(async()=>{const source=await api(c.evidence_url);if(source.snapshot_id===S.drawing?.id)showSource(source);});answer.append(b);}
    if(result?.missing_information?.length)answer.append(node('p','answer-note',result.missing_information.join('；')));
    $('chat-messages').replaceChildren(node('div','message user',run.request.question),answer);
    $('chat-messages').scrollTop=$('chat-messages').scrollHeight;
    S.parentRun=run.status==='completed'?run.id:null;
  },
  onCancel: id => post(`/api/chat-runs/${encodeURIComponent(id)}/cancel`, {}),
  onEvidence: async source => {
    if(source.snapshot_id!==S.drawing?.id) {
      toast('此证据属于关联图纸，请先在图纸列表中打开该图纸。');return;
    }
    showSource(source);
  },
  onImage: async (sid,id) => {
    const record=await api(`/api/drawings/${encodeURIComponent(sid)}/images/${encodeURIComponent(id)}`);
    if(sid===S.drawing?.id)locate(record);
    const picture=node('img');picture.src=record.image_url;picture.alt='实际发送给模型的局部图像';picture.style.maxWidth='100%';
    showDialog('模型输入图像',[picture,node('p',null,'图像中的辅助编号与高亮不是原图标注。')]);
  },
});
function runDetailsButton(runId) {
  const b=node('button','ri-open-run','查看运行详情 ↗');b.type='button';b.onclick=()=>runInspector.open(runId);return b;
}
function clearChat(invalidate = true) {
  runInspector.close();
  S.parentRun = null;
  if (invalidate) S.generation++;
  $("chat-messages").innerHTML = welcome;
  bindSuggestions();
}
function bindSuggestions() {
  document
    .querySelectorAll("[data-question]")
    .forEach((b) => (b.onclick = attempt(() => ask(b.dataset.question))));
}
async function ask(question, resumeTask=null) {
  if (S.chatBusy) return;
  if (!S.drawing) {
    toast("请先打开示例或导入一张 DXF / DWG 图纸。");
    return;
  }
  question = question.trim();
  if (!question) return;
  S.chatBusy = true;
  const gen = S.generation;
  if ($("chat-messages").querySelector(".welcome"))
    $("chat-messages").replaceChildren();
  const user = node("div", "message user", question);
  const answer = node("div", "message");
  answer.append(
    node("div", "message-label", "✳ 图纸助手"),
    node("p", null, $("chat-mode").value === "agent" ? "正在启动模型工具问答…" : "正在查询已绑定的事实…"),
  );
  $("chat-messages").append(user, answer);
  $("question").value = "";
  $("send-button").disabled = true;
  $("chat-messages").scrollTop = $("chat-messages").scrollHeight;
  try {
    const mode = resumeTask ? "agent" : $("chat-mode").value;
    const payload = {
      snapshot_id: S.drawing.id,
      question,
      fact_set_id: S.factSet,
      selected_practice_id: S.selected?.practice_id || null,
      mode,
      runtime: $("agent-runtime").value,
      model_profile: $("model-profile").value,
      allow_fallback: $("runtime-fallback").checked,
      source_read_batching: $("source-read-batching").checked,
      parent_run_id: mode === "agent" ? S.parentRun : null,
    };
    if (mode==='agent' && S.adaptationPolicy?.snapshot_id===S.drawing.id) payload.adaptation_policy_id=S.adaptationPolicy.policy_id;
    payload.related_drawings=resumeTask?.related_drawings || (S.relatedDrawings.get(S.drawing.id)||[]);
    if(resumeTask) {payload.resume_task_id=resumeTask.task_id;payload.parent_run_id=null;payload.mode='agent';}
    if (S.retrievalSupported) payload.retrieval_mode = $("retrieval-mode").value;
    let result;
    let run;
    if (mode === "agent") {
      run = await post("/api/chat-runs", payload);
      await runInspector.open(run);
      S.activeChatRun = run.id;
      $("cancel-chat-button").hidden = false;
      $("cancel-chat-button").disabled = false;
      $("cancel-chat-button").textContent = "停止分析";
      while (["queued", "running"].includes(run.status)) {
        if (gen !== S.generation) return;
        answer.replaceChildren(node("div", "message-label", "✳ 模型 · 工具问答"),
          node("p", null, run.stage), runDetailsButton(run.id));
        await new Promise((resolve) => setTimeout(resolve, 900));
        run = await api(`/api/chat-runs/${run.id}`);
        runInspector.update(run);
      }
      if (gen !== S.generation) return;
      if (["failed", "cancelled"].includes(run.status)) {
        answer.replaceChildren(node("p", null, run.error?.message || "模型问答未完成"), runDetailsButton(run.id));
        answer.append(node("p", "answer-note", run.error?.code === "MODEL_TOOLS_UNSUPPORTED"
          ? "请切换支持工具调用的模型，或使用本地事实查询；更换 Harness 无法启用远端服务的工具能力。"
          : "可更换模型或运行时重试，或切换本地事实查询；本次未发布模型答案。"));
        answer.append(node("p", "answer-note", `所选模型：${run.model} · ${run.model_profile || "default"}`));
        if(run.task_id) {
          const task=await api(`/api/tasks/${run.task_id}`),panel=node('section','room-batch-panel');
          answer.append(panel);renderTask(panel,task,'回答未发布；已完成的计算结果保留在下方。');
          renderSpatialResults(answer,task);
          if(task.spatial_results?.length)applyResultHighlights(task);
          for(const ref of task.batch_refs || []) {
            const batch=await api(ref.evidence_url);
            if(gen!==S.generation)return;
            const output=node('section','room-batch-panel');answer.append(output);
            renderRoomBatch(output,batch,{task,panel});
          }
        }
        return;
      }
      result = run.result;
      S.parentRun = run.id;
      if (result.fact_set_id && result.fact_set_id !== S.factSet) {
        S.factSet = result.fact_set_id;
        await loadFacts();
        if (gen !== S.generation) return;
      }
    } else result = await post("/api/chat", payload);
    if (gen !== S.generation) return;
    answer.replaceChildren(
      node("div", "message-label", mode === "agent" ? "✳ 模型 · 工具问答" : "✳ 本地事实查询"),
      node("p", null, result.claims?.length ? result.claims.map(c=>c.display_text||c.text).join("\n\n") : result.answer),
    );
    if (run) answer.append(runDetailsButton(run.id));
    if (result.source_excerpts?.length) {
      const excerpts = node("section", "answer-note");
      excerpts.append(node("p", null, "历史已读摘录：未经过新版结论引用筛选；对象归属及适用范围尚待核查，不计为完成。"));
      for (const excerpt of result.source_excerpts) {
        excerpts.append(node("p", null, `“${excerpt.quote}” [${excerpt.citation_number}]`));
      }
      answer.append(excerpts);
    }
    if(result.task) {
      const taskPanel=node('section','task-panel');answer.append(taskPanel);
      renderTask(taskPanel,result.task);
      for(const batch of displayedBatches(result)) {
        const panel=node('section','room-batch-panel');answer.append(panel);
        renderRoomBatch(panel,batch,{task:result.task,panel:taskPanel});
      }
      renderSpatialResults(answer,result);
      applyResultHighlights(result);
    } else if (result.room_batch) {
      const panel=node("section","room-batch-panel"); answer.append(panel);
      renderRoomBatch(panel,result.room_batch);
    }
    const audit=node("details","answer-audit");audit.append(node("summary",null,"依据与运行详情"));answer.append(audit);
    for (const {source: c, number} of publishedCitations(result)) {
      const b = node("button", "citation", `[${number}] ${c.quote}`);
      b.append(node("small", null, `${c.source_context?.frame_name ? c.source_context.frame_name + '（解析图框） · ' : ''}${c.label} · 定位原文 ↗`));
      b.onclick = attempt(async () => {
        if (c.evidence_url) {
          const source = await api(c.evidence_url);
          if (source.snapshot_id === S.drawing?.id) showSource(source);
          return;
        }
        let p = S.facts?.items.find((p) => p.practice_id === c.practice_id);
        if (!p) {
          const all = await api(`/api/fact-sets/${S.factSet}/export`);
          p = all.practices.find((p) => p.practice_id === c.practice_id);
        }
        if (p) await selectPractice(p, c.evidence_id);
      });
      audit.append(b);
    }
    for (const observation of result.tool_observations || []) {
      const details=node("details","answer-note");
      details.append(node("summary",null,"查看工具观测依据（非原图原文）"));
      details.append(node("pre",null,JSON.stringify(observation.data,null,2)));
      if (observation.data.image_id) {
        const sid = observation.snapshot_id;
        const view = node("button", "button", "查看模型所见局部图并定位原图");
        view.onclick = attempt(async () => {
          const record = await api(`/api/drawings/${sid}/images/${observation.data.image_id}`);
          if (sid !== S.drawing?.id) return;
          locate(record);
          const picture = node("img"); picture.src = record.image_url;
          picture.alt = "模型输入的局部图像，蓝色编号为辅助叠加";
          picture.style.maxWidth = "100%";
          details.append(picture); view.disabled = true;
        });
        details.append(view);
      }
      if (observation.data.image_ids?.length) {
        const view=node('button','button','查看模型所见的房间局部图');
        view.onclick=attempt(async()=>{
          for(const id of observation.data.image_ids.slice(0,2)) {
            const record=await api(`/api/drawings/${observation.snapshot_id}/images/${id}`);
            const picture=node('img');picture.src=record.image_url;picture.alt='局部原图与候选轮廓叠加；红虚线为开口假设';picture.style.maxWidth='100%';details.append(picture);
          }
          view.disabled=true;
        });details.append(view);
      }
      if (observation.data.measurement_id) {
        const sid = observation.snapshot_id;
        const view = node("button", "button", "查看测量边界与依据");
        view.onclick = attempt(async () => {
          const record = await api(`/api/drawings/${sid}/measurements/${observation.data.measurement_id}`);
          if (sid === S.drawing?.id) showMeasurement(record);
        });
        details.append(view);
      }
      audit.append(details);
    }
    if (result.note) audit.append(node("p", "answer-note", result.note));
    if(result.missing_information?.length) {
      const gaps=node("details","answer-note");gaps.append(node("summary",null,"未解决项"));
      for(const missing of result.missing_information)gaps.append(node("p",null,missing));
      answer.append(gaps);
    }
    if (result.model) audit.append(node("p", "answer-note", `实际模型：${result.model} · ${result.model_profile}`));
    if (["codex_subscription", "siwc_subscription"].includes(result.billing)) audit.append(node("p", "answer-note", result.usage_note));
    if (result.cache_usage) {
      const c=result.cache_usage;
      const ratio=c.cached_input_ratio == null ? "未知" : `${(100*c.cached_input_ratio).toFixed(1)}%`;
      const cached=c.reported_requests ? `${c.cached_input_tokens} tokens` : "未知";
      audit.append(node("p", "answer-note", `实际缓存输入：${cached} · 已报告请求中的命中占比 ${ratio} · ${c.missing_cache_usage_requests} 次请求未报告缓存用量`));
    }
    if (result.budget_policy?.version === "adaptive_v1") {
      const b=result.budget_policy;
      audit.append(node("p", "answer-note", `${b.name === "deep" ? "深度" : "标准"}预算 · 累计 ${b.total_ceiling_tokens.toLocaleString()} tokens · ${b.elapsed_ceiling_seconds / 60} 分钟 · 成稿预留 ${b.reserved_finalization_tokens.toLocaleString()} tokens`));
    }
    if (result.retrieval_mode) {
      const records = result.retrieval_usage || [];
      const fallback = records.some(x => x.fallback_reason === "embedding_unavailable");
      const embedTokens = records.reduce((n,x) => n + (x.indexing?.input_tokens || 0) + (x.query_embedding?.input_tokens || 0), 0);
      audit.append(node("p", "answer-note", `检索：${result.retrieval_mode === "hybrid" ? "混合检索" : "关键词检索"}${fallback ? " · embedding 不可用，已回退关键词" : ""} · 本地 embedding ${embedTokens} tokens（与聊天 Token 分开统计）`));
    }
    if (result.runtime_used) {
      const labels = {native: "自定义 · API", deepseek_harness: "DeepSeek · 桥接", codex: "Codex · 桥接", codex_subscription: "Codex · 订阅直连", responses_subscription: "自定义 · 订阅直连"};
      audit.append(node("p", "answer-note", `实际运行时：${labels[result.runtime_used]}${result.fallback_used ? "（已从所选运行时回退）" : " · 未回退"}`));
    }
    if (result.trace) {
      audit.append(renderTrace(result.trace));
      const total = (result.usage || []).reduce((sum, u) => sum + (u.total_tokens || 0), 0);
      audit.append(node("p", "answer-note", `${result.elapsed_seconds} 秒 · ${result.trace.length} 次工具调用 · ${total} tokens`));
      const exportLink = node("a", "export-link", "查看问答与证据记录 ↗");
      exportLink.href = `/api/chat-runs/${run.id}`;
      exportLink.target = "_blank";
      exportLink.rel = "noopener";
      audit.append(exportLink);
      if (result.metrics?.phases) {
        const phases = node("details");
        phases.append(node("summary", null, "分阶段耗时"));
        const names = {model_wait:"模型等待", answer_validation:"答案校验", runtime_total:"运行时总计", fallback_runtime_total:"回退运行时"};
        for (const [key, value] of Object.entries(result.metrics.phases)) {
          phases.append(node("p", "answer-note", `${names[key] || key.replace("tool.", "工具 · ")}：${value.seconds.toFixed(3)} 秒 · ${value.calls} 次${value.failures ? ` · ${value.failures} 次失败` : ""}`));
        }
        phases.append(node("p", "answer-note", "各阶段可能嵌套，不能直接相加。"));
        audit.append(phases);
      }
    }
    if (result.partial)
      answer.append(
        node("p", "answer-note", "仅展示前 12 条，完整记录见事实清单。"),
      );
  } catch (e) {
    if (gen === S.generation) answer.replaceChildren(node("p", null, e.message));
  } finally {
    S.chatBusy = false;
    S.activeChatRun = null;
    $("cancel-chat-button").hidden = true;
    $("send-button").disabled = false;
  }
}
function renderTrace(trace = []) {
  const list = node("div", "tool-trace");
  const names = {plan_task:"拆分任务", finish_analysis:"校验并交付答案", request_toolset:"为明确缺口补充工具", analyze_room_batch:"批量读取房间候选", get_drawing_overview: "读取图纸概览", search_sources: "检索原文", read_source_context: "读取条款与上下文",
    get_drawing_context: "读取图框与子图上下文", read_dimensions: "读取尺寸标注", query_building_relations: "查询墙与门洞关系", get_component_evidence: "读取公共组件证据",
    discover_rooms: "识别通用房间候选", measure_room: "测量房间净面积", read_geometry: "读取计算几何", query_spatial: "空间查询", measure_geometry: "几何测量", get_measurement_evidence: "读取测量依据", render_region: "读取局部图像", bind_visual_objects: "绑定图像候选对象",
    query_annotations: "读取轴网／索引／标高", locate_by_axes: "轴网定位", trace_detail: "追踪详图", read_table_semantics: "读取表格语义",
    read_table: "读取表格", query_entities: "查询图元", inspect_region: "查看局部区域",
    extract_facts: "整理图纸事实", query_facts: "查询事实", get_evidence: "核对原图证据"};
  for (const step of trace) list.append(node("p", "answer-note",
    `${step.status === "completed" ? "✓" : step.status === "failed" ? "!" : "…"} ${names[step.tool] || "工具请求"}${step.result_summary?.total != null ? ` · ${step.result_summary.total} 条匹配` : ""}`));
  return list;
}
async function browseComponents(kind, view=null, offset=0, scope=null, history=[]) {
  if(!S.drawing||!S.layout)return;
  const sid=S.drawing.id,gen=S.generation;
  const views={context:[["subframes","子图"],["frames","图框与图签"]],dimensions:[["dimensions","尺寸标注"]],relations:[["rooms","房间候选"],["walls","墙面候选"],["openings","门窗洞口"],["relations","关联"]]};
  view ||= views[kind][0][0];
  if(!scope){
    scope={layout_id:S.layout,frame_id:$("frame-select").value};
    if(kind==="relations"){
      if(!S.view)return;
      const [x,y,w,h]=S.view;scope.bbox=[x,-y-h,x+w,-y];
      if(S.drawing.unit==="unknown"){
        const select=node("select",null);select.setAttribute("aria-label","关系识别使用的图纸单位");
        select.append(option("","请选择已核实的单位"),...[["mm","毫米"],["cm","厘米"],["m","米"],["in","英寸"],["ft","英尺"]].map(([v,t])=>option(v,t)));
        const run=node("button","button","读取候选关系");run.disabled=true;select.onchange=()=>{run.disabled=!select.value;};
        run.onclick=attempt(()=>{if(gen===S.generation)return browseComponents(kind,view,0,{...scope,unit_override:select.value});});
        showDialog("关系识别单位",[node("p",null,"按当前视野读取；未知单位需明确指定，指定依据将保留在结果中。"),select,run]);return;
      }
    }
  }
  toast("正在读取图纸公共组件…");
  const args={...scope,offset,limit:8};if(kind!=="dimensions")args.view=view;
  const data=await post(`/api/drawings/${sid}/components/${kind}`,args);
  if(gen!==S.generation)return;
  const tabs=node("div","tabs");
  for(const [v,label] of views[kind]){const b=node("button",v===view?"selected":"",label);b.onclick=attempt(()=>browseComponents(kind,v,0,scope));tabs.append(b);}
  const notes={context:"标题、图框范围和比例归属均为候选，请结合原图复核。",dimensions:"显示文字、解析值和几何复算分别保留；有冲突时不能直接采用尺寸。",relations:"房间、墙面和关联均为候选；洞口也可能属于窗或设备，不能直接当作门。"};
  const content=[tabs,node("p","answer-note",`本视图 ${data.total} 项。${notes[kind]}`)];
  for(const item of data.items){
    const card=node("div","fact-card");
    const kindNames={room_candidate:"房间候选",wall_face_candidate:"墙面候选",opening_candidate:"洞口候选",bounded_by:"房间与边界墙面",opens_to_candidate_space:"洞口与空间",adjacent_to_candidate_space:"洞口与相邻空间",door_connects_candidate_spaces:"门连接两个空间（候选）",opening_in_candidate_wall:"洞口与墙面",border_candidate:"闭合边框候选",frame:"图框候选"};
    const label=item.title||item.name||(item.kind==="dimension"?`标注 ${item.rendered_texts?.join(" / ")||item.text_override||"自动文字"} · 解析值 ${item.actual_measurement??"未知"} · ${item.status==="conflict"?"存在冲突":"待核查"}`:kindNames[item.kind]||"待核查对象");
    card.append(node("strong",null,label));
    if(item.width_m!=null)card.append(node("p",null,`候选洞口宽 ${item.width_m} m · 关联 ${item.room_ids.length} 个空间候选`));
    if(item.opening_type){
      const types={door_candidate:"门候选",window_candidate:"窗候选",conflict:"门窗证据冲突",unknown:"门窗类型未定"};
      card.append(node("p",null,`${types[item.opening_type]||item.opening_type} · ${item.passage_status==="supported_door_between_two_spaces"?"门符号支持，已关联两侧空间候选":"尚未建立双侧通行关系"}`));
      const operations={single_swing:"单扇平开符号",double_swing:"双扇平开符号",sliding:"推拉面板符号",ambiguous_swing:"单扇与双扇符号并存",conflict:"开启方式证据冲突",unknown:"开启方式未定"};
      if(item.operation_type)card.append(node("p","answer-note",operations[item.operation_type]||item.operation_type));
      for(const side of item.sides||[])card.append(node("p","answer-note",`第 ${side.side+1} 侧：${side.status==="unique"?"唯一空间候选":side.status==="ambiguous"?"多个空间冲突":"空间未确定"}`));
    }
    if(item.text_regions?.length)card.append(node("p","answer-note",`区域内文字：${item.text_regions.map(t=>t.text).join(" / ")}（用途待确认）`));
    const bounds=item.bbox||item.title_bbox||(item.points?[
      Math.min(...item.points.map(p=>p[0])),Math.min(...item.points.map(p=>p[1])),
      Math.max(...item.points.map(p=>p[0])),Math.max(...item.points.map(p=>p[1]))]:null);
    if(bounds){const b=node("button","text-button","定位原图");b.onclick=()=>{if(gen!==S.generation)return;$("details-dialog").close();locate({layout_id:data.layout_id,bbox:bounds,boundary:item.boundary});};card.append(b);}
    const details=node("details",null);details.append(node("summary",null,"查看来源与候选依据"),node("pre",null,JSON.stringify(item,null,2)));card.append(details);content.push(card);
  }
  if(!data.items.length)content.push(node("p",null,"当前范围没有可返回的结果。空结果不证明图纸中不存在这些对象。"));
  for(const reason of data.diagnostics||[])content.push(node("p","answer-note",typeof reason==="string"?reason:JSON.stringify(reason)));
  const navigation=componentPageNavigation(data,offset,history);
  for(const [direction,label] of [["previous","上一页"],["next","下一页"]]){
    const page=navigation[direction];if(!page)continue;
    const b=node("button","button",label);b.onclick=attempt(()=>browseComponents(kind,view,page.offset,scope,page.history));content.push(b);
  }
  const link=node("a","button","下载完整来源证据");link.href=data.evidence_url;link.download=`${data.record_id}.json`;content.push(link);
  showDialog("图纸公共组件",content);
}
async function browseStructure(view = "sections", offset = 0) {
  if (!S.drawing) return toast("请先选择图纸。");
  const sid = S.drawing.id, gen = S.generation;
  const data = await api(`/api/drawings/${sid}/structure?view=${view}&offset=${offset}`);
  if (gen !== S.generation) return;
  const tabs = node("div", "tabs");
  for (const [v, label] of [["sections", "章节"], ["tables", "表格"], ["frames", "图框"], ["layouts", "布局"]]) {
    const b = node("button", v === view ? "selected" : "", label);
    b.onclick = attempt(() => browseStructure(v)); tabs.append(b);
  }
  const items = [tabs, node("p", "answer-note", data.note)];
  const annotations=node("button","button","轴网、索引与楼层");
  annotations.onclick=attempt(()=>browseAnnotations());items.push(annotations);
  for (const [kind,label] of [["context","图框与子图上下文"],["dimensions","读取尺寸标注"],["relations","当前视野墙与门洞关系"]]) {
    const b=node("button","button",label);b.onclick=attempt(()=>browseComponents(kind));items.push(b);
  }
  for (const item of data.items) {
    const label = item.title_candidate || item.preview || item.name || "未命名候选";
    const card = node("button", "fact-card", label);
    if (view === "tables") {
      card.append(node("p", "answer-note", `${item.row_count} 行 · ${item.column_count} 列 · 候选表格`));
      card.onclick = attempt(() => browseTable(sid, item.table_id));
    } else if (item.evidence) {
      card.onclick = () => { $("details-dialog").close(); showSource(item.evidence); };
    } else {
      card.onclick = () => {
        $("details-dialog").close(); S.layout = item.layout_id;
        $("layout-select").value = S.layout; renderScene(); setView(boxView(item.bbox));
      };
    }
    items.push(card);
  }
  if (!data.items.length) items.push(node("p", null, "当前解析未识别到这一类结构。可继续使用原文检索。"));
  if (offset) { const b=node("button","button","上一页"); b.onclick=attempt(()=>browseStructure(view,Math.max(0,offset-8)));items.push(b); }
  if (data.has_more) { const b=node("button","button","下一页"); b.onclick=attempt(()=>browseStructure(view,offset+8));items.push(b); }
  showDialog("浏览图纸结构", items);
}
async function browseTable(sid, tableId, offset = 0) {
  const gen = S.generation;
  const data = await api(`/api/drawings/${sid}/tables/${tableId}?row_offset=${offset}&row_limit=3`);
  if (gen !== S.generation) return;
  const items = [node("p", "answer-note", data.note)];
  const semantics=node("button","button","查看表头、单位与合并关系");
  semantics.onclick=attempt(()=>browseSemanticTable(sid,tableId));items.push(semantics);
  const adapt=node("button","button","尝试受控方法适配");
  adapt.onclick=attempt(()=>browseAdaptation(sid,tableId));items.push(adapt);
  for (const row of data.items) {
    const group=node("div","table-preview-row");
    group.append(node("strong",null,`第 ${row.row_index+1} 行`));
    for (const cell of row.cells) {
      const box=node("div","diagnostic");box.append(node("p",null,cell.text || "空白（未注明）"));
      for (const evidence of cell.evidence) {
        const b=node("button","text-button",`定位：${evidence.quote.slice(0,45)}`);
        b.onclick=()=>{ $("details-dialog").close();showSource(evidence); };box.append(b);
      }
      group.append(box);
    }
    items.push(group);
  }
  if(offset){const b=node("button","button","前3行");b.onclick=attempt(()=>browseTable(sid,tableId,Math.max(0,offset-3)));items.push(b);}
  if(data.has_more){const b=node("button","button","后3行");b.onclick=attempt(()=>browseTable(sid,tableId,offset+3));items.push(b);}
  const back=node("button","button","返回表格列表");back.onclick=attempt(()=>browseStructure("tables"));items.push(back);
  showDialog("表格原文与证据",items);
}
async function browseAdaptation(sid,tableId=null,region=null) {
  const gen=S.generation;
  const specs=await api(`/api/drawings/${sid}/adaptations/capabilities`);
  if(gen!==S.generation)return;
  const labels={table_grid:"表格网格",text_spans:"原文片段",segment_measurements:"原生线段长度",segment_distances:"线段最短间距",closed_contour:"闭合轮廓与面积"};
  const prompts={table_grid:"恢复该区域的真实表格网格",text_spans:"提取所选局部的相关完整原文，保留来源，不改写文字",segment_measurements:"计算所选局部原生线段的完整长度，使用图纸单位",segment_distances:"计算所选局部原生线段间的最短距离，不推定构件身份或换算单位",closed_contour:"识别一个有原生边支持的简单闭合轮廓并计算面积，不认定房间净面积"};
  const family=node("select");family.setAttribute("aria-label","调试方法");
  for (const [value,label] of Object.entries(labels)) {
    if(!specs[value] || (!tableId && !specs[value].scopes.includes("explicit_region")))continue;
    const option=node("option",null,label);option.value=value;family.append(option);
  }
  const instruction=node("textarea");instruction.setAttribute("aria-label","方法目标");instruction.rows=4;instruction.value=prompts[family.value];
  family.onchange=()=>{instruction.value=prompts[family.value];};
  const status=node("pre");const actions=node("div");
  const start=node("button","button","运行一次局部试验");
  const enable=node("button","button","授权 Agent 在读取时旁路运行");
  const disable=node("button","button","关闭 Agent 旁路");
  const scope=tableId?{table_id:tableId}:{layout_id:region.layout_id,bbox:[...region.bbox]};
  async function createPolicy(agent) {
    if(gen!==S.generation || S.drawing?.id!==sid)throw Error("图纸已切换，请重新选择局部范围。");
    return post(`/api/drawings/${sid}/adaptations/policies`,{
      idempotency_key:crypto.randomUUID(),model_profile:"siwc_sol",mode:"dynamic",
      instruction:instruction.value,max_runs:1,max_model_calls:3,max_reserved_tokens:96000,
      lifetime_seconds:14400,agent_shadow_enabled:agent,
      targets:[{snapshot_id:sid,...scope,method:family.value,trigger:agent?"authorized_read":"host_failure"}]
    });
  }
  function showActive() {
    const active=S.adaptationPolicy?.snapshot_id===sid;
    disable.hidden=!active;
    if(active)status.textContent=`Agent 旁路已授权：${S.adaptationPolicy.label}。仅匹配已选范围；同一策略只启动一个试验，最多 3 次生成/修正，4 小时后失效。`;
  }
  enable.onclick=attempt(async()=>{
    enable.disabled=true;
    try {
      const previous=S.adaptationPolicy;
      if(previous) {
        await post(`/api/drawings/${previous.snapshot_id}/adaptations/policies/${previous.policy_id}/disable`,{});
        S.adaptationPolicy=null;
      }
      const p=await createPolicy(true);
      S.adaptationPolicy={snapshot_id:sid,policy_id:p.policy_id,label:labels[family.value]};showActive();
    } finally {enable.disabled=false;}
  });
  disable.onclick=attempt(async()=>{
    const p=S.adaptationPolicy;if(!p)return;
    await post(`/api/drawings/${p.snapshot_id}/adaptations/policies/${p.policy_id}/disable`,{});
    S.adaptationPolicy=null;disable.hidden=true;status.textContent="已关闭后续旁路触发；已启动的运行仍可单独取消。";
  });
  start.onclick=attempt(async()=>{
    start.disabled=true;
    try {
      const p=await createPolicy(false);
      const event=await post(`/api/drawings/${sid}/adaptations/policies/${p.policy_id}/trials`,{target_id:p.targets[0].target_id});
      let run=await api(`/api/drawings/${sid}/adaptations/runs/${event.run_id}`);
      const cancel=node("button","button","取消执行");
      cancel.onclick=attempt(async()=>{run=await post(`/api/drawings/${sid}/adaptations/runs/${run.run_id}/cancel`,{});status.textContent=JSON.stringify(run,null,2);});
      actions.replaceChildren(cancel);
      while(["queued","running","cancelling"].includes(run.status)) {
        status.textContent=`${run.status} · 已执行 ${run.attempts.length} 次候选方法`;
        await new Promise(resolve=>setTimeout(resolve,1500));
        run=await api(`/api/drawings/${sid}/adaptations/runs/${run.run_id}`);
      }
      status.textContent=JSON.stringify(run,null,2);actions.replaceChildren();
      if(run.status==="awaiting_review") {
        const link=node("a","button","打开原图与候选结果复核");
        link.href=`/api/drawings/${sid}/adaptations/runs/${run.run_id}/review`;link.target="_blank";link.rel="noopener";actions.append(link);
      }
    } finally {start.disabled=false;}
  });
  showActive();
  const scopeText=tableId?"范围：当前选中表格。":`范围：打开此窗口时的当前视野，原坐标 ${region.bbox.map(v=>Number(v.toFixed(3))).join(", ")}。`;
  const form=node("div","adaptation-form");
  const methodLabel=node("label",null,"调试方法");methodLabel.append(family);
  const goalLabel=node("label",null,"方法目标");goalLabel.append(instruction);
  const buttons=node("div","adaptation-actions");buttons.append(start,enable,disable);
  form.append(methodLabel,goalLabel,buttons,actions,status);
  showDialog("局部方法调试",[node("p",null,scopeText),node("p",null,"运行会将该局部的文字、原生图元和调试反馈发送到 ChatGPT 直接订阅 GPT-6.1 Sol。每个试验最多 3 次生成/修正，隔离执行并独立验证。结果仅供复核，不自动替换现有结果或注册公共方法。"),form]);
}
$("adapt-region-button").onclick=attempt(()=>{
  if(!S.drawing||!S.view||!S.layout)return toast("请先选择图纸并缩放到要调试的局部。");
  const [x,y,w,h]=S.view;
  return browseAdaptation(S.drawing.id,null,{layout_id:S.layout,bbox:[x,-y-h,x+w,-y]});
});

const geometryReasons = {
  unknown_units: "图纸单位未明确", no_supported_enclosing_boundary: "未找到有证据支持的闭合边界",
  insufficient_outside_wall_face_support: "部分边界缺少外侧墙面支持",
  unclassified_interior_closed_objects: "内部闭合图形用途不明，需确认是否扣除",
  unclassified_interior_curves: "内部曲线用途未确认",
  boundary_reaches_search_or_frame_limit: "边界到达图框或搜索范围边缘",
  contains_other_room_anchor: "候选包含另一房间锚点",
  incomplete_geometry_source: "计算几何有未支持或未完整解析的图元",
  multiple_supported_boundaries: "存在多个有支持的边界",
  segment_limit_exceeded: "局部线段过多，请缩小分析范围",
  discovery_scope_too_large: "识别范围过大，请缩放到单边不超过 200 米的范围",
  curved_boundaries_not_discovered: "存在曲线；本轮直线拓扑不能保证发现曲墙房间",
  no_enclosed_space_found: "当前范围未找到闭合空间，不代表没有房间",
  no_supported_linework: "当前范围没有可用于围合的直线几何",
  opening_candidate_limit: "门洞闭合候选过多，本轮未自动补闭",
  intersection_limit_exceeded: "相交线段过密，请缩小识别范围",
  face_limit_exceeded: "围合面数量超过计算限制，请缩小识别范围",
  plan_view_unconfirmed: "尚未确认这是平面图；剖面中的房间文字和围合不能直接当作平面房间",
  non_plan_view: "当前范围已标为非平面视图（剖面、立面、详图、表格或图例），不能按平面房间识别",
  room_identity_unconfirmed: "空间是否属于房间尚未确认",
  alternative_inner_topology: "不同线形组合得到的内部环不同，当前拓扑口径优先使用完整线形图",
  area_basis_requires_semantic_review: "计量边界及内部对象用途尚待确认",
  tessellated_exclusion_contours: "所选内部曲线轮廓使用细分近似，精度记录见证据",
  contour_limit_exceeded: "闭合轮廓数量超过限制，请缩小识别范围",
};
function renderSpatialResults(container,result) {
  const rows=spatialRows(result);if(!rows.length)return;
  const section=node('section','room-batch-panel');
  section.append(node('strong',null,'几何核查结果'));
  const table=node('table','room-results-table'),head=node('tr');
  for(const label of ['对象','核查结果'])head.append(node('th',null,label));
  table.append(head);
  for(const row of rows) {
    const tr=node('tr');
    tr.append(node('td',null,row.label));
    const td=node('td');td.append(node('div',null,row.classification),node('strong',null,row.measurement));
    const detail=node('details'),summary=node('summary',null,'核查依据');detail.append(summary,node('p',null,row.reason));
    const link=node('a',null,'查看源几何 ↗');link.href=row.evidence_url;link.target='_blank';link.rel='noopener';detail.append(link);td.append(detail);
    tr.append(td);table.append(tr);
  }
  const hasDoors=rows.some(r=>r.measurement.includes('门垛'));
  section.append(table,node('small',null,hasDoors?'表中数值对应实际高亮对象；门垛间距不等于门框和完成面后的通行净宽。':'表中数值对应实际高亮对象，范围与候选口径见核查依据。'));container.append(section);
}
function applyResultHighlights(result) {
const action=result.highlight_action;
      if(action?.mode==='replace' && action.snapshot_id===S.drawing?.id) {
        $("drawing-highlight").replaceChildren(); $("drawing-measurement").replaceChildren();
        if(action.kind==='geometry') {
          const [a,b,c,d]=spatialHighlightBounds(action), pad=Math.max(c-a,d-b,1)*0.05;
          locate({layout_id:action.scope.layout_id,bbox:[a-pad,b-pad,c+pad,d+pad]});
          $("drawing-highlight").replaceChildren(); $("drawing-measurement").replaceChildren();
          renderHighlight($("drawing-measurement"),{highlight_regions:action.regions || []},"room-boundary");
          for(const path of action.paths || []) $("drawing-measurement").append(svg('path',{
            d:'M '+path.map(([x,y])=>`${x},${-y}`).join(' L '),fill:'none',stroke:'#7c3aed',
            'stroke-width':3,'vector-effect':'non-scaling-stroke'}));
        } else highlightRoomBatch({...action,total:action.items.length});
      } else for(const group of combinedBatchHighlights(result.room_batches || []))highlightRoomBatch(group);
}
function highlightRoomBatch(batch) {
  if(batch.snapshot_id!==S.drawing?.id || !batch.items.length || !batch.scope)return;
  const p=roomBatchPresentation(batch);
  // First fit and select the source layout, then replace the single-selection
  // overlays with the complete page. Numbering is shared with the answer table.
  locate({layout_id:p.layout_id,source_parts:p.labels,bbox:batch.scope.bbox});
  $("drawing-highlight").replaceChildren(); $("drawing-measurement").replaceChildren();
  const labels=svg("g",{}),boundaries=svg("g",{}),numbers=svg("g",{});
  renderHighlight(labels,{source_parts:p.labels},"source-highlight");
  if(p.boundaries.length) {
    const result=renderHighlight(boundaries,{highlight_regions:p.boundaries},"room-boundary");
    if(result.fallback)boundaries.replaceChildren();
  }
  for(const n of p.numbers) {
    const [x,y,xx,yy]=n.bbox;
    const label=svg("text",{x:xx,y:-yy,"font-size":Math.max(yy-y,1),fill:"#b24c00","pointer-events":"none"});
    label.textContent=String(n.number);numbers.append(label);
  }
  for (const shape of p.boundaries) for (const gap of shape.gaps || []) {
    boundaries.append(svg('path',{d:'M '+gap.map(([x,y])=>`${x},${-y}`).join(' L '),fill:'none',stroke:'#ea580c','stroke-width':3,'vector-effect':'non-scaling-stroke','stroke-dasharray':'7 5'}));
  }
  $("drawing-measurement").append(boundaries,labels,numbers);
}

function renderTask(panel,task,note='') {
  const labels={pending:'待执行',running:'执行中',completed:'完成',waiting_input:'需要补充信息',partial:'部分完成',blocked_capability:'未解决'};
  const failed=task.publication_status==='failed' || task.last_error;
  panel.replaceChildren(node('p','task-summary',failed ? '分析结果已保留 · 回答未发布' : `任务 · ${labels[task.status] || task.status}`));
  if(note)panel.append(node('p','answer-note',note));
  const fields = node('button', 'text-button', '查看字段、口径与来源');
  fields.onclick = attempt(() => showPropertyEvidence(task));
  panel.append(fields);
  if (task.door_schedule_relation_count) {
    const relations = node('button', 'text-button', `查看门实例与门表候选 · ${task.door_schedule_relation_count}`);
    relations.onclick = attempt(() => showDoorScheduleRelations(task));
    panel.append(relations);
  }
  if (Object.keys(task.gap_recovery || {}).length) {
    const recovery = node('button', 'text-button', '查看待补项与恢复状态');
    recovery.onclick = attempt(() => showGapRecovery(task));
    panel.append(recovery);
  }
  if (task.object_registry) {
    const objects = node('details');
    objects.append(node('summary', null, `对象与覆盖 · ${task.object_registry.candidate_object_count} 条候选记录`));
    objects.append(node('p', 'answer-note', '候选记录不代表已确认房间数量；身份、边界和计量口径分别核查。'));
    const link = node('a', 'export-link', '查看对象来源与覆盖记录 ↗');
    link.href = `/api/tasks/${task.task_id}/objects`;
    link.target = '_blank'; link.rel = 'noopener'; objects.append(link);
    panel.append(objects);
  }
  if(task.checkpoint) {
    const saved=node('details');saved.append(node('summary',null,'已保存的原文与计算（待综合核对）'));
    saved.append(node('p','answer-note',task.checkpoint.note));
    for(const e of task.checkpoint.sources || []) {
      const text=node('pre',null,e.quote), b=node('button','button','定位原文');
      b.onclick=attempt(async()=>{if(e.snapshot_id!==S.drawing?.id)await selectDrawing(e.snapshot_id);showSource(e);});saved.append(text,b);
    }
    for(const o of task.checkpoint.calculations || [])saved.append(node('pre',null,JSON.stringify(o.data,null,2)));
    panel.append(saved);
  }
  const details=node('details');details.append(node('summary',null,'查看执行步骤'));
  for(const step of task.subtasks) {
    const row=node('div','room-batch-row');
    row.append(node('strong',null,labels[step.status] || step.status),node('span',null,step.requirement));
    if(step.reason)row.append(node('small',null,step.reason==='unknown_units' ? '单位检查后仍无法换算面积。' : step.reason));
    details.append(row);
  }
  const link=node('a','export-link','任务与证据记录 ↗');link.href=task.evidence_url;link.target='_blank';link.rel='noopener';details.append(link);panel.append(details);
  if(failed || (task.status!=='completed' && task.subtasks.some(s=>s.status==='pending' || s.status==='blocked_capability'))) {
    const next=node('button','button',failed ? '恢复结果交付' : '继续未完成分析');
    next.onclick=attempt(()=>task.snapshot_id===S.drawing?.id && ask(task.question,task));panel.append(next);
  }
}

async function showPropertyEvidence(task, offset = 0) {
  const gen = S.generation;
  const packet = await api(`/api/tasks/${encodeURIComponent(task.task_id)}/property-evidence?offset=${offset}&limit=30`);
  if (gen !== S.generation) return;
  const cards = [node('p', 'answer-note', packet.note || '分别展示已读来源的属性值；来源绑定不等于对象身份确认。')];
  for (const row of propertyRows(packet)) {
    const card = node('div', 'room-batch-row');
    card.append(node('strong', null, `${row.subject} · ${row.property}`));
    card.append(node('p', null, `${row.value} ${row.unit}`));
    card.append(node('p', 'answer-note', `${row.basis} · ${row.state}`));
    for (const id of new Set(row.evidenceIds)) {
      const evidence = packet.evidence?.find(e => e.evidence_id === id);
      if (!evidence) continue;
      const button = node('button', 'text-button', `定位：${evidence.quote?.slice(0, 55) || '原图依据'}`);
      button.onclick = attempt(async () => {
        if (gen !== S.generation) return;
        if (evidence.snapshot_id !== S.drawing?.id) await selectDrawing(evidence.snapshot_id);
        $('details-dialog').close();
        showSource(evidence);
      });
      card.append(button);
    }
    if (row.observationIds.length) {
      const link = node('a', 'export-link', '查看完整工具记录 ↗');
      link.href = `/api/tasks/${encodeURIComponent(task.task_id)}/evidence-records`;
      link.target = '_blank'; link.rel = 'noopener'; card.append(link);
    }
    if (row.diagnostics.length) card.append(node('small', null, row.diagnostics.join('；')));
    cards.push(card);
  }
  if (!packet.items?.length) cards.push(node('p', null, '当前任务尚无可展示的字段证据。'));
  if (offset > 0) {
    const previous = node('button', 'button', '上一页');
    previous.onclick = attempt(() => showPropertyEvidence(task, Math.max(0, offset - 30)));
    cards.push(previous);
  }
  if (packet.has_more) {
    const next = node('button', 'button', '下一页');
    next.onclick = attempt(() => showPropertyEvidence(task, packet.next_offset));
    cards.push(next);
  }
  $('dialog-title').textContent = `字段与来源 · ${packet.total} 条记录`;
  $('dialog-content').replaceChildren(...cards);
  if (!$('details-dialog').open) $('details-dialog').showModal();
}

async function showDoorScheduleRelations(task, offset = 0) {
  const gen = S.generation;
  const packet = await api(`/api/tasks/${encodeURIComponent(task.task_id)}/door-schedule-relations?offset=${offset}&limit=10`);
  if (gen !== S.generation) return;
  const cards = [node('p', 'answer-note', '实例、型号标注和表行分别保留来源。同型号仅建立候选关系；楼层、版本与实际适用性仍需核对。')];
  const locate = evidence => {
    const button = node('button', 'text-button', `定位：${evidence.quote?.slice(0, 55) || '原文'}`);
    button.onclick = attempt(async () => {
      if (gen !== S.generation) return;
      if (evidence.snapshot_id !== S.drawing?.id) await selectDrawing(evidence.snapshot_id);
      $('details-dialog').close(); showSource(evidence);
    });
    return button;
  };
  for (const relation of packet.items || []) {
    const card = node('div', 'room-batch-row');
    card.append(node('strong', null, `${relation.models.join(' / ')} · ${relation.candidate_count} 条表行候选`));
    card.append(node('p', 'answer-note', relation.note));
    for (const ann of relation.annotations || []) card.append(locate(ann.evidence));
    for (const candidate of relation.candidates || []) {
      const row = node('details');
      row.append(node('summary', null, `门表第 ${candidate.row_index + 1} 行 · 待确认适用性`));
      for (const field of candidate.record?.fields || []) {
        row.append(node('p', null, `${field.header_path?.join(' / ') || field.field_roles?.join(' / ') || '字段'}：${field.text || '空白'}`));
        for (const evidence of field.evidence || []) row.append(locate(evidence));
      }
      card.append(row);
    }
    if (!relation.candidate_count) card.append(node('p', null, '已读范围中尚无匹配表行；不能据此判断整张门表不存在该型号。'));
    cards.push(card);
  }
  for (const [label, page] of [['上一页', offset > 0 ? Math.max(0, offset - 10) : null],
      ['下一页', packet.has_more ? packet.next_offset : null]]) {
    if (page === null) continue;
    const button = node('button', 'button', label);
    button.onclick = attempt(() => showDoorScheduleRelations(task, page)); cards.push(button);
  }
  showDialog(`门实例与门表候选 · ${packet.total} 组`, cards);
}

async function showGapRecovery(task) {
  const gen = S.generation;
  const gaps = await api(`/api/tasks/${encodeURIComponent(task.task_id)}/gap-recovery`);
  if (gen !== S.generation) return;
  const names = {
    header_unread: '表头尚未核对', unit_missing: '单位依据不足',
    visual_identity: '对象身份待核对', single_side_connection: '另一侧连接待核对',
    calculation_inputs: '计算输入不完整', no_new_evidence: '尚未找到新增依据',
  };
  const states = {open:'待补充',resolved:'已补充',stopped:'已停止重试'};
  const cards = gaps.map(gap => {
    const card = node('div', 'room-batch-row');
    card.append(node('strong', null, names[gap.kind] || '待核对项'));
    card.append(node('p', null, `${states[gap.status] || '待核对'} · 已尝试 ${gap.attempts} / ${gap.attempt_limit} 次`));
    if (gap.recommendation?.reason) card.append(node('p', null, gap.recommendation.reason));
    if (gap.stop_reason) card.append(node('p', 'answer-note', '当前恢复条件不满足，保留缺口；可在补充资料或确认范围后继续分析。'));
    return card;
  });
  if (!cards.length) cards.push(node('p', null, '当前没有登记的恢复项。'));
  showDialog('待补项与恢复状态', cards);
}

function renderRoomBatch(panel,batch,taskContext=null) {
  if(batch.snapshot_id!==S.drawing?.id)return;
  panel.replaceChildren(node("h4",null,`${batch.scope?.name || batch.request.floor_title} · 房间候选`));
  const presentation=roomBatchPresentation(batch);
  panel.append(node("p","answer-note",presentation.countText));
  const sid=batch.snapshot_id,gen=S.generation;
  if(batch.boundary_review_id) {
    const details=node('details','answer-note');details.append(node('summary',null,'逐项查看模型边界判断与原图'));
    const button=node('button','button','载入已保存的局部图与判断（不调用模型）');
    button.onclick=attempt(async()=>{
      const record=await api(`/api/drawings/${sid}/boundary-reviews/${batch.boundary_review_id}`);
      if(gen!==S.generation || sid!==S.drawing?.id)return;
      for(const item of record.items) details.append(node('p',null,`${item.number}. ${item.model_choice?.reason || '未解决'}${item.status==='unresolved' ? ' 本地校验未接受，保留名称定位。' : ' 仍为候选，非已确认净边界。'}`));
      for(const image of record.images) { const picture=node('img');picture.src=image.image_url;picture.alt='发送给模型的局部原图与轮廓候选';picture.style.maxWidth='100%';details.append(picture); }
      button.disabled=true;
    });details.append(button);panel.append(details);
  }

  const update=async(url,payload)=>{if(gen!==S.generation || sid!==S.drawing?.id)return;const next=await post(url,payload);if(gen===S.generation && sid===S.drawing?.id)renderRoomBatch(panel,next,taskContext);};
  const navigate=async(payload)=>{
    if(!taskContext)return update(payload.frame_id ? `${batch.evidence_url}/confirm-scope` : `${batch.evidence_url}/page`,payload.frame_id ? {...payload,confirmed:true} : payload);
    if(gen!==S.generation || sid!==S.drawing?.id)return;
    const next=await post(`${taskContext.task.evidence_url}/room-batches/continue`,{batch_id:batch.batch_id,...payload});
    if(gen===S.generation && sid===S.drawing?.id){taskContext.task=next.task;renderTask(taskContext.panel,next.task,next.note);renderRoomBatch(panel,next.batch,taskContext);}
  };
  if(batch.status==='needs_scope_confirmation') {
    panel.append(node("p",null,"楼层范围尚未确定。请选择有独立边界且无异名平面图标题冲突的范围；重复同名标题允许合并。确认只在本地执行，不调用模型。"));
    for(const f of batch.scope_choices) {
      const b=node("button","button",`${f.name} · ${f.frame_id}`);
      if(f.eligible===false) {
        b.disabled=true;
        const reasons={missing_closed_border:'缺少独立闭合边界',not_plan_title:'不是平面图标题',missing_matching_title:'未找到对应标题',conflicting_plan_titles:'框内包含不同平面图标题'};
        panel.append(node('p','answer-note',(f.reasons || []).map(r=>reasons[r] || r).join('；')+'。需先修正范围，重复确认无法解除。'));
      }
      b.onclick=attempt(()=>navigate({frame_id:f.frame_id}));panel.append(b);
    }
  }
  if(batch.unit_resolution?.status==='resolved')panel.append(node('p','answer-note',`${batch.unit_resolution.reason} 单位：${batch.unit}`));
  if(presentation.unitPending && batch.unit_resolution?.status==='needs_input') {
    panel.append(node("strong",null,"需要你补充：本次测量单位"),node("p",null,batch.unit_resolution.reason));
    const select=node("select"),confirm=node("button","button","确认单位并批量测量（本地）");
    select.setAttribute("aria-label","本次测量单位");
    for(const [value,label] of [["","请选择，暂不确定可不选"],["mm","毫米"],["cm","厘米"],["m","米"],["in","英寸"],["ft","英尺"]]) {
      const option=node("option",null,label);option.value=value;select.append(option);
    }
    confirm.disabled=true;select.onchange=()=>{confirm.disabled=!select.value;};
    confirm.onclick=attempt(async()=>{
      if(!select.value)return;
      confirm.disabled=true;select.disabled=true;
      try {
        if(taskContext) {
          if(gen!==S.generation || sid!==S.drawing?.id)return;
          const next=await post(`${taskContext.task.evidence_url}/confirm-unit`,{batch_id:batch.batch_id,unit:select.value,confirmed:true});
          if(gen===S.generation && sid===S.drawing?.id) {
            taskContext.task=next.task;
            renderTask(taskContext.panel,next.task,next.note);
            const updated=next.room_batches.find(b=>b.parent_batch_id===batch.batch_id);
            if(updated)renderRoomBatch(panel,updated,taskContext);
          }
        } else await update(`${batch.evidence_url}/confirm-unit`,{unit:select.value,confirmed:true});
      }
      finally {confirm.disabled=false;select.disabled=false;}
    });panel.append(select,confirm);
  }
  const table=node('table','room-results-table');
  if(batch.items.some(i=>i.candidate_area_m2!=null))panel.append(node('p','answer-note','数值为所选轮廓的围合面积，净面积口径待核查。'));
  const head=node('thead'),header=node('tr');
  for(const label of ['房间','面积（m²）','操作'])header.append(node('th',null,label));
  head.append(header);table.append(head);const body=node('tbody');table.append(body);
  if(batch.items.length)panel.append(table);
  for(const item of batch.items) {
    const row=node('tr'),name=node('td',null,`${item.number}. ${item.label}`),area=node('td'),actions=node('td');
    const value=item.area_m2 ?? item.candidate_area_m2;
    area.append(node('span',null,value!=null ? Number(value.toFixed(2)).toString() : '—'));
    row.append(name,area,actions);
    if(item.boundary_status==='unresolved')area.append(node('span','answer-note','轮廓未解决'));
    if(item.area_annotation_note)area.append(node('span','answer-note',item.area_annotation_note));
    if(item.highlight?.kind==='candidate_boundary') {
      const locateOutline=node('button','button','定位轮廓');locateOutline.onclick=()=>highlightRoomBatch({...batch,items:[item]});actions.append(locateOutline);
    }
    const evidenceDetails=node('details');evidenceDetails.append(node('summary',null,'依据'));actions.append(evidenceDetails);
    evidenceDetails.append(node('p',null,roomAreaText(item)));
    if(item.highlight.gaps?.length)evidenceDetails.append(node('p',null,'轮廓含开口闭合假设。'));
    if(item.boundary_evidence_url) { const link=node('a',null,'查看局部图、模型选择与逐边证据');link.href=item.boundary_evidence_url;link.target='_blank';evidenceDetails.append(link); }
    if(item.identity_note)evidenceDetails.append(node('span','answer-note',item.identity_note));
    const locateButton=node("button","button","定位名称原文");
    locateButton.onclick=()=>{if(sid===S.drawing?.id)showSource(item.label_evidence);};evidenceDetails.append(locateButton);
    if(item.measurement_evidence_url) {
      const b=node("button","button","查看测量依据");
      b.onclick=attempt(async()=>{const r=await api(item.measurement_evidence_url);if(sid===S.drawing?.id)showMeasurement(r);});evidenceDetails.append(b);
    }
    body.append(row);
  }
  if(batch.items.length) {
    panel.append(node("p","answer-note",presentation.note));
    const all=node("button","button","高亮当前页全部候选");all.onclick=()=>highlightRoomBatch(batch);panel.append(all);
    highlightRoomBatch(batch);
  }
  if(batch.has_more || batch.request.offset) {
    panel.append(node("p","answer-note",`当前仅展示第 ${batch.request.offset+1}–${batch.request.offset+batch.items.length} 个候选，未展示页不在高亮中。`));
    for(const [label,offset] of [["上一页",batch.request.offset-batch.request.limit],["下一页",batch.next_offset]]) {
      if(offset==null || offset<0)continue;
      const b=node("button","button",label);b.onclick=attempt(()=>navigate({offset}));panel.append(b);
    }
  }
  if(batch.resume_offset!=null) {
    const b=node("button","button","继续处理未测量候选（本地）");
    b.onclick=attempt(()=>navigate({offset:batch.resume_offset}));panel.append(b);
  }
  const evidence=node("a","export-link","批量证据与覆盖记录 ↗");evidence.href=batch.evidence_url;evidence.target="_blank";evidence.rel="noopener";panel.append(evidence);
}

function drawMeasurement(record) {
  const shape = record.selected;
  if (!shape) { $("drawing-measurement").replaceChildren(); return; }
  locate({layout_id:record.layout_id,bbox:shape.bbox,boundary:shape.boundary,holes:shape.holes});
  $("drawing-highlight").replaceChildren();
  const path = points => "M " + points.map(p => `${p[0]},${-p[1]}`).join(" L ");
  const outline = renderHighlight($("drawing-measurement"),shape,"room-boundary");
  // A display fallback rectangle must never impersonate a measured room boundary.
  if (outline.fallback) {
    $("drawing-measurement").replaceChildren();
    toast("边界轮廓超过显示限制或坐标无效，请查看完整测量证据。");
  }
  const elements=[...$("drawing-measurement").children];
  for(const closure of shape.inferred_closures||[]) elements.push(svg("path",{d:path(closure.points),class:"room-closure"}));
  for(const object of shape.unresolved_interior_objects||[]) elements.push(svg("path",{d:path(object.boundary)+" Z",class:"room-uncertain"}));
  $("drawing-measurement").replaceChildren(...elements);
  S.measurement=record;
}
function showMeasurement(record) {
  if(record.highlight_action && ['connection_review','discovery_review'].includes(record.kind)) {
    applyResultHighlights({highlight_action:record.highlight_action});
    return;
  }
  if(record.snapshot_id!==S.drawing?.id)return;
  if(record.kind==="room_discovery"){showRoomDiscovery(record);return;}
  if(record.kind==="room_area_basis"){showRoomAreaBasis(record);return;}
  drawMeasurement(record);
  const measured=record.status==="measured";
  const isRoom=record.kind==="room_net";
  const content=[node("p", "measurement-value", measured ? `${isRoom ? "净面积候选" : "几何测量"} ${record.area_m2 ?? record.value} ${record.value_unit || "m²"}` : "测量待核查 · 暂无确定结果")];
  if(!measured && record.candidate_area_m2!=null) content.push(node("p",null,`候选围合面积 ${record.candidate_area_m2} m²，仅供复核。`));
  content.push(node("p",null,record.note));
  content.push(node("p",null,`测量单位 ${record.applied_unit || record.unit} · ${record.unit_basis === "request_override" ? "本次指定" : "DXF 标头"}`));
  content.push(node("p",null,"绿色：候选净边界；橙色虚线：有门垛支持的推断闭合；红色：用途未确认的内部轮廓。"));
  if(record.selected) content.push(node("p",null,`使用 ${record.selected.entity_ids.length} 个来源图元，${record.selected.edge_evidence.length} 段边界，${record.selected.inferred_closures.length} 段推断闭合。`));
  for(const reason of record.diagnostics||[])content.push(node("p","answer-note",geometryReasons[reason]||reason));
  if(record.numerical_area_envelope_m2!=null)content.push(node("p",null,`坐标量化面积误差估计 ±${record.numerical_area_envelope_m2} m²；不包含边界识别误差。`));
  const locateButton=node("button","button","返回原图复核边界");
  locateButton.onclick=()=>{$("details-dialog").close();drawMeasurement(record);};content.push(locateButton);
  const preview=node("a","button","打开局部测量图");preview.href=record.preview_url;preview.target="_blank";preview.rel="noopener";content.push(preview);
  const download=node("a","button","下载测量证据 JSON");download.href=record.evidence_url;download.download=`${record.measurement_id}.json`;content.push(download);
  const details=node("details",null);details.append(node("summary",null,"完整计算依据"),node("pre",null,JSON.stringify(record,null,2)));content.push(details);
  showDialog(record.label ? `房间测量 · ${record.label}` : "房间测量",content);
}
async function runRoomMeasurement(payload) {
  if(!S.drawing)return;
  const sid=S.drawing.id;
  if(S.drawing.unit==="unknown" && !payload.unit_override){
    const label=node("label",null,"本次测量使用的图纸单位：");
    const select=node("select",null);select.setAttribute("aria-label","本次测量单位");
    select.append(option("","请选择已确认的单位"),...[["mm","毫米"],["cm","厘米"],["m","米"],["in","英寸"],["ft","英尺"]].map(([v,t])=>option(v,t)));
    label.append(select);
    const run=node("button","button","按所选单位分析");run.disabled=true;select.onchange=()=>{run.disabled=!select.value;};
    run.onclick=attempt(async()=>{$("details-dialog").close();if(sid===S.drawing?.id)await runRoomMeasurement({...payload,unit_override:select.value});});
    showDialog("图纸单位未明确",[node("p",null,"请核实单位后选择。本次选择会写入测量证据，原文件保持不变。"),label,run]);return;
  }
  toast("正在恢复房间边界并计算面积…");
  const record=await post(`/api/drawings/${sid}/rooms/measure`,payload);
  if(sid===S.drawing?.id)showMeasurement(record);
}
function showRoomAreaBasis(record) {
  if(record.snapshot_id!==S.drawing?.id)return;
  const draw=()=>{
    const points=record.boundary;
    locate({layout_id:record.layout_id,bbox:[Math.min(...points.map(p=>p[0])),Math.min(...points.map(p=>p[1])),Math.max(...points.map(p=>p[0])),Math.max(...points.map(p=>p[1]))]});
    $("drawing-highlight").replaceChildren();
    const path=p=>"M "+p.map(v=>`${v[0]},${-v[1]}`).join(" L ")+" Z";
    $("drawing-measurement").replaceChildren(...record.result_polygons.map(p=>svg("path",{d:[p.boundary,...p.holes].map(path).join(" "),class:"room-boundary","fill-rule":"evenodd"})));
  };
  draw();
  const labels={outer_shell:"外轮廓",topological_face:"拓扑面（扣直接内环）",selected_exclusions:"指定内部轮廓并集扣除"};
  const content=[node("p","measurement-value",`${labels[record.measurement_basis]}：${record.basis_area_m2} m²`),
    node("p",null,`外轮廓 ${record.shell_area_m2} m² − 扣除并集 ${record.excluded_union_area_m2} m²。候选几何口径，尚未确认净面积。`),
    node("p",null,record.note),node("p",null,`单位 ${record.applied_unit} · ${record.unit_basis==="request_override"?"本次指定":"DXF 标头"}`)];
  for(const reason of record.diagnostics||[])content.push(node("p","answer-note",geometryReasons[reason]||reason));
  const locateButton=node("button","button","返回原图查看口径叠加");locateButton.onclick=()=>{$("details-dialog").close();draw();};content.push(locateButton);
  for(const [label,url] of [["局部轮廓叠加",record.preview_url],["完整面积证据 JSON",record.evidence_url],["来源轮廓证据 JSON",record.source_evidence_url]]){
    const link=node("a","button",label);link.href=url;link.target="_blank";link.rel="noopener";content.push(link);
  }
  showDialog("房间轮廓 · 面积口径",content);
}
function showRoomContours(record,candidate) {
  if(record.snapshot_id!==S.drawing?.id)return;
  const model=candidate.geometry_model,byId=new Map(record.contours.map(c=>[c.contour_id,c]));
  const path=p=>"M "+p.map(v=>`${v[0]},${-v[1]}`).join(" L ")+" Z";
  const draw=()=>{
    locate({layout_id:record.layout_id,bbox:candidate.bbox});$("drawing-highlight").replaceChildren();
    $("drawing-measurement").replaceChildren(svg("path",{d:path(candidate.boundary),class:"room-boundary"}),
      ...model.interior_contour_ids.map(id=>svg("path",{d:path(byId.get(id).boundary),class:"room-uncertain"})));
  };
  draw();
  const content=[node("p",null,`1 个整体轮廓 · ${model.interior_contour_ids.length} 个内部闭合轮廓 · ${model.topological_hole_ids.length} 个拓扑直接内环。绿色为整体轮廓，红色为用途待确认的内部轮廓。`),
    node("p",null,"内部轮廓可能属于空洞、柱、设备或其他对象。保留包含关系供后续分析；选择扣除仅指定本次计算口径，不确认对象用途。")];
  const basis=node("select",null);basis.setAttribute("aria-label","面积计算口径");
  basis.append(option("outer_shell","外轮廓面积（不扣除）"),option("topological_face","拓扑面面积（扣直接内环）"),option("selected_exclusions","指定内部轮廓并集扣除"));content.push(basis);
  const selected=new Set(),choices=node("div",null);
  const ids=model.interior_contour_ids.slice(0,100),numbers=new Map(model.interior_contour_ids.map((id,i)=>[id,i+1]));
  for(const id of ids){
    const c=byId.get(id),label=node("label",null),input=node("input",null);input.type="checkbox";input.disabled=true;
    input.onchange=()=>input.checked?selected.add(id):selected.delete(id);
    const parents=c.parent_contour_ids.map(p=>p===model.outer_contour_id?"整体轮廓":numbers.has(p)?`内部 #${numbers.get(p)}`:"范围内其他轮廓").join(" / ")||"无唯一直接上级";
    label.append(input,document.createTextNode(`内部 #${numbers.get(id)} · ${c.candidate_area_m2} m² · 上级：${parents}${model.topological_hole_ids.includes(id)?" · 拓扑内环":""}${c.intersecting_boundary_ids.length?" · 存在边界相交/接触":""}`));
    const row=node("div",null);row.append(label);choices.append(row);
  }
  basis.onchange=()=>choices.querySelectorAll("input").forEach(input=>{input.disabled=basis.value!=="selected_exclusions";});content.push(choices);
  if(model.interior_contour_ids.length>100)content.push(node("p",null,"界面仅列出前 100 个内部轮廓；其余可通过轮廓工具分页读取，完整模型见来源证据。"));
  const locateButton=node("button","button","返回原图查看整体与内部轮廓");locateButton.onclick=()=>{$("details-dialog").close();draw();};content.push(locateButton);
  const run=node("button","button","按所选口径计算");
  run.onclick=attempt(async()=>{
    const result=await post(`/api/drawings/${record.snapshot_id}/rooms/area`,{discovery_id:record.measurement_id,candidate_id:candidate.candidate_id,basis:basis.value,exclusion_contour_ids:basis.value==="selected_exclusions"?[...selected]:[]});
    showRoomAreaBasis(result);
  });content.push(run);
  const back=node("button","button","返回空间候选");back.onclick=()=>showRoomDiscovery(record);content.push(back);
  showDialog("房间整体与内部轮廓",content);
}
function showRoomDiscovery(record,includeWeak=false) {
  if(record.snapshot_id!==S.drawing?.id)return;
  const sid=record.snapshot_id;
  const candidates=record.candidates||[];
  const supported=candidates.filter(c=>c.candidate_role==="room_candidate");
  const content=[node("p",null,`发现 ${supported.length} 个有双面墙线支持的空间候选，另有 ${candidates.length-supported.length} 个低支持围合。均尚未确认房间身份；没有名称的空间也会保留。`),node("p",null,record.note)];
  for(const reason of record.diagnostics||[])content.push(node("p","answer-note",geometryReasons[reason]||reason));
  const render=(chosen)=>{
    const path=p=>"M "+p.map(v=>`${v[0]},${-v[1]}`).join(" L ")+" Z";
    $("drawing-measurement").replaceChildren(...chosen.flatMap(c=>[svg("path",{d:path(c.boundary),class:"room-boundary"}),...(c.internal_rings||[]).map(r=>svg("path",{d:path(r),class:"room-uncertain"}))]));
  };
  // Bounded first page; subsequent candidates remain available in the evidence file.
  const visible=(includeWeak?candidates:supported).slice(0,50);render(visible);
  if(candidates.length>supported.length){
    const toggle=node("button","button",includeWeak?"仅显示有墙面支持的候选":"显示低支持围合（可能是墙带、设备或标注）");
    toggle.onclick=()=>showRoomDiscovery(record,!includeWeak);content.push(toggle);
  }
  if((includeWeak?candidates:supported).length>50)content.push(node("p",null,"界面展示前 50 个候选；可缩小视野或通过识别工具分页读取。完整结果保存在证据中。"));
  for(const c of visible){
    const row=node("div","measurement-candidate");
    const names=c.text_regions.map(t=>t.text).join(" / ")||"未命名空间";
    row.append(node("p",null,`关联文字：${names} · 外壳候选 ${c.candidate_shell_area_m2} m²（不是确认净面积）`));
    const locateButton=node("button","button","定位候选");
    locateButton.onclick=()=>{if(sid!==S.drawing?.id)return;$("details-dialog").close();locate({layout_id:record.layout_id,bbox:c.bbox});render([c]);};
    row.append(locateButton);
    if(c.geometry_model){
      const contours=node("button","button",`整体与内部轮廓（${c.geometry_model.interior_contour_ids.length}） / 面积口径`);
      contours.onclick=()=>showRoomContours(record,c);row.append(contours);
    }
    if(c.measurement_request){
      const measure=node("button","button","核查净边界与面积");
      measure.onclick=attempt(async()=>{if(sid!==S.drawing?.id)return;$("details-dialog").close();await runRoomMeasurement(c.measurement_request);});row.append(measure);
    }
    content.push(row);
  }
  const download=node("a","button","完整识别证据 JSON");download.href=record.evidence_url;download.download=`${record.measurement_id}.json`;content.push(download);
  showDialog("通用房间识别 · 当前视野",content);
}
async function discoverVisibleRooms(unitOverride=null){
  if(!S.drawing||!S.view)return;
  const sid=S.drawing.id;
  if(S.drawing.unit==="unknown"&&!unitOverride){
    const select=node("select",null);select.setAttribute("aria-label","识别使用的图纸单位");
    select.append(option("","请选择已核实的单位"),...[["mm","毫米"],["cm","厘米"],["m","米"],["in","英寸"],["ft","英尺"]].map(([v,t])=>option(v,t)));
    const run=node("button","button","开始识别");run.disabled=true;select.onchange=()=>{run.disabled=!select.value;};
    run.onclick=attempt(async()=>{if(sid!==S.drawing?.id)return;$("details-dialog").close();await discoverVisibleRooms(select.value);});
    showDialog("识别范围的单位未明确",[node("p",null,"请先核实图纸单位。本次指定会保存在识别证据中。"),select,run]);return;
  }
  const [x,y,w,h]=S.view;
  toast("正在发现当前视野的围合空间…");
  const summary=await post(`/api/drawings/${sid}/rooms/discover`,{layout_id:S.layout,frame_id:$("frame-select").value,bbox:[x,-y-h,x+w,-y],unit_override:unitOverride});
  const record=await api(summary.evidence_url);
  if(sid===S.drawing?.id)showRoomDiscovery(record);
}
$("room-discover-button").onclick=attempt(()=>discoverVisibleRooms());
function cancelSimilaritySelection() {
  S.similarityPick=false;
  S.similarityRequest++;
  $("similarity-button").setAttribute("aria-pressed","false");
  $("similarity-button").disabled=false;
  $("drawing-selection").replaceChildren();
  if (!S.roomPick) $("cad-canvas").classList.remove("room-pick");
  if (drag?.similarity) drag=null;
}
$("similarity-button").onclick=()=>{
  if (!S.drawing || !S.view) {toast('请先打开图纸。');return;}
  const enable=!S.similarityPick;
  cancelSimilaritySelection();
  S.roomPick=false;$("room-measure-button").setAttribute("aria-pressed","false");
  S.similarityPick=enable;
  $("similarity-button").setAttribute("aria-pressed",String(enable));
  $("cad-canvas").classList.toggle("room-pick",enable);
  toast(enable ? '拖动框选一个完整样本。将在所选图框内搜索；未选图框时搜索当前视野。' : '已取消框选。');
};
async function runSimilaritySearch(sampleBox, start) {
  const sid=S.drawing.id, lid=S.layout, frame=$("frame-select").value, gen=S.generation;
  const request=++S.similarityRequest;
  $("similarity-button").disabled=true;
  toast('正在比较当前范围内的形状候选…');
  try {
    const result=await post(`/api/drawings/${sid}/similarity-search`,{
      ...similarityScope(start.view,start.rect,lid,frame),sample_bbox:sampleBox,limit:12,
    });
    if (gen!==S.generation || request!==S.similarityRequest || sid!==S.drawing?.id ||
        lid!==S.layout || frame!==$("frame-select").value) return;
    const candidates=result.candidates || [];
    const cards=[node('p','answer-note',result.sample_issue || result.note || '形状相似仅用于定位候选，不确认门窗类型、功能或整图数量。')];
    if (result.coverage) cards.push(node('p','answer-note',
      `已扫描 ${result.coverage.entities_visited} 个图元${result.coverage.truncated ? '，处理范围或返回结果有截断' : ''}；本次结果不作为整图数量。`));
    for (const [index,item] of candidates.entries()) {
      const card=node('div','room-batch-row');
      card.append(node('strong',null,`候选 ${index+1} · 形状相似度 ${(Number(item.similarity)*100).toFixed(1)}%`));
      const locateButton=node('button','button','定位候选');
      locateButton.onclick=()=>{
        if (gen!==S.generation || sid!==S.drawing?.id || lid!==S.layout) return;
        $("details-dialog").close();
        locate({layout_id:lid,bbox:item.bbox});
        const path=similarityPath(item);
        if (path) {
          $("drawing-highlight").replaceChildren(svg('path',{
            d:path,fill:'none',stroke:'#087d65','stroke-width':3,
            'vector-effect':'non-scaling-stroke','pointer-events':'none',
          }));
          $("drawing-highlight").dataset.highlightMode='native-shape-paths';
          $("drawing-highlight").dataset.highlightFallback='';
        } else renderHighlight($("drawing-highlight"),{bbox:item.bbox});
        $("source-note").textContent='相似形状候选 · 工程身份尚未确认';
      };
      card.append(locateButton);
      if (item.bbox) card.append(node('small',null,
        `轴向包络 ${(item.bbox[2]-item.bbox[0]).toFixed(2)} × ${(item.bbox[3]-item.bbox[1]).toFixed(2)}（图纸单位，不代表规格）`));
      cards.push(card);
    }
    if (!candidates.length) cards.push(node('p',null,'本次已扫描范围未找到符合阈值的候选；可缩小样本、调整视野后重试。'));
    showDialog(`相似对象 · ${candidates.length} 条候选`,cards);
  } finally {
    if (request===S.similarityRequest) $("similarity-button").disabled=false;
  }
}
$("room-measure-button").onclick=()=>{
  if(!S.drawing){toast("请先导入图纸。");return;}
  cancelSimilaritySelection();
  S.roomPick=!S.roomPick;
  $("room-measure-button").setAttribute("aria-pressed",String(S.roomPick));
  $("cad-canvas").classList.toggle("room-pick",S.roomPick);
  toast(S.roomPick?"请在房间内部单击选点；可先缩放或拖动图纸。再次点击按钮可取消。":"已取消选点。");
};
$("measurement-history-button").onclick=attempt(async()=>{
  if(!S.drawing)return;
  const sid=S.drawing.id;
  const data=await api(`/api/drawings/${sid}/measurements`);
  if(sid!==S.drawing?.id)return;
  showDialog("测量记录",data.items.length?data.items.map(item=>{
    const b=node("button","button",`${item.label||"几何测量"} · ${item.status==="measured"?"已计算":"待核查"} · ${item.area_m2??item.candidate_area_m2??item.value??"—"}`);
    b.onclick=attempt(async()=>{const r=await api(item.evidence_url);if(sid===S.drawing?.id)showMeasurement(r);});return b;
  }):[node("p",null,"当前图纸还没有测量记录。")]);
});

$("structure-button").onclick = attempt(() => browseStructure());
function diagnostics() {
  const drawing = S.drawing;
  if (!drawing) {
    toast("请先选择图纸。");
    return;
  }
  const items = [
    node("p", null, `文件：${drawing.name}`),
    node("code", null, `SHA-256 ${drawing.sha256}`),
    node(
      "p",
      null,
      `原生文字 ${drawing.text_count} 个 · 来源区域 ${drawing.region_count} 个。图框由封闭边框与标题推定，未确认区域保留在布局范围。`,
    ),
  ];
  if (S.facts?.coverage) {
    const c = S.facts.coverage;
    items.push(
      node(
        "p",
        null,
        `候选材料区域 ${c.candidate_units} 个，已生成事实的区域 ${c.source_units_resolved} 个，需核查的区域 ${c.unresolved_region_ids.length} 个。${c.note}`,
      ),
    );
  }
  const render = S.scene?.render;
  const ds = [
    ...drawing.diagnostics.filter((d) => !render || d.code !== "PREVIEW_UNSUPPORTED"),
    ...(render?.diagnostics || []).map((message) => ({ code: "RENDER_PARTIAL", message })),
    ...(S.facts?.diagnostics || []),
  ];
  if (render) items.push(node("p", null,
    `轻量预览：保留文字位置、换行、大致字距和可读线宽；使用系统字体、实线及填充边界，保留实心填充和标注。适合查看内容，不用于精确出图。`));
  items.push(
    node(
      "p",
      null,
      "文字字体与实体边界为估算。未启用 OCR、DWG 转换和业务优化策略。",
    ),
  );
  for (const d of ds) {
    const e = node("div", "diagnostic");
    e.append(node("code", null, d.code), node("p", null, d.message));
    items.push(e);
  }
  if (!ds.length)
    items.push(
      node("p", null, "当前解析未记录额外异常；这不等于整图语义已完整识别。"),
    );
  showDialog("来源与覆盖诊断", items);
}
$("demo-button").onclick = attempt(demo);
$("empty-demo").onclick = attempt(demo);
async function importDrawingFile(file) {
  if (!file) return;
  if (S.busy) {
    toast("正在处理图纸，请完成后再导入。");
    return;
  }
  if (!/\.(dwg|dxf)$/i.test(file.name)) {
    toast("仅支持导入 DWG 或 DXF 文件。");
    return;
  }
  const isDwg = /\.dwg$/i.test(file.name);
  const status = $("import-status");
  status.hidden = false;
  status.textContent = isDwg
    ? "正在通过内网转换 DWG 并建立图纸索引，请稍候…"
    : "正在读取 DXF 并建立图纸索引…";
  busy(true, status.textContent);
  try {
    const form = new FormData();
    form.append("file", file);
    const d = await api("/api/drawings", { method: "POST", body: form });
    status.textContent = d.conversion ? "DWG 已转换为 DXF 并导入，可下载原文件、DXF 和图纸 JSON。" : "DXF 已导入。";
    await refreshDrawings();
    await selectDrawing(d.id);
    const job = await post("/api/extractions", {
      snapshot_id: d.id,
      mode: $("extract-mode").value,
    });
    await waitJob(job, d.id);
  } catch (error) {
    // A failed import never reaches drawing selection or automatic extraction.
    status.textContent = error.message;
    throw error;
  } finally {
    busy(false);
    $("file-input").value = "";
  }
}
$("upload-button").onclick = () => $("file-input").click();
$("file-input").onchange = attempt(async () => {
  try {
    await importDrawingFile($("file-input").files[0]);
  } finally {
    $("file-input").value = "";
  }
});
const uploadButton = $("upload-button");
const uploadLabel = uploadButton.textContent;
const isFileDrag = (event) => Array.from(event.dataTransfer?.types || []).includes("Files");
function resetImportDrop() {
  uploadButton.classList.remove("drag-over");
  uploadButton.textContent = uploadLabel;
}
for (const eventName of ["dragenter", "dragover"]) {
  uploadButton.addEventListener(eventName, (event) => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = S.busy ? "none" : "copy";
    if (S.busy) {
      resetImportDrop();
      return;
    }
    uploadButton.classList.add("drag-over");
    uploadButton.textContent = "松开导入图纸";
  });
}
uploadButton.addEventListener("dragleave", resetImportDrop);
uploadButton.addEventListener("drop", attempt(async (event) => {
  event.preventDefault();
  event.stopPropagation();
  resetImportDrop();
  const files = Array.from(event.dataTransfer?.files || []);
  if (!files.length) return;
  if (files.length !== 1) {
    toast("请每次拖入一张 DWG 或 DXF 图纸。");
    return;
  }
  await importDrawingFile(files[0]);
}));
// Prevent a missed drop from replacing the workspace with the local file.
for (const eventName of ["dragover", "drop"]) {
  document.addEventListener(eventName, (event) => {
    if (!isFileDrag(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "none";
    resetImportDrop();
  });
}
document.addEventListener("dragend", resetImportDrop);
$("drawing-select").onchange = attempt((e) => selectDrawing(e.target.value));
$("reindex-drawing").onclick = attempt(async () => {
  if (!S.drawing) return;
  const id = S.drawing.id;
  const generation = S.generation;
  $("reindex-drawing").disabled = true;
  try {
    const drawing = await api(`/api/drawings/${id}/reindex`, {method: "POST"});
    if (generation !== S.generation) return;
    await refreshDrawings();
    await selectDrawing(drawing.id);
  } finally {
    $("reindex-drawing").disabled = !S.drawing;
  }
});
$("layout-select").onchange = (e) => {
  cancelSimilaritySelection();
  S.layout = e.target.value;
  clearSelection();
  renderScene();
};
$("frame-select").onchange = attempt(async () => {
  cancelSimilaritySelection();
  S.limit = 100;
  await loadFacts();
  const f = S.drawing.frames.find(
    (f) => f.frame_id === $("frame-select").value,
  );
  if (f) {
    if (S.layout !== f.layout_id) {
      S.layout = f.layout_id;
      $("layout-select").value = S.layout;
      renderScene();
    }
    setView(boxView(f.bbox));
  }
});
for (const t of ["facts", "sources"])
  $(`${t}-tab`).onclick = attempt(async () => {
    S.tab = t;
    S.limit = 100;
    setTabUI();
    await loadFacts();
  });
document.querySelectorAll("[data-filter]").forEach(
  (b) =>
    (b.onclick = attempt(async () => {
      S.filter = b.dataset.filter;
      S.limit = 100;
      setTabUI();
      await loadFacts();
    })),
);
let searchTimer;
$("search").oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(
    attempt(async () => {
      S.limit = 100;
      await loadFacts();
    }),
    200,
  );
};
$("extract-button").onclick = attempt(extractCurrent);
$("close-evidence").onclick = clearSelection;
$("clear-selection").onclick = clearSelection;
$("clear-chat").onclick = clearChat;
$("run-history-button").onclick = attempt(async () => {
  if(!S.drawing){toast('请先打开图纸。');return;}
  await runInspector.showHistory(S.drawing.id);
});
$("source-read-batching").checked = false;
$("chat-settings-button").onclick = () => {
  const box=$("related-drawings");box.replaceChildren();
  const sid=S.drawing?.id,selected=S.relatedDrawings.get(sid)||[];
  for(const d of S.drawings.filter(d=>d.id!==sid)) {
    const row=node("div","settings-fields"), label=node("label",null,d.name), check=node("input");check.type="checkbox";
    const existing=selected.find(x=>x.snapshot_id===d.id);check.checked=!!existing;label.prepend(check);row.append(label);
    const inputs={};
    for(const [key,title] of [["project","项目"],["discipline","专业"],["floor","楼层"],["revision","版本"],["relationship","与当前图纸的关系"]]) {
      const input=node("input");input.placeholder=title;input.setAttribute("aria-label",`${d.name} · ${title}`);input.maxLength=key==='relationship'?160:50;input.value=existing?.[key]||'';inputs[key]=input;row.append(input);
    }
    const update=()=>{const all=S.relatedDrawings.get(sid)||[];const next=all.filter(x=>x.snapshot_id!==d.id);if(check.checked){if(next.length>=8){check.checked=false;toast("最多选择 8 份关联图纸");return;}next.push({snapshot_id:d.id,...Object.fromEntries(Object.entries(inputs).map(([k,v])=>[k,v.value]))});}S.relatedDrawings.set(sid,next);S.parentRun=null;};
    check.onchange=update;Object.values(inputs).forEach(i=>i.oninput=update);box.append(row);
  }
  if(!box.children.length)box.append(node("p",null,"导入其他图纸后，可在此选择关联资料。"));
  $("chat-settings-dialog").showModal();
};
for (const id of ["close-chat-settings", "done-chat-settings"])
  $(id).onclick = () => $("chat-settings-dialog").close();
function syncComposerSettings() {
  const local = $("chat-mode").value === "local";
  const subscription = $("model-profile").value.startsWith("codex_") || $("model-profile").value.startsWith("siwc_");
  $("agent-runtime").disabled = local;
  $("model-profile").disabled = local;
  $("model-profile").hidden = local;
  $("local-mode-label").hidden = !local;
  $("retrieval-mode").disabled = local || !S.retrievalSupported;
  $("runtime-fallback").disabled = local || subscription;
  $("model-profile").title = $("model-profile").selectedOptions[0]?.title || "选择问答模型";
  $("composer-status").textContent = local ? "仅查询本地事实" : "回答附原图依据";
  $("retrieval-help").textContent = local
    ? "本地查询直接读取已提取的事实，无需调用模型。"
    : S.retrievalSupported ? "关键词适合查找原文；混合检索可补充语义匹配。"
    : "当前服务仅支持关键词检索。";
}
function subscriptionSelection(fromRuntime = false) {
  if (fromRuntime && $("agent-runtime").value === "responses_subscription") $("model-profile").value = "siwc_sol";
  if (fromRuntime && $("agent-runtime").value === "codex_subscription" && !$("model-profile").value.startsWith("codex_")) {
    $("model-profile").value = "codex_sol_56";
  }
  const direct = $("model-profile").value.startsWith("siwc_");
  const selected = direct || $("model-profile").value.startsWith("codex_");
  if (selected) $("agent-runtime").value = direct ? "responses_subscription" : "codex_subscription";
  else if (["codex_subscription", "responses_subscription"].includes($("agent-runtime").value)) $("agent-runtime").value = "native";
  $("runtime-fallback").disabled = selected || $("chat-mode").value === "local";
  if (selected) {
    $("runtime-fallback").checked = false;
  }
  syncComposerSettings();
  S.parentRun = null;
}
async function refreshCodexAccount() {
  const state = await api("/api/codex-account");
  $("codex-account-status").textContent = state.connected
    ? `已连接 ChatGPT${state.plan_type ? ` · ${state.plan_type}` : ""}。模型调用将使用订阅额度。`
    : state.login_failed ? "上次登录未完成，请重新登录或尝试设备码登录。"
    : state.available ? "尚未连接。完成官方登录后点击“刷新登录状态”。" : "未安装可用的 Codex CLI。";
  if (state.connected || state.login_failed) {
    $("codex-auth-link").hidden = true;
    $("codex-device-code").hidden = true;
  }
  return state;
}
async function startCodexLogin(flow) {
  $("codex-account-status").textContent = "正在启动官方登录…";
  $("codex-device-code").hidden = true;
  $("codex-auth-link").hidden = true;
  const result = await post(`/api/codex-account/login?flow=${flow}`, {});
  if (result.auth_url) {
    const link = $("codex-auth-link");
    link.href = result.auth_url;
    link.hidden = false;
    $("codex-account-status").textContent = result.user_code
      ? "请在系统浏览器打开官方验证页面并输入下方设备码，授权后刷新登录状态。设备码请勿分享。"
      : "请点击下方官方登录链接，完成后刷新登录状态。若出现 JSON/HTML 错误，可改用设备码登录或在系统浏览器打开。";
    if (result.user_code) {
      $("codex-device-code").textContent = `本次设备码：${result.user_code}`;
      $("codex-device-code").hidden = false;
    }
  } else await refreshCodexAccount();
}
$("codex-login").onclick = attempt(() => startCodexLogin("browser"));
$("codex-device-login").onclick = attempt(() => startCodexLogin("device"));
$("codex-refresh").onclick = attempt(refreshCodexAccount);
$("codex-logout").onclick = attempt(async () => {
  await post("/api/codex-account/logout", {});
  $("codex-auth-link").hidden = true;
  $("codex-device-code").hidden = true;
  await refreshCodexAccount();
});
$("model-profile").onchange = () => subscriptionSelection();
async function refreshSiwcAccount() {
  const state = await api("/api/siwc-account");
  $("siwc-account-status").textContent = state.connected
    ? "独立订阅授权已连接；模型权限以实际调用为准。"
    : state.last_error ? `授权尚未完成（${state.last_error}），请重新登录。`
    : state.login_pending ? "等待在官方页面完成授权，完成后请刷新状态。" : "尚未授权。请选择账户或添加新的账户。";
  $("siwc-account-select").replaceChildren(node("option", null, "添加新的账户或工作区"));
  $("siwc-account-select").firstChild.value = "";
  for (const account of state.accounts) {
    const option = node("option", null, account.label); option.value = account.id;
    $("siwc-account-select").append(option);
  }
  $("siwc-account-select").value = state.active_account || "";
  if (!state.login_pending) {
    $("siwc-auth-link").hidden = true;
    $("siwc-auth-link").removeAttribute("href");
    $("siwc-copy-link").hidden = true;
  }
  return state;
}
$("siwc-login").onclick = attempt(async () => {
  const id = $("siwc-account-select").value;
  const result = await post("/api/siwc-account/login" + (id ? `?account_id=${encodeURIComponent(id)}` : ""), {});
  $("siwc-auth-link").href = result.auth_url;
  $("siwc-auth-link").hidden = false;
  $("siwc-copy-link").hidden = false;
  $("siwc-account-status").textContent = "请复制链接到系统浏览器，完成 CAD Atlas 订阅授权后刷新状态。新链接十分钟内有效，之前生成的链接已失效。";
});
$("siwc-copy-link").onclick = attempt(async () => {
  const state = await refreshSiwcAccount();
  const link = $("siwc-auth-link").getAttribute("href");
  if (!state.login_pending || !link) throw new Error("授权链接已失效，请点击 Continue with ChatGPT 重新生成。");
  try {
    await navigator.clipboard.writeText(link);
    $("siwc-account-status").textContent = "已复制授权链接。请粘贴到本机 Chrome / Safari 地址栏完成授权，勿分享链接；完成后刷新状态。";
  } catch {
    throw new Error("浏览器不允许复制。请右键“打开官方授权页面”复制链接，再粘贴到系统浏览器。");
  }
});
$("siwc-refresh").onclick = attempt(refreshSiwcAccount);
$("siwc-logout").onclick = attempt(async () => {
  const result = await post("/api/siwc-account/logout", {});
  await refreshSiwcAccount();
  if (!result.remote_revocation_confirmed) $("siwc-account-status").textContent = "本地凭据已清除；远程撤销尚未确认，可在 ChatGPT 设置中断开 CAD Atlas。";
});
$("retrieval-mode").onchange = () => { S.parentRun = null; };
$("agent-runtime").onchange = () => subscriptionSelection(true);
$("chat-mode").onchange = () => {
  S.parentRun = null;
  syncComposerSettings();
};
$("diagnostics-button").onclick = diagnostics;
$("close-dialog").onclick = () => $("details-dialog").close();
$("about-button").onclick = () =>
  showDialog("CAD Atlas · 图纸事实工作台", [
    node(
      "p",
      null,
      "从原生 DXF 整理砌体做法、查询事实并回到来源。所有模块独立运行，不连接现有生产流程。",
    ),
    node(
      "p",
      null,
      "当前为本地单用户 MVP。支持文字、展开块、基础线形与有明确网格的表格。抽取结果需要工程人员核查。",
    ),
    node("p", null, "界面布局参考 Nomic 的文件、原图与问答工作空间。"),
  ]);
$("nav-workspace").onclick = () =>
  $("cad-canvas").scrollIntoView({ block: "center" });
$("nav-files").onclick = () => $("drawing-select").focus();
$("cancel-chat-button").onclick = attempt(async () => {
  const id = S.activeChatRun;
  if (!id) return;
  const button = $("cancel-chat-button");
  button.disabled = true;
  button.textContent = "正在停止…";
  try {
    await post(`/api/chat-runs/${id}/cancel`, {});
  } catch (error) {
    if (S.activeChatRun === id) {
      button.disabled = false;
      button.textContent = "停止分析";
    }
    throw error;
  }
});
$("chat-form").onsubmit = attempt(async (e) => {
  e.preventDefault();
  await ask($("question").value);
});
$("question").onkeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("chat-form").requestSubmit();
  }
};
bindSuggestions();
$("zoom-in").onclick = () => zoom(0.8);
$("zoom-out").onclick = () => zoom(1.25);
$("fit-button").onclick = fit;
const canvas = $("cad-canvas");
const retryPreview = node("button", "text-button", "重试加载");
retryPreview.id = "retry-preview"; retryPreview.hidden = true;
$("source-note").after(retryPreview);
retryPreview.onclick = () => { S.preview?.retry(); if (S.view) setView(S.view); };
canvas.addEventListener(
  "wheel",
  (e) => {
    if (!S.view || !e.deltaY) return;
    e.preventDefault();
    if (drag?.similarity) return;
    const p = clientToView(S.view, $("canvas-wrap").getBoundingClientRect(), e.clientX, e.clientY);
    zoom(wheelFactor(e.deltaY, e.deltaMode, $("canvas-wrap").clientHeight), p);
  },
  { passive: false },
);
let drag = null;
canvas.onpointerdown = (e) => {
  if (!S.view || e.button !== 0) return;
  drag = {x: e.clientX, y: e.clientY, view: [...S.view], pointerId: e.pointerId,
    scale: Math.min($("canvas-wrap").clientWidth/S.view[2],$("canvas-wrap").clientHeight/S.view[3]),
    similarity:S.similarityPick, rect:$("canvas-wrap").getBoundingClientRect()};
  canvas.setPointerCapture(e.pointerId);
};
canvas.onpointermove = (e) => {
  if (!drag || drag.pointerId !== e.pointerId) return;
  if (drag.similarity) {
    const box = selectionBox(drag.view, drag.rect, [drag.x,drag.y], [e.clientX,e.clientY]);
    $("drawing-selection").replaceChildren(...(box ? [svg('rect',{
      x:box[0],y:-box[3],width:box[2]-box[0],height:box[3]-box[1],
      fill:'#2563eb18',stroke:'#2563eb','stroke-width':2,'vector-effect':'non-scaling-stroke',
    })] : []));
    return;
  }
  setView(panView(drag.view, drag.scale, e.clientX-drag.x, e.clientY-drag.y));
};
canvas.onpointerup = (e) => {
  const start=drag;drag=null;
  if (start?.similarity && start.pointerId === e.pointerId) {
    const box = selectionBox(start.view, start.rect, [start.x,start.y], [e.clientX,e.clientY]);
    S.similarityPick=false;$("similarity-button").setAttribute("aria-pressed","false");canvas.classList.remove("room-pick");
    $("drawing-selection").replaceChildren();
    if (box) attempt(() => runSimilaritySearch(box, start))();
    else toast('请拖动框选有面积的样本范围。');
    return;
  }
  if(!start || !S.roomPick || start.pointerId!==e.pointerId || Math.hypot(e.clientX-start.x,e.clientY-start.y)>4)return;
  const p=clientToView(start.view,$("canvas-wrap").getBoundingClientRect(),e.clientX,e.clientY);
  S.roomPick=false;$("room-measure-button").setAttribute("aria-pressed","false");canvas.classList.remove("room-pick");
  attempt(()=>runRoomMeasurement({layout_id:S.layout,seed:[p[0],-p[1]],frame_id:$("frame-select").value}))();
};
canvas.onpointercancel = canvas.onlostpointercapture = () => { drag = null; $("drawing-selection").replaceChildren(); };
new ResizeObserver(() => {
  if (S.view && !viewFrame) viewFrame = requestAnimationFrame(commitView);
}).observe($("canvas-wrap"));
await attempt(async () => {
  const health = await api("/api/health");
  $("codex-account-panel").hidden = !health.codex_subscription?.supported;
  $("agent-runtime").querySelector('[value="codex_subscription"]').hidden = !health.codex_subscription?.supported;
  $("siwc-account-panel").hidden = !health.siwc_subscription?.supported;
  $("account-settings").hidden = !health.codex_subscription?.supported && !health.siwc_subscription?.supported;
  $("agent-runtime").querySelector('[value="responses_subscription"]').hidden = !health.siwc_subscription?.supported;
  S.retrievalSupported = !!health.retrieval?.modes?.includes("hybrid");
  $("retrieval-mode").disabled = !S.retrievalSupported;
  if (!S.retrievalSupported) $("retrieval-mode").title = "当前后端尚未启用混合检索；仍使用原关键词流程";
  const models = await api("/api/models");
  $("model-profile").replaceChildren(...models.items.map(model => {
    const label = model.label === "默认" ? `默认 · ${model.model || "未配置"}`
      : model.label.replace("ChatGPT 直接订阅 · ", "").replace("Codex 订阅 · ", "")
        + (model.id.startsWith("siwc_") ? " · ChatGPT" : model.id.startsWith("codex_") ? " · Codex" : "");
    const option = node("option", null, label);
    option.title = `${model.label} · ${model.model || "未配置"}${model.context_length ? ` · ${model.context_length / 1024}K` : ""}`;
    option.value = model.id;
    option.disabled = !model.configured;
    return option;
  }));
  $("model-profile").value = models.default;
  const runtimes = await api("/api/runtimes");
  for (const runtime of runtimes.items) {
    const option = $("agent-runtime").querySelector(`[value="${runtime.id}"]`);
    option.disabled = !runtime.available;
    if (!runtime.available) option.textContent += " · 未安装";
  }
  subscriptionSelection();
  $("extract-mode").querySelector('[value="model"]').disabled =
    !health.model.configured;
  $("model-note").textContent = health.model.configured
    ? `问答使用所选模型；模型辅助提取使用 ${health.model.model}。按需发送当前图纸来源文字。`
    : "请在本地 .env 填写 DEEPSEEK_API_KEY 并重启；也可选择本地事实查询。";
  await refreshDrawings();
  if (S.drawings.length && !new URLSearchParams(location.search).has('loadPerf')) await selectDrawing(S.drawings[0].id);
})();

if (new URLSearchParams(location.search).get('renderPerf') === '1') {
  const {install} = await import('./render-perf.js');
  install({S, selectDrawing, fit, zoom, setView, showSource, clearSelection, api, whenPreviewReady});
}
if (new URLSearchParams(location.search).get('loadPerf') === '1') {
  const {install} = await import('./load-perf.js');
  install({S, selectDrawing, whenPreviewReady});
}

async function whenPreviewReady() {
  await new Promise(requestAnimationFrame);
  await S.preview?.wait();
  await new Promise(requestAnimationFrame);
}

if (new URLSearchParams(location.search).get('highZoomPerf') === '1') {
  const {install} = await import('./high-zoom-perf.js');
  install({S,selectDrawing,fit,zoom,setView,showSource,clearSelection,api,whenPreviewReady});
}

if (new URLSearchParams(location.search).get('panPerf') === '1') {
  const {install} = await import('./pan-perf.js');
  install({S,selectDrawing,setView,showSource,api,whenPreviewReady,setBufferEnabled: enabled => { panBuffer.disabled = !enabled; panBuffer.reset(); if (S.view) setView([...S.view]); }});
}

if (new URLSearchParams(location.search).get('highlightPerf') === '1') {
  const {install} = await import('./highlight-perf.js');
  install({S,selectDrawing,setView,locate,api,whenPreviewReady,clearSelection});
}

async function browseAnnotations(kind="axes",offset=0,query="",selected=[]) {
  if(!S.drawing||!S.layout)return;
  const sid=S.drawing.id,gen=S.generation,lid=S.layout;
  toast("正在读取符号与关系候选…");
  const data=await post(`/api/drawings/${sid}/semantics/query`,{kind,layout_id:lid,frame_id:$("frame-select").value,offset,limit:8,query});
  if(gen!==S.generation)return;
  const tabs=node("div","tabs");
  for(const [key,label] of [["axes","轴网"],["references","索引"],["leaders","引线"],["elevations","标高"],["storeys","楼层"]]) {
    const b=node("button",key===kind?"selected":"",label);b.onclick=attempt(()=>browseAnnotations(key));tabs.append(b);
  }
  const input=node("input");input.value=query;input.placeholder="按原文筛选";input.setAttribute("aria-label","筛选符号原文");
  const filter=node("button","button","筛选");filter.onclick=attempt(()=>browseAnnotations(kind,0,input.value,selected));
  const items=[tabs,input,filter,node("p","answer-note",`${data.total} 个候选。${data.note}`)];
  if(kind==="axes") {
    const locateButton=node("button","button",`定位已选轴线（${selected.length}）`);
    locateButton.disabled=![2,4].includes(selected.length);
    locateButton.onclick=attempt(async()=>{
      const r=await post(`/api/drawings/${sid}/semantics/locate`,{layout_id:lid,axis_ids:selected});
      if(gen!==S.generation)return;
      if(r.point){const [x,y]=r.point;const span=Math.max((S.view?.[2]||1000)*.05,1);$("details-dialog").close();locate({layout_id:lid,bbox:[x-span,y-span,x+span,y+span]});}
      else if(r.boundary){const xs=r.boundary.map(p=>p[0]),ys=r.boundary.map(p=>p[1]);$("details-dialog").close();locate({layout_id:lid,bbox:[Math.min(...xs),Math.min(...ys),Math.max(...xs),Math.max(...ys)],boundary:r.boundary});}
      else showDialog("轴网定位待解决",[node("p",null,r.note),node("pre",null,JSON.stringify(r.diagnostics,null,2))]);
    });items.push(locateButton);
  }
  for(const item of data.items){
    const card=node("div","fact-card");card.append(node("strong",null,item.label||item.kind));
    const go=node("button","text-button","定位原图");go.onclick=()=>{if(gen!==S.generation)return;$("details-dialog").close();locate({layout_id:lid,bbox:item.bbox});};card.append(go);
    if(kind==="axes"){
      const choose=node("button","text-button",selected.includes(item.object_id)?"取消选择":"选择轴线");
      choose.onclick=attempt(()=>{const ids=selected.includes(item.object_id)?selected.filter(x=>x!==item.object_id):[...selected,item.object_id];if(ids.length>4)return toast("最多选择四条轴线。");return browseAnnotations(kind,offset,query,ids);});card.append(choose);
    }
    if(kind==="references"){
      const trace=node("button","text-button","追踪当前图纸详图");
      trace.onclick=attempt(()=>browseDetailTrace(sid,lid,item.object_id,()=>browseAnnotations(kind,offset,query)));
      card.append(trace);
    }
    const details=node("details");details.append(node("summary",null,"来源与候选关系"),node("pre",null,JSON.stringify(item,null,2)));card.append(details);items.push(card);
  }
  if(!data.items.length)items.push(node("p",null,"当前范围未识别到候选，不代表图纸没有相关内容。"));
  if(offset){const b=node("button","button","上一页");b.onclick=attempt(()=>browseAnnotations(kind,Math.max(0,offset-8),query,selected));items.push(b);}
  if(data.has_more){const b=node("button","button","下一页");b.onclick=attempt(()=>browseAnnotations(kind,offset+8,query,selected));items.push(b);}
  showDialog("轴网、索引与楼层",items);
}
async function browseSemanticTable(sid,tableId,offset=0){
  const gen=S.generation;
  const data=await post(`/api/drawings/${sid}/semantics/table`,{table_id:tableId,row_offset:offset,row_limit:3});
  if(gen!==S.generation)return;
  const items=[node("p","answer-note",data.note),node("p",null,`表头候选：${data.header_rows.map(r=>r+1).join("、")||"未确定"}`)];
  for(const row of data.items){
    const group=node("div","table-preview-row");group.append(node("strong",null,`第 ${row.row_index+1} 行`));
    for(const c of row.cells){
      const card=node("div","diagnostic");card.append(node("p",null,c.text||"空白（未注明）"));
      card.append(node("p","answer-note",`${c.header_path.join(" → ")||"表头关系未定"} · 单位 ${c.unit||"未定"}（${c.unit_state}） · 跨 ${c.row_span} 行 ${c.column_span} 列${c.continuation_of?" · 同一合并格续行":""}`));
      for(const e of c.evidence){const b=node("button","text-button","定位原文");b.onclick=()=>{$("details-dialog").close();showSource(e);};card.append(b);}group.append(card);
    }items.push(group);
  }
  const details=node("details");details.append(node("summary",null,"表头依据与编号关联"),node("pre",null,JSON.stringify(data,null,2)));items.push(details);
  if(offset){const b=node("button","button","前3行");b.onclick=attempt(()=>browseSemanticTable(sid,tableId,Math.max(0,offset-3)));items.push(b);}
  if(data.has_more){const b=node("button","button","后3行");b.onclick=attempt(()=>browseSemanticTable(sid,tableId,offset+3));items.push(b);}
  const back=node("button","button","返回表格原文");back.onclick=attempt(()=>browseTable(sid,tableId));items.push(back);showDialog("表格语义候选",items);
}

async function browseDetailTrace(sid,lid,referenceId,back,offset=0){
  const gen=S.generation;
  const r=await post(`/api/drawings/${sid}/semantics/trace`,{layout_id:lid,reference_id:referenceId,offset,limit:8});
  if(gen!==S.generation)return;
  const states={candidate_key_match:"编号与图号／图框相符，待核查",candidate_sheet_unresolved:"编号相符，图号未确定",candidate_sheet_conflict:"图号冲突，待核查"};
  const content=[node("p",null,`${r.total} 个目标候选。${r.note}`)];
  for(const target of r.items){
    const card=node("div","fact-card");card.append(node("strong",null,target.title));
    card.append(node("p","answer-note",states[target.state]||target.state));
    if(target.title_state==="unresolved")card.append(node("p","answer-note","仅定位到编号圆与比例，详图标题尚未解析。"));
    const go=node("button","text-button","定位目标标记");go.onclick=()=>{if(gen!==S.generation)return;$("details-dialog").close();locate({layout_id:target.layout_id,bbox:target.bbox});};card.append(go);
    const detail=node("details");detail.append(node("summary",null,"编号、图签与原文依据"),node("pre",null,JSON.stringify(target,null,2)));card.append(detail);content.push(card);
  }
  if(offset){const previous=node("button","button","上一页");previous.onclick=attempt(()=>browseDetailTrace(sid,lid,referenceId,back,Math.max(0,offset-8)));content.push(previous);}
  if(r.has_more){const next=node("button","button","下一页");next.onclick=attempt(()=>browseDetailTrace(sid,lid,referenceId,back,offset+8));content.push(next);}
  const returnButton=node("button","button","返回索引");returnButton.onclick=attempt(back);content.push(returnButton);showDialog("详图追踪",content);
}
