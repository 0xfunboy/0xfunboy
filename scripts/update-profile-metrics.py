#!/usr/bin/env python3
"""Render profile metrics from public GitHub sources, using only the standard library.

GH_TOKEN (or GITHUB_TOKEN) authenticates public REST requests for rate limits.
The contribution calendar is always fetched anonymously, without cookies or a
token. It reflects the publicly visible calendar, which GitHub may augment with
anonymized private activity if the profile owner enabled that setting. No private
repositories or their metadata are requested. Counts follow GitHub's definition
of contributions, not raw commits; streaks cover the displayed calendar window.

Usage: python3 scripts/update-profile-metrics.py --output-dir dist
An optional --calendar-file accepts previously downloaded anonymous calendar HTML.
All data and SVGs are validated before any existing output is replaced.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


BG, BORDER, WHITE, MUTED = "#0b1119", "#293543", "#f0f4f8", "#aab6c3"
ORANGE, CYAN = "#fb923c", "#88c7cc"
COLORS = [ORANGE, CYAN, "#d4b894", "#9eaebe", "#5e839a", "#46525f"]
FONT = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def fetch(url: str, token: str = "") -> str:
    """Use a separate, cookieless request; never attach credentials to github.com."""
    headers = {"User-Agent": "public-profile-metrics", "Accept-Language": "en-US"}
    if url.startswith("https://api.github.com/"):
        headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        if token:
            headers["Authorization"] = f"Bearer {token}"
    else:
        require(url.startswith("https://github.com/users/"), "Unexpected calendar URL")
        headers["Accept"] = "text/html"
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=headers), timeout=45) as response:
                return response.read().decode("utf-8")
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise RuntimeError(f"GitHub request failed with HTTP {error.code}: {url}") from None
        except (URLError, TimeoutError):
            if attempt == 2:
                raise RuntimeError(f"GitHub request could not be completed: {url}") from None
        time.sleep(2 ** attempt)
    raise RuntimeError("GitHub request failed")


def public_repositories(username: str, token: str) -> list[dict]:
    repositories: list[dict] = []
    page = 1
    seen: set[int] = set()
    while True:
        url = f"https://api.github.com/users/{username}/repos?type=owner&per_page=100&page={page}&sort=full_name"
        batch = json.loads(fetch(url, token))
        require(isinstance(batch, list), "Malformed public repository response")
        for repo in batch:
            require(isinstance(repo, dict), "Malformed repository record")
            require(type(repo.get("private")) is bool and type(repo.get("fork")) is bool, "Missing visibility or fork flag")
            require(repo.get("owner", {}).get("login", "").lower() == username.lower(), "Unexpected repository owner")
            require(type(repo.get("id")) is int and repo["id"] not in seen, "Duplicate or missing repository ID")
            seen.add(repo["id"])
            if repo["private"] or repo["fork"]:
                continue
            require(isinstance(repo.get("name"), str), "Missing repository name")
            require(repo.get("languages_url") == f"https://api.github.com/repos/{repo['owner']['login']}/{repo['name']}/languages", "Unexpected language endpoint")
            require(nonnegative_int(repo.get("stargazers_count")), "Malformed star count")
            require(nonnegative_int(repo.get("forks_count")), "Malformed fork count")
            repositories.append(repo)
        if len(batch) < 100:
            break
        page += 1
        require(page <= 100, "Repository pagination exceeded safety limit")
    require(bool(repositories), "No public, owned non-fork repositories were returned")
    return repositories


def language_bytes(repositories: list[dict], token: str) -> Counter:
    def one_repository(repo: dict) -> dict:
        languages = json.loads(fetch(repo["languages_url"], token))
        require(isinstance(languages, dict), "Malformed language response")
        require(all(isinstance(name, str) and name and nonnegative_int(size) for name, size in languages.items()), "Malformed language byte counts")
        return languages

    total: Counter = Counter()
    with ThreadPoolExecutor(max_workers=6) as pool:
        for languages in pool.map(one_repository, repositories):
            total.update(languages)
    total = Counter({name: size for name, size in total.items() if size})
    require(sum(total.values()) > 0, "No language bytes were returned")
    return total


class CalendarParser(HTMLParser):
    """Join calendar cells and accessible tooltips by ID, never infer from colors."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: dict[str, dict] = {}
        self.tooltips: dict[str, str] = {}
        self.heading = ""
        self.window: tuple[date, date] | None = None
        self.capture: tuple[str, str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if attr.get("data-from") and attr.get("data-to"):
            window = (date.fromisoformat(attr["data-from"].split()[0]), date.fromisoformat(attr["data-to"].split()[0]))
            require(self.window in (None, window), "Conflicting contribution calendar ranges")
            self.window = window
        if attr.get("data-date"):
            cell_id = attr.get("id")
            require(bool(cell_id) and cell_id not in self.cells, "Missing or duplicate contribution cell ID")
            self.cells[cell_id] = attr
        if tag == "tool-tip" and attr.get("for"):
            self.capture = (tag, attr["for"], [])
        elif tag == "h2" and attr.get("id") == "js-contribution-activity-description":
            self.capture = (tag, "heading", [])

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.capture[2].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.capture and self.capture[0] == tag:
            _, key, parts = self.capture
            value = " ".join("".join(parts).split())
            if key == "heading":
                require(not self.heading, "Duplicate contribution heading")
                self.heading = value
            else:
                require(key not in self.tooltips, "Duplicate contribution tooltip")
                self.tooltips[key] = value
            self.capture = None


@dataclass(frozen=True)
class Calendar:
    days: list[tuple[date, int]]
    current: int
    longest: int
    longest_start: date | None
    longest_end: date | None

    @property
    def total(self) -> int:
        return sum(count for _, count in self.days)


def parse_calendar(html: str, today: date) -> Calendar:
    parser = CalendarParser()
    parser.feed(html)
    parser.close()
    require(parser.window is not None, "Missing contribution calendar date range")
    start, end = parser.window
    require(end == today, "Calendar does not end on today's UTC date")
    require(360 <= (end - start).days <= 372, "Unexpected rolling calendar range")
    daily: dict[date, int] = {}
    for cell_id, cell in parser.cells.items():
        day = date.fromisoformat(cell["data-date"])
        require(day not in daily, "Duplicate contribution date")
        tooltip = parser.tooltips.get(cell_id, "")
        match = re.match(r"^(No|[\d,]+) contributions? on\b", tooltip)
        require(match is not None, f"Missing or malformed contribution count for {day}")
        count = 0 if match[1] == "No" else int(match[1].replace(",", ""))
        require(cell.get("data-level") in {"0", "1", "2", "3", "4"}, "Malformed contribution level")
        require((count == 0) == (cell["data-level"] == "0"), "Contribution count and level disagree")
        require(start <= day <= end, "Contribution date falls outside displayed calendar")
        daily[day] = count
    expected = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    require(sorted(daily) == expected, "Contribution calendar has missing or unexpected dates")
    headline = re.match(r"^([\d,]+) contributions?\b", parser.heading)
    require(headline is not None, "Missing contribution total heading")
    require(sum(daily.values()) == int(headline[1].replace(",", "")), "Daily contribution counts do not match GitHub's total")

    longest = run = 0
    longest_start = longest_end = run_start = None
    for day in expected:
        if daily[day]:
            if run == 0:
                run_start = day
            run += 1
            if run > longest:
                longest, longest_start, longest_end = run, run_start, day
        else:
            run = 0
    # Today's still-empty UTC day does not break a run ending yesterday.
    cursor = end if daily[end] else end - timedelta(days=1)
    current = 0
    while cursor in daily and daily[cursor] > 0:
        current += 1
        cursor -= timedelta(days=1)
    return Calendar(sorted(daily.items()), current, longest, longest_start, longest_end)


def text(x: float, y: float, content: object, size: int = 14, color: str = MUTED, weight: int = 400, anchor: str = "start") -> str:
    return f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}">{escape(str(content))}</text>'


