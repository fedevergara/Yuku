import requests
import pandas as pd
from pymongo import MongoClient
from sodapy import Socrata
from bs4 import BeautifulSoup
import time
import urllib3
import sys
from io import StringIO


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
                    print(f"WARNING: not found id {cvlac}  in raw collection ")
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
                        f"Error processing id {cvlac}  with url = {url} status code = {r.status_code} ")
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
                            f"WARNING: found empty id {cvlac}  with url = {url} ")
                        self.db["cvlac_stage_empty"].insert_one(reg)
                        continue
                    record['datos_generales'] = pd.read_html(StringIO(table_tag.decode()))[
                        0].to_dict(orient='records')
            except Exception as e:
                print(f"Error processing id {cvlac}  with url = {url} ")
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
                        f"WARNING: found private id {cvlac}  with url = {url} ")
                    self.db["cvlac_stage_private"].insert_one(reg)
                    self.db["cvlac_stage_raw"].insert_one(
                        {"_id": cvlac, "html": html})
                    time.sleep(self.delay)
                    counter += 1
                    return True
                    continue
            except Exception as e:
                print(f"Error processing id {cvlac}  with url = {url} ")
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
                print(f"Error processing id {cvlac}  with url = {url} ")
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

    def download_cvlac_profile(self, use_raw: bool = False, max_tries: int = 1):
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
