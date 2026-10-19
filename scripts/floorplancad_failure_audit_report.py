"""Source-backed audit delivery and proposed development-only verification plan."""
import html,json,time
from pathlib import Path
from scripts.floorplancad_failure_audit import OUT,ROOT,save,sha

NOTES={
'A01':('候选生成／几何表示','目视为带开启曲线的门；该标注实例实际由23条line构成，没有解析ARC。当前需要弧种子的路径未触及它。','表示缺口已由源类型确认；不据此断言120个无弧实例全是折线门。'),
'A02':('候选生成／种子关系','门弧与门扇在图中可见，源中有约84.65度的开启弧及把手小弧，仍未形成门候选。','不能仅归因于弧角度；径向叶片对应关系需进一步量测，当前未新增容差。'),
'A03':('候选生成／构件画法','raw 11窗实例包含平行线与斜线填充，当前窗候选未触及。','画法覆盖缺口可见；未将斜线或填充形态写入新规则。'),
'A04':('候选生成／局部框架','raw 9的多条横向窗线与竖向构件相接，现有候选池未覆盖这些标注部件。','未证明具体哪个底层几何条件是唯一原因；不能直接套加宽/放宽阈值。'),
'A05':('构件归属','窗主体横线已命中，但同一候选包含6条墙标注源图元；墙窗交接端部被整体赋予窗角色。','窗口候选生成与部件归属必须分开评价；不应把整窗删除。'),
'A06':('构件归属','窗主体已命中，候选同时包含2条墙标注图元，问题集中于两端共享边界。','源标注与局部几何支持混合归属解释，但不能直接据标签决定生产角色。'),
'A07':('标签覆盖／小部件','两条raw 0短线位于门扇轮廓两端，局部视觉与门扇端边相符；被补全后按原协议计误检。','只标记标签覆盖疑点，不修改原标签，也不由视觉宣布它们一定是门。'),
'A08':('标签覆盖／共享连接','新增raw 0短线位于双扇门的铰接/墙交接附近；单凭局部图无法可靠决定归属。','保持不确定；需要源分组及人工构件归属复核，不能简单当作背景删除。'),
'A09':('共享源几何／分组信息','墙与窗源图元在边界上完全重合，原图难以目视区分副本；精确坐标和源分组审计可以区分来源。','源分组不是已验证的物理构件ID；这里只确认当前坐标接口丢失了上下文。'),
'A10':('共享源几何／分组信息','窗横线与墙端相接，重合端盖的墙/窗副本在预测中同被窗候选吸收。','不按字面图层名判断类别；需要保留源归属候选和冲突信息。'),
'A11':('房间拓扑／门洞接口','门弧及径向门扇已命中，紫色虚线仅展示既有hinge到closed_tip代理线；缺少已确认宿主墙带和相邻房间。','代理线不是已确认闭合边；未执行门洞闭合或计算房间正确率。'),
'A12':('房间拓扑／墙面与门洞','已命中门扇与弧，周围有墙厚、填充及多层边界；当前结果没有说明闭合边采用哪侧墙面。','需先定义墙带、门洞端点及房间面。局部成环数量不能代替房间数量。')}

