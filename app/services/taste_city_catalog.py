"""Curated city identities and broad interests for offline summary matching.

Official tourism references support editorial tags, not availability, prices,
weather, safety or suitability on a particular date. Reviewed 2026-10-15.
Adding a city here requires no new LLM schema enum or runtime network request.
"""

from typing import NamedTuple

from app.schemas.taste_summary import CityCode, SummaryTypeCode


class SummaryCity(NamedTuple):
    """Canonical names and editorial interest tags; source stays server-side."""

    code: CityCode
    country_ko: str
    city_ko: str
    country_en: str
    city_en: str
    interests: tuple[SummaryTypeCode, ...]
    source_url: str


CITY_CANDIDATES: tuple[SummaryCity, ...] = (
    SummaryCity("SEOUL", "대한민국", "서울", "South Korea", "Seoul",
                ("CAFE_DESSERT", "CULTURE", "FOOD", "SHOPPING", "NIGHTLIFE"), "https://english.visitseoul.net/"),
    SummaryCity("BUSAN", "대한민국", "부산", "South Korea", "Busan",
                ("BEACH", "FOOD", "CAFE_DESSERT"), "https://www.visitbusan.net/en/index.do"),
    SummaryCity("JEJU", "대한민국", "제주", "South Korea", "Jeju",
                ("BEACH", "NATURE", "OUTDOOR", "EXPERIENCE"), "https://www.visitjeju.net/en"),
    SummaryCity("GYEONGJU", "대한민국", "경주", "South Korea", "Gyeongju",
                ("CULTURE", "NATURE", "EXPERIENCE"), "https://gyeongju.go.kr/tour/"),
    SummaryCity("KYOTO", "일본", "교토", "Japan", "Kyoto",
                ("CULTURE", "NATURE", "FOOD"), "https://www.japan.travel/en/destinations/kansai/kyoto/"),
    SummaryCity("TOKYO", "일본", "도쿄", "Japan", "Tokyo",
                ("SHOPPING", "FOOD", "CULTURE", "EXPERIENCE", "NIGHTLIFE"), "https://www.japan.travel/en/destinations/kanto/tokyo/"),
    SummaryCity("OSAKA", "일본", "오사카", "Japan", "Osaka",
                ("FOOD", "SHOPPING", "NIGHTLIFE", "CULTURE"), "https://www.japan.travel/en/destinations/kansai/osaka/"),
    SummaryCity("FUKUOKA", "일본", "후쿠오카", "Japan", "Fukuoka",
                ("FOOD", "SHOPPING", "CULTURE"), "https://www.japan.travel/en/destinations/kyushu/fukuoka/"),
    SummaryCity("SAPPORO", "일본", "삿포로", "Japan", "Sapporo",
                ("FOOD", "NATURE", "OUTDOOR", "NIGHTLIFE"), "https://visit.sapporo.travel/"),
    SummaryCity("BANGKOK", "태국", "방콕", "Thailand", "Bangkok",
                ("FOOD", "CULTURE", "SHOPPING", "NIGHTLIFE", "EXPERIENCE"), "https://thai.tourismthailand.org/Destinations/Provinces/bangkok/219"),
    SummaryCity("CHIANG_MAI", "태국", "치앙마이", "Thailand", "Chiang Mai",
                ("CULTURE", "NATURE", "OUTDOOR", "EXPERIENCE", "CAFE_DESSERT"), "https://www.tourismthailand.org/Destinations"),
    SummaryCity("PHUKET", "태국", "푸껫", "Thailand", "Phuket",
                ("BEACH", "NATURE", "OUTDOOR", "WELLNESS"), "https://www.tourismthailand.org/Destinations"),
    SummaryCity("HANOI", "베트남", "하노이", "Vietnam", "Hanoi",
                ("CULTURE", "FOOD", "CAFE_DESSERT"), "https://vietnam.travel/places-to-go/northern-vietnam/ha-noi"),
    SummaryCity("HOI_AN", "베트남", "호이안", "Vietnam", "Hoi An",
                ("CULTURE", "FOOD", "EXPERIENCE", "BEACH"), "https://vietnam.travel/places-to-go/central-vietnam/hoi-an"),
    SummaryCity("DA_NANG", "베트남", "다낭", "Vietnam", "Da Nang",
                ("BEACH", "NATURE", "FOOD"), "https://www.vietnam.travel/vi/things-to-do/da-nang-travel-guides"),
    SummaryCity("TAIPEI", "대만", "타이베이", "Taiwan", "Taipei",
                ("FOOD", "CULTURE", "CAFE_DESSERT", "NATURE", "WELLNESS"), "https://www.travel.taipei/en/"),
    SummaryCity("SINGAPORE", "싱가포르", "싱가포르", "Singapore", "Singapore",
                ("SHOPPING", "FOOD", "EXPERIENCE", "WELLNESS"), "https://www.visitsingapore.com/"),
    SummaryCity("LISBON", "포르투갈", "리스본", "Portugal", "Lisbon",
                ("CULTURE", "CAFE_DESSERT", "FOOD"), "https://www.visitlisboa.com/en"),
    SummaryCity("BARCELONA", "스페인", "바르셀로나", "Spain", "Barcelona",
                ("CULTURE", "BEACH", "NIGHTLIFE"), "https://www.barcelonaturisme.com/"),
    SummaryCity("PARIS", "프랑스", "파리", "France", "Paris",
                ("CULTURE", "FOOD", "CAFE_DESSERT", "SHOPPING"), "https://parisjetaime.com/eng/"),
    SummaryCity("ROME", "이탈리아", "로마", "Italy", "Rome",
                ("CULTURE", "FOOD", "EXPERIENCE"), "https://www.italia.it/en/lazio/rome"),
    SummaryCity("FLORENCE", "이탈리아", "피렌체", "Italy", "Florence",
                ("CULTURE", "FOOD", "EXPERIENCE"), "https://www.italia.it/en/tuscany/florence"),
    SummaryCity("VIENNA", "오스트리아", "빈", "Austria", "Vienna",
                ("CULTURE", "CAFE_DESSERT", "FOOD"), "https://www.wien.info/en"),
    SummaryCity("PRAGUE", "체코", "프라하", "Czechia", "Prague",
                ("CULTURE", "FOOD", "CAFE_DESSERT"), "https://prague.eu/en/"),
    SummaryCity("BUDAPEST", "헝가리", "부다페스트", "Hungary", "Budapest",
                ("CULTURE", "WELLNESS", "FOOD"), "https://visithungary.com/documents/8/8f/8f1/8f18701e7c181a3290960b8484c97f2a49c3e58.pdf"),
    SummaryCity("AMSTERDAM", "네덜란드", "암스테르담", "Netherlands", "Amsterdam",
                ("CULTURE", "CAFE_DESSERT", "NATURE"), "https://www.iamsterdam.com/en"),
    SummaryCity("NEW_YORK", "미국", "뉴욕", "United States", "New York",
                ("CULTURE", "FOOD", "SHOPPING", "NIGHTLIFE"), "https://www.nyctourism.com/"),
    SummaryCity("SAN_FRANCISCO", "미국", "샌프란시스코", "United States", "San Francisco",
                ("CULTURE", "FOOD", "NATURE", "CAFE_DESSERT"), "https://www.sftravel.com/visitors-guide"),
    SummaryCity("VANCOUVER", "캐나다", "밴쿠버", "Canada", "Vancouver",
                ("NATURE", "OUTDOOR", "BEACH"), "https://www.destinationvancouver.com/"),
    SummaryCity("SYDNEY", "호주", "시드니", "Australia", "Sydney",
                ("BEACH", "CULTURE", "NATURE", "OUTDOOR"), "https://www.australia.com/en/places/sydney-and-surrounds/guide-to-sydney.html"),
    SummaryCity("MELBOURNE", "호주", "멜버른", "Australia", "Melbourne",
                ("CAFE_DESSERT", "CULTURE", "FOOD", "SHOPPING"), "https://www.australia.com/en/places/melbourne-and-surrounds/guide-to-melbourne.html"),
    SummaryCity("QUEENSTOWN", "뉴질랜드", "퀸스타운", "New Zealand", "Queenstown",
                ("NATURE", "OUTDOOR"), "https://www.newzealand.com/int/queenstown/"),
)

# Without supported interests, preserve a broad exploratory set, not a match claim.
EXPLORATORY_CITY_CODES = (
    "SEOUL", "BUSAN", "KYOTO", "TOKYO", "LISBON", "BARCELONA", "VANCOUVER", "SINGAPORE",
)
