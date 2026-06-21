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
parser.add_argument("--start", type=int, default=0)
parser.add_argument("--end", type=int, default=None)
args = parser.parse_args()

START_YEAR = args.start_year
END_YEAR = args.end_year
START_INDEX = args.start
END_INDEX = args.end

URL = "https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx"
DB_FILE = f"fpi_data_{START_YEAR}_{END_YEAR}_{START_INDEX}_{END_INDEX if END_INDEX else 'all'}.db"

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
        "INFO": "ℹ️", "SUCCESS": "✅", "WARNING": "⚠️",
        "ERROR": "❌", "PROGRESS": "📊", "STAGE": "🔄",
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
    return name.replace("(", "").replace(")", "").replace("/", "_").replace("&", "and").replace("$", "USD").replace(" ", "_").replace("-", "_").replace(".", "").replace("'", "")

def init_db():
    log("Initializing database...", "STAGE")
    with get_connection() as con:
        cols_def = ", ".join(f'"{c}" TEXT' for c in FIXED_FIELDS)
        con.execute(f"""
            CREATE TABLE IF NOT EXISTS fpi_daily_wide (
                {cols_def},
                PRIMARY KEY ({', '.join(f'"{k}"' for k in KEY_FIELDS)})
            )
        """)
        con.commit()
    log(f"Database ready: {DB_FILE}", "SUCCESS")

def get_existing_columns():
    with get_connection() as con:
        cur = con.execute("PRAGMA table_info(fpi_daily_wide)")
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
                con.execute(f'ALTER TABLE fpi_daily_wide ADD COLUMN "{safe_col}" TEXT DEFAULT ""')
            except Exception as e:
                log(f"  Could not add column {safe_col}: {e}", "WARNING")
        con.commit()

