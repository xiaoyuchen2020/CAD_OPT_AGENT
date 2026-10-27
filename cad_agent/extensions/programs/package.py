"""Build an immutable multi-module payload. Only the container compiles it."""
import ast
import hashlib
from pathlib import Path
import re
from .contracts import Package
from . import sdk
from ..table_recovery.bundle import digest

ALLOWED={'math','statistics','decimal','collections','itertools','functools','re','bisect','unicodedata','copy','json','cad_sdk'}


def validate(package):
    package=Package.model_validate(package)
    files=package.files
    if not 1<=len(files)<=4 or package.entry not in files:raise ValueError('PACKAGE_MODULES_INVALID')
    if sum(len(s.encode()) for s in files.values())>14000:raise ValueError('PACKAGE_CODE_LIMIT')
    if any(not re.fullmatch(r'[a-z][a-z0-9_]{0,30}',name) or name in ALLOWED | {'sys','types','builtins','os','io','pathlib','subprocess','socket','importlib'} for name in files):
        raise ValueError('PACKAGE_MODULE_NAME_INVALID')
    graph={}
    for name,code in files.items():
        tree=ast.parse(code)
        if name==package.entry and not any(isinstance(n,ast.FunctionDef) and n.name=='run' and len(n.args.args)==1 for n in tree.body):
            raise ValueError('PACKAGE_RUN_ENTRY_REQUIRED')
        imports=set()
        for n in ast.walk(tree):
            if isinstance(n,ast.Import):imports.update(a.name.split('.')[0] for a in n.names)
            if isinstance(n,ast.ImportFrom):
                if n.level:raise ValueError('PACKAGE_RELATIVE_IMPORT_FORBIDDEN')
                imports.add((n.module or '').split('.')[0])
        if imports-set(files)-ALLOWED:raise ValueError('PACKAGE_DEPENDENCY_UNAVAILABLE')
        graph[name]=imports & files.keys()
    ordered=[];pending=set()
    def visit(name):
        if name in pending:raise ValueError('PACKAGE_IMPORT_CYCLE')
        if name in ordered:return
        pending.add(name)
        for dependency in sorted(graph[name]):visit(dependency)
        pending.remove(name);ordered.append(name)
    for name in files:visit(name)
    return package,ordered


def sdk_hash():
    return hashlib.sha256(Path(sdk.__file__).read_bytes()).hexdigest()


def payload(package):
    package,order=validate(package)
    modules={'cad_sdk':Path(sdk.__file__).read_text()} | package.files
    # This string is data on the host. The existing pinned adaptation runner
    # executes adapt ONLY inside its no-network, unprivileged container.
    code='import sys, types\nfrom decimal import getcontext\ngetcontext().prec=48\n'
    code+='def adapt(bundle):\n'
    code+=f'    sources={modules!r}\n'
    code+=f'    for name in {(["cad_sdk"]+order)!r}:\n'
    code+='        module=types.ModuleType(name)\n        sys.modules[name]=module\n        exec(compile(sources[name], name, "exec"), module.__dict__)\n'
    code+=f'    return sys.modules[{package.entry!r}].run(bundle)\n'
    if len(code.encode())>24000:raise ValueError('PACKAGE_PAYLOAD_LIMIT')
    return code


def manifest(package):
    package,_=validate(package)
    return dict(package=package.model_dump(),package_sha256=digest(package.model_dump()),sdk_sha256=sdk_hash(),
                module_hashes={k:hashlib.sha256(v.encode()).hexdigest() for k,v in package.files.items()},
                dependencies='pinned_container_stdlib_and_cad_sdk')
