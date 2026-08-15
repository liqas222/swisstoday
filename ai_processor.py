import json
import logging
import re
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
Finanzen, Steuern, Wirtschaft, Politik, Kriminalität, Recht, Einwanderung, Gesundheit, Energie, Umwelt, Sonstiges

Hinweis Umwelt: Naturkatastrophen, Hochwasser, Überschwemmungen, Erdrutsche, Klimaereignisse, Umweltverschmutzung → Umwelt

Hinweis: Alles zu Banken, SNB, FINMA, Zinsen, Krediten → Finanzen

Antworte NUR mit validem JSON:
{"relevance": "HIGH" | "LOW", "reason": "kurze Begründung auf Deutsch (max 80 Zeichen)", "category": "eine der obigen Kategorien", "viral_score": 1-100}

viral_score (1-100) — Berechne so:
A) Betroffene Menschen: Trifft es fast alle Schweizer? (25 Punkte) | Viele? (15) | Eine Berufsgruppe? (8) | Wenige Spezialisten? (3)
B) Emotionale Wirkung: Wut/Freude/Schock? (30 Punkte) | Interesse? (18) | Neutral informativ? (10) | Trocken/technisch? (4)
C) Aktualität: Heute/diese Woche? (25 Punkte) | Diesen Monat? (15) | Älteres Thema? (6)
E) Konkretheit: Konkrete Zahl, Datum, Person? (20 Punkte) | Teilweise konkret? (10) | Vage? (4)

Addiere A+B+C+E = viral_score (1-100). Rechne es WIRKLICH aus — nicht schätzen!"""

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

HASHTAGS (am Ende, mit Leerzeichen getrennt, für maximale Reichweite):
- ORT: Kommt eine konkrete Stadt/Gemeinde/Region im Artikel vor, füge sie als EIGENEN Hashtag hinzu
  (z.B. #Zürich, #Genf, #Lugano, #Winterthur, #Chur, #Sitten). Nur wenn klar genannt.
- KANTON: Ist ein Kanton erkennbar, füge SEPARAT (zusätzlich zum Ort) das offizielle Kürzel als Hashtag hinzu:
  #ZH #BE #LU #UR #SZ #OW #NW #GL #ZG #FR #SO #BS #BL #SH #AR #AI #SG #GR #AG #TG #TI #VD #VS #NE #GE #JU
  → Ort und Kanton sind ZWEI getrennte Hashtags (z.B. "#Winterthur #ZH").
- THEMA (max. 1, nur wenn 100% passend): #SNB (nur SNB-Entscheide), #FINMA (nur FINMA-Entscheide),
  #Steuern (nur Steueränderungen), #AHV (nur AHV-Entscheide), #Abstimmung (nur Volksabstimmungen),
  #Immobilien (nur Immobilienmarkt), #Einwanderung (nur Migrationsentscheide), #Kriminalität (nur Strafrecht)
- Immer als LETZTES: #Schweiz
- Reihenfolge: [#Thema] [#Ort] [#Kantonskürzel] #Schweiz
- Kein #Wirtschaft, #Politik, #News oder andere generische Tags
- Bei rein nationalen Themen ohne Ortsbezug: nur (optional #Thema) + #Schweiz

Ziel: 700-1000 Zeichen gesamt. Kein Link, keine URL. Gib NUR den Post-Text zurück, nichts anderes."""


_THREAD_SHORT_PLAN = """LÄNGE: 3 bis 4 Tweets — je nachdem, wie viel Substanz die Quelle hergibt.
Lieber 3 starke Tweets als 4 mit Füllmaterial. Streiche alles, was nur wiederholt.

AUFBAU:
- Tweet 1: DER HAKEN (siehe unten)
- Tweet 2: DIE FAKTEN — was ist passiert, was ändert sich konkret (Zahlen, Daten, Fristen)
- Tweet 3 (optional, nur bei echtem Mehrwert): Hintergrund — warum passiert das, was steckt dahinter
- LETZTER Tweet: ABSCHLUSS (siehe unten)"""

