import asyncio
import csv
import json
import os
import sys
import random
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse
from firecrawl import FirecrawlApp

# Add project root to path so 'agents' package is found when run directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from dotenv import load_dotenv
load_dotenv()

from agents.config import load_secrets
load_secrets()

import requests
from bs4 import BeautifulSoup
import anthropic
from openai import OpenAI
from firecrawl import FirecrawlApp
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from datetime import datetime, timezone, timedelta
from pymongo import MongoClient
# Optional libraries
try:
    import cloudscraper
except ImportError:
    cloudscraper = None

try:
    import httpx
except ImportError:
    httpx = None

# ─────────────────────────────────────────────
# CONFIGURATION  (keys loaded from AWS Secrets Manager via load_secrets())
# ─────────────────────────────────────────────
# Abhinav jobs
START_URL = "https://www.dice.com/jobs?filters.postedDate=ONE&filters.employerType=Recruiter&filters.workplaceTypes=Remote&countryCode=US&latitude=38.7945952&location=United+States&locationPrecision=Country&longitude=-106.5348379&q=Technical+lead+"
   # ← CHANGE THIS
# Sri ram 
# START_URL= "https://www.dice.com/jobs?filters.postedDate=ONE&filters.workplaceTypes=Remote&location=United+States&q=senior+software+engineer&latitude=38.7945952&longitude=-106.5348379&countryCode=US&locationPrecision=Country"
# srinivas jobs
#START_URL ="https://www.dice.com/jobs?filters.postedDate=ONE&filters.employmentType=CONTRACTS&filters.employerType=Recruiter&location=United+States&q=technical+specialist&latitude=38.7945952&longitude=-106.5348379&countryCode=US&locationPrecision=Country"
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
ANTHROPIC_API_KEY = os.getenv("CLAUDEAI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MONGODB_URI = os.getenv("MONGO_URI")
DATABASE_NAME = "IT_Jobs"
COLLECTION_NAME = "Scrapper_jobs_agent"

DELAY_SECONDS = 2.5
HEADLESS = True
MAX_PAGES = 10          # Stop after this many pages — prevents runaway scraping
MIN_NEW_LINKS = 3       # Stop if fewer than this many new links found on 2 consecutive pages
MAX_JOBS = 20           # Hard cap on job detail pages scraped per run

OUTPUT_LINKS_CSV = "all_links_unstop.csv"
JOB_LINKS_TXT = "filtered_job_links_unstop.txt"
ALL_JOBS_JSON = "all_jobs_metadata_unstop.json"

# ─────────────────────────────────────────────
# MONGODB
# ─────────────────────────────────────────────
def init_mongodb():
    try:
        client = MongoClient(MONGODB_URI)
        client.admin.command("ping")
        db = client[DATABASE_NAME]
        collection = db[COLLECTION_NAME]
        collection.create_index("url", unique=True)
        print(" MongoDB Connected")
        return collection
    except ConnectionFailure as e:
        print(" MongoDB Connection Failed:", e)
        return None


def insert_job_to_mongodb(collection, job_record):
    if collection is None:
        print(" No MongoDB collection")
        return False

    try:
        data = job_record.get("data", {})

        document = {
            "url": job_record.get("url"),
            "apply_link": job_record.get("apply_link"),
            "title": data.get("meta", {}).get("title"),
            "company": data.get("meta", {}).get("company"),
            "location": data.get("meta", {}).get("location"),
            "job_type": data.get("meta", {}).get("jobType"),
            "experience": data.get("meta", {}).get("experience"),
            "salary": data.get("meta", {}).get("salary"),
            "posted_date": data.get("meta", {}).get("posted"),
            "skills": data.get("meta", {}).get("skills", []),
            "responsibilities": data.get("meta", {}).get("responsibilities", []),
            "qualifications": data.get("meta", {}).get("qualifications", []),
            "benefits": data.get("meta", {}).get("benefits", []),
            "full_summary": data.get("full_summary"),
            "preparation_prompt": data.get("prompt"),
            "raw_text": job_record.get("raw_text", "")[:20000],
            "method_used": job_record.get("method_used"),
            "status": job_record.get("status", "success"),
            "scraped_at": datetime.now(timezone.utc),
            "processed": "False"
        }

        result = collection.insert_one(document)
        print(f" INSERTED ID: {result.inserted_id}")
        print(f" TITLE: {document.get('title')}")
        return True

    except DuplicateKeyError:
        print("⚠️ Duplicate URL skipped")
        return False

    except Exception as e:
        print("❌ Insert Error:", e)
        return False


import time
import random


def api_key_error(service, error):
    err = str(error).lower()
    if "429" in err or "rate limit" in err or "too many requests" in err:
        time.sleep(random.uniform(8, 15))
        return "retry"
    if "401" in err or "unauthorized" in err or "invalid api key" in err:
        print(f"{service}: Auth error (check API key)")
        return "auth_error"
    print(f"{service}: Error occurred")
    return "fail"


def with_retries(service, func, max_attempts=5):
    for _ in range(max_attempts):
        try:
            time.sleep(random.uniform(1, 3))
            return func()
        except Exception as e:
            action = api_key_error(service, e)
            if action == "retry":
                continue
            else:
                return None
    return None


def detect_site_type(html: str) -> str:
    h = html.lower()
    if any(x in h for x in ["infinite-scroll", "infinitescroll"]):
        return "infinite_scroll"
    if any(x in h for x in ["react", "vue", "__next", "nuxt", "angular"]):
        return "spa"
    return "static"


def normalize_links(raw_links):
    clean = []
    for l in raw_links:
        if isinstance(l, str):
            clean.append(l)
        elif isinstance(l, list):
            clean.extend([x for x in l if isinstance(x, str)])
        elif isinstance(l, dict):
            u = l.get("url") or l.get("href")
            if u and isinstance(u, str):
                clean.append(u)
    return clean


def is_valid_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    return url.startswith("http") and not any(
        x in url.lower() for x in ["javascript:", "mailto:", "tel:", "#", "telnet:", "file:"]
    )


def get_headers():
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }


