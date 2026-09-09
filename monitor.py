#!/usr/bin/env python3
"""
odyssey-70mm-watch
==================
Monitors AMC Lincoln Square 13 for newly released showtimes of
"The Odyssey" in IMAX 70mm and sends a push notification when new
showtimes appear.

How it works
------------
1. Fetches the theatre's server-rendered showtimes page for each date in a
   rolling window (default: next 45 days).
2. Parses every showtime button (links to /showtimes/{id}) along with its
   movie title and premium-format heading (e.g. "IMAX 70MM").
   NOTE: AMC lists Odyssey IMAX 70mm shows under BOTH the regular
   "The Odyssey" listing and the "The Odyssey - IMAX 70mm Event" listing,
   so we match by title/format regex rather than a single movie id.
3. Diffs showtime IDs against state.json. Anything unseen -> notification
   via ntfy.sh and/or Pushover, with a direct seat-selection link.

First run seeds state silently (everything currently listed is "seen") and
sends a single "monitoring started" ping so you know it's alive.

Usage
-----
    python monitor.py                # normal run (use via cron)
    python monitor.py --dry-run      # scan + diff, but don't notify
    python monitor.py --dump         # save raw HTML to debug/ for inspection
    python monitor.py --reset        # wipe state and re-seed

Config is via environment variables -- see DEFAULTS below or README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cf

# --------------------------------------------------------------------------
# Configuration (override any of these with environment variables)
# --------------------------------------------------------------------------

DEFAULTS = {
    # The theatre showtimes page to watch.
    "THEATRE_SHOWTIMES_URL": (
        "https://www.amctheatres.com/movie-theatres/"
        "new-york-city/amc-lincoln-square-13/showtimes"
    ),
    # Case-insensitive regexes. Movie matches the listing title; format
    # matches the premium-format section heading. "imax\s*70" matches
    # "IMAX 70MM" but NOT the plain non-IMAX "70mm" section. To watch both,
    # set FORMAT_PATTERN='(imax\s*70|^70\s*mm)'.
    "MOVIE_PATTERN": r"odyssey",
    "FORMAT_PATTERN": r"imax\s*70",
    # How far ahead to scan, and when to stop early (N consecutive dates
    # with zero matching listings of any format -> assume run window ended).
    "DAYS_AHEAD": "45",
    "EMPTY_STREAK_STOP": "5",
    # For far-future releases (e.g. presale batches months ahead), set the
    # date scanning should begin (YYYY-MM-DD). Empty = start today.
    # DAYS_AHEAD then counts forward from this date.
    "SCAN_START": "",
    # Short label used in notification titles, so multiple watchers on the
    # same phone are distinguishable.
    "ALERT_LABEL": "Odyssey IMAX 70mm - Lincoln Sq",
    # If auto-discovery of the per-date URL fails, set this manually, e.g.
    # "https://.../showtimes?date={date}"  ({date} -> YYYY-MM-DD)
    "DATE_URL_TEMPLATE": "",
    # Politeness delay between page fetches (seconds; jitter added).
    "REQUEST_DELAY": "0.6",
    # State + notifications
    "STATE_FILE": "state.json",
    "NTFY_SERVER": "https://ntfy.sh",
    "NTFY_TOPIC": "",           # e.g. "jimmy-odyssey-70mm-x7q2"  (keep it unguessable)
    "PUSHOVER_TOKEN": "",       # optional alternative/addition to ntfy
    "PUSHOVER_USER": "",
}


def cfg(key: str) -> str:
    return os.environ.get(key, DEFAULTS[key])


# Section headings that denote a premium format block on AMC's pages.
# (Used to recognize headings generally; your FORMAT_PATTERN then selects
# which of them you actually care about.)
FORMAT_HEADING_RE = re.compile(
    r"(imax|dolby|laser|70\s*mm|prime at amc|open caption|real\s*d|d-?box|screenx|4dx|grand screen)",
    re.I,
)

SHOWTIME_HREF_RE = re.compile(r"/showtimes/(\d+)")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*[ap]m\b", re.I)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Show:
    sid: str        # AMC showtime id -- globally unique, our dedupe key
    movie: str      # listing title, e.g. "The Odyssey - IMAX 70mm Event"
    fmt: str        # format heading, e.g. "IMAX 70MM"
    day: str        # YYYY-MM-DD of the schedule page it appeared on
    time: str       # e.g. "7:00pm" (falls back to raw label)
    status: str     # available | almost full | sold out
    url: str        # direct seat-selection link

    @property
    def pretty_day(self) -> str:
        d = date.fromisoformat(self.day)
        return d.strftime("%a %b %-d") if os.name != "nt" else d.strftime("%a %b %d")


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch(url: str, attempts: int = 3) -> str:
    """GET a page with a real-browser fingerprint, retrying with growing
    pauses if AMC's traffic protection rejects a request transiently."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            r = cf.get(
                url,
                impersonate="chrome",
                timeout=30,
                headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            if i < attempts - 1:
                wait = (2 ** i) * 3 + random.uniform(0, 3)  # ~3-6s then ~6-9s
                print(f"[warn] fetch {i+1}/{attempts} failed ({e}); retrying in {wait:.0f}s",
                      file=sys.stderr)
                time.sleep(wait)
    raise last  # type: ignore[misc]


