"""Explicit permission in this request only; drawing text/history are not inputs."""
import re


def _instructions(question):
    # Examples, quoted source text and code are not user execution directives.
    text = re.sub(r'```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)', '', question)
    text = re.sub(r'(?m)^\s*>.*$', '', text)
    text = re.sub(r'`[^`]*`|“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|‘[^’]*’', '', text)
    return [part.strip() for part in re.split(r'[。；;\n，,]|(?<=[？！!?])', text) if part.strip()]


_CONDITION = r'(?:(?:必要时|需要时|如有需要|在读取时|(?:如果|若|当|在)?(?:已有|现有)工具(?:无法解决问题|不足|无法完成)(?:时)?)[：:\s]*)?'
_PREFIX = r'^(?:请\s*)?(?:(?:本轮|本次|这次)(?:任务|问答)?\s*)?' + _CONDITION
_ALLOW = re.compile(_PREFIX + r'(?:我)?(?:明确)?(?:允许|授权|可以)\s*(?:你|Agent|agent)?\s*' + _CONDITION
                    + r'(?:使用|执行|运行|生成(?:并(?:执行|运行))?)?\s*动态代码(?:执行)?(?:[。！!\s]|$|来|以|处理|完成|解决)')
_DIRECT = re.compile(_PREFIX + r'(?:使用|生成并执行|生成并运行)动态代码(?:[。！!\s]|$|来|以|处理|完成|解决)')
_DENY = re.compile(r'(?:不允许|不授权|禁止|不要|不得|不准|不能|禁用|关闭|不使用|不执行|不运行)[^。；;\n，,]{0,16}动态代码')
_QUESTION = re.compile(r'[？?]|是否|能否|可否|允许吗|可以吗|如何|怎么|假设|例如|比如|如果我说|如果输入|若输入|有人说|图纸(?:写|说)|原文(?:写|说)|解释')


def authorization(request):
    """Question denial wins; an unchecked UI switch doesn't veto permission."""
    clauses = _instructions(getattr(request, 'question', ''))
    directives = [c for c in clauses if not _QUESTION.search(c)]
    if any(_DENY.search(c) for c in directives):
        return dict(mode='off', basis='explicit_question_denial')
    if any(_ALLOW.search(c) or _DIRECT.search(c) for c in directives):
        return dict(mode='task_verified', basis='explicit_question_permission')
    if getattr(request, 'dynamic_methods', 'off') == 'task_verified':
        return dict(mode='task_verified', basis='request_switch')
    return dict(mode='off', basis='disabled')
