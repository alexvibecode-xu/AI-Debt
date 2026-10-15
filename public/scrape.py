"""
Scrape the "All financings" table from amcompute.com/gpu-financing-tracker
and write normalized data for the dashboard.

Standard library only. Run:  python scrape.py

Outputs (in ./data):
  deals.json   - normalized deals + metadata (canonical file)
  deals.js     - same data as `window.DEALS = {...}` so index.html works from file://
  deals.csv    - flat export for Excel
  history/<date>.json - snapshot of each run (so upstream edits/removals are not lost)
"""

from __future__ import annotations

import csv
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

SOURCE_URL = "https://www.amcompute.com/gpu-financing-tracker"
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
HISTORY = DATA / "history"


# ----------------------------------------------------------------------------
# HTML table extraction
# ----------------------------------------------------------------------------
class TableParser(HTMLParser):
    """Collects every <table> as a list of rows; each cell = {"text", "href"}."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[dict]]] = []
        self._row: list[dict] | None = None
        self._cell: dict | None = None
        self._depth = 0  # inside a table?

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._depth += 1
            self.tables.append([])
        elif tag == "tr" and self._depth:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": "", "href": None, "tag": tag}
        elif tag == "a" and self._cell is not None and a.get("href"):
            self._cell["href"] = a["href"]

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._cell["text"] = re.sub(r"\s+", " ", self._cell["text"]).strip()
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.tables[-1].append(self._row)
            self._row = None
        elif tag == "table":
            self._depth -= 1

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data


def fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; ai-financing-tracker/1.0)",
            "Accept": "text/html",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


# ----------------------------------------------------------------------------
# Normalization
# ----------------------------------------------------------------------------
MONEY_RE = re.compile(r"^\$?\s*([\d][\d,]*(?:\.\d+)?)\s*([KMBT])?$", re.I)
MULT = {"K": 0.001, "M": 1.0, "B": 1000.0, "T": 1_000_000.0}


def parse_amount_musd(text: str) -> float | None:
    """'$1.9B' -> 1900.0 (USD millions). 'n/d', '-' -> None."""
    m = MONEY_RE.match(text.strip())
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "M").upper()
    return round(num * MULT[unit], 3)


def parse_date(text: str) -> tuple[str | None, str]:
    """Returns (iso_date, precision). 'Mar 2026' -> ('2026-03-01', 'month')."""
    t = text.strip()
    for fmt, prec in (("%b %d, %Y", "day"), ("%B %d, %Y", "day"), ("%b %Y", "month"), ("%B %Y", "month"), ("%Y", "year")):
        try:
            d = datetime.strptime(t, fmt)
            return d.strftime("%Y-%m-%d"), prec
        except ValueError:
            continue
    return None, "unknown"


def quarter_of(iso: str | None) -> str | None:
    if not iso:
        return None
    y, m = int(iso[:4]), int(iso[5:7])
    return f"{y} Q{(m - 1) // 3 + 1}"


def parse_lenders(text: str) -> tuple[list[str], int]:
    """'JPMorgan, Goldman Sachs +6' -> (['JPMorgan','Goldman Sachs'], 6)."""
    t = text.strip()
    more = 0
    m = re.search(r"\s\+(\d+)$", t)
    if m:
        more = int(m.group(1))
        t = t[: m.start()]
    names = [n.strip() for n in t.split(",") if n.strip()]
    return names, more


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ----------------------------------------------------------------------------
# Market classification: does the instrument trade (i.e. has a Bloomberg price)?
#   "Traded bond"      - senior notes / convertible notes sold to investors (144A etc.)
#   "Syndicated loan"  - broadly syndicated term loans that trade in the loan market
#   "Private"          - bank facilities, private credit, leases, vendor financing...
# Edit MARKET_OVERRIDES to correct individual deals (key = deal id from deals.csv).
# ----------------------------------------------------------------------------
TRADED_TYPES = {"Senior notes", "Convertible notes"}
PRIVATE_CONVERT_HINTS = ("PIK",)  # converts with PIK terms are bilateral, not traded
MARKET_OVERRIDES = {
    "hut-8-2024-06-convertible-notes-150m": "Private",       # placed with Coatue
    "sharon-ai-2025-12-convertible-notes-100m": "Private",   # Canaccord-placed private convert
    "iren-2026-06-senior-notes-2-1b": "Private",             # 5.96% notes are a US private placement (USPP), not in Bloomberg's bond universe
    "lambda-2026-08-term-loan-926m": "Private",              # term loan is not publicly traded (confirmed by user via Bloomberg, 2026-10-15)
}


def classify_market(deal_id: str, type_: str, pricing: str | None, lenders: list[str]) -> str:
    if deal_id in MARKET_OVERRIDES:
        return MARKET_OVERRIDES[deal_id]
    p = pricing or ""
    if type_ in TRADED_TYPES:
        if type_ == "Convertible notes" and any(h in p for h in PRIVATE_CONVERT_HINTS):
            return "Private"
        return "Traded bond"
    if type_ in ("Term loan", "DDTL") and re.search(r"\bOID\b|price talk", p, re.I):
        return "Syndicated loan"
    return "Private"


# ----------------------------------------------------------------------------
# Ratings: ccir.io's "Rated GPU-collateral financings" table (the one rating source).
# Each ccir row is matched to amcompute deals by borrower, date (+/- 75 days) and size
# (+/- 12%); a "package" row (e.g. IREN's USPP + DDTL) matches the same-day deals whose
# sizes add up to it.
# ----------------------------------------------------------------------------
CCIR_URL = "https://ccir.io/credit"
RATED_RE = re.compile(
    r'<tr data-orig="\d+" data-rank="\d+" data-date="([^"]*)" data-issuer="([^"]*)" data-name="([^"]*)" '
    r'data-size="([^"]*)" data-offtaker="([^"]*)"[^>]*>\s*<td class="grade[^"]*">([^<]*)</td>'
)
STOP_WORDS = {"inc", "limited", "ltd", "corp", "corporation", "group", "nv", "plc", "the"}


def norm_issuer(s: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split()
    return " ".join(w for w in words if w not in STOP_WORDS)


def fetch_ratings() -> list[dict]:
    import html as html_mod
    page = fetch(CCIR_URL)
    out = []
    for m in RATED_RE.finditer(page):
        date, issuer, name, size, offtaker, grade = (html_mod.unescape(x).strip() for x in m.groups())
        out.append({"date": date, "issuer": issuer, "name": name, "size_musd": float(size), "offtaker": offtaker, "grade": grade})
    return out


def match_ratings(deals: list[dict], ratings: list[dict]) -> list[str]:
    """Attach rating / rating_detail / rating_source_url to matching deals. Returns log lines."""
    log = []
    for d in deals:
        d.setdefault("rating", None)
        d.setdefault("rating_detail", None)
        d.setdefault("rating_source_url", None)
    for r in ratings:
        rd = datetime.strptime(r["date"], "%Y-%m-%d")
        cands = []
        for d in deals:
            if not d["date"] or d["amount_musd"] is None or norm_issuer(d["borrower"]) != norm_issuer(r["issuer"]):
                continue
            if abs((datetime.strptime(d["date"], "%Y-%m-%d") - rd).days) <= 75:
                cands.append(d)
        hits = [d for d in cands if abs(d["amount_musd"] - r["size_musd"]) / r["size_musd"] <= 0.12]
        if len(hits) > 1:  # keep the single best: closest size, then matching instrument word, then closest date
            name = r["name"].lower()
            def score(d):
                size_dev = abs(d["amount_musd"] - r["size_musd"]) / r["size_musd"]
                kind = d["type"].lower().replace(" ", "")
                type_ok = ("ddtl" in name and kind == "ddtl") or ("term loan" in name and kind == "termloan") or ("notes" in name and "notes" in kind)
                days = abs((datetime.strptime(d["date"], "%Y-%m-%d") - rd).days)
                return (round(size_dev, 2), 0 if type_ok else 1, days)
            hits = [min(hits, key=score)]
        if not hits:  # package: same-day deals that add up to the rated size
            by_date: dict[str, list[dict]] = {}
            for d in cands:
                by_date.setdefault(d["date"], []).append(d)
            for grp in by_date.values():
                if len(grp) > 1 and abs(sum(x["amount_musd"] for x in grp) - r["size_musd"]) / r["size_musd"] <= 0.05:
                    hits = grp
                    break
        for d in hits:
            d["rating"] = r["grade"]
            d["rating_detail"] = f"{r['name']} · on record {r['date']} · offtaker: {r['offtaker']}"
            d["rating_source_url"] = CCIR_URL + "#rated"
        tag = ", ".join(f"{d['date_text']} {d['amount_text']} {d['type']}" for d in hits) or "NO MATCH"
        log.append(f"   {r['grade'][:34]:<34} {r['issuer']:<16} {r['name'][:26]:<26} ${r['size_musd']/1000:.2f}B -> {tag}")
    return log


PCT_RE = re.compile(r"\d+(?:\.\d+)?%")
YEAR_RE = re.compile(r"\b(20[2-4]\d)\b")


TRANCHE_RE = re.compile(r"(\d+(?:\.\d+)?)%\s*(?:fixed|coupon)?\s*(?:\(\s*(20\d\d)[^)]*\))?")
TYPE_SHORT = {"Senior notes": "senior notes", "Convertible notes": "convertible notes", "Term loan": "term loan", "DDTL": "term loan"}


def tranches_for(deal: dict) -> list[dict]:
    """One traded deal -> one or more bond tranches (e.g. Nebius '0.50% (2030) and 4.50% (2034)')."""
    p = deal["pricing"] or ""
    found = []
    for m in TRANCHE_RE.finditer(p):
        cpn = float(m.group(1))
        if cpn > 20 or re.search(re.escape(m.group(0)) + r"\s*floor", p):
            continue  # accretion / issue-price figures, SOFR floors
        found.append((m.group(1), m.group(2)))
    if deal["type"] in ("Term loan", "DDTL"):
        found = found[:1]  # loans: one tranche, the margin isn't a coupon anyway
    if not found:
        found = [(None, None)]
    out = []
    for i, (cpn, yr) in enumerate(found, 1):
        parts = [deal["borrower"]]
        if cpn and deal["type"] not in ("Term loan", "DDTL"):
            parts.append(f"{cpn}%")
        parts.append(TYPE_SHORT.get(deal["type"], deal["type"].lower()))
        if yr:
            parts.append(f"due {yr}")
        when = deal["date_text"] if deal["date_precision"] != "day" else deal["date_text"].split(",")[0].rsplit(" ", 1)[0] + " " + deal["date_text"][-4:]
        desc = " ".join(parts) + f" ({deal['amount_text']} deal, {when})"
        out.append({
            "bond_id": f"{deal['id']}-t{i}" if len(found) > 1 else deal["id"],
            "deal_id": deal["id"],
            "date": deal["date"],
            "borrower": deal["borrower"],
            "tranche": desc,
            "type": deal["type"],
            "market": deal["market"],
            "coupon": cpn or "",
            "maturity_year": yr or "",
            "deal_amount_text": deal["amount_text"],
            "deal_amount_musd": deal["amount_musd"] if deal["amount_musd"] is not None else "",
            "pricing": deal["pricing"] or "",
            "source_url": deal["source_url"] or "",
        })
    return out


BOND_KEEP = ["isin", "notes"]  # columns you fill by hand; preserved across runs


def write_bonds_csv(deals: list[dict]) -> list[dict]:
    """data/bonds.csv: one row per traded tranche, with a blank ISIN column to fill in once."""
    path = DATA / "bonds.csv"
    kept: dict[str, dict] = {}
    if path.exists():
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                kept[row.get("bond_id", "")] = {k: row.get(k, "") for k in BOND_KEEP}
    bonds: list[dict] = []
    for d in deals:
        if d["market"] in ("Traded bond", "Syndicated loan"):
            for b in tranches_for(d):
                b.update(kept.get(b["bond_id"], {k: "" for k in BOND_KEEP}))
                bonds.append(b)
    cols = ["bond_id", "date", "borrower", "tranche", "type", "market", "coupon", "maturity_year", "isin", "notes", "deal_amount_text", "deal_amount_musd", "pricing", "source_url", "deal_id"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(bonds)
    return bonds


def security_hints(pricing: str | None) -> tuple[str, str]:
    """Pull coupon(s) and maturity year(s) out of free-text pricing, for Bloomberg lookup."""
    p = pricing or ""
    # keep plausible coupons only: drop accretion/issue-price figures (>20%) and "0% floor" style floors
    cands = [c for c in PCT_RE.findall(p) if float(c[:-1]) <= 20 and not re.search(re.escape(c) + r"\s*floor", p)]
    coupons = "; ".join(dict.fromkeys(cands))
    years = "; ".join(dict.fromkeys(YEAR_RE.findall(p)))
    return coupons, years


def normalize(rows: list[list[dict]]) -> list[dict]:
    header = [c["text"].lower() for c in rows[0]]
    idx = {name: header.index(name) for name in ("date", "borrower", "amount", "type", "lenders", "pricing", "source")}
    deals: list[dict] = []
    seen: dict[str, int] = {}
    for r in rows[1:]:
        if len(r) < len(header):
            continue
        get = lambda k: r[idx[k]]["text"]  # noqa: E731
        date_text = get("date")
        iso, prec = parse_date(date_text)
        amount_text = get("amount")
        lenders, more = parse_lenders(get("lenders"))
        base = f"{slug(get('borrower'))}-{(iso or 'nd')[:7]}-{slug(get('type'))}-{slug(amount_text) or 'nd'}"
        n = seen.get(base, 0)
        seen[base] = n + 1
        deal_id = base if n == 0 else f"{base}-{n + 1}"
        pricing = None if get("pricing") in ("-", "") else get("pricing")
        deals.append(
            {
                "id": deal_id,
                "market": classify_market(deal_id, get("type"), pricing, lenders),
                "date": iso,
                "date_precision": prec,
                "date_text": date_text,
                "quarter": quarter_of(iso),
                "year": int(iso[:4]) if iso else None,
                "borrower": get("borrower"),
                "amount_musd": parse_amount_musd(amount_text),
                "amount_text": amount_text,
                "type": get("type"),
                "lenders": lenders,
                "lenders_more": more,
                "lenders_text": get("lenders"),
                "pricing": pricing,
                "source_name": get("source"),
                "source_url": r[idx["source"]]["href"],
            }
        )
    return deals


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    DATA.mkdir(exist_ok=True)
    HISTORY.mkdir(exist_ok=True)

    print(f"Fetching {SOURCE_URL} ...")
    html = fetch(SOURCE_URL)

    p = TableParser()
    p.feed(html)
    target = next((t for t in p.tables if t and t[0] and t[0][0]["text"].strip().lower() == "date"), None)
    if target is None:
        print("ERROR: could not find the financings table (page layout may have changed).", file=sys.stderr)
        return 1

    deals = normalize(target)
    upstream_updated = None
    m = re.search(r"Updated\s*(?:<!--\s*-->)?\s*([A-Z][a-z]+ \d{1,2}, \d{4})", html)
    if m:
        upstream_updated = m.group(1)

    # Preserve first_seen across runs and report changes.
    prev_path = DATA / "deals.json"
    prev: dict[str, dict] = {}
    if prev_path.exists():
        try:
            prev = {d["id"]: d for d in json.loads(prev_path.read_text(encoding="utf-8"))["deals"]}
        except Exception:
            prev = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for d in deals:
        d["first_seen"] = prev.get(d["id"], {}).get("first_seen", today)
    new_ids = [d["id"] for d in deals if d["id"] not in prev]
    gone_ids = [i for i in prev if i not in {d["id"] for d in deals}]

    # Ratings from ccir.io; if the fetch fails, keep whatever the previous run had.
    try:
        ratings = fetch_ratings()
        if not ratings:
            raise ValueError("no rated rows found (ccir page layout may have changed)")
        rating_log = match_ratings(deals, ratings)
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: ratings not refreshed ({e}); keeping previous ratings", file=sys.stderr)
        ratings, rating_log = [], []
        for d in deals:
            p = prev.get(d["id"], {})
            d["rating"], d["rating_detail"], d["rating_source_url"] = p.get("rating"), p.get("rating_detail"), p.get("rating_source_url")

    # Per-tranche bond list (data/bonds.csv) and the Bloomberg IDs on each traded deal.
    bonds = write_bonds_csv(deals)
    ids_by_deal: dict[str, list[str]] = {}
    for b in bonds:
        ids_by_deal.setdefault(b["deal_id"], [])
        if b["isin"].strip():
            ids_by_deal[b["deal_id"]].append(b["isin"].strip())
    for d in deals:
        if d["market"] == "Private":
            d["bbg_ids"] = None            # not applicable
        else:
            d["bbg_ids"] = sorted(set(ids_by_deal.get(d["id"], [])))  # [] = traded but no ID found (NA)

    out = {
        "meta": {
            "source_url": SOURCE_URL,
            "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "upstream_updated": upstream_updated,
            "deal_count": len(deals),
            "total_musd": round(sum(d["amount_musd"] or 0 for d in deals), 1),
            "new_since_last_run": new_ids,
            "removed_since_last_run": gone_ids,
        },
        "deals": deals,
    }

    text = json.dumps(out, indent=2, ensure_ascii=False)
    prev_path.write_text(text, encoding="utf-8")
    (HISTORY / f"{today}.json").write_text(text, encoding="utf-8")
    (DATA / "deals.js").write_text("window.DEALS = " + text + ";\n", encoding="utf-8")

    with (DATA / "deals.csv").open("w", newline="", encoding="utf-8") as f:
        cols = ["date", "date_precision", "quarter", "borrower", "amount_musd", "amount_text", "type", "market", "rating", "rating_detail", "lenders_text", "pricing", "source_name", "source_url", "first_seen", "id"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(deals)

    # Traded securities: the Bloomberg handoff list. Identifier columns you fill in by
    # hand (isin, figi, bbg_ticker, notes) are preserved across runs, keyed by deal id.
    ts_path = DATA / "traded_securities.csv"
    keep_cols = ["isin", "figi", "bbg_ticker", "notes"]
    kept: dict[str, dict] = {}
    if ts_path.exists():
        with ts_path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                kept[row.get("id", "")] = {k: row.get(k, "") for k in keep_cols}
    traded = [d for d in deals if d["market"] in ("Traded bond", "Syndicated loan")]
    with ts_path.open("w", newline="", encoding="utf-8") as f:
        cols = ["date", "borrower", "type", "market", "amount_text", "amount_musd", "coupons", "maturity_years", "pricing", "source_url", "id"] + keep_cols
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for d in traded:
            coupons, years = security_hints(d["pricing"])
            row = {k: d.get(k, "") for k in cols if k in d}
            row.update({"coupons": coupons, "maturity_years": years})
            row.update(kept.get(d["id"], {k: "" for k in keep_cols}))
            w.writerow(row)
    n_bond = sum(1 for d in traded if d["market"] == "Traded bond")
    print(f"  market: {n_bond} traded bonds, {len(traded) - n_bond} syndicated loans, {len(deals) - len(traded)} private")
    n_isin = sum(1 for b in bonds if b["isin"].strip())
    print(f"  bonds.csv: {len(bonds)} tranches, {n_isin} with an ISIN filled in")
    if ratings:
        print(f"  ratings (ccir.io): {len(ratings)} rated financings -> {sum(1 for d in deals if d['rating'])} deals tagged")
        for line in rating_log:
            print(line)

    print(f"OK: {len(deals)} deals, ${out['meta']['total_musd']/1000:,.1f}B (announced amounts), upstream updated {upstream_updated}")
    if prev:
        print(f"  new since last run: {len(new_ids)}  removed: {len(gone_ids)}")
        for i in new_ids:
            d = next(x for x in deals if x["id"] == i)
            print(f"   + {d['date_text']:>13}  {d['borrower']:<40} {d['amount_text']:>7}  {d['type']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
