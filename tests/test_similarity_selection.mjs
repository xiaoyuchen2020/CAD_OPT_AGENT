import test from 'node:test';
import assert from 'node:assert/strict';
import {selectionBox, similarityScope, similarityPath} from '../web/similarity-selection.mjs';

test('selection uses world Y and identical bounds in either drag direction', () => {
  const view=[0,-100,200,100], rect={left:10,top:20,width:400,height:200};
  assert.deepEqual(selectionBox(view,rect,[30,40],[110,100]),[10,60,50,90]);
  assert.deepEqual(selectionBox(view,rect,[110,100],[30,40]),[10,60,50,90]);
  assert.equal(selectionBox(view,rect,[30,40],[32,42]),null);
  assert.equal(selectionBox(view,rect,[30,40],[30,100]),null);
});
test('letterboxing and chosen frame delimit search without guessing frame identity', () => {
  const view=[0,-100,100,100], rect={left:0,top:0,width:400,height:200};
  assert.deepEqual(selectionBox(view,rect,[100,0],[300,200]),[0,0,100,100]);
  assert.deepEqual(similarityScope(view,rect,'model'),{
    layout_id:'model',frame_id:'',search_bbox:[-50,0,150,100]});
  assert.deepEqual(similarityScope(view,rect,'model','frame-1'),{
    layout_id:'model',frame_id:'frame-1'});
});
test('highlights preserve disconnected source paths without invented closure', () => {
  assert.equal(similarityPath({highlight_paths:[[[0,0],[10,0]],[[0,0],[0,10]]]}),'M0,0 L10,0 M0,0 L0,-10');
  assert.equal(similarityPath({highlight_paths:[[[0,0],[Infinity,2]]]}),'');
  assert.equal(similarityPath({highlight_paths:[Array(4097).fill([1,2])]}),'');
});
