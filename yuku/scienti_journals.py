"""Extract journal context from the explicit, locally stored ScienTI text."""

import re

from yuku.cvlac_related_works import (
    VOLUME_LABEL_WORDS, clean_bibliographic_value, clean_text, extract_publisher, norm_text,
)

JOURNAL_RULE = "scienti-explicit-journal-v1"
# Explicit country names observed in the legacy unpunctuated CVLAC slot.
# Unknown countries are left unresolved; these labels are not journal guesses.
COUNTRY_LABELS = tuple(sorted((
    "Estados Unidos de América", "Estados Unidos", "Reino Unido", "Países Bajos",
    "República Checa", "Corea del Sur", "Nueva Zelanda", "Costa Rica",
    "Colombia", "Polonia", "Brasil", "Brazil", "España", "Espana", "México",
    "Mexico", "Argentina", "Chile", "Perú", "Peru", "Ecuador", "Venezuela",
    "Uruguay", "Cuba", "Canadá", "Canada", "Francia", "Alemania", "Italia",
    "Portugal", "Suiza", "Suecia", "Noruega", "Dinamarca", "Finlandia",
    "Holanda", "Bélgica", "Belgica", "Austria", "Grecia", "Turquía",
    "Inglaterra", "Escocia", "Australia", "China", "Japón", "Japon", "India",
    "Rusia", "Sudáfrica", "Sudafrica", "United States", "United Kingdom",
), key=len, reverse=True))
ISSUE_RE = re.compile(
    r"\b(?:fasc(?:[ií]culo)?|issue|n[uú]mero)\b\.?\s*:?\s*(.*?)"
    r"(?=\s*,?\s*(?:p(?:[aá]gs?|[aá]ginas)?\.?|DOI|ISSN|ISBN|ed|Editorial|Autores?)"
    r"\s*[:.]|[,;]|$)", re.I,
)


def journal_context(text, title, *, source_fields=None, country=""):
    """Accept labelled names or the bounded journal slot immediately before ISSN.

    The legacy CVLAC layout separates country and journal with punctuation;
    without an explicit separator/label the name is left unresolved.
    """
    text, title = clean_text(text), clean_text(title)
    fields = source_fields or {}
    name = clean_bibliographic_value(fields.get("journal_name", ""))
    if not name:
        labelled = re.search(
            r"\b(?:Nombre de la revista|Revista)\s*:\s*(.*?)\s*(?=ISSN\s*:)",
            text, re.I,
        )
        if labelled:
            name = clean_bibliographic_value(labelled.group(1))
    if not name and title:
        anchor = re.search(re.escape(title), text, re.I)
        issn = re.search(r"\bISSN\s*:", text[anchor.end():], re.I) if anchor else None
        if anchor and issn:
            slot = text[anchor.end():anchor.end() + issn.start()].strip(' \"“”.,;')
            legacy = bool(re.match(r"^En\s*:", slot, re.I))
            slot = re.sub(r"^En\s*:\s*", "", slot, flags=re.I)
            # Both formats use a comma or a period after the country. Never
            # split an unbounded string of words into guessed country/name.
            if country and re.match(re.escape(country) + r"\s*[,.;]", slot, re.I):
                slot = re.sub(r"^" + re.escape(country) + r"\s*[,.;]\s*", "", slot, flags=re.I)
                slot = re.sub(r"^(?:19|20)\d{2}\s*[,.;]\s*", "", slot)
                name = clean_bibliographic_value(slot)
            elif legacy and country and re.match(re.escape(country) + r"\s+\S", slot, re.I):
                # The parser bounded the country by its <br>; the rest is the name.
                name = clean_bibliographic_value(slot[len(country):])
            elif re.match(r"^[^,.;:]{2,60}[,.;]\s*\S", slot):
                name = clean_bibliographic_value(re.split(r"[,.;]\s*", slot, maxsplit=1)[1])
            elif legacy:
                for label in COUNTRY_LABELS:
                    match = re.match(re.escape(label) + r"\s+(.+)$", slot, re.I)
                    if match:
                        name = clean_bibliographic_value(match.group(1))
                        break
    if (len(name) > 500 or re.search(r"\b(?:DOI|ISBN|Autores?|Editorial|ed|vol|pags)\s*[:.]", name, re.I)
            or norm_text(name) in {"", "n/a", "na", "0", "no disponible"}):
        name = ""
    # Search after the title: "Número 1!" inside a title is not an issue.
    anchor = re.search(re.escape(title), text, re.I) if title else None
    issue = ISSUE_RE.search(text[anchor.end():] if anchor else text)
    issue = clean_bibliographic_value(issue.group(1)) if issue else ""
    if len(issue) > 40 or norm_text(issue) in {"n/a", "na", "0"}:
        issue = ""
    publisher = re.search(
        r"\b(?:ed[.:]|Editorial\s*:)\s*(.*?)"
        r"(?=\s*,?\s*(?:v\.|vol\.?\s*:|fasc[.:]|p(?:[aá]gs?|[aá]ginas)?\.?\s*[:.]|"
        r"DOI\s*:|ISSN\s*:|ISBN\s*:|Autores?\s*:)|$)", text, re.I,
    )
    from yuku.cvlac_related_works import clean_publisher_value
    publisher = (clean_publisher_value(publisher.group(1)) if publisher
                 else extract_publisher(text, fields))
    volume = re.search(r"\b(?:v\.|vol(?:umen)?\.?\s*:)\s*([\w./-]+)(?![\w./-]|\s*:)", text, re.I)
    volume = clean_bibliographic_value(volume.group(1)) if volume else ""
    if (norm_text(volume) in {"n", "na", "n/a"} | VOLUME_LABEL_WORDS
            or not re.search(r"[0-9IVXLCivxlc]", volume)):
        volume = ""
    return {"journal_name": name, "issue": issue, "publisher": publisher, "volume": volume}


def editorial_medium(text, title, country=""):
    """Medium name and issue of non-specialized editorial products only."""
    values = journal_context(text, title, country=country)
    return {"journal_name": values["journal_name"], "issue": values["issue"]}


def enrich_editorial_medium(record):
    """Fill only missing medium name/issue; publisher and volume keep their rules."""
    if norm_text(str(record.get("type_impactu") or "")) != "publicaciones editoriales no especializadas":
        return record
    values = editorial_medium(record.get("raw_text", ""), record.get("title", ""),
                              country=record.get("country", ""))
    for field, value in values.items():
        if value and not record.get(field):
            record[field] = value
    return record


def enrich_article(record):
    """Only populate article context; keep raw text and all other fields intact."""
    if norm_text(str(record.get("type_impactu") or "")) != "articulo de revista":
        return record
    values = journal_context(record.get("raw_text", ""), record.get("title", ""),
                             country=record.get("country", ""))
    record.update(values)
    return record
