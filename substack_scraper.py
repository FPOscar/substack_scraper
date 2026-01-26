"""
Purpose:
    Scrape paid subscription articles from Substack newsletters, saving both HTML and Markdown versions.
    Supports scraping multiple substacks from a text file.

Instructions:
    - Use --substacks to specify a txt file with one substack base URL per line
    - Or set DEFAULT_SUBSTACK below for single-substack mode
    - Use --paid flag to enable scraping paid content (manual login required)
    - Use --days to filter articles by age
"""

import requests
from bs4 import BeautifulSoup
import lxml
import markdownify
import json
from selenium import webdriver
from time import sleep
import argparse
import os
import random
from datetime import datetime, timedelta

DEFAULT_SUBSTACK = "https://thescienceofhitting.com"  # Default if no --substacks file provided
SITEMAP_STRING = "/sitemap.xml"

OUTPUT_FILE = "articles.json"


def selenium_login(check_url=None):
    """Initialize Selenium driver and handle login if needed. Login is shared across all substacks."""
    # Use a persistent profile in the script directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    profile_dir = os.path.join(script_dir, "chrome_profile")
    
    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-extensions")
    
    driver = webdriver.Chrome(options=options)
    
    # Check if already logged in by looking for common logged-in indicators
    test_url = check_url or "https://substack.com"
    driver.get(test_url)
    sleep(2)
    
    # Check if we need to log in (look for subscribe/sign-in buttons as indicator of not logged in)
    page_source = driver.page_source.lower()
    needs_login = "sign in" in page_source and "subscribe" in page_source and "your account" not in page_source
    
    if needs_login:
        driver.get("https://substack.com/sign-in")
        print("\n" + "="*50)
        print("Please log into Substack in the browser window.")
        print("Once you see your account/dashboard, come back here.")
        print("(Your login will be saved for future runs!)")
        print("This login will work for ALL substacks you're scraping.")
        print("="*50)
        input("\nPress Enter after you have logged in to continue...")
    else:
        print("Already logged in! Using saved session.")
    
    print("Continuing with scraping...")
    return driver


def is_article_url(url):
    """Check if URL is an actual article (not archive, about, podcast, etc.)."""
    # Substack articles typically have /p/ in the URL
    if "/p/" in url:
        return True
    # Skip known non-article pages
    skip_patterns = [
        "/archive", "/about", "/podcast", "/subscribe", 
        "/recommendations", "/leaderboard", "/notes",
        "/badge", "/embed", "/gift"
    ]
    for pattern in skip_patterns:
        if url.rstrip("/").endswith(pattern) or f"{pattern}/" in url:
            return False
    # If URL is just the base domain, skip it
    path = url.replace("https://", "").replace("http://", "")
    if path.count("/") <= 1:  # e.g., "example.com" or "example.com/"
        return False
    return True


def get_article_urls_and_lastmod(sitemap_url):
    resp = requests.get(sitemap_url)
    soup = BeautifulSoup(resp.content, "xml")
    url_to_lastmod = {}
    urls = []
    for url_tag in soup.find_all("url"):
        loc = url_tag.find("loc")
        lastmod = url_tag.find("lastmod")
        if loc:
            url_text = loc.text
            # Only include actual article URLs
            if is_article_url(url_text):
                urls.append(url_text)
                url_to_lastmod[url_text] = lastmod.text if lastmod else ""
    return urls, url_to_lastmod


def extract_article_html_and_md(soup):
    # Prioritize content containers
    article = soup.find("div", class_="available-content")
    html_content = str(article)
    markdown_content = markdownify.markdownify(html_content, heading_style="ATX")
    return html_content, markdown_content


def random_delay():
    """Add a random delay between 0.5 and 3 seconds."""
    delay = random.uniform(0.5, 3.0)
    sleep(delay)


def is_rate_limited(content):
    """Check if page content indicates rate limiting."""
    content_lower = content.lower()
    return "too many requests" in content_lower or "rate limit" in content_lower


def scrape_article_selenium(driver, url, max_retries=3):
    """Scrape article with Selenium, with retry logic for rate limiting."""
    for attempt in range(max_retries):
        driver.get(url)
        sleep(0.3)
        page_content = driver.page_source
        
        if is_rate_limited(page_content):
            wait_time = (attempt + 1) * 10  # 10s, 20s, 30s
            print(f"  Rate limited! Waiting {wait_time}s before retry ({attempt + 1}/{max_retries})...")
            sleep(wait_time)
            continue
        
        soup = BeautifulSoup(page_content, "lxml")
        random_delay()
        return extract_article_html_and_md(soup)
    
    # If all retries failed, return what we have
    print(f"  Warning: Still rate limited after {max_retries} retries")
    soup = BeautifulSoup(driver.page_source, "lxml")
    return extract_article_html_and_md(soup)


def scrape_article_requests(url, max_retries=3):
    """Scrape article with requests, with retry logic for rate limiting."""
    for attempt in range(max_retries):
        resp = requests.get(url)
        
        # Check for HTTP 429 or rate limit text in response
        if resp.status_code == 429 or is_rate_limited(resp.text):
            wait_time = (attempt + 1) * 10  # 10s, 20s, 30s
            print(f"  Rate limited! Waiting {wait_time}s before retry ({attempt + 1}/{max_retries})...")
            sleep(wait_time)
            continue
        
        soup = BeautifulSoup(resp.content, "lxml")
        random_delay()
        return extract_article_html_and_md(soup)
    
    # If all retries failed, return what we have
    print(f"  Warning: Still rate limited after {max_retries} retries")
    soup = BeautifulSoup(resp.content, "lxml")
    return extract_article_html_and_md(soup)


