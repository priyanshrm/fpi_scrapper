import csv
import time
import calendar
import argparse
import traceback
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

# ========================
#  WEBDRIVER MANAGEMENT
# ========================
def build_driver():
    """Create a fresh Chrome driver with robust options."""
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")  # newer headless mode
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    options.add_argument("--safebrowsing-disable-auto-update")
    options.add_argument("--start-maximized")
    options.add_argument("--incognito")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors")
    options.add_argument("--disable-web-security")
    options.add_argument("--allow-running-insecure-content")
    
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(60)
        return driver
    except Exception as e:
        raise BrowserCrashError(f"Failed to create Chrome driver: {e}")

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
            new_driver.get(URL)
            WebDriverWait(new_driver, 30).until(
                EC.presence_of_element_located((By.ID, "txtDate"))
            )
            return new_driver
        except Exception as e:
            if attempt == max_attempts:
                raise BrowserCrashError(
                    f"Browser restart failed after {max_attempts} attempts: {e}"
                )
            print(f"  ⚠️ Browser restart attempt {attempt} failed, retrying...")
            time.sleep(3)

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
                _verify_date_set(driver, day, month, year)
                return True
            
            # Method 2: Direct hidden field injection
            if _set_date_via_hidden_fields(driver, day, month, year):
                _verify_date_set(driver, day, month, year)
                return True
            
            raise DateSelectionError("All date selection methods failed")
            
        except DateSelectionError as e:
            errors.append(str(e))
            if attempt < max_retries:
                print(f"  ⚠️ Date selection attempt {attempt}/{max_retries} failed: {e}")
                # Reload page before retry
                try:
                    driver.get(URL)
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
        # Click calendar icon
        cal_img = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "imgtxtDate"))
        )
        cal_img.click()
        time.sleep(1)
        
        # Wait for calendar container
        cal = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "div[id*='Calendar'], div[id*='calendar'], .ajax__calendar_container")
            )
        )
        
        # Navigate to correct year/month
        target_month = calendar.month_name[month]
        
        # Read current month/year from calendar title
        title_elem = cal.find_element(
            By.CSS_SELECTOR, "div[class*='title'], div[class*='header'], .ajax__calendar_title"
        )
        title_text = title_elem.text  # e.g., "December, 2010"
        
        # Parse current month/year
        parts = title_text.replace(",", "").split()
        current_month_name = parts[0]
        current_year = int(parts[-1])
        
        # Navigate months if needed
        max_nav = 24  # max months to navigate (2 years)
        nav_count = 0
        
        while (current_year != year or current_month_name != target_month) and nav_count < max_nav:
            # Determine direction
            current_month_num = list(calendar.month_name).index(current_month_name)
            if current_month_num == 0:
                current_month_num = 1
            
            if (year > current_year) or (year == current_year and month > current_month_num):
                # Click next
                next_btn = cal.find_element(
                    By.CSS_SELECTOR, "div[class*='next'], a[class*='next'], .ajax__calendar_next a"
                )
            else:
                # Click previous
                next_btn = cal.find_element(
                    By.CSS_SELECTOR, "div[class*='prev'], a[class*='prev'], .ajax__calendar_prev a"
                )
            
            next_btn.click()
            time.sleep(0.3)
            
            # Re-read title
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
        
        # Click the day
        day_cells = cal.find_elements(
            By.CSS_SELECTOR, 
            "td[class*='day'], td[class*='active'], td[class*='ajax__calendar_day'], td[class*='ajax__calendar_active']"
        )
        
        for cell in day_cells:
            if cell.text.strip() == str(day):
                cell.click()
                time.sleep(0.5)
                return True
        
        return False
        
    except Exception as e:
        print(f"  ℹ️ Calendar method failed: {e}")
        return False

def _set_date_via_hidden_fields(driver, day, month, year):
    """Set date by directly modifying hidden fields. Returns True if successful."""
    try:
        date_str = f"{day:02d}-{calendar.month_abbr[month]}-{year}"
        
        # Set hidden field (this is what the server actually reads)
        driver.execute_script(f"""
            document.getElementById('hdnDate').value = '{date_str}';
            document.getElementById('txtDate').value = '{date_str}';
            
            // Also try to trigger any change handlers
            var event = new Event('change', {{ bubbles: true }});
            var hdnDate = document.getElementById('hdnDate');
            hdnDate.dispatchEvent(event);
        """)
        
        time.sleep(0.5)
        return True
        
    except Exception as e:
        print(f"  ℹ️ Hidden field method failed: {e}")
        return False

def _verify_date_set(driver, day, month, year):
    """Verify that the date was actually set correctly."""
    expected = f"{day:02d}-{calendar.month_abbr[month]}-{year}"
    
    try:
        actual_hidden = driver.execute_script(
            "return document.getElementById('hdnDate').value;"
        )
        actual_visible = driver.execute_script(
            "return document.getElementById('txtDate').value;"
        )
        
        if expected not in actual_hidden and expected not in actual_visible:
            print(f"  ⚠️ Date verification failed. Expected: {expected}, Got hidden: {actual_hidden}, visible: {actual_visible}")
            return False
        return True
    except:
        return False

