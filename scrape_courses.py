"""
scrape_courses.py

Single-run scraper for course data across multiple platforms using SerpAPI + BeautifulSoup.
Outputs: courses_data.xlsx (one sheet with all courses)

Notes:
- Configure MAX_URLS_PER_SEARCH to control how many SerpAPI results per category to fetch.
- Configure MAX_COURSES_FROM_LISTING to limit number of courses parsed from a listing page.
- Each SerpAPI search consumes 1 query from your SerpAPI quota.
"""

import os
import time
import requests
from bs4 import BeautifulSoup
import pandas as pd
from urllib.parse import urlparse, urljoin

# ---------------- CONFIG ----------------
SERPAPI_KEY = "b0eb9ae6d942cc5089ac78f69b93d1e812aad54bed266fadffafb702c6979d50"

COURSE_CATEGORIES = [
    "AI/ML course",
    "Full stack development course",
    "Data science course",
    "Cyber security course",
    "Advanced Python course",
    "Database management course"
]

PLATFORMS = [
    "coursera.org",
    "edx.org",
    "udemy.com",
    "pluralsight.com",
    "khanacademy.org",
    "freecodecamp.org"
]

# how many SerpAPI results to use per category (keeps SerpAPI usage low)
MAX_URLS_PER_SEARCH = 10

# when a search result is a listing page, limit number of extracted course links per listing
MAX_COURSES_FROM_LISTING = 8

# polite delay between requests (seconds)
PAGE_REQUEST_DELAY = 1.5

# output filename
OUTPUT_XLSX = "courses_data.xlsx"

# ---------------- UTILITIES ----------------