def get_substack_name(base_url):
    """Extract substack name from URL for folder organization."""
    # e.g., "https://thescienceofhitting.com" -> "thescienceofhitting"
    # e.g., "https://www.yetanothervalueblog.com" -> "yetanothervalueblog"
    # e.g., "https://mbideepdives.substack.com" -> "mbideepdives"
    name = base_url.replace("https://", "").replace("http://", "")
    # Remove www. prefix if present
    if name.startswith("www."):
        name = name[4:]
    name = name.split(".")[0]  # Get first part before .com/.substack.com
    return name


def load_substacks(file_path):
    """Load substack base URLs from a text file."""
    substacks = []
    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):  # Skip empty lines and comments
                # Ensure URL doesn't have trailing slash
                substacks.append(line.rstrip("/"))
    return substacks


def scrape_single_substack(base_url, driver, args, all_results):
    """Scrape a single substack and return results."""
    substack_name = get_substack_name(base_url)
    sitemap_url = base_url + SITEMAP_STRING
    
    print(f"\n{'='*50}")
    print(f"Scraping: {substack_name} ({base_url})")
    print(f"{'='*50}")
    
    print("Fetching sitemap...")
    urls, url_to_lastmod = get_article_urls_and_lastmod(sitemap_url)
    print(f"Found {len(urls)} articles in sitemap.")
    
    # Filter articles by date if --days is specified
    if args.days is not None:
        cutoff_date = datetime.now() - timedelta(days=args.days)
        filtered_urls = []
        for url in urls:
            lastmod = url_to_lastmod.get(url, "")
            if lastmod:
                try:
                    date_str = lastmod.split("T")[0]
                    article_date = datetime.strptime(date_str, "%Y-%m-%d")
                    if article_date >= cutoff_date:
                        filtered_urls.append(url)
                except ValueError:
                    filtered_urls.append(url)
            else:
                filtered_urls.append(url)
        urls = filtered_urls
        print(f"Filtered to {len(urls)} articles from the last {args.days} days.")
    
    # Create substack-specific output folders
    html_dir = os.path.join("html_files", substack_name)
    md_dir = os.path.join("md_files", substack_name)
    
    for folder in [html_dir, md_dir]:
        if os.path.exists(folder):
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
        else:
            os.makedirs(folder, exist_ok=True)
    
    results = []
    for url in urls:
        print(f"Scraping {url}")
        if args.paid:
            html, md = scrape_article_selenium(driver, url)
        else:
            html, md = scrape_article_requests(url)
        if html and md:
            lastmod = url_to_lastmod.get(url, "")
            date_part = lastmod.split("T")[0] if lastmod else ""
            base_name = url.rstrip("/").split("/")[-1]
            if date_part:
                base_name = f"{date_part}_{base_name}"
            html_path = os.path.join(html_dir, base_name + ".html")
            md_path = os.path.join(md_dir, base_name + ".md")
            with open(html_path, "w", encoding="utf-8") as f_html:
                f_html.write(html)
            with open(md_path, "w", encoding="utf-8") as f_md:
                f_md.write(md)
                f_md.write(f"\n\n---\n\nSource: {url}\n")
            results.append({
                "substack": substack_name,
                "url": url,
                "html_file": html_path,
                "md_file": md_path
            })
    
    print(f"Scraped {len(results)} articles from {substack_name}")
    all_results.extend(results)
    return results


def main():
    parser = argparse.ArgumentParser(description="Substack scraper - scrape one or multiple substacks")
    parser.add_argument("--paid", action="store_true", help="Enable scraping paid content (manual login required)")
    parser.add_argument("--days", type=int, default=None, help="Only scrape articles from the last N days (default: all articles)")
    parser.add_argument("--substacks", type=str, default=None, help="Path to txt file with substack URLs (one per line)")
    args = parser.parse_args()

    # Determine which substacks to scrape
    if args.substacks:
        if not os.path.exists(args.substacks):
            print(f"Error: Substacks file '{args.substacks}' not found.")
            return
        substacks = load_substacks(args.substacks)
        print(f"Loaded {len(substacks)} substacks from {args.substacks}")
    else:
        substacks = [DEFAULT_SUBSTACK]
        print(f"No --substacks file provided, using default: {DEFAULT_SUBSTACK}")

    driver = None
    if args.paid:
        print("Paid mode enabled. Manual login required.")
        driver = selenium_login(substacks[0])  # Check login with first substack
    else:
        print("Paid mode not enabled. Scraping free content only.")

    all_results = []
    
    # Process each substack
    for i, base_url in enumerate(substacks):
        print(f"\n[{i+1}/{len(substacks)}] Processing substack...")
        try:
            scrape_single_substack(base_url, driver, args, all_results)
        except Exception as e:
            print(f"Error scraping {base_url}: {e}")
            continue
        
        # Add delay between substacks (except after the last one)
        if i < len(substacks) - 1:
            delay = random.uniform(2.0, 5.0)
            print(f"Waiting {delay:.1f}s before next substack...")
            sleep(delay)

    # Save combined results
    with open(OUTPUT_FILE, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\n{'='*50}")
    print(f"DONE! Saved {len(all_results)} total articles to {OUTPUT_FILE}")
    print(f"{'='*50}")
    
    if driver:
        driver.quit()


if __name__ == "__main__":
    main()
