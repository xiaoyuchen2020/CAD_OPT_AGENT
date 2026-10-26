"""Versioned source readings shared by parsing, validation, arithmetic and display.

Offsets always refer to the unchanged source. A reading is a lexical identity,
not proof of object ownership, applicability, dimensions or engineering units.
"""
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import re
from functools import lru_cache

VERSION = 'source-numbers-v1'
NUMBER = r"-?\d+(?:\.\d+)?"
_SOURCE_NUMBER = r"[-−]?\d+(?:\.\d+)?"
_NUMBER_TOKEN = re.compile(r"(?<![\d.\-−])" + _SOURCE_NUMBER + r"(?![\d.])")
_QUANTITY_SUFFIXES = {'mm', 'cm', 'm', 'km', 'mm2', 'cm2', 'm2', 'mm3', 'cm3', 'm3',
                      'mm²', 'cm²', 'm²', 'mm³', 'cm³', 'm³', 'h', 'H', 's', 'min',
                      'kg', 'g', 't', 'N', 'kN', 'Pa', 'kPa', 'MPa', 'W', 'kW'}


def _thickness_list_marker(text, start, end):
    """Interpret a line-leading hyphen only in an explicit thickness layer.

    A line boundary and the Chinese thickness suffix are both necessary.
    Unicode minus, ranges, offsets, elevations and identifier separators never
    take this path. Keep the sign in the source span for later auditing.
    """
    return (text[start:start + 1] == '-'
            and not text[text.rfind('\n', 0, start) + 1:start].strip(' \t\r')
            and bool(re.match(r'(?:mm|cm|毫米|厘米)?\s*厚(?!度)(?=[\u3400-\u9fffA-Za-z（(])', text[end:])))


def numeric_tokens(text):
    """Yield source-preserving numeric readings; no unit inference."""
    for match in _NUMBER_TOKEN.finditer(text):
        marker = _thickness_list_marker(text, *match.span())
        yield dict(raw=match.group(), value=match.group()[1:] if marker else match.group(),
                   text_span=list(match.span()),
                   interpretation='thickness_list_marker' if marker else 'literal')


def numbers(text):
    return [token['value'] for token in numeric_tokens(text)]


def _decimal_quantity(text, start, end):
    """Only ordinary quantities may change precision; codes remain literal."""
    token = text[start:end]
    if token.startswith('−'):
        return False  # Preserve a native Unicode sign literally; do not normalize it away.
    integer = token.lstrip('-').split('.')[0]
    if len(integer) > 1 and integer.startswith('0'):
        return False
    if start and re.match(r'[A-Za-z_/.\-−]', text[start - 1]):
        return False
    suffix = re.match(r'[A-Za-z_][A-Za-z_0-9²³]*', text[end:])
    if suffix and suffix.group() not in _QUANTITY_SUFFIXES:
        return False
    return not (text[end:end + 1] in ('/', '_'))


def _equivalent_token(source, start, end, value, value_start, value_end):
    a, b = source[start:end], value[value_start:value_end]
    if _thickness_list_marker(source, start, end):
        a = a[1:]
    if _thickness_list_marker(value, value_start, value_end):
        b = b[1:]
    if a == b:
        return True
    return (a.startswith('-') == b.startswith('-')
            and _decimal_quantity(source, start, end)
            and _decimal_quantity(value, value_start, value_end)
            and Decimal(a) == Decimal(b))


def literal_contains_number(text, value):
    """Check a bound reading, allowing precision and explicit layer markers.

    This is not a unit conversion or a claim-semantic check. Callers must retain
    the source quote, including its units, inequalities and material identifiers.
    Fractions and identifier digits retain the existing literal-only contract.
    """
    if not re.fullmatch(_SOURCE_NUMBER, value):
        # Retain literal fractions; they are not decimal conversions.
        return bool(re.search(r"(?<![\d.\-−])" + re.escape(value) + r"(?![\d.])", text))
    return any(_equivalent_token(text, m.start(), m.end(), value, 0, len(value))
               for m in _NUMBER_TOKEN.finditer(text))