def serpapi_search(query, num_results=10):
    """Call SerpAPI search.json and return organic result URLs."""
    base = "https://serpapi.com/search.json"
    params = {
        "q": query,
        "engine": "google",
        "num": num_results,
        "api_key": SERPAPI_KEY
    }
    r = requests.get(base, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    urls = []
    for res in data.get("organic_results", []):
        link = res.get("link")
        if link:
            urls.append(link)
    return urls

def get_soup(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    r = requests.get(url, headers=headers, timeout=12)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def short_text(text, max_len=300):
    if not text:
        return "Not available"
    text = ' '.join(text.split())
    return text if len(text) <= max_len else text[:max_len].rsplit(' ', 1)[0] + "..."

# ---------------- PLATFORM-SPECIFIC PARSERS ----------------
# These parsers attempt to extract the requested fields. They are heuristics;
# some sites have complex JS rendering — if a field is not found, "Not available" is used.

def parse_common_meta(soup):
    """Try to extract common metadata-like fields from meta tags."""
    meta = {}
    # Course name
    title = None
    if soup.title and soup.title.string:
        title = soup.title.string
    og_title = soup.find('meta', property='og:title')
    if og_title and og_title.get('content'):
        title = og_title['content']
    meta['course_name'] = short_text(title or "Not available", 200)

    # description
    desc = None
    desc_tag = soup.find('meta', attrs={'name': 'description'}) or soup.find('meta', property='og:description')
    if desc_tag and desc_tag.get('content'):
        desc = desc_tag['content']
    meta['short_description'] = short_text(desc or "Not available", 300)

    return meta

def parse_coursera(url, soup):
    # heuristics for coursera
    meta = parse_common_meta(soup)
    meta['platform'] = "Coursera"
    meta['url'] = url

    # duration
    dur = soup.find(lambda tag: tag.name in ["span","div"] and "hours" in (tag.get_text() or "").lower())
    meta['duration'] = short_text(dur.get_text() if dur else "Not available")

    # rating
    rating = soup.find('span', attrs={'data-test': 'rating-text'})
    if not rating:
        rating = soup.find('div', class_='Hk4XNb')  # backup class pattern
    meta['rating'] = short_text(rating.get_text() if rating else "Not available")

    # skill level
    level = soup.find(string=lambda t: t and any(x in t.lower() for x in ["beginner", "intermediate", "advanced"]))
    meta['skill_level'] = short_text(level.strip() if level else "Not available")

    # price - often Coursera shows "Free" or "Paid"
    meta['price'] = "Not available"
    price_tag = soup.find(string=lambda t: t and ("free" in t.lower() or "paid" in t.lower() or "$" in t))
    if price_tag:
        meta['price'] = short_text(price_tag.strip())

    # skills covered - try to find syllabus or skills list
    skills = []
    for ul in soup.find_all('ul'):
        if any('skill' in (li.get_text() or "").lower() for li in ul.find_all('li')[:5]):
            skills = [li.get_text(strip=True) for li in ul.find_all('li')]
            break
    meta['skills_covered'] = ', '.join(skills) if skills else "Not available"

    return meta

def parse_udemy(url, soup):
    meta = parse_common_meta(soup)
    meta['platform'] = "Udemy"
    meta['url'] = url

    # duration (Udemy often has 'Total length' or 'Last updated' labels)
    dur = soup.find(string=lambda t: t and "total length" in t.lower())
    if dur:
        parent = dur.find_parent()
        meta['duration'] = short_text(parent.get_text()) if parent else "Not available"
    else:
        # try alternative
        dur_alt = soup.find('span', class_='ud-component--course-landing-page-udlite--curriculum-length')
        meta['duration'] = short_text(dur_alt.get_text() if dur_alt else "Not available")

    # rating
    rating = soup.find('span', class_='udlite-heading-sm star-rating--rating-number--2o8YM')
    if not rating:
        rating = soup.find('span', attrs={'data-purpose': 'rating-number'})
    meta['rating'] = short_text(rating.get_text() if rating else "Not available")

    # skill level
    level = soup.find(string=lambda t: t and any(x in t.lower() for x in ["beginner","all levels","intermediate","advanced"]))
    meta['skill_level'] = short_text(level.strip() if level else "Not available")

    # price
    price = soup.find('div', class_='price-text--price-part--Tu6MH')
    if not price:
        price = soup.find(string=lambda t: t and ("free" in t.lower() or "$" in t))
    meta['price'] = short_text(price.get_text() if price else "Not available")

    # skills - Udemy often lists objectives; try to capture them
    skills = []
    for section in soup.find_all(['ul','ol']):
        text = ' '.join(li.get_text() for li in section.find_all('li')[:8])
        if len(text) > 30:
            skills = [li.get_text(strip=True) for li in section.find_all('li')[:10]]
            break
    meta['skills_covered'] = ', '.join(skills) if skills else "Not available"

    return meta

def parse_edx(url, soup):
    meta = parse_common_meta(soup)
    meta['platform'] = "edX"
    meta['url'] = url

    # duration
    dur = soup.find(string=lambda t: t and ("weeks" in t.lower() or "hours" in t.lower()))
    meta['duration'] = short_text(dur.strip() if dur else "Not available")

    # rating (edX doesn't always show ratings on course pages)
    meta['rating'] = "Not available"

    # skill level
    level = soup.find(string=lambda t: t and any(x in t.lower() for x in ["introductory","intermediate","advanced","beginner"]))
    meta['skill_level'] = short_text(level.strip() if level else "Not available")

    # price
    price = soup.find(string=lambda t: t and ("verified" in t.lower() or "free" in t.lower() or "$" in t))
    meta['price'] = short_text(price.strip() if price else "Not available")

    # skills covered
    skills = []
    ul = soup.find('ul', class_='course-skills') or soup.find('ul', attrs={'aria-label': 'Skills'})
    if ul:
        skills = [li.get_text(strip=True) for li in ul.find_all('li')]
    meta['skills_covered'] = ', '.join(skills) if skills else "Not available"

    return meta

def parse_pluralsight(url, soup):
    meta = parse_common_meta(soup)
    meta['platform'] = "Pluralsight"
    meta['url'] = url

    meta['duration'] = short_text(soup.find(string=lambda t: t and "hours" in t.lower()) or "Not available")
    meta['rating'] = "Not available"
    meta['skill_level'] = short_text(soup.find(string=lambda t: t and any(x in t.lower() for x in ["beginner","intermediate","advanced"])) or "Not available")
    meta['price'] = "Paid (Pluralsight subscription)"
    meta['skills_covered'] = "Not available"
    return meta

def parse_khanacademy(url, soup):
    meta = parse_common_meta(soup)
    meta['platform'] = "Khan Academy"
    meta['url'] = url

    meta['duration'] = "Not available"
    meta['rating'] = "Free / Not rated"
    meta['skill_level'] = "Not available"
    meta['price'] = "Free"
    meta['skills_covered'] = "Not available"
    return meta

def parse_freecodecamp(url, soup):
    meta = parse_common_meta(soup)
    meta['platform'] = "FreeCodeCamp"
    meta['url'] = url
    meta['duration'] = "Not available"
    meta['rating'] = "Free / Not rated"
    meta['skill_level'] = "Not available"
    meta['price'] = "Free"
    meta['skills_covered'] = "Not available"
    return meta

def fallback_parser(url, soup):
    meta = parse_common_meta(soup)
    meta['platform'] = urlparse(url).netloc
    meta['url'] = url
    # best-effort: try to find duration/rating by keywords
    meta['duration'] = short_text(soup.find(string=lambda t: t and ("hours" in t.lower() or "weeks" in t.lower())) or "Not available")
    meta['rating'] = short_text(soup.find(string=lambda t: t and ("rating" in t.lower() or "stars" in t.lower())) or "Not available")
    meta['skill_level'] = short_text(soup.find(string=lambda t: t and any(x in t.lower() for x in ["beginner","intermediate","advanced"])) or "Not available")
    meta['price'] = short_text(soup.find(string=lambda t: t and ("free" in t.lower() or "$" in t or "paid" in t.lower())) or "Not available")
    meta['skills_covered'] = "Not available"
    return meta

# ---------------- LINK EXTRACTION FROM LISTING PAGES ----------------

def extract_course_links_from_listing(url, soup, domain):
    """
    From a listing/category page, attempt to extract multiple course page links.
    Simple heuristics: find <a> tags with hrefs containing platform domain and keywords like 'course'
    """
    links = set()
    for a in soup.find_all('a', href=True):
        href = a['href']
        # normalize
        if href.startswith("//"):
            href = "https:" + href
        if href.startswith("/"):
            parsed = urlparse(url)
            href = urljoin(f"{parsed.scheme}://{parsed.netloc}", href)
        if domain in href and ('course' in href or 'learn' in href or 'certificate' in href or 'program' in href):
            links.add(href.split('?')[0])
        # also add if anchor text looks like a course
        text = (a.get_text() or "").lower()
        if domain in href or domain in text:
            if any(k in text for k in ["course","program","specialization","path","certificate","bootcamp"]):
                links.add(href.split('?')[0])
        if len(links) >= MAX_COURSES_FROM_LISTING:
            break
    return list(links)

# ---------------- MAIN SCRAPING LOGIC ----------------

def route_and_parse(url):
    """
    Decide which parser to use based on domain and run it.
    Returns a dict with required fields.
    """
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    try:
        soup = get_soup(url)
    except Exception as e:
        print(f"Failed to fetch {url}: {e}")
        return None

    if "coursera.org" in netloc:
        return parse_coursera(url, soup)
    if "udemy.com" in netloc:
        return parse_udemy(url, soup)
    if "edx.org" in netloc:
        return parse_edx(url, soup)
    if "pluralsight.com" in netloc:
        return parse_pluralsight(url, soup)
    if "khanacademy.org" in netloc:
        return parse_khanacademy(url, soup)
    if "freecodecamp.org" in netloc:
        return parse_freecodecamp(url, soup)
    # generic fallback
    return fallback_parser(url, soup)

def is_listing_page(url, soup, domain):
    """
    Heuristic: if the page contains many links to course-like pages or contains words like 'courses' in URL/path/title.
    """
    path = urlparse(url).path.lower()
    title = (soup.title.string or "").lower() if soup.title else ""
    if "courses" in path or "courses" in title or "catalog" in path or "learn" in path:
        return True
    # also if there are many course-like <a> tags:
    count = 0
    for a in soup.find_all('a', href=True)[:200]:
        href = a['href']
        if any(k in href.lower() for k in ["course", "learn", "program", "specialization"]):
            count += 1
    return count >= 3

def scrape_for_category(category_query):
    """Perform one SerpAPI search, then scrape resulting URLs."""
    site_filter = " OR ".join([f"site:{p}" for p in PLATFORMS])
    query = f"{category_query} {site_filter}"
    print(f"\nSearching SerpAPI for: {query}")
    urls = serpapi_search(query, num_results=MAX_URLS_PER_SEARCH)
    print(f"Found {len(urls)} URLs from SerpAPI (using up to {MAX_URLS_PER_SEARCH}).")
    results = []
    for u in urls:
        time.sleep(PAGE_REQUEST_DELAY)
        print(f"\nProcessing: {u}")
        try:
            soup = get_soup(u)
        except Exception as e:
            print(f"  Could not fetch {u}: {e}")
            continue

        domain = urlparse(u).netloc.lower()
        # if listing page -> extract multiple course links
        if is_listing_page(u, soup, domain):
            print("  Detected listing page — extracting multiple course links...")
            links = extract_course_links_from_listing(u, soup, domain)
            print(f"  Found {len(links)} course links in listing (limiting to {MAX_COURSES_FROM_LISTING}).")
            for link in links:
                time.sleep(PAGE_REQUEST_DELAY)
                parsed = route_and_parse(link)
                if parsed:
                    # add the category as an extra field
                    parsed['category_query'] = category_query
                    results.append(parsed)
        else:
            # single course page — parse directly
            parsed = None
            try:
                parsed = route_and_parse(u)
            except Exception as e:
                print(f"  Error parsing {u}: {e}")
            if parsed:
                parsed['category_query'] = category_query
                results.append(parsed)

    return results

# ---------------- RUNNER ----------------

def main():
    all_data = []
    total_searches = 0

    for cat in COURSE_CATEGORIES:
        if total_searches >= len(COURSE_CATEGORIES):
            # defensive: we plan 1 search per category
            break
        print(f"\n=== CATEGORY: {cat} ===")
        cat_results = scrape_for_category(cat)
        print(f"Collected {len(cat_results)} course items for category '{cat}'.")
        all_data.extend(cat_results)
        total_searches += 1
        # small pause between SerpAPI searches (not strictly needed but polite)
        time.sleep(1.0)

    if not all_data:
        print("No data scraped. Exiting.")
        return

    # normalize output fields and make DataFrame
    rows = []
    for item in all_data:
        row = {
            "course_name": item.get("course_name", "Not available"),
            "platform": item.get("platform", "Not available"),
            "url": item.get("url", "Not available"),
            "duration": item.get("duration", "Not available"),
            "rating": item.get("rating", "Not available"),
            "skill_level": item.get("skill_level", "Not available"),
            "short_description": item.get("short_description", "Not available"),
            "price": item.get("price", "Not available"),
            "skills_covered": item.get("skills_covered", "Not available"),
            "category_query": item.get("category_query", "Not available")
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    df.to_excel(OUTPUT_XLSX, index=False)
    print(f"\nScraping complete. Saved {len(df)} rows to '{OUTPUT_XLSX}'.")

if __name__ == "__main__":
    main()
