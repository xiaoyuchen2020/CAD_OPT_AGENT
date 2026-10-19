"""Local, reproducible before/after source audit. Never calls an external model.

python -m scripts.compare_drawings --root artifacts/real-validation-20261014
The root must contain an immutable before/ capture and samples.json.
"""

import argparse
from hashlib import sha256
import html
import json
from pathlib import Path
import subprocess
import sys
import re
import ezdxf
from cad_agent.building.masonry import material_matches


def read(path):
    return json.loads(path.read_text())


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def capture(path):
    facts = read(path / "facts.json")
    snapshot = path / "data/drawings" / facts["snapshot_id"]
    return (
        facts,
        {e["entity_id"]: e for e in read(snapshot / "entities.json")},
        {r["region_id"]: r for r in read(snapshot / "regions.json")},
    )


def handles(fact, evidence, entities):
    quote = re.sub(r"[\s|]+", "", fact["original_text"])
    result = set()
    for eid in fact["evidence_ids"][:1]:
        for entity_id in evidence[eid]["entity_ids"]:
            entity = entities[entity_id]
            if any(
                re.sub(r"\s+", "", m.group()) in quote
                for m in material_matches(entity["text"])
            ):
                result.add(tuple(entity["entity_path"]))
    return result


def values(fact):
    return {
        k: v["value"]
        for k, v in fact["attributes"].items()
        if v["value"] is not None and v["value"] != []
    }


def audit(facts, entities, regions):
    count = 0
    for e in facts["evidence"]:
        source = regions[e["region_id"]]
        a, b = e["text_span"]
        assert source["text"][a:b] == e["quote"]
        parts = [
            p
            for p in source["parts"]
            if p["text_span"][0] < b and p["text_span"][1] > a
        ]
        assert e["entity_ids"] == list(dict.fromkeys(p["entity_id"] for p in parts))
        for p in parts:
            x, y = p["text_span"]
            u, v = p["entity_text_span"]
            assert source["text"][x:y] == entities[p["entity_id"]]["text"][u:v]
            assert p["bbox"] == entities[p["entity_id"]]["bbox"]
            count += 1
        assert parts and e["source_parts"] == parts
        assert e["component_boxes"] == [p["bbox"] for p in parts]
    valid = {e["evidence_id"] for e in facts["evidence"]}
    for f in facts["practices"]:
        for v in f["attributes"].values():
            assert set(v["evidence_ids"]) <= set(f["evidence_ids"]) <= valid
    return count


