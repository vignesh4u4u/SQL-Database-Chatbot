import asyncio
import json
import math
import os
import re
from typing import Any, Dict, List, Literal, Optional, Set
from urllib.parse import urlparse

from crewai import Agent, Crew, Process, Task
from crewai_tools import ScrapeWebsiteTool, SerperDevTool
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

load_dotenv()

LINKEDIN_COMPANY_RE = re.compile(
    r"^https?://([a-z]{2,3}\.)?linkedin\.com/company/[a-zA-Z0-9\-_%]+/?$"
)
LINKEDIN_PERSON_RE = re.compile(
    r"^https?://([a-z]{2,3}\.)?linkedin\.com/in/[a-zA-Z0-9\-_%]+/?$"
)
HTTP_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")
PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\s.\-]?)?\(?\d{2,4}\)?[\s.\-]\d{3,4}[\s.\-]\d{3,4}"
)

IGNORED_EMAIL_DOMAINS = {
    "example.com",
    "domain.com",
    "email.com",
    "sentry.io",
    "wixpress.com",
    "w3.org",
    "schema.org",
    "mysite.com",
    "yourdomain.com",
}
IGNORED_EMAIL_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")

AGGREGATOR_DOMAINS = {
    "linkedin.com",
    "crunchbase.com",
    "g2.com",
    "capterra.com",
    "clutch.co",
    "goodfirms.co",
    "wellfound.com",
    "angel.co",
    "zoominfo.com",
    "apollo.io",
    "pitchbook.com",
    "tracxn.com",
    "yelp.com",
    "glassdoor.com",
    "indeed.com",
    "indiamart.com",
    "justdial.com",
    "tradeindia.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "wikipedia.org",
    "bloomberg.com",
    "forbes.com",
    "techcrunch.com",
    "medium.com",
    "substack.com",
    "reddit.com",
}

DEFAULT_DECISION_MAKER_ROLES = [
    "CEO",
    "Founder",
    "Co-Founder",
    "Managing Director",
    "Director",
    "President",
    "Owner",
    "Partner",
    "Head of Sales",
    "VP of Sales",
    "Sales Lead",
    "Operations Manager",
    "COO",
    "CTO",
]


def _normalize_url(url: Optional[str]) -> Optional[str]:
    if not url or not isinstance(url, str):
        return None
    cleaned = url.strip().split("#")[0].rstrip("/")
    if not cleaned:
        return None
    if cleaned.startswith(("http://", "https://")):
        return cleaned if HTTP_URL_RE.match(cleaned) else None
    cleaned = f"https://{cleaned}"
    return cleaned if HTTP_URL_RE.match(cleaned) else None


def _normalize_linkedin_url(
    url: Optional[str], kind: Literal["company", "in"]
) -> Optional[str]:
    cleaned = _normalize_url(url)
    if not cleaned:
        return None
    cleaned = cleaned.split("?")[0].rstrip("/")
    parsed = urlparse(cleaned)
    if "linkedin.com" not in parsed.netloc.lower():
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0] != kind:
        return None
    canonical = f"https://www.linkedin.com/{kind}/{parts[1]}"
    pattern = LINKEDIN_COMPANY_RE if kind == "company" else LINKEDIN_PERSON_RE
    return canonical if pattern.match(canonical) else None


def _root_domain(url: Optional[str]) -> str:
    if not url:
        return ""
    host = urlparse(url.lower()).netloc.removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "in", "uk", "au"}:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _homepage_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    return f"{scheme}://{parsed.netloc}"


def _extract_valid_emails(text: str) -> List[str]:
    found: List[str] = []
    for raw in EMAIL_RE.findall(text or ""):
        email = raw.lower().strip(".,;:()[]<>\"'")
        if email.endswith(IGNORED_EMAIL_EXTENSIONS):
            continue
        domain = email.split("@")[-1]
        if domain in IGNORED_EMAIL_DOMAINS:
            continue
        if email not in found:
            found.append(email)
    return found


