import csv
import time
import calendar
from datetime import datetime, timedelta

from selenium import webdriver

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service

# ========================
#  CONFIGURATION
# ========================
URL = "https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx"
OUTPUT_CSV = "fpi_monthly_totals.csv"

START_YEAR = 2012
END_YEAR = 2025
# Set to True to get full‑month aggregates (use last day of month)
USE_MONTH_LAST_DAY = False   # Change to True for full‑month totals

# Main metric columns (excluding the derivative table)
METRIC_COLS = [
    "Gross Purchases(Rs Crore)",
    "Gross Sales(Rs Crore)",
    "Net Investment (Rs Crore)",
    "Net Investment US($) million",
]

# ========================
#  WEBDRIVER SETUP
# ========================
def get_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--start-maximized")
    options.add_argument("--incognito")
    # Optional: ignore certificate errors if needed
    options.add_argument("--ignore-certificate-errors")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    return driver

# ========================
#  HELPER TO SET DATE AND CLICK 'VIEW REPORT'
# ========================
def set_date_and_view(driver, date_str):
    """
    date_str: 'DD-Mon-YYYY' e.g. '01-Jan-2012'
    Sets the hidden field, updates the visible field, and clicks the View Report link.
    """
    # 1. Set the hidden field that actually holds the date
    driver.execute_script(f"document.getElementById('hdnDate').value = '{date_str}';")
    # 2. Update the visible (disabled) textbox (optional, but good for visual feedback)
    driver.execute_script(f"document.getElementById('txtDate').value = '{date_str}';")
    # 3. Click the 'View Report' link
    view_btn = driver.find_element(By.ID, "btnSubmit1")
    view_btn.click()

# ========================
#  WAIT FOR THE TOTAL ROW TO APPEAR
# ========================
def wait_for_total_row(driver, month_name, timeout=30):
    """
    month_name: full month name, e.g. 'December'
    Returns True if the row is found.
    """
    wait = WebDriverWait(driver, timeout)
    try:
        # The total row contains <td rowspan="...">Total for December</td>
        wait.until(EC.presence_of_element_located(
            (By.XPATH, f"//td[contains(text(),'Total for {month_name}')]")
        ))
        return True
    except Exception:
        return False

# ========================
#  PARSE THE TOTAL BLOCK AND RETURN COMBINATIONS
# ========================
def parse_total_block(driver, month_name):
    """
    Locates the 'Total for <month>' row and extracts the 7‑row block.
    Returns a dict like:
    {
        "Equity_Stock Exchange_Gross Purchases(Rs Crore)": "123.45",
        "Equity_Stock Exchange_Gross Sales(Rs Crore)": "678.90",
        ...
    }
    """
    # Find the total row
    total_row = driver.find_element(By.XPATH, f"//td[contains(text(),'Total for {month_name}')]/parent::tr")
    # The block consists of the total_row + the next 6 <tr> elements
    # We collect all rows until we hit a row that contains <td class="total">Total</td>
    rows = []
    next_row = total_row
    while True:
        rows.append(next_row)
        next_row = next_row.find_element(By.XPATH, "following-sibling::tr[1]")
        # Stop when we reach the row with class 'total' (the final Total)
        if "total" in next_row.get_attribute("class").split():
            break
    # Now rows[0] is the header, rows[1..6] are the data rows
    # We'll parse each row.
    current_d_e = None
    combinations = {}

    for row in rows[1:]:  # skip header
        cells = row.find_elements(By.TAG_NAME, "td")
        # Row structure depends on rowspan. We detect Debt/Equity if present
        if len(cells) >= 2 and cells[0].text.strip() in ("Equity", "Debt"):
            current_d_e = cells[0].text.strip()
            inv_route = cells[1].text.strip()
            metrics = [cells[2].text.strip(), cells[3].text.strip(),
                       cells[4].text.strip(), cells[5].text.strip()]
        elif len(cells) >= 1:
            # No debt/equity cell, so inherit current_d_e
            inv_route = cells[0].text.strip()
            metrics = [cells[1].text.strip(), cells[2].text.strip(),
                       cells[3].text.strip(), cells[4].text.strip()]
        else:
            continue

        # Skip "Sub-total" and "Total" rows
        if inv_route in ("Sub-total", "Total") or not inv_route:
            continue

        # Build column names
        for metric_name, val in zip(METRIC_COLS, metrics):
            col = f"{current_d_e}_{inv_route}_{metric_name}"
            # Replace any problematic characters for CSV headers (not really needed, but safe)
            col = col.replace("/", "_").replace("&", "and")
            combinations[col] = val

    return combinations

# ========================
#  MAIN SCRAPING LOOP
# ========================
def main():
    driver = get_driver()
    driver.get(URL)
    # Wait for the page to fully load
    WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "txtDate")))

    all_rows = []
    fieldnames = ["Reporting Date"]  # Will be extended dynamically

    for year in range(START_YEAR, END_YEAR + 1):
        for month in range(1, 13):
            month_name = calendar.month_name[month]
            if USE_MONTH_LAST_DAY:
                # Use last calendar day of the month
                last_day = calendar.monthrange(year, month)[1]
                day = last_day
            else:
                day = 1

            date_str = f"{day:02d}-{month_name[:3]}-{year}"  # e.g. '01-Jan-2012'
            print(f"Processing: {month_name} {year} → {date_str}")

            # Set date and click View Report
            set_date_and_view(driver, date_str)

            # Wait for the total row
            if not wait_for_total_row(driver, month_name, timeout=40):
                print(f"  ⚠️  'Total for {month_name}' not found. Skipping.")
                continue

            # Small extra wait for any dynamic content
            time.sleep(2)

            # Parse the block
            combos = parse_total_block(driver, month_name)
            row_data = {"Reporting Date": f"{year}-{month:02d}-{day:02d}"}
            row_data.update(combos)
            all_rows.append(row_data)

            # Update fieldnames list (preserve order)
            for col in combos.keys():
                if col not in fieldnames:
                    fieldnames.append(col)

            print(f"  ✅ Extracted {len(combos)} values")

    driver.quit()

    # Write CSV
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nDone! Data saved to {OUTPUT_CSV}")

if __name__ == "__main__":
    main()