import test from 'node:test';
import assert from 'node:assert/strict';
import {componentPageNavigation} from '../web/component-pages.mjs';

test('variable evidence pages visit every offset and go back to actual previous pages', () => {
  const first=componentPageNavigation({has_more:true,next_offset:3},0);
  assert.equal(first.previous,null);
  const second=componentPageNavigation({has_more:true,next_offset:5},first.next.offset,first.next.history);
  assert.equal(second.previous.offset,0);
  const last=componentPageNavigation({has_more:false,next_offset:null},second.next.offset,second.next.history);
  assert.equal(last.next,null);
  assert.deepEqual(last.previous,{offset:3,history:[0]});
  assert.deepEqual(componentPageNavigation({has_more:true,next_offset:5},last.previous.offset,last.previous.history).next,second.next);
});

test('legacy responses advance by delivered count and empty pages never loop', () => {
  assert.equal(componentPageNavigation({has_more:true,items:[1,2]},3).next.offset,5);
  assert.equal(componentPageNavigation({has_more:true,items:[]},3).next,null);
  assert.equal(componentPageNavigation({has_more:true,next_offset:3},3).next,null);
  assert.equal(componentPageNavigation({has_more:true,next_offset:null},3).next,null);
});