def upsert_daily_row(row_data):
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
            f'INSERT OR REPLACE INTO fpi_daily_wide ({cols_sql}) VALUES ({placeholders})',
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
#  EXTRACT ONE DAY'S DATA AND PIVOT TO WIDE
# ========================
def extract_single_day_data(driver, target_date_str):
    """
    Extract data for ONE specific date from the daily table.
    The table shows cumulative data up to the selected date,
    but we only want the row for target_date_str.
    
    Returns a dict with ONE row: {reporting_date: ..., col1: val1, ...}
    """
    wide_data = {}
    
    try:
        tables = driver.find_elements(By.CSS_SELECTOR, "table.tbls01")
        
        if not tables:
            log("  No data tables found!", "ERROR")
            return None
        
        table = tables[0]
        tr_elements = table.find_elements(By.TAG_NAME, "tr")
        
        current_date = None
        current_debt_equity = None
        date_pattern = r'^\d{2}-[A-Za-z]{3}-\d{4}$'
        found_target = False
        rows_for_target = 0
        
        for tr in tr_elements:
            try:
                cells = tr.find_elements(By.TAG_NAME, "td")
                if not cells:
                    continue
                
                cell_texts = [c.text.strip() for c in cells]
                
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
                    
                    # If we've moved past our target date, stop
                    if found_target and current_date != target_date_str:
                        break
                    
                    # Check if this is our target date
                    if current_date == target_date_str:
                        found_target = True
                    
                    if not found_target:
                        continue
                    
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
                            metrics = cell_texts[3:7]
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
                                rows_for_target += 1
                    
                elif found_target and len(cell_texts) >= 2 and cell_texts[0] in (
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
                            rows_for_target += 1
                        
                elif found_target and len(cell_texts) >= 5 and current_debt_equity:
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
                        rows_for_target += 1
                
            except StaleElementReferenceException:
                continue
            except Exception:
                continue
        
        if rows_for_target > 0:
            wide_data["reporting_date"] = target_date_str
            
            combinations = rows_for_target // 4
            log(f"  Extracted {combinations} combinations ({rows_for_target} metrics)", "SUCCESS")
            
            return wide_data
        else:
            log(f"  No data found for {target_date_str} (might be weekend/holiday)", "WARNING")
            return None
        
    except Exception as e:
        log(f"  Extraction error: {e}", "ERROR")
        return None

# ========================
#  MAIN SCRAPING LOOP
# ========================
def scrape_all_dates():
    global start_time_global
    start_time_global = datetime.now()
    
    driver = None
    total_processed = 0
    failed_dates = []
    
    log("=" * 60, "STAGE")
    log("FPI ARCHIVE SCRAPER - DAILY WIDE FORMAT", "STAGE")
    log("=" * 60, "STAGE")
    log(f"Year range: {START_YEAR} - {END_YEAR}", "INFO")
    log(f"Scraping EVERY calendar day (weekends/holidays will have no data)", "INFO")
    
    init_db()
    
    # Generate ALL calendar dates
    all_dates = []
    for year in range(START_YEAR, END_YEAR + 1):
        for month in range(1, 13):
            num_days = calendar.monthrange(year, month)[1]
            for day in range(1, num_days + 1):
                all_dates.append((day, month, year))
    
    # Apply chunking
    if END_INDEX:
        all_dates = all_dates[START_INDEX:END_INDEX]
    elif START_INDEX > 0:
        all_dates = all_dates[START_INDEX:]
    
    total_dates = len(all_dates)
    log(f"Total calendar dates to scrape: {total_dates}", "INFO")
    log(f"Estimated trading days: ~{int(total_dates * 0.7)} (excluding weekends/holidays)", "INFO")
    
    try:
        driver = build_driver()
        load_url_with_retry(driver, URL)
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "txtDate")))
        log("Page ready. Starting scraping...\n", "SUCCESS")
        
        consecutive_empty = 0
        
        for idx, (day, month, year) in enumerate(all_dates, 1):
            date_str = f"{day:02d}-{calendar.month_abbr[month]}-{year}"
            
            log_progress(idx, total_dates, date_str)
            
            # Skip if we've had many consecutive empty days (likely reached future dates)
            if consecutive_empty > 10:
                log(f"  Skipping {date_str} (10+ consecutive empty days, likely future date)", "INFO")
                continue
            
            success = False
            
            for attempt in range(1, 3):  # Only 2 attempts per date for speed
                try:
                    try:
                        driver.current_url
                    except:
                        driver = restart_browser(driver)
                    
                    if not set_date(driver, day, month, year):
                        if attempt < 2:
                            load_url_with_retry(driver, URL)
                            continue
                    
                    if not click_view_report(driver):
                        if attempt < 2:
                            load_url_with_retry(driver, URL)
                            continue
                    
                    time.sleep(2)
                    
                    daily_row = extract_single_day_data(driver, date_str)
                    
                    if daily_row and len(daily_row) > 1:
                        upsert_daily_row(daily_row)
                        total_processed += 1
                        consecutive_empty = 0
                        success = True
                        break
                    else:
                        consecutive_empty += 1
                        log(f"  No data (weekend/holiday) - {consecutive_empty} consecutive empty", "INFO")
                        success = True  # Not an error, just no data
                        break
                
                except Exception as e:
                    log(f"  Error: {e}", "ERROR")
                    if attempt < 2:
                        try:
                            load_url_with_retry(driver, URL)
                        except:
                            driver = restart_browser(driver)
                
                if attempt == 2 and not success:
                    failed_dates.append(date_str)
                    consecutive_empty += 1
    
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
        log(f"Dates with data: {total_processed}", "SUCCESS")
        log(f"Dates without data (weekends/holidays): {total_dates - total_processed - len(failed_dates)}", "INFO")
        
        if failed_dates:
            log(f"Failed dates ({len(failed_dates)}): {failed_dates[:10]}...", "ERROR")
        else:
            log("NO FAILED DATES!", "SUCCESS")
        
        columns = get_existing_columns()
        with get_connection() as con:
            count = con.execute("SELECT COUNT(*) FROM fpi_daily_wide").fetchone()[0]
        
        log(f"\nDatabase: {DB_FILE}", "INFO")
        log(f"Rows (trading days): {count}", "INFO")
        log(f"Columns: {len(columns)} (1 date + {len(columns)-1} metrics)", "INFO")
        print()

if __name__ == "__main__":
    scrape_all_dates()