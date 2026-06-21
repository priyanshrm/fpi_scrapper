import csv
import time
import calendar
import argparse
import traceback
import random
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service

# ========================
#  ARGUMENTS
# ========================
parser = argparse.ArgumentParser()
parser.add_argument("--start-year", type=int, default=2012)
parser.add_argument("--end-year", type=int, default=2025)
parser.add_argument("--use-last-day", type=str, default="True")
args = parser.parse_args()

START_YEAR = args.start_year
END_YEAR = args.end_year
USE_MONTH_LAST_DAY = args.use_last_day.lower() == "true"

URL = "https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx"
OUTPUT_CSV = "fpi_monthly_totals.csv"

METRIC_COLS = [
    "Gross Purchases(Rs Crore)",
    "Gross Sales(Rs Crore)",
    "Net Investment (Rs Crore)",
    "Net Investment US($) million",
]

# ========================
#  CUSTOM EXCEPTIONS
# ========================
class ScraperError(Exception):
    """Base exception for scraper-specific errors."""
    pass

class DateSelectionError(ScraperError):
    """Raised when date selection fails after all retries."""
    pass

class TableNotFoundError(ScraperError):
    """Raised when the total row cannot be found after all retries."""
    pass

class DataExtractionError(ScraperError):
    """Raised when data extraction fails or returns incomplete data."""
    pass

class BrowserCrashError(ScraperError):
    """Raised when the browser crashes and restart fails."""
    pass

class ConnectionError(ScraperError):
    """Raised when connection to the website fails."""
    pass

# ========================
#  WEBDRIVER MANAGEMENT
# ========================
def build_driver():
    """Create a fresh Chrome driver with robust options."""
    options = webdriver.ChromeOptions()
    
    # Headless mode
    options.add_argument("--headless=new")
    
    # Security/SSL options for problematic sites
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors")
    options.add_argument("--disable-web-security")
    options.add_argument("--allow-running-insecure-content")
    
    # Performance options
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    options.add_argument("--safebrowsing-disable-auto-update")
    options.add_argument("--start-maximized")
    options.add_argument("--incognito")
    
    # Additional options to avoid detection and connection issues
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-features=NetworkService,NetworkServiceInProcess")
    options.add_argument("--disable-features=VizDisplayCompositor")
    options.add_argument("--disable-features=IsolateOrigins,site-per-process")
    options.add_argument("--dns-prefetch-disable")
    options.add_argument("--disable-client-side-phishing-detection")
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-domain-reliability")
    
    # User agent to look more like a real browser
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    # Page load strategy
    options.page_load_strategy = 'eager'  # Don't wait for all resources
    
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(30)  # Shorter timeout
        driver.set_script_timeout(30)
        return driver
    except Exception as e:
        raise BrowserCrashError(f"Failed to create Chrome driver: {e}")

def load_url_with_retry(driver, url, max_retries=5):
    """Load URL with exponential backoff on connection errors."""
    for attempt in range(1, max_retries + 1):
        try:
            driver.get(url)
            # Wait for page to be ready
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(2)
            return True
        except WebDriverException as e:
            if "ERR_CONNECTION_RESET" in str(e) or "ERR_CONNECTION_CLOSED" in str(e) or "ERR_TIMED_OUT" in str(e):
                wait_time = 2 ** attempt + random.uniform(0, 1)
                print(f"  ⚠️ Connection error (attempt {attempt}/{max_retries}), retrying in {wait_time:.1f}s...")
                time.sleep(wait_time)
                if attempt == max_retries:
                    raise ConnectionError(f"Failed to load URL after {max_retries} attempts: {e}")
            else:
                raise

def restart_browser(driver, max_attempts=3):
    """Restart browser with retries on failure."""
    for attempt in range(1, max_attempts + 1):
        try:
            if driver:
                try:
                    driver.quit()
                except:
                    pass
            new_driver = build_driver()
            load_url_with_retry(new_driver, URL)
            WebDriverWait(new_driver, 20).until(
                EC.presence_of_element_located((By.ID, "txtDate"))
            )
            return new_driver
        except Exception as e:
            if attempt == max_attempts:
                raise BrowserCrashError(
                    f"Browser restart failed after {max_attempts} attempts: {e}"
                )
            print(f"  ⚠️ Browser restart attempt {attempt} failed, retrying...")
            time.sleep(5)

