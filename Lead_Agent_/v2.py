import asyncio
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
import json_repair
from bs4 import BeautifulSoup
from ddgs import DDGS
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

load_dotenv()


def setup_logger(name: str = "LeadFinderApp") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    return logger


logger = setup_logger()

DIRECTORY_SEARCH_TARGETS = [
    "clutch.co/developers",
    "clutch.co/it-services",
    "clutch.co/agencies",
    "clutch.co/consulting",
    "goodfirms.co/directory",
    "goodfirms.co/it-services",
    "goodfirms.co/business-services",
    "designrush.com/agency",
    "sortlist.com",
    "g2.com/categories",
    "capterra.com/categories",
    "wellfound.com",
    "ycombinator.com/companies",
    "thomasnet.com",
    "kompass.com",
]

DIRECTORY_PROFILE_PATTERNS = (
    "goodfirms.co/company/",
    "clutch.co/profile/",
    "designrush.com/agency/profile/",
    "sortlist.com/agency/",
    "g2.com/products/",
    "capterra.com/p/",
    "crunchbase.com/organization/",
    "wellfound.com/company/",
    "ycombinator.com/companies/",
)

BLOCKED_DOMAINS = {
    "linkedin.com",
    "clutch.co",
    "g2.com",
    "capterra.com",
    "crunchbase.com",
    "wellfound.com",
    "ycombinator.com",
    "producthunt.com",
    "designrush.com",
    "goodfirms.co",
    "sortlist.com",
    "glassdoor.com",
    "ambitionbox.com",
    "zoominfo.com",
    "kompass.com",
    "thomasnet.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "wikipedia.org",
    "bloomberg.com",
    "forbes.com",
    "techcrunch.com",
    "yelp.com",
    "bbb.org",
    "google.com",
    "microsoft.com",
    "amazon.com",
    "apple.com",
    "meta.com",
    "salesforce.com",
    "hubspot.com",
    "oracle.com",
    "ibm.com",
    "accenture.com",
    "deloitte.com",
    "pwc.com",
    "ey.com",
    "kpmg.com",
    "infosys.com",
    "tcs.com",
    "wipro.com",
    "cognizant.com",
    "capgemini.com",
    "owler.com",
    "pitchbook.com",
    "dnb.com",
    "apollo.io",
    "rocketreach.co",
    "signalhire.com",
    "lusha.com",
    "builtin.com",
    "builtinaustin.com",
    "builtindallas.com",
    "builtinhouston.com",
    "indeed.com",
    "ziprecruiter.com",
    "medium.com",
    "substack.com",
    "github.com",
    "reddit.com",
    "inc.com",
    "fastcompany.com",
    "entrepreneur.com",
    "businessinsider.com",
    "cnbc.com",
    "reuters.com",
    "prnewswire.com",
    "businesswire.com",
    "saastock.com",
    "growthlist.co",
    "plugandplaytechcenter.com",
    "capitalfactory.com",
    "gregslist.com",
    "texasborderbusiness.com",
    "findhelp.org",
    "wixsite.com",
    "wordpress.com",
    "blogspot.com",
    "squarespace.com",
}

INVALID_NAME_PATTERN = re.compile(
    r"(\btop\s+\d+|\bbest\s+\d+|\d+\+?\s+funded|\bstartups?\s+in\b|\bcompanies\s+in\b|"
    r"\bagencies\s+in\b|\bfirms\s+in\b|\bfor\s+sale\b|\bstate\s+agenc|\bboards\s+and\s+commissions|"
    r"\blist\s+of\b|\bdirectory\b|\bblog\b|\bnews\b|\bconference\b|\bsummit\b|\baccelerator\b|\bventure\s+capital\b|"
    r"\breviews?\b|\brankings?\b|\bsponsored\b|\bget\s+a\s+quote\b)",
    re.I,
)

SOFT_404_PATTERN = re.compile(
    r"(404\s*(error|not\s*found|page)|page\s*(not\s*found|doesn'?t\s*exist|cannot\s*be\s*found)|"
    r"domain\s*(is\s*)?(for\s*sale|expired|parked)|buy\s*this\s*domain|godaddy\s*parked|"
    r"this\s*site\s*can'?t\s*be\s*reached|account\s*suspended|website\s*coming\s*soon|under\s*construction)",
    re.I,
)


class ICPAnalysis(BaseModel):
    seller_company_or_product: Optional[str] = None
    what_seller_offers: str
    target_location: str
    sub_locations: List[str] = Field(default_factory=list)
    primary_industry: str
    target_industries: List[str]
    soft_preference_signals: List[str] = Field(default_factory=list)
    seller_product_exclusions: List[str]
    already_using_product_exclusions: List[str] = Field(default_factory=list)
    excluded_industries_or_types: List[str]
    min_employees: int = Field(default=5)
    max_employees: int = Field(default=100)
    min_sales_team: int = Field(default=2)
    max_sales_team: int = Field(default=15)
    decision_maker_titles: List[str] = Field(
        default=["CEO", "Founder", "Co-Founder", "Head of Sales", "Operations Manager"]
    )
    directory_listing_queries: List[str]
    direct_web_queries: List[str] = Field(default_factory=list)


class DirectoryCardCandidate(BaseModel):
    company_name: str
    company_website: Optional[str] = None
    directory_profile_url: Optional[str] = None
    city: Optional[str] = None
    location_text: Optional[str] = None
    employee_range: Optional[str] = None
    tagline_or_industry: Optional[str] = None
    is_valid_buyer_company: bool = True


class DirectoryExtractionBatch(BaseModel):
    companies: List[DirectoryCardCandidate]


class WebsiteSelection(BaseModel):
    verified_website_url: Optional[str] = None
    exact_brand_matches_website: bool = False
    confidence: int = Field(ge=0, le=100)


class EntityMatchVerification(BaseModel):
    matched_company_linkedin_url: Optional[str] = None
    company_linkedin_matches_exact_business: bool = False
    matched_person_name: Optional[str] = None
    matched_person_title: Optional[str] = None
    matched_person_linkedin_url: Optional[str] = None
    person_works_at_exact_company: bool = False


class LeadProfile(BaseModel):
    company_name: str
    city: Optional[str] = None
    industry_niche: str
    total_employees: Optional[str] = None
    estimated_sales_team_size: int = Field(default=2)
    company_phone_number: Optional[str] = None
    company_website: Optional[str] = None
    company_linkedin_url: Optional[str] = None
    contact_person_name: Optional[str] = None
    contact_person_title: Optional[str] = None
    contact_person_linkedin_url: Optional[str] = None
    contact_phone_number: Optional[str] = None
    icp_match_score: int = Field(ge=0, le=100)
    icp_tier: str
    suitability_reason: str
    is_qualified_lead: bool


