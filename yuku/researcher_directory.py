from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import math
import re
import threading
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup
from pymongo import ASCENDING, UpdateOne
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from yuku.cvlac_related_works import SCIENTI_CVLAC_URL
from yuku.scienti_profiles import GlobalRateLimiter


RESULTS_URL = (
    "https://scienti.minciencias.gov.co/ciencia-war/"
    "enRecursoHumanoBusqueda.do"
)
TABLE_ID = "investigadores"
PAGE_SIZE = 100
COD_RH_RE = re.compile(r"[?&]cod_rh=(\d+)(?:&|$)", re.IGNORECASE)
RESULT_RANGE_RE = re.compile(
    r"Resultados\s+([\d.]+)\s*-\s*([\d.]+)\s+de\s+([\d.]+)", re.IGNORECASE
)
LEVEL_VALUES = [
    "Primario && incompleta",
    "Primario",
    "Secundario",
    "residencia",
    "MBA",
    "Cursos && de && corta && duración",
    "Perfeccionamiento",
    "Otros",
    "No && informado",
    "Pregrado && Universitario",
    "Especialización",
    "Maestria && Magister",
    "Doctorado",
    "Postdoctorado",
    "Jefe && de && Cátedra",
    "Técnico && nivel && medio",
    "Extensión",
    "Técnico && nivel && superior",
]
COMBINED_LEVEL_FILTER = " || ".join(f"({value})" for value in LEVEL_VALUES)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def page_url(page: int) -> str:
    params = [
        ("instFormacion", ""),
        ("txtNamesRh", ""),
        ("ciuInstAct", ""),
        ("areaActuacion", ""),
        ("instActual", ""),
        ("instProfesion", ""),
        ("nivelFormacion", COMBINED_LEVEL_FILTER),
        ("parEvaluadoEn", ""),
        ("sglPaisNacim", ""),
        ("producto", ""),
        ("clave", ""),
        ("sectorAplicacion", ""),
        ("nroDocumentoIdent", ""),
        ("paisInstAct", ""),
        ("areaFormacion", ""),
        ("grupos", ""),
        ("maxRows", str(PAGE_SIZE)),
        (f"{TABLE_ID}_tr_", "true"),
        (f"{TABLE_ID}_p_", str(page)),
        (f"{TABLE_ID}_mr_", str(PAGE_SIZE)),
        (f"{TABLE_ID}_f_tpoNacionailidad", ""),
    ]
    return f"{RESULTS_URL}?{urlencode(params, encoding='iso-8859-1')}"


def parse_page(response: requests.Response) -> dict:
    soup = BeautifulSoup(response.content.decode("iso-8859-1"), "html.parser")
    match = RESULT_RANGE_RE.search(soup.get_text(" ", strip=True))
    if not match:
        raise RuntimeError("Scienti directory result range could not be parsed")
    start, end, total = [int(value.replace(".", "")) for value in match.groups()]
    table = soup.find("table", id=TABLE_ID)
    if table is None:
        raise RuntimeError("Scienti researcher directory table was not found")
    records = []
    for row in table.select(f'tr[id^="{TABLE_ID}_row"]'):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        anchor = cells[1].find("a", href=True)
        href = urljoin(response.url, anchor.get("href", "")) if anchor else ""
        code_match = COD_RH_RE.search(href)
        if not anchor or not code_match:
            raise RuntimeError("Scienti directory row has no CVLAC identifier")
        code = code_match.group(1).zfill(10)
        records.append(
            {
                "cod_rh": code,
                "nombre_completo": " ".join(anchor.get_text(" ", strip=True).split()),
                "nacionalidad": " ".join(cells[2].get_text(" ", strip=True).split()),
                "url_cvlac": f"{SCIENTI_CVLAC_URL}{code}",
            }
        )
    return {"start": start, "end": end, "total": total, "records": records}


