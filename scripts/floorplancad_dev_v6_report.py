"""Frozen ownership experiment reporting; no selection on fresh checks."""
import json,html
from collections import Counter
from scripts.floorplancad_dev_v6_data import OUT,ROOT,sha,save
from scripts.floorplancad_dev_v4_report import instances
LABELS={'baseline':'既有 v3 基线','door_reference':'固定独立门扇补全','v5_full':'v5 完整组合（本轮重算）','cap_support_all':'全部端盖仅作支持','shared_caps':'共享墙端盖仅作支持','wall_evidence_caps':'既有墙证据端盖仅作支持','full':'两类端盖证据合并','no_wall_restore':'合并端盖证据，不恢复墙候选','outer_support_only':'连续外轨仅作支持','full_with_outer':'端盖合并＋连续外轨支持'}
def pct(x):return '—' if x is None else f'{100*x:.1f}%'
def table(totals,names=None):
    out=['<table><tr><th>配置</th><th>类别</th><th>TP / FP / FN</th><th>精确率</th><th>召回率</th><th>F1</th><th>墙误判为窗</th><th>不完整</th></tr>']
    for name in names or totals:
        t=totals[name]
        for c,m in t['micro'].items():out.append(f'<tr><td>{LABELS[name]}</td><td>{c}</td><td>{m["tp"]} / {m["fp"]} / {m["fn"]}</td><td>{pct(m["precision"])}</td><td>{pct(m["recall"])}</td><td>{pct(m["f1"])}</td><td>{t["wall_as_window_fp"] if c=="window" else "—"}</td><td>{t["incomplete_samples"]}</td></tr>')
    return ''.join(out)+'</table>'
def paired(rows):
    result={}
    for v in rows[0]['variants']:
        if v in ('baseline','door_reference','v5_full'):continue
        result[v]={}
        for c in ('door','window','wall'):
            counter=Counter(improved=0,worse=0,unchanged=0);base=[];current=[]
            for row in rows:
                a=row['variants'][v]['counts'][c];b=row['variants']['v5_full']['counts'][c]
                den=lambda m:2*m['tp']+m['fp']+m['fn'];f1=lambda m:2*m['tp']/den(m) if den(m) else 0
                if not den(a) and not den(b):continue
                x,y=f1(a),f1(b);counter['improved' if x>y else 'worse' if x<y else 'unchanged']+=1;current.append(x);base.append(y)
            result[v][c]=dict(counter,eligible_families=len(base),v5_macro_f1=sum(base)/len(base) if base else None,macro_f1=sum(current)/len(current) if current else None)
    return result

