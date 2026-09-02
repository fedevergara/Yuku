import requests
import pandas as pd
import hashlib
import json
from pymongo import MongoClient
from sodapy import Socrata
from bs4 import BeautifulSoup
import time
import urllib3
import sys
from io import StringIO

from yuku.cvlac_related_works import SCIENTI_CVLAC_URL, normalize_related_works_document
from yuku.cvlac_evaluation import CvlacProfileEvaluation
from yuku.cvlac_priority_snapshot import CvlacPrioritySnapshot, FULL_SOURCES
from yuku.cvlac_comparison import CvlacEvaluationComparator
from yuku.cvlac_work_graph import (
    CheckpointedNormalizedWorkGraphBuilder,
    CvlacWorkGraphBuilder,
    validate_scienti_graph_gate,
)
from yuku.gruplac_related_works import normalize_gruplac_document
from yuku.gruplac_verification import GruplacIncompleteVerifier
from yuku.minciencias_measurements import MincienciasMeasurementPipeline
from yuku.researcher_directory import AllResearchersDirectory
from yuku.scienti_profiles import ScientiProfileDownloader
from yuku.scienti_search import ScientiIdentifierResolver
from yuku.scienti_entities import ScientiEntityNormalizationRun
from yuku.scienti_entity_graph import ScientiExactEntityGraphBuilder
from yuku.scienti_entity_materialization import ScientiEntitySnapshotMaterializer
from yuku.scienti_entity_comparison import ScientiEntityVersionComparator
from yuku.scienti_entity_publication import ScientiEntityPublisher
from yuku.scienti_auxiliary_audit import ScientiAuxiliarySemanticAudit
from yuku.scienti_project_audit import ScientiProjectSemanticAudit
from yuku.scienti_bibliographic_audit import ScientiBibliographicEnrichmentAuditor
from yuku.scienti_release import ScientiFinalReleaseManager
from yuku.scienti_pipeline import (
    ScientiFullPipeline,
    load_pipeline_config,
    pipeline_status,
)
from yuku.scienti_normalization import (
    CvlacNormalizationAuditor,
    CvlacNormalizationRun,
    GruplacNormalizationRun,
    GruplacNormalizationAuditor,
)