# ─────────────────────────────────────────────
#  PAGINATION SCRAPER — FIXED VERSION
#  Fixes:
#  1. Playwright is now the primary link extractor;
#     Firecrawl only kicks in when Playwright finds < 5 links
#  2. Scroll check threshold raised to avoid premature stops
#  3. URL pagination checks for 404 before continuing
# ─────────────────────────────────────────────

async def run_pagination_scraper():
    if not FIRECRAWL_API_KEY or not (ANTHROPIC_API_KEY or OPENAI_API_KEY):
        print(" Missing FIRECRAWL_API_KEY or both ANTHROPIC_API_KEY and OPENAI_API_KEY")
        return []

    firecrawl = FirecrawlApp(api_key=FIRECRAWL_API_KEY)
    seen_links = set()
    all_links = []
    visited_pages = set()
    consecutive_low_pages = 0  # pages with fewer than MIN_NEW_LINKS new links

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        page = await browser.new_page()

        await page.goto(START_URL, wait_until="networkidle")
        await asyncio.sleep(DELAY_SECONDS)

        page_num = 0

        while True:
            page_num += 1
            current_url = page.url

            print(f"\n── Page {page_num} ── {current_url}")

            # Hard stop — prevents runaway scraping and wasted API credits
            if page_num > MAX_PAGES:
                print(f"[STOP] Reached MAX_PAGES limit ({MAX_PAGES})")
                break

            if current_url in visited_pages:
                print("[STOP] Duplicate page detected")
                break
            visited_pages.add(current_url)

            # ================= EXTRACT LINKS =================
            # Playwright is PRIMARY (free). Firecrawl only used as fallback
            # when Playwright finds fewer than 5 links — saves Firecrawl credits.
            links = await extract_links_playwright(page)
            print(f" [Playwright] Got {len(links)} links")

            if len(links) < 5:
                print(f" [Playwright] Too few links, trying Firecrawl as fallback...")
                fc_links = firecrawl_collect_links(firecrawl, current_url)
                if fc_links:
                    links = fc_links
                    print(f" [Firecrawl fallback] Got {len(links)} links")

            links = normalize_links(links)

            new_count = 0
            for url in links:
                if is_valid_url(url) and url not in seen_links:
                    seen_links.add(url)
                    all_links.append({"page": page_num, "url": url})
                    new_count += 1

            print(f" [+] {new_count} new links (Total: {len(all_links)})")

            # Stop if results are drying up — 2 consecutive pages below MIN_NEW_LINKS
            if new_count < MIN_NEW_LINKS:
                consecutive_low_pages += 1
                if consecutive_low_pages >= 2:
                    print(f"[STOP] 2 consecutive pages with fewer than {MIN_NEW_LINKS} new links — pagination exhausted")
                    break
            else:
                consecutive_low_pages = 0

            old_url = page.url

            # =========================================================
            #  1. INFINITE SCROLL
            #  FIX 2: Raised threshold to +10 so minor DOM changes
            #  don't falsely trigger "new content loaded"
            # =========================================================
            prev_count = len(await page.query_selector_all("a[href]"))

            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

            new_count_scroll = len(await page.query_selector_all("a[href]"))

            if new_count_scroll > prev_count + 10:
                print("[SCROLL] New content loaded, continuing same page")
                continue

            # =========================================================
            #  2. CLICK NEXT (unchanged)
            # =========================================================
            if await find_and_click_next(page):
                await wait_for_page(page, "dynamic")

                if page.url != old_url:
                    continue

            # =========================================================
            #  3. URL PAGINATION
            #  FIX 3: Added 404 detection so bad URLs don't silently stop
            # =========================================================
            next_url = None

            if "page=" in current_url:
                import re
                next_url = re.sub(r'page=\d+', f'page={page_num + 1}', current_url)
            else:
                if "?" in current_url:
                    next_url = current_url + f"&page={page_num + 1}"
                else:
                    next_url = current_url.rstrip("/") + f"/page-{page_num + 1}"

            if next_url and next_url != current_url:
                try:
                    print(f"[URL PAGINATION] Trying: {next_url}")
                    resp = await page.goto(next_url)
                    await wait_for_page(page, "dynamic")

                    # FIX 3: Check HTTP status — stop on 404/error pages
                    if resp and resp.status in (404, 403, 410):
                        print(f"[URL PAGINATION] Got HTTP {resp.status}, stopping URL pagination")
                    else:
                        # Also check page title for soft 404s
                        title = await page.title()
                        if "404" in title or "not found" in title.lower():
                            print(f"[URL PAGINATION] Soft 404 detected in title: '{title}'")
                        elif page.url != old_url:
                            continue
                except Exception as e:
                    print(f"[URL PAGINATION] Error: {e}")

            # =========================================================
            #  4. CLAUDE SELECTOR (unchanged)
            # =========================================================
            print("[Claude] Finding NEXT button...")

            data = await page.evaluate("""
                () => Array.from(document.querySelectorAll('a, button'))
                    .map(e => ({
                        text: e.innerText,
                        class: e.className,
                        aria: e.getAttribute('aria-label')
                    }))
            """)

            selector = claude_find_next(data)

            if selector != "NONE":
                try:
                    el = await page.query_selector(selector)
                    if el:
                        print(f"[Claude Click] {selector}")
                        await el.click()
                        await wait_for_page(page, "dynamic")

                        if page.url != old_url:
                            continue
                except:
                    pass

            # =========================================================
            #  STOP CONDITION
            # =========================================================
            print("[DONE] No more pages")
            break

        await browser.close()

    # ================= SAVE =================
    with open(OUTPUT_LINKS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["page", "url"])
        writer.writeheader()
        writer.writerows(all_links)

    print(f" Saved {len(all_links)} total links → {OUTPUT_LINKS_CSV}")

    return [item["url"] for item in all_links]


