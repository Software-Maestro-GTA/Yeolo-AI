"""Compose recommendation reasons from supplied preferences and verified stops.

The composer makes no provider/model calls. It accepts already verified itinerary
facts; draft categories, draft prose, and inferred MBTI traits are never inputs.
"""

from collections import Counter
from dataclasses import dataclass

from app.agent.tools.verified_maps import meal_category_supported
from app.schemas.course import CourseRequestSchema, DayItinerarySchema, StopSchema
from app.schemas.taste_profile import TasteProfileSchema

CULTURE = frozenset({'museum', 'art_gallery', 'cultural_center', 'historical_landmark', 'historical_place', 'monument', 'heritage_museum', 'history_museum', 'buddhist_temple', 'shinto_shrine', 'hindu_temple', 'mosque', 'church', 'synagogue'})
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
        'shinto_shrine': '신사', 'buddhist_temple': '사찰', 'hindu_temple': '힌두교 사원', 'mosque': '모스크',
        'church': '교회', 'synagogue': '유대교 회당', 'library': '도서관',
        'park': '공원', 'national_park': '국립공원', 'state_park': '주립공원',
        'nature_preserve': '자연보호구역', 'botanical_garden': '식물원', 'garden': '정원',
        'hiking_area': '하이킹 구역', 'beach': '해변', 'wildlife_park': '야생동물 공원',
        'aquarium': '수족관', 'zoo': '동물원', 'planetarium': '천문관',
        'observation_deck': '전망대', 'tourist_attraction': '관광지', 'convention_center': '컨벤션 센터',
        'ramen_restaurant': '라멘 음식점', 'japanese_restaurant': '일식 음식점',
        'hamburger_restaurant': '햄버거 음식점', 'tonkatsu_restaurant': '돈카츠 음식점', 'sushi_restaurant': '초밥 음식점',
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


def _experience(category: str) -> tuple[str, str]:
    """Return an experience label and action supported by a verified place type."""
    if category in {'museum', 'heritage_museum', 'history_museum'}:
        return '전시·문화 관람', '전시와 자료를 살펴보는'
    if category == 'art_gallery':
        return '전시·문화 관람', '작품을 둘러보는'
    if category == 'shinto_shrine':
        return '신사 방문', '신사 공간을 둘러보는'
    if category == 'buddhist_temple':
        return '사찰 방문', '사찰 공간을 둘러보는'
    if category in CULTURE:
        return '문화 공간 방문', '문화 공간을 직접 살펴보는'
    if category in NATURE:
        return '자연 산책', '산책하며 주변을 둘러보는'
    if category == 'observation_deck':
        return '전망 감상', '전망을 바라보는'
    if category in VIEWING:
        return '관람', f'{_category_label(category)}을 둘러보는'
    if category in CAFE:
        return '카페·디저트', '음료나 디저트를 골라 즐기는'
    dishes = {'ramen_restaurant': '라멘', 'japanese_restaurant': '일식', 'hamburger_restaurant': '햄버거', 'tonkatsu_restaurant': '돈카츠', 'sushi_restaurant': '초밥'}
    if category in dishes:
        return f'{dishes[category]} 식사', f'{dishes[category]} 식사를 즐기는'
    if meal_category_supported(category, 'lunch'):
        return '식사', '음식점에서 먹고 싶은 음식을 골라 즐기는'
    if category in SHOPPING:
        return '쇼핑', '마음에 드는 상품을 직접 살펴보는'
    if category in WELLNESS:
        return '웰니스 공간 방문', '웰니스 공간을 방문하는'
    if category in NIGHTLIFE:
        return '밤문화 공간 방문', '밤문화 공간을 둘러보는'
    if category in {'amusement_park', 'theme_park', 'water_park'}:
        return '테마파크 방문', '테마파크를 둘러보는'
    if category == 'convention_center':
        return '현장 공간 방문', '현장 방문 안내를 확인하고 공간을 둘러보는'
    if category == 'tourist_attraction':
        return '관광 공간 방문', '관광 공간을 직접 둘러보는'
    if category == 'library':
        return '책 살펴보기', '책을 직접 살펴보는'
    return '장소 방문', '장소를 직접 둘러보는'