def plan(cases):
    selected=[dict(case_id=c['case_id'],sample_id=c['sample_id'],group_id=c['group_id'],source_sha256=c['source_sha256']) for c in cases]
    return dict(status='proposal_not_executed',scope=dict(analysis_families_only=154,excluded_check_families=486,new_check_families=0,initial_audit_families=selected,default_integration=False),
        objective='先判断正确门洞/墙带能否解决房间问题，再决定开发哪个识别环节；不以门扇细碎图元召回替代产品验收。',
        phase_0=dict(name='恢复与核验源上下文（仅诊断接口）',tasks=[
            '保留SVG文字、原始节点路径、父分组路径、坐标变换及原始几何；文字基线不是房间质心或文字bbox。',
            '源分组在实验输入中使用不透明ID；先核对其导出来源，确认不是由semantic-id/instance-id生成的答案包装。图层名称、颜色仅保留审计，不新增字面名称规则。',
            '检查直线链表示的门弧与解析ARC差异，只建立表示类型清单；不在此阶段拟合替换源几何或新增识别规则。']),
        phase_1=dict(name='小型开发参考集',sources='优先使用已审阅的12个分析族，在同一文件中扩大局部范围以包含完整相邻房间；确实不足时仅从原154族按预先固定顺序补齐。',
            target=dict(local_contexts=12,reviewed_door_openings_min=12,reviewed_wall_bands_min=12,complete_room_references_min=8),
            annotations=['门洞两端/净开口、对应墙带、开启方向是否可判定、观察线与需要闭合的线分别标注。','墙带两侧边链、转角/交接、断口、与门窗共享边界；不要只标一对孤立平行线。','房间内侧边界、内部参考点、相邻房间/开口关联、裁剪边界、无法判定区域。','每项记录源ID、节点路径、原图哈希、标注者和状态；另记不确定性，不改写FloorPlanCAD原标签。'],
            limitations='这是开发夹具，不是验证集或新的独立泛化证据。无法完整确认的房间列为不可评估并报告数量，不用虚构闭合边凑样本。'),
        phase_2=dict(name='固定下游的四组上界实验（尚未执行）',
            consumer='使用同一份冻结的无量纲局部拓扑实验消费者和同一批人工复核内部点；不直接给现有米制rooms.py传入假定单位，也不把人工点伪装为原生文字锚点。',
            arms=[dict(id='B0',openings='既有候选',walls='既有候选',purpose='实验输入基线，不能冒称生产房间结果'),dict(id='B1',openings='人工复核门洞参考',walls='既有候选',purpose='定位门洞识别瓶颈'),dict(id='B2',openings='既有候选',walls='人工复核墙带参考',purpose='定位墙带瓶颈'),dict(id='B3',openings='人工复核门洞参考',walls='人工复核墙带参考',purpose='判断拓扑消费者在正确输入下能否工作')],
            note='人工参考即oracle，仅用于诊断上界，不能作为识别成绩；四组消费者、预算、匹配方法必须相同并在运行前冻结。',
            decision_rules=['B3仍失败：优先修拓扑消费者/房间参考定义，停止继续补符号规则。','B1改善、B2不改善：优先门洞对象；B2改善、B1不改善：优先墙带连接。','只有B3改善：需要联合对象关系，不能只看单类F1。','几何输入正确但房间锚点错误：另开固定几何的原生文字锚点对照，不把它混入门窗归属调参。']),
        phase_3=dict(name='最小验收与停止条件',metrics=[
            '门洞：对象级精确率/召回、端点与开口宽度误差、宿主墙带关联正确性；计错误闭合，不计细碎装饰线收益。',
            '墙带：沿长度覆盖率、误纳入长度、转角/交接断连，以及共享边界冲突；相同源线副本不重复加分。',
            '房间：漏检、重复、错误合并/分裂、邻接关系、与复核边界的IoU及归一化边界偏差；单位未知时不报告平方米。'],
            gates=['源几何与引用100%保留，每条边注明observed/opening_closure/unresolved。','所有可评估小样本不新增错误闭合、合并或分裂，原正确房间不退化。','至少两个不同开发族出现可定位的房间级纠错，才能进入下一轮识别开发；这不代表泛化通过。','匹配容差由源坐标精度与标注不确定范围预先确定，禁止看算法结果后选择；未达参考数量时如实报告。','完成四组上界诊断之前，不提高预算、不新增规则、不使用新检查族。']))

def contracts():
    return dict(status='proposed_only_not_implemented',shared=dict(provenance=['source_sha256','sample_id','source_node_paths','source_primitive_ids','parent_group_opaque_ids','coordinate_transform'],
        forbidden_in_recognizer=['semantic-id','instance-id','file-family-specific overrides','literal layer-name exception rules'],
        roles='observed/supporting/conflicting/shared/unknown; multiple candidate memberships allowed; no destructive relabeling'),
        door_opening=dict(required=['opening_id','jamb_endpoints','host_wall_band_candidates','observed_source_ids','support_source_ids','conflicts','review_status'],
            optional=['opening_direction','dimensionless_clear_width','adjacent_room_candidates'],note='hinge→closed_tip仅是候选代理，不自动等于两侧墙面的闭合边。'),
        wall_band=dict(required=['band_id','left_face_chains','right_face_chains','junctions','gaps','source_ids','shared_opening_ids','review_status'],optional=['centerline','dimensionless_width_profile'],note='同一源部件可支持多个构件，分组ID保留上下文但不直接等于物理墙。'),
        room_boundary=dict(required=['room_candidate_id','seed','seed_origin','boundary_edges','edge_evidence_types','adjacency','scope_completeness','review_status'],
            optional=['inner_boundary_polygon','holes'],note='seed_origin区分原生文字、人工开发参考；未知单位禁止m²。'))

