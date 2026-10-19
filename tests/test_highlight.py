from cad_agent.contracts import SourceRegion, SourcePart
from cad_agent.evidence import bind_evidence


def test_irregular_highlight_binds_only_selected_source_parts():
    shape = dict(boundary=[[0,0],[6,0],[6,2],[2,2],[2,6],[0,6]], holes=[])
    parts = [SourcePart(entity_id='a', text_span=[0,1], entity_text_span=[0,1], bbox=[0,0,6,6], highlight_regions=[shape]),
             SourcePart(entity_id='b', text_span=[1,2], entity_text_span=[0,1], bbox=[10,0,12,2])]
    source = SourceRegion(region_id='r', layout_id='l', frame_id='f', frame_name='F', kind='text', text='甲乙',
                          entity_ids=['a','b'], bbox=[0,0,12,6], parts=parts, highlight_regions=[shape])
    full = bind_evidence('s', source, 0, 2, 'mm')
    assert full.highlight_regions == source.highlight_regions
    first = bind_evidence('s', source, 0, 1, 'mm')
    assert first.highlight_regions == parts[0].highlight_regions
    last = bind_evidence('s', source, 1, 2, 'mm')
    assert last.highlight_regions == []
    assert last.component_boxes == [[10,0,12,2]]
    assert last.entity_ids == ['b'] and last.quote == '乙'
    assert SourceRegion.model_validate(source.model_dump()).highlight_regions == source.highlight_regions


def test_reject_invalid_highlight_topology_and_nonfinite_coordinates():
    import pytest
    from pydantic import ValidationError
    from cad_agent.contracts import HighlightRegion
    for data in [dict(boundary=[[0,0],[float('inf'),0],[0,1]]),
                 dict(boundary=[[0,0],[2,2],[0,2],[2,0]]),
                 dict(boundary=[[0,0],[4,0],[4,4],[0,4]], holes=[[[5,5],[6,5],[6,6],[5,6]]]),
                 dict(boundary=[[0,0],[4,0],[4,4],[0,4]], holes=[[[1,1],[2,2]]])]:
        with pytest.raises(ValidationError):
            HighlightRegion(**data)


def test_legacy_sources_do_not_gain_empty_outline_payloads():
    part = SourcePart(entity_id='a', text_span=[0,1], entity_text_span=[0,1], bbox=[0,0,1,1])
    assert 'highlight_regions' not in part.model_dump()