_THREAD_LONG_PLAN = """LÄNGE: 5 bis 7 Tweets — nutze nur so viele, wie die Quelle WIRKLICH trägt.
Hat der Artikel zu wenig Material für 7, mach 5. Niemals strecken, niemals Füllmaterial.

AUFBAU (überspringe Punkte, für die die Quelle nichts hergibt):
- Tweet 1: DER HAKEN (siehe unten)
- Tweet 2: DIE ZAHLEN — was genau ändert sich, was steigt, was bleibt gleich (konkrete Werte, Daten, Fristen)
- Tweet 3: DAS WARUM — Hintergrund, Auslöser, Begründung der Verantwortlichen
- Tweet 4: DIE AUSWIRKUNG — Volumen, Mehreinnahmen/-kosten, wer ist betroffen
- Tweet 5: WAS DAS FÜR DICH HEISST — dieser Tweet ist PFLICHT, nur die Form richtet sich nach der Quelle:
  · Liefert die Quelle Zahlen: rechne konkret vor, was das für einen normalen Haushalt / ein KMU
    bedeutet ("Bei 5'000 Fr. Konsum pro Monat sind das rund 35 Fr. mehr pro Jahr").
    Rechne NUR mit Zahlen aus der Quelle — erfinde NIEMALS Beträge, Prozente oder Fristen.
  · Liefert die Quelle keine Zahlen: nenne stattdessen die konkrete Folge im Alltag — wer muss
    ab wann was anders machen ("Wer bereits ein Gesuch eingereicht hat, muss neu einen
    Sprachnachweis beilegen — für laufende Verfahren gilt das ab sofort").
  · Im Zweifel lieber die konkrete Folge als eine unsichere Rechnung.
- Tweet 6: NÄCHSTE SCHRITTE — wie geht es weiter (Parlament, Referendum, Abstimmung, Inkrafttreten)
- LETZTER Tweet: ABSCHLUSS (siehe unten)"""

