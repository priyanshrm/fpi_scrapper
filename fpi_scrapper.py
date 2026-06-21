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

def init_db():
    """Create the table with only reporting_date as fixed column."""
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
    """Return current column names."""
    with get_connection() as con:
        cur = con.execute("PRAGMA table_info(fpi_monthly_data)")
        return [row[1] for row in cur.fetchall()]

def ensure_columns(col_names):
    """Add new combination columns that don't exist yet."""
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
    """Make column name SQL-safe but still descriptive."""
    return name.replace("(", "").replace(")", "").replace("/", "_").replace("&", "and").replace("$", "USD").replace(" ", "_").replace("-", "_").replace(".", "_")

def upsert_monthly_row(row_data):
    """Insert or replace a monthly row with dynamic columns."""
    # Ensure all columns exist
    ensure_columns(row_data.keys())
    
    # Get all existing columns
    all_cols = get_existing_columns()
    
    # Build complete row (missing columns get empty string)
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
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    options.page_load_strategy = 'eager'
    
    chrome_path = os.environ.get("CHROME_PATH")
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    
    if chrome_path and chromedriver_path:
        log(f"Using GitHub Actions Chrome", "INFO")
        options.binary_location = chrome_path
        service = Service(executable_path=chromedriver_path)
    else:
        log("Using webdriver-manager", "INFO")
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
    log(f"Loading URL: {url}", "STAGE")
    
    for attempt in range(1, max_retries + 1):
        try:
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
                log(f"  Timeout, retrying...", "WARNING")
                time.sleep(5)
            else:
                log(f"  Error: {error_msg[:100]}", "ERROR")
            
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
            log("  Browser restarted successfully", "SUCCESS")
            return new_driver
        except Exception as e:
            log(f"  Restart attempt {attempt} failed: {e}", "ERROR")
            if attempt == max_attempts:
                raise

# ========================
#  DATE SELECTION
# ========================
def set_date(driver, day, month, year):
    """Set date via hidden fields."""
    date_str = f"{day:02d}-{calendar.month_abbr[month]}-{year}"
    
    driver.execute_script(f"""
        document.getElementById('hdnDate').value = '{date_str}';
        document.getElementById('txtDate').value = '{date_str}';
        var event = new Event('change', {{ bubbles: true }});
        var hdnDate = document.getElementById('hdnDate');
        if (hdnDate) hdnDate.dispatchEvent(event);
    """)
    
    time.sleep(0.3)
    
    actual = driver.execute_script("return document.getElementById('hdnDate').value;")
    if date_str in actual:
        return True
    
    return False