class Yuku:
    def __init__(self, mongo_db: str = "yuku", mongodb_uri: str = "mongodb://localhost:27017/", socrata_endpoint: str = "www.datos.gov.co", delay: float = 0.3):
        """
        Contructor for Yuku, we only support open datasets, credentials are not supported.

        Parameters:
        ------------
        socrata_endpoint:str
            endpoint for socrata, default "www.datos.gov.co"
        """
        self.client = Socrata(socrata_endpoint, None, timeout=120)
        self.mlient = MongoClient(mongodb_uri)
        self.db = self.mlient[mongo_db]
        self.socrata_endpoint = socrata_endpoint
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.delay = delay

    def run_scienti_pipeline(
        self,
        config_path: str,
        *,
        from_stage: str = "",
        to_stage: str = "",
        dry_run: bool = False,
    ):
        """Run or resume the complete ordered Scienti pipeline from JSON config."""
        config = load_pipeline_config(config_path)
        return ScientiFullPipeline(self, config).run(
            from_stage=from_stage,
            to_stage=to_stage,
            dry_run=dry_run,
        )

    def scienti_pipeline_status(self, run_name: str):
        """Return compact per-stage status for a full Scienti pipeline."""
        return pipeline_status(self.db, run_name)

    def cvlav_private_profile(self, soup: BeautifulSoup):
        """
        Check if the profile is private

        Parameters:
        ------------
        soup:BeautifulSoup
            soup object from cvlac profile

        Returns:
        ------------
        bool
            True if the profile is private, False otherwise
        """
        blockquotes = soup.find_all('blockquote')
        text_private = 'La información de este currículo no está disponible por solicitud del investigador'
        for i in blockquotes:
            if text_private == i.text:
                return True
        return False

    def download_cvlac_data(self, dataset_id: str):
        """
        Method to download cvlav information.
        Unfortunately we dont have support for checkpoint in this method.

        Parameters:
        ------------
        dataset_id:str
            id for dataset in socrata ex: bqtm-4y2h
        """
        if "cvlac_dataset_info" in self.db.list_collection_names():
            print("WARNING: cvlac_dataset_info already in the database, it wont be downloaded again, drop the database if you want start over.")
        else:
            print(f"INFO: downloading dataset metadata from id {dataset_id}")
            dataset_info = self.client.get_metadata(dataset_id)
            self.db["cvlac_dataset_info"].insert_one(dataset_info)
        if "cvlac_data" in self.db.list_collection_names():
            print("WARNING: cvlac_data already in the database, it wont be downloaded again, drop the database if you want start over.")
        else:
            data = self.client.get_all(dataset_id)
            data = list(data)
            self.db["cvlac_data"].insert_many(data)

    def process_cvlac_profile(self, cvlac, scienti_url, counter, count, use_raw, max_tries=1):
        for t in range(max_tries):
            url = f'{scienti_url}{cvlac}'
            if t > 0:
                print(f"INFO: retrying {t} {url} ")
            if counter % 10 == 0:
                if use_raw:
                    print(f"INFO: Parsed {counter} of {count} from raw")
                else:
                    print(f"INFO: Downloaded {counter} of {count}")
            if use_raw:
                r = self.db["cvlac_stage_raw"].find_one({"_id": cvlac})
                if r is None:
                    print(f"WARNING: not found id {cvlac} in raw collection ")
                    continue
                else:
                    html = r['html']
            else:
                try:
                    r = requests.get(url, verify=False)
                    html = r.text
                except Exception as e:
                    print(e, file=sys.stderr)
                    self.db["cvlac_stage_error"].insert_one(
                        {"url": url, "id_persona_pr": cvlac, "status_code": 'unkown', "html": '', "exception": str(e)})
                    continue

                if r.status_code != 200:
                    print(
                        f"Error processing id {cvlac} with url = {url} status code = {r.status_code} ")
                    self.db["cvlac_stage_error"].insert_one(
                        {"url": url, "id_persona_pr": cvlac, "status_code": r.status_code, "html": html})
                    continue

            if not html:
                continue

            soup = BeautifulSoup(html, 'lxml')  # Parse the HTML as a string

            reg = {'id_persona_pr': cvlac, "url": url}
            record = {}
            try:
                # Datos Generales (checking if the page is empty)
                a_tag = soup.find('a', {'name': 'datos_generales'}).parent
                if a_tag is not None:
                    # a_tag = a_tag
                    table_tag = a_tag.find_next('table')

                    if table_tag is None:
                        print(
                            f"WARNING: found empty id {cvlac} with url = {url} ")
                        self.db["cvlac_stage_empty"].insert_one(reg)
                        continue
                    record['datos_generales'] = pd.read_html(StringIO(table_tag.decode()))[
                        0].to_dict(orient='records')
            except Exception as e:
                print(f"Error processing id {cvlac} with url = {url} ")
                print("=" * 20)
                print(html)
                print("=" * 20)
                print(e)
                self.db["cvlac_stage_error"].insert_one(
                    {"url": url, "id_persona_pr": cvlac, "status_code": r.status_code, "html": html, "exception": str(e)})
                continue
            # Datos Generales (Extracting data if not empty)
            a_tag = soup.find('a', {'name': 'datos_generales'})
            table_tag = a_tag.find_next('table')
            reg['datos_generales'] = {}
            reg['datos_generales']['Nombre'] = ''

            record = pd.read_html(StringIO(table_tag.decode()))[
                0].to_dict(orient='records')

            for d in record:
                if d and isinstance(d.get(0), str) and isinstance(d.get(1), str):
                    reg['datos_generales'][d.get(0)] = d.get(
                        1).replace('\xa0', ' ')
                else:
                    continue
            try:
                if self.cvlav_private_profile(soup):
                    print(
                        f"WARNING: found private id {cvlac} with url = {url} ")
                    self.db["cvlac_stage_private"].insert_one(reg)
                    self.db["cvlac_stage_raw"].insert_one(
                        {"_id": cvlac, "html": html})
                    time.sleep(self.delay)
                    counter += 1
                    return True
                    continue
            except Exception as e:
                print(f"Error processing id {cvlac} with url = {url} ")
                print(e, file=sys.stderr)
                self.db["cvlac_stage_error"].insert_one(
                    {"url": url, "id_persona_pr": cvlac, "status_code": r.status_code, "html": html, "exception": str(e)})
                continue
            try:
                # Redes
                a_tag = soup.find(
                    'a', {'name': 'redes_identificadores'}).parent
                table_tag = a_tag.find('table')
                reg['redes_identificadores'] = {}

                if table_tag is not None:
                    record = table_tag.find_all('a')
                    for link in record:
                        reg['redes_identificadores'][link.text] = link['href']

                # Identificadores
                a_tag = soup.find('a', {'name': 'red_identificadores'}).parent
                table_tag = a_tag.find('table')

                reg['red_identificadores'] = {}
                if table_tag is not None:
                    record = table_tag.find_all('a')
                    for link in record:
                        reg['red_identificadores'][link.text] = link['href']

                # Formación académica
                a_tag = soup.find('a', {'name': 'formacion_acad'}).parent
                table_tag = a_tag.find('table')
                reg['formacion_acad'] = {}
                if table_tag is not None:
                    record = table_tag.find_all('td')
                    for tag in record:
                        b_title = tag.find_all('b')
                        if len(b_title) > 0:
                            reg['formacion_acad'][b_title[0].text] = tag.text.split(
                                '\r\n')

                # Experiencia laboral
                a_tag = soup.find('a', {'name': 'experiencia'}).parent
                table_tag = a_tag.find('table')
                reg['experiencia'] = {}
                if table_tag is not None:
                    record = table_tag.find_all('td')
                    for tag in record:
                        b_title = tag.find_all('b')
                        if len(b_title) > 0:
                            reg['experiencia'][b_title[0].text] = {}

                            data0 = tag.text.replace('\xa0', ' ').split(
                                'Actividades de administración')
                            reg['experiencia'][b_title[0].text]["General"] = data0[0].split(
                                "\r\n")
                            if len(data0) > 1:
                                data1 = data0[1].split(
                                    'Actividades de docencia')
                                reg['experiencia'][b_title[0].text]['Actividades de administración'] = data1[0].split(
                                    "\r\n")
                                if len(data1) > 1:
                                    data2 = data1[1].split(
                                        'Actividades de investigación')
                                    reg['experiencia'][b_title[0].text]['Actividades de docencia'] = data2[0].split(
                                        "\r\n")
                                    if len(data2) > 1:
                                        reg['experiencia'][b_title[0].text]['Actividades de investigación'] = data2[1].split(
                                            "\r\n")

                self.db["cvlac_stage"].insert_one(reg)
                if use_raw is False:
                    self.db["cvlac_stage_raw"].insert_one(
                        {"_id": cvlac, "html": html})
            except Exception as e:
                print(f"Error processing id {cvlac} with url = {url} ")
                print(e, file=sys.stderr)
                if use_raw:
                    self.db["cvlac_stage_error"].insert_one(
                        {"url": url, "id_persona_pr": cvlac, "status_code": "from raw", "html": html, "exception": str(e)})
                else:
                    self.db["cvlac_stage_error"].insert_one(
                        {"url": url, "id_persona_pr": cvlac, "status_code": r.status_code, "html": html, "exception": str(e)})
                return False
            if use_raw is False:
                time.sleep(self.delay)
            return True

        return False  # if max_tries is reached then it fails

    def download_cvlac_profile(
        self,
        use_raw: bool = False,
        max_tries: int = 1,
        create_works: bool = True,
        works_collection: str = "cvlac_works",
        works_source_collection: str = "cvlac_stage_raw",
        works_replace: bool = True,
        works_batch_size: int = 500,
        works_max_title_group_size: int = 250,
    ):
        """
        Method to download cvlav profile information.
        This can take long time, but if something goes wrong we support checkpoint.

        Parameters:
        ------------
        use_raw:bool
            process data from raw html collection (previously dowloaded), default False
        """
        scienti_url = 'https://scienti.minciencias.gov.co/cvlac/visualizador/generarCurriculoCv.do?cod_rh='
        if "gruplac_production_data" not in self.db.list_collection_names():
            print("ERROR: gruplac_production_data not in the database, please download gruplac_production_data first https://github.com/colav/Yuku?tab=readme-ov-file#download-gruplac-groups-data")

        if "cvlac_data" not in self.db.list_collection_names():
            print("ERROR: cvlac_data not in the database, please download cvlac_data frist https://github.com/colav/yuku?tab=readme-ov-file#download-cvlac-data")
            return
        cod_rh_data_grup = self.db["gruplac_production_data"].distinct(
            "id_persona_pd")  # taking cod_rh from gruplac
        cod_rh_data_cvlac = self.db["cvlac_data"].distinct(
            "id_persona_pr")  # taking cod_rh from cvlac
        cod_rh_data = set(cod_rh_data_grup).union(set(cod_rh_data_cvlac))
        cod_rh_data = list(cod_rh_data)
        cod_rh_stage = self.db["cvlac_stage"].distinct("id_persona_pr")
        cod_rh_stage_priv = self.db["cvlac_stage_private"].distinct(
            "id_persona_pr")
        cod_rh_stage_empty = self.db["cvlac_stage_empty"].distinct(
            "id_persona_pr")

        # computing the remaining ids for scrapping
        cod_rh = set(cod_rh_data) - set(cod_rh_stage) - \
            set(cod_rh_stage_priv) - set(cod_rh_stage_empty)
        cod_rh = list(cod_rh)
        print(f"INFO: found {len(cod_rh_data)} records in cvlac_data and gruplac_production_data and \n      found {len(cod_rh_stage)} in stage\n      found {len(cod_rh)} remain records to download.")

        counter = 0
        count = len(cod_rh)
        for cvlac in cod_rh:
            if self.process_cvlac_profile(cvlac, scienti_url, counter, count, use_raw, max_tries):
                counter += 1
        print(f"INFO: Downloaded {counter} of {count}")

        if create_works:
            print("INFO: creating unified CVLAC works collection.")
            self.create_cvlac_works_collection(
                collection=works_collection,
                source_collection=works_source_collection,
                replace=works_replace,
                batch_size=works_batch_size,
                max_title_group_size=works_max_title_group_size,
            )

    def create_cvlac_works_collection(
        self,
        collection: str = "cvlac_works",
        source_collection: str = "cvlac_stage_raw",
        group_source_collection: str = None,
        authority_collection: str = "minciencias_bibliographic_authorities",
        profile_ids=None,
        replace: bool = True,
        batch_size: int = 500,
        max_title_group_size: int = 250,
    ):
        """Build graph-unified, dehydrated works from raw CVLAC profiles."""
        builder = CvlacWorkGraphBuilder(
            db=self.db,
            collection=collection,
            source_collection=source_collection,
            group_source_collection=group_source_collection,
            authority_collection=authority_collection,
            batch_size=batch_size,
            max_title_group_size=max_title_group_size,
        )
        summary = builder.build(
            profile_ids=(
                [str(profile_id) for profile_id in profile_ids]
                if profile_ids is not None
                else None
            ),
            replace=replace,
        )
        print(
            "INFO: CVLAC work graph completed: "
            f"profiles={summary['profiles']} nodes={summary['nodes']} "
            f"works={summary['works']} automatic_unions={summary['automatic_unions']} "
            f"review_edges={summary['review_edges']}."
        )
        return summary

    def load_bibliographic_authorities_json(
        self,
        path,
        collection: str = "minciencias_bibliographic_authorities",
    ):
        """Load verified, provenance-bearing authorship authorities."""
        with open(path, encoding="utf-8") as source:
            records = json.load(source)
        if not isinstance(records, list) or not records:
            raise ValueError("Bibliographic authorities JSON must be a non-empty array")
        destination = self.db[collection]
        processed = 0
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Every bibliographic authority must be an object")
            match = record.get("match")
            authors = record.get("authors")
            evidence = record.get("evidence")
            if not isinstance(match, dict) or not match.get("title") or not match.get("year"):
                raise ValueError("Authority match requires title and year")
            if not isinstance(authors, list) or not authors:
                raise ValueError("Authority requires at least one author")
            if not evidence:
                raise ValueError("Authority requires evidence provenance")
            if any(not isinstance(author, dict) or not author.get("full_name") for author in authors):
                raise ValueError("Every authority author requires full_name")
            identity = json.dumps(match, ensure_ascii=False, sort_keys=True)
            authority_id = str(record.get("_id") or hashlib.sha256(identity.encode("utf-8")).hexdigest())
            document = {
                **record,
                "_id": authority_id,
                "status": "verified",
            }
            destination.replace_one({"_id": authority_id}, document, upsert=True)
            processed += 1
        destination.create_index("status")
        destination.create_index("match.doi")
        destination.create_index("match.isbn")
        return {"authorities_loaded": processed, "collection": collection}

    def download_all_researchers_directory(
        self,
        collection: str = "all_researchers",
        pages_collection: str = "scienti_researcher_directory_pages",
        workers: int = 2,
        requests_per_second: float = 0.5,
        limit_pages: int = 0,
        refresh: bool = False,
    ):
        """Download the complete public researcher directory with checkpoints."""
        downloader = AllResearchersDirectory(
            self.db,
            workers=workers,
            requests_per_second=requests_per_second,
            pages_collection=pages_collection,
            output_collection=collection,
        )
        return downloader.download(limit_pages=limit_pages, refresh=refresh)

    def recognize_researchers_json(
        self,
        path,
        collection: str = "recognized_researchers",
        offset: int = 0,
        limit: int = 0,
        refresh: bool = False,
        delay: float = 0.75,
    ):
        """Resolve historical researcher JSON records against the Scienti app."""
        resolver = ScientiIdentifierResolver(self.db, delay=delay)
        try:
            return resolver.recognize_researchers(
                path,
                collection=collection,
                offset=offset,
                limit=limit,
                refresh=refresh,
            )
        finally:
            resolver.close()

    def recognize_groups_json(
        self,
        path,
        collection: str = "recognized_groups",
        offset: int = 0,
        limit: int = 0,
        refresh: bool = False,
        delay: float = 0.75,
    ):
        """Resolve historical group JSON records against the Scienti app."""
        resolver = ScientiIdentifierResolver(self.db, delay=delay)
        try:
            return resolver.recognize_groups(
                path,
                collection=collection,
                offset=offset,
                limit=limit,
                refresh=refresh,
            )
        finally:
            resolver.close()

    def download_scienti_cvlac_profiles(
        self,
        source_collections=None,
        raw_collection: str = "cvlac_stage_raw",
        workers: int = 4,
        requests_per_second: float = 2.0,
        limit: int = 0,
        refresh_days: int = None,
    ):
        """Download the union of known COD_RH values, idempotently."""
        source_collections = source_collections or [
            ("recognized_researchers", ("cod_rh", "consulta_scienti.coincidencias")),
            ("gruplac_production_data", ("id_persona_pd",)),
            ("cvlac_data", ("id_persona_pr",)),
            ("recognized_groups", ("cod_rh_lider",)),
            ("gruplac_related_works", ("members.cod_rh",)),
            ("all_researchers", ("cod_rh",)),
        ]
        if isinstance(source_collections, dict):
            source_collections = list(source_collections.items())
        existing = set(self.db.list_collection_names())
        identifiers = []
        seen_identifiers = set()
        for collection, fields in source_collections:
            if collection in existing:
                if isinstance(fields, str):
                    fields = (fields,)
                for field in fields:
                    for value in self.db[collection].distinct(field):
                        code = str(value or "").strip().zfill(10)
                        if code.isdigit() and len(code) == 10 and code not in seen_identifiers:
                            seen_identifiers.add(code)
                            identifiers.append(code)
        downloader = ScientiProfileDownloader(
            self.db,
            workers=workers,
            requests_per_second=requests_per_second,
        )
        return downloader.download(
            "cvlac",
            downloader.cvlac_targets(identifiers),
            raw_collection=raw_collection,
            limit=limit,
            refresh_days=refresh_days,
        )

    def evaluate_scienti_cvlac_profiles(
        self,
        run_name: str,
        reference_collection: str = "cvlac_stage_raw",
        new_count: int = 500,
        refresh_count: int = 500,
        seed: str = "yuku-cvlac-evaluation-v1",
        workers: int = 4,
        requests_per_second: float = 2.0,
    ):
        """Run an isolated, resumable download and work-graph evaluation."""
        evaluation = CvlacProfileEvaluation(
            self.db,
            run_name=run_name,
            reference_collection=reference_collection,
            new_count=new_count,
            refresh_count=refresh_count,
            seed=seed,
            workers=workers,
            requests_per_second=requests_per_second,
        )
        try:
            cohort = evaluation.prepare_cohort()
            print(
                f"INFO: evaluation {run_name}: cohort ready with {len(cohort)} profiles, "
                f"raw={evaluation.raw_collection} works={evaluation.works_collection}",
                flush=True,
            )
            download_counts = evaluation.download(cohort)
            download_summary = evaluation.summarize_download(cohort)
            profile_ids = self.db[evaluation.raw_collection].distinct("_id")
            graph_summary = self.create_cvlac_works_collection(
                collection=evaluation.works_collection,
                source_collection=evaluation.raw_collection,
                profile_ids=profile_ids,
                replace=True,
            )
            evaluation.complete(graph_summary)
            result = {
                "run_name": run_name,
                "cohort_collection": evaluation.cohort_collection,
                "raw_collection": evaluation.raw_collection,
                "works_collection": evaluation.works_collection,
                "download_counts": download_counts,
                "download_summary": download_summary,
                "graph_summary": graph_summary,
            }
            print(f"INFO: evaluation {run_name} complete: {result}", flush=True)
            return result
        except Exception as error:
            evaluation.fail(error)
            print(
                f"ERROR: evaluation {run_name} failed: {type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            raise

    def download_scienti_cvlac_priority_snapshot(
        self,
        run_name: str,
        reference_collection: str,
        raw_collection: str,
        workers: int = 4,
        requests_per_second: float = 2.0,
        max_passes: int = 3,
    ):
        """Freeze and download only strong, non-ambiguous priority COD_RH values."""
        snapshot = CvlacPrioritySnapshot(
            self.db,
            run_name=run_name,
            reference_collection=reference_collection,
            raw_collection=raw_collection,
            workers=workers,
            requests_per_second=requests_per_second,
            max_passes=max_passes,
        )
        try:
            return snapshot.run()
        except Exception as error:
            snapshot.fail(error)
            print(
                f"ERROR: priority CVLAC run {run_name} failed: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            raise

    def download_scienti_cvlac_full_snapshot(
        self,
        run_name: str,
        seed_collection: str,
        raw_collection: str,
        workers: int = 4,
        requests_per_second: float = 2.0,
        max_passes: int = 3,
        copy_seed: bool = True,
        fallback_requests_per_second: float = None,
        fallback_after_errors: int = 5,
        identifier_sources=None,
    ):
        """Download the directory plus strong identifiers, reusing a raw snapshot."""
        snapshot = CvlacPrioritySnapshot(
            self.db,
            run_name=run_name,
            reference_collection=seed_collection,
            raw_collection=raw_collection,
            workers=workers,
            requests_per_second=requests_per_second,
            max_passes=max_passes,
            identifier_sources=identifier_sources or FULL_SOURCES,
            copy_reference=copy_seed,
            fallback_requests_per_second=fallback_requests_per_second,
            fallback_after_errors=fallback_after_errors,
        )
        try:
            return snapshot.run()
        except Exception as error:
            snapshot.fail(error)
            print(
                f"ERROR: complete CVLAC run {run_name} failed: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            raise

    def create_scienti_full_graph(
        self,
        run_name: str,
        collection: str,
        gruplac_raw_collection: str,
        gruplac_related_collection: str,
        graph_run_name: str = "scienti_full_graph",
        cvlac_related_collection: str = "cvlac_related_works_full",
        cvlac_audit_name: str = "cvlac_normalization_audit",
        gruplac_audit_name: str = "gruplac_normalization_audit",
        recognized_groups_collection: str = "recognized_groups",
        authority_collection: str = "minciencias_bibliographic_authorities",
        batch_size: int = 500,
        max_title_group_size: int = 250,
        candidate_partitions: int = 64,
        publish_pointer: bool = True,
    ):
        """Build and atomically publish an audited, checkpointed full graph."""
        gate = validate_scienti_graph_gate(
            self.db,
            snapshot_run_name=run_name,
            cvlac_normalized_collection=cvlac_related_collection,
            cvlac_audit_name=cvlac_audit_name,
            recognized_groups_collection=recognized_groups_collection,
            gruplac_raw_collection=gruplac_raw_collection,
            gruplac_normalized_collection=gruplac_related_collection,
            gruplac_audit_name=gruplac_audit_name,
        )
        print(
            "INFO: full graph gate passed: "
            f"CVLAC={gate['cvlac_profiles']} GrupLAC={gate['gruplac_groups']} ",
            f"audits={cvlac_audit_name}, {gruplac_audit_name}.",
            flush=True,
        )
        builder = CheckpointedNormalizedWorkGraphBuilder(
            db=self.db,
            collection=collection,
            source_collection=cvlac_related_collection,
            group_source_collection=gruplac_related_collection,
            recognized_groups_collection=recognized_groups_collection,
            authority_collection=authority_collection,
            batch_size=batch_size,
            max_title_group_size=max_title_group_size,
            candidate_partitions=candidate_partitions,
            publish_pointer=publish_pointer,
            gate=gate,
        )
        return builder.build_checkpointed(graph_run_name)

    def normalize_minciencias_measured_products(
        self,
        *,
        run_name: str,
        source_collection: str = "gruplac_production_data",
        destination_collection: str = "minciencias_measured_products",
        batch_size: int = 500,
        progress_every: int = 10000,
        limit: int = 0,
        replace: bool = False,
    ):
        """Normalize official product measurements into Kahi-compatible works."""
        return MincienciasMeasurementPipeline(self.db).normalize(
            run_name=run_name,
            source_collection=source_collection,
            destination_collection=destination_collection,
            batch_size=batch_size,
            progress_every=progress_every,
            limit=limit,
            replace=replace,
        )

    def link_minciencias_measured_products(
        self,
        *,
        run_name: str,
        measured_collection: str = "minciencias_measured_products",
        graph_collection: str = "",
        links_collection: str = "minciencias_measured_product_links",
        target_entity: str = "works",
        batch_size: int = 500,
        progress_every: int = 10000,
        max_candidates: int = 100,
        limit: int = 0,
        replace: bool = False,
    ):
        """Link official measurements to the published scraped graph safely."""
        if not graph_collection:
            publication = self.db["scienti_work_graph_publications"].find_one(
                {"_id": "current"}
            ) or {}
            graph_collection = str(publication.get("current_collection") or "")
            if not graph_collection:
                raise ValueError("the current published Scienti graph was not found")
        return MincienciasMeasurementPipeline(self.db).link(
            run_name=run_name,
            measured_collection=measured_collection,
            graph_collection=graph_collection,
            links_collection=links_collection,
            target_entity=target_entity,
            batch_size=batch_size,
            progress_every=progress_every,
            max_candidates=max_candidates,
            limit=limit,
            replace=replace,
        )

    def materialize_minciencias_enriched_graph(
        self,
        *,
        run_name: str,
        target_collection: str,
        graph_collection: str = "",
        measured_collection: str = "minciencias_measured_products",
        links_collection: str = "minciencias_measured_product_links",
        target_entity: str = "works",
        batch_size: int = 500,
        progress_every: int = 10000,
        replace: bool = False,
        publish_pointer: bool = True,
    ):
        """Publish a lossless graph copy enriched with linked measurements."""
        if not graph_collection:
            publication = self.db["scienti_work_graph_publications"].find_one(
                {"_id": "current"}
            ) or {}
            graph_collection = str(publication.get("current_collection") or "")
            if not graph_collection:
                raise ValueError("the current published Scienti graph was not found")
        return MincienciasMeasurementPipeline(self.db).materialize(
            run_name=run_name,
            graph_collection=graph_collection,
            measured_collection=measured_collection,
            links_collection=links_collection,
            target_collection=target_collection,
            target_entity=target_entity,
            batch_size=batch_size,
            progress_every=progress_every,
            replace=replace,
            publish_pointer=publish_pointer,
        )

    def publish_scienti_final_release(
        self,
        *,
        release_name: str,
        collections: dict[str, str],
        materialization_runs: dict[str, str],
        audit_name: str = "",
    ):
        """Publish works, projects, patents and events as one audited release."""
        return ScientiFinalReleaseManager(self.db).publish(
            release_name=release_name,
            collections=collections,
            materialization_runs=materialization_runs,
            audit_name=audit_name,
        )

    def audit_scienti_bibliographic_enrichment(
        self,
        *,
        audit_name: str,
        run_name: str,
        base_collection: str,
        final_collection: str,
        materialization_run_name: str,
    ):
        """Audit final explicit book and publisher metadata before release."""
        return ScientiBibliographicEnrichmentAuditor(self.db).audit(
            audit_name=audit_name,
            run_name=run_name,
            base_collection=base_collection,
            final_collection=final_collection,
            materialization_run_name=materialization_run_name,
        )

    def cleanup_scienti_final_release(
        self,
        *,
        cleanup_name: str,
        release_name: str,
        candidates,
        protected=(),
        reset_entity_publication: bool = True,
    ):
        """Remove only explicit intermediates of the current final release."""
        return ScientiFinalReleaseManager(self.db).cleanup(
            cleanup_name=cleanup_name,
            release_name=release_name,
            candidates=candidates,
            protected=protected,
            reset_entity_publication=reset_entity_publication,
        )

    def compare_scienti_cvlac_evaluation(self, run_name: str):
        """Compare the refreshed half of an isolated CVLAC evaluation."""
        runs = self.db[CvlacProfileEvaluation.RUNS_COLLECTION]
        run = runs.find_one({"_id": run_name})
        if not run:
            raise ValueError(f"CVLAC evaluation {run_name!r} was not found")
        config = run.get("config") or {}
        output_collection = f"{run_name}_refresh_comparison"
        comparator = CvlacEvaluationComparator(
            self.db,
            run_name=run_name,
            reference_collection=config["reference_collection"],
            refreshed_collection=config["raw_collection"],
            cohort_collection=config["cohort_collection"],
            output_collection=output_collection,
        )
        try:
            runs.update_one(
                {"_id": run_name},
                {"$set": {"comparison_status": "running"}, "$unset": {"comparison_error": ""}},
            )
            summary = comparator.compare()
            runs.update_one(
                {"_id": run_name},
                {
                    "$set": {
                        "comparison_status": "complete",
                        "comparison_summary": summary,
                    }
                },
            )
            print(f"INFO: CVLAC refresh comparison completed: {summary}", flush=True)
            return summary
        except Exception as error:
            runs.update_one(
                {"_id": run_name},
                {
                    "$set": {
                        "comparison_status": "failed",
                        "comparison_error": f"{type(error).__name__}: {error}",
                    }
                },
            )
            raise

    def download_scienti_gruplac_profiles(
        self,
        source_collection: str = "recognized_groups",
        raw_collection: str = "gruplac_stage_raw",
        workers: int = 4,
        requests_per_second: float = 2.0,
        limit: int = 0,
        refresh_days: int = None,
        state_collection: str = "scienti_profile_downloads",
    ):
        """Download recognized GrupLAC pages without inferring group authorship."""
        if source_collection not in self.db.list_collection_names():
            raise ValueError(f"Collection {source_collection!r} was not found")
        cursor = self.db[source_collection].find(
            {"url_gruplac": {"$type": "string"}},
            {"codigo_grupo": 1, "group_code": 1, "url_gruplac": 1, "nro_gruplac": 1},
        )
        groups = (
            {
                **group,
                "nro": group.get("nro_gruplac", ""),
            }
            for group in cursor
        )
        downloader = ScientiProfileDownloader(
            self.db,
            workers=workers,
            requests_per_second=requests_per_second,
            state_collection=state_collection,
        )
        return downloader.download(
            "gruplac",
            downloader.gruplac_targets(groups),
            raw_collection=raw_collection,
            limit=limit,
            refresh_days=refresh_days,
        )

    def normalize_scienti_cvlac_snapshot(
        self,
        run_name: str,
        source_snapshot_run_name: str,
        destination_collection: str,
        source_state_collection: str = None,
        batch_size: int = 25,
        progress_every: int = 100,
        workers: int = 1,
    ):
        """Normalize a frozen complete CVLAC snapshot with safe resume semantics."""
        normalization = CvlacNormalizationRun(
            self.db,
            run_name=run_name,
            source_snapshot_run_name=source_snapshot_run_name,
            destination_collection=destination_collection,
            source_state_collection=source_state_collection,
            batch_size=batch_size,
            progress_every=progress_every,
            workers=workers,
        )
        return normalization.run()

    def audit_scienti_cvlac_normalization(
        self,
        audit_name: str,
        normalization_run_name: str,
        progress_every: int = 1000,
        anomaly_example_limit: int = 100,
    ):
        """Audit CVLAC normalized coverage, provenance and metadata invariants."""
        auditor = CvlacNormalizationAuditor(
            self.db,
            audit_name=audit_name,
            normalization_run_name=normalization_run_name,
            progress_every=progress_every,
            anomaly_example_limit=anomaly_example_limit,
        )
        return auditor.run()

    def audit_scienti_gruplac_normalization(
        self,
        audit_name: str,
        recognized_groups_collection: str,
        raw_collection: str,
        state_collection: str,
        normalized_collection: str,
        verification_run_name: str = None,
        anomaly_example_limit: int = 100,
    ):
        """Audit GrupLAC coverage, normalization and incomplete verification."""
        auditor = GruplacNormalizationAuditor(
            self.db,
            audit_name=audit_name,
            recognized_groups_collection=recognized_groups_collection,
            raw_collection=raw_collection,
            state_collection=state_collection,
            normalized_collection=normalized_collection,
            verification_run_name=verification_run_name,
            anomaly_example_limit=anomaly_example_limit,
        )
        return auditor.run()

    def normalize_scienti_gruplac_snapshot(
        self,
        run_name: str,
        source_raw_collection: str,
        source_state_collection: str,
        destination_collection: str,
        workers: int = 1,
        progress_every: int = 50,
    ):
        """Normalize frozen GrupLAC HTML with versioned, parallel safe resume."""
        normalization = GruplacNormalizationRun(
            self.db,
            run_name=run_name,
            source_raw_collection=source_raw_collection,
            source_state_collection=source_state_collection,
            destination_collection=destination_collection,
            workers=workers,
            progress_every=progress_every,
        )
        return normalization.run()

    def normalize_scienti_entities(
        self,
        run_name: str,
        cvlac_collection: str,
        gruplac_collection: str,
        works_collection: str,
        projects_collection: str,
        patents_collection: str,
        events_collection: str,
        batch_size: int = 500,
        progress_every: int = 1000,
        replace: bool = False,
        limit_sources: int = 0,
    ):
        """Publish four exact-routed, Kahi-compatible Scienti entities."""
        normalization = ScientiEntityNormalizationRun(
            self.db,
            run_name=run_name,
            cvlac_collection=cvlac_collection,
            gruplac_collection=gruplac_collection,
            destinations={
                "works": works_collection,
                "projects": projects_collection,
                "patents": patents_collection,
                "events": events_collection,
            },
            batch_size=batch_size,
            progress_every=progress_every,
            replace=replace,
            limit_sources=limit_sources,
        )
        return normalization.run()

    def create_scienti_entity_graph(
        self,
        entity: str,
        run_name: str,
        source_collection: str,
        target_collection: str,
        entity_run_name: str = "",
        batch_size: int = 1000,
        progress_every: int = 10000,
        max_candidate_group: int = 100,
        replace: bool = False,
    ):
        """Build a conservative exact graph for projects or patents."""
        return ScientiExactEntityGraphBuilder(
            self.db,
            entity=entity,
            run_name=run_name,
            source_collection=source_collection,
            target_collection=target_collection,
            entity_run_name=entity_run_name,
            batch_size=batch_size,
            progress_every=progress_every,
            max_candidate_group=max_candidate_group,
            replace=replace,
        ).run()

    def materialize_scienti_entity_snapshot(
        self,
        entity: str,
        run_name: str,
        source_collection: str,
        target_collection: str,
        entity_run_name: str,
    ):
        """Atomically materialize an audited entity that needs no graph."""
        return ScientiEntitySnapshotMaterializer(
            self.db,
            entity=entity,
            run_name=run_name,
            source_collection=source_collection,
            target_collection=target_collection,
            entity_run_name=entity_run_name,
        ).run()

    def audit_scienti_projects_semantics(
        self,
        audit_name: str,
        collection: str,
        entity_run_name: str,
        progress_every: int = 10000,
        batch_size: int = 2000,
        example_limit: int = 100,
    ):
        """Audit every normalized project without changing source records."""
        audit = ScientiProjectSemanticAudit(
            self.db,
            audit_name=audit_name,
            collection=collection,
            entity_run_name=entity_run_name,
            progress_every=progress_every,
            batch_size=batch_size,
            example_limit=example_limit,
        )
        return audit.run()

    def compare_scienti_entity_versions(
        self,
        comparison_name: str,
        old_run_name: str,
        new_run_name: str,
        progress_every: int = 10000,
        batch_size: int = 1000,
        example_limit: int = 100,
    ):
        """Exhaustively compare two completed strict entity normalizations."""
        comparison = ScientiEntityVersionComparator(
            self.db,
            comparison_name=comparison_name,
            old_run_name=old_run_name,
            new_run_name=new_run_name,
            progress_every=progress_every,
            batch_size=batch_size,
            example_limit=example_limit,
        )
        return comparison.run()

    def publish_scienti_entities(
        self,
        run_name: str,
        comparison_name: str,
        project_audit_name: str,
        works_audit_name: str,
        patents_audit_name: str,
        events_audit_name: str,
        allow_snapshot_without_comparison: bool = False,
    ):
        """Atomically promote an entity run after all immutable gates pass."""
        return ScientiEntityPublisher(
            self.db,
            run_name=run_name,
            comparison_name=comparison_name,
            project_audit_name=project_audit_name,
            works_audit_name=works_audit_name,
            patents_audit_name=patents_audit_name,
            events_audit_name=events_audit_name,
            allow_snapshot_without_comparison=allow_snapshot_without_comparison,
        ).publish()

    def audit_scienti_auxiliary_semantics(
        self,
        audit_name: str,
        entity: str,
        collection: str,
        entity_run_name: str,
        progress_every: int = 10000,
        batch_size: int = 2000,
        example_limit: int = 100,
    ):
        """Audit normalized theses, patents or events with specialized rules."""
        audit = ScientiAuxiliarySemanticAudit(
            self.db,
            audit_name=audit_name,
            entity=entity,
            collection=collection,
            entity_run_name=entity_run_name,
            progress_every=progress_every,
            batch_size=batch_size,
            example_limit=example_limit,
        )
        return audit.run()

    def verify_scienti_gruplac_incomplete(
        self,
        run_name: str,
        source_raw_collection: str,
        source_state_collection: str,
        normalized_collection: str,
        workers: int = 1,
        requests_per_second: float = 0.25,
    ):
        """Recheck only frozen incomplete GrupLAC pages and promote improvements."""
        verifier = GruplacIncompleteVerifier(
            self.db,
            run_name=run_name,
            source_raw_collection=source_raw_collection,
            source_state_collection=source_state_collection,
            normalized_collection=normalized_collection,
            workers=workers,
            requests_per_second=requests_per_second,
        )
        return verifier.run()

    def create_gruplac_related_works_collection(
        self,
        collection: str = "gruplac_related_works",
        source_collection: str = "gruplac_stage_raw",
        group_codes=None,
        replace: bool = True,
    ):
        """Normalize raw GrupLAC pages into group metadata and work occurrences."""
        if source_collection not in self.db.list_collection_names():
            raise ValueError(f"Collection {source_collection!r} was not found")
        query = {}
        if group_codes is not None:
            query = {"_id": {"$in": [str(code).upper() for code in group_codes]}}
        destination = self.db[collection]
        processed = 0
        production = 0
        for raw in self.db[source_collection].find(query, {"html": 1, "url": 1, "nro": 1}):
            document = normalize_gruplac_document(
                str(raw["_id"]),
                raw.get("html", ""),
                nro=str(raw.get("nro") or ""),
                url=str(raw.get("url") or ""),
            )
            if replace:
                destination.replace_one({"_id": document["_id"]}, document, upsert=True)
            else:
                destination.update_one(
                    {"_id": document["_id"]}, {"$setOnInsert": document}, upsert=True
                )
            processed += 1
            production += document["production_count"]
        destination.create_index("group_code", unique=True)
        destination.create_index("production.doi")
        destination.create_index("production.isbn")
        return {"groups": processed, "production": production}

    def create_cvlac_related_works_collection(
        self,
        collection: str = "cvlac_related_works",
        source_collection: str = "cvlac_stage_raw",
        profile_ids=None,
        download_missing: bool = False,
        replace: bool = True,
        max_tries: int = 1,
        resume: bool = False,
    ):
        """
        Create a normalized CVLAC related works collection.

        Each document is keyed by id_persona_pr and includes url_persona,
        production, patents, events and projects. This first version extracts
        production and patents from the CVLAC HTML stored in cvlac_stage_raw.

        Parameters:
        ------------
        collection:str
            destination MongoDB collection, default cvlac_related_works.
        source_collection:str
            source collection with raw CVLAC HTML documents, default cvlac_stage_raw.
        profile_ids:list|None
            optional list of CVLAC ids to process. When omitted, all ids in
            source_collection are processed.
        download_missing:bool
            download profiles missing in source_collection before normalizing.
        replace:bool
            replace documents in destination collection when they already exist.
        max_tries:int
            max retries for missing profile downloads.
        """
        if profile_ids is None:
            if source_collection not in self.db.list_collection_names():
                print(f"ERROR: {source_collection} not in the database.")
                return
            profile_ids = self.db[source_collection].distinct("_id")
        else:
            profile_ids = [str(profile_id) for profile_id in profile_ids]

        dst = self.db[collection]
        src = self.db[source_collection]
        processed = 0
        skipped = 0
        errors = 0
        count = len(profile_ids)
        completed_ids = set(dst.distinct("_id")) if resume else set()
        error_collection = self.db[f"{collection}_errors"]

        for position, profile_id in enumerate(profile_ids, start=1):
            if profile_id in completed_ids:
                skipped += 1
                continue
            if position == 1 or position % 100 == 0:
                print(
                    f"INFO: CVLAC normalization position={position}/{count} "
                    f"processed={processed} skipped={skipped} errors={errors}.",
                    flush=True,
                )

            raw = src.find_one({"_id": profile_id}, {"html": 1})
            html = raw.get("html", "") if raw else ""
            if not html and download_missing:
                html = self._download_cvlac_html(profile_id, max_tries=max_tries)
                if html:
                    src.replace_one({"_id": profile_id}, {"_id": profile_id, "html": html}, upsert=True)

            if not html:
                print(f"WARNING: not found raw CVLAC HTML for id {profile_id}")
                errors += 1
                error_collection.replace_one(
                    {"_id": profile_id},
                    {"_id": profile_id, "error": "raw_html_not_found"},
                    upsert=True,
                )
                continue

            try:
                document = normalize_related_works_document(profile_id, html)
                if replace:
                    dst.replace_one({"_id": profile_id}, document, upsert=True)
                else:
                    dst.update_one(
                        {"_id": profile_id}, {"$setOnInsert": document}, upsert=True
                    )
                error_collection.delete_one({"_id": profile_id})
                processed += 1
            except Exception as error:
                errors += 1
                error_collection.replace_one(
                    {"_id": profile_id},
                    {
                        "_id": profile_id,
                        "error": f"{type(error).__name__}: {error}",
                    },
                    upsert=True,
                )
                print(
                    f"ERROR: CVLAC normalization failed for {profile_id}: "
                    f"{type(error).__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )

        summary = {
            "source_profiles": count,
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
            "destination_profiles": dst.count_documents({}),
            "collection": collection,
        }
        print(f"INFO: CVLAC normalization completed: {summary}", flush=True)
        return summary

    def _download_cvlac_html(self, profile_id: str, max_tries: int = 1):
        url = f"{SCIENTI_CVLAC_URL}{profile_id}"
        for current_try in range(max_tries):
            if current_try > 0:
                print(f"INFO: retrying {current_try} {url}")
            try:
                response = requests.get(url, verify=False)
            except Exception as e:
                print(e, file=sys.stderr)
                self.db["cvlac_stage_error"].insert_one(
                    {
                        "url": url,
                        "id_persona_pr": profile_id,
                        "status_code": "unknown",
                        "html": "",
                        "exception": str(e),
                    }
                )
                continue

            if response.status_code == 200:
                time.sleep(self.delay)
                return response.text

            print(f"Error processing id {profile_id} with url = {url} status code = {response.status_code}")
            self.db["cvlac_stage_error"].insert_one(
                {
                    "url": url,
                    "id_persona_pr": profile_id,
                    "status_code": response.status_code,
                    "html": response.text,
                }
            )
            time.sleep(self.delay)
        return ""

    def cvlac_related_works_type_impactu_distribution(
        self,
        collection: str = "cvlac_related_works",
        output_collection: str = None,
    ):
        """
        Compute the type_impactu distribution for a normalized CVLAC related
        works collection.

        The summary is grouped globally by type_impactu across production,
        patents, events and projects. For each type it reports the number of
        records and the number of distinct CVLAC authors (id_persona_pr) with
        at least one record in that type. The returned object also includes the
        total number of processed authors in the source collection.

        Parameters:
        ------------
        collection:str
            source normalized related works collection.
        output_collection:str|None
            optional collection where the summary will be stored.
        """
        if collection not in self.db.list_collection_names():
            print(f"ERROR: {collection} not in the database.")
            return []

        summary = {}
        processed_authors = set()
        entities = ["production", "patents", "events", "projects"]
        for document in self.db[collection].find({}, {"id_persona_pr": 1, **{entity: 1 for entity in entities}}):
            profile_id = document.get("id_persona_pr") or document.get("_id")
            if profile_id:
                processed_authors.add(profile_id)
            for entity in entities:
                for record in document.get(entity, []):
                    type_impactu = record.get("type_impactu") or "Sin tipo ImpactU"
                    key = type_impactu
                    if key not in summary:
                        summary[key] = {
                            "type_impactu": type_impactu,
                            "records_count": 0,
                            "authors": set(),
                        }
                    summary[key]["records_count"] += 1
                    if profile_id:
                        summary[key]["authors"].add(profile_id)

        records = []
        for item in summary.values():
            records.append(
                {
                    "type_impactu": item["type_impactu"],
                    "records_count": item["records_count"],
                    "authors_count": len(item["authors"]),
                }
            )
        records.sort(key=lambda item: (-item["records_count"], item["type_impactu"]))

        if output_collection:
            self.db[output_collection].drop()
            output_records = [
                {
                    "_id": "__summary__",
                    "authors_processed": len(processed_authors),
                    "types_count": len(records),
                    "records_count": sum(record["records_count"] for record in records),
                }
            ] + records
            if output_records:
                self.db[output_collection].insert_many(output_records)
            print(f"INFO: saved {len(records)} type_impactu distribution records into {output_collection}.")

        print(f"authors_processed={len(processed_authors)}")
        for record in records:
            print(
                f"{record['type_impactu']}: {record['records_count']} "
                f"(authors={record['authors_count']})"
            )
        return {
            "authors_processed": len(processed_authors),
            "distribution": records,
        }

    def download_gruplac_production(self, dataset_id: str):
        """
        Method to download gruplac production information.
        Unfortunately we dont have support for checkpoint in this method.

        Parameters:
        ------------
        dataset_id:str
            id for dataset in socrata ex: 33dq-ab5a
        """
        if "gruplac_production_dataset_info" in self.db.list_collection_names():
            print("WARNING: gruplac_production_dataset_info already in the database, it wont be downloaded again, drop the database if you want start over.")
        else:
            print(f"INFO: downloading dataset metadata from id {dataset_id}")
            dataset_info = self.client.get_metadata(dataset_id)
            self.db["gruplac_production_dataset_info"].insert_one(dataset_info)
        if "gruplac_production_data" in self.db.list_collection_names():
            print("WARNING: gruplac_production_data already in the database, it wont be downloaded again, drop the database if you want start over.")
        else:
            dataset = self.db["gruplac_production_dataset_info"].find_one()
            self.db["gruplac_production_data_cache"].drop()
            cursor = self.client.get_all(dataset_id)
            data = []
            count = int(dataset['columns'][0]['cachedContents']['count'])
            print(f"INFO: Total group products found = {count}.")
            counter = 1
            for i in cursor:
                if counter % 20000 == 0:
                    print(f"INFO: downloaded {counter} of {count}")
                    self.db["gruplac_production_data_cache"].insert_many(data)
                    data = []
                data.append(i)

                counter += 1

            self.db["gruplac_production_data_cache"].insert_many(data)
            print(f"INFO: downloaded {counter} of {count}")
            self.db["gruplac_production_data_cache"].rename(
                "gruplac_production_data")

    def download_gruplac_groups(self, dataset_id: str):
        """
        Method to download gruplac groups information.
        Unfortunately we dont have support for checkpoint in this method.

        Parameters:
        ------------
        dataset_id:str
            id for dataset in socrata ex: 33dq-ab5a
        """
        if "gruplac_groups_dataset_info" in self.db.list_collection_names():
            print("WARNING: gruplac_groups_dataset_info already in the database, it wont be downloaded again, drop the database if you want start over.")
        else:
            print(f"INFO: downloading dataset metadata from id {dataset_id}")
            dataset_info = self.client.get_metadata(dataset_id)
            self.db["gruplac_groups_dataset_info"].insert_one(dataset_info)
        if "gruplac_groups_data" in self.db.list_collection_names():
            print("WARNING: gruplac_groups_data already in the database, it wont be downloaded again, drop the database if you want start over.")
        else:
            cursor = self.client.get_all(dataset_id)
            data = list(cursor)
            self.db["gruplac_groups_data"].insert_many(data)

    def download(self, dataset_id: str, collection: str):
        """
        Method to download any dataset information/data.
        Unfortunately we dont have support for checkpoint in this method.
        This is a generic one, if the dataset is too big can take long time.

        WARNING:  USE THIS METHOD WITH CAUTION, THIS IS NOT AN OPTIMIZED REQUEST, DOWNLOADED DATA IS SAVED IN RAM BEFORE SAVE IT IN MONGODB.

        Parameters:
        ------------
        dataset_id:str
            id for dataset in socrata ex: 33dq-ab5a
        collection:str
            name of the collection prefix to save dataset
        """
        if f"{collection}_dataset_info" in self.db.list_collection_names():
            print(
                f"WARNING: {collection} already in the database, it wont be downloaded again, drop the collections if you want start over.")
        else:
            print(f"INFO: downloading dataset metadata from id {dataset_id}")
            dataset_info = self.client.get_metadata(dataset_id)
            self.db[f"{collection}_dataset_info"].insert_one(dataset_info)
        if f"{collection}_data" in self.db.list_collection_names():
            print(
                "WARNING: {collection}_data already in the database, it wont be downloaded again, drop the collections if you want start over.")
        else:
            cursor = self.client.get_all(dataset_id)
            data = list(cursor)
            self.db[f"{collection}_data"].insert_many(data)

    def search(self, q: str, limit: int = 5):
        """
        Method to search datasets in socrata for the endpoint www.datos.gov.co

        examples:
        * q="Investigadores Reconocidos por convocatoria"
        * q="Producción Grupos Investigación"
        * q="Grupos de Investigación Reconocidos"

        Parameters:
        -----------
        q:str
            Elastic search query, results of datasets are besed on similarity
        limit:int
            number of results to display, default firts 5 elements.
        """
        datasets = self.client.datasets(
            q=q, public=True)  # busca en elastic search con query "q"
        for dataset in datasets[0:limit]:
            print("name: ", dataset["resource"]["name"])
            print("id: ", dataset["resource"]["id"])
            print("description: ", dataset["resource"]["description"])
            print("attribution: ", dataset["resource"]["attribution"])
            print("attribution_link: ",
                  dataset["resource"]["attribution_link"])
            print("type: ", dataset["resource"]["type"])
            print("updatedAt: ", dataset["resource"]["updatedAt"])
            print("createdAt: ", dataset["resource"]["createdAt"])
            print('\n\n')
        return datasets[0:limit]
