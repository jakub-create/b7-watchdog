#!/usr/bin/env python3
"""
B7 ID marketplace watchdog.

Detects when new race-entry ("startovné") listings appear on the b7id.cz
marketplace for a given race and sends an e-mail alert.

How it works
------------
b7id.cz is a React SPA, but the listings come from a clean JSON API:
    GET https://app-main-prod.b7id.cz/market/listOffers?raceId=<id>   ->  {"offers": [...]}
Auth is a session cookie. So we:
  1. log in once via the login form (Playwright) to obtain the session cookie,
  2. call the listOffers API within the same browser context (cookies shared),
  3. compare the returned offers against the saved state and e-mail anything new.

Modes (env var MODE):
  - watch     : the real run (default).
  - discovery : log in, call the API, and dump the raw JSON + a screenshot into
                ./debug/ so the offer fields can be inspected when offers exist.

Secrets (password, e-mail login) come from the environment — never hard-coded.
Locally from a .env file; in GitHub Actions from repository Secrets.
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration (from environment)
# ---------------------------------------------------------------------------
MODE = os.environ.get("MODE", "watch").strip().lower()

LOGIN_URL = os.environ.get("B7_LOGIN_URL", "https://b7id.cz/login")
RACE_ID = os.environ.get("B7_RACE_ID", "699b407072bb7f5cd634f41a")
API_URL = os.environ.get(
    "B7_API_URL",
    f"https://app-main-prod.b7id.cz/market/listOffers?raceId={RACE_ID}",
)
# Human-facing URL used only in the e-mail body.
MARKETPLACE_URL = os.environ.get(
    "B7_MARKETPLACE_URL", f"https://b7id.cz/marketplace?raceId={RACE_ID}"
)

B7_EMAIL = os.environ.get("B7_EMAIL", "")
B7_PASSWORD = os.environ.get("B7_PASSWORD", "")

# Login form: fields are matched by their visible label (the e-mail field is
# type="text", not "email"). Env CSS selectors override if the page changes.
SEL_EMAIL = os.environ.get("B7_SEL_EMAIL", "")
SEL_PASSWORD = os.environ.get("B7_SEL_PASSWORD", "")
SEL_SUBMIT = os.environ.get("B7_SEL_SUBMIT", "")
LABEL_EMAIL = "E-mailová adresa"
LABEL_PASSWORD = "Heslo"
LABEL_SUBMIT = "Přihlásit"

# E-mail (SMTP) — works with Gmail, Seznam, etc.
SMTP_HOST = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
SMTP_PORT = int(os.environ.get("SMTP_PORT") or "587")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
# If MAIL_TO/MAIL_FROM are unset (or blank), send from/to the SMTP account itself.
MAIL_TO = os.environ.get("MAIL_TO") or SMTP_USER
MAIL_FROM = os.environ.get("MAIL_FROM") or SMTP_USER

STATE_FILE = Path(os.environ.get("STATE_FILE", "state/seen.json"))
DEBUG_DIR = Path("debug")

# --- Auto-reserve (optional) ---
# RESERVE_MODE:
#   "off"    – never reserve (default).
#   "dryrun" – only log + e-mail what it WOULD reserve; does NOT call the API.
#   "live"   – actually call buyOffer.
# Keep this at "off"/"dryrun" until the buyOffer request has been confirmed on a
# REAL offer to be a non-charging, cancellable 24h hold (the endpoint is literally
# named "buyOffer", so we do not enable "live" on a guess).
RESERVE_MODE = os.environ.get("RESERVE_MODE", "off").strip().lower()
# Max buyerTotal (CZK) to auto-reserve; 0 = no limit.
RESERVE_MAX_PRICE = int(os.environ.get("RESERVE_MAX_PRICE") or "0")
BUY_URL = os.environ.get("B7_BUY_URL", "https://app-main-prod.b7id.cz/market/buyOffer")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[watchdog] {msg}", flush=True)


def send_email(subject: str, body: str) -> None:
    if not (SMTP_USER and SMTP_PASS and MAIL_TO):
        log("SMTP not configured — printing the alert instead of e-mailing:")
        log(f"  SUBJECT: {subject}")
        log(f"  BODY:\n{body}")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    log(f"E-mail sent to {MAIL_TO}: {subject}")


def load_state() -> set | None:
    """Return the set of previously-seen offer ids, or None if we've never run
    (state file missing / unreadable). None means "establish a silent baseline";
    an empty set means "last poll legitimately had zero offers"."""
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception as e:  # noqa: BLE001
            log(f"Could not read state ({e}); re-establishing baseline.")
    return None


def save_state(ids: set) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=2))


def offer_id(offer: dict) -> str:
    """Stable unique key for an offer."""
    for key in ("id", "_id", "offerId", "uuid"):
        if offer.get(key) is not None:
            return str(offer[key])
    # Fallback: hash the whole offer so we still detect changes.
    return str(hash(json.dumps(offer, sort_keys=True, ensure_ascii=False)))


def offer_label(offer: dict) -> str:
    """Human-readable one-liner for the e-mail."""
    name = (
        offer.get("name")
        or offer.get("racerName")
        or offer.get("sellerName")
        or offer.get("category")
        or offer.get("title")
    )
    price = (
        offer.get("buyerTotal")
        or offer.get("price")
        or offer.get("askPrice")
        or offer.get("amount")
    )
    size = (offer.get("slotIncludes") or {}).get("tshirtSize")
    parts = []
    if name:
        parts.append(str(name))
    if price is not None:
        parts.append(f"{price} Kč")
    if size:
        parts.append(f"triko {size}")
    if not parts:  # unknown shape — show the raw fields so nothing is lost
        parts.append(json.dumps(offer, ensure_ascii=False))
    return " — ".join(parts)


# ---------------------------------------------------------------------------
# Browser session helpers
# ---------------------------------------------------------------------------
def do_login(page) -> None:
    log(f"Opening login page: {LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="networkidle", timeout=60000)
    email_field = page.locator(SEL_EMAIL) if SEL_EMAIL else page.get_by_label(LABEL_EMAIL)
    pw_field = page.locator(SEL_PASSWORD) if SEL_PASSWORD else page.get_by_label(LABEL_PASSWORD)
    submit = (
        page.locator(SEL_SUBMIT)
        if SEL_SUBMIT
        else page.get_by_role("button", name=LABEL_SUBMIT)
    )
    email_field.fill(B7_EMAIL, timeout=15000)
    pw_field.fill(B7_PASSWORD, timeout=15000)
    submit.click(timeout=15000)
    page.wait_for_load_state("networkidle", timeout=60000)
    page.wait_for_timeout(2000)
    log("Logged in.")


def get_offers(context) -> tuple[list[dict], dict]:
    resp = context.request.get(API_URL, timeout=30000)
    if resp.status != 200:
        raise RuntimeError(f"API returned HTTP {resp.status}: {resp.text()[:300]}")
    data = resp.json()
    offers = data.get("offers", []) if isinstance(data, dict) else []
    if not isinstance(offers, list):
        offers = []
    return offers, data


def reserve_offer(context, offer) -> str:
    """Place a 24h hold on an offer via the buyOffer endpoint.

    WARNING: the exact request shape has NOT been confirmed against a real offer
    yet. This best-effort guess is only ever invoked in RESERVE_MODE=live, which
    must stay disabled until we observe one real reservation — to lock in the call
    AND confirm it merely holds the slot (no charge). The full response is returned
    so the real shape can be finalized.
    """
    oid = offer.get("_id") or offer.get("id")
    resp = context.request.post(
        BUY_URL,
        data=json.dumps({"offerId": oid}),
        headers={"content-type": "application/json"},
        timeout=30000,
    )
    return f"HTTP {resp.status}: {resp.text()[:300]}"


def maybe_reserve(context, new_ids, current) -> list[str]:
    """Auto-reserve qualifying new offers per RESERVE_MODE. Returns human notes."""
    if RESERVE_MODE not in ("dryrun", "live"):
        return []
    notes = []
    for i in new_ids:
        o = current[i]
        price = o.get("buyerTotal") or o.get("askPrice") or 0
        if RESERVE_MAX_PRICE and price and price > RESERVE_MAX_PRICE:
            log(f"Offer {i}: price {price} > limit {RESERVE_MAX_PRICE}; not reserving.")
            notes.append(f"  • {offer_label(o)} → NEzarezervováno (nad limit {RESERVE_MAX_PRICE} Kč)")
            continue
        if RESERVE_MODE == "dryrun":
            log(f"[DRYRUN] would reserve offer {i} (buyerTotal={price}).")
            notes.append(f"  • {offer_label(o)} → NANEČISTO (rezervace by proběhla)")
        else:  # live
            try:
                res = reserve_offer(context, o)
                log(f"[LIVE] reserve {i}: {res}")
                notes.append(f"  • {offer_label(o)} → {res}")
            except Exception as e:  # noqa: BLE001
                log(f"[LIVE] reserve error {i}: {e}")
                notes.append(f"  • {offer_label(o)} → CHYBA rezervace: {e}")
    return notes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run() -> int:
    if MODE == "testmail":
        send_email(
            "✅ Test – B7 ID hlídač funguje",
            "Tohle je testovací zpráva z hlídače startovného.\n"
            "Pokud ti dorazila, odesílání e-mailů je správně nastavené.\n\n"
            f"Sleduji: {MARKETPLACE_URL}",
        )
        return 0

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            do_login(page)
            offers, raw = get_offers(context)
            log(f"API returned {len(offers)} offer(s).")

            if MODE == "discovery":
                DEBUG_DIR.mkdir(exist_ok=True)
                (DEBUG_DIR / "listOffers.json").write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2)
                )
                log(f"Discovery: raw API response saved to ./{DEBUG_DIR}/listOffers.json")
                return 0

            current = {offer_id(o): o for o in offers}
            current_ids = set(current)
            seen = load_state()

            if seen is None:
                # Truly the first run (no state file yet): silent baseline so we
                # don't e-mail about offers that already exist. An EMPTY saved
                # state is different — last poll had zero offers, so a newly
                # appearing offer must trigger an alert.
                save_state(current_ids)
                log(f"First run — baseline saved ({len(current_ids)} offer(s)), no alert sent.")
                return 0

            new_ids = current_ids - seen
            if not new_ids:
                log("No new offers.")
                save_state(current_ids)
                return 0

            reserve_notes = maybe_reserve(context, new_ids, current)

            lines = "\n".join(f"  • {offer_label(current[i])}" for i in new_ids)
            body = f"Na tržišti přibylo nové startovné ({len(new_ids)}):\n\n{lines}\n\n"
            if reserve_notes:
                mode_label = {
                    "dryrun": "NANEČISTO – nic se reálně nerezervovalo",
                    "live": "OSTRÁ rezervace",
                }.get(RESERVE_MODE, RESERVE_MODE)
                body += f"Automatická rezervace [{mode_label}]:\n" + "\n".join(reserve_notes) + "\n\n"
            body += f"Otevři: {MARKETPLACE_URL}"
            send_email(f"🏁 Nové startovné ({len(new_ids)}) — B7 ID", body)

            # Keep current offers as the new baseline (disappeared+reappeared = new).
            save_state(current_ids)
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(run())
