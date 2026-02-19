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
import shutil
import random
import re
import subprocess
import sys
import importlib
from io import BytesIO
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

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


def get_article_title_from_filename(filename):
    """Extract a readable article title from a markdown filename."""
    name = os.path.splitext(filename)[0]
    if re.match(r"^\d{4}-\d{2}-\d{2}_", name):
        name = name[11:]
    title = name.replace("-", " ").replace("_", " ").strip()
    return title.title() if title else filename


def _markdown_inline_to_html(text):
    """Convert basic inline markdown into ReportLab-compatible HTML."""
    if not text:
        return ""

    # Replace markdown images with text placeholders for inline contexts.
    text = re.sub(
        r"!\[([^\]]*)\]\((https?://[^\s)]+)(?:\s+\"[^\"]*\")?\)",
        lambda m: f"[Image: {(m.group(1) or 'image').strip()}] {m.group(2).strip()}",
        text,
    )

    def apply_basic_formatting(segment):
        escaped_segment = escape(segment)
        escaped_segment = re.sub(
            r"`([^`]+)`",
            lambda m: f'<font face="Courier">{m.group(1)}</font>',
            escaped_segment,
        )
        escaped_segment = re.sub(r"\*\*([^\*]+)\*\*", r"<b>\1</b>", escaped_segment)
        escaped_segment = re.sub(r"(?<!\*)\*([^\*\n]+)\*(?!\*)", r"<i>\1</i>", escaped_segment)
        escaped_segment = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<i>\1</i>", escaped_segment)
        return escaped_segment

    # Keep link conversion conservative; malformed markdown remains plain text.
    link_pattern = re.compile(r"\[([^\]]+)\]\((https?://[^\s\"'<>]+)\)")
    output = []
    last_end = 0
    for match in link_pattern.finditer(text):
        output.append(apply_basic_formatting(text[last_end:match.start()]))
        label = apply_basic_formatting(match.group(1))
        href = escape(match.group(2))
        output.append(f'<link href="{href}">{label}</link>')
        last_end = match.end()
    output.append(apply_basic_formatting(text[last_end:]))
    return "".join(output)


def _extract_image_from_markdown_line(text):
    """Extract markdown image info from an image-only line."""
    line = text.strip()

    linked_image = re.match(
        r'^\[\!\[([^\]]*)\]\((https?://[^\s)]+)(?:\s+\"[^\"]*\")?\)\]\((https?://[^\s)]+)\)$',
        line,
    )
    if linked_image:
        alt_text = linked_image.group(1).strip() or "image"
        image_url = linked_image.group(3).strip()
        return alt_text, image_url

    plain_image = re.match(
        r'^\!\[([^\]]*)\]\((https?://[^\s)]+)(?:\s+\"[^\"]*\")?\)$',
        line,
    )
    if plain_image:
        alt_text = plain_image.group(1).strip() or "image"
        image_url = plain_image.group(2).strip()
        return alt_text, image_url

    return None, None


