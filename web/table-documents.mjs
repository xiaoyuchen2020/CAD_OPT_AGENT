export function renderTableDocuments(result,{document,openSource,onError}) {
  const data=result?.table_documents;
  if(!data)return null;
  const el=(tag,text)=>{const x=document.createElement(tag);if(text!==undefined)x.textContent=text;return x;};
  const root=el('section');root.className='table-document';
  root.append(el('h4',data.title));
  const c=data.coverage;
  root.append(el('p',`已读 ${c.read_cells}/${c.selected_cells} 个选定单元格 · ${c.table_read_complete?'整表已读':'整表尚未读完'} · ${data.binding_state==='source_bound'?'字段关系有来源支持':'部分字段关系待确认'}`));
  const overlay=el('a','查看原图与单元格边界 ↗');overlay.href=data.overlay_url;overlay.target='_blank';overlay.rel='noopener';root.append(overlay);
  const groups=new Map();const rows=new Map();
  for(const item of data.items){
    const lineage=item.lineage.filter(n=>n.kind!=='table');
    const key=lineage.map(n=>n.node_id).join('/');
    let body=groups.get(key);
    if(!body){
      const section=el('details');section.open=true;
      section.append(el('summary',lineage.map(n=>n.title).join(' / ')||'基本信息'));
      const table=el('table');body=el('tbody');table.append(body);section.append(table);root.append(section);groups.set(key,body);
    }
    const rowKey=key+':'+item.row;let row=rows.get(rowKey);
    if(!row){row=el('tr');body.append(row);rows.set(rowKey,row);}
    const content=el('td');content.rowSpan=item.row_span;content.colSpan=item.column_span;content.title=`原表行 ${item.row+1}，列 ${item.column+1}`;const text=el('div',item.text||'（空白）');text.style.whiteSpace='pre-wrap';content.append(text);
    if(item.review_state==='uncertain')content.append(el('small','本格待复核'));
    if(item.diagnostics?.includes('TEXT_INSERTION_NEAR_OUTER_EDGE'))content.append(el('small','文字靠近外边框，归属待核'));
    const button=el('button','定位原图');button.className='text-button';button.onclick=()=>Promise.resolve(openSource(item)).catch(onError);content.append(button);
    row.append(content);
  }
  root.append(el('p','原图文字、单元格结构与字段关系分别保留；整表已读不代表整张图纸解析完整。'));
  return root;
}
