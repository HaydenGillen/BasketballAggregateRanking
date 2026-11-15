#!/usr/bin/env python3
"""
rank_blend.py - average KenPom, Torvik and EvanMiya rankings into one HTML page.

Each source can be a saved HTML file (File > Save Page As, or view-source and save)
or a URL. With no arguments it tries to fetch all three live.

  python rank_blend.py
  python rank_blend.py --kenpom kenpom.html --torvik torvik.html
  python rank_blend.py --kenpom kenpom.html --skip evanmiya --out blend.html

KenPom is subscriber-only. To fetch it live, set KENPOM_EMAIL and KENPOM_PASSWORD
in your environment. If login or fetching fails (Torvik and EvanMiya sometimes
block scripts or build their tables with JavaScript), save the page from your
browser and pass the file path instead.
"""
import argparse
import csv
import datetime
import io
import html
import http.cookiejar
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser
from pathlib import Path

NOTES = []  # shown under the page title, e.g. when a source comes from a saved copy
SOURCES = ["kenpom", "torvik", "evanmiya"]
LABELS = {"kenpom": "KenPom", "torvik": "Torvik", "evanmiya": "EvanMiya"}


def season_year():
    today = datetime.date.today()
    return today.year + 1 if today.month >= 8 else today.year


def default_url(name):
    return {
        "kenpom": "https://kenpom.com/",
        "torvik": f"https://barttorvik.com/trank.php?year={season_year()}",
        "evanmiya": "https://evanmiya.com/team-ratings",
    }[name]


def candidate_urls(name):
    """Addresses to try in order. Torvik is tried as the page, then as JSON, then as its CSV data file."""
    y = season_year()
    if name == "torvik":
        return [f"https://barttorvik.com/trank.php?year={y}",
                f"https://barttorvik.com/trank.php?year={y}&json=1",
                f"https://barttorvik.com/{y}_team_results.csv"]
    return [default_url(name)]


