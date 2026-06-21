import time
import calendar
import argparse
import traceback
import random
import sqlite3
import os
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
parser.add_argument("--start", type=int, default=0, help="Start month index (for parallel runs)")
parser.add_argument("--end", type=int, default=None, help="End month index (for parallel runs)")
args = parser.parse_args()

START_YEAR = args.start_year
END_YEAR = args.end_year
USE_MONTH_LAST_DAY = args.use_last_day.lower() == "true"
START_INDEX = args.start
END_INDEX = args.end

URL = "https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx"
DB_FILE = f"fpi_data_{START_YEAR}_{END_YEAR}_{START_INDEX}_{END_INDEX if END_INDEX else 'all'}.db"
PROGRESS_FILE = f"fpi_progress_{START_YEAR}_{END_YEAR}.json"

# Fixed fields that form the primary key
FIXED_FIELDS = ["reporting_date"]
KEY_FIELDS = ["reporting_date"]

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
    pass

class DateSelectionError(ScraperError):
    pass

class TableNotFoundError(ScraperError):
    pass

class DataExtractionError(ScraperError):
    pass

class BrowserCrashError(ScraperError):
    pass

class ConnectionError(ScraperError):
    pass

# ========================
#  DATABASE HELPERS
# ========================
def get_connection():
    """Get SQLite connection for this chunk."""
    return sqlite3.connect(DB_FILE)

def init_db():
    """Create the table with fixed columns if it doesn't exist."""
    with get_connection() as con:
        cols_def = ", ".join(f'"{c}" TEXT' for c in FIXED_FIELDS)
        con.execute(f"""
            CREATE TABLE IF NOT EXISTS fpi_monthly_data (
                {cols_def},
                PRIMARY KEY ({', '.join(f'"{k}"' for k in KEY_FIELDS)})
            )
        """)
        con.commit()
    print(f"  [DB] Initialized database: {DB_FILE}")

def get_existing_columns():
    """Return the current column names in the table."""
    with get_connection() as con:
        cur = con.execute("PRAGMA table_info(fpi_monthly_data)")
        return [row[1] for row in cur.fetchall()]

def ensure_columns(col_names):
    """Add any new combination columns that don't exist yet."""
    existing = set(get_existing_columns())
    new_cols = [c for c in col_names if c not in existing]
    if not new_cols:
        return
    with get_connection() as con:
        for col in new_cols:
            # Sanitize column name for SQLite
            safe_col = col.replace("(", "_").replace(")", "_").replace(" ", "_").replace("/", "_").replace("&", "and").replace("$", "USD")
            try:
                con.execute(f'ALTER TABLE fpi_monthly_data ADD COLUMN "{safe_col}" TEXT DEFAULT ""')
            except Exception as e:
                print(f"  [DB WARNING] Could not add column {safe_col}: {e}")
        con.commit()
    print(f"  [DB] Added {len(new_cols)} new columns: {new_cols}")

def sanitize_column_name(name):
    """Make column name safe for SQLite."""
    return name.replace("(", "_").replace(")", "_").replace(" ", "_").replace("/", "_").replace("&", "and").replace("$", "USD")

def upsert_row(row_data):
    """
    Insert or replace a row into the database.
    Uses INSERT OR REPLACE with the primary key (reporting_date).
    """
    # First, ensure all columns exist
    ensure_columns(row_data.keys())
    
    # Get all existing columns
    all_cols = get_existing_columns()
    
    # Create a complete row with all columns (missing ones get empty string)
    full_row = {}
    for col in all_cols:
        if col in row_data:
            full_row[col] = row_data[col]
        else:
            full_row[col] = ""
    
    # Build SQL
    cols_sql = ", ".join(f'"{c}"' for c in all_cols)
    placeholders = ", ".join("?" for _ in all_cols)
    values = [full_row[c] for c in all_cols]
    
    with get_connection() as con:
        con.execute(
            f'INSERT OR REPLACE INTO fpi_monthly_data ({cols_sql}) VALUES ({placeholders})',
            values
        )
        con.commit()

# ========================
#  WEBDRIVER MANAGEMENT
# ========================
def build_driver():
    """Create a fresh Chrome driver with robust options."""
    options = webdriver.ChromeOptions()
    
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors")
    options.add_argument("--disable-web-security")
    options.add_argument("--allow-running-insecure-content")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    options.add_argument("--safebrowsing-disable-auto-update")
    options.add_argument("--start-maximized")
    options.add_argument("--incognito")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-features=NetworkService,NetworkServiceInProcess")
    options.add_argument("--dns-prefetch-disable")
    options.add_argument("--disable-client-side-phishing-detection")
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-domain-reliability")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    options.page_load_strategy = 'eager'
    
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(30)
        return driver
    except Exception as e:
        raise BrowserCrashError(f"Failed to create Chrome driver: {e}")

