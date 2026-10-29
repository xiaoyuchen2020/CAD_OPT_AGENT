"""E0: durable, source-bound rule exploration; no automatic rule acceptance."""

from .contracts import ExplorationConflict, ExplorationError
from .question import build_job_payload, build_question
from .records import summarize_archive
from .store import ExplorationStore

__all__ = [
    "ExplorationStore", "ExplorationError", "ExplorationConflict",
    "build_question", "build_job_payload", "summarize_archive",
]
