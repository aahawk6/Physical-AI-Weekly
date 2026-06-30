#!/usr/bin/env python3
"""Weekly Physical AI digest: fetches from many sources, synthesizes with Claude, sends via Gmail."""

import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
import feedparser
import requests

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

RSS_FEEDS = [
    # Industry / news
    ("IEEE Spectrum Robotics", "https://spectrum.ieee.org/feeds/topic/robotics.rss"),
    ("TechCrunch Robotics", "https://techcrunch.com/tag/robotics/feed/"),
    ("The Robot Report", "https://www.therobotreport.com/feed/"),
    ("MIT News – AI", "https://news.mit.edu/rss/topic/artificial-intelligence2"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Wired – AI", "https://www.wired.com/feed/tag/artificial-intelligence/rss"),
    # Research blogs
    ("Google DeepMind Blog", "https://deepmind.google/blog/rss.xml"),
    ("CMU RI News", "https://www.ri.cmu.edu/feed/"),
]

PHYSICAL_AI_KEYWORDS = [
    "robot", "robotic", "embodied", "humanoid", "manipulation", "locomotion",
    "physical ai", "physical intelligence", "dexterous", "actuator", "gripper",
    "sim-to-real", "sim2real", "imitation learning", "policy learning",
    "motion planning", "boston dynamics", "figure ai", "1x technologies",
    "agility robotics", "unitree", "apptronik", "tesla optimus", "sanctuary",
    "physical intelligence", "legged", "bipedal", "quadruped", "arm",
]

HN_QUERIES = [
    "humanoid robot", "embodied AI", "robot learning", "physical AI",
    "Boston Dynamics", "Figure AI", "robot manipulation",
]

GITHUB_TOPICS = [
    "embodied-ai", "robot-learning", "humanoid-robot",
    "robotic-manipulation", "sim-to-real", "legged-robot",
]


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def _is_relevant(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in PHYSICAL_AI_KEYWORDS)


def fetch_rss(label: str, url: str, days: int = 7) -> list[dict]:
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:30]:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            if not _is_relevant(title + " " + summary):
                continue
            items.append({
                "source": label,
                "title": title,
                "summary": summary[:400],
                "url": entry.get("link", ""),
            })
        return items[:8]
    except Exception as exc:
        print(f"  [warn] RSS failed ({label}): {exc}")
        return []


def fetch_arxiv(days: int = 7) -> list[dict]:
    """Pull recent cs.RO papers plus AI papers mentioning robotics."""
    queries = [
        ("cs.RO", "cat:cs.RO"),
        ("cs.AI+robots", "cat:cs.AI AND (ti:robot OR ti:embodied OR ti:manipulation OR ti:locomotion)"),
    ]
    results, seen = [], set()
    for label, q in queries:
        try:
            r = requests.get(
                "http://export.arxiv.org/api/query",
                params={"search_query": q, "start": 0, "max_results": 15,
                        "sortBy": "submittedDate", "sortOrder": "descending"},
                timeout=20,
            )
            feed = feedparser.parse(r.text)
            for entry in feed.entries:
                url = entry.get("link", "")
                if url in seen:
                    continue
                seen.add(url)
                results.append({
                    "source": f"Arxiv ({label})",
                    "title": entry.title.replace("\n", " ").strip(),
                    "summary": entry.summary[:400].replace("\n", " ").strip(),
                    "url": url,
                    "authors": ", ".join(a.name for a in entry.get("authors", [])[:3]),
                })
        except Exception as exc:
            print(f"  [warn] Arxiv query failed ({label}): {exc}")
    return results


