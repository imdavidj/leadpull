#!/usr/bin/env python3
"""
LeadPull — Property Lead Generation Web Application
====================================================
Run:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:5000
"""

import io
import csv
import math
import uuid
import time
import threading
import requests
import pandas as pd
from datetime import datetime
from flask import Flask, jsonify, request, render_template, Response

app = Flask(__name__)

# ─── In-memory stores (fine for single-user local app) ────────────────────────
JOBS  = {}   # job_id → job metadata dict
LEADS = {}   # job_id → list[dict]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

RATE_LIMIT = 1.5  # seconds between requests


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/counties")
def get_counties():
    """Return available counties grouped by state."""
    return jsonify(COUNTY_CONFIG)


@app.route("/api/scrape", methods=["POST"])
def start_scrape():
    data   = request.get_json() or {}
    state  = data.get("state", "").strip()
    county = data.get("county", "").strip()

    if not state or not county:
        return jsonify({"error": "state and county are required"}), 400

    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {
        "id":       job_id,
        "state":    state,
        "county":   county,
        "status":   "running",
        "progress": 3,
        "message":  "Initializing...",
        "started":  datetime.now().isoformat(),
        "total":    0,
    }

    thread = threading.Thread(
        target=_run_scraper, args=(job_id, state, county), daemon=True
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/jobs/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job)


@app.route("/api/leads/<job_id>")
def get_leads(job_id):
    leads = list(LEADS.get(job_id, []))

    # Filtering
    q          = request.args.get("q", "").lower().strip()
    min_amount = request.args.get("min_amount", type=float)
    authority  = request.args.get("authority", "").lower()
    violations = request.args.get("violations")  # "1" = only with violations
    page       = max(1, request.args.get("page", 1, type=int))
    per_page   = min(500, request.args.get("per_page", 100, type=int))

    if q:
        leads = [l for l in leads if
            q in str(l.get("owner_name", "")).lower() or
            q in str(l.get("property_address", "")).lower() or
            q in str(l.get("parcel_id", "")).lower()]

    if min_amount is not None:
        leads = [l for l in leads if _to_float(l.get("tax_amount_owed")) >= min_amount]

    if authority:
        leads = [l for l in leads if authority in str(l.get("taxing_authority", "")).lower()]

    if violations == "1":
        leads = [l for l in leads if _to_float(l.get("code_violation_count")) > 0]

    # Default sort: highest tax owed first
    leads.sort(key=lambda x: _to_float(x.get("tax_amount_owed")), reverse=True)

    total = len(leads)
    start = (page - 1) * per_page
    return jsonify({
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "leads":    leads[start: start + per_page],
    })


@app.route("/api/export/<job_id>")
def export_csv(job_id):
    leads = LEADS.get(job_id, [])
    if not leads:
        return jsonify({"error": "no leads for this job"}), 404

    output = io.StringIO()
    fields = [
        "owner_name", "property_address", "owner_mailing_address",
        "parcel_id", "tax_amount_owed", "tax_year", "taxing_authority",
        "code_violation_count", "code_violations_summary",
        "state", "county", "scraped_date",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(leads)
    output.seek(0)

    job   = JOBS.get(job_id, {})
    fname = f"{job.get('county', 'leads')}_{job.get('state', '')}_leads.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={fname}"},
    )


@app.route("/api/stats/<job_id>")
def job_stats(job_id):
    leads = LEADS.get(job_id, [])
    if not leads:
        return jsonify({})

    amounts = [_to_float(l.get("tax_amount_owed")) for l in leads]
    amounts = [a for a in amounts if a > 0]

    return jsonify({
        "total":          len(leads),
        "with_mailing":   sum(1 for l in leads if l.get("owner_mailing_address")),
        "with_violations":sum(1 for l in leads if _to_float(l.get("code_violation_count")) > 0),
        "avg_tax_owed":   round(sum(amounts) / len(amounts), 2) if amounts else 0,
        "total_tax_owed": round(sum(amounts), 2),
        "high_value":     sum(1 for a in amounts if a >= 5000),
    })


