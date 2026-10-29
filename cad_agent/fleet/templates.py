"""Versioned factual questions; no engineering advice or compliance policy."""
TEMPLATES = [
    {"id": "design_notes", "title": "设计说明查阅", "version": "1", "question": "读取图纸中的设计说明，按条款整理原文事实，保留适用范围、条件、例外和来源位置；标明未读完或不清楚的内容。"},
    {"id": "material_layers", "title": "材料做法提取", "version": "1", "question": "提取图纸中的材料和构造做法，保留编号、完整层次、厚度、单位、备注和来源；缺失项明确标注，不补造条件。"},
    {"id": "tables", "title": "表格提取", "version": "1", "question": "读取图纸中的表格，保留表头、单位、行列对应关系、备注与来源。逐项说明缺失、冲突或未完整读取的部分。"},
    {"id": "numeric_facts", "title": "数值核算", "version": "1", "question": "核对图纸中明确列出的数值及其计算关系，给出输入来源、公式、单位和结果；不推定未知单位或工程对象身份。"},
    {"id": "cross_drawing", "title": "跨图纸事实比较", "version": "1", "question": "比较本任务所提供图纸中对应内容，列出相同、不同、缺失及不确定的事实，并分别给出来源；不作优化或合规结论。"},
]
