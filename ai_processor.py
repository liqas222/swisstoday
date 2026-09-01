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

HIGH wenn konkretes Ereignis in der Schweiz MIT BREITER BEDEUTUNG:
- Schwere Gewaltverbrechen: Tötungsdelikte, Schusswaffengebrauch, Anschläge, Geiselnahmen,
  Fälle mit mehreren Opfern
- Grosse Polizeiaktionen, Razzien, organisierte Kriminalität, Terrorermittlungen
- Gerichtsurteile von öffentlichem Interesse: Bundesgericht, Präzedenzfälle, bekannte Personen,
  hohe Deliktsummen, aussergewöhnlich hohe Strafen
- Beschlossenes Gesetz/Verordnung, Abstimmungsresultat
- SNB/FINMA Entscheid, Zinsänderung
- Unternehmenstransaktion (Übernahme, Konkurs, Entlassung >100 Stellen)
- Politischer Skandal mit konkreten Folgen

ROUTINE-POLIZEIMELDUNGEN sind LOW — davon erscheinen täglich dutzende, sie tragen keinen Post:
- Einzelne Einbrüche, Diebstähle, Sachbeschädigung, Ladendiebstahl
- Verkehrsunfälle ohne Todesopfer, Selbstunfälle, Fahren in fahrunfähigem Zustand
- Festnahmen ohne besondere Umstände, Fahndungsaufrufe, Vermisstmeldungen
- Betrugs- und Diebstahlsfälle im Kleinbereich, Hausfriedensbruch
- Prüfe bei Kriminalität immer: Würde eine überregionale Zeitung darüber berichten?
  Wenn nur die Lokalzeitung — dann LOW.

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

Das ist sachliche Nachrichtenberichterstattung. Auch Verbrechen, Todesfälle, Unfälle und
Katastrophen werden berichtet — nüchtern, faktentreu und respektvoll gegenüber Betroffenen,
so wie es jede seriöse Zeitung tut. Bei solchen Themen gilt: kein Humor, keine Zuspitzung,
keine reisserische Sprache, nur die belegten Fakten.

Gib IMMER GENAU EINEN fertigen Post aus — niemals mehrere, niemals eine Auswahl.
Deine Antwort beginnt beim ersten Zeichen des Posts und endet mit den Hashtags. Verboten sind:
Einleitungen ("Ich erstelle 5 separate Posts für diese Themen"), Überschriften ("POST 1:"),
Trennlinien ("---"), Kommentare davor oder danach, Rückfragen, Ablehnungen und Hinweise auf
Richtlinien. Solcher Text würde ungeprüft veröffentlicht.
Behandelt die Quelle mehrere Themen, wähle das wichtigste und schreibe NUR dazu einen Post.
Ist ein Thema heikel, formuliere sachlicher — aber liefere den Post.

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

SPRACHE — jeder Satz muss grammatikalisch korrekt sein. Lies jeden Satz vor der Ausgabe nochmals:
- Stimmen Artikel, Fall und Verbform? ("Erst die Trockenheit" — nicht "Erste die Trockenheit".
  "Die Frage ist, ob…" — nicht "Fragen ist, ob…". "die Aufklärung des genauen Hergangs" — nicht "der genauen Hergang".)
- Schweizer Rechtschreibung: immer "ss" statt "ß".
- Keine abgebrochenen oder verstümmelten Sätze. Lieber ein Satz weniger als ein fehlerhafter.

Ziel: 700-1000 Zeichen gesamt. Kein Link, keine URL. Gib NUR den Post-Text zurück, nichts anderes."""


_THREAD_SHORT_PLAN = """LÄNGE: 2 bis 3 Tweets — je nachdem, wie viel Substanz die Quelle hergibt.
Lieber 3 starke Tweets als 4 mit Füllmaterial. Streiche alles, was nur wiederholt.

AUFBAU:
- Tweet 1: DER HAKEN (siehe unten)
- Tweet 2: DIE FAKTEN — was ist passiert, was ändert sich konkret (Zahlen, Daten, Fristen)
- Tweet 3 (optional, nur bei echtem Mehrwert): Hintergrund — warum passiert das, was steckt dahinter
- LETZTER Tweet: ABSCHLUSS (siehe unten)"""

_THREAD_LONG_PLAN = """LÄNGE: 3 bis 4 Tweets — nutze nur so viele, wie die Quelle WIRKLICH trägt.
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

