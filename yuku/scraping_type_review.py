"""Streaming inventory and spreadsheet review of scraping classifications."""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from yuku.scraping_type_catalog import (
    ScrapingTypeCatalog, classification_context, get_scraping_catalog, label_key, observation_id,
)

CHANNELS = ("production", "patents", "projects", "events")
COLUMNS = ("id", "parent", "type", "status", "entity", "type_impactu", "justification",
           "reviewer", "frequency", "cvlac", "gruplac", "candidate_frequency", "evidence")


def consolidate(payload):
    """Refresh proposals and recover cataloged headings rejected by old parsers.

    Candidate frequencies are kept separate: they are not additional works.
    Uncataloged bold text stays in the rejected-candidate worksheet.
    """
    result = deepcopy(payload)
    catalog = get_scraping_catalog()
    entries = {}
    for observed in result["entries"]:
        key = (label_key(observed["parent"]), label_key(observed["type"]))
        observed.setdefault("variants", [{"parent": observed["parent"], "type": observed["type"],
                                          "frequency": observed.get("frequency", 0)}])
        if key not in entries:
            entries[key] = observed
            continue
        # Presentation aliases may have been reviewed since the inventory was
        # captured. Merge their counts instead of losing one of the rows.
        combined = entries[key]
        for field in ("frequency", "cvlac", "gruplac"):
            combined[field] = combined.get(field, 0) + observed.get(field, 0)
        for channel, count in observed.get("channels", {}).items():
            combined.setdefault("channels", {})[channel] = combined.get("channels", {}).get(channel, 0) + count
        combined["variants"].extend(observed["variants"])
        combined["examples"] = (combined.get("examples", []) + observed.get("examples", []))[:3]

    def refresh(entry):
        key = (label_key(entry["parent"]), label_key(entry["type"]))
        reviewed = catalog.entries.get(key)
        if reviewed and (reviewed["status"] in {"approved", "excluded"} or reviewed.get("reviewer")):
            proposal = catalog.resolve(entry["type"], entry["parent"])
            entry.update({k: reviewed.get(k, "") for k in ("justification", "reviewer")})
        else:
            proposal = catalog.inherit(entry["type"], entry["parent"])
            entry.update(justification="Exact label/hierarchy match; unique entity and ImpactU type"
                         if proposal["mapping"] else "Needs human review; no unique context-compatible mapping", reviewer="")
        mapping = proposal["mapping"] or {}
        entry.update(status=proposal["status"], entity=mapping.get("entity", ""),
                     type_impactu=mapping.get("type_impactu", ""), evidence=proposal["evidence"])

    for entry in entries.values():
        refresh(entry)
        entry["candidate_frequency"] = 0
    for rejected in result.get("rejected_candidates", []):
        value, section = rejected["candidate"], rejected["parent"]
        proposed = catalog.resolve(value, section)
        if not proposed["mapping"]:
            continue
        key = (label_key(section), label_key(value))
        if key not in entries:
            entries[key] = {"id": observation_id(section, value), "parent": section, "type": value,
                            "frequency": 0, "cvlac": 0, "gruplac": 0, "candidate_frequency": 0,
                            "channels": {}, "examples": []}
            refresh(entries[key])
        entry = entries[key]
        entry["candidate_frequency"] += rejected["frequency"]
        if len(entry["examples"]) < 3:
            entry["examples"].append(deepcopy(rejected["example"]))
    # Keep exact contracts ready for source variants not present in this snapshot.
    for key, seed in catalog.entries.items():
        if key not in entries and (seed["status"] == "approved" or seed.get("reviewer")):
            entry = deepcopy(seed)
            entry.update(id=observation_id(seed["parent"], seed["type"]), frequency=0,
                         cvlac=0, gruplac=0, candidate_frequency=0, channels={}, examples=[])
            entries[key] = entry
    result["entries"] = sorted(entries.values(), key=lambda e: (e["parent"], e["type"]))
    expected = sum(count for key, count in result.get("totals", {}).items() if key.endswith("_occurrences"))
    if sum(e.get("frequency", 0) for e in result["entries"]) != expected:
        raise ValueError("catalog consolidation changed the number of observed occurrences")
    result["shared_catalog"] = {"version": catalog.shared.version, "sha256": catalog.shared.source_sha256}
    ScrapingTypeCatalog(result)
    return result


