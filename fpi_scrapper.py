import time
import calendar
import argparse
import traceback
import random
import sqlite3
import os
import sys
from datetime import datetime, timedelta
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
parser.add_argument("--start", type=int, default=0)
parser.add_argument("--end", type=int, default=None)
args = parser.parse_args()

START_YEAR = args.start_year
END_YEAR = args.end_year
USE_MONTH_LAST_DAY = args.use_last_day.lower() == "true"
START_INDEX = args.start
END_INDEX = args.end

URL = "https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx"
DB_FILE = f"fpi_data_{START_YEAR}_{END_YEAR}_{START_INDEX}_{END_INDEX if END_INDEX else 'all'}.db"

METRIC_COLS = [
    "Gross Purchases(Rs Crore)",
    "Gross Sales(Rs Crore)",
    "Net Investment (Rs Crore)",
    "Net Investment US($) million",
]

# ========================
#  LOGGING HELPERS
# ========================
start_time_global = None
month_start_time = None

def log(msg, level="INFO"):
    """Print timestamped log message."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    elapsed = ""
    if start_time_global:
        total_elapsed = (datetime.now() - start_time_global).total_seconds()
        elapsed = f" [T+{int(total_elapsed//60)}m{int(total_elapsed%60)}s]"
    
    prefix = {
        "INFO": "ℹ️",
        "SUCCESS": "✅",
        "WARNING": "⚠️",
        "ERROR": "❌",
        "PROGRESS": "📊",
        "STAGE": "🔄",
    }.get(level, "•")
    
    print(f"{timestamp}{elapsed} {prefix} {msg}", flush=True)

def log_stage(stage_name):
    """Log a major stage transition."""
    log(f"{'='*60}", "STAGE")
    log(f"STAGE: {stage_name}", "STAGE")
    log(f"{'='*60}", "STAGE")

def log_progress(current, total, month_name, year):
    """Log progress with percentage and ETA."""
    global month_start_time
    
    pct = (current / total) * 100
    elapsed = (datetime.now() - start_time_global).total_seconds()
    
    if current > 1:
        avg_per_month = elapsed / current
        remaining_months = total - current
        eta_seconds = avg_per_month * remaining_months
        eta = str(timedelta(seconds=int(eta_seconds)))
    else:
        eta = "calculating..."
    
    log(f"[{current}/{total}] ({pct:.1f}%) Processing: {month_name} {year} | ETA: {eta}", "PROGRESS")

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
    return sqlite3.connect(DB_FILE)

def init_db():
    log("Initializing database...", "STAGE")
    with get_connection() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS fpi_monthly_data (
                "reporting_date" TEXT PRIMARY KEY
            )
        """)
        con.commit()
    log(f"Database ready: {DB_FILE}", "SUCCESS")

def get_existing_columns():
    with get_connection() as con:
        cur = con.execute("PRAGMA table_info(fpi_monthly_data)")
        return [row[1] for row in cur.fetchall()]

def ensure_columns(col_names):
    existing = set(get_existing_columns())
    new_cols = [c for c in col_names if c not in existing]
    if not new_cols:
        return
    with get_connection() as con:
        for col in new_cols:
            safe_col = sanitize_column_name(col)
            try:
                con.execute(f'ALTER TABLE fpi_monthly_data ADD COLUMN "{safe_col}" TEXT DEFAULT ""')
            except Exception as e:
                log(f"Could not add column {safe_col}: {e}", "WARNING")
        con.commit()
    log(f"Added {len(new_cols)} new columns to database", "INFO")

def sanitize_column_name(name):
    return name.replace("(", "_").replace(")", "_").replace(" ", "_").replace("/", "_").replace("&", "and").replace("$", "USD")

def upsert_row(row_data):
    ensure_columns(row_data.keys())
    all_cols = get_existing_columns()
    
    full_row = {}
    for col in all_cols:
        full_row[col] = row_data.get(col, "")
    
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
    log("Building Chrome driver...", "STAGE")
    
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
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    options.page_load_strategy = 'eager'
    
    # Use provided Chrome paths if available (GitHub Actions)
    chrome_path = os.environ.get("CHROME_PATH")
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    
    if chrome_path and chromedriver_path:
        log(f"Using GitHub Actions Chrome: {chrome_path}", "INFO")
        options.binary_location = chrome_path
        service = Service(executable_path=chromedriver_path)
    else:
        log("Using webdriver-manager to install Chrome driver", "INFO")
        service = Service(ChromeDriverManager().install())
    
    try:
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(30)
        log("Chrome driver built successfully", "SUCCESS")
        return driver
    except Exception as e:
        log(f"Failed to create Chrome driver: {e}", "ERROR")
        raise BrowserCrashError(f"Failed to create Chrome driver: {e}")

