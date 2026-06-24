import json
import logging
import time
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

SCORE_SYSTEM = """Du bewertest Schweizer Nachrichten für einen exklusiven Intelligence-Feed. Sei EXTREM streng — nur wirklich wichtige Neuigkeiten verdienen HIGH. Im Zweifel immer LOW.

HIGH nur wenn ALLE diese Kriterien erfüllt sind:
1. Es ist eine NEUE Entwicklung (nicht Analyse/Kommentar/Meinung/Studie über etwas Bestehendes)
2. Es betrifft direkt die Schweiz (nicht nur internationale Nachrichten mit CH-Erwähnung)
3. Es handelt sich um ein BESCHLOSSENES FAKTUM — nicht um eine Prognose, Studie oder Meinung
4. Es ist ein konkretes Ereignis — kein Marktbericht, keine Analyse, kein Trend

HIGH-Beispiele (wirklich wichtig):
- Neues Gesetz/Verordnung verabschiedet mit konkretem Datum
- Abstimmungsresultat mit direkter Wirkung
- SNB/FINMA Entscheid mit Marktwirkung (z.B. Zinsänderung)
- Grosse Verhaftung, Mord, Gewaltverbrechen oder Strafprozess von öffentlichem Interesse
- Terroranschlag, Anschlagsversuch oder konkrete Bedrohungslage in der Schweiz
- Steueränderung konkret beschlossen
- Wichtige Unternehmenstransaktion (Übernahme, Konkurs, Börsengang)
- Politischer Skandal mit konkreten Folgen

ABSOLUT IMMER LOW — keine Ausnahmen:
- Immobilien-Analysen, Preistrends, Wohnungsmarkt-Berichte — IMMER LOW
- Meinungsartikel, Kommentare, Interviews, Ratgeber
- Internationale Nachrichten ohne direkte CH-Relevanz (Deutschland, EU ohne CH-Bezug)
- Sport, Kultur, Entertainment, Lifestyle
- Alles mit "könnte", "plant", "diskutiert", "erwartet", "prognostiziert", "dürfte"
- Vorstösse/Motionen die noch nicht beschlossen sind

HIGH wenn konkretes Ereignis in der Schweiz:
- Verhaftung, Verbrechen, Polizeieinsatz, Gerichtsurteil — IMMER HIGH
- Bundesgerichtsurteil, Strafprozess — IMMER HIGH
- Beschlossenes Gesetz/Verordnung, Abstimmungsresultat
- SNB/FINMA Entscheid, Zinsänderung
- Unternehmenstransaktion (Übernahme, Konkurs, Entlassung >100 Stellen)
- Politischer Skandal mit konkreten Folgen

Kategorien (wähle die passendste):
Finanzen, Steuern, Wirtschaft, Politik, Kriminalität, Recht, Einwanderung, Gesundheit, Energie, Sonstiges

Hinweis: Alles zu Banken, SNB, FINMA, Zinsen, Krediten → Finanzen

Antworte NUR mit validem JSON:
{"relevance": "HIGH" | "LOW", "reason": "kurze Begründung auf Deutsch (max 80 Zeichen)", "category": "eine der obigen Kategorien", "viral_score": 1-100}

viral_score (1-100) — Berechne so:
A) Betroffene Menschen: Trifft es fast alle Schweizer? (20 Punkte) | Viele? (12) | Eine Berufsgruppe? (6) | Wenige Spezialisten? (2)
B) Emotionale Wirkung: Wut/Freude/Schock? (25 Punkte) | Interesse? (15) | Neutral informativ? (8) | Trocken/technisch? (3)
C) Aktualität: Heute/diese Woche? (20 Punkte) | Diesen Monat? (12) | Älteres Thema? (5)
D) Trends: Thema steht in den X-TRENDS SCHWEIZ unten? (25 Punkte) | In GLOBAL-Trends? (15) | Kein Trend-Bezug? (0)
E) Konkretheit: Konkrete Zahl, Datum, Person? (10 Punkte) | Vage? (3)

Addiere A+B+C+D+E = viral_score (1-100). Rechne es WIRKLICH aus — nicht schätzen!"""

