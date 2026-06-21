import csv
import time
import calendar
import argparse
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
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
#  WEBDRIVER
# ========================
def get_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--start-maximized")
    options.add_argument("--incognito")
    options.add_argument("--ignore-certificate-errors")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    return driver

# ========================
#  ROBUST DATE SELECTION VIA CALENDAR WIDGET
# ========================
def set_date_calendar(driver, day, month, year):
    """
    Clicks the calendar image, waits for the popup, navigates to
    year/month, and clicks the desired day.
    Falls back to direct hidden‑field injection if the calendar popup
    does not appear after a few seconds.
    """
    # 1. Click the calendar icon
    wait = WebDriverWait(driver, 10)
    try:
        cal_img = driver.find_element(By.ID, "imgtxtDate")
        cal_img.click()
    except Exception:
        print("  ⚠️ Calendar image not found, falling back to hidden‑field method.")
        set_date_hidden(driver, day, month, year)
        return

    # 2. Wait for the calendar popup (typical ASP.NET AjaxControlToolkit calendar)
    try:
        # The calendar is usually a div with class starting with 'ajax__calendar'
        cal_container = WebDriverWait(driver, 6).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, "div.ajax__calendar_container"))
        )
    except Exception:
        print("  ⚠️ Calendar popup didn't appear, using hidden‑field fallback.")
        set_date_hidden(driver, day, month, year)
        return

    # 3. Navigate year/month if needed
    # The calendar shows month name and year in a <span class="ajax__calendar_month"> / year
    try:
        # Get current displayed month/year
        month_year_text = cal_container.find_element(By.CSS_SELECTOR, "div.ajax__calendar_title").text
        # Example format: "December, 2010"
        parts = month_year_text.split(",")
        current_month = parts[0].strip()
        current_year = int(parts[1].strip())
    except:
        current_month = "Unknown"
        current_year = 0

    target_month_name = calendar.month_name[month]

    # Navigate to the correct month/year (simple prev/next buttons)
    while True:
        if current_year == year and current_month == target_month_name:
            break
        # Click next month if target is in the future, else previous
        if (year > current_year) or (year == current_year and month > calendar.month_name.index(current_month)):
            # Next month button: class "ajax__calendar_next"
            next_btn = cal_container.find_element(By.CSS_SELECTOR, "div.ajax__calendar_next a")
            next_btn.click()
        else:
            prev_btn = cal_container.find_element(By.CSS_SELECTOR, "div.ajax__calendar_prev a")
            prev_btn.click()
        time.sleep(0.3)
        # Re‑read current month/year
        try:
            month_year_text = cal_container.find_element(By.CSS_SELECTOR, "div.ajax__calendar_title").text
            parts = month_year_text.split(",")
            current_month = parts[0].strip()
            current_year = int(parts[1].strip())
        except:
            print("  ⚠️ Could not read calendar title after navigation, falling back.")
            set_date_hidden(driver, day, month, year)
            return

    # 4. Click the day cell
    # Day cells are <td class="ajax__calendar_day"> or <td class="ajax__calendar_active"> etc.
    # Find all clickable day numbers
    day_cells = cal_container.find_elements(By.CSS_SELECTOR, "td.ajax__calendar_day, td.ajax__calendar_active")
    for cell in day_cells:
        if cell.text.strip() == str(day):
            cell.click()
            break
    else:
        print(f"  ⚠️ Day {day} not clickable in calendar, using hidden‑field method.")
        set_date_hidden(driver, day, month, year)
        return

    # Wait a moment for the textbox to update
    time.sleep(0.5)

def set_date_hidden(driver, day, month, year):
    """
    Directly sets the hidden fields (used as fallback).
    """
    date_str = f"{day:02d}-{calendar.month_abbr[month]}-{year}"
    driver.execute_script(f"document.getElementById('hdnDate').value = '{date_str}';")
    driver.execute_script(f"document.getElementById('txtDate').value = '{date_str}';")
    print(f"  ℹ️  Date set via hidden field: {date_str}")

# ========================
#  CLICK VIEW REPORT BUTTON
# ========================
def click_view_report(driver):
    wait = WebDriverWait(driver, 10)
    view_btn = wait.until(EC.element_to_be_clickable((By.ID, "btnSubmit1")))
    view_btn.click()
    # Small wait for postback to start
    time.sleep(1)