def card(width: int, height: int, title: str, description: str, contents: list[str]) -> str:
    return '\n'.join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>',
        f'<rect width="{width}" height="{height}" rx="14" fill="{BG}"/>',
        f'<rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="13" fill="none" stroke="{BORDER}"/>',
        f'<path d="M 28 0 V 13" stroke="{ORANGE}" stroke-width="3"/>',
        f'<g font-family="{escape(FONT, quote=True)}">', *contents, '</g></svg>\n',
    ])


def stats_svg(repos: list[dict], languages: Counter, updated: date) -> str:
    stars = sum(repo["stargazers_count"] for repo in repos)
    forks = sum(repo["forks_count"] for repo in repos)
    content = [text(28, 40, "PUBLIC FOOTPRINT", 19, WHITE, 600), text(28, 65, "Owned repositories · non-forks only", 13)]
    metrics = [(28, 128, len(repos), "Repositories"), (234, 128, stars, "Stars received"), (28, 221, forks, "Forks of these repos"), (234, 221, len(languages), "Languages detected")]
    for x, y, value, label in metrics:
        content += [text(x, y, f"{value:,}", 43, ORANGE if x == 28 and y == 128 else WHITE, 650), text(x, y + 25, label, 13)]
    content += [f'<path d="M 28 264 H 412" stroke="{BORDER}"/>', text(28, 286, f"GitHub REST · updated {updated} UTC", 11)]
    description = f"Public repositories owned by the profile, excluding forks: {len(repos)} repositories, {stars} stars received, {forks} forks of these repositories, and {len(languages)} languages detected. Updated {updated} UTC."
    return card(440, 304, "Public GitHub footprint", description, content)