def load_url_with_retry(driver, url, max_retries=5):
    """Load URL with exponential backoff on connection errors."""
    for attempt in range(1, max_retries + 1):
        try:
            driver.get(url)
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
                raise BrowserCrashError(f"Browser restart failed after {max_attempts} attempts: {e}")
            print(f"  ⚠️ Browser restart attempt {attempt} failed, retrying...")
            time.sleep(5)

# ========================
#  DATE SELECTION
# ========================
def set_date_robust(driver, day, month, year, max_retries=3):
    """Set date with fallback methods."""
    errors = []
    
    for attempt in range(1, max_retries + 1):
        try:
            if _set_date_via_hidden_fields(driver, day, month, year):
                if _verify_date_set(driver, day, month, year):
                    return True
            
            raise DateSelectionError("Date selection failed")
            
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
    
    raise DateSelectionError(f"Date selection failed after {max_retries} attempts")

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
            
            driver.execute_script("arguments[0].click();", view_btn)
            
            time.sleep(3)
            
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
    """Wait for the total row and extract all combination data."""
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
                            col_name = sanitize_column_name(
                                f"{current_d_e}_{inv_route}_{metric_name}"
                            )
                            data[col_name] = val
                
                elif len(cell_texts) >= 1 and current_d_e:
                    inv_route = cell_texts[0]
                    
                    if inv_route in ("Sub-total", "Total", ""):
                        continue
                    
                    if len(cell_texts) >= 5:
                        metrics = cell_texts[1:5]
                        for metric_name, val in zip(METRIC_COLS, metrics):
                            col_name = sanitize_column_name(
                                f"{current_d_e}_{inv_route}_{metric_name}"
                            )
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
    """Main function with database storage."""
    driver = None
    successful_count = 0
    failed_months = []
    retry_queue = []
    
    # Initialize database
    init_db()
    
    # Generate list of months to scrape
    months_to_scrape = []
    for year in range(START_YEAR, END_YEAR + 1):
        for month in range(1, 13):
            months_to_scrape.append((year, month))
    
    # Apply start/end slicing for parallel runs
    if END_INDEX:
        months_to_scrape = months_to_scrape[START_INDEX:END_INDEX]
    elif START_INDEX > 0:
        months_to_scrape = months_to_scrape[START_INDEX:]
    
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
        print(f"Total months to scrape in this chunk: {total_months}\n")
        
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
                    
                    # Build row with reporting_date as primary key
                    row_data = {
                        "reporting_date": f"{year}-{month:02d}-{day:02d}"
                    }
                    row_data.update(data)
                    
                    # Upsert into database
                    upsert_row(row_data)
                    
                    successful_count += 1
                    print(f"  ✅ Successfully inserted into DB ({len(data)} value columns)\n")
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
                    
                    row_data = {
                        "reporting_date": f"{year}-{month:02d}-{day:02d}"
                    }
                    row_data.update(data)
                    
                    upsert_row(row_data)
                    successful_count += 1
                    print(f"  ✅ Retry successful: {len(data)} value columns\n")
                    
                except Exception as e:
                    print(f"  ❌ Retry failed: {e}")
                    failed_months.append(f"{month_name} {year}")
    
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
        
        # Report results
        print(f"\n{'='*60}")
        print(f"SCRAPING COMPLETE")
        print(f"{'='*60}")
        print(f"Total months in this chunk: {total_months}")
        print(f"Successful: {successful_count}")
        print(f"Failed: {len(failed_months)}")
        
        if failed_months:
            print(f"Failed months:")
            for m in failed_months:
                print(f"  - {m}")
        else:
            print("✅ ALL MONTHS IN THIS CHUNK SCRAPED SUCCESSFULLY!")
        
        # Show database info
        columns = get_existing_columns()
        print(f"\nDatabase: {DB_FILE}")
        print(f"Total columns: {len(columns)}")
        print(f"Columns: {columns}")
        
        # Show row count
        with get_connection() as con:
            count = con.execute("SELECT COUNT(*) FROM fpi_monthly_data").fetchone()[0]
            print(f"Total rows in database: {count}")

# ========================
#  ENTRY POINT
# ========================
if __name__ == "__main__":
    scrape_all_months()