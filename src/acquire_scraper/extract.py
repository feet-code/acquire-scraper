from __future__ import annotations
import hashlib
import re
from urllib.parse import urljoin, urlsplit
from .models import SourceProduct

ORIGIN = "https://app.acquire.com"
LISTING_SELECTOR = 'a[href^="/startup/"]'
TITLE_SELECTOR = '.base-info-wrap__listing-headline'
DESCRIPTION_SELECTOR = '.base-info-wrap__about .public-info-block-item__description'


def canonical_url(value: str) -> str:
    p = urlsplit(urljoin(ORIGIN, value))
    if (p.scheme != 'https' or p.hostname != 'app.acquire.com' or p.port not in (None,443)
            or p.username or p.password or not re.fullmatch(r'/startup/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/?', p.path)):
        raise ValueError('Expected an Acquire startup listing URL.')
    return ORIGIN + p.path.rstrip('/')


def listing_id(url: str) -> str:
    # Both path components matter; one seller can have multiple listings.
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()[:24]


def clean_text(text: str) -> str:
    text = re.sub(r'https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '', text)
    text = re.sub(r'\b(?:[a-z0-9-]+\.)+(?:com|io|ai|net|org|app|co)\b', '', text, flags=re.I)
    return re.sub(r'\s+', ' ', text).strip()


def card_record(href: str, text: str, types: set[str]) -> dict | None:
    lines = [v.strip() for v in text.splitlines() if v.strip()]
    if not lines or lines[0].lower() not in types:
        return None
    title = []
    for line in lines[1:]:
        if line.upper() in {'TTM REVENUE', 'TTM PROFIT', 'ASKING PRICE'}:
            break
        if line.startswith('LAST SEEN '):
            continue
        if line not in {'Add to My deals', 'Create list', 'Sample list', 'Save'}:
            title.append(line)
    url = canonical_url(href)
    return {'id': listing_id(url), 'url': url, 'kind': lines[0], 'title': clean_text(' '.join(title))[:800]}


def extract_source(record: dict, title: str, description: str, brand: str = '') -> SourceProduct:
    description = re.sub(r'\s*See (?:more|less)\s*$', '', description, flags=re.I)
    title, description = clean_text(title), clean_text(description)
    if len(title) < 15 or len(description) < 80:
        raise ValueError('Listing description missing or too short; not generating from price/upgrade text.')
    if any(v in description.lower() for v in ('upgrade to unlock', 'sign in to continue')):
        raise ValueError('Only an access prompt was found; listing was not extracted.')
    return SourceProduct(external_id=record['id'], source_url=record['url'],
        source_name=brand, tagline=title[:800], description=description[:6000],
        categories=[record['kind']])
