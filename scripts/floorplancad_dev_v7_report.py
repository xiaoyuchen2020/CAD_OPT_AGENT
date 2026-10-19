"""Render frozen experiment results; no dataset or older check reads."""
import json,html
from scripts.floorplancad_dev_v7_data import ROOT,OUT,save,sha
from scripts.floorplancad_benchmark import summarize
LABELS={'baseline':'v3 无补全','door_reference':'v5 独立门扇补全','v5_full':'v5 完整对照','v6_full':'v6 端盖归属对照','door_connected':'门：真实种子连通','door_unique':'门：重合新增部件暂不归属','door_connected_unique':'门：连通＋重合归属','door_closed_contour':'门：闭合轮廓','cap_both_extensions':'窗：端盖双向延续','cap_any_extension':'窗：端盖任一方向延续','cap_coincident':'窗：重合端盖暂不归属','cap_both_or_coincident':'窗：双向延续或重合','combined':'门窗组合（诊断）'}
def pct(v):return '—' if v is None else f'{v*100:.2f}%'
def table(t):
    s='<table><tr><th>配置</th><th>类别</th><th>TP / FP / FN</th><th>精确率</th><th>召回率</th><th>F1</th><th>墙误判窗</th><th>不完整</th></tr>'
    for name,z in t.items():
        for c in ('door','window','wall'):
            m=z['micro'][c];s+=f'<tr><td>{LABELS[name]}</td><td>{c}</td><td>{m["tp"]} / {m["fp"]} / {m["fn"]}</td><td>{pct(m["precision"])}</td><td>{pct(m["recall"])}</td><td>{pct(m["f1"])}</td><td>{z["wall_as_window_fp"] if c=="window" else "—"}</td><td>{z["incomplete_samples"]}</td></tr>'
    return s+'</table>'
def families(result):
    out={}
    for name in result['totals']:
        if name in ('baseline','door_reference','v5_full'):continue
        out[name]={}
        for c in ('door','window','wall'):
            pairs=[]
            for r in result['sample_results']:
                old=summarize(r['variants']['v5_full']['counts'])[c]['f1'];new=summarize(r['variants'][name]['counts'])[c]['f1']
                if old is None and new is None:continue
                pairs.append((old or 0,new or 0))
            out[name][c]=dict(improved=sum(y>x+1e-12 for x,y in pairs),worse=sum(y<x-1e-12 for x,y in pairs),unchanged=sum(abs(y-x)<=1e-12 for x,y in pairs),v5_macro_f1=sum(x for x,y in pairs)/len(pairs) if pairs else None,macro_f1=sum(y for x,y in pairs)/len(pairs) if pairs else None)
    return out

