"""Source lookup vocabulary; aliases, directed children and literal facets."""
import re
from itertools import permutations

VERSION = 'source-vocabulary-v2'
BOARD_PREFIXES=('地下室','室外','室内','架空','分户','保温')
BOARD_TERMS={''.join(parts)+'楼板' for n in (1,2,3) for parts in permutations(BOARD_PREFIXES,n)}
BOARD_PREFIX=re.compile('|'.join(BOARD_PREFIXES))
ALIASES = {
    '外墙面': '外墙', '外墙饰面': '外墙', '内墙面': '内墙', '内墙饰面': '内墙',
    '天棚': '顶棚', '顶棚面': '顶棚', '天花': '顶棚', '天花板': '顶棚',
    '踢脚线': '踢脚', '雨蓬': '雨篷', '屋顶': '屋面', '不上人屋面': '非上人屋面',
}
CHILDREN = {
    '墙面': ('外墙', '内墙', '墙裙', '护墙'),
    '顶板': ('地下室顶板',), '底板': ('地下室底板',),
    '外墙': ('地下室外墙',),
    '楼地面': ('楼面', '地面', '地坪'),
    '顶棚': ('吊顶',), '踢脚': ('踢脚板',),
    '屋面': ('平屋面', '坡屋面', '种植屋面', '上人屋面', '非上人屋面', '倒置式屋面', '正置式屋面'),
    '楼板': ('架空楼板', '保温楼板', '分户楼板', '地下室楼板', '室外楼板', '室内楼板'),
    '幕墙': ('玻璃幕墙', '铝板幕墙', '石材幕墙'),
    '门窗': ('门', '窗'), '墙体': ('隔墙', '砌体'),
    '变形缝': ('伸缩缝', '沉降缝', '抗震缝'),
    '地下室': ('地下室外墙', '地下室侧壁', '地下室底板', '地下室顶板', '地下室顶棚', '地下室楼板'),
}
CLAUSE_FAMILIES = {'墙体', '隔墙', '砌体', '幕墙', '玻璃幕墙', '铝板幕墙', '石材幕墙',
    '门窗', '门', '窗', '建筑设计说明', '工程材料做法', '室内装修', '室外工程'}
NODE_FAMILIES = {'勒脚', '护墙', '栏杆', '栏板', '扶手', '楼梯', '踏步', '阳台', '女儿墙',
    '变形缝', '伸缩缝', '沉降缝', '抗震缝', '天沟', '檐沟', '防火封堵'}
FACETS = {'防水', '保温', '防潮', '防火', '防腐', '防锈', '油漆', '涂料',
    '防水涂膜', '保温板', '找平层', '保护层'}
TERMS = (BOARD_TERMS | set(ALIASES) | set(ALIASES.values()) | set(CHILDREN)
    | {c for children in CHILDREN.values() for c in children}
    | CLAUSE_FAMILIES | NODE_FAMILIES
    | {'地面', '楼地板', '墙身', '底板', '顶板', '顶板底', '雨篷', '散水', '台阶', '坡道', '墙裙'})
FAMILIES = '|'.join(re.escape(t) for t in sorted(TERMS, key=lambda t: (-len(t), t)))
FAMILY = re.compile(FAMILIES)
FACET = re.compile('|'.join(sorted(FACETS, key=lambda t: (-len(t), t))))


def canonical(term):
    if term in BOARD_TERMS:
        parts=set(BOARD_PREFIX.findall(term[:-2]))
        return ''.join(p for p in BOARD_PREFIXES if p in parts)+'楼板'
    return ALIASES.get(term, term)


def expand(term):
    """Include spelling aliases and descendants, never ancestors or siblings."""
    if term=='楼板':return {'楼板'}|BOARD_TERMS
    if term in BOARD_TERMS:
        required=set(BOARD_PREFIX.findall(term[:-2]))
        return {t for t in BOARD_TERMS if required<=set(BOARD_PREFIX.findall(t[:-2]))}
    found = {canonical(term)}
    todo = list(found)
    while todo:
        current=todo.pop()
        for child in (expand(current) if current in BOARD_TERMS else CHILDREN.get(current, ())):
            if child not in found:
                found.add(child); todo.append(child)
    return found | {a for a, c in ALIASES.items() if c in found}


def identities(text):
    # Longest token first preserves negation: 非上人屋面 cannot become 上人屋面.
    return {canonical(m[0]) for m in FAMILY.finditer(text)}


def normalized_code(text):
    # Character translation only; original sources and offsets are untouched.
    text = ''.join(chr(ord(c)-0xfee0) if 0xff01 <= ord(c) <= 0xff5e else c for c in text)
    text=re.sub(r'[\s—–－\-:：]', '', text).upper()
    family=FAMILY.match(text)
    return canonical(family[0])+text[family.end():] if family else text


def record_type(identity):
    found = identities(identity)
    if found & CLAUSE_FAMILIES: return 'material_clause'
    if found & NODE_FAMILIES: return 'node_record'
    return 'layer_assembly'
