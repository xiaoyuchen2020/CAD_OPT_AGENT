"""Authenticated workbenches with fixed loopback upstreams and no credential forwarding."""
import json
import os
import re
from urllib.parse import unquote, urlsplit
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response


def rewrite(value, prefix, key=None):
    """Rewrite link fields only: factual answer and quoted source strings stay exact."""
    if isinstance(value, str):
        is_link = key in ('url', 'href', 'src', 'urls') or bool(key and key.endswith(('_url', '_urls')))
        return prefix + value if is_link and value.startswith(('/api/', '/assets/')) else value
    if isinstance(value, list):
        return [rewrite(x, prefix, key) for x in value]
    if isinstance(value, dict):
        return {k: rewrite(v, prefix, k) for k, v in value.items()}
    return value


def upstream_url(node):
    value = os.environ.get('CAD_FLEET_AGENT_'+node.upper()+'_URL',
                           'http://127.0.0.1:'+('8765' if node=='a' else '8766'))
    parsed = urlsplit(value)
    try:
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname in ('127.0.0.1', 'localhost', '::1')
                 and parsed.port and not parsed.username and not parsed.password
                 and parsed.path in ('', '/') and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(503, '执行节点地址必须配置为本机服务')
    return value.rstrip('/')


def allowed_path(path):
    # ASGI decodes once; also check residual encodings before forwarding to another decoder.
    decoded = path
    for _ in range(4):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    if '%' in decoded or '\\' in decoded or '\x00' in decoded or '?' in decoded or '#' in decoded:
        return False
    if decoded.startswith('/') or any(p in ('.', '..') for p in decoded.split('/')):
        return False
    normalized = '/'.join(p for p in decoded.split('/') if p)
    return not normalized.startswith(('api/fleet-', 'api/fleet/'))


def build_router(app):
    router = APIRouter()

    @router.api_route('/nodes/{node}/{path:path}', methods=['GET','HEAD','POST','PUT','PATCH','DELETE'])
    async def proxy(node: str, path: str, request: Request):
        app.state.require_session(request)
        node = node.lower()
        if node not in ('a','b') or not allowed_path(path):
            raise HTTPException(404)
        prefix='/nodes/'+node
        base=upstream_url(node)
        target=httpx.URL(base).copy_with(path='/'+path, query=request.scope.get('query_string', b''))
        headers={k:v for k,v in request.headers.items() if k.lower() in ('content-type','accept','range')}
        headers['accept-encoding']='identity'
        # No Authorization, Cookie, X-Fleet-Token, Host or forwarded headers cross this boundary.
        try:
            async with httpx.AsyncClient(timeout=660, trust_env=False, follow_redirects=False) as client:
                result=await client.request(request.method,target,headers=headers,content=await request.body())
        except httpx.HTTPError:
            raise HTTPException(502,'执行节点暂时无法连接') from None
        content=result.content
        ct=result.headers.get('content-type','application/octet-stream')
        if 'text/html' in ct:
            content=re.sub(rb'((?:src|href|action)\s*=\s*["\x27])(/(?:api|assets)/)',
                           lambda m:m[1]+prefix.encode()+m[2], content, flags=re.IGNORECASE)
        elif any(t in ct for t in ('javascript','text/css')):
            content=content.replace(b'/api/',(prefix+'/api/').encode()).replace(b'/assets/',(prefix+'/assets/').encode())
        elif 'application/json' in ct:
            try: content=json.dumps(rewrite(result.json(),prefix),ensure_ascii=False).encode()
            except ValueError: pass
        out={k:v for k,v in result.headers.items() if k.lower() in ('content-type','content-disposition','content-range')}
        out.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
                    'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'self'"})
        return Response(content,status_code=result.status_code,headers=out)
    return router