def contains_numeric_phrase(text, value):
    """Match a literal phrase except for safely equivalent numeric precision.

    Compare every nonnumeric character verbatim, so units, inequality signs,
    material names and their order cannot be rewritten by numeric matching.
    """
    tokens = list(_NUMBER_TOKEN.finditer(value))
    if not tokens:
        return False
    parts, previous = [], 0
    for token in tokens:
        parts.extend((re.escape(value[previous:token.start()]), '(' + _NUMBER_TOKEN.pattern + ')'))
        previous = token.end()
    parts.append(re.escape(value[previous:]))
    pattern = ''.join(parts)
    if re.match(r'[A-Za-z_]', value):
        pattern = r'(?<![A-Za-z_])' + pattern
    if re.search(r'[A-Za-z_²³]$', value):
        pattern += r'(?![A-Za-z_\d²³])'
    for match in re.finditer(pattern, text):
        if all(_equivalent_token(text, *match.span(i), value, token.start(), token.end())
               for i, token in enumerate(tokens, 1)):
            return True
    return False



UNIT_TEXT={'mm':r'mm|毫米','cm':r'cm|厘米','m':r'(?<![a-z])m(?![a-z²2])|米(?![²2])',
           'mm2':r'mm[²2]|平方毫米','cm2':r'cm[²2]|平方厘米','m2':r'(?<![a-z])m[²2]|㎡|平方米',
           'm2/count':r'(?:m[²2]|㎡|平方米)\s*[/／]\s*(?:车位|车|辆|个|count)',
           'percent':r'%|％','count':r'个|处|辆|车位|樘|部|间|数量','ratio':r'比例|比值|系数'}



def explicit_unit(text, start, end):
    order=('m2/count','mm2','cm2','m2','mm','cm','m','percent','count','ratio')
    unit=next((u for u in order if re.match(r'\s*(?:'+UNIT_TEXT[u]+r')(?![A-Za-z²0-9])',text[end:],re.I)), '')
    if not unit and re.search(r'(?:比例|比值|系数)\s*[:：=]?\s*$',text[:start]): unit='ratio'
    return unit


# Character-wise translation keeps offsets stable; never NFKC an entire quote.
_TRANSLATION = str.maketrans('０１２３４５６７８９．', '0123456789.')
_CIRCLED = {chr(0x2460+i): str(i+1) for i in range(20)}
_MARKER = re.compile(
    r'(?:^|[\n；;]|(?<!\d)[:：])[ \t]*'
    r'(?P<marker>(?:(?P<opening>[（(]?)(?P<n>[1-9]\d{0,2})'
    r'(?P<sep>[.、)）])(?P<gap>[ \t]*))|(?P<circle>[①-⑳])|第(?P<named>\d{1,3})项[：:])')