def inventory(db, sources, progress=None):
    """Read only projected type fields; never load full HTML/documents in memory."""
    catalog = get_scraping_catalog()
    entries, rejected, totals = {}, {}, Counter()
    for kind, collection in sources.items():
        projection = {"url_persona": 1, "url_gruplac": 1, "parser.version": 1}
        for channel in CHANNELS:
            for field in ("product_type", "project_type", "event_type", "source_section", "title", "type_findings"):
                projection[channel + "." + field] = 1
        documents = 0
        with db[collection].find({}, projection).batch_size(100) as cursor:
            for doc in cursor:
                documents += 1
                for channel in CHANNELS:
                    for position, record in enumerate(doc.get(channel) or []):
                        if not isinstance(record, dict):
                            continue
                        value, section = classification_context(record, channel=channel)
                        key = (label_key(section), label_key(value))
                        sample = {"source_kind": kind, "collection": collection, "source_id": str(doc["_id"]),
                                  "channel": channel, "position": position, "title": record.get("title", ""),
                                  "url": doc.get("url_persona") or doc.get("url_gruplac") or "",
                                  "parser_version": (doc.get("parser") or {}).get("version"),
                                  "observed_parent": section, "observed_type": value}
                        if key not in entries:
                            result = catalog.resolve(value, section)
                            mapping = result["mapping"] or {}
                            entries[key] = {"id": observation_id(section, value), "parent": section, "type": value,
                                            "status": result["status"], "entity": mapping.get("entity", ""),
                                            "type_impactu": mapping.get("type_impactu", ""),
                                            "justification": "Exact label/hierarchy match; unique entity and ImpactU type"
                                                if mapping else "Needs human review; no unique context-compatible mapping",
                                            "reviewer": "", "evidence": result["evidence"],
                                            "frequency": 0, "cvlac": 0, "gruplac": 0, "channels": {}, "examples": []}
                        entry = entries[key]
                        entry["frequency"] += 1
                        entry[kind] += 1
                        entry["channels"][channel] = entry["channels"].get(channel, 0) + 1
                        variants = entry.setdefault("variants", [])
                        variant = next((v for v in variants if v["parent"] == section and v["type"] == value), None)
                        if variant is None:
                            variant = {"parent": section, "type": value, "frequency": 0}
                            variants.append(variant)
                        variant["frequency"] += 1
                        if len(entry["examples"]) < 3:
                            entry["examples"].append(sample)
                        totals[kind + "_occurrences"] += 1
                        for finding in record.get("type_findings") or []:
                            raw = str(finding.get("raw_type") or "")
                            rejected_key = (section, raw, finding.get("reason", ""))
                            item = rejected.setdefault(rejected_key, {"parent": section, "candidate": raw,
                                                       "reason": finding.get("reason", ""), "frequency": 0, "example": sample})
                            item["frequency"] += 1
                if progress and documents % 10000 == 0:
                    progress({"stage": "inventory", "source_kind": kind, "documents": documents,
                              "occurrences": totals[kind + "_occurrences"], "unique_types": len(entries)})
        totals[kind + "_documents"] = documents
    ordered = sorted(entries.values(), key=lambda e: (e["parent"], e["type"]))
    return {"schema_version": 1, "catalog_version": "1.0.0", "provider": "minciencias",
            "channels": ["cvlac", "gruplac"], "created_at": datetime.now(timezone.utc).isoformat(),
            "shared_catalog": {"version": catalog.shared.version, "sha256": catalog.shared.source_sha256},
            "sources": sources, "totals": dict(totals), "entries": ordered,
            "rejected_candidates": list(rejected.values())}


