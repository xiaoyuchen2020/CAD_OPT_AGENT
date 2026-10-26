"""Shared reference representation. Only the source ledger grants evidence authority.

Aliases may live for a session or a review packet. Source definitions always live
for one request: a previous request's alias never substitutes for visible text.
"""
from copy import deepcopy
import json
import re

PREFIXES = {'ev': 'e', 'region': 'r', 'snap': 'd', 'obs': 'o', 'num': 'n'}
LONG_ID = re.compile(r'^(ev|region|snap|obs|num)_[0-9a-f]{16,64}$')
SHORT_ID = re.compile(r'^[erdon][1-9][0-9]*$')
IDENTITY_FIELDS = ('snapshot_id', 'region_id', 'text_span', 'quote')


def check_source_identity(left, right):
    if any(left[k] != right[k] for k in IDENTITY_FIELDS if k in left and k in right):
        raise ValueError('MODEL_CONTEXT_SOURCE_CONFLICT：同一证据ID的来源内容不一致')


class ReferenceAliases:
    def __init__(self, mapping=None):
        self.forward = dict(mapping or {})
        self.reverse = {alias: identity for identity, alias in self.forward.items()}
        if len(self.reverse) != len(self.forward):
            raise ValueError('MODEL_CONTEXT_ALIAS_CONFLICT')
        self.counts = {}
        for alias in self.reverse:
            if not SHORT_ID.fullmatch(alias):
                raise ValueError('MODEL_CONTEXT_INVALID_ALIAS')
            self.counts[alias[0]] = max(self.counts.get(alias[0], 0), int(alias[1:]))

    def encode(self, identity, *, prefix=None):
        match = LONG_ID.fullmatch(identity)
        if prefix is None:
            if not match:
                return identity
            prefix = PREFIXES[match[1]]
        if identity not in self.forward:
            self.counts[prefix] = self.counts.get(prefix, 0) + 1
            alias = prefix + str(self.counts[prefix])
            self.forward[identity] = alias
            self.reverse[alias] = identity
        return self.forward[identity]

    def decode(self, alias):
        if SHORT_ID.fullmatch(alias):
            if alias not in self.reverse:
                raise ValueError('MODEL_CONTEXT_UNKNOWN_ID：未提供过的短ID ' + alias)
            return self.reverse[alias]
        return alias


class RequestSourceCatalog:
    def __init__(self, canonical=None):
        # None is for standalone serializers with already trusted input. Runtime
        # callers always pass the session ledger, including an empty ledger.
        self.canonical = canonical
        self.entries = {}

    def include(self, evidence_id, source=None):
        if self.canonical is not None:
            canonical = self.canonical.get(evidence_id)
            if canonical is None:
                raise ValueError('MODEL_CONTEXT_UNREGISTERED_SOURCE: ' + evidence_id)
            if source is not None:
                check_source_identity(canonical, source)
            source = canonical
        if not isinstance(source, dict) or not isinstance(source.get('quote'), str):
            raise ValueError('MODEL_CONTEXT_MISSING_SOURCE: ' + evidence_id)
        if source.get('evidence_id') != evidence_id:
            raise ValueError('MODEL_CONTEXT_SOURCE_CONFLICT: identity')
        previous = self.entries.get(evidence_id)
        if previous is not None:
            check_source_identity(previous, source)
            # An incomplete copy cannot erase previously known identity fields.
            self.entries[evidence_id] = {**deepcopy(source), **previous}
        else:
            self.entries[evidence_id] = deepcopy(source)
        return evidence_id

    def definitions(self):
        return list(self.entries.values())

    def assert_resolved(self, referenced_ids):
        missing = set(referenced_ids) - self.entries.keys()
        if missing:
            raise ValueError('MODEL_CONTEXT_MISSING_SOURCE: ' + ','.join(sorted(missing)))


def render_source_line(source, aliases, *, with_region=False):
    label = aliases.encode(source['evidence_id'], prefix='e')
    if with_region:
        label += ' ' + aliases.encode(source.get('region_id', 'unknown'))
    return '[' + label + '] ' + json.dumps(source['quote'], ensure_ascii=False)
