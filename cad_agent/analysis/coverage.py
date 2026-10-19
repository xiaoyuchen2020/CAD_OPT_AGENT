"""Bounded provenance coverage, not a claim of semantic completeness."""
import re


def question_parts(question):
    # Explicit user enumeration only. Do not invent a domain-specific checklist.
    markers = list(re.finditer(r"[①②③④⑤⑥⑦⑧⑨⑩]|(?:^|\n)\s*\d{1,2}[、.)）]\s*", question))
    if not 2 <= len(markers) <= 10:
        return []
    return [{"id": f"q{i+1}", "text": question[m.end():markers[i+1].start() if i+1 < len(markers) else len(question)].strip()}
            for i, m in enumerate(markers)]


class EvidenceCoverage:
    def __init__(self, question):
        self.parts = question_parts(question)
        self.candidates = {}
        self.read_spans = {}
        self.reserve_used = False
        self.task_owners = {}

    def record(self, name, result, task_ids=None):
        if name == "search_sources" and result.get("items"):
            # Keep two leading candidates: the top semantic hit can be a general
            # clause while the next is the requested object's table row.
            # Reverse insertion preserves rank order within the newest search.
            for item in reversed(result["items"][:2]):
                rid = item.get("region_id")
                if rid:
                    start = item.get("snippet_start", 0)
                    self.task_owners.setdefault((rid,start),set()).update(task_ids or [])
                    self.candidates.pop((rid, start), None)
                    self.candidates[(rid, start)] = {"region_id": rid, "start": start, "neighbors": 1}
        def visit(value):
            if isinstance(value, dict):
                if "evidence_id" in value and "quote" in value and "text_span" in value:
                    self.read_spans.setdefault(value["region_id"], []).append(value["text_span"])
                else:
                    for child in value.values(): visit(child)
            elif isinstance(value, list):
                for child in value: visit(child)
        visit(result)

    def owners(self,args):
        return sorted(self.task_owners.get((args["region_id"],args["start"]),set()))

    def pending(self):
        return [args for (rid, start), args in reversed(list(self.candidates.items()))
                if not any(a <= start < b for a, b in self.read_spans.get(rid, []))][:2]

    def summary(self):
        return {"question_parts": self.parts, "pending_source_reads": self.pending(),
                "read_region_count": len(self.read_spans),
                "note": "候选未读不等于必须适用；已读数量不证明完整性。逐项回答或明确标 unknown。"}