def fetch_hn(days: int = 7) -> list[dict]:
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    results, seen = [], set()
    for query in HN_QUERIES:
        try:
            r = requests.get(
                "https://hn.algolia.com/api/v1/search",
                params={"query": query, "tags": "story",
                        "numericFilters": f"created_at_i>{cutoff}", "hitsPerPage": 8},
                timeout=10,
            )
            for hit in r.json().get("hits", []):
                oid = hit.get("objectID")
                if not oid or oid in seen:
                    continue
                seen.add(oid)
                results.append({
                    "source": "Hacker News",
                    "title": hit.get("title", ""),
                    "url": hit.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                    "points": hit.get("points", 0),
                    "comments": hit.get("num_comments", 0),
                })
        except Exception as exc:
            print(f"  [warn] HN query failed ({query}): {exc}")
    return sorted(results, key=lambda x: x["points"], reverse=True)[:12]


def fetch_github() -> list[dict]:
    headers = {}
    if token := os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    results, seen = [], set()
    for topic in GITHUB_TOPICS:
        try:
            r = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": f"topic:{topic}", "sort": "updated", "order": "desc", "per_page": 5},
                headers=headers,
                timeout=10,
            )
            for repo in r.json().get("items", []):
                name = repo["full_name"]
                if name in seen:
                    continue
                seen.add(name)
                results.append({
                    "source": "GitHub",
                    "name": name,
                    "description": repo.get("description", ""),
                    "url": repo["html_url"],
                    "stars": repo.get("stargazers_count", 0),
                    "topic": topic,
                })
        except Exception as exc:
            print(f"  [warn] GitHub topic failed ({topic}): {exc}")
    return sorted(results, key=lambda x: x["stars"], reverse=True)[:10]


def fetch_company_news(days: int = 7) -> list[dict]:
    """Targeted searches for major Physical AI companies via HN + web."""
    companies = [
        "Figure AI", "Physical Intelligence pi", "1X Technologies",
        "Agility Robotics", "Apptronik", "Unitree Robotics",
        "Boston Dynamics", "Tesla Optimus", "Sanctuary AI",
    ]
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    results, seen = [], set()
    for company in companies:
        try:
            r = requests.get(
                "https://hn.algolia.com/api/v1/search",
                params={"query": company, "tags": "story",
                        "numericFilters": f"created_at_i>{cutoff}", "hitsPerPage": 3},
                timeout=8,
            )
            for hit in r.json().get("hits", []):
                oid = hit.get("objectID")
                if not oid or oid in seen or not hit.get("title"):
                    continue
                seen.add(oid)
                results.append({
                    "source": f"HN – {company}",
                    "title": hit["title"],
                    "url": hit.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                    "points": hit.get("points", 0),
                })
        except Exception as exc:
            print(f"  [warn] Company search failed ({company}): {exc}")
    return sorted(results, key=lambda x: x["points"], reverse=True)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def gather_all(days: int = 7) -> str:
    sections = []

    # Arxiv
    print("  Fetching Arxiv...")
    papers = fetch_arxiv(days)
    if papers:
        block = "=== ARXIV PAPERS ===\n"
        for p in papers:
            block += f"- {p['title']}\n  Authors: {p['authors']}\n  {p['summary']}\n  {p['url']}\n\n"
        sections.append(block)

    # RSS feeds
    for label, url in RSS_FEEDS:
        print(f"  Fetching {label}...")
        items = fetch_rss(label, url, days)
        if items:
            block = f"=== {label.upper()} ===\n"
            for it in items:
                block += f"- {it['title']}\n  {it['summary']}\n  {it['url']}\n\n"
            sections.append(block)

    # Company-specific news
    print("  Fetching company news...")
    company_hits = fetch_company_news(days)
    if company_hits:
        block = "=== COMPANY NEWS (via HN) ===\n"
        for h in company_hits:
            block += f"- [{h['points']} pts] {h['title']}  ({h['source']})\n  {h['url']}\n\n"
        sections.append(block)

    # Hacker News general
    print("  Fetching Hacker News...")
    hn = fetch_hn(days)
    if hn:
        block = "=== HACKER NEWS ===\n"
        for h in hn:
            block += f"- [{h['points']} pts, {h['comments']} comments] {h['title']}\n  {h['url']}\n\n"
        sections.append(block)

    # GitHub
    print("  Fetching GitHub repos...")
    repos = fetch_github()
    if repos:
        block = "=== GITHUB REPOS ===\n"
        for repo in repos:
            block += f"- {repo['name']} ({repo['stars']}★) [{repo['topic']}]\n  {repo['description']}\n  {repo['url']}\n\n"
        sections.append(block)

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Claude synthesis
# ---------------------------------------------------------------------------

