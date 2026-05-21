"""
Daily Financial Review — PocketSmith + Gmail SMTP
Designed for GitHub Actions (cron schedule). No local dependencies.

THE FENCE (non-negotiable invariant):
  This script may ONLY read (GET) and categorize transactions (PATCH /transactions).
  It must NEVER delete/create/rename accounts, touch data feeds, or delete rules,
  categories, or transactions. The PocketSmith API has endpoints for those actions;
  this script must never call them. (On 2026-05-21 an automated run deleted an IRA
  account over what was just feed lag — never again.) Account-level issues are
  FLAGGED in the email for Todd, never acted on.

LEARNING ("learn as it goes"):
  Merchant knowledge lives in the RULES list below. When a new payee shows up under
  "Action Required" in the email, add one line to RULES and commit — it is then
  auto-categorized forever, across past and future transactions, on every run.

Required environment variables:
  POCKETSMITH_API_KEY  — from pocketsmith.com/manage#developer
  GMAIL_ADDRESS        — your Gmail address (todd@toddcop.com)
  GMAIL_APP_PASSWORD   — App Password from myaccount.google.com/apppasswords
  REPORT_TO_EMAIL      — recipient address (can be same as GMAIL_ADDRESS)
"""

import os
import re
import sys
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date, timedelta

import requests

# ── Config ──────────────────────────────────────────────────────────────────

USER_ID = 740584
PS_BASE = "https://api.pocketsmith.com/v2"

# Fallback FX (used only if fx-rates.json is missing). The fx-rates.yml workflow
# refreshes fx-rates.json daily; we read the live USD→GBP rate from it at runtime.
FALLBACK_USD_GBP = 0.859
FALLBACK_EUR_GBP = 0.85
FX_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fx-rates.json")

LARGE_TXN_THRESHOLD = 500   # flag transactions over this amount (original currency)
FEED_STALE_DAYS = 3         # flag a live (non-offline) account if it hasn't updated in this many days


def load_fx():
    """Read live USD→GBP from fx-rates.json; fall back to constants. EUR not in file → fallback."""
    usd_gbp, eur_gbp = FALLBACK_USD_GBP, FALLBACK_EUR_GBP
    try:
        with open(FX_FILE) as f:
            data = json.load(f)
        if data.get("USD"):
            usd_gbp = float(data["USD"])
        print(f"  FX: USD->GBP {usd_gbp} (fx-rates.json {data.get('date')})")
    except Exception as e:
        print(f"  FX: fx-rates.json unavailable, using fallback {usd_gbp}: {e}", file=sys.stderr)
    return usd_gbp, eur_gbp


# Known merchants — do NOT flag these as "unusual" large transactions
KNOWN_PAYEES = [
    "MINDY APPEL", "ALLWYN ENT", "T-MOBILE", "TMOBILE", "URSA MINOR",
    "GOFUNDME", "GFM*GOFUNDME", "META PPGF", "MONAGEER", "FLATFAIR",
    "UBS", "DANSKE", "ZELLE", "HYPERION", "CURRENCIES DIRECT", "CIT BANK",
    "SCHWAB", "MOOD",
]

# ── PocketSmith helpers ──────────────────────────────────────────────────────

def ps_headers(key):
    return {"X-Developer-Key": key, "Accept": "application/json"}


def ps_get(key, path, params=None):
    r = requests.get(f"{PS_BASE}{path}", headers=ps_headers(key), params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def ps_patch(key, path, data):
    h = {**ps_headers(key), "Content-Type": "application/json"}
    r = requests.patch(f"{PS_BASE}{path}", headers=h, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_uncategorized(key):
    return ps_get(key, f"/users/{USER_ID}/transactions",
                  {"uncategorised": 1, "per_page": 100})


def fetch_needs_review(key):
    return ps_get(key, f"/users/{USE_ID}/transactions",
                  {"needs_review": 1, "per_page": 100})


def fetch_recent(key, days=2):
    start = (date.today() - timedelta(days=days)).isoformat()
    end = date.today().isoformat()
    return ps_get(key, f"/users/{USER_ID}/transactions",
                  {"start_date": start, "end_date": end, "per_page": 100})


def fetch_budget(key):
    return ps_get(key, f"/users/{USE_ID}/budget", {"roll_up": 1})


def fetch_transaction_accounts(key):
    return ps_get(key, f"/users/{USE_ID}/transaction_accounts")


def update_transaction(key, txn_id, payload):
    return ps_patch(key, f"/transactions/{txn_id}", payload)
