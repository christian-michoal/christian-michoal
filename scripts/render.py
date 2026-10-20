#!/usr/bin/env python3
"""Render the animated terminal SVGs and refresh the README activity log.

Runs in GitHub Actions on a schedule. Needs GITHUB_TOKEN for the GraphQL
contribution calendar. Pass --sample to render offline with fake data.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"

QUERY = """
query($login: String!) {
  user(login: $login) {
    createdAt
    contributionsCollection {
      contributionCalendar {
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""

# Terminal grid. Every text run gets textLength = chars * CW so the layout is
# identical whatever monospace font the viewer has.
CW, LH, FONT = 8.4, 21, 14
COLS, PAD_X, TOP, BOTTOM = 80, 22, 56, 22
WIDTH = round(PAD_X * 2 + COLS * CW)

THEMES = {
    "dark": {
        "bg": "#0d1117", "bar": "#161b22", "border": "#30363d", "fg": "#e6edf3",
        "dim": "#7d8590", "green": "#3fb950", "blue": "#58a6ff",
        "yellow": "#d29922", "red": "#f85149", "purple": "#bc8cff",
    },
    "light": {
        "bg": "#ffffff", "bar": "#f6f8fa", "border": "#d0d7de", "fg": "#1f2328",
        "dim": "#656d76", "green": "#1a7f37", "blue": "#0969da",
        "yellow": "#9a6700", "red": "#cf222e", "purple": "#8250df",
    },
}


# --- data --------------------------------------------------------------------

def request(url: str, token: str | None, body: dict | None = None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "profile-terminal"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=30) as r:
        return json.load(r)


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch(login: str, token: str) -> dict:
    res = request(f"{API}/graphql", token, {"query": QUERY, "variables": {"login": login}})
    if res.get("errors"):
        raise SystemExit(f"GraphQL error: {res['errors']}")
    user = res["data"]["user"]
    weeks = user["contributionsCollection"]["contributionCalendar"]["weeks"]
    days = [(date.fromisoformat(d["date"]), d["contributionCount"])
            for w in weeks for d in w["contributionDays"]]
    events = request(f"{API}/users/{login}/events/public?per_page=100", token)
    return {"created_at": parse_ts(user["createdAt"]), "days": days, "events": events}


def sample(login: str, now: datetime) -> dict:
    rng = random.Random(7)
    today = now.date()
    days = [(today - timedelta(days=i), 0 if i > 12 and rng.random() < 0.6 else rng.randint(1, 9))
            for i in range(364, -1, -1)]
    repos = [f"{login}/inventory", f"{login}/{login}", f"{login}/ctf-writeups"]
    events = [{"type": "PushEvent", "repo": {"name": repos[i % 3]}, "payload": {},
               "created_at": (now - timedelta(hours=9 * i)).isoformat()} for i in range(10)]
    return {"created_at": datetime(2021, 4, 24, tzinfo=timezone.utc), "days": days, "events": events}


def streaks(days: list[tuple[date, int]]) -> tuple[int, int]:
    counts = [c for _, c in sorted(days)]
    longest = run = 0
    for c in counts:
        run = run + 1 if c else 0
        longest = max(longest, run)
    # Today isn't over yet, so an empty today doesn't break the streak.
    recent = counts[:-1] if counts and counts[-1] == 0 else counts
    current = 0
    for c in reversed(recent):
        if not c:
            break
        current += 1
    return current, longest


def activity(events: list[dict], limit: int = 6) -> list[tuple[str, str, str]]:
    out, seen = [], set()
    for e in events:
        kind, payload = e["type"], e.get("payload") or {}
        if kind == "PushEvent":
            verb = "pushed to"
        elif kind == "CreateEvent" and payload.get("ref_type") == "repository":
            verb = "created"
        elif kind == "PullRequestEvent" and payload.get("action") in ("opened", "closed"):
            verb = "merged a PR in" if (payload.get("pull_request") or {}).get("merged") else "opened a PR on"
        elif kind == "IssuesEvent" and payload.get("action") == "opened":
            verb = "opened an issue on"
        elif kind == "ReleaseEvent":
            verb = "released"
        elif kind == "ForkEvent":
            verb = "forked"
        else:
            continue
        key = (e["created_at"][:10], verb, e["repo"]["name"])
        if key not in seen:
            seen.add(key)
            out.append(key)
        if len(out) >= limit:
            break
    return out


def stats(data: dict, now: datetime) -> dict:
    current, longest = streaks(data["days"])
    push = next((e for e in data["events"] if e["type"] == "PushEvent"), None)
    return {
        "current": current,
        "longest": longest,
        "year_total": sum(c for d, c in data["days"] if d.year == now.year),
        "years": (now - data["created_at"]).days / 365.25,
        "last_push": (push["repo"]["name"].split("/")[-1], push["created_at"][:10]) if push else None,
        "log": activity(data["events"]),
    }


# --- terminal content ---------------------------------------------------------

def build_rows(cfg: dict, s: dict, now: datetime) -> list[dict]:
    """Each row is {kind: cmd|out|end, segs: [(text, color)], cmd?: str}."""
    prompt = [(f"{cfg['name']}@{cfg['host']}", "green"), (":", "fg"), ("~", "blue"), ("$ ", "fg")]

    def cmd(text):
        return {"kind": "cmd", "segs": prompt, "cmd": text}

    def out(*segs):
        return {"kind": "out", "segs": list(segs)}

    def kv(key, value, color="fg", mark=("[+]", "green")):
        return out(mark, (f" {key} ".ljust(24, "."), "dim"), (f" {value}", color))

    rows = [out((f"Last login: {now:%a %b %d %H:%M:%S %Y} UTC from github-actions", "dim")),
            cmd("id"),
            out((cfg["identity"], "dim")),
            out(),
            cmd(f"nmap -sV -Pn {cfg['login']}")]
    # Column widths follow the longest cell, like real nmap output.
    widths = [max(len(p[i]) for p in cfg["ports"] + [["PORT", "STATE", "SERVICE"]]) + 2 for i in range(3)]
    state_colors = {"open": "green", "closed": "red"}
    rows.append(out(("PORT".ljust(widths[0]) + "STATE".ljust(widths[1])
                     + "SERVICE".ljust(widths[2]) + "VERSION", "dim")))
    for port, state, service, version in cfg["ports"]:
        rows.append(out((port.ljust(widths[0]), "fg"),
                        (state.ljust(widths[1]), state_colors.get(state, "yellow")),
                        (service.ljust(widths[2]), "purple"), (version, "dim")))
    if cfg.get("service_info"):
        rows.append(out(("Service Info: ", "dim"), (cfg["service_info"], "fg")))
    rows += [out(("Nmap done: 1 IP address (1 host up) scanned in ", "dim"),
                 (f"{s['years']:.2f} years", "blue")),
             out(),
             cmd("./streak --status")]

    cur = s["current"]
    rows.append(kv("current streak", f"{cur} day{'s' * (cur != 1)}", "green" if cur else "red"))
    rows.append(kv("longest streak", f"{s['longest']} days"))
    rows.append(kv(f"contributions ({now.year})", str(s["year_total"])))
    if s["last_push"]:
        repo, day = s["last_push"]
        rows.append(kv("last push", f"{repo} @ {day}", "blue"))
    if cur == 0:
        status = ("dormant", "dim")
    elif cur < 7:
        status = ("beaconing", "yellow")
    elif cur < 30:
        status = ("persistent", "green")
    else:
        status = ("resident", "purple")
    rows.append(kv("status", status[0], status[1], ("[*]", "blue")))
    rows.append({"kind": "end", "segs": prompt})
    return rows


# --- svg ----------------------------------------------------------------------

def text_run(col: int, y: float, text: str, color: str) -> tuple[str, int]:
    """One <text> on the grid. Returns (svg, next column)."""
    lead = len(text) - len(text.lstrip(" "))
    body = text.strip(" ")
    if not body:
        return "", col + len(text)
    x = PAD_X + (col + lead) * CW
    svg = (f'<text x="{x:.1f}" y="{y}" fill="var(--{color})" textLength="{len(body) * CW:.1f}" '
           f'lengthAdjust="spacingAndGlyphs">{escape(body)}</text>')
    return svg, col + len(text)


def render_svg(rows: list[dict], theme: dict, cfg: dict) -> str:
    height = TOP + len(rows) * LH + BOTTOM
    body, t = [], 0.4
    for i, row in enumerate(rows):
        y = TOP + i * LH + FONT
        col, parts = 0, []
        for text, color in row["segs"]:
            svg, col = text_run(col, y, text, color)
            parts.append(svg)

        if row["kind"] == "cmd":
            n = len(row["cmd"])
            svg, _ = text_run(col, y, row["cmd"], "fg")
            parts.append(svg)
            typing = n * 0.045
            # Cover the command with a background-colored block and shrink it
            # toward the right edge in n steps, so it looks typed out.
            x, w = PAD_X + col * CW, n * CW + 2
            parts.append(f'<rect class="t" x="{x:.1f}" y="{y - FONT}" width="{w:.1f}" height="{LH}" '
                         f'fill="var(--bg)" style="animation:type {typing:.2f}s steps({n},end) '
                         f'{t + 0.35:.2f}s forwards"/>')
            body.append(f'<g class="l" style="animation-delay:{t:.2f}s">{"".join(parts)}</g>')
            t += 0.35 + typing + 0.3
        elif row["kind"] == "end":
            x = PAD_X + col * CW
            parts.append(f'<rect class="c" x="{x:.1f}" y="{y - FONT + 1}" width="{CW:.1f}" '
                         f'height="{FONT + 3}" fill="var(--fg)" style="animation-delay:{t:.2f}s"/>')
            body.append(f'<g class="l" style="animation-delay:{t:.2f}s">{"".join(parts)}</g>')
        else:
            body.append(f'<g class="l" style="animation-delay:{t:.2f}s">{"".join(parts)}</g>')
            t += 0.07

    vars_ = ";".join(f"--{k}:{v}" for k, v in theme.items())
    title = f"{cfg['name']}@{cfg['host']}: ~"
    title_w = len(title) * 7
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" role="img" aria-label="Terminal session showing {escape(cfg['name'])}'s profile">
<!-- {escape(cfg['flag'])} -->
<style>
svg{{{vars_}}}
text{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace;font-size:{FONT}px;white-space:pre}}
.l{{opacity:0;animation:show .01s forwards}}
.t{{transform-box:fill-box;transform-origin:right}}
.c{{opacity:0;animation:blink 1.1s step-end infinite}}
@keyframes show{{to{{opacity:1}}}}
@keyframes type{{to{{transform:scaleX(0)}}}}
@keyframes blink{{0%{{opacity:1}}50%{{opacity:0}}}}
@media (prefers-reduced-motion:reduce){{.l,.c{{opacity:1!important;animation:none!important}}.t{{display:none}}}}
</style>
<rect x=".5" y=".5" width="{WIDTH - 1}" height="{height - 1}" rx="10" fill="var(--bg)" stroke="var(--border)"/>
<path d="M.5 34.5V10.5a10 10 0 0 1 10-10h{WIDTH - 21}a10 10 0 0 1 10 10v24z" fill="var(--bar)" stroke="var(--border)"/>
<circle cx="20" cy="17.5" r="6" fill="#ff5f57"/><circle cx="40" cy="17.5" r="6" fill="#febc2e"/><circle cx="60" cy="17.5" r="6" fill="#28c840"/>
<text x="{(WIDTH - title_w) / 2:.1f}" y="22" fill="var(--dim)" textLength="{title_w}" style="font-size:12px">{escape(title)}</text>
{chr(10).join(body)}
</svg>
"""


# --- readme -------------------------------------------------------------------

def replace_block(text: str, name: str, content: str) -> str:
    pattern = re.compile(rf"(<!--{name}:START-->).*?(<!--{name}:END-->)", re.S)
    return pattern.sub(lambda m: f"{m.group(1)}\n{content}\n{m.group(2)}", text)


def update_readme(s: dict, now: datetime) -> None:
    path = ROOT / "README.md"
    if s["log"]:
        lines = [f"{day}  {verb:<19} {repo}" for day, verb, repo in s["log"]]
    else:
        lines = ["(no signal)"]
    readme = path.read_text(encoding="utf-8")
    readme = replace_block(readme, "LOG", "```text\n" + "\n".join(lines) + "\n```")
    readme = replace_block(readme, "UPDATED", f"<sub>last refreshed {now:%Y-%m-%d %H:%M} UTC</sub>")
    path.write_text(readme, encoding="utf-8", newline="\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="render with fake data, no network")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    token = os.environ.get("GITHUB_TOKEN")
    if not args.sample and not token:
        raise SystemExit("GITHUB_TOKEN is not set (use --sample for an offline render)")
    data = sample(cfg["login"], now) if args.sample else fetch(cfg["login"], token)

    s = stats(data, now)
    rows = build_rows(cfg, s, now)
    (ROOT / "assets").mkdir(exist_ok=True)
    for name, theme in THEMES.items():
        (ROOT / "assets" / f"terminal-{name}.svg").write_text(
            render_svg(rows, theme, cfg), encoding="utf-8", newline="\n")
    update_readme(s, now)
    print(f"streak={s['current']} longest={s['longest']} year={s['year_total']} log={len(s['log'])}")


if __name__ == "__main__":
    main()
