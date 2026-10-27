"""Evidence-backed corpus report; reviewed findings are not blanket gold labels."""
from collections import Counter
import csv
import hashlib
import html
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'artifacts/business-tables-corpus-20261026'
def read(path):return json.loads(path.read_text())
def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
def esc(value):return html.escape(str(value))


def findings():
    return [
      dict(id='BT-CORPUS-001',priority='P1',kind='confirmed_failure',title='坏候选表导致规划指标发现整体异常',
        cases=['24-4e1dc15b','25-8e421d47'],
        observed='汇能E返回NON_RECTANGULAR_CELL_COMPONENT；汇能F返回“表格网格超过有界分析范围”。两次extract_planning_metrics发现请求均抛异常，未返回可继续处理的候选页。',
        expected='将单表结构失败记录为诊断，隔离坏候选并继续检查其他表；保留网格安全上限。',
        basis='result.json中的DISCOVERY_EXCEPTION；冻结代码_discover调用_headers时未隔离单表grid异常。',
        evidence=['cases/24-4e1dc15b/result.json','cases/25-8e421d47/result.json'],images=[]),
      dict(id='BT-CORPUS-002',priority='P1',kind='confirmed_omission',title='楼号、子项及功能表头未形成建筑记录',
        cases=['03-47b890bf','05-5aab4a78','24-4e1dc15b'],
        observed='城建1的“子项主要特征”有7条数据行；城建2的“新建项目单体工程概况一览表”有3条；汇能E的单体指标表有6条。建筑入口全部返回0条。原始网格可见“楼号”“属性”“主要功能”等明确表头。',
        expected='支持建筑身份和功能的通用表头变体并保留原称；不可将楼号强制简化成数字序号，或把多种高度混为一个值。',
        basis='原生文字/线框回看与table-index.json、方法返回逐项对照；多个缺陷可能叠加，不宣称仅加别名即可全部修复。',
        evidence=['cases/03-47b890bf/table-index.json','cases/05-5aab4a78/building_schedule/table_05bf07a7233dce3c7d08c2ca.json','cases/24-4e1dc15b/table-index.json'],
        images=['cases/03-47b890bf/review/embedded-features-detail.png','cases/05-5aab4a78/review/schedule-unmapped.png','cases/24-4e1dc15b/review/school-schedule.png']),
      dict(id='BT-CORPUS-003',priority='P1',kind='confirmed_header_limitation',title='分离的面积上标2破坏字段名和单位识别',
        cases=['03-47b890bf','24-4e1dc15b'],
        observed='源表平方米单位由m与上标2分成实体，网格文本出现“建筑面积\\n2\\n(m )”“计容建筑面积\\n2\\n(m )”。去括号后标签含多余2，不能匹配面积字段。',
        expected='依据同一表头单元格的源实体位置恢复上标单位；标签与单位独立归一，保留原始字符和位置，不把任意邻近2拼入单位。',
        basis='原始网格表头与原生图对照；字段未被识别是确定现象，整体漏行还叠加身份表头等问题。',
        evidence=['cases/03-47b890bf/table-index.json','cases/24-4e1dc15b/table-index.json'],
        images=['cases/24-4e1dc15b/review/school-schedule.png']),
      dict(id='BT-CORPUS-004',priority='P1',kind='confirmed_index_loss',title='嵌入式建筑小表并入整页说明框',
        cases=['01-4692b93e','29-e06df74c'],
        observed='原图“子项”表列出1#办公至地下室共6行，含面积、层数、规划和消防高度。索引对应区域为整页2行×16列大表，小表多行内容合入长文本，建筑入口无候选。',
        expected='保留可靠的嵌套小表，不能仅因大包围框包含就丢弃内部表；候选粒度应由表结构支持。',
        basis='两份文件的原生文本与table-index一致；并非把无候选推定为源图没有数据。',
        evidence=['cases/01-4692b93e/table-index.json','cases/29-e06df74c/table-index.json'],
        images=['cases/01-4692b93e/review/embedded-schedule.png']),
      dict(id='BT-CORPUS-005',priority='P1',kind='confirmed_index_loss',title='住宅区概况表丢失首行字段表头',
        cases=['22-c8a9d664'],
        observed='源图有“层数、建筑高度、住宅/配套地上建筑面积”等表头，其Y下界约159603；候选网格上边界为159578.16，首行只剩“(层)、(米)、(户)、(平方米)”等单位。两个入口均未发现目标候选。',
        expected='修复网格包络/多层表头完整性；同时保留2#楼跨行身份和面积口径，不擅自把占地等同基底。',
        basis='源实体bbox与网格bbox直接比较，原图回看确认完整表头确实存在。',
        evidence=['cases/22-c8a9d664/table-index.json','cases/22-c8a9d664/review/header-clipped.json'],
        images=['cases/22-c8a9d664/review/header-clipped.png']),
      dict(id='BT-CORPUS-006',priority='P1',kind='confirmed_partial_omission',title='停车分类标签变体漏提5个有值分项',
        cases=['11-fa75e535'],
        observed='城建7总图返回8条指标，包含机动车708辆和非机动车1026辆，但漏掉地上机动车24、地下机动车684、装卸车5、地上非机动车476、地下非机动车550。',
        expected='组合识别车辆类型、地上/地下位置和停车位标签，逐分项保留原称及来源；不自行汇总或把分项当总数。',
        basis='人工回看原表并与全部7页结构化输出比对。8条已输出主值匹配不代表停车分类完整。',
        evidence=['cases/11-fa75e535/review/reference-and-comparison.json','cases/11-fa75e535/planning_metrics/table_bd00c08540eb767f9cb97d40.json'],
        images=['cases/11-fa75e535/review/metrics.png']),
      dict(id='BT-CORPUS-007',priority='P2',kind='confirmed_context_contamination',title='底图高程混入指标限定信息',
        cases=['11-fa75e535'],
        observed='绿地率主值30%正确，但qualifiers包含“≥\\n30%\\n2.88”；2.88来自重叠底图高程，标题区域也混入1.96。字段原文可核验并不能证明这些文字都属于同一业务表。',
        expected='加强同坐标叠加内容的来源/结构归属校验，不能把所有相交文字都作为业务限定；无法区分时单独标为干扰。',
        basis='原生图中的地形线、高程位置与字段限定信息对照。',
        evidence=['cases/11-fa75e535/planning_metrics/table_bd00c08540eb767f9cb97d40.json','cases/11-fa75e535/review/metrics.json'],
        images=['cases/11-fa75e535/review/metrics.png']),
      dict(id='BT-CORPUS-008',priority='P2',kind='confirmed_capability_boundary',title='说明段落中的项目指标不在当前表格入口覆盖范围',
        cases=['01-4692b93e','29-e06df74c'],
        observed='项目说明明确写出用地15909平方米、总建筑面积100588.11平方米、容积率4.50、建筑密度39.56%、绿地率20%，但规划指标入口没有提取这些段落值。',
        expected='如业务入口要求覆盖建筑说明全文，应增加有来源绑定的段落指标读取分支；当前有线框表格的能力边界需要在产品结果中明确表达。',
        basis='已确认的能力范围不足，区别于已承诺表格场景内的解析缺陷；其他图的标题或空白模板不能据此全部判为遗漏。',
        evidence=['cases/01-4692b93e/review/prose-metrics.json','cases/01-4692b93e/result.json','cases/29-e06df74c/result.json'],
        images=['cases/01-4692b93e/review/prose-metrics.png']),
      dict(id='BT-CORPUS-009',priority='P1',kind='confirmed_import_blocker',title='两张DWG转换持续失败，方法测试受阻',
        cases=['17-0f4bab48','23-dabcd356'],
        observed='汇能4孔浦说明和汇能D建筑设计总说明均返回ODA_CONVERSION_FAILED；首轮后各独立复核一次，仍失败。未进入两个提取方法。',
        expected='查看转换服务对这两个文件的原生错误日志，区分格式、代理对象、损坏或资源限制；目前不能认定具体原因。',
        basis='两次结果和首次日志均保留。转换失败不等于方法无候选，也不是解析准确性失败。',
        evidence=['cases/17-0f4bab48/result.json','cases/17-0f4bab48/attempts/1/result.json','cases/23-dabcd356/result.json','cases/23-dabcd356/attempts/1/result.json'],images=[]),
    ]


