#!/usr/bin/env python3
"""ticketwatch: alert when BookMyShow or District opens bookings at your theatres.

Commands:
  theatres [SEARCH]   list theatres in your city with the ids to put in config.toml
  check [--dry-run]   check once and send alerts for newly opened shows
  run                 check in a loop (foreground)
  status              show last check time and failure counts
  test-alert          send a test notification to every configured channel
  install / uninstall run `check` in the background every few minutes (launchd)
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import re
import subprocess
import sys
import time
import tomllib
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi import requests

HOME = Path(__file__).resolve().parent
CONFIG = HOME / "config.toml"
SECRETS = HOME / "secrets.toml"
STATE = HOME / "state.json"
LOCK = HOME / ".lock"
LOG_DIR = HOME / "logs"
PLIST = Path.home() / "Library/LaunchAgents/com.lavanuru.ticketwatch.plist"
LABEL = "com.lavanuru.ticketwatch"

IST = timezone(timedelta(hours=5, minutes=30))
FAIL_ALERT_AFTER = 3       # consecutive failed checks before a "tracker broken" alert
MAX_DATES = 10             # dates fetched per theatre per check
MAX_LINES = 8              # showtimes listed in one alert
BMS_AVAIL = {"3": "available", "2": "filling fast", "1": "almost full", "0": "sold out"}


class Blocked(Exception):
    """The site refused us (403/429/challenge page)."""


class ShapeError(Exception):
    """The response no longer looks like what we parse; the site probably changed."""


def log(msg: str) -> None:
    print(f"{datetime.now(IST):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text).lower()).split())


def slugify(text: str) -> str:
    return norm(text).replace(" ", "-")


def iso(datecode: str) -> str:
    return f"{datecode[:4]}-{datecode[4:6]}-{datecode[6:8]}"


def pretty_date(d: str) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b")


@dataclass
class Show:
    platform: str
    theatre_id: str
    theatre: str
    movie: str
    language: str
    fmt: str
    date: str   # YYYY-MM-DD
    time: str   # 10:30 AM
    seats: str
    link: str
    screen: str = ""   # "SCREEN 6"; part of what makes a show new

class Http:
    """Browser-fingerprinted session with polite pacing between requests."""

    # Cloudflare sometimes rejects one fingerprint and accepts another, so a
    # refused request is retried once as a different browser.
    BROWSERS = ("chrome", "safari")

    def __init__(self) -> None:
        self.browser = 0
        self.s = requests.Session(impersonate=self.BROWSERS[0])
        self.last = 0.0

    def _send(self, url: str, **kw) -> requests.Response:
        wait = self.last + random.uniform(1.5, 3.0) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        return self.s.get(url, timeout=20, **kw)

    @staticmethod
    def _refused(r: requests.Response) -> bool:
        head = r.text[:3000].lower()
        return r.status_code in (401, 403, 429) or "just a moment" in head or "cf-chl-" in head

    def get(self, url: str, **kw) -> requests.Response:
        r = self._send(url, **kw)
        if self._refused(r) and r.status_code != 429:
            self.browser = (self.browser + 1) % len(self.BROWSERS)
            self.s = requests.Session(impersonate=self.BROWSERS[self.browser])
            time.sleep(random.uniform(3, 6))
            r = self._send(url, **kw)
        if self._refused(r):
            raise Blocked(f"HTTP {r.status_code} from {urlparse(url).netloc}")
        if r.status_code >= 400:
            raise ShapeError(f"HTTP {r.status_code} for {urlparse(url).path}")
        return r

    def json(self, url: str, **kw):
        r = self.get(url, **kw)
        try:
            return r.json()
        except ValueError:
            raise ShapeError(f"non-JSON response from {urlparse(url).path}")


class BookMyShow:
    name = "BookMyShow"
    BASE = "https://in.bookmyshow.com"

    def __init__(self, http: Http, city: str, code: str = "") -> None:
        self.http = http
        self.city = city
        self.code = code
        self._region: tuple[str, str] | None = None

    def region(self) -> tuple[str, str]:
        if self._region:
            return self._region
        if self.code:   # set in config.toml, no lookup needed
            self._region = (self.code, slugify(self.city))
            return self._region
        cache = HOME / ".bms_region.json"
        try:
            cached = json.loads(cache.read_text())
            if cached.get("city") == self.city:
                self._region = (cached["code"], cached["slug"])
                return self._region
        except (FileNotFoundError, ValueError, KeyError):
            pass
        self._region = self._lookup_region()
        cache.write_text(json.dumps({"city": self.city, "code": self._region[0], "slug": self._region[1]}))
        return self._region

    def _lookup_region(self) -> tuple[str, str]:
        data = self.http.json(f"{self.BASE}/api/explore/v1/discover/regions",
                              headers={"Accept": "application/json"})
        want = norm(self.city)
        names = []
        for group in data.get("BookMyShow", {}).values():
            for c in group if isinstance(group, list) else []:
                names.append(c.get("RegionSlug", ""))
                aliases = [norm(a) for a in c.get("Alias") or []]
                if want in (norm(c.get("RegionSlug", "")), norm(c.get("RegionName", ""))) or want in aliases:
                    return (c["RegionCode"], c["RegionSlug"])
        raise SystemExit(f"BookMyShow has no city '{self.city}'. Examples: {', '.join(sorted(names)[:15])}")

    def theatres(self) -> list[tuple[str, str]]:
        code, _ = self.region()
        data = self.http.json(f"{self.BASE}/serv/getData", params={"cmd": "QUICKBOOK", "type": "MT"},
                              headers={"Cookie": f"Rgn=|Code={code}|"})
        venues = data.get("cinemas", {}).get("BookMyShow", {}).get("aiVN", {}).get("venues", [])
        return sorted({(v["VenueCode"], v["VenueName"]) for v in venues if v.get("VenueCode")}, key=lambda x: x[1])

    def _byvenue(self, venue: str, datecode: str) -> dict:
        code, _ = self.region()
        data = self.http.json(
            f"{self.BASE}/api/v3/mobile/showtimes/byvenue",
            params={"venueCode": venue, "regionCode": code, "dateCode": datecode, "appCode": "WEB"},
            headers={"Accept": "application/json", "x-app-code": "WEB", "x-region-code": code},
        )
        if not isinstance(data, dict) or "ShowDetails" not in data:
            raise ShapeError("BookMyShow byvenue response has no ShowDetails")
        return data

    def shows(self, venue: str, wanted, start: str | None = None) -> list[Show]:
        first = self._byvenue(venue, (start or datetime.now(IST).strftime("%Y-%m-%d")).replace("-", ""))
        out, done = self._parse(venue, first)
        dates = [d["DateCode"] for d in first.get("ShowDatesArray") or [] if not d.get("isDisabled")]
        for dc in [d for d in dates if d not in done and wanted(iso(d))][:MAX_DATES]:
            more, _ = self._parse(venue, self._byvenue(venue, dc))
            out += more
        return out

    def _parse(self, venue: str, data: dict) -> tuple[list[Show], set[str]]:
        _, slug = self.region()
        out, dates = [], set()
        for det in data.get("ShowDetails") or []:
            datecode = str(det.get("Date", ""))
            dates.add(datecode)
            vname = (det.get("Venues") or {}).get("VenueName") or venue
            link = f"{self.BASE}/cinemas/{slug}/{slugify(vname)}/buytickets/{venue}/{datecode}"
            for ev in det.get("Event") or []:
                for ch in ev.get("ChildEvents") or []:
                    for st in ch.get("ShowTimes") or []:
                        dt = str(st.get("ShowDateTime", ""))
                        if len(dt) != 12:
                            continue
                        out.append(Show(
                            platform=self.name, theatre_id=venue, theatre=vname,
                            movie=ev.get("EventTitle") or ch.get("EventName", ""),
                            language=ch.get("EventLanguage", ""),
                            fmt=" ".join(x for x in (ch.get("EventDimension"), st.get("Attributes"),
                                                     st.get("ScreenName")) if x),
                            date=iso(dt), time=st.get("ShowTime") or f"{dt[8:10]}:{dt[10:]}",
                            seats=BMS_AVAIL.get(str(st.get("AvailStatus")), ""), link=link,
                            screen=st.get("ScreenName", ""),
                        ))
        return out, dates


class District:
    name = "District"
    BASE = "https://www.district.in"
    NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

    def __init__(self, http: Http, city: str) -> None:
        self.http = http
        self.city = city

    def theatres(self) -> list[tuple[str, str]]:
        data = self.http.json(
            f"{self.BASE}/gw/consumer/movies/v3/cinemas",
            params={"version": "3", "site_id": "1", "channel": "web", "child_site_id": "1",
                    "platform": "district", "city": slugify(self.city)},
            headers={"api_source": "district", "x-app-type": "ed_web", "Accept": "*/*",
                     "x-guest-token": f"{int(time.time() * 1000)}_{random.getrandbits(60)}"},
        )
        cinemas = (data.get("data") or {}).get("cinemas") or []
        return sorted({(str(c["id"]), c["name"]) for c in cinemas if c.get("id")}, key=lambda x: x[1])

    def _page(self, cid: str, date: str | None = None) -> dict:
        url = f"{self.BASE}/movies/cinema-CD{cid}" + (f"?fromdate={date}" if date else "")
        m = self.NEXT_DATA.search(self.http.get(url).text)
        if not m:
            raise ShapeError("District cinema page has no __NEXT_DATA__")
        try:
            states = json.loads(m.group(1))["props"]["pageProps"]["data"]["serverState"]
        except (KeyError, TypeError, ValueError):
            raise ShapeError("District cinema page data moved")
        return next(iter(states.values()), {}) if isinstance(states, dict) else {}

    def shows(self, cid: str, wanted, start: str | None = None) -> list[Show]:
        first = self._page(cid, start)
        out, done = self._parse(cid, first)
        dates = (first.get("data") or {}).get("sessionDates") or []
        for d in [d for d in dates if d not in done and wanted(d)][:MAX_DATES]:
            more, _ = self._parse(cid, self._page(cid, d))
            out += more
        return out

    def _parse(self, cid: str, st: dict) -> tuple[list[Show], set[str]]:
        cname = ((st.get("meta") or {}).get("cinema") or {}).get("name") or f"District cinema {cid}"
        out, dates = [], set()
        for group in st.get("arrangedSessions") or []:
            movie = group.get("entityName") or (group.get("data") or {}).get("label", "")
            for s in group.get("sessions") or []:
                when = str(s.get("showTime", ""))
                if len(when) < 16:
                    continue
                # District sends UTC ("2026-11-18T17:30" is 11:00 PM IST).
                start = datetime.fromisoformat(when[:16]).replace(tzinfo=timezone.utc).astimezone(IST)
                date = start.strftime("%Y-%m-%d")
                dates.add(date)
                avail, total = s.get("avail"), s.get("total")
                seats = (f"{avail}/{total} seats free" if total else "") if avail else "sold out"
                out.append(Show(
                    platform=self.name, theatre_id=cid, theatre=cname, movie=movie,
                    language=s.get("lang", ""),
                    fmt=" ".join(x for x in (s.get("scrnFmt"), s.get("audi")) if x),
                    date=date, time=start.strftime("%I:%M %p"),
                    seats=seats, link=f"{self.BASE}/movies/{slugify(cname)}-CD{cid}?fromdate={date}",
                    screen=s.get("audi", ""),
                ))
        return out, dates


# ---------- config, state, matching ----------

@dataclass
class Watch:
    movie: str
    languages: list[str]
    formats: list[str]
    dates: list[str]
    theatres: list[tuple[str, str]]   # (platform, id)
    name: str = ""
    high: bool = False                # priority = "high": the screens you really want

    def wants_date(self, d: str) -> bool:
        return not self.dates or d in self.dates

    def matches(self, s: Show) -> bool:
        if self.movie != "*" and f" {norm(self.movie)} " not in f" {norm(s.movie)} ":
            return False
        if self.languages and norm(s.language) not in {norm(x) for x in self.languages}:
            return False
        if self.formats and not any(norm(f) in norm(s.fmt) for f in self.formats):
            return False
        return self.wants_date(s.date)


def load_config() -> dict:
    if not CONFIG.exists():
        raise SystemExit(f"Missing {CONFIG}. Copy config.example.toml to config.toml and edit it.")
    cfg = tomllib.loads(CONFIG.read_text())
    secrets = tomllib.loads(SECRETS.read_text()) if SECRETS.exists() else {}
    # Environment variables win (used by the GitHub Actions copy, where secrets come from repo secrets).
    for env, key in (("TELEGRAM_BOT_TOKEN", "telegram_bot_token"), ("TELEGRAM_CHAT_ID", "telegram_chat_id"),
                     ("NTFY_TOPIC", "ntfy_topic")):
        if os.environ.get(env):
            secrets[key] = os.environ[env]
    watches = []
    for w in cfg.get("watch", []):
        theatres = []
        for t in w.get("theatres", []):
            platform, _, tid = str(t).partition(":")
            if platform not in ("bms", "district") or not tid:
                raise SystemExit(f"Bad theatre '{t}': use 'bms:CODE' or 'district:ID' (see `theatres` command)")
            theatres.append((platform, tid.strip()))
        watches.append(Watch(
            movie=str(w.get("movie", "*")).strip() or "*",
            languages=list(w.get("languages", [])), formats=list(w.get("formats", [])),
            dates=[str(d) for d in w.get("dates", [])], theatres=theatres,
            name=str(w.get("name", "")), high=str(w.get("priority", "normal")).lower() == "high",
        ))
    if not watches:
        raise SystemExit("config.toml has no [[watch]] entries")
    return {"city": cfg.get("city", "hyderabad"), "bms_region": str(cfg.get("bms_region_code", "")),
            "every": int(cfg.get("check_every_minutes", 5)),
            "mac": cfg.get("notify", {}).get("mac", True), "watches": watches, "secrets": secrets}


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(STATE)


# ---------- notifications ----------

def notify(cfg: dict, title: str, body: str, link: str | None = None, urgent: bool = True) -> None:
    sec = cfg["secrets"]
    if os.environ.get("TW_LABEL"):   # e.g. "cloud", so you can tell which copy sent it
        title = f"[{os.environ['TW_LABEL']}] {title}"
    if cfg["mac"] and sys.platform == "darwin":
        script = ['on run argv', 'display notification (item 2 of argv) with title (item 1 of argv) '
                  'sound name "Glass"', 'end run']
        args = ["osascript"] + sum((["-e", s] for s in script), []) + [title, body]
        subprocess.run(args, check=False, capture_output=True)
    topic = sec.get("ntfy_topic")
    if topic:
        payload = {"topic": topic, "title": title, "message": body, "priority": 5 if urgent else 3,
                   "tags": ["tickets" if urgent else "warning"]}
        if link:
            payload["click"] = link
            payload["actions"] = [{"action": "view", "label": "Book now", "url": link}]
        _post("https://ntfy.sh", payload)
    token, chat = sec.get("telegram_bot_token"), sec.get("telegram_chat_id")
    if token and chat:
        text = f"{title}\n\n{body}" + (f"\n\n{link}" if link else "")
        _post(f"https://api.telegram.org/bot{token}/sendMessage",
              {"chat_id": chat, "text": text, "disable_web_page_preview": True})


def _post(url: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except Exception as e:  # a broken channel must not stop the others
        log(f"notify failed for {urlparse(url).netloc}: {e}")


def show_start(s: Show) -> str:
    """'YYYY-MM-DD HH:MM' in 24h, for sorting and skipping shows that already started."""
    try:
        return f"{s.date} {datetime.strptime(s.time, '%I:%M %p'):%H:%M}"
    except ValueError:
        return f"{s.date} 23:59"


def show_line(s: Show) -> str:
    bits = [s.time, s.language, s.fmt]
    line = " · ".join(b for b in bits if b)
    return f"{line} ({s.seats})" if s.seats else line


# ---------- check ----------

def finished(cfg: dict) -> bool:
    """True once every watch has dates and they are all in the past."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return all(w.dates for w in cfg["watches"]) and max(d for w in cfg["watches"] for d in w.dates) < today


