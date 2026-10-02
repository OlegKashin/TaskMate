"""Timezone utilities: city aliases, coordinate-to-timezone resolution, and picker UI."""

import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Curated reference cities for coordinate lookup and text alias matching:
# (lat, lon, iana_tz, display_name, [aliases])
CITIES_DB: list[tuple[float, float, str, str, list[str]]] = [
    # Russia UTC+2
    (54.71, 20.51, "Europe/Kaliningrad", "Калининград (UTC+2)", ["калининград", "kaliningrad"]),
    # Russia UTC+3
    (55.75, 37.61, "Europe/Moscow", "Москва, СПб (UTC+3)", ["москва", "moscow", "мск", "санкт-петербург", "питер", "петербург", "спб", "spb", "saint petersburg"]),
    (56.33, 44.00, "Europe/Moscow", "Нижний Новгород (UTC+3)", ["нижний новгород", "nizhny novgorod", "нижний"]),
    (55.79, 49.12, "Europe/Moscow", "Казань (UTC+3)", ["казань", "kazan"]),
    (47.22, 39.72, "Europe/Moscow", "Ростов-на-Дону (UTC+3)", ["ростов", "ростов-на-дону", "rostov"]),
    (45.04, 38.98, "Europe/Moscow", "Краснодар (UTC+3)", ["краснодар", "krasnodar"]),
    (43.59, 39.73, "Europe/Moscow", "Сочи (UTC+3)", ["сочи", "sochi"]),
    (51.67, 39.18, "Europe/Moscow", "Воронеж (UTC+3)", ["воронеж", "voronezh"]),
    (57.63, 39.87, "Europe/Moscow", "Ярославль (UTC+3)", ["ярославль", "yaroslavl"]),
    (68.97, 33.08, "Europe/Moscow", "Мурманск (UTC+3)", ["мурманск", "murmansk"]),
    # Russia UTC+4
    (53.20, 50.15, "Europe/Samara", "Самара (UTC+4)", ["самара", "samara", "тольятти", "tolyatti"]),
    (51.53, 46.03, "Europe/Saratov", "Саратов (UTC+4)", ["саратов", "saratov"]),
    (48.71, 44.51, "Europe/Volgograd", "Волгоград (UTC+4)", ["волгоград", "volgograd"]),
    (54.32, 48.40, "Europe/Ulyanovsk", "Ульяновск (UTC+4)", ["ульяновск", "ulyanovsk"]),
    (46.35, 48.04, "Europe/Astrakhan", "Астрахань (UTC+4)", ["астрахань", "astrakhan"]),
    (56.85, 53.20, "Europe/Samara", "Ижевск (UTC+4)", ["ижевск", "izhevsk", "удмуртия"]),
    # Russia UTC+5
    (56.84, 60.61, "Asia/Yekaterinburg", "Екатеринбург (UTC+5)", ["екатеринбург", "екб", "yekaterinburg", "ekaterinburg", "свердловск"]),
    (55.16, 61.43, "Asia/Yekaterinburg", "Челябинск (UTC+5)", ["челябинск", "chelyabinsk"]),
    (54.74, 55.97, "Asia/Yekaterinburg", "Уфа (UTC+5)", ["уфа", "ufa", "башкирия", "башкортостан"]),
    (58.01, 56.25, "Asia/Yekaterinburg", "Пермь (UTC+5)", ["пермь", "perm"]),
    (57.15, 65.54, "Asia/Yekaterinburg", "Тюмень (UTC+5)", ["тюмень", "tyumen"]),
    (51.77, 55.10, "Asia/Yekaterinburg", "Оренбург (UTC+5)", ["оренбург", "orenburg"]),
    (61.25, 73.41, "Asia/Yekaterinburg", "Сургут (UTC+5)", ["сургут", "surgut", "хмао", "нижневартовск"]),
    # Russia UTC+6
    (54.99, 73.37, "Asia/Omsk", "Омск (UTC+6)", ["омск", "omsk"]),
    # Russia UTC+7
    (55.01, 82.93, "Asia/Novosibirsk", "Новосибирск (UTC+7)", ["новосибирск", "нск", "novosibirsk"]),
    (53.36, 83.76, "Asia/Barnaul", "Барнаул (UTC+7)", ["барнаул", "barnaul", "алтай"]),
    (56.50, 84.97, "Asia/Tomsk", "Томск (UTC+7)", ["томск", "tomsk"]),
    (56.01, 92.85, "Asia/Krasnoyarsk", "Красноярск (UTC+7)", ["красноярск", "крск", "krasnoyarsk"]),
    (55.35, 86.08, "Asia/Krasnoyarsk", "Кемерово (UTC+7)", ["кемерово", "kemerovo", "новокузнецк", "кузбасс"]),
    (53.72, 91.43, "Asia/Krasnoyarsk", "Абакан (UTC+7)", ["абакан", "abakan", "хакасия"]),
    (51.72, 94.44, "Asia/Krasnoyarsk", "Кызыл (UTC+7)", ["кызыл", "kyzyl", "тыва"]),
    # Russia UTC+8
    (52.29, 104.30, "Asia/Irkutsk", "Иркутск (UTC+8)", ["иркутск", "irkutsk", "байкал"]),
    (51.83, 107.61, "Asia/Irkutsk", "Улан-Удэ (UTC+8)", ["улан-удэ", "ulan-ude", "бурятия"]),
    # Russia UTC+9
    (52.03, 113.50, "Asia/Chita", "Чита (UTC+9)", ["чита", "chita", "забайкалье"]),
    (62.03, 129.73, "Asia/Yakutsk", "Якутск (UTC+9)", ["якутск", "yakutsk", "якутия"]),
    (50.27, 127.54, "Asia/Yakutsk", "Благовещенск (UTC+9)", ["благовещенск", "blagoveshchensk", "амур"]),
    # Russia UTC+10
    (43.12, 131.89, "Asia/Vladivostok", "Владивосток (UTC+10)", ["владивосток", "влад", "vladivostok", "приморье"]),
    (48.48, 135.07, "Asia/Vladivostok", "Хабаровск (UTC+10)", ["хабаровск", "khabarovsk"]),
    # Russia UTC+11
    (59.57, 150.81, "Asia/Magadan", "Магадан (UTC+11)", ["магадан", "magadan"]),
    (46.96, 142.74, "Asia/Sakhalin", "Южно-Сахалинск (UTC+11)", ["южно-сахалинск", "сахалин", "sakhalin"]),
    # Russia UTC+12
    (53.02, 158.65, "Asia/Kamchatka", "Камчатка (UTC+12)", ["петропавловск-камчатский", "камчатка", "kamchatka"]),
    (64.73, 177.51, "Asia/Anadyr", "Анадырь (UTC+12)", ["анадырь", "anadyr", "чукотка"]),

    # Neighboring & World Countries
    (53.90, 27.56, "Europe/Minsk", "Минск (UTC+3)", ["минск", "minsk", "беларусь", "belarus"]),
    (50.45, 30.52, "Europe/Kyiv", "Киев (UTC+2)", ["киев", "kyiv", "kiev", "украина", "ukraine"]),
    (43.24, 76.91, "Asia/Almaty", "Алматы (UTC+5)", ["алматы", "almaty", "алма-ата", "астана", "astana", "казахстан", "kazakhstan", "шымкент"]),
    (41.31, 69.28, "Asia/Tashkent", "Ташкент (UTC+5)", ["ташкент", "tashkent", "узбекистан", "uzbekistan", "самарканд"]),
    (42.87, 74.59, "Asia/Bishkek", "Бишкек (UTC+6)", ["бишкек", "bishkek", "кыргызстан", "kyrgyzstan"]),
    (38.56, 68.79, "Asia/Dushanbe", "Душанбе (UTC+5)", ["душанбе", "dushanbe", "таджикистан"]),
    (37.95, 58.38, "Asia/Ashgabat", "Ашхабад (UTC+5)", ["ашхабад", "ashgabat", "туркменистан"]),
    (41.72, 44.78, "Asia/Tbilisi", "Тбилиси (UTC+4)", ["тбилиси", "tbilisi", "грузия", "georgia", "батуми"]),
    (40.18, 44.51, "Asia/Yerevan", "Ереван (UTC+4)", ["ереван", "yerevan", "армения", "armenia"]),
    (40.41, 49.87, "Asia/Baku", "Баку (UTC+4)", ["баку", "baku", "азербайджан", "azerbaijan"]),
    (41.01, 28.98, "Europe/Istanbul", "Стамбул (UTC+3)", ["стамбул", "istanbul", "турция", "turkey", "анталья"]),
    (25.20, 55.27, "Asia/Dubai", "Дубай (UTC+4)", ["дубай", "dubai", "оаэ", "uae", "абу-даби"]),
    (32.08, 34.78, "Asia/Jerusalem", "Тель-Авив (UTC+2)", ["тель-авив", "tel aviv", "израиль", "israel", "иерусалим"]),
    (51.51, -0.13, "Europe/London", "Лондон (UTC+0)", ["лондон", "london", "uk", "великобритания"]),
    (52.52, 13.40, "Europe/Berlin", "Берлин (UTC+1)", ["берлин", "berlin", "германия", "germany", "мюнхен", "франкфурт"]),
    (48.86, 2.35, "Europe/Paris", "Париж (UTC+1)", ["париж", "paris", "франция", "france"]),
    (41.90, 12.50, "Europe/Rome", "Рим (UTC+1)", ["рим", "rome", "италия", "italy", "милан"]),
    (40.42, -3.70, "Europe/Madrid", "Мадрид (UTC+1)", ["мадрид", "madrid", "испания", "spain", "барселона"]),
    (52.23, 21.01, "Europe/Warsaw", "Варшава (UTC+1)", ["варшава", "warsaw", "польша", "poland"]),
    (50.08, 14.44, "Europe/Prague", "Прага (UTC+1)", ["прага", "prague", "чехия"]),
    (44.82, 20.46, "Europe/Belgrade", "Белград (UTC+1)", ["белград", "belgrade", "сербия", "serbia"]),
    (40.71, -74.01, "America/New_York", "Нью-Йорк (UTC-5)", ["нью-йорк", "new york", "nyc"]),
    (34.05, -118.24, "America/Los_Angeles", "Лос-Анджелес (UTC-8)", ["лос-анджелес", "los angeles", "la"]),
    (35.68, 139.69, "Asia/Tokyo", "Токио (UTC+9)", ["токио", "tokyo", "япония"]),
    (13.76, 100.50, "Asia/Bangkok", "Бангкок (UTC+7)", ["бангкок", "bangkok", "таиланд", "пхукет"]),
    (-8.65, 115.22, "Asia/Makassar", "Бали (UTC+8)", ["бали", "bali", "денпасар"]),
]

