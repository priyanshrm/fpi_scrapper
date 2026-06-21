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
    log(f"  Added {len(new_cols)} new columns", "INFO")

def upsert_daily_rows(rows_list):
    """Insert multiple wide-format rows at once."""
    if not rows_list:
        return
    
    # Collect all column names from all rows
    all_col_names = set()
    for row in rows_list:
        all_col_names.update(row.keys())
    
    ensure_columns(all_col_names)
    all_cols = get_existing_columns()
    
    with get_connection() as con:
        for row_data in rows_list:
            full_row = {}
            for col in all_cols:
                full_row[col] = row_data.get(col, "")
            
            cols_sql = ", ".join(f'"{c}"' for c in all_cols)
            placeholders = ", ".join("?" for _ in all_cols)
            values = [full_row[c] for c in all_cols]
            
            try:
                con.execute(
                    f'INSERT OR REPLACE INTO fpi_daily_wide ({cols_sql}) VALUES ({placeholders})',
                    values
                )
            except Exception as e:
                log(f"  DB insert error: {e}", "WARNING")
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
#  EXTRACT ALL DAYS FROM A MONTHLY TABLE
# ========================
def extract_all_days_from_month(driver, month_name, year):
    """
    Select the last day of the month, then extract ALL trading days
    visible in the table. Handles both old and new table formats.
    """
    all_daily_rows = []
    
    try:
        tables = driver.find_elements(By.CSS_SELECTOR, "table.tbls01")
        
        if not tables:
            log("  No data tables found!", "ERROR")
            return all_daily_rows
        
        table = tables[0]
        tr_elements = table.find_elements(By.TAG_NAME, "tr")
        
        # First, detect table format by checking header row
        has_investment_route = False
        for tr in tr_elements[:5]:  # Check first few rows
            cells = tr.find_elements(By.TAG_NAME, "th")
            if cells:
                header_texts = [c.text.strip() for c in cells]
                if "Investment Route" in " ".join(header_texts):
                    has_investment_route = True
                    break
        
        if has_investment_route:
            log(f"  Detected MODERN table format (with Investment Route column)", "INFO")
        else:
            log(f"  Detected OLD table format (no Investment Route column)", "INFO")
        
        current_date = None
        current_debt_equity = None
        date_pattern = r'^\d{2}-[A-Za-z]{3}-\d{4}$'
        current_row_data = {}
        days_found = 0
        
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
                if "Total for" in " ".join(cell_texts):
                    continue
                if "Grand Total" in " ".join(cell_texts):
                    continue
                
                # Check for date row
                if re.match(date_pattern, cell_texts[0]):
                    # Save previous date's data
                    if current_date and current_row_data:
                        current_row_data["reporting_date"] = current_date
                        all_daily_rows.append(current_row_data)
                        days_found += 1
                    
                    # Start new date
                    current_date = cell_texts[0]
                    current_row_data = {}
                    
                    if has_investment_route:
                        # MODERN FORMAT: Date | Debt/Equity | Investment Route | GP | GS | NI | USD
                        if len(cell_texts) >= 3:
                            # Check if cell[1] is a Debt/Equity category
                            if cell_texts[1] in ("Equity", "Debt", "Debt-General Limit", 
                                                   "Debt-VRR", "Debt-FAR", "Hybrid", 
                                                   "Mutual Funds", "AIFs"):
                                current_debt_equity = cell_texts[1]
                                inv_route = cell_texts[2] if len(cell_texts) > 2 else ""
                            else:
                                inv_route = cell_texts[1]
                            
                            if inv_route not in ("Sub-total", "Total", "") and len(cell_texts) >= 7:
                                metrics = cell_texts[3:7]
                                metric_suffixes = [
                                    "Gross_Purchases_Rs_Crore",
                                    "Gross_Sales_Rs_Crore",
                                    "Net_Investment_Rs_Crore",
                                    "Net_Investment_USD_million"
                                ]
                                for suffix, val in zip(metric_suffixes, metrics):
                                    col_name = sanitize_column_name(f"{current_debt_equity}_{inv_route}_{suffix}")
                                    current_row_data[col_name] = val
                    
                    else:
                        # OLD FORMAT: Date | Debt/Equity | GP | GS | NI | USD
                        if len(cell_texts) >= 2:
                            if cell_texts[1] in ("Equity", "Debt"):
                                current_debt_equity = cell_texts[1]
                                
                                if len(cell_texts) >= 6:
                                    metrics = cell_texts[2:6]
                                    metric_suffixes = [
                                        "Gross_Purchases_Rs_Crore",
                                        "Gross_Sales_Rs_Crore",
                                        "Net_Investment_Rs_Crore",
                                        "Net_Investment_USD_million"
                                    ]
                                    for suffix, val in zip(metric_suffixes, metrics):
                                        col_name = sanitize_column_name(f"{current_debt_equity}_Total_{suffix}")
                                        current_row_data[col_name] = val
                    
                elif has_investment_route and len(cell_texts) >= 2 and cell_texts[0] in (
                    "Equity", "Debt", "Debt-General Limit", "Debt-VRR", 
                    "Debt-FAR", "Hybrid", "Mutual Funds", "AIFs"
                ):
                    current_debt_equity = cell_texts[0]
                    inv_route = cell_texts[1]
                    
                    if inv_route not in ("Sub-total", "Total", "") and len(cell_texts) >= 6:
                        metrics = cell_texts[2:6]
                        metric_suffixes = [
                            "Gross_Purchases_Rs_Crore",
                            "Gross_Sales_Rs_Crore",
                            "Net_Investment_Rs_Crore",
                            "Net_Investment_USD_million"
                        ]
                        for suffix, val in zip(metric_suffixes, metrics):
                            col_name = sanitize_column_name(f"{current_debt_equity}_{inv_route}_{suffix}")
                            current_row_data[col_name] = val
                        
                elif has_investment_route and len(cell_texts) >= 5 and current_debt_equity:
                    inv_route = cell_texts[0]
                    
                    if inv_route not in ("Sub-total", "Total", ""):
                        metrics = cell_texts[1:5]
                        metric_suffixes = [
                            "Gross_Purchases_Rs_Crore",
                            "Gross_Sales_Rs_Crore",
                            "Net_Investment_Rs_Crore",
                            "Net_Investment_USD_million"
                        ]
                        for suffix, val in zip(metric_suffixes, metrics):
                            col_name = sanitize_column_name(f"{current_debt_equity}_{inv_route}_{suffix}")
                            current_row_data[col_name] = val
                
            except StaleElementReferenceException:
                continue
            except Exception:
                continue
        
        # Save last date
        if current_date and current_row_data:
            current_row_data["reporting_date"] = current_date
            all_daily_rows.append(current_row_data)
            days_found += 1
        
        log(f"  Extracted {days_found} trading days for {month_name} {year}", "SUCCESS")
        
        if days_found > 0:
            sample = all_daily_rows[0]
            log(f"  Sample: {sample['reporting_date']} - {len(sample)-1} metrics", "INFO")
        
        return all_daily_rows
        
    except Exception as e:
        log(f"  Extraction error: {e}", "ERROR")
        traceback.print_exc()
        return all_daily_rows
    
