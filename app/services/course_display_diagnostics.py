"""Record internal display-stage outcomes without source text or provider messages.

Graph state and logs retain failures even when optional display work recovers.
These reports never become fields of the public course response.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, TypedDict

logger = logging.getLogger(__name__)


class DisplayFailure(TypedDict):
    """Describe a failed phase using only identifiers, counts and error types."""

    phase: str
    reason: str
    count: int
    day: int | None
    batch: int | None
    attempt: int | None
    error_type: str | None
    status_code: int | None


class DisplayStageResult(TypedDict):
    """Serializable internal outcome of one optional graph stage."""

    status: str
    total: int
    completed: int
    missing: int
    retries: int
    requests: dict[str, int]
    failures: list[DisplayFailure]
    elapsed_seconds: float


@dataclass
class DisplayDiagnostics:
    """Collect one stage's safe diagnostics without changing its error policy.

    Args:
        stage: Internal graph-stage label, either translation or place_copy.
        total: Number of translation units or visit pairs requiring processing.
    """

    stage: str
    total: int = 0
    completed: int = 0
    retries: int = 0
    status: str = 'pending'
    requests: dict[str, int] = field(default_factory=dict)
    failures: list[DisplayFailure] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    _started: float = field(default_factory=time.monotonic, repr=False)

    def fail(
        self, phase: str, reason: str, *, count: int = 1,
        day: int | None = None, batch: int | None = None,
        attempt: int | None = None, error: BaseException | None = None,
    ) -> None:
        """Record a failure; raw exceptions, prompts and credentials are excluded."""
        status_code = None
        current = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            code = getattr(current, 'status_code', None)
            if isinstance(code, int):
                status_code = code
                break
            current = current.__cause__ or current.__context__
        failure: DisplayFailure = {
            'phase': phase, 'reason': reason, 'count': count,
            'day': day, 'batch': batch, 'attempt': attempt,
            'error_type': type(error).__name__ if error is not None else None,
            'status_code': status_code,
        }
        self.failures.append(failure)
        logger.warning('Course display failure: stage=%s details=%s', self.stage, failure)

    def call(self, phase: str) -> None:
        """Count a named chain invocation attempt."""
        self.requests[phase] = self.requests.get(phase, 0) + 1

    def finish(self, completed: int, *, cancelled: bool = False) -> None:
        """Finalize and log counts; recovered failures remain in the report."""
        self.completed = completed
        if cancelled:
            self.status = 'cancelled'
        elif self.total == 0:
            self.status = 'skipped'
        elif completed == self.total:
            self.status = 'success'
        else:
            self.status = 'partial' if completed else 'failed'
        self.elapsed_seconds = round(time.monotonic() - self._started, 3)
        logger.info('Course display result: stage=%s result=%s', self.stage, self.snapshot())

    def snapshot(self) -> DisplayStageResult:
        """Return JSON-compatible diagnostics for internal LangGraph state."""
        return {
            'status': self.status, 'total': self.total, 'completed': self.completed,
            'missing': self.total - self.completed, 'retries': self.retries,
            'requests': dict(self.requests), 'failures': [dict(item) for item in self.failures],
            'elapsed_seconds': self.elapsed_seconds,
        }

    def config(self, phase: str, **metadata: Any) -> dict[str, Any]:
        """Name each chain and attach safe batch context to its trace."""
        return {
            'run_name': f'course_{phase}',
            'metadata': {'display_stage': self.stage, 'display_phase': phase, **metadata},
        }