# Quick alias lookup map (lowercase string -> iana_tz)
_ALIAS_LOOKUP: dict[str, str] = {}
for _lat, _lon, _tz, _display, _aliases in CITIES_DB:
    for alias in _aliases:
        _ALIAS_LOOKUP[alias.strip().lower()] = _tz

# Popular quick-selection buttons
POPULAR_TIMEZONES: list[tuple[str, str]] = [
    ("🇷🇺 Москва / СПб (UTC+3)", "Europe/Moscow"),
    ("🇷🇺 Екатеринбург (UTC+5)", "Asia/Yekaterinburg"),
    ("🇷🇺 Самара (UTC+4)", "Europe/Samara"),
    ("🇷🇺 Новосибирск (UTC+7)", "Asia/Novosibirsk"),
    ("🇷🇺 Калининград (UTC+2)", "Europe/Kaliningrad"),
    ("🇷🇺 Красноярск (UTC+7)", "Asia/Krasnoyarsk"),
    ("🇷🇺 Иркутск (UTC+8)", "Asia/Irkutsk"),
    ("🇷🇺 Владивосток (UTC+10)", "Asia/Vladivostok"),
    ("🇧🇾 Минск (UTC+3)", "Europe/Minsk"),
    ("🇰🇿 Алматы / Астана (UTC+5)", "Asia/Almaty"),
    ("🇺🇿 Ташкент (UTC+5)", "Asia/Tashkent"),
    ("🇬🇪 Тбилиси (UTC+4)", "Asia/Tbilisi"),
    ("🇬🇧 Лондон / UTC (UTC+0)", "UTC"),
    ("🇪🇺 Берлин / Париж (UTC+1)", "Europe/Berlin"),
]