POST_SYSTEM = """Du erstellst X-Posts (Twitter) auf Deutsch für den Account @SwissIntelNews.
Zielgruppe: Unternehmer, Gründer, Investoren, Anwälte, Expats in der Schweiz.

Format (EXAKT so):
EMOJI HEADLINE

ERKLÄRUNG (4-5 Sätze)

HASHTAGS

HEADLINE-Regeln (wichtigste Regel überhaupt):
- Maximal 6-8 Wörter, knapp und direkt
- Nur die nackte Kernaussage — kein "Die", kein "wird", kein "hat"
- Mit konkreter Zahl falls vorhanden
- Beispiele guter Headlines:
  🔒 Zürcher Vermögensverwalter verhaftet
  ⚖️ Selbständige zahlen bis 30% mehr AHV
  📈 Schweizer BIP wächst 2.1%
  🏦 SNB senkt Leitzins auf 0%
  🗳️ Volksinitiative für 13. AHV-Rente angenommen

ERKLÄRUNG-Regeln:
- 4-5 Sätze, menschlich und direkt, kein Nachrichtenagentur-Ton
- Satz 1: Was ist passiert? (konkretes Ereignis/Entscheid)
- Satz 2: Warum ist das wichtig? (Hintergrund, Bedeutung)
- Satz 3: Was ändert sich konkret? (Zahlen, Daten, Fristen falls vorhanden)
- Satz 4-5: Weitere relevante Details oder Kontext
- Fachbegriffe kurz erklären
- Nur Fakten aus der Quelle, keine Meinungen
- NIEMALS erwähnen für wen es relevant ist ("relevant für...", "betrifft Unternehmer...", etc.)
- Schweizer Direktheit: Komm auf den Punkt, kein Blabla, kein Weichspülen
- Subtiler Humor erlaubt wenn passend — nie bei Katastrophen, Verbrechen oder ernsten Themen

HASHTAGS:
- Immer am Ende: #Schweiz
- Nur wenn 100% thematisch passend, max. 1 weiterer:
  #SNB (nur SNB-Entscheide), #FINMA (nur FINMA-Entscheide), #Steuern (nur konkrete Steueränderungen),
  #AHV (nur AHV-Entscheide), #Abstimmung (nur Volksabstimmungen), #Immobilien (nur Immobilienmarkt),
  #Einwanderung (nur Migrationsentscheide), #Kriminalität (nur Strafrecht/Verhaftungen)
- Kein #Wirtschaft, #Politik, #News oder andere generische Tags

Ziel: 700-1000 Zeichen gesamt. Kein Link, keine URL. Gib NUR den Post-Text zurück, nichts anderes."""


def _call_claude(client: anthropic.Anthropic, model: str, system: str, user_msg: str, max_retries: int = 3) -> Optional[str]:
    delays = [5, 15, 45]
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=512,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )
            return response.content[0].text.strip()
        except anthropic.RateLimitError as exc:
            retry_after = int(getattr(exc, "response", None) and exc.response.headers.get("retry-after", delays[attempt]) or delays[attempt])
            logger.warning("Rate limit hit, sleeping %ds", retry_after)
            time.sleep(retry_after)
        except anthropic.APIError as exc:
            if attempt < max_retries - 1:
                logger.warning("Claude API error (attempt %d): %s", attempt + 1, exc)
                time.sleep(delays[attempt])
            else:
                logger.error("Claude API error after %d retries: %s", max_retries, exc)
    return None


