import asyncio
import html
import json
import logging
import os
import re
import time
from typing import Any, Literal, Optional, Union
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, field_validator
from rapidfuzz import fuzz

import config
from llm_providers import LLMRouter


def get_logger() -> logging.Logger:
    log = logging.getLogger("lead_discovery")
    if not log.handlers:
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


logger = get_logger()

DEFAULT_FALLBACK_CITIES: tuple[str, ...] = ("New York", "Austin", "San Francisco", "Chicago")
DEFAULT_MIN_EMPLOYEES = 11
DEFAULT_MAX_EMPLOYEES = 50
SERPER_URL = "https://google.serper.dev/search"

COUNTRY_CODE_ALIASES: dict[str, str] = {
    "uk": "gb",
    "usa": "us",
    "u.s.": "us",
    "u.s.a.": "us",
    "uae": "ae",
}

LOCATION_TO_COUNTRY_CODE: dict[str, str] = {
    "united states": "us", "usa": "us", "texas": "us", "california": "us", "new york": "us",
    "florida": "us", "illinois": "us", "massachusetts": "us", "washington": "us", "georgia": "us",
    "north carolina": "us", "colorado": "us", "virginia": "us", "pennsylvania": "us", "arizona": "us",
    "ohio": "us", "michigan": "us", "new jersey": "us", "utah": "us", "tennessee": "us",
    "austin": "us", "dallas": "us", "houston": "us", "san antonio": "us", "san francisco": "us",
    "los angeles": "us", "san diego": "us", "san jose": "us", "seattle": "us", "boston": "us",
    "chicago": "us", "miami": "us", "atlanta": "us", "denver": "us", "phoenix": "us",
    "india": "in", "bengaluru": "in", "bangalore": "in", "chennai": "in", "hyderabad": "in",
    "mumbai": "in", "pune": "in", "delhi": "in", "new delhi": "in", "gurugram": "in",
    "gurgaon": "in", "noida": "in", "kolkata": "in", "ahmedabad": "in", "kochi": "in",
    "united kingdom": "gb", "great britain": "gb", "england": "gb", "scotland": "gb", "wales": "gb",
    "london": "gb", "manchester": "gb", "edinburgh": "gb", "bristol": "gb", "leeds": "gb", "glasgow": "gb",
    "canada": "ca", "ontario": "ca", "quebec": "ca", "british columbia": "ca", "alberta": "ca",
    "toronto": "ca", "vancouver": "ca", "montreal": "ca", "ottawa": "ca", "calgary": "ca", "waterloo": "ca",
    "australia": "au", "sydney": "au", "melbourne": "au", "brisbane": "au", "perth": "au", "adelaide": "au",
    "germany": "de", "deutschland": "de", "berlin": "de", "munich": "de", "hamburg": "de",
    "frankfurt": "de", "stuttgart": "de", "cologne": "de",
    "france": "fr", "paris": "fr", "lyon": "fr", "marseille": "fr", "toulouse": "fr",
    "netherlands": "nl", "holland": "nl", "amsterdam": "nl", "rotterdam": "nl", "utrecht": "nl",
    "singapore": "sg",
    "united arab emirates": "ae", "uae": "ae", "dubai": "ae", "abu dhabi": "ae", "sharjah": "ae",
    "ireland": "ie", "dublin": "ie", "cork": "ie",
    "spain": "es", "madrid": "es", "barcelona": "es",
    "italy": "it", "milan": "it", "rome": "it",
    "switzerland": "ch", "zurich": "ch", "geneva": "ch", "basel": "ch",
    "sweden": "se", "stockholm": "se", "gothenburg": "se",
    "poland": "pl", "warsaw": "pl", "krakow": "pl",
    "japan": "jp", "tokyo": "jp", "osaka": "jp",
    "brazil": "br", "sao paulo": "br", "rio de janeiro": "br",
    "mexico": "mx", "mexico city": "mx", "monterrey": "mx",
    "israel": "il", "tel aviv": "il",
    "new zealand": "nz", "auckland": "nz", "wellington": "nz",
    "saudi arabia": "sa", "riyadh": "sa", "jeddah": "sa",
    "south africa": "za", "johannesburg": "za", "cape town": "za",
}

CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "bengaluru": ("bengaluru", "bangalore"),
    "bangalore": ("bangalore", "bengaluru"),
    "mumbai": ("mumbai", "bombay"),
    "kolkata": ("kolkata", "calcutta"),
    "gurugram": ("gurugram", "gurgaon"),
    "gurgaon": ("gurgaon", "gurugram"),
    "kochi": ("kochi", "cochin"),
    "new york": ("new york", "new york city", "nyc"),
    "new york city": ("new york city", "new york", "nyc"),
    "san francisco": ("san francisco", "sf"),
    "washington": ("washington", "washington dc", "washington, d.c."),
}

LINKEDIN_BRACKETS: tuple[tuple[int, int, str], ...] = (
    (1, 10, '"1-10 employees"'),
    (11, 50, '"11-50 employees"'),
    (51, 200, '"51-200 employees"'),
    (201, 500, '"201-500 employees"'),
    (501, 1000, '"501-1,000 employees"'),
    (1001, 5000, '"1,001-5,000 employees"'),
    (5001, 10000, '"5,001-10,000 employees"'),
    (10001, 10_000_000, '"10,001+ employees"'),
)

CLUTCH_BRACKETS: tuple[tuple[int, int, str], ...] = (
    (2, 9, '"2 - 9"'),
    (10, 49, '"10 - 49"'),
    (50, 249, '"50 - 249"'),
    (250, 999, '"250 - 999"'),
    (1000, 10_000_000, '"1,000+"'),
)

DIRECTORY_DOMAINS: frozenset[str] = frozenset({
    "linkedin.com", "clutch.co", "cloudtango.net", "zoominfo.com", "apollo.io", "bbb.org",
    "glassdoor.com", "indeed.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "yelp.com", "crunchbase.com", "pitchbook.com", "dnb.com", "owler.com",
    "bloomberg.com", "mapquest.com", "yellowpages.com", "goodfirms.co", "designrush.com",
    "sortlist.com", "rocketreach.co", "lusha.com", "signalhire.com", "leadiq.com", "craft.co",
    "cbinsights.com", "g2.com", "capterra.com", "trustpilot.com", "upcity.com", "themanifest.com",
    "manifest.com", "builtin.com", "builtinaustin.com", "wikipedia.org", "sec.gov",
    "opencorporates.com", "chamberofcommerce.com", "manta.com", "kompass.com", "thomasnet.com",
    "expertise.com", "ziprecruiter.com", "simplyhired.com", "comparably.com", "zippia.com",
    "adapt.io", "seamless.ai", "contactout.com", "datanyze.com", "growjo.com", "fixnhour.com",
    "techbehemoths.com", "itfirms.co", "selectedfirms.co", "softwareworld.co", "topdevelopers.co",
    "appfutura.com",
})

NULL_STRINGS: frozenset[str] = frozenset({
    "", "none", "null", "n/a", "na", "unknown", "not available", "not found",
    "unavailable", "not listed", "not specified", "-", "--",
})

GENERIC_PRODUCT_WORDS: frozenset[str] = frozenset({
    "software", "platform", "solution", "solutions", "system", "systems", "service",
    "services", "tool", "tools", "app", "application", "product", "saas", "ai",
    "cloud", "b2b", "enterprise", "management", "automation", "analytics",
})

