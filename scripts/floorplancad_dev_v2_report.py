"""Report development-only family checks without reading the original holdout."""
from collections import Counter
from html import escape
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import resvg_py

from scripts.floorplancad_dev_v2 import BASE,OUT,ROOT,load_rows,install_guard,read,save,sha
from scripts.floorplancad_dev_v2_eval import aggregate,check,VARIANTS
from scripts.floorplancad_svg import SHAPES

COLORS={'door':'#d64933','window':'#1678cc','wall':'#26945d','other':'#c8cdd5','ambiguous':'#a647b9'}
NAMES={'v1':'v1 基线','full':'完整 v2','join_only':'仅共线拼接','door_only':'仅门符号补全',
       'window_only':'仅端部覆盖支持','wall_only':'仅薄双线上下文','no_join':'v2 去掉拼接','no_door':'v2 去掉门补全',
       'no_window':'v2 去掉端部覆盖','no_wall':'v2 去掉薄双线约束'}


def render(raw,roles,path):
    root=ET.fromstring(raw);root.set('style','background-color:white');root.set('width','650');root.set('height','650')
    i=0
    for node in root.iter():
        tag=node.tag.rsplit('}',1)[-1]
        if tag in SHAPES:
            node.set('stroke',COLORS.get(roles.get(f'p{i}','other'),COLORS['other']))
            node.set('fill','none');node.set('stroke-width','.17');i+=1
        elif tag=='text':node.set('fill','#a5aab2')
    path.write_bytes(resvg_py.svg_to_bytes(svg_string=ET.tostring(root,encoding='unicode')))


def pct(v):return '—' if v is None else f'{100*v:.1f}%'


def table(totals):
    rows=[]
    for name,t in totals.items():
        for cls,m in t['micro'].items():
            values=[NAMES[name],cls,m['tp'],m['fp'],m['fn'],pct(m['precision']),pct(m['recall']),pct(m['f1']),t['incomplete_samples']]
            rows.append('<tr>'+''.join(f'<td>{v}</td>' for v in values)+'</tr>')
    return '<div class="scroll"><table><thead><tr><th>方案</th><th>类别</th><th>TP</th><th>FP</th><th>FN</th><th>精确率</th><th>召回率</th><th>F1</th><th>未完成样本</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'