_THREAD_TEMPLATE = """Du erstellst einen X-THREAD auf Deutsch für den Account @SwissIntelNews.
Zielgruppe: Unternehmer, Gründer, Investoren, Anwälte, Expats in der Schweiz.

Trenne die Tweets mit einer eigenen Zeile, die exakt so aussieht:
===NEXT===

SUBSTANZ-TEST (NUR IM KOPF — schreibe davon KEIN EINZIGES WORT in die Antwort):
Prüfe still, wie viele KONKRETE, EIGENSTÄNDIGE Fakten die Quelle liefert — also Zahlen, Beträge,
Daten, Fristen, Namen, Orte, Entscheide, Begründungen. Allgemeinwissen zählt NICHT.
- Weniger als 3 solche Fakten → die Quelle trägt keinen Thread.
  Antworte dann mit GENAU diesem einen Wort und sonst nichts: KEIN_THREAD
- Ab 3 Fakten → gib DIREKT den fertigen Thread aus, beginnend mit Tweet 1.
Ein knapper starker Einzelpost ist immer besser als ein aufgeblasener Thread.

AUSGABEFORMAT — daran halten, sonst ist die Antwort unbrauchbar:
Deine Antwort beginnt SOFORT mit dem ersten Zeichen von Tweet 1. Keine Einleitung, keine
Aufzählung deiner Prüfung, keine Überschriften wie "SUBSTANZ-TEST" oder "THREAD", keine
Trennlinien wie "---", kein Kommentar davor oder danach. Nur die Tweets und ===NEXT===.

{plan}

VERBOTEN — daran scheitern die meisten Threads:
- WIEDERHOLUNG: Kein Tweet darf sagen, was schon dasteht. Tweet 2 muss NEUE Information
  bringen, nicht den Hook mit anderen Worten. Prüfe jeden Tweet: Steht das sinngemäss schon oben?
  Dann streiche ihn und mach den Thread kürzer.
- ALLGEMEINPLÄTZE: Streiche jeden Satz, der genauso für jede andere Meldung dieser Art gelten
  würde. Beispiele für Sätze, die NICHT vorkommen dürfen: "ist Teil der regulären Polizeiarbeit",
  "wirkt präventiv und repressiv", "die Behörden arbeiten eng zusammen", "bleibt ein
  kontinuierliches Anliegen", "ein Todesfall ist immer ein Verlust", "die Behörden werden den
  Fall aufklären". Solche Füllsätze sind schlimmer als ein kurzer Thread.
- RHETORISCHE FÜLLFRAGEN mitten im Thread: keine aufgeworfenen Fragen, die du gar nicht
  beantwortest ("Wie werden Kutschen überwacht? Welche Standards gelten?"). Entweder du hast
  die Antwort aus der Quelle — dann schreib sie hin — oder der Punkt gehört gestrichen.
- SELBSTABSCHWÄCHUNG im Hook: keine Formulierungen wie "die Details sind noch spärlich",
  "bisher ist wenig bekannt", "genaue Umstände unklar". Das nimmt die Spannung, die der Hook
  aufbauen soll. Schreibe stattdessen, was FESTSTEHT.
Lieber 3 dichte Tweets als 5 mit Luft.

TWEET 1 — DER HAKEN (kurz, max. ~220 Zeichen, KEINE Hashtags):
- EMOJI + knackige Headline (max. 6-8 Wörter, nur Kernaussage, mit Zahl falls vorhanden)
- 1 Satz Kontext, der neugierig macht
- Letzte Zeile: ein Cliffhanger, der auf den nächsten Tweet verweist. Diese Zeile MUSS mit den
  beiden Emojis 👇🧵 DIREKT NEBENEINANDER am Ende schliessen (ohne Leerzeichen dazwischen) — PFLICHT.
- VARIIERE die Formulierung (nicht immer dieselbe!). Passende Beispiele:
  "Das bedeutet das für dich 👇🧵" · "Was das konkret heisst 👇🧵" · "Die wichtigsten Punkte 👇🧵"
  · "Das steckt dahinter 👇🧵" · "Was jetzt wichtig wird 👇🧵"
- KEIN Clickbait: Der Cliffhanger muss exakt das halten, was danach kommt — nicht übertreiben.
- Bei ERNSTEN Themen (Tote, Katastrophen, Verbrechen, Unglücke, Gewalt): sachlicher Cliffhanger wie
  "Die Fakten 👇🧵" oder "Was bisher bekannt ist 👇🧵" — NIEMALS reisserisch oder marktschreierisch.
  Die beiden Emojis 👇🧵 stehen auch hier am Ende.

MITTLERE TWEETS (je max. ~500 Zeichen, KEINE Hashtags, KEINE Nummerierung):
- Jeder Tweet behandelt GENAU EINEN Gedanken aus dem Aufbau oben.
- Der Sog entsteht durch den INHALT — schreibe KEINE Übergangsfloskeln wie "Weiter 👇",
  "Mehr dazu", "Lies weiter". Diese Emojis/Hinweise gehören AUSSCHLIESSLICH in Tweet 1.
- Menschlich und direkt, kein Nachrichtenagentur-Ton. Nur Fakten aus der Quelle, keine Meinungen.

LETZTER TWEET — ABSCHLUSS + HASHTAGS (max. ~450 Zeichen):
- 1-2 Sätze Einordnung: was heisst das mittelfristig, worauf sollte man achten.
- Danach eine ECHTE Frage an die Leser. Sie muss sich auf die SACHE beziehen — auf den Entscheid,
  die Zahl, die Konsequenz. Kein Smalltalk über persönliche Erlebnisse.
  GUT: "Findest du die Erhöhung gerechtfertigt? Und warum?"
  SCHLECHT: "Warst du in letzter Zeit in Bern unterwegs?" — das hat nichts mit der Meldung zu tun.
  Trägt das Thema keine sinnvolle Frage, lass sie weg und ende mit der Einordnung.
- ABSOLUTE AUSNAHME bei ERNSTEN Themen (Todesfälle, tödliche Unfälle, Katastrophen, Verbrechen,
  Gewalt, schwere Krankheit): Dann stellst du ÜBERHAUPT KEINE Frage — kein Fragezeichen im
  gesamten letzten Tweet. Weder Meinungsfrage noch rhetorische Frage noch Anteilnahme.
  VERBOTEN sind Formulierungen wie "Deine Gedanken sind bei den Angehörigen?",
  "Was denkst du dazu?", "Wie siehst du das?" — mit Toten wird kein Engagement erzeugt.
  Der Thread endet dort mit einem sachlichen Aussagesatz, danach direkt die Hashtags.
- Danach die Hashtags nach den Regeln unten.
- NIEMALS erwähnen für wen es relevant ist ("relevant für...", "betrifft Unternehmer...").

Stil: Schweizer Direktheit, kein Blabla, kein Weichspülen. Subtiler Humor nur bei unkritischen Themen.
Nummeriere die Tweets NICHT (kein "1/6", "2/6") — das verrät die Länge und kostet Leser.

HASHTAGS (NUR im letzten Tweet, ganz am Ende, mit Leerzeichen getrennt):
- ORT: Konkrete Stadt/Gemeinde als eigener Hashtag, falls im Artikel genannt (#Zürich, #Lugano, #Chur ...).
- KANTON: SEPARAT das offizielle Kürzel (#ZH #BE #LU #UR #SZ #OW #NW #GL #ZG #FR #SO #BS #BL #SH #AR #AI #SG #GR #AG #TG #TI #VD #VS #NE #GE #JU). Ort und Kanton = ZWEI getrennte Hashtags.
- THEMA (max. 1, nur wenn 100% passend): #SNB #FINMA #Steuern #AHV #Abstimmung #Immobilien #Einwanderung #Kriminalität
- Immer als LETZTES: #Schweiz. Keine generischen Tags (#Wirtschaft, #Politik, #News).
- Reihenfolge: [#Thema] #Ort #Kantonskürzel #Schweiz

Gib NUR die Tweets mit den ===NEXT===-Trennern zurück, nichts anderes. Kein Link, keine URL."""