def languages_svg(languages: Counter) -> str:
    total = sum(languages.values())
    ranked = sorted(languages.items(), key=lambda pair: (-pair[1], pair[0]))
    displayed = ranked[:5]
    if len(ranked) > 5:
        displayed.append(("Other", sum(size for _, size in ranked[5:])))
    content = [text(28, 40, "CODE COMPOSITION", 19, WHITE, 600), text(28, 65, "Public, owned non-fork repos · language bytes", 12)]
    x = 28.0
    for index, (name, size) in enumerate(displayed):
        width = 384 * size / total
        content.append(f'<rect x="{x:.3f}" y="86" width="{width:.3f}" height="12" fill="{COLORS[index]}"/>')
        y = 127 + index * 25
        content += [f'<circle cx="33" cy="{y - 5}" r="4" fill="{COLORS[index]}"/>', text(47, y, name, 14, WHITE), text(412, y, f"{size / total:.1%}", 14, MUTED, anchor="end")]
        x += width
    content += [f'<path d="M 28 264 H 412" stroke="{BORDER}"/>', text(28, 286, "Languages by code volume · GitHub REST", 11)]
    summary = "; ".join(f"{name}: {size:,} bytes ({size / total:.1%})" for name, size in ranked)
    return card(440, 304, "Code composition by language bytes", f"Aggregated GitHub language bytes across public, owned non-fork repositories. {summary}. This measures repository composition, not personal proficiency or lines authored.", content)


def streak_svg(calendar: Calendar) -> str:
    start, end = calendar.days[0][0], calendar.days[-1][0]
    content = [text(28, 42, "CONSISTENCY", 22, WHITE, 600), text(28, 72, f"Public profile calendar · {start} → {end} · UTC", 16)]
    values = [(28, calendar.current, "Current · days", ORANGE), (251, calendar.longest, "Longest · days", WHITE), (466, calendar.total, "Contributions", CYAN)]
    for x, value, label, color in values:
        content += [text(x, 143, f"{value:,}", 52, color, 650), text(x, 175, label, 18)]
    content += [f'<path d="M 28 199 H 652" stroke="{BORDER}"/>', text(28, 227, "Rolling calendar · current day may be incomplete", 17)]
    longest_period = f"{calendar.longest_start} to {calendar.longest_end}" if calendar.longest else "none"
    description = f"Publicly visible GitHub contribution calendar from {start} to {end}, UTC. Current streak: {calendar.current} days. Longest streak in this window: {calendar.longest} days, {longest_period}. Total contributions in the window: {calendar.total}. An empty current UTC day does not yet break yesterday's run. GitHub may include publicly anonymized private activity if the profile owner enabled it."
    return card(680, 250, "Contribution streaks and total", description, content)


