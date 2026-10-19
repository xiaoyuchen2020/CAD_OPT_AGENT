// Page sizes can change with evidence size; retain the offsets actually visited.
export function componentPageNavigation(data, offset, history = []) {
  const candidate = data.next_offset === undefined
    ? offset + (data.items?.length || 0) : data.next_offset;
  return {
    previous: offset > 0 ? {offset: history.at(-1) ?? 0, history: history.slice(0, -1)} : null,
    next: data.has_more && Number.isInteger(candidate) && candidate > offset
      ? {offset: candidate, history: [...history, offset]} : null,
  };
}