def polite_sleep() -> None:
    base = float(cfg("REQUEST_DELAY"))
    time.sleep(base + random.uniform(0, base))


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_showtimes(html: str, day: str, base_url: str) -> list[Show]:
    """
    Linear scan of the document in DOM order:
      * an <a href="/movies/..."> sets the current movie (and resets format)
      * a heading (h1-h6) matching a premium-format label sets current format
      * an <a href="/showtimes/{id}"> is a showtime belonging to
        (current movie, current format)
    This avoids depending on class names, which AMC changes freely.
    Amenity chips like "IMAX at AMC" / "70mm" inside a section are NOT
    headings, so they don't clobber the current format.
    """
    soup = BeautifulSoup(html, "html.parser")
    shows: list[Show] = []
    movie: str | None = None
    fmt: str | None = None

    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "a",
                            "li", "span", "button", "p", "div"]):
        if el.name in ("li", "span", "button", "p", "div"):
            # Sold-out showtimes are rendered as dead chips, not links, so the
            # anchor branch never sees them. Capture them anyway (with a
            # synthetic id) so we can tell when they come back on sale.
            if not movie or not fmt or el.find("a"):
                continue
            text = el.get_text(" ", strip=True)
            if len(text) > 60 or "sold out" not in text.lower():
                continue
            tm = TIME_RE.search(text)
            if not tm:
                continue
            t = tm.group(0).lower()
            shows.append(Show(
                sid=f"so:{day}:{fmt}:{t}:{movie[:24]}",
                movie=movie, fmt=fmt, day=day, time=t,
                status="sold out", url=base_url,
            ))
            continue
        if el.name == "a":
            href = el.get("href") or ""
            if "/movies/" in href:
                title = el.get_text(" ", strip=True)
                if title:            # skip poster-image-only anchors
                    movie = title
                    fmt = None       # new movie card -> format unknown again
                continue
            m = SHOWTIME_HREF_RE.search(href)
            if m and movie and fmt:
                label = el.get_text(" ", strip=True)
                tm = TIME_RE.search(label)
                low = label.lower()
                status = (
                    "sold out" if "sold out" in low
                    else "almost full" if "almost full" in low
                    else "available"
                )
                shows.append(Show(
                    sid=m.group(1),
                    movie=movie,
                    fmt=fmt,
                    day=day,
                    time=tm.group(0).lower() if tm else label,
                    status=status,
                    url=urljoin(base_url, href.split("?")[0]),
                ))
        else:
            # Heading element. Movie-title headings contain a /movies/ link
            # (handled above via the anchor); theatre-name headings won't
            # match FORMAT_HEADING_RE.
            if el.find("a", href=re.compile("/movies/")):
                continue
            text = el.get_text(" ", strip=True)
            head = text.split(":")[0].strip()   # "IMAX 70MM: EXTRAORDINARY..." -> "IMAX 70MM"
            if head and len(head) <= 60 and FORMAT_HEADING_RE.search(head):
                fmt = head

    return shows


