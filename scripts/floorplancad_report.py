"""Render the frozen vector baseline, with no parameter search or rule changes."""
import argparse
from collections import Counter
from html import escape
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import resvg_py

from scripts.floorplancad_benchmark import read_sample, check_frozen
from scripts.floorplancad_svg import SHAPES, parse_svg

COLORS={'door':'#d64933','window':'#1678cc','wall':'#26945d','other':'#ccd0d5','ambiguous':'#a647b9'}


def overlay(raw,roles,dest):
    root=ET.fromstring(raw)
    root.set('style','background-color:#fff');root.set('width','700');root.set('height','700')
    i=0
    for node in root.iter():
        tag=node.tag.rsplit('}',1)[-1]
        if tag in SHAPES:
            color=COLORS.get(roles.get(f'p{i}','other'),COLORS['other']);i+=1
            node.set('stroke',color);node.set('fill','none');node.set('stroke-width','.16')
        if tag=='text':node.set('fill','#a5aab2')
    dest.write_bytes(resvg_py.svg_to_bytes(svg_string=ET.tostring(root,encoding='unicode')))


def report(dataset,output):
    check_frozen(dataset,output)
    inventory=json.loads((dataset/'inventory.json').read_text())
    audit=json.loads((output/'geometry_audit.json').read_text())
    selection=json.loads((output/'selection.json').read_text())
    reports={s:json.loads((output/s/'metrics.json').read_text()) for s in ('development','validation')}
    percent=lambda n:'—' if n is None else f'{100*n:.1f}%'
    rows=[]
    for split,r in reports.items():
        for cls,c in r['micro'].items():
            rows.append('<tr>'+''.join(f'<td>{v}</td>' for v in (split,cls,c['tp'],c['fp'],c['fn'],percent(c['precision']),percent(c['recall']),percent(c['f1'])))+'</tr>')
    cards=[];images=output/'images';images.mkdir(exist_ok=True)
    # First three preselected development samples, not best-scoring examples.
    for row in selection['development'][:3]:
        raw=read_sample(row,dataset);p,truth,d,h=parse_svg(raw)
        sid=row['sample_id'].replace('/','__')
        pred=json.loads((output/'development'/'predictions'/(sid+'.json')).read_text())
        overlay(raw,{eid:t['category'] for eid,t in truth.items()},images/(sid+'-truth.png'))
        overlay(raw,pred['entity_roles'],images/(sid+'-pred.png'))
        cards.append(f'<section><h3>{escape(row["sample_id"])}</h3><div class="pair"><figure><img src="images/{sid}-truth.png"><figcaption>标注：门 / 窗 / 墙</figcaption></figure><figure><img src="images/{sid}-pred.png"><figcaption>几何基线候选</figcaption></figure></div></section>')
    summary={s:{k:r[k] for k in ('samples','groups','primitives','parsed','incomplete_samples','micro','family_macro','elapsed_seconds')} for s,r in reports.items()}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    html='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>FloorPlanCAD 矢量识别公共方法 v1</title>
