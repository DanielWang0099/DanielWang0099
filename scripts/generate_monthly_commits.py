#!/usr/bin/env python3
import calendar
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape


USERNAME = os.environ.get("GITHUB_USERNAME", "DanielWang0099")
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
OUTPUT = os.environ.get("OUTPUT", "assets/monthly-commits.svg")


def request_json(url, headers=None, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def graphql(query, variables=None):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "monthly-commit-graph",
    }
    data = request_json(
        "https://api.github.com/graphql",
        headers=headers,
        payload={"query": query, "variables": variables or {}},
    )
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data["data"]


def parse_github_date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def month_key(dt):
    return f"{dt.year}-{dt.month:02d}"


def add_month(year, month):
    if month == 12:
        return year + 1, 1
    return year, month + 1


def month_ranges(start, end):
    year, month = start.year, start.month
    ranges = []
    while (year, month) <= (end.year, end.month):
        next_year, next_month = add_month(year, month)
        ranges.append(
            (
                f"{year}-{month:02d}",
                datetime(year, month, 1, tzinfo=timezone.utc),
                datetime(next_year, next_month, 1, tzinfo=timezone.utc),
            )
        )
        year, month = next_year, next_month
    return ranges


def fetch_monthly_commits_graphql():
    user_data = graphql(
        """
        query($login: String!) {
          user(login: $login) {
            createdAt
          }
        }
        """,
        {"login": USERNAME},
    )
    created_at = parse_github_date(user_data["user"]["createdAt"])
    ranges = month_ranges(created_at, datetime.now(timezone.utc))
    counts = {}

    for start_index in range(0, len(ranges), 12):
        batch = ranges[start_index : start_index + 12]
        field_parts = []
        for i, (_, start, end) in enumerate(batch):
            end_for_query = end - timedelta(seconds=1)
            field_parts.append(
                f'm{i}: contributionsCollection(from: "{start.isoformat()}", to: "{end_for_query.isoformat()}") '
                "{ totalCommitContributions }"
            )
        fields = "\n".join(field_parts)
        data = graphql(
            f"""
            query($login: String!) {{
              user(login: $login) {{
                {fields}
              }}
            }}
            """,
            {"login": USERNAME},
        )
        user = data["user"]
        for i, (key, _, _) in enumerate(batch):
            counts[key] = int(user[f"m{i}"]["totalCommitContributions"])

    return ranges, counts


def fetch_monthly_commits_rest():
    headers = {"User-Agent": "monthly-commit-graph"}
    try:
        user = request_json(f"https://api.github.com/users/{USERNAME}", headers=headers)
        created_at = parse_github_date(user["created_at"])
    except urllib.error.HTTPError as exc:
        print(f"Could not fetch public user data ({exc.code}); rendering fallback range.", file=sys.stderr)
        created_at = datetime(datetime.now(timezone.utc).year, 1, 1, tzinfo=timezone.utc)

    ranges = month_ranges(created_at, datetime.now(timezone.utc))
    valid_months = {key for key, _, _ in ranges}
    counts = defaultdict(int)
    seen_shas = set()

    page = 1
    repos = []
    while True:
        url = (
            f"https://api.github.com/users/{USERNAME}/repos?"
            f"{urllib.parse.urlencode({'per_page': 100, 'page': page, 'type': 'owner'})}"
        )
        try:
            batch = request_json(url, headers=headers)
        except urllib.error.HTTPError as exc:
            print(f"Could not fetch public repositories ({exc.code}); using partial data.", file=sys.stderr)
            break
        if not batch:
            break
        repos.extend(batch)
        page += 1

    for repo in repos:
        page = 1
        while True:
            params = urllib.parse.urlencode(
                {"author": USERNAME, "per_page": 100, "page": page}
            )
            url = f"https://api.github.com/repos/{repo['full_name']}/commits?{params}"
            try:
                commits = request_json(url, headers=headers)
            except urllib.error.HTTPError as exc:
                if exc.code in (409, 422):
                    break
                if exc.code == 403:
                    print("Public REST rate limit reached; using partial data.", file=sys.stderr)
                    return ranges, counts
                raise

            if not commits:
                break

            for commit in commits:
                sha = commit.get("sha")
                if sha in seen_shas:
                    continue
                seen_shas.add(sha)
                date = commit.get("commit", {}).get("author", {}).get("date")
                if not date:
                    continue
                key = month_key(parse_github_date(date))
                if key in valid_months:
                    counts[key] += 1

            if len(commits) < 100:
                break
            page += 1

    return ranges, counts


def nice_max(value):
    if value <= 5:
        return 5
    magnitude = 10 ** math.floor(math.log10(value))
    normalized = value / magnitude
    if normalized <= 2:
        return 2 * magnitude
    if normalized <= 5:
        return 5 * magnitude
    return 10 * magnitude