# ─────────────────────────────────────────────
# PLAYWRIGHT HELPERS
# ─────────────────────────────────────────────

async def extract_links_playwright(page):
    elements = await page.query_selector_all("a[href]")
    links = []

    for el in elements:
        try:
            href = await el.get_attribute("href")
            if href:
                absolute = urljoin(page.url, href)
                if is_valid_url(absolute):
                    links.append(absolute)
        except:
            continue

    print(f" [Playwright] Extracted {len(links)} links")
    return links


async def find_and_click_next(page):
    selectors = [
        "a[rel='next']",
        "button:has-text('Next')",
        "a:has-text('Next')",
        "[aria-label*='next' i]",
        ".next",
        ".pagination-next"
    ]

    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                print(f" [Next] Clicking {sel}")
                await el.click()
                return True
        except:
            continue

    return False


async def wait_for_page(page, site_type):
    try:
        if site_type == "static":
            await page.wait_for_load_state("load")
        else:
            await page.wait_for_load_state("networkidle")
    except PlaywrightTimeout:
        pass

    await asyncio.sleep(DELAY_SECONDS)


# ─────────────────────────────────────────────
# FIRECRAWL FALLBACK
# ─────────────────────────────────────────────
def firecrawl_collect_links(app, url):
    print(f" [Firecrawl] Scraping: {url}")
    try:
        result = app.scrape(url, formats=["html", "links"])
        links = result.links if hasattr(result, "links") and result.links else []
        return normalize_links(links)
    except Exception as e:
        print(f" [Firecrawl] Error: {e}")
        return []