def dedupe(shows: list[Show]) -> list[Show]:
    seen: dict[str, Show] = {}
    for s in shows:
        seen.setdefault(s.sid, s)
    return list(seen.values())


# --------------------------------------------------------------------------
# Date-URL discovery
# --------------------------------------------------------------------------

CANDIDATE_TEMPLATES = [
    "{base}?date={date}",
    "{base}/all/{date}/{slug}/all",   # legacy AMC path style
    "{base}/{date}",
    "{base}?view-date={date}",
]


def showtime_ids(html: str) -> set[str]:
    return set(SHOWTIME_HREF_RE.findall(html))


def discover_date_template(base_url: str, base_html: str) -> str | None:
    """
    Figure out how to request a specific date's schedule.
    1) Prefer any date-bearing showtimes link present in the base page HTML.
    2) Otherwise try known URL shapes and keep the first one where two
       different dates return different showtime-id sets.
    """
    # 1) links embedded in the page (date picker), if server-rendered
    m = re.search(r'href="([^"]*showtimes[^"]*\d{4}-\d{2}-\d{2}[^"]*)"', base_html)
    if m:
        href = m.group(1)
        template = re.sub(r"\d{4}-\d{2}-\d{2}", "{date}", href)
        return urljoin(base_url, template)

    # 2) probe candidates
    slug = base_url.rstrip("/").split("/")[-2]  # e.g. amc-lincoln-square-13
    base_ids = showtime_ids(base_html)
    d1 = (date.today() + timedelta(days=1)).isoformat()
    d2 = (date.today() + timedelta(days=8)).isoformat()
    for tpl in CANDIDATE_TEMPLATES:
        template = tpl.format(base=base_url, slug=slug, date="{date}")
        try:
            polite_sleep()
            ids1 = showtime_ids(fetch(template.format(date=d1)))
            polite_sleep()
            ids2 = showtime_ids(fetch(template.format(date=d2)))
        except Exception:
            continue
        if ids1 and (ids1 != base_ids or ids2 != ids1):
            return template
    return None


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"seen": {}, "meta": {}}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def notify(title: str, body: str, click_url: str | None = None) -> None:
    # ntfy transmits the Title header as latin-1; strip emoji/non-ascii or the
    # send fails. (Tags: clapper renders the emoji on the phone instead.)
    title = title.encode("ascii", "ignore").decode().strip()
    configured = False
    sent = False
    topic = cfg("NTFY_TOPIC")
    if topic:
        configured = True
        headers = {"Title": title, "Priority": "high", "Tags": "clapper"}
        if click_url:
            headers["Click"] = click_url
        try:
            cf.post(
                f"{cfg('NTFY_SERVER').rstrip('/')}/{topic}",
                data=body.encode(),
                headers=headers,
                timeout=15,
            )
            sent = True
        except Exception as e:
            print(f"[warn] ntfy send failed: {e}", file=sys.stderr)

    if cfg("PUSHOVER_TOKEN") and cfg("PUSHOVER_USER"):
        configured = True
        try:
            cf.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": cfg("PUSHOVER_TOKEN"),
                    "user": cfg("PUSHOVER_USER"),
                    "title": title,
                    "message": body,
                    "priority": 1,
                    **({"url": click_url, "url_title": "Pick seats"} if click_url else {}),
                },
                timeout=15,
            )
            sent = True
        except Exception as e:
            print(f"[warn] pushover send failed: {e}", file=sys.stderr)

    if not configured:
        print("[info] no notifier configured (set NTFY_TOPIC and/or Pushover vars); printing only")
    elif not sent:
        print("[warn] notifier configured but send failed -- see warnings above", file=sys.stderr)
    print(f"--- {title} ---\n{body}\n")


