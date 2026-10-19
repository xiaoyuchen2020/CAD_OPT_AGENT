from pathlib import Path
import time
import ezdxf
import pytest
from cad_agent.config import Settings
from cad_agent.demo import create_demo
from cad_agent.service import Workspace
from cad_agent.contracts import SourceRegion, ExtractionRequest


@pytest.fixture
def workspace(tmp_path):
    w = Workspace(Settings(data_dir=tmp_path / "data"))
    yield w
    w.close()


@pytest.fixture
def demo_bytes(tmp_path):
    p = tmp_path / "demo.dxf"
    create_demo(p)
    return p.read_bytes()


def region(text, key="r1", **kwargs):
    return SourceRegion(
        region_id=key,
        layout_id=kwargs.pop("layout_id", "layout1"),
        frame_id=kwargs.pop("frame_id", "frame1"),
        frame_name="同名说明",
        kind="paragraph",
        text=text,
        entity_ids=[key + "-ent"],
        bbox=[0, 0, 100, 30],
        **kwargs,
    )


def finish(workspace, request):
    job = workspace.start_extract(request)
    for _ in range(100):
        job = workspace.store.get("job", job["id"])
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError("Job timed out")


def dxf_bytes(tmp_path, build):
    doc = ezdxf.new("R2018")
    doc.units = 4
    build(doc)
    p = tmp_path / "drawing.dxf"
    doc.saveas(p)
    return p.read_bytes()
