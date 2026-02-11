"""
Google Maps med spa lead generation scraper.

This script:
1. Iterates through three hardcoded advanced Google Maps search strings.
2. Scrolls the lazy-loaded left results panel until no additional cards appear.
3. Opens each result card to extract detailed business information.
4. Deduplicates leads using (business name + address).
5. Exports the final dataset to med_spa_leads_2026.csv.

Requirements:
    pip install selenium webdriver-manager
"""

from __future__ import annotations

import csv
import random
import re
import time
from urllib.parse import quote_plus
from dataclasses import dataclass
from typing import List, Set, Tuple

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    # Preferred path requested in the original task.
    from webdriver_manager.chrome import ChromeDriverManager
except ModuleNotFoundError:
    ChromeDriverManager = None


SEARCH_QUERIES = [
    'category:"medical_spa" in Florida * 4.5..5 stars',
    'category:"medical_spa" in Texas * 4.0..5 stars',
    'category:"medical_spa" near "Boca Raton, FL" price:$$..$$$$',
]

OUTPUT_FILE = "med_spa_leads_2026.csv"
HEADLESS = True  # Toggle to False if you want to watch the browser actions.


@dataclass
class Lead:
    name: str = ""
    rating: str = ""
    reviews: str = ""
    category: str = ""
    address: str = ""
    phone: str = ""
    website: str = ""


def human_sleep(min_s: float = 2.0, max_s: float = 5.0) -> None:
    """Sleep a random interval to mimic human browsing behavior."""
    time.sleep(random.uniform(min_s, max_s))


def build_driver(headless: bool = True) -> webdriver.Chrome:
    """Initialize Chrome with a standard user agent and optional headless mode."""
    chrome_options = Options()

    # Use the modern headless mode when requested.
    if headless:
        chrome_options.add_argument("--headless=new")

    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--lang=en-US")

    # Standard desktop Chrome user agent string.
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )

    # If webdriver-manager is available, use it.
    # If not, Selenium Manager (built into Selenium 4.6+) will auto-resolve driver.
    if ChromeDriverManager is not None:
        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=chrome_options)

    return webdriver.Chrome(options=chrome_options)


def close_popups_if_present(driver: webdriver.Chrome) -> None:
    """Best-effort close of consent/privacy dialogs that can block interaction."""
    possible_buttons = [
        "//button[contains(., 'Accept all')]",
        "//button[contains(., 'I agree')]",
        "//button[contains(., 'Reject all')]",
    ]
    for xpath in possible_buttons:
        try:
            btn = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, xpath)))
            btn.click()
            human_sleep(1, 2)
            break
        except TimeoutException:
            continue


def open_maps_home(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    """
    Open Google Maps and wait for UI readiness.

    Some machines get a region/consent shell where `input#searchboxinput` never renders.
    We accept either the standard searchbox OR the left feed as readiness signals.
    """
    candidates = [
        "https://www.google.com/maps?hl=en",
        "https://maps.google.com/?hl=en",
    ]

    last_error: Exception | None = None
    for url in candidates:
        try:
            driver.get(url)
            close_popups_if_present(driver)

            wait.until(
                lambda d: (
                    len(d.find_elements(By.CSS_SELECTOR, "input#searchboxinput")) > 0
                    or len(d.find_elements(By.CSS_SELECTOR, "div[role='feed']")) > 0
                )
            )
            return
        except TimeoutException as exc:
            last_error = exc

    raise TimeoutException(
        "Google Maps UI did not become ready (search box/feed not found)."
    ) from last_error


def open_query_via_url(driver: webdriver.Chrome, wait: WebDriverWait, query: str) -> None:
    """Fallback navigation path: open a query directly via URL parameters."""
    encoded = quote_plus(query)
    driver.get(f"https://www.google.com/maps/search/?api=1&hl=en&query={encoded}")
    close_popups_if_present(driver)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[role='feed'] div.Nv2PK")))


def submit_query(driver: webdriver.Chrome, wait: WebDriverWait, query: str) -> None:
    """Enter and submit a query in Google Maps."""
    try:
        search_input = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "input#searchboxinput")))
        search_input.clear()
        search_input.send_keys(query)

        # CSS selector explanation:
        # button#searchbox-searchbutton is the magnifier button used to run the search.
        driver.find_element(By.CSS_SELECTOR, "button#searchbox-searchbutton").click()

        # Wait until at least one result card appears.
        # CSS selector explanation:
        # div[role='feed'] is the scrollable left panel containing result cards in list mode.
        # div.Nv2PK is a common class attached to each individual result card.
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[role='feed'] div.Nv2PK")))
        human_sleep()
    except TimeoutException:
        # Fallback for environments where the search box is not interactable/visible.
        open_query_via_url(driver, wait, query)
        human_sleep()


