"""Audit frozen real-drawing runs against manually reviewed native table crops.

Reference bindings below come from the original table layout, never the business
extractor output. This is an audit artifact generator, not a product parser.
"""
import hashlib
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'artifacts/business-tables-real-e2e-20261026'
SCHEDULE = BASE.with_name(BASE.name + '-schedule')
HOSPITAL = BASE.with_name(BASE.name + '-hospital')


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def ref(texts, raw, y=None, block=None):
    result = [t for t in texts if t['text'] == raw and
              (y is None or abs(t['origin'][1] - y) < 250) and
              (block is None or t.get('block_name') == block)]
    assert result, (raw, y, block)
    return [dict(entity_id=t['entity_id'], bbox=t['bbox'], raw_text=t['text']) for t in result]


def table(headers, rows):
    esc = lambda v: html.escape(str(v))
    return '<table><thead><tr>' + ''.join('<th>'+esc(h)+'</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in r)+'</tr>' for r in rows) + '</tbody></table>'


def run_summary(folder, kind):
    path = folder/'live'/(kind+'-run.json')
    if not path.exists():
        return {'status': 'not_run'}
    r = read(path)
    t = read(folder/'live'/(kind+'-task.json'))
    result = dict(run_id=r['id'], status=r['status'], task_status=t['status'], model=r['model'],
                  model_profile=r['model_profile'], runtime_requested=r['runtime_requested'],
                  allow_fallback=r['request']['allow_fallback'], source_read_batching=r['request']['source_read_batching'],
                  tool_calls=len(r['trace']), tool_names=[x['tool'] for x in r['trace']],
                  failed_calls=[dict(tool=x['tool'],error=x.get('error')) for x in r['trace'] if x['status']!='completed'],
                  model_requests=len(r['usage']), usage_total=sum(u.get('total_tokens',0) for u in r['usage']),
                  billing=sorted({u.get('billing','unknown') for u in r['usage']}),
                  error=r.get('error'), answer=(r.get('result') or {}).get('answer',''))
    return result


def main():
    texts = read(BASE/'raw-texts.json')
    # Reviewed against metrics.png. Eight unambiguous, present target categories;
    # footprint naming is deliberately not equated with the raw 占地面积 label.
    metrics = [
        ('land_area','用地面积','12636.0','平方米','12636.0','数值出现，但最终答案将对应关系列为未确认'),
        ('total_building_area','总建筑面积','35104.7','平方米','35104.7','数值出现，但最终答案将对应关系列为未确认'),
        ('counted_area','计容建筑面积','25161.37','平方米','25161.37','遗漏：原表由同一行备注明确标注计容'),
        ('excluded_area','不计容建筑面积','9943.33','平方米','9943.33','遗漏：原表由同一行备注明确标注不计容'),
        ('floor_area_ratio','容积率','1.99','','1.99','数值正确，来自通用语义表读取'),
        ('building_density','建筑密度','28.96','%','28.96%%%','数字正确，但最终保留了错误显示的三重百分号'),
        ('green_ratio','绿地率','30.0','%','30.0%%%','数字正确，但最终保留了错误显示的三重百分号'),
        ('parking_spaces','机动车停车位','258','个','258','正确；地下227、地上31及其他分类也保留'),
    ]
    gold_metrics = [dict(field=k, label=label, value=v, unit=u, native_raw=raw,
                         source_refs=ref(texts,raw,block='指标11'), agent_audit=audit)
                    for k,label,v,u,raw,audit in metrics]
    parking = {'机动车停车位':'258','地下停车位':'227','地上停车位':'31',
               '非机动车停车位':'393','装卸停车位':'3'}
    for label, value in parking.items():
        ref(texts,label,block='指标11'); ref(texts,value,block='指标11')
    metric_gold = dict(method='manual_native_table_image_review_with_raw_entity_crosscheck',
        image='metrics.png', fields=gold_metrics, parking=parking,
        parking_notes=['含无障碍停车位5个','含充电桩停车位6个，其中快充2个、慢充4个','装卸停车位3个不计入总数'],
        source_absent=['green_area'],
        label_ambiguous=[dict(field='footprint_area',raw_label='占地面积',value='3659.3',unit='平方米',
                             note='保留原称；不强行认定与建筑基底面积等同。',source_refs=ref(texts,'3659.3',block='指标11'))])
    write(BASE/'reference.json',metric_gold)

    texts = read(SCHEDULE/'raw-texts.json')
    rows = [('1','D1#办公','16F','1097.92','17499.43',302925),
            ('2','D2#办公','16F','1089.79','17499.14',301405),
            ('3','D3#宿舍','19F','3598.12','56235.34',299900),
            ('4','D4#厂房','10F','2575.50','26066.96',298380),
            ('5','D5#厂房','10F','2581.38','26072.84',296855),
            ('6','地下室','-/-2F',None,'45501.98',295410)]
    gold_rows = []
    for ordinal,name,storeys,foot,total,y in rows:
        values=dict(ordinal=ordinal,name=name,storeys=storeys,footprint_area=foot,total_building_area=total)
        gold_rows.append(dict(values=values, source_refs={k:ref(texts,v,y) for k,v in values.items() if v is not None}))
    schedule_gold=dict(method='manual_native_table_image_review_with_raw_entity_crosscheck',
        image='building-schedule.png',title='建筑物一览表（四期）',rows=gold_rows,
        explicit_target_cells=sum(v is not None for r in gold_rows for v in r['values'].values()),
        source_absent_columns=['use','height','counted_area','excluded_area'],
        absent_cells=[dict(ordinal='6',field='footprint_area',source_state='blank')],
        summary=dict(footprint_area='10942.71',aboveground_area='143373.71',underground_area='45501.98',total_building_area='188875.69'),
        note='办公/宿舍/厂房保留在原名称中，不推断独立使用性质；地上/地下不等同计容/不计容；F及-/-2F保留原文。')
    write(SCHEDULE/'reference.json',schedule_gold)
    # Verify frozen implementation has not drifted since the run started.
    manifest=read(BASE/'code-manifest.json')
    assert all(hashlib.sha256((BASE/'product'/p).read_bytes()).hexdigest()==v for p,v in manifest.items())
    metric_pages=[page for path in (BASE/'planning_metrics').glob('table_*.json') for page in read(path)]
    metric_fields=[f for page in metric_pages for f in page.get('fields',[])]
    schedule_discovery=read(SCHEDULE/'building_schedule/discovery.json')
    audit=dict(verdict='failed_real_drawing_acceptance',frozen_source_verified=True,
        planning_metrics=dict(expected_present_target_categories=8,structured_fields_returned=len(metric_fields),
                              diagnostics=[p.get('diagnostics') for p in metric_pages],
                              agent=run_summary(BASE,'planning_metrics')),
        building_schedule=dict(expected_rows=6,expected_explicit_target_cells=29,
                               discovered_candidates=schedule_discovery['total'],
                               structured_rows_returned=0 if schedule_discovery['total']==0 else None,
                               agent=run_summary(SCHEDULE,'building_schedule')),
        limits=['Three real drawing samples, not a representative generalization benchmark.',
                'Visual references use native DXF rendering; no independent DWG desktop renderer was used.',
                'Schedule uses archived real DXF, not fresh conversion of current DWG; isolated upload limit raised to 128 MiB.',
                'No business parser repairs were made in this validation.'])
    write(BASE/'audit.json',audit)
    ma=audit['planning_metrics']['agent'];sa=audit['building_schedule']['agent']
    sections=[
        '<h1>两个通用提取入口：真实图纸验证</h1>',
        '<p class="fail">结论：当前冻结版本未通过真实图纸的完整、准确提取验收。</p>',
        '<p>本次保留了 DWG/DXF 哈希、冻结代码、原生文字、表格索引、提取结果及真实订阅调用记录。两个业务入口均未产出可用结构化结果；模型通用读取补救不能算作专用入口通过。</p>',
        '<h2>验证范围与结果</h2>',
        table(['样本','验证范围','直接调用结果'],[
            ['尚桥头总平面 DWG','规划指标表；人工核对原图','1个候选，HEADER_UNRESOLVED，0个结构化字段'],
            ['第六医院总图 DWG','多份经济技术指标表','4个候选全部 HEADER_UNRESOLVED；无一览表候选'],
            ['汇能B说明图（历史真实DXF）','建筑物一览表（四期）6行+合计','0个候选，0/6行；29个有值的目标单元格全部未输出']]),
        '<h2>规划指标逐项核对</h2>',
        '<p>8个明确有值的目标类别均未由专用入口提取。绿地面积原表未直接给值；“占地面积3659.3平方米”保留原称，不强行认定为建筑基底面积。</p>',
        table(['字段','原图值','专用入口','真实Agent最终答案核对'],[(x['label'],x['value']+' '+x['unit'],'未输出',x['agent_audit']) for x in gold_metrics]),
        '<p>停车分类：机动车258、地下227、地上31、非机动车393；装卸3个明确不计入总数。无障碍5、充电桩6（快充2、慢充4）属于包含关系，不另加总。</p>',
        '<a href="metrics.png"><img src="metrics.png" alt="尚桥头原生指标表" style="max-width:700px"></a>',
        '<h2>一览表逐行核对</h2>',
        table(['编号','名称','楼层数原文','基底面积㎡','总建筑面积㎡','专用入口'],[(a,b,c,d or '原表空白',e,'整行遗漏') for a,b,c,d,e,y in rows]),
        '<p>合计行：基底10942.71㎡、地上143373.71㎡、地下45501.98㎡、总建筑188875.69㎡。原表没有独立使用性质、建筑高度、计容和不计容列；这些属于原图缺列，不计为提取遗漏。地下室基底为空，不填零。</p>',
        '<a href="../business-tables-real-e2e-20261026-schedule/building-schedule.png"><img src="../business-tables-real-e2e-20261026-schedule/building-schedule.png" alt="真实建筑物一览表" style="max-width:1100px"></a>',
        '<h2>真实订阅运行</h2>',
        table(['入口','运行状态 / 任务状态','模型请求数','工具调用数','运行ID'],[
            ['规划指标',ma.get('status','')+' / '+ma.get('task_status',''),ma.get('model_requests',0),ma.get('tool_calls',0),ma.get('run_id','')],
            ['一览表',sa.get('status','')+' / '+sa.get('task_status',''),sa.get('model_requests',0),sa.get('tool_calls',0),sa.get('run_id','')]]),
        '<p>使用 siwc_sol / gpt-6.1-sol、responses_subscription，allow_fallback=false、source_read_batching=false。流程 completed 只表示结束，不代表业务验收通过。一览表首次请求在工具执行前报 SIWC_STREAM_FORMAT，已保留 attempt-1；本表记录重试结果。</p>',
        '<p>两次有效模型运行的任务状态均为partial。规划指标借助通用表格读取恢复了部分结果，但未读全面积范围。一览表只确认标题，表格概览还有未读分页；后续调用受到TASK_NOT_EXECUTABLE / no_requirement_progress阻断，未返回任何建筑记录。最终一览表答复还将候选发现标为“待确认测量”，与本次原文提取任务不符。</p>',
        '<details><summary>规划指标最终答案</summary><pre>'+html.escape(ma.get('answer',''))+'</pre></details>',
        '<details><summary>一览表最终答案</summary><pre>'+html.escape(sa.get('answer','') or json.dumps(sa.get('error'),ensure_ascii=False))+'</pre></details>',
        '<h2>已定位的原因与修复顺序</h2>',
        '<ol><li>先修复上游表格结构：一张表被拆成多个片段，跨列和跨行单元格被错误合并，底图文字混入表格。汇能B的“建筑名称”和“楼层数”混在同一单元格，3/4/5行合并；总面积又在另一片段。</li><li>增加表格标题与表体的有证据关联，以及无显式表头的键值表读取。当前标题在候选表包络外就不能参与发现；尚桥头首行已是指标数据，强制要求表头导致全部退回。</li><li>补充真实表头和表示法：建筑编号、楼层数、机动车位数量，以及备注列的计容/不计容、F楼层表示。应在结构和来源归属明确之后映射，不能仅靠扩大关键词匹配。</li><li>区分 CAD 百分号编码与展示值，保留原始来源并提供经规则验证的显示归一化，避免28.96%%%直接交付。</li><li>核查有未读来源时的任务无进展判定与恢复路径，避免发现真实标题后过早阻断后续读取；纠正将原文提取标为测量的交付分类。</li><li>用本次冻结样本回归专用入口、完整分页和真实Agent交付，分别统计原图缺项、解析遗漏、错误归属及无法确认。</li></ol>',
        '<h2>证据与复现</h2>',
        '<p><a href="audit.json">审计汇总JSON</a> · <a href="reference.json">规划指标人工参考</a> · <a href="../business-tables-real-e2e-20261026-schedule/reference.json">一览表人工参考</a> · <a href="code-manifest.json">冻结代码哈希</a> · <a href="live/planning_metrics-run.json">规划指标完整运行</a> · <a href="../business-tables-real-e2e-20261026-schedule/live/building_schedule-run.json">一览表完整运行</a></p>',
        '<p>验证脚本：scripts/validate_business_tables_real.py；审计生成：scripts/audit_business_tables_real.py。参考值由原生图像人工核对，并用源文字实体逐项交叉检查，不来自待测方法结果。未改动产品解析器，也未将失败样本替换成成功样本。</p>',
        '<p>限制：本次仅三个真实样本，不代表总体准确率；图像来自转换后DXF原生渲染，未使用独立DWG桌面渲染器。汇能B使用历史DXF（来源及哈希见cached-input-provenance.json），仅隔离验证环境将上传上限从32MiB提高至128MiB以保留完整91MiB图纸，业务代码不变。</p>'
    ]
    (BASE/'report.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>真实图纸提取验证</title><style>body{max-width:1160px;margin:40px auto;padding:0 24px;font:16px/1.7 system-ui;color:#202934}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{border:1px solid #cbd5df;padding:9px;text-align:left;vertical-align:top}th{background:#eef3f7}h2{margin-top:36px}.fail{font-weight:700;color:#a02020}img{width:100%;height:auto;border:1px solid #ddd}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#1761a0}</style><body>'+''.join(sections)+'</body></html>')
    print(json.dumps({k:v for k,v in audit.items() if k not in ('planning_metrics','building_schedule')},ensure_ascii=False))


if __name__=='__main__':
    main()