def _normalize_phone(phone: Optional[str]) -> Optional[str]:
    if not phone or not isinstance(phone, str):
        return None
    cleaned = " ".join(phone.strip().split())
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) < 7 or len(digits) > 15:
        return None
    if re.match(r"^(19|20)\d{2}[\-\s](19|20)\d{2}$", cleaned):
        return None
    return cleaned


def _location_matches(
    lead_location: str,
    target_location: str,
    location_keywords: Optional[List[str]] = None,
) -> bool:
    lead_lower = lead_location.lower().strip()
    target_lower = target_location.lower().strip()
    if not target_lower or target_lower in {"global", "worldwide", "any"}:
        return True
    if target_lower in lead_lower or lead_lower in target_lower:
        return True
    for kw in location_keywords or []:
        clean_kw = kw.strip().lower()
        if len(clean_kw) >= 2 and re.search(rf"\b{re.escape(clean_kw)}\b", lead_lower):
            return True
    lead_tokens = {t.strip() for t in re.split(r"[,/\-\s]+", lead_lower) if len(t.strip()) >= 3}
    target_tokens = {t.strip() for t in re.split(r"[,/\-\s]+", target_lower) if len(t.strip()) >= 3}
    return bool(lead_tokens & target_tokens)


class ParsedICPConfig(BaseModel):
    product_description: str = Field(
        ..., description="Inferred or stated product/solution being sold"
    )
    target_buyer_profile: str = Field(
        ..., description="Target industries, company stage, ARR, size, and buying signals"
    )
    target_location: str = Field(
        ..., description="Exact target city, state, country, or region extracted from prompt"
    )
    country_code: str = Field(
        ..., description="2-letter ISO country code matching target_location"
    )
    location_match_keywords: List[str] = Field(
        default_factory=list,
        description="Lowercase aliases, state/province names, and major cities inside target_location",
    )
    search_queries: List[str] = Field(
        default_factory=list,
        description="10 to 14 diverse Google search queries tailored to target_buyer_profile and target_location",
    )
    target_roles: List[str] = Field(
        default_factory=lambda: list(DEFAULT_DECISION_MAKER_ROLES),
        description="Target decision-maker titles",
    )
    employee_size_constraint: Optional[str] = Field(
        default=None, description="Employee count range constraint"
    )
    exclusions: List[str] = Field(
        default_factory=list, description="Excluded industries, company sizes, or business models"
    )
    disqualifying_existing_products: List[str] = Field(
        default_factory=list,
        description="Existing software/products that disqualify a lead if detected on their website",
    )
    lead_count: int = Field(default=10, ge=1, le=100)


class CandidateCompany(BaseModel):
    company_name: str
    company_website_url: str
    location: str
    industry_vertical: str
    employee_count_estimate: Optional[str] = None
    initial_signal: str


class CandidateCompanyList(BaseModel):
    companies: List[CandidateCompany] = Field(default_factory=list)


class ICPReasonBreakdown(BaseModel):
    executive_fit_verdict: str = Field(
        ...,
        description="Clear 2-sentence explanation of why this company urgently needs the offered product and fits the ICP",
    )
    industry_and_size_alignment: str = Field(
        ...,
        description="Specific match on industry vertical, business model, company stage, and verified employee headcount",
    )
    geographic_verification: str = Field(
        ...,
        description="Verified city, state/region, and country presence with source confirmation",
    )
    pain_point_and_buying_signals: str = Field(
        ...,
        description="Concrete factual evidence from website pages or search snippets showing buying intent or workflow gaps",
    )
    decision_maker_reachability: str = Field(
        ...,
        description="Summary of identified leadership contact, role relevance, email/phone availability, and LinkedIn verification",
    )
    exclusion_and_competitor_audit: str = Field(
        ...,
        description="Explicit confirmation that no excluded industries or disqualified competitor tools were found",
    )


