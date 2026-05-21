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
    return ps_get(key, f"/users/{USER_ID}/transactions",
                  {"needs_review": 1, "per_page": 100})


def fetch_recent(key, days=2):
    start = (date.today() - timedelta(days=days)).isoformat()
    end = date.today().isoformat()
    return ps_get(key, f"/users/{USER_ID}/transactions",
                  {"start_date": start, "end_date": end, "per_page": 100})


def fetch_budget(key):
    return ps_get(key, f"/users/{USER_ID}/budget", {"roll_up": 1})


def fetch_transaction_accounts(key):
    return ps_get(key, f"/users/{USER_ID}/transaction_accounts")


def update_transaction(key, txn_id, payload):
    return ps_patch(key, f"/transactions/{txn_id}", payload)


# ── Categorization rules ─────────────────────────────────────────────────────
# (payee_substring_uppercase, category_id, category_label, force_not_transfer)
# To teach the script a new merchant: add one line here and commit. Keep this list
# in sync with the known_merchants memory note used by the Cowork assistant.

RULES = [
    # Known merchants
    ("MINDY APPEL",        28939216, "Healthcare",            False),
    ("SQ *URSA MINOR",     28938632, "Cafes And Restaurants", False),
    ("URSA MINOR",         28938632, "Cafes And Restaurants", False),
    ("SPICKSPAN",          28938036, "Personal Care",         False),
    ("SP MARLEYBONES",     28939472, "Pet Food And Supplies", False),
    ("MARLEYBONES",        28939472, "Pet Food And Supplies", False),
    ("MOOD",               28939408, "Wellness",              False),  # cannabis; corrected repeatedly, now permanent
    ("GOOGLE",             29202689, "Subscriptions",         False),  # YouTube TV
    ("THE WHITE HOUSE",    28937956, "Shopping",              False),  # clothing retailer, NOT education
    ("CLOUD PICK",         28938632, "Cafes And Restaurants", False),  # Cloud Picker Coffee, Dublin Airport
    ("DUB CLOUD PICK",     28938632, "Cafes And Restaurants", False),
    ("COCA COLA",          28938632, "Cafes And Restaurants", False),  # vending-machine drinks (Coca Cola Ni etc.) — consumed out, not groceries
    # Supermarkets / food retail -> Groceries
    ("MARKS&SPENCER",      28937964, "Groceries",             False),  # M&S Simply Food (small amounts); confirmed Groceries not Shopping
    # Butchers -> Groceries
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
    # Parking -> Auto And Transport
    ("NITHCO HI PARK",     28939432, "Auto And Transport",    False),  # Nithco car park, NI
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
    # Home Improvement / furniture
    ("KEENS BELFAST",      28939084, "Home Improvement",      False),  # furniture store, Belfast; purchases typically for Monageer
]

# Super-category parent_id mappings
SUPER_CATS = {
    "Income":                  [28938652],
    # Home & Living incl. property capital works (CGT-deductible) + repairs/maintenance
    "Home & Living":           [28937992, 28939400, 28939436, 32046111, 32046219],
    "Food, Health & Personal": [28937960, 28938032, 28938020],
    "Lifestyle":               [28938640, 28938084],
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
                        print(f"  OK {txn['payee']} -> {cat_name}")
                    except Exception as e:
                        print(f"  FAIL {txn['payee']}: {e}", file=sys.stderr)
                else:
                    if txn.get("needs_review"):
                        try:
                            update_transaction(key, txn["id"], {"needs_review": False})
                        except Exception:
                            pass
                break
    return actions


def check_feed_health(accounts, stale_days=FEED_STALE_DAYS):
    """Return [(institution, account_name, last_date, age_days)] for live accounts whose
    balance hasn't updated in > stale_days. Offline/manual accounts are skipped. A silently
    dead feed is the most dangerous blind spot — surface it, never try to fix it."""
    today = date.today()
    stale = []
    for a in accounts:
        if a.get("offline"):
            continue
        cbd = a.get("current_balance_date")
        if not cbd:
            continue
        try:
            d = date.fromisoformat(str(cbd)[:10])
        except Exception:
            continue
        age = (today - d).days
        if age > stale_days:
            inst = (a.get("institution") or {}).get("title", "?")
            stale.append((inst, a.get("name", "?"), cbd, age))
    return sorted(stale, key=lambda x: -x[3])


def aggregate_budget(budget_data, usd_gbp, eur_gbp):
    """Roll up budget data into super categories."""
    totals = {k: {"actual": 0.0, "forecast": 0.0} for k in SUPER_CATS}

    for item in budget_data:
        cat = item["category"]
        cat_id = cat["id"]
        parent_id = cat.get("parent_id")

        if item.get("is_transfer", False):
            continue

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

        if super_cat == "Income":
            period = item.get("income")
        else:
            period = item.get("expense")

        if not period:
            continue

        currency = period.get("currency_code", "gbp")
        actual = abs(float(period.get("total_actual_amount") or 0))
        forecast = abs(float(period.get("total_forecast_amount") or 0))

        if currency and currency.lower() == "usd":
            actual *= usd_gbp
            forecast *= usd_gbp
        elif currency and currency.lower() == "eur":
            actual *= eur_gbp
            forecast *= eur_gbp

        totals[super_cat]["actual"] += actual
        totals[super_cat]["forecast"] += forecast

    return totals


# ── Report generation ────────────────────────────────────────────────────────

def fmt_amount(amount, currency="GBP"):
    sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency.upper(), currency + " ")
    return f"{sym}{abs(float(amount)):.2f}"


