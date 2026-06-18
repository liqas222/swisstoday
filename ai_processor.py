import json
import logging
import time
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

SCORE_SYSTEM = """Du bewertest Schweizer Behördenmitteilungen für eine Zielgruppe aus Unternehmern, Gründern, Investoren, Anwälten, Treuhändern und Expats.

Relevanz HIGH wenn: Steueränderungen, neue oder geänderte Gesetze und Verordnungen, neue Regulierungen, Wirtschafts- und Finanzthemen, Start-up- und unternehmensrelevante Nachrichten, Immobilien- und Wohnungsmarkt, Arbeitsmarkt, Einwanderung und Aufenthalt, Volksabstimmungen und politische Entscheide mit breiter Wirkung.

Relevanz LOW wenn: interne Verwaltungsmitteilungen, Routineankündigungen, Ausschreibungen und Beschaffungen, technische oder organisatorische Updates ohne breite gesellschaftliche oder wirtschaftliche Auswirkungen, Personalentscheide auf tieferer Ebene.

Antworte NUR mit validem JSON, kein weiterer Text:
{"relevance": "HIGH" | "LOW", "reason": "kurze Begründung auf Deutsch (max 100 Zeichen)"}"""

POST_SYSTEM = """Du erstellst X-Posts (Twitter) auf Deutsch für den Account @SwissIntelNews.
Zielgruppe: Unternehmer, Gründer, Investoren, Anwälte, Expats in der Schweiz.

Regeln:
- Kein bürokratischer Ton. Keine Passivkonstruktionen wie "wurde beschlossen".
- Kurz, prägnant, verständlich für Nicht-Experten.
- Sachlich und neutral — keine Übertreibungen, keine Meinungen.
- Faktenbasiert: nur was in der Mitteilung steht.
- Fokus auf Relevanz und Auswirkungen für die Zielgruppe.

Format:
[Einleitungssatz: Was passiert, warum relevant]

Das ändert sich / Das bedeutet:
• [Punkt 1]
• [Punkt 2]
• [Punkt 3 — falls vorhanden]

[URL]

Der gesamte Post soll unter 280 Zeichen bleiben wenn möglich, ansonsten max 500 Zeichen (erster Tweet eines Threads).
Gib NUR den Post-Text zurück, nichts anderes."""


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


def score_relevance(client: anthropic.Anthropic, model: str, item: dict) -> tuple[str, str]:
    user_msg = f"Titel: {item['title']}\n\nZusammenfassung: {item.get('summary', '')[:500]}\n\nQuelle: {item.get('source_id', '')}"
    raw = _call_claude(client, model, SCORE_SYSTEM, user_msg)
    if not raw:
        return "LOW", "API-Fehler bei Bewertung"
    try:
        data = json.loads(raw)
        return data.get("relevance", "LOW"), data.get("reason", "")
    except json.JSONDecodeError:
        logger.warning("Could not parse relevance JSON: %s", raw[:200])
        return "LOW", "JSON-Parsing-Fehler"


def generate_post(client: anthropic.Anthropic, model: str, item: dict) -> Optional[str]:
    user_msg = (
        f"Titel: {item['title']}\n\n"
        f"Zusammenfassung: {item.get('summary', '')[:800]}\n\n"
        f"URL: {item.get('url', '')}\n\n"
        f"Quelle: {item.get('source_id', '')}"
    )
    return _call_claude(client, model, POST_SYSTEM, user_msg)
