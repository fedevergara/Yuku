from __future__ import annotations

import html as ihtml
import re
import unicodedata
from typing import Any

from bs4 import BeautifulSoup


SCIENTI_CVLAC_URL = "https://scienti.minciencias.gov.co/cvlac/visualizador/generarCurriculoCv.do?cod_rh="

ALLOWED_SECTION_ALIASES = {
    "trabajos dirigidos/tutorias": "Trabajos dirigidos/tutorias",
    "articulos": "Articulos",
    "libros": "Libros",
    "capitulos de libro": "Capitulos de libro",
    "libros de divulgacion y/o compilacion de divulgacion": "Libros de divulgacion y/o Compilacion de divulgacion",
    "libro de formacion": "Libro de Formacion",
    "publicaciones editoriales no especializadas": "Publicaciones editoriales no especializadas",
    "patentes": "Patentes",
    "textos en publicaciones no cientificas": "Publicaciones editoriales no especializadas",
}

DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s<>\"{}|\\^`\[\]]+", re.IGNORECASE)
YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
ISSN_RE = re.compile(r"\bISSN\s*:\s*([\dXx]{4}-?[\dXx]{3}[\dXx])", re.IGNORECASE)
ISBN_RE = re.compile(r"\bISBN\s*:\s*([0-9Xx](?:[0-9Xx]-?){8,16}[0-9Xx])", re.IGNORECASE)
VALID_PAGES_RE = re.compile(r"^[0-9]+(?:\s*-\s*[0-9]+)?$")
VALID_LANGUAGE_RE = re.compile(r"^[^\W\d_]+(?:[ /-][^\W\d_]+){0,5}$")
SUSPICIOUS_PUBLISHER_RE = re.compile(
    r"^(?:Autores\s*:|v\.(?=\s|:|$)|(?:vol|p[aá]gs?)\.?(?=\s|:|$)|"
    r"isbn\b|issn\b|"
    r"[0-9Xx](?:[0-9Xx]-?){8,16}[0-9Xx]$)",
    re.IGNORECASE,
)

LABEL_ALIASES = {
    "nombre del producto": "product_name",
    "nombre del capitulo": "chapter_name",
    "nombre del libro": "book_name",
    "fecha de presentacion": "presentation_date",
    "palabras": "keywords",
    "areas": "areas",
    "sectores": "sectors",
    "estado": "status",
    "dirigio como": "advisor_role",
    "persona(s) orientada(s)": "oriented_people",
    "tutor(es)/cotutor(es)": "tutors",
    "isbn": "isbn_label",
    "editorial": "publisher",
    "edicion": "edition",
    "numero de edicion": "edition",
    "numero de paginas": "pages",
    "lugar de publicacion": "publication_place",
    "medio de divulgacion": "dissemination_medium",
    "idioma": "language",
    "idiomas": "language",
    "institucion": "institution",
    "via de solicitud": "request_route",
    "nombre del solicitante de la patente": "patent_applicant",
    "gaceta industrial de publicacion": "industrial_publication_gazette",
}
LABEL_ORDER = sorted(LABEL_ALIASES, key=len, reverse=True)
LABEL_RE = re.compile(r"(?P<label>" + "|".join(re.escape(x) for x in LABEL_ORDER) + r")\s*:", re.IGNORECASE)

KNOWN_INLINE_TYPES = [
    "Otro capítulo de libro publicado",
    "Capítulo de libro",
    "Libro resultado de investigación",
    "Libros de divulgación y/o Compilación de divulgación",
    "Libros de formación",
]
KNOWN_INLINE_TYPES_RE = re.compile(
    r"\bTipo\s*:\s*(?:" + "|".join(re.escape(item) for item in KNOWN_INLINE_TYPES) + r")\s*",
    re.IGNORECASE,
)
IGNORED_NAME_TOKENS = {
    "de",
    "del",
    "la",
    "las",
    "los",
    "el",
    "y",
    "ltd",
    "ltda",
    "limited",
    "inc",
    "corp",
    "corporation",
    "sa",
    "sas",
}
PROJECT_TYPES = [
    "Investigación, desarrollo e Innovación",
    "Investigación y desarrollo",
    "Investigación-Creación",
    "Extensión y responsabilidad social CTI",
]
PROJECT_TYPES_RE = re.compile(
    r"Tipo de proyecto\s*:\s*(?P<project_type>"
    + "|".join(re.escape(item) for item in PROJECT_TYPES)
    + r")\s*(?P<title>.*?)\s+Inicio\s*:",
    re.IGNORECASE,
)


def remove_accents(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", value or "") if not unicodedata.combining(char)
    )


def norm_text(value: str) -> str:
    value = ihtml.unescape(value or "").replace("\xa0", " ")
    value = remove_accents(value).lower().strip()
    return re.sub(r"\s+", " ", value)


