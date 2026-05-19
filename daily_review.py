"""
Daily Financial Review — PocketSmith + Claude + Gmail SMTP
Designed for GitHub Actions (cron schedule). No local dependencies.

Required environment variables:
  POCKETSMITH_API_KEY  — from pocketsmith.com/manage#developer
  ANTHROPIC_API_KEY    — from console.anthropic.com/settings/keys
  GMAIL_ADDRESS        — your Gmail address (todd@toddcop.com)
  GMAIL_APP_PASSWORD   — App Password from myaccount.google.com/apppasswords
  REPORT_TO_EMAIL      — recipient address (can be same as GMAIL_ADDRESS)
"""

import os
import json
import re
import sys
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date, timedelta

import requests
import anthropic

# ── Config ──────────────────────────────────────────────────────────────────

USER_ID = 740584
PS_BASE = "https://api.pocketsmith.com/v2"
GBP_USD = 0.859  # fallback exchange rate; PocketSmith provides per-transaction rates

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
    return ps_get(key, f"/users/{USER_ID}/transactions",
                  {"needs_review": 1, "per_page": 100})


def fetch_recent(key, days=2):
    start = (date.today() - timedelta(days=days)).isoformat()
    return ps_get(key, f"/users/{USER_ID}/transactions",
                  {"start_date": start, "per_page": 100})


def fetch_budget(key):
    return ps_get(key, f"/users/{USER_ID}/budget", {"roll_up": 1})


def update_transaction(key, txn_id, payload):
    return ps_patch(key, f"/transactions/{txn_id}", payload)


# ── Categorization rules ─────────────────────────────────────────────────────
# (payee_substring_uppercase, category_id, category_label, is_transfer)

RULES = [
    # Known merchants
    ("MINDY APPEL",        28939216, "Healthcare",            False),
    ("SQ *URSA MINOR",     28938632, "Cafes And Restaurants", False),
    ("SPICKSPAN",          28938036, "Personal Care",         False),
    ("SP MARLEYBONES",     28939472, "Pet Food And Supplies", False),
    # Butchers
    ("BUSHMILLS MEAT",     28937964, "Groceries",             False),
    ("MOUNTSANDEL MEAT",   28937964, "Groceries",             False),
    ("DONNELLY BUTCHER",   28937964, "Groceries",             False),
    # Bookshops
    ("WATERSTONES",        28938644, "Books And Supplies",    False),
    ("KEATS AND CHAPMAN",  28938644, "Books And Supplies",    False),
    # Hotels
    ("MALDRON",            28938628, "Hotel",                 False),
    ("CLAYTON",            28938628, "Hotel",                 False),
    ("FITZWILLIAM",        28938628, "Hotel",                 False),
    ("MARYLEBONE",         28938628, "Hotel",                 False),
    ("ARTHOUSE",           28938628, "Hotel",                 False),
    # Petrol
    ("CIRCLE K",           28938648, "Gas And Fuel",          False),
    ("APPLEGREEN",         28938648, "Gas And Fuel",          False),
    ("SAINSBURYS PETROL",  28938648, "Gas And Fuel",          False),
    ("BP ",                28938648, "Gas And Fuel",          False),
    # Subscriptions
    ("CHATPDF",            29202689, "Subscriptions",         False),
    ("DASHLANE",           29202689, "Subscriptions",         False),
    ("EVERNOTE",           29202689, "Subscriptions",         False),
    ("HEADWAY",            29202689, "Subscriptions",         False),
    ("ROCKET MONEY",       29202689, "Subscriptions",         False),
    ("NORD",               29202689, "Subscriptions",         False),
    ("HPI INSTANT INK",    29202689, "Subscriptions",         False),
    ("HP INSTANT INK",     29202689, "Subscriptions",         False),
    ("ADOBE",              29202689, "Subscriptions",         False),
    # Charity
    ("GA PUBLIC BROADCASTING", 28939484, "Charity",           False),
    ("META PPGF",          28939484, "Charity",               False),
    ("GOFUNDME",           28939484, "Charity",               False),
    ("GFM*GOFUNDME",       28939484, "Charity",               False),
    # Shopping
    ("WH SMITH",           28937956, "Shopping",              False),
    # Utilities
    ("T-MOBILE",           28939468, "Utilities",             False),
    ("TMOBILE",            28939468, "Utilities",             False),
    # Restaurants / cafes (broad)
    ("SHANTY BITES",       28938632, "Cafes And Restaurants", False),
]

# Super-category parent_id mappings
SUPER_CATS = {
    "Income":                  [28938652],
    "Home & Living":           [28937992, 28939400, 28939436],
    "Food, Health & Personal": [28937960, 28938032, 28938020],
    "Lifestyle":               [28938640, 28938084],      # + Subscriptions id 29202689
    "Money & Work":            [28938056, 28937944, 28939072, 28939220, 28937952],
}

SUBSCRIPTIONS_ID = 29202689


