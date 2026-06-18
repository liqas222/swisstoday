"""Quick test: fetch sources + AI scoring, no posting, no scheduler."""
import logging
import os
import time

import anthropic
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

import monitor
import ai_processor

def main():
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=0)
    model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

    logger.info("Fetching all sources...")
    all_items = monitor.fetch_all_sources()
    logger.info("Total items fetched: %d", len(all_items))

    # Deduplicate by guid for this test run
    seen = set()
    unique = []
    for item in all_items:
        if item["guid"] not in seen:
            seen.add(item["guid"])
            unique.append(item)

    logger.info("Unique items: %d — scoring first 20 with AI...", len(unique))

    high_items = []
    for item in unique[:20]:
        relevance, reason = ai_processor.score_relevance(client, model, item)
        status = "🔴 HIGH" if relevance == "HIGH" else "⚪ LOW"
        print(f"\n{status} [{item['source_id']}] {item['title'][:80]}")
        print(f"   Grund: {reason}")
        if relevance == "HIGH":
            high_items.append(item)
        time.sleep(0.5)

    print(f"\n\n{'='*60}")
    print(f"HIGH relevance items: {len(high_items)}")
    print(f"{'='*60}")

    for item in high_items:
        print(f"\nGeneriere Post für: {item['title'][:60]}...")
        post = ai_processor.generate_post(client, model, item)
        print(f"\n--- POST ---\n{post}\n--- END ---")
        time.sleep(0.5)

if __name__ == "__main__":
    main()