def format_new_shows(new: list[Show]) -> str:
    lines = []
    for s in sorted(new, key=lambda s: (s.day, s.time)):
        tag = "  [SOLD OUT - refill watch on]" if s.status == "sold out" else ""
        lines.append(f"{s.pretty_day} - {s.time} - {s.fmt}{tag}")
        if s.status != "sold out":
            lines.append(f"  {s.url}")
    return "\n".join(lines)


def slot_key(day: str, fmt: str, time_: str) -> str:
    """Identifies a screening by when/where rather than by AMC's showtime id,
    so a sold-out chip and the bookable link that replaces it are recognised
    as the same screening."""
    return f"{day}|{fmt}|{time_}".lower()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def scan(dump_dir: Path | None) -> tuple[list[Show], set[str]]:
    base_url = cfg("THEATRE_SHOWTIMES_URL").rstrip("/")
    movie_re = re.compile(cfg("MOVIE_PATTERN"), re.I)
    fmt_re = re.compile(cfg("FORMAT_PATTERN"), re.I)
    days_ahead = int(cfg("DAYS_AHEAD"))
    empty_stop = int(cfg("EMPTY_STREAK_STOP"))

    today = date.today()
    start = today
    if cfg("SCAN_START"):
        start = max(date.fromisoformat(cfg("SCAN_START")), today)
    try:
        base_html = fetch(base_url)
    except Exception as e:
        sys.exit(f"[error] Couldn't load the theatre page even with retries ({e}). "
                 "AMC is likely rate-limiting this computer right now; "
                 "the next scheduled run will try again automatically.")
    if dump_dir:
        (dump_dir / "base.html").write_text(base_html)

    template = cfg("DATE_URL_TEMPLATE") or discover_date_template(base_url, base_html)
    if not template:
        sys.exit(
            "[error] Couldn't discover the per-date URL pattern.\n"
            "Open the theatre page in Chrome, click a future date, copy the URL\n"
            "from the address bar (or the request from DevTools > Network),\n"
            "replace the date with {date}, and set it as DATE_URL_TEMPLATE."
        )
    print(f"[info] date url template: {template}")

    all_shows: list[Show] = []
    scanned_days: set[str] = set()   # days we actually retrieved this run
    fetch_failures = 0
    if start == today:  # the base page shows today's schedule
        all_shows = parse_showtimes(base_html, today.isoformat(), base_url)
        scanned_days.add(today.isoformat())
    started = any(movie_re.search(s.movie) for s in all_shows)
    empty_streak = 0
    first = 1 if start == today else 0
    for i in range(first, days_ahead + 1):
        d = (start + timedelta(days=i)).isoformat()
        polite_sleep()
        try:
            html = fetch(template.format(date=d))
        except Exception as e:
            print(f"[warn] fetch failed for {d}: {e}", file=sys.stderr)
            fetch_failures += 1
            if fetch_failures >= 3:
                sys.exit("[error] 3 dates in a row failed even with retries -- "
                         "AMC is likely rate-limiting this computer right now. "
                         "The next scheduled run will try again automatically.")
            continue
        if dump_dir:
            (dump_dir / f"{d}.html").write_text(html)
        fetch_failures = 0
        scanned_days.add(d)
        day_shows = parse_showtimes(html, d, base_url)
        all_shows.extend(day_shows)

        # Early stop once the movie disappears from the schedule entirely
        # for `empty_stop` consecutive days (end of released window).
        if any(movie_re.search(s.movie) for s in day_shows):
            started = True
            empty_streak = 0
        elif started:  # gaps before the first listed date don't end the scan
            empty_streak += 1
            if empty_streak >= empty_stop:
                print(f"[info] no listings for {empty_streak} consecutive days; stopping at {d}")
                break

    matched = [s for s in dedupe(all_shows)
               if movie_re.search(s.movie) and fmt_re.search(s.fmt)]
    live = sum(1 for s in matched if s.status != "sold out")
    print(f"[info] scan complete: {len(matched)} matching showtimes listed "
          f"({live} bookable, {len(matched) - live} sold out)")
    return matched, scanned_days