THREAD_SYSTEM_SHORT = _THREAD_TEMPLATE.format(plan=_THREAD_SHORT_PLAN)
THREAD_SYSTEM_LONG = _THREAD_TEMPLATE.format(plan=_THREAD_LONG_PLAN)

# viral_score at or above this gets the long-form treatment
THREAD_LONG_MIN_SCORE = 66


def _call_claude(client: anthropic.Anthropic, model: str, system: str, user_msg: str, max_retries: int = 3, max_tokens: int = 512) -> Optional[str]:
    delays = [5, 15, 45]
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
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


def score_relevance(client: anthropic.Anthropic, model: str, item: dict) -> tuple[str, str, str, int]:
    title = item.get("title", "").strip()
    summary = item.get("summary", "").strip()
    if not title and not summary:
        return "LOW", "Kein Inhalt verfügbar", "Sonstiges", 0
    user_msg = f"Titel: {title}\n\nZusammenfassung: {summary[:500]}\n\nQuelle: {item.get('source_id', '')}"
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


def check_topic_overlap(client: anthropic.Anthropic, model: str, new_item: dict, recent_items: list[dict]) -> tuple[str, Optional[str]]:
    """Check if new item is duplicate, update, or new topic.
    Returns (status, quote_tweet_id) where status is 'new'|'duplicate'|'update'.
    On API failure returns ('duplicate', None) to be safe."""
    if not recent_items:
        return "new", None

    new_title = new_item.get("title", "")
    new_text = (new_item.get("post_text") or new_item.get("summary") or "")[:300]

    recent_list = recent_items[-30:]
    recent_str = "\n".join(
        f"[{i}] Titel: {r['title']}\n    Post: {(r.get('post_text') or '')[:150]}"
        for i, r in enumerate(recent_list)
    )

    system = (
        "Du prüfst ob ein neuer Artikel ein Duplikat, ein Update oder ein neues Thema ist.\n"
        "DUPLIKAT: Exakt dasselbe Ereignis, dieselbe Entscheidung — keine neuen Fakten.\n"
        "UPDATE: Dasselbe Thema, aber neue Entwicklung (z.B. neue Zahlen, Urteil, Reaktion).\n"
        "NEU: Anderes Thema oder neues unabhängiges Ereignis.\n"
        "Antworte NUR mit JSON: {\"status\": \"new\"|\"duplicate\"|\"update\", \"related_index\": null|0..N, \"reason\": \"...\"}\n"
        "related_index = Index des verwandten Posts aus der Liste (nur bei update/duplicate)."
    )
    user_msg = (
        f"NEUER ARTIKEL:\nTitel: {new_title}\nPost: {new_text}\n\n"
        f"BEREITS GEPOSTET (letzte 24h):\n{recent_str}\n\n"
        f"Bewerte den neuen Artikel."
    )

    raw = _call_claude(client, model, system, user_msg)
    if not raw:
        logger.warning("Topic check API failure — skipping: %s", new_title[:60])
        return "duplicate", None
    try:
        data = json.loads(_extract_json(raw))
        status = data.get("status", "duplicate")
        reason = data.get("reason", "")
        related_idx = data.get("related_index")
        logger.info("[TOPIC CHECK] %s → %s (%s)", new_title[:60], status, reason)
        quote_tweet_id = None
        if status in ("update", "duplicate") and related_idx is not None:
            try:
                related = recent_list[int(related_idx)]
                tid = related.get("tweet_id")
                if tid and tid not in ("skipped_history", "dry_run", "duplicate_topic", "archived"):
                    quote_tweet_id = tid
            except (IndexError, TypeError, ValueError):
                pass
        return status, quote_tweet_id
    except (json.JSONDecodeError, Exception):
        return "duplicate", None


