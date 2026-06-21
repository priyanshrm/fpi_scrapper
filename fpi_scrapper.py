import time
import calendar
import argparse
import traceback
import random
import sqlite3
import os
import re
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

# Fixed columns
FIXED_FIELDS = ["reporting_date"]
KEY_FIELDS = ["reporting_date"]

# ========================
#  LOGGING HELPERS
# ========================
start_time_global = None

def log(msg, level="INFO"):
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

def log_progress(current, total, desc=""):
    pct = (current / total) * 100 if total > 0 else 0
    elapsed = (datetime.now() - start_time_global).total_seconds()
    
    if current > 1:
        avg_per_item = elapsed / current
        remaining = total - current
        eta_seconds = avg_per_item * remaining
        eta = str(timedelta(seconds=int(eta_seconds)))
    else:
        eta = "calculating..."
    
    log(f"[{current}/{total}] ({pct:.1f}%) {desc} | ETA: {eta}", "PROGRESS")

# ========================
#  DATABASE HELPERS
# ========================
def get_connection():
    return sqlite3.connect(DB_FILE)

def sanitize_column_name(name):
    """Make column name SQL-safe and readable."""
    return name.replace("(", "").replace(")", "").replace("/", "_").replace("&", "and").replace("$", "USD").replace(" ", "_").replace("-", "_").replace(".", "").replace("'", "")

def init_db():
    """Create the table with only reporting_date as fixed column."""
    log("Initializing database...", "STAGE")
    with get_connection() as con:
        cols_def = ", ".join(f'"{c}" TEXT' for c in FIXED_FIELDS)
        con.execute(f"""
            CREATE TABLE IF NOT EXISTS fpi_wide_data (
                {cols_def},
                PRIMARY KEY ({', '.join(f'"{k}"' for k in KEY_FIELDS)})
            )
        """)
        con.commit()
    log(f"Database ready: {DB_FILE}", "SUCCESS")

def get_existing_columns():
    """Return current column names."""
    with get_connection() as con:
        cur = con.execute("PRAGMA table_info(fpi_wide_data)")
        return [row[1] for row in cur.fetchall()]

def ensure_columns(col_names):
    """Add new columns that don't exist yet."""
    existing = set(get_existing_columns())
    new_cols = [c for c in col_names if c not in existing]
    if not new_cols:
        return
    with get_connection() as con:
        for col in new_cols:
            safe_col = sanitize_column_name(col)
            try:
                con.execute(f'ALTER TABLE fpi_wide_data ADD COLUMN "{safe_col}" TEXT DEFAULT ""')
            except Exception as e:
                log(f"  Could not add column {safe_col}: {e}", "WARNING")
        con.commit()
    log(f"  Added {len(new_cols)} new columns", "INFO")

def upsert_wide_row(row_data):
    """Insert or replace a wide-format row with dynamic columns."""
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
            f'INSERT OR REPLACE INTO fpi_wide_data ({cols_sql}) VALUES ({placeholders})',
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
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    options.page_load_strategy = 'eager'
    
    chrome_path = os.environ.get("CHROME_PATH")
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    
    if chrome_path and chromedriver_path:
        options.binary_location = chrome_path
        service = Service(executable_path=chromedriver_path)
    else:
        service = Service(ChromeDriverManager().install())
    
    try:
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(30)
        driver.set_script_timeout(30)
        log("Chrome driver built successfully", "SUCCESS")
        return driver
    except Exception as e:
        log(f"Failed to create Chrome driver: {e}", "ERROR")
        raise

def load_url_with_retry(driver, url, max_retries=5):
    for attempt in range(1, max_retries + 1):
        try:
            driver.get(url)
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(2)
            return True
        except WebDriverException as e:
            error_msg = str(e)
            if "ERR_CONNECTION_RESET" in error_msg:
                wait_time = 2 ** attempt + random.uniform(0, 1)
                log(f"  Connection reset, retrying in {wait_time:.1f}s...", "WARNING")
                time.sleep(wait_time)
            elif "ERR_TIMED_OUT" in error_msg:
                log(f"  Timeout, retrying...", "WARNING")
                time.sleep(5)
            if attempt == max_retries:
                raise