def resolve_timezone(text: str) -> str | None:
    """Resolve a user input string (IANA timezone, city name, alias) to an IANA timezone string."""
    cleaned = text.strip()
    if not cleaned:
        return None
    if cleaned.upper() == "UTC":
        return "UTC"
    # 1. Direct valid ZoneInfo check
    try:
        ZoneInfo(cleaned)
        return cleaned
    except ZoneInfoNotFoundError:
        pass

    lowered = cleaned.lower()

    # 2. Check exact alias dictionary match
    if lowered in _ALIAS_LOOKUP:
        return _ALIAS_LOOKUP[lowered]

    # 3. Handle UTC/GMT offsets: UTC+3, UTC+03:00, +3, +04:00, -5, GMT-8, etc.
    raw_offset = lowered.removeprefix("utc").removeprefix("gmt").strip()
    if raw_offset and (raw_offset[0] in "+-" or raw_offset.isdigit()):
        offset_map = {
            2: "Europe/Kaliningrad",
            3: "Europe/Moscow",
            4: "Europe/Samara",
            5: "Asia/Yekaterinburg",
            6: "Asia/Omsk",
            7: "Asia/Novosibirsk",
            8: "Asia/Irkutsk",
            9: "Asia/Yakutsk",
            10: "Asia/Vladivostok",
            11: "Asia/Magadan",
            12: "Asia/Kamchatka",
            0: "UTC",
            1: "Europe/Berlin",
            -5: "America/New_York",
            -8: "America/Los_Angeles",
        }
        try:
            val = int(raw_offset.split(":")[0])
            if val in offset_map:
                return offset_map[val]
        except ValueError:
            pass

    # 4. Substring / partial alias match (only for aliases >= 3 chars to avoid false positives)
    for alias, tz in _ALIAS_LOOKUP.items():
        if len(alias) >= 3 and (alias in lowered or (len(lowered) >= 3 and lowered in alias)):
            return tz

    return None