# ─────────────────────────────────────────────
# LLM HELPER — Claude first, OpenAI fallback
# ─────────────────────────────────────────────
def call_llm(messages, system=None, max_tokens=4000, temperature=0) -> str | None:
    """Try Claude Haiku first; fall back to GPT-4o-mini on quota/rate errors."""
    if ANTHROPIC_API_KEY:
        try:
            kwargs = dict(
                model="claude-haiku-4-5-20251001",
                max_tokens=max_tokens,
                messages=messages,
            )
            if system:
                kwargs["system"] = system
            resp = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(**kwargs)
            return "".join(b.text for b in resp.content if hasattr(b, "text"))
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ["credit", "quota", "rate", "429", "402", "529", "overload", "insufficient"]):
                print(f"[Claude] Quota/rate limit hit, switching to OpenAI: {e}")
            else:
                print(f"[Claude] Error, switching to OpenAI: {e}")

    if OPENAI_API_KEY:
        try:
            oai_msgs = ([{"role": "system", "content": system}] if system else []) + messages
            resp = OpenAI(api_key=OPENAI_API_KEY).chat.completions.create(
                model="gpt-5.4-mini",
                max_completion_tokens=max_tokens,
                temperature=temperature,
                messages=oai_msgs,
            )
            return resp.choices[0].message.content
        except Exception as e:
            print(f"[OpenAI] Fallback also failed: {e}")

    return None


# ─────────────────────────────────────────────
# CLAUDE FALLBACK
# ─────────────────────────────────────────────
def claude_find_next(elements):
    """Find the CSS selector for the Next button — Claude first, OpenAI fallback."""
    print(" [LLM] Finding next button selector from DOM elements...")
    elements_text = json.dumps(elements[:80], ensure_ascii=False)
    txt = call_llm(
        messages=[{"role": "user", "content": f"""You are given a list of clickable elements (links and buttons) from a webpage.
Find the element that navigates to the NEXT page of results.
Return ONLY a CSS selector string that uniquely identifies it, or return NONE if not found.
Examples of valid returns: "a[aria-label='Next']", "button.next-page", "a:contains('Next')"

Elements:
{elements_text}"""}],
        max_tokens=100,
    )
    if not txt:
        return "NONE"
    return "NONE" if txt.strip().upper() == "NONE" else txt.strip()


def claude_find_next_url(html, current_url):
    """Find next page URL from HTML — Claude first, OpenAI fallback."""
    print(" [LLM] Trying to find next page URL...")
    txt = call_llm(
        messages=[{"role": "user", "content": f"""Find next page URL from this HTML.
Return ONLY full URL or NONE.

Current URL: {current_url}

HTML:
{html[:6000]}"""}],
        max_tokens=200,
    )
    if not txt:
        return None
    return None if txt.strip().upper() == "NONE" else txt.strip()


