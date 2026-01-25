<center><img src="https://raw.githubusercontent.com/colav/colav.github.io/master/img/Logo.png"/></center>

# Yuku
Scienti Open Data / Yuku, god of rain in Yaqui mythology in northern Mexico.


# Description
This package allows to download Scienti open data using socrata api service.
We are downloading the next datasets 
* "Investigadores Reconocidos por convocatoria"
* "Producción Grupos Investigación"
* "Grupos de Investigación Reconocidos"

Additionally we are scrapping cvlac profiles of researches from scienti website.

All the data is saved in MongoDB.

# Installation

## Dependencies
* Install MongoDB
    * Debian based system: `apt-get install mongodb`
    * Redhat based system instructions [here](https://docs.mongodb.com/manual/tutorial/install-mongodb-on-red-hat/)
    * Conda: `conda install mongodb mongo-tools`

NOTE:

To start mongodb server on conda please run the next steps

`
mkdir -p $HOME/data/db 
`

`
mongodb mongod --dbpath $HOME/data/db/
`

## Package
`pip install yuku`

# Usage
## Searching for required datasets IDs
Example to get dataset id for researchers

`
yuku_run --search "Investigadores Reconocidos por convocatoria" --search_limit 3
`

Output is like next example, where you can take the required dateset ID ex: bqtm-4y2h

```
WARNING:root:Requests made without an app_token will be subject to strict throttling limits.
name:  Investigadores Reconocidos por convocatoria
id:  bqtm-4y2h
description:  Investigadores reconocidos por convocatoria a través de la Plataforma ScienTI - Colombia.
attribution:  Ministerio de Ciencia y Tecnología e Innovación
attribution_link:  https://www.minciencias.gov.co
type:  dataset
updatedAt:  2022-09-25T05:33:58.000Z
createdAt:  2021-07-23T20:17:20.000Z

name:  Investigadores Reconocidos por convocatoria 2019
id:  izwp-q8gg
description:  Investigadores reconocidos por convocatoria a través de la Plataforma ScienTI - Colombia.
attribution:  Ministerio de Ciencia y Tecnología e Innovación
attribution_link:  https://www.minciencias.gov.co
type:  chart
updatedAt:  2022-09-25T05:35:54.000Z
createdAt:  2021-07-27T04:57:43.000Z

name:  Investigadores Reconocidos por convocatoria 2021
id:  gzff-pwwc
description:  Investigadores reconocidos por convocatoria a través de la Plataforma ScienTI - Colombia.
attribution:  Ministerio de Ciencia y Tecnología e Innovación
attribution_link:  https://www.minciencias.gov.co
type:  chart
updatedAt:  2022-09-25T05:43:29.000Z
createdAt:  2022-09-25T05:40:12.000Z

```

Example to get dataset id for groups production, in this example I took ID ex: 33dq-ab5a

`
yuku_run --search "Producción Grupos Investigación" --search_limit 3
`

Output:

```
WARNING:root:Requests made without an app_token will be subject to strict throttling limits.
name:  Producción Grupos Investigación
id:  33dq-ab5a
description:  Producción revisada y evaluada con la cual participó el grupo de investigación de acuerdo con la ventana de observación para la convocatoria
attribution:  Ministerio de Ciencia, Tecnología e Innovación
attribution_link:  https://www.minciencias.gov.co
type:  dataset
updatedAt:  2022-10-13T18:28:46.000Z
createdAt:  2021-07-26T07:14:22.000Z

name:  Producción Grupos Investigación 2019
id:  cpuy-2qxm
description:  Producción revisada y evaluada con la cual participó el grupo de investigación de acuerdo con la ventana de observación para la convocatoria
attribution:  Ministerio de Ciencia, Tecnología e Innovación
attribution_link:  https://www.minciencias.gov.co
type:  chart
updatedAt:  2022-10-13T18:23:02.000Z
createdAt:  2021-07-27T02:20:39.000Z

name:  Producción Grupos Investigación 2021
id:  bs69-ze7w
description:  Producción revisada y evaluada con la cual participó el grupo de investigación de acuerdo con la ventana de observación para la convocatoria
attribution:  Ministerio de Ciencia, Tecnología e Innovación
attribution_link:  https://www.minciencias.gov.co
type:  chart
updatedAt:  2022-10-13T17:48:49.000Z
createdAt:  2022-10-13T17:41:10.000Z

```


Example to get dataset id for groups, in this example I took ID ex: hrhc-c4wu

`
yuku_run --search "Grupos de Investigación Reconocidos" --search_limit 3
`

Output:

```
WARNING:root:Requests made without an app_token will be subject to strict throttling limits.
name:  Grupos de Investigación Reconocidos
id:  hrhc-c4wu
description:  Información de los grupos de investigación registrados en la Plataforma ScienTI - Colombia, avalados por una Institución, reconocidos y clasificados.
attribution:  Ministerio de Ciencia y Tecnología e Innovación
attribution_link:  https://www.minciencias.gov.co
type:  dataset
updatedAt:  2022-09-25T05:58:20.000Z
createdAt:  2021-07-23T03:21:48.000Z

name:  Grupos de Investigación reconocidos 2019
id:  92tk-xn3q
description:  Información de los grupos de investigación registrados en la Plataforma ScienTI - Colombia, avalados por una Institución, reconocidos y clasificados.
attribution:  Ministerio de Ciencia y Tecnología e Innovación
attribution_link:  https://www.minciencias.gov.co
type:  chart
updatedAt:  2022-09-25T06:00:48.000Z
createdAt:  2021-07-27T02:45:43.000Z

name:  Grupos de Investigación reconocidos 2021
id:  b5ub-mixn
description:  Información de los grupos de investigación registrados en la Plataforma ScienTI - Colombia, avalados por una Institución, reconocidos y clasificados.
attribution:  Ministerio de Ciencia y Tecnología e Innovación
attribution_link:  https://www.minciencias.gov.co
type:  chart
updatedAt:  2022-09-25T06:03:19.000Z
createdAt:  2022-09-25T06:03:10.000Z
```


## Download CVLAC data

The cvlac download supports checkpoints, it takes long time to download the profiles, about 9 hours.

`
yuku_run --download_cvlac bqtm-4y2h
`

## Download GRUPLAC production data

The gruplac download dont supports checkpoints, but it support pagination, the cache is saved in the collection gruplac_production_data_cache, but it is eventually removed if the execution fails.  This run takes about 1 hour.
`
yuku_run --download_gruplac_production 33dq-ab5a
`

## Download GRUPLAC groups data

The gruplac download dont supports checkpoints, but it support pagination, the cache is saved in the collection gruplac_data_cache, but it is eventually removed if the execution fails.  This run takes about 1 hour.
`
yuku_run --download_gruplac_groups hrhc-c4wu
`

# Yuku Results

By default the data is saved in the database **yuku** with the next collections:
```
yuku> show collections
cvlac_data
cvlac_dataset_info
cvlac_stage
cvlac_stage_empty
cvlac_stage_private
cvlac_stage_raw
gruplac_groups_data
gruplac_groups_dataset_info
gruplac_production_data
gruplac_production_dataset_info
```

* cvlav_data: is the dataset for cvlac, downloaded from socrata (www.datos.gov.co)
* cvlac_dataset_info: information about the dataset, this explains the fields and provide metadata about the cvlac dataset.
* cvlac_stage: this is the collection for the scrapped data from cvlac (minciencias web site).
* cvlac_stage_empty: Empty pages, not possible to do any scrapping
* cvlac_stage_private: private profiles but still some basic information
* cvlac_stage_raw: raw html text
* gruplac_groups_data: is the dataset for gruplac groups, downloaded from socrata (www.datos.gov.co)
* gruplac_groups_dataset_info: information about the dataset, this explains the fields and provide metadata about the gruplac groups dataset.
* gruplac_production_data: is the dataset for gruplac production, downloaded from socrata (www.datos.gov.co)
* gruplac_production_dataset_info: information about the dataset, this explains the fields and provide metadata about the gruplac production dataset.

# License
BSD-3-Clause License 

# Links
http://colav.udea.edu.co/




## Soporte: Publindex


Para descargar el dataset "Revistas Indexadas — Índice Nacional Publindex" (resource id `mwmn-inyg`) se agregó soporte en la clase `Yuku` y en el script `bin/yuku_run`.


Uso desde terminal (almacena en MongoDB usando `download`):

```
bin/yuku_run --download_publindex
```

Esto guardará las colecciones `publindex_dataset_info` y `publindex_data` en la base de datos configurada.

O desde Python, reutilizando la clase existente:

```
from yuku.Yuku import Yuku
y = Yuku()
y.download('mwmn-inyg', 'publindex')
```

Para crear una rama git localmente y trabajar sobre ella:

```
git checkout -b publindex
```