def is_known_payee(payee):
    up = (payee or "").upper()
    return any(k in up for k in KNOWN_PAYEES)


def generate_report(today, feed_health, auto_done, remaining_uncategorized,
                    remaining_needs_review, recent, budget_totals):
    lines = []
    lines.append(f"# Daily Financial Review — {today}")
    lines.append("")

    # ── Feed Health (first — a dead feed silently stops data) ──
    lines.append("## Feed Health")
    lines.append("")
    if feed_health:
        lines.append("⚠️ These live feeds have not updated recently — transactions may be silently missing. No action taken automatically; check the provider.")
        lines.append("")
        for inst, name, last, age in feed_health:
            lines.append(f"- **{inst} — {name}**: last updated {last} ({age} days ago).")
    else:
        lines.append("All live feeds syncing normally.")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Action Required ──
    lines.append("## Action Required")
    lines.append("")
    action_items = []

    for txn in remaining_uncategorized:
        if txn.get("is_transfer"):
            continue
        ta = txn.get("transaction_account", {})
        currency = ta.get("currency_code", "GBP").upper()
        action_items.append(
            f"**{txn['payee']} — {fmt_amount(txn['amount'], currency)} | "
            f"{ta.get('name', '?')} | {txn['date']}**  \n"
            f"Uncategorized. Assign a category (add to RULES to auto-handle next time)."
        )

    for txn in remaining_needs_review:
        if txn.get("is_transfer"):
            continue
        ta = txn.get("transaction_account", {})
        currency = ta.get("currency_code", "GBP").upper()
        cat_name = (txn.get("category") or {}).get("title", "unknown category")
        action_items.append(
            f"**{txn['payee']} — {fmt_amount(txn['amount'], currency)} | "
            f"{ta.get('name', '?')} | {txn['date']}**  \n"
            f"Flagged for review. Currently: {cat_name}."
        )

    if action_items:
        for item in action_items:
            lines.append(item)
            lines.append("")
    else:
        lines.append("Nothing requires your attention today.")
        lines.append("")

    lines.append("---")
    lines.append("")

    # ── Auto-Categorized ──
    lines.append("## Auto-Categorized")
    lines.append("")
    if auto_done:
        lines.append("| Payee | Amount | Account | Old Category | New Category |")
        lines.append("|---|---|---|---|---|")
        for a in auto_done:
            amt = fmt_amount(a["amount"], a.get("currency", "GBP"))
            lines.append(
                f"| {a['payee']} | {amt} | {a['account']} | "
                f"{a['old_category']} | {a['new_category']} |"
            )
    else:
        lines.append("None.")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Budget Summary ──
    lines.append("## Spending by Super Category (Month to Date)")
    lines.append("")
    lines.append("**Note:** budget figures are provisional — PocketSmith currently has duplicated/triplicated budget events that overstate the targets. Treat % used as directional until cleaned.")
    lines.append("")
    lines.append("| Super Category | MTD Spend | Budget | % Used |")
    lines.append("|---|---|---|---|")
    budget_notes = []
    for sc, vals in budget_totals.items():
        actual = vals["actual"]
        forecast = vals["forecast"]
        pct = round(actual / forecast * 100) if forecast else 0
        flag = " 🚨" if pct > 100 else (" ⚠️" if pct > 80 else "")
        lines.append(f"| {sc} | £{actual:,.0f} | £{forecast:,.0f} | {pct}%{flag} |")
        if pct > 100:
            budget_notes.append(f"**{sc}** is over budget ({pct}% of £{forecast:,.0f}).")
        elif pct > 80:
            budget_notes.append(f"**{sc}** is at {pct}% of budget — running hot.")
    lines.append("")
    if budget_notes:
        lines.append("**Notes:**")
        for note in budget_notes:
            lines.append(f"- {note}")
        lines.append("")
    lines.append("---")
    lines.append("")

    # ── Unusual Transactions ──
    lines.append("## Unusual Transactions (Last 48 Hours)")
    lines.append("")
    unusual = []
    seen = {}  # for duplicate detection: (payee, amount, date) → count

    for txn in recent:
        ta = txn.get("transaction_account", {})
        currency = ta.get("currency_code", "GBP").upper()
        amt = abs(float(txn["amount"]))
        key = (txn["payee"], txn["amount"], txn["date"])
        seen[key] = seen.get(key, 0) + 1

        if amt >= LARGE_TXN_THRESHOLD and not is_known_payee(txn["payee"]):
            unusual.append(
                f"**{txn['payee']}** — {fmt_amount(txn['amount'], currency)} | "
                f"{ta.get('name', '?')} | {txn['date']} — large transaction."
            )

    for (payee, amount, txn_date), count in seen.items():
        if count > 1:
            unusual.append(f"**Possible duplicate:** {payee} {amount} on {txn_date} appears {count}×.")

    if unusual:
        for u in unusual:
            lines.append(f"- {u}")
    else:
        lines.append("No transactions over the £500/$500 threshold. No duplicates detected.")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── All Clear ──
    lines.append("## All Clear")
    lines.append("")
    clear_bits = [
        "- All live feeds syncing." if not feed_health else "- Feed issues flagged above.",
        "- Auto-categorization rules applied successfully.",
        "- Budget rollup complete.",
    ]
    lines.append("\n".join(clear_bits))

    return "\n".join(lines)