# ========================
#  DATE SELECTION - MULTI-LAYER FALLBACK
# ========================
def set_date_robust(driver, day, month, year, max_retries=3):
    """
    Set date using the most reliable method available.
    Tries: 1) Calendar popup, 2) Direct JS injection, 3) Page reload + retry
    """
    errors = []
    
    for attempt in range(1, max_retries + 1):
        try:
            # Method 1: Try calendar popup
            if _set_date_via_calendar(driver, day, month, year):
                if _verify_date_set(driver, day, month, year):
                    return True
            
            # Method 2: Direct hidden field injection
            if _set_date_via_hidden_fields(driver, day, month, year):
                if _verify_date_set(driver, day, month, year):
                    return True
            
            raise DateSelectionError("All date selection methods failed")
            
        except DateSelectionError as e:
            errors.append(str(e))
            if attempt < max_retries:
                print(f"  ⚠️ Date selection attempt {attempt}/{max_retries} failed: {e}")
                try:
                    load_url_with_retry(driver, URL)
                    WebDriverWait(driver, 20).until(
                        EC.presence_of_element_located((By.ID, "txtDate"))
                    )
                    time.sleep(2)
                except:
                    pass
    
    raise DateSelectionError(
        f"Date selection failed after {max_retries} attempts. Errors: {'; '.join(errors)}"
    )

def _set_date_via_calendar(driver, day, month, year):
    """Try to set date using the calendar widget. Returns True if successful."""
    try:
        cal_img = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "imgtxtDate"))
        )
        cal_img.click()
        time.sleep(1)
        
        cal = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "div[id*='Calendar'], div[id*='calendar'], .ajax__calendar_container")
            )
        )
        
        target_month = calendar.month_name[month]
        
        title_elem = cal.find_element(
            By.CSS_SELECTOR, "div[class*='title'], div[class*='header'], .ajax__calendar_title"
        )
        title_text = title_elem.text
        
        parts = title_text.replace(",", "").split()
        current_month_name = parts[0]
        current_year = int(parts[-1])
        
        max_nav = 36
        nav_count = 0
        
        while (current_year != year or current_month_name != target_month) and nav_count < max_nav:
            current_month_num = list(calendar.month_name).index(current_month_name)
            if current_month_num == 0:
                current_month_num = 1
            
            if (year > current_year) or (year == current_year and month > current_month_num):
                next_btn = cal.find_element(
                    By.CSS_SELECTOR, "div[class*='next'], a[class*='next'], .ajax__calendar_next a"
                )
            else:
                next_btn = cal.find_element(
                    By.CSS_SELECTOR, "div[class*='prev'], a[class*='prev'], .ajax__calendar_prev a"
                )
            
            next_btn.click()
            time.sleep(0.3)
            
            title_elem = cal.find_element(
                By.CSS_SELECTOR, "div[class*='title'], div[class*='header'], .ajax__calendar_title"
            )
            title_text = title_elem.text
            parts = title_text.replace(",", "").split()
            current_month_name = parts[0]
            current_year = int(parts[-1])
            
            nav_count += 1
        
        if nav_count >= max_nav:
            return False
        
        day_cells = cal.find_elements(
            By.CSS_SELECTOR, 
            "td[class*='day'], td[class*='active'], .ajax__calendar_day, .ajax__calendar_active"
        )
        
        for cell in day_cells:
            if cell.text.strip() == str(day):
                cell.click()
                time.sleep(0.5)
                return True
        
        return False
        
    except Exception as e:
        return False

def _set_date_via_hidden_fields(driver, day, month, year):
    """Set date by directly modifying hidden fields."""
    try:
        date_str = f"{day:02d}-{calendar.month_abbr[month]}-{year}"
        
        driver.execute_script(f"""
            document.getElementById('hdnDate').value = '{date_str}';
            document.getElementById('txtDate').value = '{date_str}';
            var event = new Event('change', {{ bubbles: true }});
            var hdnDate = document.getElementById('hdnDate');
            if (hdnDate) hdnDate.dispatchEvent(event);
        """)
        
        time.sleep(0.5)
        return True
        
    except Exception:
        return False

def _verify_date_set(driver, day, month, year):
    """Verify that the date was actually set correctly."""
    expected = f"{day:02d}-{calendar.month_abbr[month]}-{year}"
    
    try:
        actual_hidden = driver.execute_script(
            "return document.getElementById('hdnDate').value;"
        )
        
        if expected in actual_hidden:
            return True
        
        # Also check visible field
        actual_visible = driver.execute_script(
            "return document.getElementById('txtDate').value;"
        )
        
        if expected in actual_visible:
            return True
            
        return False
    except:
        return False