SOLO_MICRO_RE = re.compile(
    r"\b(?:0\s+employees?|self[- ]employed|sole\s+proprietor|freelancer|solo\s+founder)\b",
    re.I,
)
EMP_RANGE_RE = re.compile(
    r"\b(\d[\d,]{0,6})\s*[-–—to]+\s*(\d[\d,]{0,6})\s*(?:total\s+)?employees?\b",
    re.I,
)
KNOWN_BRACKET_RE = re.compile(
    r"\b(1\s*[-–—]\s*10|2\s*[-–—]\s*9|10\s*[-–—]\s*49|11\s*[-–—]\s*50|50\s*[-–—]\s*249|51\s*[-–—]\s*200|201\s*[-–—]\s*500|250\s*[-–—]\s*999|501\s*[-–—]\s*1,?000|1,?001\s*[-–—]\s*5,?000|5,?001\s*[-–—]\s*10,?000)\b",
    re.I,
)
EMP_PLUS_RE = re.compile(r"\b(\d[\d,]{0,6})\s*\+\s*(?:total\s+)?employees?\b", re.I)
EXACT_EMP_RE = re.compile(r"\b(\d[\d,]{0,6})\s+(?:total\s+)?employees?\b", re.I)
CRM_RE = re.compile(
    r"\b(?:hubspot|salesforce|pipedrive|zoho\s*crm|zoho|microsoft\s*dynamics|dynamics\s*365|monday\.com|close\.com|copper\s*crm|freshsales|gohighlevel|connectwise|autotask|sugar\s*crm|keap|crm)\b",
    re.I,
)
HIRING_RE = re.compile(
    r"\b(?:hiring|we're\s+hiring|job\s+opening|open\s+roles?|careers|account\s+executive|sales\s+representative|sdr|bdr|business\s+development\s+representative|sales\s+development)\b",
    re.I,
)
REVOPS_RE = re.compile(
    r"\b(?:revops|revenue\s+operations|vp\s+of\s+revenue\s+operations|head\s+of\s+revops)\b",
    re.I,
)
LEAD_GEN_RE = re.compile(
    r"\b(?:inbound|outbound|lead\s+generation|demand\s+generation|paid\s+ads|ppc|seo|hubspot|google\s+ads|linkedin\s+ads|100\+|hundreds\s+of\s+clients|reviews)\b",
    re.I,
)
ROLE_RE = re.compile(
    r"\b(?:ceo|cto|coo|cfo|cro|cmo|founder|co-founder|owner|president|managing\s+partner|partner|vp|vice\s+president|head\s+of\s+sales|sales\s+lead|sales\s+team\s+lead|director|business\s+development|revenue)\b",
    re.I,
)
SUFFIX_RE = re.compile(
    r"\b(?:inc\.?|incorporated|llc|l\.l\.c\.?|corp\.?|corporation|ltd\.?|limited|co\.?|company|lp|llp|pllc|pc|gmbh|group|holdings)\b",
    re.I,
)
STOP_TOKENS: frozenset[str] = frozenset({
    "the", "and", "for", "services", "solutions", "technologies", "technology",
    "systems", "software", "consulting", "partners", "digital", "global", "usa", "texas",
})
PHONE_RE = re.compile(
    r"(?:\+\d{1,3}[\s.\-]?(?:\(\s*\d{1,5}\s*\)[\s.\-]?)?(?:\d[\s.\-]?){6,12}\d|(?:\+?1[\s.\-]?)?(?:\(\s*\d{3}\s*\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}|(?:\(\s*0?\d{2,5}\s*\)[\s.\-]?)\d{2,5}(?:[\s.\-]?\d{2,5}){1,2})"
)
LI_CO_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([a-zA-Z0-9\-_%]+)", re.I)
LI_IN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([a-zA-Z0-9\-_%]+)", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)
EXISTING_USERS_INTENT_RE = re.compile(
    r"\b(?:companies|leads|accounts|businesses|firms)\s+(?:that\s+)?(?:already\s+use|already\s+using|currently\s+using|using)\b|\bexisting\s+users\s+of\b",
    re.I,
)
EXCLUDE_USERS_INTENT_RE = re.compile(
    r"\b(?:not\s+(?:already\s+)?using|don't\s+use|do\s+not\s+use|without\s+using|ignore\s+.*using|we\s+sell|our\s+product)\b",
    re.I,
)


class ICPRequirements(BaseModel):
    product_offering: str
    product_names: list[str] = Field(default_factory=list)
    exclude_existing_users: bool = True
    target_buyer_need: str = ""
    target_industries: list[str]
    target_cities: list[str]
    region_hint: str = ""
    search_region: str = ""
    country_code: str = "us"
    explicit_cities: bool = False
    city_aliases: dict[str, list[str]] = Field(default_factory=dict)
    location_search_map: dict[str, str] = Field(default_factory=dict)
    regional_directories: list[str] = Field(default_factory=list)
    min_employees: int = DEFAULT_MIN_EMPLOYEES
    max_employees: int = DEFAULT_MAX_EMPLOYEES
    requested_count: int = 10
    search_keywords: list[str]
    exclude_companies: list[str] = Field(default_factory=list)


class LeadRecord(BaseModel):
    company_name: str
    city: str
    industry_niche: str
    total_employees: Union[int, str]
    estimated_sales_team_size: int = Field(ge=1)
    company_phone_number: Optional[str] = None
    company_website: Optional[str] = None
    company_linkedin_url: Optional[str] = None
    contact_person_name: Optional[str] = None
    contact_person_title: Optional[str] = None
    contact_person_linkedin_url: Optional[str] = None
    contact_phone_number: Optional[str] = None
    icp_match_score: int = Field(ge=0, le=100)
    icp_tier: Literal["Hot ICP", "Good ICP", "Nurture", "Deprioritize"]
    suitability_reason: str

    @field_validator(
        "company_phone_number", "company_website", "company_linkedin_url",
        "contact_person_name", "contact_person_title", "contact_person_linkedin_url",
        "contact_phone_number", mode="before",
    )
    @classmethod
    def clean_opt(cls, v: Any) -> Optional[str]:
        return norm_str(v)


class LeadDiscoveryResponse(BaseModel):
    matched_count: int
    leads: list[LeadRecord]


def norm_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in NULL_STRINGS else text


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text or ""))).strip()


def _to_int(raw: str) -> int:
    return int(raw.replace(",", "").strip())


def extract_domain(url: str) -> Optional[str]:
    if not url:
        return None
    raw = url.strip().rstrip(".,;:!?")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    try:
        host = urlparse(raw).netloc.lower().split(":")[0].rstrip(".")
    except ValueError:
        return None
    host = host[4:] if host.startswith("www.") else host
    return host if "." in host and " " not in host else None


def is_directory(domain: Optional[str]) -> bool:
    return not domain or any(domain == d or domain.endswith("." + d) for d in DIRECTORY_DOMAINS)


def norm_website(url: Optional[str]) -> Optional[str]:
    dom = extract_domain(url or "")
    return f"https://{dom}" if dom and not is_directory(dom) else None


def norm_li_url(url: Optional[str], kind: Literal["company", "in"]) -> Optional[str]:
    if not url:
        return None
    pattern = LI_CO_RE if kind == "company" else LI_IN_RE
    m = pattern.search(url.strip())
    if not m:
        return None
    slug = m.group(1).strip("/").lower()
    return f"https://www.linkedin.com/{kind}/{slug}" if slug else None


def norm_phone(phone: Optional[str], country_code: str = "us") -> Optional[str]:
    if not phone:
        return None
    raw = phone.strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 8 or len(digits) > 15 or len(set(digits)) <= 2:
        return None
    if digits in {"1234567890", "0123456789", "01234567890"}:
        return None
    cc = (country_code or "us").lower()
    if cc in {"us", "ca"} and not (raw.startswith("+") and not raw.startswith("+1")):
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10 or digits[3:6] == "555" or digits[0] in {"0", "1"} or digits[3] in {"0", "1"}:
            return None
        return digits
    return digits


def fmt_phone(digits: str, raw_text: Optional[str] = None, country_code: str = "us") -> str:
    cc = (country_code or "us").lower()
    if len(digits) == 10 and cc in {"us", "ca"} and digits[0] not in {"0", "1"} and digits[3] not in {"0", "1"}:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if raw_text:
        cleaned = re.sub(r"\s+", " ", raw_text).strip()
        if cleaned:
            return cleaned
    return f"+{digits}" if len(digits) >= 11 else digits


def core_company_name(name: str) -> str:
    stripped = re.sub(r"[,\s]+$", "", SUFFIX_RE.sub("", name)).strip()
    return stripped or name.strip()


def company_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", core_company_name(name).lower()).strip()


def company_tokens(name: str) -> list[str]:
    all_toks = [t for t in re.findall(r"[a-z0-9]+", core_company_name(name).lower()) if len(t) >= 2]
    distinct = [t for t in all_toks if t not in STOP_TOKENS]
    return distinct or all_toks


def city_variants(city: str, custom_aliases: Optional[dict[str, list[str]]] = None) -> tuple[str, ...]:
    low = city.strip().lower()
    if not low:
        return ()
    variants: list[str] = [low]
    for v in CITY_ALIASES.get(low, ()):
        if v not in variants:
            variants.append(v)
    if custom_aliases:
        for key, vals in custom_aliases.items():
            if key.strip().lower() == low:
                for v in vals:
                    vl = v.strip().lower()
                    if vl and vl not in variants:
                        variants.append(vl)
    return tuple(variants)


def _phrase_in_text(phrase: str, text_lower: str) -> bool:
    if len(phrase) <= 3:
        return bool(re.search(rf"\b{re.escape(phrase)}\b", text_lower))
    return phrase in text_lower


def match_city(
    city_text: Optional[str],
    allowed: list[str],
    custom_aliases: Optional[dict[str, list[str]]] = None,
) -> Optional[str]:
    if not city_text:
        return None
    low = city_text.lower()
    for c in allowed:
        if any(_phrase_in_text(v, low) for v in city_variants(c, custom_aliases)):
            return c
    return None


def location_in_text(
    city: str,
    region_hint: str,
    full_text_lower: str,
    explicit_cities: bool,
    custom_aliases: Optional[dict[str, list[str]]] = None,
    search_region: str = "",
) -> bool:
    if any(_phrase_in_text(v, full_text_lower) for v in city_variants(city, custom_aliases)):
        return True
    if not explicit_cities:
        for reg in (region_hint, search_region):
            if reg and _phrase_in_text(reg.strip().lower(), full_text_lower):
                return True
    return False


def resolve_country_code(raw_code: Optional[str], region_hint: str, cities: list[str], prompt: str) -> str:
    reg_low = (region_hint or "").lower()
    for place, code in sorted(LOCATION_TO_COUNTRY_CODE.items(), key=lambda kv: len(kv[0]), reverse=True):
        if re.search(rf"\b{re.escape(place)}\b", reg_low):
            return code
    if raw_code:
        norm = COUNTRY_CODE_ALIASES.get(raw_code.strip().lower(), raw_code.strip().lower())
        if re.fullmatch(r"[a-z]{2}", norm):
            return norm
    search_space = " | ".join([*cities, prompt]).lower()
    for place, code in sorted(LOCATION_TO_COUNTRY_CODE.items(), key=lambda kv: len(kv[0]), reverse=True):
        if re.search(rf"\b{re.escape(place)}\b", search_space):
            return code
    return "us"


