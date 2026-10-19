import {visibleBounds,intersects} from './render-viewport.mjs';

export class PreviewStream {
 constructor(manifest,fetcher,onChange=()=>{}) {
  this.manifest=manifest;this.fetcher=fetcher;this.onChange=onChange;
  this.items=new Array(manifest.total_items);this.loaded=new Set();this.pending=new Set();
  this.failed=new Map();this.wanted=new Set();this.waiters=[];this.disposed=false;
  this.controller=new AbortController();
  this.resident=new Map();this.residentCount=0;this.cacheLimit=40000;
  this.prefix=`/api/drawings/${encodeURIComponent(manifest.snapshot_id)}/preview/${manifest.revision}/`;
 }
 get ready(){return [...this.wanted].every(name=>this.loaded.has(name));}
 get error(){return [...this.wanted].map(name=>this.failed.get(name)).find(Boolean);}
 setTarget(layout,view,width,height) {
  if(this.disposed || width<=0 || height<=0) return;
  const q=visibleBounds(view,width,height,.25);
  const needed=[this.manifest.bases[layout]];
  for(const chunk of this.manifest.chunks) if(chunk.layout_id===layout && chunk.max_height*q.scale*1.2>=(chunk.min_pixels??2) && intersects(chunk.bbox,q.box)) needed.push(chunk.file);
  this.wanted=new Set(needed.filter(Boolean));
  for(const name of this.wanted) if(this.resident.has(name)) {
   const indices=this.resident.get(name);this.resident.delete(name);this.resident.set(name,indices);
  }
  this.evict();this.drain();this.settle();
 }
 evict() {
  // Retain the working view even if it exceeds the cache budget. Only evict
  // offscreen packets, which can be fetched again at their immutable URL.
  for(const [name,indices] of this.resident) {
   if(this.residentCount<=this.cacheLimit)break;
   if(this.wanted.has(name))continue;
   for(const index of indices)delete this.items[index];
   this.residentCount-=indices.length;this.resident.delete(name);this.loaded.delete(name);
  }
 }
 drain() {
  if(this.disposed)return;
  for(const name of this.wanted) {
   if(this.pending.size>=4)break;
   if(this.loaded.has(name)||this.pending.has(name)||this.failed.has(name))continue;
   this.pending.add(name);
   this.fetcher(this.prefix+name,{signal:this.controller.signal}).then(entries=>{
    if(this.disposed)return;
    for(const [index,item] of entries)this.items[index]=item;
    this.loaded.add(name);
    const indices=entries.map(entry=>entry[0]);this.resident.set(name,indices);this.residentCount+=indices.length;
    this.evict();
   }).catch(error=>{if(!this.disposed)this.failed.set(name,error);}).finally(()=>{
    this.pending.delete(name);if(this.disposed)return;
    this.drain();this.onChange();this.settle();
   });
  }
 }
 wait() {
  if(this.disposed || this.ready)return Promise.resolve();
  if(this.error)return Promise.reject(this.error);
  return new Promise((resolve,reject)=>this.waiters.push({resolve,reject}));
 }
 settle(){if(this.ready || this.error){for(const w of this.waiters.splice(0))this.error?w.reject(this.error):w.resolve();}}
 retry(){for(const name of this.wanted)this.failed.delete(name);this.drain();}
 metadata(){return this.metaPromise??=(this.fetcher(this.prefix+'meta',{signal:this.controller.signal}).catch(error=>{this.metaPromise=null;throw error;}));}
 dispose(){this.disposed=true;this.controller.abort();for(const w of this.waiters.splice(0))w.resolve();}
}