Das ist sachliche Nachrichtenberichterstattung. Auch Verbrechen, Todesfälle, Unfälle und
Katastrophen werden berichtet — nüchtern, faktentreu und respektvoll gegenüber Betroffenen.
Bei solchen Themen: kein Humor, keine Zuspitzung, nur belegte Fakten.

Antworte AUSSCHLIESSLICH mit dem Thread oder mit KEIN_THREAD. Schreibe NIEMALS über den
Auftrag, lehne NIEMALS mit Begründung ab und stelle keine Rückfragen — solcher Text würde
ungeprüft veröffentlicht. Ist ein Thema heikel, formuliere sachlicher.

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

TWEET 1 — DER HAKEN (max. ~260 Zeichen, KEINE Hashtags):
Aufbau, jeweils durch eine Leerzeile getrennt:
  EMOJI + knackige Headline (max. 6-8 Wörter, nur Kernaussage, mit Zahl falls vorhanden)
  1-2 Sätze mit der WICHTIGSTEN Information: was ist passiert, wo, wann, wie viele Betroffene
  der Cliffhanger
- WICHTIG: Tweet 1 muss FÜR SICH ALLEIN VERSTÄNDLICH sein. Die Hälfte der Leser klappt den
  Thread nie auf — wer nur diesen einen Tweet sieht, muss die Nachricht trotzdem kennen.
- Der Cliffhanger verspricht die VERTIEFUNG (Hintergrund, Zahlen, Folgen), nicht die Grundinfo.
- Der Cliffhanger MUSS mit den beiden Emojis 👇🧵 DIREKT NEBENEINANDER enden
  (ohne Leerzeichen dazwischen) — PFLICHT.
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

SPRACHE — jeder Satz muss grammatikalisch korrekt sein. Lies jeden Satz vor der Ausgabe nochmals:
- Stimmen Artikel, Fall und Verbform? ("Erst die Trockenheit" — nicht "Erste die Trockenheit".
  "Die Frage ist, ob…" — nicht "Fragen ist, ob…". "die Aufklärung des genauen Hergangs" — nicht "der genauen Hergang".)
- Schweizer Rechtschreibung: immer "ss" statt "ß".
- Keine abgebrochenen oder verstümmelten Sätze. Lieber ein Satz weniger als ein fehlerhafter.
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
THREAD_LONG_MIN_SCORE = 80


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
    user_msg = (f"Titel: {title}\n\n{_source_material(item)}\n\n"
                f"Quelle: {item.get('source_id', '')}")
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


# How many posts one event may get. A big story earns more room than a routine
# one — but not unlimited: the Aarau shooting ran to 31 posts and impressions
# fell from 14'969 to 39.
MAX_POSTS_PER_TOPIC = 4          # ordinary story
MAX_POSTS_PER_TOPIC_BIG = 8      # viral_score >= 75
MAX_POSTS_PER_TOPIC_MAJOR = 14   # viral_score >= 88 — a genuine major event


def topic_cap(viral_score: int) -> int:
    """Room a story gets, scaled to how big it is."""
    if viral_score >= 88:
        return MAX_POSTS_PER_TOPIC_MAJOR
    if viral_score >= 75:
        return MAX_POSTS_PER_TOPIC_BIG
    return MAX_POSTS_PER_TOPIC

# 5-Zeichen-Stämme von Wörtern, die in halben Schweizer Nachrichten vorkommen.
# Nur grossgeschriebene Wörter und Zahlen landen überhaupt in der Prüfung,
# deshalb stehen hier Substantive und keine Füllwörter.
_TOPIC_STOP = {
    "schwe", "heute", "video", "updat", "blick", "news", "jahre", "proze",
    "frank", "milli", "bunde", "parla", "kanto", "gemei", "poliz", "medie",
    "regie", "depar", "kommi", "rat", "natio", "stadt", "einwo", "behör",
    "monta", "diens", "mittw", "donne", "freit", "samst", "sonnt",
    "janua", "febru", "märz", "april", "juni", "juli", "augus", "septe",
    "oktob", "novem", "dezem", "woche", "mensc", "person", "perso",
}


