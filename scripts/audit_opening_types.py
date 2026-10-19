"""Score a declared, manually reviewed challenge set without claiming census accuracy."""
import argparse
from collections import Counter
from html import escape
import json
from pathlib import Path
from shapely.geometry import Point, Polygon


def dump(p,data):p.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def score(graph,sample):
    p=Point(sample['anchor']);hits=[o for o in graph['openings'] if Polygon(o['boundary']).distance(p)<=180]
    expected=sample['expected_identity']+'_candidate';typed={o.get('opening_type','unknown') for o in hits}-{'unknown'}
    result='missed_aperture' if not hits else 'wrong_identity' if typed and typed!={expected} else 'identity_unresolved' if not typed else 'supported_candidate'
    return dict(result=result,opening_ids=[o['object_id'] for o in hits],observed_types=sorted(typed),operations=sorted({o.get('operation_type','not_available') for o in hits}),duplicate_hypotheses=len(hits)>1)


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--previous',type=Path,required=True);p.add_argument('--exploration',type=Path,required=True);a=p.parse_args();out=a.output
    annotation=json.loads((out/'manual-annotations.json').read_text());summary=json.loads((out/'summary.json').read_text())
    graphs={r['name']:json.loads((out/f"{r['name']}.json").read_text()) for r in summary}
    before={n:json.loads(((a.previous/f'{n}.json') if n in ('property-bath','ligong-east') else (a.exploration/f'{n}-before.json')).read_text()) for n in ['property-bath','ligong-east','d5','d7-plan']}
    audit=[]
    for sample in annotation['samples']:
        audit.append(sample|dict(before=score(before[sample['case']],sample),after=score(graphs[sample['case']],sample)))
    counts={when:dict(Counter(r[when]['result'] for r in audit)) for when in ('before','after')}
    # A label read from a neighboring aperture must not silently become proof.
    lig=graphs['ligong-east'];d5=graphs['d5']
    for graph,point,tag in [(lig,[115578,-172195],"TLM3924'"),(d5,[620759,-150310],'FM0821')]:
        o=min(graph['openings'],key=lambda o:Polygon(o['boundary']).distance(Point(point)))
        assert tag not in [t['text'] for t in o['classification_evidence']['labels']]
    overlap=[]
    for name,g in graphs.items():
        seen=set()
        byid={o['object_id']:o for o in g['openings']}
        for o in g['openings']:
            if not o.get('competing_opening_ids') or o['object_id'] in seen:continue
            component=set();stack=[o['object_id']]
            while stack:
                key=stack.pop()
                if key in component:continue
                component.add(key);stack.extend(byid[key].get('competing_opening_ids',[]))
            seen|=component
            assert all(byid[k]['passage_status']=='not_established' for k in component)
            overlap.append(dict(case=name,opening_ids=sorted(component),all_passages_blocked=True,extra_hypotheses=len(component)-1))
    geometry_review=[]
    for name,point,note in [('d5',[618680,-157105],'Continuous/stepped wall-side strip spans 4.49 m; not a single supported aperture.'),('d7-plan',[463439,-5082],'Vertical wall strip; no local opening symbol spanning the proposed gap.'),('d7-plan',[457519,-4062],'Vertical wall strip; no local opening symbol spanning the proposed gap.')]:
        o=min(graphs[name]['openings'],key=lambda o:Polygon(o['boundary']).distance(Point(point)))
        assert o['opening_type']=='unknown' and o['passage_status']=='not_established'
        geometry_review.append(dict(case=name,opening_id=o['object_id'],bbox=o['bbox'],finding='suspect_false_geometric_aperture',review_note=note,identity_claim=False,passage_claim=False))
    report=dict(evidence_class=annotation['evidence_class'],sampling=annotation['sampling'],counts=counts,samples=audit,overlapping_hypotheses=overlap,geometric_false_positive_review=geometry_review,limits=['Not a blinded benchmark or exhaustive recall count.','No real sliding-window positive established in these four selected scopes; same-symbol door/window discrimination is covered by synthetic tests.','Three suspect geometric gaps remain unknown rather than being deleted or promoted to door/window.','D7 small-angle double leaf at lobby remains unknown without identity evidence.'])
    dump(out/'manual-audit.json',report)
    html=['<!doctype html><meta charset="utf-8"><title>双扇门与推拉门窗验证</title><style>body{font:16px system-ui;max-width:1400px;margin:32px auto;padding:0 20px;color:#243840}table{border-collapse:collapse;width:100%}td,th{padding:10px;border:1px solid #ccd6da;text-align:left}img{max-width:100%}pre{white-space:pre-wrap;background:#f1f4f5;padding:16px}a{color:#156a87}details{margin:20px 0}</style><h1>双扇门与推拉门窗：真实图纸局部核对</h1>',
    '<p>冻结代码、本地真实 DXF 回放；不是平台运行、真实模型问答或工程验收。源线形及文字由代理逐项检查，未经过独立人工标注验收；不是全图识别率。</p>',
    '<h2>上次 5 个未定洞口</h2><table><tr><th>图纸 / 原编号</th><th>本次结果</th><th>依据与限制</th></tr><tr><td>D1–D3 / O2</td><td>双扇门候选</td><td>两扇弧线与径向扇线、闭合端会合；外置约 0.20 m，原生框线连接墙洞。符号跨度约 1.464 m，墙洞 2.04 m，均非净宽。</td></tr><tr><td>D1–D3 / O3</td><td>继续未定</td><td>只有玻璃与框线，缺少唯一门窗身份及开启证据。</td></tr><tr><td>理工楼 / O2</td><td>推拉门候选</td><td>TLM1624 编号与错轨重叠的两扇薄面板。</td></tr><tr><td>理工楼 / O7</td><td>窗候选；开启方式未定</td><td>C0715′ 旋转文字范围与窗框；不把窗候选称作推拉窗。</td></tr><tr><td>理工楼 / O15</td><td>继续未定</td><td>曲折外墙与下侧曲窗未建立唯一绑定，洞口几何本身待核对。</td></tr></table><img src="five-unknown-source.png">',
    '<h2>双扇与推拉的原生证据</h2><img src="composite-details.png"><h2>新增图纸与候选数量</h2><p>同一物理位置可能有多种门垛假设；下表是原始候选数，不能当作门窗数量。D5 / D7–D8 的最大开口显式设为 6 m；默认仍为 3 m。</p><table><tr><th>图纸 / 范围</th><th>空间</th><th>洞口</th><th>门 / 窗 / 未定</th><th>双侧门连接</th></tr>']
    for r in summary:
        t=r['types'];html.append(f'<tr><td>{escape(r["dwg"])}<br>{escape(r["name"])}</td><td>{r["rooms"]}</td><td>{r["openings"]}</td><td>{t.get("door_candidate",0)} / {t.get("window_candidate",0)} / {t.get("unknown",0)}</td><td>{r["two_sided_door_connections"]}</td></tr>')
    html+=['</table><h2>误判与漏判核对</h2>',f'<p>固定挑战样本共 {len(audit)} 个可由源图复核身份的物理门窗，含未被返回的目标。更新前后：</p><pre>{escape(json.dumps(counts,ensure_ascii=False,indent=2))}</pre>',
    '<p>supported_candidate 表示输出身份与本次源图复核一致，identity_unresolved 是已有洞口但未判定门窗，missed_aperture 是未形成洞口候选，wrong_identity 是明确分错门窗。未定与漏检都没有算作正确。此样本由本轮排查选择，不能推算全图准确率。</p>',
    '<p>仍有 2 处 D5 入户门身份未识别、1 处 D7–D8 小角度双扇身份未定；另有 2 处 D5 推拉门和 4 处 D7–D8 门符号没有形成受墙面证据支持的洞口。下面是这 6 个漏检位置。缺少完整墙洞时，没有仅凭门符号补造房间通行关系。</p><img src="missed-symbols.png">',
    '<p>此外，D5 一组、D7–D8 一组重叠门垛假设共多出 3 个记录，均标记冲突并禁止通行边；3 处可疑墙带仍被底层返回为几何洞口候选，但类型未定、没有门窗身份或通行声明。这些几何假阳性没有混进上面的门窗身份分错计数。</p>',
    '<p>本轮真实图纸未找到足以独立确认的推拉窗正例。推拉门与推拉窗采用相同面板几何的身份区分已做合成测试；真实窗反例不产生通行关系。理工楼 6 处同时存在单扇及双扇符号，输出 ambiguous_swing，未强行确定扇数。</p>',
    '<p><a href="manual-annotations.json">人工式核对目标与坐标</a> · <a href="manual-audit.json">逐项更新前后结果</a> · <a href="code-manifest.json">冻结代码哈希</a> · <a href="full-tests.txt">完整测试</a> · <a href="replay.log">回放日志</a></p>']
    for r in summary:
        n=r['name'];html.append(f'<details><summary>{escape(r["dwg"])} — {n}：原图与候选</summary><pre>{escape(json.dumps(r,ensure_ascii=False,indent=2))}</pre><img src="{n}.png"><p><a href="{n}.json">完整关系</a> · <a href="{n}-checks.json">双侧探测复核</a> · <a href="{n}-api-agent.json">HTTP / Agent 一致性</a></p></details>')
    html.append('<h2>测试与版本</h2><p>冻结代码完整测试：'+escape((out/'full-tests.txt').read_text().strip().splitlines()[-1])+'</p><p>当前工作区专项测试：'+escape((out/'current-workspace-tests.txt').read_text().strip().splitlines()[-1])+'</p><p>四张源 DXF 哈希前后相同；每个范围均核对 HTTP 与 Agent 工具读取同一记录。当前工作区另有并行索引优化，真实回放与完整测试以冻结代码为准。<a href="workspace-comparison.json">版本差异</a> · <a href="validation-notes.txt">核验脚本修正记录</a></p>')
    (out/'review.html').write_text(''.join(html));print(json.dumps(counts),flush=True)


if __name__=='__main__':main()
