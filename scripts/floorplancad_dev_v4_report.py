"""Read frozen outputs and report; no parameter selection from fresh checks."""
import html,json
from collections import Counter
from scripts.floorplancad_dev_v4_data import OUT,ROOT,save,sha

def pct(x):return '—' if x is None else f'{x*100:.1f}%'
def table(totals):
    out=['<table><tr><th>配置</th><th>类别</th><th>TP / FP / FN</th><th>精确率</th><th>召回率</th><th>F1</th><th>不完整样本</th></tr>']
    for name,t in totals.items():
        for cls,m in t['micro'].items():out.append(f'<tr><td>{name}</td><td>{cls}</td><td>{m["tp"]} / {m["fp"]} / {m["fn"]}</td><td>{pct(m["precision"])}</td><td>{pct(m["recall"])}</td><td>{pct(m["f1"])}</td><td>{t["incomplete_samples"]}</td></tr>')
    return ''.join(out)+'</table>'
def instances(totals):
    out=['<table><tr><th>配置</th><th>类别</th><th>实例数</th><th>任一线条命中</th><th>至少半数线条命中</th></tr>']
    for name,t in totals.items():
        for cls,z in t['instances'].items():out.append(f'<tr><td>{name}</td><td>{cls}</td><td>{z["total"]}</td><td>{z["any_hit"]} ({pct(z["any_hit"]/z["total"] if z["total"] else None)})</td><td>{z["half_covered"]} ({pct(z["half_covered"]/z["total"] if z["total"] else None)})</td></tr>')
    return ''.join(out)+'</table>'
def paired(rows):
    result={}
    for variant in rows[0]['variants']:
        if variant=='baseline':continue
        result[variant]={}
        for cls in ('door','window','wall'):
            c=Counter(improved=0,worse=0,unchanged=0);am=[];bm=[]
            for row in rows:
                a=row['variants'][variant]['counts'][cls];b=row['variants']['baseline']['counts'][cls]
                den=lambda x:2*x['tp']+x['fp']+x['fn']
                if not den(a) and not den(b):continue
                f1=lambda x:2*x['tp']/den(x) if den(x) else 0
                x,y=f1(a),f1(b);am.append(x);bm.append(y);c['improved' if x>y else 'worse' if x<y else 'unchanged']+=1
            result[variant][cls]=dict(c,eligible_families=len(am),macro_f1=sum(am)/len(am) if am else None,baseline_macro_f1=sum(bm)/len(bm) if bm else None)
    return result