# ========================
#  CLICK VIEW REPORT
# ========================
def click_view_report(driver):
    """Click View Report button."""
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
#  EXTRACT MONTHLY TOTAL AND PIVOT TO WIDE FORMAT
# ========================
def extract_monthly_totals(driver, month_name):
    """
    Find the 'Total for Month' row and extract all combinations.
    Returns a single dict (one row) with all combinations as columns.
    """
    
    # Find the "Total for Month" row
    total_cells = driver.find_elements(By.XPATH, f"//td[contains(text(),'Total for {month_name}')]")
    
    if not total_cells:
        log(f"  Could not find 'Total for {month_name}' row", "ERROR")
        return None
    
    # Use the LAST occurrence (monthly summary, not daily table)
    total_cell = total_cells[-1]
    total_row = total_cell.find_element(By.XPATH, "./ancestor::tr")
    
    log(f"  Found 'Total for {month_name}' (using occurrence #{len(total_cells)})", "SUCCESS")
    
    # Collect rows BEFORE the total row (the data rows)
    all_rows = []
    current_row = total_row
    
    while True:
        try:
            prev_row = current_row.find_element(By.XPATH, "preceding-sibling::tr[1]")
            row_text = prev_row.text.strip()
            
            # Stop if we hit the header or another month's total
            if "Daily Trends" in row_text:
                break
            if "Total for" in row_text and month_name not in row_text:
                break
            
            # Stop if we hit a daily date row
            cells = prev_row.find_elements(By.TAG_NAME, "td")
            if cells:
                first_text = cells[0].text.strip()
                if re.match(r'^\d{2}-[A-Za-z]{3}-\d{4}$', first_text):
                    break
            
            all_rows.insert(0, prev_row)
            current_row = prev_row
        except:
            break
    
    log(f"  Found {len(all_rows)} data rows in monthly block", "INFO")
    
    # Parse rows and build combinations
    row_data = {}
    current_de = None
    combo_count = 0
    
    for row in all_rows:
        try:
            cells = row.find_elements(By.TAG_NAME, "td")
            if not cells:
                continue
            
            cell_texts = [c.text.strip() for c in cells]
            
            # Skip empty rows
            if all(t == "" for t in cell_texts):
                continue
            
            # Skip Sub-total and Total rows
            if cell_texts[0] in ("Sub-total", "Total"):
                continue
            
            # Check if this is a Debt/Equity header row (has rowspan)
            rowspan = cells[0].get_attribute("rowspan")
            rowspan_int = int(rowspan) if rowspan and rowspan.isdigit() else 0
            
            if rowspan_int > 1 and len(cell_texts) >= 6:
                # This is a category header: Debt/Equity + Investment Route + 4 metrics
                current_de = cell_texts[0]
                inv_route = cell_texts[1]
                
                if inv_route in ("Sub-total", "Total", ""):
                    continue
                
                metrics = cell_texts[2:6]  # GP, GS, NI, USD
                
            elif rowspan_int > 1 and len(cell_texts) >= 2:
                # Category header where Investment Route is in cell[1] and metrics start at cell[2]
                current_de = cell_texts[0]
                inv_route = cell_texts[1]
                
                if inv_route in ("Sub-total", "Total", ""):
                    continue
                
                if len(cell_texts) >= 6:
                    metrics = cell_texts[2:6]
                else:
                    continue
                
            elif current_de and len(cell_texts) >= 5:
                # Data row inheriting Debt/Equity
                inv_route = cell_texts[0]
                
                if inv_route in ("Sub-total", "Total", ""):
                    continue
                
                metrics = cell_texts[1:5]
                
            else:
                continue
            
            # Build column names for each metric
            metric_names = [
                "Gross_Purchases_Rs_Crore",
                "Gross_Sales_Rs_Crore",
                "Net_Investment_Rs_Crore",
                "Net_Investment_USD_million"
            ]
            
            for metric_name, val in zip(metric_names, metrics):
                col_name = sanitize_column_name(f"{current_de}_{inv_route}_{metric_name}")
                row_data[col_name] = val
                combo_count += 1
        
        except StaleElementReferenceException:
            continue
        except Exception as e:
            continue
    
    log(f"  Created {combo_count} metric columns from {combo_count//4} combinations", "SUCCESS")
    
    return row_data