def restart_browser(driver, max_attempts=3):
    log("Restarting browser...", "WARNING")
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
                raise

# ========================
#  DATE SELECTION
# ========================
def set_date(driver, day, month, year):
    date_str = f"{day:02d}-{calendar.month_abbr[month]}-{year}"
    
    driver.execute_script(f"""
        document.getElementById('hdnDate').value = '{date_str}';
        document.getElementById('txtDate').value = '{date_str}';
    """)
    time.sleep(0.3)
    
    actual = driver.execute_script("return document.getElementById('hdnDate').value;")
    return date_str in actual

# ========================
#  CLICK VIEW REPORT
# ========================
def click_view_report(driver):
    view_btn = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable((By.ID, "btnSubmit1"))
    )
    driver.execute_script("arguments[0].click();", view_btn)
    time.sleep(3)
    
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "dvArchiveData"))
        )
        return True
    except TimeoutException:
        return False

# ========================
#  EXTRACT DAILY ROWS AND PIVOT TO WIDE FORMAT
# ========================
def extract_and_pivot_to_wide(driver, target_date_str):
    """
    Extract all daily rows from the table and pivot to wide format.
    Returns a dict with ONE row per date: {reporting_date: ..., col1: val1, col2: val2, ...}
    """
    wide_data = {}
    
    try:
        tables = driver.find_elements(By.CSS_SELECTOR, "table.tbls01")
        
        if not tables:
            log("  No data tables found!", "ERROR")
            return None
        
        table = tables[0]
        tr_elements = table.find_elements(By.TAG_NAME, "tr")
        
        log(f"  Scanning {len(tr_elements)} rows...", "INFO")
        
        current_date = None
        current_debt_equity = None
        date_pattern = r'^\d{2}-[A-Za-z]{3}-\d{4}$'
        rows_found = 0
        
        for tr in tr_elements:
            try:
                cells = tr.find_elements(By.TAG_NAME, "td")
                if not cells:
                    continue
                
                cell_texts = [c.text.strip() for c in cells]
                
                # Skip empty/header/footer rows
                if all(t == "" for t in cell_texts):
                    continue
                if cell_texts[0] in ("Reporting Date", "Debt/Equity"):
                    continue
                if "Daily Trends" in " ".join(cell_texts):
                    continue
                if "The data presented above" in " ".join(cell_texts):
                    continue
                if "Stock exchanges compile" in " ".join(cell_texts):
                    continue
                
                # Check for date row
                if re.match(date_pattern, cell_texts[0]):
                    current_date = cell_texts[0]
                    
                    # Determine Debt/Equity and Investment Route
                    if len(cell_texts) >= 3:
                        if cell_texts[1] in ("Equity", "Debt", "Debt-General Limit", "Debt-VRR", 
                                               "Debt-FAR", "Hybrid", "Mutual Funds", "AIFs"):
                            current_debt_equity = cell_texts[1]
                            inv_route = cell_texts[2] if len(cell_texts) > 2 else ""
                        else:
                            inv_route = cell_texts[1]
                        
                        if inv_route in ("Sub-total", "Total", ""):
                            continue
                        
                        if len(cell_texts) >= 7:
                            metrics = cell_texts[3:7]  # GP, GS, NI, USD
                            metric_suffixes = [
                                "Gross_Purchases_Rs_Crore",
                                "Gross_Sales_Rs_Crore",
                                "Net_Investment_Rs_Crore",
                                "Net_Investment_USD_million"
                            ]
                            
                            for suffix, val in zip(metric_suffixes, metrics):
                                col_name = sanitize_column_name(
                                    f"{current_debt_equity}_{inv_route}_{suffix}"
                                )
                                wide_data[col_name] = val
                                rows_found += 1
                    
                elif len(cell_texts) >= 2 and cell_texts[0] in (
                    "Equity", "Debt", "Debt-General Limit", "Debt-VRR", 
                    "Debt-FAR", "Hybrid", "Mutual Funds", "AIFs"
                ):
                    current_debt_equity = cell_texts[0]
                    inv_route = cell_texts[1]
                    
                    if inv_route in ("Sub-total", "Total", ""):
                        continue
                    
                    if len(cell_texts) >= 6:
                        metrics = cell_texts[2:6]
                        metric_suffixes = [
                            "Gross_Purchases_Rs_Crore",
                            "Gross_Sales_Rs_Crore",
                            "Net_Investment_Rs_Crore",
                            "Net_Investment_USD_million"
                        ]
                        
                        for suffix, val in zip(metric_suffixes, metrics):
                            col_name = sanitize_column_name(
                                f"{current_debt_equity}_{inv_route}_{suffix}"
                            )
                            wide_data[col_name] = val
                            rows_found += 1
                        
                elif len(cell_texts) >= 5 and current_debt_equity:
                    inv_route = cell_texts[0]
                    
                    if inv_route in ("Sub-total", "Total", ""):
                        continue
                    
                    metrics = cell_texts[1:5]
                    metric_suffixes = [
                        "Gross_Purchases_Rs_Crore",
                        "Gross_Sales_Rs_Crore",
                        "Net_Investment_Rs_Crore",
                        "Net_Investment_USD_million"
                    ]
                    
                    for suffix, val in zip(metric_suffixes, metrics):
                        col_name = sanitize_column_name(
                            f"{current_debt_equity}_{inv_route}_{suffix}"
                        )
                        wide_data[col_name] = val
                        rows_found += 1
                
            except StaleElementReferenceException:
                continue
            except Exception:
                continue
        
        if rows_found > 0:
            # Add the reporting date
            wide_data["reporting_date"] = target_date_str
            
            combinations = rows_found // 4
            log(f"  Pivoted {combinations} combinations → {rows_found} columns for {target_date_str}", "SUCCESS")
            
            # Show sample
            sample_keys = list(wide_data.keys())[1:4]  # Skip reporting_date
            for k in sample_keys:
                log(f"    {k}: {wide_data[k]}", "INFO")
            
            return wide_data
        else:
            log(f"  No rows extracted for {target_date_str}", "WARNING")
            return None
        
    except Exception as e:
        log(f"  Extraction error: {e}", "ERROR")
        traceback.print_exc()
        return None

