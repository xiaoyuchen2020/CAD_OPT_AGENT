# Rule source libraries

`RuleLibraryStore(root)` owns a separate SQLite database, `rule_libraries.sqlite3`.
It seeds the packaged `architecture-cn` workbook and does not modify the source
workbook, the fleet database, or existing CAD task state. No model is invoked.

The built-in source is the user-specified `20260320 优化规则库-建筑.xlsx`. Its
SHA-256 is `655696b873ff2003c0a7eb5dae27611273508257d47e0623b04fe3ee368d6bdd`.
The workbook contains 38 parent records, of which three are marked cancelled,
and 46 embedded images. These are source inventory counts, not validated rules.

## Public contract

- `list_libraries(project_id=None)` returns source summaries. With no project,
  only built-in sources are listed.
- `import_xlsx(filename, content: bytes, project_id)` creates a project-scoped,
  immutable source version. Reimporting the same filename and bytes in the same
  project returns the existing version. A changed file creates a new version.
- `get_library(library_id, project_id=None)` returns metadata, complete parsed
  workbook cells, image/merge relationships, diagnostics, and `rules`.
- `get_rule(library_id, rule_id, project_id=None)` returns a complete rule source
  record. Its `qualified_ref` is `<library_id>:<rule_id>`.
- `get_source(library_id, project_id=None)` returns the original XLSX bytes.
- `get_image(library_id, image_id, project_id=None)` returns `(bytes, mime_type)`.
  Images are not OCRed or interpreted; row-anchor overlap is only an association
  hint. Unsupported drawing content remains available in the original XLSX.
- `create_effective_set(project_id, selections, overrides=None)` freezes the
  selected rules, source versions, and explicit changes. `selections` contains
  `{library_id, rule_ids?: [...]}`. Omitting `rule_ids` includes every rule from
  that source, recording source-disabled rules as disabled.
- Overrides are `{op: "add", rule_ref}`, `{op: "disable", target_ref}`, or
  `{op: "replace", target_ref, replacement_ref, reason?}`. Use complete qualified
  refs. Same-ID conflicts require explicit resolution. Source-disabled rules
  cannot be activated by a selection or override.
- `get_effective_set(effective_set_id, project_id)` returns an immutable
  snapshot. `list_effective_sets(project_id)` returns summaries without rules.

All project-scoped reads require the matching project ID. The API must also
authorize the current user for that project; a project ID is not authentication.
`RuleLibraryError` exposes `code`, `diagnostics`, and a human-readable message.

## Interpretation boundaries

Every imported library has `status: source_only`; every rule and effective set
has `readiness: needs_interpretation`. Ingestion does not establish that a rule
is current, unambiguous, executable, applicable, or validated. Annotations that
look like revisions remain verbatim sources requiring interpretation. No date
or column is silently given precedence over another.

Rule extraction requires an explicit rule ID header. The built-in layout uses
`要点编码` for parent records and `规则编码` for steps. Simple imported workbooks
can use `规则编号`, `规则编码`, `rule_id`, or `rule_code`. Continuation rows and
unnumbered additions are preserved. Unrecognized sheets and unassigned rows
produce diagnostics; imports with no identifiable rules fail instead of
returning successful empty libraries. Duplicate IDs within a workbook fail
with explicit source diagnostics.

The parser reads ZIP/XML directly and does not depend on malformed or
application-specific Excel styles. Formula source and cached values are
preserved without recalculation. Original bytes remain authoritative for
formatting, rich-text runs, and unsupported embedded objects. Size, ZIP entry,
expanded-size, sheet, cell, text, and merge limits bound ingestion. External
relationships are never fetched. XML entity declarations and unsafe ZIP paths
are rejected.