# ══════════════════════════════════════════════════════════════════════════════
# SCRAPER ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _run_scraper(job_id, state, county):
    try:
        if state == "TN":
            _scrape_tennessee(job_id, county)
        elif state == "FL":
            _scrape_florida(job_id, county)
        else:
            _upd(job_id, status="error", message=f"{state} not yet supported")
            return

        n = len(LEADS.get(job_id, []))
        _upd(job_id, status="complete", progress=100,
             message=f"Complete — {n:,} leads collected", total=n)

    except Exception as e:
        _upd(job_id, status="error", message=str(e))


# ─── Tennessee ─────────────────────────────────────────────────────────────────

TN_COUNTY_URLS = {
    # Add more TN county trustee URLs here as you expand
    "Shelby":   "https://www.shelbycountytrustee.com/DocumentCenter/View/1504/ExhibitA",
    "Davidson":  None,   # Nashville — add URL when available
    "Knox":      None,   # Knoxville
    "Hamilton":  None,   # Chattanooga
}

def _scrape_tennessee(job_id, county):
    url = TN_COUNTY_URLS.get(county)
    if not url:
        raise ValueError(
            f"Tennessee/{county} URL not configured yet. "
            "Add the trustee Excel URL to TN_COUNTY_URLS in app.py."
        )

    _upd(job_id, progress=10, message=f"Downloading {county} County delinquent list...")
    resp = requests.get(url, headers=HEADERS, timeout=40)
    resp.raise_for_status()

    _upd(job_id, progress=40, message="Parsing spreadsheet...")
    df = pd.read_excel(io.BytesIO(resp.content), header=0)

    # Rename columns to standard names
    col_map = {
        "Name":              "owner_name",
        "ParcelID":          "parcel_id",
        "Year":              "tax_year",
        "Taxing Autority":   "taxing_authority",
        "Property Location": "property_address",
        "Mailing Address":   "_mail_street",
        "City, St  Zip":     "_mail_city_zip",
        "TaxUnpaid":         "tax_amount_owed",
    }
    df.rename(columns={k: v for k, v in col_map.items() if k in df.columns}, inplace=True)

    # Combine mailing address parts
    mail_street   = df.get("_mail_street",   pd.Series("", index=df.index)).fillna("").astype(str).str.strip()
    mail_city_zip = df.get("_mail_city_zip", pd.Series("", index=df.index)).fillna("").astype(str).str.strip()
    df["owner_mailing_address"] = (mail_street + ", " + mail_city_zip).str.strip(", ")

    df["state"]                  = "TN"
    df["county"]                 = county
    df["scraped_date"]            = datetime.now().strftime("%Y-%m-%d")
    df["code_violation_count"]    = 0
    df["code_violations_summary"] = ""

    _upd(job_id, progress=85, message=f"Processing {len(df):,} records...")
    LEADS[job_id] = _clean_df(df)
    _upd(job_id, progress=95, total=len(LEADS[job_id]))


# ─── Florida ───────────────────────────────────────────────────────────────────
#
# Florida public records strategy:
#   - Each county Tax Collector publishes a "Lands Available for Taxes" (LATF) list
#     = properties that didn't sell at tax deed auction, available for direct purchase
#   - Many FL counties also have tax certificate/deed auction lists
#   - The Tax Collector Association standardized much of this but URLs vary
#
# Verified sources (update if counties change their portals):

