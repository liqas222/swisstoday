"""Diagnostic: print raw X API metrics for one old + one recent tweet."""
import tweepy
from config import load_config

cfg = load_config()
client = tweepy.Client(
    bearer_token=cfg.x_bearer_token, consumer_key=cfg.x_api_key,
    consumer_secret=cfg.x_api_secret, access_token=cfg.x_access_token,
    access_token_secret=cfg.x_access_token_secret, wait_on_rate_limit=False,
)

# One old tweet (had 588 views) + a couple recent ones
ids = ["2070895632462549336", "2070921972314239326"]  # 27.6 tweets with real views
import sqlite3
db = sqlite3.connect(cfg.db_path)
recent = db.execute(
    "SELECT tweet_id FROM seen_items WHERE posted_at IS NOT NULL "
    "AND tweet_id GLOB '[0-9]*' ORDER BY posted_at DESC LIMIT 3"
).fetchall()
ids += [r[0] for r in recent]

print("Fetching:", ids)
resp = client.get_tweets(
    ids=ids,
    tweet_fields=["public_metrics", "non_public_metrics", "organic_metrics", "created_at"],
    user_auth=True,
)
if resp.errors:
    print("\n=== ERRORS ===")
    for e in resp.errors:
        print(e)
if resp.data:
    for t in resp.data:
        print(f"\n--- {t.id} ({t.created_at}) ---")
        print("  non_public:", t.non_public_metrics)
        print("  organic   :", t.organic_metrics)
        print("  public    :", t.public_metrics)