def apply_categorization_rules(key, transactions, source_label):
    """Auto-categorize transactions matching known rules. Returns list of action dicts."""
    actions = []
    for txn in transactions:
        if txn.get("is_transfer") and not any(
            r[0] in (txn.get("payee") or "").upper() for r in RULES
        ):
            continue
        payee_up = (txn.get("payee") or "").upper()
        current_cat = txn.get("category") or {}
        for substring, cat_id, cat_name, force_not_transfer in RULES:
            if substring in payee_up:
                current_cat_id = current_cat.get("id")
                needs_update = (current_cat_id != cat_id) or txn.get("needs_review")
                if needs_update:
                    payload = {"category_id": cat_id, "needs_review": False}
                    if force_not_transfer is False and txn.get("is_transfer"):
                        payload["is_transfer"] = False
                    try:
                        update_transaction(key, txn["id"], payload)
                        actions.append({
                            "payee": txn["payee"],
                            "amount": txn["amount"],
                            "currency": txn["transaction_account"]["currency_code"].upper(),
                            "account": txn["transaction_account"]["name"],
                            "old_category": current_cat.get("title", source_label),
                            "new_category": cat_name,
                        })
                        print(f"  ✓ {txn['payee']} → {cat_name}")
                    except Exception as e:
                        print(f"  ✗ Failed {txn['payee']}: {e}", file=sys.stderr)
                else:
                    # Correct category but needs_review still set — just clear it
                    if txn.get("needs_review"):
                        try:
                            update_transaction(key, txn["id"], {"needs_review": False})
                        except Exception:
                            pass
                break
    return actions


def aggregate_budget(budget_data):
    """Roll up budget data into super categories. Returns dict of super_cat → {actual, forecast}."""
    # Build category → parent_id map
    cat_parent = {item["category"]["id"]: item["category"].get("parent_id")
                  for item in budget_data}

    totals = {k: {"actual": 0.0, "forecast": 0.0} for k in SUPER_CATS}

    for item in budget_data:
        cat = item["category"]
        cat_id = cat["id"]
        parent_id = cat.get("parent_id")
        is_transfer_cat = item.get("is_transfer", False)

        if is_transfer_cat:
            continue

        # Determine which super category this belongs to
        super_cat = None
        if cat_id == SUBSCRIPTIONS_ID or parent_id == SUBSCRIPTIONS_ID:
            super_cat = "Lifestyle"
        else:
            for sc, parents in SUPER_CATS.items():
                if parent_id in parents:
                    super_cat = sc
                    break

        if not super_cat:
            continue

        currency = None
        actual = 0.0
        forecast = 0.0

        # Use expense data for expense super categories, income for Income
        if super_cat == "Income":
            period = item.get("income")
            if period:
                currency = period.get("currency_code", "gbp")
                actual = float(period.get("total_actual_amount") or 0)
                forecast = float(period.get("total_forecast_amount") or 0)
        else:
            period = item.get("expense")
            if period:
                currency = period.get("currency_code", "gbp")
                actual = abs(float(period.get("total_actual_amount") or 0))
                forecast = abs(float(period.get("total_forecast_amount") or 0))

        if not period:
            continue

        # Convert to GBP if needed
        if currency and currency.lower() == "usd":
            actual *= GBP_USD
            forecast *= GBP_USD
        elif currency and currency.lower() == "eur":
            actual *= 0.85  # approximate EUR→GBP
            forecast *= 0.85

        totals[super_cat]["actual"] += actual
        totals[super_cat]["forecast"] += forecast

    return totals


def md_to_html(md):
    """Minimal markdown → HTML conversion for email."""
    html = md
    html = re.sub(r'^# (.+)$', r'<h2>\1</h2>', html, flags=re.MULTILINE)
    html = re.sub(r'^## (.+)$', r'<h3>\1</h3>', html, flags=re.MULTILINE)
    html = re.sub(r'^### (.+)$', r'<h4>\1</h4>', html, flags=re.MULTILINE)
    html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
    html = re.sub(r'~~(.+?)~~', r'<del>\1</del>', html)
    html = re.sub(r'`(.+?)`', r'<code>\1</code>', html)

    # Tables
    lines = html.split('\n')
    result_lines, in_table, table_rows = [], False, []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('|') and '|' in stripped[1:]:
            if re.match(r'^\|[-| :]+\|$', stripped):
                continue
            cells = [c.strip() for c in stripped.strip('|').split('|')]
            if not in_table:
                in_table = True
                table_rows = ['<thead><tr>' + ''.join(f'<th style="padding:6px;border:1px solid #ccc">{c}</th>' for c in cells) + '</tr></thead><tbody>']
            else:
                table_rows.append('<tr>' + ''.join(f'<td style="padding:6px;border:1px solid #ccc">{c}</td>' for c in cells) + '</tr>')
        else:
            if in_table:
                result_lines.append('<table style="border-collapse:collapse;margin:12px 0">' + ''.join(table_rows) + '</tbody></table>')
                in_table = False
                table_rows = []
            result_lines.append(line)
    if in_table:
        result_lines.append('<table style="border-collapse:collapse;margin:12px 0">' + ''.join(table_rows) + '</tbody></table>')

    html = '\n'.join(result_lines)
    html = re.sub(r'^- (.+)$', r'<li>\1</li>', html, flags=re.MULTILINE)
    html = re.sub(r'(?:<li>.*?</li>\n?)+', lambda m: f'<ul style="margin:8px 0">{m.group(0)}</ul>', html, flags=re.DOTALL)
    html = re.sub(r'\n{2,}', '<br>', html)
    return html