FL_COUNTY_SOURCES = {
    "Broward": {
        "note": "Broward County Revenue Collection — Lands Available for Taxes",
        "urls": [
            "https://revenue.broward.org/taxes/taxsales/Documents/LandsAvailable.xlsx",
            "https://revenue.broward.org/taxes/taxsales/Documents/LandsAvailable.xls",
        ],
        "type": "excel",
    },
    "Miami-Dade": {
        "note": "Miami-Dade Tax Collector — LATF list (HTML table)",
        "urls": [
            "https://www.miamidade.gov/taxcollector/library/2024-list-of-lands-available.asp",
        ],
        "type": "html_table",
    },
    "Palm Beach": {
        "note": "Palm Beach County Tax Collector — Lands Available",
        "urls": [
            "https://www.pbctax.com/tax-certificate-sales/lands-available/",
        ],
        "type": "html_table",
    },
    "Hillsborough": {
        "note": "Hillsborough County Tax Collector (Tampa)",
        "urls": [
            "https://www.hillstax.org/",
        ],
        "type": "html_table",
    },
    "Orange": {
        "note": "Orange County Tax Collector (Orlando)",
        "urls": [
            "https://www.octaxcol.com/",
        ],
        "type": "html_table",
    },
    "Pinellas": {
        "note": "Pinellas County Tax Collector (St. Petersburg / Clearwater)",
        "urls": [
            "https://www.taxcollect.com/",
        ],
        "type": "html_table",
    },
    "Duval": {
        "note": "Duval County Tax Collector (Jacksonville)",
        "urls": [
            "https://www.coj.net/departments/finance/treasury-division/tax-certificates.aspx",
        ],
        "type": "html_table",
    },
    "Lee": {
        "note": "Lee County Tax Collector (Fort Myers)",
        "urls": [
            "https://www.leetc.com/",
        ],
        "type": "html_table",
    },
    "Polk": {
        "note": "Polk County Tax Collector (Lakeland / Winter Haven)",
        "urls": [
            "https://polktaxes.com/",
        ],
        "type": "html_table",
    },
    "Volusia": {
        "note": "Volusia County Property Appraiser (Daytona Beach)",
        "urls": [
            "https://vcpa.vcgov.org/",
        ],
        "type": "html_table",
    },
    "Sarasota": {
        "note": "Sarasota County Tax Collector",
        "urls": [
            "https://www.sarasotataxcollector.com/",
        ],
        "type": "html_table",
    },
    "Collier": {
        "note": "Collier County Tax Collector (Naples / Marco Island)",
        "urls": [
            "https://www.colliertaxcollector.com/",
        ],
        "type": "html_table",
    },
}


def _scrape_florida(job_id, county):
    source = FL_COUNTY_SOURCES.get(county)
    if not source:
        raise ValueError(
            f"Florida/{county} is not yet configured. "
            "Add it to FL_COUNTY_SOURCES in app.py."
        )

    _upd(job_id, progress=10, message=f"Fetching {county} County, FL...")

    if source["type"] == "excel":
        leads = _fl_excel(job_id, county, source)
    else:
        leads = _fl_html(job_id, county, source)

    LEADS[job_id] = leads
    _upd(job_id, progress=95, total=len(leads))


def _fl_excel(job_id, county, source):
    """Download and parse an Excel-format FL LATF list."""
    for url in source["urls"]:
        try:
            _upd(job_id, progress=30, message=f"Downloading {county} Excel list...")
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code != 200:
                continue
            df = pd.read_excel(io.BytesIO(resp.content), header=0)
            return _fl_normalize_df(df, county)
        except Exception:
            continue
    raise ValueError(f"Could not download Excel list for {county}. URLs may have changed — check {source['note']}.")


def _fl_html(job_id, county, source):
    """Scrape an HTML table from a FL tax collector site."""
    for url in source["urls"]:
        try:
            _upd(job_id, progress=30, message=f"Fetching {county} page...")
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code != 200:
                continue

            _upd(job_id, progress=60, message="Parsing HTML table...")
            tables = pd.read_html(io.StringIO(resp.text))
            if not tables:
                continue

            # Use the largest table found
            df = max(tables, key=len)
            return _fl_normalize_df(df, county)
        except Exception:
            continue
    raise ValueError(
        f"Could not parse {county} FL page. "
        "The site may require JavaScript — manual URL verification needed. "
        f"See: {source['note']}"
    )