def catmull_rom_path(points):
    if not points:
        return ""
    if len(points) == 1:
        x, y = points[0]
        return f"M {x:.2f} {y:.2f}"

    parts = [f"M {points[0][0]:.2f} {points[0][1]:.2f}"]
    for i in range(len(points) - 1):
        p0 = points[max(i - 1, 0)]
        p1 = points[i]
        p2 = points[i + 1]
        p3 = points[min(i + 2, len(points) - 1)]
        c1x = p1[0] + (p2[0] - p0[0]) / 6
        c1y = p1[1] + (p2[1] - p0[1]) / 6
        c2x = p2[0] - (p3[0] - p1[0]) / 6
        c2y = p2[1] - (p3[1] - p1[1]) / 6
        parts.append(
            f"C {c1x:.2f} {c1y:.2f}, {c2x:.2f} {c2y:.2f}, {p2[0]:.2f} {p2[1]:.2f}"
        )
    return " ".join(parts)


def render_svg(ranges, counts):
    width, height = 1160, 430
    left, right, top, bottom = 70, 34, 44, 74
    plot_w = width - left - right
    plot_h = height - top - bottom
    values = [counts.get(key, 0) for key, _, _ in ranges]
    max_y = nice_max(max(values) if values else 0)
    x_step = plot_w / max(len(ranges) - 1, 1)

    def x_at(index):
        return left + index * x_step

    def y_at(value):
        return top + plot_h - (value / max_y) * plot_h

    points = [(x_at(i), y_at(value)) for i, value in enumerate(values)]
    line_path = catmull_rom_path(points)
    area_path = (
        f"{line_path} L {points[-1][0]:.2f} {top + plot_h:.2f} "
        f"L {points[0][0]:.2f} {top + plot_h:.2f} Z"
        if points
        else ""
    )

    y_ticks = [round(max_y * i / 5) for i in range(6)]
    y_grid = []
    for value in y_ticks:
        y = y_at(value)
        y_grid.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{width - right}" y2="{y:.2f}" '
            'stroke="#30363d" stroke-width="1" stroke-dasharray="2 3" />'
        )
        y_grid.append(
            f'<text x="{left - 12}" y="{y + 4:.2f}" text-anchor="end">{value}</text>'
        )

    x_grid = []
    label_every = max(1, math.ceil(len(ranges) / 12))
    for i, (key, _, _) in enumerate(ranges):
        x = x_at(i)
        x_grid.append(
            f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{top + plot_h}" '
            'stroke="#30363d" stroke-width="1" stroke-dasharray="2 3" />'
        )
        year, month = key.split("-")
        if i == 0 or month == "01" or i % label_every == 0 or i == len(ranges) - 1:
            label = year if month == "01" else calendar.month_abbr[int(month)]
            x_grid.append(
                f'<text x="{x:.2f}" y="{top + plot_h + 28}" text-anchor="middle">{escape(label)}</text>'
            )

    circles = "\n".join(
        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4.6" fill="#8b949e" />'
        for x, y in points
    )

    total_commits = sum(values)
    svg = f"""<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Monthly commit history">
  <rect width="{width}" height="{height}" fill="#0d1117"/>
  <defs>
    <linearGradient id="activity-fill" x1="0" y1="{top}" x2="0" y2="{top + plot_h}" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#2ea043" stop-opacity="0.42"/>
      <stop offset="1" stop-color="#2ea043" stop-opacity="0.02"/>
    </linearGradient>
  </defs>
  <g font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif" font-size="14" font-weight="600" fill="#8b949e">
    {''.join(y_grid)}
    {''.join(x_grid)}
    <text x="24" y="{top + plot_h / 2:.2f}" transform="rotate(-90 24 {top + plot_h / 2:.2f})" text-anchor="middle">Commits</text>
    <text x="{width - right}" y="26" text-anchor="end">Total commits: {total_commits}</text>
  </g>
  <path d="{area_path}" fill="url(#activity-fill)"/>
  <path d="{line_path}" stroke="#2ea043" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
  {circles}
</svg>
"""
    return svg


def main():
    output_dir = os.path.dirname(OUTPUT)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    if TOKEN:
        ranges, counts = fetch_monthly_commits_graphql()
    else:
        print("GITHUB_TOKEN/GH_TOKEN not set; using public REST commit data fallback.", file=sys.stderr)
        ranges, counts = fetch_monthly_commits_rest()

    with open(OUTPUT, "w", encoding="utf-8") as handle:
        handle.write(render_svg(ranges, counts))


if __name__ == "__main__":
    main()
