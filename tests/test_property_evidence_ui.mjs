import test from 'node:test';
import assert from 'node:assert/strict';
import {propertyRows} from '../web/property-evidence.mjs';

test('different measurement bases remain separate with their own evidence', () => {
  const common = {subject_id:'door1',subject_label:'M1021',property_name:'width',property_label:'宽度',unit:'mm',unit_state:'explicit',binding_state:'source_bound'};
  const rows = propertyRows({items:[
    {...common,field_id:'table',value:900,basis:'source_nominal',comparison_state:'different_bases',source_refs:[{kind:'evidence',ref_id:'table-cell'}]},
    {...common,field_id:'geometry',value:920,basis:'measurement',measurement_basis:'opening_width',comparison_state:'different_bases',source_refs:[{kind:'observation',ref_id:'measure1'}]},
  ]});
  assert.equal(rows.length,2);
  assert.equal(rows[0].value,'900');
  assert.equal(rows[1].value,'920');
  assert.notEqual(rows[0].basis,rows[1].basis);
  assert.match(rows[1].state,/口径不同/);
  assert.deepEqual(rows[0].evidenceIds,['table-cell']);
  assert.deepEqual(rows[1].observationIds,['measure1']);
});
test('unknown and conditional units never look like confirmed measurements', () => {
  const rows = propertyRows({items:[
    {field_id:'a',value:null,unit:null,binding_state:'unresolved'},
    {field_id:'b',value:0,unit:'m',unit_state:'conditional',comparison_state:'conflict'},
  ]});
  assert.equal(rows[0].value,'未确认');
  assert.equal(rows[0].unit,'单位未确认');
  assert.equal(rows[1].value,'0');
  assert.match(rows[1].unit,/条件单位/);
  assert.match(rows[1].state,/冲突/);
});
