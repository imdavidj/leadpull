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
import logging

log = logging.getLogger(__name__)
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
# Florida county data sources.
# status: "live" = verified working | "beta" = needs JS or verification | "soon" = not started
#
# FL data strategy:
#   Most county Tax Collector homepages are JS-rendered and won't parse with requests alone.
#   We prioritize: (1) direct Excel/CSV download URLs, (2) static HTML tables,
#   (3) county Clerk tax deed sale lists (more stable, uses standard court software).

FL_COUNTY_SOURCES = {
    # ── Verified / mostly working ────────────────────────────────────────────────
    "Lee": {
        "note": "Lee County Tax Collector — Tax Certificates page (Fort Myers)",
        "urls": [
            "https://leetc.com/tax-certificates/",
            "https://www.leetc.com/delinquent-taxes/",
        ],
        "type": "html_table",
        "status": "beta",
    },
    "Volusia": {
        "note": "Volusia County Tax Collector — Tax Certificate info (Daytona Beach)",
        "urls": [
            "https://www.vctaxcollector.org/taxes/tax-certificate-info.html",
            "https://vctaxcollector.org/taxes/delinquent/",
        ],
        "type": "html_table",
        "status": "beta",
    },
    "Charlotte": {
        "note": "Charlotte County Tax Collector — Delinquent Taxes",
        "urls": [
            "https://taxcollector.charlottecountyfl.gov/delinquent-tax",
        ],
        "type": "html_table",
        "status": "beta",
    },
    "Sarasota": {
        "note": "Sarasota County Tax Collector — Delinquent Taxes",
        "urls": [
            "https://www.sarasotataxcollector.gov/services/tax-services/property-tax/delinquent-taxes",
            "https://www.sarasotataxcollector.com/services/tax-services/property-tax/delinquent-taxes",
        ],
        "type": "html_table",
        "status": "beta",
    },
    "Collier": {
        "note": "Collier County Tax Collector (Naples / Marco Island)",
        "urls": [
            "https://www.colliertaxcollector.com/taxes/delinquent-real-estate/",
            "https://www.colliertaxcollector.com/",
        ],
        "type": "html_table",
        "status": "beta",
    },

    # ── Major markets — use Playwright (headless browser) ───────────────────────
    "Broward": {
        "note": "Broward County — Lands Available for Taxes (LATF)",
        "urls": [
            "https://revenue.broward.org/taxes/taxsales/Documents/LandsAvailable.xlsx",
            "https://www.broward.org/RecordsTaxesTreasury/TaxesFees/Pages/LandsAvailableforTaxes.aspx",
        ],
        "type": "excel_then_html",
        "status": "beta",
    },
    "Miami-Dade": {
        "note": "Miami-Dade Tax Collector — Lands Available for Taxes",
        "urls": [
            "https://www.miamidade.gov/taxcollector/library/2025-list-of-lands-available.asp",
            "https://www.miamidade.gov/taxcollector/library/2024-list-of-lands-available.asp",
        ],
        "type": "html_table",
        "status": "beta",
    },
    "Palm Beach": {
        "note": "Palm Beach County Clerk — Tax Deed Applications",
        "urls": [
            "https://www.mypalmbeachclerk.com/departments/courts/tax-deeds",
            "https://www.pbctax.com/tax-certificate-sales/lands-available/",
        ],
        "type": "html_table",
        "status": "beta",
    },
    "Hillsborough": {
        "note": "Hillsborough County Clerk — Tax Deeds & Lands Available (Tampa)",
        "urls": [
            "https://www.hillsclerk.com/public-records/tax-deeds-lands-available-for-taxes/",
            "https://www.hillstax.org/taxes/delinquent-taxes/",
        ],
        "type": "html_table",
        "status": "beta",
    },
    "Orange": {
        "note": "Orange County Comptroller — Tax Deed Sales (Orlando)",
        "urls": [
            "https://www.occompt.com/191/Tax-Deed-Sales",
            "https://myorangeclerk.com/divisions/tax-deeds",
        ],
        "type": "html_table",
        "status": "beta",
    },
    "Pinellas": {
        "note": "Pinellas County Clerk — Tax Deeds (St. Pete / Clearwater)",
        "urls": [
            "https://www.pinellasclerk.org/tax-deeds/",
            "https://www.taxcollect.com/taxes/delinquent-taxes/",
        ],
        "type": "html_table",
        "status": "beta",
    },
    "Duval": {
        "note": "Duval County Clerk — Tax Deed Portal (Jacksonville)",
        "urls": [
            "https://taxdeed.duvalclerk.com/",
            "https://fl-duval-taxcollector.publicaccessnow.com/",
        ],
        "type": "html_table",
        "status": "beta",
    },
    "Polk": {
        "note": "Polk County Clerk — Tax Deeds (Lakeland / Winter Haven)",
        "urls": [
            "https://www.polkcountyclerk.net/tax-deeds/",
            "https://polktaxes.com/delinquent-taxes/",
        ],
        "type": "html_table",
        "status": "beta",
    },
}