def references(results):
    bycase={r['case']['case_id']:r for r in results}
    p=OUT/'cases/11-fa75e535';pages=read(p/'planning_metrics/table_bd00c08540eb767f9cb97d40.json')
    fields=[f for page in pages for f in page['fields']]
    expected=[('land_area','29873'),('total_building_area','96761.8'),('floor_area_ratio','2.03'),('footprint_area','8876.63'),
              ('building_density','29.71'),('green_ratio','30'),('parking_spaces','708'),('parking_spaces','1026')]
    assert [(f['field'],f['value']) for f in fields]==expected
    omitted=[('地上机动车停车位','24'),('地下机动车停车位','684'),('装卸车停车位','5'),('地上非机动车停车位','476'),('地下非机动车停车位','550')]
    for label,value in omitted:assert not any(label in f['source_labels'] for f in fields)
    save(p/'review/reference-and-comparison.json',dict(method='manual_native_scene_review_and_raw_source_crosscheck',
         correct_returned_values=expected,missing_source_rows=omitted,qualifier_contamination='green_ratio includes topographic 2.88',
         file_acceptance=False,reason='Returned main values match, but parking subcategories omitted and qualifiers contaminated.'))
    p=OUT/'cases/21-0942faf4';m=bycase[p.name]['methods']['building_schedule'];rows=[];summary=[]
    for table in m['tables']:
        for page in read(p/'building_schedule'/(table['table_id']+'.json')):
            rows+=page.get('records',[]);summary+=page.get('summary_rows',[])
    gold=read(ROOT/'artifacts/business-tables-real-e2e-20261026-schedule/reference.json')
    assert len(rows)==6 and len(summary)==1
    checks=[]
    for row,expected in zip(rows,gold['rows']):
        actual={f['field']:f for f in row['fields']}
        for key,value in expected['values'].items():
            normal=value[:-1] if key=='storeys' and value and value.endswith('F') and value[:-1].isdigit() else value
            assert actual[key]['value']==normal
            assert actual[key]['state']==('missing' if value is None else 'observed')
            if value is not None:checks.append(dict(ordinal=expected['values']['ordinal'],field=key,value=normal))
    assert {f['field']:f['value'] for f in summary[0]['fields']}['total_building_area']=='188875.69'
    save(p/'reference-comparison.json',dict(passed=True,scope='source-present requested cells in the four-phase building schedule',rows=6,explicit_target_cells=29,comparisons=checks,summary_total='188875.69',whole_drawing_acceptance=False))


