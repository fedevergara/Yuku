from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from yuku.cvlac_related_works import (
    DOI_RE,
    ISBN_RE,
    ISSN_RE,
    YEAR_RE,
    clean_bibliographic_value,
    clean_language_value,
    clean_pages_value,
    clean_publisher_value,
    clean_text,
    normalize_doi,
    normalize_isbn,
    split_people,
    strip_value,
    uniq_keep_order,
)


SCIENTI_GRUPLAC_URL = (
    "https://scienti.minciencias.gov.co/gruplac/jsp/visualiza/"
    "visualizagr.jsp?nro="
)
COD_RH_RE = re.compile(r"[?&]cod_rh=(\d+)(?:&|$)", re.IGNORECASE)
NUMBERED_PRODUCT_RE = re.compile(r"^\s*\d+\s*\.\-\s*")
PAGES_RE = re.compile(
    r"\bp(?:(?:[aá]gs?)|(?:[aá]ginas))\.?\s*:\s*(.*?)"
    r"(?=\bAutores\s*:|\s*,\s*(?:Ed\.|Editorial|Idiomas?|Vol\.)|[,;\n]|$)",
    re.IGNORECASE,
)
VOLUME_RE = re.compile(
    r"\b(?:volumen|vol\.?)(?=\s|:|$)\s*:?\s*(.*?)"
    r"(?=\s*,?\s*(?:fasc(?:[ií]culo)?\.?|p(?:[aá]gs?|[aá]ginas)\.?|Ed\.|"
    r"Editorial|ISBN|ISSN|DOI|Autores?)\s*:|[,;\n]|$)",
    re.IGNORECASE,
)
PUBLISHER_RE = re.compile(
    r"(?:\bEditorial\s*:|\bEd\.\s*)(.*?)"
    r"(?=\bAutores\s*:|\s*,\s*(?:Idiomas?|P[aá]ginas|Vol\.|ISBN|ISSN|DOI)\s*:|$)",
    re.IGNORECASE,
)
EDITION_RE = re.compile(
    r"\b(?:N[uú]mero de edici[oó]n|Edici[oó]n)\s*:\s*(.*?)"
    r"(?=\s+Autores\s*:|\s*,?\s*(?:Idiomas?|P[aá]ginas|Vol\.|ISBN|ISSN|DOI|"
    r"Editorial|Serie|Autor del documento original)\s*:|$)",
    re.IGNORECASE,
)
LANGUAGE_RE = re.compile(
    r"\bIdiomas?\s*:\s*(.*?)(?=\s+Autores\s*:|\s*,\s*(?:P[aá]ginas|Vol\.|ISBN|ISSN|DOI|Editorial)\s*:|$)",
    re.IGNORECASE,
)
TIMELINE_PRODUCT_SECTIONS = {
    "Estrategias Pedagógicas para el fomento a la CTI",
    "Estrategias de Comunicación del Conocimiento",
    "Participación Ciudadana en Proyectos de CTI",
}
TIMELINE_ROW_RE = re.compile(
    r"^(?P<title>.+?)\s*:\s*desde\s+(?P<start>.+?)\s+hasta"
    r"(?:\s+(?P<end>.*?))?$",
    re.IGNORECASE,
)
DESCRIPTION_RE = re.compile(r"\s+Descripci[oó]n\s*:\s*", re.IGNORECASE)
THESIS_SECTION = "Trabajos dirigidos/turorías"
PROJECT_SECTION = "Proyectos"
EVENT_SECTION = "Eventos Científicos"
INTELLECTUAL_PROPERTY_SECTIONS = {
    "Diseños industriales",
    "Signos distintivos",
    "Nuevas variedades vegetal",
    "Nuevas variedades animal",
    "Esquemas de trazados de circuito integrado",
}
GRUPLAC_PROJECT_TYPES = (
    "Investigación y desarrollo",
    "Investigación, desarrollo e innovación",
    "Extensión y responsabilidad social CTI",
    "Investigación + Creación",
)


