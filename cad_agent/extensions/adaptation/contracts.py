from typing import Literal
from pydantic import Field, model_validator
from cad_agent.contracts import Record, FiniteFloat

Family = Literal['table_grid', 'text_spans', 'segment_measurements', 'segment_distances', 'closed_contour']


class Start(Record):
    table_id: str | None = Field(default=None, min_length=1, max_length=100)
    layout_id: str | None = None
    bbox: list[FiniteFloat] | None = Field(default=None, min_length=4, max_length=4)
    method: Family = 'table_grid'
    instruction: str = Field(default='恢复该区域的真实表格网格', max_length=2000)
    model_profile: str = Field(default='default', max_length=50)
    idempotency_key: str = Field(min_length=1, max_length=100)
    mode: Literal['auto', 'fixed', 'dynamic', 'replay', 'registered'] = 'auto'
    method_version_id: str | None = None
    reuse_run_id: str | None = None
    revision_of_run_id: str | None = None

    @model_validator(mode='after')
    def scope(self):
        if self.revision_of_run_id and self.mode != 'dynamic':
            raise ValueError('REVISION_REQUIRES_DYNAMIC_MODE')
        if self.table_id:
            if self.layout_id is not None or self.bbox is not None:
                raise ValueError('AMBIGUOUS_INPUT_SCOPE')
        elif self.method == 'table_grid' or self.layout_id is None or self.bbox is None:
            raise ValueError('TABLE_ID_OR_EXPLICIT_REGION_REQUIRED')
        return self


class Proposal(Record):
    strategy: Literal['code', 'abstain']
    explanation: str = Field(max_length=2000)
    code: str = Field(default='', max_length=24000)


class Review(Record):
    candidate_sha256: str
    decision: Literal['accepted', 'rejected']
    scope: Literal['table_identity_and_grid', 'source_spans', 'geometric_measurement', 'closed_geometry']
    note: str = Field(min_length=1, max_length=2000)


class Promote(Record):
    run_ids: list[str] = Field(min_length=2, max_length=20)
    name: str = Field(min_length=1, max_length=80)


SPECS = {
    'table_grid': dict(version='table-grid-adapter@1', validator='table-recovery-validator@2',
                      review_scope='table_identity_and_grid', output='Candidate(input_id,status,xs,ys,cells); use sdk.recover(bundle) after changing ONLY a local copy of xs/ys.'),
    'text_spans': dict(version='source-spans-adapter@1', validator='exact-source-spans@1',
                      review_scope='source_spans', output='{"input_id": str, "items": [{"entity_id": str, "start": int, "end": int}]} where source text[start:end] is the desired exact quote.'),
    'segment_measurements': dict(version='native-length-adapter@1', validator='native-length@1',
                      review_scope='geometric_measurement', output='{"input_id": str, "items": [{"segment_id": str, "length": float}]} in drawing units, no assumed mm conversion.'),
    'segment_distances': dict(version='native-distance-adapter@1', validator='native-distance@1',
                      review_scope='geometric_measurement', output='{"input_id": str, "items": [{"segment_ids": [str, str], "distance": float}]} selecting distinct native straight segments. Return minimum Euclidean distance between the WHOLE finite segments, including zero for intersections. Drawing units only; no assumed mm conversion or wall/door identity claim.'),
    'closed_contour': dict(version='native-contour-adapter@1', validator='closed-native-contour@1',
                      review_scope='closed_geometry', output='{"input_id": str, "segment_ids": [str,...], "area": float} selecting a single simple closed native polygon in drawing units squared. Compute area with UNROUNDED native endpoints; translate to a local origin before shoelace to avoid cancellation. Rounding to integers/tolerance changes geometry and is rejected. No room-identity claim.'),
}
