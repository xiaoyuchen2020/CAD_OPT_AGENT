"""Read-only task review endpoints; never create a model session."""
from fastapi import APIRouter, Query

from cad_agent.analysis.tasks import TaskLedger
from cad_agent.analysis.similarity_search import SimilarObjectSearch


def router(workspace):
    api = APIRouter()

    @api.post("/api/drawings/{snapshot_id}/similarity-search")
    def similarity_search(snapshot_id: str, request: SimilarObjectSearch):
        from cad_agent.analysis.similarity_search import search_similar_objects
        return search_similar_objects(workspace, snapshot_id, request)

    @api.get("/api/tasks/{task_id}/property-evidence")
    def property_evidence(
        task_id: str, subject_id: str = "", property_name: str = "",
        offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100),
    ):
        from cad_agent.analysis.property_evidence import query_task_property_evidence
        data = TaskLedger.read(workspace, task_id)
        allowed = {data["snapshot_id"], *(
            item["snapshot_id"] for item in data.get("related_drawings", [])
        )}
        return query_task_property_evidence(
            data, allowed_snapshots=allowed, subject_id=subject_id,
            property_name=property_name, offset=offset, limit=limit,
        )

    @api.get("/api/tasks/{task_id}/gap-recovery")
    def gap_recovery(task_id: str):
        from cad_agent.analysis.gap_routing import context
        return context(TaskLedger.read(workspace, task_id))

    @api.get("/api/tasks/{task_id}/door-schedule-relations")
    def door_schedule_relations(
        task_id: str, object_id: str = "",
        offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
    ):
        from cad_agent.analysis.door_schedule import DoorScheduleRead, read_task_door_schedules
        return read_task_door_schedules(
            TaskLedger.read(workspace, task_id),
            DoorScheduleRead(object_id=object_id, offset=offset, limit=limit),
        )

    return api