def find_timezone_by_coordinates(lat: float, lon: float) -> str:
    """Find the closest matching IANA timezone for the given GPS coordinates."""
    best_dist = float("inf")
    best_tz = "UTC"
    for city_lat, city_lon, tz, _display, _ in CITIES_DB:
        # Scale longitude delta by cos of average latitude
        d_lat = lat - city_lat
        d_lon = (lon - city_lon) * math.cos(math.radians(lat))
        dist = d_lat * d_lat + d_lon * d_lon
        if dist < best_dist:
            best_dist = dist
            best_tz = tz
    return best_tz


def get_timezone_display(tz_name: str) -> str:
    """Get a user-friendly display string for a timezone."""
    if tz_name == "UTC":
        return "UTC (UTC+0)"
    for _, _, tz, display, _ in CITIES_DB:
        if tz == tz_name:
            return display
    return tz_name


def timezone_picker_menu(db, user) -> dict:
    """Generate inline keyboard for timezone selection."""
    from app.services.domain import UIActionService

    action_service = UIActionService(db)
    rows: list[list[dict]] = []

    # 2 buttons per row for popular cities
    for i in range(0, len(POPULAR_TIMEZONES), 2):
        row = []
        label1, tz1 = POPULAR_TIMEZONES[i]
        token1 = action_service.create(user, "setting_set_timezone", {"timezone": tz1}, commit=False)
        row.append({"text": label1, "callback_data": token1})
        if i + 1 < len(POPULAR_TIMEZONES):
            label2, tz2 = POPULAR_TIMEZONES[i + 1]
            token2 = action_service.create(user, "setting_set_timezone", {"timezone": tz2}, commit=False)
            row.append({"text": label2, "callback_data": token2})
        rows.append(row)

    # Location request button and back button
    loc_token = action_service.create(user, "setting_request_location", {}, commit=False)
    rows.append([{"text": "📍 Определить по геолокации", "callback_data": loc_token}])
    settings_token = action_service.create(user, "navigate", {"section": "settings"}, commit=False)
    rows.append([{"text": "« В настройки", "callback_data": settings_token}])
    db.commit()

    return {"inline_keyboard": rows}