def crop(fact, evs, entities, path):
    ids = list(
        dict.fromkeys(i for eid in fact["evidence_ids"] for i in evs[eid]["entity_ids"])
    )
    selected = [entities[i] for i in ids]
    # Readable source-coordinate reconstruction, NOT a native CAD screenshot.
    # Original anchors and per-entity text remain intact; font advances estimated
    # and capped at the next native insertion to avoid substituted-font overlap.
    xmin = min(e["origin"][0] for e in selected)
    ymax = max(e["origin"][1] + e["height"] * 1.3 for e in selected)
    ymin = min(e["origin"][1] - e["height"] * 0.4 for e in selected)
    widths = {}
    for e in selected:
        width = sum(e["height"] * (0.72 if ord(c) > 255 else 0.48) for c in e["text"])
        nexts = [
            n["origin"][0] - e["origin"][0]
            for n in selected
            if n["origin"][0] > e["origin"][0]
            and abs(n["origin"][1] - e["origin"][1])
            < min(n["height"], e["height"]) * 0.35
        ]
        widths[e["entity_id"]] = min([width] + [n * 0.97 for n in nexts])
    xmax = max(e["origin"][0] + widths[e["entity_id"]] for e in selected)
    margin = max(e["height"] for e in selected)
    w, h = xmax - xmin + 2 * margin, ymax - ymin + 2 * margin
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="1500" height="{max(100,1500*h/w)}"><rect width="100%" height="100%" fill="#fff"/>'
    ]
    for e in selected:
        x = e["origin"][0] - xmin + margin
        y = ymax - e["origin"][1] + margin
        label = "/".join(e["entity_path"])
        body.append(
            f'<g><title>{html.escape(label+": "+e["text"])}</title><text x="{x}" y="{y}" font-size="{e["height"]}" font-family="Arial Unicode MS, sans-serif" textLength="{widths[e["entity_id"]]}" lengthAdjust="spacingAndGlyphs" fill="#17332d">{html.escape(e["text"])}</text></g>'
        )
    body.append("</svg>")
    path.write_text("".join(body))
    return [
        {
            "entity_path": e["entity_path"],
            "text": e["text"],
            "origin": e["origin"],
            "bbox": e["bbox"],
        }
        for e in selected
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--reuse-after", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    samples = read(root / "samples.json")
    results = []
    all_rows = []
    html_sections = []
    for sample in samples:
        sid = sample["sample_id"]
        source = Path(sample["source_path"])
        assert sha256(source.read_bytes()).hexdigest() == sample["sha256"]
        out = root / "after" / sid
        if not args.reuse_after:
            # Each audit uses a fresh capture, not a stale local cache.
            if out.exists():
                raise ValueError(f"{out} exists; choose a fresh root or --reuse-after")
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.check_drawing",
                    str(source),
                    "--output",
                    str(out),
                ],
                check=True,
            )
        before, be, br = capture(root / "before" / sid)
        after, ae, ar = capture(out)
        assert (
            after["manifest"]["input_sha256"] == sample["sha256"]
            if "input_sha256" in after["manifest"]
            else True
        )
        checks = audit(after, ae, ar)
        doc = ezdxf.readfile(source)
        native_verified = 0
        native_unverified = []
        used_ids = {i for e in after["evidence"] for i in e["entity_ids"]}
        for entity_id in used_ids:
            entity = ae[entity_id]
            path = entity["entity_path"]
            native = doc.entitydb.get(path[-1])
            if len(path) != 1 or native is None or native.dxftype() != "TEXT":
                native_unverified.append(path)
                continue
            original = native.dxf.text
            original = re.sub(
                r"\\U\+([0-9A-Fa-f]{4,8})", lambda m: chr(int(m[1], 16)), original
            )
            for code, value in (("p", "±"), ("c", "Φ"), ("d", "°")):
                original = re.sub("%%" + code, value, original, flags=re.I)
            assert original.replace("\x00", "").strip() == entity["text"]
            assert all(
                abs(float(native.dxf.insert[i]) - entity["origin"][i]) < 1e-6
                for i in (0, 1)
            )
            native_verified += 1
        bev = {e["evidence_id"]: e for e in before["evidence"]}
        aev = {e["evidence_id"]: e for e in after["evidence"]}
        previous = [handles(p, bev, be) for p in before["practices"]]
        current = [handles(p, aev, ae) for p in after["practices"]]
        matched = set()
        rows = []
        snippets = []
        notes = (
            read(root / "review-notes.json").get(sid, {})
            if (root / "review-notes.json").exists()
            else {}
        )
        crops = out / "evidence"
        crops.mkdir(exist_ok=True)
        for i, p in enumerate(after["practices"]):
            # Match only material-bearing native text to avoid a shared heading
            # spuriously matching all practices in its section.
            material_sources = current[i]
            matches = [j for j, hs in enumerate(previous) if material_sources & hs]
            matched.update(matches)
            filename = f"fact-{i+1:03}.svg"
            native = crop(p, aev, ae, crops / filename)
            row = {
                "sample_id": sid,
                "after_index": i + 1,
                "before_indices": [j + 1 for j in matches],
                "before": [before["practices"][j] for j in matches],
                "after": p,
                "native_entities": native,
                "evidence_check": "exact_native_spans_verified",
                "agent_review_note": notes.get(str(i + 1), ""),
                "semantic_status": (
                    "review_required"
                    if p["review_status"] == "needs_review"
                    else "source_bound_not_formally_accepted"
                ),
            }
            rows.append(row)
            old = (
                "\n\n".join(
                    f'B{j+1}: {before["practices"][j]["original_text"]}\n{json.dumps(values(before["practices"][j]),ensure_ascii=False)}'
                    for j in matches
                )
                or "修正前无对应记录"
            )
            new = (
                p["original_text"]
                + "\n"
                + json.dumps(values(p), ensure_ascii=False, indent=2)
            )
            caveats = (
                "；".join(p["conflicts"] + p["missing_fields"])
                or "来源已绑定；非正式验收"
            )
            table = "".join(
                "<tr><td>"
                + html.escape("/".join(n["entity_path"]))
                + "</td><td>"
                + html.escape(n["text"])
                + "</td><td>"
                + html.escape(str([round(v, 2) for v in n["origin"]]))
                + "</td></tr>"
                for n in native
            )
            snippets.append(
                f'<article id="{sid}-{i+1}"><h3>{sid} · F{i+1:02} ← '
                + ", ".join("B" + str(j + 1) for j in matches)
                + f'</h3><p class="note">{html.escape(notes.get(str(i+1), "") + " | " + caveats)}</p><div class="compare"><pre>{html.escape(old)}</pre><pre>{html.escape(new)}</pre></div><details><summary>原始实体与坐标证据（{len(native)} 个实体）</summary><img loading="lazy" src="after/{sid}/evidence/{filename}" alt="原坐标文字重建，字体宽度估算"/><table><tr><th>原始实体路径</th><th>原文</th><th>插入坐标</th></tr>{table}</table></details></article>'
            )
        unmatched = [j + 1 for j in range(len(previous)) if j not in matched]
        for index in unmatched:
            p = before["practices"][index - 1]
            rows.append(
                {
                    "sample_id": sid,
                    "before_indices": [index],
                    "before": [p],
                    "after": None,
                    "semantic_status": "unmatched_requires_review",
                }
            )
            snippets.append(
                f'<article><h3>{sid} · B{index} 无后续对应记录（需核查）</h3><pre>{html.escape(p["original_text"])}</pre></article>'
            )
        assert set(j for row in rows for j in row["before_indices"]) == set(
            range(1, len(previous) + 1)
        )
        summary = {
            "sample_id": sid,
            "before": len(previous),
            "after": len(current),
            "before_accounted_for": len(previous),
            "unmatched_before": unmatched,
            "native_part_checks": checks,
            "direct_native_text_entities_verified": native_verified,
            "direct_native_unverified_paths": native_unverified,
            "source_unchanged": sha256(source.read_bytes()).hexdigest()
            == sample["sha256"],
            "needs_review": sum(
                p["review_status"] == "needs_review" for p in after["practices"]
            ),
        }
        results.append(summary)
        all_rows.extend(rows)
        html_sections.append(
            f"<section><h2>{sid}：{len(previous)} → {len(current)} 条记录</h2>"
            + "".join(snippets)
            + "</section>"
        )
    write(root / "comparison.json", all_rows)
    write(root / "audit-summary.json", results)
    page = (
        """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>真实 DXF 抽取修正对比</title><style>
body{font:15px/1.65 system-ui,sans-serif;background:#f5f7f6;color:#20372f;margin:0;padding:40px}main{max-width:1400px;margin:auto}h1{font-size:34px}article{background:white;border:1px solid #dbe3df;border-radius:12px;margin:22px 0;padding:24px}.compare{display:grid;grid-template-columns:1fr 1fr;gap:20px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7f6;padding:18px;border-radius:8px;font:13px/1.8 system-ui}.note{color:#88652f}img{width:100%;min-height:80px;margin:20px 0}table{width:100%;border-collapse:collapse;font-size:13px}td,th{border-bottom:1px solid #dde5e0;text-align:left;padding:8px}input{padding:12px;width:420px;max-width:90%;font:inherit}summary{cursor:pointer}header{border-bottom:1px solid #cdd9d1;padding-bottom:24px} @media(max-width:850px){.compare{grid-template-columns:1fr}body{padding:16px}}</style><main><header><p>CAD / ATLAS · GENERIC MASONRY · 2026-10-14</p><h1>真实 DXF · 修正前后逐条对比</h1><p>左侧修正前，右侧修正后。按原始实体路径关联，保留合并、拆分、新增与未匹配记录；不是按事实数量计算准确率。</p><p>4 份历史样本来自两组相关图纸，非独立留出集。所有来源在本地验证；未接入 AR024 或工程优化策略，未调用远端模型。字体缺失，附图为原始坐标文字重建（字宽估算），不是原生 CAD 截图；以实体原文、路径、坐标和源文件哈希核查。多角色或材料备选、未知单位和未确认图框仍需复核。</p><input id="q" placeholder="搜索文字、材料、范围或样本编号…"><p>证据校验只证明来源绑定，不等于语义或工程验收。</p></header>"""
        + "".join(html_sections)
        + """</main><script>document.querySelector('#q').oninput=e=>{const q=e.target.value.toLowerCase();document.querySelectorAll('article').forEach(a=>a.hidden=!a.textContent.toLowerCase().includes(q))}</script></html>"""
    )
    (root / "comparison.html").write_text(page)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
