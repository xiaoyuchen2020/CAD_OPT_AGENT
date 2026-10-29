import importlib.util
from pathlib import Path
import pytest
spec=importlib.util.spec_from_file_location('gate',Path(__file__).parents[1]/'scripts/fleet_sandbox_docker.py')
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
IMAGE='sha256:'+'a'*64
NAME='cad-table-'+'b'*32

def test_fixed_run_and_denied_privileged_docker():
    args=['run','--name',NAME,*gate.PREFIX,IMAGE]
    assert gate.validate(args,[IMAGE])==('run',NAME)
    for invalid in [args[:-1]+['other-image'],args[:3]+['--privileged']+args[3:],
                    ['run','--name',NAME,'-v','/:/host',IMAGE],['exec',NAME,'sh'],
                    ['rm','-f','oda-service'],['image','inspect','unapproved']]:
        with pytest.raises(ValueError):gate.validate(invalid,[IMAGE])
