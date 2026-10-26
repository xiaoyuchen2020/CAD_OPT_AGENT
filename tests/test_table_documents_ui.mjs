import test from 'node:test';
import assert from 'node:assert/strict';
import {renderTableDocuments} from '../web/table-documents.mjs';
class Element {constructor(tag){this.tag=tag;this.children=[];this.style={};}append(...nodes){this.children.push(...nodes);}}
const document={createElement:tag=>new Element(tag)};
test('grid retains merged spans, exact values, source links and uncertainty',async()=>{
 const data={title:'表格',coverage:{read_cells:3,selected_cells:4,table_read_complete:false},binding_state:'candidate',overlay_url:'/overlay',items:[
  {cell_id:'c1',row:0,column:0,row_span:2,column_span:1,text:'原文<script>',lineage:[],evidence_url:'/c1'},
  {cell_id:'c2',row:0,column:1,row_span:1,column_span:1,text:'933',lineage:[],evidence_url:'/c2'},
  {cell_id:'c3',row:1,column:1,row_span:1,column_span:1,text:'830',lineage:[],review_state:'uncertain',evidence_url:'/c3'}]};
 const seen=[];const view=renderTableDocuments({table_documents:data},{document,openSource:c=>seen.push(c.evidence_url),onError:e=>{throw e;}});
 assert.match(view.children[1].textContent,/尚未读完/);
 const body=view.children[3].children[1].children[0];assert.equal(body.children.length,2);
 const first=body.children[0].children[0];assert.equal(first.rowSpan,2);assert.equal(first.children[0].textContent,'原文<script>');assert.equal(first.innerHTML,undefined);
 await body.children[0].children[1].children[1].onclick();assert.deepEqual(seen,['/c2']);
 assert.equal(body.children[1].children[0].children[1].textContent,'本格待复核');
});
