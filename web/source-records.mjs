/** Source-record presentation only. Claims/citations remain server-validated. */
export function recordPages(result, pageSize=6) {
  const groups=result.source_records?.groups||[], pages=[];
  for(let i=0;i<groups.length;i+=pageSize) pages.push(groups.slice(i,i+pageSize));
  return pages;
}

export function renderSourceRecords(result,{document,openSource,onError=()=>{}}) {
  const pages=recordPages(result);
  if(!result.source_records)return null;
  const make=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
  const root=make('section',undefined,'source-record-results');
  root.append(make('p',result.source_records.query?.action==='compare'
    ? '按来源并列对照原文；相同编号不表示同一对象，未自动计算差值。'
    : '按来源整理原文。图框名称用于定位，工程适用性仍需核对。','answer-note'));
  const quality=result.source_records.quality;
  if(quality)root.append(make('p',`已绑定来源 ${quality.source_bound} 条；候选 ${quality.candidates} 条；未归属命中 ${quality.unresolved_hits} 处；引用待核 ${quality.unresolved_references} 条。流程完成不表示整图穷尽或工程适用性已确认。`,'answer-note'));
  const types={layer_assembly:'构造做法',material_clause:'材料/性能条款',node_record:'节点记录',source_section:'段落/章节原文',table_group:'表格分组（子项边界待核）'};
  const body=make('div'),nav=make('div',undefined,'source-record-nav');
  const prev=make('button','上一页'),next=make('button','下一页'),status=make('span');
  prev.type=next.type='button';let page=0;
  function draw(){
    body.replaceChildren();
    for(const group of pages[page]||[]){
      const details=make('details',undefined,'source-record-group');
      const candidate=group.states?.binding_state!=='source_bound';
      const label=`${group.title} · ${types[group.record_type]||'来源记录'} · ${group.source_scope?.frame_name||'未命名图框'}${candidate?' · 待核来源':''}${group.reference_only?' · 引用来源':''}`;
      details.append(make('summary',label));details.open=pages[page].length===1;
      const notes=[];
      if(candidate)notes.push('原文已保留，归属或边界仍待核查。');
      if(!candidate&&group.binding_basis==='explicit_title_and_numbered_closed_row')notes.push('已按明确标题、连续层次和闭合表行确认原文归属；表头未识别。');
      if(!candidate&&group.binding_warnings?.includes('UNCLASSIFIED_ROW_COLUMNS'))notes.push('其余单元格保留原文，未判定用途或字段角色。');
      if(!candidate&&group.binding_basis==='explicit_heading_bounded_source_paragraph')notes.push('已确认此标题下的文字做法范围；原文未分层编号。');
      if(group.states?.reference_state==='unresolved')notes.push('显式引用未解决，未将引用层次自动叠加。');
      if(notes.length)details.append(make('p',notes.join(''),'answer-note'));
      for(const index of group.claim_indexes){
        const claim=result.claims?.[index];if(!claim)continue;
        details.append(make('p',claim.display_text||claim.text));
        for(const number of claim.citation_numbers||[]){
          const source=result.citations?.[number-1];if(!source?.evidence_url)continue;
          const b=make('button',`[${number}] 定位原文`,'citation');b.type='button';
          b.onclick=()=>Promise.resolve(openSource(source)).catch(onError);details.append(b);
        }
      }
      for(const note of group.notes||[])details.append(make('p','模型标注的原文：'+note.quote,'answer-note'));
      body.append(details);
    }
    prev.disabled=page===0;next.disabled=page>=pages.length-1;status.textContent=`第 ${page+1} / ${pages.length} 页`;
  }
  prev.onclick=()=>{if(page>0){page--;draw();}};next.onclick=()=>{if(page<pages.length-1){page++;draw();}};
  nav.append(prev,status,next);root.append(body);if(pages.length>1)root.append(nav);draw();
  const excluded=result.source_records.excluded||[];
  if(excluded.length){const audit=make('details');audit.append(make('summary',`筛选排除 ${excluded.length} 条来源`));
    for(const r of excluded)audit.append(make('p',`${r.title} · ${r.source_scope?.frame_name||'未命名图框'}：${r.reason}`));root.append(audit);}
  const grouped=new Set((result.source_records.groups||[]).flatMap(g=>g.claim_indexes));
  for(const [index,claim] of (result.claims||[]).entries())if(!grouped.has(index))root.append(make('p',claim.display_text||claim.text,'answer-note'));
  return root;
}
