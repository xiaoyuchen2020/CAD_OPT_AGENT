"""Conservative plan-title aliases with explicit unspecified-mode candidates."""
import re


def plan_title_key(text):
    text = re.sub(r'\s+', '', text).casefold().removesuffix('平面图')
    # These two spellings denote the same basement floor, not a substring match.
    return re.sub(r'^地下室(?=[一二三四五六七八九十百零〇两\d]+层)', '地下', text)


def same_plan_title(candidate, query):
    return bool(query.strip()) and plan_title_key(candidate) == plan_title_key(query)


def plan_title_relation(candidate, query):
    """Unspecified operating mode widens candidate discovery, never equivalence.

    Floor, roof/mezzanine/detail qualifiers are never removed. Explicit peace
    and wartime titles remain different identities even on the same floor.
    """
    a, b = plan_title_key(candidate), plan_title_key(query)
    if not b:
        return 'unfiltered'
    if a == b:
        return 'equivalent'
    if not any(mode in b for mode in ('平时', '战时')):
        for mode in ('平时', '战时'):
            if mode in a and a.replace(mode, '', 1) == b:
                return 'unspecified_mode'
    return 'different'