def run():
    analysis=json.loads((OUT/'analysis_results.json').read_text());checks=json.loads((OUT/'fresh_checks_results.json').read_text());seal=json.loads((OUT/'selection_seal.json').read_text())
    protocol=json.loads((OUT/'protocol.json').read_text());diagnosis=json.loads((OUT/'analysis_diagnosis.json').read_text());freeze=json.loads((OUT/'candidate_freeze.json').read_text())
    assert seal['analysis_sha256']==sha((OUT/'analysis_results.json').read_bytes())
    assert seal['freeze_sha256']==sha((OUT/'candidate_freeze.json').read_bytes())
    assert seal['sealed_unix']<checks['completed_unix']
    # Every fresh worker output was created after selection seal.
    progress=list((OUT/'progress/fresh_checks').glob('*.json'))
    assert len(progress)==96 and min(p.stat().st_mtime for p in progress)>=seal['sealed_unix']
    paired_results={phase:paired(result['sample_results']) for phase,result in [('analysis',analysis),('fresh_checks',checks)]}
    save(OUT/'family_comparison.json',paired_results)
    title='FloorPlanCAD：宿主墙与局部拓扑实验'
    parts=[f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{title}</title><style>body{{font:16px system-ui;max-width:1280px;margin:36px auto;padding:0 24px;color:#192b3b;background:#fafbfc}}table{{border-collapse:collapse;width:100%;margin:18px 0;background:white;font-size:14px}}td,th{{border:1px solid #d9e1e8;padding:8px;text-align:right}}td:first-child,th:first-child{{text-align:left}}th{{background:#eaf1f7}}.box{{background:#eef5fa;border-left:4px solid #3a799f;padding:16px}}details{{margin:20px 0}}code{{overflow-wrap:anywhere}}a{{color:#17618b}}</style><h1>{title}</h1>']
    selected=seal['selected_variant']
    parts += [f'<p class="box">分析阶段冻结选择：<b>{selected or "无配置通过全部准入条件，不接入公共默认路径"}</b>。新检查结果仅作审计，不用于改参数、改规则或选择配置。产品预览和默认房间识别路径没有修改。</p>',
        '<h2>数据隔离与证据范围</h2><p>只分析原开发折 0–2 的 154 个文件族。从其余未使用开发族中固定 96 个新族，每族一张，分成两个各 48 族的检查队列。排除全部已用 256 族，包括已消耗的 102 个检查族。原验证集和 test 的图形、标签与性能结果不参与本轮。旧清单及几何哈希元数据仅用于排除名单。</p>',
        '<p>文件族按文件名前缀定义，并非已证实的真实项目边界。已排除已知精确几何重复与跨原分割重复族；没有完成近重复审计，因此不能宣称项目级独立泛化。门窗指标是源图元分类，不是目标框 IoU 或房间边界指标；未证明房间识别改善。</p>',
        '<h2>分析集共性诊断</h2><p>窗的 213 个实例中，20 个完整、14 个部分命中、179 个未命中。193 个未完整实例中，175 个有至少三条平行线，157 个有垂直线，137 个在两端附近接触标注墙线。门的 501 个实例中，32 个完整、324 个部分命中、145 个未命中。标注墙线只用于这一诊断，不进入识别模块；这些结构属性相互重叠，不能视作已证实的因果归因。</p>',
        '<h2>冻结实验与准入条件</h2><p>baseline = v3 no_caps；host_only = 三轨窗或圆弧＋外侧成对墙面，不要求局部闭合；topology_only = 有观测端盖的多轨窗＋连通门扇部件，不要求宿主墙；combined = 两者组合；no_door_parts = 关闭门扇部件扩展；no_window_caps = 关闭窗端盖要求。已有基线候选保留；宿主证据只来自开口外部，不能把框自身当宿主，支持线不作为门窗语义输出。候选不会自动闭门洞或生成房间面积。</p>',
        '<p>分析准入：门与窗召回均提高，二者精确率下降各不超过 2 个百分点，墙 TP 保留至少 98%、墙 FP 不增加，三类 F1 均不下降且无不完整样本。通过者按三类平均 F1 选择；没有通过者就不升级。没有阈值搜索，也没有文件名、图层、单图或设计单位分支。</p>']
    for label,r in [('154 族开发分析集',analysis),('96 个新开发文件族检查',checks)]:
        parts += [f'<h2>{label}</h2>',table(r['totals']),'<details><summary>实例覆盖率</summary>'+instances(r['totals'])+'</details>']
        for cohort,t in r['cohorts'].items():parts.append(f'<details><summary>队列 {cohort} 的固定消融</summary>{table(t)}</details>')
        parts.append('<details><summary>所有准入条件判定（新检查只作审计）</summary><pre>'+html.escape(json.dumps(r['gates'],ensure_ascii=False,indent=2))+'</pre></details>')
    parts.append('<h2>冻结后分析复盘</h2><p>组合方案在分析集新增 184 条正确窗线，也新增 310 条窗误检，其中 222 条真值为墙、52 条未分类、24 条门类线条（原始类别 5）、6 条幕墙、6 条栏杆。基线正确墙线损失 35 条，其中 34 条被改判为窗，1 条因配对墙候选移除而失去角色。这说明外部墙面延续能抑制部分假窗，但多层墙线仍能满足窗框拓扑。</p><p>门方面，仅拓扑扩展使分析集 TP 从 899 增至 1,119，而组合宿主约束仅增至 911。当前的双侧墙面硬条件限制了部件补全覆盖。此处只解释分析现象，不据此修改已冻结代码。新检查的两个 48 族队列均有窗召回提升，但精确率下降幅度不同；整体不支持升级。</p><p>以下两例正确召回与两例误检均来自分析集，按冻结输出选取以展示失效类型，未增加单图规则。图中蓝线只是几何宿主证据，橙线为窗误检。部分裁剪旋转以便阅读；原几何不变。标题只统计所选候选的源部件，周围显示其他预测作上下文。</p><img src="analysis_examples.png" alt="分析集窗识别成功与误检对照" style="width:100%;height:auto"><p><a href="analysis_postmortem.json">复盘明细及源实体 ID</a></p>')
    parts.append('<h2>按文件族等权对照</h2><p>下表排除该类既无真值也无任何预测的空族，避免用大量空样本掩盖差异。改进/下降以每族 F1 比较，未按新检查选择方案。</p>')
    for phase,variants in paired_results.items():
        parts.append(f'<h3>{phase}</h3><table><tr><th>配置 / 类别</th><th>改善 / 下降 / 不变</th><th>基线宏 F1</th><th>实验宏 F1</th></tr>')
        for v,classes in variants.items():
            for c,z in classes.items():parts.append(f'<tr><td>{v} / {c}</td><td>{z["improved"]} / {z["worse"]} / {z["unchanged"]}</td><td>{pct(z["baseline_macro_f1"])}</td><td>{pct(z["macro_f1"])}</td></tr>')
        parts.append('</table>')
    parts.append('<h2>可复核文件</h2><ul>')
    for filename in ['protocol.json','analysis_roster.json','fresh_checks_roster.json','analysis_diagnosis.json','candidate_freeze.json','selection_seal.json','analysis_results.json','fresh_checks_results.json','family_comparison.json','delivery_validation.json']:
        parts.append(f'<li><a href="{filename}">{filename}</a></li>')
    parts += ['</ul><p>每个样本保留 source_sha256、geometry_sha256、源 ID 候选、宿主支持 ID 与分阶段输出。完整冻结源码在 frozen_source/。识别模块接口只接受 Primitive 几何，没有标签、文件族或图层输入。</p></html>']
    (OUT/'report.html').write_text(''.join(parts))
    save(OUT/'summary.json',dict(selected_variant=selected,analysis=analysis['totals'],fresh_checks=checks['totals'],family_comparison=paired_results,protocol=protocol,source_hashes=freeze['source_hashes']))
    print(json.dumps(dict(selected_variant=selected,analysis=analysis['totals'],fresh_checks=checks['totals']),indent=2))
if __name__=='__main__':run()