def scroll_results_panel(driver: webdriver.Chrome, max_idle_rounds: int = 6) -> List:
    """
    Scroll the left results panel until no new cards load for several rounds.

    Returns all result card WebElements found after scrolling.
    """
    # CSS selector explanation:
    # div[role='feed'] points to the left sidebar list container that supports lazy loading.
    feed = driver.find_element(By.CSS_SELECTOR, "div[role='feed']")

    previous_count = 0
    idle_rounds = 0

    while idle_rounds < max_idle_rounds:
        cards = driver.find_elements(By.CSS_SELECTOR, "div[role='feed'] div.Nv2PK")
        current_count = len(cards)

        if current_count > previous_count:
            previous_count = current_count
            idle_rounds = 0
        else:
            idle_rounds += 1

        # JS scrollBy is required by the task; this scrolls inside the feed element.
        driver.execute_script("arguments[0].scrollBy(0, 1200);", feed)
        human_sleep(2, 5)

    return driver.find_elements(By.CSS_SELECTOR, "div[role='feed'] div.Nv2PK")


def _text_or_empty(driver: webdriver.Chrome, by: By, selector: str) -> str:
    try:
        return driver.find_element(by, selector).text.strip()
    except NoSuchElementException:
        return ""


def _extract_from_button_label(driver: webdriver.Chrome, data_item_id: str, prefix_pattern: str) -> str:
    """Extract content from the aria-label text of a detail button."""
    try:
        btn = driver.find_element(By.CSS_SELECTOR, f"button[data-item-id='{data_item_id}']")
        label = (btn.get_attribute("aria-label") or "").strip()
        cleaned = re.sub(prefix_pattern, "", label, flags=re.IGNORECASE).strip(" :")
        return cleaned
    except NoSuchElementException:
        return ""


def parse_rating_reviews(raw_text: str) -> Tuple[str, str]:
    """Parse strings like '4.8(245)' or '4.6 (1,120)' into rating and review count."""
    if not raw_text:
        return "", ""

    rating_match = re.search(r"(\d+(?:\.\d+)?)", raw_text)
    reviews_match = re.search(r"\(([^\)]+)\)", raw_text)

    rating = rating_match.group(1) if rating_match else ""
    reviews = reviews_match.group(1).replace(",", "") if reviews_match else ""
    return rating, reviews