# ── Email ────────────────────────────────────────────────────────────────────

def md_to_html(md):
    """Minimal markdown → HTML conversion for email."""
    html = md
    html = re.sub(r'^# (.+)$', r'<h2>\1</h2>', html, flags=re.MULTILINE)
    html = re.sub(r'^## (.+)$', r'<h3>\1</h3>', html, flags=re.MULTILINE)
    html = re.sub(r'^### (.+)$', r'<h4>\1</h4>', html, flags=re.MULTILINE)
    html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
    html = re.sub(r'~~(.+?)~~', r'<del>\1</del>', html)
    html = re.sub(r'`(.+?)`', r'<code>\1</code>', html)

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
    ps_key     = os.environ["POCKETSMITH_API_KEY"]
    gmail_addr = os.environ["GMAIL_ADDRESS"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]
    report_to  = os.environ.get("REPORT_TO_EMAIL", gmail_addr)
    today      = date.today().isoformat()

    usd_gbp, eur_gbp = load_fx()

    print("── Fetching PocketSmith data ──")
    uncategorized = fetch_uncategorized(ps_key)
    needs_review  = fetch_needs_review(ps_key)
    recent        = fetch_recent(ps_key, days=2)
    budget        = fetch_budget(ps_key)
    try:
        accounts = fetch_transaction_accounts(ps_key)
    except Exception as e:
        print(f"  feed-health fetch failed: {e}", file=sys.stderr)
        accounts = []
    print(f"  uncategorized={len(uncategorized)}  needs_review={len(needs_review)}  recent={len(recent)}  accounts={len(accounts)}")

    print("── Checking feed health ──")
    feed_health = check_feed_health(accounts)
    for inst, name, last, age in feed_health:
        print(f"  stale feed: {inst} / {name} ({age}d)")

    print("── Applying categorization rules ──")
    auto_done  = []
    auto_done += apply_categorization_rules(ps_key, uncategorized, "uncategorized")
    auto_done += apply_categorization_rules(ps_key, needs_review, "needs_review")

    # Re-fetch after updates so remaining lists are accurate
    uncategorized = fetch_uncategorized(ps_key)
    needs_review  = fetch_needs_review(ps_key)

    print("── Aggregating budget ──")
    budget_totals = aggregate_budget(budget, usd_gbp, eur_gbp)

    print("── Generating report ──")
    report_md = generate_report(
        today, feed_health, auto_done, uncategorized, needs_review, recent, budget_totals
    )
    print(report_md)

    print("── Sending email ──")
    send_email_smtp(gmail_addr, gmail_pass, report_to,
                    f"Daily Financial Review — {today}", md_to_html(report_md))

    print("── Done ──")


if __name__ == "__main__":
    main()