def classify_gruplac_product(product_type: str, section: str) -> str:
    value = clean_text(f"{product_type} {section}").lower()
    if "capítulo" in value or "capitulo" in value:
        return "Capitulo de libro"
    if "libro" in value:
        return "Libro"
    if "artículo" in value or "articulo" in value or "revista" in value:
        return "Articulo de revista"
    if "tesis de doctorado" in value:
        return "Tesis de posgrado"
    if "maestría" in value or "maestria" in value:
        return "Tesis de posgrado"
    if "grado de pregrado" in value:
        return "Tesis de pregrado"
    if "documento de trabajo" in value or "working paper" in value:
        return "Documento de trabajo"
    return product_type or section


def table_heading(table) -> str:
    header = table.find("td", class_="celdaEncabezado")
    return clean_text(header.get_text(" ", strip=True)) if header else ""


def exact_labeled_value(
    text: str, label: str, following_labels: tuple[str, ...] = ()
) -> str:
    """Extract a GrupLAC field only between explicitly named labels."""
    end = (
        "|".join(re.escape(value) for value in following_labels)
        if following_labels
        else r"\Z"
    )
    pattern = (
        re.escape(label)
        + r"\s*:\s*(.*?)(?=\s+(?:"
        + end
        + r")(?:\s*:|\s|$)|$)"
    )
    match = re.search(pattern, text or "", flags=re.IGNORECASE)
    return strip_value(match.group(1)) if match else ""


def enrich_thesis_record(record: dict[str, Any]) -> None:
    text = str(record.get("raw_text") or "")
    orientation_type = exact_labeled_value(
        text, "Tipo de orientación", ("Nombre del estudiante",)
    )
    if orientation_type or "orientation_type" not in record:
        record["orientation_type"] = orientation_type
    student = exact_labeled_value(
        text, "Nombre del estudiante", ("Programa académico",)
    )
    if student or "students" not in record:
        record["students"] = [student] if student else []
    academic_program = exact_labeled_value(
        text, "Programa académico", ("Número de páginas",)
    )
    if academic_program or "academic_program" not in record:
        record["academic_program"] = academic_program
    pages = exact_labeled_value(
        text, "Número de páginas", ("Valoración",)
    )
    if pages or "pages" not in record:
        record["pages"] = pages
    assessment = exact_labeled_value(
        text, "Valoración", ("Institución",)
    )
    if assessment or "assessment" not in record:
        record["assessment"] = assessment
    institution = exact_labeled_value(
        text, "Institución", ("Tutor(es)/Cotutor(es)",)
    )
    if institution or "institution" not in record:
        record["institution"] = institution
    tutors = exact_labeled_value(text, "Tutor(es)/Cotutor(es)")
    if tutors or "advisors" not in record:
        record["advisors"] = split_people(tutors) if tutors else []
    period = re.search(
        r"\bDesde\s+(?P<start>.*?)\s+hasta\s+(?P<end>.*?)"
        r"(?=\s*,?\s*Tipo de orientación\s*:)",
        text,
        flags=re.IGNORECASE,
    )
    if period or "start_date" not in record:
        record["start_date"] = strip_value(period.group("start")) if period else ""
    if period or "end_date" not in record:
        record["end_date"] = strip_value(period.group("end")) if period else ""


