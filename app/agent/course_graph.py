"""Connect course generation nodes and stream the existing SSE payloads.

Node execution, state, LLM proposals, planning and recovery decisions are defined
in their own modules. Start at build_course_graph to inspect the workflow."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing, nullcontext

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
from app.services.myrealtrip_quotes import MyRealTripQuoteProvider

logger = logging.getLogger(__name__)


def build_course_graph(
    provider: VerifiedMapsProvider,
    history: CourseHistory,
    quotes: MyRealTripQuoteProvider | None = None,
) -> CompiledStateGraph:
    """Compile the normal generation flow and its existing recovery branches.

    Normal flow:
        prepare → quote_trip → draft → resolve_anchors → verify_places → verify_routes → schedule
        → finalize → attach_lodging → attach_airports → translate_display
        → generate_place_copy → finalize_course. Travel costs are resolved
        before daily proposals receive their remaining activity budget. Deferred
        venues are queried before further model calls. Failed proposals can
        refill missing days, repair verified candidates or restore a snapshot.

    Args:
        provider: Injectable place and route provider.
        history: Injectable recent course history storage.
        quotes: Optional injectable flight and accommodation quote boundary.

    Returns:
        Compiled graph with bounded draft and deferred-query recovery paths.

    Raises:
        CourseGenerationError: During execution, no feasible verified course
            remains after the existing bounded recovery attempts.
    """
    nodes = CourseGraphNodes(provider, history, quotes)
    graph = StateGraph(CourseState)

    # Only proposals and verification use the existing fallback time budget.
    graph.add_node("prepare", nodes.prepare)
    graph.add_node("draft", nodes.bounded(nodes.draft))
    graph.add_node("resolve_anchors", nodes.resolve_anchors)
    graph.add_node("verify_places", nodes.bounded(nodes.verify_places))
    graph.add_node("verify_routes", nodes.bounded(nodes.verify_routes))
    graph.add_node("schedule", nodes.schedule)
    graph.add_node("finalize", nodes.finalize)
    graph.add_node("attach_lodging", nodes.attach_lodging)
    graph.add_node("attach_airports", nodes.attach_airports)
    graph.add_node("translate_display", nodes.translate_display)
    graph.add_node("generate_place_copy", nodes.generate_place_copy)
    graph.add_node("finalize_course", nodes.finalize_course)
    graph.add_node("quote_trip", nodes.quote_trip)
    graph.add_node("expand_places", nodes.bounded(nodes.expand_places))
    graph.add_node("refill", nodes.bounded(nodes.refill))
    graph.add_node("repair", nodes.repair)
    graph.add_node("restore", nodes.restore)

    # Entry and recovery paths have unconditional successors.
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "quote_trip")
    graph.add_edge("quote_trip", "draft")
    graph.add_edge("resolve_anchors", "verify_places")
    graph.add_edge("expand_places", "verify_places")
    graph.add_edge("repair", "verify_routes")
    graph.add_edge("restore", "schedule")
    graph.add_edge("attach_lodging", "attach_airports")
    graph.add_edge("attach_airports", "translate_display")
    graph.add_edge("translate_display", "generate_place_copy")
    graph.add_edge("generate_place_copy", "finalize_course")
    graph.add_edge("finalize_course", END)

    # Every main stage shares the same bounded retry and snapshot decision.
    for source, successor in (
        ("draft", "resolve_anchors"),
        ("refill", "resolve_anchors"),
        ("verify_places", "verify_routes"),
        ("verify_routes", "schedule"),
        ("schedule", "finalize"),
        ("finalize", "attach_lodging"),
    ):
        graph.add_conditional_edges(
            source,
            route_after(successor),
            [successor, "draft", "refill", "repair", "restore", "expand_places"],
        )

    return graph.compile().with_config({"recursion_limit": 100})


async def stream_course_generation(
    request: CourseRequestSchema, *, deadline: float | None = None
) -> AsyncGenerator[tuple[str, dict]]:
    """Stream existing progress/complete payloads while the graph actually executes.

    A request-wide timeout and generator closure cancel active graph work.
    Completion is published only after the graph and provider close successfully.
    """
    completed: dict | None = None
    provider = VerifiedMapsProvider(
        concurrency=settings.COURSE_MAPS_CONCURRENCY, language=request.language
    )
    try:
        quotes = MyRealTripQuoteProvider(
            partner_api_key=settings.MYREALTRIP_PARTNER_API_KEY,
            origin_airport=settings.MYREALTRIP_ORIGIN_AIRPORT,
            adults=settings.MYREALTRIP_ADULTS,
            timeout_seconds=settings.MYREALTRIP_QUOTE_TIMEOUT_SECONDS,
            mcp_url=settings.MYREALTRIP_MCP_URL,
            partner_base_url=settings.MYREALTRIP_PARTNER_BASE_URL,
        )
    except ValueError:
        logger.warning("Optional travel quote configuration unavailable")
        quotes = None
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
            async with provider, quotes if quotes is not None else nullcontext():
                graph = build_course_graph(provider, CourseHistory(), quotes)
                async with aclosing(
                    graph.astream(
                        {"request": request, "attempt": 0, "deadline": timeout.when()},
                        stream_mode=["custom", "updates"],
                    )
                ) as events:
                    async for mode, value in events:
                        if mode == "custom":
                            yield "progress", value
                        elif "finalize_course" in value and value[
                            "finalize_course"
                        ].get("course"):
                            completed = {
                                "course": value["finalize_course"][
                                    "course"
                                ].model_dump(context={"public_response": True})
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