# ========================
#  MAIN SCRAPING LOOP
# ========================
def scrape_all_months():
    global start_time_global
    start_time_global = datetime.now()
    
    driver = None
    total_days_saved = 0
    failed_months = []
    
    log("=" * 60, "STAGE")
    log("FPI ARCHIVE SCRAPER - OPTIMIZED MONTHLY EXTRACTION", "STAGE")
    log("=" * 60, "STAGE")
    log(f"Year range: {START_YEAR} - {END_YEAR}", "INFO")
    log(f"Strategy: 1 page load per month, extract all trading days", "INFO")
    
    init_db()
    
    # Generate months list
    months_to_scrape = []
    for year in range(START_YEAR, END_YEAR + 1):
        for month in range(1, 13):
            last_day = calendar.monthrange(year, month)[1]
            months_to_scrape.append((year, month, last_day))
    
    # Apply chunking
    if END_INDEX:
        months_to_scrape = months_to_scrape[START_INDEX:END_INDEX]
    elif START_INDEX > 0:
        months_to_scrape = months_to_scrape[START_INDEX:]
    
    total_months = len(months_to_scrape)
    log(f"Total months to scrape: {total_months}", "INFO")
    log(f"Estimated time: {total_months * 15 // 60}-{total_months * 20 // 60} minutes", "INFO")
    
    try:
        driver = build_driver()
        load_url_with_retry(driver, URL)
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "txtDate")))
        log("Page ready. Starting scraping...\n", "SUCCESS")
        
        for idx, (year, month, last_day) in enumerate(months_to_scrape, 1):
            month_name = calendar.month_name[month]
            
            log_progress(idx, total_months, f"{month_name} {year}")
            
            success = False
            
            for attempt in range(1, 4):
                try:
                    try:
                        driver.current_url
                    except:
                        driver = restart_browser(driver)
                    
                    # Set to last day of month
                    if not set_date(driver, last_day, month, year):
                        log(f"  Date set failed", "WARNING")
                        if attempt < 3:
                            load_url_with_retry(driver, URL)
                            continue
                    
                    log(f"  Date set: {last_day:02d}-{calendar.month_abbr[month]}-{year}", "SUCCESS")
                    
                    if not click_view_report(driver):
                        log(f"  View Report failed", "WARNING")
                        if attempt < 3:
                            load_url_with_retry(driver, URL)
                            continue
                    
                    time.sleep(2)
                    
                    # Extract ALL days from this month's table
                    daily_rows = extract_all_days_from_month(driver, month_name, year)
                    
                    if daily_rows:
                        upsert_daily_rows(daily_rows)
                        total_days_saved += len(daily_rows)
                        log(f"  ✅ Saved {len(daily_rows)} trading days for {month_name} {year}", "SUCCESS")
                        print()
                        success = True
                        break
                    else:
                        log(f"  No data extracted (attempt {attempt}/3)", "WARNING")
                        if attempt < 3:
                            load_url_with_retry(driver, URL)
                
                except Exception as e:
                    log(f"  Error (attempt {attempt}/3): {e}", "ERROR")
                    if attempt < 3:
                        try:
                            load_url_with_retry(driver, URL)
                        except:
                            driver = restart_browser(driver)
                
                if attempt == 3 and not success:
                    failed_months.append(f"{month_name} {year}")
    
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
        log(f"Months processed: {total_months - len(failed_months)}/{total_months}", "SUCCESS")
        log(f"Total trading days saved: {total_days_saved}", "SUCCESS")
        
        if failed_months:
            log(f"Failed months ({len(failed_months)}):", "ERROR")
            for m in failed_months:
                log(f"  - {m}", "ERROR")
        else:
            log("ALL MONTHS SUCCESSFULLY SCRAPED!", "SUCCESS")
        
        columns = get_existing_columns()
        with get_connection() as con:
            count = con.execute("SELECT COUNT(*) FROM fpi_daily_wide").fetchone()[0]
            dates = con.execute("SELECT COUNT(DISTINCT reporting_date) FROM fpi_daily_wide").fetchone()[0]
        
        log(f"\nDatabase: {DB_FILE}", "INFO")
        log(f"Rows (trading days): {count}", "INFO")
        log(f"Unique dates: {dates}", "INFO")
        log(f"Columns: {len(columns)} (1 date + {len(columns)-1} metrics)", "INFO")
        print()

if __name__ == "__main__":
    scrape_all_months()