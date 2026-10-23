import test from 'node:test';
import assert from 'node:assert/strict';
import {publishedCitations} from '../web/evidence-selection.mjs';

test('manifest excludes investigated sources without inventing or renumbering citations', () => {
  const result = {citations: [{evidence_id: 'inner'}, {evidence_id: 'outer', quote: '外墙原文'}],
    delivery_manifest: {selected_evidence_ids: ['outer', 'nonexistent']}};
  assert.deepEqual(publishedCitations(result), [{source: result.citations[1], number: 2, legacy: false}]);
});
test('historical citations remain inspectable with a legacy marker', () => {
  assert.equal(publishedCitations({citations: [{evidence_id: 'old'}]})[0].legacy, true);
  assert.deepEqual(publishedCitations({}), []);
});