def load_url_with_retry(driver, url, max_retries=5):
    log(f"Loading URL: {url}", "STAGE")
    
    for attempt in range(1, max_retries + 1):
        try:
            log(f"  Attempt {attempt}/{max_retries}...", "INFO")
            driver.get(url)
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(2)
            log("  Page loaded successfully", "SUCCESS")
            return True
        except WebDriverException as e:
            error_msg = str(e)
            if "ERR_CONNECTION_RESET" in error_msg:
                wait_time = 2 ** attempt + random.uniform(0, 1)
                log(f"  Connection reset (attempt {attempt}/{max_retries}), retrying in {wait_time:.1f}s...", "WARNING")
                time.sleep(wait_time)
            elif "ERR_TIMED_OUT" in error_msg:
                log(f"  Timeout (attempt {attempt}/{max_retries}), retrying...", "WARNING")
                time.sleep(5)
            else:
                log(f"  WebDriver error: {error_msg[:100]}", "ERROR")
                if attempt == max_retries:
                    raise ConnectionError(f"Failed to load URL: {e}")
            
            if attempt == max_retries:
                raise ConnectionError(f"Failed to load URL after {max_retries} attempts")
        except Exception as e:
            log(f"  Unexpected error: {e}", "WARNING")
            time.sleep(3)

def restart_browser(driver, max_attempts=3):
    log("Restarting browser...", "WARNING")
    
    for attempt in range(1, max_attempts + 1):
        try:
            if driver:
                try:
                    driver.quit()
                except:
                    pass
            
            log(f"  Restart attempt {attempt}/{max_attempts}", "INFO")
            new_driver = build_driver()
            load_url_with_retry(new_driver, URL)
            WebDriverWait(new_driver, 20).until(
                EC.presence_of_element_located((By.ID, "txtDate"))
            )
            log("  Browser restarted successfully", "SUCCESS")
            return new_driver
        except Exception as e:
            log(f"  Restart attempt {attempt} failed: {e}", "ERROR")
            if attempt == max_attempts:
                raise BrowserCrashError(f"Browser restart failed after {max_attempts} attempts")
            time.sleep(5)

# ========================
#  DATE SELECTION
# ========================
def set_date_robust(driver, day, month, year, max_retries=3):
    month_name = calendar.month_name[month]
    date_str = f"{day:02d}-{calendar.month_abbr[month]}-{year}"
    log(f"Setting date to: {date_str} ({month_name})", "STAGE")
    
    for attempt in range(1, max_retries + 1):
        try:
            if _set_date_via_hidden_fields(driver, day, month, year):
                if _verify_date_set(driver, day, month, year):
                    log(f"  Date set successfully: {date_str}", "SUCCESS")
                    return True
            
            log(f"  Date selection attempt {attempt}/{max_retries} failed", "WARNING")
            if attempt < max_retries:
                try:
                    load_url_with_retry(driver, URL)
                    WebDriverWait(driver, 20).until(
                        EC.presence_of_element_located((By.ID, "txtDate"))
                    )
                    time.sleep(2)
                except:
                    pass
        
        except Exception as e:
            log(f"  Error: {e}", "ERROR")
    
    raise DateSelectionError(f"Date selection failed after {max_retries} attempts")

def _set_date_via_hidden_fields(driver, day, month, year):
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
    except Exception as e:
        log(f"  Hidden field method failed: {e}", "WARNING")
        return False

def _verify_date_set(driver, day, month, year):
    expected = f"{day:02d}-{calendar.month_abbr[month]}-{year}"
    
    try:
        actual_hidden = driver.execute_script(
            "return document.getElementById('hdnDate').value;"
        )
        actual_visible = driver.execute_script(
            "return document.getElementById('txtDate').value;"
        )
        
        log(f"  Expected: {expected} | Hidden: {actual_hidden} | Visible: {actual_visible}", "INFO")
        
        if expected in actual_hidden or expected in actual_visible:
            return True
        
        return False
    except Exception as e:
        log(f"  Verification failed: {e}", "WARNING")
        return False

