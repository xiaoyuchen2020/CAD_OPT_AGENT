// The server manifest is authoritative; investigation material is not a citation.
export function publishedCitations(result) {
  const citations = result.citations || [];
  const manifest = result.delivery_manifest;
  if (!manifest) return citations.map((source, index) => ({source, number: index + 1, legacy: true}));
  const selected = new Set(manifest.selected_evidence_ids || []);
  return citations.flatMap((source, index) => selected.has(source.evidence_id)
    ? [{source, number: index + 1, legacy: false}] : []);
}
