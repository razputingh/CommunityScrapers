import json
import re
import sys
import time
from difflib import SequenceMatcher
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from lxml import html
from py_common import log
from py_common.util import scraper_args

BASE_URL = "https://www.titanmen.com/"
TITLE_MATCH_THRESHOLD = 0.92
TITLE_MATCH_MARGIN = 0.03
DURATION_TOLERANCE_SECONDS = 10
CARD_XPATH = (
    '//div[starts-with(@id, "scene-grid-item-") '
    'and contains(concat(" ", normalize-space(@class), " "), " scene-grid-item ")]'
)
TITLE_LINK_XPATH = (
    './/a[contains(concat(" ", normalize-space(@class), " "), " scene-link ")]'
)


def movie_key(title: str) -> str:
    movie = title.rsplit(":", 1)[0]
    return re.sub(r"[^a-z0-9]+", " ", movie.lower()).strip()


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def movie_id(url: str) -> str | None:
    query = parse_qs(urlparse(url).query)
    if query.get("sceneid") and query.get("id"):
        return query["id"][0]
    return None


def scene_id(card) -> str | None:
    match = re.fullmatch(r"scene-grid-item-(\d+)", card.get("id", ""))
    return match.group(1) if match else None


def closest_movie_id(key: str, movie_ids: dict[str, str]) -> str | None:
    if key in movie_ids:
        return movie_ids[key]

    matches = (
        (SequenceMatcher(None, key, candidate).ratio(), value)
        for candidate, value in movie_ids.items()
    )
    score, value = max(matches, default=(0, None))
    return value if score >= 0.8 else None


def parse_date_and_duration(card) -> tuple[str | None, int | None]:
    nodes = card.xpath(
        './/div[contains(concat(" ", normalize-space(@class), " "), '
        '" overlay-dates-time ")]'
    )
    if not nodes:
        return None, None

    text = " ".join(nodes[0].text_content().split())
    date_match = re.search(r"Released:\s*([^|]+)", text)
    duration_match = re.search(r"Length:\s*(\d+(?::\d+){1,2})", text)

    date = None
    if date_match:
        try:
            parsed_date = time.strptime(date_match.group(1).strip(), "%b %d, %Y")
            date = time.strftime("%Y-%m-%d", parsed_date)
        except ValueError:
            log.warning(f"Unable to parse date: {date_match.group(1).strip()}")

    duration = None
    if duration_match:
        parts = [int(part) for part in duration_match.group(1).split(":")]
        duration = sum(part * 60**index for index, part in enumerate(reversed(parts)))

    return date, duration


def fetch_tree(url: str):
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return html.fromstring(response.content)


def clean_text(node) -> str:
    return " ".join(node.text_content().split())


def parse_date(value: str) -> str | None:
    try:
        parsed_date = time.strptime(value.strip(), "%b %d, %Y")
    except ValueError:
        log.warning(f"Unable to parse date: {value.strip()}")
        return None
    return time.strftime("%Y-%m-%d", parsed_date)


def parse_duration(value: str) -> int | None:
    match = re.search(r"\d+(?::\d+){1,2}", value)
    if not match:
        return None
    parts = [int(part) for part in match.group().split(":")]
    return sum(part * 60**index for index, part in enumerate(reversed(parts)))


def labeled_value(details, label: str) -> str | None:
    nodes = details.xpath(f'.//strong[starts-with(normalize-space(.), "{label}")]/..')
    if not nodes:
        return None
    value = clean_text(nodes[0])
    return re.sub(rf"^{re.escape(label)}:\s*", "", value, flags=re.IGNORECASE)