# ========================
#  CLICK VIEW REPORT
# ========================
def click_view_report_robust(driver, max_retries=3):
    log("Clicking 'View Report' button...", "STAGE")
    
    for attempt in range(1, max_retries + 1):
        try:
            view_btn = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.ID, "btnSubmit1"))
            )
            
            driver.execute_script("arguments[0].scrollIntoView(true);", view_btn)
            time.sleep(0.3)
            driver.execute_script("arguments[0].click();", view_btn)
            
            log("  Button clicked, waiting for table to load...", "INFO")
            time.sleep(3)
            
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.ID, "dvArchiveData"))
                )
                log("  Table container loaded", "SUCCESS")
                return True
            except TimeoutException:
                log(f"  Table container not found (attempt {attempt}/{max_retries})", "WARNING")
                if attempt == max_retries:
                    raise
                continue
        
        except Exception as e:
            log(f"  Click failed (attempt {attempt}/{max_retries}): {e}", "ERROR")
            if attempt == max_retries:
                raise ScraperError(f"Failed to click View Report: {e}")
            time.sleep(2)

# ========================
#  WAIT FOR AND EXTRACT DATA
# ========================
def wait_and_extract_data(driver, month_name, max_wait=120):
    log(f"Waiting for 'Total for {month_name}' row...", "STAGE")
    start_time = time.time()
    last_log_time = start_time
    
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
                        elapsed = time.time() - start_time
                        log(f"  Found 'Total for {month_name}' row after {elapsed:.1f}s", "SUCCESS")
                        data = _extract_monthly_data(driver, total_cells[0], month_name)
                        if data and len(data) > 0:
                            log(f"  Extracted {len(data)} values", "SUCCESS")
                            return data
                except StaleElementReferenceException:
                    continue
                except:
                    continue
            
            # Log waiting status every 10 seconds
            if time.time() - last_log_time > 10:
                elapsed = time.time() - start_time
                log(f"  Still waiting... ({elapsed:.0f}s elapsed)", "INFO")
                last_log_time = time.time()
            
            time.sleep(2)
            
        except Exception as e:
            time.sleep(2)
            continue
    
    elapsed = time.time() - start_time
    log(f"  Timeout after {elapsed:.0f}s", "ERROR")
    raise TableNotFoundError(f"Could not find 'Total for {month_name}' row within {max_wait}s")

def _extract_monthly_data(driver, total_cell, month_name):
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
        row_count = 0
        
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
                        row_count += 1
                
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
                        row_count += 1
                            
            except StaleElementReferenceException:
                continue
            except:
                continue
        
        log(f"  Parsed {row_count} combination rows", "INFO")
        return data
        
    except Exception as e:
        raise DataExtractionError(f"Data extraction failed: {e}")

