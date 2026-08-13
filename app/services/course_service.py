import json
import logging
from collections.abc import AsyncGenerator

from fastapi import HTTPException

from app.agent.course_chain import run_course_generation_chain
from app.agent.tools.google_maps import (
    compute_route_between_places,
    search_place_detail,
)
from app.schemas.course import CourseRequestSchema, CourseSchema, TransportToNextSchema

logger = logging.getLogger(__name__)


async def enrich_course_with_google_maps(course: CourseSchema) -> CourseSchema:
    """Google Maps Place (New) API와 Routes API를 활용하여
    코스 내 스톱 중 장소 정보나 이동 정보가 누락되거나 미흡한 스톱에 대해서만 선택적으로 보강(Lazy Enrichment)합니다.
    LLM Agent가 Tool Calling으로 이미 구글 맵스 조회를 완료한 스톱은 API 이중 호출을 Skip합니다.
    """
    destination_city = course.destinationCity
    for day in course.itinerary.days:
        stops = day.stops
        stops_count = len(stops)

        # 1. Place (New) API 선택적 조건부 보강
        place_details = []
        for stop in stops:
            p_name = stop.place.placeName if stop.place and stop.place.placeName else "장소"

            # 이미 유효한 Google Place ID(places/...) 및 영업시간/주소가 정밀하게 채워져 있는 경우 Skip
            is_place_complete = (
                stop.place
                and stop.place.placeId
                and not stop.place.placeId.startswith("place_")
                and len(stop.place.openingHours) > 0
                and stop.place.address != ""
            )

            if is_place_complete:
                logger.debug(f"[course_service] Skip Place API call for '{p_name}' (already enriched by LLM Tool Calling)")
                detail = stop.place
            else:
                detail = await search_place_detail(query=p_name, destination_city=destination_city)
                stop.place = detail

            place_details.append(detail)

        # 2. Routes API 선택적 조건부 보강
        for i in range(stops_count):
            if i < stops_count - 1:
                curr_place = place_details[i]
                next_place = place_details[i + 1]

                # 이미 이동 소요시간과 거리가 정밀하게 계산되어 있는 경우 Skip
                is_route_complete = (
                    stops[i].transportToNext
                    and stops[i].transportToNext.minutes is not None
                    and stops[i].transportToNext.distance is not None
                    and stops[i].transportToNext.memo is not None
                    and "약" in (stops[i].transportToNext.memo or "")
                )

                if is_route_complete:
                    logger.debug(f"[course_service] Skip Routes API call between '{curr_place.placeName}' and '{next_place.placeName}'")
                else:
                    origin = {"latitude": curr_place.latitude, "longitude": curr_place.longitude}
                    destination = {"latitude": next_place.latitude, "longitude": next_place.longitude}

                    travel_mode = stops[i].transportToNext.type if stops[i].transportToNext else "transit"
                    transport_info = await compute_route_between_places(
                        origin=origin,
                        destination=destination,
                        travel_mode=travel_mode,
                    )
                    stops[i].transportToNext = transport_info
            else:
                stops[i].transportToNext = TransportToNextSchema(
                    type="none",
                    distance=0.0,
                    minutes=0,
                    cost=0,
                    memo="오늘의 마지막 일정입니다.",
                )

    return course


async def generate_course_service(request: CourseRequestSchema) -> AsyncGenerator[str]:
    """
    성향 프로필과 여행 조건 기반 코스를 비동기로 생성한 후 SSE 스트림을 반환합니다.
    1) LLM 파이프라인 실행 중 오류나 조건 미충족 시 404 / 500 HTTPException 발생 (StreamingResponse 시작 전)
    2) 정상 생성 완료 시 event: progress, event: complete SSE 메시지 스트리밍
    """
    logger.info(f"[course_service] Invoking course_generation_chain for userId={request.userId}")
    try:
        # LLM 비동기 파이프라인 호출
        course_result: CourseSchema = await run_course_generation_chain(request)
        if course_result:
            # Google Maps API를 통해 장소 정보 및 이동 정보 보강 (Tool Calling / API-AI-2)
            course_result = await enrich_course_with_google_maps(course_result)
    except HTTPException:
        raise
    except Exception:
        logger.exception(f"[course_service] Error in course generation chain for userId={request.userId}")
        raise HTTPException(status_code=500, detail="AI 코스 생성 중 오류가 발생했습니다.")

    if not course_result:
        # 조건에 맞는 장소가 없거나 결과 생성이 안된 경우
        logger.warning(f"[course_service] No course generated for userId={request.userId}")
        raise HTTPException(status_code=404, detail="조건에 맞는 장소가 없습니다.")

    logger.info(f"[course_service] Successfully generated course '{course_result.title}' for userId={request.userId}. Starting SSE stream.")

    async def sse_generator() -> AsyncGenerator[str]:
        # 1. 진행 상태 전송 (event: progress)
        progress_data = {
            "step": "GENERATING_ROUTE",
            "message": "장소와 이동 순서를 구성 중입니다.",
        }
        logger.debug(f"[course_service] Yielding event: progress for userId={request.userId}")
        yield f"event: progress\ndata: {json.dumps(progress_data, ensure_ascii=False)}\n\n"

        # 2. 최종 완료 응답 전송 (event: complete)
        complete_data = {"course": course_result.model_dump()}
        logger.info(f"[course_service] Yielding event: complete for userId={request.userId}")
        yield f"event: complete\ndata: {json.dumps(complete_data, ensure_ascii=False)}\n\n"

    return sse_generator()