class AllResearchersDirectory:
    """Resumable, validated materialization of the public researcher index."""

    def __init__(
        self,
        db,
        *,
        workers: int = 2,
        requests_per_second: float = 0.5,
        pages_collection: str = "scienti_researcher_directory_pages",
        output_collection: str = "all_researchers",
    ):
        self.db = db
        self.workers = max(1, workers)
        self.rate = GlobalRateLimiter(requests_per_second)
        self.pages = db[pages_collection]
        self.output = db[output_collection]
        self.local = threading.local()

    def session(self) -> requests.Session:
        session = getattr(self.local, "session", None)
        if session is None:
            retry = Retry(
                total=6,
                connect=6,
                read=6,
                status=6,
                backoff_factor=2,
                backoff_max=120,
                respect_retry_after_header=True,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=frozenset({"GET"}),
            )
            session = requests.Session()
            session.headers["User-Agent"] = "Mozilla/5.0 (compatible; Yuku-Scienti/4.0)"
            session.mount("https://", HTTPAdapter(max_retries=retry))
            self.local.session = session
        return session

    def fetch_page(self, page: int) -> dict:
        self.rate.wait()
        response = self.session().get(page_url(page), timeout=90)
        response.raise_for_status()
        value = parse_page(response)
        value.update(
            {
                "_id": page,
                "page": page,
                "schema_version": 1,
                "filter": COMBINED_LEVEL_FILTER,
                "fetched_at": utc_now(),
            }
        )
        expected_start = (page - 1) * PAGE_SIZE + 1
        expected_end = min(page * PAGE_SIZE, value["total"])
        if (value["start"], value["end"]) != (expected_start, expected_end):
            raise RuntimeError(f"Unexpected result range on page {page}")
        if len(value["records"]) != expected_end - expected_start + 1:
            raise RuntimeError(f"Unexpected row count on page {page}")
        self.pages.replace_one({"_id": page}, value, upsert=True)
        return value

    def download(self, *, limit_pages: int = 0, refresh: bool = False) -> dict[str, int]:
        first = None if refresh else self.pages.find_one({"_id": 1, "schema_version": 1})
        if first is None:
            first = self.fetch_page(1)
        total = int(first["total"])
        total_pages = math.ceil(total / PAGE_SIZE)
        target_pages = min(limit_pages, total_pages) if limit_pages else total_pages
        cached_pages = {1} if refresh else set(
            self.pages.distinct(
                "page",
                {
                    "schema_version": 1,
                    "filter": COMBINED_LEVEL_FILTER,
                    "total": total,
                    "page": {"$lte": target_pages},
                },
            )
        )
        missing = [page for page in range(1, target_pages + 1) if page not in cached_pages]
        counts = Counter({"total": total, "target_pages": target_pages, "cached_pages": len(cached_pages)})
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(self.fetch_page, page): page for page in missing}
            for position, future in enumerate(as_completed(futures), start=1):
                value = future.result()
                if int(value["total"]) != total:
                    raise RuntimeError("Scienti directory total changed during pagination")
                counts["downloaded_pages"] += 1
                if position % 25 == 0 or position == len(futures):
                    print(f"INFO: researcher directory pages {position}/{len(futures)}")

        seen: set[str] = set()
        operations: list[UpdateOne] = []
        processed = 0
        for page in self.pages.find(
            {"page": {"$lte": target_pages}, "total": total},
            {"records": 1},
        ).sort("page", ASCENDING):
            for record in page["records"]:
                code = record["cod_rh"]
                if code in seen:
                    raise RuntimeError(f"Duplicate COD_RH across directory pages: {code}")
                seen.add(code)
                operations.append(
                    UpdateOne({"cod_rh": code}, {"$set": record}, upsert=True)
                )
                processed += 1
                if len(operations) >= 2000:
                    self.output.bulk_write(operations, ordered=False)
                    operations.clear()
        if operations:
            self.output.bulk_write(operations, ordered=False)
        expected = min(target_pages * PAGE_SIZE, total)
        if processed != expected:
            raise RuntimeError(f"Processed {processed} directory rows, expected {expected}")
        self.output.create_index("cod_rh", unique=True)
        self.output.create_index("nombre_completo")
        counts["researchers_synced"] = processed
        return dict(counts)