def sanitize_search_location(
    raw_loc: Optional[str],
    city: str,
    search_region: str,
) -> str:
    """Validate an LLM-formatted search location clause or fall back to deterministic formatting."""
    fallback = f'"{city}" {search_region}'.strip() if search_region and search_region.lower() != city.lower() else f'"{city}"'
    text = (raw_loc or "").strip()
    if not text or len(text) > 90 or "site:" in text.lower() or text.count('"') % 2 != 0:
        return fallback
    if city.lower() not in text.lower() and not any(v in text.lower() for v in city_variants(city)):
        return fallback
    return re.sub(r"\s+", " ", text)


def parse_count_and_bounds(icp_prompt: str) -> tuple[int, Optional[int], Optional[int]]:
    """Extract requested lead count and any explicitly mentioned employee bounds."""
    count = 10
    for pat in (
        r"(?:exactly|need|want|find|get|return|give|deliver|provide)\s+(\d{1,2})\s+(?:qualified\s+)?leads?\b",
        r"\b(\d{1,2})\s+(?:qualified\s+)?leads?\b",
        r"(?:exactly|top|limit)\s+(\d{1,2})\b",
    ):
        m = re.search(pat, icp_prompt, re.I)
        if m and 1 <= int(m.group(1)) <= 50:
            count = int(m.group(1))
            break

    min_e: Optional[int] = None
    max_e: Optional[int] = None

    range_pats = (
        r"\b(\d[\d,]{0,6})\s*[-–—to]+\s*(\d[\d,]{0,6})\s*(?:total\s+)?(?:employees?|staff|people|headcount)\b",
        r"(?:company\s+size|employee\s+count|employees?|headcount|team\s+size)\s*(?:of|is|:|=|between)?\s*(\d[\d,]{0,6})\s*(?:[-–—to]+|and)\s*(\d[\d,]{0,6})\b",
    )
    for pat in range_pats:
        m = re.search(pat, icp_prompt, re.I)
        if m:
            lo, hi = sorted((_to_int(m.group(1)), _to_int(m.group(2))))
            if 1 <= lo <= hi <= 1_000_000:
                return count, lo, hi

    plus_m = re.search(
        r"\b(\d[\d,]{0,6})\s*\+\s*(?:total\s+)?(?:employees?|staff|people|headcount)\b|(?:at\s+least|more\s+than|over|minimum|min)\s+(\d[\d,]{0,6})\s+(?:employees?|staff|people|headcount)\b",
        icp_prompt,
        re.I,
    )
    if plus_m:
        val = _to_int(plus_m.group(1) or plus_m.group(2))
        if 1 <= val <= 1_000_000:
            return count, val, 1_000_000

    under_m = re.search(
        r"\b(?:under|less\s+than|below|up\s+to|max(?:imum)?)\s+(\d[\d,]{0,6})\s+(?:employees?|staff|people|headcount)\b",
        icp_prompt,
        re.I,
    )
    if under_m:
        val = _to_int(under_m.group(1))
        if 1 <= val <= 1_000_000:
            return count, 1, val

    exact_m = re.search(
        r"(?:company\s+size|employee\s+count|headcount)\s*(?:of|is|:|=|around|approx(?:imately)?)?\s*(\d[\d,]{0,6})\b|\b(?:around|approx(?:imately)?|about)?\s*(\d[\d,]{0,6})\s+(?:total\s+)?employees?\b",
        icp_prompt,
        re.I,
    )
    if exact_m:
        val = _to_int(exact_m.group(1) or exact_m.group(2))
        if 1 <= val <= 1_000_000:
            lo = max(1, round(val * 0.7))
            hi = max(val, round(val * 1.3))
            return count, lo, hi

    return count, min_e, max_e


def resolve_employee_bounds(
    regex_min: Optional[int],
    regex_max: Optional[int],
    llm_min: Any,
    llm_max: Any,
) -> tuple[int, int]:
    """Use user-mentioned employee bounds when available; otherwise default to 11-50."""
    if regex_min is not None and regex_max is not None:
        return regex_min, regex_max
    l_min = int(llm_min) if isinstance(llm_min, (int, float)) and llm_min >= 1 else None
    l_max = int(llm_max) if isinstance(llm_max, (int, float)) and llm_max >= 1 else None
    if l_min is not None and l_max is not None:
        return min(l_min, l_max), max(l_min, l_max)
    if l_min is not None:
        return l_min, max(l_min, 1_000_000)
    if l_max is not None:
        return 1, l_max
    return DEFAULT_MIN_EMPLOYEES, DEFAULT_MAX_EMPLOYEES


def _range_overlaps(lo: int, hi: int, min_e: int, max_e: int) -> bool:
    if hi < min_e or lo > max_e:
        return False
    # Reject broad micro-brackets (e.g. "1-50" or "1-10") when targeting >= 10 employees, while keeping Clutch's "10-49".
    if min_e >= 10 and lo < max(1, min_e - 1) and hi <= max_e:
        return False
    return True


def parse_employee_value(value: Any, min_e: int, max_e: int) -> Optional[Union[int, str]]:
    if value is None:
        return None
    if isinstance(value, int):
        return value if min_e <= value <= max_e else None
    text = str(value).strip()
    low = text.lower()
    if low in NULL_STRINGS:
        return None
    if SOLO_MICRO_RE.search(text) and min_e > 1:
        return None
    cleaned = text.replace(",", "")
    if re.fullmatch(r"\d+", cleaned):
        n = int(cleaned)
        return n if min_e <= n <= max_e else None
    pm = re.fullmatch(r"(\d+)\s*\+(?:\s*employees?)?", cleaned, re.I)
    if pm:
        n = int(pm.group(1))
        return text if max_e >= n else None
    rm = re.search(r"(\d+)\s*[-–—to]+\s*(\d+)", cleaned, re.I)
    if rm:
        lo, hi = sorted((int(rm.group(1)), int(rm.group(2))))
        if _range_overlaps(lo, hi, min_e, max_e):
            return f"{lo}-{hi}"
        return None
    sm = re.search(r"\b(\d{1,6})\b", cleaned)
    if sm:
        n = int(sm.group(1))
        return n if min_e <= n <= max_e else None
    return None


def model_sales_size(
    total_emp: Union[int, str],
    extracted: Any,
    min_e: int = DEFAULT_MIN_EMPLOYEES,
    max_e: int = DEFAULT_MAX_EMPLOYEES,
) -> int:
    upper = 15 if max_e <= 50 else max(15, round(max_e * 0.35))
    lower = 2 if min_e >= 6 else 1
    if isinstance(extracted, int) and lower <= extracted <= upper:
        if isinstance(total_emp, int) and extracted > total_emp:
            return max(lower, min(upper, round(total_emp * 0.25)))
        return extracted
    if isinstance(extracted, str) and re.fullmatch(r"\d+", extracted.strip()):
        v = int(extracted.strip())
        if lower <= v <= upper:
            return v
    if isinstance(total_emp, int):
        return max(lower, min(upper, round(total_emp * 0.22)))
    return min(upper, max(lower, round(((min_e + min(max_e, 1000)) / 2) * 0.2)))


def _extract_emp_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for m in EMP_RANGE_RE.finditer(text):
        lo, hi = sorted((_to_int(m.group(1)), _to_int(m.group(2))))
        spans.append((lo, hi))
    for m in KNOWN_BRACKET_RE.finditer(text):
        nums = re.findall(r"\d[\d,]*", m.group(1))
        if len(nums) == 2:
            lo, hi = sorted((_to_int(nums[0]), _to_int(nums[1])))
            if (lo, hi) not in spans:
                spans.append((lo, hi))
    for m in EMP_PLUS_RE.finditer(text):
        n = _to_int(m.group(1))
        spans.append((n, 10_000_000))
    for m in EXACT_EMP_RE.finditer(text):
        n = _to_int(m.group(1))
        spans.append((n, n))
    return spans


def size_disqualified(text: str, min_e: int, max_e: int) -> bool:
    if min_e > 1 and SOLO_MICRO_RE.search(text):
        return True
    spans = _extract_emp_spans(text)
    if not spans:
        return False
    has_qualifying = any(_range_overlaps(lo, hi, min_e, max_e) for lo, hi in spans)
    if has_qualifying:
        return False
    return True


def linkedin_company_disqualified(evidence: list[dict[str, str]], min_e: int, max_e: int) -> bool:
    for ev in evidence:
        link = ev.get("link", "")
        if LI_CO_RE.search(link) or "zoominfo.com" in link or "apollo.io" in link:
            txt = f"{ev.get('title', '')} {ev.get('snippet', '')}"
            if size_disqualified(txt, min_e, max_e):
                return True
    return False


def linkedin_size_clause(min_e: int, max_e: int) -> str:
    hits = [label for lo, hi, label in LINKEDIN_BRACKETS if hi >= min_e and lo <= max_e][:3]
    if not hits:
        return '"11-50 employees"'
    return hits[0] if len(hits) == 1 else f"({' OR '.join(hits)})"


def clutch_size_clause(min_e: int, max_e: int) -> str:
    hits = [label for lo, hi, label in CLUTCH_BRACKETS if hi >= min_e and lo <= max_e][:3]
    if not hits:
        return '"10 - 49"'
    return hits[0] if len(hits) == 1 else f"({' OR '.join(hits)})"