def main():
    check();roster=load_rows();install_guard(roster)
    analysis=json.loads((OUT/'analysis_results.json').read_text());checks=json.loads((OUT/'checks_results.json').read_text())
    diagnosis=json.loads((OUT/'development_diagnosis.json').read_text());selection=json.loads((OUT/'selection_seal.json').read_text())
    combined=aggregate(analysis['sample_results']+checks['sample_results'])
    summary=dict(analysis=analysis['totals'],internal_checks=checks['totals'],all_development=combined,
        selected_variant=selection['selected_variant'],original_validation_read=False,test_read=False)
    save(OUT/'summary.json',summary)
    foldrows=[]
    for fold,data in {**analysis['folds'],**checks['folds']}.items():
        for cls in ('door','window','wall'):
            a=data['v1']['micro'][cls];b=data['full']['micro'][cls]
            foldrows.append('<tr>'+''.join(f'<td>{v}</td>' for v in (fold,'分析' if int(fold)<3 else '内部检查',cls,
                f"{pct(a['precision'])} → {pct(b['precision'])}",f"{pct(a['recall'])} → {pct(b['recall'])}",f"{a['fp']} → {b['fp']}"))+'</tr>')
    # Three pre-specified mechanisms, selected only from analysis folds by stable roster order.
    selected=[]
    for title,condition in [
        ('墙线误检减少',lambda r:r['variants']['full']['counts']['wall']['fp']<r['variants']['v1']['counts']['wall']['fp']),
        ('门图元覆盖增加',lambda r:r['variants']['full']['counts']['door']['tp']>r['variants']['v1']['counts']['door']['tp']),
        ('窗图元覆盖增加',lambda r:r['variants']['full']['counts']['window']['tp']>r['variants']['v1']['counts']['window']['tp']),
        ('反例：墙真阳性减少',lambda r:r['variants']['full']['counts']['wall']['tp']<r['variants']['v1']['counts']['wall']['tp'])]:
        row=next((r for r in analysis['sample_results'] if condition(r)),None)
        if row:selected.append((title,row))
    image_dir=OUT/'images';image_dir.mkdir(exist_ok=True);cards=[]
    for index,(title,result) in enumerate(selected):
        row=next(r for r in roster if r['sample_id']==result['sample_id'])
        raw=Path(row['path']).read_bytes();ps,truth,d,h=read(row)
        images=[]
        for variant in ('truth','v1','full'):
            roles={eid:t['category'] for eid,t in truth.items()} if variant=='truth' else json.loads((OUT/'predictions'/variant/(row['sample_id'].replace('/','__')+'.json')).read_text())['entity_roles']
            name=f'{index}-{variant}.png';render(raw,roles,image_dir/name)
            images.append(f'<figure><img src="images/{name}"><figcaption>{variant}</figcaption></figure>')
        cards.append(f'<section><h3>{escape(title)} · {escape(row["sample_id"])}</h3><div class="triple">'+''.join(images)+'</div><small>按开发分析清单顺序选择首个符合展示条件的案例；包含退化案例，不代表总体效果。</small></section>')
    stats=diagnosis['stats'];wallfp=sum(stats['wall_false_positive_raw_class'].values())
    wall_doors=sum(stats['wall_false_positive_raw_class'].get(str(i),0) for i in (3,4,5,6,7,8))
    verdict='分析集未有方案同时通过全部门槛，因此不推荐替换默认方法。' if selection['selected_variant'] is None else '分析集按预定门槛选择：'+NAMES[selection['selected_variant']]+'。内部检查结果独立保留，不据此再调参。'
    html='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>开发集内的矢量识别改进与消融</title>
