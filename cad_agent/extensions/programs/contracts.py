"""Host-owned result contracts; generated programs cannot install validators."""
from typing import Literal
from pydantic import Field, model_validator
from cad_agent.contracts import Record

TOOLS={'inspect_tool_implementation','revise_tool','execute_tool_version','develop_program',
       'execute_program','read_program_result','register_program','list_programs'}
RESULT_TOOLS=TOOLS-{'inspect_tool_implementation','register_program','list_programs'}
VERSION='task-programs@1'


class Node(Record):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,30}$')
    op: Literal['source','select','field','normalize','number','count','sum','compare','join','sort','distinct']
    inputs: list[str] = Field(default_factory=list,max_length=2)
    source: int = Field(default=0,ge=0,le=3)
    keywords: list[str] = Field(default_factory=list,max_length=6)
    prefix: str = Field(default='',max_length=80)
    suffix: str = Field(default='',max_length=80)
    unit: str = Field(default='',max_length=20)


class Pipeline(Record):
    nodes: list[Node] = Field(min_length=1,max_length=12)
    output: str

    @model_validator(mode='after')
    def dag(self):
        seen=set()
        for n in self.nodes:
            count=0 if n.op=='source' else 2 if n.op in ('join','compare') else 1
            if n.id in seen or len(n.inputs)!=count or any(i not in seen for i in n.inputs):
                raise ValueError('PROGRAM_DAG_INVALID')
            if n.op=='select' and (not n.keywords or any(not k for k in n.keywords)):
                raise ValueError('PROGRAM_SELECTION_REQUIRED')
            if n.op=='field' and not(n.prefix or n.suffix):raise ValueError('PROGRAM_FIELD_DELIMITERS_REQUIRED')
            seen.add(n.id)
        if self.output not in seen:raise ValueError('PROGRAM_OUTPUT_NODE_REQUIRED')
        return self


class Package(Record):
    files: dict[str,str]
    entry: str = 'main'


class Proposal(Record):
    strategy: Literal['code','abstain']
    explanation: str = Field(max_length=2000)
    package: Package | None = None


class InspectTool(Record):
    tool: Literal['read_assembly'] = 'read_assembly'


class ReviseTool(Record):
    baseline_observation_id: str
    base_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    gap: str = Field(min_length=8,max_length=600)
    instruction: str = Field(min_length=8,max_length=2000)
    order: Literal['columns_exact_top_down'] = 'columns_exact_top_down'


class ExecuteToolVersion(Record):
    version_id: str
    baseline_observation_id: str


class DevelopProgram(Record):
    baseline_observation_ids: list[str] = Field(min_length=1,max_length=4)
    instruction: str = Field(min_length=8,max_length=2000)
    contract: Pipeline


class ExecuteProgram(Record):
    program_id: str
    baseline_observation_ids: list[str] = Field(min_length=1,max_length=4)


class ReadResult(Record):
    result_id: str
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=5,ge=1,le=10)


class RegisterProgram(Record):
    program_id: str
    name: str = Field(min_length=1,max_length=80)


class ListPrograms(Record):
    offset: int = Field(default=0,ge=0)
    limit: int = Field(default=5,ge=1,le=10)
