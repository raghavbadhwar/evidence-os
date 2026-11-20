"""Matter-scoped source declarations supplied by the records owner."""

import json
import os
from pathlib import Path

from pydantic import Field

from .contracts import Contract, Role
from .store import Problem


class Source(Contract):
    path: Path
    sha256: str = Field(pattern="^[a-f0-9]{64}$")
    role: Role = "other"
    required: bool = Field(default=True, strict=True)


def library(mid):
    config = os.getenv("EVIDENCE_LIBRARY_CONFIG")
    if not config:
        return {}
    try:
        catalog = json.loads(Path(config).read_text())[mid]
    except KeyError:
        return {}
    except (OSError, ValueError, TypeError) as exc:
        raise Problem(503, "Ask the records owner to check the source list.") from exc
    try:
        return {name: Source.model_validate(spec) for name, spec in catalog.items()}
    except (ValueError, AttributeError) as exc:
        raise Problem(503, "The approved source list needs correction.") from exc


def packet_requirements(matter):
    # Each authorized row belongs to one database transaction and sees one declaration.
    if "_required_files" not in matter:
        matter["_required_files"] = {
            name: [spec.sha256, spec.role] for name, spec in library(matter["id"]).items() if spec.required
        }
    return matter["_required_files"]


def required_file_gaps(matter, documents):
    required = packet_requirements(matter)
    if not required:
        return {}
    current = {doc["name"]: doc for doc in documents}
    gaps = {}
    for name, (sha, role) in required.items():
        doc = current.get(name)
        if doc is None:
            gaps[name] = "not fetched"
        elif doc["sha"] != sha:
            gaps[name] = "different version"
        elif doc["role"] != role:
            gaps[name] = "different document role"
        elif doc["status"] != "ready":
            gaps[name] = doc["status"]
    return gaps
