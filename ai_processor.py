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
3. Es hat konkrete, messbare Auswirkungen für Unternehmer, Investoren, Anwälte oder Expats
4. Es handelt sich um ein BESCHLOSSENES FAKTUM — nicht um eine Prognose, Studie oder Meinung

HIGH-Beispiele (wirklich wichtig):
- Neues Gesetz/Verordnung verabschiedet mit konkretem Datum
- Abstimmungsresultat mit direkter Wirkung
- SNB/FINMA Entscheid mit Marktwirkung (z.B. Zinsänderung)
- Grosse Verhaftung, Mord, Gewaltverbrechen oder Strafprozess von öffentlichem Interesse
- Terroranschlag, Anschlagsversuch oder konkrete Bedrohungslage in der Schweiz
- Steueränderung konkret beschlossen
- Wichtige Unternehmenstransaktion (Übernahme, Konkurs, Börsengang)
- Politischer Skandal mit konkreten Folgen
- Wichtiger Wirtschaftsbericht, Konjunkturbericht oder Studie mit konkreten Zahlen zur Schweiz (z.B. BIP, Arbeitslosigkeit, Inflation)
- Offizielle Prognose von SNB, SECO, IWF oder ähnlichen Institutionen mit konkreten Zahlen

LOW-Beispiele (IMMER LOW — nie posten):
- Debatten, Vorstösse, Motionen die noch nicht beschlossen sind
- Kommentare, Meinungsartikel, Interviews
- Internationale Nachrichten ohne direkte CH-Relevanz
- Routinemeldungen, Statistiken, Marktberichte, Studien
- Immobilienmarkt-Analysen, Preistrends, Prognosen (IMMER LOW)
- "Könnte", "plant", "diskutiert", "erwartet", "prognostiziert" — nur beschlossene Fakten zählen
- Ratgeber-Artikel ("So kaufen Sie...", "Was Sie wissen müssen...")

Kategorien (wähle die passendste):
Finanzen, Steuern, Wirtschaft, Politik, Kriminalität, Recht, Einwanderung, Gesundheit, Energie, Sonstiges

Hinweis: Alles zu Banken, SNB, FINMA, Zinsen, Krediten → Finanzen

Antworte NUR mit validem JSON:
{"relevance": "HIGH" | "LOW", "reason": "kurze Begründung auf Deutsch (max 80 Zeichen)", "category": "eine der obigen Kategorien"}"""

POST_SYSTEM = """Du erstellst X-Posts (Twitter) auf Deutsch für den Account @SwissIntelNews.
Zielgruppe: Unternehmer, Gründer, Investoren, Anwälte, Expats in der Schweiz.

Format (EXAKT so):
EMOJI HEADLINE

ERKLÄRUNG (2-3 Sätze)

URL

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
- Menschlich und direkt, kein Nachrichtenagentur-Ton
- Was bedeutet das konkret? Wer ist betroffen? Was ändert sich?
- Fachbegriffe kurz erklären
- Nur Fakten aus der Quelle, keine Meinungen

Ziel: 400-600 Zeichen gesamt. Gib NUR den Post-Text zurück, nichts anderes."""


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
    """Strip markdown code fences if present."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return raw.strip()


def score_relevance(client: anthropic.Anthropic, model: str, item: dict) -> tuple[str, str, str]:
    title = item.get("title", "").strip()
    summary = item.get("summary", "").strip()
    if not title and not summary:
        return "LOW", "Kein Inhalt verfügbar", "Sonstiges"
    user_msg = f"Titel: {title}\n\nZusammenfassung: {summary[:500]}\n\nQuelle: {item.get('source_id', '')}"
    raw = _call_claude(client, model, SCORE_SYSTEM, user_msg)
    if not raw:
        return "LOW", "API-Fehler bei Bewertung", "Sonstiges"
    try:
        data = json.loads(_extract_json(raw))
        return data.get("relevance", "LOW"), data.get("reason", ""), data.get("category", "Sonstiges")
    except json.JSONDecodeError:
        logger.warning("Could not parse relevance JSON: %s", raw[:200])
        return "LOW", "JSON-Parsing-Fehler", "Sonstiges"


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


def generate_post(client: anthropic.Anthropic, model: str, item: dict) -> Optional[str]:
    user_msg = (
        f"Titel: {item['title']}\n\n"
        f"Zusammenfassung: {item.get('summary', '')[:800]}\n\n"
        f"URL: {item.get('url', '')}\n\n"
        f"Quelle: {item.get('source_id', '')}"
    )
    return _call_claude(client, model, POST_SYSTEM, user_msg)