def extract_details(driver: webdriver.Chrome, wait: WebDriverWait) -> Lead:
    """Extract business details from an opened place panel."""
    # XPath selector explanation:
    # //h1[contains(@class, 'DUwDvf')] locates the place title in the details panel header.
    name = _text_or_empty(driver, By.XPATH, "//h1[contains(@class, 'DUwDvf')]")

    # CSS selector explanation:
    # div.F7nice span[aria-hidden='true'] captures text where rating/review metadata is shown.
    rating_text = _text_or_empty(driver, By.CSS_SELECTOR, "div.F7nice span[aria-hidden='true']")
    rating, reviews = parse_rating_reviews(rating_text)

    # CSS selector explanation:
    # button[jsaction*='pane.rating.category'] is the category chip/button near rating block.
    category = _text_or_empty(driver, By.CSS_SELECTOR, "button[jsaction*='pane.rating.category']")

    # CSS selector explanation:
    # button[data-item-id='address'] is the address row in place details.
    address = _extract_from_button_label(driver, "address", r"^Address")

    # CSS selector explanation:
    # button[data-item-id^='phone:tel:'] is the phone row; starts-with handles formatting differences.
    phone = ""
    try:
        phone_button = driver.find_element(By.CSS_SELECTOR, "button[data-item-id^='phone:tel:']")
        phone_label = (phone_button.get_attribute("aria-label") or "").strip()
        phone = re.sub(r"^Phone", "", phone_label, flags=re.IGNORECASE).strip(" :")
    except NoSuchElementException:
        pass

    # CSS selector explanation:
    # a[data-item-id='authority'] is the official website link row.
    website = ""
    try:
        website_link = driver.find_element(By.CSS_SELECTOR, "a[data-item-id='authority']")
        website = (website_link.get_attribute("href") or "").strip()
    except NoSuchElementException:
        pass

    # Small wait to reduce race condition when detail panel transitions.
    wait.until(EC.presence_of_element_located((By.XPATH, "//h1[contains(@class, 'DUwDvf') or @class='fontHeadlineLarge']")))

    return Lead(
        name=name,
        rating=rating,
        reviews=reviews,
        category=category,
        address=address,
        phone=phone,
        website=website,
    )


def collect_for_query(driver: webdriver.Chrome, wait: WebDriverWait, query: str, seen_keys: Set[Tuple[str, str]]) -> List[Lead]:
    """Run one query, scrape all visible businesses, and deduplicate by (name, address)."""
    print(f"\n[INFO] Searching: {query}")
    submit_query(driver, wait, query)

    cards = scroll_results_panel(driver)
    print(f"[INFO] Cards discovered after scrolling: {len(cards)}")

    query_leads: List[Lead] = []

    for idx in range(len(cards)):
        # Reacquire card references each loop because the DOM updates frequently after clicks.
        cards = driver.find_elements(By.CSS_SELECTOR, "div[role='feed'] div.Nv2PK")
        if idx >= len(cards):
            break

        card = cards[idx]

        try:
            # JS click is often more reliable on Maps cards than native click.
            driver.execute_script("arguments[0].click();", card)
            human_sleep(2, 5)

            # Wait for details panel title to appear before extraction.
            wait.until(EC.presence_of_element_located((By.XPATH, "//h1[contains(@class, 'DUwDvf')]")))
            lead = extract_details(driver, wait)

            key = (lead.name.strip().lower(), lead.address.strip().lower())
            if lead.name and lead.address and key not in seen_keys:
                seen_keys.add(key)
                query_leads.append(lead)
                print(f"  [+] Added: {lead.name}")
            else:
                print(f"  [=] Skipped duplicate/incomplete: {lead.name or 'Unknown'}")

        except (TimeoutException, WebDriverException) as exc:
            print(f"  [!] Failed card #{idx + 1}: {exc}")

    return query_leads


def write_csv(leads: List[Lead], path: str) -> None:
    """Write output CSV in UTF-8 with required headers."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Name", "Rating", "Reviews", "Category", "Address", "Phone", "Website"])
        for lead in leads:
            writer.writerow([
                lead.name,
                lead.rating,
                lead.reviews,
                lead.category,
                lead.address,
                lead.phone,
                lead.website,
            ])


def main() -> None:
    driver = build_driver(headless=HEADLESS)
    wait = WebDriverWait(driver, 20)
    seen_keys: Set[Tuple[str, str]] = set()
    all_leads: List[Lead] = []

    try:
        open_maps_home(driver, wait)
        for query in SEARCH_QUERIES:
            leads = collect_for_query(driver, wait, query, seen_keys)
            all_leads.extend(leads)

        write_csv(all_leads, OUTPUT_FILE)
        print(f"\n[INFO] Done. Saved {len(all_leads)} unique leads to {OUTPUT_FILE}")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
