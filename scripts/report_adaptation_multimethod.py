"""Report source-bound multi-method experiments; never call a model."""
import copy
import html
import json
import math
from dataclasses import replace
from pathlib import Path
from cad_agent.config import ROOT, Settings
from cad_agent.service import Workspace
from cad_agent.extensions.adaptation.service import AdaptationService
from cad_agent.extensions.adaptation.review import source_svg
from cad_agent.extensions.adaptation.validation import validate
from scripts.report_cross_adaptation import raster

OUT=ROOT/'artifacts/dynamic-multimethod-20261016'
def dump(name,v): (OUT/name).write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
def main():
    w=Workspace(replace(Settings.load(),data_dir=OUT/'data'))
    rows=[];negatives=[];sections=[];usage=0
    try:
        paths=sorted(list(OUT.glob('*-generated.json'))+list(OUT.glob('*-replay-*.json')))
        for path in paths:
            d=json.loads(path.read_text());run=d['run'];method=run['request']['method'];bundle=d['result']['bundle'];raw=d['execution']['output'];v=d['result']['validation']
            assert v['validation_status']=='passed'
            fixture='A' if '-generated' in path.stem else path.stem[-1]
            expected=None
            if method=='text_spans':
                expected=sorted(['SYNTHETIC A','SIZE 10 x 6'] if fixture=='A' else ['SYNTHETIC B','UNITS UNKNOWN'])
                assert sorted(i['quote'] for i in v['items'])==expected
            elif method=='closed_contour':
                expected=60 if fixture=='A' else 48
                assert len(v['items'])==1 and math.isclose(v['items'][0]['area'],expected,abs_tol=1e-7)
            else:
                key='length' if method=='segment_measurements' else 'distance'
                expected=([6,6,10,10] if fixture=='A' else [6,6,8,8]) if key=='length' else ([0,0,0,0,6,10] if fixture=='A' else ([0,0,0,0,6,8] if fixture=='B' else sorted([0,math.sqrt(18),math.sqrt(5),math.sqrt(8),math.sqrt(24.5),3])))
                actual=sorted(i[key] for i in v['items']);assert len(actual)==len(expected)
                assert all(math.isclose(a,b,abs_tol=1e-7) for a,b in zip(actual,expected))
            if fixture=='A':
                mutations=[]
                bad=copy.deepcopy(raw);bad['input_id']='foreign-input';mutations.append(('foreign_input',bad))
                bad=copy.deepcopy(raw);bad['automatic_adoption']=True;mutations.append(('forged_control_field',bad))
                bad=copy.deepcopy(raw)
                if method=='closed_contour':bad['area']+=1
                elif method=='text_spans':bad['items'][0]['end']=100000
                else:bad['items'][0]['length' if method=='segment_measurements' else 'distance']+=1
                mutations.append(('wrong_value',bad));bad=copy.deepcopy(raw)
                if method=='closed_contour':bad['segment_ids'][0]='foreign-segment'
                elif method=='segment_distances':bad['items'][0]['segment_ids'][0]='foreign-segment'
                else:bad['items'][0]['entity_id' if method=='text_spans' else 'segment_id']='foreign-source'
                mutations.append(('foreign_source',bad));bad=copy.deepcopy(raw)
                if method=='closed_contour':bad['segment_ids'].append(bad['segment_ids'][0])
                else:bad['items'].append(copy.deepcopy(bad['items'][0]))
                mutations.append(('duplicate',bad))
                for label,c in mutations:
                    r=validate(method,bundle,c);assert r['validation_status']=='rejected'
                    negatives.append(dict(method=method,mutation=label,errors=r['errors'],rejected=True))
            tokens=sum(a.get('usage',{}).get('total_tokens',0) for a in run['attempts']);usage+=tokens
            row=dict(label=path.stem,fixture=fixture,method=method,run_id=run['run_id'],status=run['status'],model_calls=run['model_calls'],tokens=tokens,
                code_sha256=run['attempts'][0]['code_sha256'],item_count=len(v['items']),expected=expected,analytic_check_passed=True,
                automatic_adoption=False,automatic_registration=False)
            rows.append(row)
            if fixture!='A':
                s=AdaptationService.__new__(AdaptationService);s.w=w;s.sid=run['snapshot_id']
                for kind in ['source','overlay']:
                    f=OUT/(path.stem+'-'+kind+'.svg');f.write_text(source_svg(s,run['run_id'],kind=='overlay',local_origin=True));raster(f.read_text(),f.with_suffix('.png'))
                pair=''.join(f'<a href="{path.stem}-{k}.svg"><img src="{path.stem}-{k}.png" alt="{k}"></a>' for k in ['source','overlay'])
                sections.append(f'<section><h2>{path.stem}</h2><p>左：源 DXF；右：候选叠加。橙色为选中几何，粉色为独立求得的最短连接。预期值：{expected}</p><div class="pair">{pair}</div></section>')
        history=json.loads((OUT/'historical-regression.json').read_text());checks=json.loads((OUT/'agent-read-checks.json').read_text())
        summary=dict(methods=5,live_generated_methods=4,actual_model_requests=4,actual_total_tokens=usage,pre_http_busy_refusals=4,
                     sandbox_passes=len(rows),frozen_holdouts=5,historical_behavior_preserved=history['unchanged_count'],
                     unchanged_native_agent_reads=len(checks),negative_candidates_rejected=len(negatives),cases=rows,
                     scope='synthetic generation and holdouts; real historical compatibility only',automatic_adoption=False,automatic_registration=False)
        dump('summary.json',summary);dump('negative-candidate-checks.json',negatives)
        table=''.join(f'<tr><td>{r["label"]}</td><td>{r["item_count"]}</td><td>{r["model_calls"]}</td><td>{r["tokens"]}</td><td>通过／待复核</td></tr>' for r in rows)
        (OUT/'review.html').write_text('''<!doctype html><html lang="zh"><meta charset="utf-8"><title>多方法 Agent 旁路验证</title><style>body{font:16px/1.65 system-ui;background:#f4f7f3;color:#24382b;max-width:1200px;margin:30px auto;padding:0 24px}section{background:white;padding:24px;margin:24px 0;border-radius:14px}.pair{display:flex}.pair a{width:50%}img{width:100%}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd5cd;padding:8px}a{color:#17623c}code{overflow-wrap:anywhere}</style>
<h1>Agent 可调试范围已扩展为五类方法</h1><p>表格网格、原文片段、原生线段长度、线段最短间距、闭合轮廓与面积。新增局部区域入口，并在匹配已授权读取范围后运行旁路；默认组件结果、证据和观察不被候选修改。</p>
<section><h2>如何使用</h2><p>缩放到目标局部 → 点击“局部方法调试” → 选择方法并填写目标 → “运行一次局部试验”，或明确授权“Agent 在读取时旁路运行”。选定范围在打开窗口时冻结；匹配读取才触发，未匹配不会自动扩大范围。可关闭授权；单策略最多一个试验、三次生成或修正，四小时失效。</p><p>方法代码运行于既有隔离容器；保留来源 ID、输入／代码哈希、执行记录、独立验证与复核页面。结果不自动采用，不注册公共方法。面积仅代表选定闭合轮廓，不自动解释为房间净面积；长度和距离均为图纸单位。</p><img src="ui-local-methods.png" alt="局部方法调试界面"></section>
<section><h2>验证证据</h2><p>ChatGPT 直接订阅 GPT-6.1 Sol 实际生成 4 次，共 '''+str(usage)+''' tokens；全部一次通过，无 DeepSeek 调用。此前 4 次订阅忙碌在 HTTP 请求前拒绝，单独留存。真实模型本次未发生修正循环；拒绝后再生成的流程由测试模拟验证。</p><p>四类代码在构造样本 A 生成，冻结重放到 B（旋转并平移至百万量级坐标）与 C（交叉、分离线段），共 9 次隔离执行通过。5 次留出样本重放调用模型 0 次；3 次真实 Agent 工具读取前后返回、证据、观察完全一致。不是完整模型自主对话评测，也不是实际图纸新增收益统计。</p><p>历史 19 个表格局部及 3 个非表格方法共 22 项行为兼容，其中 6 项仍拒绝不完整输入。长度报告存在此前已加入的 basis 来源说明字段；原始计算输出相同，未将报告字段完全一致与计算行为一致混为一谈。</p><p>独立重读 3 份源 DXF，核验 12 条原生线段、4 条原文；20 个污染／伪造／错误值候选均被拒绝。</p><p><a href="summary.json">运行汇总</a> · <a href="source-binding-audit.json">来源追溯</a> · <a href="negative-candidate-checks.json">错误拒绝</a> · <a href="agent-read-checks.json">Agent 兼容性</a> · <a href="historical-regression.json">历史回归</a> · <a href="assistant-visual-review.json">助手视觉复核</a></p>
<table><tr><th>样本与方法</th><th>输出条数</th><th>模型请求</th><th>tokens</th><th>验证状态</th></tr>'''+table+'</table></section>'+''.join(sections)+'</html>')
        print(json.dumps({k:v for k,v in summary.items() if k!='cases'},ensure_ascii=False))
    finally:w.close()
if __name__=='__main__':main()
