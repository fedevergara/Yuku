from __future__ import annotations

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import random
import re
import threading
import time
from typing import Iterable

from bs4 import BeautifulSoup
from pymongo import ASCENDING
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from yuku.cvlac_related_works import SCIENTI_CVLAC_URL
from yuku.gruplac_related_works import SCIENTI_GRUPLAC_URL


PRIVATE_CVLAC_TEXT = (
    "La información de este currículo no está disponible por solicitud del investigador"
)
CHARSET_RE = re.compile(
    br"charset\s*=\s*[\"']?([^\"'\s;>]+)",
    re.IGNORECASE,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def decode_scienti_response(response) -> tuple[str, str, int]:
    """Decode Scienti HTML using its declared charset, not heuristic guessing."""
    content = response.content
    match = CHARSET_RE.search(content[:10000])
    declared = match.group(1).decode("ascii", "replace") if match else ""
    encoding = declared or response.encoding or "utf-8"
    try:
        html = content.decode(encoding, "replace")
    except LookupError:
        encoding = "iso-8859-1"
        html = content.decode(encoding, "replace")
    return html, encoding.lower(), html.count("\ufffd")


class GlobalRateLimiter:
    """Thread-safe minimum interval between requests across all workers."""

    def __init__(self, requests_per_second: float):
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.interval = 1.0 / requests_per_second
        self.lock = threading.Lock()
        self.next_request = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_request - now)
            self.next_request = max(now, self.next_request) + self.interval
        if delay:
            time.sleep(delay + random.uniform(0, min(0.05, self.interval / 4)))

    def set_requests_per_second(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        with self.lock:
            self.interval = 1.0 / requests_per_second


class ScientiProfileDownloader:
    """Concurrent, resumable downloader for CVLAC and GrupLAC HTML.

    Raw HTML is immutable evidence for parsers. A separate state collection
    records attempts and errors; successful upserts are keyed by the stable
    COD_RH or COL group code, making interrupted executions idempotent.
    """

    def __init__(
        self,
        db,
        *,
        workers: int = 4,
        requests_per_second: float = 2.0,
        timeout: float = 90.0,
        max_retries: int = 5,
        state_collection: str = "scienti_profile_downloads",
    ):
        if workers < 1:
            raise ValueError("workers must be positive")
        self.db = db
        self.workers = workers
        self.requests_per_second = requests_per_second
        self.timeout = timeout
        self.max_retries = max_retries
        self.state = db[state_collection]
        self.rate_limiter = GlobalRateLimiter(requests_per_second)
        self.local = threading.local()
        self.state.create_index([("kind", ASCENDING), ("status", ASCENDING)])
        self.state.create_index("updated_at")

    def session(self) -> requests.Session:
        session = getattr(self.local, "session", None)
        if session is None:
            retry = Retry(
                total=self.max_retries,
                connect=self.max_retries,
                read=self.max_retries,
                status=self.max_retries,
                backoff_factor=1.5,
                backoff_max=90,
                respect_retry_after_header=True,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=frozenset({"GET"}),
            )
            session = requests.Session()
            session.headers["User-Agent"] = "Mozilla/5.0 (compatible; Yuku-Scienti/4.0)"
            session.mount(
                "https://",
                HTTPAdapter(
                    max_retries=retry,
                    pool_connections=self.workers,
                    pool_maxsize=self.workers,
                ),
            )
            self.local.session = session
        return session

    @staticmethod
    def cvlac_targets(cod_rh_values: Iterable[str]):
        for value in cod_rh_values:
            cod_rh = str(value or "").strip().zfill(10)
            if cod_rh.isdigit() and len(cod_rh) == 10:
                yield {
                    "id": cod_rh,
                    "url": f"{SCIENTI_CVLAC_URL}{cod_rh}",
                }

    @staticmethod
    def gruplac_targets(groups: Iterable[dict]):
        for group in groups:
            code = str(group.get("codigo_grupo") or group.get("group_code") or "")
            code = code.strip().upper()
            url = str(group.get("url_gruplac") or "").strip().rstrip(",")
            nro = str(group.get("nro") or "").strip()
            if not url and nro:
                url = f"{SCIENTI_GRUPLAC_URL}{nro}"
            if code.startswith("COL") and url:
                yield {"id": code, "url": url, "nro": nro}

    @staticmethod
    def classify_html(kind: str, html: str) -> str:
        soup = BeautifulSoup(html or "", "lxml")
        text = soup.get_text(" ", strip=True)
        if kind == "cvlac":
            if PRIVATE_CVLAC_TEXT in text:
                return "private"
            return "downloaded" if soup.find("a", attrs={"name": "datos_generales"}) else "empty"
        if kind == "gruplac":
            basic = soup.find(
                "td",
                class_="celdaEncabezado",
                string=lambda value: value and value.strip() == "Datos básicos",
            )
            if basic:
                return "downloaded"
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            if "GrupLAC" in title and soup.find(class_="celdaEncabezado"):
                return "incomplete"
            return "empty"
        raise ValueError(f"Unsupported profile kind: {kind}")

    def _download_one(self, kind: str, target: dict, raw_collection: str) -> str:
        item_id = target["id"]
        url = target["url"]
        state_id = "{}:{}".format(kind, item_id)
        self.state.update_one(
            {"_id": state_id},
            {
                "$set": {
                    "kind": kind,
                    "source_id": item_id,
                    "url": url,
                    "status": "running",
                    "updated_at": utc_now(),
                },
                "$inc": {"attempts": 1},
            },
            upsert=True,
        )
        try:
            self.rate_limiter.wait()
            response = self.session().get(url, timeout=self.timeout)
            response.raise_for_status()
            html, encoding, replacement_chars = decode_scienti_response(response)
            status = self.classify_html(kind, html)
            raw = {
                "_id": item_id,
                "kind": kind,
                "url": response.url,
                "html": html,
                "content_sha256": sha256(html.encode("utf-8")).hexdigest(),
                "source_content_sha256": sha256(response.content).hexdigest(),
                "encoding": encoding,
                "decode_replacement_chars": replacement_chars,
                "fetched_at": utc_now(),
                "http_status": response.status_code,
            }
            if target.get("nro"):
                raw["nro"] = target["nro"]
            self.db[raw_collection].replace_one({"_id": item_id}, raw, upsert=True)
            self.state.update_one(
                {"_id": state_id},
                {
                    "$set": {
                        "status": status,
                        "http_status": response.status_code,
                        "updated_at": utc_now(),
                    },
                    "$unset": {"error": ""},
                },
            )
            return status
        except Exception as error:
            self.state.update_one(
                {"_id": state_id},
                {
                    "$set": {
                        "status": "error",
                        "error": f"{type(error).__name__}: {error}",
                        "updated_at": utc_now(),
                    }
                },
            )
            return "error"

    def download(
        self,
        kind: str,
        targets: Iterable[dict],
        *,
        raw_collection: str,
        limit: int = 0,
        refresh_days: int | None = None,
        progress_every: int = 25,
        fallback_requests_per_second: float | None = None,
        fallback_after_errors: int = 5,
    ) -> dict[str, int | float]:
        if kind not in {"cvlac", "gruplac"}:
            raise ValueError("kind must be cvlac or gruplac")
        existing: set[str] = set()
        if refresh_days is None:
            existing = {str(value) for value in self.db[raw_collection].distinct("_id")}
        else:
            cutoff = utc_now() - timedelta(days=max(0, refresh_days))
            existing = {
                str(value)
                for value in self.db[raw_collection].distinct(
                    "_id", {"fetched_at": {"$gte": cutoff}}
                )
            }

        selected: list[dict] = []
        seen: set[str] = set()
        skipped = 0
        for target in targets:
            item_id = str(target.get("id") or "")
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            if item_id in existing:
                skipped += 1
                continue
            selected.append(target)
            if limit and len(selected) >= limit:
                break

        counts = Counter({"selected": len(selected), "skipped": skipped})
        fallback_applied = False
        print(
            f"{utc_now().isoformat()} INFO: starting {kind} download, "
            f"selected={len(selected)} skipped={skipped} workers={self.workers} "
            f"requests_per_second={self.requests_per_second} "
            f"raw_collection={raw_collection}",
            flush=True,
        )
        total = len(selected)
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            target_iterator = iter(selected)
            pending = set()

            def fill_pending() -> None:
                # Keep only a small bounded window of futures. A complete
                # Scienti directory may contain hundreds of thousands of
                # identifiers and submitting all of them at once needlessly
                # consumes memory.
                while len(pending) < self.workers * 2:
                    try:
                        target = next(target_iterator)
                    except StopIteration:
                        return
                    pending.add(
                        executor.submit(self._download_one, kind, target, raw_collection)
                    )

            fill_pending()
            position = 0
            while pending:
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    position += 1
                    counts[future.result()] += 1
                    if (
                        not fallback_applied
                        and fallback_requests_per_second
                        and fallback_requests_per_second < self.requests_per_second
                        and counts["error"] >= max(1, fallback_after_errors)
                    ):
                        previous_rate = self.requests_per_second
                        self.requests_per_second = fallback_requests_per_second
                        self.rate_limiter.set_requests_per_second(
                            fallback_requests_per_second
                        )
                        fallback_applied = True
                        counts["rate_fallbacks"] += 1
                        print(
                            f"{utc_now().isoformat()} WARNING: final error threshold "
                            f"reached ({counts['error']}), reducing global rate from "
                            f"{previous_rate} to {fallback_requests_per_second} requests/s",
                            flush=True,
                        )
                    if position % max(1, progress_every) == 0 or position == total:
                        print(
                            f"{utc_now().isoformat()} INFO: {kind} profiles "
                            f"{position}/{total}, "
                            f"statuses={dict(sorted(counts.items()))}",
                            flush=True,
                        )
                fill_pending()
        print(
            f"{utc_now().isoformat()} INFO: completed {kind} download, "
            f"statuses={dict(sorted(counts.items()))}",
            flush=True,
        )
        counts["final_requests_per_second"] = self.requests_per_second
        return dict(counts)
