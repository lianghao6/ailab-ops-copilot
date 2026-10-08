"""Load observable case assets; evaluation labels have a separate entry point.

The default is the source checkout's data/v2 directory (including editable
installs). Deployment outside a checkout must supply an explicit data root.
No legacy playbook, inference rules, or evaluation labels enter CaseWorld.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import re


@dataclass
class CaseWorld:
    case_id: str
    title: str
    jobs: list[dict]
    logs: list[dict]
    metrics: list[dict]
    nodes: list[dict]
    incidents: list[dict]
    actions: list[dict]
    telemetry: dict

    def get_job(self, job_id: str) -> dict | None:
        return next((job for job in self.jobs if job["job_id"] == job_id), None)

    def logs_for(self, job_id: str) -> list[dict]:
        return [log for log in self.logs if log["job_id"] == job_id]

    def metrics_for(self, job_id: str) -> list[dict]:
        return [metric for metric in self.metrics if metric["job_id"] == job_id]


def _data_root(root: Path | None = None) -> Path:
    return Path(root) if root is not None else Path(__file__).resolve().parents[3] / "data" / "v2"


def list_cases() -> list[str]:
    """Return the source checkout's case catalog in stable order."""
    return sorted(path.stem for path in (_data_root() / "cases").glob("case-*.json"))


def load_case(case_id: str, root: Path | None = None) -> CaseWorld:
    """Load a fresh observable world; root, if supplied, is authoritative."""
    if not re.fullmatch(r"case-[a-z0-9]+(?:-[a-z0-9]+)*", case_id):
        raise ValueError(f"Invalid case ID: {case_id!r}")
    path = _data_root(root) / "cases" / f"{case_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"Case {case_id!r} not found at {path}")
    return CaseWorld(**json.loads(path.read_text(encoding="utf-8")))


def load_eval_labels(path: Path) -> dict[str, dict]:
    """Evaluation-only opt-in reader. Never called by load_case or list_cases."""
    labels = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        case_id = row["case_id"]
        if case_id in labels:
            raise ValueError(f"Duplicate evaluation case ID: {case_id}")
        labels[case_id] = row
    return labels
