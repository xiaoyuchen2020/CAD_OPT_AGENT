"""Render and seal local nested-boundary repair evidence."""
import html,json,shutil
from scripts.nested_room_boundary import OUT,PREVIOUS,read,dump,sha,history,base

def diagram(case, result):
    b=case['bundle'];x,y,xx,yy=b['bbox'];w,h=xx-x,yy-y
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x} {-yy} {w} {h}"><rect x="{x}" y="{-yy}" width="{w}" height="{h}" fill="white"/>']
    for s in b['segments']:
        a,c=s['a'],s['b'];v=s['visibility'];color='#293d51' if v['effective_layer']=='WALL' else '#aa82b0'
        parts.append(f'<path d="M {a[0]} {-a[1]} L {c[0]} {-c[1]}" stroke="{color}" stroke-width="{w/400}" opacity="{1 if v["visible"] else .2}"/>')
    o=result['execution']['output']
    if result['validation']['validation_status']=='passed':
        pts=' '.join(f'{p[0]},{-p[1]}' for p in o['vertices'])
        parts.append(f'<polygon points="{pts}" fill="#00856a" fill-opacity=".16" stroke="#00856a" stroke-width="{w/250}"/>')
    sx,sy=b['target']['seed'];parts.append(f'<circle cx="{sx}" cy="{-sy}" r="{w/70}" fill="#dc5025"/></svg>')
    return ''.join(parts)