def is_duplicate_topic(client: anthropic.Anthropic, model: str, new_item: dict, recent_items: list[dict]) -> bool:
    """Legacy wrapper — use check_topic_overlap for full update/quote support."""
    status, _ = check_topic_overlap(client, model, new_item, recent_items)
    return status == "duplicate"


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


# Marker between tweets in a stored thread (must match publisher.THREAD_SEP_TOKEN)
THREAD_SEP_TOKEN = "===NEXT==="


MAX_THREAD_TWEETS = 7

_DIVIDER_RE = re.compile(r'^\s*[-–—_*=]{3,}\s*$')
# Blocks that are the model thinking out loud, not tweet content
_META_RE = re.compile(r'SUBSTANZ|KEIN_THREAD|→\s*THREAD|^\s*✓|^\s*THREAD\s*:?\s*$'
                      r'|^\s*(TWEET|AUFBAU|LÄNGE)\b', re.I | re.M)


def _looks_like_meta(block: str) -> bool:
    b = block.strip()
    return bool(_DIVIDER_RE.match(b) or _META_RE.search(b) or re.match(r'^\s*\d+\.\s', b))


_SERIOUS_RE = re.compile(
    r'\b(tot|tote[nrs]?|todesfall|todesopfer|gestorben|verstorben|ums leben|leblos|'
    r'getötet|tötet|tötung|leiche|opfer|verunglückt|tödlich|umgekommen|erschossen|'
    r'ermordet|mord|totschlag|selbstmord|suizid|amok|attentat)\b', re.I)


def _has_fatality(*texts: str) -> bool:
    return any(_SERIOUS_RE.search(t or "") for t in texts)


