"""Reviewable CvLAC/GrupLAC vocabulary, independent of the dumps' vocabulary.

The shared catalog owns published assignments; local observations preserve
their evidence. Matching a dumps' label does not make a scraped observation
originate in that database. No fuzzy matching or fallback from an unknown
subtype to its section is allowed.
"""

from collections import defaultdict
from functools import lru_cache
from hashlib import sha256
from importlib.resources import files
import json
import re

from kahi_impactu_type_catalog import exact_key, get_impactu_catalog
from yuku.scienti_events import normalize_event_type

CATALOG_RESOURCE = "data/minciencias_scraping_types_v1.json"
POLICY_VERSION = "minciencias-scraping-types-v2"
CODE = re.compile(r"(?:^|\s)[A-Z0-9]+:\s+")

# Presentation variants only; these do not establish new semantic mappings.
ALIASES = {
    exact_key(value): target for target, values in {
        "Capítulo de libro": ("Capitulo de libro", "Capitulos de libro", "Capítulos de libro",
                              "Capítulos de libro publicados", "Capitulos de libro publicados"),
        "Artículo": ("Articulos", "Artículos", "Artículos publicados", "Articulos publicados"),
        "Artículo de revista": ("Articulo de revista",),
        "Proyecto": ("Proyectos",), "Patente": ("Patentes",),
        "Libro": ("Libros", "Libros publicados", "Otros Libros publicados"),
        "Otro artículo publicado": ("Otros artículos publicados", "Otros articulos publicados"),
        "Libros de formación": ("Libro de Formacion", "Libro de formación"),
        "Libros de divulgación y/o Compilación de divulgación": (
            "Libros de divulgacion y/o Compilacion de divulgacion",),
        "Libro pedagógico y/o de divulgación": ("Libro pedagógico",),
        "Trabajos dirigidos/Tutorías": ("Trabajos dirigidos/turorías", "Trabajos dirigidos/tutorias"),
        "Editoración o revisión": ("Ediciones",),
        "Diseño Industrial": ("Diseños industriales",),
        "Variedad vegetal": ("Nuevas variedades vegetal",),
        "Variedad animal": ("Nuevas variedades animal",),
        "Esquema de circuito integrado": ("Esquemas de trazados de circuito integrado",),
        "Estrategia de comunicación del conocimiento": ("Estrategias de Comunicación del Conocimiento",),
        "Publicaciones editoriales no especializadas": (
            "Textos en publicaciones no científicas", "Textos en publicaciones no cientificas"),
        "Red de conocimiento especializado": ("Redes de Conocimiento Especializado",),
        "Asesoría al programa Ondas": ("Asesorías al Programa Ondas",),
    }.items() for value in values
}


def label_key(value):
    value = str(value or "").strip().rstrip(" :")
    return exact_key(ALIASES.get(exact_key(value), value))


def path_labels(value):
    return tuple(part.strip() for part in CODE.split(str(value)) if part.strip())


# HTML section names that describe the same explicitly named catalog parent.
# This changes presentation context, never the subtype's meaning.
SECTION_ALIASES = {
    label_key("Generación de Contenido Impreso"): "Impresa",
    label_key("Generación de Contenido Virtual"): "Contenido Virtual",
    label_key("Generación de Contenido de Audio"): "Generación de contenido de audio",
    label_key("Generación de Contenido Multimedia"): "Multimedia",
    label_key("Eventos Científicos"): "Organización de eventos",
    label_key("Capitulos de libro"): "Libro",
    label_key("Documentos de trabajo"): "Documento de trabajo (Working Paper)",
    label_key("Curso de Corta Duración Dictados"): "Cursos de corta duración dictados",
    label_key("Otros productos tecnológicos"): "Productos tecnológicos",
    label_key("Regulaciones y Normas"): "Regulación, norma, reglamento o legislación",
    label_key("Reglamentos técnicos"): "Reglamento Técnico",
    label_key("Guias de práctica clínica"): "Guía de práctica clínica",
    label_key("Plantas piloto"): "Planta piloto",
    label_key("Proyectos de ley"): "Proyecto de ley",
    label_key("Prototipos"): "Prototipo",
    label_key("Otra publicación divulgativa"): "Otra producción bibliográfica",
    label_key("Innovaciones en Procesos y Procedimientos"): "Innovación de proceso o procedimiento",
    label_key("Empresas de base tecnológica"): "Empresa de base tecnológica",
    label_key("Innovaciones generadas en la Gestión Empresarial"): "Innovaciones generadas de producción empresarial",
    label_key("Redes de Conocimiento Especializado"): "Red de conocimiento especializado",
    label_key("Asesorías al Programa Ondas"): "Asesoría al programa Ondas",
}