# ---------------------------------------------------------------- fetching

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def make_opener():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", UA), ("Accept", "text/html,*/*")]
    return opener


def kenpom_login(opener):
    email, pw = os.environ.get("KENPOM_EMAIL"), os.environ.get("KENPOM_PASSWORD")
    if not (email and pw):
        return False
    data = urllib.parse.urlencode({"email": email, "password": pw, "submit": "Login!"}).encode()
    opener.open("https://kenpom.com/handlers/login_handler.php", data, timeout=30).read()
    return True


def load(name, spec):
    """Return HTML text for a source given a path or URL."""
    if spec and not re.match(r"https?://", spec):
        return Path(spec).read_text(encoding="utf-8", errors="replace")
    url = spec or default_url(name)
    opener = make_opener()
    if name == "kenpom":
        kenpom_login(opener)  # optional; the public page already lists every team
    data = None
    if name == "torvik" and "trank.php" in url and "json" not in url:
        # Torvik serves a "verifying your browser" page unless this form field is posted.
        data = b"js_test_submitted=1"
    return opener.open(url, data, timeout=30).read().decode("utf-8", errors="replace")


def set_page_size(page, size):
    """Set a 'rows per page' control to `size` (or the largest option it offers)."""
    target = str(size)
    try:  # EvanMiya's table: <select class="...pageSizeSelect"> with options 5 ... 1000
        direct = page.locator('select[class*="pageSizeSelect"]')
        if direct.count():
            direct.first.select_option(value=target)
            return size
    except Exception:
        pass
    best = None  # (number, select element, option value)
    for sel in page.query_selector_all("select"):
        opts = sel.eval_on_selector_all("option", "els => els.map(e => [e.value, e.textContent.trim()])")
        parsed = []
        for value, label in opts:
            m = re.match(r"\s*(\d+)\b", label) or re.match(r"\s*(\d+)\b", value)
            if m:
                parsed.append((int(m.group(1)), value))
        if len(parsed) < 3 or len(parsed) < len(opts) - 1:
            continue  # not a rows-per-page dropdown
        top = max(parsed)
        if best is None or top[0] > best[0]:
            best = (top[0], sel, top[1])
        for n, value in parsed:
            if n == size:
                sel.select_option(value=value)
                return size
    if best:
        best[1].select_option(value=best[2])
        return best[0]
    try:  # custom (non-<select>) dropdown: click the "10 rows" control, then the 1000 option
        page.get_by_text(re.compile(r"^\d+\s*rows?$", re.I)).first.click(timeout=3000)
        page.get_by_text(re.compile(rf"^{target}(\s*rows?)?$", re.I)).first.click(timeout=3000)
        return size
    except Exception:
        return None


def click_next(page):
    """Click a 'Next' pagination control. Returns False when there is none or it is disabled."""
    for loc in (page.get_by_role("button", name=re.compile(r"^\s*next\b", re.I)),
                page.get_by_text(re.compile(r"^\s*next\s*[>›»]?\s*$", re.I))):
        if loc.count() == 0:
            continue
        el = loc.first
        try:
            cls = (el.get_attribute("class") or "").lower()
            if el.is_disabled() or el.get_attribute("aria-disabled") == "true" or "disabled" in cls:
                return False
            el.click(timeout=3000)
            return True
        except Exception:
            return False
    return False


def render_js(url, page_size=None):
    """Render a JavaScript-built page with Playwright. Returns a list of HTML snapshots
    (one per page of results), or None if Playwright isn't installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    snaps = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        jsons = []  # data requests the page makes; the full table often arrives in one of them
        page.on("response", lambda r: jsons.append(r) if "json" in (r.headers.get("content-type") or "") else None)
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)
        if page_size:
            chosen = set_page_size(page, page_size)
            if chosen is None:
                print("  couldn't find the rows-per-page control; paging through with Next instead.")
                try:
                    hint = page.evaluate(
                        "() => { const el = [...document.querySelectorAll('*')].find(e => "
                        "/\\d+\\s*rows/i.test(e.textContent) && e.children.length < 6 && e.textContent.length < 80);"
                        " return el ? el.outerHTML.slice(0, 400) : '' }")
                    if hint:
                        print("  nearest element mentioning rows:", hint)
                except Exception:
                    pass
            elif chosen != page_size:
                print(f"  rows per page: {page_size} isn't offered, used {chosen}.")
            else:
                print(f"  rows per page set to {chosen}.")
            page.wait_for_timeout(3000)
        snaps.append(page.content())
        for _ in range(100):  # walk the remaining pages with Next
            if not click_next(page):
                break
            snap = snaps[-1]
            for _ in range(20):  # wait up to ~4s for the table to change
                page.wait_for_timeout(200)
                snap = page.content()
                if snap != snaps[-1]:
                    break
            if snap == snaps[-1]:
                break
            snaps.append(snap)
        print(f"  read {len(snaps)} page(s) of results.")
        for r in jsons:
            try:
                body = r.text()
            except Exception:
                continue
            if len(body) > 2000:
                print(f"  data request: {r.url[:150]} ({len(body):,} bytes)")
                snaps.append(body)
        browser.close()
    return snaps


# ----------------------------------------------------------------- parsing

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.stack = [], []
        self.cur, self.in_a, self.skip = None, False, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "table":
            self.stack.append([])
        elif not self.stack:
            return
        elif tag == "tr":
            self.stack[-1].append([])
        elif tag in ("td", "th"):
            if not self.stack[-1]:
                self.stack[-1].append([])
            self.cur = {"text": "", "link": "", "th": tag == "th"}
            self.stack[-1][-1].append(self.cur)
        elif tag == "a":
            self.in_a = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag == "table" and self.stack:
            self.tables.append(self.stack.pop())
            self.cur = None
        elif tag in ("td", "th"):
            self.cur = None
        elif tag == "a":
            self.in_a = False

    def handle_data(self, data):
        if self.cur is not None and not self.skip:
            self.cur["text"] += data
            if self.in_a:
                self.cur["link"] += data


def clean(s):
    return re.sub(r"\s+", " ", s).strip()


def strip_game(name):
    """Drop upcoming-game text Torvik appends, e.g. 'Texas Tech (H) 345 Stonehill' -> 'Texas Tech'."""
    name = re.sub(r"\s*\((?:H|A|N)\)\s*\d*.*$", "", name)
    # past results look like 'George Mason vs. 206 Bradley (lost)'
    name = re.sub(r"\s+(?:vs\.?|at|@)\s+\d+\b.*$", "", name, flags=re.I)
    return name.strip()


def is_num(s):
    return bool(re.fullmatch(r"[-+]?\d*\.?\d+%?|\d+-\d+", s))


def rows_from_table(table):
    out = {}
    for row in table:
        cells = [c for c in row if not c["th"]] or row
        if len(cells) < 2:
            continue
        m = re.match(r"\s*#?(\d{1,3})\b", cells[0]["text"])
        if not m:
            continue
        rank = int(m.group(1))
        team = ""
        for c in cells[1:]:
            txt = clean(c["link"]) or clean(c["text"])
            if re.search(r"[A-Za-z]", txt) and not is_num(txt):
                team = strip_game(txt)
                break
        key = normalize(team)
        if key and key not in out:
            out[key] = (rank, team)
    return out


def parse_html(text):
    p = TableParser()
    p.feed(text)
    best = {}
    for t in p.tables:
        rows = rows_from_table(t)
        if len(rows) > len(best):
            best = rows
    return best


def parse_json(text):
    """Torvik-style JSON: a list of rows like [rank, team, conf, ...]."""
    t = text.strip()
    if not t.startswith("["):
        return {}
    try:
        arr = json.loads(t)
    except ValueError:
        return {}
    out = {}
    for row in arr:
        if isinstance(row, list) and len(row) > 1 and isinstance(row[1], str) \
                and re.fullmatch(r"\d{1,3}", str(row[0]).strip()):
            out.setdefault(normalize(strip_game(row[1])), (int(row[0]), strip_game(row[1])))
    if not out:  # no rank column: use row order, team name first
        for i, row in enumerate(arr, 1):
            if isinstance(row, list) and row and isinstance(row[0], str) and re.search(r"[A-Za-z]", row[0]):
                out.setdefault(normalize(row[0]), (i, row[0]))
    return out


def _record_lists(obj, found, depth=0):
    if depth > 8:
        return
    if isinstance(obj, list):
        if len(obj) >= 100 and all(isinstance(x, dict) for x in obj[:20]):
            found.append(obj)
        for x in obj[:3]:
            _record_lists(x, found, depth + 1)
    elif isinstance(obj, dict):
        for v in obj.values():
            _record_lists(v, found, depth + 1)


def _records_to_rows(recs):
    """Turn a list of dicts into {key: (rank, team)} by guessing which fields are team and rank."""
    keys = list(recs[0].keys())
    strs = [k for k in keys if all(isinstance(r.get(k), str) for r in recs[:20])]
    team_key = next((k for k in strs if "team" in k.lower()), None) or \
        next((k for k in strs if "name" in k.lower()), None)
    if not team_key:
        return {}
    rank_key = next((k for k in keys if "rank" in k.lower() and
                     all(re.fullmatch(r"\d+", str(r.get(k, "")).strip()) for r in recs[:20])), None)
    ordered = recs
    if not rank_key:  # no rank field: order by the biggest-looking rating, best first
        rate_key = next((k for k in keys if re.search(r"net|rating|rate|rel", k, re.I) and
                         all(isinstance(r.get(k), (int, float)) for r in recs[:20])), None)
        if not rate_key:
            return {}
        ordered = sorted(recs, key=lambda r: -(r.get(rate_key) or 0))
    out = {}
    for i, r in enumerate(ordered, 1):
        name = strip_game(str(r.get(team_key, "")))
        rank = int(str(r[rank_key]).strip()) if rank_key else i
        if name:
            out.setdefault(normalize(name), (rank, name))
    return out


def parse_embedded(text):
    """JSON data inside a page (script tags) or a raw JSON response: a big list of team records."""
    chunks = []
    t = text.strip()
    if t[:1] in "[{":
        chunks.append(t)
    chunks += re.findall(r"<script[^>]*>\s*([\[{].*?)</script>", text, re.S)
    for chunk in chunks:
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        found = []
        _record_lists(obj, found)
        for recs in sorted(found, key=len, reverse=True):
            rows = _records_to_rows(recs)
            if len(rows) >= 100:
                return rows
    return {}


def parse_flight(text):
    """Next.js pages embed their data in script tags as escaped JSON, e.g.
    {\\"rank\\":1,\\"team\\":\\"Florida\\",...}. EvanMiya's team ratings page carries all 365 teams this way."""
    out = {}
    pat = r'\\?"rank\\?":(\d+),\\?"team\\?":\\?"((?:[^\\"]|\\u[0-9a-fA-F]{4})*?)\\?"'
    for rank, name in re.findall(pat, text):
        try:
            name = json.loads('"' + name + '"')  # turns & back into &
        except ValueError:
            pass
        name = strip_game(name)
        out.setdefault(normalize(name), (int(rank), name))
    return out


class LinkRankParser(HTMLParser):
    """Div-based grids (EvanMiya): a rank number, then a link to ...team-breakdown?team=NAME."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.num, self.team_num = [], None, None
        self.in_team, self.href_name, self.buf, self.skip = False, "", "", 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            m = re.search(r"team-breakdown[^\"']*[?&]team=([^&#]+)", href)
            self.in_team = bool(m)
            self.href_name = urllib.parse.unquote_plus(m.group(1)) if m else ""
            self.buf = ""
            self.team_num, self.num = self.num, None  # any link ends the current rank

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.in_team:
            text = clean(self.buf)
            # Use the name from the URL; link text can carry extras like "Texas Tech (H) 345 Stonehill".
            plain = not text or normalize(text) == normalize(self.href_name)
            self.rows.append((self.team_num, self.href_name, plain))
            self.in_team = False

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_team:
            self.buf += data
        elif re.fullmatch(r"#?\d{1,3}", data.strip()):
            self.num = int(data.strip().lstrip("#"))


def parse_links(text):
    p = LinkRankParser()
    p.feed(text)
    out = {}
    # Plain team links first; links with game text (schedules, matchups) only fill gaps.
    for want_plain in (True, False):
        for n, t, plain in p.rows:
            if n is not None and plain == want_plain:
                out.setdefault(normalize(t), (n, t))
    if not out:  # no rank cells found: use the order the teams appear in
        for _, t, plain in p.rows:
            if plain:
                out.setdefault(normalize(t), (len(out) + 1, t))
    return out


def parse_csv(text):
    """A downloaded CSV with a header row containing a team column (and usually a rank column)."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if len(rows) < 2:
        return {}
    header = [h.strip().lower() for h in rows[0]]
    team_i = next((i for i, h in enumerate(header) if "team" in h), None)
    if team_i is None:
        return {}
    rank_i = next((i for i, h in enumerate(header) if "rank" in h or h in ("rk", "#")), None)
    out = {}
    for n, row in enumerate(rows[1:], 1):
        if len(row) <= team_i or not row[team_i].strip():
            continue
        name = strip_game(row[team_i].strip())
        rank = n
        if rank_i is not None and len(row) > rank_i and re.fullmatch(r"\d+", row[rank_i].strip()):
            rank = int(row[rank_i].strip())
        out.setdefault(normalize(name), (rank, name))
    return out


def parse_text(text):
    """Plain text copied from a rendered table: one 'rank  team ...' per line."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*#?(\d{1,3})[.)\s,\t-]+(.+)$", line)
        if not m:
            continue
        rest, team = m.group(2), ""
        cells = [c.strip() for c in re.split(r"\t|\|", rest) if c.strip()]
        if len(cells) > 1:
            team = next((c for c in cells if re.search(r"[A-Za-z]", c) and not is_num(c)), "")
        else:
            toks = []
            for tok in rest.split():
                if is_num(tok):
                    break
                toks.append(tok)
            team = " ".join(toks)
        team = strip_game(team)
        key = normalize(team)
        if key and key not in out:
            out[key] = (int(m.group(1)), team)
    return out


def parse_any(text):
    return (parse_html(text) or parse_json(text) or parse_embedded(text) or parse_flight(text)
            or parse_links(text) or parse_csv(text) or parse_text(text))


ALIAS = {
    "uconn": "connecticut", "miami fla": "miami fl", "miami florida": "miami fl",
    "ole miss": "mississippi", "unc": "north carolina", "pitt": "pittsburgh",
    "nc state": "north carolina state", "lsu": "louisiana state",
    "usc": "southern california", "smu": "southern methodist", "ucf": "central florida",
    "vcu": "virginia commonwealth", "byu": "brigham young",
    # names the three sites spell differently
    "csun": "cal state northridge", "fiu": "florida international", "liu": "long island",
    "umkc": "kansas city", "missouri kansas city": "kansas city",
    "siue": "siu edwardsville", "louisiana lafayette": "louisiana",
    "arkansas little rock": "little rock", "utrgv": "texas rio grande valley",
    "ut rio grande valley": "texas rio grande valley", "southern miss": "southern mississippi",
    "saint thomas mn": "saint thomas", "cal baptist": "california baptist",
    "usc upstate": "south carolina upstate", "loyola md": "loyola maryland",
    "nc central": "north carolina central", "uic": "illinois chicago",
}


def normalize(name):
    s = name.lower().replace("&", "and")
    s = re.sub(r"[.'’()]", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    toks = re.sub(r"\s+", " ", s).strip().split()
    for i, t in enumerate(toks):
        if t == "st":  # St. John's -> saint, Michigan St. / Cal St. Fullerton -> state
            toks[i] = "saint" if i == 0 or toks[i - 1] == "mount" else "state"
    s = " ".join(toks)
    return ALIAS.get(s, s)


_STOP = {"of", "the", "and", "college", "university"}


def _tok_eq(a, b):
    return a == b or (len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)))


def _subset(a, b):
    return all(any(_tok_eq(x, y) for y in b) for x in a)


def similar_names(ka, kb):
    """True when one name's words all appear in the other: 'mcneese' / 'mcneese state'."""
    a = [t for t in ka.split() if t not in _STOP]
    b = [t for t in kb.split() if t not in _STOP]
    return bool(a and b) and (_subset(a, b) or _subset(b, a))


# --------------------------------------------------------------- combining

def merge_similar(teams, nsrc, max_gap=40):
    """Join entries that are the same team under different spellings: they come from different
    sources, their names overlap, and their ranks are close."""
    def mean(t):
        return sum(t["ranks"].values()) / len(t["ranks"])

    while True:
        keys = [k for k, t in teams.items() if len(t["ranks"]) < nsrc]
        best = None
        for i, ka in enumerate(keys):
            for kb in keys[i + 1:]:
                a, b = teams[ka], teams[kb]
                if set(a["ranks"]) & set(b["ranks"]) or not similar_names(ka, kb):
                    continue
                gap = abs(mean(a) - mean(b))
                if gap <= max_gap and (best is None or gap < best[0]):
                    best = (gap, ka, kb)
        if not best:
            return
        _, ka, kb = best
        a, b = teams[ka], teams.pop(kb)
        a["ranks"].update(b["ranks"])
        if len(b["name"]) > len(a["name"]):
            a["name"] = b["name"]


def blend(data):
    teams = {}
    for src, rows in data.items():
        for key, (rank, name) in rows.items():
            t = teams.setdefault(key, {"name": name, "ranks": {}})
            t["ranks"][src] = rank
    merge_similar(teams, len(data))
    out = []
    for t in teams.values():
        rs = list(t["ranks"].values())
        out.append({
            "name": t["name"], "ranks": t["ranks"], "n": len(rs),
            "avg": sum(rs) / len(rs), "spread": max(rs) - min(rs),
        })
    out.sort(key=lambda x: (x["avg"], -x["n"], x["name"]))
    place, prev = 0, None
    for i, x in enumerate(out, 1):
        if x["avg"] != prev:
            place, prev = i, x["avg"]
        x["place"] = place
    return out


# ------------------------------------------------------------------ output

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rank Blend</title>
<style>
:root{--bg:#f3f5f8;--surface:#fff;--fg:#14202e;--muted:#5b6b7d;--line:#d9e0e8;--accent:#0b5fd0;--warn:#a15c00;--chip:#e7edf5}
@media (prefers-color-scheme:dark){:root{--bg:#0f1620;--surface:#18222f;--fg:#e8eef5;--muted:#93a3b5;--line:#2a3647;--accent:#5aa2ff;--warn:#e0a24a;--chip:#223044}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;padding:28px 16px 48px}
.wrap{max-width:1000px;margin:0 auto}
h1{font-size:30px;margin:0 0 4px}
p{color:var(--muted);margin:0 0 16px}
.box{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;min-width:560px}
th,td{padding:9px 12px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);cursor:pointer;user-select:none}
th:nth-child(2),td:nth-child(2){text-align:left;white-space:normal}
tbody tr:last-child td{border-bottom:0}
td.rk{font-weight:700;color:var(--accent)}td.avg{font-weight:600}td.miss{color:var(--muted)}
.pill{font-size:11px;padding:1px 7px;border-radius:999px;background:var(--chip);color:var(--warn)}
</style></head><body><div class="wrap">
<h1>Rank Blend</h1>
<p>__SUMMARY__ Click a column to sort.</p>
<div class="box"><table id="t"><thead><tr><th>#</th><th>Team</th>__HEADERS__<th>Avg rank</th><th>Spread</th></tr></thead>
<tbody>__ROWS__</tbody></table></div></div>
<script>
(function(){
  var t=document.getElementById("t"),tb=t.tBodies[0],dir={};
  t.tHead.rows[0].addEventListener("click",function(e){
    var th=e.target.closest("th");if(!th)return;
    var i=th.cellIndex,d=dir[i]=!dir[i];
    var rows=[].slice.call(tb.rows);
    function v(r){var s=r.cells[i].textContent.trim(),n=parseFloat(s);return i===1?s.toLowerCase():(isNaN(n)?1e9:n)}
    rows.sort(function(a,b){var x=v(a),y=v(b);return (x<y?-1:x>y?1:0)*(d?1:-1)});
    rows.forEach(function(r){tb.appendChild(r)});
  });
})();
</script></body></html>
"""


def render(blended, sources):
    headers = "".join(f"<th>{LABELS[s]}</th>" for s in sources)
    rows = []
    for x in blended:
        cells = "".join(
            f"<td>{x['ranks'][s]}</td>" if s in x["ranks"] else '<td class="miss">&ndash;</td>'
            for s in sources
        )
        tag = f' <span class="pill">{x["n"]} of {len(sources)}</span>' if x["n"] < len(sources) else ""
        spread = x["spread"] if x["n"] > 1 else "&ndash;"
        rows.append(
            f'<tr><td class="rk">{x["place"]}</td><td>{html.escape(x["name"])}{tag}</td>{cells}'
            f'<td class="avg">{x["avg"]:.1f}</td><td>{spread}</td></tr>'
        )
    full = sum(1 for x in blended if x["n"] == len(sources))
    summary = (f"{len(blended)} teams from {', '.join(LABELS[s] for s in sources)}; "
               f"{full} appear in every list. Generated {datetime.datetime.now():%b %d, %Y %I:%M %p}."
               + "".join(" " + n for n in NOTES))
    return (PAGE.replace("__SUMMARY__", html.escape(summary))
                .replace("__HEADERS__", headers)
                .replace("__ROWS__", "\n".join(rows)))


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Average KenPom, Torvik and EvanMiya rankings.")
    for s in SOURCES:
        ap.add_argument(f"--{s}", metavar="PATH_OR_URL", help=f"{LABELS[s]} page (default: {default_url(s)})")
    ap.add_argument("--skip", nargs="*", choices=SOURCES, default=[], help="sources to leave out")
    ap.add_argument("--out", default=None,
                    help="output file (default: rank_blend.html here, or in your Downloads folder when run as an app)")
    ap.add_argument("--no-open", action="store_true", help="write the file but don't open a browser")
    args = ap.parse_args()

    data = {}
    for s in SOURCES:
        if s in args.skip:
            continue
        print(f"{LABELS[s]}: loading...")
        spec = getattr(args, s)
        rows = {}
        is_file = bool(spec) and not re.match(r"https?://", spec)
        for url in ([spec] if spec else candidate_urls(s)):
            try:
                text = load(s, url)
            except Exception as e:  # network errors, blocked requests, missing files
                print(f"  {url}\n    failed: {e}", file=sys.stderr)
                continue
            rows = parse_any(text)
            if rows:
                break
            print(f"  {url}\n    no rankings found. The response began: {clean(text[:150])!r}", file=sys.stderr)
        # EvanMiya's raw page only has the first rows, so use a browser unless we already got everything.
        if not is_file and (not rows or (s == "evanmiya" and len(rows) < 300)):
            print("  trying a rendered browser (needs Playwright)...")
            snaps = render_js(spec or default_url(s), page_size=1000 if s == "evanmiya" else None)
            if snaps is None:
                print("  Playwright isn't installed. Either run: pip install playwright && "
                      "playwright install chromium\n  or open the page in your browser, "
                      "select the table, copy it into a .txt file and pass that file.", file=sys.stderr)
            else:
                for snap in snaps:
                    for key, val in parse_any(snap).items():
                        rows.setdefault(key, val)
        if not rows and not spec:
            # Live fetch failed (sites sometimes block GitHub's servers). Use a copy saved in data/.
            for ext in ("html", "csv", "json", "txt"):
                saved = Path(__file__).resolve().parent / "data" / f"{s}.{ext}"
                if saved.is_file():
                    rows = parse_any(saved.read_text(encoding="utf-8", errors="replace"))
                    if rows:
                        day = datetime.date.fromtimestamp(saved.stat().st_mtime)
                        NOTES.append(f"{LABELS[s]} comes from a saved copy dated {day:%b %d, %Y}.")
                        print(f"  live fetch failed, using saved copy {saved.name} ({day}).")
                        break
        if not rows:
            print("  no rankings found, skipping.", file=sys.stderr)
            continue
        if s == "evanmiya" and len(rows) < 300:
            print(f"  warning: only {len(rows)} EvanMiya teams (expected about 365). Copy the "
                  "'data request' lines above and send them to get this fixed.", file=sys.stderr)
        print(f"  {len(rows)} teams")
        data[s] = rows

    if not data:
        sys.exit("No rankings loaded, nothing to blend.")

    sources = [s for s in SOURCES if s in data]
    if args.out:
        out = Path(args.out).resolve()
    elif getattr(sys, "frozen", False):  # double-clicked app: the working folder is unpredictable
        folder = Path.home() / "Downloads"
        out = (folder if folder.is_dir() else Path.home()) / "rank_blend.html"
    else:
        out = Path("rank_blend.html").resolve()
    out.write_text(render(blend(data), sources), encoding="utf-8")
    print(f"Wrote {out}")
    if not args.no_open:
        webbrowser.open(out.as_uri())


def _run():
    frozen = getattr(sys, "frozen", False)
    try:
        main()
    except SystemExit as e:
        if frozen and e.code not in (None, 0):
            input("\nPress Enter to close...")  # keep the window open so the error can be read
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        if frozen:
            input("\nPress Enter to close...")
        sys.exit(1)


if __name__ == "__main__":
    _run()