@lru_cache(maxsize=2048)
def ordinals(text):
    """Explicit delimiters are independent; compact dots require list context.

    Only a consecutive 1/2/3 run with two unambiguous initial items can resolve
    a decimal-looking dot. Gaps never turn an ambiguous decimal into an ordinal.
    """
    normalized = text.translate(_TRANSLATION)
    candidates = []
    for m in _MARKER.finditer(normalized):
        opening, sep = m['opening'], m['sep']
        if opening and (opening, sep) not in {('(', ')'), ('（', '）')}:
            continue
        n = m['n'] or m['named'] or _CIRCLED[m['circle']]
        tail = normalized[m.end():]
        explicit = sep != '.' or bool(m['gap']) or bool(tail and not re.match(r'[\d.]', tail))
        # A chapter reference is not a list item even when it has trailing text.
        if sep == '.' and re.match(r'\d+(?:\.\d+)+(?:条|节|款|\s|$)', tail):
            continue
        candidates.append(dict(value=n, text_span=[m.start('marker'),m.end()], explicit=explicit))
    accepted = {tuple(c['text_span']) for c in candidates if c['explicit']}
    i = 0
    while i < len(candidates):
        group = [candidates[i]]; i += 1
        while i < len(candidates) and int(candidates[i]['value']) == int(group[-1]['value'])+1:
            # Blank lines and a new heading break contextual numbering.
            between = normalized[group[-1]['text_span'][1]:candidates[i]['text_span'][0]]
            if re.search(r'\n\s*\n|\n[^\n]*(?:——|做法表)', between): break
            group.append(candidates[i]); i += 1
        if len(group) >= 3 and group[0]['value'] == '1' and all(c['explicit'] for c in group[:2]):
            accepted.update(tuple(c['text_span']) for c in group)
    return [dict(kind='ordinal', value=c['value'], raw=text[slice(*c['text_span'])],
                 text_span=c['text_span'], status='confirmed',
                 interpretation='explicit_delimiter' if c['explicit'] else 'consecutive_list')
            for c in candidates if tuple(c['text_span']) in accepted]


def mask_ordinals(text, tokens):
    result = list(text)
    for token in tokens:
        a,b = token['text_span']
        result[a:b] = ' '*(b-a-1)+')'  # preserve a negative value's boundary
    return ''.join(result)


def without_claim_list_markers(text):
    """Compatibility projection backed by the same source ordinal parser."""
    return mask_ordinals(text, ordinals(text))


def format_numbered_text(text, atoms=None):
    """Reading view only. The source quote and stored claim text are immutable."""
    result = text
    for token in reversed(ordinals(text) if atoms is None else [t for t in atoms if t['kind']=='ordinal']):
        a,b = token['text_span']
        result = result[:a] + '第'+token['value']+'项：' + result[b:]
    return result


def _identifier(text, a, b):
    # Grade/model names and construction/axis references retain lexical identity.
    left = re.search(r'(?:[A-Za-z_][A-Za-z_0-9.\-]*|(?:外墙面?|内墙面?|楼地面|地面|屋面|顶棚|踢脚|轴|编号|型号)\s*)$', text[:a])
    right = re.match(r'[A-Za-z_][A-Za-z_0-9.\-]*', text[b:])
    if left or (right and right.group() not in _QUANTITY_SUFFIXES):
        lo = left.start() if left else a
        hi = b+len(right.group()) if right else b
        return [lo,hi]
    if re.match(r'\s*(?:号|轴|防火分区|[#＃])', text[b:]):
        return [a,b+re.match(r'\s*(?:号|轴|防火分区|[#＃])', text[b:]).end()]
    return None