# ========================
#  MAIN SCRAPING LOOP
# ========================
def scrape_all_months():
    global start_time_global
    start_time_global = datetime.now()
    
    driver = None
    total_processed = 0
    failed_months = []
    
    log("=" * 60, "STAGE")
    log("FPI ARCHIVE SCRAPER - MONTHLY WIDE FORMAT", "STAGE")
    log("=" * 60, "STAGE")
    log(f"Year range: {START_YEAR} - {END_YEAR}", "INFO")
    log(f"Date mode: {'Last day of month (full month)' if USE_MONTH_LAST_DAY else 'First day of month'}", "INFO")
    
    # Initialize database
    init_db()
    
    # Generate months list
    months_to_scrape = []
    for year in range(START_YEAR, END_YEAR + 1):
        for month in range(1, 13):
            if USE_MONTH_LAST_DAY:
                day = calendar.monthrange(year, month)[1]
            else:
                day = 1
            months_to_scrape.append((year, month, day))
    
    # Apply chunking
    if END_INDEX:
        months_to_scrape = months_to_scrape[START_INDEX:END_INDEX]
    elif START_INDEX > 0:
        months_to_scrape = months_to_scrape[START_INDEX:]
    
    total_months = len(months_to_scrape)
    log(f"Total months to scrape: {total_months}", "INFO")
    
    try:
        driver = build_driver()
        load_url_with_retry(driver, URL)
        
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.ID, "txtDate"))
        )
        log("Page ready. Starting scraping...\n", "SUCCESS")
        
        for idx, (year, month, day) in enumerate(months_to_scrape, 1):
            month_name = calendar.month_name[month]
            
            log_progress(idx, total_months, f"{month_name} {year}")
            
            success = False
            
            for attempt in range(1, 4):
                try:
                    # Check browser
                    try:
                        driver.current_url
                    except:
                        driver = restart_browser(driver)
                    
                    # Set date
                    if not set_date(driver, day, month, year):
                        log(f"  Date set failed (attempt {attempt}/3)", "WARNING")
                        if attempt < 3:
                            load_url_with_retry(driver, URL)
                            continue
                    
                    # Click view report
                    if not click_view_report(driver):
                        log(f"  View Report failed (attempt {attempt}/3)", "WARNING")
                        if attempt < 3:
                            load_url_with_retry(driver, URL)
                            continue
                    
                    time.sleep(2)
                    
                    # Extract monthly totals
                    monthly_data = extract_monthly_totals(driver, month_name)
                    
                    if monthly_data and len(monthly_data) > 0:
                        # Build row
                        row_data = {
                            "reporting_date": f"{year}-{month:02d}-{day:02d}"
                        }
                        row_data.update(monthly_data)
                        
                        upsert_monthly_row(row_data)
                        total_processed += 1
                        log(f"  ✅ Inserted {len(monthly_data)} metrics for {month_name} {year}", "SUCCESS")
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
                
                if attempt == 3:
                    failed_months.append(f"{month_name} {year}")
        
        # Retry failed months
        if failed_months:
            log(f"\nRetrying {len(failed_months)} failed months...", "STAGE")
            
            try:
                driver = restart_browser(driver)
            except:
                driver = build_driver()
                load_url_with_retry(driver, URL)
            
            still_failed = []
            for fail_str in failed_months:
                log(f"Retrying: {fail_str}", "STAGE")
                
                parts = fail_str.split()
                month_name = parts[0]
                year = int(parts[1])
                month = list(calendar.month_name).index(month_name)
                day = calendar.monthrange(year, month)[1] if USE_MONTH_LAST_DAY else 1
                
                try:
                    set_date(driver, day, month, year)
                    click_view_report(driver)
                    time.sleep(2)
                    
                    monthly_data = extract_monthly_totals(driver, month_name)
                    
                    if monthly_data and len(monthly_data) > 0:
                        row_data = {"reporting_date": f"{year}-{month:02d}-{day:02d}"}
                        row_data.update(monthly_data)
                        upsert_monthly_row(row_data)
                        total_processed += 1
                        log(f"  ✅ Retry successful", "SUCCESS")
                    else:
                        still_failed.append(fail_str)
                        
                except Exception as e:
                    still_failed.append(fail_str)
                    log(f"  ❌ Retry failed: {e}", "ERROR")
            
            failed_months = still_failed
    
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
        log(f"Months processed: {total_processed}/{total_months}", "SUCCESS" if total_processed == total_months else "WARNING")
        
        if failed_months:
            log(f"Failed ({len(failed_months)}): {failed_months}", "ERROR")
        else:
            log("ALL MONTHS SUCCESSFULLY SCRAPED!", "SUCCESS")
        
        # Database info
        columns = get_existing_columns()
        with get_connection() as con:
            count = con.execute("SELECT COUNT(*) FROM fpi_monthly_data").fetchone()[0]
        
        log(f"\nDatabase: {DB_FILE}", "INFO")
        log(f"Rows (months): {count}", "INFO")
        log(f"Columns: {len(columns)} (1 date + {len(columns)-1} metrics)", "INFO")
        
        print()

if __name__ == "__main__":
    scrape_all_months()