def observation_id(section, native_type):
    return sha256(json.dumps([label_key(section), label_key(native_type)],
                             ensure_ascii=False).encode()).hexdigest()[:24]


def classification_context(record, entity="", channel=""):
    section = str(record.get("source_section") or
                  {"projects": "Proyectos", "events": "Eventos Científicos", "patents": "Patentes"}.get(channel or entity, ""))
    field = ("project_type" if entity == "projects" or channel == "projects" or section == "Proyectos" else
             "event_type" if entity == "events" or channel == "events" or section == "Eventos Científicos" else "product_type")
    value = str(record.get(field) or record.get("product_type") or "")
    if entity == "events" or channel == "events" or exact_key(section) == exact_key("Eventos Científicos"):
        value = normalize_event_type(value)
    return value, section


def catalog_identity():
    catalog = get_scraping_catalog()
    return {"policy": POLICY_VERSION, "version": catalog.version, "sha256": catalog.fingerprint,
            "shared_version": catalog.shared.version, "shared_sha256": catalog.shared.source_sha256}


class ScrapingTypeCatalog:
    def __init__(self, payload, shared=None):
        self.shared = shared or get_impactu_catalog()
        self.payload = payload
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported scraping catalog schema")
        self.version = str(payload["catalog_version"])
        # Observation counts/examples and rejected titles do not change mapping
        # behavior. The shipped catalog and its richer workbook share this ID.
        behavior = {"schema": 1, "version": self.version, "policy": POLICY_VERSION,
                    "aliases": ALIASES, "section_aliases": SECTION_ALIASES,
                    "shared_version": self.shared.version, "shared_sha256": self.shared.source_sha256,
                    "entries": [{k: e.get(k) for k in ("parent", "type", "status", "entity", "type_impactu",
                                                          "justification", "reviewer", "evidence")}
                                for e in payload.get("entries", [])]}
        self.fingerprint = sha256(json.dumps(behavior, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.entries = {}
        self.allowed = {(m["entity"], m["type_impactu"]) for m in self.shared.payload["mappings"]}
        for entry in payload.get("entries", []):
            key = (label_key(entry.get("parent")), label_key(entry.get("type")))
            if not key[1] or key in self.entries:
                raise ValueError("empty or duplicate scraping catalog type/context")
            status = entry.get("status")
            if status not in {"inherited", "approved", "pending", "ambiguous", "excluded"}:
                raise ValueError("invalid scraping catalog status")
            if status in {"inherited", "approved"}:
                if (entry.get("entity"), entry.get("type_impactu")) not in self.allowed:
                    raise ValueError("scraping mapping must target a canonical entity/type")
                if not entry.get("justification"):
                    raise ValueError("scraping mapping needs a justification")
            self.entries[key] = entry
        self.index = defaultdict(list)
        for mapping in self.shared.payload["mappings"]:
            if mapping["source"] == "scienti":
                labels = path_labels(mapping["type"])
                for start in range(len(labels)):
                    for end in range(start + 1, len(labels) + 1):
                        alias = " - ".join(labels[start:end])
                        self.index[label_key(alias)].append((mapping, labels, alias))
                self.index[exact_key(mapping["type"])].append((mapping, labels, " - ".join(labels)))
            elif mapping["source"] == "minciencias":
                labels = tuple(mapping["type"].split(": ", 1))
                self.index[label_key(mapping["type"])].append((mapping, labels, mapping["type"]))
                if len(labels) == 2:
                    self.index[label_key(labels[1])].append((mapping, labels, labels[1]))

    def inherit(self, value, section=""):
        """Propose only an exact, context-compatible, single semantic target."""
        key, section_key = label_key(value), label_key(section)
        section_key = label_key(SECTION_ALIASES.get(section_key, section))
        candidates = list(self.index.get(key, []))
        # A complete leaf label wins over an ancestor of the same name.
        # E.g. "Demás trabajos" is itself cataloged; its Póster child is not
        # evidence that the generic label is a conference result.
        leaves = [c for c in candidates if label_key(c[1][-1]) == key]
        if leaves:
            candidates = leaves
        if section_key:
            contextual = [c for c in candidates if section_key in {
                label_key(v) for v in (c[1][:1] if c[0]["source"] == "minciencias" else c[1])
            }]
            # Do not match a familiar subtype under an unrelated category.
            full_paths = [c for c in candidates if " - " in str(value)
                          and label_key(" - ".join(c[1])) == key]
            candidates = full_paths or contextual or (candidates if section_key == key else [])
        canonical = [(e, t) for e, t in self.allowed if label_key(t) == key]
        if len(canonical) == 1 and (not section_key or section_key == key):
            entity, impactu = canonical[0]
            return {"native": impactu, "mapping": {"entity": entity, "type_impactu": impactu},
                    "status": "inherited", "evidence": [{"source": "impactu", "type": impactu,
                                                              "entity": entity, "type_impactu": impactu}]}
        semantics = {(m["entity"], m["type_impactu"]) for m, _, _ in candidates}
        references = sorted({(m["source"], m["type"], m["entity"], m["type_impactu"])
                             for m, _, _ in candidates})
        evidence = [dict(zip(("source", "type", "entity", "type_impactu"), ref)) for ref in references]
        if len(semantics) == 1:
            entity, impactu = next(iter(semantics))
            aliases = {alias for _, _, alias in candidates}
            native = next(iter(aliases)) if len(aliases) == 1 else str(value).strip().rstrip(" :")
            return {"native": native, "mapping": {"entity": entity, "type_impactu": impactu},
                    "status": "inherited", "evidence": evidence}
        if semantics:
            return {"native": ALIASES.get(exact_key(str(value).strip().rstrip(" :")), str(value).strip().rstrip(" :")), "mapping": None,
                    "status": "ambiguous", "evidence": evidence}
        # A canonical label itself is an exact identity, never a guessed subtype.
        return {"native": "", "mapping": None, "status": "pending", "evidence": []}

    def resolve(self, value, section=""):
        entry = self.entries.get((label_key(section), label_key(value)))
        if entry is None:
            return self.inherit(value, section)
        status = entry["status"]
        mapping = ({"entity": entry["entity"], "type_impactu": entry["type_impactu"]}
                   if status in {"inherited", "approved"} else None)
        raw = str(value).strip().rstrip(" :")
        if mapping and mapping["entity"] == "events" and exact_key(section) == exact_key("Eventos Científicos"):
            raw = normalize_event_type(raw)
        return {"native": ALIASES.get(exact_key(raw), raw) if status in {"inherited", "approved", "ambiguous"} else "",
                "mapping": mapping, "status": status, "evidence": entry.get("evidence", [])}


@lru_cache(maxsize=1)
def get_scraping_catalog():
    payload = json.loads(files("yuku").joinpath(CATALOG_RESOURCE).read_text(encoding="utf-8"))
    # The shared workbook owns published assignments. Local data retains the
    # observation counts, samples and decision history, never a competing target.
    shared = get_impactu_catalog()
    central = shared.payload.get("scraping_mappings", [])
    if "scraping_mappings" in shared.payload:
        observed = {(label_key(e["parent"]), label_key(e["type"])): e for e in payload["entries"]}
        merged = []
        for mapping in central:
            key = (label_key(mapping["parent"]), label_key(mapping["type"]))
            entry = dict(observed.get(key) or {})
            previous_target = (entry.get("entity"), entry.get("type_impactu"))
            entry.update(parent=mapping["parent"], type=mapping["type"], entity=mapping["entity"],
                         type_impactu=mapping["type_impactu"])
            if previous_target != (mapping["entity"], mapping["type_impactu"]) or entry.get("status") not in {"inherited", "approved"}:
                entry.update(status="approved", justification="Assignment from the shared editorial catalog",
                             reviewer="shared catalog " + shared.version)
            entry.setdefault("id", observation_id(mapping["parent"], mapping["type"]))
            merged.append(entry)
        payload["entries"] = merged
        payload["shared_catalog"] = {"version": shared.version, "sha256": shared.source_sha256}
    return ScrapingTypeCatalog(payload)
