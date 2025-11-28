
"""
broad_many_to_many_scraper.py

Restorative "high-yield" scraper:
- Searches all (platform, category) pairs (4 platforms x 6 categories = 24 SerpAPI searches).
- For each SerpAPI result, if it's a listing page, we extract multiple course links from that listing.
- Parsers are lenient: prefer JSON-LD, otherwise H1/meta/first large paragraph + lists.
- Output file: courses_data.xlsx (same folder); fallback timestamped file if Excel is open.

Requirements:
pip install requests beautifulsoup4 pandas openpyxl
"""

import requests, time, json, os
from bs4 import BeautifulSoup
import pandas as pd
from urllib.parse import urlparse, urljoin
from datetime import datetime

# ---------------- CONFIG ----------------
API_KEY = "b0eb9ae6d942cc5089ac78f69b93d1e812aad54bed266fadffafb702c6979d50"    # <<-- put your real key here
PLATFORMS = ["coursera.org", "edx.org", "pluralsight.com", "freecodecamp.org"]
PLATFORM_NAME_MAP = {
    "coursera.org": "Coursera",
    "edx.org": "edX",
    "pluralsight.com": "Pluralsight",
    "freecodecamp.org": "FreeCodeCamp"
}
CATEGORIES = [
    "AI/ML course",
    "Full stack development course",
    "Data science course",
    "Cyber security course",
    "Advanced python course",
    "Database management course"
]

MAX_URLS_PER_SEARCH = 5           # SerpAPI results per (platform, category)
MAX_COURSES_FROM_LISTING = 8      # how many course links to scrape from a listing page
PAGE_REQUEST_DELAY = 1.2
SERPAPI_DELAY = 0.8
REQUEST_TIMEOUT = 12
OUTPUT_XLSX = "courses_data.xlsx"

# ---------------- UTILITIES ----------------

def serpapi_search(query, num_results=5):
    base = "https://serpapi.com/search.json"
    params = {"engine":"google","q":query,"num":num_results,"api_key":API_KEY}
    try:
        r = requests.get(base, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json().get("organic_results", [])
    except Exception as e:
        print("SerpAPI error:", e)
        return []

def get_soup(url):
    headers = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def short_text(text, max_len=400):
    if not text: return ""
    s = " ".join(text.split())
    return s if len(s)<=max_len else s[:max_len].rsplit(" ",1)[0]+"..."

def remove_scripts(soup):
    for t in soup(["script","style","noscript"]):
        t.decompose()

def parse_jsonld_course(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            txt = script.string
            if not txt:
                continue
            data = json.loads(txt)
        except Exception:
            try:
                data = json.loads(script.get_text())
            except Exception:
                continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict):
                if "Course" in str(it.get("@type","")) or it.get("name") and it.get("description"):
                    return it
                graph = it.get("@graph")
                if graph and isinstance(graph, list):
                    for g in graph:
                        if isinstance(g, dict) and ("Course" in str(g.get("@type","")) or (g.get("name") and g.get("description"))):
                            return g
    return None

# Heuristic: treat as listing if many course-like anchors or URL contains /courses /learn /catalog /browse /search /topic /specialization
def is_listing_page(url, soup):
    path = urlparse(url).path.lower()
    title = (soup.title.string or "").lower() if soup.title else ""
    if any(k in path for k in ["/courses","/learn","/browse","/catalog","/topic","/specialization","/search","/discover"]):
        return True
    # count anchor hints
    count = 0
    for a in soup.find_all("a", href=True)[:200]:
        href = a['href'].lower()
        text = (a.get_text() or "").lower()
        if any(w in href for w in ["course","learn","certificate","program","specialization","path","bootcamp"]) or any(w in text for w in ["course","learn","certificate","specialization","program","path"]):
            count += 1
    return count >= 6

def extract_links_from_listing(url, soup, platform_domain):
    links = []
    parsed_base = urlparse(url)
    for a in soup.find_all("a", href=True):
        href = a['href']
        if href.startswith("//"):
            href = "https:" + href
        if href.startswith("/"):
            href = urljoin(f"{parsed_base.scheme}://{parsed_base.netloc}", href)
        href = href.split("#")[0].split("?")[0]
        if platform_domain in href and any(k in href.lower() for k in ["course","learn","program","specialization","certificate","path","bootcamp","courses"]):
            if href not in links:
                links.append(href)
        # also accept anchors with course-like text even if href domain differs (rare)
        text = (a.get_text() or "").lower()
        if platform_domain in href and any(t in text for t in ["course","learn","specialization","program"]):
            if href not in links:
                links.append(href)
        if len(links) >= MAX_COURSES_FROM_LISTING:
            break
    return links

# Wide but safe extractor: prefer JSON-LD, else h1/meta/first long paragraph, plus reasonable "what you'll learn" lists
def extract_course_info_lenient(url, soup, platform_domain):
    remove_scripts(soup)
    jsonld = parse_jsonld_course(soup)
    title = description = skills = level = ""
    if jsonld:
        title = jsonld.get("name") or jsonld.get("headline") or ""
        description = jsonld.get("description") or jsonld.get("summary") or ""
        # skills try: about, learningOutcome, teaches, keywords
        skills_list = []
        for key in ("about","learningOutcome","teaches","keywords","skills"):
            v = jsonld.get(key)
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        skills_list.append(item.get("name") or item.get("headline") or str(item))
                    else:
                        skills_list.append(str(item))
            elif isinstance(v, str):
                skills_list += [x.strip() for x in v.split(",") if x.strip()]
        if not skills_list:
            # sometimes "edu" nested graph
            g = jsonld.get("@graph")
            if isinstance(g, list):
                for gitem in g:
                    if isinstance(gitem, dict) and gitem.get("keywords"):
                        ks = gitem.get("keywords")
                        if isinstance(ks, str):
                            skills_list += [x.strip() for x in ks.split(",")]
        skills = ", ".join(dict.fromkeys([s for s in skills_list if s]))  # uniq preserve order
        level = jsonld.get("educationalLevel") or jsonld.get("audience") or ""
    # Fallbacks:
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)
    if not description:
        # large paragraph blocks: choose paragraph with >= 100 chars
        paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        long_para = ""
        for p in paras:
            if len(p) >= 120:
                long_para = p
                break
        description = long_para or (paras[0] if paras else "")
    if not skills:
        # try bullet lists around headings like "What you'll learn"
        for header in soup.find_all(['h2','h3','h4','strong']):
            txt = header.get_text().strip().lower()
            if any(k in txt for k in ["what you'll learn","you will learn","learning outcomes","skills you'll gain","what you'll be able to do","what you'll learn"]):
                ul = header.find_next_sibling("ul")
                if ul:
                    li_texts = [li.get_text(strip=True) for li in ul.find_all("li")]
                    if li_texts:
                        skills = ", ".join(li_texts)
                        break
        # generic fallback: collect first 6 list items on page
        if not skills:
            all_li = [li.get_text(strip=True) for li in soup.find_all("li")]
            if all_li:
                skills = ", ".join(all_li[:8])
    if not level:
        # search for occurrence of Beginner/Intermediate/Advanced in visible strings
        for s in soup.strings:
            st = s.strip()
            if "beginner" in st.lower():
                level = "Beginner"; break
            if "intermediate" in st.lower():
                level = "Intermediate"; break
            if "advanced" in st.lower():
                level = "Advanced"; break
    # normalize
    title = short_text(title, 300)
    description = short_text(description, 400)
    skills = short_text(skills, 400)
    level = short_text(level, 80)
    return title, description, skills, level

