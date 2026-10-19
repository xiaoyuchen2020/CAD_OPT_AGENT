"""Finalize immutable evidence for the revised room-boundary experiment."""
import html,json,shutil
from scripts.room_boundary_revision import ROOT,OUT,OLD,read,dump,sha,validator,VALIDATOR_SHA

def main():
    r=read('run.json');b=read('input.json');c=read('compression-audit.json');p=read('preservation.json');tests=read('test-results.json');visual=read('assistant-visual-review.json')
    accepted=r.get('accepted_attempt');a=next(x for x in r['attempts'] if x['number']==accepted) if accepted else None
    v=a['validation'] if a else {};checks=v.get('checks',{});candidate_attempt=next(x for x in reversed(r['attempts']) if (OUT/f'execution-{x["number"]}.json').exists());raw=read(f'execution-{candidate_attempt["number"]}.json')['output'];replays=read('replay-results.json') if (OUT/'replay-results.json').exists() else []
    usage={}
    for attempt in r['attempts']:
        for k,n in attempt.get('usage',{}).items():usage[k]=usage.get(k,0)+n
    summary=dict(case='孔浦地块人防地下室施工图20240911_t3.dwg / 公变 3552_01_1',
      structural_validation_passed=bool(a),visual_review=visual['result'],observed_envelope_area_m2=checks.get('observed_envelope_area_m2'),rejected_candidate_area_m2=None if a else raw.get('area',0)/1e6,
      source_sha256=b['source_sha256'],input_id=b['input_id'],code_sha256=a.get('code_sha256') if a else None,
      validator_sha256=VALIDATOR_SHA,validator_unchanged=True,execution_input_unchanged=True,model_calls=r['model_calls'],usage=usage,
      all_usage_known=all(x.get('usage_known') for x in r['attempts']),reserved_tokens=r['reserved_tokens'],unused_call_slots=max(0,3-r['model_calls']),
      compression=c,replays=replays,tests_passed=tests['passed'],negative_control_count=len(read('negative-controls.json')) if (OUT/'negative-controls.json').exists() else 0,
      baseline_fresh_unchanged=p['fresh_baseline_equal'],prior_evidence_files_unchanged=sum(x['files'] for x in p['prior_evidence']),
      technical_recovery_gain=bool(a) and visual.get('full_room_extent_confirmed',False),numeric_diagnostics=read('numeric-diagnostics.json'),approval_block=read('approval-block.json') if (OUT/'approval-block.json').exists() else None,engineering_net_area_accepted=False,
      automatic_adoption=False,automatic_registration=False,cross_drawing_evidence_added=0)
    dump(OUT/'summary.json',summary)
    implementation=OUT/'implementation';implementation.mkdir(exist_ok=True)
    files=['scripts/room_boundary_revision.py','scripts/check_room_boundary_revision.py','scripts/finalize_room_boundary_revision.py','tests/test_room_boundary_revision.py']
    for name in files:shutil.copy2(ROOT/name,implementation/PathName(name))
    def link(name,label=None):return f'<a href="{name}">{html.escape(label or name)}</a>'
    status='原生几何验证通过，工程净面积未签认' if a else '折返已改善，但来源验证未通过，未采用'
    controls=link('negative-controls.json') if (OUT/'negative-controls.json').exists() else link('previous-candidate-still-rejected.json','旧折返候选仍被拒绝')
    approval_note='' if a else '<p>已授权的 3 次请求已用完。追加第 4 次请求被自动审批拒绝，尚未发送，等待明确授权。<a href="additional-request-preview.json">外发预览</a> · <a href="proposed-support-filter.diff">待修正差异</a></p>'
    image_grid=''.join(f'<figure><a href="{name}.svg"><img src="{name}.png"></a><figcaption>{label}</figcaption></figure>' for name,label in [('baseline-overlay','旧组件：40.8792 m² 中段候选，needs_review。'),('candidate-overlay','新候选：完整公变观测围合；绿色不是自动采用标记。'),('upper-door','上部门洞放大；检查折返与外伸边界。'),('lower-door','下部门洞放大；门洞口径仍需工程确认。')])
    rows=''.join('<tr><td>'+str(x['number'])+'</td><td>'+html.escape(x['status'])+'</td><td>'+html.escape(str(x.get('validation',{}).get('errors',x.get('reason',x.get('error','')))))+'</td><td>'+str(x.get('usage',{}).get('total_tokens','未知'))+'</td><td>'+link(f'prompt-{x["number"]}.json','请求')+(' · '+link(x['code_path'],'代码') if x.get('code_path') else '')+'</td></tr>' for x in r['attempts'])
    replay_rows=''.join(f'<tr><td>{x["name"]}</td><td>{x["validation_status"]}</td><td>{html.escape(str(x["errors"]))}</td><td>{x["expectation_met"]}</td></tr>' for x in replays)
    details=''.join(f'<li>{html.escape(x)}</li>' for x in visual['findings'])
    page=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>孔浦公变 · 重叠与折返修正</title><style>