# ========================
#  MAIN SCRAPING LOOP
# ========================
def scrape_all_months():
    global start_time_global
    start_time_global = datetime.now()
    
    driver = None
    total_processed = 0
    failed_dates = []
    
    log("=" * 60, "STAGE")
    log("FPI ARCHIVE SCRAPER - WIDE FORMAT", "STAGE")
    log("=" * 60, "STAGE")
    log(f"Year range: {START_YEAR} - {END_YEAR}", "INFO")
    log(f"Mode: {'Last day of month' if USE_MONTH_LAST_DAY else 'First day of month'}", "INFO")
    
    init_db()
    
    # Generate dates to scrape
    dates_to_scrape = []
    for year in range(START_YEAR, END_YEAR + 1):
        for month in range(1, 13):
            if USE_MONTH_LAST_DAY:
                day = calendar.monthrange(year, month)[1]
            else:
                day = 1
            dates_to_scrape.append((day, month, year))
    
    if END_INDEX:
        dates_to_scrape = dates_to_scrape[START_INDEX:END_INDEX]
    elif START_INDEX > 0:
        dates_to_scrape = dates_to_scrape[START_INDEX:]
    
    total_dates = len(dates_to_scrape)
    log(f"Total dates to scrape: {total_dates}", "INFO")
    
    try:
        driver = build_driver()
        load_url_with_retry(driver, URL)
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "txtDate")))
        log("Page ready. Starting scraping...\n", "SUCCESS")
        
        for idx, (day, month, year) in enumerate(dates_to_scrape, 1):
            month_name = calendar.month_name[month]
            date_str = f"{day:02d}-{calendar.month_abbr[month]}-{year}"
            
            log_progress(idx, total_dates, f"{date_str}")
            
            success = False
            
            for attempt in range(1, 4):
                try:
                    try:
                        driver.current_url
                    except:
                        driver = restart_browser(driver)
                    
                    if not set_date(driver, day, month, year):
                        log(f"  Date set failed", "WARNING")
                        if attempt < 3:
                            load_url_with_retry(driver, URL)
                            continue
                    
                    log(f"  Date set: {date_str}", "SUCCESS")
                    
                    if not click_view_report(driver):
                        log(f"  View Report failed", "WARNING")
                        if attempt < 3:
                            load_url_with_retry(driver, URL)
                            continue
                    
                    time.sleep(2)
                    
                    # Extract and pivot to wide format
                    wide_row = extract_and_pivot_to_wide(driver, date_str)
                    
                    if wide_row and len(wide_row) > 1:  # More than just reporting_date
                        upsert_wide_row(wide_row)
                        total_processed += 1
                        log(f"  ✅ Inserted {len(wide_row)-1} metrics for {date_str}", "SUCCESS")
                        print()
                        success = True
                        break
                    else:
                        log(f"  No data (attempt {attempt}/3)", "WARNING")
                        if attempt < 3:
                            load_url_with_retry(driver, URL)
                
                except Exception as e:
                    log(f"  Error: {e}", "ERROR")
                    if attempt < 3:
                        try:
                            load_url_with_retry(driver, URL)
                        except:
                            driver = restart_browser(driver)
                
                if attempt == 3 and not success:
                    failed_dates.append(date_str)
        
        # Retry failed dates
        if failed_dates:
            log(f"\nRetrying {len(failed_dates)} failed dates...", "STAGE")
            try:
                driver = restart_browser(driver)
            except:
                driver = build_driver()
                load_url_with_retry(driver, URL)
            
            still_failed = []
            for date_str in failed_dates:
                log(f"Retrying: {date_str}", "STAGE")
                parts = date_str.split("-")
                day = int(parts[0])
                month = list(calendar.month_abbr).index(parts[1])
                year = int(parts[2])
                
                try:
                    set_date(driver, day, month, year)
                    click_view_report(driver)
                    time.sleep(2)
                    wide_row = extract_and_pivot_to_wide(driver, date_str)
                    
                    if wide_row and len(wide_row) > 1:
                        upsert_wide_row(wide_row)
                        total_processed += 1
                        log(f"  ✅ Retry successful", "SUCCESS")
                    else:
                        still_failed.append(date_str)
                except Exception as e:
                    still_failed.append(date_str)
            
            failed_dates = still_failed
    
    except Exception as e:
        log(f"FATAL ERROR: {e}", "ERROR")
        traceback.print_exc()
    
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
        
        total_elapsed = (datetime.now() - start_time_global).total_seconds()
        
        log("\n" + "=" * 60, "STAGE")
        log("SCRAPING COMPLETE", "STAGE")
        log("=" * 60, "STAGE")
        log(f"Total time: {timedelta(seconds=int(total_elapsed))}", "INFO")
        log(f"Dates processed: {total_processed}/{total_dates}", "SUCCESS")
        
        if failed_dates:
            log(f"Failed ({len(failed_dates)}): {failed_dates}", "ERROR")
        else:
            log("ALL DATES SCRAPED SUCCESSFULLY!", "SUCCESS")
        
        columns = get_existing_columns()
        with get_connection() as con:
            count = con.execute("SELECT COUNT(*) FROM fpi_wide_data").fetchone()[0]
        
        log(f"\nDatabase: {DB_FILE}", "INFO")
        log(f"Rows (dates): {count}", "INFO")
        log(f"Columns: {len(columns)} (1 date + {len(columns)-1} metrics)", "INFO")
        print()

if __name__ == "__main__":
    scrape_all_months()