# ========================
#  MAIN SCRAPING LOOP
# ========================
def scrape_all_months():
    global start_time_global, month_start_time
    start_time_global = datetime.now()
    
    driver = None
    successful_count = 0
    failed_months = []
    retry_queue = []
    
    log("=" * 60, "STAGE")
    log("FPI ARCHIVE SCRAPER STARTING", "STAGE")
    log("=" * 60, "STAGE")
    log(f"Year range: {START_YEAR} - {END_YEAR}", "INFO")
    log(f"Date mode: {'Last day of month' if USE_MONTH_LAST_DAY else 'First day of month'}", "INFO")
    log(f"Chunk: months {START_INDEX} to {END_INDEX if END_INDEX else 'end'}", "INFO")
    
    # Initialize database
    init_db()
    
    # Generate list of months
    months_to_scrape = []
    for year in range(START_YEAR, END_YEAR + 1):
        for month in range(1, 13):
            months_to_scrape.append((year, month))
    
    if END_INDEX:
        months_to_scrape = months_to_scrape[START_INDEX:END_INDEX]
    elif START_INDEX > 0:
        months_to_scrape = months_to_scrape[START_INDEX:]
    
    total_months = len(months_to_scrape)
    log(f"Total months to scrape in this chunk: {total_months}", "INFO")
    log(f"Estimated time: {total_months * 20 // 60}-{total_months * 25 // 60} minutes", "INFO")
    
    try:
        # Build browser
        driver = build_driver()
        
        # Load page
        load_url_with_retry(driver, URL)
        
        # Wait for page to be ready
        log("Waiting for page elements...", "STAGE")
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.ID, "txtDate"))
        )
        log("Page ready. Starting scraping...\n", "SUCCESS")
        
        # Main scraping loop
        for idx, (year, month) in enumerate(months_to_scrape, 1):
            month_name = calendar.month_name[month]
            month_start_time = datetime.now()
            
            if USE_MONTH_LAST_DAY:
                last_day = calendar.monthrange(year, month)[1]
                day = last_day
            else:
                day = 1
            
            log_progress(idx, total_months, month_name, year)
            
            month_success = False
            
            for attempt in range(1, 4):
                try:
                    # Check browser
                    try:
                        driver.current_url
                    except:
                        log("Browser unresponsive, restarting...", "WARNING")
                        driver = restart_browser(driver)
                    
                    # Set date
                    set_date_robust(driver, day, month, year)
                    
                    # Click view report
                    click_view_report_robust(driver)
                    
                    # Extract data
                    data = wait_and_extract_data(driver, month_name)
                    
                    # Save to database
                    row_data = {"reporting_date": f"{year}-{month:02d}-{day:02d}"}
                    row_data.update(data)
                    
                    log("Inserting into database...", "STAGE")
                    upsert_row(row_data)
                    
                    successful_count += 1
                    month_elapsed = (datetime.now() - month_start_time).total_seconds()
                    log(f"Month completed in {month_elapsed:.1f}s | Total successful: {successful_count}", "SUCCESS")
                    print()  # blank line for readability
                    month_success = True
                    break
                    
                except (DateSelectionError, TableNotFoundError, ConnectionError) as e:
                    log(f"Attempt {attempt}/3 failed: {e}", "WARNING")
                    if attempt == 3:
                        log(f"Adding to retry queue: {month_name} {year}", "ERROR")
                        retry_queue.append((year, month, day, month_name))
                    else:
                        try:
                            load_url_with_retry(driver, URL)
                            time.sleep(2)
                        except:
                            driver = restart_browser(driver)
                
                except BrowserCrashError:
                    log("Browser crashed!", "ERROR")
                    try:
                        driver = restart_browser(driver)
                    except:
                        pass
                    if attempt == 3:
                        retry_queue.append((year, month, day, month_name))
                
                except Exception as e:
                    log(f"Unexpected error: {e}", "ERROR")
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
            log(f"Processing {len(retry_queue)} failed months with fresh browser...", "WARNING")
            
            try:
                driver = restart_browser(driver)
            except:
                driver = build_driver()
                load_url_with_retry(driver, URL)
            
            for year, month, day, month_name in retry_queue:
                log(f"Retrying: {month_name} {year}", "STAGE")
                
                try:
                    set_date_robust(driver, day, month, year)
                    click_view_report_robust(driver)
                    data = wait_and_extract_data(driver, month_name)
                    
                    row_data = {"reporting_date": f"{year}-{month:02d}-{day:02d}"}
                    row_data.update(data)
                    upsert_row(row_data)
                    successful_count += 1
                    log(f"Retry successful for {month_name} {year}", "SUCCESS")
                    
                except Exception as e:
                    log(f"Retry failed for {month_name} {year}: {e}", "ERROR")
                    failed_months.append(f"{month_name} {year}")
    
    except Exception as e:
        log(f"FATAL ERROR: {e}", "ERROR")
        traceback.print_exc()
    
    finally:
        # Cleanup
        if driver:
            try:
                driver.quit()
            except:
                pass
        
        # Final report
        total_elapsed = (datetime.now() - start_time_global).total_seconds()
        
        log("\n" + "=" * 60, "STAGE")
        log("SCRAPING COMPLETE", "STAGE")
        log("=" * 60, "STAGE")
        log(f"Total time: {timedelta(seconds=int(total_elapsed))}", "INFO")
        log(f"Total months in chunk: {total_months}", "INFO")
        log(f"Successful: {successful_count}", "SUCCESS")
        log(f"Failed: {len(failed_months)}", "ERROR" if failed_months else "SUCCESS")
        
        if failed_months:
            log("Failed months:", "ERROR")
            for m in failed_months:
                log(f"  - {m}", "ERROR")
        else:
            log("ALL MONTHS SCRAPED SUCCESSFULLY!", "SUCCESS")
        
        # Database info
        columns = get_existing_columns()
        log(f"Database: {DB_FILE}", "INFO")
        log(f"Total columns: {len(columns)}", "INFO")
        
        with get_connection() as con:
            count = con.execute("SELECT COUNT(*) FROM fpi_monthly_data").fetchone()[0]
            log(f"Total rows in database: {count}", "INFO")
        
        print()

if __name__ == "__main__":
    scrape_all_months()