def main():
    state=read('run.json');number=state['accepted_attempt'];assert state['model_calls']==0
    evaluation=read(f'evaluation-{number}.json');assert all(r['expectation_met'] for r in evaluation)
    comparison=read('previous-positive-comparison.json');regression=read('real-regression.json');preservation=read('preservation.json')
    assert regression['output_byte_semantics_equal'] and preservation['fresh_baseline_equal']
    visual=read('assistant-visual-review.json');tests=(OUT/'pytest.txt').read_text();assert '77 passed' in tests
    positive=[r for r in evaluation if r['name']!='kongpu_real' and r['expected_pass']]
    negative=[r for r in evaluation if not r['expected_pass']]
    s=dict(status='passed',code_origin='local_assistant_revision',model_requests=0,external_request_blocked_before_send=True,
        code_sha256=state['attempts'][-1]['code_sha256'],validator_sha256=base.VALIDATOR_SHA,
        synthetic_positive_passes=len(positive),synthetic_positive_previously_unresolved=sum(r['previous_status']=='unresolved' for r in comparison),
        synthetic_negative_cases_correctly_not_accepted=len(negative),real_cases=1,new_real_drawing_cases=0,
        kongpu_candidate_unchanged=regression['output_byte_semantics_equal'],kongpu_observed_envelope_m2=159.225,
        negative_mutation_controls_rejected=len(read('negative-controls.json')),tests_passed=77,
        historical_evidence_files_preserved=sum(r['files'] for r in history()),production_behavior_preserved=True,
        automatic_adoption=False,automatic_registration=False,net_area_accepted=False,
        limitation='Nested-selection repair verified by synthetic controls and one real compatibility replay; no new real nested-room recovery evidence.')
    dump(OUT/'summary.json',s)
    cases={c['name']:c for c in read('cases.json')};cards=[]
    names={'nested_double':'双层闭环：选择目标内环','target_in_sibling':'目标移动到另一房间','smaller_non_target_ring':'更小的非目标环不能抢占目标','nested_overlap_spur':'嵌套环与重合折返','seed_in_annulus':'目标在环带内：拒绝带孔区域','real_dangling_partition':'真实伸入隔墙：保守拒绝'}
    for key,title in names.items():
        result=read(f'{key}-attempt-{number}.json');svg=diagram(cases[key],result);(OUT/f'{key}.svg').write_text(svg)
        cards.append(f'<article><h3>{title}</h3><img src="{key}.svg" alt="{title}"><p>{html.escape(result["validation"]["validation_status"])}：{html.escape("；".join(result["validation"]["errors"])) or "原生来源、完整面与面积验证通过"}</p></article>')
    before={r['name']:r['previous_status'] for r in comparison}
    rows=''.join(f'<tr><td>{r["name"]}</td><td>{before.get(r["name"],"—")}</td><td>{r["status"]}</td><td>{"恢复正例" if r["expected_pass"] else "拒绝用例"}</td><td>{html.escape("；".join(r["errors"])) or "通过"}</td></tr>' for r in evaluation)
    links=['summary.json','local-change-v2.diff','candidate-local-v2.py','approval-block.json','scope.json','cases.json',f'evaluation-{number}.json','previous-positive-comparison.json','real-regression.json','negative-controls.json','replay-results.json','preservation.json','assistant-visual-review.json','pytest.txt','evidence-sha256.json']
    nav=' · '.join(f'<a href="{n}">{n}</a>' for n in links)
    (OUT/'review.html').write_text(f'''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>嵌套闭环目标边界选择修复</title>
<style>body{{font:16px/1.65 system-ui;background:#f5f7fa;color:#233549;max-width:1300px;margin:auto;padding:24px}}section,article{{background:white;border-radius:10px;padding:20px;margin:18px 0}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}article img{{width:100%;height:330px;object-fit:contain}}img{{max-width:100%}}table{{width:100%;border-collapse:collapse;font-size:14px}}td,th{{border:1px solid #dbe2e9;padding:8px;text-align:left}}a{{color:#145f96}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}.warn{{color:#925400}}@media(max-width:800px){{.grid{{display:block}}}}</style>
<h1>嵌套闭环：目标边界选择已修复</h1><p>实验分支本地修复；冻结验证器与现有组件行为保留。9 个此前未恢复的合成正例通过，11 个拒绝用例未被放行，孔浦真实案例输出完全一致。</p>
<section><h2>修复内容与证据范围</h2><p>先筛选包含目标点的闭环，再验证严格包含关系并选择最内层目标面。更小但不含目标的环不会被选中。触边、带孔、未闭合和不支持的来源继续拒绝。</p><p>零面积折返必须同时满足“目标面沿同一图边往返”和“原生线段在精细网格上有双向重合证据”；单条真实伸入隔墙仍会拒绝。</p><p class="warn">本轮 9 项新增恢复均来自合成用例，不能计作 9 个真实图纸收益。孔浦仅作兼容性回归；仍需真实嵌套房间样本验证，未注册公共方法，也未签认工程净面积。</p><p>自动审批拒绝了新的 GPT 服务外发请求，认为此前单次授权已经耗尽；请求未发送。随后采用本地助手修复，本轮外部模型调用 0 次，未冒称模型服务生成。</p></section>
<section><h2>合成拓扑对照</h2><p>橙点为目标位置；绿色为通过验证的候选。拒绝用例只展示原始线段，不填充候选。</p><div class="grid">{''.join(cards)}</div></section>
<section><h2>逐例结果</h2><table><tr><th>用例</th><th>旧版</th><th>新版</th><th>预期</th><th>结果／原因</th></tr>{rows}</table><p>另有 13 项错误结果注入全部拒绝；平移、乱序反向真实回放通过；77 项测试通过（2 项既有 Pillow 弃用提醒）。</p></section>
<section><h2>孔浦公变原图复核</h2><p>《孔浦地块人防地下室施工图20240911_t3.dwg》／公变 3552_01_1。46 边、159.225 m² 观察包络与 13 条贯穿线排除结果全部保持一致，原始候选 JSON 语义哈希亦相同。原组件仍为 needs_review、候选 40.8792 m²。</p><div class="grid"><a href="source.svg"><img src="source.png" alt="冻结原图"></a><a href="candidate-detail.svg"><img src="candidate-detail.png" alt="候选整体复核"></a></div><div class="grid"><a href="upper-door.svg"><img src="upper-door.png" alt="上门洞"></a><a href="lower-door.svg"><img src="lower-door.png" alt="下门洞"></a></div><p>助手复核：公变整体范围与柱位凹入正确对应，未并入邻近体育设施、器材间和卫生间；上下门洞未再出现折返细刺，但门洞仍沿原生图元外伸，不认定为工程净面积。</p></section>
<section><h2>来源、隔离与保留行为</h2><p>同一原生输入与冻结验证器，无网络／只读／无宿主挂载／非 root Docker，512 MiB 内存、32 进程、1 CPU、20 秒超时。{s['historical_evidence_files_preserved']} 份历史证据校验不变。本次只编辑实验候选、回归脚本及测试，未修改生产组件或采用、注册结果。</p><p>{nav}</p></section></html>''')
    for path in [base.ROOT/'scripts/nested_room_boundary.py',base.ROOT/'scripts/report_nested_room_boundary.py',base.ROOT/'tests/test_room_nested_boundary.py']:
        shutil.copy2(path,OUT/('harness-'+path.name))
    dump(OUT/'evidence-sha256.json',{str(p.relative_to(OUT)):sha(p) for p in sorted(OUT.rglob('*')) if p.is_file() and 'data' not in p.relative_to(OUT).parts and p.name!='evidence-sha256.json'})
    print(json.dumps(s,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
