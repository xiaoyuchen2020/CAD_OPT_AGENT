"""Adapt only the sidecar's served viewer; never edit the shared workbench file."""


def viewer_script(code):
    if "installSourceOverlay({" in code:
        return code  # Existing sidecar releases already contain these hooks.
    saved = 'const saved=keepConversation ? {items:[]} : await api(`/api/drawings/${id}/tasks`);'
    facts = '  renderScene();\n  await loadFacts();'
    if code.count(saved) != 1 or code.count(facts) != 1:
        raise ValueError('查看器接口已变化，拒绝注入，请更新来源总览适配器')
    code = code.replace(saved, "const sourceOverviewMode=new URLSearchParams(location.search).get('sources')==='1';\n  const saved=keepConversation || sourceOverviewMode ? {items:[]} : await api(`/api/drawings/${id}/tasks`);")
    code = code.replace(facts, '  renderScene();\n  if (!sourceOverviewMode) await loadFacts();')
    return ("import {installSourceOverlay} from './source-overlay.mjs';\n" + code
            + '\ninstallSourceOverlay({state:S,selectDrawing,renderScene,setView,ready:Promise.resolve()});\n')
