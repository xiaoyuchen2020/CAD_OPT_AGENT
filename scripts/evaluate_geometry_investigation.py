"""Freeze independent object gold, score predictions, or check one-factor comparability."""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cad_agent.geometry.experiment_protocol import freeze,load_frozen,score,compare_manifests


def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
    f=sub.add_parser('freeze');f.add_argument('cases');f.add_argument('output')
    s=sub.add_parser('score');s.add_argument('gold');s.add_argument('case_id');s.add_argument('prediction');s.add_argument('--guidance')
    c=sub.add_parser('compare');c.add_argument('a');c.add_argument('b');c.add_argument('--factor',required=True)
    a=p.parse_args();read=lambda name:json.loads(Path(name).read_text())
    if a.command=='freeze':result=freeze(read(a.cases),a.output)
    elif a.command=='score':
        gold=load_frozen(a.gold);case=next(c for c in gold['cases'] if c['case_id']==a.case_id)
        result=score(case,read(a.prediction),read(a.guidance) if a.guidance else [])|dict(gold_sha256=gold['sha256'])
    else:result=compare_manifests(read(a.a),read(a.b),a.factor)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