def method_status(result,kind):
    if result['status']!='completed':return '导入失败／未运行'
    m=result['methods'][kind]
    if m['status']=='error':return '执行异常'
    if kind=='building_schedule' and m['record_count']:return f"{m['record_count']}条建筑记录"
    if m['field_count']:return f"{m['field_count']}条指标"
    if m['candidates']:return f"{m['candidates']}个候选／无提取值"
    return '未匹配候选'


def main():
    manifest=read(OUT/'manifest.json');results=[read(OUT/'cases'/c['case_id']/'result.json') for c in manifest]
    assert len(results)==39
    frozen=read(OUT/'code-manifest.json')
    assert all(hashlib.sha256((OUT/'product'/f).read_bytes()).hexdigest()==h for f,h in frozen.items())
    assert all(hashlib.sha256(Path(c['path']).read_bytes()).hexdigest()==c['sha256'] for c in manifest)
    previous=read(ROOT/'artifacts/business-tables-fixed-final-v6-20261026/code-manifest.json')
    changes=sorted(f for f in previous.keys()|frozen.keys() if previous.get(f)!=frozen.get(f))
    references(results);issues=findings();save(OUT/'reviewed-issues.json',issues)
    auto=[dict(case_id=r['case']['case_id'],filename=r['case']['name'],**i) for r in results for i in r['issues']]
    save(OUT/'automatic-findings.json',auto)
    methods={}
    for kind in ('planning_metrics','building_schedule'):
        ms=[r['methods'][kind] for r in results if kind in r['methods']]
        methods[kind]=dict(executed=len(ms),exception_files=sum(m['status']=='error' for m in ms),
            nonempty_files=sum(m['field_count']>0 for m in ms),no_candidate_files=sum(m['status']=='no_candidate' for m in ms),
            candidates_without_fields=sum(m['status']!='error' and m['candidates']>0 and not m['field_count'] for m in ms),
            candidate_count=sum(m['candidates'] for m in ms),field_objects=sum(m['field_count'] for m in ms),
            building_records=sum(m['record_count'] for m in ms),field_states=dict(sum((Counter(m['field_states']) for m in ms),Counter())))
    summary=dict(files=39,imported=37,import_failed=2,methods=methods,reviewed_issue_groups=len(issues),
        automatic_findings=len(auto),automatic_findings_by_code=dict(Counter(i['code'] for i in auto)),
        import_diagnostics=dict(Counter(d['code'] for r in results for d in r.get('drawing',{}).get('diagnostics',[]))),
        controls={kind:read(OUT/'controls'/kind/'result.json')['counts'] for kind in methods},
        scope=read(OUT/'scope.json'),code_changes_since_previous_two_drawing_test=changes,
        source_hashes_unchanged=True,frozen_code_verified=True,
        current_workspace_code_drift=[f for f,h in frozen.items() if not (ROOT/f).exists() or hashlib.sha256((ROOT/f).read_bytes()).hexdigest()!=h])
    save(OUT/'summary.json',summary)
    with (OUT/'file-results.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(['编号','图纸','导入状态','规划指标','建筑一览表','人工确认问题ID','自动待查项数','结果路径'])
        for r in results:
            c=r['case'];writer.writerow([c['case_id'],c['name'],r['status'],method_status(r,'planning_metrics'),method_status(r,'building_schedule'),
                ';'.join(i['id'] for i in issues if c['case_id'] in i['cases']),len(r['issues']),f"cases/{c['case_id']}/result.json"])
    names={c['case_id']:c['name'] for c in manifest}
    def table(headers,rows):return '<table><thead><tr>'+''.join('<th>'+esc(x)+'</th>' for x in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+str(x)+'</td>' for x in row)+'</tr>' for row in rows)+'</tbody></table>'
    parts=['<h1>建筑设计说明：39张图纸批量测试</h1>',
      '<p class="lead">39张全部尝试；37张导入后执行两个方法，2张转换持续失败。已确认存在泛化遗漏，不能将“执行完成”视为“提取完整”。</p>',
      table(['检查项','结果'],[['规划指标入口','37张执行：1张有8条指标，10张有候选但无字段，24张无候选，2张发现阶段异常'],
        ['建筑一览表入口','37张执行：1张返回6条建筑记录，2张有候选但无记录，34张无候选'],
        ['输出核对','城建7的8条主值匹配，但漏5个停车分项且限定信息被污染；汇能B的6行、29个有值目标单元格及合计通过核对'],
        ['控制回归','上次尚桥头指标表与汇能B一览表，在本轮冻结版本下分别保持8类/12条字段、6行/29个有值单元格'],
        ['测试范围','完整DWG转换/缓存校验→重新解析→两个入口所有候选与分页→来源引用核验；对问题区域人工回看原生文字/线框']]),
      '<p>本轮未逐图调用大模型，也未建立39张图的逐单元格人工金标准，因此不计算总体准确率。无候选不代表图纸不存在数据；候选不等于目标表。原图及业务算法均未在本轮修改。</p>',
      '<p><a href="summary.json">汇总JSON</a> · <a href="file-results.csv">逐图结果CSV</a> · <a href="reviewed-issues.json">9类已复核问题</a> · <a href="automatic-findings.json">自动待查项</a> · <a href="manifest.json">39张文件哈希</a> · <a href="code-manifest.json">冻结源码哈希</a></p>',
      '<h2>已复核问题与能力限制</h2><p>以下案例是已核对的代表样本，并非各问题在全目录的穷尽影响范围。优先处理异常隔离与明确表格遗漏，再扩展段落指标。</p>']
    for i in issues:
        parts.append(f'<section id="{i["id"]}"><h3>{i["priority"]} · {i["id"]}　{esc(i["title"])}</h3><p><b>现象：</b>{esc(i["observed"])}</p><p><b>建议：</b>{esc(i["expected"])}</p><p><b>证据及边界：</b>{esc(i["basis"])}</p>')
        parts.append('<p>已核对样本：'+ '；'.join(esc(names[c]) for c in i['cases'])+'</p>')
        parts.append('<p>'+ ' · '.join(f'<a href="{esc(e)}">证据{n}</a>' for n,e in enumerate(i['evidence'],1))+'</p>')
        if i['images']:
            parts.append('<details><summary>查看原生图证据</summary>'+''.join(f'<a href="{esc(img)}"><img loading="lazy" src="{esc(img)}" alt="原生CAD文字与线框复核"/></a>' for img in i['images'])+'<p>视图直接来自原生CAD文字和线段，未渲染提取器输出。字体排版为近似显示，精确原文和位置见同名JSON；原始DWG/DXF保留。</p></details>')
        parts.append('</section>')
    parts+=['<h2>全部39张逐图结果</h2><input id="filter" placeholder="按图纸名、状态或问题ID筛选" aria-label="筛选图纸">']
    rows=[]
    for r in results:
        c=r['case'];base='cases/'+c['case_id'];hits=[i for i in issues if c['case_id'] in i['cases']]
        rows.append([esc(c['case_id']),f'<a href="{base}/result.json">{esc(c["name"])}</a>',esc(method_status(r,'planning_metrics')),esc(method_status(r,'building_schedule')),
            '<br>'.join(f'<a href="#{i["id"]}">{i["id"]}</a>' for i in hits) or '未逐单元格验收',
            f'<details><summary>{len(r["issues"])}项自动记录</summary><pre>{esc(json.dumps(r["issues"],ensure_ascii=False,indent=2))}</pre></details>'])
    parts.append('<div id="files">'+table(['编号','图纸','规划指标','建筑一览表','已核对问题','诊断'],rows)+'</div>')
    parts+=['<h2>来源、版本与限制</h2>',
      '<p>输入清单与复制清单的SHA-256逐一核对，39份原文件测试前后不变。3份转换缓存通过原DWG、DXF、ODA JSON三重哈希核验后重新解析；其余36份已获授权发送到项目内网转换服务。2张失败样本各复核一次，仍失败，首次记录位于各目录attempts/1。</p>',
      '<p>37份导入含PREVIEW_UNSUPPORTED显示诊断；30份坐标单位未明确；3份有无目标定义的块引用未导出。预览类型不支持不等于表格文字缺失，坐标单位未知也不应覆盖表格明示面积单位；无法恢复的源对象会限制全图完整性判断。</p>',
      '<p>工作区在上次两图验收后已有其他更新。本轮冻结当前源码，并重新核对两张控制样本；与上次版本的差异文件已写入summary.json。所有批量样本使用同一个冻结版本，未随测试中途切换代码。</p>',
      '<p>复现脚本：scripts/validate_business_tables_corpus.py；原生复核脚本：scripts/review_business_tables_corpus.py；报告脚本：scripts/report_business_tables_corpus.py。每张图的独立data目录保留DWG、DXF、ODA结果、解析场景与来源索引，方法目录保存发现页和全部提取页。</p>',
      '<script>document.getElementById("filter").addEventListener("input",e=>{const q=e.target.value.toLowerCase();document.querySelectorAll("#files tbody tr").forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));});</script>']
    style='body{max-width:1300px;margin:36px auto;padding:0 24px;font:16px/1.7 system-ui;color:#23313d}h1{font-size:30px}.lead{font-size:20px;font-weight:600}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{padding:10px;border:1px solid #ccd6de;text-align:left;vertical-align:top}th{background:#edf3f7}a{color:#09618c}section{padding:16px 20px;margin:20px 0;background:#f7f9fb;border-left:4px solid #b67930}img{max-width:100%;margin:12px 0;border:1px solid #ccd6de}pre{white-space:pre-wrap;word-break:break-word;font-size:12px;max-height:400px;overflow:auto}input{width:70%;padding:12px;font-size:16px}summary{cursor:pointer}'
    (OUT/'report.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>39张图纸业务提取批测</title><style>'+style+'</style><body>'+''.join(parts)+'</body></html>')
    print('Report',OUT/'report.html');print(json.dumps({k:summary[k] for k in ('files','imported','import_failed','methods','reviewed_issue_groups')},ensure_ascii=False))


if __name__=='__main__':main()
