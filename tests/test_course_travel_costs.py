"""Verify required trip costs through real graph ordering and public serialization.

Fixed baseline fixtures exercise arithmetic independently of operational rate
tables. All LLM, Maps and quote providers remain offline at their boundaries.
"""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.agent.course_graph import build_course_graph
from app.services.course_localization import progress_message
from app.services.myrealtrip_quotes import (
    MyRealTripQuoteProvider,
    QuoteItem,
    TripQuotes,
)
from tests.test_course_api_contract import assert_complete_contract
from tests.test_course_contract_v2 import add_second_day, use_overseas_destination
from tests.test_course_graph import graph_dependencies as _graph_dependencies
from tests.test_course_graph import request_data as _request_data

graph_dependencies = _graph_dependencies
request_data = _request_data


def estimate(amount):
    """Represent an explicit offline reference amount rather than a supplier price."""
    return QuoteItem('estimated', amount, source='offline-test-baseline', reference_date='2026-10-19')


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,flight,lodging', [
    ('unavailable', None, None),
    ('exception', None, None),
    ('flight_only', 200000, None),
    ('lodging_only', None, 80000),
])
async def test_required_costs_precede_draft_and_survive_provider_failure(
    request_data, graph_dependencies, mocker, failure, flight, lodging,
):
    """Resolved and fallback amounts shape the first draft and are counted once."""
    provider, history, llm, draft, places = graph_dependencies
    use_overseas_destination(request_data, provider)
    add_second_day(request_data, draft, places)
    request_data.tripCondition.budget = 600000
    provider.route.return_value.cost = 1200
    baseline = TripQuotes(estimate(300000), estimate(120000))
    baseline_lookup = mocker.patch('app.agent.course_nodes.baseline_trip_quotes', return_value=baseline)
    quote_completed = False

    async def quote(request, *, deadline):
        nonlocal quote_completed
        assert deadline > asyncio.get_running_loop().time()
        await asyncio.sleep(.01)
        quote_completed = True
        if failure == 'exception':
            raise RuntimeError('offline supplier failure')
        return TripQuotes(
            QuoteItem('available', flight) if flight else QuoteItem('unavailable', reason='no_options'),
            QuoteItem('available', lodging) if lodging else QuoteItem('unavailable', reason='no_options'),
        )

    quotes = mocker.Mock(spec=MyRealTripQuoteProvider)
    quotes.quote = AsyncMock(side_effect=quote)
    expected_trip_cost = (flight or 300000) + (lodging or 120000)
    expected_allowance = (600000 - expected_trip_cost) / 2

    async def propose(*args, **kwargs):
        assert quote_completed, 'The first draft must await travel cost resolution'
        feedback = args[2]
        assert f'일별 장소·식사 예산 상한 목표(KRW): {expected_allowance:.0f}' in feedback
        assert f'왕복 이동·전체 숙박 비용 합계(KRW): {expected_trip_cost}' in feedback
        return draft

    llm.side_effect = propose
    final = None
    progress = []
    stages = []
    graph = build_course_graph(provider, history, quotes=quotes)
    async for mode, update in graph.astream(
        {'request': request_data, 'attempt': 0}, stream_mode=['custom', 'updates'],
    ):
        if mode == 'custom':
            progress.append(update)
        else:
            stages.extend(update)
            if 'finalize_course' in update:
                final = update['finalize_course']['course']

    assert final is not None
    assert stages.index('prepare') < stages.index('quote_trip') < stages.index('draft')
    assert final.totalCost == expected_trip_cost + 70000 + 8 * 1200
    assert all(len(day.stops) >= 5 for day in final.itinerary.days)
    payload = {'course': final.model_dump(context={'public_response': True})}
    assert_complete_contract(payload)
    estimated_items = tuple(name for name, amount in (('transport', flight), ('lodging', lodging)) if amount is None)
    assert any(item['message'] == progress_message('ko', 'quote_estimated', items=estimated_items)
               for item in progress)
    baseline_lookup.assert_called_once()
    quotes.quote.assert_awaited_once()
    llm.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('city,verified_city,transport_mode,flight_required,origin', [
    ('서울', 'Seoul', 'none', False, None),
    ('부산', 'Busan', 'ground', False, None),
    ('제주', 'Jeju', 'air', True, 'GMP'),
])
async def test_domestic_overnight_trip_selects_transport_and_includes_lodging(
    request_data, graph_dependencies, mocker, city, verified_city, transport_mode, flight_required, origin,
):
    """Domestic travel selects the appropriate mode while preserving hotel quotes."""
    provider, history, llm, draft, places = graph_dependencies
    add_second_day(request_data, draft, places)
    request_data.tripCondition.destinationCity = city
    provider.resolve_destination.return_value = replace(
        provider.resolve_destination.return_value,
        verified_country_name='대한민국', verified_city_name=verified_city,
    )
    quotes = mocker.Mock(spec=MyRealTripQuoteProvider)
    # A nonair trip must ignore this unrelated supplier airfare.
    quotes.quote = AsyncMock(return_value=TripQuotes(
        QuoteItem('available', 123456), QuoteItem('available', 95000),
    ))
    result = await build_course_graph(provider, history, quotes=quotes).ainvoke(
        {'request': request_data, 'attempt': 0},
    )
    resolved = result['trip_quotes']
    assert resolved.transport_mode == transport_mode
    assert resolved.lodging == QuoteItem('available', 95000)
    if transport_mode == 'none':
        assert resolved.flight.status == 'not_applicable'
        assert resolved.flight.amount_krw is None
        assert resolved.total_krw == 95000
    elif transport_mode == 'ground':
        assert resolved.flight.status == 'estimated' and resolved.flight.amount_krw > 0
    else:
        assert resolved.flight == QuoteItem('available', 123456)
    assert result['course'].totalCost == 70000 + resolved.total_krw
    quoted = quotes.quote.await_args.args[0]
    assert quoted.flight_required is flight_required
    assert quoted.origin_airport == origin
    llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_one_day_overseas_trip_ignores_inapplicable_supplier_hotel(
    request_data, graph_dependencies, mocker,
):
    """Same-date return trips have airfare but no lodging or double counting."""
    provider, history, llm, _, _ = graph_dependencies
    use_overseas_destination(request_data, provider)
    quotes = mocker.Mock(spec=MyRealTripQuoteProvider)
    quotes.quote = AsyncMock(return_value=TripQuotes(
        QuoteItem('available', 220000), QuoteItem('available', 95000),
    ))
    result = await build_course_graph(provider, history, quotes=quotes).ainvoke(
        {'request': request_data, 'attempt': 0},
    )
    assert result['trip_quotes'].lodging.status == 'not_applicable'
    assert result['course'].totalCost == 255000
    assert len(result['course'].itinerary.days[0].stops) == 5
    llm.assert_awaited_once()
