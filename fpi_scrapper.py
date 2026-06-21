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

# Fixed columns for primary key
FIXED_FIELDS = ["reporting_date", "debt_equity", "investment_route"]
KEY_FIELDS = ["reporting_date", "debt_equity", "investment_route"]

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
    log("Initializing database...", "STAGE")
    with get_connection() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS fpi_daily_data (
                reporting_date TEXT NOT NULL,
                debt_equity TEXT NOT NULL,
                investment_route TEXT NOT NULL,
                gross_purchases_rs_crore TEXT DEFAULT '',
                gross_sales_rs_crore TEXT DEFAULT '',
                net_investment_rs_crore TEXT DEFAULT '',
                net_investment_usd_million TEXT DEFAULT '',
                PRIMARY KEY (reporting_date, debt_equity, investment_route)
            )
        """)
        con.commit()
    log(f"Database ready: {DB_FILE}", "SUCCESS")

def insert_daily_rows(rows):
    """Insert multiple daily rows at once."""
    if not rows:
        return
    
    with get_connection() as con:
        for row in rows:
            try:
                con.execute("""
                    INSERT OR REPLACE INTO fpi_daily_data 
                    (reporting_date, debt_equity, investment_route, 
                     gross_purchases_rs_crore, gross_sales_rs_crore,
                     net_investment_rs_crore, net_investment_usd_million)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    row["reporting_date"],
                    row["debt_equity"],
                    row["investment_route"],
                    row["gross_purchases_rs_crore"],
                    row["gross_sales_rs_crore"],
                    row["net_investment_rs_crore"],
                    row["net_investment_usd_million"],
                ))
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
        log(f"Using GitHub Actions Chrome: {chrome_path}", "INFO")
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
                raise ConnectionError(f"Failed to load URL after {max_retries} attempts")

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
    
    # Verify
    actual = driver.execute_script("return document.getElementById('hdnDate').value;")
    if date_str in actual:
        log(f"  Date set: {date_str}", "SUCCESS")
        return True
    
    log(f"  Date verification failed. Expected: {date_str}, Got: {actual}", "WARNING")
    return False

# ========================
#  CLICK VIEW REPORT
# ========================
def click_view_report(driver):
    """Click View Report button."""
    log("  Clicking 'View Report'...", "INFO")
    
    view_btn = WebDriverWait(driver, 15).until(
        EC.element_to_be_clickable((By.ID, "btnSubmit1"))
    )
    driver.execute_script("arguments[0].click();", view_btn)
    time.sleep(3)
    
    # Wait for table to load
    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "dvArchiveData"))
        )
        log("  Table loaded", "SUCCESS")
        return True
    except TimeoutException:
        log("  Table load timeout", "WARNING")
        return False

