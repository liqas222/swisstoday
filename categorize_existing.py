"""
One-time script to retroactively assign categories to all posted tweets
that currently have category=NULL.
Run: python categorize_existing.py
"""
import json
import sqlite3
import time
import anthropic
from config import load_config

CATEGORIES = ["Finanzen", "Steuern", "Wirtschaft", "Politik", "Kriminalität",
              "Banken", "Recht", "Einwanderung", "Gesundheit", "Energie", "Sonstiges"]

SYSTEM = (
    "Du kategorisierst Schweizer Nachrichten-Tweets. "
    "Antworte NUR mit einer der folgenden Kategorien als JSON: "
    '{"category": "..."}\n'
    "Kategorien: " + ", ".join(CATEGORIES)
)


def get_category(client, model, title, post_text):
    content = f"Titel: {title}\n\nPost: {(post_text or '')[:400]}"
    for attempt in range(3):
        try:
            r = client.messages.create(
                model=model, max_tokens=64, system=SYSTEM,
                messages=[{"role": "user", "content": content}]
            )
            raw = r.content[0].text.strip()
            if raw.startswith("```"):
                raw = "\n".join(raw.splitlines()[1:-1])
            data = json.loads(raw)
            cat = data.get("category", "Sonstiges")
            return cat if cat in CATEGORIES else "Sonstiges"
        except anthropic.RateLimitError:
            print("  Rate limit, waiting 30s...")
            time.sleep(30)
        except Exception as e:
            print(f"  Error: {e}")
            time.sleep(5)
    return "Sonstiges"


def main():
    cfg = load_config()
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key, max_retries=0)

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, title, post_text FROM seen_items "
        "WHERE posted_at IS NOT NULL AND (category IS NULL OR category = '') "
        "ORDER BY id"
    ).fetchall()

    print(f"Found {len(rows)} tweets to categorize")
    if not rows:
        print("Nothing to do.")
        conn.close()
        return

    for i, row in enumerate(rows, 1):
        cat = get_category(client, cfg.claude_model, row["title"] or "", row["post_text"] or "")
        conn.execute("UPDATE seen_items SET category=? WHERE id=?", (cat, row["id"]))
        conn.commit()
        print(f"[{i}/{len(rows)}] #{row['id']} → {cat} | {(row['title'] or '')[:60]}")
        time.sleep(0.4)

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