def _fl_normalize_df(df: pd.DataFrame, county: str) -> list:
    """Normalize a Florida dataframe to the standard lead schema."""
    # Lowercase and clean column names
    df.columns = [str(c).strip().lower().replace(" ", "_").replace("/", "_") for c in df.columns]

    # Map common FL column name variants to standard names
    renames = {}
    for col in df.columns:
        if any(x in col for x in ["owner", "name"]) and "owner_name" not in renames.values():
            renames[col] = "owner_name"
        elif any(x in col for x in ["parcel", "folio", "account"]) and "parcel_id" not in renames.values():
            renames[col] = "parcel_id"
        elif any(x in col for x in ["situs", "property_addr", "location", "address"]) and "property_address" not in renames.values():
            renames[col] = "property_address"
        elif any(x in col for x in ["amount", "tax", "balance", "owed"]) and "tax_amount_owed" not in renames.values():
            renames[col] = "tax_amount_owed"
        elif any(x in col for x in ["mail", "mailing"]) and "owner_mailing_address" not in renames.values():
            renames[col] = "owner_mailing_address"
        elif any(x in col for x in ["year"]) and "tax_year" not in renames.values():
            renames[col] = "tax_year"

    df.rename(columns=renames, inplace=True)

    df["state"]                  = "FL"
    df["county"]                 = county
    df["scraped_date"]            = datetime.now().strftime("%Y-%m-%d")
    df["taxing_authority"]        = f"{county} County, FL"
    df["code_violation_count"]    = 0
    df["code_violations_summary"] = ""

    if "owner_mailing_address" not in df.columns:
        df["owner_mailing_address"] = ""

    return _clean_df(df)


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _clean_df(df: pd.DataFrame) -> list:
    """Convert DataFrame to list of dicts with NaN replaced by ''."""
    records = df.to_dict("records")
    out = []
    for r in records:
        out.append({
            k: ("" if isinstance(v, float) and math.isnan(v) else v)
            for k, v in r.items()
        })
    return out


def _upd(job_id, **kw):
    if job_id in JOBS:
        JOBS[job_id].update(kw)


def _to_float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# COUNTY CONFIG  (drives the frontend dropdowns)
# ══════════════════════════════════════════════════════════════════════════════

COUNTY_CONFIG = {
    "TN": {
        "name": "Tennessee",
        "counties": [
            {"value": "Shelby",   "label": "Shelby County (Memphis)",      "live": True},
            {"value": "Davidson", "label": "Davidson County (Nashville)",    "live": False},
            {"value": "Knox",     "label": "Knox County (Knoxville)",        "live": False},
            {"value": "Hamilton", "label": "Hamilton County (Chattanooga)",  "live": False},
        ],
    },
    "FL": {
        "name": "Florida",
        "counties": [
            {"value": "Broward",     "label": "Broward County (Ft. Lauderdale)", "live": True},
            {"value": "Miami-Dade",  "label": "Miami-Dade County",               "live": True},
            {"value": "Palm Beach",  "label": "Palm Beach County",               "live": True},
            {"value": "Hillsborough","label": "Hillsborough County (Tampa)",      "live": True},
            {"value": "Orange",      "label": "Orange County (Orlando)",          "live": True},
            {"value": "Pinellas",    "label": "Pinellas County (St. Pete)",       "live": True},
            {"value": "Duval",       "label": "Duval County (Jacksonville)",      "live": True},
            {"value": "Lee",         "label": "Lee County (Fort Myers)",          "live": True},
            {"value": "Polk",        "label": "Polk County (Lakeland)",           "live": True},
            {"value": "Volusia",     "label": "Volusia County (Daytona Beach)",   "live": True},
            {"value": "Sarasota",    "label": "Sarasota County",                  "live": True},
            {"value": "Collier",     "label": "Collier County (Naples)",          "live": True},
        ],
    },
    "TX": {
        "name": "Texas",
        "counties": [
            {"value": "Harris",  "label": "Harris County (Houston)",   "live": False},
            {"value": "Dallas",  "label": "Dallas County",              "live": False},
            {"value": "Tarrant", "label": "Tarrant County (Ft. Worth)", "live": False},
            {"value": "Bexar",   "label": "Bexar County (San Antonio)", "live": False},
            {"value": "Travis",  "label": "Travis County (Austin)",     "live": False},
        ],
    },
    "GA": {
        "name": "Georgia",
        "counties": [
            {"value": "Fulton",   "label": "Fulton County (Atlanta)",   "live": False},
            {"value": "Gwinnett", "label": "Gwinnett County",           "live": False},
            {"value": "Cobb",     "label": "Cobb County",               "live": False},
            {"value": "DeKalb",   "label": "DeKalb County",             "live": False},
        ],
    },
    "OH": {
        "name": "Ohio",
        "counties": [
            {"value": "Franklin",  "label": "Franklin County (Columbus)",  "live": False},
            {"value": "Cuyahoga",  "label": "Cuyahoga County (Cleveland)", "live": False},
            {"value": "Hamilton",  "label": "Hamilton County (Cincinnati)", "live": False},
        ],
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import os
    os.makedirs("templates", exist_ok=True)
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    print(f"\n  LeadPull running at  →  http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