def visit_tip(category: str) -> str:
    """Suggest a short visit action without inventing facilities or provider facts.

    Args:
        category: Verified provider business/place type.
    Returns:
        Plaintext advice; it does not establish crowding, amenities or availability.
    """
    if category in {'museum', 'heritage_museum', 'history_museum'}:
        return '관람 안내를 보고 궁금한 주제부터 골라 보세요. 전시의 설명과 자료를 함께 살펴보고, 눈에 들어오는 내용에 집중해 보세요.'
    if category == 'art_gallery':
        return '관람 안내를 보고 살펴보고 싶은 작품부터 골라 보세요. 눈에 들어오는 색과 형태를 먼저 보고, 작품 설명이 있다면 함께 읽어 보세요.'
    if category in {'shinto_shrine', 'buddhist_temple'}:
        venue_type = _category_label(category)
        return f'현장 방문 예절과 안내를 먼저 확인해 주세요. 이동 가능한 범위를 살펴본 뒤, {venue_type} 공간의 모습과 주변을 둘러보세요.'
    if category in {'hindu_temple', 'mosque', 'church', 'synagogue'}:
        return '현장 방문 예절과 안내를 먼저 확인해 주세요. 다른 방문자의 활동을 존중하며 공간을 둘러보세요.'
    if category in NATURE:
        return '현장 안내를 보고 걸어 보고 싶은 방향을 골라 보세요. 산책 중 눈에 들어오는 풍경을 살펴보고, 돌아올 동선도 함께 생각해 보세요.'
    if category == 'observation_deck':
        return '현장 안내를 확인하고 바라보고 싶은 방향부터 골라 보세요. 가까이 보이는 부분과 먼 풍경을 번갈아 살펴보며 전망을 감상해 보세요.'
    meal_tips = {
        'ramen_restaurant': '메뉴에서 면·국물 구성과 양을 먼저 살펴보세요. 추가 메뉴가 있다면 한 끼로 먹고 싶은 조합인지 확인한 뒤 주문해 보세요.',
        'sushi_restaurant': '초밥 메뉴의 재료와 구성을 살펴보세요. 먹고 싶은 종류를 고른 뒤, 제공되는 양도 확인해 주문해 보세요.',
        'tonkatsu_restaurant': '돈카츠 메뉴의 구성과 양을 먼저 살펴보세요. 메뉴에 곁들이는 음식이 있다면 함께 먹고 싶은 조합인지 확인해 보세요.',
        'hamburger_restaurant': '햄버거 메뉴의 속재료와 구성을 살펴보세요. 추가 음식이나 음료가 있다면 원하는 조합과 양을 확인해 주문해 보세요.',
        'japanese_restaurant': '일식 메뉴의 재료와 구성을 살펴보세요. 한 끼로 먹고 싶은 조합을 고른 뒤, 제공되는 양을 확인해 주문해 보세요.',
    }
    if category in meal_tips:
        return meal_tips[category]
    if category in CAFE or meal_category_supported(category, 'lunch') or category.endswith('_restaurant'):
        return '메뉴의 재료와 양을 먼저 살펴보세요. 먹고 싶은 조합을 고른 뒤, 주문할 구성을 확인해 보세요.'
    if category in SHOPPING:
        return '관심 있는 상품부터 골라 살펴보세요. 구매하려는 상품의 구성을 비교하고, 필요한 물건인지 생각해 보세요.'
    if category == 'convention_center':
        return '현장 방문 안내에서 현재 방문할 수 있는 범위를 확인해 주세요. 둘러보고 싶은 부분을 고르고, 이동 중 현장 안내를 따라 살펴보세요.'
    if category in WELLNESS or category in NIGHTLIFE or category in {'amusement_park', 'theme_park', 'water_park'}:
        return '현장 이용 안내를 먼저 확인해 주세요. 해 보고 싶은 활동을 고르고, 필요한 이용 조건을 살펴보세요.'
    return '방문 안내를 먼저 확인해 주세요. 눈에 들어오는 부분을 고르고, 둘러볼 수 있는 범위에서 살펴보세요.'


def _experience_value(category: str) -> str:
    """Explain a type-supported visit purpose without inventing venue amenities."""
    if category in {'museum', 'heritage_museum', 'history_museum', 'art_gallery'}:
        return '전시의 구성과 눈에 들어오는 내용을 연결해 보며 관람의 초점을 찾을 수 있어요.'
    if category in {'shinto_shrine', 'buddhist_temple'}:
        return '공간의 구성과 방문 예절을 직접 접하며 문화가 드러나는 모습을 살펴볼 수 있어요.'
    if category in NATURE:
        return '걷는 방향에 따라 달라지는 시야와 주변 풍경을 살펴보는 데 초점을 두었어요.'
    if category == 'observation_deck':
        return '가까운 곳과 먼 곳의 모습을 비교하며 주변 풍경을 넓게 살펴볼 수 있어요.'
    if category == 'ramen_restaurant':
        return '면과 국물의 조합을 맛보며 라멘 식사의 즐거움을 느끼는 데 초점을 두었어요.'
    if category == 'sushi_restaurant':
        return '초밥의 재료와 한 끼 구성을 살펴보며 먹고 싶은 조합을 찾아볼 수 있어요.'
    if category == 'tonkatsu_restaurant':
        return '돈카츠 메뉴의 구성과 제공되는 양을 살펴보며 원하는 한 끼를 고르는 데 초점을 두었어요.'
    if category == 'hamburger_restaurant':
        return '햄버거의 속재료와 조합을 살펴보며 먹고 싶은 구성을 찾아볼 수 있어요.'
    if category == 'japanese_restaurant':
        return '일식 메뉴에 담긴 재료와 구성을 비교하며 먹고 싶은 맛의 조합을 찾아볼 수 있어요.'
    if meal_category_supported(category, 'lunch') or category in CAFE:
        return '메뉴의 재료와 양을 비교하며 먹고 싶은 조합을 찾는 데 초점을 두었어요.'
    if category == 'convention_center':
        return '현장 안내와 공간의 구성을 연결해 보며 둘러보고 싶은 부분을 찾아볼 수 있어요.'
    if category in SHOPPING:
        return '상품의 구성을 비교하며 자신에게 필요한 물건인지 생각해 볼 수 있어요.'
    return '방문 안내와 현장의 모습을 참고해 자신이 보고 싶은 부분에 초점을 맞출 수 있어요.'


