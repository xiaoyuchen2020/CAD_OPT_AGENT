import test from 'node:test';
import assert from 'node:assert/strict';
import {recordPages,renderSourceRecords} from '../web/source-records.mjs';
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.textContent='';}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
}
const document={createElement:tag=>new Element(tag)};
const group=i=>({record:`a${i}`,title:`外墙${i}`,snapshot_id:'other',source_scope:{frame_name:'图框'},states:{binding_state:'source_bound'},claim_indexes:[i]});
test('whole groups paginate and citations retain validated numbering',async()=>{
 const result={source_records:{groups:Array.from({length:7},(_,i)=>group(i))},claims:Array.from({length:7},()=>({display_text:'第3项：5厚砂浆',citation_numbers:[1]})),citations:[{evidence_url:'/source/1',quote:'3. 5厚砂浆'}]};
 assert.deepEqual(recordPages(result).map(p=>p.length),[6,1]);
 const seen=[];const view=renderSourceRecords(result,{document,openSource:source=>seen.push(source)});
 const body=view.children[1],nav=view.children[2];assert.equal(body.children.length,6);
 await body.children[0].children[2].onclick();assert.equal(seen[0].quote,'3. 5厚砂浆');
 nav.children[2].onclick();assert.equal(body.children.length,1);assert.equal(nav.children[2].disabled,true);
 nav.children[0].onclick();assert.equal(body.children.length,6);
});
test('candidate and unresolved-reference states remain visible and text stays inert',()=>{
 const g=group(0);g.title='<script>alert(1)</script>';g.states={binding_state:'candidate',reference_state:'unresolved'};
 const view=renderSourceRecords({source_records:{groups:[g]},claims:[{text:'原文',citation_numbers:[]}]},{document,openSource:()=>{}});
 const block=view.children[1].children[0];assert.ok(block.children[0].textContent.includes('<script>'));
 assert.ok(block.children[1].textContent.includes('未将引用层次自动叠加'));
 assert.equal(block.innerHTML,undefined);
});
test('coverage gaps outside record groups are never hidden by pagination',()=>{
 const view=renderSourceRecords({source_records:{groups:[group(0)]},claims:[{text:'来源原文'},{text:'另有命中边界未核实',state:'unknown'}]},{document,openSource:()=>{}});
 assert.equal(view.children.at(-1).textContent,'另有命中边界未核实');
});