def enrich_project_record(record: dict[str, Any]) -> None:
    title = str(record.get("title") or "")
    raw_text = str(record.get("raw_text") or "")
    existing_project_type = str(record.get("project_type") or "")
    raw_product_type = str(record.get("product_type") or "")
    project_type = next(
        (
            candidate
            for candidate in GRUPLAC_PROJECT_TYPES
            if existing_project_type.casefold() == candidate.casefold()
            or raw_product_type.casefold() == candidate.casefold()
        ),
        "",
    )
    if not project_type:
        for candidate in GRUPLAC_PROJECT_TYPES:
            prefix = candidate + " : "
            if title.casefold().startswith(prefix.casefold()):
                project_type = candidate
                title = strip_value(title[len(prefix):])
                break
    if not project_type:
        for candidate in GRUPLAC_PROJECT_TYPES:
            if re.match(
                r"^\s*\d+\s*\.\-\s*" + re.escape(candidate) + r"\s*:",
                raw_text,
                flags=re.IGNORECASE,
            ):
                project_type = candidate
                break
    period_pattern = (
        r"\s+(?P<start>\d{4}/\d{1,2})\s+-\s+"
        r"(?P<end>Actual|\d{4}/\d{1,2})\s*$"
    )
    title_period = re.search(period_pattern, title, flags=re.IGNORECASE)
    period = re.search(period_pattern, raw_text or title, flags=re.IGNORECASE)
    if title_period:
        title = strip_value(title[: title_period.start()])
    record["project_type"] = project_type
    record["title"] = title
    record["start_date"] = strip_value(period.group("start")) if period else ""
    record["end_date"] = strip_value(period.group("end")) if period else ""
    if record["start_date"]:
        record["year"] = int(record["start_date"][:4])


def enrich_event_record(record: dict[str, Any]) -> None:
    text = str(record.get("raw_text") or "")
    dates = re.search(
        r"\bdesde\s+(?P<start>\d{4}-\d{2}-\d{2})\s+-\s+hasta"
        r"(?:\s+(?P<end>\d{4}-\d{2}-\d{2}))?",
        text,
        flags=re.IGNORECASE,
    )
    record["start_date"] = dates.group("start") if dates else ""
    record["end_date"] = (dates.group("end") or "") if dates else ""
    record["scope"] = exact_labeled_value(
        text, "Ámbito", ("Tipos de participación", "Instituciones asociadas")
    )
    participation = exact_labeled_value(
        text, "Tipos de participación", ("Instituciones asociadas",)
    )
    record["participation_types"] = [
        value
        for value in (strip_value(item) for item in re.split(r"\s*,\s*|\s*;\s*", participation))
        if value
    ]
    institutions = []
    for match in re.finditer(
        r"Nombre de la institución\s*:\s*(?P<name>.*?)\s+"
        r"Tipo de vinculación\s*:?(?P<linkage>.*?)"
        r"(?=\s+Nombre de la institución\s*:|$)",
        text,
        flags=re.IGNORECASE,
    ):
        institutions.append(
            {
                "name": strip_value(match.group("name")),
                "linkage": strip_value(match.group("linkage")),
            }
        )
    record["associated_institutions"] = institutions


def enrich_intellectual_property_record(record: dict[str, Any]) -> None:
    text = str(record.get("raw_text") or "")
    labels = (
        ("registration_number", "Número del registro", ("Nombre del titular",)),
        ("holder", "Nombre del titular", ()),
        ("availability", "Disponibilidad", ("Institución financiadora", "Autores")),
        ("funding_institution", "Institución financiadora", ("Autores",)),
        ("cycle_type", "Tipo de ciclo", ("Sitio web", "Institución financiadora")),
        ("website", "Sitio web", ("Institución financiadora", "Autores")),
        ("administrative_act", "Acto administrativo del ICA", ("Institución financiadora",)),
    )
    for field, label, following in labels:
        record[field] = exact_labeled_value(text, label, following)


def enrich_exact_section(record: dict[str, Any], section: str) -> None:
    if section == THESIS_SECTION:
        enrich_thesis_record(record)
    elif section == PROJECT_SECTION:
        enrich_project_record(record)
    elif section == EVENT_SECTION:
        enrich_event_record(record)
    elif section in INTELLECTUAL_PROPERTY_SECTIONS:
        enrich_intellectual_property_record(record)


def first_line(cell) -> str:
    values: list[str] = []
    for child in cell.children:
        if getattr(child, "name", None) == "br":
            break
        if hasattr(child, "get_text"):
            values.append(child.get_text(" ", strip=True))
        else:
            values.append(str(child))
    return clean_text(" ".join(values))


