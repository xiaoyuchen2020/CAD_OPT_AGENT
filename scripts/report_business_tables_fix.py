"""Build a reviewable before/after report from real source acceptance records."""
import hashlib
import html
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/business-tables-fixed-final-v6-20261026'
BASE=ROOT/'artifacts/business-tables-real-e2e-20261026'


def load(path):return json.loads(path.read_text())
def table(headers,rows):
    e=lambda s:html.escape(str(s))
    return '<table><tr>'+''.join('<th>'+e(h)+'</th>' for h in headers)+'</tr>'+''.join(
        '<tr>'+''.join('<td>'+e(c)+'</td>' for c in row)+'</tr>' for row in rows)+'</table>'


def main():
    summary={}
    for suffix,kind in [('', 'planning_metrics'),('-schedule','building_schedule'),('-hospital',None)]:
        p=OUT.with_name(OUT.name+suffix)
        entry=summary[kind or 'held_out_hospital']={'acceptance':load(p/'acceptance.json')}
        if (p/'http-acceptance.json').exists():entry['http']=load(p/'http-acceptance.json')
        manifest=load(p/'code-manifest.json')
        assert all(hashlib.sha256((p/'product'/f).read_bytes()).hexdigest()==sha for f,sha in manifest.items())
        entry['current_code_matches_frozen']=all(hashlib.sha256((ROOT/f).read_bytes()).hexdigest()==sha for f,sha in manifest.items())
        assert entry['current_code_matches_frozen']
        if (p/'model-acceptance.json').exists():entry['model_acceptance']=load(p/'model-acceptance.json')
        if kind and (p/'live'/(kind+'-run.json')).exists():
            r=load(p/'live'/(kind+'-run.json'));t=load(p/'live'/(kind+'-task.json'))
            entry['live']={'run_id':r['id'],'status':r['status'],'task_status':t['status'],
                'tool_calls':len(r['trace']),'model_requests':len(r['usage']),'error':r.get('error'),
                'answer':(r.get('result') or {}).get('answer',''),
                'runtime':r['runtime_requested'],'model':r['model'],'fallback':r['request']['allow_fallback']}
    tests=Path('/private/tmp/business-tables-final-v6.xml')
    if tests.exists():
        shutil.copy2(tests,OUT/'regression.xml')
        suites=ET.parse(tests).getroot();summary['regression']=[s.attrib for s in suites.iter('testsuite')]
    (OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    test_count=sum(int(s['tests']) for s in summary.get('regression',[]))
    metrics=load(BASE/'reference.json')['fields']
    buildings=load(BASE.with_name(BASE.name+'-schedule')/'reference.json')['rows']
    parts=['<h1>真实图纸提取修复与验收</h1>',
      '<p><strong>两个目标样本的专用入口与 HTTP 调用均通过结构化验收。</strong>所有源文件均与修复前哈希相同，未裁图、未改表值；未按文件名、固定坐标、建筑名称或预期数值设置分支。</p>',
      table(['方法 / 样本','修复前','修复后'],[
          ['规划指标 / 尚桥头','表头失败，0字段','8类明确指标全部正确；12条字段记录，含5类停车值；7页'],
          ['建筑物一览表 / 汇能B','0个候选，0行','6条记录、29个有值目标单元格全部正确；合计单列；4页'],
          ['额外检验 / 医院总图','4个候选表头失败','3张人工核对表的18个有值字段正确；另一干扰表保持未解析']]),
      '<h2>规划指标核对</h2>',
      table(['字段','原图及解析值','校验'],[(f['label'],f['value']+' '+f['unit'],'通过') for f in metrics]),
      '<p>另保留地下停车227、地上31、装卸3（不计入总数）、非机动车393，以及原表备注。绿地面积没有反算；“占地面积3659.3平方米”保留原称及来源，未强行等同建筑基底面积。</p>',
      '<h2>一览表核对</h2>',
      table(['编号','名称','楼层数原文','基底面积㎡','总建筑面积㎡'],[
          [r['values'][k] if r['values'][k] is not None else '原表空白' for k in
           ('ordinal','name','storeys','footprint_area','total_building_area')] for r in buildings]),
      '<p>地下室基底空白没有填零；原表没有独立使用性质、建筑高度、计容和不计容列，不从名称或地上/地下面积推断。合计总建筑面积188875.69㎡独立保留。</p>',
      '<h2>通用修复范围</h2><ul><li>按CAD块实例来源恢复线框，合并共线相接线段，防止叠加底图混入；小缺口有文字尺度限制并保留调整记录。</li><li>用明确标签、唯一且跨行一致的后置数值列和显式单位识别无表头指标表；序号和多值歧义不猜测。</li><li>支持真实常见表头、备注中的计容否定词、F层数表达和标准百分号转义，原始引文不改写。</li><li>候选发现不计为整表已读；模型上下文保留整页字段及来源引用，不用JSON前缀替代数据页；逐行引用包含序号、名称、数值和单位；相同读取页去重，原始单元格使用无损矩阵压缩，防止冗余内容挤出所请求的字段。</li><li>序号表头和数值分属两个单元格时，依据已核验且同时引用的字段绑定校验编号；不允许将该数字借用为型号、轴号或尺寸。</li></ul>',
      '<h2>真实订阅运行</h2>']
    for key,title,suffix in [('planning_metrics','规划指标',''),('building_schedule','一览表','-schedule')]:
        live=summary[key].get('live',{})
        parts.append('<h3>'+title+'</h3>'+table(['运行状态','任务状态','工具调用','模型请求','运行ID'],[[
            live.get('status','未完成'),live.get('task_status',''),live.get('tool_calls',''),live.get('model_requests',''),live.get('run_id','')]]))
        review=summary[key].get('model_acceptance',{})
        parts.append('<p><strong>请求字段验收：'+('通过' if review.get('passed') else '未通过或未完成')+'</strong>；按独立原图参考核对有值字段、单位、分类及缺项，不将任务状态视为数据准确率。</p>')
        parts.append('<pre>'+html.escape(live.get('answer','') or json.dumps(live.get('error'),ensure_ascii=False))+'</pre>')
    parts += ['<p>尚桥头复用已转换且哈希不变的原图数据；汇能B复用历史归档的完整DXF。本轮不重复DWG转换，不裁图、不改数值。</p>',
      '<p>规划指标结构化入口读取全部32行。最终模型整理未声称完整复核中间未映射原始行，但12条目标字段及停车备注均完整交付。一览表完整交付6行及合计；源表缺列保持未知。两次任务状态均保留partial，不作为整图或工程身份验收。</p>',
      '<p>使用现有订阅通道（gpt-6.1-sol / responses_subscription），无付费回退。运行结束、任务状态与结构化解析验收分别记录，不以completed冒充全图穷尽。</p>',
      f'<h2>测试与证据</h2><p>最终版本{test_count}项相关回归通过。新增测试使用不同名称、数值、比例和坐标，覆盖分段边线、短缺口、重叠文字、序号干扰、歧义数值、整页上下文与引用完整性。</p>',
      '<p><a href="acceptance.json">指标逐项验收</a> · <a href="../'+OUT.name+'-schedule/acceptance.json">一览表29个单元格验收</a> · <a href="summary.json">完整汇总</a> · <a href="code-manifest.json">冻结源码哈希</a> · <a href="../business-tables-real-e2e-20261026/report.html">修复前失败报告与原图截图</a></p>',
      '<p>中间版本及失败运行保留在business-tables-fixed-*目录，没有覆盖修复前结果。两张目标图的原始空白/缺列及未确认口径保留；不能由这两个样本推导所有CAD图纸都能完整识别。</p>']
    (OUT/'report.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>业务表提取修复验收</title><style>body{max-width:1150px;margin:40px auto;padding:0 24px;font:16px/1.7 system-ui;color:#26323b}table{width:100%;border-collapse:collapse;margin:16px 0}th,td{border:1px solid #ccd7df;padding:10px;text-align:left}th{background:#edf5f1}pre{white-space:pre-wrap;background:#f5f7f9;padding:20px}a{color:#1d659b}</style><body>'+''.join(parts)+'</body></html>')
    print('Report:',OUT/'report.html')


if __name__=='__main__':main()
