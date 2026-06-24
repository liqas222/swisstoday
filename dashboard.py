import os
import sqlite3
import subprocess
import functools
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request, Response, make_response, redirect
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
DB_PATH = os.getenv("DB_PATH", "swissintel.db")
LOG_PATH = os.getenv("LOG_PATH", "bot.log")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "swiss2024")
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "15"))
# Path prefix when served behind a reverse proxy (e.g. Tailscale "/intel"). Empty = root.
BASE_PATH = os.getenv("BASE_PATH", "").rstrip("/")


def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("dash_token")
        if token != DASHBOARD_PASSWORD:
            return redirect(BASE_PATH + "/login")
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = False
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == DASHBOARD_PASSWORD:
            resp = make_response(redirect(BASE_PATH + "/"))
            resp.set_cookie("dash_token", pw, max_age=86400 * 30, httponly=True)
            return resp
        error = True
    return render_template("login.html", error=error, base_path=BASE_PATH)


def _migrate_db():
    """Add columns and apply category merges."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(seen_items)").fetchall()]
        for col, typedef in [("category", "TEXT"), ("views", "INTEGER"), ("viral_score", "INTEGER")]:
            if col not in cols:
                conn.execute(f"ALTER TABLE seen_items ADD COLUMN {col} {typedef}")
        # Merge Banken → Finanzen
        conn.execute("UPDATE seen_items SET category='Finanzen' WHERE category='Banken'")
        conn.commit()
        conn.close()
    except Exception:
        pass


def query_db(sql, params=()):
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        app.logger.error("query_db error: %s | sql: %s", e, sql[:120])
        return []


def get_bot_status():
    try:
        result = subprocess.run(
            ["pgrep", "-f", "main.py"], capture_output=True, text=True
        )
        running = result.returncode == 0
        pid = result.stdout.strip().split("\n")[0] if running else None
        return {"running": running, "pid": pid}
    except Exception:
        return {"running": False, "pid": None}


@app.route("/")
@require_auth
def index():
    return render_template("dashboard.html", base_path=BASE_PATH)


@app.route("/api/stats")
@require_auth
def api_stats():
    total = query_db("SELECT COUNT(*) as c FROM seen_items")
    high = query_db("SELECT COUNT(*) as c FROM seen_items WHERE relevance='HIGH'")
    posted = query_db("SELECT COUNT(*) as c FROM seen_items WHERE posted_at IS NOT NULL AND tweet_id NOT IN ('skipped_history', 'dry_run', 'duplicate_topic')")
    # Today stats using Swiss time offset (+2h)
    today_new = query_db("SELECT COUNT(*) as c FROM seen_items WHERE date(fetched_at, '+2 hours')=date('now', '+2 hours')")
    today_high = query_db("SELECT COUNT(*) as c FROM seen_items WHERE relevance='HIGH' AND date(fetched_at, '+2 hours')=date('now', '+2 hours')")
    today_posted = query_db("SELECT COUNT(*) as c FROM seen_items WHERE posted_at IS NOT NULL AND tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic') AND date(posted_at, '+2 hours')=date('now', '+2 hours')")
    today_views = query_db("SELECT COALESCE(SUM(views),0) as c FROM seen_items WHERE posted_at IS NOT NULL AND tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic') AND views IS NOT NULL AND date(posted_at, '+2 hours')=date('now', '+2 hours')")
    last_run = query_db("SELECT run_at FROM run_log ORDER BY id DESC LIMIT 1")
    bot = get_bot_status()
    return jsonify({
        "total": total[0]["c"] if total else 0,
        "high": high[0]["c"] if high else 0,
        "posted": posted[0]["c"] if posted else 0,
        "today_new": today_new[0]["c"] if today_new else 0,
        "today_high": today_high[0]["c"] if today_high else 0,
        "today_posted": today_posted[0]["c"] if today_posted else 0,
        "today_views": today_views[0]["c"] if today_views else 0,
        "last_run": last_run[0]["run_at"] if last_run else None,
        "bot_running": bot["running"],
        "bot_pid": bot["pid"],
        "interval_seconds": CHECK_INTERVAL_MINUTES * 60,
    })


@app.route("/api/runs")
@require_auth
def api_runs():
    rows = query_db(
        "SELECT id, run_at, fetched, new_items, high_relevance, posted, errors "
        "FROM run_log ORDER BY id DESC LIMIT 20"
    )
    return jsonify(list(reversed(rows)))


@app.route("/api/items/posted")
@require_auth
def api_posted():
    days = int(request.args.get("days", 1))
    rows = query_db(
        "SELECT source_id, title, url, post_text, posted_at, tweet_id, category "
        "FROM seen_items WHERE posted_at IS NOT NULL "
        "AND (tweet_id IS NULL OR tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic')) "
        "AND posted_at > datetime('now', ? || ' days') "
        "ORDER BY posted_at DESC",
        (f"-{days}",)
    )
    return jsonify(rows)


@app.route("/api/chart")
@require_auth
def api_chart():
    daily = query_db(
        "SELECT date(posted_at, '+2 hours') as day, COUNT(*) as count "
        "FROM seen_items WHERE posted_at IS NOT NULL "
        "AND (tweet_id IS NULL OR tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic')) "
        "AND posted_at > datetime('now', '-30 days') "
        "GROUP BY day ORDER BY day"
    )
    cats = query_db(
        "SELECT COALESCE(category,'Sonstiges') as category, COUNT(*) as count "
        "FROM seen_items WHERE posted_at IS NOT NULL "
        "AND (tweet_id IS NULL OR tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic')) "
        "AND posted_at > datetime('now', '-30 days') "
        "GROUP BY COALESCE(category,'Sonstiges') ORDER BY count DESC"
    )
    srcs = query_db(
        "SELECT source_id, COUNT(*) as count "
        "FROM seen_items WHERE posted_at IS NOT NULL "
        "AND (tweet_id IS NULL OR tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic')) "
        "AND posted_at > datetime('now', '-30 days') "
        "GROUP BY source_id ORDER BY count DESC"
    )
    daily_views = query_db(
        "SELECT date(posted_at, '+2 hours') as day, COALESCE(SUM(views),0) as views "
        "FROM seen_items WHERE posted_at IS NOT NULL AND views IS NOT NULL "
        "AND (tweet_id IS NULL OR tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic')) "
        "AND posted_at > datetime('now', '-30 days') "
        "GROUP BY day ORDER BY day"
    )
    return jsonify({"daily": daily, "categories": cats, "sources": srcs, "daily_views": daily_views})


@app.route("/api/items/today")
@require_auth
def api_posted_today():
    rows = query_db(
        "SELECT source_id, title, url, post_text, posted_at, tweet_id, category "
        "FROM seen_items WHERE posted_at IS NOT NULL "
        "AND (tweet_id IS NULL OR tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic')) "
        "AND date(posted_at, '+2 hours') = date('now', '+2 hours') "
        "ORDER BY posted_at DESC"
    )
    return jsonify(rows)


@app.route("/api/sources/today")
@require_auth
def api_sources_today():
    rows = query_db(
        "SELECT source_id, "
        "SUM(CASE WHEN date(fetched_at,'+2 hours')=date('now','+2 hours') THEN 1 ELSE 0 END) as today_total, "
        "SUM(CASE WHEN relevance='HIGH' AND date(fetched_at,'+2 hours')=date('now','+2 hours') THEN 1 ELSE 0 END) as today_high, "
        "SUM(CASE WHEN posted_at IS NOT NULL AND tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic') AND date(posted_at,'+2 hours')=date('now','+2 hours') THEN 1 ELSE 0 END) as today_posted "
        "FROM seen_items GROUP BY source_id "
        "HAVING today_total > 0 ORDER BY today_total DESC"
    )
    return jsonify(rows)


@app.route("/api/items/queue")
@require_auth
def api_queue():
    rows = query_db(
        "SELECT id, source_id, title, url, post_text, category, viral_score, fetched_at "
        "FROM seen_items WHERE relevance='HIGH' AND posted_at IS NULL "
        "AND error IS NULL AND post_text IS NOT NULL "
        "ORDER BY COALESCE(viral_score,0) DESC, id ASC"
    )
    return jsonify(rows)


@app.route("/api/items/high")
@require_auth
def api_high():
    rows = query_db(
        "SELECT source_id, title, url, relevance_reason, post_text, posted_at, tweet_id, fetched_at "
        "FROM seen_items WHERE relevance='HIGH' ORDER BY id DESC LIMIT 50"
    )
    return jsonify(rows)


@app.route("/api/items/low")
@require_auth
def api_low():
    rows = query_db(
        "SELECT source_id, title, url, relevance_reason, fetched_at "
        "FROM seen_items WHERE relevance='LOW' ORDER BY id DESC LIMIT 50"
    )
    return jsonify(rows)


@app.route("/api/sources")
@require_auth
def api_sources():
    rows = query_db(
        "SELECT source_id, COUNT(*) as total, "
        "SUM(CASE WHEN relevance='HIGH' THEN 1 ELSE 0 END) as high_count, "
        "SUM(CASE WHEN posted_at IS NOT NULL AND tweet_id != 'skipped_history' THEN 1 ELSE 0 END) as posted_count "
        "FROM seen_items GROUP BY source_id ORDER BY total DESC"
    )
    return jsonify(rows)


@app.route("/api/log")
@require_auth
def api_log():
    try:
        with open(LOG_PATH, "r") as f:
            lines = f.readlines()
        return jsonify({
            "lines": [l.rstrip() for l in lines[-50:]],
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    except FileNotFoundError:
        return jsonify({"lines": ["Log file not found"], "timestamp": ""})


@app.route("/api/views")
@require_auth
def api_views():
    by_cat = query_db(
        "SELECT COALESCE(category,'Sonstiges') as category, "
        "SUM(views) as total_views, COUNT(*) as tweet_count, "
        "ROUND(AVG(views)) as avg_views "
        "FROM seen_items WHERE posted_at IS NOT NULL AND views IS NOT NULL "
        "AND (tweet_id IS NULL OR tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic')) "
        "AND posted_at > datetime('now', '-30 days') "
        "GROUP BY COALESCE(category,'Sonstiges') ORDER BY total_views DESC"
    )
    return jsonify(by_cat)


@app.route("/api/insights")
@require_auth
def api_insights():
    import anthropic as _anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"tips": [], "error": "No API key"})

    # Gather stats for Claude to analyze
    by_cat = query_db(
        "SELECT COALESCE(category,'Sonstiges') as cat, COUNT(*) as tweets, "
        "COALESCE(SUM(views),0) as views, COALESCE(ROUND(AVG(views)),0) as avg_views "
        "FROM seen_items WHERE posted_at IS NOT NULL "
        "AND (tweet_id IS NULL OR tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic')) "
        "AND posted_at > datetime('now', '-30 days') "
        "GROUP BY COALESCE(category,'Sonstiges') ORDER BY avg_views DESC"
    )
    top_tweets = query_db(
        "SELECT title, post_text, views, COALESCE(category,'Sonstiges') as category "
        "FROM seen_items WHERE posted_at IS NOT NULL AND views IS NOT NULL "
        "AND (tweet_id IS NULL OR tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic')) "
        "ORDER BY views DESC LIMIT 5"
    )
    low_tweets = query_db(
        "SELECT title, post_text, views, COALESCE(category,'Sonstiges') as category "
        "FROM seen_items WHERE posted_at IS NOT NULL AND views IS NOT NULL "
        "AND (tweet_id IS NULL OR tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic')) "
        "ORDER BY views ASC LIMIT 5"
    )

    total = sum(r["tweets"] for r in by_cat)
    if total == 0:
        return jsonify({"tips": [], "tweet_count": 0})

    has_views = any(r["views"] > 0 for r in by_cat)

    cat_summary = "\n".join(
        f"- {r['cat']}: {r['tweets']} Tweets, {r['views']} Views total, ∅{r['avg_views']} Views/Tweet"
        for r in by_cat
    )
    top_summary = "\n".join(
        f"- [{r['category']}] {r['views']} Views: {(r['title'] or '')[:80]}"
        for r in top_tweets if r.get("views")
    )
    low_summary = "\n".join(
        f"- [{r['category']}] {r['views']} Views: {(r['title'] or '')[:80]}"
        for r in low_tweets if r.get("views") is not None
    )

    prompt = f"""Du bist Social-Media-Stratege für den X-Account @SwissIntelNews (Schweizer Nachrichten für Unternehmer, Investoren, Expats).