def main() -> None:
    ap = argparse.ArgumentParser(description="Watch AMC for new Odyssey IMAX 70mm showtimes")
    ap.add_argument("--dry-run", action="store_true", help="scan and diff but never notify")
    ap.add_argument("--dump", action="store_true", help="save fetched HTML to ./debug for inspection")
    ap.add_argument("--reset", action="store_true", help="clear state and re-seed")
    args = ap.parse_args()

    state_path = Path(cfg("STATE_FILE"))
    if args.reset and state_path.exists():
        state_path.unlink()

    dump_dir = None
    if args.dump:
        dump_dir = Path("debug")
        dump_dir.mkdir(exist_ok=True)

    state = load_state(state_path)
    first_run = not state["seen"]

    current, scanned_days = scan(dump_dir)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Snapshot of what each screening looked like BEFORE this run.
    prev = state["seen"]
    prev_soldout_slots = {
        slot_key(e.get("day", ""), e.get("fmt", ""), e.get("time", ""))
        for e in prev.values() if e.get("status") == "sold out"
    }
    live_now = {s.sid for s in current if s.status != "sold out"}

    # A screening that vanishes from the listing is usually sold out. Only
    # count it as missing if we actually retrieved its date this run --
    # otherwise a failed fetch would look like a sell-out and then a refill.
    for sid, e in prev.items():
        if sid not in live_now and e.get("day") in scanned_days:
            e["missing_scans"] = e.get("missing_scans", 0) + 1

    new: list[Show] = []
    reopened: list[Show] = []
    for s in current:
        before = prev.get(s.sid)
        if before is None:
            # Never seen this id. If it fills a slot we were tracking as sold
            # out, it's a refill rather than a newly released showtime.
            if s.status != "sold out" and slot_key(s.day, s.fmt, s.time) in prev_soldout_slots:
                reopened.append(s)
            else:
                new.append(s)
        elif s.status != "sold out" and (
            before.get("status") == "sold out" or before.get("missing_scans", 0) >= 2
        ):
            reopened.append(s)

    for s in current:
        entry = state["seen"].setdefault(s.sid, {"first_seen": now})
        entry.update(asdict(s))
        entry["last_seen"] = now
        entry["missing_scans"] = 0
    state["meta"]["last_run"] = now
    save_state(state_path, state)

    if first_run:
        days = sorted({s.day for s in current})
        span = f"{days[0]} → {days[-1]}" if days else "none yet"
        label = cfg("ALERT_LABEL")
        msg = (f"Monitoring started. Currently tracking {len(current)} "
               f"showtimes ({span}). You'll be pinged when new ones drop.")
        if not args.dry_run:
            notify(f"{label}: watch is live", msg, cfg("THEATRE_SHOWTIMES_URL"))
        else:
            print(msg)
        return

    label = cfg("ALERT_LABEL")

    if reopened:
        n = len(reopened)
        title = f"SEATS OPEN ({n}): {label}"
        body = ("Seats have come back on sale - grab them fast:\n"
                + format_new_shows(reopened))
        if not args.dry_run:
            notify(title, body, reopened[0].url)
        else:
            print(f"[dry-run] would notify:\n--- {title} ---\n{body}")

    if new:
        bookable = [s for s in new if s.status != "sold out"]
        title = f"{len(new)} new showtime{'s' if len(new) > 1 else ''}: {label}"
        body = format_new_shows(new)
        if not args.dry_run:
            notify(title, body, bookable[0].url if bookable else cfg("THEATRE_SHOWTIMES_URL"))
        else:
            print(f"[dry-run] would notify:\n--- {title} ---\n{body}")

    if not new and not reopened:
        print("[info] no new showtimes, no refills")


if __name__ == "__main__":
    main()
