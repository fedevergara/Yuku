"""Shared quality rules for projects and patents, independent of DB access."""

from datetime import date, datetime
import html
import re
import unicodedata
from zoneinfo import ZoneInfo


PRODUCT_QUALITY_VERSION = "minciencias-project-patent-quality-v1"
BOGOTA = ZoneInfo("America/Bogota")
# Exact whole-title matches only: short names and trademarks remain eligible.
PLACEHOLDER_TITLES = frozenset({
    "prueba", "pruebas", "test", "testing", "patente", "patentes",
    "sin titulo", "sin título", "sin información", "sin informacion",
    "no aplica", "n/a", "na", "pendiente",
})
MONTHS = {
    name: month for month, names in enumerate((
        ("enero", "january"), ("febrero", "february"), ("marzo", "march"),
        ("abril", "april"), ("mayo", "may"), ("junio", "june"),
        ("julio", "july"), ("agosto", "august"),
        ("septiembre", "setiembre", "september"), ("octubre", "october"),
        ("noviembre", "november"), ("diciembre", "december"),
    ), 1) for name in names
}


def clean_product_text(value):
    """Decode bounded nested HTML entities, compose accents and collapse spaces."""
    text = str(value or "")
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    text = unicodedata.normalize("NFC", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Cc" or c.isspace())
    return re.sub(r"\s+", " ", text).strip()


def placeholder_title(value):
    return clean_product_text(value).casefold().strip(" .,:;!¿?¡-_()") in PLACEHOLDER_TITLES


def explicit_language(record):
    """Only use a declared language; the profile site's language proves nothing."""
    value = clean_product_text(record.get("language")).casefold()
    languages = {
        "es": "es", "español": "es", "spanish": "es", "castellano": "es",
        "en": "en", "inglés": "en", "ingles": "en", "english": "en",
        "pt": "pt", "portugués": "pt", "portugues": "pt", "portuguese": "pt",
        "fr": "fr", "francés": "fr", "frances": "fr", "french": "fr",
        "de": "de", "alemán": "de", "aleman": "de", "german": "de",
        "it": "it", "italiano": "it", "italian": "it",
    }
    return languages.get(value, "")


def actual_date(value, *, as_of=None):
    """Parse explicit date precision; reject 1900, impossible and future dates.

    This applies to real starts/ends and patent presentation dates. Planned
    ends are evidence only and deliberately do not pass through this function.
    """
    today = as_of or datetime.now(BOGOTA).date()
    year = month = day = None
    if isinstance(value, datetime):
        dt = value.astimezone(BOGOTA) if value.tzinfo else value.replace(tzinfo=BOGOTA)
        year, month, day = dt.year, dt.month, dt.day
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if 1000 <= value <= 9999:
            year = int(value) if int(value) == value else None
        else:
            try:
                dt = datetime.fromtimestamp(value, BOGOTA)
                year, month, day = dt.year, dt.month, dt.day
            except (ValueError, OverflowError, OSError):
                return None
    else:
        text = clean_product_text(value).casefold().strip(" ,.;")
        if not text:
            return None
        if re.fullmatch(r"\d{4}", text):
            year = int(text)
        elif match := re.fullmatch(r"(\d{4})[-/](\d{1,2})(?:[-/](\d{1,2}))?", text):
            year, month, day = (int(v) if v else None for v in match.groups())
        elif match := re.fullmatch(r"(\d{1,2})/(\d{4})", text):
            month, year = map(int, match.groups())
        elif match := re.fullmatch(r"([a-z]+)\s+(\d{4})", text):
            month, year = MONTHS.get(match[1]), int(match[2])
            if month is None:
                return None
        elif match := re.fullmatch(r"(\d{4})\s+([a-z]+)", text):
            year, month = int(match[1]), MONTHS.get(match[2])
            if month is None:
                return None
        else:
            # Socrata uses ISO timestamps. Do not extract a year from junk text.
            try:
                dt = datetime.fromisoformat(text.replace("z", "+00:00"))
            except ValueError:
                return None
            return actual_date(dt, as_of=today)
    if year is None or not 1901 <= year <= today.year:
        return None
    try:
        earliest = date(year, month if month is not None else 1, day if day is not None else 1)
    except ValueError:
        return None
    if earliest > today:
        return None
    return {"year": year, "month": month, "day": day}


def date_consensus(values):
    """Return a coherent timestamp/year pair using only supported precision."""
    dates = [parsed for value in values if (parsed := actual_date(value))]
    years = {d["year"] for d in dates}
    if len(years) != 1:
        return None, None
    year = next(iter(years))
    months = {d["month"] for d in dates if d["month"] is not None}
    if len(months) != 1:
        return None, year
    month = next(iter(months))
    days = {d["day"] for d in dates if d["day"] is not None}
    # As with month-only source dates, the month's first day represents month
    # precision. A year-only consensus never invents January 1st.
    day = next(iter(days)) if len(days) == 1 else 1
    return int(datetime(year, month, day, tzinfo=BOGOTA).timestamp()), year


def reconcile_project_dates(document):
    """Recompute after every merge; never let the first occurrence win a conflict."""
    occurrences = (document.get("source_metadata") or {}).get("occurrences") or []
    metadata = [o.get("metadata") or {} for o in occurrences if o.get("source_kind") != "minciencias_open_data"]
    for source, stamp, year in (("start_date", "date_init", "year_init"), ("end_date", "date_end", "year_end")):
        values = [m.get(source) or (m.get("year") if source == "start_date" else None) for m in metadata]
        values = [v for v in values if v not in (None, "")]
        if not values:
            values = [document.get(stamp), document.get(year)]
        document[stamp], document[year] = date_consensus(values)
    return document


def product_quality_findings(document, entity):
    """Final publication checks, including reused/stale upstream collections."""
    if entity not in {"projects", "patents"}:
        return []
    findings = []
    for title in document.get("titles") or []:
        text = title.get("title", "")
        if placeholder_title(text) or not clean_product_text(text):
            findings.append("unusable_title")
        elif text != clean_product_text(text):
            findings.append("unclean_title")
    if entity == "projects":
        for stamp, year in (("date_init", "year_init"), ("date_end", "year_end")):
            parsed = actual_date(document.get(stamp))
            if document.get(stamp) is not None and (
                parsed is None or parsed["year"] != document.get(year)
            ):
                findings.append("inconsistent_actual_date")
            if document.get(year) is not None and actual_date(document[year]) is None:
                findings.append("invalid_year")
        expected = reconcile_project_dates(dict(document))
        if any(expected.get(k) != document.get(k) for k in ("date_init", "year_init", "date_end", "year_end")):
            findings.append("date_consensus_not_applied")
    metadata = document.get("source_metadata") or {}
    scraped_occurrences = [o for o in metadata.get("occurrences") or [] if o.get("source_kind") in {"cvlac", "gruplac"}]
    declared_languages = {explicit_language(o.get("metadata") or {}) for o in scraped_occurrences}
    if scraped_occurrences and any(t.get("lang") and t["lang"] not in declared_languages for t in document.get("titles") or []):
        findings.append("unsupported_title_language")
    official = metadata.get("minciencias") or {}
    values = list(official.get("measurements") or [])
    for product in official.get("measured_products") or []:
        values.extend(product.get("measurements") or [])
    if entity == "patents":
        values.extend(o.get("metadata") or {} for o in metadata.get("occurrences") or []
                      if o.get("source_kind") != "minciencias_open_data")
    for measurement in values:
        for field in ("presentation_date", "presentation_year"):
            value = measurement.get(field)
            if value not in (None, "") and actual_date(value) is None:
                findings.append("invalid_presentation_date")
    return sorted(set(findings))