def _strip_closing_question(text: str) -> str:
    """Remove a closing question from the last tweet. Engagement bait has no
    place under a report about people who died."""
    lines = text.rstrip().split("\n")
    tail = []  # keep the trailing hashtag block where it is
    while lines and (not lines[-1].strip()
                     or re.fullmatch(r'(#\S+\s*)+', lines[-1].strip())):
        tail.insert(0, lines.pop())
    body = "\n".join(lines).rstrip()
    while True:
        m = re.search(r'(?:^|(?<=[.!?\n]))\s*[^.!?\n]*\?\s*$', body)
        if not m or m.start() == 0:
            break
        body = body[:m.start()].rstrip()
    if not body:
        return text  # nothing but a question — leave it rather than post nothing
    return "\n".join([body] + tail).rstrip()


def _strip_preamble(text: str) -> str:
    """Remove reasoning the model printed before tweet 1 (substance test,
    headings, divider lines). The prompt forbids it, but prompts are not
    guarantees and this text must never reach X."""
    blocks = re.split(r'\n\s*\n', text.strip())
    cut = 0
    for i, b in enumerate(blocks):
        if _looks_like_meta(b):
            cut = i + 1
        else:
            break  # first real block ends the preamble
    cleaned = "\n\n".join(blocks[cut:]).strip()
    return cleaned or text.strip()


def generate_thread_detailed(client: anthropic.Anthropic, model: str, item: dict,
                             viral_score: int = 0) -> tuple[Optional[str], str]:
    """Generate an X thread and report why it failed if it did.

    Returns (thread_or_None, reason) where reason is one of:
      'ok'          — thread generated
      'thin'        — the model judged the source too thin (single post is correct)
      'api_error'   — Claude did not respond
      'bad_format'  — response had no usable ===NEXT=== structure
    """
    import re
    long_form = viral_score >= THREAD_LONG_MIN_SCORE
    system = THREAD_SYSTEM_LONG if long_form else THREAD_SYSTEM_SHORT
    user_msg = (
        f"Titel: {item['title']}\n\n"
        f"Zusammenfassung: {item.get('summary', '')[:800]}\n\n"
        f"Quelle: {item.get('source_id', '')}"
    )
    raw = _call_claude(client, model, system, user_msg,
                       max_tokens=2200 if long_form else 1000)
    if not raw:
        return None, "api_error"
    # The model may decline when the source is too thin for a thread
    if "KEIN_THREAD" in raw[:200].upper():
        logger.info("Thread declined (source too thin): %s", item.get("title", "")[:60])
        return None, "thin"
    # Split on the ===NEXT=== marker (tolerant of extra = or whitespace)
    parts = [p.strip() for p in re.split(r'={2,}\s*NEXT\s*={2,}', raw) if p.strip()]
    if len(parts) < 2:
        logger.warning("Thread response had no separators: %r", raw[:160])
        return None, "bad_format"
    # Drop any reasoning the model printed before the first tweet
    parts[0] = _strip_preamble(parts[0])
    if len(parts) > 2 and _looks_like_meta(parts[0]):
        parts = parts[1:]  # the whole first block was meta, not a tweet
    parts = parts[:MAX_THREAD_TWEETS]
    # Never end a thread about a fatality with a question
    if _has_fatality(item.get("title", ""), item.get("summary", ""), parts[-1]):
        cleaned_last = _strip_closing_question(parts[-1])
        if cleaned_last != parts[-1]:
            logger.info("Removed closing question from a thread about a fatality")
            parts[-1] = cleaned_last
    # Tweet 1 must end with 👇🧵 side by side — repair it if the model didn't
    if "🧵" not in parts[0]:
        lines = parts[0].rstrip().split("\n")
        last = lines[-1].rstrip()
        # Glue onto an existing 👇, otherwise add the pair
        last = (last + "🧵") if last.endswith("👇") else (last + " 👇🧵")
        lines[-1] = last
        parts[0] = "\n".join(lines)
    return (f"\n{THREAD_SEP_TOKEN}\n").join(parts), "ok"


def generate_thread(client: anthropic.Anthropic, model: str, item: dict,
                    viral_score: int = 0) -> Optional[str]:
    """Thread text, or None when a single post should be used instead."""
    return generate_thread_detailed(client, model, item, viral_score)[0]