def clean_text(value: str) -> str:
    value = ihtml.unescape(value or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def strip_value(value: str) -> str:
    return clean_text(value).strip(" ,.;:-")


def clean_bibliographic_value(value: str) -> str:
    value = strip_value(value)
    if norm_text(value) in {"0", "n/a", "na", "no aplica", "no disponible"}:
        return ""
    return value


def is_suspicious_publisher(value: str) -> bool:
    """Return whether a publisher value is leaked metadata or an identifier."""
    return bool(SUSPICIOUS_PUBLISHER_RE.match(clean_bibliographic_value(value)))


def clean_publisher_value(value: str) -> str:
    """Accept a bounded publisher name while leaving the raw record untouched."""
    value = clean_bibliographic_value(value)
    if len(value) > 300 or is_suspicious_publisher(value):
        return ""
    return value


def clean_pages_value(value: str) -> str:
    """Accept only one explicit page or a numeric page range."""
    value = clean_bibliographic_value(value)
    return value if VALID_PAGES_RE.fullmatch(value) else ""


def clean_language_value(value: str) -> str:
    """Accept a short language name, not prose matched after an inline label."""
    value = clean_bibliographic_value(value)
    return value if len(value) <= 60 and VALID_LANGUAGE_RE.fullmatch(value) else ""


def uniq_keep_order(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        value = strip_value(value)
        if not value:
            continue
        key = norm_text(value)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def normalize_doi(value: str) -> str:
    value = value.strip().rstrip(".,;:)]}")
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.IGNORECASE)
    return "https://doi.org/" + value.lower()


def split_terms(value: str) -> list[str]:
    parts = re.split(r"\s*,\s*|\s*;\s*", value or "")
    return uniq_keep_order([p for p in parts if len(strip_value(p)) > 1])


def split_areas(value: str) -> list[str]:
    value = re.split(r"\bsectores\s*:", value or "", maxsplit=1, flags=re.IGNORECASE)[0]
    area_paths = re.sub(
        r"\s*,\s*(?=(?:ciencias|humanidades|ingenieria|ingeniería)\b)",
        "|||",
        value,
        flags=re.IGNORECASE,
    ).split("|||")

    levels = []
    for area_path in area_paths:
        levels.extend(re.split(r"\s*--\s*", area_path))
    return uniq_keep_order([level for level in levels if len(strip_value(level)) > 1])


def normalize_isbn(value: str) -> str:
    match = re.search(r"[0-9Xx](?:[0-9Xx]-?){8,16}[0-9Xx]", value or "")
    return match.group(0).upper() if match else ""


def split_people(value: str) -> list[str]:
    parts = re.split(r"\s*,\s*|\s+;\s+", value or "")
    return uniq_keep_order([p for p in parts if len(strip_value(p)) > 2])


def split_oriented_people(value: str) -> list[str]:
    """Split thesis students without guessing boundaries between bare names."""
    value = re.split(
        r"(?:^|\s+)(?:asesor(?:\s*\(es\))?|tutor(?:\s*\(es\))?|"
        r"cotutor(?:\s*\(es\))?)\s*:",
        value or "",
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    parts = re.split(
        r"\s*,\s*|\s*;\s*|\s+/\s+|\s+-\s+|\s+y\s+",
        value,
        flags=re.IGNORECASE,
    )
    cleaned = []
    for part in parts:
        part = re.sub(r"\b\S+@\S+\b", " ", part)
        part = re.split(
            r"\s+(?:universidad|corporacion(?:\s+universitaria)?|fundacion|"
            r"institucion)\b",
            part,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        part = re.sub(
            r"\s+\d+\s+mes(?:es)?\s*$",
            "",
            part,
            flags=re.IGNORECASE,
        )
        part = strip_value(part)
        if len(part) > 2:
            cleaned.append(part)
    return uniq_keep_order(cleaned)


def name_tokens(value: str) -> set[str]:
    tokens = re.split(r"[^a-z0-9]+", norm_text(value))
    return {token for token in tokens if len(token) > 2 and token not in IGNORED_NAME_TOKENS}


def clean_inline_type_prefix(value: str) -> str:
    value = KNOWN_INLINE_TYPES_RE.sub("", value or "")
    if norm_text(value).startswith("tipo:"):
        value = re.sub(r"\bTipo\s*:\s*[^,.;\n]{2,80}", "", value, count=1, flags=re.IGNORECASE)
    return strip_value(value)


def extract_inline_type(text: str) -> str:
    text_n = norm_text(text)
    for item in KNOWN_INLINE_TYPES:
        pattern = r"(?:^|\b)tipo\s*:\s*" + re.escape(norm_text(item)) + r"\b"
        if re.search(pattern, text_n, flags=re.IGNORECASE):
            return item
    match = re.search(r"(?:^|\b)tipo\s*:\s*([^,.;\n]{2,80})", text_n, flags=re.IGNORECASE)
    return strip_value(match.group(1)) if match else ""


def first_person_before_comma(text: str) -> list[str]:
    if "," not in text:
        return []
    first = clean_inline_type_prefix(text.split(",", 1)[0])
    return split_people(first)


def parse_labeled_fields(text: str) -> dict[str, Any]:
    text_n = norm_text(text)
    matches = list(LABEL_RE.finditer(text_n))
    fields: dict[str, Any] = {}

    for i, match in enumerate(matches):
        canonical = LABEL_ALIASES.get(norm_text(match.group("label")))
        if not canonical:
            continue
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text_n)
        value = strip_value(text_n[start:end])
        if value:
            fields[canonical] = value
    return fields


def parse_html_labeled_fields(blockquote) -> dict[str, str]:
    """Read CVLAC ``<i>Label:</i> value`` pairs without normalizing values.

    ``parse_labeled_fields`` predates the entity normalizers and intentionally
    normalizes its complete input.  That is useful for search, but unsuitable
    for preserving thesis, patent and institutional metadata.  CVLAC renders
    those values as sibling nodes after an italic label, so this parser uses
    the HTML boundary as the authority and never guesses a label by
    similarity.
    """
    fields: dict[str, str] = {}
    for label_node in blockquote.find_all(["i", "em"]):
        label = strip_value(label_node.get_text(" ", strip=True)).rstrip(":")
        canonical = LABEL_ALIASES.get(norm_text(label))
        if not canonical:
            continue
        values: list[str] = []
        for sibling in label_node.next_siblings:
            if getattr(sibling, "name", None) in {"i", "em"}:
                break
            if getattr(sibling, "name", None) in {"b", "strong"} and norm_text(
                sibling.get_text(" ", strip=True)
            ).rstrip(":") in {"areas", "palabras", "sectores"}:
                break
            if hasattr(sibling, "get_text"):
                values.append(sibling.get_text(" ", strip=True))
            else:
                values.append(str(sibling))
        value = strip_value(" ".join(values))
        if value:
            fields[canonical] = value
    return fields


def extract_product_type(section_title: str, blockquote) -> str:
    parent_tr = blockquote.find_parent("tr")
    if parent_tr:
        previous_tr = parent_tr.find_previous_sibling("tr")
        if previous_tr and not previous_tr.find("blockquote"):
            b = previous_tr.find("b")
            if b:
                candidate = clean_text(b.get_text(" ", strip=True))
                if candidate and not candidate.endswith(":"):
                    return candidate

    if norm_text(section_title) == "patentes":
        return "Patente"

    detail_text = clean_text(blockquote.get_text(" ", strip=True))
    inline_type = extract_inline_type(detail_text)
    return inline_type or section_title


def classify_target_category(section_title: str, product_type: str) -> str:
    section_n = norm_text(section_title)
    product_n = norm_text(product_type)
    blob_n = f"{section_n} | {product_n}"

    if section_n == "trabajos dirigidos/tutorias":
        if any(token in blob_n for token in ["tesis de doctorado", "trabajo de grado de maestria", "especialidad clinica"]):
            return "Tesis de posgrado"
        if any(token in blob_n for token in ["trabajos de grado de pregrado", "tesis de pregrado"]):
            return "Tesis de pregrado"
        return "Trabajos dirigidos/tutorias"
    if section_n == "articulos":
        return "Articulo de revista"
    if section_n == "capitulos de libro":
        return "Capitulo de libro"
    if section_n in {"libros", "libros de divulgacion y/o compilacion de divulgacion", "libro de formacion"}:
        return "Libro"
    if section_n in {"publicaciones editoriales no especializadas", "textos en publicaciones no cientificas"}:
        return "Publicaciones editoriales no especializadas"
    if section_n == "patentes":
        return "Patente"
    return section_title


def normalize_advisor_role(value: str) -> str:
    value_n = norm_text(value)
    if value_n in {"tutor principal", "coturor/asesor", "cotutor/asesor"}:
        return "advisor"
    return strip_value(value)


def extract_patent_title(text: str) -> str:
    head = re.split(r"\bInstitución\s*:", text, maxsplit=1, flags=re.IGNORECASE)[0]
    head = strip_value(head)
    if " - " in head:
        title = head.split(" - ", 1)[1]
    else:
        title = head
    title = re.sub(r"^\([A-Z0-9 /._-]+(?:\s*\([A-Z0-9 /._-]+\))?\)\s*-?\s*", "", title, flags=re.IGNORECASE)
    return strip_value(title)


def extract_patent_applicant(text: str, fields: dict[str, Any]) -> str:
    match = re.search(
        r"Nombre del solicitante de la patente\s*:\s*(.*?)(?:,\s*\.\s*Gaceta|\.\s*Gaceta|$)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return normalize_applicant(match.group(1))
    return normalize_applicant(str(fields.get("patent_applicant", "")))


def remove_applicant_when_author_match(applicant: str, profile_author_name: str) -> str:
    if not applicant or not profile_author_name:
        return applicant
    if name_tokens(applicant) & name_tokens(profile_author_name):
        return ""
    return applicant


def normalize_applicant(value: str) -> str:
    value = strip_value(value)
    value = re.sub(
        r"\s*,\s*(?=(?:ltd|ltda|limited|inc|corp|corporation|s\.?\s*a|s\.?\s*a\.?\s*s|sas)\b)",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    replacements = [
        (r"\bS\s*\.?\s*A\s*\.?\s*S\.?\b", "S.A.S"),
        (r"\bS\s*\.?\s*A\.?\b(?!\s*\.?\s*S\b)", "S.A"),
        (r"\bSAS\b", "S.A.S"),
        (r"\bLTD\.?\b", "LTD"),
        (r"\bLTDA\.?\b", "LTDA"),
        (r"\bINC\.?\b", "INC"),
        (r"\bCORP\.?\b", "CORP"),
    ]
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
    return strip_value(value)


def extract_affiliation(text: str, fields: dict[str, Any]) -> str:
    match = re.search(
        r"\bInstitución\s*:\s*(.*?)(?=\s+(?:Estado|Fecha de presentación|Via de solicitud|Vía de solicitud|Nombre del solicitante de la patente|Gaceta industrial de publicación|Palabras|Areas|Sectores)\s*:|$)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return strip_value(match.group(1))
    return strip_value(str(fields.get("institution", "")))


def extract_country(text: str) -> str:
    match = re.search(
        r"\bEn\s*:\s*(.*?)(?=\s+(?:ISSN|ISBN|DOI|ed|Editorial|v\.|Vol\.|fasc\.|pags?\.|p\.)\s*:|[,.;\n]|$)",
        text,
        flags=re.IGNORECASE,
    )
    return strip_value(match.group(1)) if match else ""


def extract_publisher(text: str, source_fields: dict[str, str]) -> str:
    """Extract only explicitly labelled publisher/editorial values.

    CVLAC has two stable layouts: ``Editorial:`` in newer labelled records
    and the historical inline ``ed:`` form.  Both are bounded by known
    bibliographic labels so a publisher can never absorb authors, pages or
    subject metadata.
    """
    if source_fields.get("publisher"):
        return clean_publisher_value(source_fields["publisher"])
    patterns = (
        r"\bEditorial\s*:\s*(.*?)"
        r"(?=\s*,?\s*(?:Idiomas?|P[aá]ginas|Areas|Sectores|Palabras|"
        r"Medio de divulgaci[oó]n|Lugar de publicaci[oó]n)\s*:|$)",
        r"\bed\s*:\s*(.*?)"
        r"(?=\s+(?:ISBN|ISSN|DOI)\s*:|\s+v\.|\s+p(?:ags?)?\.|"
        r"\s+(?:Areas|Sectores|Palabras)\s*:|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            value = clean_publisher_value(match.group(1))
            if value:
                return value
    return ""


def extract_book_title(
    text: str,
    source_fields: dict[str, str],
    section_title: str,
) -> str:
    """Return the containing book title for chapters, never the work title."""
    if norm_text(section_title) != "capitulos de libro":
        return ""
    if source_fields.get("book_name"):
        return strip_value(source_fields["book_name"])
    match = re.search(
        r'"[^"\n]{3,1000}"\s*(.*?)\s*\.\s*En\s*:',
        text or "",
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    value = re.sub(r"\s*\((?:ISSN|ISBN)\)\s*$", "", match.group(1), flags=re.IGNORECASE)
    return strip_value(value)


def extract_pages(text: str, source_fields: dict[str, str]) -> tuple[str, str, str]:
    value = clean_pages_value(source_fields.get("pages", ""))
    if not value:
        match = re.search(
            r"\bp(?:(?:[aá]gs?)|(?:[aá]ginas))?\.\s*:?[ ]*"
            r"([0-9]+(?:\s*-\s*[0-9]+)?)",
            text or "",
            flags=re.IGNORECASE,
        )
        value = clean_pages_value(match.group(1)) if match else ""
    numbers = re.fullmatch(r"([0-9]+)(?:\s*-\s*([0-9]+))?", value)
    if not numbers:
        return value, "", ""
    start = numbers.group(1)
    end = numbers.group(2) or ""
    if start == "0":
        start = ""
    if end == "0":
        end = ""
    return value, start, end


def extract_volume(text: str) -> str:
    match = re.search(
        r"\bv\.\s*:?\s*([^,;]*?)"
        r"(?=\s*,?\s*(?:fasc\.?|ISBN|ISSN|DOI|ed|Editorial)\s*:|"
        r"\s*,?\s*p(?:[aá]gs?|[aá]ginas)?\.?\s*:?(?=\s*(?:\d|[,;]|$))|"
        r"\s*,?\s*(?:19|20)\d{2}\b|$)",
        text or "",
        flags=re.IGNORECASE,
    )
    return clean_bibliographic_value(match.group(1)) if match else ""


def extract_title(
    text: str,
    fields: dict[str, Any],
    section_title: str = "",
    profile_author_name: str = "",
) -> str:
    if norm_text(section_title) == "patentes":
        return extract_patent_title(text)

    title_keys = ["product_name", "chapter_name"]
    if norm_text(section_title) != "capitulos de libro":
        title_keys.append("book_name")
    for key in title_keys:
        if fields.get(key):
            return strip_value(str(fields[key]))

    # In directed works, quoted text frequently belongs to the institution
    # (for example Escuela Militar de Cadetes "General Jose Maria Cordova"),
    # not to the work title. Prefer the text between the owner and institution.
    if norm_text(section_title) == "trabajos dirigidos/tutorias":
        title_source = text
        if "," in text:
            prefix, after_prefix = text.split(",", 1)
            prefix_tokens = name_tokens(prefix)
            owner_tokens = name_tokens(profile_author_name)
            has_owner_prefix = not owner_tokens or (
                prefix_tokens
                and (
                    prefix_tokens.issubset(owner_tokens)
                    or owner_tokens.issubset(prefix_tokens)
                )
            )
            if has_owner_prefix:
                title_source = after_prefix
        title_part = re.split(
            r"\s+(?:UNIVERSIDAD|Universidad|FUNDACION|Fundación|Fundacion|"
            r"COLEGIO|Colegio|ESCUELA|Escuela|INSTITUTO|Instituto|"
            r"INSTITUCI[ÓO]N|Instituci[óo]n)\b|"
            r"\s+Estado\s*:|\s+Dirigi[oó]\s+como\s*:",
            title_source,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        title = strip_value(title_part)
        if title:
            return title

    quoted = re.search(r'"([^"\n]{3,500})"', text)
    if quoted:
        return strip_value(quoted.group(1))

    if "Estado:" in text and "," in text:
        after_first_author = text.split(",", 1)[1]
        title_part = re.split(
            r"\s+(?:UNIVERSIDAD|Universidad|FUNDACION|Fundación|Fundacion|COLEGIO|Colegio)\b|\s+Estado\s*:",
            after_first_author,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        title = strip_value(title_part)
        if title:
            return title

    plain_match = re.search(
        r"^(?:[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ'´`\-]+(?:\s+|,\s*)){2,},?\s*(?P<title>.+?)\s+"
        r"(?:UNIVERSIDAD|Universidad|Estado\s*:|Finalidad\s*:|Nombre comercial\s*:|\.\s*En\s*:)",
        text,
    )
    if plain_match:
        return strip_value(plain_match.group("title"))
    return ""


def extract_directed_title_and_affiliation(
    blockquote, profile_author_name: str = ""
) -> tuple[str, str]:
    """Use CVLAC field line boundaries instead of guessing institution words."""
    strings = list(blockquote.stripped_strings)
    if not strings:
        return "", ""
    before_status = re.split(
        r"\bEstado\s*:", strings[0], maxsplit=1, flags=re.IGNORECASE
    )[0]
    lines = [
        strip_value(clean_text(line))
        for line in re.split(r"[\r\n]+", before_status)
        if strip_value(clean_text(line))
    ]
    if not lines:
        return "", ""
    first_tokens = name_tokens(lines[0].rstrip(","))
    owner_tokens = name_tokens(profile_author_name)
    if owner_tokens and first_tokens and (
        first_tokens.issubset(owner_tokens) or owner_tokens.issubset(first_tokens)
    ):
        lines.pop(0)
    if len(lines) < 2:
        return "", ""
    return strip_value(" ".join(lines[:-1])), strip_value(lines[-1])


def extract_authors(
    text: str,
    title: str,
    fields: dict[str, Any],
    product_type: str,
    section_title: str = "",
    profile_author_name: str = "",
) -> list[str]:
    if norm_text(section_title) == "patentes":
        return extract_patent_applicant(text, fields)

    if fields.get("product_name") or fields.get("chapter_name") or (
        fields.get("book_name") and norm_text(section_title) != "capitulos de libro"
    ):
        return []

    if norm_text(product_type).startswith("trabajos dirigidos/tutorias"):
        people = first_person_before_comma(text)
        if not profile_author_name or not people:
            return people
        prefix_tokens = name_tokens(people[0])
        owner_tokens = name_tokens(profile_author_name)
        if prefix_tokens and (
            prefix_tokens.issubset(owner_tokens)
            or owner_tokens.issubset(prefix_tokens)
        ):
            return people
        return []

    if '"' in text:
        prefix = clean_inline_type_prefix(text.split('"', 1)[0])
        return split_people(prefix)

    prefix = re.split(
        r"\b(?:Estado|Finalidad|Nombre comercial|\.\s*En|Tipo de trabajo presentado)\s*:",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    prefix = clean_inline_type_prefix(prefix)
    if title and title in prefix:
        prefix = prefix.split(title, 1)[0]
    return split_people(prefix)


def extract_year(text: str, fields: dict[str, Any]) -> int | None:
    presentation_date = str(fields.get("presentation_date", ""))
    match = re.search(r"(19\d{2}|20\d{2})", presentation_date)
    if match:
        return int(match.group(1))

    main_text = re.split(r"\bDOI\s*:", text, maxsplit=1, flags=re.IGNORECASE)[0]
    priority_patterns = [
        r",\s*(19\d{2}|20\d{2})\s*,?\s*(?:Palabras\s*:|Areas\s*:|Sectores\s*:|$)",
        r",\s*(19\d{2}|20\d{2})\s*,?\s*$",
        r",\s*(19\d{2}|20\d{2})\s*\.\s*(?:Dirigió|Dirigio|Areas\s*:|Sectores\s*:|$)",
        r"\b(19\d{2}|20\d{2})\.\s*(?:ed\s*:|pags?\.|Palabras\s*:|Areas\s*:|Sectores\s*:|$)",
    ]
    for pattern in priority_patterns:
        matches = re.findall(pattern, main_text, flags=re.IGNORECASE)
        if matches:
            return int(matches[-1])

    years = [int(year) for year in YEAR_RE.findall(main_text)]
    plausible_years = [year for year in years if 1950 <= year <= 2026]
    return plausible_years[-1] if plausible_years else (years[-1] if years else None)


def parse_blockquote(profile_id: str, section_title: str, blockquote, profile_author_name: str = "") -> dict[str, Any]:
    text = clean_text(blockquote.get_text(" ", strip=True))
    fields = parse_labeled_fields(text)
    source_fields = parse_html_labeled_fields(blockquote)
    preferred_fields = dict(fields)
    preferred_fields.update(source_fields)
    product_type = extract_product_type(section_title, blockquote)
    type_impactu = classify_target_category(section_title, product_type)
    book_related = type_impactu in {
        "Libro",
        "Capitulo de libro",
        "Publicaciones editoriales no especializadas",
    }
    directed_title = ""
    directed_affiliation = ""
    if norm_text(section_title) == "trabajos dirigidos/tutorias":
        directed_title, directed_affiliation = extract_directed_title_and_affiliation(
            blockquote, profile_author_name
        )
    title = directed_title or extract_title(
        text, preferred_fields, section_title, profile_author_name
    )
    doi = uniq_keep_order([normalize_doi(match) for match in DOI_RE.findall(text)])
    issn = uniq_keep_order([match.upper() for match in ISSN_RE.findall(text)])
    isbn_from_text = [normalize_isbn(match) for match in ISBN_RE.findall(text)]
    isbn_from_label = [normalize_isbn(str(fields["isbn_label"]))] if fields.get("isbn_label") else []
    is_patent = norm_text(section_title) == "patentes"
    pages, start_page, end_page = extract_pages(text, source_fields)
    publication_place = strip_value(source_fields.get("publication_place", ""))

    record = {
        "profile_id": profile_id,
        "product_type": product_type,
        "type_impactu": type_impactu,
        "source_section": ALLOWED_SECTION_ALIASES.get(
            norm_text(section_title), section_title
        ),
        "title": title,
        "year": extract_year(text, fields),
        "affiliation": directed_affiliation or extract_affiliation(text, fields),
        "country": extract_country(text),
        "publication_place": publication_place,
        "publisher": extract_publisher(text, source_fields) if book_related else "",
        "book_title": extract_book_title(text, source_fields, section_title),
        "edition": clean_bibliographic_value(source_fields.get("edition", "")),
        "volume": extract_volume(text),
        "pages": pages,
        "start_page": start_page,
        "end_page": end_page,
        "dissemination_medium": strip_value(
            source_fields.get("dissemination_medium", "")
        ),
        "language": clean_language_value(source_fields.get("language", "")),
        "keywords": split_terms(str(fields.get("keywords", ""))),
        "areas": split_areas(str(fields.get("areas", ""))),
        "advisor_role": normalize_advisor_role(str(fields.get("advisor_role", ""))),
        "oriented_people": split_oriented_people(
            str(fields.get("oriented_people", ""))
        ),
        "doi": doi,
        "issn": issn,
        "isbn": uniq_keep_order(isbn_from_text + isbn_from_label),
        "profile_name": profile_author_name,
        "status": source_fields.get("status", ""),
        "raw_text": text,
    }
    if is_patent:
        applicant = source_fields.get("patent_applicant") or extract_patent_applicant(
            text, fields
        )
        record["applicant"] = remove_applicant_when_author_match(applicant, profile_author_name)
        head = text.split(" - ", 1)[0].strip() if " - " in text else ""
        presentation_match = re.search(r"\b(19\d{2}|20\d{2})-\d{2}-\d{2}\b", text)
        record.update(
            {
                "registration_number": head
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/._-]{3,80}", head)
                else "",
                "presentation_date": source_fields.get("presentation_date", "")
                or (presentation_match.group(0) if presentation_match else ""),
                "request_route": source_fields.get("request_route", ""),
                "industrial_publication_gazette": source_fields.get(
                    "industrial_publication_gazette", ""
                ),
            }
        )
    else:
        record["authors"] = extract_authors(
            text,
            title,
            preferred_fields,
            product_type,
            section_title,
            profile_author_name,
        )
    return record


def extract_profile_author_name(soup: BeautifulSoup) -> str:
    anchor = soup.find("a", {"name": "datos_generales"})
    table = anchor.find_next("table") if anchor else None
    if not table:
        return ""

    cells = [clean_text(cell.get_text(" ", strip=True)) for cell in table.find_all(["td", "th"])]
    for index, cell in enumerate(cells[:-1]):
        if norm_text(cell) == "nombre":
            return strip_value(cells[index + 1])
    return ""


def extract_records_from_soup(
    profile_id: str,
    soup: BeautifulSoup,
    profile_author_name: str = "",
) -> list[dict[str, Any]]:
    if not profile_author_name:
        profile_author_name = extract_profile_author_name(soup)
    records: list[dict[str, Any]] = []
    seen = set()

    for blockquote in soup.find_all("blockquote"):
        text = clean_text(blockquote.get_text(" ", strip=True))
        if len(text) < 20:
            continue

        section_h3 = blockquote.find_previous("h3")
        section_title = clean_text(section_h3.get_text(" ", strip=True)) if section_h3 else ""
        canonical_section = ALLOWED_SECTION_ALIASES.get(norm_text(section_title))
        if not canonical_section:
            continue

        record = parse_blockquote(profile_id, section_title, blockquote, profile_author_name=profile_author_name)
        key = (profile_id, canonical_section, record["product_type"], record["title"], record["year"], text[:300])
        if key in seen:
            continue
        seen.add(key)
        records.append(record)

    return records


def extract_records_from_html(profile_id: str, html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html or "", "html.parser")
    return extract_records_from_soup(profile_id, soup)


def extract_profile_works_from_html(
    profile_id: str,
    html: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Parse a profile once and return its owner name and work records."""
    soup = BeautifulSoup(html or "", "html.parser")
    profile_author_name = extract_profile_author_name(soup)
    records = extract_records_from_soup(
        profile_id,
        soup,
        profile_author_name=profile_author_name,
    )
    return profile_author_name, records


def extract_between_labels(text: str, start_label: str, end_labels: list[str]) -> str:
    def label_pattern(label: str) -> str:
        if norm_text(label) in {"duracion", "resumen"}:
            return r"\b" + re.escape(label) + r"\b\s*:?\s*"
        return r"\b" + re.escape(label) + r"\s*:\s*"

    if not end_labels:
        match = re.search(label_pattern(start_label) + r"(.*)$", text, flags=re.IGNORECASE)
        return strip_value(match.group(1)) if match else ""
    end_pattern = "|".join(label_pattern(label) for label in end_labels)
    match = re.search(
        label_pattern(start_label) + r"(.*?)(?=\s*(?:" + end_pattern + r")|$)",
        text,
        flags=re.IGNORECASE,
    )
    return strip_value(match.group(1)) if match else ""


def extract_project_year(value: str) -> int | None:
    match = YEAR_RE.search(value or "")
    return int(match.group(1)) if match else None


def parse_project_blockquote(
    profile_id: str, blockquote, profile_author_name: str = ""
) -> dict[str, Any]:
    text = clean_text(blockquote.get_text(" ", strip=True))
    match = PROJECT_TYPES_RE.search(text)
    project_type = strip_value(match.group("project_type")) if match else ""
    title = strip_value(match.group("title")) if match else ""

    start = extract_between_labels(text, "Inicio", ["Fin proyectado", "Fin", "Duración", "Resumen"])
    projected_end = extract_between_labels(text, "Fin proyectado", ["Fin", "Duración", "Resumen"])
    end = extract_between_labels(text, "Fin", ["Duración", "Resumen"])
    duration = extract_between_labels(text, "Duración", ["Resumen"])
    summary = extract_between_labels(text, "Resumen", [])

    return {
        "profile_id": profile_id,
        "type_impactu": "Proyecto",
        "project_type": project_type,
        "title": title,
        "start_date": start,
        "projected_end_date": projected_end,
        "end_date": end,
        "duration": duration,
        "year": extract_project_year(start),
        "summary": summary,
        "profile_name": profile_author_name,
        "raw_text": text,
    }


def extract_projects_from_html(
    profile_id: str, soup: BeautifulSoup, profile_author_name: str = ""
) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    seen = set()
    for blockquote in soup.find_all("blockquote"):
        section_h3 = blockquote.find_previous("h3")
        section_title = clean_text(section_h3.get_text(" ", strip=True)) if section_h3 else ""
        if norm_text(section_title) != "proyectos":
            continue
        project = parse_project_blockquote(
            profile_id, blockquote, profile_author_name=profile_author_name
        )
        key = (project["project_type"], project["title"], project["start_date"], project["end_date"])
        if key in seen:
            continue
        seen.add(key)
        projects.append(project)
    return projects


def parse_event_dates_and_location(value: str) -> dict[str, Any]:
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", value or "")
    location = ""
    if re.search(r"\ben\b", value or "", flags=re.IGNORECASE):
        location = re.split(r"\ben\b", value, maxsplit=1, flags=re.IGNORECASE)[1]
    location = strip_value(location)

    city = ""
    venue = ""
    if " - " in location:
        city, venue = [strip_value(part) for part in location.split(" - ", 1)]
    else:
        city = location

    return {
        "start_date": dates[0] if dates else "",
        "end_date": dates[1] if len(dates) > 1 else "",
        "year": int(dates[0][:4]) if dates else None,
        "location": location,
        "city": city,
        "venue": venue,
    }


def parse_event_products(text: str) -> list[dict[str, str]]:
    section = re.split(r"\bProductos asociados\b", text, maxsplit=1, flags=re.IGNORECASE)
    if len(section) == 1:
        return []
    section_text = re.split(r"\b(?:Instituciones asociadas|Participantes)\b", section[1], maxsplit=1, flags=re.IGNORECASE)[0]
    products = []
    pattern = re.compile(
        r"Nombre del producto\s*:\s*(?P<name>.*?)\s+Tipo de producto\s*:\s*(?P<type>.*?)(?=\s+Nombre del producto\s*:|$)",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(section_text):
        products.append(
            {
                "name": strip_value(match.group("name")),
                "type": strip_value(match.group("type")),
            }
        )
    return products


def parse_event_institutions(text: str) -> list[dict[str, str]]:
    section = re.split(r"\bInstituciones asociadas\b", text, maxsplit=1, flags=re.IGNORECASE)
    if len(section) == 1:
        return []
    section_text = re.split(r"\bParticipantes\b", section[1], maxsplit=1, flags=re.IGNORECASE)[0]
    institutions = []
    pattern = re.compile(
        r"Nombre de la institución\s*:\s*(?P<name>.*?)\s+Tipo de vinculación\s*(?P<linkage>.*?)(?=\s+Nombre de la institución\s*:|$)",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(section_text):
        institutions.append(
            {
                "name": strip_value(match.group("name")),
                "linkage": strip_value(match.group("linkage")),
            }
        )
    return institutions


def parse_event_participants(text: str) -> list[dict[str, str]]:
    section = re.split(r"\bParticipantes\b", text, maxsplit=1, flags=re.IGNORECASE)
    if len(section) == 1:
        return []
    participants = []
    pattern = re.compile(
        r"Nombre\s*:\s*(?P<name>.*?)\s+Rol en el evento\s*:\s*(?P<role>.*?)(?=\s+Nombre\s*:|$)",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(section[1]):
        participants.append(
            {
                "name": strip_value(match.group("name")),
                "role": strip_value(match.group("role")),
            }
        )
    return participants


def parse_event_row(
    profile_id: str, row, profile_author_name: str = ""
) -> dict[str, Any]:
    text = clean_text(row.get_text(" ", strip=True))
    main = re.search(
        r"^(?P<order>\d+)\s+Nombre del evento\s*:\s*(?P<title>.*?)\s+Tipo de evento\s*:\s*(?P<event_type>.*?)\s+"
        r"Ámbito\s*:\s*(?P<scope>.*?)\s+Realizado el\s*:\s*(?P<date_location>.*?)(?=\s+(?:Productos asociados|Instituciones asociadas|Participantes)\b|$)",
        text,
        flags=re.IGNORECASE,
    )
    if not main:
        return {}

    event = {
        "profile_id": profile_id,
        "type_impactu": "Evento",
        "order": int(main.group("order")),
        "title": strip_value(main.group("title")),
        "event_type": strip_value(main.group("event_type")),
        "scope": strip_value(main.group("scope")),
        "profile_name": profile_author_name,
        "raw_text": text,
    }
    event.update(parse_event_dates_and_location(main.group("date_location")))
    event["associated_products"] = parse_event_products(text)
    event["associated_institutions"] = parse_event_institutions(text)
    event["participants"] = parse_event_participants(text)
    return event


def extract_events_from_html(
    profile_id: str, soup: BeautifulSoup, profile_author_name: str = ""
) -> list[dict[str, Any]]:
    section_h3 = None
    for h3 in soup.find_all("h3"):
        if norm_text(h3.get_text(" ", strip=True)) == "eventos cientificos":
            section_h3 = h3
            break
    if not section_h3:
        return []

    section_table = section_h3.find_parent("table")
    if not section_table:
        return []

    events = []
    seen = set()
    for row in section_table.find_all("tr", recursive=False):
        text = clean_text(row.get_text(" ", strip=True))
        if not re.match(r"^\d+\s+Nombre del evento\s*:", text, flags=re.IGNORECASE):
            continue
        event = parse_event_row(
            profile_id, row, profile_author_name=profile_author_name
        )
        if not event:
            continue
        key = (event["order"], event["title"], event["start_date"], event["location"])
        if key in seen:
            continue
        seen.add(key)
        events.append(event)
    return events


def normalize_related_works_document(profile_id: str, html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "html.parser")
    profile_author_name = extract_profile_author_name(soup)
    records = extract_records_from_soup(
        profile_id, soup, profile_author_name=profile_author_name
    )
    production = [record for record in records if norm_text(record.get("type_impactu", "")) != "patente"]
    patents = [record for record in records if norm_text(record.get("type_impactu", "")) == "patente"]
    events = extract_events_from_html(
        profile_id, soup, profile_author_name=profile_author_name
    )
    projects = extract_projects_from_html(
        profile_id, soup, profile_author_name=profile_author_name
    )
    return {
        "_id": profile_id,
        "id_persona_pr": profile_id,
        "url_persona": SCIENTI_CVLAC_URL + profile_id,
        "profile_name": profile_author_name,
        "production_counts": len(production),
        "production": production,
        "patents_counts": len(patents),
        "patents": patents,
        "events_counts": len(events),
        "events": events,
        "projects_counts": len(projects),
        "projects": projects,
    }