Analysiere diese Tweet-Performance-Daten und gib 4-5 konkrete, umsetzbare Empfehlungen auf Deutsch, wie der Account mehr Views bekommen kann.

KATEGORIEN (letzte 30 Tage):
{cat_summary}

TOP 5 TWEETS (meiste Views):
{top_summary or '(keine Views-Daten verfügbar)'}

SCHWACHE TWEETS (wenigste Views):
{low_summary or '(keine Views-Daten verfügbar)'}

Regeln für deine Antwort:
- Direkt und konkret, keine Allgemeinplätze
- Basiere Empfehlungen auf den tatsächlichen Zahlen
- Fokus auf: Themen, Stil, Timing, Format
- Antworte NUR mit JSON: {{"tips": ["Tipp 1", "Tipp 2", "Tipp 3", "Tipp 4"]}}"""

    try:
        client = _anthropic.Anthropic(api_key=api_key, max_retries=0)
        resp = client.messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        import json as _json
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.splitlines()[1:-1])
        data = _json.loads(raw)
        return jsonify({
            "tips": data.get("tips", []),
            "tweet_count": total,
            "has_views": has_views,
            "generated_at": datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        })
    except Exception as e:
        return jsonify({"tips": [], "error": str(e)})


@app.route("/api/sync-views", methods=["POST"])
@require_auth
def api_sync_views():
    import tweepy
    import time as _time
    x_api_key = os.getenv("X_API_KEY", "")
    x_api_secret = os.getenv("X_API_SECRET", "")
    x_access_token = os.getenv("X_ACCESS_TOKEN", "")
    x_access_token_secret = os.getenv("X_ACCESS_TOKEN_SECRET", "")
    x_bearer_token = os.getenv("X_BEARER_TOKEN", "")
    if not x_bearer_token:
        return jsonify({"ok": False, "error": "X API nicht konfiguriert"}), 400
    try:
        conn = __import__("sqlite3").connect(DB_PATH)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(seen_items)").fetchall()]
        if "views" not in cols:
            conn.execute("ALTER TABLE seen_items ADD COLUMN views INTEGER")
            conn.commit()
        rows = conn.execute(
            "SELECT id, tweet_id FROM seen_items "
            "WHERE posted_at IS NOT NULL "
            "AND tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic') "
            "AND tweet_id IS NOT NULL ORDER BY posted_at DESC LIMIT 500"
        ).fetchall()
        if not rows:
            conn.close()
            return jsonify({"ok": True, "updated": 0})
        client = tweepy.Client(
            bearer_token=x_bearer_token, consumer_key=x_api_key,
            consumer_secret=x_api_secret, access_token=x_access_token,
            access_token_secret=x_access_token_secret, wait_on_rate_limit=False,
        )
        id_map = {r[1]: r[0] for r in rows}
        views_before = conn.execute("SELECT COALESCE(SUM(views),0) FROM seen_items WHERE tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic') AND views IS NOT NULL").fetchone()[0]
        updated = 0
        for i in range(0, len(rows), 100):
            batch = list(id_map.keys())[i:i+100]
            try:
                resp = client.get_tweets(ids=batch,
                    tweet_fields=["public_metrics","non_public_metrics","organic_metrics"],
                    user_auth=True)
                if resp.data:
                    for tweet in resp.data:
                        views = None
                        if tweet.non_public_metrics:
                            views = tweet.non_public_metrics.get("impression_count")
                        if views is None and tweet.organic_metrics:
                            views = tweet.organic_metrics.get("impression_count")
                        if views is None and tweet.public_metrics:
                            views = tweet.public_metrics.get("impression_count")
                        if views is not None:
                            db_id = id_map.get(str(tweet.id))
                            if db_id:
                                conn.execute("UPDATE seen_items SET views=? WHERE id=?", (views, db_id))
                                updated += 1
            except Exception as e:
                conn.close()
                return jsonify({"ok": False, "error": str(e)}), 500
            _time.sleep(1)
        conn.commit()
        views_after = conn.execute("SELECT COALESCE(SUM(views),0) FROM seen_items WHERE tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic') AND views IS NOT NULL").fetchone()[0]
        conn.close()
        new_views = max(0, views_after - views_before)
        return jsonify({"ok": True, "updated": updated, "new_views": new_views})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/followers")
@require_auth
def api_followers():
    import tweepy
    import database as _db
    try:
        client = tweepy.Client(
            bearer_token=os.getenv("X_BEARER_TOKEN", ""),
            consumer_key=os.getenv("X_API_KEY", ""),
            consumer_secret=os.getenv("X_API_SECRET", ""),
            access_token=os.getenv("X_ACCESS_TOKEN", ""),
            access_token_secret=os.getenv("X_ACCESS_TOKEN_SECRET", ""),
            wait_on_rate_limit=False,
        )
        me = client.get_me(user_fields=["public_metrics"], user_auth=True)
        if me.data:
            m = me.data.public_metrics
            count = m.get("followers_count", 0)
            try:
                _db.log_follower_count(DB_PATH, count)
            except Exception:
                pass
            return jsonify({
                "followers": count,
                "following": m.get("following_count", 0),
                "tweets": m.get("tweet_count", 0),
            })
        return jsonify({"followers": None})
    except Exception as e:
        return jsonify({"followers": None, "error": str(e)})


@app.route("/api/followers/history")
@require_auth
def api_followers_history():
    rows = query_db(
        "SELECT date, count FROM follower_log "
        "ORDER BY date DESC LIMIT 30"
    )
    rows = list(reversed(rows))
    return jsonify(rows)


@app.route("/api/trends")
@require_auth
def api_trends():
    try:
        import trends as _trends
        topics = _trends._cache.get("topics", [])
        return jsonify({"topics": topics})
    except Exception:
        return jsonify({"topics": []})


@app.route("/api/highlights")
@require_auth
def api_highlights():
    """Best-of statistics: top tweet, top source this week, best time of day."""
    EXCL = "('skipped_history','dry_run','duplicate_topic','archived')"

    best_tweet = query_db(
        f"SELECT title, post_text, views, COALESCE(category,'Sonstiges') as category, "
        f"source_id, tweet_id, posted_at "
        f"FROM seen_items WHERE posted_at IS NOT NULL AND views IS NOT NULL AND views > 0 "
        f"AND tweet_id NOT IN {EXCL} "
        f"ORDER BY views DESC LIMIT 1"
    )

    top_source_week = query_db(
        f"SELECT source_id, COUNT(*) as posts, COALESCE(SUM(views),0) as views "
        f"FROM seen_items WHERE posted_at IS NOT NULL "
        f"AND tweet_id NOT IN {EXCL} "
        f"AND posted_at > datetime('now', '-7 days') "
        f"GROUP BY source_id ORDER BY posts DESC, views DESC LIMIT 1"
    )

    best_hour = query_db(
        f"SELECT strftime('%H', posted_at, '+2 hours') as hour, "
        f"ROUND(AVG(views)) as avg_views, COUNT(*) as cnt "
        f"FROM seen_items WHERE posted_at IS NOT NULL AND views IS NOT NULL AND views > 0 "
        f"AND tweet_id NOT IN {EXCL} "
        f"GROUP BY hour HAVING cnt >= 2 ORDER BY avg_views DESC LIMIT 1"
    )

    return jsonify({
        "best_tweet": best_tweet[0] if best_tweet else None,
        "top_source_week": top_source_week[0] if top_source_week else None,
        "best_hour": best_hour[0] if best_hour else None,
    })


@app.route("/api/achievements")
@require_auth
def api_achievements():
    """Milestone badges based on real performance data."""
    EXCL = "('skipped_history','dry_run','duplicate_topic','archived')"

    followers_row = query_db("SELECT count FROM follower_log ORDER BY date DESC LIMIT 1")
    followers = followers_row[0]["count"] if followers_row else 0

    total_posted_row = query_db(
        f"SELECT COUNT(*) as c FROM seen_items WHERE posted_at IS NOT NULL "
        f"AND tweet_id NOT IN {EXCL}"
    )
    total_posted = total_posted_row[0]["c"] if total_posted_row else 0

    max_day_views_row = query_db(
        f"SELECT date(posted_at,'+2 hours') as day, COALESCE(SUM(views),0) as v "
        f"FROM seen_items WHERE posted_at IS NOT NULL AND views IS NOT NULL "
        f"AND tweet_id NOT IN {EXCL} "
        f"GROUP BY day ORDER BY v DESC LIMIT 1"
    )
    max_day_views = max_day_views_row[0]["v"] if max_day_views_row else 0

    total_views_row = query_db(
        f"SELECT COALESCE(SUM(views),0) as v FROM seen_items WHERE posted_at IS NOT NULL "
        f"AND views IS NOT NULL AND tweet_id NOT IN {EXCL}"
    )
    total_views = total_views_row[0]["v"] if total_views_row else 0

    # Posting streak: consecutive days (ending today/yesterday) with >=1 post
    days = query_db(
        f"SELECT DISTINCT date(posted_at,'+2 hours') as day "
        f"FROM seen_items WHERE posted_at IS NOT NULL AND tweet_id NOT IN {EXCL} "
        f"ORDER BY day DESC LIMIT 60"
    )
    day_set = {r["day"] for r in days}
    from datetime import timedelta
    streak = 0
    cursor = datetime.now(timezone.utc).date()
    # Allow streak to start today or yesterday
    if cursor.isoformat() not in day_set and (cursor - timedelta(days=1)).isoformat() in day_set:
        cursor = cursor - timedelta(days=1)
    while cursor.isoformat() in day_set:
        streak += 1
        cursor = cursor - timedelta(days=1)

    def milestone(value, tiers):
        """Return (achieved_tier, next_tier) for a value against a tier list."""
        achieved = 0
        nxt = tiers[-1]
        for t in tiers:
            if value >= t:
                achieved = t
            elif nxt == tiers[-1] or t < nxt:
                if t > value:
                    nxt = t
                    break
        return achieved, nxt

    badges = []

    # Follower milestones
    f_tiers = [10, 50, 100, 250, 500, 1000, 2500, 5000, 10000]
    f_ach, f_next = milestone(followers, f_tiers)
    badges.append({
        "id": "followers", "icon": "👥", "label": "Follower",
        "value": followers, "achieved": f_ach, "next": f_next,
        "unlocked": f_ach > 0,
        "title": f"{f_ach}+ Follower" if f_ach else "Erste Follower",
    })

    # Total posts milestones
    p_tiers = [10, 50, 100, 250, 500, 1000, 2500, 5000]
    p_ach, p_next = milestone(total_posted, p_tiers)
    badges.append({
        "id": "posts", "icon": "📢", "label": "Tweets gesamt",
        "value": total_posted, "achieved": p_ach, "next": p_next,
        "unlocked": p_ach > 0,
        "title": f"{p_ach}+ Tweets" if p_ach else "Erste Tweets",
    })

    # Views in a single day
    v_tiers = [100, 500, 1000, 5000, 10000, 50000, 100000]
    v_ach, v_next = milestone(max_day_views, v_tiers)
    badges.append({
        "id": "dayviews", "icon": "🔥", "label": "Views an einem Tag",
        "value": max_day_views, "achieved": v_ach, "next": v_next,
        "unlocked": v_ach > 0,
        "title": f"{v_ach}+ Views/Tag" if v_ach else "Erste Views",
    })

    # Total views
    tv_tiers = [1000, 10000, 50000, 100000, 500000, 1000000]
    tv_ach, tv_next = milestone(total_views, tv_tiers)
    badges.append({
        "id": "totalviews", "icon": "👁", "label": "Views gesamt",
        "value": total_views, "achieved": tv_ach, "next": tv_next,
        "unlocked": tv_ach > 0,
        "title": f"{tv_ach}+ Views" if tv_ach else "Erste Views",
    })

    # Posting streak
    s_tiers = [3, 7, 14, 30, 60, 100]
    s_ach, s_next = milestone(streak, s_tiers)
    badges.append({
        "id": "streak", "icon": "⚡", "label": "Streak",
        "value": streak, "achieved": s_ach, "next": s_next,
        "unlocked": streak >= 1,
        "title": f"{streak}-Tage-Streak" if streak >= 1 else "Streak starten",
    })

    return jsonify({"badges": badges})


@app.route("/api/slots/today")
@require_auth
def api_slots_today():
    import database as _db
    slots = _db.get_today_slots(DB_PATH, max_slots=5)
    return jsonify(slots)


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True})


@app.route("/api/stream")
@require_auth
def api_stream():
    def generate():
        import time as _time
        import subprocess as _sp
        # Try log file first, fall back to journalctl
        try:
            if LOG_PATH and os.path.exists(LOG_PATH):
                with open(LOG_PATH, "r") as f:
                    f.seek(0, 2)
                    while True:
                        line = f.readline()
                        if line:
                            yield f"data: {line.rstrip()}\n\n"
                        else:
                            _time.sleep(0.5)
            else:
                proc = _sp.Popen(
                    ["journalctl", "-u", "swissintel-bot", "-f", "-n", "50", "--no-pager", "-o", "cat"],
                    stdout=_sp.PIPE, stderr=_sp.DEVNULL, text=True
                )
                try:
                    for line in proc.stdout:
                        yield f"data: {line.rstrip()}\n\n"
                finally:
                    proc.terminate()
        except GeneratorExit:
            pass
        except Exception as e:
            yield f"data: [stream error: {e}]\n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class _StripPrefix:
    def __init__(self, wsgi_app, prefix):
        self.app = wsgi_app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path.startswith(self.prefix):
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
            environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + self.prefix
        return self.app(environ, start_response)


if BASE_PATH:
    app.wsgi_app = _StripPrefix(app.wsgi_app, BASE_PATH)


if __name__ == "__main__":
    _migrate_db()
    app.run(host="0.0.0.0", port=8081, debug=False, threaded=True)
