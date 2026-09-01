from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from pymongo import ASCENDING, UpdateOne
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from yuku.cvlac_related_works import SCIENTI_CVLAC_URL


RESEARCHER_SEARCH_URL = (
    "https://scienti.minciencias.gov.co/ciencia-war/jsp/enRecurso/"
    "IndexRecursoHumano.jsp"
)
GROUP_SEARCH_URL = (
    "https://scienti.minciencias.gov.co/ciencia-war/"
    "busquedaAvanzadaGrupos.do?buscar=sinBuscar"
)
COD_RH_RE = re.compile(r"[?&]cod_rh=(\d+)(?:&|$)", re.IGNORECASE)
COD_GROUP_RE = re.compile(r"COL\d+", re.IGNORECASE)
GRUPLAC_RE = re.compile(r"/gruplac/.*/visualizagr\.jsp\?nro=(\d+)", re.IGNORECASE)
RESULT_RANGE_RE = re.compile(
    r"Resultados\s+([\d.]+)\s*-\s*([\d.]+)\s+de\s+([\d.]+)",
    re.IGNORECASE,
)


def build_session() -> requests.Session:
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.5,
        backoff_max=60,
        respect_retry_after_header=True,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset({"GET", "POST"}),
    )
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; Yuku-Scienti/4.0)"
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split())


def parse_total(soup: BeautifulSoup) -> int:
    text = soup.get_text(" ", strip=True)
    match = RESULT_RANGE_RE.search(text)
    if match:
        return int(match.group(3).replace(".", ""))
    if "No se encontraron resultados" in text:
        return 0
    raise RuntimeError("Scienti result count could not be parsed")