# ─────────────────────────────────────────────
# JOB LINK FILTERING
# ─────────────────────────────────────────────
def filter_job_links(all_links):
    if not all_links:
        return []

    # Only feed Claude enough links to find MAX_JOBS results — avoids token overflow
    candidate_links = all_links[:MAX_JOBS * 3]
    links_text = "\n".join(candidate_links)

    prompt = f"""
            You are given a list of URLs scraped from a website.

            Your task:
            Identify and return ONLY URLs that point to specific job posting pages (individual job listings), not general career or listing pages.

            A valid job URL typically:
            - Represents a single job detail page
            - Contains job-specific identifiers such as /job/, /position/, /vacancy/, or query params like ?jobId= or ?id=
            - Often includes unique slugs (e.g., /software-engineer-12345)

            Examples of VALID job URLs:
            - https://example.com/jobs/software-engineer-12345
            - https://example.com/job/98765

            Examples of INVALID URLs:
            - https://example.com/careers
            - https://example.com/jobs (listing page)
            - https://example.com/about

            Rules:
            - Only include links that clearly represent a single job posting
            - Favor precision over recall
            - Return at most {MAX_JOBS} URLs
            - Return output as a clean JSON array of URLs only. No explanation.

            Here is the list of URLs:
            {links_text}
            """

    text = call_llm(messages=[{"role": "user", "content": prompt}], max_tokens=4000, temperature=0)
    try:
        text = text.replace("```json", "").replace("```", "").strip()
        job_links = json.loads(text)
        print(f" Filtered {len(job_links)} job posting URLs")
        return job_links
    except Exception as e:
        print(f" Error in job filtering: {e}")
        return []


# ─────────────────────────────────────────────
# SINGLE JOB SCRAPING USING PYTHON TOOLS
# ─────────────────────────────────────────────
def scrape_url(url):
    print(f"\n→ Scraping: {url}")
    clean_text = ""
    apply_link = ""
    method_used = "failed"

    methods = [
        ("requests", lambda: requests_session_scrape(url)),
        ("cloudscraper", lambda: cloudscraper_scrape(url) if cloudscraper else None),
        ("httpx", lambda: httpx_scrape(url) if httpx else None),
        ("Playwright", lambda: playwright_scrape(url)),
    ]

    for name, func in methods:
        if not func:
            continue
        try:
            result = func()
            if result and len(result[0]) >= 300:
                clean_text, apply_link = result
                method_used = name
                print(f"   Success with {name} ({len(clean_text)} chars)")
                break
        except Exception as e:
            print(f"   {name} failed: {e}")

    return clean_text, apply_link, method_used


def requests_session_scrape(url):
    session = requests.Session()
    session.headers.update(get_headers())
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    return extract_text_and_links(resp.text, url)


def cloudscraper_scrape(url):
    scraper = cloudscraper.create_scraper()
    resp = scraper.get(url, timeout=20)
    return extract_text_and_links(resp.text, url)


def httpx_scrape(url):
    with httpx.Client(http2=True, headers=get_headers(), timeout=15, follow_redirects=True) as c:
        resp = c.get(url)
        return extract_text_and_links(resp.text, url)


def playwright_scrape(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=get_headers()["User-Agent"])
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(2)
        content = page.content()
        browser.close()
        return extract_text_and_links(content, url)


