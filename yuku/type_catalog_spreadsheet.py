"""One-sheet review of the original vocabulary and Minciencias scraping types."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from datetime import datetime, timezone

from kahi_impactu_type_catalog import exact_key, get_impactu_catalog
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill

from yuku.scraping_type_catalog import ScrapingTypeCatalog, observation_id, path_labels

COLUMNS = ("Fuente", "Origen", "Parent", "Type", "Tipo ImpactU", "Entidad", "Tipo ImpactU Sugerido")


def _key(row):
    return tuple(exact_key(row[i]) for i in (0, 2, 3, 5))


def _native_parts(mapping):
    source, value = mapping["source"], mapping["type"]
    if source == "minciencias" and ": " in value:
        return tuple(value.split(": ", 1))
    if source == "scienti":
        labels = path_labels(value)
        return " - ".join(labels[:-1]), labels[-1] if labels else value
    return "", value


def _entity(entry):
    if entry.get("entity"):
        return entry["entity"]
    # Preserve the extraction channel for pending rows without inventing a type.
    if entry.get("parent") == "Eventos Científicos":
        return "events"
    for channel in ("projects", "events", "patents"):
        if entry.get("channels", {}).get(channel):
            return channel
    return "works"


def unified_rows(payload, shared=None):
    """Retain original mappings; inheritance alone never creates a shared origin."""
    shared = shared or get_impactu_catalog()
    rows, original_keys, scraping_keys = {}, set(), {}
    for mapping in shared.payload["mappings"] + shared.payload.get("unmapped", []):
        source = mapping["source"]
        parent, native = _native_parts(mapping)
        origin = {"minciencias": "socrata", "scienti": "dumps"}.get(source, source)
        row = [source, origin, parent, native, mapping.get("type_impactu", ""), mapping.get("entity", ""), ""]
        key = _key(row)
        if key in rows and rows[key][4] != row[4]:
            raise ValueError("original catalog contains conflicting assignments")
        rows[key] = row
        original_keys.add(key)
    for entry in payload["entries"]:
        row = ["minciencias", "scraping", entry["parent"], entry["type"], "", _entity(entry),
               entry.get("type_impactu", "") if entry["status"] in {"inherited", "approved"} else ""]
        key = _key(row)
        if key in scraping_keys:
            raise ValueError("duplicate scraping row in review sheet")
        scraping_keys[key] = entry["id"]
        if key in rows:
            rows[key][1] = "socrata; scraping"
            rows[key][6] = row[6]
        else:
            rows[key] = row
    ordered = sorted(rows.values(), key=lambda r: tuple(exact_key(r[i]) for i in (0, 2, 3, 5)))
    return ordered, original_keys, scraping_keys


def workbook_identity(payload):
    shared = get_impactu_catalog()
    return "yuku-unified-types-v1:" + sha256(json.dumps({
        "scraping": ScrapingTypeCatalog(payload).fingerprint,
        "shared": shared.payload,
    }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def export_excel(payload, target):
    from yuku.scraping_type_review import append_row, _text
    rows, _, _ = unified_rows(payload)
    book = Workbook()
    sheet = book.active
    sheet.title = "Catalogo"
    sheet.append(COLUMNS)
    for row in rows:
        append_row(sheet, row)
    for row in sheet:
        for cell in row:
            _text(cell, cell.value)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="285E61")
    for row in sheet.iter_rows(min_row=2):
        if "scraping" in str(row[1].value):
            row[6].fill = PatternFill("solid", fgColor="E7F1ED" if row[6].value else "FFF2CC")
    for column, width in zip("ABCDEFG", (19, 23, 61, 75, 48, 16, 48)):
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet["B1"].comment = Comment("Minciencias: socrata, scraping o dumps. En otros vocabularios se conserva su fuente. Compartir una traducción no implica compartir origen.", "Yuku")
    sheet["C1"].comment = Comment("Contexto desglosado de las rutas originales. Las rutas originales completas y los controles se conservan en JSON.", "Yuku")
    sheet["E1"].comment = Comment("Asignación del catálogo original; vacía para los tipos nuevos del scraping.", "Yuku")
    sheet["G1"].comment = Comment("Propuesta para revisión. Las celdas amarillas del scraping no tienen asignación clara. Editar aquí; no sustituye automáticamente al catálogo compartido.", "Yuku")
    # Store the revision without adding hidden sheets or technical columns.
    book.properties.identifier = workbook_identity(payload)
    book.properties.title = "Catálogo original y tipos del scraping de Minciencias"
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    book.save(target)


def import_excel(book, original, version, filename):
    """Record suggestions; never write to the shared catalog or MongoDB."""
    from yuku.scraping_type_review import excel_value
    if book.sheetnames != ["Catalogo"] or book.properties.identifier != workbook_identity(original):
        raise ValueError("spreadsheet belongs to a different catalog revision")
    if book["Catalogo"].max_column != len(COLUMNS):
        raise ValueError("catalog spreadsheet must contain exactly seven columns")
    if not version or version == original["catalog_version"]:
        raise ValueError("reviewed catalog must have a new version")
    template, original_keys, scraping_keys = unified_rows(original)
    # Compare the literal values exported to Excel, including escaped controls.
    expected = {_key([excel_value(v) for v in row]): row for row in template}
    source_keys = {_key([excel_value(v) for v in row]): _key(row) for row in template}
    by_id = {e["id"]: deepcopy(e) for e in original["entries"]}
    seen, additions, shared_suggestions = set(), [], []
    iterator = iter(book["Catalogo"].values)
    if tuple(next(iterator)) != COLUMNS:
        raise ValueError("catalog spreadsheet columns changed")
    allowed = ScrapingTypeCatalog(original).allowed
    for values in iterator:
        if not any(v is not None for v in values):
            continue
        row = [str(v or "").strip() for v in values]
        key = _key(row)
        if key in seen:
            raise ValueError("duplicate spreadsheet type/context/entity")
        seen.add(key)
        base = expected.get(key)
        if base is not None:
            if any(row[i] != str(excel_value(base[i]) or "") for i in range(6)):
                raise ValueError("preserve original columns; edit Tipo ImpactU Sugerido")
            native_key = source_keys[key]
            entry_id = scraping_keys.get(native_key)
            if row[6] and (row[5], row[6]) not in allowed:
                raise ValueError("suggested type must be a canonical entity/type pair")
            if entry_id is not None:
                entry = by_id[entry_id]
                if row[6] != str(base[6] or ""):
                    entry.update(status="approved" if row[6] else "pending", type_impactu=row[6],
                                 entity=row[5], reviewer="Excel: " + Path(filename).name,
                                 justification="Explicit suggestion in reviewed unified workbook")
            if native_key in original_keys and row[6] and row[6] != str(base[6] or ""):
                shared_suggestions.append(dict(zip(COLUMNS, row)))
        else:
            # A new reviewed label must explicitly belong to the scraping.
            if row[0] != "minciencias" or row[1] != "scraping" or not row[3] or row[4]:
                raise ValueError("new rows must identify a minciencias scraping type with no original assignment")
            if row[5] not in get_impactu_catalog().payload["entities"]:
                raise ValueError("new rows must identify a valid entity")
            if row[6] and (row[5], row[6]) not in allowed:
                raise ValueError("suggested type must be a canonical entity/type pair")
            additions.append({"id": observation_id(row[2], row[3]), "parent": row[2], "type": row[3],
                              "status": "approved" if row[6] else "pending", "entity": row[5], "type_impactu": row[6],
                              "justification": "Explicit new label in reviewed unified workbook", "reviewer": "Excel: " + Path(filename).name,
                              "evidence": [], "examples": [], "frequency": 0, "cvlac": 0, "gruplac": 0, "candidate_frequency": 0, "channels": {}})
    if not set(expected).issubset(seen):
        raise ValueError("spreadsheet is missing catalog rows; preserve their identities")
    result = deepcopy(original)
    result.update(catalog_version=version, entries=list(by_id.values()) + additions,
                  reviewed_at=datetime.now(timezone.utc).isoformat(), shared_catalog_suggestions=shared_suggestions)
    ScrapingTypeCatalog(result)
    return result