DIGEST_PROMPT = """\
You are an expert editor producing a weekly Physical AI digest email.

Physical AI covers: robotics, embodied AI, humanoid robots, robot manipulation,
locomotion, sim-to-real transfer, robot learning, and physical intelligence systems.

Today's date: {today}

Below is raw content gathered this week from Arxiv, IEEE Spectrum, TechCrunch,
DeepMind blog, MIT News, Hacker News, GitHub, and targeted company searches.

---
{raw_content}
---

Write a polished weekly digest as HTML (inner body content only — no <html>/<head>/<body> tags).
Use clean inline styles suitable for email. Structure:

<h2>TL;DR</h2>
3–4 sentence executive summary of the most important developments this week.

<h2>Research Highlights</h2>
Top 3–5 papers with a sentence on why each matters. Link titles.

<h2>Industry & Company News</h2>
Notable announcements, products, demos, or funding. Link sources.

<h2>Community Picks</h2>
Interesting HN discussions (high-points threads) and GitHub repos worth watching.

<h2>Trend to Watch</h2>
1–2 sentences on an emerging pattern across this week's content.

Guidelines:
- Explain WHY things matter, not just what happened.
- Omit anything not clearly relevant to Physical AI.
- Keep each section tight — quality over quantity.
- Inline styles only; keep design minimal and readable in Gmail.
- Include hyperlinks on all sources/titles.
"""


def generate_digest(raw: str) -> str:
    client = anthropic.Anthropic()
    today = datetime.now().strftime("%B %d, %Y")
    msg = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4096,
        messages=[{"role": "user", "content": DIGEST_PROMPT.format(today=today, raw_content=raw)}],
    )
    return msg.content[0].text


# ---------------------------------------------------------------------------
# Email send
# ---------------------------------------------------------------------------

def send_email(subject: str, html_body: str) -> None:
    gmail_user = os.environ["GMAIL_USER"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ["RECIPIENT_EMAIL"]

    wrapper = f"""
    <div style="max-width:680px;margin:0 auto;font-family:Georgia,serif;color:#1a1a1a;line-height:1.6;">
      <div style="border-bottom:3px solid #111;padding-bottom:12px;margin-bottom:24px;">
        <span style="font-size:11px;text-transform:uppercase;letter-spacing:2px;color:#666;">Physical AI Weekly</span>
        <h1 style="margin:4px 0 0;font-size:22px;">{subject}</h1>
      </div>
      {html_body}
      <div style="border-top:1px solid #ddd;margin-top:32px;padding-top:12px;font-size:11px;color:#999;">
        Generated by Claude + fetched from Arxiv, IEEE Spectrum, TechCrunch, HN, GitHub, and more.
      </div>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Physical AI Digest <{gmail_user}>"
    msg["To"] = recipient
    msg.attach(MIMEText("This digest requires an HTML email client.", "plain"))
    msg.attach(MIMEText(wrapper, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, app_password)
        server.sendmail(gmail_user, recipient, msg.as_string())

    print(f"Sent to {recipient}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Gathering content from all sources...")
    raw = gather_all(days=7)
    print(f"Collected {len(raw):,} chars across all sources")

    if len(raw) < 500:
        print("Warning: very little content gathered — check source availability")

    print("Synthesizing digest with Claude...")
    html = generate_digest(raw)

    date_str = datetime.now().strftime("%B %d, %Y")
    subject = f"Physical AI Weekly — {date_str}"

    print("Sending email...")
    send_email(subject, html)
    print("Done.")
