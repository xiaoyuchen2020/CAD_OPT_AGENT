"""Frozen v5 evidence report; never selects configurations on check outcomes."""
import json,html
from scripts.floorplancad_dev_v5_data import OUT,ROOT,sha,save
from scripts.floorplancad_dev_v4_report import paired,instances
LABELS={'baseline':'既有 v3 基线','v4_combined':'上一轮组合（本轮重算）','door_parts_only':'只补全部件，无宿主硬约束','door_parts_host_hard':'只补全部件，要求宿主','window_inner_only':'只增加内部窗轨约束','decoupled_no_veto':'拆开门补全，不筛除窗','full':'内部窗轨＋独立门补全','full_host_hard':'完整方案＋门宿主硬约束','full_with_through_veto':'完整方案＋两端延续否决','full_with_crossing_veto':'完整方案＋内部穿越否决','full_with_both_veto':'完整方案＋两种否决'}
def pct(x):return '—' if x is None else f'{x*100:.1f}%'
def table(totals,names=None):
    out=['<table><tr><th>配置</th><th>类别</th><th>TP / FP / FN</th><th>精确率</th><th>召回率</th><th>F1</th><th>墙误判为窗</th><th>不完整样本</th></tr>']
    for name in names or totals:
        t=totals[name]
        for cls,m in t['micro'].items():out.append(f'<tr><td>{LABELS[name]}</td><td>{cls}</td><td>{m["tp"]} / {m["fp"]} / {m["fn"]}</td><td>{pct(m["precision"])}</td><td>{pct(m["recall"])}</td><td>{pct(m["f1"])}</td><td>{t["wall_as_window_fp"] if cls=="window" else "—"}</td><td>{t["incomplete_samples"]}</td></tr>')
    return ''.join(out)+'</table>'