# ========================
#  CLICK VIEW REPORT
# ========================
def click_view_report_robust(driver, max_retries=3):
    """Click View Report button with retries."""
    for attempt in range(1, max_retries + 1):
        try:
            view_btn = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.ID, "btnSubmit1"))
            )
            
            driver.execute_script("arguments[0].scrollIntoView(true);", view_btn)
            time.sleep(0.3)
            
            # Use JavaScript click
            driver.execute_script("arguments[0].click();", view_btn)
            
            time.sleep(3)
            
            # Wait for the table container to update
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.ID, "dvArchiveData"))
                )
                return True
            except TimeoutException:
                if attempt < max_retries:
                    print(f"  ⚠️ Page didn't load after click, attempt {attempt}/{max_retries}")
                    continue
                raise
            
        except Exception as e:
            if attempt == max_retries:
                raise ScraperError(f"Failed to click View Report: {e}")
            print(f"  ⚠️ Click attempt {attempt} failed: {e}")
            time.sleep(2)

# ========================
#  WAIT FOR AND EXTRACT DATA
# ========================
def wait_and_extract_data(driver, month_name, max_wait=120):
    """
    Wait for the total row and extract all combination data.
    """
    start_time = time.time()
    
    while time.time() - start_time < max_wait:
        try:
            xpaths = [
                f"//td[contains(text(),'Total for {month_name}')]",
                f"//td[contains(.,'Total for {month_name}')]",
                f"//tr[contains(@class,'total')]//td[contains(text(),'Total for {month_name}')]",
            ]
            
            for xpath in xpaths:
                try:
                    total_cells = driver.find_elements(By.XPATH, xpath)
                    if total_cells:
                        data = _extract_monthly_data(driver, total_cells[0], month_name)
                        if data and len(data) > 0:
                            return data
                except StaleElementReferenceException:
                    continue
                except:
                    continue
            
            time.sleep(2)
            
        except Exception as e:
            time.sleep(2)
            continue
    
    raise TableNotFoundError(f"Could not find 'Total for {month_name}' row within {max_wait}s")

def _extract_monthly_data(driver, total_cell, month_name):
    """Extract all combination data starting from the total row."""
    try:
        total_row = total_cell.find_element(By.XPATH, "./ancestor::tr")
        
        all_rows = []
        current_row = total_row
        
        while current_row and len(all_rows) < 20:
            all_rows.append(current_row)
            try:
                next_row = current_row.find_element(By.XPATH, "following-sibling::tr[1]")
                current_row = next_row
            except:
                break
        
        data = {}
        current_d_e = None
        
        for row in all_rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if not cells:
                    continue
                
                cell_texts = [c.text.strip() for c in cells]
                
                if len(cell_texts) >= 2 and cell_texts[0] in ("Equity", "Debt"):
                    current_d_e = cell_texts[0]
                    inv_route = cell_texts[1]
                    
                    if inv_route in ("Sub-total", "Total", ""):
                        continue
                    
                    if len(cell_texts) >= 6:
                        metrics = cell_texts[2:6]
                        for metric_name, val in zip(METRIC_COLS, metrics):
                            col_name = f"{current_d_e}_{inv_route}_{metric_name}".replace("/", "_").replace("&", "and")
                            data[col_name] = val
                
                elif len(cell_texts) >= 1 and current_d_e:
                    inv_route = cell_texts[0]
                    
                    if inv_route in ("Sub-total", "Total", ""):
                        continue
                    
                    if len(cell_texts) >= 5:
                        metrics = cell_texts[1:5]
                        for metric_name, val in zip(METRIC_COLS, metrics):
                            col_name = f"{current_d_e}_{inv_route}_{metric_name}".replace("/", "_").replace("&", "and")
                            data[col_name] = val
                            
            except StaleElementReferenceException:
                continue
            except:
                continue
        
        return data
        
    except Exception as e:
        raise DataExtractionError(f"Data extraction failed: {e}")

