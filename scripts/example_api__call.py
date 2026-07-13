import requests
import datetime
import csv

BASE_URL = "https://api.fac.gov"
API_KEY = "YOUR_API_KEY"

def fetch_audits_last_7_days(output_csv="FAC_last_7_days_audit_reports.csv"):
    # Calculate date range (last 7 days)
    today = datetime.date.today()
    past_7_days = today - datetime.timedelta(days=7)

    start_date = past_7_days.strftime("%Y-%m-%d")  # Format: YYYY-MM-DD
    end_date = today.strftime("%Y-%m-%d")

    print(f"\nSearching audits from {start_date} to {end_date} (last 7 days)...")

    fields = [
        "report_id", "auditee_uei", "audit_year", 
        "auditee_name", "auditee_state",
        "submitted_date", "total_amount_expended", 
        "audit_type"
    ]

    url = (f"{BASE_URL}/general" 
        + f"?submitted_date=gte.{start_date}"
        + f"&submitted_date=lte.{end_date}"
        + f"&select={','.join(fields)}"
        )
    headers = {"X-Api-Key": API_KEY}

    try:
        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code == 200:
            audits = response.json()
            
            print(f"\nFound {len(audits)} audits submitted in the last 7 days.\n")
            
            if audits:
                
                with open(output_csv, "w", 
                    newline="", 
                    encoding="utf-8") as csv_file:
                    writer = csv.DictWriter(csv_file, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(audits)

                print(f"Data saved to {output_csv} (CSV).")

            else:
                print("No audits found in the last 7 days.")

            return audits

        else:
            print(f"\nAPI Error {response.status_code}: {response.text}")
            return []
    
    except requests.exceptions.Timeout:
        print("\nThe request timed out. Try again later.")
    except requests.exceptions.RequestException as e:
        print(f"\nAPI request failed: {e}")

fetch_audits_last_7_days()