# ---------------- MAIN FLOW ----------------

def run_many_to_many():
    rows = []
    planned_searches = len(PLATFORMS) * len(CATEGORIES)
    used_searches = 0
    print(f"Planned {planned_searches} SerpAPI searches (platform × categories). Each will request up to {MAX_URLS_PER_SEARCH} results.")
    for platform in PLATFORMS:
        pname = PLATFORM_NAME_MAP.get(platform, platform)
        for category in CATEGORIES:
            used_searches += 1
            q = f'{category} site:{platform}'
            print(f"\n[{used_searches}/{planned_searches}] SerpAPI: {pname} <{category}>")
            res = serpapi_search(q, num_results=MAX_URLS_PER_SEARCH)
            time.sleep(SERPAPI_DELAY)

            for hit in res:
                link = hit.get("link") or hit.get("url") or hit.get("displayed_link") or ""
                if not link:
                    continue
                # normalize link
                link = link.split("#")[0].split("?")[0]

                # Fetch page (no strict filter — we will be lenient)
                print("  -> fetching:", link)
                try:
                    html = get_html_safe(link)
                except Exception as e:
                    print("    fetch failed:", e)
                    continue
                if not html:
                    print("    empty fetch, skipping")
                    continue
                soup = BeautifulSoup(html, "html.parser")

                # if listing page -> extract multiple course links and scrape each
                if is_listing_page(link, soup):
                    print("    detected listing page, extracting multiple course links...")
                    links = extract_links_from_listing(link, soup, platform)
                    print(f"    extracted {len(links)} links from listing")
                    for course_link in links:
                        time.sleep(PAGE_REQUEST_DELAY)
                        try:
                            html2 = get_html_safe(course_link)
                        except Exception:
                            continue
                        if not html2:
                            continue
                        soup2 = BeautifulSoup(html2, "html.parser")
                        title, desc, skills, level = extract_course_info_lenient(course_link, soup2, platform)
                        # accept even when one of title/desc exists
                        if not title and not desc:
                            print("      skipping extracted link: no title/desc")
                            continue
                        rows.append({
                            "course_title": title,
                            "short_description": desc,
                            "skills": skills,
                            "URL": course_link,
                            "course_level": level,
                            "platform": pname,
                            "category_query": category
                        })
                else:
                    # single page: parse directly leniently
                    title, desc, skills, level = extract_course_info_lenient(link, soup, platform)
                    if not title and not desc:
                        print("    skipping: no title/desc found on single page")
                        continue
                    rows.append({
                        "course_title": title,
                        "short_description": desc,
                        "skills": skills,
                        "URL": link,
                        "course_level": level,
                        "platform": pname,
                        "category_query": category
                    })
                # short pause to be polite
                time.sleep(PAGE_REQUEST_DELAY)
    return rows

# helper get_html_safe with retries:
def get_html_safe(url):
    headers = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    tries = 2
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as e:
            time.sleep(1 + i)
    return None

# ---------------- SAVE ----------------

def save_to_excel(rows, filename=OUTPUT_XLSX):
    df = pd.DataFrame(rows, columns=["course_title","short_description","skills","URL","course_level","platform","category_query"])
    if df.empty:
        print("No rows to save.")
        return
    try:
        df.to_excel(filename, index=False)
        print(f"Saved {len(df)} rows to {filename}")
    except PermissionError:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback = f"courses_data_{ts}.xlsx"
        df.to_excel(fallback, index=False)
        print(f"Permission denied for {filename}. Saved to {fallback} ({len(df)} rows).")

# ---------------- RUN ----------------

if __name__ == "__main__":
    if not API_KEY or API_KEY.strip().startswith("YOUR"):
        print("ERROR: Put your SerpAPI key into API_KEY variable in the script before running.")
    else:
        rows = run_many_to_many()
        save_to_excel(rows)