# ========================
#  MAIN SCRAPING LOOP
# ========================
def scrape_all_months():
    """Main function with comprehensive error handling and recovery."""
    driver = None
    all_rows = []
    fieldnames = ["Reporting Date"]
    failed_months = []
    retry_queue = []
    
    # Generate list of months to scrape FIRST (before try block)
    months_to_scrape = []
    for year in range(START_YEAR, END_YEAR + 1):
        for month in range(1, 13):
            months_to_scrape.append((year, month))
    
    total_months = len(months_to_scrape)
    
    try:
        # Initialize browser
        print("Initializing browser...")
        driver = build_driver()
        
        print(f"Loading URL: {URL}")
        load_url_with_retry(driver, URL)
        
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.ID, "txtDate"))
        )
        print("Browser initialized successfully.\n")
        print(f"Total months to scrape: {total_months}\n")
        
        for idx, (year, month) in enumerate(months_to_scrape, 1):
            month_name = calendar.month_name[month]
            
            if USE_MONTH_LAST_DAY:
                last_day = calendar.monthrange(year, month)[1]
                day = last_day
            else:
                day = 1
            
            print(f"[{idx}/{total_months}] Processing: {month_name} {year} (day {day})")
            
            month_success = False
            
            for attempt in range(1, 4):
                try:
                    # Check if browser is responsive
                    try:
                        driver.current_url
                    except:
                        print("  ⚠️ Browser unresponsive, restarting...")
                        driver = restart_browser(driver)
                    
                    # Set date
                    set_date_robust(driver, day, month, year)
                    
                    # Click view report
                    click_view_report_robust(driver)
                    
                    # Wait for and extract data
                    data = wait_and_extract_data(driver, month_name)
                    
                    # Build row
                    row_data = {"Reporting Date": f"{year}-{month:02d}-{day:02d}"}
                    row_data.update(data)
                    all_rows.append(row_data)
                    
                    # Update fieldnames
                    for col in data.keys():
                        if col not in fieldnames:
                            fieldnames.append(col)
                    
                    print(f"  ✅ Successfully extracted {len(data)} values\n")
                    month_success = True
                    break
                    
                except (DateSelectionError, TableNotFoundError, ConnectionError) as e:
                    print(f"  ⚠️ Attempt {attempt}/3 failed: {e}")
                    if attempt == 3:
                        print(f"  ❌ Adding to retry queue: {month_name} {year}")
                        retry_queue.append((year, month, day, month_name))
                    else:
                        try:
                            load_url_with_retry(driver, URL)
                            WebDriverWait(driver, 20).until(
                                EC.presence_of_element_located((By.ID, "txtDate"))
                            )
                            time.sleep(2)
                        except:
                            driver = restart_browser(driver)
                
                except BrowserCrashError:
                    print(f"  ⚠️ Browser crashed, restarting...")
                    try:
                        driver = restart_browser(driver)
                    except:
                        pass
                    if attempt == 3:
                        retry_queue.append((year, month, day, month_name))
                
                except Exception as e:
                    print(f"  ⚠️ Unexpected error: {e}")
                    traceback.print_exc()
                    if attempt == 3:
                        retry_queue.append((year, month, day, month_name))
                    else:
                        try:
                            driver = restart_browser(driver)
                        except:
                            pass
        
        # Process retry queue
        if retry_queue:
            print(f"\n{'='*60}")
            print(f"Processing {len(retry_queue)} failed months with fresh browser...")
            print(f"{'='*60}\n")
            
            try:
                driver = restart_browser(driver)
            except:
                driver = build_driver()
                load_url_with_retry(driver, URL)
            
            for year, month, day, month_name in retry_queue:
                print(f"Retrying: {month_name} {year}")
                
                try:
                    set_date_robust(driver, day, month, year)
                    click_view_report_robust(driver)
                    data = wait_and_extract_data(driver, month_name)
                    
                    row_data = {"Reporting Date": f"{year}-{month:02d}-{day:02d}"}
                    row_data.update(data)
                    all_rows.append(row_data)
                    
                    for col in data.keys():
                        if col not in fieldnames:
                            fieldnames.append(col)
                    
                    print(f"  ✅ Retry successful: {len(data)} values\n")
                    
                except Exception as e:
                    print(f"  ❌ Retry failed: {e}")
                    failed_months.append(f"{month_name} {year}")
                    row_data = {"Reporting Date": f"{year}-{month:02d}-{day:02d}"}
                    all_rows.append(row_data)
    
    except Exception as e:
        print(f"FATAL ERROR: {e}")
        traceback.print_exc()
    
    finally:
        # Clean up browser
        if driver:
            try:
                driver.quit()
            except:
                pass
        
        # Write CSV
        if all_rows:
            print(f"\n{'='*60}")
            print(f"Writing {len(all_rows)} rows to {OUTPUT_CSV}")
            
            all_rows.sort(key=lambda x: x.get("Reporting Date", ""))
            
            with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
                writer.writeheader()
                writer.writerows(all_rows)
            
            print(f"CSV written successfully with {len(fieldnames)} columns.")
        
        # Report results
        print(f"\n{'='*60}")
        print(f"SCRAPING COMPLETE")
        print(f"{'='*60}")
        print(f"Total months: {total_months}")
        print(f"Rows collected: {len(all_rows)}")
        print(f"Successful: {len(all_rows) - len(failed_months)}")
        
        if failed_months:
            print(f"Failed months ({len(failed_months)}):")
            for m in failed_months:
                print(f"  - {m}")
        else:
            print("✅ ALL MONTHS SCRAPED SUCCESSFULLY!")
        
        print(f"\nOutput file: {OUTPUT_CSV}")

# ========================
#  ENTRY POINT
# ========================
if __name__ == "__main__":
    scrape_all_months()