def _extract_json(raw: str) -> str:
    """Extract first JSON object or array from raw text, stripping markdown fences."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    raw = raw.strip()
    # Find first { or [ and matching closing bracket
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        idx = raw.find(start_char)
        if idx != -1:
            depth = 0
            for i, c in enumerate(raw[idx:], idx):
                if c == start_char:
                    depth += 1
                elif c == end_char:
                    depth -= 1
                    if depth == 0:
                        return raw[idx:i+1]
    return raw


def score_relevance(client: anthropic.Anthropic, model: str, item: dict, trends: list[str] | None = None) -> tuple[str, str, str, int]:
    title = item.get("title", "").strip()
    summary = item.get("summary", "").strip()
    if not title and not summary:
        return "LOW", "Kein Inhalt verfügbar", "Sonstiges", 0
    trends_block = ""
    if trends:
        trends_block = f"\n\nAKTUELLE X-TRENDS (Schweiz zuerst):\n" + "\n".join(f"- {t}" for t in trends[:25])
    user_msg = f"Titel: {title}\n\nZusammenfassung: {summary[:500]}\n\nQuelle: {item.get('source_id', '')}{trends_block}"
    raw = _call_claude(client, model, SCORE_SYSTEM, user_msg)
    if not raw:
        return "LOW", "API-Fehler bei Bewertung", "Sonstiges", 0
    try:
        data = json.loads(_extract_json(raw))
        viral = max(1, min(100, int(data.get("viral_score", 50))))
        return data.get("relevance", "LOW"), data.get("reason", ""), data.get("category", "Sonstiges"), viral
    except (json.JSONDecodeError, ValueError):
        logger.warning("Could not parse relevance JSON: %s", raw[:200])
        return "LOW", "JSON-Parsing-Fehler", "Sonstiges", 0


def is_duplicate_topic(client: anthropic.Anthropic, model: str, new_item: dict, recent_items: list[dict]) -> bool:
    """Returns True if the new article covers the same topic as a recently posted one.
    Errs on the side of caution — returns True (skip) on API failure."""
    if not recent_items:
        return False

    new_title = new_item.get("title", "")
    new_text = (new_item.get("post_text") or new_item.get("summary") or "")[:300]

    recent_str = "\n".join(
        f"- Titel: {r['title']}\n  Post: {(r.get('post_text') or '')[:150]}"
        for r in recent_items[-30:]
    )

    system = (
        "Du prüfst ob ein neuer Artikel bereits durch einen anderen Artikel abgedeckt wurde. "
        "Sei STRENG: Wenn dasselbe Ereignis, dieselbe Entscheidung oder dieselbe Person bereits "
        "gepostet wurde — auch wenn der Winkel leicht anders ist — ist es ein Duplikat. "
        "Im Zweifel: duplicate=true. Antworte NUR mit JSON: {\"duplicate\": true|false, \"reason\": \"...\"}"
    )
    user_msg = (
        f"NEUER ARTIKEL:\nTitel: {new_title}\nPost: {new_text}\n\n"
        f"BEREITS GEPOSTET (letzte 24h):\n{recent_str}\n\n"
        f"Ist der neue Artikel ein Duplikat?"
    )

    raw = _call_claude(client, model, system, user_msg)
    if not raw:
        logger.warning("Duplicate check API failure — skipping item to be safe: %s", new_title[:60])
        return True  # safe default: skip rather than post duplicate
    try:
        data = json.loads(_extract_json(raw))
        is_dup = bool(data.get("duplicate", False))
        if is_dup:
            logger.info("[DUPLICATE] %s — reason: %s", new_title[:60], data.get("reason", ""))
        return is_dup
    except (json.JSONDecodeError, Exception):
        return True  # safe default


def rank_items_by_potential(client: anthropic.Anthropic, model: str, items: list[dict]) -> list[dict]:
    """Rank unposted HIGH items by estimated engagement potential. Returns sorted list."""
    if len(items) <= 1:
        return items

    summaries = "\n\n".join(
        f"ID:{item['id']} | {item.get('title','')[:100]}\nKategorie: {item.get('category','')}\nPost: {(item.get('post_text') or '')[:200]}"
        for item in items
    )

    system = (
        "Du bewertest Schweizer News-Posts nach ihrem Engagement-Potenzial auf X (Twitter). "
        "Kriterien: Betrifft viele Menschen direkt, konkrete Zahlen/Fakten, emotionale Relevanz, Aktualität. "
        "Antworte NUR mit JSON: {\"ranking\": [ID1, ID2, ID3, ...]} — beste zuerst."
    )
    user_msg = f"Ranke diese Posts nach Engagement-Potenzial (bester zuerst):\n\n{summaries}"

    raw = _call_claude(client, model, system, user_msg)
    if not raw:
        return items
    try:
        import json as _json
        data = _json.loads(_extract_json(raw))
        ranked_ids = [int(x) for x in data.get("ranking", [])]
        id_map = {item["id"]: item for item in items}
        ranked = [id_map[i] for i in ranked_ids if i in id_map]
        # append any items not in ranking (safety fallback)
        ranked_ids_set = set(ranked_ids)
        ranked += [item for item in items if item["id"] not in ranked_ids_set]
        logger.info("Ranked %d items by engagement potential", len(ranked))
        return ranked
    except Exception as e:
        logger.warning("Ranking failed: %s", e)
        return items


def generate_post(client: anthropic.Anthropic, model: str, item: dict) -> Optional[str]:
    user_msg = (
        f"Titel: {item['title']}\n\n"
        f"Zusammenfassung: {item.get('summary', '')[:800]}\n\n"
        f"Quelle: {item.get('source_id', '')}"
    )
    return _call_claude(client, model, POST_SYSTEM, user_msg)