class SingleCompanyEnrichmentOutput(BaseModel):
    is_qualified_lead: bool = Field(
        ...,
        description="False if disqualified by existing competitor product, wrong location, or exclusion rules",
    )
    disqualification_reason: Optional[str] = Field(
        default=None, description="Reason if is_qualified_lead is False"
    )
    company_name: str
    location: str
    industry_vertical: str
    employee_count_estimate: Optional[str] = None
    company_website_url: Optional[str] = None
    email: Optional[str] = None
    phone_number: Optional[str] = None
    decision_maker_name: Optional[str] = None
    decision_maker_role: Optional[str] = None
    company_linkedin_url: Optional[str] = None
    decision_maker_linkedin_url: Optional[str] = None
    purchase_intent_signal: str
    icp_score: int = Field(..., ge=0, le=100)
    icp_reason_breakdown: ICPReasonBreakdown


class LeadRecord(BaseModel):
    company_name: str
    location: str
    industry_vertical: str
    employee_count_estimate: Optional[str] = None
    company_website_url: Optional[str] = None
    email: Optional[str] = None
    email_type: Optional[Literal["decision_maker", "general_contact"]] = None
    phone_number: Optional[str] = None
    decision_maker_name: Optional[str] = None
    decision_maker_role: Optional[str] = None
    company_linkedin_url: Optional[str] = None
    decision_maker_linkedin_url: Optional[str] = None
    source_urls: List[str] = Field(default_factory=list)
    purchase_intent_signal: str
    icp_score: int = Field(..., ge=0, le=100)
    icp_range: Literal["Hot", "Good", "Warm", "Cold"] = "Good"
    icp_reason: str
    icp_reason_details: ICPReasonBreakdown

    @field_validator("company_linkedin_url", mode="before")
    @classmethod
    def _val_comp_li(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_linkedin_url(v, "company")

    @field_validator("decision_maker_linkedin_url", mode="before")
    @classmethod
    def _val_pers_li(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_linkedin_url(v, "in")

    @field_validator("phone_number", mode="before")
    @classmethod
    def _val_phone(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_phone(v)

    @model_validator(mode="after")
    def _sync_fields(self) -> "LeadRecord":
        if self.icp_score >= 80:
            self.icp_range = "Hot"
        elif self.icp_score >= 60:
            self.icp_range = "Good"
        elif self.icp_score >= 40:
            self.icp_range = "Warm"
        else:
            self.icp_range = "Cold"

        if not self.email:
            self.email_type = None
        else:
            local = self.email.split("@")[0]
            generic = (
                "info", "contact", "sales", "support", "admin",
                "hello", "enquiry", "inquiry", "hr", "office", "mail", "team",
            )
            self.email_type = (
                "general_contact" if local.startswith(generic) else "decision_maker"
            )
        return self


class LeadFinderResult(BaseModel):
    product_or_service: str
    target_region: str
    total_leads: int
    leads: List[LeadRecord] = Field(default_factory=list)


def _format_reason_text(b: ICPReasonBreakdown) -> str:
    return (
        f"• Fit Verdict: {b.executive_fit_verdict}\n"
        f"• Industry & Size: {b.industry_and_size_alignment}\n"
        f"• Location Verified: {b.geographic_verification}\n"
        f"• Buying Signals: {b.pain_point_and_buying_signals}\n"
        f"• Decision-Maker & Contact: {b.decision_maker_reachability}\n"
        f"• Exclusions & Competitor Audit: {b.exclusion_and_competitor_audit}"
    )


def _log_step(message: str, verbose: bool = True) -> None:
    if verbose:
        print(f"\n{'=' * 80}\n[CREWAI LEAD ENGINE] {message}\n{'=' * 80}", flush=True)


async def _run_icp_parser_crew_async(
    user_payload: Dict[str, Any],
    default_lead_count: int,
    model_name: str,
    verbose: bool = True,
) -> ParsedICPConfig:
    icp_prompt = (user_payload.get("icp_prompt") or "").strip()
    if not icp_prompt:
        raise ValueError("Payload must contain a non-empty 'icp_prompt' field.")

    _log_step("STAGE 1: Parsing ICP Prompt & Dynamically Resolving Location & Exclusions", verbose)

    parser_agent = Agent(
        role="B2B ICP & Global Location Architect",
        goal=(
            "Parse the user's natural-language ICP prompt into exact structured search parameters, "
            "dynamically resolving target_location, country_code, location_match_keywords, target_roles, "
            "and disqualifying_existing_products without hardcoding any geography."
        ),
        backstory=(
            "You convert complex B2B Ideal Customer Profile instructions into clean, machine-executable "
            "parameters for any country, city, or region worldwide."
        ),
        llm=model_name,
        verbose=verbose,
        allow_delegation=False,
    )

    parse_task = Task(
        description=(
            f"User ICP Prompt:\n{icp_prompt}\n\n"
            "Instructions:\n"
            "1. Extract `target_location` exactly as requested by the user (city, state, country, or region).\n"
            "2. Determine the 2-letter ISO `country_code` matching `target_location`.\n"
            "3. Populate `location_match_keywords` with lowercase aliases, state/province names, and major "
            "cities inside `target_location` so location validation works dynamically.\n"
            "4. Infer `product_description` from the pain points and target tools described.\n"
            "5. Generate 12 diverse `search_queries` that surface genuine company websites (not directories "
            "or listicles) matching `target_buyer_profile` inside `target_location`.\n"
            "6. Extract any existing software/CRM/product that disqualifies a company into "
            "`disqualifying_existing_products`."
        ),
        expected_output="Structured ParsedICPConfig JSON.",
        agent=parser_agent,
        output_pydantic=ParsedICPConfig,
    )

    crew = Crew(
        agents=[parser_agent],
        tasks=[parse_task],
        process=Process.sequential,
        verbose=verbose,
    )
    out = await crew.kickoff_async()
    parsed: ParsedICPConfig = out.pydantic
    if parsed is None:
        raise RuntimeError("ICP Parser Crew failed to produce structured configuration.")

    if isinstance(user_payload.get("lead_count"), int):
        parsed.lead_count = user_payload["lead_count"]
    else:
        parsed.lead_count = default_lead_count

    for item in user_payload.get("exclude_existing_products") or []:
        if (
            isinstance(item, str)
            and item.strip()
            and item.strip() not in parsed.disqualifying_existing_products
        ):
            parsed.disqualifying_existing_products.append(item.strip())

    _log_step(
        f"Parsed Location: {parsed.target_location} ({parsed.country_code.upper()}) | "
        f"Target Lead Count: {parsed.lead_count} | "
        f"Disqualifying Products: {parsed.disqualifying_existing_products}",
        verbose,
    )
    return parsed


async def _discover_candidates_with_serper_tool_async(
    config: ParsedICPConfig,
    already_seen_domains: Set[str],
    wave_num: int,
    model_name: str,
    verbose: bool = True,
) -> List[CandidateCompany]:
    _log_step(
        f"STAGE 2 (Wave {wave_num + 1}): Discovering Candidate Companies via CrewAI SerperDevTool",
        verbose,
    )
    serper_tool = SerperDevTool(
        country=config.country_code.lower(),
        location=config.target_location,
        n_results=10,
    )

    wave_modifiers = ["", "startup", "agency", "services", "solutions"]
    mod = wave_modifiers[wave_num % len(wave_modifiers)]
    selected_queries = (config.search_queries or [f"{config.target_buyer_profile} {config.target_location}"])[:6]
    formatted_queries = [
        f'{q} "{config.target_location}" {mod}'.strip()
        if config.target_location.lower() not in q.lower()
        else f"{q} {mod}".strip()
        for q in selected_queries
    ]

    skip_domains_str = ", ".join(sorted(already_seen_domains)[:40]) if already_seen_domains else "None"

    discovery_agent = Agent(
        role="B2B ICP Company Discovery Specialist",
        goal=(
            f"Use `Search the internet with Serper` to find genuine B2B companies in '{config.target_location}' "
            f"matching '{config.target_buyer_profile}' with official company website URLs."
        ),
        backstory=(
            "You discover real operating B2B companies in the requested target region using SerperDevTool. "
            "You strictly skip directory/aggregator sites (LinkedIn, G2, Clutch, Crunchbase, Yelp, IndiaMART) "
            "and never include already-discovered domains."
        ),
        tools=[serper_tool],
        llm=model_name,
        verbose=verbose,
        allow_delegation=False,
    )

    discovery_task = Task(
        description=(
            f"Target Location: {config.target_location}\n"
            f"Target Buyer Profile: {config.target_buyer_profile}\n"
            f"Employee Size Constraint: {config.employee_size_constraint or 'Any'}\n"
            f"Exclusions: {', '.join(config.exclusions) if config.exclusions else 'None'}\n"
            f"Already Discovered Domains (SKIP ALL): {skip_domains_str}\n\n"
            "Run `Search the internet with Serper` on each of these queries:\n"
            + "\n".join(f"- {q}" for q in formatted_queries)
            + "\n\nExtract every distinct, genuine B2B company website (homepage URL) found in the results "
            "with its `company_name`, `company_website_url`, `location`, `industry_vertical`, "
            "`employee_count_estimate`, and `initial_signal`."
        ),
        expected_output="Clean CandidateCompanyList JSON of verified official company homepages.",
        agent=discovery_agent,
        output_pydantic=CandidateCompanyList,
    )

    crew = Crew(
        agents=[discovery_agent],
        tasks=[discovery_task],
        process=Process.sequential,
        verbose=verbose,
    )
    out = await crew.kickoff_async()
    return out.pydantic.companies if out.pydantic else []


def _build_single_company_enrichment_crew(
    config: ParsedICPConfig,
    model_name: str,
    verbose: bool = True,
) -> Crew:
    scrape_tool = ScrapeWebsiteTool()
    serper_tool = SerperDevTool(
        country=config.country_code.lower(),
        n_results=6,
    )

    roles = (config.target_roles or DEFAULT_DECISION_MAKER_ROLES)[:8]
    roles_clause = "(" + " OR ".join(f'"{r}"' for r in roles) + ")"
    disqual_list = (
        ", ".join(config.disqualifying_existing_products)
        if config.disqualifying_existing_products
        else "None"
    )

    website_agent = Agent(
        role="Website Contact, Phone, Leadership & Competitor-Check Specialist",
        goal=(
            "Scrape the company's official website (/contact, /contact-us, /about, /team, homepage) "
            "using `Read website content` and fallback Serper search to extract verified company email, "
            "phone number, founder/leader name, and check if any disqualified product is already used."
        ),
        backstory=(
            "You inspect company websites using CrewAI's ScrapeWebsiteTool (`Read website content`). "
            "You always check the homepage plus `/contact`, `/contact-us`, and `/about` pages to ensure "
            "you capture the company's official phone number, email address, and leadership names. If the "
            "phone number or email is not visible on the scraped pages, you immediately run a targeted "
            "`Search the internet with Serper` query for the company's contact phone and email."
        ),
        tools=[scrape_tool, serper_tool],
        llm=model_name,
        verbose=verbose,
        allow_delegation=False,
    )

    linkedin_verifier_agent = Agent(
        role="Entity-Locked LinkedIn Matcher & ICP Reason Analyst",
        goal=(
            "Use `Search the internet with Serper` to find the exact `linkedin.com/company/...` URL "
            "and decision-maker `linkedin.com/in/...` profile for THIS exact company, then output "
            "a 100% grounded SingleCompanyEnrichmentOutput with a thorough ICP reason breakdown."
        ),
        backstory=(
            "You resolve exact LinkedIn URLs using SerperDevTool. You always verify that both the "
            "company LinkedIn URL and decision-maker LinkedIn profile explicitly match the target "
            "company name and senior role, and you write clear, evidence-backed ICP qualification reasons."
        ),
        tools=[serper_tool],
        llm=model_name,
        verbose=verbose,
        allow_delegation=False,
    )

    scrape_task = Task(
        description=(
            "Target Company: `{company_name}`\n"
            "Official Website: `{company_website_url}`\n"
            "Location: `{location}` | Industry: `{industry_vertical}`\n"
            f"Disqualifying Existing Products (If used on website, mark DISQUALIFIED): {disqual_list}\n\n"
            "Mandatory Steps:\n"
            "1. Call `Read website content` (`website_url`) on `{company_website_url}`.\n"
            "2. Call `Read website content` on `{company_website_url}/contact` and `{company_website_url}/contact-us` "
            "(if available) AND `{company_website_url}/about` to extract:\n"
            "   - `phone_number` (company contact phone number from header, footer, or contact page)\n"
            "   - `email` (official company or decision-maker email)\n"
            "   - `decision_maker_name` and `decision_maker_role` (Founder, CEO, Co-Founder, Director, etc.)\n"
            "3. If `phone_number` OR `email` is STILL missing after scraping the website, run "
            "`Search the internet with Serper` with:\n"
            "   `\"{company_name}\" contact phone number email`\n"
            "   and extract the verified phone number and email from the official company snippet.\n"
            "4. Check if the website mentions or uses any of the Disqualifying Existing Products "
            f"({disqual_list}). If found, clearly mark DISQUALIFIED."
        ),
        expected_output=(
            "Verified contact phone number, email, leadership names, and disqualification status for {company_name}."
        ),
        agent=website_agent,
    )

    verify_task = Task(
        description=(
            f"Product/Service Offered: {config.product_description}\n"
            f"Target Buyer Profile: {config.target_buyer_profile}\n"
            f"Mandatory Target Location: {config.target_location}\n"
            f"Employee Size Constraint: {config.employee_size_constraint or 'Any'}\n"
            f"Exclusions: {', '.join(config.exclusions) if config.exclusions else 'None'}\n"
            f"Disqualifying Existing Products: {disqual_list}\n\n"
            "Using the website & contact findings from the previous task for `{company_name}` (`{company_website_url}`):\n"
            "1. If the website check marked `{company_name}` as DISQUALIFIED or violates exclusions/location, "
            "set `is_qualified_lead=false` and explain why.\n"
            "2. Preserve the verified `phone_number` and `email` extracted in the previous step.\n"
            "3. Find Company LinkedIn URL (`company_linkedin_url`):\n"
            "   - Run `Search the internet with Serper` with:\n"
            "     `site:linkedin.com/company \"{company_name}\"`\n"
            "   - Copy ONLY an exact `https://www.linkedin.com/company/...` URL from the search output that "
            "matches `{company_name}`.\n"
            "4. Find Decision-Maker LinkedIn URL (`decision_maker_linkedin_url`):\n"
            "   - If a founder/leader name was extracted from `/about` or `/team`, run:\n"
            "     `site:linkedin.com/in \"<Leader Name>\" \"{company_name}\"`\n"
            "   - Otherwise run:\n"
            f"     `site:linkedin.com/in \"{{company_name}}\" {roles_clause}`\n"
            "   - Copy ONLY an exact `https://www.linkedin.com/in/...` URL whose title/snippet explicitly "
            "confirms that person works at `{company_name}` in a senior role.\n"
            "5. Write a detailed, crystal-clear `icp_reason_breakdown`:\n"
            "   - `executive_fit_verdict`: 2 clear sentences on why this company needs the product now.\n"
            "   - `industry_and_size_alignment`: Exact industry vertical, stage, and employee count match.\n"
            "   - `geographic_verification`: Exact city/state/country confirmed.\n"
            "   - `pain_point_and_buying_signals`: Specific factual signals observed on their site/snippets.\n"
            "   - `decision_maker_reachability`: Leader name/role + verified phone/email/LinkedIn channels.\n"
            "   - `exclusion_and_competitor_audit`: Confirmation that zero excluded industries or competitor tools exist.\n"
            "6. Output strictly as `SingleCompanyEnrichmentOutput`."
        ),
        expected_output="100% verified SingleCompanyEnrichmentOutput JSON for {company_name}.",
        agent=linkedin_verifier_agent,
        output_pydantic=SingleCompanyEnrichmentOutput,
    )

    return Crew(
        agents=[website_agent, linkedin_verifier_agent],
        tasks=[scrape_task, verify_task],
        process=Process.sequential,
        verbose=verbose,
    )


async def _run_lead_finder_async(
    user_payload: Dict[str, Any],
    lead_count: int = 50,
    model_name: str = "gpt-4o-mini",
    max_waves: int = 4,
    verbose: bool = True,
    output_json_path: str = "verified_leads.json",
) -> dict:
    if not os.getenv("OPENAI_API_KEY") or not os.getenv("SERPER_API_KEY"):
        raise ValueError("Both OPENAI_API_KEY and SERPER_API_KEY must be set in environment.")

    config = await _run_icp_parser_crew_async(
        user_payload=user_payload,
        default_lead_count=lead_count,
        model_name=model_name,
        verbose=verbose,
    )

    verified_leads: List[LeadRecord] = []
    seen_domains: Set[str] = set()
    seen_company_names: Set[str] = set()

    for wave in range(max_waves):
        remaining = config.lead_count - len(verified_leads)
        if remaining <= 0:
            break

        candidates = await _discover_candidates_with_serper_tool_async(
            config=config,
            already_seen_domains=seen_domains,
            wave_num=wave,
            model_name=model_name,
            verbose=verbose,
        )

        batch_cap = math.ceil(remaining * 1.5)
        crew_inputs: List[Dict[str, str]] = []
        for cand in candidates:
            dom = _root_domain(cand.company_website_url)
            norm_name = re.sub(r"[^a-z0-9]", "", cand.company_name.lower())
            if (
                not dom
                or dom in AGGREGATOR_DOMAINS
                or dom in seen_domains
                or norm_name in seen_company_names
            ):
                continue
            seen_domains.add(dom)
            crew_inputs.append(
                {
                    "company_name": cand.company_name,
                    "company_website_url": _homepage_url(cand.company_website_url),
                    "location": cand.location,
                    "industry_vertical": cand.industry_vertical,
                    "initial_signal": cand.initial_signal,
                }
            )
            if len(crew_inputs) >= batch_cap:
                break

        if not crew_inputs:
            continue

        _log_step(
            f"STAGE 3 (Wave {wave + 1}): Running Parallel CrewAI Enrichment on {len(crew_inputs)} Companies "
            f"(Current Verified: {len(verified_leads)}/{config.lead_count})",
            verbose,
        )

        enrichment_crew = _build_single_company_enrichment_crew(
            config=config,
            model_name=model_name,
            verbose=verbose,
        )
        crew_outputs = await enrichment_crew.akickoff_for_each(inputs=crew_inputs)

        for inp, out in zip(crew_inputs, crew_outputs):
            if len(verified_leads) >= config.lead_count:
                break
            enriched: Optional[SingleCompanyEnrichmentOutput] = out.pydantic
            if not enriched or not enriched.is_qualified_lead:
                if verbose and enriched:
                    print(
                        f"[DISQUALIFIED] {enriched.company_name}: {enriched.disqualification_reason}",
                        flush=True,
                    )
                continue

            if not _location_matches(
                enriched.location, config.target_location, config.location_match_keywords
            ):
                continue

            norm_name = re.sub(r"[^a-z0-9]", "", enriched.company_name.lower())
            if not norm_name or norm_name in seen_company_names:
                continue

            site_url = _normalize_url(enriched.company_website_url) or inp["company_website_url"]
            comp_li = _normalize_linkedin_url(enriched.company_linkedin_url, "company")
            pers_li = _normalize_linkedin_url(enriched.decision_maker_linkedin_url, "in")

            clean_email = None
            if enriched.email:
                extracted_em = _extract_valid_emails(enriched.email)
                if extracted_em:
                    em = extracted_em[0]
                    em_dom = em.split("@")[-1]
                    site_root = _root_domain(site_url)
                    if (site_root and em_dom.endswith(site_root)) or (
                        len(norm_name) >= 4
                        and norm_name in re.sub(r"[^a-z0-9]", "", em_dom.split(".")[0])
                    ):
                        clean_email = em

            sources = [site_url]
            if comp_li:
                sources.append(comp_li)
            if pers_li:
                sources.append(pers_li)

            record = LeadRecord(
                company_name=enriched.company_name,
                location=enriched.location,
                industry_vertical=enriched.industry_vertical,
                employee_count_estimate=enriched.employee_count_estimate,
                company_website_url=site_url,
                email=clean_email,
                phone_number=enriched.phone_number,
                decision_maker_name=enriched.decision_maker_name,
                decision_maker_role=enriched.decision_maker_role,
                company_linkedin_url=comp_li,
                decision_maker_linkedin_url=pers_li,
                source_urls=sources,
                purchase_intent_signal=enriched.purchase_intent_signal,
                icp_score=enriched.icp_score,
                icp_reason=_format_reason_text(enriched.icp_reason_breakdown),
                icp_reason_details=enriched.icp_reason_breakdown,
            )

            seen_company_names.add(norm_name)
            verified_leads.append(record)
            if verbose:
                print(
                    f"[VERIFIED {len(verified_leads)}/{config.lead_count}] {record.company_name} | "
                    f"Phone: {record.phone_number} | Email: {record.email} | "
                    f"Leader: {record.decision_maker_name} ({record.decision_maker_role})",
                    flush=True,
                )

    final_leads = verified_leads[: config.lead_count]
    final_result = LeadFinderResult(
        product_or_service=config.product_description,
        target_region=config.target_location,
        total_leads=len(final_leads),
        leads=final_leads,
    )
    payload = final_result.model_dump()

    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    _log_step(
        f"COMPLETED: {payload['total_leads']}/{config.lead_count} Verified Leads Saved to '{output_json_path}'",
        verbose,
    )
    return payload


def run_lead_finder_from_payload(
    user_payload: Dict[str, Any],
    lead_count: int = 15,
    model_name: str = "gpt-4o-mini",
    verbose: bool = True,
    output_json_path: str = "verified_leads.json",
) -> dict:
    return asyncio.run(
        _run_lead_finder_async(
            user_payload=user_payload,
            lead_count=lead_count,
            model_name=model_name,
            verbose=verbose,
            output_json_path=output_json_path,
        )
    )


if __name__ == "__main__":
    user_payload = {
        "icp_prompt": (
            "Find genuine B2B companies in United States across B2B SaaS, agencies, professional services, "
            "and tech-enabled SMBs with a consultative sales pipeline. Companies should have about 5-100 "
            "employees, a sales team of roughly 2-15 people with no dedicated RevOps hire, and be at seed "
            "to Series B or bootstrapped stage with roughly $500K-$15M ARR. Prioritize companies where the "
            "founder is still involved in selling, that run on spreadsheets, a free CRM tier, or a lightly "
            "used HubSpot/Pipedrive, that hold sales conversations over WhatsApp, that have an inbound "
            "website with a pricing page, or that pay for multiple point tools (CRM, enrichment, visitor ID, "
            "note-taker). Target Founders, CEOs, Co-founders, Heads of Sales, Sales Leads, and Operations "
            "Managers. Exclude enterprises with 200+ reps, companies deeply invested in HubSpot's marketing "
            "hub, pure B2C or transactional e-commerce, and highly regulated industries needing HIPAA or "
            "FedRAMP. Provide verified company details, websites, decision-makers, and LinkedIn profiles. "
            "Avoid duplicates and fabricated leads."
        ),
        "exclude_existing_products": ["HubSpot Marketing Hub", "Salesforce"],
    }

    leads = run_lead_finder_from_payload(
        user_payload=user_payload,
        lead_count=15,
        model_name="gpt-4o-mini",
        verbose=True,
        output_json_path="verified_leads.json",
    )
    print(f"Total verified leads extracted: {leads['total_leads']}")
    print(json.dumps(leads, indent=2, ensure_ascii=False))