def check(cfg: dict, dry_run: bool = False) -> None:
    if finished(cfg):
        log("all watched dates have passed; nothing to check")
        return
    state = load_state()
    seen: dict = state.setdefault("alerted", {})
    fails: dict = state.setdefault("failures", {})
    paused: dict = state.setdefault("paused_until", {})
    http = Http()
    sources = {"bms": BookMyShow(http, cfg["city"], cfg["bms_region"]), "district": District(http, cfg["city"])}

    # One fetch per theatre, covering the dates any watch wants there.
    targets: dict[tuple[str, str], list[Watch]] = {}
    for w in cfg["watches"]:
        for t in w.theatres:
            targets.setdefault(t, []).append(w)

    today = datetime.now(IST).strftime("%Y-%m-%d")
    errors: dict[str, str] = {}
    found = 0
    for (platform, tid), watches in targets.items():
        src = sources[platform]
        if platform in errors or time.time() < paused.get(platform, 0):
            continue
        try:
            # If every watch here names dates, start at the earliest one instead of today
            # (saves a request when only one date matters).
            named = [d for w in watches for d in w.dates if d >= today]
            start = min(named) if named and all(w.dates for w in watches) else None
            shows = src.shows(tid, lambda d, ws=watches: d >= today and any(w.wants_date(d) for w in ws),
                              start)
        except Blocked as e:
            n = fails.get(platform, 0)
            paused[platform] = time.time() + min(5 * 2 ** n, 60) * 60
            errors[platform] = f"blocked ({e})"
            continue
        except (ShapeError, requests.RequestsError, KeyError, TypeError, ValueError) as e:
            errors[platform] = f"{type(e).__name__}: {e}"
            continue

        # Remember each show (screen + time) so a new screen or an added showtime alerts too,
        # even when other shows that day already alerted. High-priority watches win.
        ordered = sorted(watches, key=lambda w: not w.high)
        groups: dict[tuple, list[Show]] = {}
        new_keys: dict[tuple, list[str]] = {}
        now = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
        matched = 0
        for s in shows:
            w = next((w for w in ordered if w.matches(s)), None)
            if w is None or show_start(s) < now:
                continue
            matched += 1
            key = f"{platform}|{tid}|{norm(s.movie)}|{norm(s.screen)}|{s.time}|{s.date}"
            if key in seen:
                continue
            g = (not w.high, s.date, norm(s.movie), s.screen, w.name)
            groups.setdefault(g, []).append(s)
            new_keys.setdefault(g, []).append(key)
        log(f"{src.name} {tid}: {len(shows)} shows, {matched} matching, {sum(map(len, groups.values()))} new")
        for g, group in sorted(groups.items()):
            found += 1
            group.sort(key=show_start)
            s0 = group[0]
            high = not g[0]
            if high:
                title = f"🚨 {s0.movie}: {g[4] or 'top-choice screen'} OPEN"
            else:
                title = f"🎟 {s0.movie}: new shows" + (f" ({g[4]})" if g[4] else "")
            lines = [show_line(s) for s in group[:MAX_LINES]]
            if len(group) > MAX_LINES:
                lines.append(f"+{len(group) - MAX_LINES} more shows")
            body = f"{s0.theatre}\n{pretty_date(s0.date)} on {s0.platform}\n" + "\n".join(lines)
            log(f"ALERT {title} | {body.replace(chr(10), ' | ')} | {s0.link}")
            if not dry_run:
                notify(cfg, title, body, s0.link, urgent=high)
                for k in new_keys[g]:
                    seen[k] = today

    # Health: alert once when a site keeps failing, and once when it recovers.
    for platform in {p for p, _ in targets}:
        name = sources[platform].name
        if platform in errors:
            fails[platform] = fails.get(platform, 0) + 1
            log(f"{name} check failed ({fails[platform]} in a row): {errors[platform]}")
            if fails[platform] == FAIL_ALERT_AFTER and not dry_run:
                notify(cfg, f"⚠️ ticketwatch: {name} checks failing",
                       f"{FAIL_ALERT_AFTER} checks in a row failed, so a booking opening could be missed.\n"
                       f"{errors[platform]}", urgent=False)
        elif time.time() >= paused.get(platform, 0):
            if fails.get(platform, 0) >= FAIL_ALERT_AFTER and not dry_run:
                notify(cfg, f"✅ ticketwatch: {name} working again", "Checks are succeeding again.", urgent=False)
            fails[platform] = 0
            paused.pop(platform, None)

    # Forget alerts for past dates so the state file stays small.
    for k in [k for k in seen if k.rsplit("|", 1)[-1] < today]:
        del seen[k]
    state["last_check"] = datetime.now(IST).isoformat(timespec="seconds")
    state["last_errors"] = errors
    if not dry_run:
        save_state(state)
    log(f"check done: {found} new alert(s)" + (" [dry run, nothing sent or saved]" if dry_run else ""))


