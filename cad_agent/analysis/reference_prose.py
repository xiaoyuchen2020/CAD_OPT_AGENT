"""Reject internal reference prose without misreporting its digits as CAD quantities."""
import re

def check_reference_prose(text, aliases, literal_source=False):
    if literal_source:
        return text, []
    found=[]
    def replace(match):
        token=match.group()
        if token not in aliases:
            return token
        found.append(token)
        return '内部引用代号'
    masked=re.sub(r'(?<![A-Za-z0-9_])[erdon][1-9][0-9]*(?![A-Za-z0-9_])',replace,text)
    return masked, list(dict.fromkeys(found))