def metadata_after_title(cell) -> str:
    parts = re.split(r"<br\s*/?>", str(cell), maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return ""
    return clean_text(BeautifulSoup(parts[1], "lxml").get_text(" ", strip=True))


def extract_gruplac_book_title(metadata: str, product_type: str, section: str) -> str:
    """Extract a chapter's containing book from the fixed country/year layout."""
    if classify_gruplac_product(product_type, section) != "Capitulo de libro":
        return ""
    match = re.search(
        r"^[^,]+,\s*(?:19|20)\d{2}\s*,\s*(.*?)"
        r"(?=\s*,\s*(?:ISBN|ISSN|DOI|Vol\.?|p[aá]gs?\.?|Ed\.|Editorial)\s*:?)",
        metadata or "",
        flags=re.IGNORECASE,
    )
    return strip_value(match.group(1)) if match else ""


def split_page_range(value: str) -> tuple[str, str]:
    match = re.fullmatch(r"\s*([0-9]+)(?:\s*-\s*([0-9]+))?\s*", value or "")
    if not match:
        return "", ""
    start = "" if match.group(1) == "0" else match.group(1)
    end = "" if (match.group(2) or "") == "0" else (match.group(2) or "")
    return start, end


def parse_timeline_product_row(cell, text: str, section: str) -> dict[str, Any] | None:
    """Parse timeline rows even when malformed HTML swallows text into ``strong``."""
    visible = NUMBERED_PRODUCT_RE.sub("", text, count=1)
    parts = DESCRIPTION_RE.split(visible, maxsplit=1)
    heading = strip_value(parts[0])
    period = TIMELINE_ROW_RE.fullmatch(heading)
    if not period:
        return None
    title = clean_text(period.group("title"))
    start_date = strip_value(period.group("start"))
    end_date = strip_value(period.group("end") or "")
    start_years = [int(value) for value in YEAR_RE.findall(start_date)]
    end_years = [int(value) for value in YEAR_RE.findall(end_date)]
    description = clean_text(parts[1]) if len(parts) == 2 else ""
    author_match = re.search(
        r"\bAutores\s*:\s*(.+)$", description, re.IGNORECASE
    )
    authors = split_people(author_match.group(1)) if author_match else []
    if author_match:
        description = description[: author_match.start()].rstrip(" ,;")
    cells = cell.find_parent("tr").find_all("td", recursive=False)
    img = cells[0].find("img", src=True) if cells else None
    validation_marker = str(img.get("src", "")) if img else ""
    return {
        "product_type": section,
        "type_impactu": section,
        "title": title,
        "year": (end_years or start_years or [None])[0],
        "start_date": start_date,
        "end_date": end_date,
        "description": description,
        "country": "",
        "authors": authors,
        "doi": uniq_keep_order(
            [normalize_doi(value) for value in DOI_RE.findall(text)]
        ),
        "issn": uniq_keep_order(
            [value.upper() for value in ISSN_RE.findall(text)]
        ),
        "isbn": uniq_keep_order(
            [normalize_isbn(value) for value in ISBN_RE.findall(text)]
        ),
        "publisher": "",
        "book_title": "",
        "edition": "",
        "pages": "",
        "start_page": "",
        "end_page": "",
        "volume": "",
        "publication_place": "",
        "dissemination_medium": "",
        "language": "",
        "source_section": section,
        "validated": validation_marker.endswith("chulo_1.jpg"),
        "raw_text": text,
    }


def parse_product_row(row, section: str) -> dict[str, Any] | None:
    cells = row.find_all("td", recursive=False)
    if len(cells) < 2:
        return None
    cell = cells[-1]
    text = clean_text(cell.get_text(" ", strip=True))
    if not NUMBERED_PRODUCT_RE.match(text):
        return None
    if section in TIMELINE_PRODUCT_SECTIONS:
        return parse_timeline_product_row(cell, text, section)
    strong = cell.find("strong")
    product_type = clean_text(strong.get_text(" ", strip=True)) if strong else ""
    line = first_line(cell)
    line = NUMBERED_PRODUCT_RE.sub("", line, count=1)
    if product_type and line.lower().startswith(product_type.lower()):
        line = line[len(product_type):].lstrip(" :")
    title = strip_value(line)
    if not title:
        return None

    metadata = metadata_after_title(cell)
    author_match = re.search(r"\bAutores\s*:\s*(.+)$", metadata, re.IGNORECASE)
    authors = split_people(author_match.group(1)) if author_match else []
    metadata_without_authors = re.split(
        r"\bAutores\s*:", metadata, maxsplit=1, flags=re.IGNORECASE
    )[0]
    years = [int(value) for value in YEAR_RE.findall(metadata_without_authors)]
    plausible_years = [value for value in years if 1950 <= value <= 2026]
    country = strip_value(metadata_without_authors.split(",", 1)[0])
    publisher_match = PUBLISHER_RE.search(metadata)
    pages_match = PAGES_RE.search(metadata)
    volume_match = VOLUME_RE.search(metadata)
    edition_match = EDITION_RE.search(metadata)
    language_match = LANGUAGE_RE.search(metadata)
    img = cells[0].find("img", src=True)
    validation_marker = str(img.get("src", "")) if img else ""

    isbn = uniq_keep_order(
        [normalize_isbn(value) for value in ISBN_RE.findall(metadata)]
    )
    type_impactu = classify_gruplac_product(product_type, section)
    book_related = (
        type_impactu in {"Libro", "Capitulo de libro"}
        or "editorial" in clean_text(f"{product_type} {section}").lower()
    )
    pages = clean_pages_value(pages_match.group(1)) if pages_match else ""
    start_page, end_page = split_page_range(pages)
    publication_place = "" if YEAR_RE.fullmatch(country) else country
    publisher = (
        clean_publisher_value(publisher_match.group(1))
        if book_related and publisher_match
        else ""
    )
    record: dict[str, Any] = {
        "product_type": product_type or section,
        "type_impactu": type_impactu,
        "title": title,
        "year": plausible_years[0] if plausible_years else None,
        "country": country,
        "authors": authors,
        "doi": uniq_keep_order(
            [normalize_doi(value) for value in DOI_RE.findall(metadata)]
        ),
        "issn": uniq_keep_order([value.upper() for value in ISSN_RE.findall(metadata)]),
        "isbn": isbn,
        "publisher": publisher,
        "book_title": extract_gruplac_book_title(metadata, product_type, section),
        "edition": clean_bibliographic_value(edition_match.group(1)) if edition_match else "",
        "pages": pages,
        "start_page": start_page,
        "end_page": end_page,
        "volume": clean_bibliographic_value(volume_match.group(1)) if volume_match else "",
        "publication_place": publication_place,
        "dissemination_medium": "",
        "language": clean_language_value(language_match.group(1)) if language_match else "",
        "source_section": section,
        "validated": validation_marker.endswith("chulo_1.jpg"),
        "raw_text": text,
    }
    enrich_exact_section(record, section)
    return record


def parse_basic_data(table) -> dict[str, str]:
    result: dict[str, str] = {}
    if table is None:
        return result
    for row in table.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) != 2 or "celdasTitulo" not in (cells[0].get("class") or []):
            continue
        key = clean_text(cells[0].get_text(" ", strip=True))
        value = clean_text(cells[1].get_text(" ", strip=True))
        if key:
            result[key] = value
    return result


