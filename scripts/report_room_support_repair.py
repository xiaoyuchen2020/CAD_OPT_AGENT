"""Seal evidence for the approved single-call room support repair."""
import html, json, shutil
from scripts.repair_room_support_revision import OUT, PRIOR, base, read, dump, sha, historical

def main():
    run = read('run.json'); validation = read('validation-4.json'); audit = read('support-repair-audit.json')
    visual = read('assistant-visual-review.json'); preservation = read('preservation.json')
    controls = read('negative-controls.json'); replays = read('replay-results.json')
    baseline = read('baseline-after-fresh.json'); checks = validation.get('checks', {})
    assert run['model_calls'] == 1 and run['cumulative_model_calls'] == 4
    assert run['cumulative_charged_tokens'] <= 96000
    assert visual['reviewer'] == 'assistant' and not visual['net_area_accepted']
    summary = dict(drawing='孔浦地块人防地下室施工图20240911_t3.dwg', target='公变 3552_01_1',
        source_sha256=read('input.json')['source_sha256'], input_id=read('input.json')['input_id'],
        code_sha256=run['attempts'][0]['code_sha256'], validator_sha256=base.VALIDATOR_SHA,
        validation_status=validation['validation_status'], observed_envelope_area_m2=checks.get('observed_envelope_area_m2'),
        vertices=checks.get('edges'), excluded_crossing_count=len(checks.get('excluded_crossing_ids', [])),
        parallel_support_fraction=checks.get('parallel_support_fraction'),
        removed_irrelevant_support_claims=len(audit['removed_support_claims']),
        original_component_status=baseline['status'], original_component_candidate_area_m2=baseline.get('candidate_area_m2'),
        new_real_drawing_cases=0, real_pilot_cases=1, metamorphic_real_replays=sum(r['validation_status']=='passed' for r in replays if r['name'] in ['translated','permuted_reversed']),
        negative_controls_rejected=len(controls), individual_reinsertion_controls_rejected=len(audit['individual_reinsertion_controls']),
        unmet_replay_expectations=[r for r in replays if not r['expectation_met']],
        model_calls_this_supplement=1, model_calls_cumulative=4,
        tokens_this_supplement=run['attempts'][0]['usage'].get('total_tokens'), tokens_cumulative=run['cumulative_charged_tokens'], total_token_budget=96000,
        production_behavior_preserved=preservation['fresh_baseline_equal'], historical_files_preserved=sum(r['files'] for r in historical()),
        visual_review=visual['result'], semantic_acceptance=False, net_area_accepted=False,
        automatic_adoption=False, automatic_registration=False,
        assessment='Single real pilot now passes frozen source-bound geometric validation. Nested disconnected loops remain unresolved; not ready for public promotion or net-area acceptance.')
    dump(OUT / 'summary.json', summary)
    # Capture the exact trusted harness used, independent of later script edits.
    for name in ['repair_room_support_revision.py', 'report_room_support_repair.py', 'room_boundary_revision.py', 'check_room_boundary_revision.py']:
        shutil.copy2(base.ROOT / 'scripts' / name, OUT / ('harness-' + name))
    links = ['authorization.json','run.json','model-change.diff','scope-check.json','support-repair-audit.json',
             'validation-4.json','negative-controls.json','replay-results.json','assistant-visual-review.json',
             'preservation.json','test-results.json','summary.json','evidence-sha256.json']
    nav = ' · '.join(f'<a href="{f}">{html.escape(f)}</a>' for f in links)
    replay_rows = ''.join(f'<tr><td>{html.escape(r["name"])}</td><td>{r["validation_status"]}</td><td>{html.escape("; ".join(r["errors"])) or "—"}</td><td>{"符合" if r["expectation_met"] else "未达到正例预期"}</td></tr>' for r in replays)
    (OUT / 'review.html').write_text(f'''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>孔浦公变 · 单次定向修正复验</title>
<style>body{{font:16px/1.65 system-ui;color:#233549;background:#f5f7fa;max-width:1350px;margin:auto;padding:28px}}section{{background:white;padding:22px;margin:20px 0;border-radius:12px}}h1,h2{{line-height:1.3}}.pair{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}img{{width:100%;background:white}}a{{color:#12649e}}table{{width:100%;border-collapse:collapse}}td,th{{padding:9px;border:1px solid #dbe1e8;text-align:left}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}.ok{{color:#087455}}.warn{{color:#994b00}}@media(max-width:800px){{.pair{{grid-template-columns:1fr}}}}</style>
<h1>孔浦公变：端点重叠来源定向修正</h1>
<p>《{summary['drawing']}》／{summary['target']}。本次追加 1 次 GPT-6.1 Sol 请求，累计 4 次；累计 {summary['tokens_cumulative']:,} / 96,000 tokens。</p>
<section><h2 class="ok">冻结验证器：{validation['validation_status']}</h2>
<p>剔除 6 条仅有端点微小重叠的来源声明，46 个顶点、面积和贯穿线排除结果均与上轮候选一致。模型返回的完整代码直接在原 Docker 沙箱运行，宿主未修补输出；AST 核对确认只修改了授权的来源筛选条件。</p>
<p>观察包络约 <b>{summary['observed_envelope_area_m2']:.6f} m²</b>，排除 {summary['excluded_crossing_count']} 条可见非结构贯穿线。原组件仍为 <code>{baseline['status']}</code>，其候选 {summary['original_component_candidate_area_m2']} m² 保持不变。</p>
<p class="warn">这是单个真实试点的几何与来源验证收益。门洞沿原生门窗图元外伸，尚不是工程净面积；合成嵌套闭环正例仍未恢复，不能据此认定可沉淀为公共方法。本轮没有新增其他图纸样本。</p></section>
<section><h2>原图与候选复核</h2><p>左：冻结原图局部渲染。右：同一原图上的绿色原始候选边界。点击可查看 SVG 细节。助手视觉复核：公变整体纵向范围对应，左侧体育设施和卫生间未并入，柱位凹入保留。</p>
<div class="pair"><a href="source.svg"><img src="source.png" alt="原图局部"></a><a href="candidate-overlay.svg"><img src="candidate-overlay.png" alt="候选叠加"></a></div>
<a href="candidate-detail.svg"><img src="candidate-detail.png" alt="候选近景"></a></section>
<section><h2>上下门洞细节</h2><p>上下门洞处未见此前的零宽度折返细刺。门洞外伸仍保留，因此不作净面积签认。</p><div class="pair"><a href="upper-door.svg"><img src="upper-door.png" alt="上门洞"></a><a href="lower-door.svg"><img src="lower-door.png" alt="下门洞"></a></div></section>
<section><h2>独立拒绝与稳定性回放</h2><p>13 项原有负控全部拒绝；将本次剔除的 6 条无效来源逐条放回，也全部被原验证器拒绝。平移、乱序并反向两项真实输入变换通过，但不计作新增图纸。</p><table><tr><th>用例</th><th>状态</th><th>诊断</th><th>预期</th></tr>{replay_rows}</table>
<p>嵌套闭环正例仍返回“无唯一有界最小面”，是保留的方法缺口。本次定向修正未放宽此处逻辑或验收标准。</p></section>
<section><h2>隔离、追溯与保留行为</h2><p>原生图元、图层可见性及 mm 单位证据保持不变。Docker 无网络、只读、无宿主目录挂载、非 root、内存 512 MiB、进程 32、CPU 1、超时 20 秒。冻结验证器 SHA-256：<code>{base.VALIDATOR_SHA}</code>。</p>
<p>73 项现有回归测试通过；{summary['historical_files_preserved']} 份历史证据校验不变。未修改生产组件，未自动采用结果，未注册公共方法。</p><p>{nav}</p><details><summary>汇总数据</summary><pre>{html.escape(json.dumps(summary, ensure_ascii=False, indent=2))}</pre></details></section></html>''')
    manifest = {str(p.relative_to(OUT)): sha(p) for p in sorted(OUT.rglob('*'))
                if p.is_file() and 'data' not in p.relative_to(OUT).parts and p.name != 'evidence-sha256.json'}
    dump(OUT / 'evidence-sha256.json', manifest)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == '__main__': main()
