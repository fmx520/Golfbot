"""Read ForeUp's own checkout JavaScript to find the fields we do not send.

Our reservation payload was reverse-engineered from observed requests, and it
is missing whatever satisfies a booking fee -- ForeUp accepts the hold and then
refuses the conversion with "A booking fee is required to book this tee time."
The definitive answer is a captured checkout, but that costs a real booking.

This is the free attempt first. The booking page ships the script that builds
that payload, and our payload contains strings distinctive enough to locate it:
nothing else on the internet has an `airQuotesCart`. Find that, read the object
being assembled around it, and any key we do not already send is a candidate.

Read-only: fetches the page and its scripts and nothing else. Holds nothing,
books nothing.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urljoin

# Strings from our own payload, unusual enough that a hit means we are looking
# at the code that builds the reservation rather than at unrelated bundle noise.
ANCHORS = (
    "airQuotesCart",
    "pending_reservation_id",
    "preTaxSubtotal",
    "reservation_confirmation_uid",
    "booking_fee_required",
    "totalWithTax",
)

# What a fee-paying field plausibly looks like. Deliberately broad -- a false
# positive costs a glance, a miss costs the whole exercise.
PAYMENT_HINTS = (
    "booking_fee", "bookingFee", "pay_online", "payOnline", "payment",
    "sale_id", "saleId", "card", "token", "charge", "stripe", "cardknox",
    "customer_card", "savedCard", "saved_card", "credit", "authorize",
    "transaction", "invoice", "receipt", "checkout",
)

# Cheap guards: booking pages pull in a lot of vendor code we do not care about.
MAX_SCRIPTS = 15
MAX_BYTES = 4_000_000
CONTEXT = 700

_SRC = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)
_IDENT = re.compile(r"""["']?([A-Za-z_][A-Za-z0-9_]{2,40})["']?\s*:""")


def script_urls(html: str, base_url: str, page_url: str) -> list[str]:
    """Absolute URLs for every script the booking page pulls in, ForeUp's first.

    Third-party bundles (analytics, recaptcha) cannot contain ForeUp's payload,
    so same-origin scripts are tried before anything else and the cap is spent
    where it can pay off.
    """
    urls: list[str] = []
    for src in _SRC.findall(html):
        if src.startswith("//"):
            full = "https:" + src
        elif src.startswith("http"):
            full = src
        else:
            full = urljoin(page_url, src)
        if full not in urls:
            urls.append(full)
    host = base_url.split("//")[-1].split("/")[0]
    return sorted(urls, key=lambda u: host not in u)[:MAX_SCRIPTS]


def _keys_near(text: str, index: int) -> set[str]:
    window = text[max(0, index - CONTEXT): index + CONTEXT]
    return {m.group(1) for m in _IDENT.finditer(window)}


def scan_source(name: str, text: str, known_keys: Iterable[str]) -> dict[str, Any]:
    """Look for the payload builder in one script, and report what surrounds it."""
    known = {k.lower() for k in known_keys}
    hits: list[dict[str, Any]] = []
    candidates: set[str] = set()

    for anchor in ANCHORS:
        for m in re.finditer(re.escape(anchor), text):
            near = _keys_near(text, m.start())
            new = {k for k in near if k.lower() not in known}
            candidates |= new
            hits.append({
                "anchor": anchor,
                "at": m.start(),
                "excerpt": " ".join(
                    text[max(0, m.start() - 240): m.start() + 240].split()
                )[:480],
                "unknown_keys_nearby": sorted(new)[:40],
            })
            break  # one example per anchor is enough to read

    # Payment-shaped names anywhere in a script that mentions the reservation,
    # since the charge may be assembled well away from the payload itself.
    payment: list[dict[str, str]] = []
    if hits:
        for hint in PAYMENT_HINTS:
            m = re.search(re.escape(hint), text, re.I)
            if not m:
                continue
            payment.append({
                "hint": hint,
                "excerpt": " ".join(
                    text[max(0, m.start() - 200): m.start() + 200].split()
                )[:400],
            })

    return {
        "script": name,
        "bytes": len(text),
        "anchor_hits": hits,
        "payment_mentions": payment,
        "candidate_fields": sorted(candidates)[:60],
    }


# ForeUp's booking bundle exports its API surface by name. Two of these are
# calls we have never made, and one of them -- postNonCreditCardPaymentType --
# is the shape of the "Pay at Facility" selection that sits between the code
# prompt and Book Time.
API_NAMES = (
    "postNonCreditCardPaymentType",
    "postCreateBooking",
    "postCreditCardWindow",
    "postPendingReservationRefresh",
    "getDetailedCart",
    "postGoogleBookingConversion",
)

# Path-ish string literals: what a fetch() target looks like once minified.
_URLISH = re.compile(r"""["'`]([/][A-Za-z0-9_\-/{}.:]{3,120}|[A-Za-z0-9_]+/[A-Za-z0-9_\-/{}.:]{3,120})["'`]""")
_CONCAT = re.compile(r"""(REST_API_URL|BASE_API_URL|COURSE_ID|API_URL)""")


def find_api_calls(text: str, names: Iterable[str] = API_NAMES) -> list[dict[str, Any]]:
    """Locate named API helpers and recover the URL each one builds.

    Minification renames the local function but leaves the exported name and
    the URL string literals intact, so a window around the name is enough to
    read the endpoint back out.
    """
    out: list[dict[str, Any]] = []
    for name in names:
        for m in re.finditer(re.escape(name), text):
            window = text[max(0, m.start() - 200): m.start() + 1600]
            urls = [u for u in _URLISH.findall(window) if "/" in u]
            bases = sorted(set(_CONCAT.findall(window)))
            if not urls and not bases:
                continue
            out.append({
                "name": name,
                "url_fragments": sorted(set(urls))[:8],
                "url_bases": bases,
                "excerpt": " ".join(window[200:900].split())[:600],
            })
            break
    return out


def scan_checkout(
    *,
    html: str,
    base_url: str,
    page_url: str,
    fetch: Callable[[str], Optional[str]],
    known_keys: Iterable[str],
) -> dict[str, Any]:
    """Scan the booking page and its scripts for the reservation payload builder."""
    results: list[dict[str, Any]] = []
    sources: list[tuple[str, str]] = [("(booking page HTML)", html)]
    budget = MAX_BYTES

    # The page itself first: ForeUp inlines a good deal of its booking code.
    results.append(scan_source("(booking page HTML)", html, known_keys))

    for url in script_urls(html, base_url, page_url):
        if budget <= 0:
            break
        text = fetch(url)
        if not text:
            continue
        budget -= len(text)
        sources.append((url, text))
        found = scan_source(url, text, known_keys)
        # Only keep scripts that actually touch the reservation.
        if found["anchor_hits"]:
            results.append(found)

    # Also hunt the named API helpers wherever they live, anchored or not.
    api_calls: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for name, text in sources:
        for call in find_api_calls(text):
            if call["name"] in seen_names:
                continue
            seen_names.add(call["name"])
            call["script"] = name
            api_calls.append(call)

    anchored = [r for r in results if r["anchor_hits"]]
    all_candidates: set[str] = set()
    for r in anchored:
        all_candidates |= set(r["candidate_fields"])

    return {
        "scripts_scanned": len(results),
        "scripts_with_payload_code": [r["script"] for r in anchored],
        "results": anchored or results[:1],
        "candidate_fields": sorted(all_candidates)[:80],
        "api_calls": api_calls,
        "verdict": (
            f"Found the reservation payload in {len(anchored)} source(s). "
            f"{len(all_candidates)} field name(s) appear near it that we do not send — "
            f"look for anything fee- or payment-shaped."
            if anchored else
            "Did not find our payload's fields in the page or its scripts. The "
            "checkout is probably built in a bundle we could not fetch, or the "
            "names are minified away. The DevTools capture is the fallback."
        ),
    }
