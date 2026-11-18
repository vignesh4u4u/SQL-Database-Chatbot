# High-Level Project Plan: Automated ICP Lead Discovery & Qualification System

## 1. Executive Summary & Objective
The objective of this project is to build an automated, AI-driven B2B lead generation and qualification pipeline. Using **Serper.dev** for targeted web intelligence and a self-hosted **llama.cpp** inference server (`KOVAION_AI_URL`), the system will discover, verify, enrich, and output structured JSON data for companies in four major Texas markets (**Austin, Dallas, Houston, and San Antonio**) that align with a specific product offering and Ideal Customer Profile (ICP).

---

## 2. Ideal Customer Profile (ICP) Qualification Criteria
Every candidate lead must pass strict multi-point verification before being included in the final JSON output:
* **Geographic Constraint:** Must maintain an active office or headquarters in **Austin, Dallas, Houston, or San Antonio (Texas)**.
* **Company Size (Total Employees):** Must strictly fall between **11 and 50 employees**.
* **Sales Organization Size:** Must have an estimated sales team of **2 to 15 people** (derived from direct directory listings or proportional B2B team modeling).
* **Product-Market Fit:** The company’s industry, service offerings, or technology stack must demonstrate a clear use case for the target product.

---

## 3. Target Data Schema (12 Core Fields)
The system will normalize and output a deduplicated JSON payload containing a total `matched_count` and an array of qualified lead records with the following structure:
1. **Company Name** (Official legal or brand name)
2. **City** (Restricted to Austin, Dallas, Houston, or San Antonio)
3. **Industry / Niche** (Primary vertical or B2B specialization)
4. **Total Employees** (Verified 11–50 range)
5. **Estimated Sales Team Size** (Verified or modeled 2–15 range)
6. **Company Phone Number** (Main HQ switchboard or direct business line—consolidated from duplicate request fields)
7. **Company Website** (Verified root domain)
8. **Company LinkedIn URL** (Official corporate profile)
9. **Contact Person Name** (Primary decision-maker, e.g., Founder, CEO, VP of Sales, Director of Business Development)
10. **Contact Person Title** (Verified executive or sales leadership role)
11. **Contact Person LinkedIn URL** (Direct executive profile)
12. **Contact Phone Number** (Direct dial or mobile number, when publicly indexed)

---

## 4. Multi-Source Web Intelligence Strategy
Rather than relying on a single directory, the system uses **Serper.dev** search operators (`site:` dorks) across high-signal B2B repositories to triangulate company data:

* **Cloudtango (`cloudtango.net`):** Primary source for Managed Service Providers (MSPs), IT consultancies, and regional tech service firms in Texas.
* **Clutch (`clutch.co`):** Ideal for B2B agencies, software firms, and service providers, featuring verified `10 - 49` employee brackets, city tags, and headquarters phone numbers.
* **LinkedIn Company Pages (`linkedin.com/company`):** Authoritative source for confirming the `11-50 employees` headcount bracket, official website URLs, and industry classifications.
* **LinkedIn People Profiles (`linkedin.com/in`):** Used during the enrichment phase to identify sales leadership and C-suite decision-makers at verified companies.
* **B2B Data Aggregators (`zoominfo.com`, `apollo.io`):** Indexed snippets frequently expose HQ phone numbers, revenue brackets, and departmental headcount ratios (such as sales team size).
* **Local Business Registries (`bbb.org`, Texas Chambers of Commerce):** Secondary verification layer for local Texas phone numbers and principal executive names.

---

## 5. End-to-End System Architecture (4-Stage Pipeline)

### Stage 1: Multi-Directory Discovery
* The system generates targeted search queries combining the four Texas cities, niche keywords derived from the product description, and specific site operators (`cloudtango.net`, `clutch.co`, `linkedin.com`).
* Serper retrieves top organic snippets, titles, and URLs across these sources in parallel.

### Stage 2: Initial AI Filtering & Deduplication
* Raw search snippets are passed to the local **llama.cpp** model via its OpenAI-compatible endpoint.
* The LLM filters out enterprise firms (>50 employees), micro-businesses (<11 employees), and companies outside the four target cities.
* Duplicate companies appearing across multiple directories (e.g., both Cloudtango and Clutch) are merged into a single candidate profile.

### Stage 3: Targeted Deep Enrichment
* For every qualified candidate company from Stage 2, the pipeline executes secondary, company-specific Serper searches to fill missing attributes:
  * Query 1 targets the company's official website and HQ phone number.
  * Query 2 targets the company's LinkedIn page to double-check headcount brackets.
  * Query 3 targets decision-maker LinkedIn profiles (Sales Leaders, Founders, CEOs) and direct contact numbers.

### Stage 4: Schema Enforcement & Output Generation
* The local LLM merges the initial discovery data with the enrichment snippets.
* **Anti-Hallucination Guardrails:** The prompt strictly instructs the model to leave fields as `null` rather than guessing missing phone numbers or URLs.
* **Grammar-Constrained Output:** Using `llama.cpp` JSON schema enforcement, the system outputs a clean, machine-readable JSON file containing the exact count of matched leads and their structured records.

---

## 6. Operational Considerations & Best Practices
* **Rate Limiting & Cost Efficiency:** Batching discovery queries by city and niche minimizes Serper API credit consumption before running targeted per-company enrichment.
* **Context Window Management:** Snippets are stripped of unnecessary HTML/metadata before being sent to `llama.cpp` so local inference remains fast and accurate within the model's context limit.
* **Auditability:** Each lead record can optionally retain a confidence note explaining why the company matches the product interest and how the sales team size was verified or estimated.

## Final output:
- Final return clean two Python file code. one is app.py and another one lead_search_agent.py
- Code is very clean and code inside no comments or explanation .
- If user ask 10 leads means final output inside also must give the 10 leads data, not 2 or 5 strictly avoid not less data.
  
sample input payload format:

```json
{
  "icp_prompt": "We sell DelveAnt, an AI CRM for small B2B sales teams.\n\nFind IT services and software companies in Texas (Austin, Dallas, Houston, San Antonio).\nCompany size: 11-50 employees.\nSales team size: 2-15 salespeople.I need exactly 8 leads."
}
```


web search alrenative use :

docker run -d --name searxng -p 8080:8080 -v C:\searxng:/etc/searxng docker.io/searxng/searxng:latest