def _topic_keywords(text: str) -> set:
    """Distinctive words of a headline: proper nouns and numbers, cut to a stem
    so that Aarau/Aarauer and Verletzte/Verletzt count as the same word."""
    words = re.findall(r'\b[A-ZÄÖÜ][\wäöüéèà]{3,}\b|\b\d{2,}\b', text or "")
    out = set()
    for w in words:
        stem = w.lower()[:5]  # crude stem — enough to link variants of one event
        if stem in _TOPIC_STOP:
            continue
        out.add(stem)
    return out


# One shared distinctive word is enough. Measured against the real Aarau
# headlines this linked all 45 pairs with no false match against other topics.
TOPIC_OVERLAP_WORDS = 1


# Headlines on one story often share no wording at all — "Ständerat schwächt
# UBS-Eigenkapitalregeln ab" and "UBS-Regulierung: Kommission einigt sich auf
# Kompromiss" have not one word in common. The body does: same names, same
# figures. So a second, stricter signal reads the whole text.
# Gemessen an den fünf UBS-Eigenkapital-Meldungen: untereinander teilen sie
# mindestens 2 Stämme, mit fremden Themen null. Der Anteil-Wert fängt lange
# Artikeltexte ab, in denen sich zufällige Treffer sonst häufen.
BODY_OVERLAP_WORDS = 2
BODY_OVERLAP_RATIO = 0.12
_BODY_CHARS = 1200


def _body_keywords(item: dict) -> set:
    """Distinctive words of the whole story, not just its headline."""
    parts = [item.get("title") or "", item.get("post_text") or "",
             item.get("summary") or "", (item.get("article_text") or "")[:_BODY_CHARS]]
    return _topic_keywords(" ".join(parts))


def _is_same_topic(new_item: dict, recent: dict) -> bool:
    """Same event? Either the headlines match, or the substance does."""
    title_kw = _topic_keywords(new_item.get("title", ""))
    if len(title_kw) >= 2:
        other_title = _topic_keywords(recent.get("title") or recent.get("post_text", ""))
        if len(title_kw & other_title) >= TOPIC_OVERLAP_WORDS:
            return True
    a, b = _body_keywords(new_item), _body_keywords(recent)
    if not a or not b:
        return False
    shared = len(a & b)
    return (shared >= BODY_OVERLAP_WORDS
            and shared / min(len(a), len(b)) >= BODY_OVERLAP_RATIO)


def _same_topic_count(new_item, recent_items: list[dict]) -> int:
    """How many recent posts are clearly about the same event."""
    if isinstance(new_item, str):  # older callers passed just the headline
        new_item = {"title": new_item}
    return sum(1 for r in recent_items if _is_same_topic(new_item, r))


def _fact_already_posted(fact: str, recent_items: list[dict]) -> bool:
    """Is the supposedly new fact already in one of the posts we sent?"""
    kw = _topic_keywords(fact)
    if not kw:
        return True  # nothing distinctive in it at all
    for r in recent_items:
        posted = _topic_keywords(f"{r.get('title') or ''} {r.get('post_text') or ''}")
        if kw <= posted:  # every distinctive word of the "news" is old news
            return True
    return False