# ========================
#  WAIT FOR TOTAL ROW
# ========================
def wait_for_total_row(driver, month_name, timeout=60):
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(
                (By.XPATH, f"//td[contains(text(),'Total for {month_name}')]")
            )
        )
        return True
    except Exception:
        return False

# ========================
#  PARSE THE MONTHLY TOTAL BLOCK
# ========================
def parse_total_block(driver, month_name):
    """
    Finds the 'Total for <month>' row and collects all the
    Debt/Equity – Investment Route combinations and their metrics.
    Skips Sub‑total and Total rows.
    """
    total_row = driver.find_element(
        By.XPATH, f"//td[contains(text(),'Total for {month_name}')]/parent::tr"
    )
    rows = []
    next_row = total_row
    while True:
        rows.append(next_row)
        next_row = next_row.find_element(By.XPATH, "following-sibling::tr[1]")
        if "total" in next_row.get_attribute("class").split():
            rows.append(next_row)  # include the final Total row (we'll skip it later)
            break

    current_d_e = None
    combinations = {}

    # Skip the header row (index 0)
    for row in rows[1:]:
        cells = row.find_elements(By.TAG_NAME, "td")
        if not cells:
            continue

        # Determine if this row contains a new Debt/Equity label
        if len(cells) >= 2 and cells[0].text.strip() in ("Equity", "Debt"):
            current_d_e = cells[0].text.strip()
            inv_route = cells[1].text.strip()
            # Metrics: cells[2] to cells[5] are GP, GS, NI, NI US$
            if len(cells) >= 6:
                metrics = [cells[2].text.strip(), cells[3].text.strip(),
                           cells[4].text.strip(), cells[5].text.strip()]
            else:
                continue
        elif len(cells) >= 1:
            # Inherit Debt/Equity from previous row
            if current_d_e is None:
                continue
            inv_route = cells[0].text.strip()
            if len(cells) >= 5:
                metrics = [cells[1].text.strip(), cells[2].text.strip(),
                           cells[3].text.strip(), cells[4].text.strip()]
            else:
                continue
        else:
            continue

        # Ignore Sub‑total and Total
        if inv_route in ("Sub-total", "Total") or not inv_route:
            continue

        # Build column name for each metric
        for metric_name, val in zip(METRIC_COLS, metrics):
            col = f"{current_d_e}_{inv_route}_{metric_name}".replace("/", "_").replace("&", "and")
            combinations[col] = val

    return combinations

# ========================
#  MAIN SCRAPE LOOP
# ========================
def main():
    driver = get_driver()
    driver.get(URL)
    # Ensure page is loaded
    WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "txtDate")))

    all_rows = []
    fieldnames = ["Reporting Date"]

    for year in range(START_YEAR, END_YEAR + 1):
        for month in range(1, 13):
            month_name = calendar.month_name[month]
            if USE_MONTH_LAST_DAY:
                last_day = calendar.monthrange(year, month)[1]
                day = last_day
            else:
                day = 1

            print(f"Processing: {month_name} {year} → day {day}")

            # Set date using the calendar widget (or hidden fallback)
            set_date_calendar(driver, day, month, year)

            # Click the View Report button
            click_view_report(driver)

            # Wait for the total row to appear
            if not wait_for_total_row(driver, month_name, timeout=40):
                print(f"  ⚠️ 'Total for {month_name}' not found after waiting. Skipping.")
                # Try to reload the page before next month to avoid stale state
                driver.get(URL)
                time.sleep(2)
                continue

            # Extra stabilisation
            time.sleep(2)

            # Extract the combinations
            combos = parse_total_block(driver, month_name)
            row_data = {"Reporting Date": f"{year}-{month:02d}-{day:02d}"}
            row_data.update(combos)
            all_rows.append(row_data)

            # Update field list
            for col in combos.keys():
                if col not in fieldnames:
                    fieldnames.append(col)

            print(f"  ✅ Extracted {len(combos)} value cells")

    driver.quit()

    # Write CSV
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nDone! Data saved to {OUTPUT_CSV}")

if __name__ == "__main__":
    main()