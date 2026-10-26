"""Read ACAD_TABLE metadata without exploding or mutating the source entity."""
from ezdxf.entities.acad_table import read_acad_table_content


def metadata(entity,entity_id,layout_id,path):
    result=dict(entity_id=entity_id,layout_id=layout_id,entity_path=path,rows=[],diagnostics=[])
    try:
        tags=entity.xtags.get_subclass('AcDbTable')
        result.update(row_count=int(tags.get_first_value(91,0)),column_count=int(tags.get_first_value(92,0)),
                      row_heights=[float(t.value) for t in tags if t.code==141],
                      column_widths=[float(t.value) for t in tags if t.code==142],
                      rows=read_acad_table_content(entity),
                      # Retain native merge flags; differing DXF versions encode
                      # merge ranges differently, so geometry remains necessary.
                      merge_flags=[t.value for t in tags if t.code==173])
    except (ValueError,IndexError,AttributeError,TypeError):
        result['diagnostics'].append('NATIVE_TABLE_METADATA_UNRESOLVED')
    return result