<style>body{font:16px/1.65 system-ui;margin:36px auto;max-width:1200px;color:#203047;padding:0 24px;background:#f7f9fc}section,.box{background:white;padding:22px;margin:20px 0;border:1px solid #dce3ec;border-radius:12px}h1{font-size:30px}h2{font-size:22px}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:9px;border-bottom:1px solid #ddd}.pair{display:grid;grid-template-columns:1fr 1fr;gap:12px}figure{margin:0}img{width:100%;background:white}small{color:#526177}code{background:#eaf0f7;padding:2px 5px}a{color:#185fa5}</style>
<h1>FloorPlanCAD：无预训练模型的矢量识别基线</h1>
<p>本轮完成数据隔离、几何公共方法、CAD 适配器和冻结验证。结果是候选识别基线，尚不能代替现有房间识别。</p>
<div class="box"><b>Test 保持封存</b><p>开发命令只允许 train-00.tar.xz 与 train-01.tar.xz；未打开、解压或评分 test 包。标签和实例 ID 与识别输入分离；不使用文件名、图层名、颜色或标注作为特征。没有加载外部模型权重，没有基于验证分数调参。</p></div>
'''+f'''<section><h2>数据与重复隔离</h2><p>训练来源共 {inventory['samples']:,} 份 SVG。原始文件哈希检查发现 {inventory['initial_exact_cross_split_duplicate_sets']} 组跨划分重复，隔离 {len(inventory['quarantine_groups'])} 个文件族、{inventory['splits'].get('quarantine',0)} 份样本。进一步去标签的几何检查隔离 {len(audit['quarantined_families'])} 个文件族，另去除 {len(audit['same_geometry_duplicates_removed'])} 份同几何重复样本。</p>
<p>最终开发评估 {reports['development']['samples']} 份、{reports['development']['groups']} 个文件族；验证评估 {reports['validation']['samples']} 份、{reports['validation']['groups']} 个文件族。按固定哈希顺序选样，先覆盖文件族，每族最多四份。文件名前缀只是暂定家族分组，未经证实等同独立项目；尚未完成旋转、平移、局部重叠等近重复审计。</p>
<p><a href="freeze.json">代码与参数冻结记录</a> · <a href="selection.json">固定样本清单</a> · <a href="geometry_audit.json">几何重复审计</a> · <a href="../dataset/protocol.json">数据协议</a></p></section>
<section><h2>逐图元候选识别结果</h2><p>门：原始类别 3–8；窗：9–11；墙：1。幕墙、洞口符号、栏杆不混入这些目标。P / R / F1 按图元计数，未识别和不支持的目标图元仍计入漏检；没有把候选数量当作准确率。</p>
<table><thead><tr><th>划分</th><th>类别</th><th>TP</th><th>FP</th><th>FN</th><th>Precision</th><th>Recall</th><th>F1</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p>开发集解析 {reports['development']['parsed']:,}/{reports['development']['primitives']:,} 个图元；验证集解析 {reports['validation']['parsed']:,}/{reports['validation']['primitives']:,} 个图元。超预算或识别不完整样本：开发 {reports['development']['incomplete_samples']}，验证 {reports['validation']['incomplete_samples']}。</p>
<p><a href="development/metrics.json">开发集逐样本及文件族指标</a> · <a href="validation/metrics.json">验证集逐样本及文件族指标</a></p></section>
<section><h2>公共方法与边界</h2><p><code>recognize_vectors(primitives)</code> 接收只包含几何的类型化图元；<code>recognize_cad_records(records)</code> 适配现有 WCS 几何记录，保留 INSERT 路径和多段线分段来源。独立函数可调用，未接入默认房间流程或新增 Agent 工具。</p>
<p>门候选依据圆弧与原生径向门扇；窗候选依据有两端封口的多轨平行线；墙候选依据带实测端部连接的双线。均采用无量纲关系参数，无单图、图号、图层或设计院特例。输出只能证明这些几何关系，家具和其他相似符号可能误检；当前基线也不覆盖全部门窗形式。</p>
<p>没有运行门洞自动闭合，没有计算房间边界准确率、净面积或实例 AP。本数据的逐图元类别标签不足以直接证明房间边界改善；后续必须另用有独立边界标注的真实图纸验证。</p></section>
<h2>固定开发样本可视化</h2><p>按选样顺序展示前三份，不挑选最佳案例。红色门、蓝色窗、绿色墙、紫色歧义，灰色为其他或未赋类图元。</p>
{''.join(cards)}
<section><h2>后续开发约束</h2><p>保留 v1 为不可覆盖基线。下一版只根据 development 的跨文件族错误类型提出可复用结构改进，并记录参数及消融依据；validation 只作固定版本检查，不逐例反馈到规则。test 当前没有开发读取入口，后续仅在方案和参数冻结后用于最终评价，不能用于参数选择。</p>
<p>来源：<a href="https://floorplancad.github.io/">FloorPlanCAD 官方站</a>；<a href="https://github.com/VITA-Group/CADTransformer/blob/main/config/anno_config.py">官方研究代码的原始编号映射</a>。官方注明标注为 CC BY-NC 4.0；商业产品使用前需核实适用授权。</p></section></html>'''
    (output/'report.html').write_text(html)
    print(output/'report.html')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--dataset',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    args=p.parse_args();report(args.dataset,args.output)