<style>body{font:16px/1.65 system-ui;max-width:1300px;margin:32px auto;padding:0 22px;background:#f5f8fc;color:#203047}section,.notice{background:white;border:1px solid #dce3eb;border-radius:12px;padding:22px;margin:18px 0}h1{font-size:30px}h2{font-size:22px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left;white-space:nowrap}.scroll{overflow:auto}.triple{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}figure{margin:0}img{width:100%}small{color:#5a6b7c}a{color:#175ea5}summary{cursor:pointer;font-weight:bold;padding:12px}code{background:#edf2f8;padding:2px 5px}</style>
<h1>FloorPlanCAD：开发集内的墙线筛选、门窗召回与消融</h1>
<div class="notice"><b>原 validation 与 test 均未读取样本或结果</b><p>本轮全部来自上一轮的 256 份 development 样本。按文件族固定成五组：0–2 组共 154 族用于原因分析，3–4 组共 102 族用于冻结后的内部检查。每族本轮一份样本。文件族不是经确认的项目身份；既有 v1 开发集汇总已在前一轮可见，因此这不是全新盲测。</p></div>
'''+f'''<section><h2>结论边界</h2><p>{escape(verdict)}</p><p>这是固定规则的跨文件族检查和特征消融，没有模型训练，也不是每折重新调参的五折训练实验。内部检查完成后不调整参数、不选择新方案。未自动删除墙线或闭合房间，未评估房间边界准确率。</p></section>
<section><h2>开发分析中直接观察到的共性</h2><ul>
<li>门共 {sum(stats['door_total_kind'].values())} 个目标图元，其中 {stats['door_total_kind']['line']} 个为直线。漏检中 {stats['door_miss_kind']['line']} 个为直线；已有命中的门实例里仍漏掉 {stats['door_missed_entities_in_hit_instances']['missed']} 个图元，说明只命中摆弧/单根门扇不足以覆盖完整符号。</li>
<li>漏检摆弧中，45° 与 60° 合计 {stats['door_miss_arc_sweep'].get('45',0)+stats['door_miss_arc_sweep'].get('60',0)} 个，原先 80–100° 的限制会直接排除这些非直角开启形式。</li>
<li>窗的 {sum(stats['window_miss_kind'].values())} 个漏检图元中，{stats['window_miss_kind']['line']} 个是直线。原方法要求每个端部由一根恰好匹配的线支持；分段、延伸横线与断开的轨道需要分别验证。</li>
<li>墙线假阳性共 {wallfp} 个，涉及 {diagnosis['families_affected']['wall_false_positive_raw_class']} 个文件族，其中 {wall_doors} 个属于门，{stats['wall_false_positive_raw_class'].get('0',0)} 个为未标注背景。双线带端部连接本身不能确立墙身份。</li>
<li>极薄双线中仍有真实墙候选，不能直接按宽度删除。薄双线约束必须依赖局部图形尺度及观察到的外部连接，并单独检查真阳性保留率。</li></ul><a href="development_diagnosis.json">完整诊断计数</a> · <a href="analysis_wall_scale.json">薄双线分布</a></section>
<section><h2>四项改进与预设门槛</h2><p>共线拼接：只连接实际相接且相对误差有界的共线片段，保留来源；门补全：放宽有径向门扇支持的开启角，并沿观察到的连接补充局部门扇/门框；端部支持：允许分段或延伸横线覆盖窗端部，延伸墙线仅作支持证据；薄双线约束：至少三个门摆弧提供尺度参考时，再核验薄双线的外部连接。</p>
<p>预设接纳门槛：墙 FP 至少降低 20%，墙 TP 至少保留 80%；门窗召回提高且精确率下降不超过 5 个百分点；各类 F1 不下降，且没有未完成样本。门槛在首次消融评分前冻结。完整方案、四个单项、四个去一项消融与 v1 共十组。</p>
<a href="candidate_freeze.json">冻结代码和方案</a> · <a href="selection_seal.json">分析集选择记录</a> · <a href="protocol.json">分组协议</a></section>
<section><h2>内部检查：102 个开发文件族</h2>{table({'v1':checks['totals']['v1'],'full':checks['totals']['full']})}<p>指标为逐图元 P/R/F1。实例覆盖指标另计入 JSON：至少一半标注图元被正确赋类的比例；它不是实例 AP，也不代表门洞可通行。</p></section>
<section><h2>逐组对照：v1 → 完整 v2</h2><table><thead><tr><th>组</th><th>用途</th><th>类别</th><th>精确率</th><th>召回率</th><th>FP</th></tr></thead><tbody>{''.join(foldrows)}</tbody></table></section>
<section><h2>完整消融记录</h2><details><summary>154 个分析文件族</summary>{table(analysis['totals'])}</details><details><summary>102 个内部检查文件族</summary>{table(checks['totals'])}</details><details><summary>全部 256 个开发文件族（仅描述性汇总）</summary>{table(combined)}</details><p><a href="analysis_results.json">分析原始结果</a> · <a href="checks_results.json">内部检查原始结果</a> · <a href="summary.json">精简汇总</a></p></section>
<h2>分析集几何证据示例</h2><p>红：门；蓝：窗；绿：墙；灰：其他/未赋类。只展示分析分组，不读取内部检查或原验证集图纸作错误反馈。</p>{''.join(cards)}
<section><h2>运行与证据</h2><p>每份样本的原始哈希、分组、逐方案计数、候选图元来源和运行耗时均已保存。冻结源码位于 frozen_source。读取守卫拒绝原 validation 路径、test 包及当前工作进程清单外 SVG。SVG 解析器仍与标签评估分离，识别器只接收几何。</p><p>运行开销包含几何关系检查；密集平行线可能显著增加端部覆盖判断的成本。原图没有修改，已有产品预览和默认房间识别没有变更。</p></section></html>'''
    (OUT/'report.html').write_text(html)
    print(OUT/'report.html')


if __name__=='__main__':main()
