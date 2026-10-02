"""Compose recommendation reasons from supplied preferences and verified stops.

The composer makes no provider/model calls. It accepts already verified itinerary
facts; draft categories, draft prose, and inferred MBTI traits are never inputs.
"""

from collections import Counter
from dataclasses import dataclass

from app.agent.tools.verified_maps import meal_category_supported
from app.schemas.course import CourseRequestSchema, DayItinerarySchema, StopSchema
from app.schemas.taste_profile import TasteProfileSchema

CULTURE = frozenset({'museum', 'art_gallery', 'cultural_center', 'historical_landmark', 'historical_place', 'monument', 'heritage_museum', 'history_museum', 'buddhist_temple', 'hindu_temple', 'mosque', 'church', 'synagogue'})
NATURE = frozenset({'park', 'national_park', 'state_park', 'nature_preserve', 'botanical_garden', 'garden', 'hiking_area', 'beach', 'wildlife_park'})
VIEWING = CULTURE | frozenset({'aquarium', 'zoo', 'planetarium', 'observation_deck', 'botanical_garden'})
CAFE = frozenset({'cafe', 'coffee_shop', 'tea_house', 'bakery', 'dessert_shop', 'dessert_restaurant', 'ice_cream_shop', 'chocolate_shop', 'pastry_shop'})
SHOPPING = frozenset({'shopping_mall', 'department_store', 'shopping_center', 'clothing_store', 'book_store', 'gift_shop', 'souvenir_store', 'market'})
WELLNESS = frozenset({'spa', 'wellness_center', 'massage', 'sauna', 'yoga_studio'})
NIGHTLIFE = frozenset({'night_club', 'bar', 'pub', 'wine_bar', 'dance_club'})


@dataclass(frozen=True)
class PreferenceRule:
    """An explicit profile score and a conservative set of compatible place types."""

    section: str
    field: str
    label: str
    categories: frozenset[str]


RULES = (
    PreferenceRule('travelPurpose', 'culturalExperience', '문화 체험', CULTURE),
    PreferenceRule('activityPreference', 'viewing', '관람', VIEWING),
    PreferenceRule('foodPreference', 'cafeDessert', '카페·디저트', CAFE),
    PreferenceRule('travelPurpose', 'natureExploration', '자연 탐방', NATURE),
    PreferenceRule('travelPurpose', 'gourmet', '미식', frozenset({'restaurant'})),
    PreferenceRule('activityPreference', 'gourmetExploration', '미식 탐방', frozenset({'restaurant'})),
    PreferenceRule('travelPurpose', 'shopping', '쇼핑', SHOPPING),
    PreferenceRule('activityPreference', 'shopping', '쇼핑', SHOPPING),
    PreferenceRule('travelPurpose', 'relaxation', '휴식', NATURE | CAFE | WELLNESS),
    PreferenceRule('activityPreference', 'relaxation', '휴식', NATURE | CAFE | WELLNESS),
    PreferenceRule('travelPurpose', 'wellness', '웰니스', WELLNESS),
    PreferenceRule('activityPreference', 'nightlife', '밤문화', NIGHTLIFE),
    PreferenceRule('travelPurpose', 'sightseeing', '관광', VIEWING | NATURE | frozenset({'tourist_attraction'})),
    PreferenceRule('travelPurpose', 'selfDevelopment', '배움', CULTURE | frozenset({'library'})),
    PreferenceRule('preferredLocationType', 'beachResort', '해변 방문', frozenset({'beach'})),
    PreferenceRule('preferredLocationType', 'historicalCity', '역사 공간 방문', frozenset({'historical_landmark', 'historical_place', 'monument', 'heritage_museum', 'history_museum'})),
    PreferenceRule('preferredLocationType', 'themeParkResort', '테마파크 방문', frozenset({'amusement_park', 'theme_park', 'water_park'})),
)


def _evidence(profile: TasteProfileSchema, category: str) -> list[tuple[int, PreferenceRule]]:
    """Match only strong actual scores, never popularity or unverified venue traits."""
    matches = []
    for rule in RULES:
        score = getattr(getattr(profile, rule.section), rule.field)
        compatible = category in rule.categories
        if rule.field in {'gourmet', 'gourmetExploration'}:
            compatible = compatible or meal_category_supported(category, 'lunch')
        if score >= 4 and compatible:
            matches.append((score, rule))
    return matches