@lru_cache(maxsize=2048)
def readings(text, source_key='', entity_ordinals=()):
    """Typed atoms with stable identities and original spans; no unit inference."""
    result = [dict(t) for t in ordinals(text)]
    for a,b,value in entity_ordinals:
        if not any(t['text_span']==[a,b] for t in result):
            result.append(dict(kind='ordinal',value=value,raw=text[a:b],text_span=[a,b],
                status='confirmed',interpretation='separate_aligned_entity'))
    result.sort(key=lambda t:t['text_span'])
    projected = mask_ordinals(text,result).translate(_TRANSLATION)
    references = []
    # Explicit references to source list items are not model identifiers.
    # Keep bare 编号/型号 and decimal or alphanumeric codes on the identity path.
    reference_pattern = (r'第(?P<item>\d+(?:[、,，]\d+)*)[项层条节]'
                         r'|(?:原编号|原序号|序号)\s*[:：]?\s*'
                         r'(?P<source>\d+(?:[、,，]\d+)*)(?![\d.A-Za-z_—–~～-])')
    for m in re.finditer(reference_pattern, projected):
        group = 'item' if m['item'] is not None else 'source'
        for n in re.finditer(r'\d+',m[group]):
            a,b = m.start(group)+n.start(),m.start(group)+n.end()
            references.append((a,b))
            result.append(dict(kind='ordinal_reference',status='confirmed',value=n.group(),raw=text[a:b],
                text_span=[a,b],interpretation='explicit_reference',role=''))
    for token in numeric_tokens(projected):
        a,b = token['text_span']; before,tail = projected[:a],projected[b:]
        if (a,b) in references: continue
        kind, status = 'quantity','confirmed'
        identity = _identifier(projected,a,b)
        # Numeric runs with several dots are chapter/model references, never quantities.
        if identity:
            kind = 'identifier'
        elif (re.search(r'[\d.][.:/～~–]$',before) or re.match(r'[:/～~–-]\d',tail)
              or (token['value'].startswith('-') and re.search(r'\d$',before))):
            kind = 'expression_component'
        elif (token['interpretation']=='literal' and '.' in token['value'] and
              not before[before.rfind('\n')+1:].strip() and
              re.match(r'(?:mm|cm|毫米|厘米)?\s*厚',tail)):
            kind,status = 'ambiguous','needs_review'
        # Semicolon/colon-delimited compact decimals are likewise unresolved.
        elif (token['interpretation']=='literal' and '.' in token['value'] and re.search(r'[；;：:]\s*$',before)
              and re.match(r'(?:mm|cm|毫米|厘米)?\s*厚',tail)
              and not re.search(r'(?:厚度|尺寸)\s*[:：]\s*$',before)):
            kind,status = 'ambiguous','needs_review'
        role_match = re.search(r'(面积|停车数|车位数|数量|厚度|长度|宽度|净宽|标高)\s*(?:为|[:：=])?\s*(?:约)?\s*$', before)
        role = role_match[1] if role_match else '厚度' if re.search(r'厚\s*$',before) else '厚度' if re.match(r'\s*(?:mm|cm|毫米|厘米)?\s*厚(?!度)',tail) else ''
        result.append(dict(token,raw=text[a:b],kind=kind,status=status,role=role,unit=explicit_unit(projected,a,b),
            **({'identity':text[slice(*identity)]} if identity else {})))
    # A whole printed fraction can be an operand; its individual digits cannot.
    for m in re.finditer(r'(?<![A-Za-z_\d./])-?\d+/\d+(?![\d./A-Za-z_])',projected):
        result.append(dict(kind='quantity',status='confirmed',value=m.group(),raw=text[m.start():m.end()],
            text_span=list(m.span()),interpretation='literal_fraction',role='ratio'))
    # Multi-dot chapters are not seen by the ordinary decimal lexer.
    for m in re.finditer(r'(?<![\d.])\d+(?:\.\d+){2,}(?![\d.])',projected):
        result.append(dict(kind='identifier',status='confirmed',value=m.group(),raw=text[m.start():m.end()],
            identity=text[m.start():m.end()],text_span=list(m.span()),interpretation='chapter_reference',role=''))
    for token in result:
        a,b = token['text_span']
        token['number_id'] = 'num_'+sha256((VERSION+'\x1f'+source_key+'\x1f'+text+'\x1f'+str(a)+':'+str(b)).encode()).hexdigest()[:20]
    return sorted(result,key=lambda t:t['text_span'])


def evidence_readings(evidence):
    """Old evidence is parsed lazily. Versioned source context survives slicing."""
    catalog = evidence.get('numeric_readings')
    if (catalog and catalog.get('version') == VERSION
            and catalog.get('quote_sha256') == sha256(evidence['quote'].encode()).hexdigest()):
        return catalog['items']
    return readings(evidence['quote'], evidence.get('evidence_id',''))


def matching_quantities(evidence, value, quantity_id=''):
    candidates = []
    for token in evidence_readings(evidence):
        if token['kind'] != 'quantity' or token['status'] != 'confirmed': continue
        if quantity_id and token['number_id'] != quantity_id: continue
        try:
            if token['value']==value or ('/' not in token['value']+value and Decimal(token['value']) == Decimal(value)): candidates.append(token)
        except InvalidOperation:
            continue
    return candidates