def _add_image_to_story(image_url, alt_text, story, styles, image_cls, paragraph_cls, image_cache):
    """Download and embed an image in the PDF, with link fallback."""
    image_bytes = image_cache.get(image_url)
    if image_bytes is None and image_url not in image_cache:
        try:
            resp = requests.get(
                image_url,
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 200 and resp.content:
                image_bytes = resp.content
                image_cache[image_url] = image_bytes
            else:
                image_cache[image_url] = False
        except Exception:
            image_cache[image_url] = False

    image_bytes = image_cache.get(image_url)
    if image_bytes and image_bytes is not False:
        try:
            flowable = image_cls(BytesIO(image_bytes))
            flowable.hAlign = "CENTER"
            flowable._restrictSize(460, 460)
            story.append(flowable)
            story.append(styles["Spacer"](1, 6))
            caption = _markdown_inline_to_html(alt_text or "image")
            story.append(paragraph_cls(caption, styles["ImageCaption"]))
            story.append(styles["Spacer"](1, 10))
            return
        except Exception:
            pass

    fallback = f'[Image: {escape(alt_text or "image")}] <link href="{escape(image_url)}">{escape(image_url)}</link>'
    story.append(paragraph_cls(fallback, styles["Body"]))
    story.append(styles["Spacer"](1, 8))


def _append_markdown_to_story(md_text, story, styles, hr_cls, pre_cls, image_cls, image_cache):
    """Render markdown into a simple, clean PDF structure."""
    lines = md_text.splitlines()
    para_buffer = []
    in_code_block = False
    code_buffer = []

    def add_small_spacer():
        story.append(styles["Spacer"](1, 8))

    def flush_paragraph():
        if not para_buffer:
            return
        text = " ".join(part.strip() for part in para_buffer if part.strip())
        para_buffer.clear()
        if not text:
            return

        source_match = re.match(r"^Source:\s+(https?://\S+)\s*$", text, flags=re.IGNORECASE)
        if source_match:
            url = escape(source_match.group(1))
            text = f'Source: <link href="{url}">{url}</link>'
        else:
            text = _markdown_inline_to_html(text)

        story.append(pre_cls(text, styles["Body"]))
        add_small_spacer()

    heading_styles = {
        1: "Heading1",
        2: "Heading2",
        3: "Heading3",
    }

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_paragraph()
            if in_code_block:
                if code_buffer:
                    story.append(pre_cls("\n".join(code_buffer), styles["Code"]))
                    add_small_spacer()
                code_buffer = []
                in_code_block = False
            else:
                in_code_block = True
            continue

        if in_code_block:
            code_buffer.append(line)
            continue

        if not stripped:
            flush_paragraph()
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            flush_paragraph()
            level = min(len(heading_match.group(1)), 3)
            heading_text = _markdown_inline_to_html(heading_match.group(2))
            story.append(pre_cls(heading_text, styles[heading_styles[level]]))
            add_small_spacer()
            continue

        if re.match(r"^---+$", stripped):
            flush_paragraph()
            story.append(hr_cls(width="100%"))
            add_small_spacer()
            continue

        alt_text, image_url = _extract_image_from_markdown_line(stripped)
        if image_url:
            flush_paragraph()
            _add_image_to_story(image_url, alt_text, story, styles, image_cls, pre_cls, image_cache)
            continue

        bullet_match = re.match(r"^[-*]\s+(.*)$", stripped)
        if bullet_match:
            flush_paragraph()
            bullet_text = _markdown_inline_to_html(bullet_match.group(1))
            story.append(pre_cls(bullet_text, styles["Bullet"], bulletText="-"))
            continue

        numbered_match = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if numbered_match:
            flush_paragraph()
            bullet_text = _markdown_inline_to_html(numbered_match.group(2))
            story.append(pre_cls(bullet_text, styles["Bullet"], bulletText=f'{numbered_match.group(1)}.'))
            continue

        para_buffer.append(stripped)

    flush_paragraph()


def create_archive_pdf(archive_dir):
    """Create a combined PDF from archived markdown files with a linked TOC."""
    if not archive_dir or not os.path.exists(archive_dir):
        print("Archive directory not found, skipping PDF generation.")
        return

    md_files = sorted([name for name in os.listdir(archive_dir) if name.endswith(".md")])
    if not md_files:
        print("No markdown files found in archive, skipping PDF generation.")
        return

    try:
        importlib.import_module("reportlab")
    except ImportError:
        print("reportlab not installed. Installing now...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "reportlab"])

    colors = importlib.import_module("reportlab.lib.colors")
    pagesizes = importlib.import_module("reportlab.lib.pagesizes")
    styles_module = importlib.import_module("reportlab.lib.styles")
    platypus = importlib.import_module("reportlab.platypus")

    LETTER = pagesizes.LETTER
    ParagraphStyle = styles_module.ParagraphStyle
    getSampleStyleSheet = styles_module.getSampleStyleSheet
    HRFlowable = platypus.HRFlowable
    PageBreak = platypus.PageBreak
    Paragraph = platypus.Paragraph
    SimpleDocTemplate = platypus.SimpleDocTemplate
    Spacer = platypus.Spacer
    Image = platypus.Image

    pdf_path = os.path.join(archive_dir, "combined_archive.pdf")
    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=LETTER,
        leftMargin=50,
        rightMargin=50,
        topMargin=50,
        bottomMargin=50,
        title="Substack Archive",
    )

    base_styles = getSampleStyleSheet()
    styles = {
        "Title": ParagraphStyle(
            "ArchiveTitle",
            parent=base_styles["Title"],
            fontSize=24,
            spaceAfter=14,
            alignment=1,
        ),
        "Subtitle": ParagraphStyle(
            "ArchiveSubtitle",
            parent=base_styles["Normal"],
            fontSize=11,
            textColor=colors.grey,
            alignment=1,
            spaceAfter=20,
        ),
        "TocHeading": ParagraphStyle(
            "TocHeading",
            parent=base_styles["Heading1"],
            spaceAfter=12,
        ),
        "TocEntry": ParagraphStyle(
            "TocEntry",
            parent=base_styles["Normal"],
            leftIndent=16,
            leading=16,
            textColor=colors.HexColor("#1f4e79"),
            spaceAfter=4,
        ),
        "ArticleHeading": ParagraphStyle(
            "ArticleHeading",
            parent=base_styles["Heading1"],
            fontSize=18,
            spaceAfter=10,
        ),
        "Heading1": ParagraphStyle("MdH1", parent=base_styles["Heading1"], fontSize=16, spaceAfter=6),
        "Heading2": ParagraphStyle("MdH2", parent=base_styles["Heading2"], fontSize=14, spaceAfter=6),
        "Heading3": ParagraphStyle("MdH3", parent=base_styles["Heading3"], fontSize=12, spaceAfter=4),
        "Body": ParagraphStyle("MdBody", parent=base_styles["BodyText"], leading=16),
        "Bullet": ParagraphStyle("MdBullet", parent=base_styles["BodyText"], leftIndent=20, leading=16),
        "Code": ParagraphStyle(
            "MdCode",
            parent=base_styles["Code"],
            fontName="Courier",
            fontSize=9,
            leading=12,
            backColor=colors.HexColor("#f5f5f5"),
            borderPadding=6,
        ),
        "ImageCaption": ParagraphStyle(
            "ImageCaption",
            parent=base_styles["Italic"],
            alignment=1,
            textColor=colors.grey,
            fontSize=9,
        ),
        "Spacer": Spacer,
    }
    image_cache = {}

    story = []
    story.append(Paragraph("Substack Daily Archive", styles["Title"]))
    story.append(Paragraph(f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}', styles["Subtitle"]))
    story.append(Paragraph("Table of Contents", styles["TocHeading"]))

    for index, md_file in enumerate(md_files, start=1):
        article_title = get_article_title_from_filename(md_file)
        toc_line = (
            f'{index}. <link href="#article_{index}">{escape(article_title)}</link>'
        )
        story.append(Paragraph(toc_line, styles["TocEntry"]))

    story.append(PageBreak())

    for index, md_file in enumerate(md_files, start=1):
        md_path = os.path.join(archive_dir, md_file)
        with open(md_path, "r", encoding="utf-8") as handle:
            md_text = handle.read()

        article_title = get_article_title_from_filename(md_file)
        heading = f'<a name="article_{index}"/>{escape(article_title)}'
        story.append(Paragraph(heading, styles["ArticleHeading"]))
        story.append(Paragraph(f"File: {escape(md_file)}", styles["Subtitle"]))
        _append_markdown_to_story(md_text, story, styles, HRFlowable, Paragraph, Image, image_cache)

        if index < len(md_files):
            story.append(PageBreak())

    doc.build(story)
    print(f"Created combined PDF archive: {pdf_path}")


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
        # Compare date-only values so `--days 1` reliably includes yesterday.
        cutoff_date = (datetime.now() - timedelta(days=args.days)).date()
        filtered_urls = []
        for url in urls:
            lastmod = url_to_lastmod.get(url, "")
            if lastmod:
                try:
                    date_str = lastmod.strip()[:10]  # Expected YYYY-MM-DD
                    article_date = datetime.strptime(date_str, "%Y-%m-%d").date()
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


def archive_md_files(md_base="md_files"):
    """Copy all MD files into a timestamped subfolder for archival."""
    if not os.path.exists(md_base):
        print("No md_files directory found, skipping archive.")
        return None

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    archive_dir = os.path.join(md_base, "_archive", timestamp)

    os.makedirs(archive_dir, exist_ok=True)

    count = 0
    for substack_name in sorted(os.listdir(md_base)):
        substack_dir = os.path.join(md_base, substack_name)
        if not os.path.isdir(substack_dir) or substack_name == "_archive":
            continue
        for md_file in sorted(os.listdir(substack_dir)):
            if not md_file.endswith(".md"):
                continue
            src = os.path.join(substack_dir, md_file)
            shutil.copy2(src, os.path.join(archive_dir, md_file))
            count += 1

    if count:
        print(f"Archived {count} MD files to {archive_dir}")
        return archive_dir
    else:
        print("No MD files found to archive.")
        return None


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

    # Archive all MD files into a timestamped subfolder
    archive_dir = archive_md_files()
    create_archive_pdf(archive_dir)

    if driver:
        driver.quit()


if __name__ == "__main__":
    main()