def read_json_array(path: str | Path) -> list[dict[str, Any]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value:
        raise ValueError("The source JSON must be a non-empty array")
    if any(not isinstance(record, dict) for record in value):
        raise ValueError("Every JSON record must be an object")
    return value


class ScientiIdentifierResolver:
    """Resolve historical JSON entities against the public Scienti forms.

    Query results are cached in MongoDB, so retries and repeated imports do not
    repeat requests. Destination writes use stable source ids and upserts.
    """

    def __init__(
        self,
        db,
        *,
        delay: float = 0.75,
        cache_collection: str = "scienti_identifier_search_cache",
    ):
        self.db = db
        self.delay = max(0.0, delay)
        self.cache = db[cache_collection]
        self.cache.create_index([("kind", ASCENDING), ("query", ASCENDING)])
        self.session = build_session()
        self.last_request = 0.0

    def close(self) -> None:
        self.session.close()

    def wait(self) -> None:
        remaining = self.delay - (time.monotonic() - self.last_request)
        if remaining > 0:
            time.sleep(remaining)

    def open_form(self, kind: str) -> tuple[str, dict[str, str]]:
        url = RESEARCHER_SEARCH_URL if kind == "researcher" else GROUP_SEARCH_URL
        form_name = (
            "enRecursoHumanoBusquedaForm"
            if kind == "researcher"
            else "busquedaAvanzadaGruposForm"
        )
        response = self.session.get(url, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        form = soup.find("form", attrs={"name": form_name})
        if form is None or not form.get("action"):
            raise RuntimeError(f"Scienti {kind} search form was not found")
        defaults: dict[str, str] = {}
        for tag in form.select("input[name], select[name], textarea[name]"):
            name = tag.get("name")
            if not name:
                continue
            if tag.name == "select":
                option = tag.find("option", selected=True) or tag.find("option")
                defaults[name] = option.get("value", "") if option else ""
            else:
                defaults[name] = tag.get("value", "") or ""
        defaults["maxRows"] = "100"
        return urljoin(response.url, form["action"]), defaults

    def cached_query(
        self,
        kind: str,
        query: str,
        action: str,
        defaults: dict[str, str],
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        cache_id = sha256(f"{kind}|{query}".encode("utf-8")).hexdigest()
        if not refresh:
            cached = self.cache.find_one({"_id": cache_id}, {"_id": 0})
            if cached and cached.get("schema_version") == 1:
                return cached
        self.wait()
        field = "nroDocumentoIdent" if kind == "researcher" else "codIdGrupo"
        response = self.session.post(
            action,
            data={**defaults, field: query},
            timeout=90,
        )
        self.last_request = time.monotonic()
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        results = (
            self.parse_researchers(soup, response.url)
            if kind == "researcher"
            else self.parse_groups(soup, response.url)
        )
        value = {
            "schema_version": 1,
            "kind": kind,
            "query": query,
            "total_results": parse_total(soup),
            "results": results,
        }
        self.cache.replace_one({"_id": cache_id}, {"_id": cache_id, **value}, upsert=True)
        return value

    @staticmethod
    def parse_researchers(soup: BeautifulSoup, response_url: str) -> list[dict[str, str]]:
        by_code: dict[str, dict[str, str]] = {}
        table = soup.find("table", id="investigadores")
        if table is None:
            return []
        for row in table.select('tr[id^="investigadores_row"]'):
            anchor = row.find("a", href=COD_RH_RE)
            if anchor is None:
                continue
            href = urljoin(response_url, anchor.get("href", ""))
            match = COD_RH_RE.search(href)
            if not match:
                continue
            code = match.group(1).zfill(10)
            by_code[code] = {
                "nombre_completo": clean_text(anchor.get_text(" ", strip=True)),
                "cod_rh": code,
                "url_cvlac": f"{SCIENTI_CVLAC_URL}{code}",
            }
        return list(by_code.values())

    @staticmethod
    def parse_groups(soup: BeautifulSoup, response_url: str) -> list[dict[str, Any]]:
        table = soup.find("table", id="gruposAvanzada")
        if table is None:
            return []
        results = []
        for row in table.find_all("tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) < 8:
                continue
            code = clean_text(cells[1].get_text(" ", strip=True)).upper()
            if not COD_GROUP_RE.fullmatch(code):
                continue
            group_anchor = cells[2].find("a", href=True)
            leader_anchor = cells[3].find("a", href=True)
            profiles_anchor = cells[4].find("a", href=True)
            group_url = urljoin(response_url, group_anchor["href"]) if group_anchor else ""
            leader_url = urljoin(response_url, leader_anchor["href"]) if leader_anchor else ""
            leader_match = COD_RH_RE.search(leader_url)
            cod_rh = leader_match.group(1).zfill(10) if leader_match else None
            group_match = GRUPLAC_RE.search(group_url)
            results.append(
                {
                    "codigo_grupo": code,
                    "nombre_grupo": clean_text(cells[2].get_text(" ", strip=True)),
                    "lider": clean_text(cells[3].get_text(" ", strip=True)),
                    "cod_rh_lider": cod_rh,
                    "url_cvlac_lider": f"{SCIENTI_CVLAC_URL}{cod_rh}" if cod_rh else None,
                    "url_gruplac": group_url.rstrip(",") if group_match else None,
                    "nro_gruplac": group_match.group(1) if group_match else None,
                    "url_perfiles": (
                        urljoin(response_url, profiles_anchor["href"]).rstrip(",")
                        if profiles_anchor else None
                    ),
                    "avalado": clean_text(cells[5].get_text(" ", strip=True)) or None,
                    "estado": clean_text(cells[6].get_text(" ", strip=True)) or None,
                    "clasificado_en": clean_text(cells[7].get_text(" ", strip=True)) or None,
                }
            )
        return results

    @staticmethod
    def document_candidates(record: dict[str, Any]) -> list[str]:
        values = [record.get("documento_normalizado")]
        values.extend(str(record.get("documento_original") or "").split(" | "))
        result = []
        for value in values:
            candidate = clean_text(value)
            if candidate.endswith(".0"):
                candidate = candidate[:-2]
            if candidate and candidate not in result:
                result.append(candidate)
        return result

    def recognize_researchers(
        self,
        path: str | Path,
        *,
        collection: str = "recognized_researchers",
        offset: int = 0,
        limit: int = 0,
        refresh: bool = False,
    ) -> dict[str, int]:
        records = read_json_array(path)
        selected = records[offset:None if not limit else offset + limit]
        action, defaults = self.open_form("researcher")
        operations = []
        counts = Counter()
        for record in selected:
            candidates = self.document_candidates(record)
            results: dict[str, dict[str, str]] = {}
            try:
                for candidate in candidates:
                    query = self.cached_query(
                        "researcher", candidate, action, defaults, refresh=refresh
                    )
                    for result in query["results"]:
                        results[result["cod_rh"]] = result
                matches = list(results.values())
                if len(matches) == 1:
                    selected_match = matches[0]
                    status = "encontrado_unico"
                elif len(matches) > 1 and len({x["nombre_completo"] for x in matches}) == 1:
                    selected_match = None
                    status = "encontrado_varios"
                elif matches:
                    selected_match = None
                    status = "ambiguo"
                else:
                    selected_match = None
                    status = "sin_resultados"
                document = {
                    **record,
                    "nombre_completo": selected_match["nombre_completo"] if selected_match else (
                        matches[0]["nombre_completo"] if status == "encontrado_varios" else None
                    ),
                    "cod_rh": selected_match["cod_rh"] if selected_match else None,
                    "url_cvlac": selected_match["url_cvlac"] if selected_match else None,
                    "consulta_scienti": {
                        "estado": status,
                        "documentos_consultados": candidates,
                        "cantidad_coincidencias": len(matches),
                        "coincidencias": sorted(results),
                    },
                }
            except Exception as error:
                status = "error"
                document = {
                    **record,
                    "nombre_completo": None,
                    "cod_rh": None,
                    "url_cvlac": None,
                    "consulta_scienti": {
                        "estado": status,
                        "documentos_consultados": candidates,
                        "cantidad_coincidencias": 0,
                        "coincidencias": [],
                        "error": f"{type(error).__name__}: {error}",
                    },
                }
            counts[status] += 1
            source_id = str(record.get("investigador_id") or "")
            if not source_id:
                raise ValueError("Every researcher requires investigador_id")
            operations.append(UpdateOne({"investigador_id": source_id}, {"$set": document}, upsert=True))
            if len(operations) >= 100:
                self.db[collection].bulk_write(operations, ordered=False)
                operations.clear()
        if operations:
            self.db[collection].bulk_write(operations, ordered=False)
        self.db[collection].create_index("investigador_id", unique=True)
        self.db[collection].create_index("cod_rh")
        return dict(counts)

    def recognize_groups(
        self,
        path: str | Path,
        *,
        collection: str = "recognized_groups",
        offset: int = 0,
        limit: int = 0,
        refresh: bool = False,
    ) -> dict[str, int]:
        records = read_json_array(path)
        selected_records = records[offset:None if not limit else offset + limit]
        action, defaults = self.open_form("group")
        operations = []
        counts = Counter()
        for record in selected_records:
            code = clean_text(record.get("codigo_grupo")).upper()
            if not COD_GROUP_RE.fullmatch(code):
                raise ValueError(f"Invalid codigo_grupo: {code}")
            try:
                query = self.cached_query("group", code, action, defaults, refresh=refresh)
                exact = [x for x in query["results"] if x["codigo_grupo"] == code]
                match = exact[0] if len(exact) == 1 else None
                if match and match.get("url_gruplac"):
                    missing = [key for key, value in match.items() if value in (None, "")]
                    status = "encontrado_incompleto" if missing else "encontrado_unico"
                elif match:
                    missing = ["url_gruplac"]
                    status = "error"
                elif exact:
                    missing = []
                    status = "ambiguo"
                elif query["total_results"]:
                    missing = []
                    status = "sin_coincidencia_exacta"
                else:
                    missing = []
                    status = "sin_resultados"
                document = {**record, **(match or {})}
                document["consulta_scienti"] = {
                    "estado": status,
                    "cantidad_coincidencias": len(exact),
                    "campos_faltantes": missing,
                }
            except Exception as error:
                status = "error"
                document = {
                    **record,
                    "consulta_scienti": {
                        "estado": status,
                        "cantidad_coincidencias": 0,
                        "campos_faltantes": ["url_gruplac"],
                        "error": f"{type(error).__name__}: {error}",
                    },
                }
            counts[status] += 1
            operations.append(UpdateOne({"codigo_grupo": code}, {"$set": document}, upsert=True))
            if len(operations) >= 100:
                self.db[collection].bulk_write(operations, ordered=False)
                operations.clear()
        if operations:
            self.db[collection].bulk_write(operations, ordered=False)
        self.db[collection].create_index("codigo_grupo", unique=True)
        self.db[collection].create_index("cod_rh_lider")
        return dict(counts)