# ========================
#  EXTRACT ALL DAILY ROWS
# ========================
def extract_all_daily_rows(driver):
    """
    Extract ALL daily rows from the first table on the page.
    Each row has: Reporting Date, Debt/Equity, Investment Route, 
                  Gross Purchases, Gross Sales, Net Investment, Net Investment US$
    
    Returns a list of dicts, each dict is one row.
    """
    rows = []
    
    try:
        # Find the main data table (first table with class 'tbls01')
        tables = driver.find_elements(By.CSS_SELECTOR, "table.tbls01")
        
        if not tables:
            log("  No data tables found on page!", "ERROR")
            return rows
        
        # Use the first table (daily trends)
        table = tables[0]
        
        # Get all rows from the table
        tr_elements = table.find_elements(By.TAG_NAME, "tr")
        
        log(f"  Found {len(tr_elements)} rows in table", "INFO")
        
        current_date = None
        current_debt_equity = None
        daily_rows_found = 0
        
        for tr in tr_elements:
            try:
                cells = tr.find_elements(By.TAG_NAME, "td")
                if not cells:
                    continue
                
                cell_texts = [c.text.strip() for c in cells]
                
                # Skip empty rows
                if all(t == "" for t in cell_texts):
                    continue
                
                # Skip header rows
                if cell_texts[0] in ("Reporting Date", "Debt/Equity"):
                    continue
                
                # Skip table title rows
                if "Daily Trends" in " ".join(cell_texts):
                    continue
                
                # Skip the disclaimer/note rows
                if "The data presented above" in " ".join(cell_texts):
                    continue
                if "Stock exchanges compile" in " ".join(cell_texts):
                    continue
                
                # Check if this row has a date (like "01-Dec-2010")
                date_pattern = r'^\d{2}-[A-Za-z]{3}-\d{4}$'
                
                if len(cell_texts) > 0 and re.match(date_pattern, cell_texts[0]):
                    # This is a date header row
                    current_date = cell_texts[0]
                    
                    # Also has debt/equity and possibly investment route
                    if len(cell_texts) >= 3:
                        current_debt_equity = cell_texts[1] if cell_texts[1] in ("Equity", "Debt") else current_debt_equity
                        inv_route = cell_texts[2] if len(cell_texts) > 2 else ""
                        
                        # Skip sub-total rows
                        if inv_route in ("Sub-total", "Total", ""):
                            continue
                        
                        if len(cell_texts) >= 7:
                            row_data = {
                                "reporting_date": current_date,
                                "debt_equity": current_debt_equity,
                                "investment_route": inv_route,
                                "gross_purchases_rs_crore": cell_texts[3],
                                "gross_sales_rs_crore": cell_texts[4],
                                "net_investment_rs_crore": cell_texts[5],
                                "net_investment_usd_million": cell_texts[6],
                            }
                            rows.append(row_data)
                            daily_rows_found += 1
                    
                elif len(cell_texts) >= 2 and cell_texts[0] in ("Equity", "Debt"):
                    # Debt/Equity row without date (continuation of same date)
                    current_debt_equity = cell_texts[0]
                    inv_route = cell_texts[1] if len(cell_texts) > 1 else ""
                    
                    if inv_route in ("Sub-total", "Total", ""):
                        continue
                    
                    if len(cell_texts) >= 6 and current_date:
                        row_data = {
                            "reporting_date": current_date,
                            "debt_equity": current_debt_equity,
                            "investment_route": inv_route,
                            "gross_purchases_rs_crore": cell_texts[2],
                            "gross_sales_rs_crore": cell_texts[3],
                            "net_investment_rs_crore": cell_texts[4],
                            "net_investment_usd_million": cell_texts[5],
                        }
                        rows.append(row_data)
                        daily_rows_found += 1
                        
                elif len(cell_texts) >= 5 and current_date and current_debt_equity:
                    # Data row inheriting date and debt/equity from above
                    inv_route = cell_texts[0]
                    
                    if inv_route in ("Sub-total", "Total", ""):
                        continue
                    
                    row_data = {
                        "reporting_date": current_date,
                        "debt_equity": current_debt_equity,
                        "investment_route": inv_route,
                        "gross_purchases_rs_crore": cell_texts[1],
                        "gross_sales_rs_crore": cell_texts[2],
                        "net_investment_rs_crore": cell_texts[3],
                        "net_investment_usd_million": cell_texts[4],
                    }
                    rows.append(row_data)
                    daily_rows_found += 1
                
            except StaleElementReferenceException:
                continue
            except Exception as e:
                continue
        
        log(f"  Extracted {daily_rows_found} daily data rows", "SUCCESS")
        
        # Debug: Show first few rows
        if daily_rows_found > 0:
            log(f"  Sample rows:", "INFO")
            for row in rows[:3]:
                log(f"    {row['reporting_date']} | {row['debt_equity']} | {row['investment_route']} | GP:{row['gross_purchases_rs_crore']}", "INFO")
        
        return rows
        
    except Exception as e:
        log(f"  Extraction error: {e}", "ERROR")
        traceback.print_exc()
        return rows