def _travel_experience(label: str) -> str:
    """Describe strong survey evidence as a travel experience, not a direct choice."""
    experiences = {
        '문화 체험': '문화 공간을 둘러보는 여행', '관람': '관람을 즐기는 여행',
        '카페·디저트': '카페와 디저트를 즐기는 여행', '자연 탐방': '자연을 둘러보는 여행',
        '미식': '먹는 즐거움을 더하는 여행', '미식 탐방': '음식점을 찾아가는 여행',
        '쇼핑': '쇼핑을 즐기는 여행', '휴식': '쉬어 가는 여행',
        '웰니스': '웰니스 시간을 갖는 여행', '밤문화': '밤문화를 즐기는 여행',
        '관광': '관광 공간을 둘러보는 여행', '배움': '배움을 더하는 여행',
        '해변 방문': '해변을 둘러보는 여행', '역사 공간 방문': '역사 공간을 둘러보는 여행',
        '테마파크 방문': '테마파크를 즐기는 여행',
    }
    return experiences[label]


def _supported_reason(stop: StopSchema, label: str, variant: int) -> str:
    """Connect an actual strong preference to a supported venue experience."""
    name = stop.place.placeName
    _, action = _experience(stop.place.category)
    experience = _travel_experience(label)
    templates = (
        f'{experience}에 어울리도록 {name}에서 {action} 경험을 더했어요.',
        f'{name}에서 {action} 경험을 {experience}과 연결해 추천했어요.',
        f'{experience}에 맞춰 {name}에서 {action} 방문을 담았어요.',
    )
    return templates[variant % len(templates)] + ' ' + _experience_value(stop.place.category)


def apply_personalized_reasons(request: CourseRequestSchema, days: list[DayItinerarySchema]) -> str:
    """Explain verified experiences and supplied strong preferences without schedule.

    Args:
        request: Original supplied preferences; MBTI traits are never inferred.
        days: Final verified places; each stop reason is updated in place.
    Returns:
        A summary of selected experiences and actual supported preferences.
        No external calls, itinerary values or price estimates are changed.
    """
    used: Counter[str] = Counter()
    experiences: Counter[str] = Counter()
    examples: dict[str, str] = {}
    example_categories: dict[str, str] = {}
    profile = request.tasteProfile
    for day in days:
        for stop in day.stops:
            label, action = _experience(stop.place.category)
            experiences[label] += 1
            examples.setdefault(label, stop.place.placeName)
            example_categories.setdefault(label, stop.place.category)
            evidence = _evidence(profile, stop.place.category) if profile else []
            if evidence:
                evidence.sort(key=lambda item: (-item[0], used[item[1].label]))
                _, rule = evidence[0]
                stop.reason = _supported_reason(stop, rule.label, used[rule.label])
                used[rule.label] += 1
            else:
                stop.reason = f'{stop.place.placeName}에서 {action} 경험을 여행에 더할 수 있도록 추천했어요. {_experience_value(stop.place.category)}'
    if not experiences:
        return '직접 장소를 둘러보는 경험을 담은 여행이에요. 눈에 들어오는 부분을 스스로 살펴보는 방문을 더했어요.'
    connections = ', '.join(f'{label}({examples[label]})' for label, _ in experiences.most_common(3))
    summary = f'이번 여행에는 {connections} 경험을 함께 담았어요.'
    if used:
        travel_experiences = ', '.join(_travel_experience(label) for label, _ in used.most_common(3))
        summary += f' 취향에 맞춰 {travel_experiences}이 이어지도록 구성했어요.'
    else:
        main_experience = experiences.most_common(1)[0][0]
        summary += ' ' + _experience_value(example_categories[main_experience])
    return summary
