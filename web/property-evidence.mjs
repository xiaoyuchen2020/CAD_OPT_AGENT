const basisLabels = {
  source_nominal: '表列／规定值', annotation: '图纸标注',
  measurement: '几何测量', calculation: '计算结果',
};
const comparisonLabels = {
  conflict: '同口径值冲突', different_bases: '口径不同，分别保留',
  unit_unresolved: '单位关系未确认', consistent: '同口径值一致',
  uncompared: '尚未比较',
};
export function propertyRows(packet) {
  return (packet.items || []).map(item => ({
    id: item.field_id,
    subject: item.subject_label || item.subject_id,
    property: item.property_label || item.property_name,
    value: item.value === null || item.value === undefined ? '未确认' : (
      typeof item.value === 'object' ? JSON.stringify(item.value) : String(item.value)
    ),
    unit: item.unit ? `${item.unit}${item.unit_state === 'conditional' ? '（条件单位）' : ''}` : '单位未确认',
    basis: [basisLabels[item.basis] || item.basis, item.measurement_basis].filter(Boolean).join(' · '),
    state: [
      item.binding_state === 'source_bound' ? '来源已绑定' : '归属待核',
      comparisonLabels[item.comparison_state] || '尚未比较',
    ].join(' · '),
    evidenceIds: (item.source_refs || []).filter(ref => ref.kind === 'evidence').map(ref => ref.ref_id),
    observationIds: (item.source_refs || []).filter(ref => ref.kind === 'observation').map(ref => ref.ref_id),
    diagnostics: item.diagnostics || [],
  }));
}