def _text(cell, value):
    """Keep source text literal, including values starting with '='."""
    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"


def excel_value(value):
    if isinstance(value, str):
        value = ILLEGAL_CHARACTERS_RE.sub(lambda m: "\\u%04x" % ord(m[0]), value)
        return value[:32767]
    return value


def append_row(sheet, values):
    sheet.append([excel_value(value) for value in values])


def export_audit_excel(payload, target):
    book = Workbook()
    intro = book.active
    intro.title = "LEEME"
    for row in [
        ("Catálogo", "Tipos de productos extraídos de CvLAC y GrupLAC; proveedor Minciencias"),
        ("Revisión", "Editar status, entity, type_impactu, justification y reviewer en Catalogo."),
        ("Aprobación", "Para asignaciones nuevas o corregidas usar approved y completar reviewer y justification."),
        ("Pendientes", "pending/ambiguous no asignan tipo ImpactU. excluded descarta evidencia que no es tipo."),
        ("Identidad", "No editar id, parent o type; identifican la clasificación observada."),
        ("Tipo nuevo", "Añadir una fila en Catalogo con id vacío, parent/type, estado approved, entidad/tipo canónicos, justificación y responsable."),
        ("Importación", "El Excel no cambia el pipeline: importar, validar y ejecutar una nueva sesión."),
        ("Frecuencia", "Ocurrencias en perfiles, no trabajos únicos; un trabajo puede repetirse."),
        ("candidate_frequency", "Veces que un encabezado catalogable fue rechazado por el parser anterior; no sumar a frequency."),
        ("Rechazados", "Candidatos descartados por el parser, incluidos títulos; no son tipos confirmados."),
        ("Fuentes internas", "scienti/minciencias en evidence indican dónde se encontró una equivalencia, no el origen del HTML."),
        ("Fecha UTC", payload.get("created_at", "")),
    ]:
        append_row(intro, row)
    sheet = book.create_sheet("Catalogo")
    sheet.append(COLUMNS)
    examples = book.create_sheet("Ejemplos")
    examples.append(("id", "source_kind", "collection", "source_id", "channel", "position", "title", "url", "parser_version"))
    variants = book.create_sheet("Variantes")
    variants.append(("id", "parent", "type", "frequency"))
    for entry in payload["entries"]:
        values = [json.dumps(entry.get(k, []), ensure_ascii=False) if k == "evidence" else entry.get(k, "") for k in COLUMNS]
        append_row(sheet, values)
        for sample in entry.get("examples", []):
            append_row(examples, [entry["id"]] + [sample.get(k, "") for k in ("source_kind", "collection", "source_id", "channel", "position", "title", "url", "parser_version")])
        for variant in entry.get("variants", []):
            append_row(variants, [entry["id"], variant["parent"], variant["type"], variant["frequency"]])
    pending = book.create_sheet("Pendientes")
    pending.append(("id", "parent", "type", "status", "frequency"))
    for e in payload["entries"]:
        if e["status"] in {"pending", "ambiguous"}:
            append_row(pending, [e[k] for k in ("id", "parent", "type", "status", "frequency")])
    reject = book.create_sheet("Candidatos_rechazados")
    reject.append(("parent", "candidate", "reason", "frequency", "example"))
    for entry in payload.get("rejected_candidates", []):
        append_row(reject, [entry[k] for k in ("parent", "candidate", "reason", "frequency")] + [json.dumps(entry["example"], ensure_ascii=False)])
    allowed = book.create_sheet("Tipos_ImpactU")
    allowed.append(("entity", "type_impactu"))
    for entity, native in sorted(get_scraping_catalog().allowed):
        allowed.append((entity, native))
    metadata = book.create_sheet("_metadata")
    metadata.append(("catalog_version", payload["catalog_version"]))
    metadata.append(("fingerprint", ScrapingTypeCatalog(payload).fingerprint))
    metadata.sheet_state = "hidden"
    for ws in book:
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for row in ws:
            for cell in row:
                _text(cell, cell.value)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="285E61")
        for column in ws.columns:
            ws.column_dimensions[column[0].column_letter].width = min(65, max(14, max(len(str(c.value or "")) for c in column[:30]) + 2))
    status = DataValidation(type="list", formula1='"inherited,approved,pending,ambiguous,excluded"')
    status.error = "Selecciona un estado válido"; status.showErrorMessage = True
    sheet.add_data_validation(status); status.add("D2:D1048576")
    entities = DataValidation(type="list", formula1='"works,projects,events,patents"')
    sheet.add_data_validation(entities); entities.add("E2:E1048576")
    target = Path(target); target.parent.mkdir(parents=True, exist_ok=True)
    book.save(target)