def _scrape_florida(job_id, county):
    source = FL_COUNTY_SOURCES.get(county)
    if not source:
        raise ValueError(f"Florida/{county} is not yet configured.")

    _upd(job_id, progress=5, message=f"Connecting to {county} County, FL...")
    leads = None

    # Step 1: Try direct file download (Excel/CSV) — fastest
    if source["type"] in ("excel", "excel_then_html"):
        leads = _fl_try_excel(job_id, county, source)

    # Step 2: Try plain HTML table parsing — works if page is server-rendered
    if leads is None:
        leads = _fl_try_html(job_id, county, source)

    # Step 3: Playwright headless browser — handles JS-rendered pages
    if leads is None:
        leads = _fl_playwright(job_id, county, source)

    if not leads:
        _fl_raise_helpful(county, source)

    LEADS[job_id] = leads
    _upd(job_id, progress=95, total=len(leads))


def _fl_try_excel(job_id, county, source):
    """Try downloading an Excel file. Returns list or None."""
    for url in source["urls"]:
        try:
            ct = url.split(".")[-1].lower()
            if ct not in ("xlsx", "xls", "csv"):
                continue
            _upd(job_id, progress=25, message=f"Downloading {county} data file...")
            resp = requests.get(url, headers=HEADERS, timeout=40)
            if resp.status_code != 200:
                continue
            if ct == "csv":
                df = pd.read_csv(io.StringIO(resp.text), low_memory=False)
            else:
                df = pd.read_excel(io.BytesIO(resp.content), header=0)
            if len(df) > 0:
                _upd(job_id, progress=70, message=f"Parsing {len(df):,} records...")
                return _fl_normalize_df(df, county)
        except Exception:
            continue
    return None


def _fl_try_html(job_id, county, source):
    """Try scraping an HTML table. Returns list or None."""
    for url in source["urls"]:
        try:
            _upd(job_id, progress=30, message=f"Fetching {county} data page...")
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code != 200:
                continue
            if len(resp.content) < 1000:
                continue  # Likely a redirect or empty page

            _upd(job_id, progress=60, message="Scanning for data tables...")
            tables = pd.read_html(io.StringIO(resp.text))
            if not tables:
                continue

            # Pick the largest table that looks like property data (has 3+ columns)
            candidates = [t for t in tables if len(t.columns) >= 3 and len(t) > 0]
            if not candidates:
                continue

            df = max(candidates, key=len)
            _upd(job_id, progress=75, message=f"Parsing {len(df):,} records...")
            return _fl_normalize_df(df, county)
        except Exception:
            continue
    return None


