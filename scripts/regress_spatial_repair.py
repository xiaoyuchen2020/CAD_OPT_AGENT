"""Real-model regression: original cases retained, no scripted model answers."""
from pathlib import Path
import os,sys,fcntl,json,shutil,hashlib,time
import regress_complex_repair as suite
import regress_complex_extensions as extra
from regress_agent_workflow import ROOT
os.environ.setdefault('CAD_RENDER_FONT_DIR',str(ROOT/'.data/render-fonts'))
SUFFIX=Path(__file__).resolve().parents[1].name.split('20261016')[-1] or '-development'
suite.OUT=ROOT/('artifacts/spatial-repair-live-20261016'+SUFFIX)
original=suite.CASES+extra.CASES
priority=['door_width','adjacency','unnamed_spaces']
suite.CASES=sorted(original,key=lambda c:priority.index(c['id']) if c['id'] in priority else len(priority)+original.index(c))
suite.CASES += [
 dict(id='screened_followup',source=0,parent='unnamed_spaces',question='沿用刚才已逐项看图筛选的三处空间，只保留面积最小的一处；不要重新搜索名称。给出它的原编号、面积及外环周长，其他轮廓清掉。',acceptance=['继承已审查空间集合','不能重新引入排除对象','实际仅保留最小空间及原编号','面积周长独立复算']),
 dict(id='adjacency_rephrase',source=1,parent=None,question='按毫米，核对二层各组餐厅和厨房之间的实际通道。请逐组指出有没有门扇或推拉构件，把确实对应的门垛两侧量出来；窗户和装修表不要算进去。把测量边线留在图上。',acceptance=['不同问法实际逐处核查','两侧门垛源段测量可独立复算','拒绝外窗和表格名称','测距边线实际高亮']),
 dict(id='other_spaces_rephrase',source=1,parent=None,question='二层平面图选一块有实际建筑线条的局部，不按房间名称检索，以围合墙线发现空间。剔除床柜、表格和尺寸线围出的框，逐项看图后列出候选中最大的两处面积和外环周长，高亮所选轮廓。按毫米计算，仅代表已检查局部。',acceptance=['跨图纸无名称几何发现','逐项空间筛选','多边形和面积周长相符','真实高亮两处']),
 dict(id='door_negative_rephrase',source=2,parent=None,question='请看一层水泵房与天井相接的界面，区分真正直接相通的门和附近通向其他区域的门。若存在直接门，按毫米测两侧门垛间距；若没有可靠直接门，指出实际隔断边线并高亮，别套用附近1100的标注。',acceptance=['反例不借用旁侧门宽','实际边界图像核查','高亮实际源边界或门垛']),
]
BaseWorkspace=suite.Workspace
class SerialWorkspace(BaseWorkspace):
 def start_chat(self,request):
  path=ROOT/'artifacts/hybrid-retrieval-20261015/data/siwc-subscription/run.lock'
  if path.exists():
   for attempt in range(60):
    fd=os.open(path,os.O_RDONLY)
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
     os.close(fd)
     if attempt==0:print('WAIT shared subscription busy; preserving other run',flush=True)
     time.sleep(15);continue
    os.close(fd);break
   else:raise RuntimeError('Shared subscription stayed busy; no request sent.')
  return super().start_chat(request)
suite.Workspace=SerialWorkspace
base_prepare=suite.prepare

def prepare(w):
 base_prepare(w)
 selected=[]
 for n,arg in enumerate(sys.argv):
  if arg=='--case':selected.append(sys.argv[n+1])
 indices={c['source'] for c in suite.CASES if not selected or c['id'] in selected}
 from cad_agent.cad.render import RENDER_VERSION,font_signature
 for idx in sorted(indices):
  sid=suite.SOURCES[idx][1];cache=ROOT/'artifacts/spatial-repair-20261016-probe/data/drawings'/sid/'render.json'
  if cache.exists():
   data=json.loads(cache.read_text())
   if data.get('version')==RENDER_VERSION and data.get('font_signature')==font_signature():shutil.copy2(cache,w.store.artifact(sid,'render.json'))
  # Render cache preparation is explicit setup, not charged as model thinking.
  w.rendering(sid)
  print('READY',sid,flush=True)
suite.prepare=prepare

def code_hashes():
 root=Path(__file__).resolve().parents[1]
 return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'cad_agent').rglob('*.py')}
suite.code_hashes=code_hashes
if __name__=='__main__':
 suite.OUT.mkdir(parents=True,exist_ok=True)
 with (suite.OUT/'runner.lock').open('w') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);suite.main()