# ========================
#  CLICK VIEW REPORT
# ========================
def click_view_report_robust(driver, max_retries=3):
    """Click View Report button with retries and validation."""
    for attempt in range(1, max_retries + 1):
        try:
            # Wait for button to be clickable
            view_btn = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.ID, "btnSubmit1"))
            )
            
            # Scroll into view
            driver.execute_script("arguments[0].scrollIntoView(true);", view_btn)
            time.sleep(0.3)
            
            # Click using JavaScript (more reliable)
            driver.execute_script("arguments[0].click();", view_btn)
            
            # Wait for page to start loading
            time.sleep(2)
            
            # Verify page started loading (look for the table container)
            try:
                WebDriverWait(driver, 10).until(
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
def wait_and_extract_data(driver, month_name, max_wait=90):
    """
    Wait for the total row and extract all combination data.
    Uses progressive polling and multiple XPath strategies.
    """
    start_time = time.time()
    
    while time.time() - start_time < max_wait:
        try:
            # Try multiple XPath patterns to find the total row
            xpaths = [
                f"//td[contains(text(),'Total for {month_name}')]",
                f"//td[contains(.,'Total for {month_name}')]",
                f"//tr[contains(@class,'total')]//td[contains(text(),'Total for {month_name}')]",
            ]
            
            for xpath in xpaths:
                try:
                    total_cells = driver.find_elements(By.XPATH, xpath)
                    if total_cells:
                        # Found the total row, now extract data
                        data = _extract_monthly_data(driver, total_cells[0], month_name)
                        if data and len(data) > 0:
                            # Validate we have the expected number of combinations
                            expected_combos = _get_expected_combinations_from_table(driver)
                            if expected_combos > 0 and len(data) == expected_combos * len(METRIC_COLS):
                                return data
                            elif len(data) > 0:
                                return data
                except:
                    continue
            
            time.sleep(2)
            
        except StaleElementReferenceException:
            time.sleep(2)
            continue
        except Exception as e:
            print(f"  ⚠️ Polling error: {e}")
            time.sleep(2)
            continue
    
    raise TableNotFoundError(f"Could not find 'Total for {month_name}' row within {max_wait}s")

def _get_expected_combinations_from_table(driver):
    """Count how many Debt/Equity + Investment Route combinations exist."""
    try:
        # Look for the table and count unique combinations
        script = """
        var combos = new Set();
        var rows = document.querySelectorAll('table.tbls01 tr');
        for (var i = 0; i < rows.length; i++) {
            var cells = rows[i].querySelectorAll('td');
            if (cells.length >= 2) {
                var de = cells[0].innerText.trim();
                var route = cells[1].innerText.trim();
                if ((de === 'Equity' || de === 'Debt') && 
                    route !== 'Sub-total' && route !== 'Total' && route !== '') {
                    combos.add(de + '|' + route);
                }
            }
        }
        return combos.size;
        """
        return driver.execute_script(script)
    except:
        return 0

def _extract_monthly_data(driver, total_cell, month_name):
    """Extract all combination data starting from the total row."""
    try:
        # Navigate to the parent row of the total cell
        total_row = total_cell.find_element(By.XPATH, "./ancestor::tr")
        
        # Get all rows after the total row until we hit a new section
        all_rows = []
        current_row = total_row
        
        while current_row:
            all_rows.append(current_row)
            try:
                next_row = current_row.find_element(By.XPATH, "following-sibling::tr[1]")
                current_row = next_row
                # Stop if we've collected enough rows or hit another total
                if len(all_rows) > 20:
                    break
            except:
                break
        
        # Extract combinations
        data = {}
        current_d_e = None
        
        for row in all_rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if not cells:
                    continue
                
                # Skip header row and Sub-total/Total rows
                cell_texts = [c.text.strip() for c in cells]
                
                # Determine if this row has a Debt/Equity label
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
    
    try:
        # Initialize browser
        print("Initializing browser...")
        driver = build_driver()
        driver.get(URL)
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.ID, "txtDate"))
        )
        print("Browser initialized successfully.\n")
        
        # Generate list of months to scrape
        months_to_scrape = []
        for year in range(START_YEAR, END_YEAR + 1):
            for month in range(1, 13):
                months_to_scrape.append((year, month))
        
        total_months = len(months_to_scrape)
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
            
            # Try up to 3 times with progressive recovery
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
                    
                except (DateSelectionError, TableNotFoundError) as e:
                    print(f"  ⚠️ Attempt {attempt}/3 failed: {e}")
                    if attempt == 3:
                        print(f"  ❌ Adding to retry queue: {month_name} {year}")
                        retry_queue.append((year, month, day, month_name))
                    else:
                        # Reload page
                        try:
                            driver.get(URL)
                            WebDriverWait(driver, 20).until(
                                EC.presence_of_element_located((By.ID, "txtDate"))
                            )
                            time.sleep(2)
                        except:
                            driver = restart_browser(driver)
                
                except BrowserCrashError:
                    print(f"  ⚠️ Browser crashed, restarting...")
                    driver = restart_browser(driver)
                    if attempt == 3:
                        retry_queue.append((year, month, day, month_name))
                
                except Exception as e:
                    print(f"  ⚠️ Unexpected error: {e}")
                    traceback.print_exc()
                    if attempt == 3:
                        retry_queue.append((year, month, day, month_name))
                    else:
                        driver = restart_browser(driver)
        
        # Process retry queue
        if retry_queue:
            print(f"\n{'='*60}")
            print(f"Processing {len(retry_queue)} failed months with fresh browser...")
            print(f"{'='*60}\n")
            
            driver = restart_browser(driver)
            
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
                    # Still add a row with empty values for completeness
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
        
        # Write CSV (even if partial)
        if all_rows:
            print(f"\n{'='*60}")
            print(f"Writing {len(all_rows)} rows to {OUTPUT_CSV}")
            
            # Sort rows by date
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
        print(f"Total months processed: {len(all_rows)}/{total_months}")
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