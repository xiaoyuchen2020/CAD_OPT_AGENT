"""Serve isolated synthetic acceptance data with existing SIWC and ODA connections.

Credentials stay in the configured account store. Only subscription model
profiles are exposed. No model request or DWG conversion starts automatically.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cad_agent.app import create_app
from cad_agent.config import Settings
from cad_agent.adapters.siwc import SiwcAccount
from cad_agent.demo import create_demo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8782)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    original = Settings.load()
    settings = replace(original, data_dir=(args.output/'data').resolve(),
                       api_base='', api_key='', model='', provider='custom',
                       llm_query_intent=False)
    app = create_app(settings)
    workspace = app.state.workspace
    workspace.siwc_account.close()
    workspace.siwc_account = SiwcAccount(original.data_dir, original.siwc_max_concurrency)
    workspace.model_catalog = {key: value for key, value in workspace.model_catalog.items()
                               if key == 'default' or key.startswith('siwc_')}
    source = args.output/'subscription-demo.dxf'
    if not source.exists():
        create_demo(source)
    drawing = workspace.ingest('真实订阅验收 · 合成设计说明.dxf', source.read_bytes(), synthetic=True)
    report = dict(snapshot_id=drawing['id'],
                  subscription_connected=workspace.siwc_account.status()['connected'],
                  oda_configured=workspace.converter.status()['configured'],
                  paid_api_profiles_exposed=False)
    (args.output/'setup.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