def check_topic_overlap(client: anthropic.Anthropic, model: str, new_item: dict, recent_items: list[dict]) -> tuple[str, Optional[str]]:
    """Check if new item is duplicate, update, or new topic.
    Returns (status, quote_tweet_id) where status is 'new'|'duplicate'|'update'.
    On API failure returns ('duplicate', None) to be safe."""
    if not recent_items:
        return "new", None

    new_title = new_item.get("title", "")

    # Hard cap first — no AI call needed once an event is exhausted
    cap = topic_cap(int(new_item.get("viral_score") or 0))
    seen = _same_topic_count(new_item, recent_items)
    if seen >= cap:
        logger.info("[TOPIC CAP] %s — bereits %d/%d Posts zu diesem Ereignis, kein weiterer",
                    new_title[:60], seen, cap)
        return "duplicate", None
    new_text = (new_item.get("post_text") or new_item.get("summary") or "")[:300]

    recent_list = recent_items[-60:]  # 72h window, so keep more in view
    recent_str = "\n".join(
        f"[{i}] Titel: {r['title']}\n    Post: {(r.get('post_text') or '')[:150]}"
        for i, r in enumerate(recent_list)
    )

    system = (
        "Du prüfst, ob ein neuer Artikel ein Duplikat, ein Update oder ein neues Thema ist.\n"
        "Sei STRENG. Im Zweifel immer 'duplicate'.\n\n"
        "DUPLIKAT: Dasselbe Ereignis wie ein bereits geposteter Artikel, ohne EINEN konkreten\n"
        "  neuen Fakt. Auch wenn eine andere Quelle es anders formuliert, andere Details\n"
        "  betont oder eine neue Schlagzeile wählt — das bleibt ein Duplikat.\n"
        "UPDATE: Dasselbe Ereignis, aber mit einer NEUEN, BENENNBAREN Tatsache, die in den\n"
        "  bisherigen Posts nachweislich fehlt — z.B. Festnahme erfolgt, Opferzahl geändert,\n"
        "  Täter identifiziert, Urteil gefallen, offizielle Reaktion.\n"
        "NEU: Ein anderes, unabhängiges Ereignis.\n\n"
        "Für 'update' MUSST du in 'new_fact' die neue Tatsache in wenigen Worten benennen.\n"
        "Kannst du keine benennen, ist es 'duplicate'. Formulierungen wie 'mehr Details',\n"
        "'andere Perspektive', 'ausführlicher' zählen NICHT als neue Tatsache.\n\n"
        "Antworte NUR mit JSON: {\"status\": \"new\"|\"duplicate\"|\"update\", "
        "\"related_index\": null|0..N, \"new_fact\": \"\", \"reason\": \"...\"}\n"
        "related_index = Index des verwandten Posts aus der Liste (nur bei update/duplicate)."
    )
    user_msg = (
        f"NEUER ARTIKEL:\nTitel: {new_title}\nPost: {new_text}\n\n"
        f"BEREITS GEPOSTET (letzte 72h):\n{recent_str}\n\n"
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
        # An update has to name what is actually new — otherwise it is a rerun
        if status == "update":
            fact = (data.get("new_fact") or "").strip()
            vague = re.search(r'mehr detail|ausführlich|andere perspektive|neue quelle|'
                              r'genauer|weitere info|zusätzliche info', fact, re.I)
            if len(fact) < 8 or vague:
                logger.info("[TOPIC CHECK] %s → update ohne benennbaren neuen Fakt (%r) "
                            "→ als Duplikat behandelt", new_title[:60], fact[:40])
                status = "duplicate"
            elif _fact_already_posted(fact, recent_list):
                # Das Modell nennt zwar eine Tatsache, sie stand aber schon in
                # einem früheren Post — genau so entstanden fünf "Updates" zur
                # selben UBS-Eigenkapitalvorlage.
                logger.info("[TOPIC CHECK] %s → \"neuer\" Fakt %r stand schon in einem "
                            "früheren Post → Duplikat", new_title[:60], fact[:40])
                status = "duplicate"
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


# Phrases that mean the model talked *about* the task instead of doing it.
# Such text must never be published — it once went out as a tweet verbatim.
_REFUSAL_RE = re.compile(
    r"(ich kann (dir |diesen |hier )?(leider )?kein|ich erstelle kein|ich werde kein|"
    r"nach meinen richtlinien|meine richtlinien|ich darf (hier )?kein|"
    r"als (ki|ai|sprachmodell)\b|verstösst gegen|"
    r"falls du einen anderen|helfe ich (dir )?gerne weiter|"
    r"möchtest du, dass ich|soll ich stattdessen|"
    r"erfordern sensibilität|wäre unangemessen|"
    r"i (can'?t|cannot|am unable|won'?t)\b|as an ai\b|i'?m sorry\b)",
    re.I)


def _is_publishable(text: Optional[str]) -> tuple[bool, str]:
    """A generated post must BE a post — not a refusal, apology or a question
    back to us. Returns (ok, reason)."""
    if not text or not text.strip():
        return False, "leer"
    t = text.strip()
    m = _REFUSAL_RE.search(t)
    if m:
        return False, f"Verweigerung/Meta-Antwort der KI ({m.group(0)!r})"
    # Every post is required to end with #Schweiz; a refusal never does.
    if "#Schweiz" not in t:
        return False, "kein #Schweiz — vermutlich kein gültiger Post"
    return True, "ok"


# Sent on a second attempt when the model declined. Reporting a crime or a
# fatality is ordinary journalism — the news must not simply be dropped.
_NEWS_CONTEXT_HINT = (
    "\n\nWICHTIG: Das ist ein Auftrag für sachliche Nachrichtenberichterstattung eines "
    "Schweizer News-Accounts. Auch Verbrechen, Todesfälle und Unfälle gehören dazu und werden "
    "berichtet — genau wie in jeder Zeitung. Fasse ausschliesslich die belegten Fakten "
    "zusammen: was ist passiert, wo, wann, wer ist betroffen, wie geht es weiter. "
    "Nüchtern und respektvoll, ohne Humor, ohne Zuspitzung, ohne Spekulation. "
    "Gib NUR den fertigen Post aus — keine Ablehnung, kein Kommentar zum Auftrag."
)



def _source_material(item: dict) -> str:
    """What the model gets to work with. The full article when we could fetch
    it, otherwise the RSS snippet."""
    full = (item.get("article_text") or "").strip()
    if len(full) > 400:
        return f"Artikeltext:\n{full[:5000]}"
    return f"Zusammenfassung: {(item.get('summary') or '')[:800]}"


def generate_post(client: anthropic.Anthropic, model: str, item: dict) -> Optional[str]:
    user_msg = (
        f"Titel: {item['title']}\n\n"
        f"{_source_material(item)}\n\n"
        f"Quelle: {item.get('source_id', '')}"
    )
    def _make(msg):
        raw = _call_claude(client, model, POST_SYSTEM, msg)
        if not raw:
            return None
        # Drop any commentary the model wrote around the post, and keep only
        # the first post if it bundled several into one answer.
        return _first_post_only(_strip_preamble(raw))

    text = _make(user_msg)
    ok, reason = _is_publishable(text)
    if not ok and "Verweigerung" in reason:
        logger.info("Post abgelehnt — zweiter Versuch mit Nachrichten-Kontext: %s",
                    item.get("title", "")[:60])
        text = _make(user_msg + _NEWS_CONTEXT_HINT)
        ok, reason = _is_publishable(text)
    if not ok:
        logger.warning("Post verworfen (%s): %s | %r",
                       reason, item.get("title", "")[:60], (text or "")[:120])
        return None
    return text


# Marker between tweets in a stored thread (must match publisher.THREAD_SEP_TOKEN)
THREAD_SEP_TOKEN = "===NEXT==="


MAX_THREAD_TWEETS = 4

_DIVIDER_RE = re.compile(r'^\s*[-–—_*=]{3,}\s*$')
# Blocks that are the model thinking out loud, not tweet content
_META_RE = re.compile(r'SUBSTANZ|KEIN_THREAD|→\s*THREAD|^\s*✓|^\s*THREAD\s*:?\s*$'
                      r'|^\s*(TWEET|AUFBAU|LÄNGE)\b'
                      # "**POST 1:**", "Ich erstelle 5 separate Posts:", "Hier sind 3 …"
                      r'|^\s*\**\s*POST\s*\d+\s*[:.]?\s*\**\s*$'
                      r'|ich erstelle\s+\d+|hier sind\s+\d+|folgende\s+\d+\s+posts',
                      re.I | re.M)

# A line that is nothing but hashtags — the end of a post
_HASHTAG_LINE_RE = re.compile(r'^\s*#\S+(?:\s+#\S+)*\s*$')


def _first_post_only(text: str) -> str:
    """Keep only the first post. The model sometimes bundles several posts into
    one answer; everything after the first hashtag line belongs to the next one."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if _HASHTAG_LINE_RE.match(line):
            return "\n".join(lines[:i + 1]).rstrip()
    return text


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


def _isolate_cliffhanger(text: str) -> str:
    """Put the 👇🧵 cliffhanger on its own line, separated by a blank line.
    Glued to the body text it disappears when readers skim."""
    t = text.rstrip()
    if "🧵" not in t:
        return text
    lines = t.split("\n")
    # Cliffhanger stuck to the end of the preceding sentence → break it out
    m = re.match(r'^(.*[.!?:])\s+([^.!?]*🧵[^.!?]*)$', lines[-1].strip())
    if m:
        cliff = m.group(2).strip()
        # Splitting can leave just the emojis behind — that is not a cliffhanger
        if not re.sub(r'[👇🧵\s]', '', cliff):
            cliff = "Was dahinter steckt 👇🧵"
        lines[-1] = m.group(1).rstrip()
        lines += ["", cliff]
    # Already its own line, but no blank line above it → add one
    elif len(lines) > 1 and lines[-2].strip():
        lines.insert(len(lines) - 1, "")
    return "\n".join(lines)


def _trim_hook(text: str) -> str:
    """Tweet 1 is headline + cliffhanger only. Body text there would give away
    what tweet 2 is for."""
    blocks = [b.strip() for b in re.split(r'\n\s*\n', text.strip()) if b.strip()]
    if len(blocks) > 2 and "🧵" in blocks[-1]:
        return blocks[0] + "\n\n" + blocks[-1]
    return text


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
        f"{_source_material(item)}\n\n"
        f"Quelle: {item.get('source_id', '')}"
    )
    max_tokens = 2200 if long_form else 1000
    raw = _call_claude(client, model, system, user_msg, max_tokens=max_tokens)
    # A refusal is not an answer — retry once, spelling out the news context
    if raw and _REFUSAL_RE.search(raw) and "KEIN_THREAD" not in raw[:200].upper():
        logger.info("Thread abgelehnt — zweiter Versuch mit Nachrichten-Kontext: %s",
                    item.get("title", "")[:60])
        raw = _call_claude(client, model, system, user_msg + _NEWS_CONTEXT_HINT,
                           max_tokens=max_tokens)
    if not raw:
        return None, "api_error"
    # The model may decline when the source is too thin for a thread
    if "KEIN_THREAD" in raw[:200].upper():
        logger.info("Thread declined (source too thin): %s", item.get("title", "")[:60])
        return None, "thin"
    # Still refusing after the retry — that text must never become a tweet
    if _REFUSAL_RE.search(raw):
        logger.warning("Thread refused by model: %s | %r",
                       item.get("title", "")[:60], raw[:120])
        return None, "refused"
    # Split on the ===NEXT=== marker (tolerant of extra = or whitespace)
    parts = [p.strip() for p in re.split(r'={2,}\s*NEXT\s*={2,}', raw) if p.strip()]
    if len(parts) < 2:
        logger.warning("Thread response had no separators: %r", raw[:160])
        return None, "bad_format"
    # Drop any reasoning the model printed before the first tweet
    parts[0] = _strip_preamble(parts[0])
    if len(parts) > 2 and _looks_like_meta(parts[0]):
        parts = parts[1:]  # the whole first block was meta, not a tweet
    # When capping, keep the closing tweet — it carries the hashtags
    if len(parts) > MAX_THREAD_TWEETS:
        parts = parts[:MAX_THREAD_TWEETS - 1] + [parts[-1]]
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
    parts[0] = _isolate_cliffhanger(parts[0])
    # A missing hashtag is not worth throwing a good thread away — add it
    if "#Schweiz" not in parts[-1]:
        logger.info("Thread ohne #Schweiz — Hashtag ergänzt")
        parts[-1] = parts[-1].rstrip() + "\n\n#Schweiz"
    return (f"\n{THREAD_SEP_TOKEN}\n").join(parts), "ok"


def generate_thread(client: anthropic.Anthropic, model: str, item: dict,
                    viral_score: int = 0) -> Optional[str]:
    """Thread text, or None when a single post should be used instead."""
    return generate_thread_detailed(client, model, item, viral_score)[0]