def already_uses_product_in_evidence(
    evidence: list[dict[str, str]],
    product_names: list[str],
) -> bool:
    """Detect explicit usage of a distinct branded product in company evidence."""
    distinct = [
        p.strip()
        for p in product_names
        if p and len(p.strip()) >= 3 and p.strip().lower() not in GENERIC_PRODUCT_WORDS
    ]
    if not distinct:
        return False
    joined = " ".join(f"{e.get('title', '')} {e.get('snippet', '')}" for e in evidence)
    for prod in distinct:
        esc = re.escape(prod)
        usage_re = re.compile(
            rf"(?:\b(?:uses|using|use|powered\s+by|built\s+with|built\s+on|runs\s+on|implemented|deployed|adopted|migrated\s+to|customer\s+of|certified\s+partner\s+for)\s+{esc}\b|\b{esc}\s+(?:customer|user|client|partner|implementation|certified)\b)",
            re.I,
        )
        if usage_re.search(joined):
            return True
    return False


def compute_score_and_tier(
    signals: dict[str, Any],
    total_emp: Union[int, str],
    sales_size: int,
    full_lower: str,
    has_website: bool,
    evidence_count: int,
    interested_buyer: bool = True,
) -> tuple[int, Literal["Hot ICP", "Good ICP", "Nurture", "Deprioritize"], dict[str, int]]:
    has_demand = (
        (bool(signals.get("leads_100_plus_per_month")) and bool(LEAD_GEN_RE.search(full_lower)))
        or (bool(signals.get("existing_crm")) and bool(CRM_RE.search(full_lower)))
    )
    breakdown = {
        "b2b_company": 15 if bool(signals.get("b2b_company", True)) else 0,
        "employees_in_range": 15 if total_emp is not None else 0,
        "sales_team_fit": 15 if sales_size >= 1 else 0,
        "product_buyer_fit": 15 if interested_buyer else 0,
        "meaningful_website_traffic": 10 if (has_website and (evidence_count >= 3 or bool(signals.get("meaningful_website_traffic")))) else 0,
        "growth_or_demand_signal": 10 if has_demand else 0,
        "no_dedicated_revops": 5 if not REVOPS_RE.search(full_lower) else 0,
        "hiring_salespeople": 5 if (bool(signals.get("hiring_salespeople")) and bool(HIRING_RE.search(full_lower))) else 0,
        "long_sales_cycle": 5 if bool(signals.get("long_sales_cycle", True)) else 0,
        "founder_led_sales": 5 if bool(signals.get("founder_led_sales")) else 0,
    }
    score = sum(breakdown.values())
    if score >= 80:
        tier: Literal["Hot ICP", "Good ICP", "Nurture", "Deprioritize"] = "Hot ICP"
    elif score >= 60:
        tier = "Good ICP"
    elif score >= 40:
        tier = "Nurture"
    else:
        tier = "Deprioritize"
    return score, tier, breakdown


def _parse_api_keys(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(k).strip() for k in raw if str(k).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(k).strip() for k in parsed if str(k).strip()]
        except json.JSONDecodeError:
            pass
    if "," in text:
        return [k.strip().strip("\"'") for k in text.strip("[]").split(",") if k.strip().strip("\"'")]
    return [text.strip("\"'")]


class PipelineClients:
    def __init__(self) -> None:
        settings = config.get_all_settings()
        raw_serper = settings.get("SERPER_API_KEY") or os.getenv("SERPER_API_KEY", "")
        self.serper_api_keys: list[str] = _parse_api_keys(raw_serper)
        if not self.serper_api_keys:
            raise ValueError("SERPER_API_KEY is missing from settings / environment.")
        self.llm = LLMRouter(logger)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        self.serper_sem = asyncio.Semaphore(12)
        self.llm_sem = asyncio.Semaphore(8)

    async def close(self) -> None:
        await self.http.aclose()
        await self.llm.close()

    async def serper_search(self, query: str, num: int = 10, page: int = 1, gl: str = "us") -> list[dict[str, str]]:
        country = (gl or "us").strip().lower()
        body: dict[str, Any] = {"q": query, "num": num, "gl": country, "hl": "en"}
        if page > 1:
            body["page"] = page

        errors: list[str] = []
        async with self.serper_sem:
            for k_idx, api_key in enumerate(self.serper_api_keys):
                key_label = f"serper[key#{k_idx + 1}]"
                headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
                switch_key = False

                for attempt in range(3):
                    try:
                        resp = await self.http.post(SERPER_URL, headers=headers, json=body)
                    except Exception as exc:
                        if attempt < 2:
                            await asyncio.sleep(1.0 * (attempt + 1))
                            continue
                        errors.append(f"{key_label}: {exc}")
                        switch_key = True
                        break

                    if resp.status_code in (401, 403):
                        errors.append(f"{key_label}: unauthorized ({resp.status_code})")
                        switch_key = True
                        break

                    if resp.status_code in (429, 500, 502, 503, 504):
                        if attempt < 2:
                            await asyncio.sleep(1.0 * (attempt + 1))
                            continue
                        errors.append(f"{key_label}: HTTP {resp.status_code}")
                        switch_key = True
                        break

                    if resp.status_code >= 400:
                        text_low = resp.text.lower()
                        logger.warning("Serper %s error %s for %s: %s", key_label, resp.status_code, query, resp.text[:160])
                        if "credit" in text_low or "unauthorized" in text_low or "api key" in text_low or "limit" in text_low:
                            errors.append(f"{key_label}: HTTP {resp.status_code}")
                            switch_key = True
                            break
                        resp.raise_for_status()

                    raw = resp.json()
                    out: list[dict[str, str]] = []
                    kg = raw.get("knowledgeGraph")
                    if isinstance(kg, dict) and kg.get("title"):
                        out.append({
                            "title": clean_text(str(kg.get("title", ""))),
                            "link": str(kg.get("website") or ""),
                            "snippet": clean_text(f"{kg.get('type', '')} {kg.get('description', '')} {json.dumps(kg.get('attributes', {}))}"),
                            "query": query,
                        })
                    for item in raw.get("organic", []):
                        t, s = clean_text(str(item.get("title", ""))), clean_text(str(item.get("snippet", "")))
                        if t or s:
                            out.append({"title": t, "link": str(item.get("link", "")).strip(), "snippet": s, "query": query})
                    logger.info("Serper search | %s | gl=%s | page=%s | results=%s | q=%s", key_label, country, page, len(out), query)
                    return out

                if switch_key:
                    if k_idx < len(self.serper_api_keys) - 1:
                        logger.warning("%s failed (%s); switching to serper[key#%d]", key_label, errors[-1], k_idx + 2)
                    else:
                        logger.error("%s failed (%s); exhausted all Serper keys", key_label, errors[-1])

        raise PermissionError("All Serper API keys failed: " + " | ".join(errors))

    async def llm_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        json_schema: dict[str, Any],
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        async with self.llm_sem:
            return await self.llm.json_completion(system_prompt, user_prompt, schema_name, json_schema, max_tokens)