def import_excel(path, original, version):
    """Fail closed: keep all observed identities and require reviewed changes."""
    book = load_workbook(path, read_only=True, data_only=False)
    try:
        if book.properties.identifier and book.properties.identifier.startswith("yuku-unified-types-v1:"):
            from yuku.type_catalog_spreadsheet import import_excel as import_unified
            return import_unified(book, original, version, path)
        metadata = dict(book["_metadata"].values)
        if metadata.get("fingerprint") != ScrapingTypeCatalog(original).fingerprint:
            raise ValueError("spreadsheet belongs to a different catalog revision")
        rows = iter(book["Catalogo"].values)
        if tuple(next(rows)) != COLUMNS:
            raise ValueError("catalog spreadsheet columns changed")
        previous = {e["id"]: e for e in original["entries"]}
        seen, updated, new_keys = set(), [], set()
        original_keys = {(label_key(e["parent"]), label_key(e["type"])) for e in original["entries"]}
        for row in rows:
            item = dict(zip(COLUMNS, row))
            identifier = item["id"]
            if not any(value is not None for value in row):
                continue
            if not identifier:
                key = (label_key(item["parent"]), label_key(item["type"]))
                if not key[1] or key in original_keys or key in new_keys:
                    raise ValueError("new type must have a unique nonempty type/context")
                if item["status"] != "approved" or not item["reviewer"] or not item["justification"]:
                    raise ValueError("new types require approved status, reviewer and justification")
                entry = {k: str(item.get(k) or "").strip() for k in
                         ("parent", "type", "status", "entity", "type_impactu", "justification", "reviewer")}
                entry.update(id=observation_id(entry["parent"], entry["type"]), evidence=[], examples=[],
                             frequency=0, cvlac=0, gruplac=0, candidate_frequency=0, channels={})
                updated.append(entry); new_keys.add(key)
                continue
            if identifier not in previous or identifier in seen:
                raise ValueError("unknown or duplicated spreadsheet row")
            seen.add(identifier)
            base = previous[identifier]
            if any(str(item.get(k) or "") != str(excel_value(base.get(k)) or "") for k in ("parent", "type")):
                raise ValueError("do not change observed type/context identity")
            entry = deepcopy(base)
            for key in ("status", "entity", "type_impactu", "justification", "reviewer"):
                entry[key] = str(item.get(key) or "").strip()
            changed = any(entry[k] != str(base.get(k) or "") for k in ("status", "entity", "type_impactu"))
            if changed:
                if entry["status"] == "inherited" or not entry["reviewer"] or not entry["justification"]:
                    raise ValueError("changed assignments require approved/excluded status, reviewer and justification")
            updated.append(entry)
        if seen != set(previous):
            raise ValueError("spreadsheet is missing catalog rows")
        result = deepcopy(original)
        result.update(catalog_version=version, entries=updated, reviewed_at=datetime.now(timezone.utc).isoformat())
        if version == original["catalog_version"]:
            raise ValueError("reviewed catalog must have a new version")
        ScrapingTypeCatalog(result)
        return result
    finally:
        book.close()


def export_excel(payload, target):
    from yuku.type_catalog_spreadsheet import export_excel as export_unified
    return export_unified(payload, target)
