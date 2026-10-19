"""Report real recovery evidence separately from existing component alternatives."""
import html,json,shutil
from collections import Counter
from scripts.validate_nested_real_rooms import OUT,ROOT,read,dump,sha,history,VALIDATOR_SHA

def main():
    rows=read('results.json');screen=read('screening.json');visual=read('assistant-visual-review.json');preservation=read('preservation.json')
    gain=[r for r in rows if r['nested_repair_gain']];assert len(gain)==1
    summary=dict(drawings_screened=len({r['paper_id'] for r in screen}),screening_records=len(screen),
        screening_count_note='Includes older partial snapshots, later complete snapshots and exact-scope retries; not independent room count.',
        screening_statuses=dict(Counter(r['screen_status'] for r in screen)),
        replayed_regions=len(rows),replayed_drawings=len({r['case']['source_sha256'] for r in rows}),
        old_statuses=dict(Counter(r['old']['validation_status'] for r in rows)),new_statuses=dict(Counter(r['new']['validation_status'] for r in rows)),
        repair_specific_new_recoveries=len(gain),new_geometry_absent_from_existing_component_alternatives=sum(r['component_geometry_gain_candidate'] for r in rows),
        gain_cases=[dict(key=r['case']['key'],drawing=r['case']['dwg_name'],label=r['case']['label'],area_m2=r['new']['checks']['observed_envelope_area_m2'],
            existing_component_selected_area_m2=r['baseline_candidate_area_m2'],existing_component_has_similar_alternative=r['baseline_contains_same_geometry'],
            old_errors=r['old']['errors']) for r in gain],
        visual_reviewed_regions=len(visual['records']),visual_scope='Two nested wind-shaft regions and three controls; other technical passes have not all received visual acceptance.',
        negative_controls=len(read('negative-controls.json')),tests_passed=77,
        source_and_parsed_hashes_verified=True,fresh_component_baselines_unchanged=len(preservation['fresh_baselines']),
        historical_files_preserved=sum(r['files'] for r in history()),
        model_calls=0,code_modified_this_round=False,validator_sha256=VALIDATOR_SHA,
        automatic_adoption=False,automatic_registration=False,net_area_accepted=False,
        promotion_assessment='Not ready: one drawing supplies repair-specific real evidence; the component already enumerates a similar correct alternative; independent drawings remain blocked by input evidence.')
    dump(OUT/'summary.json',summary)
    gain_key=gain[0]['case']['key'];compat='11816-native-061-full-precise'
    cards=[]
    for key,title,description in [(gain_key,'新增恢复：风井 1.305 m²','旧实验代码因 No unique bounded minimal face 未解决；新版通过。原组件选中 1.125 m²，另有 1.305 m² 的配对墙备选。新增收益是完整目标面的选择与验证，并非首次枚举该几何。'),
                                   (compat,'兼容性对照：另一图示位置的风井','旧版、新版均通过，都是 1.305 m²；不计本次修复新增，也不当作独立图纸。原组件选中 1.276 m²，另有 1.305 m² 备选。')]:
        cards.append(f'<section><h2>{title}</h2><p><code>{key}</code> · {description}</p><p>左：无叠加原图。右：绿色为新版完整候选，紫色虚线为公共组件当前选中候选。助手复核确认中间风井与原图四边对应，未并入左右强电井、水井。</p><div class="pair"><a href="figures/{key}-source.svg"><img src="figures/{key}-source.png" alt="风井原图"></a><a href="figures/{key}-comparison.svg"><img src="figures/{key}-comparison.png" alt="新候选和公共组件选中结果对照"></a></div><p><a href="results/{key}.json">新旧验证结果</a> · <a href="baselines/{key}.json">原组件完整候选清单</a> · <a href="scope-audits/{key}.json">精确范围剔除证明</a></p></section>')
    drawings=[]
    for pid in sorted({r['paper_id'] for r in screen}):
        rs=[r for r in screen if r['paper_id']==pid];counts=Counter(r['screen_status'] for r in rs)
        drawings.append(f'<tr><td>{pid}</td><td>{html.escape(rs[0]["dwg_name"])}</td><td>{len(rs)}</td><td>{html.escape(json.dumps(counts,ensure_ascii=False))}</td></tr>')
    result_rows=[]
    for r in rows:
        key=r['case']['key'];label='本次修复新增' if r['nested_repair_gain'] else '旧版已有／兼容' if r['new']['validation_status']=='passed' else '仍未通过'
        result_rows.append(f'<tr><td><a href="results/{key}.json">{key}</a></td><td>{html.escape(r["case"]["label"])}</td><td>{r["old"]["validation_status"]}</td><td>{r["new"]["validation_status"]}</td><td>{r["new"].get("checks",{}).get("observed_envelope_area_m2","—")}</td><td>{label}</td></tr>')
    links=['summary.json','scope.json','screening.json','global-discovery-reproduced.json','component-comparison.json','assistant-visual-review.json','source-binding-audit.json','negative-controls.json','preservation.json','pytest.txt','candidate-old.py','candidate-new.py','validator-frozen.py','evidence-sha256.json']
    nav=' · '.join(f'<a href="{p}">{p}</a>' for p in links)
    (OUT/'review.html').write_text(f'''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>真实嵌套风井 · 修复收益验证</title><style>body{{font:16px/1.65 system-ui;color:#243349;background:#f5f7fa;max-width:1400px;margin:auto;padding:26px}}section{{background:white;border-radius:12px;margin:22px 0;padding:24px}}.pair{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}img{{width:100%}}table{{width:100%;border-collapse:collapse;font-size:13px}}td,th{{padding:8px;text-align:left;border:1px solid #dae2e9;overflow-wrap:anywhere}}a{{color:#125e98}}.warn{{color:#925400}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}@media(max-width:850px){{.pair{{display:block}}}}</style>
<h1>真实图纸验证：确认 1 处嵌套选择修复收益</h1><p>《孔浦地块人防地下室施工图20240911_t3.dwg》风井，4 边、1.305 m²。旧实验代码未解决，新版通过同一冻结验证器，原图已由助手复核。</p>
<section><h2>收益与边界</h2><p>7 张图纸共形成 261 条筛查记录，含早期截断快照、完整快照复筛及局部范围复核，不等于 261 个独立房间。26 个满足执行前提的真实局部来自孔浦同一张图纸；旧版通过 19 个，新版通过 20 个，新增 1 个，其余旧版通过结果均保留。</p><p class="warn">现有公共组件已经枚举出与新增恢复相近的 1.305 m² 配对墙候选，但当前选中 1.125 m² 局部并返回 needs_review。因此，本轮新增的是目标边界选择与独立验证收益，公共组件此前完全没有枚举的新增几何为 0。不能把这次结果解释成新增工程净面积能力。</p><p>本轮模型调用 0 次；旧、新实验代码及冻结验证器均未修改。现有组件行为保留，未自动采用或注册公共方法。</p></section>
{''.join(cards)}
<section><h2>为何允许移除两条范围外线段</h2><p>两处原局部输入均因 XCLIP 未知状态被拒绝。追溯发现，仅有两条中心线的外包框与范围相交，原生线段本身分别距离目标约 85 米／32 米。精确检查证明它们与“局部范围＋原输入容差”完全不相交；裁剪只能删除线段部分，不能将其移动进范围。</p><p>新旧代码对照使用同一份精确范围输入：只剔除有不相交证明的线段，保留原生端点、图层状态、单位证据及所有可能相交的图元。未把未知裁剪标为支持，未清除范围内诊断。恢复原粗筛输入后，冻结验证器仍按 UNSUPPORTED_VISIBILITY 拒绝。范围收集的这项修正仅存在于本轮实验输入准备脚本，没有修改公共组件。</p></section>
<section><h2>按图纸筛查结果</h2><p>另有 15 张历史图纸的单位声明不满足当前冻结毫米验证契约，未作单位覆盖。下表中的输入问题、非嵌套对照和未闭合／带孔区域不计新增恢复；本轮没有把设备房或风井标签自动等同于可靠边界。</p><table><tr><th>图纸</th><th>DWG 名称</th><th>筛查记录</th><th>分类</th></tr>{''.join(drawings)}</table></section>
<section><h2>26 个局部的新旧代码对照</h2><table><tr><th>局部</th><th>原文</th><th>旧版</th><th>新版</th><th>观察面积 m²</th><th>归类</th></tr>{''.join(result_rows)}</table><p>技术通过的 20 处并非全部完成视觉验收。本轮视觉复核覆盖两处嵌套风井及 3 个对照；集气室候选仍被 NOT_COMPLETE_MINIMAL_STRUCTURAL_FACE 拒绝，不因视觉近似而放行。</p></section>
<section><h2>验证、保留行为与后续判断</h2><p>20 处通过候选的原生端点、源文件和解析工件哈希复核完成；20 组公共组件前后重新计算一致。4 项定向负控均拒绝，77 项现有测试通过。{summary['historical_files_preserved']} 份历史证据校验不变。沙箱继续使用无网络、只读、无主机挂载、非 root 及既有限额。</p><p>目前仍只有一张图纸贡献嵌套修复的真实正例，暂不具备公共方法沉淀证据。下一步应优先补齐另一张图纸的可靠单位、代理图元与裁剪证据，再检验完整目标选择是否稳定；不能用放宽验证要求换取通过数。</p><p>{nav}</p></section></html>''')
    for name in ['validate_nested_real_rooms.py','review_nested_real_rooms.py','report_nested_real_rooms.py']:
        shutil.copy2(ROOT/'scripts'/name,OUT/('harness-'+name))
    dump(OUT/'evidence-sha256.json',{str(p.relative_to(OUT)):sha(p) for p in sorted(OUT.rglob('*')) if p.is_file() and 'data' not in p.relative_to(OUT).parts and p.name!='evidence-sha256.json'})
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
