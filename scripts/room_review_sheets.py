"""Deterministic contact sheets; never label automated comparisons as visual approval."""
import json
from pathlib import Path
from PIL import Image, ImageOps, ImageDraw
root=Path('artifacts/ar030-real-room-validation-20261015');out=root/'after/sheets';out.mkdir(exist_ok=True)
rows=json.loads((root/'review_points.json').read_text());manifest=[]
for offset in range(0,len(rows),8):
 group=rows[offset:offset+8]
 if not all((root/'after/figures'/(r['key']+'.png')).exists() for r in group):continue
 sheet=Image.new('RGB',(2048,512*4),'#e8edf1')
 for j,r in enumerate(group):
  im=Image.open(root/'after/figures'/(r['key']+'.png'));im.thumbnail((1024,512));sheet.paste(im,((j%2)*1024,(j//2)*512))
 name=f'{offset//8+1:02}.jpg';sheet.save(out/name,quality=92)
 manifest.append({'sheet':name,'keys':[r['key'] for r in group]})
(out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2));print('sheets',len(manifest))