def parse_group_institutions(table) -> list[dict[str, Any]]:
    """Extract named GrupLAC institutions without attempting identity resolution."""
    institutions: list[dict[str, Any]] = []
    if table is None:
        return institutions
    for row in table.find_all("tr", recursive=False):
        cells = row.find_all("td", recursive=False)
        if len(cells) != 1:
            continue
        value = clean_text(cells[0].get_text(" ", strip=True))
        value = NUMBERED_PRODUCT_RE.sub("", value)
        endorsed = bool(re.search(r"\(\s*Avalado\s*\)\s*$", value, re.I))
        value = re.sub(r"\s*-?\s*\(\s*Avalado\s*\)\s*$", "", value, flags=re.I)
        if value and value != "Instituciones":
            institutions.append({"name": value, "endorsed": endorsed})
    return institutions


def parse_group_strategic_plan(table) -> dict[str, str]:
    """Extract the five explicitly labelled public strategic-plan sections."""
    if table is None:
        return {}
    text = clean_text(table.get_text(" ", strip=True))
    labels = (
        ("TXT_PLAN_TRABAJO", "Plan de trabajo"),
        ("TXT_ESTADO_ARTE", "Estado del arte"),
        ("TXT_OBJETIVOS", "Objetivos"),
        ("TXT_RETOS", "Retos"),
        ("TXT_VISION", "Visión"),
    )
    result: dict[str, str] = {}
    for position, (key, label) in enumerate(labels):
        following = labels[position + 1:]
        end = "|".join(re.escape(item[1]) for item in following) or r"\Z"
        match = re.search(
            re.escape(label) + r"\s*:\s*(.*?)(?=\s+(?:" + end + r")\s*:|$)",
            text,
            flags=re.I,
        )
        value = clean_text(match.group(1)) if match else ""
        if value:
            result[key] = value
    return result