def run():
    a=json.loads((OUT/'analysis_results.json').read_text());b=json.loads((OUT/'fresh_checks_results.json').read_text());seal=json.loads((OUT/'selection_seal.json').read_text());d=json.loads((OUT/'analysis_diagnosis.json').read_text());post=json.loads((OUT/'analysis_postmortem.json').read_text())
    fam={phase:families(r) for phase,r in [('analysis',a),('fresh_checks',b)]};save(OUT/'family_comparison.json',fam)
    parts=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>FloorPlanCAD v7：门扇补全与墙窗归属</title><style>body{font:16px system-ui;max-width:1400px;margin:32px auto;padding:0 24px;background:#f8fafc;color:#183044}h1{font-size:29px}p{line-height:1.7}table{border-collapse:collapse;width:100%;background:white;margin:16px 0;font-size:13px}td,th{border:1px solid #d4dfe8;padding:7px;text-align:right}td:first-child,th:first-child{text-align:left}th{background:#e8f0f6}.box{padding:18px;background:#eaf3f8;border-left:4px solid #2e789f}pre{white-space:pre-wrap}a{color:#125b8e}details{margin:16px 0}img{width:100%}</style><h1>门扇补全误检与墙窗共享边界：v7 冻结消融</h1>']
    parts.append('<p class="box">分析阶段封存选择：'+html.escape(json.dumps(seal['selected_components'],ensure_ascii=False))+'。null 表示没有配置通过全部门槛。公共方法默认、产品预览、自动门洞闭合和房间边界流程均未改动。</p>')
    parts.append('<h2>结论与新检查结果</h2><p>本轮没有达到升级条件，继续保持实验状态。门的重合新增部件处理：分析集 FP 237→198、TP 1177→1168，F1 36.176→36.167%；新检查 TP 652、FP 144 均未变化，两个 48 族队列也都没有改善。分析集中，39 条误检削减的 34 条集中在同一文件族；文件族等权 F1 为 1 族改善、2 族下降，不能视为普遍有效。严格闭合轮廓在分析集和新检查分别损失 250、132 条正确门部件，不采用。</p><p>窗端盖双向延续较保守：分析集保留全部 373 条正确窗图元，墙误判窗 220→216；新检查保留 259 条正确窗图元，只减少 1 条其他类别误检，墙误判窗仍是 132。重合端盖处理在新检查把墙误判窗 132→48，但正确窗图元 259→233，窗精确率 52.64%→64.36%、召回 20.03%→18.02%、F1 29.02%→28.16%；两个队列 F1 都下降。没有用新检查表现来调整条件。</p><p>新检查 train-01/0157-1138 有 1842 个可解析图元，触发既有 250000 次关系配对预算。v3 基线完整，其余高级配置回退到已有基础候选并标记 complete=false；该样本保留在全部 96 张主指标中，没有提高预算或重跑。所有本轮配置的墙分类 TP/FP 不变；减少墙误判窗不等于提高墙召回。未验证房间边界改善。</p>')
    parts.append('<h2>数据与实验顺序</h2><p>开发只使用原固定 154 个分析族。排除旧 102＋96＋96＋96＝390 个检查族，既有 544 个已用族整体不进入新检查。仅用既有清单和几何哈希元数据排除交叉与精确重复，从剩余 234 个合格开发族固定选取 96 个新族，每族一张，分成两个各 48 族队列。文件族是文件名前缀代理，不等于已核实项目身份，也未证明所有近重复独立。</p><p>分析诊断 → 冻结源码、9 种消融、参数、评分协议和名单 → 154 族评分并封存组件选择 → 一次性运行新 96 族。原验证集及 test 内容未读；旧检查图形、标签和结果未用于本轮诊断。新检查只报告，不用于调参或重选。新 96 族之后也封存为检查族：累计已用检查族 486，总已用族 640。</p>')
    parts.append('<h2>分析集发现</h2><p>v5 独立补全相对 v3 新增 278 条正确门图元及 78 条误检，其中 76 条为其他类别、墙和窗各 1 条。72 条误检的原始语义标签是 0，另有 4 条标签为 30；按原有评分协议均计误检，不能据此断言这些线在实际建筑语义上都不是门部件。275 条正确补全部件和至少 57 条误检出现在全部种子图元均标为门的候选附近：正确种子也可能扩展到其他部件。</p><p>几何特征不能简单当成排除规则：全部新增部件都能通过真实种子端点连通检查，所以去掉虚拟铰链/门尖锚点在分析集中没有效果；250 条正确门部件不在闭合轮廓内，严格闭合会严重损失召回；254 条正确门部件也有外部连接，不能把“与外界相连”直接当成干扰。重合新增部件涉及 39 条误检和 9 条正确门线，值得保留为归属不确定的证据，但有真实召回代价。</p><p>窗的端盖角色涉及 190 条墙图元，也涉及 106 条真实窗图元。重合端盖涉及 157 条墙和 75 条真窗；任一方向向框外延续涉及 34 条墙和 21 条真窗；双向延续只涉及 4 条墙、8 条其他类别，未涉及真窗。角色按每族去重，跨角色和候选可重叠；不能把这些诊断数直接当作最终净收益。</p><p>几何相同的源部件可以带有不同语义标签。重合本身不证明哪条源线属于墙、窗或门；本轮对不确定部件只撤销角色归属并保留证据，不按 ID、文件名、图层或标签选择某个副本，也不将撤销部分自动标成墙。</p>')
    parts.append('<h2>冻结消融与准入</h2><p>门部件：真实种子连通、重合部件暂不归属、二者组合、闭合轮廓。窗部件：端盖双向延续、任一方向延续、重合端盖暂不归属、双向或重合；另有一个门窗组合诊断。四个固定对照是 v3、v5 独立门扇补全、v5 完整组合、v6 端盖方案。宿主墙不作门补全硬条件；没有数值阈值扫描。</p><p>门组件相对 v5：误检至少降 20%，正确门图元至少保留 95%，精确率和 F1 不下降，精确率相对 v3 最多下降 2 个百分点，窗和墙角色不变且所有输出完整。窗组件：墙误判窗至少降 20%，正确窗图元保留至少 95%，精确率及 F1 不下降，门角色不变，墙 TP 不减、FP 不增，所有输出完整。门、窗分别选择；未通过不升级，组合不能放宽单组件门槛。</p>')
    for label,result in [('154 个分析族',a),('冻结后的新 96 个检查族',b)]:
        parts.append(f'<h2>{label}</h2>')
        shown=['v5_full','door_unique','door_closed_contour','cap_both_extensions','cap_coincident','combined']
        parts.append(table({n:result['totals'][n] for n in shown}))
        parts.append('<details><summary>全部 13 个配置</summary>'+table(result['totals'])+'</details>')
        for fold,t in result['cohorts'].items():parts.append(f'<details><summary>固定分组 {fold}</summary>'+table({n:t[n] for n in shown})+'</details>')
        parts.append('<details><summary>各项门槛（新检查仅审计）</summary><pre>'+html.escape(json.dumps(result['gates'],ensure_ascii=False,indent=2))+'</pre></details>')
        parts.append('<details><summary>门窗实例覆盖</summary><table><tr><th>配置</th><th>类别</th><th>实例总数</th><th>任一部件命中</th><th>至少一半部件命中</th></tr>')
        for n in shown:
            for c,m in result['totals'][n]['instances'].items():parts.append(f'<tr><td>{LABELS[n]}</td><td>{c}</td><td>{m["total"]}</td><td>{m["any_hit"]}</td><td>{m["half_covered"]}</td></tr>')
        parts.append('</table></details>')
    parts.append('<h2>文件族等权对照</h2><p>相对 v5，逐族比较 F1；两侧都无真值且无预测的类别不计。以下展示重点配置，完整数据见 family_comparison.json。</p>')
    for phase,vs in fam.items():
        parts.append(f'<h3>{phase}</h3><table><tr><th>配置 / 类别</th><th>改善 / 下降 / 不变</th><th>v5 宏 F1</th><th>本轮宏 F1</th></tr>')
        for n,c in [('door_unique','door'),('door_closed_contour','door'),('cap_both_extensions','window'),('cap_coincident','window')]:
            z=vs[n][c];parts.append(f'<tr><td>{LABELS[n]} / {c}</td><td>{z["improved"]} / {z["worse"]} / {z["unchanged"]}</td><td>{pct(z["v5_macro_f1"])}</td><td>{pct(z["macro_f1"])}</td></tr>')
        parts.append('</table>')
    parts.append('<h2>分析集效果与损失示例</h2><p>每个组件各选一个误检移除最多、一个正确部件损失最多的分析族；只用于解释冻结结果，不据图添加规则。绿色正确、橙色误检、蓝色暂不归属、紫色虚线真值；精确重合源线在图上可能遮叠。原始几何保留。</p><img src="analysis_examples.png" alt="四个分析集案例的消融前后对照">')
    parts.append('<h2>证据边界</h2><p>全部指标是源图元分类及标注实例的部件覆盖，未评估实例 AP、真实 CAD 房间边界或面积。精确率提高不等于房间识别改善，也不证明被撤销的线应该删除。未读取原验证集和 test；不完整样本保留在主分母，详见交付校验。</p><ul>')
    for f in ['protocol.json','analysis_roster.json','fresh_checks_roster.json','excluded_used_families.json','analysis_diagnosis.json','analysis_postmortem.json','candidate_freeze.json','selection_seal.json','analysis_results.json','fresh_checks_results.json','family_comparison.json','delivery_validation.json']:
        parts.append(f'<li><a href="{f}">{f}</a></li>')
    parts.append('</ul></html>');(OUT/'report.html').write_text(''.join(parts))
    save(OUT/'summary.json',dict(selected_components=seal['selected_components'],analysis=a['totals'],fresh_checks=b['totals'],family_comparison=fam,original_validation_access=False,test_access=False,public_method_changed=False))
if __name__=='__main__':run()