async def parse_icp_prompt(
    clients: PipelineClients,
    icp_prompt: str,
    exclude_companies: Optional[list[str]] = None,
) -> ICPRequirements:
    req_count, regex_min, regex_max = parse_count_and_bounds(icp_prompt)
    schema = {
        "type": "object",
        "properties": {
            "product_offering": {"type": "string"},
            "product_names": {"type": "array", "items": {"type": "string"}},
            "exclude_existing_users": {"type": "boolean"},
            "target_buyer_need": {"type": "string"},
            "target_industries": {"type": "array", "items": {"type": "string"}},
            "target_cities": {"type": "array", "items": {"type": "string"}},
            "suggested_cities": {"type": "array", "items": {"type": "string"}},
            "region_hint": {"type": "string"},
            "search_region": {"type": "string"},
            "country_code": {"type": "string"},
            "location_formats": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string"},
                        "aliases": {"type": "array", "items": {"type": "string"}},
                        "search_location": {"type": "string"},
                    },
                    "required": ["city", "aliases", "search_location"],
                    "additionalProperties": False,
                },
            },
            "regional_directories": {"type": "array", "items": {"type": "string"}},
            "min_employees": {"type": ["integer", "null"]},
            "max_employees": {"type": ["integer", "null"]},
            "search_keywords": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "product_offering",
            "product_names",
            "exclude_existing_users",
            "target_buyer_need",
            "target_industries",
            "target_cities",
            "suggested_cities",
            "region_hint",
            "search_region",
            "country_code",
            "location_formats",
            "regional_directories",
            "min_employees",
            "max_employees",
            "search_keywords",
        ],
        "additionalProperties": False,
    }
    sys_prompt = (
        "Extract structured B2B buyer ICP parameters from the prompt.\n"
        "- product_offering: what the user is selling or pitching.\n"
        "- product_names: 1-4 concise product/brand/tool names for the offered product (e.g. ['DelveAnt', 'AI CRM']).\n"
        "- exclude_existing_users: true by default when selling a product so we find prospective buyers who do NOT already use the product; set false ONLY if the prompt explicitly asks to find companies that already use the product.\n"
        "- target_buyer_need: 1-sentence summary of why target companies would want/need to buy product_offering.\n"
        "- target_industries: industries of the TARGET BUYER companies.\n"
        "- target_cities: ONLY specific cities explicitly named in the prompt (empty list if none named).\n"
        "- suggested_cities: if target_cities is empty and a state/region/country/macro-area is mentioned, list 3-5 major commercial hub cities in that geography; otherwise empty list.\n"
        "- region_hint: full state, province, or country name mentioned (e.g. 'Texas', 'Tamil Nadu', 'United Kingdom', 'Germany'). Use 'United States' ONLY if no location is mentioned.\n"
        "- search_region: compact Google search qualifier for region_hint (e.g. 'TX' for Texas, 'CA' for California, 'UK' for United Kingdom, 'India' for India, or '' if city is self-sufficient).\n"
        "- country_code: 2-letter lowercase ISO 3166-1 alpha-2 country code (e.g. 'us', 'in', 'gb', 'ca', 'au', 'de', 'sg', 'ae'). Use 'gb' for UK.\n"
        "- location_formats: for each city in target_cities or suggested_cities, return city, aliases (local/alternate names like ['Bengaluru', 'Bangalore']), and search_location (ready Google search location string like '\"Austin\" TX' or '\"Bengaluru\" OR \"Bangalore\" India', no site: operators).\n"
        "- regional_directories: 0-2 regional B2B company directory domains suited to the target country (e.g. 'ambitionbox.com' for India, 'northdata.de' for Germany); empty list if none needed.\n"
        "- min_employees / max_employees: integer employee bounds ONLY if explicitly mentioned in the prompt; otherwise null.\n"
        "- search_keywords: 3-6 search phrases describing the TARGET BUYERS' industries/services. When exclude_existing_users=true, NEVER include the seller's product name/brand and NEVER include city or country names."
    )

    raw_country_code: Optional[str] = None
    suggested_cities: list[str] = []
    llm_cities: list[str] = []
    city_aliases: dict[str, list[str]] = {}
    location_search_map: dict[str, str] = {}
    regional_directories: list[str] = []
    product_names: list[str] = []
    exclude_existing_users = True
    target_buyer_need = ""
    search_region = ""
    llm_min_emp: Any = None
    llm_max_emp: Any = None

    try:
        parsed = await clients.llm_json(sys_prompt, f"ICP Prompt:\n{icp_prompt}", "icp_requirements", schema, 1000)
        industries = [s.strip() for s in parsed.get("target_industries", []) if s.strip()][:6] or ["IT services", "software"]
        keywords = [s.strip() for s in parsed.get("search_keywords", []) if s.strip()][:8] or industries
        offering = str(parsed.get("product_offering") or icp_prompt[:200]).strip()
        product_names = [s.strip() for s in parsed.get("product_names", []) if s.strip()][:4]
        exclude_existing_users = bool(parsed.get("exclude_existing_users", True))
        target_buyer_need = str(parsed.get("target_buyer_need") or "").strip()
        llm_cities = [s.strip() for s in parsed.get("target_cities", []) if s.strip()]
        suggested_cities = [s.strip() for s in parsed.get("suggested_cities", []) if s.strip()]
        region_hint = str(parsed.get("region_hint") or "").strip()
        search_region = str(parsed.get("search_region") or "").strip()
        raw_country_code = str(parsed.get("country_code") or "").strip()
        llm_min_emp = parsed.get("min_employees")
        llm_max_emp = parsed.get("max_employees")

        for item in parsed.get("location_formats", []):
            if not isinstance(item, dict):
                continue
            c_name = str(item.get("city") or "").strip()
            if not c_name:
                continue
            aliases = [str(a).strip() for a in item.get("aliases", []) if str(a).strip()]
            if c_name not in aliases:
                aliases.insert(0, c_name)
            city_aliases[c_name.lower()] = aliases
            location_search_map[c_name.lower()] = sanitize_search_location(
                item.get("search_location"), c_name, search_region or region_hint
            )

        for d in parsed.get("regional_directories", []):
            dom = extract_domain(str(d))
            if dom and dom not in regional_directories:
                regional_directories.append(dom)
    except Exception:
        industries = ["IT services", "software"]
        keywords = ["managed IT services", "software development", "IT consulting", "B2B SaaS"]
        offering = icp_prompt[:250].strip()
        region_hint = ""
        matched_places = [
            place.title()
            for place in sorted(LOCATION_TO_COUNTRY_CODE, key=len, reverse=True)
            if re.search(rf"\b{re.escape(place)}\b", icp_prompt, re.I)
        ]
        if matched_places:
            region_hint = matched_places[0]

    if EXISTING_USERS_INTENT_RE.search(icp_prompt) and not EXCLUDE_USERS_INTENT_RE.search(icp_prompt):
        exclude_existing_users = False

    explicit_cities = bool(llm_cities)
    if explicit_cities:
        cities = llm_cities[:6]
    elif suggested_cities:
        cities = suggested_cities[:6]
    elif region_hint:
        cities = [region_hint]
    else:
        region_hint = "United States"
        search_region = search_region or "USA"
        cities = list(DEFAULT_FALLBACK_CITIES)

    if not region_hint:
        region_hint = cities[0] if cities else "United States"
    if not search_region:
        search_region = region_hint
    for c in cities:
        location_search_map.setdefault(c.lower(), sanitize_search_location(None, c, search_region))

    min_e, max_e = resolve_employee_bounds(regex_min, regex_max, llm_min_emp, llm_max_emp)
    country_code = resolve_country_code(raw_country_code, region_hint, cities, icp_prompt)

    return ICPRequirements(
        product_offering=offering,
        product_names=product_names,
        exclude_existing_users=exclude_existing_users,
        target_buyer_need=target_buyer_need,
        target_industries=industries,
        target_cities=cities,
        region_hint=region_hint,
        search_region=search_region,
        country_code=country_code,
        explicit_cities=explicit_cities,
        city_aliases=city_aliases,
        location_search_map=location_search_map,
        regional_directories=regional_directories[:2],
        min_employees=min_e,
        max_employees=max_e,
        requested_count=req_count,
        search_keywords=keywords,
        exclude_companies=exclude_companies or [],
    )


def build_queries(icp: ICPRequirements, wave: int, seen: set[str]) -> list[tuple[str, int]]:
    terms = list(dict.fromkeys(icp.search_keywords + icp.target_industries))
    p_clause = " OR ".join(f'"{t}"' for t in terms[:3])
    s_clause = " OR ".join(f'"{t}"' for t in (terms[3:6] or terms[:3]))
    broad_clause = " OR ".join(f'"{t}"' for t in terms[:4])

    li_emp = linkedin_size_clause(icp.min_employees, icp.max_employees)
    clutch_emp = clutch_size_clause(icp.min_employees, icp.max_employees)
    reg = icp.search_region or icp.region_hint

    base_dirs = (
        ["bbb.org", "goodfirms.co", "designrush.com"]
        if icp.country_code in {"us", "ca", "mx"}
        else ["goodfirms.co", "designrush.com", "crunchbase.com/organization"]
    )
    for rd in icp.regional_directories:
        if rd not in base_dirs:
            base_dirs.append(rd)
    na_dirs = " OR ".join(f"site:{d}" for d in base_dirs[:4])
    alt_dir = (
        "site:crunchbase.com/organization OR site:bbb.org"
        if icp.country_code in {"us", "ca", "mx"}
        else " OR ".join(f"site:{d}" for d in (["crunchbase.com/organization", "goodfirms.co"] + icp.regional_directories)[:3])
    )

    planned: list[tuple[str, int]] = []
    for city in icp.target_cities:
        loc_q = icp.location_search_map.get(city.lower()) or sanitize_search_location(None, city, reg)
        region_full = f"{icp.region_hint} " if icp.region_hint and icp.region_hint.lower() != city.lower() else ""
        if wave == 1:
            cands = [
                (f"site:clutch.co {loc_q} {clutch_emp} ({p_clause})", 1),
                (f'site:cloudtango.net "{city}" {region_full}'.strip(), 1),
                (f"site:linkedin.com/company {loc_q} {li_emp} ({p_clause})", 1),
                (f"(site:zoominfo.com OR site:apollo.io) {loc_q} ({p_clause})", 1),
            ]
        elif wave == 2:
            cands = [
                (f"site:linkedin.com/company {loc_q} ({s_clause}) {li_emp}", 1),
                (f"site:clutch.co/profile {loc_q} {clutch_emp} ({s_clause})", 1),
                (f"({na_dirs}) {loc_q} ({p_clause})", 1),
                (f"{loc_q} ({li_emp} OR {clutch_emp}) ({p_clause})", 1),
            ]
        else:
            pg = max(1, wave - 1)
            cands = [
                (f"site:clutch.co {loc_q} {clutch_emp} ({broad_clause})", pg),
                (f"site:linkedin.com/company {loc_q} {li_emp} ({broad_clause})", pg),
                (f'site:cloudtango.net "{city}" {region_full}'.strip(), pg),
                (f"({alt_dir}) {loc_q} ({p_clause})", 1),
            ]
        for q, p in cands:
            key = f"{p}:{q}"
            if key not in seen:
                seen.add(key)
                planned.append((q, p))

    if not icp.explicit_cities and icp.region_hint and wave >= 2:
        extra_q = f'site:linkedin.com/company "{icp.region_hint}" {li_emp} ({p_clause})'
        key = f"{max(1, wave - 1)}:{extra_q}"
        if key not in seen:
            seen.add(key)
            planned.append((extra_q, max(1, wave - 1)))
    return planned