def scrape_scene_details(url: str) -> dict:
    tree = fetch_tree(url)
    details_nodes = tree.xpath('//div[contains(@class, "scene-page-detail")]')
    title_nodes = tree.xpath('//h1[@class="scene-header-title"]')
    if not details_nodes or not title_nodes:
        raise ValueError(f"TitanMen scene details not found at {url}")

    details = details_nodes[0]
    scene = {"title": clean_text(title_nodes[0]), "urls": [url]}

    code_nodes = tree.xpath('//div[@class="rating_box"]/@data-id')
    if code_nodes:
        scene["code"] = code_nodes[0]

    description_nodes = details.xpath(
        './/h2[normalize-space(.)="Description"]/following-sibling::p'
    )
    if description_nodes:
        scene["details"] = "\n\n".join(clean_text(node) for node in description_nodes)

    if (released := labeled_value(details, "Released")) and (
        date := parse_date(released)
    ):
        scene["date"] = date

    if (length := labeled_value(details, "Length")) and (
        duration := parse_duration(length)
    ):
        scene["duration"] = duration

    performer_nodes = details.xpath(
        './/strong[starts-with(normalize-space(.), "Starring")]/following-sibling::a'
    )
    if performer_nodes:
        scene["performers"] = [
            {
                "name": clean_text(node),
                "urls": [urljoin(BASE_URL, node.get("href", ""))],
            }
            for node in performer_nodes
        ]

    tag_nodes = details.xpath(
        './/strong[starts-with(normalize-space(.), "Categories")]/following-sibling::a'
    )
    if tag_nodes:
        scene["tags"] = [{"name": clean_text(node)} for node in tag_nodes]

    director_nodes = details.xpath(
        './/strong[starts-with(normalize-space(.), "Director")]/following-sibling::a'
    )
    if director_nodes:
        scene["director"] = clean_text(director_nodes[0])

    group_nodes = details.xpath(
        './/strong[starts-with(normalize-space(.), "Movie Title")]/following-sibling::a'
    )
    if group_nodes:
        group_url = urljoin(BASE_URL, group_nodes[0].get("href", ""))
        scene["groups"] = [{"name": clean_text(group_nodes[0]), "urls": [group_url]}]
        add_movie_metadata(scene, group_url)

    if scene.get("code"):
        image_nodes = tree.xpath(
            f'//div[@id="scene-grid-item-{scene["code"]}"]'
            '//img[contains(concat(" ", normalize-space(@class), " "), '
            '" scene-grid-image ")]/@src'
        )
        if image_nodes:
            scene["image"] = urljoin(BASE_URL, image_nodes[0])

    return scene


def add_movie_metadata(scene: dict, group_url: str) -> None:
    try:
        tree = fetch_tree(group_url)
    except requests.RequestException as error:
        log.warning(f"Unable to fetch TitanMen movie metadata: {error}")
        return

    studio_nodes = tree.xpath(
        '//div[contains(@class, "movie-page-detail")]'
        '//strong[starts-with(normalize-space(.), "Studio")]/following-sibling::a'
    )
    if studio_nodes:
        name = clean_text(studio_nodes[0])
        scene["studio"] = {
            "name": "TitanMen Rough" if name == "Rough" else name,
            "urls": [urljoin(BASE_URL, studio_nodes[0].get("href", ""))],
        }

    if not scene.get("director"):
        director_nodes = tree.xpath(
            '//div[contains(@class, "movie-page-detail")]'
            '//strong[starts-with(normalize-space(.), "Director")]/following-sibling::a'
        )
        if director_nodes:
            scene["director"] = clean_text(director_nodes[0])


def search_scenes(name: str) -> list[dict]:
    response = requests.get(
        urljoin(BASE_URL, "search.php"), params={"query": name}, timeout=30
    )
    response.raise_for_status()
    cards = html.fromstring(response.content).xpath(CARD_XPATH)

    movie_ids: dict[str, str] = {}
    parsed_cards = []
    for card in cards:
        links = card.xpath(TITLE_LINK_XPATH)
        if not links:
            continue

        link = links[0]
        title = " ".join(link.text_content().split())
        if not title or ": Photos:" in title:
            continue

        href = urljoin(BASE_URL, link.get("href", ""))
        parsed_cards.append((card, title, href))
        if direct_movie_id := movie_id(href):
            movie_ids[movie_key(title)] = direct_movie_id

    scenes = []
    for card, title, href in parsed_cards:
        direct_movie_id = movie_id(href)
        card_scene_id = scene_id(card)
        if not direct_movie_id and card_scene_id:
            direct_movie_id = closest_movie_id(movie_key(title), movie_ids)
            if direct_movie_id:
                href = urljoin(
                    BASE_URL, f"dvds.php?id={direct_movie_id}&sceneid={card_scene_id}"
                )

        if not direct_movie_id:
            log.warning(f"Unable to resolve scene URL for: {title}")
            continue

        scene = {"title": title, "url": href, "code": card_scene_id}
        images = card.xpath(
            './/img[contains(concat(" ", normalize-space(@class), " "), '
            '" scene-grid-image ")]/@src'
        )
        if images:
            scene["image"] = urljoin(BASE_URL, images[0])

        performer_nodes = card.xpath(
            './/div[contains(concat(" ", normalize-space(@class), " "), '
            '" overlay-stars ")]'
        )
        if performer_nodes:
            names = [
                value.strip()
                for value in performer_nodes[0].text_content().split(",")
                if value.strip()
            ]
            scene["performers"] = [{"name": value} for value in names]

        date, duration = parse_date_and_duration(card)
        if date:
            scene["date"] = date
        if duration is not None:
            scene["duration"] = duration

        scenes.append(scene)

    return scenes


