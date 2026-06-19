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


def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("dash_token")
        if token != DASHBOARD_PASSWORD:
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = False
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == DASHBOARD_PASSWORD:
            resp = make_response(redirect("/"))
            resp.set_cookie("dash_token", pw, max_age=86400 * 30, httponly=True)
            return resp
        error = True
    return render_template("login.html", error=error)


def query_db(sql, params=()):
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
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
    return render_template("dashboard.html")


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
    last_run = query_db("SELECT run_at FROM run_log ORDER BY id DESC LIMIT 1")
    bot = get_bot_status()
    return jsonify({
        "total": total[0]["c"] if total else 0,
        "high": high[0]["c"] if high else 0,
        "posted": posted[0]["c"] if posted else 0,
        "today_new": today_new[0]["c"] if today_new else 0,
        "today_high": today_high[0]["c"] if today_high else 0,
        "today_posted": today_posted[0]["c"] if today_posted else 0,
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
        "SELECT source_id, title, url, post_text, posted_at, tweet_id "
        "FROM seen_items WHERE posted_at IS NOT NULL "
        "AND tweet_id NOT IN ('skipped_history','dry_run','duplicate_topic') "
        "AND posted_at > datetime('now', ? || ' days') "
        "ORDER BY posted_at DESC",
        (f"-{days}",)
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


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True})


@app.route("/api/stream")
@require_auth
def api_stream():
    def generate():
        try:
            with open(LOG_PATH, "r") as f:
                f.seek(0, 2)  # seek to end
                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {line.rstrip()}\n\n"
                    else:
                        import time as _time
                        _time.sleep(0.5)
        except GeneratorExit:
            pass
        except Exception:
            yield "data: [stream error]\n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