def _fl_playwright(job_id, county, source):
    """
    Headless Chromium via Playwright — handles JS-rendered FL county sites.
    This is the fallback when simple HTTP requests return empty pages.
    Returns list of lead dicts, or None if it still fails.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        log.warning("Playwright not installed — skipping JS rendering")
        return None

    # Try each URL in order
    for url in source["urls"]:
        try:
            _upd(job_id, progress=30,
                 message=f"Loading {county} County (JavaScript mode)...")

            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-extensions",
                        "--no-first-run",
                        "--window-size=1280,900",
                    ],
                )
                ctx  = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 900},
                )
                page = ctx.new_page()

                _upd(job_id, progress=40,
                     message=f"Navigating to {county} data page...")
                try:
                    page.goto(url, wait_until="networkidle", timeout=30_000)
                except PwTimeout:
                    page.goto(url, wait_until="domcontentloaded", timeout=20_000)

                # Give JS time to finish rendering
                _upd(job_id, progress=55, message="Waiting for data to render...")
                page.wait_for_timeout(3_000)

                # Scroll down to trigger any lazy-loaded content
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1_000)

                content = page.content()
                browser.close()

            if len(content) < 2_000:
                continue  # Got basically nothing

            _upd(job_id, progress=70, message="Parsing rendered page...")
            tables = pd.read_html(io.StringIO(content))
            candidates = [t for t in tables if len(t.columns) >= 3 and len(t) > 2]
            if not candidates:
                continue

            df = max(candidates, key=len)
            leads = _fl_normalize_df(df, county)
            if leads:
                log.info(f"Playwright pulled {len(leads):,} records from {county}")
                return leads

        except Exception as e:
            log.debug(f"Playwright failed for {county} / {url}: {e}")
            continue

    return None


def _fl_raise_helpful(county, source):
    """Raise a user-friendly error explaining what happened and what to do."""
    raise ValueError(
        f"{county} County, FL — could not retrieve data automatically. "
        f"This county's portal didn't return parseable data even with "
        f"JavaScript rendering. Visit {source['urls'][0]} in your browser, "
        f"export the table to CSV, then use 'Import CSV' (coming soon). "
        f"Source: {source['note']}"
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

# status: "live" = verified working | "beta" = may work, being verified | "soon" = not yet built
COUNTY_CONFIG = {
    "TN": {
        "name": "Tennessee",
        "counties": [
            {"value": "Shelby",   "label": "Shelby County (Memphis)",     "live": True,  "status": "live"},
            {"value": "Davidson", "label": "Davidson County (Nashville)",  "live": False, "status": "soon"},
            {"value": "Knox",     "label": "Knox County (Knoxville)",      "live": False, "status": "soon"},
            {"value": "Hamilton", "label": "Hamilton County (Chattanooga)","live": False, "status": "soon"},
        ],
    },
    "FL": {
        "name": "Florida",
        "counties": [
            {"value": "Charlotte",   "label": "Charlotte County (Punta Gorda)",  "live": True,  "status": "beta"},
            {"value": "Collier",     "label": "Collier County (Naples)",          "live": True,  "status": "beta"},
            {"value": "Sarasota",    "label": "Sarasota County",                  "live": True,  "status": "beta"},
            {"value": "Lee",         "label": "Lee County (Fort Myers)",          "live": True,  "status": "beta"},
            {"value": "Volusia",     "label": "Volusia County (Daytona Beach)",   "live": True,  "status": "beta"},
            {"value": "Broward",     "label": "Broward County (Ft. Lauderdale)", "live": True,  "status": "beta"},
            {"value": "Miami-Dade",  "label": "Miami-Dade County",               "live": True,  "status": "beta"},
            {"value": "Palm Beach",  "label": "Palm Beach County",               "live": True,  "status": "beta"},
            {"value": "Hillsborough","label": "Hillsborough County (Tampa)",     "live": True,  "status": "beta"},
            {"value": "Orange",      "label": "Orange County (Orlando)",         "live": True,  "status": "beta"},
            {"value": "Pinellas",    "label": "Pinellas County (St. Pete)",      "live": True,  "status": "beta"},
            {"value": "Duval",       "label": "Duval County (Jacksonville)",     "live": True,  "status": "beta"},
            {"value": "Polk",        "label": "Polk County (Lakeland)",          "live": True,  "status": "beta"},
        ],
    },
    "TX": {
        "name": "Texas",
        "counties": [
            {"value": "Harris",  "label": "Harris County (Houston)",   "live": False, "status": "soon"},
            {"value": "Dallas",  "label": "Dallas County",              "live": False, "status": "soon"},
            {"value": "Tarrant", "label": "Tarrant County (Ft. Worth)", "live": False, "status": "soon"},
            {"value": "Bexar",   "label": "Bexar County (San Antonio)", "live": False, "status": "soon"},
            {"value": "Travis",  "label": "Travis County (Austin)",     "live": False, "status": "soon"},
        ],
    },
    "GA": {
        "name": "Georgia",
        "counties": [
            {"value": "Fulton",   "label": "Fulton County (Atlanta)",  "live": False, "status": "soon"},
            {"value": "Gwinnett", "label": "Gwinnett County",          "live": False, "status": "soon"},
            {"value": "Cobb",     "label": "Cobb County",              "live": False, "status": "soon"},
            {"value": "DeKalb",   "label": "DeKalb County",            "live": False, "status": "soon"},
        ],
    },
    "OH": {
        "name": "Ohio",
        "counties": [
            {"value": "Franklin",  "label": "Franklin County (Columbus)",  "live": False, "status": "soon"},
            {"value": "Cuyahoga",  "label": "Cuyahoga County (Cleveland)", "live": False, "status": "soon"},
            {"value": "Hamilton",  "label": "Hamilton County (Cincinnati)", "live": False, "status": "soon"},
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