async def extract_candidates(
    clients: PipelineClients,
    icp: ICPRequirements,
    raw_results: list[dict[str, str]],
) -> list[dict[str, Any]]:
    deduped, seen = [], set()
    for item in raw_results:
        k = f"{item.get('link', '')}|{item.get('title', '')}"
        if k not in seen:
            seen.add(k)
            deduped.append(item)
    if not deduped:
        return []

    city_schema: dict[str, Any] = (
        {"type": "string", "enum": list(icp.target_cities)} if icp.explicit_cities else {"type": "string"}
    )
    schema = {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "company_name": {"type": "string"},
                        "city": city_schema,
                        "industry_niche": {"type": "string"},
                        "employee_signal": {"type": ["string", "null"]},
                        "already_uses_product": {"type": "boolean"},
                        "source_indices": {"type": "array", "items": {"type": "integer"}},
                    },
                    "required": [
                        "company_name",
                        "city",
                        "industry_niche",
                        "employee_signal",
                        "already_uses_product",
                        "source_indices",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["candidates"],
        "additionalProperties": False,
    }
    city_rule = (
        f"1. City MUST be one of: {', '.join(icp.target_cities)}."
        if icp.explicit_cities
        else f"1. City MUST be located in {icp.region_hint} (e.g. {', '.join(icp.target_cities)})."
    )
    usage_rule = (
        "5. Set already_uses_product=true if snippet shows the company ALREADY uses/adopted the Product Offering (exclude them); we want prospective buyers who do NOT already use it."
        if icp.exclude_existing_users
        else "5. Set already_uses_product=true if snippet shows the company currently uses the Product Offering."
    )
    sys_prompt = (
        "Extract ONLY real B2B BUYER companies explicitly named in the search results.\n"
        f"{city_rule}\n"
        f"2. Strictly EXCLUDE companies outside the {icp.min_employees}-{icp.max_employees} employee range (and solo founders/freelancers if min > 1).\n"
        "3. Exclude directories, marketplaces, job boards, and listicle titles.\n"
        "4. Extract companies operating in Target Buyer Industries that are realistic buyers for Product Offering. Reject competitors selling the same product.\n"
        f"{usage_rule}\n"
        "6. Cite exact source_indices [0..N-1]."
    )

    async def run_chunk(chunk: list[dict[str, str]]) -> list[dict[str, Any]]:
        lines = [f"[{i}] Title: {r['title']} | URL: {r['link']} | Snippet: {r['snippet']}" for i, r in enumerate(chunk)]
        user_p = (
            f"Product Offering: {icp.product_offering}\n"
            f"Product Names: {', '.join(icp.product_names) or icp.product_offering}\n"
            f"Target Buyer Industries: {', '.join(icp.target_industries)}\n\n"
            + "\n".join(lines)
        )
        try:
            resp = await clients.llm_json(sys_prompt, user_p, "stage2_candidates", schema, 2500)
        except Exception:
            return []
        valid: list[dict[str, Any]] = []
        for c in resp.get("candidates", []):
            name = norm_str(c.get("company_name"))
            city = (
                match_city(c.get("city"), icp.target_cities, icp.city_aliases)
                if icp.explicit_cities
                else (match_city(c.get("city"), icp.target_cities, icp.city_aliases) or norm_str(c.get("city")))
            )
            if not name or not city or re.search(r"\b(?:top\s+\d+|best\s+\d+|companies\s+in|directory|agencies\s+in)\b", name, re.I):
                continue
            already_uses = bool(c.get("already_uses_product", False))
            if icp.exclude_existing_users and already_uses:
                continue
            idxs = [i for i in c.get("source_indices", []) if isinstance(i, int) and 0 <= i < len(chunk)]
            if not idxs:
                continue
            ev = [chunk[i] for i in idxs]
            txt = " ".join(f"{e['title']} {e['snippet']} {e['link']}" for e in ev)
            toks = company_tokens(name)
            if toks and not any(t in txt.lower() for t in toks):
                continue
            if size_disqualified(txt, icp.min_employees, icp.max_employees):
                continue
            if icp.exclude_existing_users and already_uses_product_in_evidence(ev, icp.product_names):
                continue
            valid.append({
                "company_name": name,
                "city": city,
                "industry_niche": norm_str(c.get("industry_niche")) or icp.target_industries[0],
                "employee_signal": norm_str(c.get("employee_signal")),
                "evidence": ev,
            })
        return valid

    batches = await asyncio.gather(*(run_chunk(deduped[i : i + 8]) for i in range(0, len(deduped), 8)))
    return [item for sub in batches for item in sub]