def search_fragment_candidates(title: str) -> list[dict]:
    queries = []
    if ":" in title:
        subtitle = title.rsplit(":", 1)[1].strip()
        queries.append(subtitle)
        queries.append(re.sub(r"\s+and\s+", " & ", subtitle, flags=re.IGNORECASE))
    queries.append(title)

    candidates = {}
    for query in dict.fromkeys(queries):
        for candidate in search_scenes(query):
            candidates[candidate["url"]] = candidate
    return list(candidates.values())


def titan_scene_url(arguments: dict) -> str | None:
    urls = list(arguments.get("urls") or [])
    if arguments.get("url"):
        urls.append(arguments["url"])

    for value in urls:
        parsed = urlparse(value)
        if (
            parsed.hostname
            and parsed.hostname.lower().endswith("titanmen.com")
            and parse_qs(parsed.query).get("sceneid")
        ):
            return value
    return None


def fragment_duration(arguments: dict) -> float | None:
    for file_data in arguments.get("files") or []:
        duration = file_data.get("duration")
        if duration is not None:
            return float(duration)
    return None


def candidate_has_conflict(
    candidate: dict, date: str | None, duration: float | None
) -> bool:
    if date and candidate.get("date") and candidate["date"] != date:
        return True
    if duration is not None and candidate.get("duration") is not None:
        return abs(candidate["duration"] - duration) > DURATION_TOLERANCE_SECONDS
    return False


def candidate_has_corroboration(
    candidate: dict, date: str | None, duration: float | None
) -> bool:
    date_matches = bool(date and candidate.get("date") == date)
    duration_matches = bool(
        duration is not None
        and candidate.get("duration") is not None
        and abs(candidate["duration"] - duration) <= DURATION_TOLERANCE_SECONDS
    )
    return date_matches or duration_matches


def select_fragment_candidate(candidates: list[dict], arguments: dict) -> dict | None:
    code = str(arguments.get("code") or "").strip()
    if code:
        code_matches = [
            candidate for candidate in candidates if candidate.get("code") == code
        ]
        if len(code_matches) == 1:
            return code_matches[0]

    title = normalize_title(arguments.get("title") or "")
    if not title:
        return None

    date = arguments.get("date")
    duration = fragment_duration(arguments)
    exact_matches = [
        candidate
        for candidate in candidates
        if normalize_title(candidate["title"]) == title
        and not candidate_has_conflict(candidate, date, duration)
        and candidate_has_corroboration(candidate, date, duration)
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if exact_matches:
        return None

    fuzzy_matches = []
    for candidate in candidates:
        score = SequenceMatcher(
            None, title, normalize_title(candidate["title"])
        ).ratio()
        date_matches = bool(date and candidate.get("date") == date)
        duration_matches = bool(
            duration is not None
            and candidate.get("duration") is not None
            and abs(candidate["duration"] - duration) <= DURATION_TOLERANCE_SECONDS
        )
        if score >= TITLE_MATCH_THRESHOLD and date_matches and duration_matches:
            fuzzy_matches.append((score, candidate))

    fuzzy_matches.sort(key=lambda item: item[0], reverse=True)
    if not fuzzy_matches:
        return None
    if len(fuzzy_matches) > 1:
        margin = fuzzy_matches[0][0] - fuzzy_matches[1][0]
        if margin < TITLE_MATCH_MARGIN:
            return None
    return fuzzy_matches[0][1]


def scrape_scene_fragment(arguments: dict) -> dict | None:
    if url := titan_scene_url(arguments):
        return scrape_scene_details(url)

    title = arguments.get("title") or ""
    if not title:
        return None

    candidates = search_fragment_candidates(title)
    candidate = select_fragment_candidate(candidates, arguments)
    if not candidate:
        log.warning(f"No confident TitanMen match for: {title}")
        return None
    return scrape_scene_details(candidate["url"])


if __name__ == "__main__":
    operation, arguments = scraper_args()
    if operation == "scene-by-name" and arguments.get("name"):
        result = search_scenes(arguments["name"])
    elif operation in ("scene-by-fragment", "scene-by-query-fragment"):
        result = scrape_scene_fragment(arguments)
    else:
        log.error(f"Operation: {operation}, arguments: {json.dumps(arguments)}")
        sys.exit(1)

    log.debug(f"JSON Return Value: {result}")
    print(json.dumps(result))