def get_mongo_uri() -> str:
    return (
        os.getenv("MONGODB_URL")
        or os.getenv("mongodb_url")
        or os.getenv("MONGO_URI")
        or "mongodb://localhost:27017"
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProviderManager:
    def __init__(self, mongo_uri: Optional[str] = None):
        self.mongo_uri = mongo_uri or get_mongo_uri()
        self.db_name = "b2b_leads"
        self.collection_name = "settings_config"
        self.config: Dict[str, Any] = {}
        self.search_providers: List[str] = []
        self.serper_keys: List[str] = []
        self.dead_serper_keys: Set[str] = set()
        self.serper_index: int = 0
        self.searxng_urls: List[str] = []
        self.ddgs_semaphore = asyncio.Semaphore(2)
        self.llm_endpoints: List[Dict[str, Any]] = []

    def _as_list(self, val: Any) -> List[str]:
        if isinstance(val, list):
            return [str(v).strip() for v in val if v and str(v).strip()]
        if isinstance(val, str) and val.strip():
            return [val.strip()]
        return []

    async def load_config(self) -> None:
        doc: Optional[Dict[str, Any]] = None
        try:
            client = AsyncIOMotorClient(self.mongo_uri, serverSelectionTimeoutMS=5000)
            db = client[self.db_name]
            doc = await db[self.collection_name].find_one(
                {"config_key": {"$in": ["app_settings", "app_settings1"]}}
            )
            client.close()
        except Exception as exc:
            logger.warning(f"MongoDB settings load failed ({exc}); falling back to env vars.")

        self.config = doc or {}

        main_search = str(self.config.get("MAIN_SEARCH_PROVIDER") or os.getenv("MAIN_SEARCH_PROVIDER", "serper")).strip().lower()
        fallback_search = [p.lower() for p in self._as_list(self.config.get("FALLBACK_SEARCH_PROVIDERS"))]
        if not fallback_search:
            fallback_search = ["ddgs", "searxng"]
        self.search_providers = list(dict.fromkeys([main_search] + fallback_search))

        db_serper = self._as_list(self.config.get("SERPER_API_KEY"))
        env_serper = self._as_list(os.getenv("SERPER_API_KEY"))
        self.serper_keys = list(dict.fromkeys(db_serper + env_serper))
        self.dead_serper_keys = set()

        db_searx = self._as_list(self.config.get("SEARXNG_BASE_URL"))
        env_searx = self._as_list(os.getenv("SEARXNG_BASE_URL"))
        self.searxng_urls = [u.rstrip("/") for u in dict.fromkeys(db_searx + env_searx)]

        main_provider = str(self.config.get("MAIN_LLM_PROVIDER") or os.getenv("MAIN_LLM_PROVIDER", "openai")).strip()
        fallback_providers = self._as_list(self.config.get("FALLBACK_LLM_PROVIDERS"))
        if not fallback_providers:
            fallback_providers = ["openai", "kovaion_qwen3", "groq", "gemini"]

        ordered_providers = list(dict.fromkeys([main_provider] + fallback_providers))
        self.llm_endpoints = []

        for provider in ordered_providers:
            p_key = provider.lower()
            if p_key == "kovaion_gemma":
                base_url = str(self.config.get("KOVAION_AI_GEMMA") or os.getenv("KOVAION_AI_GEMMA", "")).strip()
                models = self._as_list(self.config.get("KOVAION_AI_GEMMA_MODEL")) or self._as_list(os.getenv("KOVAION_AI_GEMMA_MODEL"))
                keys = self._as_list(self.config.get("KOVAION_AI_API_KEY")) or self._as_list(os.getenv("KOVAION_AI_API_KEY")) or ["not-needed"]
                if base_url and models:
                    for m in models:
                        for k in keys:
                            self.llm_endpoints.append({"provider": "kovaion_gemma", "base_url": base_url, "api_key": k, "model": m})

            elif p_key == "kovaion_qwen3":
                base_url = str(self.config.get("KOVAION_AI_QWEN3") or os.getenv("KOVAION_AI_QWEN3", "")).strip()
                models = self._as_list(self.config.get("KOVAION_AI_QWEN3_MODEL")) or self._as_list(os.getenv("KOVAION_AI_QWEN3_MODEL"))
                keys = self._as_list(self.config.get("KOVAION_AI_API_KEY")) or self._as_list(os.getenv("KOVAION_AI_API_KEY")) or ["not-needed"]
                if base_url and models:
                    for m in models:
                        for k in keys:
                            self.llm_endpoints.append({"provider": "kovaion_qwen3", "base_url": base_url, "api_key": k, "model": m})

            elif p_key == "openai":
                base_url = str(self.config.get("OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")).strip()
                models = self._as_list(self.config.get("OPENAI_MODEL")) or self._as_list(os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
                keys = self._as_list(self.config.get("OPENAI_API_KEY")) or self._as_list(os.getenv("OPENAI_API_KEY"))
                for m in models:
                    for k in keys:
                        self.llm_endpoints.append({"provider": "openai", "base_url": base_url, "api_key": k, "model": m})

            elif p_key == "groq":
                base_url = "https://api.groq.com/openai/v1"
                models = self._as_list(self.config.get("GROQ_MODEL")) or self._as_list(os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"))
                keys = self._as_list(self.config.get("GROQ_API_KEY")) or self._as_list(os.getenv("GROQ_API_KEY"))
                for m in models:
                    for k in keys:
                        self.llm_endpoints.append({"provider": "groq", "base_url": base_url, "api_key": k, "model": m})

            elif p_key == "gemini":
                base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
                models = self._as_list(self.config.get("GEMINI_MODEL")) or self._as_list(os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
                keys = self._as_list(self.config.get("GEMINI_API_KEY")) or self._as_list(os.getenv("GEMINI_API_KEY"))
                for m in models:
                    for k in keys:
                        self.llm_endpoints.append({"provider": "gemini", "base_url": base_url, "api_key": k, "model": m})

        if not self.llm_endpoints:
            raise ValueError("No valid LLM provider endpoints or API keys configured.")

        logger.info(
            f"Loaded config | Search={self.search_providers} | Serper Keys={len(self.serper_keys)} | "
            f"SearXNG URLs={len(self.searxng_urls)} | LLM Endpoints={len(self.llm_endpoints)}"
        )

    def get_default_lead_count(self) -> int:
        raw = self.config.get("DEFAULT_LEAD_COUNT") or os.getenv("DEFAULT_LEAD_COUNT", "10")
        try:
            return int(raw)
        except Exception:
            return 10

    async def search(
        self,
        http: httpx.AsyncClient,
        query: str,
        num: int = 10,
        page: int = 1,
        gl: str = "us",
    ) -> List[Dict[str, Any]]:
        clean_query = " ".join(str(query or "").split()).strip()
        if not clean_query:
            return []

        for provider in self.search_providers:
            if provider == "serper":
                results = await self._search_serper(http, clean_query, num=num, page=page, gl=gl)
                if results:
                    return results
            elif provider == "searxng" and self.searxng_urls:
                results = await self._search_searxng(http, clean_query, num=num, page=page, gl=gl)
                if results:
                    return results
            elif provider == "ddgs":
                results = await self._search_ddgs(clean_query, num=num, gl=gl)
                if results:
                    return results
        return []

    async def _search_serper(
        self,
        http: httpx.AsyncClient,
        query: str,
        num: int = 10,
        page: int = 1,
        gl: str = "us",
    ) -> List[Dict[str, Any]]:
        active_keys = [k for k in self.serper_keys if k not in self.dead_serper_keys]
        if not active_keys:
            return []

        endpoint = "https://google.serper.dev/search"
        payload: Dict[str, Any] = {
            "q": query[:2000],
            "num": max(1, min(int(num), 100)),
            "gl": gl.lower(),
        }
        if int(page) > 1:
            payload["page"] = int(page)

        total = len(active_keys)

        for attempt in range(total):
            idx = (self.serper_index + attempt) % total
            api_key = active_keys[idx]
            headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
            try:
                resp = await http.post(endpoint, headers=headers, json=payload, timeout=20.0)
                if resp.status_code in (401, 403):
                    logger.warning(f"Serper key invalid/unauthorized (HTTP {resp.status_code}); disabling key.")
                    self.dead_serper_keys.add(api_key)
                    continue
                if resp.status_code == 400:
                    err_body = resp.text[:200]
                    logger.warning(f"Serper 400 Bad Request ({err_body}); checking next key.")
                    if "api key" in err_body.lower() or "unauthorized" in err_body.lower():
                        self.dead_serper_keys.add(api_key)
                    continue
                if resp.status_code == 429 or resp.status_code >= 500:
                    logger.warning(f"Serper rate-limited/server error (HTTP {resp.status_code}); rotating key.")
                    self.serper_index = (idx + 1) % total
                    continue
                resp.raise_for_status()
                data = resp.json()
                self.serper_index = idx
                organic = data.get("organic", [])
                if organic:
                    return organic
            except Exception as exc:
                logger.warning(f"Serper error ({exc}); rotating key.")
                self.serper_index = (idx + 1) % total

        return []

    async def _search_searxng(
        self,
        http: httpx.AsyncClient,
        query: str,
        num: int = 10,
        page: int = 1,
        gl: str = "us",
    ) -> List[Dict[str, Any]]:
        lang = "en-IN" if gl.lower() == "in" else "en-US"
        params = {
            "q": query,
            "format": "json",
            "pageno": page,
            "language": lang,
            "categories": "general",
        }

        for base_url in self.searxng_urls:
            try:
                resp = await http.get(f"{base_url}/search", params=params, timeout=15.0)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                raw_results = data.get("results", [])
                normalized: List[Dict[str, Any]] = []
                for item in raw_results[:num]:
                    link = item.get("url") or item.get("link") or ""
                    if not link:
                        continue
                    normalized.append(
                        {
                            "title": item.get("title", ""),
                            "link": link,
                            "snippet": item.get("content") or item.get("snippet") or "",
                        }
                    )
                if normalized:
                    return normalized
            except Exception:
                continue

        return []

    async def _search_ddgs(
        self,
        query: str,
        num: int = 10,
        gl: str = "us",
    ) -> List[Dict[str, Any]]:
        region = "in-en" if gl.lower() == "in" else "us-en"

        def _sync_ddgs() -> List[Dict[str, Any]]:
            with DDGS() as ddgs:
                raw = ddgs.text(query, region=region, max_results=num)
                normalized: List[Dict[str, Any]] = []
                for item in raw or []:
                    link = item.get("href") or item.get("url") or ""
                    if not link:
                        continue
                    normalized.append(
                        {
                            "title": item.get("title", ""),
                            "link": link,
                            "snippet": item.get("body") or item.get("snippet") or "",
                        }
                    )
                return normalized

        async with self.ddgs_semaphore:
            try:
                return await asyncio.to_thread(_sync_ddgs)
            except Exception:
                return []

    async def call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        response_schema: type[BaseModel],
        temperature: float = 0.0,
    ) -> BaseModel:
        schema_json = json.dumps(response_schema.model_json_schema(), indent=2)
        strict_sys = (
            f"{system_prompt}\n\n"
            f"Respond ONLY with valid JSON conforming strictly to this JSON Schema:\n{schema_json}"
        )
        last_error: Optional[Exception] = None

        for ep in self.llm_endpoints:
            client = AsyncOpenAI(
                api_key=ep["api_key"],
                base_url=ep["base_url"],
                timeout=60.0,
                max_retries=1,
            )
            try:
                completion = await client.beta.chat.completions.parse(
                    model=ep["model"],
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format=response_schema,
                    temperature=temperature,
                )
                parsed = completion.choices[0].message.parsed
                if parsed is not None:
                    return parsed
            except Exception:
                pass

            try:
                completion = await client.chat.completions.create(
                    model=ep["model"],
                    messages=[
                        {"role": "system", "content": strict_sys},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                    temperature=temperature,
                )
                raw = completion.choices[0].message.content or "{}"
                repaired = json_repair.loads(raw)
                return response_schema.model_validate(repaired)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    f"LLM fallback triggered [{ep['provider']} | {ep['model']}] ({exc}); trying next endpoint."
                )

        raise RuntimeError(f"All LLM providers and API keys failed. Last error: {last_error}")


def clean_nullable_str(val: Optional[str]) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in {"null", "none", "n/a", "na", "unknown", "undefined", "nil", "-"}:
        return None
    return s


def sanitize_lead_nulls(lead: LeadProfile) -> LeadProfile:
    lead.company_name = lead.company_name.strip()
    lead.city = clean_nullable_str(lead.city)
    lead.total_employees = clean_nullable_str(lead.total_employees)
    lead.company_phone_number = clean_nullable_str(lead.company_phone_number)
    lead.company_website = clean_nullable_str(lead.company_website)
    lead.company_linkedin_url = clean_nullable_str(lead.company_linkedin_url)
    lead.contact_person_name = clean_nullable_str(lead.contact_person_name)
    lead.contact_person_title = clean_nullable_str(lead.contact_person_title)
    lead.contact_person_linkedin_url = clean_nullable_str(lead.contact_person_linkedin_url)
    lead.contact_phone_number = clean_nullable_str(lead.contact_phone_number)
    return lead


def normalize_domain(url: Optional[str]) -> str:
    if not url:
        return ""
    try:
        cleaned = url.strip()
        if not cleaned.startswith(("http://", "https://")):
            cleaned = "https://" + cleaned
        netloc = urlparse(cleaned).netloc.lower()
        return netloc.removeprefix("www.").split(":")[0]
    except Exception:
        return ""


def normalize_root_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        cleaned = url.strip()
        if not cleaned.startswith(("http://", "https://")):
            cleaned = "https://" + cleaned
        parsed = urlparse(cleaned)
        if not parsed.netloc or "." not in parsed.netloc:
            return None
        return f"https://{parsed.netloc.lower()}"
    except Exception:
        return None


def unwrap_redirect_url(href: str, base_url: str) -> Optional[str]:
    if not href:
        return None
    full = urljoin(base_url, href.strip())
    parsed = urlparse(full)
    qs = parse_qs(parsed.query)
    for key in ("u", "url", "target", "redirect", "dest", "destination", "q", "link"):
        if key in qs and qs[key]:
            candidate = unquote(qs[key][0])
            if candidate.startswith(("http://", "https://")):
                return candidate
    if full.startswith(("http://", "https://")):
        return full
    return None


def is_blocked_domain(url: Optional[str]) -> bool:
    domain = normalize_domain(url)
    if not domain or "." not in domain:
        return True
    if domain.endswith((".gov", ".edu", ".mil", ".org")):
        return True
    return any(domain == b or domain.endswith("." + b) for b in BLOCKED_DOMAINS)


def is_garbage_company_name(name: str) -> bool:
    cleaned = name.strip()
    if len(cleaned) < 2 or len(cleaned) > 55:
        return True
    if INVALID_NAME_PATTERN.search(cleaned):
        return True
    if len(cleaned.split()) > 6:
        return True
    return False


def tokens_from_name(name: str) -> List[str]:
    stop = {
        "inc", "llc", "ltd", "corp", "corporation", "co", "company", "technologies",
        "technology", "solutions", "group", "services", "studio", "agency", "labs",
        "io", "ai", "the", "and", "of", "for", "consulting", "partners", "digital",
    }
    raw = re.findall(r"[a-z0-9]+", name.lower())
    return [t for t in raw if t not in stop and len(t) > 1]


def url_matches_company_name(company_name: str, url: Optional[str]) -> bool:
    domain = normalize_domain(url)
    if not domain or is_blocked_domain(url):
        return False
    host_base = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    name_tokens = [re.sub(r"[^a-z0-9]", "", t) for t in tokens_from_name(company_name)]
    name_tokens = [t for t in name_tokens if t]
    if not name_tokens:
        return True
    joined = "".join(name_tokens)
    if joined in host_base or host_base in joined:
        return True
    acronym = "".join(t[0] for t in name_tokens if t)
    if len(acronym) >= 2 and acronym == host_base:
        return True
    return any(tok in host_base for tok in name_tokens if len(tok) >= 3)


def normalize_linkedin_company_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([a-zA-Z0-9\-_%]+)", url.strip())
    if not m:
        return None
    slug = m.group(1).rstrip("/")
    return f"https://www.linkedin.com/company/{slug}"


def normalize_linkedin_person_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([a-zA-Z0-9\-_%]+)", url.strip())
    if not m:
        return None
    slug = m.group(1).rstrip("/")
    return f"https://www.linkedin.com/in/{slug}"


def slug_matches_company(company_name: str, domain: str, li_url: str) -> bool:
    m = re.search(r"/company/([^/]+)", li_url.lower())
    if not m:
        return False
    slug = re.sub(r"[^a-z0-9]", "", m.group(1))
    host_base = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    tokens = [re.sub(r"[^a-z0-9]", "", t) for t in tokens_from_name(company_name)]
    tokens = [t for t in tokens if len(t) >= 2]
    if host_base and len(host_base) >= 3 and (host_base in slug or slug in host_base):
        return True
    if tokens and any(t in slug for t in tokens):
        return True
    return False


def snippet_mentions_exact_company(company_name: str, domain: str, title: str, snippet: str) -> bool:
    text = f"{title} {snippet}".lower()
    host_base = domain.split(".")[0].lower()
    if len(host_base) >= 3 and host_base in text:
        return True
    tokens = tokens_from_name(company_name)
    if not tokens:
        return company_name.lower() in text
    if len(tokens) == 1:
        return bool(re.search(rf"\b{re.escape(tokens[0])}\b", text))
    matched = sum(1 for t in tokens if re.search(rf"\b{re.escape(t)}\b", text))
    return matched >= len(tokens)


def parse_linkedin_person_snippet(title: str, url: str, snippet: str) -> Dict[str, Optional[str]]:
    clean_title = re.split(r"\s*[\|\-–—]\s*LinkedIn", title, flags=re.I)[0].strip()
    parts = [p.strip() for p in re.split(r"\s*[\-–—\|]\s*", clean_title) if p.strip()]
    person_name = parts[0] if parts and 2 <= len(parts[0].split()) <= 4 else None
    person_title = parts[1] if len(parts) > 1 else None
    return {
        "parsed_name": person_name,
        "parsed_title": person_title,
        "raw_title": title,
        "url": url,
        "snippet": snippet,
    }


def detect_existing_competing_stack(html_or_text: str, exclusions: List[str]) -> Optional[str]:
    if not html_or_text or not exclusions:
        return None
    lower = html_or_text.lower()
    for term in exclusions:
        clean_term = term.strip().lower()
        if len(clean_term) < 3:
            continue
        if re.search(rf"\b{re.escape(clean_term)}\b", lower):
            return term
    return None


def is_valid_live_page(status_code: int, title: str, body_text: str) -> bool:
    if status_code != 200:
        return False
    clean_body = body_text.strip()
    if len(clean_body) < 120:
        return False
    check_text = f"{title} {clean_body[:600]}"
    if SOFT_404_PATTERN.search(check_text):
        return False
    return True


async def verify_linkedin_url_live(http: httpx.AsyncClient, url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = await http.get(url, headers=headers, follow_redirects=True, timeout=8.0)
        if resp.status_code == 404 or resp.status_code >= 410:
            return None
        final_u = str(resp.url).lower()
        if "linkedin.com/404" in final_u or "/unavailable" in final_u:
            return None
        return url
    except Exception:
        return url


async def scrape_directory_listing_page(
    http: httpx.AsyncClient,
    directory_url: str,
) -> List[Dict[str, Any]]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = await http.get(directory_url, headers=headers, follow_redirects=True, timeout=14.0)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
            tag.decompose()

        cards: List[Dict[str, Any]] = []
        seen_card_keys: Set[str] = set()

        containers = soup.find_all(
            ["li", "article", "div"],
            class_=re.compile(r"(provider|company|listing|firm|card|item|row|result)", re.I),
        )
        if not containers:
            containers = soup.find_all(["li", "article", "section"])

        for node in containers:
            text = " ".join(node.get_text(separator=" ").split())
            if len(text) < 40 or len(text) > 1400:
                continue

            heading = node.find(["h2", "h3", "h4"])
            raw_title = heading.get_text(strip=True) if heading else ""

            external_websites: List[str] = []
            profile_links: List[str] = []

            for a in node.find_all("a", href=True):
                unwrapped = unwrap_redirect_url(str(a["href"]), str(resp.url))
                if not unwrapped:
                    continue
                lower_u = unwrapped.lower()
                if any(pat in lower_u for pat in DIRECTORY_PROFILE_PATTERNS):
                    if unwrapped not in profile_links:
                        profile_links.append(unwrapped)
                elif not is_blocked_domain(unwrapped):
                    root = normalize_root_url(unwrapped)
                    if root and root not in external_websites:
                        external_websites.append(root)

            if not external_websites and not profile_links:
                continue

            dedup_key = f"{raw_title.lower()}|{external_websites[0] if external_websites else profile_links[0]}"
            if dedup_key in seen_card_keys:
                continue
            seen_card_keys.add(dedup_key)

            cards.append(
                {
                    "heading": raw_title[:100],
                    "external_websites": external_websites[:2],
                    "profile_links": profile_links[:2],
                    "card_text": text[:650],
                }
            )
            if len(cards) >= 35:
                break

        return cards
    except Exception:
        return []


async def scrape_and_verify_website(
    http: httpx.AsyncClient,
    url: Optional[str],
    company_name: str,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "final_url": None,
        "website_reachable": False,
        "page_title": "",
        "meta_description": "",
        "raw_html_signals": "",
        "scraped_text": "",
        "extracted_phones": [],
        "site_linkedin_company_url": None,
        "site_linkedin_person_urls": [],
        "has_pricing_page": False,
        "name_verified_on_site": False,
    }
    root = normalize_root_url(url)
    if not root or is_blocked_domain(root):
        return result

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    urls_to_try = [root]
    if root.startswith("https://"):
        urls_to_try.append("http://" + root[len("https://"):])

    root_resp: Optional[httpx.Response] = None
    for candidate_root in urls_to_try:
        try:
            resp = await http.get(candidate_root, headers=headers, follow_redirects=True, timeout=11.0)
            if resp.status_code == 200:
                root_resp = resp
                break
        except Exception:
            continue

    if root_resp is None:
        return result

    final_host = normalize_root_url(str(root_resp.url))
    if not final_host or is_blocked_domain(final_host):
        return result

    root_soup = BeautifulSoup(root_resp.text, "html.parser")
    root_title = root_soup.title.string.strip()[:200] if (root_soup.title and root_soup.title.string) else ""
    meta_desc_tag = root_soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    root_meta = str(meta_desc_tag["content"]).strip()[:300] if (meta_desc_tag and meta_desc_tag.get("content")) else ""

    for tag in root_soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    root_text = " ".join(root_soup.get_text(separator=" ").split())

    if not is_valid_live_page(root_resp.status_code, root_title, root_text):
        return result

    result["website_reachable"] = True
    result["final_url"] = final_host
    result["page_title"] = root_title
    result["meta_description"] = root_meta
    result["raw_html_signals"] = root_resp.text[:15000]

    chunks: List[str] = [f"[{final_host}]: {root_text[:1800]}"]
    phones: Set[str] = set()
    li_comp_links: List[str] = []
    li_ppl_links: Set[str] = set()
    phone_regex = re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}")

    for m in phone_regex.findall(root_text):
        phones.add(m.strip())

    raw_soup_for_links = BeautifulSoup(root_resp.text, "html.parser")
    internal_subpages: List[str] = []
    for a in raw_soup_for_links.find_all("a", href=True):
        href = str(a["href"]).strip()
        lower_href = href.lower()
        if "linkedin.com/company/" in lower_href:
            norm_li = normalize_linkedin_company_url(href)
            if norm_li and norm_li not in li_comp_links:
                li_comp_links.append(norm_li)
        elif "linkedin.com/in/" in lower_href:
            norm_p = normalize_linkedin_person_url(href)
            if norm_p:
                li_ppl_links.add(norm_p)
        if "/pricing" in lower_href:
            result["has_pricing_page"] = True
        if lower_href.startswith("tel:"):
            raw_tel = re.sub(r"[^\d+()-.\s]", "", href[4:]).strip()
            if len(re.sub(r"\D", "", raw_tel)) >= 10:
                phones.add(raw_tel)
        if any(k in lower_href for k in ("/about", "/team", "/leadership", "/pricing", "/contact")):
            full_sub = urljoin(final_host, href)
            if normalize_domain(full_sub) == normalize_domain(final_host) and full_sub not in internal_subpages:
                internal_subpages.append(full_sub)

    for sub_url in internal_subpages[:4]:
        try:
            sub_resp = await http.get(sub_url, headers=headers, follow_redirects=True, timeout=8.0)
            if sub_resp.status_code != 200:
                continue
            if "/pricing" in str(sub_resp.url).lower():
                result["has_pricing_page"] = True
            sub_soup = BeautifulSoup(sub_resp.text, "html.parser")
            for a in sub_soup.find_all("a", href=True):
                href = str(a["href"]).strip()
                lower_href = href.lower()
                if "linkedin.com/company/" in lower_href:
                    norm_li = normalize_linkedin_company_url(href)
                    if norm_li and norm_li not in li_comp_links:
                        li_comp_links.append(norm_li)
                elif "linkedin.com/in/" in lower_href:
                    norm_p = normalize_linkedin_person_url(href)
                    if norm_p:
                        li_ppl_links.add(norm_p)
                if lower_href.startswith("tel:"):
                    raw_tel = re.sub(r"[^\d+()-.\s]", "", href[4:]).strip()
                    if len(re.sub(r"\D", "", raw_tel)) >= 10:
                        phones.add(raw_tel)
            for tag in sub_soup(["script", "style", "noscript", "svg", "iframe"]):
                tag.decompose()
            sub_text = " ".join(sub_soup.get_text(separator=" ").split())
            if is_valid_live_page(200, "", sub_text):
                for m in phone_regex.findall(sub_text):
                    phones.add(m.strip())
                chunks.append(f"[{sub_url}]: {sub_text[:1200]}")
        except Exception:
            continue

    combined = "\n".join(chunks)[:5200]
    result["scraped_text"] = combined
    result["extracted_phones"] = list(phones)[:4]
    result["site_linkedin_company_url"] = li_comp_links[0] if li_comp_links else None
    result["site_linkedin_person_urls"] = list(li_ppl_links)[:4]

    tokens = tokens_from_name(company_name)
    haystack = f"{result['page_title']} {result['meta_description']} {combined[:1500]}".lower()
    if url_matches_company_name(company_name, result["final_url"]):
        result["name_verified_on_site"] = True
    elif tokens:
        matched = sum(1 for t in tokens if t in haystack)
        result["name_verified_on_site"] = matched >= max(1, (len(tokens) + 1) // 2)
    else:
        result["name_verified_on_site"] = result["website_reachable"]

    return result


class LeadFinderAgent:
    def __init__(self, mongo_uri: Optional[str] = None):
        self.providers = ProviderManager(mongo_uri=mongo_uri)
        self.semaphore = asyncio.Semaphore(8)

    async def parse_icp(self, icp_prompt: str, max_leads: int) -> ICPAnalysis:
        sys_prompt = (
            "You are an expert B2B Lead Generation Strategist. Dissect the user's ICP prompt accurately.\n"
            "1. Extract the EXACT target state/city/country into `target_location` (e.g., if the prompt says 'Texas, United States', "
            "`target_location` MUST be 'Texas', and `sub_locations` MUST be ['Austin', 'Dallas', 'Houston', 'San Antonio', 'Plano']).\n"
            "2. Identify products/services the seller offers and put competing products into `seller_product_exclusions`.\n"
            "3. Identify tools/platforms the prompt explicitly excludes if already heavily used (e.g., 'HubSpot marketing hub') into `already_using_product_exclusions`.\n"
            "4. Separate hard constraints (location, industry, size <= max_employees) from `soft_preference_signals` (pricing page, founder selling, spreadsheets, WhatsApp).\n"
            "5. Generate 10 directory search queries (`site:clutch.co`, `site:goodfirms.co/directory`, `site:designrush.com/agency`, `site:g2.com/categories`) "
            "AND 8 `direct_web_queries` specifically targeting `target_location` and `sub_locations`."
        )
        user_prompt = f"Target Lead Count: {max_leads}\n\nICP Prompt:\n{icp_prompt}"
        return await self.providers.call_llm(sys_prompt, user_prompt, ICPAnalysis)

    def _build_wave_queries(self, icp: ICPAnalysis, wave: int) -> List[str]:
        locations = [icp.target_location] + [
            loc for loc in icp.sub_locations if loc.lower() != icp.target_location.lower()
        ]
        loc = locations[(wave - 1) % len(locations)]
        industries = icp.target_industries or [icp.primary_industry]
        ind = industries[(wave - 1) % len(industries)]
        ind_alt = industries[wave % len(industries)]

        dir_start = ((wave - 1) * 3) % len(DIRECTORY_SEARCH_TARGETS)
        selected_dirs = DIRECTORY_SEARCH_TARGETS[dir_start : dir_start + 3]

        queries: List[str] = []
        if wave == 1:
            queries.extend(icp.directory_listing_queries[:4])
            queries.extend(icp.direct_web_queries[:3])
        elif wave == 2:
            queries.extend(icp.directory_listing_queries[4:8])
            queries.extend(icp.direct_web_queries[3:6])

        for d in selected_dirs:
            queries.append(f'site:{d} "{ind}" "{loc}"')

        queries.append(f'site:clutch.co/profile "{ind}" "{loc}"')
        queries.append(f'site:goodfirms.co/company "{ind}" "{loc}"')
        queries.append(f'site:linkedin.com/company "{ind}" "{loc}"')
        queries.append(f'"{loc}" "{ind_alt}" company "about us" OR "services" -jobs -salary')

        return list(dict.fromkeys(queries))[:10]

    async def discover_candidates_wave(
        self,
        http: httpx.AsyncClient,
        icp: ICPAnalysis,
        wave: int,
        seen_domains: Set[str],
        seen_names: Set[str],
        seen_dir_urls: Set[str],
    ) -> List[DirectoryCardCandidate]:
        queries = self._build_wave_queries(icp, wave)
        page_num = ((wave - 1) // 4) + 1
        gl_code = "in" if "india" in icp.target_location.lower() else "us"

        serp_results = await asyncio.gather(
            *[self.providers.search(http, q, num=10, page=page_num, gl=gl_code) for q in queries]
        )

        directory_pages: List[str] = []
        serp_items: List[Dict[str, str]] = []

        for q, items in zip(queries, serp_results):
            for item in items:
                link = item.get("link", "")
                title = item.get("title", "")
                snippet = item.get("snippet", "")
                serp_items.append({"query": q, "title": title, "link": link, "snippet": snippet})

                if link and link not in seen_dir_urls:
                    lower_l = link.lower()
                    if any(
                        dom in lower_l
                        for dom in (
                            "goodfirms.co/directory",
                            "goodfirms.co/it-services",
                            "goodfirms.co/business-services",
                            "clutch.co/developers",
                            "clutch.co/it-services",
                            "clutch.co/agencies",
                            "clutch.co/consulting",
                            "designrush.com/agency",
                            "sortlist.com",
                            "g2.com/categories",
                            "capterra.com/categories",
                            "wellfound.com",
                            "ycombinator.com/companies",
                            "thomasnet.com",
                            "kompass.com",
                        )
                    ):
                        seen_dir_urls.add(link)
                        directory_pages.append(link)

        scraped_cards_nested = await asyncio.gather(
            *[scrape_directory_listing_page(http, url) for url in directory_pages[:6]]
        )

        all_cards: List[Dict[str, Any]] = []
        for page_url, cards in zip(directory_pages[:6], scraped_cards_nested):
            for c in cards:
                c["source_directory"] = page_url
                all_cards.append(c)

        discovered: List[DirectoryCardCandidate] = []

        if all_cards:
            for i in range(0, len(all_cards), 25):
                chunk = all_cards[i : i + 25]
                sys_prompt = (
                    "Extract structured company records from B2B directory listing cards (GoodFirms, Clutch, DesignRush, G2, Thomasnet).\n"
                    "RULES:\n"
                    "1. Extract each listed company's exact `company_name`, `company_website` (ONLY if `external_websites` clearly matches that exact company's domain—otherwise null), "
                    "`directory_profile_url` (from `profile_links` if present), `city`, `location_text`, `employee_range`, and `tagline_or_industry`.\n"
                    "2. NEVER extract directory names (GoodFirms, Clutch, G2), sponsored ads without a real company, or list headings.\n"
                    f"3. Set `is_valid_buyer_company = false` ONLY if the company is clearly an MNC/enterprise (> {icp.max_employees} employees), "
                    f"sells excluded products ({icp.seller_product_exclusions}), already uses excluded tools ({icp.already_using_product_exclusions}), "
                    f"belongs to excluded industries ({icp.excluded_industries_or_types}), or is explicitly headquartered outside `{icp.target_location}`."
                )
                user_prompt = (
                    f"Target Industry: {icp.primary_industry} ({icp.target_industries})\n"
                    f"Target Location: {icp.target_location} ({icp.sub_locations})\n"
                    f"Already Collected (SKIP): {list(seen_names)[:150]}\n"
                    f"Directory Listing Cards:\n{json.dumps(chunk, ensure_ascii=False)}"
                )
                batch: DirectoryExtractionBatch = await self.providers.call_llm(
                    sys_prompt, user_prompt, DirectoryExtractionBatch
                )
                for item in batch.companies:
                    clean_name = item.company_name.strip()
                    if is_garbage_company_name(clean_name) or not item.is_valid_buyer_company:
                        continue
                    norm_key = re.sub(r"[^a-z0-9]", "", clean_name.lower())
                    if len(norm_key) < 2 or norm_key in seen_names:
                        continue
                    if item.company_website and (
                        is_blocked_domain(item.company_website)
                        or not url_matches_company_name(clean_name, item.company_website)
                    ):
                        item.company_website = None
                    domain = normalize_domain(item.company_website)
                    if domain and domain in seen_domains:
                        continue
                    seen_names.add(norm_key)
                    if domain:
                        seen_domains.add(domain)
                    discovered.append(item)

        if serp_items:
            for i in range(0, len(serp_items), 35):
                chunk = serp_items[i : i + 35]
                sys_prompt = (
                    "Extract ALL individual B2B companies, agencies, or SMBs mentioned in these search results "
                    "(including Clutch profiles, GoodFirms profiles, LinkedIn company pages, and direct company websites).\n"
                    "RULES:\n"
                    "1. Extract the clean brand `company_name` (e.g. 'Five Pack', not 'Five Pack Reviews | Clutch.co').\n"
                    "2. Set `company_website` ONLY if the company's own official domain is visible AND matches the brand (NEVER clutch.co, linkedin.com, goodfirms.co, g2.com—use null).\n"
                    f"3. Exclude MNCs/enterprises (> {icp.max_employees} employees), directories themselves, companies selling {icp.seller_product_exclusions}, "
                    f"companies already using {icp.already_using_product_exclusions}, and excluded industries {icp.excluded_industries_or_types}."
                )
                user_prompt = (
                    f"Target Industry: {icp.primary_industry} ({icp.target_industries}) | Location: {icp.target_location} ({icp.sub_locations})\n"
                    f"Already Collected (SKIP): {list(seen_names)[:150]}\n"
                    f"Search Results:\n{json.dumps(chunk, ensure_ascii=False)}"
                )
                serp_batch: DirectoryExtractionBatch = await self.providers.call_llm(
                    sys_prompt, user_prompt, DirectoryExtractionBatch
                )
                for item in serp_batch.companies:
                    clean_name = item.company_name.strip()
                    if is_garbage_company_name(clean_name) or not item.is_valid_buyer_company:
                        continue
                    norm_key = re.sub(r"[^a-z0-9]", "", clean_name.lower())
                    if len(norm_key) < 2 or norm_key in seen_names:
                        continue
                    if item.company_website and (
                        is_blocked_domain(item.company_website)
                        or not url_matches_company_name(clean_name, item.company_website)
                    ):
                        item.company_website = None
                    domain = normalize_domain(item.company_website)
                    if domain and domain in seen_domains:
                        continue
                    seen_names.add(norm_key)
                    if domain:
                        seen_domains.add(domain)
                    discovered.append(item)

        logger.info(
            f"Wave {wave}: Scraped {len(directory_pages)} directory pages + {len(serp_items)} search hits -> Extracted {len(discovered)} unique candidates."
        )
        return discovered

    async def resolve_verified_website(
        self,
        http: httpx.AsyncClient,
        company_name: str,
        location: str,
        initial_url: Optional[str],
        gl_code: str,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        candidate_urls: List[str] = []
        if initial_url and not is_blocked_domain(initial_url) and url_matches_company_name(company_name, initial_url):
            root = normalize_root_url(initial_url)
            if root:
                candidate_urls.append(root)

        if candidate_urls:
            site_data = await scrape_and_verify_website(http, candidate_urls[0], company_name)
            if site_data["website_reachable"] and site_data["name_verified_on_site"] and url_matches_company_name(
                company_name, site_data["final_url"]
            ):
                return site_data["final_url"], site_data

        search_hits = await self.providers.search(
            http, f'"{company_name}" {location} official website', num=6, gl=gl_code
        )
        serp_summary = []
        for hit in search_hits:
            link = hit.get("link", "")
            serp_summary.append(
                {"title": hit.get("title", ""), "link": link, "snippet": hit.get("snippet", "")}
            )
            if not is_blocked_domain(link):
                root = normalize_root_url(link)
                if root and root not in candidate_urls:
                    candidate_urls.append(root)

        if not candidate_urls:
            return None, {}

        prioritized = sorted(
            candidate_urls,
            key=lambda u: (0 if url_matches_company_name(company_name, u) else 1),
        )

        for url in prioritized[:3]:
            if not url_matches_company_name(company_name, url):
                continue
            site_data = await scrape_and_verify_website(http, url, company_name)
            if site_data["website_reachable"] and site_data["name_verified_on_site"]:
                return site_data["final_url"], site_data

        if serp_summary:
            sys_prompt = (
                "Select the official corporate website URL for the EXACT company given from the search results. "
                "STRICT RULE: The website domain and brand MUST belong to this exact company (not a directory, partner, client, or different brand). "
                "Return null and `exact_brand_matches_website = false` if none clearly match."
            )
            user_prompt = (
                f"Company Name: {company_name}\nLocation: {location}\n"
                f"Search Results:\n{json.dumps(serp_summary, ensure_ascii=False)}"
            )
            selection: WebsiteSelection = await self.providers.call_llm(
                sys_prompt, user_prompt, WebsiteSelection
            )
            if (
                selection.verified_website_url
                and selection.exact_brand_matches_website
                and selection.confidence >= 85
                and not is_blocked_domain(selection.verified_website_url)
            ):
                site_data = await scrape_and_verify_website(
                    http, selection.verified_website_url, company_name
                )
                if site_data["website_reachable"] and site_data["name_verified_on_site"]:
                    return site_data["final_url"], site_data

        return None, {}

    async def verify_exact_linkedin_entities(
        self,
        company_name: str,
        domain: str,
        location: str,
        site_summary: str,
        site_li_company: Optional[str],
        candidate_li_companies: List[Dict[str, str]],
        candidate_li_people: List[Dict[str, Optional[str]]],
    ) -> EntityMatchVerification:
        if site_li_company and not candidate_li_people:
            return EntityMatchVerification(
                matched_company_linkedin_url=site_li_company,
                company_linkedin_matches_exact_business=True,
            )

        if not site_li_company and not candidate_li_companies and not candidate_li_people:
            return EntityMatchVerification()

        sys_prompt = (
            "You are an Entity Resolution Auditor. Verify whether the candidate LinkedIn Company URL and LinkedIn Person URL "
            "belong to the EXACT same company identified by `company_name` and `verified_domain`.\n\n"
            "STRICT DISAMBIGUATION RULES:\n"
            "1. Compare the company's industry, description, city/state, and domain against the LinkedIn snippet.\n"
            "2. If `site_embedded_linkedin_company_url` is provided (scraped directly from the company's own website), it is 100% authoritative.\n"
            "3. For `matched_person_linkedin_url`, ONLY accept a person whose LinkedIn title/snippet explicitly states they currently work "
            "at THIS exact company (matching brand/industry/location) as Founder, CEO, Co-Founder, Owner, President, Head of Sales, or Operations Manager.\n"
            "4. If there is ANY doubt or name collision with another company, return `null` and `false`."
        )

        payload = {
            "company_name": company_name,
            "verified_domain": domain,
            "target_location": location,
            "website_description": site_summary[:1200],
            "site_embedded_linkedin_company_url": site_li_company,
            "candidate_linkedin_companies": candidate_li_companies,
            "candidate_linkedin_people": candidate_li_people,
        }

        return await self.providers.call_llm(
            sys_prompt, json.dumps(payload, ensure_ascii=False), EntityMatchVerification
        )

    async def enrich_and_verify_candidate(
        self,
        http: httpx.AsyncClient,
        candidate: DirectoryCardCandidate,
        icp: ICPAnalysis,
        icp_prompt: str,
        seen_domains: Set[str],
    ) -> Optional[LeadProfile]:
        async with self.semaphore:
            name = candidate.company_name.strip()
            loc = candidate.city or candidate.location_text or icp.target_location
            gl_code = "in" if "india" in icp.target_location.lower() else "us"

            verified_website, site_data = await self.resolve_verified_website(
                http=http,
                company_name=name,
                location=loc,
                initial_url=candidate.company_website,
                gl_code=gl_code,
            )

            if not verified_website or not site_data.get("website_reachable"):
                logger.info(f"Rejected '{name}' | Unreachable, 404, or unverified website.")
                return None

            domain = normalize_domain(verified_website)
            if domain in seen_domains and candidate.company_website is None:
                return None
            seen_domains.add(domain)

            detected_used_tool = detect_existing_competing_stack(
                f"{site_data.get('raw_html_signals', '')}\n{site_data.get('scraped_text', '')}",
                icp.already_using_product_exclusions,
            )
            if detected_used_tool:
                logger.info(
                    f"Filtered '{name}' | Already using excluded product/tool: {detected_used_tool}"
                )
                return None

            site_li_company = site_data.get("site_linkedin_company_url")

            li_comp_query = f'site:linkedin.com/company "{name}" "{icp.target_location}"'
            li_founder_query = f'site:linkedin.com/in "{name}" ("Founder" OR "CEO" OR "Co-Founder" OR "Owner")'
            li_sales_query = f'site:linkedin.com/in "{name}" ("Head of Sales" OR "VP Sales" OR "Operations Manager")'
            headcount_query = f'"{name}" "{icp.target_location}" employees OR founded OR headquarters'

            tasks = [
                self.providers.search(http, li_founder_query, num=5, gl=gl_code),
                self.providers.search(http, li_sales_query, num=5, gl=gl_code),
                self.providers.search(http, headcount_query, num=4, gl=gl_code),
            ]
            if not site_li_company:
                tasks.insert(0, self.providers.search(http, li_comp_query, num=5, gl=gl_code))

            search_results = await asyncio.gather(*tasks)
            if not site_li_company:
                li_comp_hits, li_founder_hits, li_sales_hits, profile_hits = search_results
            else:
                li_comp_hits = []
                li_founder_hits, li_sales_hits, profile_hits = search_results

            candidate_li_companies: List[Dict[str, str]] = []
            seen_comp_urls: Set[str] = set()
            for hit in li_comp_hits:
                norm_li = normalize_linkedin_company_url(hit.get("link"))
                if not norm_li or norm_li in seen_comp_urls:
                    continue
                if not slug_matches_company(name, domain, norm_li) and not snippet_mentions_exact_company(
                    name, domain, hit.get("title", ""), hit.get("snippet", "")
                ):
                    continue
                seen_comp_urls.add(norm_li)
                candidate_li_companies.append(
                    {
                        "url": norm_li,
                        "title": hit.get("title", ""),
                        "snippet": hit.get("snippet", ""),
                    }
                )

            candidate_li_people: List[Dict[str, Optional[str]]] = []
            seen_person_urls: Set[str] = set()
            for p_url in site_data.get("site_linkedin_person_urls", []):
                if p_url not in seen_person_urls:
                    seen_person_urls.add(p_url)
                    candidate_li_people.append(
                        {
                            "parsed_name": None,
                            "parsed_title": None,
                            "raw_title": "Linked directly on company website",
                            "url": p_url,
                            "snippet": "",
                        }
                    )

            for hit in li_founder_hits + li_sales_hits:
                norm_p = normalize_linkedin_person_url(hit.get("link"))
                if not norm_p or norm_p in seen_person_urls:
                    continue
                title_str = hit.get("title", "")
                snippet_str = hit.get("snippet", "")
                if not snippet_mentions_exact_company(name, domain, title_str, snippet_str):
                    continue
                seen_person_urls.add(norm_p)
                candidate_li_people.append(
                    parse_linkedin_person_snippet(title_str, norm_p, snippet_str)
                )

            site_summary = (
                f"Title: {site_data.get('page_title', '')} | "
                f"Meta: {site_data.get('meta_description', '')} | "
                f"Excerpt: {site_data.get('scraped_text', '')[:1200]}"
            )

            entity_check = await self.verify_exact_linkedin_entities(
                company_name=name,
                domain=domain,
                location=loc,
                site_summary=site_summary,
                site_li_company=site_li_company,
                candidate_li_companies=candidate_li_companies,
                candidate_li_people=candidate_li_people,
            )

            verified_company_li = site_li_company
            if not verified_company_li and entity_check.company_linkedin_matches_exact_business:
                allowed_comp_urls = {c["url"] for c in candidate_li_companies}
                if entity_check.matched_company_linkedin_url in allowed_comp_urls:
                    verified_company_li = entity_check.matched_company_linkedin_url

            verified_person_name: Optional[str] = None
            verified_person_title: Optional[str] = None
            verified_person_li: Optional[str] = None
            if entity_check.person_works_at_exact_company:
                allowed_ppl_urls = {p["url"] for p in candidate_li_people if p.get("url")}
                if entity_check.matched_person_linkedin_url in allowed_ppl_urls:
                    verified_person_li = entity_check.matched_person_linkedin_url
                    verified_person_name = clean_nullable_str(entity_check.matched_person_name)
                    verified_person_title = clean_nullable_str(entity_check.matched_person_title)

            if verified_company_li:
                verified_company_li = await verify_linkedin_url_live(http, verified_company_li)
            if verified_person_li:
                verified_person_li = await verify_linkedin_url_live(http, verified_person_li)
                if not verified_person_li:
                    verified_person_name = None
                    verified_person_title = None

            sys_prompt = (
                "You are a B2B Data Verification & ICP Auditor. Evaluate this verified company realistically against the ICP.\n\n"
                "MANDATORY FIELD LOCKS:\n"
                "1. Use JSON `null` (NEVER \"null\" or \"N/A\") if any field is not explicitly verified.\n"
                f"2. `company_website`: MUST be `{verified_website}`.\n"
                f"3. `company_linkedin_url`: MUST be `{verified_company_li}` (or null).\n"
                f"4. `contact_person_name`: MUST be `{verified_person_name}` (or null, unless explicitly named as Founder/CEO in `website_content_excerpt`).\n"
                f"5. `contact_person_title`: MUST be `{verified_person_title}` (or null).\n"
                f"6. `contact_person_linkedin_url`: MUST be `{verified_person_li}` (or null).\n"
                "7. `city`: extract exact city in target region from directory metadata, website footer/contact, or LinkedIn snippet.\n"
                "8. `total_employees`: extract employee count/range ONLY if explicitly stated in evidence; otherwise null.\n\n"
                "HARD DISQUALIFICATION RULES (set `is_qualified_lead = false` ONLY when proven):\n"
                f"1. Clearly unrelated to `{icp.primary_industry}` / `{icp.target_industries}` or is pure B2C retail.\n"
                f"2. Clearly headquartered outside `{icp.target_location}` ({icp.sub_locations}) with no local presence.\n"
                f"3. Proven to be an MNC, public giant, or > {icp.max_employees} employees (or solo 1-person freelancer).\n"
                f"4. Sells competing products: {icp.seller_product_exclusions}.\n"
                f"5. Proven to already use excluded tools: {icp.already_using_product_exclusions}.\n"
                f"6. Belongs to excluded industries: {icp.excluded_industries_or_types}.\n\n"
                "IMPORTANT REAL-WORLD RULE:\n"
                "- Private SMBs and agencies rarely publish exact headcount, sales team size, ARR, internal CRM stack, or a `/pricing` page on their website. "
                "Do NOT disqualify a valid B2B company in the target geography & industry just because headcount, sales team size, or pricing page is not publicly listed—give it a 'Good ICP' score (60-74) instead!\n\n"
                "SCORING:\n"
                "- 80-100: 'Hot ICP' (verified geography + industry + SMB size <=100 + decision-maker + pricing page or strong buyer signals)\n"
                "- 60-79: 'Good ICP' (verified B2B SMB/startup/agency in target geography & industry with no disqualifiers)\n"
                "- Below 60: set `is_qualified_lead = false`."
            )

            evidence = {
                "company_name": name,
                "directory_metadata": {
                    "city": candidate.city,
                    "location_text": candidate.location_text,
                    "employee_range": candidate.employee_range,
                    "tagline_or_industry": candidate.tagline_or_industry,
                },
                "verified_website": verified_website,
                "verified_company_linkedin_url": verified_company_li,
                "verified_contact_person": {
                    "name": verified_person_name,
                    "title": verified_person_title,
                    "linkedin_url": verified_person_li,
                },
                "website_title": site_data.get("page_title", ""),
                "website_meta_description": site_data.get("meta_description", ""),
                "has_pricing_page": site_data.get("has_pricing_page", False),
                "website_phones": site_data.get("extracted_phones", []),
                "website_content_excerpt": site_data.get("scraped_text", ""),
                "company_linkedin_snippets": candidate_li_companies,
                "company_profile_snippets": [
                    {"title": h.get("title"), "link": h.get("link"), "snippet": h.get("snippet")}
                    for h in profile_hits
                ],
            }

            user_prompt = (
                f"User ICP Requirements:\n{icp_prompt}\n\n"
                f"Parsed ICP Criteria:\n{json.dumps(icp.model_dump(), ensure_ascii=False)}\n\n"
                f"Verified Evidence:\n{json.dumps(evidence, ensure_ascii=False)}"
            )

            lead: LeadProfile = await self.providers.call_llm(sys_prompt, user_prompt, LeadProfile)
            lead = sanitize_lead_nulls(lead)

            lead.company_website = verified_website
            lead.company_linkedin_url = verified_company_li
            lead.contact_person_linkedin_url = verified_person_li
            if verified_person_name:
                lead.contact_person_name = verified_person_name
            if verified_person_title:
                lead.contact_person_title = verified_person_title
            if not lead.contact_person_linkedin_url and not lead.contact_person_name:
                lead.contact_person_title = None

            if not lead.total_employees and candidate.employee_range:
                lead.total_employees = clean_nullable_str(candidate.employee_range)

            if not lead.city and candidate.city:
                lead.city = clean_nullable_str(candidate.city)

            if not lead.company_phone_number and site_data.get("extracted_phones"):
                lead.company_phone_number = site_data["extracted_phones"][0]

            if not lead.contact_phone_number and lead.company_phone_number:
                lead.contact_phone_number = lead.company_phone_number

            if not lead.is_qualified_lead or lead.icp_match_score < 60:
                logger.info(f"Filtered '{name}' | score={lead.icp_match_score} | reason={lead.suitability_reason}")
                return None

            logger.info(
                f"Verified Lead [+] {lead.company_name} | {lead.company_website} | "
                f"Industry={lead.industry_niche} | Score={lead.icp_match_score} | LI={lead.company_linkedin_url or 'None'} | "
                f"Contact={lead.contact_person_name or 'None'} ({lead.contact_person_linkedin_url or 'None'})"
            )
            return lead

    async def run(
        self,
        icp_prompt: str,
        max_leads: Optional[int] = None,
        exclude_companies: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        try:
            await self.providers.load_config()
            target_count = max_leads if max_leads is not None else self.providers.get_default_lead_count()
            logger.info(f"=== Starting Directory-First Lead Finder Agent | Target Lead Count = {target_count} ===")

            icp = await self.parse_icp(icp_prompt, target_count)
            logger.info(
                f"Parsed ICP | Primary Industry='{icp.primary_industry}' | "
                f"Location='{icp.target_location}' | Exclusions={icp.seller_product_exclusions} | "
                f"Stack Exclusions={icp.already_using_product_exclusions}"
            )

            qualified_leads: List[Dict[str, Any]] = []
            seen_domains: Set[str] = set()
            seen_names: Set[str] = set()
            seen_dir_urls: Set[str] = set()

            for comp in exclude_companies or []:
                norm_ex = re.sub(r"[^a-z0-9]", "", str(comp).lower())
                if norm_ex:
                    seen_names.add(norm_ex)
                dom_ex = normalize_domain(str(comp))
                if dom_ex:
                    seen_domains.add(dom_ex)

            max_waves = 24

            async with httpx.AsyncClient() as http:
                for wave in range(1, max_waves + 1):
                    if len(qualified_leads) >= target_count:
                        break

                    needed = target_count - len(qualified_leads)
                    logger.info(
                        f"Wave {wave}/{max_waves} | Verified: {len(qualified_leads)}/{target_count} | Needed: {needed}"
                    )

                    candidates = await self.discover_candidates_wave(
                        http=http,
                        icp=icp,
                        wave=wave,
                        seen_domains=seen_domains,
                        seen_names=seen_names,
                        seen_dir_urls=seen_dir_urls,
                    )

                    if not candidates:
                        continue

                    for start_idx in range(0, len(candidates), 12):
                        if len(qualified_leads) >= target_count:
                            break
                        chunk = candidates[start_idx : start_idx + 12]
                        results = await asyncio.gather(
                            *[
                                self.enrich_and_verify_candidate(http, cand, icp, icp_prompt, seen_domains)
                                for cand in chunk
                            ]
                        )
                        for lead in results:
                            if lead is not None and len(qualified_leads) < target_count:
                                qualified_leads.append(lead.model_dump(exclude={"is_qualified_lead"}))

            qualified_leads.sort(key=lambda x: x["icp_match_score"], reverse=True)
            final_leads = qualified_leads[:target_count]

            logger.info(f"=== Pipeline Complete | Returned {len(final_leads)}/{target_count} Verified Leads ===")
            return {
                "matched_count": len(final_leads),
                "leads": final_leads,
                "error": None,
            }

        except Exception as exc:
            logger.exception(f"Fatal error in LeadFinderAgent: {exc}")
            return {
                "matched_count": 0,
                "leads": [],
                "error": str(exc),
            }


mongo_client = AsyncIOMotorClient(get_mongo_uri())
db = mongo_client["b2b_leads"]
jobs_collection = db["lead_generation_jobs"]
background_tasks_registry: Set[asyncio.Task] = set()


def _extract_requested_lead_count(data: Dict[str, Any], icp_prompt: str) -> Optional[int]:
    for key in ("max_leads", "lead_count", "count", "limit", "requested_lead_count", "total_leads"):
        if data.get(key) is not None:
            try:
                val = int(data[key])
                if val > 0:
                    return val
            except Exception:
                pass
    m = re.search(
        r"\b(?:exactly|find|get|generate|need|want)?\s*(\d{1,3})\s+(?:verified\s+)?leads?\b",
        icp_prompt,
        re.I,
    )
    if m:
        val = int(m.group(1))
        if val > 0:
            return val
    return None


async def _run_background_lead_job(
    job_id: str,
    icp_prompt: str,
    max_leads: Optional[int],
    exclude_companies: List[str],
) -> None:
    try:
        agent = LeadFinderAgent(mongo_uri=get_mongo_uri())
        outcome = await agent.run(
            icp_prompt=icp_prompt,
            max_leads=max_leads,
            exclude_companies=exclude_companies,
        )

        if outcome.get("error"):
            await jobs_collection.update_one(
                {"job_id": job_id},
                {
                    "$set": {
                        "status": "failed",
                        "updated_at": utc_now_iso(),
                        "result": None,
                        "error": outcome["error"],
                    }
                },
            )
            return

        leads = outcome.get("leads", [])
        await jobs_collection.update_one(
            {"job_id": job_id},
            {
                "$set": {
                    "status": "sucess",
                    "updated_at": utc_now_iso(),
                    "result": {
                        "matched_count": len(leads),
                        "leads": leads,
                    },
                    "error": None,
                }
            },
        )
    except Exception as exc:
        logger.exception(f"Background job {job_id} crashed: {exc}")
        await jobs_collection.update_one(
            {"job_id": job_id},
            {
                "$set": {
                    "status": "failed",
                    "updated_at": utc_now_iso(),
                    "result": None,
                    "error": str(exc),
                }
            },
        )


async def start_lead_generation(data: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    icp_prompt = str(data.get("icp_prompt") or data.get("prompt") or "").strip()
    if not icp_prompt:
        return 400, {"error": "icp_prompt is required"}

    raw_excludes = data.get("exclude_companies") or []
    exclude_companies = (
        [str(x).strip() for x in raw_excludes if x and str(x).strip()]
        if isinstance(raw_excludes, list)
        else []
    )
    max_leads = _extract_requested_lead_count(data, icp_prompt)

    job_id = str(uuid.uuid4())
    now_iso = utc_now_iso()

    job_doc = {
        "job_id": job_id,
        "status": "processing",
        "icp_prompt": icp_prompt,
        "exclude_companies": exclude_companies,
        "created_at": now_iso,
        "updated_at": now_iso,
        "result": None,
        "error": None,
    }

    await jobs_collection.insert_one(job_doc)

    task = asyncio.create_task(
        _run_background_lead_job(
            job_id=job_id,
            icp_prompt=icp_prompt,
            max_leads=max_leads,
            exclude_companies=exclude_companies,
        )
    )
    background_tasks_registry.add(task)
    task.add_done_callback(background_tasks_registry.discard)

    return 202, {
        "job_id": job_id,
        "status": "processing",
    }


async def fetch_lead_generation(job_id: str) -> Tuple[int, Dict[str, Any]]:
    doc = await jobs_collection.find_one({"job_id": job_id})
    if not doc:
        return 404, {"error": "Job not found", "job_id": job_id}

    status = doc.get("status", "processing")
    if status == "processing":
        return 200, {
            "job_id": doc["job_id"],
            "status": "processing",
        }

    if status == "failed":
        return 200, {
            "job_id": doc["job_id"],
            "status": "failed",
            "icp_prompt": doc.get("icp_prompt"),
            "exclude_companies": doc.get("exclude_companies", []),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
            "result": None,
            "error": doc.get("error"),
        }

    return 200, {
        "job_id": doc["job_id"],
        "status": doc.get("status", "sucess"),
        "icp_prompt": doc.get("icp_prompt"),
        "exclude_companies": doc.get("exclude_companies", []),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "result": doc.get("result") or {"matched_count": 0, "leads": []},
        "error": doc.get("error"),
    }

===

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from lead_generation import fetch_lead_generation, start_lead_generation

app = FastAPI(title="Lead Generation API")


@app.post("/lead-generation")
async def create_lead_generation(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON payload"})

    status_code, payload = await start_lead_generation(data)
    return JSONResponse(status_code=status_code, content=payload)


@app.get("/lead-generation/{job_id}")
async def get_lead_generation(job_id: str):
    status_code, payload = await fetch_lead_generation(job_id)
    return JSONResponse(status_code=status_code, content=payload)