# ========================
#  MAIN SCRAPING LOOP
# ========================
def scrape_all_months():
    global start_time_global
    start_time_global = datetime.now()
    
    driver = None
    total_days_processed = 0
    total_rows_inserted = 0
    failed_dates = []
    
    log("=" * 60, "STAGE")
    log("FPI ARCHIVE SCRAPER - DAILY EXTRACTION MODE", "STAGE")
    log("=" * 60, "STAGE")
    log(f"Year range: {START_YEAR} - {END_YEAR}", "INFO")
    log(f"Date mode: {'Last day of month' if USE_MONTH_LAST_DAY else 'First day of month'}", "INFO")
    
    # Initialize database
    init_db()
    
    # Generate list of dates to scrape
    dates_to_scrape = []
    for year in range(START_YEAR, END_YEAR + 1):
        for month in range(1, 13):
            if USE_MONTH_LAST_DAY:
                day = calendar.monthrange(year, month)[1]
            else:
                day = 1
            dates_to_scrape.append((day, month, year))
    
    # Apply chunking
    if END_INDEX:
        dates_to_scrape = dates_to_scrape[START_INDEX:END_INDEX]
    elif START_INDEX > 0:
        dates_to_scrape = dates_to_scrape[START_INDEX:]
    
    total_dates = len(dates_to_scrape)
    log(f"Total dates to scrape: {total_dates}", "INFO")
    log(f"Estimated time: {total_dates * 20 // 60}-{total_dates * 25 // 60} minutes", "INFO")
    
    try:
        # Build browser
        driver = build_driver()
        load_url_with_retry(driver, URL)
        
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.ID, "txtDate"))
        )
        log("Page ready. Starting scraping...\n", "SUCCESS")
        
        # Main scraping loop
        for idx, (day, month, year) in enumerate(dates_to_scrape, 1):
            month_name = calendar.month_name[month]
            date_str = f"{day:02d}-{calendar.month_abbr[month]}-{year}"
            
            log_progress(idx, total_dates, f"{date_str}")
            
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
                        if attempt < 3:
                            load_url_with_retry(driver, URL)
                            continue
                    
                    # Click view report
                    if not click_view_report(driver):
                        if attempt < 3:
                            load_url_with_retry(driver, URL)
                            continue
                    
                    # Additional wait for data to render
                    time.sleep(2)
                    
                    # Extract daily rows
                    daily_rows = extract_all_daily_rows(driver)
                    
                    if daily_rows:
                        insert_daily_rows(daily_rows)
                        total_days_processed += 1
                        total_rows_inserted += len(daily_rows)
                        log(f"  ✅ Inserted {len(daily_rows)} rows for {date_str}", "SUCCESS")
                        print()
                        success = True
                        break
                    else:
                        log(f"  No rows extracted for {date_str} (attempt {attempt}/3)", "WARNING")
                        if attempt == 3:
                            failed_dates.append(date_str)
                        else:
                            load_url_with_retry(driver, URL)
                
                except ConnectionError as e:
                    log(f"  Connection error: {e}", "ERROR")
                    if attempt < 3:
                        try:
                            driver = restart_browser(driver)
                        except:
                            pass
                    if attempt == 3:
                        failed_dates.append(date_str)
                
                except Exception as e:
                    log(f"  Error: {e}", "ERROR")
                    if attempt < 3:
                        try:
                            load_url_with_retry(driver, URL)
                        except:
                            driver = restart_browser(driver)
                    if attempt == 3:
                        failed_dates.append(date_str)
        
        # Retry failed dates
        if failed_dates:
            log(f"\n{'='*60}", "STAGE")
            log(f"RETRYING {len(failed_dates)} FAILED DATES", "STAGE")
            log(f"{'='*60}", "STAGE")
            
            try:
                driver = restart_browser(driver)
            except:
                driver = build_driver()
                load_url_with_retry(driver, URL)
            
            still_failed = []
            for date_str in failed_dates:
                log(f"Retrying: {date_str}", "STAGE")
                
                # Parse date
                parts = date_str.split("-")
                day = int(parts[0])
                month = list(calendar.month_abbr).index(parts[1])
                year = int(parts[2])
                
                try:
                    set_date(driver, day, month, year)
                    click_view_report(driver)
                    time.sleep(2)
                    
                    daily_rows = extract_all_daily_rows(driver)
                    
                    if daily_rows:
                        insert_daily_rows(daily_rows)
                        total_days_processed += 1
                        total_rows_inserted += len(daily_rows)
                        log(f"  ✅ Retry successful: {len(daily_rows)} rows", "SUCCESS")
                    else:
                        still_failed.append(date_str)
                        log(f"  ❌ Retry failed", "ERROR")
                        
                except Exception as e:
                    still_failed.append(date_str)
                    log(f"  ❌ Retry error: {e}", "ERROR")
            
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
        
        # Final report
        total_elapsed = (datetime.now() - start_time_global).total_seconds()
        
        log("\n" + "=" * 60, "STAGE")
        log("SCRAPING COMPLETE", "STAGE")
        log("=" * 60, "STAGE")
        log(f"Total time: {timedelta(seconds=int(total_elapsed))}", "INFO")
        log(f"Dates processed: {total_days_processed}/{total_dates}", "INFO" if total_days_processed == total_dates else "WARNING")
        log(f"Total rows inserted: {total_rows_inserted}", "INFO")
        
        if failed_dates:
            log(f"Failed dates ({len(failed_dates)}):", "ERROR")
            for d in failed_dates:
                log(f"  - {d}", "ERROR")
        else:
            log("ALL DATES SCRAPED SUCCESSFULLY!", "SUCCESS")
        
        # Database stats
        with get_connection() as con:
            count = con.execute("SELECT COUNT(*) FROM fpi_daily_data").fetchone()[0]
            dates = con.execute("SELECT COUNT(DISTINCT reporting_date) FROM fpi_daily_data").fetchone()[0]
            log(f"\nDatabase: {DB_FILE}", "INFO")
            log(f"Total rows: {count}", "INFO")
            log(f"Unique dates: {dates}", "INFO")
        
        print()

if __name__ == "__main__":
    scrape_all_months()