def activity_svg(calendar: Calendar) -> str:
    days = calendar.days[-90:]
    counts = [count for _, count in days]
    total, active, peak = sum(counts), sum(count > 0 for count in counts), max(counts)
    content = [text(28, 42, "BUILDING IN PUBLIC", 21, WHITE, 600), text(28, 72, f"Last 90 days · {total:,} contributions · {active} active days · peak {peak}/day", 17)]
    left, top, plot_width, plot_height = 60, 109, 812, 136
    maximum = max(peak, 1)
    for ratio in (0, 0.5, 1):
        y = top + plot_height * (1 - ratio)
        content.append(f'<path d="M {left} {y} H 872" stroke="{BORDER}"/>')
        content.append(text(47, y + 5, f"{maximum * ratio:g}", 13, anchor="end"))
    step = plot_width / len(days)
    for index, (day, count) in enumerate(days):
        if count:
            height = max(1, plot_height * count / maximum)
            x, y = left + index * step + 1, top + plot_height - height
            content.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{step - 2:.2f}" height="{height:.2f}" rx="1.5" fill="{ORANGE}"><title>{day}: {count} contributions</title></rect>')
    content += [text(left, 273, days[0][0].isoformat(), 14), text(872, 273, days[-1][0].isoformat(), 14, anchor="end"), f'<path d="M 28 295 H 872" stroke="{BORDER}"/>', text(28, 324, "Public profile calendar · daily contribution count · UTC", 15)]
    series = "; ".join(f"{day}: {count}" for day, count in days)
    description = f"Daily public profile calendar activity from {days[0][0]} to {days[-1][0]}, UTC. {total} contributions over {active} active days. Maximum {peak} in a day. Daily counts: {series}. GitHub may include publicly anonymized private activity if the profile owner enabled it."
    return card(900, 346, "Daily GitHub activity over the last 90 days", description, content)


def write_assets(output: Path, svgs: dict[str, str]) -> None:
    # Parsing every SVG before staging ensures a fetch/validation failure leaves
    # every last-good card in place. Individual replacements are atomic.
    for name, svg in svgs.items():
        root = ET.fromstring(svg)
        require(root.tag == "{http://www.w3.org/2000/svg}svg", f"Invalid SVG: {name}")
        require(not any(node.tag.rsplit("}", 1)[-1] in {"script", "foreignObject", "image"} for node in root.iter()), f"Unexpected active content: {name}")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".metrics-", dir=output) as staging:
        for name, svg in svgs.items():
            Path(staging, name).write_text(svg, encoding="utf-8")
        for name in svgs:
            os.replace(Path(staging, name), output / name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="0xfunboy")
    parser.add_argument("--output-dir", type=Path, default=Path("assets"))
    parser.add_argument("--calendar-file", type=Path, help="Previously downloaded anonymous GitHub calendar HTML")
    args = parser.parse_args()
    require(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", args.username) is not None, "Invalid GitHub username")
    today = datetime.now(timezone.utc).date()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    html = args.calendar_file.read_text(encoding="utf-8") if args.calendar_file else fetch(f"https://github.com/users/{args.username}/contributions")
    calendar = parse_calendar(html, today)
    repos = public_repositories(args.username, token)
    languages = language_bytes(repos, token)
    svgs = {"stats.svg": stats_svg(repos, languages, today), "toplangs.svg": languages_svg(languages), "streak.svg": streak_svg(calendar), "activity.svg": activity_svg(calendar)}
    write_assets(args.output_dir, svgs)
    print(json.dumps({"username": args.username, "updated_utc": today.isoformat(), "public_owned_nonfork_repositories": len(repos), "stars_received": sum(repo["stargazers_count"] for repo in repos), "language_bytes": dict(languages.most_common()), "calendar_from": calendar.days[0][0].isoformat(), "calendar_to": calendar.days[-1][0].isoformat(), "contributions_in_window": calendar.total, "current_streak_days": calendar.current, "longest_streak_days": calendar.longest, "last_90_days_total": sum(count for _, count in calendar.days[-90:]), "outputs": list(svgs)}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        print(f"Profile metrics not updated: {error}", file=sys.stderr)
        raise SystemExit(1)
