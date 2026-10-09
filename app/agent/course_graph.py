"""Connect course generation nodes and stream the existing SSE payloads.

Node execution, state, LLM proposals, planning and recovery decisions are defined
in their own modules. Start at build_course_graph to inspect the workflow."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.course_nodes import CourseGraphNodes
from app.agent.course_state import CourseState
from app.agent.course_transitions import route_after
from app.agent.tools.verified_maps import (
    VerifiedMapsProvider,
)
from app.core.config import settings
from app.schemas.course import (
    CourseRequestSchema,
)
from app.services.course_history import CourseHistory
from app.services.maps_cost import MapsCostMetrics

logger = logging.getLogger(__name__)


def build_course_graph(
    provider: VerifiedMapsProvider, history: CourseHistory
) -> CompiledStateGraph:
    """Compile the normal generation flow and its existing recovery branches.

    Normal flow:
        prepare → draft → verify_places → verify_routes → schedule → finalize
        → enrich_images. Failed or insufficient proposals may draft again,
        refill missing days, repair verified candidates or restore a snapshot.

    Args:
        provider: Injectable place, route and photo provider.
        history: Injectable recent course history storage.

    Returns:
        Compiled graph with the existing node names and recursion limit.

    Raises:
        CourseGenerationError: During execution, no feasible verified course
            remains after the existing bounded recovery attempts.
    """
    nodes = CourseGraphNodes(provider, history)
    graph = StateGraph(CourseState)

    # Only proposals and verification use the existing fallback time budget.
    graph.add_node("prepare", nodes.prepare)
    graph.add_node("draft", nodes.bounded(nodes.draft))
    graph.add_node("verify_places", nodes.bounded(nodes.verify_places))
    graph.add_node("verify_routes", nodes.bounded(nodes.verify_routes))
    graph.add_node("schedule", nodes.schedule)
    graph.add_node("finalize", nodes.finalize)
    graph.add_node("enrich_images", nodes.enrich_images)
    graph.add_node("refill", nodes.bounded(nodes.refill))
    graph.add_node("repair", nodes.repair)
    graph.add_node("restore", nodes.restore)

    # Entry and recovery paths have unconditional successors.
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "draft")
    graph.add_edge("repair", "verify_routes")
    graph.add_edge("restore", "schedule")
    graph.add_edge("enrich_images", END)

    # Every main stage shares the same bounded retry and snapshot decision.
    for source, successor in (
        ("draft", "verify_places"),
        ("refill", "verify_places"),
        ("verify_places", "verify_routes"),
        ("verify_routes", "schedule"),
        ("schedule", "finalize"),
        ("finalize", "enrich_images"),
    ):
        graph.add_conditional_edges(
            source,
            route_after(successor),
            [successor, "draft", "refill", "repair", "restore"],
        )

    return graph.compile().with_config({"recursion_limit": 50})


async def stream_course_generation(
    request: CourseRequestSchema, *, deadline: float | None = None
) -> AsyncGenerator[tuple[str, dict]]:
    """Stream existing progress/complete payloads while the graph actually executes.

    A request-wide timeout and generator closure cancel active graph work.
    Completion is published only after the graph and provider close successfully.
    """
    completed: dict | None = None
    provider = VerifiedMapsProvider(concurrency=settings.COURSE_MAPS_CONCURRENCY)
    now = asyncio.get_running_loop().time()
    request_deadline = min(
        deadline if deadline is not None else float("inf"),
        now + settings.COURSE_TIMEOUT_SECONDS,
    )
    remaining = max(0.0, request_deadline - now)
    # Leave up to one second for cancelled provider/graph work to close cleanly.
    processing_deadline = request_deadline - min(1.0, remaining * 0.1)
    try:
        async with asyncio.timeout_at(processing_deadline) as timeout:
            async with provider:
                graph = build_course_graph(provider, CourseHistory())
                async with aclosing(
                    graph.astream(
                        {"request": request, "attempt": 0, "deadline": timeout.when()},
                        stream_mode=["custom", "updates"],
                    )
                ) as events:
                    async for mode, value in events:
                        if mode == "custom":
                            yield "progress", value
                        elif "enrich_images" in value and value["enrich_images"].get(
                            "course"
                        ):
                            completed = {
                                "course": value["enrich_images"]["course"].model_dump()
                            }
    finally:
        metrics = getattr(provider, "metrics", None)
        if isinstance(metrics, MapsCostMetrics):
            try:
                logger.info("Maps request cost summary: %s", metrics.snapshot())
            except Exception:  # noqa: BLE001 - diagnostic counters cannot discard a completed course
                logger.warning("Maps request cost summary unavailable")
    if completed is not None:
        yield "complete", completed