def _category_label(category: str) -> str:
    """Translate only the verified provider type, without adding venue attributes."""
    labels = {
        'museum': '박물관', 'art_gallery': '미술관', 'cultural_center': '문화 시설',
        'historical_landmark': '역사 명소', 'historical_place': '역사 장소',
        'monument': '기념물', 'heritage_museum': '문화유산 박물관', 'history_museum': '역사 박물관',
        'buddhist_temple': '사찰', 'hindu_temple': '힌두교 사원', 'mosque': '모스크',
        'church': '교회', 'synagogue': '유대교 회당', 'library': '도서관',
        'park': '공원', 'national_park': '국립공원', 'state_park': '주립공원',
        'nature_preserve': '자연보호구역', 'botanical_garden': '식물원', 'garden': '정원',
        'hiking_area': '하이킹 구역', 'beach': '해변', 'wildlife_park': '야생동물 공원',
        'aquarium': '수족관', 'zoo': '동물원', 'planetarium': '천문관',
        'observation_deck': '전망대', 'tourist_attraction': '관광지',
        'cafe': '카페', 'coffee_shop': '커피 전문점', 'noodle_shop': '국수 전문점', 'sandwich_shop': '샌드위치 전문점', 'food_court': '푸드코트', 'meal_takeaway': '포장 음식점', 'tea_house': '찻집',
        'bakery': '베이커리', 'dessert_shop': '디저트 가게', 'dessert_restaurant': '디저트 음식점',
        'ice_cream_shop': '아이스크림 가게', 'chocolate_shop': '초콜릿 가게', 'pastry_shop': '제과점',
        'shopping_mall': '쇼핑몰', 'department_store': '백화점', 'shopping_center': '쇼핑센터',
        'clothing_store': '의류 매장', 'book_store': '서점', 'gift_shop': '선물 가게',
        'souvenir_store': '기념품 가게', 'market': '시장',
        'spa': '스파', 'wellness_center': '웰니스 시설', 'massage': '마사지 시설',
        'sauna': '사우나', 'yoga_studio': '요가 시설', 'night_club': '나이트클럽',
        'bar': '바', 'pub': '펍', 'wine_bar': '와인 바', 'dance_club': '댄스클럽',
        'amusement_park': '놀이공원', 'theme_park': '테마파크', 'water_park': '워터파크',
    }
    if category == 'restaurant' or category.endswith('_restaurant'):
        return labels.get(category, '음식점')
    return labels.get(category, '방문 장소')


def _supported_reason(stop: StopSchema, label: str, variant: int) -> str:
    """Show the connection between the actual interest, verified type, and visit."""
    name, arrival, stay = stop.place.placeName, stop.arrivalTime, stop.stayMinutes
    venue_type = _category_label(stop.place.category)
    experiences = {
        '문화 체험': '문화 공간을 둘러보는 여행',
        '관람': '관람을 즐기는 여행',
        '카페·디저트': '카페와 디저트를 즐기는 여행',
        '자연 탐방': '자연을 둘러보는 여행',
        '미식': '먹는 즐거움을 더하는 여행',
        '미식 탐방': '음식점을 찾아가는 여행',
        '쇼핑': '쇼핑을 즐기는 여행',
        '휴식': '쉬어 가는 여행',
        '웰니스': '웰니스 시간을 갖는 여행',
        '밤문화': '밤문화를 즐기는 여행',
        '관광': '관광지를 둘러보는 여행',
        '배움': '배움을 더하는 여행',
        '해변 방문': '해변을 둘러보는 여행',
        '역사 공간 방문': '역사 공간을 둘러보는 여행',
        '테마파크 방문': '테마파크를 즐기는 여행',
    }
    experience = experiences[label]
    templates = (
        f'{experience}에 어울리도록 {venue_type}인 {name} 방문을 추천했어요. {arrival}에 방문해 {stay}분 머무는 시간을 마련했어요.',
        f'{experience} 중에 {venue_type}인 {name}에 들를 수 있도록 일정에 넣었어요. {arrival}부터 {stay}분 머물도록 구성했어요.',
        f'이번 일정에는 {venue_type}인 {name} 방문을 더했어요. {experience}에 맞춰 {arrival}부터 {stay}분 머무는 시간을 잡았어요.',
    )
    return templates[variant % len(templates)]