def run():
    a=json.loads((OUT/'analysis_results.json').read_text());b=json.loads((OUT/'fresh_checks_results.json').read_text());seal=json.loads((OUT/'selection_seal.json').read_text());protocol=json.loads((OUT/'protocol.json').read_text())
    assert seal['analysis_sha256']==sha((OUT/'analysis_results.json').read_bytes()) and seal['freeze_sha256']==sha((OUT/'candidate_freeze.json').read_bytes())
    assert min(p.stat().st_mtime for p in (OUT/'progress/fresh_checks').glob('*.json'))>=seal['sealed_unix']
    selected=seal['selected_variant'];component=seal['door_component_variant'];names=list(dict.fromkeys(['baseline','v4_combined','full']+([selected] if selected else [])+([component] if component else [])))
    comparisons={phase:paired(r['sample_results']) for phase,r in [('analysis',a),('fresh_checks',b)]};save(OUT/'family_comparison.json',comparisons)
    parts=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>FloorPlanCAD v5：墙窗混淆与门扇补全</title><style>body{font:16px system-ui;max-width:1320px;margin:36px auto;padding:0 24px;background:#f8fafc;color:#182a39}table{border-collapse:collapse;width:100%;background:white;margin:16px 0;font-size:14px}td,th{border:1px solid #d4dfe7;padding:8px;text-align:right}th{background:#e5eef5}td:first-child,th:first-child{text-align:left}.box{padding:18px;background:#eaf2f8;border-left:4px solid #367b9f}details{margin:20px 0}code{overflow-wrap:anywhere}a{color:#145f91}</style><h1>墙窗混淆与门扇部件补全实验</h1>']
    parts.append(f'<p class="box">分析阶段封存的整套方案：<b>{LABELS.get(selected,"无配置通过全部准入条件")}</b>。单独门扇补全候选：<b>{LABELS.get(component,"无")}</b>。这两项选择均在新检查前确定；公共默认方法、产品预览和房间边界流程没有修改。</p>')
    parts.append('<p class="box">新检查有 1 张预算终止：train-00/0049-0002，13,672 图元，超过既有 12,000 上限；全部配置均为不完整。主指标保留全部 96 张，不删除该样本、不放宽预算重跑。因此门扇补全虽有正向识别证据，尚不构成完整运行准入。</p>')
    parts.append('<h2>本轮结果</h2><p>内部窗轨约束在分析集将墙误判为窗从 v4 组合的 320 条降至 220 条（减少 31.3%），窗 TP 377→373，FP 605→472；新检查墙误判为窗为 137→120（减少 12.4%），窗 TP 保持 328，FP 297→246。新检查未达到预设 20% 降幅，且窗精确率仍低于保守 v3 基线，不升级整体方案。</p><p>独立门扇补全在分析集使门 TP 899→1,177、FP 159→237；新检查 TP 451→571、FP 110→134，召回率 20.2%→25.5%、精确率 80.4%→81.0%。新检查门实例任一命中保持 199/288，至少半数部件命中从 80/288 提升到 172/288，主要收益是已有门的部件完整度。两个 48 族队列均提高门召回，但其中一队列精确率略降，详见分组表。</p>')
    parts.append('<h2>隔离范围与执行顺序</h2><p>仅用原 154 个开发分析族开发。旧的 102＋96 个检查族全部隔离；固定新检查名单时排除全部已用 352 族，从剩余 426 个开发族中固定 96 族，每族一张，拆成两个各 48 族的检查队列。原验证集与 test 的样本内容不读取。只读取旧清单和既有几何哈希元数据以排除重叠。</p><p>分析诊断 → 固定有限几何假设和消融 → 冻结源码、参数、协议及名单 → 分析集计分与选择封存 → 新检查。新检查没有用于调参、挑选配置或添加规则。已用的新 96 族今后同样不得用于调参。每个评估进程仅允许读取当前一张 SVG，拒绝其他 SVG 和历史检查结果路径。</p>')
    parts.append('<h2>分析发现与通用修改</h2><p>旧窗候选可能把重合外框线、贴边线或跨度不足的短线算作内部窗轨。分析中 19 个缺乏有效内部轨的候选涉及 108 条墙线、9 条真窗线；候选会重叠，这不是独立图元损益数。新约束要求内部轨与两侧框线分离，且覆盖至少 80% 框长，沿用既有 2% 框线对齐容差与 80% 重叠比例，没有根据文件调整阈值。</p><p>门扇补全必须从已有门弧＋门扇线证据出发，只收集狭窄门扇区域内实际连通的部件。宿主证据可以保留，是否要求宿主墙由单独开关控制。没有叶片种子的弧不能触发补全；长墙和不连通邻线不会被随意吸收。</p><p>两端延续和横向穿越也会出现在真窗中，所以分别作为否决消融，而非默认条件。所有识别输入只有几何；语义标签只供计分和开发诊断。没有文件名、图层或单图专用分支。</p>')
    parts.append('<h2>预设准入</h2><p>整套方案要求：相对既有基线，门窗召回均提高，精确率下降各不超过 2 个百分点，墙 TP 至少保留 98%、墙 FP 不增加，各类 F1 不下降、所有输出完整；同时墙误判为窗数量比本轮重算的 v4 组合至少减少 20%。只在分析集通过者中按三类平均 F1 选择。门部件候选单独要求窗角色完全不变，门召回增加，并满足精确率和墙线保留条件。</p>')
    for title,r in [('154 族开发分析集',a),('96 个新开发族检查',b)]:
        parts += [f'<h2>{title}</h2>',table(r['totals'],names),'<details><summary>全部 11 个配置消融</summary>'+table(r['totals'])+'</details>','<details><summary>门窗实例覆盖率</summary>'+instances(r['totals'])+'</details>']
        for fold,t in r['cohorts'].items():parts.append(f'<details><summary>固定队列 {fold}</summary>{table(t,names)}</details>')
        parts.append('<details><summary>整体准入判定（检查阶段只作审计）</summary><pre>'+html.escape(json.dumps(r['gates'],ensure_ascii=False,indent=2))+'</pre></details>')
    parts.append('<h2>按文件族等权核对</h2><p>改善、下降与不变按每族 F1 统计。排除该类无真值也无预测的空族，避免空样本掩盖问题；不根据新检查宏指标改选择。</p>')
    for phase,variants in comparisons.items():
        parts.append(f'<h3>{phase}</h3><table><tr><th>配置 / 类别</th><th>改善 / 下降 / 不变</th><th>基线宏 F1</th><th>实验宏 F1</th></tr>')
        for v in [n for n in names if n!='baseline']:
            for c,z in variants[v].items():parts.append(f'<tr><td>{LABELS[v]} / {c}</td><td>{z["improved"]} / {z["worse"]} / {z["unchanged"]}</td><td>{pct(z["baseline_macro_f1"])}</td><td>{pct(z["macro_f1"])}</td></tr>')
        parts.append('</table>')
    if (OUT/'analysis_examples.png').exists():parts.append('<h2>分析集源几何对照</h2><p>示例仅用于解释冻结结果。标题统计选定源部件，周边图形用于上下文；不据示例加入规则。绿色为正确类别，橙色为该类别误检，灰色为未赋该类的上下文。</p><img src="analysis_examples.png" alt="分析集墙窗筛除与门扇补全对照" style="width:100%;height:auto">')
    parts.append('<h2>解释边界</h2><p>统计单位是源图元，实例任一命中和半数覆盖另列；不是检测框 AP，也不证明房间边界或面积改善。文件名前缀只是族代理，并非真实项目身份。已隔离已知精确几何重复，尚未证明没有裁剪、变换等近重复。新方案保持实验状态，不自动闭合门洞或改变产品预览。</p><h2>复核文件</h2><ul>')
    for file in ['protocol.json','analysis_geometry_diagnosis.json','analysis_roster.json','fresh_checks_roster.json','excluded_used_families.json','candidate_freeze.json','selection_seal.json','analysis_results.json','fresh_checks_results.json','family_comparison.json','delivery_validation.json']:
        parts.append(f'<li><a href="{file}">{file}</a></li>')
    parts.append('</ul></html>');(OUT/'report.html').write_text(''.join(parts))
    save(OUT/'summary.json',dict(selected_variant=selected,door_component_variant=component,analysis=a['totals'],fresh_checks=b['totals'],family_comparison=comparisons,protocol=protocol))
    print(json.dumps(dict(selected_variant=selected,door_component_variant=component),ensure_ascii=False))
if __name__=='__main__':run()