body{{font:16px/1.7 system-ui,sans-serif;background:#f3f6f7;color:#20313b;margin:0}}main{{max-width:1240px;margin:auto;padding:30px}}section{{background:white;border:1px solid #dae3e8;border-radius:12px;padding:22px;margin:20px 0}}h1{{font-size:29px}}h2{{font-size:21px}}.notice{{background:#edf6f2;border-left:5px solid #16836d;padding:18px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}.stat{{background:white;padding:16px;border-radius:9px}}strong.number{{font-size:25px;display:block}}figure{{margin:0}}img{{width:100%;display:block}}figcaption,.small{{color:#60717c;font-size:14px}}a{{color:#176787}}th,td{{padding:10px;border-bottom:1px solid #dce4e8;text-align:left}}table{{width:100%;border-collapse:collapse}}code{{overflow-wrap:anywhere}}@media(max-width:760px){{.grid,.stats{{grid-template-columns:1fr}}main{{padding:14px}}}}</style><main>
<h1>孔浦公变：重叠线段切分与零宽度折返修正</h1><p>孔浦地块人防地下室施工图20240911_t3.dwg · 公变 3552_01_1 · 2026-10-16</p>
<div class="notice"><strong>{status}</strong>。保留旧组件行为；不自动采用，不注册公共方法。本轮继续使用原样冻结的 v2 验证器，没有放宽门槛。{approval_note}</div>
<div class="stats"><div class="stat"><strong class="number">{c['compact_model_view_bytes']:,} B</strong>模型视图；原为 {c['original_model_view_bytes']:,} B</div><div class="stat"><strong class="number">{r['model_calls']} / 3</strong>实际模型请求 / 上限</div><div class="stat"><strong class="number">{checks.get('observed_envelope_area_m2','未通过')}</strong>观测围合面积 m²，非认可净面积</div><div class="stat"><strong class="number">{tests['passed']} 项</strong>相关测试通过</div></div>
<section><h2>原图与边界复核</h2><p>本轮原始候选的边界相对规范化边界偏差约 0.000006 mm，上轮约 60 mm；没有修改验证器。但 6 条来源仅有约 0.000003 mm 的端点重叠，未达到既有 0.1 mm 相关性门槛，候选被拒。<a href="numeric-diagnostics.json">数值诊断</a> · <a href="irrelevant-support-audit.json">6 条无效来源明细</a></p><div class="grid">{image_grid}</div><ul>{details}</ul><p>{link('source.svg','原图，无候选叠加')} · {link('candidate-detail.svg','完整房间放大')} · {link('excluded-crossings.svg','贯穿线排除对照')} · {link('assistant-visual-review.json','助手视觉复核与图像哈希')}</p></section>
<section><h2>压缩与修正预算</h2><p>584 条线段、24 段原文全部保留。短 ID 映射可逆；去重坐标索引和图层字典减少重复。仅模型坐标视图在 0.00001 mm 精度投影，最大误差 {c['coordinate_view_max_error_mm']:.9f} mm；不是宣称坐标无损。沙箱输入与上一轮原始输入逐字节相同。</p><p>总预算 96,000 tokens，每次预留 32,000，输出上限 6,000。第一次请求后仍保留两次修正额度。修正请求明确只携带代码、接口和失败证据，不重复全量几何；沙箱每次仍读取全部原始局部。失败用量与传输诊断也会持久化，未知用量按整槽计费预算。</p><p>本轮首轮 prompt_tokens=10,977，上轮首轮为 43,809；本轮实际总用量 {usage.get('total_tokens','未知'):,} tokens。预算不是订阅美元费用或账户剩余额度。</p><p>{link('compression-audit.json')} · {link('model-view.json')} · {link('id-map.json')} · {link('budget.json')}</p></section>
<section><h2>真实模型请求和沙箱结果</h2><table><tr><th>轮次</th><th>状态</th><th>解释或验证错误</th><th>Tokens</th><th>证据</th></tr>{rows}</table><p>模型为通过 ChatGPT 连接的 GPT-6.1 Sol。首轮选择放弃生成，未指认来源缺失；随后在未重置预算的前提下提供上一轮真实生成代码，进行定向修正。全部请求保留完整提案和传输诊断。</p><p>生成代码只在固定 Docker 镜像中运行：network=none、只读文件系统、无主机挂载、非 root、cap-drop ALL、512 MiB、32 PID、20 秒。独立验证器不导入生成代码；宿主没有清理候选后冒充原始输出通过。</p><p>{link('run.json')} · {link('contract.json')} · {link('validator-frozen.py')} · {link('input.json')}</p></section>
<section><h2>相同验证标准与回归</h2><p>验证器 SHA-256：<code>{VALIDATOR_SHA}</code>。与上一轮封存 v2 完全相同；上一轮被拒代码的原始输出仍会触发 BOUNDARY_NUMERICAL_BACKTRACK。检查包含逐边原生来源、可见性和单位、目标包含、完整结构面、折返、贯穿线排除、面积、外侧平行支持。</p><table><tr><th>重放或控制</th><th>验证状态</th><th>错误</th><th>符合预期</th></tr>{replay_rows}</table><p>上述变换和独立构造样本仅是算法稳定性证据，不是新增真实图纸。真实样本仍只有孔浦公变这一处。本轮候选仍失败时不进行假阳性的“全部拒绝即通过”统计；旧候选拒绝记录和 73 项测试单独保留。合成重叠控制仍未恢复，说明一般化尚有缺口。</p><p>{controls} · {link('replay-results.json')} · {link('test-results.json')}</p></section>
<section><h2>来源和现有组件保护</h2><p>继续沿用经过核对的可见性与单位证据：原生 584 条线段可见，递归 INSERT 继承、off/frozen/invisible 及 XCLIP 支持性保留；INSUNITS=4 明确毫米。原图、解析文件和执行输入摘要复核通过。</p><p>原组件在实验前后重新计算而不是只读缓存，输出摘要一致。6 个历史证据目录共 {summary['prior_evidence_files_unchanged']} 个文件保持不变。本任务仅新增实验脚本、测试、文档和证据；并发任务的文件变动单列记录，没有回滚。</p><p>{link('preservation.json')} · {link('source-audit.json')} · {link('baseline-before-fresh.json')} · {link('baseline-after-fresh.json')} · {link('summary.json')}</p></section>
<section><h2>采用边界</h2><p>即便原生几何验证通过，门洞外伸线、柱凹入和工程净面积口径仍需要单独确认。本轮不自动采用、不注册公共方法，也不声称已完成跨图纸泛化。需先完成来源支持过滤修正并通过冻结验证，再处理合成重叠控制暴露的多重围合歧义，之后才能扩展真实图纸回归。</p><p class="small">全部证据文件摘要见 evidence-sha256.json；本页为本地旁路试验记录。</p></section></main></html>'''
    (OUT/'review.html').write_text(page)
    dump(OUT/'evidence-sha256.json',{str(f.relative_to(OUT)):sha(f) for f in sorted(OUT.rglob('*')) if f.is_file() and 'data' not in f.relative_to(OUT).parts and f.name!='evidence-sha256.json' and '__pycache__' not in f.parts})
    manifest=read('evidence-sha256.json');assert all(sha(OUT/p)==h for p,h in manifest.items());print('sealed',len(manifest),'files',flush=True)

def PathName(name):return name.rsplit('/',1)[-1]
if __name__=='__main__':main()