def extract_text_and_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    apply_link = ""

    keywords = ["apply now", "apply for this job", "easy apply", "quick apply"]
    for a in soup.find_all("a", href=True):
        if any(kw in a.get_text(strip=True).lower() for kw in keywords):
            href = a["href"]
            apply_link = urljoin(base_url, href) if href.startswith("/") else href
            break

    for tag in soup(["script", "style", "nav", "footer", "header", "iframe"]):
        tag.decompose()

    clean_text = soup.get_text(separator="\n", strip=True)
    return clean_text, apply_link


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    print(" Job Scraper with MongoDB Started\n")
    if not ANTHROPIC_API_KEY:
        print("Claude API key missing")
        return

    if not FIRECRAWL_API_KEY:
        print("Firecrawl API key missing")

    mongo_collection = init_mongodb()
    if mongo_collection is None:
        print("  Continuing without MongoDB (data will only be saved to JSON)")

    # Step 1: Paginated crawling
    print("Step 1: Crawling all pages...")
    all_links = asyncio.run(run_pagination_scraper())

    # Step 2: Filter job links
    print("\nStep 2: Filtering individual job links using Claude...")
    JOB_URLS = filter_job_links(all_links)

    if not JOB_URLS:
        print("No job links found.")
        return

    if len(JOB_URLS) > MAX_JOBS:
        print(f" Capping to {MAX_JOBS} jobs (found {len(JOB_URLS)})")
        JOB_URLS = JOB_URLS[:MAX_JOBS]

    with open(JOB_LINKS_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(JOB_URLS))

    # Step 3: Process each job
    print(f"\nStep 3: Processing {len(JOB_URLS)} job postings...\n")

    IST = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST)
    scraped_at_ist = now_ist.strftime("%d/%m/%Y %H:%M IST")

    SYSTEM_PROMPT = f"""You are a job analysis expert. The user gives you the full raw text from a job posting page.
            Read every part of it carefully and extract ALL details.
            IMPORTANT: The current date and time when this job is being scraped is: {scraped_at_ist}.
            Use this as the reference point when converting relative dates like "2 hours ago", "1 day ago", "3 days ago" into absolute dates.
            For example, if it is currently {scraped_at_ist} and the job says "11 hours ago", subtract 11 hours from {scraped_at_ist} to get the posted date.
            Always express the posted date in DD/MM/YYYY HH:MM IST format (do NOT use your training data date — use only the reference time above).
            Respond ONLY with a valid JSON object (no markdown fences, no extra text):
            {{
            "meta": {{
                "title": "",
                "company": "",
                "location": "",
                "jobType": "",
                "experience": "",
                "salary": "",
                "posted": "",
                "skills": [],
                "responsibilities": [],
                "qualifications": [],
                "benefits": []
            }},
            "full_summary": "A complete summary of the entire job posting in detail.",
            "prompt": "A detailed preparation guide covering: 1) Resume tailoring tips, 2) Key technical skills to highlight, 3) Likely interview questions with answer tips, 4) How to research the company, 5) Cover letter advice."
            }}"""

    all_results = []

    for i, url in enumerate(JOB_URLS, 1):
        print(f"[{i}/{len(JOB_URLS)}] {url}")

        clean_text, apply_link, method_used = scrape_url(url)

        if len(clean_text) < 300:
            record = {"url": url, "apply_link": apply_link, "method_used": "failed", "status": "failed", "data": {}}
        else:
            try:
                raw = call_llm(
                    messages=[{"role": "user", "content": f"Analyze this job:\n\n{clean_text[:12000]}"}],
                    system=SYSTEM_PROMPT,
                    max_tokens=4000,
                    temperature=0,
                )
                cleaned = raw.replace("```json", "").replace("```", "").strip()
                job_data = json.loads(cleaned)

                record = {
                    "url": url,
                    "apply_link": apply_link or "Not found",
                    "method_used": method_used,
                    "status": "success",
                    "data": job_data,
                    "raw_text": clean_text[:15000]
                }

                insert_job_to_mongodb(mongo_collection, record)

            except Exception as e:
                print(f"   LLM error: {e}")
                record = {"url": url, "apply_link": apply_link, "method_used": method_used, "status": "error", "data": {}}

        all_results.append(record)

        with open(ALL_JOBS_JSON, "a", encoding="utf-8") as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)

        if i < len(JOB_URLS):
            time.sleep(random.uniform(4, 8))

    print("\n Scraping completed successfully!")
    print(f"   Jobs processed : {len(all_results)}")
    print(f"   JSON backup    : {ALL_JOBS_JSON}")
    if mongo_collection is not None:
        print(f"   MongoDB        : {DATABASE_NAME}.{COLLECTION_NAME}")


if __name__ == "__main__":
    main()