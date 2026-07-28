"""Chat usefulness scoring for research/scraping triage.

Adapted from tg_chat_scorer.py: ranks chats by recency, sender diversity,
topic relevance, link density, and a noise denylist. Reads the tg-cli messages.db
(local-first cache) so it works offline after a sync.
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("DB_PATH", Path.home() / ".local/share/tg-cli/messages.db"))

TOPIC_RE = re.compile(
    r"(https?://|course|job|hire|internship|rust|python|java|ai|ml|llm|"
    r"kubernetes|devops|aws|gcp|azure|github|tutorial|scholarship|cert|"
    r"paper|arxiv|book|free|repo|dataset|opensource|code|sql|linux|security)",
    re.IGNORECASE,
)
NOISE_RE = re.compile(
    r"(prayer|loveworld|telecom|scribd|issuu|slideshare|downloader|"
    r"directors global|influencers|miracle|church|prophe|testimony|"
    r"earn money|crypto signal|forex|investment scheme|airdrop|"
    r"leak|premium course|dm admin|whatsapp group|t\.me/)",
    re.IGNORECASE,
)
NOW = datetime.now(timezone.utc)


def score_chats(db_path: str, topics: str | None = None, min_score: float = 0.0, top: int | None = None):
    topic_filter = None
    if topics:
        parts = re.split(r"[,\s]+", topics.strip())
        topic_filter = re.compile("(" + "|".join(re.escape(p) for p in parts if p) + ")", re.IGNORECASE)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT chat_id, chat_name, COUNT(*) AS msg_count,
                  COUNT(DISTINCT sender_id) AS unique_senders,
                  MAX(timestamp) AS last_msg,
                  SUM(CASE WHEN content LIKE '%http%' THEN 1 ELSE 0 END) AS link_msgs
           FROM messages GROUP BY chat_id"""
    ).fetchall()

    scored = []
    for r in rows:
        mc = r["msg_count"]
        if mc == 0:
            continue
        unique = r["unique_senders"] or 1
        age_days = max((NOW - datetime.fromisoformat(r["last_msg"].replace("Z", "+00:00"))).total_seconds() / 86400, 0.0)
        recency = max(0.0, 100.0 - (age_days / 60.0) * 100.0)
        diversity = min(100.0, (unique / mc) * 300.0)
        links = r["link_msgs"] or 0
        link_density = min(100.0, (links / mc) * 200.0)

        contents = [row["content"] or "" for row in conn.execute(
            "SELECT content FROM messages WHERE chat_id=?", (r["chat_id"],))]
        topic_hits = sum(1 for t in contents if TOPIC_RE.search(t))
        topic_density = min(100.0, (topic_hits / mc) * 150.0)

        rel_pct = 0.0
        if topic_filter is not None:
            rel_hits = sum(1 for t in contents if topic_filter.search(t))
            rel_pct = (rel_hits / mc) * 100.0
            topic_density = min(100.0, rel_pct * 2.0)

        noise_hits = sum(1 for t in contents if NOISE_RE.search(t))
        noise_ratio = noise_hits / mc

        bonus = 10.0 if 0.05 < (unique / mc) < 0.8 else 0.0
        total = round(0.35 * recency + 0.25 * diversity + 0.20 * topic_density + 0.20 * link_density + bonus, 1)
        if topic_filter is not None and rel_pct < 15.0:
            total = min(total, 30.0)
        if noise_ratio > 0.25:
            total = min(total, 25.0)

        scored.append({
            "chat_id": r["chat_id"], "chat_name": r["chat_name"], "score": total,
            "msgs": mc, "senders": unique, "age_days": round(age_days, 1),
            "links": links, "topic_hits": topic_hits,
            "verdict": "USEFUL" if total >= 55 else ("OK" if total >= 35 else "NOISE"),
        })

    conn.close()
    scored.sort(key=lambda x: x["score"], reverse=True)
    if min_score:
        scored = [s for s in scored if s["score"] >= min_score]
    if top:
        scored = scored[:top]
    return scored


def format_table(scored: list[dict]) -> str:
    lines = ["SCORE  VERDICT   MSGS  SND  AGE(d)  LK   TPC  CHAT", "-" * 86]
    for s in scored:
        lines.append(
            f"{s['score']:>6}  {s['verdict']:<8} {s['msgs']:>4} {s['senders']:>4} "
            f"{s['age_days']:>7} {s['links']:>4} {s['topic_hits']:>4}  {s['chat_name']}"
        )
    lines.append(f"\nTotal chats scored: {len(scored)}")
    return "\n".join(lines)