def deduplicate_and_interleave(
    candidates: list[dict[str, Any]],
    processed_keys: set[str],
    cities: list[str],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for cand in candidates:
        key = company_key(cand["company_name"])
        if not key or key in processed_keys or any(fuzz.token_sort_ratio(key, pk) >= 88 for pk in processed_keys):
            continue
        target = next(
            (e for e in merged if key == e["norm_key"] or (e["city"] == cand["city"] and fuzz.token_sort_ratio(key, e["norm_key"]) >= 88)),
            None,
        )
        if target:
            seen_links = {e["link"] for e in target["evidence"]}
            target["evidence"].extend(ev for ev in cand["evidence"] if ev["link"] not in seen_links)
            target["employee_signal"] = target.get("employee_signal") or cand.get("employee_signal")
        else:
            merged.append({**cand, "norm_key": key})

    buckets: dict[str, list[dict[str, Any]]] = {c: [] for c in cities}
    extra: list[dict[str, Any]] = []
    for item in merged:
        (buckets[item["city"]] if item["city"] in buckets else extra).append(item)
    ordered: list[dict[str, Any]] = []
    while any(buckets.values()):
        for c in cities:
            if buckets[c]:
                ordered.append(buckets[c].pop(0))
    return ordered + extra


async def enrich_candidate(
    clients: PipelineClients,
    cand: dict[str, Any],
    icp: ICPRequirements,
) -> dict[str, Any]:
    core = core_company_name(cand["company_name"])
    city = cand["city"]
    region = icp.search_region or icp.region_hint
    country_code = icp.country_code
    reg_q = f"{region} " if region and region.lower() != city.lower() else ""
    reg_or = f" OR {region}" if region and region.lower() != city.lower() else ""
    dir_sites = (
        "site:zoominfo.com OR site:apollo.io OR site:bbb.org OR site:clutch.co"
        if country_code in {"us", "ca", "mx"}
        else "site:zoominfo.com OR site:apollo.io OR site:clutch.co OR site:crunchbase.com"
    )
    prod_hint = ""
    if icp.product_names:
        first_prod = re.sub(r'["()]', "", icp.product_names[0]).strip()
        if 2 <= len(first_prod) <= 30:
            prod_hint = f' OR "{first_prod}"'
    queries = [
        f'"{core}" "{city}" {reg_q}(official website OR phone OR contact OR hiring OR software{prod_hint})',
        f'site:linkedin.com/company "{core}" "{city}"{reg_or}',
        f'site:linkedin.com/in "{core}" (CEO OR CTO OR Founder OR President OR "Head of Sales" OR "VP of Sales")',
        f'"{core}" "{city}" ({dir_sites})',
    ]
    batches = await asyncio.gather(*(clients.serper_search(q, num=6, gl=country_code) for q in queries), return_exceptions=True)
    evidence = list(cand["evidence"])
    seen = {(e.get("link", ""), e.get("title", "")) for e in evidence}
    for b in batches:
        if isinstance(b, PermissionError):
            raise b
        if isinstance(b, Exception):
            continue
        for item in b:
            sig = (item.get("link", ""), item.get("title", ""))
            if sig not in seen:
                seen.add(sig)
                evidence.append(item)
    return {**cand, "evidence": evidence}


def verify_company_linkedin(url: Optional[str], evidence: list[dict[str, str]], company_name: str) -> Optional[str]:
    toks = company_tokens(company_name)
    compact = re.sub(r"[^a-z0-9]", "", core_company_name(company_name).lower())
    candidates: list[tuple[int, str]] = []
    for ev in evidence:
        for raw in [ev.get("link", "")] + [m.group(0) for m in LI_CO_RE.finditer(ev.get("snippet", ""))]:
            li = norm_li_url(raw, "company")
            if not li:
                continue
            slug = li.rsplit("/", 1)[-1].replace("-", " ")
            title_low = ev.get("title", "").lower()
            slug_compact = slug.replace(" ", "")
            if (len(compact) >= 3 and compact in slug_compact) or any(t in slug or t in title_low for t in toks):
                score = fuzz.token_sort_ratio(company_key(company_name), slug)
                if url and li == url:
                    score += 15
                candidates.append((score, li))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def verify_contact_profile(
    raw_name: Optional[str],
    raw_title: Optional[str],
    raw_li: Optional[str],
    evidence: list[dict[str, str]],
    company_name: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    toks = company_tokens(company_name)
    verified_profiles: list[tuple[str, str, str]] = []
    for ev in evidence:
        li = norm_li_url(ev.get("link", ""), "in")
        if not li:
            continue
        title = ev.get("title", "")
        combined = f"{title} {ev.get('snippet', '')}".lower()
        if toks and not any(t in combined for t in toks):
            continue
        parts = [p.strip() for p in re.split(r"\s*[-–|]\s*", title) if p.strip()]
        if len(parts) >= 2 and re.fullmatch(r"[A-Za-z][A-Za-z.'\- ]{2,40}", parts[0]):
            p_name = parts[0]
            p_role = parts[1] if "linkedin" not in parts[1].lower() else ""
            if p_role and ROLE_RE.search(f"{p_role} {combined}"):
                verified_profiles.append((p_name, p_role, li))

    if raw_name:
        name_toks = [t for t in re.findall(r"[a-z]+", raw_name.lower()) if len(t) >= 2]
        if len(name_toks) >= 2:
            for p_name, p_role, p_li in verified_profiles:
                if all(t in p_name.lower() for t in name_toks):
                    return raw_name, raw_title or p_role, p_li
            for ev in evidence:
                ev_low = f"{ev.get('title', '')} {ev.get('snippet', '')}".lower()
                if all(t in ev_low for t in name_toks) and (not toks or any(t in ev_low for t in toks)):
                    li = norm_li_url(raw_li, "in")
                    if li and norm_li_url(ev.get("link", ""), "in") == li:
                        return raw_name, raw_title, li
                    if raw_title and ROLE_RE.search(raw_title):
                        return raw_name, raw_title, None

    if verified_profiles:
        return verified_profiles[0]
    return None, None, None


def extract_artifacts(
    evidence: list[dict[str, str]],
    company_name: str,
    country_code: str = "us",
) -> dict[str, Any]:
    toks = company_tokens(company_name)
    websites, phones, full_parts = [], [], []
    for ev in evidence:
        link, title, snippet = ev.get("link", ""), ev.get("title", ""), ev.get("snippet", "")
        full_parts.append(f"{title} {snippet} {link}")
        for raw_url in [link] + [u.rstrip(".,;:!?") for u in URL_RE.findall(snippet)]:
            site = norm_website(raw_url)
            if site:
                dom_compact = re.sub(r"[^a-z0-9]", "", extract_domain(site) or "")
                if not toks or any(t in dom_compact for t in toks):
                    if site not in websites:
                        websites.append(site)
        for m in PHONE_RE.findall(f"{title} {snippet}"):
            if d := norm_phone(m, country_code):
                fp = fmt_phone(d, raw_text=m, country_code=country_code)
                if fp not in phones:
                    phones.append(fp)
    full_text = " ".join(full_parts)
    return {
        "websites": websites,
        "phones": phones,
        "full_text": full_text,
        "full_text_lower": full_text.lower(),
        "full_digits": re.sub(r"\D", "", full_text),
    }


def _find_qualifying_emp_in_text(text: str, min_e: int, max_e: int) -> Optional[Union[int, str]]:
    for lo, hi in _extract_emp_spans(text):
        candidate = str(lo) if lo == hi else f"{lo}-{hi}"
        if parsed := parse_employee_value(candidate, min_e, max_e):
            return parsed
    return None


def verify_and_meta_check(
    result: dict[str, Any],
    candidate: dict[str, Any],
    artifacts: dict[str, Any],
    icp: ICPRequirements,
) -> Optional[LeadRecord]:
    if not result.get("qualified") or result.get("is_competitor") or not result.get("is_target_industry_buyer", True):
        logger.info("Meta-check rejected %s (%s): %s", candidate["company_name"], candidate["city"], result.get("rejection_reason"))
        return None

    already_uses = bool(result.get("already_uses_product", False))
    interested_buyer = bool(result.get("interested_buyer", True))
    if icp.exclude_existing_users:
        if already_uses or already_uses_product_in_evidence(candidate["evidence"], icp.product_names):
            logger.info("Rejected %s (%s): already uses product (%s)", candidate["company_name"], candidate["city"], icp.product_offering)
            return None
        if not interested_buyer:
            logger.info("Rejected %s (%s): low/no product buying fit", candidate["company_name"], candidate["city"])
            return None
    else:
        if not already_uses and not already_uses_product_in_evidence(candidate["evidence"], icp.product_names):
            logger.info("Rejected %s (%s): not confirmed as existing product user", candidate["company_name"], candidate["city"])
            return None

    raw = result.get("lead") or {}
    company_name = norm_str(raw.get("company_name")) or candidate["company_name"]
    if any(fuzz.token_sort_ratio(company_key(company_name), company_key(ex)) >= 88 for ex in icp.exclude_companies):
        logger.info("Excluded previously used company: %s", company_name)
        return None

    if icp.explicit_cities:
        city = (
            match_city(raw.get("city"), icp.target_cities, icp.city_aliases)
            or match_city(candidate["city"], icp.target_cities, icp.city_aliases)
        )
    else:
        city = (
            match_city(raw.get("city"), icp.target_cities, icp.city_aliases)
            or norm_str(raw.get("city"))
            or norm_str(candidate["city"])
        )
    if not city or not location_in_text(
        city,
        icp.region_hint,
        artifacts["full_text_lower"],
        icp.explicit_cities,
        icp.city_aliases,
        icp.search_region,
    ):
        return None

    if linkedin_company_disqualified(candidate["evidence"], icp.min_employees, icp.max_employees) or size_disqualified(
        artifacts["full_text"], icp.min_employees, icp.max_employees
    ):
        logger.info("Headcount guard rejected %s (%s): outside %s-%s range", company_name, city, icp.min_employees, icp.max_employees)
        return None

    emp = (
        parse_employee_value(raw.get("total_employees"), icp.min_employees, icp.max_employees)
        or parse_employee_value(candidate.get("employee_signal"), icp.min_employees, icp.max_employees)
        or _find_qualifying_emp_in_text(artifacts["full_text"], icp.min_employees, icp.max_employees)
    )
    if emp is None:
        return None

    sales_size = model_sales_size(emp, raw.get("estimated_sales_team_size"), icp.min_employees, icp.max_employees)
    industry = norm_str(raw.get("industry_niche")) or candidate["industry_niche"]

    website = norm_website(norm_str(raw.get("company_website")))
    if not website or (extract_domain(website) or "") not in artifacts["full_text_lower"]:
        website = artifacts["websites"][0] if artifacts["websites"] else None

    co_linkedin = verify_company_linkedin(norm_li_url(norm_str(raw.get("company_linkedin_url")), "company"), candidate["evidence"], company_name)
    c_name, c_title, c_li = verify_contact_profile(
        norm_str(raw.get("contact_person_name")),
        norm_str(raw.get("contact_person_title")),
        norm_str(raw.get("contact_person_linkedin_url")),
        candidate["evidence"],
        company_name,
    )

    raw_co_phone = norm_str(raw.get("company_phone_number"))
    co_digits = norm_phone(raw_co_phone, icp.country_code)
    co_phone = (
        fmt_phone(co_digits, raw_text=raw_co_phone, country_code=icp.country_code)
        if (co_digits and co_digits in artifacts["full_digits"])
        else (artifacts["phones"][0] if artifacts["phones"] else None)
    )

    c_phone: Optional[str] = None
    if c_name:
        raw_c_phone = norm_str(raw.get("contact_phone_number"))
        c_digits = norm_phone(raw_c_phone, icp.country_code)
        name_toks = [t for t in re.findall(r"[a-z]+", c_name.lower()) if len(t) >= 2]
        person_digits = re.sub(
            r"\D",
            "",
            " ".join(
                f"{ev.get('title', '')} {ev.get('snippet', '')}"
                for ev in candidate["evidence"]
                if LI_IN_RE.search(ev.get("link", "")) or all(t in f"{ev.get('title', '')} {ev.get('snippet', '')}".lower() for t in name_toks)
            ),
        )
        if c_digits and c_digits in person_digits:
            c_phone = fmt_phone(c_digits, raw_text=raw_c_phone, country_code=icp.country_code)
            if not co_phone:
                co_phone = c_phone
        if not c_phone and co_phone:
            c_phone = co_phone

    score, tier, breakdown = compute_score_and_tier(
        signals=result.get("icp_signals") or {},
        total_emp=emp,
        sales_size=sales_size,
        full_lower=artifacts["full_text_lower"],
        has_website=bool(website),
        evidence_count=len(candidate["evidence"]),
        interested_buyer=interested_buyer,
    )
    reason = norm_str(result.get("suitability_reason")) or f"{company_name} is a {city}-based {industry} company ({emp} employees, ~{sales_size} sales reps)."

    record = LeadRecord(
        company_name=company_name,
        city=city,
        industry_niche=industry,
        total_employees=emp,
        estimated_sales_team_size=sales_size,
        company_phone_number=co_phone,
        company_website=website,
        company_linkedin_url=co_linkedin,
        contact_person_name=c_name,
        contact_person_title=c_title,
        contact_person_linkedin_url=c_li,
        contact_phone_number=c_phone,
        icp_match_score=score,
        icp_tier=tier,
        suitability_reason=reason,
    )
    logger.info(
        "Meta-check QUALIFIED %s (%s) | emp=%s | score=%s (%s) | contact=%s | co_li=%s | breakdown=%s",
        company_name, city, emp, score, tier, c_name, co_linkedin, breakdown,
    )
    return record


async def qualify_candidate(clients: PipelineClients, icp: ICPRequirements, cand: dict[str, Any]) -> Optional[LeadRecord]:
    if linkedin_company_disqualified(cand["evidence"], icp.min_employees, icp.max_employees):
        return None
    artifacts = extract_artifacts(cand["evidence"], cand["company_name"], icp.country_code)
    if size_disqualified(artifacts["full_text"], icp.min_employees, icp.max_employees):
        return None
    if icp.exclude_existing_users and already_uses_product_in_evidence(cand["evidence"], icp.product_names):
        return None

    city_schema: dict[str, Any] = (
        {"type": "string", "enum": list(icp.target_cities)} if icp.explicit_cities else {"type": "string"}
    )
    schema = {
        "type": "object",
        "properties": {
            "qualified": {"type": "boolean"},
            "is_competitor": {"type": "boolean"},
            "is_target_industry_buyer": {"type": "boolean"},
            "already_uses_product": {"type": "boolean"},
            "interested_buyer": {"type": "boolean"},
            "rejection_reason": {"type": ["string", "null"]},
            "suitability_reason": {"type": "string"},
            "icp_signals": {
                "type": "object",
                "properties": {
                    "b2b_company": {"type": "boolean"},
                    "existing_crm": {"type": "boolean"},
                    "meaningful_website_traffic": {"type": "boolean"},
                    "leads_100_plus_per_month": {"type": "boolean"},
                    "no_dedicated_revops": {"type": "boolean"},
                    "hiring_salespeople": {"type": "boolean"},
                    "long_sales_cycle": {"type": "boolean"},
                    "founder_led_sales": {"type": "boolean"},
                },
                "required": [
                    "b2b_company", "existing_crm", "meaningful_website_traffic",
                    "leads_100_plus_per_month", "no_dedicated_revops", "hiring_salespeople",
                    "long_sales_cycle", "founder_led_sales",
                ],
                "additionalProperties": False,
            },
            "lead": {
                "type": "object",
                "properties": {
                    "company_name": {"type": "string"},
                    "city": city_schema,
                    "industry_niche": {"type": "string"},
                    "total_employees": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                    "estimated_sales_team_size": {"type": "integer"},
                    "company_phone_number": {"type": ["string", "null"]},
                    "company_website": {"type": ["string", "null"]},
                    "company_linkedin_url": {"type": ["string", "null"]},
                    "contact_person_name": {"type": ["string", "null"]},
                    "contact_person_title": {"type": ["string", "null"]},
                    "contact_person_linkedin_url": {"type": ["string", "null"]},
                    "contact_phone_number": {"type": ["string", "null"]},
                },
                "required": [
                    "company_name", "city", "industry_niche", "total_employees",
                    "estimated_sales_team_size", "company_phone_number", "company_website",
                    "company_linkedin_url", "contact_person_name", "contact_person_title",
                    "contact_person_linkedin_url", "contact_phone_number",
                ],
                "additionalProperties": False,
            },
        },
        "required": [
            "qualified", "is_competitor", "is_target_industry_buyer",
            "already_uses_product", "interested_buyer", "rejection_reason",
            "suitability_reason", "icp_signals", "lead",
        ],
        "additionalProperties": False,
    }

    loc_desc = ", ".join(icp.target_cities) if icp.explicit_cities else icp.region_hint
    usage_policy = (
        "- Set already_uses_product=true if evidence shows this company ALREADY uses, has deployed, or is an existing customer/user/partner of Product Offering (or Product Names). Reject such companies (qualified=false).\n"
        "- Set interested_buyer=true ONLY if this company is a strong prospective B2B buyer with a clear need/use-case to buy Product Offering."
        if icp.exclude_existing_users
        else "- Set already_uses_product=true if evidence confirms this company currently uses Product Offering (or Product Names), and qualified=true only when confirmed."
    )
    sys_prompt = (
        "You are a senior B2B Marketing Researcher performing strict ICP meta-verification.\n"
        "The user SELLS the Product Offering TO prospective B2B BUYERS in Target Buyer Industries.\n"
        f"- Active office in {loc_desc} ({icp.region_hint}); employees strictly {icp.min_employees}-{icp.max_employees}.\n"
        f"- REJECT any company outside {icp.min_employees}-{icp.max_employees} employees.\n"
        "- Set is_competitor=true if the candidate's own primary product directly competes with Product Offering.\n"
        "- Set is_target_industry_buyer=false if it is a nonprofit or outside Target Buyer Industries.\n"
        f"{usage_policy}\n"
        "- Extract contact_person_name, contact_person_title (CEO, CTO, Founder, President, VP/Head/Director/Lead of Sales), and contact_person_linkedin_url ONLY if verified in evidence for THIS company.\n"
        "- Copy URLs, phones, and names verbatim from evidence; NEVER guess or assume; use null for missing fields.\n"
        "- Score signals true ONLY when supported by evidence. Write a concrete 1-2 sentence suitability_reason grounded in evidence."
    )
    ev_text = "\n".join(f"[{i}] {e['title']} | {e['link']} | {e['snippet']}" for i, e in enumerate(cand["evidence"][:18]))
    user_p = (
        f"Product Offering: {icp.product_offering}\n"
        f"Product Names: {', '.join(icp.product_names) or icp.product_offering}\n"
        f"Target Buyer Need: {icp.target_buyer_need or icp.product_offering}\n"
        f"Target Buyer Industries: {', '.join(icp.target_industries)}\n"
        f"Candidate: {cand['company_name']} ({cand['city']})\nWebsites: {artifacts['websites']}\nPhones: {artifacts['phones']}\n\n{ev_text}"
    )
    try:
        res = await clients.llm_json(sys_prompt, user_p, "qualified_lead", schema, 2400)
        return verify_and_meta_check(res, cand, artifacts, icp)
    except Exception as exc:
        logger.warning("Stage 4 error for %s: %s", cand["company_name"], exc)
        return None


def completeness_score(lead: LeadRecord) -> int:
    return (
        (2 if lead.company_website else 0)
        + (2 if lead.company_linkedin_url else 0)
        + (2 if lead.company_phone_number else 0)
        + (3 if lead.contact_person_name and lead.contact_person_title else 0)
        + (2 if lead.contact_person_linkedin_url else 0)
        + (1 if lead.contact_phone_number else 0)
    )


def is_dup_lead(lead: LeadRecord, existing: list[LeadRecord]) -> bool:
    key, dom = company_key(lead.company_name), extract_domain(lead.company_website or "")
    return any(
        fuzz.token_sort_ratio(key, company_key(x.company_name)) >= 88
        or (dom and dom == extract_domain(x.company_website or ""))
        or (lead.company_linkedin_url and lead.company_linkedin_url == x.company_linkedin_url)
        for x in existing
    )


def select_top_leads(leads: list[LeadRecord], cities: list[str], count: int) -> list[LeadRecord]:
    ranked = sorted(leads, key=lambda l: (l.icp_match_score, completeness_score(l)), reverse=True)
    by_city: dict[str, list[LeadRecord]] = {c: [] for c in cities}
    extra: list[LeadRecord] = []
    for lead in ranked:
        (by_city[lead.city] if lead.city in by_city else extra).append(lead)
    selected: list[LeadRecord] = []
    while len(selected) < count:
        progressed = False
        for c in cities:
            if by_city[c] and len(selected) < count:
                selected.append(by_city[c].pop(0))
                progressed = True
        if not progressed:
            break
    selected.extend(extra[: max(0, count - len(selected))])
    return sorted(selected[:count], key=lambda l: (l.icp_match_score, completeness_score(l)), reverse=True)


async def discover_leads(
    icp_prompt: str,
    exclude_companies: Optional[list[str]] = None,
) -> LeadDiscoveryResponse:
    if not icp_prompt or not icp_prompt.strip():
        raise ValueError("icp_prompt must not be empty.")
    t0 = time.perf_counter()
    config.load_settings()
    clients = PipelineClients()
    try:
        icp = await parse_icp_prompt(clients, icp_prompt, exclude_companies)
        logger.info(
            "ICP parsed | count=%s | emp=%s-%s | exclude_existing=%s | products=%s | region=%s (%s) | gl=%s | explicit_cities=%s | cities=%s | keywords=%s",
            icp.requested_count, icp.min_employees, icp.max_employees,
            icp.exclude_existing_users, icp.product_names,
            icp.region_hint, icp.search_region, icp.country_code, icp.explicit_cities,
            icp.target_cities, icp.search_keywords,
        )
        verified: list[LeadRecord] = []
        processed_keys: set[str] = {company_key(c) for c in icp.exclude_companies if company_key(c)}
        seen_queries: set[str] = set()

        for wave in range(1, 6):
            if len(verified) >= icp.requested_count:
                break
            queries = build_queries(icp, wave, seen_queries)
            if not queries:
                break
            logger.info("Wave %s Stage 1 starting | queries=%s", wave, len(queries))
            outputs = await asyncio.gather(*(clients.serper_search(q, num=10, page=p, gl=icp.country_code) for q, p in queries), return_exceptions=True)
            raw_results: list[dict[str, str]] = []
            for out in outputs:
                if isinstance(out, PermissionError):
                    raise out
                if not isinstance(out, Exception):
                    raw_results.extend(out)
            candidates = await extract_candidates(clients, icp, raw_results)
            unique_cands = deduplicate_and_interleave(candidates, processed_keys, icp.target_cities)
            logger.info("Wave %s Stage 2 | raw=%s | extracted=%s | unique_new=%s", wave, len(raw_results), len(candidates), len(unique_cands))

            idx = 0
            while idx < len(unique_cands) and len(verified) < icp.requested_count:
                batch_size = max((icp.requested_count - len(verified)) + 4, 4)
                batch = unique_cands[idx : idx + batch_size]
                idx += batch_size
                for c in batch:
                    processed_keys.add(c["norm_key"])
                enriched = await asyncio.gather(*(enrich_candidate(clients, c, icp) for c in batch), return_exceptions=True)
                valid_enriched = []
                for item in enriched:
                    if isinstance(item, PermissionError):
                        raise item
                    if not isinstance(item, Exception):
                        valid_enriched.append(item)
                qualified = await asyncio.gather(*(qualify_candidate(clients, icp, c) for c in valid_enriched))
                for lead in qualified:
                    if lead and not is_dup_lead(lead, verified):
                        verified.append(lead)
                logger.info("Wave %s batch complete | verified=%s/%s", wave, len(verified), icp.requested_count)

        final_leads = select_top_leads(verified, icp.target_cities, icp.requested_count)
        logger.info("Pipeline finished | matched_count=%s | elapsed=%.2fs", len(final_leads), time.perf_counter() - t0)
        return LeadDiscoveryResponse(matched_count=len(final_leads), leads=final_leads)
    finally:
        await clients.close()