def run():
    a=json.loads((OUT/'analysis_results.json').read_text());b=json.loads((OUT/'fresh_checks_results.json').read_text());seal=json.loads((OUT/'selection_seal.json').read_text());protocol=json.loads((OUT/'protocol.json').read_text())
    assert seal['analysis_sha256']==sha((OUT/'analysis_results.json').read_bytes()) and seal['freeze_sha256']==sha((OUT/'candidate_freeze.json').read_bytes())
    assert min(p.stat().st_mtime for p in (OUT/'progress/fresh_checks').glob('*.json'))>=seal['sealed_unix']
    chosen=seal['selected_variant'];names=list(dict.fromkeys(['door_reference','v5_full','full','wall_evidence_caps']+([chosen] if chosen else [])))
    family={phase:paired(r['sample_results']) for phase,r in [('analysis',a),('fresh_checks',b)]};save(OUT/'family_comparison.json',family)
    incomplete=[]
    for row in b['sample_results']:
        if not row['variants']['v5_full']['complete']:
            p=OUT/'predictions/fresh_checks/v5_full'/(row['sample_id'].replace('/','__')+'.json');pred=json.loads(p.read_text());incomplete.append(dict(sample_id=row['sample_id'],primitives=row['primitives'],diagnostics=pred['diagnostics']))
    parts=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>FloorPlanCAD v6：墙窗端盖归属</title><style>body{font:16px system-ui;max-width:1360px;margin:36px auto;padding:0 24px;background:#f8fafc;color:#172d3d}table{border-collapse:collapse;width:100%;background:#fff;margin:18px 0;font-size:14px}td,th{border:1px solid #d4dfe8;padding:8px;text-align:right}td:first-child,th:first-child{text-align:left}th{background:#e7eff6}.box{padding:18px;border-left:4px solid #2e789f;background:#eaf3f8}details{margin:20px 0}a{color:#125b8e}pre{white-space:pre-wrap}</style><h1>剩余墙窗混淆：端盖归属与支持证据</h1>']
    parts.append(f'<p class="box">分析阶段封存选择：<b>{LABELS.get(chosen,"没有配置通过全部准入条件")}</b>。独立门扇补全固定沿用 v5，不要求宿主墙硬条件。所有方案仍为实验输出，公共默认方法、产品预览和房间边界流程未修改。</p>')
    parts.append('<h2>数据隔离</h2><p>仅用原 154 个开发分析族诊断和开发。原验证集、test 样本内容不读取；旧 102＋96＋96＝294 个检查族禁止调参。固定新检查名单时排除全部已用 448 族，从剩余 330 个开发族中固定 96 个新族，每族一张，拆为两个各 48 族队列。旧清单及既有几何哈希元数据仅用于排除重叠。</p><p>先分析结构，再冻结有限消融、源码、参数及名单，随后分析计分并封存选择，最后读取新检查图形。本轮没有根据新检查结果修改代码、预算、阈值或选择方案。新 96 族今后也属于已用检查族。</p>')
    parts.append('<h2>只来自分析集的诊断</h2><p>v5 的 220 条墙误判为窗中，190 条充当端盖；137 条位于同时包含真实窗线的候选中。这说明剩余问题不完全是整窗候选错误，也包括把墙端共享边界赋予窗语义。237 个窗候选中，165 个含真窗线，72 个完全不含真窗线；候选可能重叠。</p><p>端盖归属不能简单一刀切：端盖角色也涉及 106 条真窗线。连接外侧墙面的共享端盖涉及 113 条墙线和 59 条真窗线；既有墙候选的返回边证据涉及 22 条墙线和 12 条真窗线。这些是开发诊断，标签不参与识别函数。按角色计数可重叠，不能相加当作独立损益。</p>')
    parts.append('<h2>同几何、不同语义的重合源部件</h2><p>冻结后的分析集审计发现：44 个分析族包含 195 组端点坐标完全一致、但同时具有墙和窗标注的线；对应 250 条墙源图元与 195 条窗源图元。比较使用精确解析坐标、忽略端点方向，不做四舍五入或近似匹配。v5 的 220 条墙误判为窗中，90 条处于这种重合组。</p><p>合并端盖方案移出的 125 条墙误检中，有 64 条来自上述重合组；同时损失的 59 条正确窗图元中，有 47 条来自这些组。这不证明哪一类标注错误：共享边界可能属于不同构件。当前不含构件归属的几何输入无法仅凭相同坐标区分这些副本，因此继续硬排除端盖存在固有的召回代价。该审计只解释分析集，不进入识别器或改动冻结选择。</p><p><a href="analysis_coincident_label_audit.json">重合组的坐标、源 ID 与标注审计</a></p>')
    parts.append('<h2>结果解读</h2><p>合并端盖证据相对 v5，在分析集将墙误判为窗 220→95、正确窗图元 373→314；在新检查集为墙误判为窗 150→82、正确窗图元 263→235。新检查窗精确率 46.2%→53.4%，召回率 24.2%→21.7%，F1 31.8%→30.8%；不满足正确窗图元至少保留 95% 和 F1 不下降，因此没有升级。</p><p>新检查窗实例任一命中保持 51/135，至少半数部件命中保持 38/135；分析集对应值也不变。候选窗主体仍被识别，变化集中在部件角色。所有实验配置的门图元角色与固定独立补全完全一致。端盖方案恢复旧墙候选的开关没有改变这两批的墙 TP/FP；不能把墙误判为窗减少解释成墙识别召回提高。</p>')
    parts.append('<h2>本轮修改与消融</h2><p>把窗候选的完整观测框架（observed_frame_entity_ids）和实际赋予窗类别的源部件（entity_ids）分开。移出的端盖保留为 shared_boundary_support_ids，不删除源几何，也不直接标成墙。只有原先已存在、且不再与门窗角色冲突的墙候选可以恢复。若一个源部件在另一候选中具有纵向窗轨证据，不会被全局当作端盖移除。</p><p>七种配置：全部端盖、外侧连接端盖、已有墙几何证据端盖、两种证据合并、合并但不恢复旧墙候选、连续外轨、端盖与外轨合并。三种固定对照：v3、独立门扇补全、v5 完整组合。没有文件名、图层或单图特例；没有数值阈值搜索。</p>')
    parts.append('<h2>预设准入</h2><p>相对 v5 完整组合：墙误判为窗至少减少 20%，正确窗图元至少保留 95%，窗精确率和 F1 不下降，墙 TP 不减少、墙 FP 不增加；门图元角色必须与固定独立门扇补全完全一致；所有样本输出完整。只按分析集通过者的三类平均 F1 选取，未通过就不升级。</p>')
    parts.append(f'<p>新检查不完整样本数：<b>{len(incomplete)}</b>。主指标保留全部 96 张，未剔除预算终止样本。</p>')
    if incomplete:parts.append('<pre>'+html.escape(json.dumps(incomplete,ensure_ascii=False,indent=2))+'</pre>')
    for label,r in [('154 个开发分析族',a),('96 个全新开发检查族',b)]:
        parts.extend([f'<h2>{label}</h2>',table(r['totals'],names),'<details><summary>全部十种配置</summary>'+table(r['totals'])+'</details>','<details><summary>门窗实例覆盖</summary>'+instances(r['totals'])+'</details>'])
        for fold,t in r['cohorts'].items():parts.append(f'<details><summary>固定队列 {fold}</summary>{table(t,names)}</details>')
        parts.append('<details><summary>预设准入判定（新检查只作审计）</summary><pre>'+html.escape(json.dumps(r['gates'],ensure_ascii=False,indent=2))+'</pre></details>')
    parts.append('<h2>文件族等权对照</h2><p>以下以 v5 完整组合为对照，统计每族 F1 的改善、下降和不变；不计该类无真值也无任何预测的空族。</p>')
    for phase,variants in family.items():
        parts.append(f'<h3>{phase}</h3><table><tr><th>配置 / 类别</th><th>改善 / 下降 / 不变</th><th>v5 宏 F1</th><th>本轮宏 F1</th></tr>')
        for v in [n for n in names if n in variants]:
            for c,z in variants[v].items():parts.append(f'<tr><td>{LABELS[v]} / {c}</td><td>{z["improved"]} / {z["worse"]} / {z["unchanged"]}</td><td>{pct(z["v5_macro_f1"])}</td><td>{pct(z["macro_f1"])}</td></tr>')
        parts.append('</table>')
    if (OUT/'analysis_examples.png').exists():parts.append('<h2>分析集效果与反例</h2><p>仅用于解释冻结结果，不据示例添加规则。绿色正确窗线，橙色窗误检，紫色虚线为窗真值，蓝色为仅作支持的源部件；原始几何保留。标题只统计选定部件。</p><img src="analysis_examples.png" alt="端盖归属分析对照" style="width:100%;height:auto">')
    parts.append('<h2>证据边界与复核</h2><p>指标是源图元分类，实例覆盖另列；不等于门窗 AP，也不能证明房间边界或面积改善。文件名前缀不是已核实项目身份；精确几何重复已按既有审计隔离，但裁剪、变换等近重复独立性尚未证实。</p><ul>')
    for f in ['protocol.json','analysis_diagnosis.json','analysis_ownership_diagnosis.json','analysis_cap_scale_diagnosis.json','analysis_roster.json','fresh_checks_roster.json','excluded_used_families.json','candidate_freeze.json','selection_seal.json','analysis_results.json','fresh_checks_results.json','family_comparison.json','delivery_validation.json']:
        parts.append(f'<li><a href="{f}">{f}</a></li>')
    parts.append('</ul></html>');(OUT/'report.html').write_text(''.join(parts))
    save(OUT/'summary.json',dict(selected_variant=chosen,door_component='fixed_v5_independent_completion',analysis=a['totals'],fresh_checks=b['totals'],family_comparison=family,incomplete_checks=incomplete,protocol=protocol))
    print(json.dumps(dict(selected_variant=chosen,incomplete_checks=incomplete),ensure_ascii=False))
if __name__=='__main__':run()