def send_email_smtp(gmail_address, app_password, to_address, subject, body_html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_address
    msg["To"]      = to_address
    msg.attach(MIMEText(body_html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, app_password)
        server.sendmail(gmail_address, to_address, msg.as_string())
    print(f"  Email sent to {to_address}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ps_key       = os.environ["POCKETSMITH_API_KEY"]
    anth_key     = os.environ["ANTHROPIC_API_KEY"]
    gmail_addr   = os.environ["GMAIL_ADDRESS"]
    gmail_pass   = os.environ["GMAIL_APP_PASSWORD"]
    report_to    = os.environ.get("REPORT_TO_EMAIL", gmail_addr)
    today = date.today().isoformat()

    print("── Fetching PocketSmith data ──")
    uncategorized = fetch_uncategorized(ps_key)
    needs_review  = fetch_needs_review(ps_key)
    recent        = fetch_recent(ps_key, days=2)
    budget        = fetch_budget(ps_key)
    print(f"  uncategorized={len(uncategorized)}  needs_review={len(needs_review)}  recent={len(recent)}")

    print("── Applying categorization rules ──")
    auto_done = []
    auto_done += apply_categorization_rules(ps_key, uncategorized, "uncategorized")
    auto_done += apply_categorization_rules(ps_key, needs_review, "needs_review")

    print("── Aggregating budget ──")
    budget_totals = aggregate_budget(budget)

    print("── Calling Claude for report ──")
    client = anthropic.Anthropic(api_key=anth_key)

    # Trim transaction data to what Claude needs (strip verbose institution/logo fields)
    def slim(txns):
        out = []
        for t in txns:
            ta = t.get("transaction_account", {})
            out.append({
                "id": t["id"],
                "date": t["date"],
                "payee": t["payee"],
                "amount": t["amount"],
                "currency": ta.get("currency_code", "gbp").upper(),
                "account": ta.get("name"),
                "category": (t.get("category") or {}).get("title"),
                "is_transfer": t.get("is_transfer"),
                "needs_review": t.get("needs_review"),
                "status": t.get("status"),
                "memo": t.get("memo"),
            })
        return out

    budget_summary = []
    for sc, vals in budget_totals.items():
        pct = round(vals["actual"] / vals["forecast"] * 100) if vals["forecast"] else 0
        budget_summary.append({
            "super_category": sc,
            "actual_gbp": round(vals["actual"], 2),
            "forecast_gbp": round(vals["forecast"], 2),
            "pct_used": pct,
        })

    prompt = f"""You are running a daily financial review for Todd Copilevitz. Today is {today}.

The following categorizations were already applied automatically this run:
{json.dumps(auto_done, indent=2)}

Remaining uncategorized transactions (after rules applied — anything here is genuinely ambiguous):
{json.dumps(slim(uncategorized), indent=2)}

Remaining needs-review transactions (after rules applied):
{json.dumps(slim(needs_review), indent=2)}

Recent transactions (last 48 hours) — flag any over £500 or $500, cryptic payees not on known-merchant list, duplicates:
{json.dumps(slim(recent), indent=2)}

Pre-aggregated budget by super category (already converted to GBP):
{json.dumps(budget_summary, indent=2)}

Known merchants (DO NOT flag these, they are expected):
- MINDY APPEL = therapist payments (Healthcare)
- ALLWYN ENT = National Lottery (Games)
- T-Mobile / TMOBILE = phone plan (Utilities)
- URSA MINOR = Ursa Minor Bakehouse, local café
- GoFundMe / GFM = charity donation
- Monageer property account entries = Ireland house purchase transfers, expected

Write the report in this exact markdown format:

# Daily Financial Review — {today}

## Action Required
[Items needing Todd's decision. If nothing, write: Nothing requires your attention today.]

## Auto-Categorized
[Table with columns: Payee | Amount | Account | Old Category | New Category
Only include entries from the auto_done list above. If empty, write: None.]

## Spending by Super Category (Month to Date)
| Super Category | MTD Spend | Budget | % Used |
|---|---|---|---|
[Use the budget_summary data. Flag rows over 80% with ⚠️ and over 100% with 🚨.]

## Unusual Transactions
[Large amounts or suspicious items from recent transactions. If none, say so.]

## All Clear
[Confirm what areas have nothing to flag.]

Be terse. No preamble. No sign-off."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    report_md = response.content[0].text
    print("── Report generated ──")
    print(report_md)

    print("── Sending email ──")
    send_email_smtp(gmail_addr, gmail_pass, report_to,
                    f"Daily Financial Review — {today}", md_to_html(report_md))

    print("── Done ──")


if __name__ == "__main__":
    main()
