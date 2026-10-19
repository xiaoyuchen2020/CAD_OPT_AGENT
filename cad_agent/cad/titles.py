"""Conservative title equivalence; never equate floor-name substrings."""
import re


def plan_title_key(text):
    return re.sub(r'\s+','',text).casefold().removesuffix('平面图')


def same_plan_title(candidate, query):
    return bool(query.strip()) and plan_title_key(candidate)==plan_title_key(query)