def run():
    summary=json.loads((OUT/'summary.json').read_text());meta=json.loads((OUT/'source_metadata_audit.json').read_text());rej=json.loads((OUT/'rejection_path_summary.json').read_text());presence=json.loads((OUT/'label_presence_audit.json').read_text());cases=json.loads((OUT/'case_index.json').read_text())['cases'];group=json.loads((OUT/'source_group_overlap_check.json').read_text())
    reviews=[]
    for c in cases:
        mechanism,observation,limit=NOTES[c['case_id']];reviews.append(dict(case_id=c['case_id'],sample_id=c['sample_id'],mechanism=mechanism,observation=observation,limitation=limit,reviewer='assistant_visual_review',expert_confirmed=False,original_labels_changed=False,image=c['image'],image_sha256=sha((OUT/c['image']).read_bytes())))
    save(OUT/'visual_review_notes.json',dict(reviewed_unix=time.time(),cases=reviews,scope='Assistant inspected all twelve rendered cases plus source coordinates/types. These are provisional diagnostic observations, not expert-adjudicated ground truth.'))
    proposed=plan(cases);save(OUT/'minimal_validation_plan.json',proposed);save(OUT/'proposed_interfaces.json',contracts())
    save(OUT/'development_reference_template.json',dict(status='unannotated_template_only',case_sources=proposed['scope']['initial_audit_families'],opening_references=[],wall_band_references=[],room_references=[],annotation_status='pending_review',original_labels_immutable=True,new_checks_allowed=False))
    parts=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>FloorPlanCAD 失败机制审计与最小验证方案</title><style>body{font:16px system-ui;max-width:1360px;margin:32px auto;padding:0 24px;background:#f7f9fc;color:#173147}p,li{line-height:1.7}h1{font-size:30px}table{border-collapse:collapse;width:100%;background:#fff;margin:18px 0}td,th{border:1px solid #d7e1eb;padding:9px;text-align:left}th{background:#e9f0f7}.box{border-left:4px solid #2e789f;padding:18px;background:#eaf3f8}img{width:100%;border:1px solid #d4dfe8}pre{white-space:pre-wrap;word-break:break-word}a{color:#145b8e}details{margin:20px 0}</style><h1>从图元规则转向门洞、墙带与房间：失败机制审计</h1>']
    parts.append('<p class="box"><b>本轮只有审计，没有新增识别规则。</b>固定154个开发分析族，486个旧检查族全部隔离，新检查族使用数为0。复用并核对冻结v5结果，未修改公共默认方法、产品预览、预算或原始标签。12个不同文件族案例已由助手结合源几何查看；不是专家确认的房间真值。</p>')
    parts.append('<h2>最重要的结论</h2><p>后续应优先验证“源上下文是否被丢失”和“对象级输入能否支持房间拓扑”，而不是继续在同一图元分类接口上加排除条件。候选生成、候选过滤、部件归属和房间评价缺口是不同问题。</p><ul><li><b>候选覆盖：</b>501个标注门实例中143个未被当前候选池触及；其中120个实例自己的标注部件中没有解析ARC。A01显示外观上的开启曲线实际是直线链；不能把无ARC直接等同无门弧。raw 5类别的70个门实例均未被触及，说明当前路径并未覆盖所有标注门类别。</li><li><b>过滤损失：</b>213个窗实例中85个有候选但过滤后未被覆盖，全部涉及外侧宿主墙条件未满足，75个只涉及该条件。其余还叠加独立内轨/端盖条件；这解释损失，不证明放宽条件安全。</li><li><b>归属信息丢失：</b>195组精确重合的墙/窗线分布在44个分析族；所有195组的墙、窗副本来自不同SVG父分组路径。当前typed Primitive没有保留该上下文。分组不是物理构件真值，未来必须核实来源且不能写图层名称特例。</li><li><b>房间线索并非完全不存在：</b>150族有4081个SVG文字节点，均有可读取的位置基线，含房间名称；当前图元适配器忽略文字。文字基线不是房间中心，SVG坐标也没有确认的工程单位。</li></ul>')
    stages=[('generation_no_target_candidate','候选池未触及'),('filtering_candidate_lost','候选存在、过滤后丢失'),('accepted_role_conflict','候选接受但唯一角色冲突'),('partial_part_assignment','有命中但不足一半部件'),('half_coverage_or_more','至少一半部件命中')]
    parts.append('<h2>154族的阶段账本</h2><p>以现有标注(category, raw ID, instance ID)组合作为实例。候选池定义为未改动的v3候选＋v4关系提案；“触及”只需命中一个标注源部件，是宽松的可达性指标，不能解释为对象检测AP。每个实例只落入下表一行。≥50%沿用已有报告口径，不是新增规则。</p><table><tr><th>阶段</th><th>门（501）</th><th>窗（213）</th></tr>')
    for k,label in stages:parts.append(f'<tr><td>{label}</td><td>{summary["instance_stages"]["door"].get(k,0)}</td><td>{summary["instance_stages"]["window"].get(k,0)}</td></tr>')
    parts.append('</table><p>这里没有accepted_role_conflict实例，并不代表没有物理归属冲突：当前逻辑会消除与门窗角色重叠的墙候选，而且最终角色字段不表达跨构件共享边界。</p><table><tr><th>图元类别</th><th>真值图元</th><th>可解析</th><th>候选池触及</th><th>最终正确归属</th></tr>')
    for kind in ('door','window','wall'):
        m=summary['totals']['primitive_'+kind];parts.append(f'<tr><td>{kind}</td><td>{m["total"]}</td><td>{m["parsed"]}</td><td>{m["raw_pool_touched"]}</td><td>{m["assigned"]}</td></tr>')
    parts.append('</table><p>全部目标图元可解析不等于它们的表示形式被识别器支持。墙池仅代表现有v3后的墙候选，不是所有更早的平行线对；墙带的连续连接目前没有单独交付。237个窗候选中70个混入其他类别、72个不含标注窗；9个候选跨多个标注窗实例。这些计数支持把候选对象和源部件归属分开，不能直接作为物理构件正确率。</p>')
    parts.append('<h2>标签覆盖：保持原真值，单列不确定性</h2><p>完整v5门预测的237条图元误检中195条原始类别为0；独立补全新增78条误检中72条为0。原解析器把缺少semantic-id的源图元也映射为0，本轮额外区分“字段缺失”和“显式0”，不将它们从分母剔除或自动改标。A07短线接在门扇端部，A08短线位于交接处，都需要归属复核。</p><table><tr><th>预测误检类别</th><th>缺少semantic-id</th><th>显式0</th><th>显式其他标签</th></tr>')
    for k,m in presence['false_positive_annotation_origin'].items():parts.append(f'<tr><td>{k}</td><td>{m.get("missing_semantic_attribute",0)}</td><td>{m.get("explicit_zero",0)}</td><td>{m.get("explicit_nonzero",0)}</td></tr>')
    parts.append('</table><p>775组精确重合线含不同类别，其中195组同时含墙与窗。这不证明标注错误，也不允许按标签挑选源副本。原标签评测和面向产品的人工对象参考将分开保存。</p>')
    parts.append('<h2>房间拓扑：目前确认的是接口与评价缺口</h2><p>795个门候选对应478条精确去重的hinge→closed_tip代理线，仅5条附有当前严格宿主墙条件的见证。这个低数不代表其他473处实际没有墙；它反映现有见证条件很窄。重复候选/同轴提案也不等于重复物理门。门洞端点、墙带两侧边、相邻房间和共享边界归属尚未形成完整对象接口。</p><p>本轮12个局部区域只做无吸附、无补线的线网诊断，比较全部解析直线、墙标签线和预测墙线。成环可能来自墙厚、家具、填充或裁剪，不是房间数；裁剪会制造悬挂端点。未提供房间真值、未运行生产房间流程，所以没有声称房间漏检率或边界质量改善。</p><p>现有rooms.py需要真实unit_to_m，room_boundary.py依赖文字锚点、范围及候选选择。SVG的width/viewBox和尺寸文字不能未经关联就用作工程比例。应先在独立无量纲开发夹具中做上界实验。</p>')
    parts.append('<h2>最小验证方案（拟议，尚未执行）</h2><ol><li>恢复源文字、节点路径和不透明父分组ID；核实元数据不是由标注答案生成。保留源曲线/直线链差异，不在本轮改几何或加图层名称规则。</li><li>优先在本轮12个分析族内扩大局部范围，建立12个上下文、至少12个门洞、12段墙带、8个完整房间参考。对不完整/不确定目标单独记录；不足只能从原154族补齐。先冻结参考和容差，再开发实验消费者。</li><li>固定同一无量纲拓扑消费者及人工复核内部点，运行下面四组。人工参考是上界诊断，不是模型成绩。</li></ol><table><tr><th>实验</th><th>门洞输入</th><th>墙带输入</th><th>要回答的问题</th></tr>')
    for arm in proposed['phase_2']['arms']:parts.append(f'<tr><td>{arm["id"]}</td><td>{arm["openings"]}</td><td>{arm["walls"]}</td><td>{arm["purpose"]}</td></tr>')
    parts.append('</table><p><b>B3仍失败，优先处理房间拓扑消费者或参考定义；B1/B2能单独纠错，再确定门洞或墙带优先级；仅B3改善则需要联合对象关系。</b>原生文字锚点的贡献放在固定几何后的独立对照中，不混入门窗归属调参。</p><p>验收看门洞对象精确率/召回和端点误差，墙带长度覆盖及交接连通，房间漏检/重复/误合并/误分裂和边界偏差。小型开发夹具不新增错误闭合，保留所有已正确房间；至少两个不同开发族出现可定位的房间纠错后才进入下一轮识别开发。通过它仍不能声称泛化通过或接入默认公共方法。</p>')
    parts.append('<h2>12个案例：原几何、标注、预测</h2><p>按六个诊断分层，每层哈希顺序选两个不同族，不按改善幅度挑选。灰色原图保留全部源几何，仅统一样式；绿=门，蓝=窗，橙=墙，紫色虚线=未确认门洞代理。观察由助手完成，未由领域专家签认；字段和标签未修改。</p>')
    for c,n in zip(cases,reviews):
        parts.append(f'<details><summary>{c["case_id"]} · {html.escape(c["sample_id"])} · {n["mechanism"]}</summary><p>{n["observation"]}</p><p>{n["limitation"]}</p><img src="{c["image"]}" alt="{c["case_id"]}三栏对照"><p><a href="cases/{c["case_id"]}.json">源ID、坐标、标注与局部线网诊断</a></p></details>')
    parts.append('<h2>证据与隔离</h2><p>只读取154族源SVG及其旧分析预测；旧检查名单只读取族ID元数据。486检查族、原验证集和test内容由白名单守卫拒绝；无新检查名单、无检查评分。曾修复审计脚本将XML属性名当计数器的TypeError，修复前没有完成样本输出，原协议及失败日志保留；识别源码、参数及产品代码未改变。</p><ul>')
    for f in ['protocol.json','summary.json','cohorts.json','source_metadata_audit.json','label_presence_audit.json','source_group_overlap_check.json','rejection_path_summary.json','case_selection.json','visual_review_notes.json','minimal_validation_plan.json','proposed_interfaces.json','development_reference_template.json','analysis_roster.json','excluded_check_families.json','delivery_validation.json']:
        parts.append(f'<li><a href="{f}">{f}</a></li>')
    parts.append('</ul></html>');(OUT/'report.html').write_text(''.join(parts))
    (OUT/'README.txt').write_text('FloorPlanCAD 失败机制审计\n入口：report.html\n154分析族；486检查族隔离；新检查族0。无识别规则/预算/默认公共方法改动。\nsummary.json：实例阶段账本。source_metadata_audit.json：文字与源分组。\nlabel_presence_audit.json：标签字段缺失/显式0分开。rejection_path_summary.json：冻结过滤条件重放。\ncases/：12个不同分析族的三栏图和源证据。visual_review_notes.json：助手目视审计，非专家真值。\nminimal_validation_plan.json、proposed_interfaces.json：拟议方案，尚未实现/执行。\ndevelopment_reference_template.json：空标注模板，不是已完成房间参考。\n原标签与几何不改写；单位未确认；当前不能报告房间准确率或平方米。\n复核：.venv/bin/python -m scripts.floorplancad_failure_audit_validate\n')
    print('Audit report and proposed plan saved')
if __name__=='__main__':run()
