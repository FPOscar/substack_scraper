"""
Purpose:
    Scrape paid subscription articles from a Substack newsletter, saving both HTML and Markdown versions.
    You must edit BASE_URL and SITEMAP_STRING below to match your target newsletter.

Instructions:
    - Set BASE_URL to the newsletter's main URL (e.g., "https://newsletter.eng-leadership.com")
    - Set SITEMAP_STRING to the sitemap path (e.g., "/sitemap.xml")
    - Use --paid flag to enable scraping paid content (manual login required)
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
from datetime import datetime, timedelta

BASE_URL = "https://thescienceofhitting.com"  # Change to your newsletter base URL
SITEMAP_STRING = "/sitemap.xml"  # Change if your sitemap path is different

SITEMAP_URL = BASE_URL + SITEMAP_STRING

OUTPUT_FILE = "articles.json"


def selenium_login():
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
    driver.get(BASE_URL)
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
        print("="*50)
        input("\nPress Enter after you have logged in to continue...")
    else:
        print("Already logged in! Using saved session.")
    
    print("Continuing with scraping...")
    return driver


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
            urls.append(url_text)
            url_to_lastmod[url_text] = lastmod.text if lastmod else ""
    return urls, url_to_lastmod


def extract_article_html_and_md(soup):
    # Prioritize content containers
    article = soup.find("div", class_="available-content")
    html_content = str(article)
    markdown_content = markdownify.markdownify(html_content, heading_style="ATX")
    return html_content, markdown_content


def scrape_article_selenium(driver, url):
    driver.get(url)
    sleep(0.3)
    soup = BeautifulSoup(driver.page_source, "lxml")
    return extract_article_html_and_md(soup)


def scrape_article_requests(url):
    resp = requests.get(url)
    soup = BeautifulSoup(resp.content, "lxml")
    return extract_article_html_and_md(soup)


def main():
    parser = argparse.ArgumentParser(description="Substack scraper")
    parser.add_argument("--paid", action="store_true", help="Enable scraping paid content (manual login required)")
    parser.add_argument("--days", type=int, default=None, help="Only scrape articles from the last N days (default: all articles)")
    args = parser.parse_args()

    driver = None
    if args.paid:
        print("Paid mode enabled. Manual login required.")
        driver = selenium_login()
    else:
        print("Paid mode not enabled. Scraping free content only.")

    print("Fetching sitemap...")
    urls, url_to_lastmod = get_article_urls_and_lastmod(SITEMAP_URL)
    print(f"Found {len(urls)} articles in sitemap.")

    # Filter articles by date if --days is specified
    if args.days is not None:
        cutoff_date = datetime.now() - timedelta(days=args.days)
        filtered_urls = []
        for url in urls:
            lastmod = url_to_lastmod.get(url, "")
            if lastmod:
                try:
                    # Parse ISO format date (e.g., "2025-01-15T12:00:00Z" or "2025-01-15")
                    date_str = lastmod.split("T")[0]
                    article_date = datetime.strptime(date_str, "%Y-%m-%d")
                    if article_date >= cutoff_date:
                        filtered_urls.append(url)
                except ValueError:
                    # If date parsing fails, include the article
                    filtered_urls.append(url)
            else:
                # If no lastmod, include the article
                filtered_urls.append(url)
        urls = filtered_urls
        print(f"Filtered to {len(urls)} articles from the last {args.days} days.")
    with open("urls.txt", "w") as url_file:
        for url in urls:
            url_file.write(url + "\n")
    print(f"Saved URLs to urls.txt")
    # Create folders for html and md files, clearing them first
    html_dir = "html_files"
    md_dir = "md_files"
    # Remove all files in html_files and md_files if they exist
    for folder in [html_dir, md_dir]:
        if os.path.exists(folder):
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
        else:
            os.makedirs(folder, exist_ok=True)
    results = []
    # for url in urls[:5]:  # to test on less articles
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
            results.append({"url": url, "html_file": html_path, "md_file": md_path})
    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Saved {len(results)} articles to {OUTPUT_FILE}")
    if driver:
        driver.quit()


if __name__ == "__main__":
    main()