def locked_check(cfg: dict, dry_run: bool = False) -> None:
    with open(LOCK, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another check is still running; skipping")
            return
        check(cfg, dry_run)


# ---------- other commands ----------

def cmd_theatres(city: str, search: str) -> None:
    http = Http()
    for platform, src in (("bms", BookMyShow(http, city)), ("district", District(http, city))):
        try:
            rows = src.theatres()
        except (Blocked, ShapeError, requests.RequestsError) as e:
            print(f"\n{src.name}: could not list theatres ({e})")
            continue
        if search:  # every search word must start a word in the name ("amb" != "Musarambagh")
            words = norm(search).split()
            rows = [r for r in rows if all(re.search(rf"\b{re.escape(w)}", norm(r[1])) for w in words)]
        print(f"\n{src.name} ({len(rows)}):")
        for tid, name in rows:
            print(f'  "{platform}:{tid}"'.ljust(22) + name)


def cmd_install(cfg: dict) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{sys.executable}</string><string>{Path(__file__).resolve()}</string><string>check</string>
  </array>
  <key>StartInterval</key><integer>{cfg["every"] * 60}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{LOG_DIR / "ticketwatch.log"}</string>
  <key>StandardErrorPath</key><string>{LOG_DIR / "ticketwatch.log"}</string>
</dict></plist>
""")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], check=True)
    print(f"Installed. Checks every {cfg['every']} min while the Mac is awake. Log: {LOG_DIR / 'ticketwatch.log'}")


def cmd_uninstall() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    PLIST.unlink(missing_ok=True)
    print("Uninstalled.")


def trim_log() -> None:
    f = LOG_DIR / "ticketwatch.log"
    if f.exists() and f.stat().st_size > 5_000_000:
        f.write_text("\n".join(f.read_text(errors="replace").splitlines()[-5000:]) + "\n")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("theatres")
    t.add_argument("search", nargs="?", default="")
    t.add_argument("--city")
    c = sub.add_parser("check")
    c.add_argument("--dry-run", action="store_true")
    for name in ("run", "status", "test-alert", "install", "uninstall", "finished"):
        sub.add_parser(name)
    a = p.parse_args()

    if a.cmd == "theatres":
        city = a.city or (tomllib.loads(CONFIG.read_text()).get("city") if CONFIG.exists() else None)
        if not city:
            raise SystemExit("Pass --city, e.g. `theatres AMB --city hyderabad`")
        return cmd_theatres(city, a.search)
    if a.cmd == "uninstall":
        return cmd_uninstall()

    cfg = load_config()
    if a.cmd == "finished":   # exit 0 when all watched dates are past (used by the cloud loop)
        sys.exit(0 if finished(cfg) else 1)
    if a.cmd == "check":
        trim_log()
        locked_check(cfg, a.dry_run)
    elif a.cmd == "run":
        while True:
            locked_check(cfg)
            time.sleep(cfg["every"] * 60 + random.uniform(-30, 30))
    elif a.cmd == "status":
        s = load_state()
        print(json.dumps({k: s.get(k) for k in ("last_check", "failures", "paused_until", "last_errors")}, indent=1))
        print(f"{len(s.get('alerted', {}))} movie/date openings already alerted")
    elif a.cmd == "test-alert":
        notify(cfg, "🎟 ticketwatch test", "If you see this, alerts work.", "https://in.bookmyshow.com")
        print("Sent a test alert to every configured channel.")
    elif a.cmd == "install":
        cmd_install(cfg)


if __name__ == "__main__":
    main()