def parse_group_research_lines(table) -> list[str]:
    """Extract declared research lines as ordered source values."""
    values: list[str] = []
    if table is None:
        return values
    for row in table.find_all("tr", recursive=False):
        cells = row.find_all("td", recursive=False)
        if len(cells) != 1:
            continue
        value = NUMBERED_PRODUCT_RE.sub(
            "", clean_text(cells[0].get_text(" ", strip=True))
        )
        if value and value != "Líneas de investigación declaradas por el grupo":
            values.append(value)
    return uniq_keep_order(values)


def parse_members(table) -> list[dict[str, str]]:
    members: list[dict[str, str]] = []
    if table is None:
        return members
    for row in table.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) != 4:
            continue
        anchor = cells[0].find("a", href=True)
        match = COD_RH_RE.search(anchor.get("href", "")) if anchor else None
        if not match:
            continue
        members.append(
            {
                "cod_rh": match.group(1).zfill(10),
                "full_name": clean_text(anchor.get_text(" ", strip=True)),
                "role": clean_text(cells[1].get_text(" ", strip=True)),
                "hours": clean_text(cells[2].get_text(" ", strip=True)),
                "period": clean_text(cells[3].get_text(" ", strip=True)),
            }
        )
    return members


def normalize_gruplac_document(
    group_code: str,
    html: str,
    *,
    nro: str = "",
    url: str = "",
) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "lxml")
    name_tag = soup.find("span", class_="celdaEncabezado")
    group_name = clean_text(name_tag.get_text(" ", strip=True)) if name_tag else ""
    tables = soup.find_all("table")
    by_heading = {table_heading(table): table for table in tables if table_heading(table)}
    basic = parse_basic_data(by_heading.get("Datos básicos"))
    institutions = parse_group_institutions(by_heading.get("Instituciones"))
    strategic_plan = parse_group_strategic_plan(by_heading.get("Plan Estratégico"))
    research_lines = parse_group_research_lines(
        by_heading.get("Líneas de investigación declaradas por el grupo")
    )
    members = parse_members(by_heading.get("Integrantes del grupo"))
    production: list[dict[str, Any]] = []
    for table in tables:
        section = table_heading(table)
        if not section:
            continue
        for row in table.find_all("tr", recursive=False):
            record = parse_product_row(row, section)
            if record is not None:
                record["group_code"] = group_code.upper()
                record["group_name"] = group_name
                production.append(record)
    return {
        "_id": group_code.upper(),
        "group_code": group_code.upper(),
        "group_name": group_name,
        "nro": str(nro),
        "url_gruplac": url or (f"{SCIENTI_GRUPLAC_URL}{nro}" if nro else ""),
        "basic": basic,
        "institutions": institutions,
        "strategic_plan": strategic_plan,
        "research_lines": research_lines,
        "members_count": len(members),
        "members": members,
        "production_count": len(production),
        "production": production,
    }