def apply_personalized_reasons(request: CourseRequestSchema, days: list[DayItinerarySchema]) -> str:
    """Replace final stop reasons and return a summary based on selected stops only.

    Args:
        request: Actual supplied taste profile and explicit trip conditions.
        days: Final verified places, arrival/stay times and confirmed route estimates.
    Returns:
        Course recommendation reason, with supported preferences and final venues.
        Each stop's reason is updated in place. No external calls are performed.

    Scores below four, incompatible or unknown categories, and unsupported claims
    use schedule facts. Cuisine origin, atmosphere, photographic merit, dietary
    safety, accessibility, popularity and price value are not inferred from a type.
    """
    used: Counter[str] = Counter()
    examples: dict[str, str] = {}
    stops = [stop for day in days for stop in day.stops]
    profile = request.tasteProfile
    for day in days:
        pace_used = False
        for stop in day.stops:
            evidence = _evidence(profile, stop.place.category) if profile else []
            if evidence:
                # Preserve stronger evidence; vary compatible equally strong interests.
                evidence.sort(key=lambda item: (-item[0], used[item[1].label]))
                _, rule = evidence[0]
                stop.reason = _supported_reason(stop, rule.label, used[rule.label])
                used[rule.label] += 1
                examples.setdefault(rule.label, stop.place.placeName)
            elif profile and not pace_used and profile.travelPaceDensity in {'slow_stay', 'long_stay'} and stop.stayMinutes >= 90:
                stop.reason = f'한 곳에 천천히 머무는 여행이 되도록 {stop.place.placeName}에 {stop.arrivalTime}부터 {stop.stayMinutes}분의 체류 시간을 마련했어요.'
                used['천천히 머무는 일정'] += 1
                examples.setdefault('천천히 머무는 일정', stop.place.placeName)
                pace_used = True
            elif profile and not pace_used and profile.travelPaceDensity == 'dense_schedule' and len(day.stops) >= 6:
                stop.reason = f'하루에 여러 곳을 둘러볼 수 있도록 {len(day.stops)}곳을 계획했고, 그중 한 곳으로 {stop.place.placeName} 방문을 넣었어요. {stop.arrivalTime}부터 {stop.stayMinutes}분 방문할 계획이에요.'
                used['촘촘한 일정'] += 1
                examples.setdefault('촘촘한 일정', stop.place.placeName)
                pace_used = True
            else:
                stop.reason = f'{request.tripCondition.destinationCity} 여행 일정에 맞춰 {stop.arrivalTime}에 {stop.place.placeName}에 방문하고 {stop.stayMinutes}분 머물도록 배치했어요.'
                transport = stop.transportToNext
                if transport.type != 'none' and transport.minutes is not None and transport.minutes > 0:
                    stop.reason += f' 다음 장소까지 확인된 약 {transport.minutes}분의 이동 시간도 일정에 반영했어요.'
    if used:
        connections = ', '.join(f'{label}({examples[label]} 방문)' for label, _ in used.most_common(3))
        summary = f'이번 코스에 {connections} 일정을 담았어요. {request.tripCondition.destinationCity}의 {len(days)}일 일정에 최종 {len(stops)}곳을 담았어요.'
    else:
        summary = f'{request.tripCondition.destinationCity}의 {len(days)}일 여행 조건에 맞춰 최종 {len(stops)}곳을 방문하도록 구성했어요.'
    budget = {'cost_effective': '가성비', 'moderate': '보통', 'luxury': '고급'}[request.tripCondition.budgetType]
    return f'{summary} 최종 방문 시간과 장소 간 이동 시간을 반영했어요. 비용은 요청하신 {budget} 예산 유형을 참고한 추정치예요.'
