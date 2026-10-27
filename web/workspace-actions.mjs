// Exact workspace requests only. Semantic drawing questions still go to the Agent.
export function workspaceAction(question) {
  const text = String(question).trim().replace(/[。！!？?]$/, '');
  const actions = {
    '下载图纸': 'download', '下载原图': 'download', '下载原文件': 'download',
    '下载 DWG': 'download', '下载 DXF': 'download', '下载图纸 JSON': 'download',
    '测量房间': 'measure', '在图上选点测量房间': 'measure',
    '测量选中房间': 'measure-evidence',
    '框选找相似': 'similarity', '框选样本找相似': 'similarity',
    '识别当前视野的房间': 'discover',
    '局部方法调试': 'adapt', '查看测量记录': 'measurements',
    '查看原文与事实': 'library', '浏览图纸结构': 'structure', '查看解析诊断': 'diagnostics',
  };
  return Object.prototype.hasOwnProperty.call(actions, text) ? actions[text] : null;
}

export function drawingDownloads(drawing) {
  const base = `/api/drawings/${encodeURIComponent(drawing.id)}`;
  const links = [{label: `原始 ${drawing.source_format || 'DXF'}`, href: `${base}/source`}];
  if (drawing.conversion) links.push(
    {label: '转换 DXF', href: `${base}/dxf`},
    {label: '图纸 JSON', href: `${base}/oda-json`},
  );
  return links;
}
