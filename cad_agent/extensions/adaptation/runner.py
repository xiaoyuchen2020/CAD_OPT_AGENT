"""Pinned container entry point. Untrusted code is executed ONLY inside Linux isolation."""
import json
import sys
import types
import hashlib
from pathlib import Path

# -I intentionally excludes cwd; this path is baked into the read-only image.
sys.path.insert(0, '/app')
from fixed_worker import recover

request = json.load(sys.stdin)
source = request['code']
if len(source.encode()) > 24000 or hashlib.sha256(source.encode()).hexdigest() != request['code_sha256']:
    raise ValueError('CODE_INTEGRITY_FAILED')
sdk = types.ModuleType('sdk')
sdk.recover = recover
sys.modules['sdk'] = sdk
namespace = {'__name__': 'adaptation'}
exec(compile(source, '<immutable-adaptation>', 'exec'), namespace)
output = namespace['adapt'](request['bundle'])
json.dump(output, sys.stdout, ensure_ascii=False, allow_nan=False)