def equivalent_value(source, value):
    if source == value: return True
    if not re.fullmatch(_SOURCE_NUMBER,source) or not re.fullmatch(_SOURCE_NUMBER,value): return False
    return _equivalent_token(source,0,len(source),value,0,len(value))


def unsupported_values(text, evidence, allowed=()):
    """Ordinals validate structurally; quantities cannot borrow code/ordinal digits."""
    sources = [t for e in evidence for t in evidence_readings(e)]
    missing = []
    exact = next((e for e in evidence if e['quote']==text), None)
    claim_tokens = evidence_readings(exact) if exact is not None else readings(text)
    for token in claim_tokens:
        if token['kind'] == 'ordinal': continue
        value = token['value']
        if token['kind']=='ordinal_reference':
            if not any(t['kind'] in ('ordinal','ordinal_reference') and t['value']==value for t in sources): missing.append(value)
            continue
        if value in allowed: continue
        if token['kind'] == 'identifier':
            ok = any(t['kind']=='identifier' and t.get('identity',t['raw']).translate(_TRANSLATION) == token.get('identity',token['raw']).translate(_TRANSLATION) for t in sources)
        else:
            ok = any(t['kind'] in ('quantity','ambiguous','expression_component') and
                     (t['value']==value if t['kind']=='expression_component' else
                      equivalent_value(t['value'],value)) for t in sources)
        if not ok: missing.append(value)
    return sorted(set(missing))


def contains_number(text, value):
    """Numeric presence in semantic readings, never an ordinal's digits."""
    if not re.fullmatch(_SOURCE_NUMBER,value):
        return literal_contains_number(text,value)
    for token in readings(text):
        if token['kind'] in ('ordinal','ordinal_reference'): continue
        if token['kind'] in ('identifier','expression_component'):
            if token['value']==value: return True
        elif '/' not in token['value'] and _equivalent_token(without_claim_list_markers(text).translate(_TRANSLATION),*token['text_span'],value,0,len(value)):
            return True
    return False


def source_contains_number(evidence, value):
    return any(t['kind'] not in ('ordinal','ordinal_reference') and
               (t['value']==value if t['kind'] in ('identifier','expression_component') else equivalent_value(t['value'],value))
               for t in evidence_readings(evidence))


def region_readings(region, snapshot_id):
    """Separate CAD numbering entities can resolve a compact decimal-like line.

    Require a complete marker entity followed by a distinct, aligned body entity
    in the same source region. Geometry alone never labels bare digits ordinal.
    """
    text=region.text
    parts=sorted(region.parts,key=lambda p:p.text_span)
    markers=[]
    for part,following in zip(parts,parts[1:]):
        a,b=part.text_span; c,d=following.text_span
        if a>=b or c>=d or b>c or text[b:c].strip() or '\n' in text[b:c]: continue
        raw=text[a:b].translate(_TRANSLATION)
        m=re.fullmatch(r'\s*([1-9]\d{0,2})\.\s*',raw)
        if not m or part.entity_id==following.entity_id: continue
        line_start=text.rfind('\n',0,a)+1
        if text[line_start:a].strip(): continue
        h=min(part.bbox[3]-part.bbox[1],following.bbox[3]-following.bbox[1])
        if h<=0 or abs((part.bbox[1]+part.bbox[3]-following.bbox[1]-following.bbox[3])/2)>.35*h: continue
        gap=following.bbox[0]-part.bbox[2]
        if not 0<=gap<=2*h: continue
        markers.append((a+m.start(1),b,m[1]))
    return readings(text,snapshot_id+':'+region.region_id,tuple(markers))


def format_claim_text(text, evidence):
    exact = next((e for e in evidence if e['quote']==text), None)
    return format_numbered_text(text, evidence_readings(exact) if exact is not None else None)
