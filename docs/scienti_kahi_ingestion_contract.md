# Contrato de ingestión ScienTI de Yuku a Kahi

Versión: `scienti-kahi-ingestion-v1`
Contrato canónico: `docs/scienti_kahi_ingestion_contract_v1.json`

## Alcance de esta versión

Este contrato fija la entrada de desarrollo y los materializadores auditados
de afiliaciones y personas; los consumidores Kahi se implementarán después.
Kahi debe leer DAM sin modificarla y
tratar las cuatro colecciones publicadas como entidades pre-normalizadas. No
debe volver a interpretarlas como filas de `gruplac_production_data`.

La release se selecciona por su identificador inmutable
`scienti_final_release_publishers_v11_20260902`, no mediante el alias dinámico
`current`. Antes de leer datos se deben comprobar su documento de publicación,
su auditoría `passed`, cero anomalías críticas, las cuatro colecciones y sus
conteos. La selección se resuelve una sola vez por ejecución.

## Colecciones fijadas

| Entidad | Colección | Documentos |
|---|---|---:|
| works | `scienti_works_final_publishers_v11_20260902` | 3.837.870 |
| projects | `scienti_projects_final_v5_20260901` | 642.519 |
| patents | `scienti_patents_final_v5_20260901` | 9.789 |
| events | `scienti_events_final_v5_20260901` | 1.466.152 |

El nombre v11 corresponde a la release conjunta; proyectos, patentes y eventos
continúan en sus materializaciones v5 dentro de esa misma publicación.

## Frontera de responsabilidades

- Yuku descarga, normaliza, construye grafos, conserva evidencia, audita y
  publica snapshots inmutables.
- Kahi proyecta campos de consumo, resuelve referencias contra sus entidades
  canónicas y fusiona después metadatos de otras fuentes.
- La evidencia exhaustiva `source_metadata` permanece en DAM. No se copia a
  proyectos, patentes o eventos de Kahi; reduce su tamaño lógico estimado entre
  57 % y 65 % sin eliminar campos del esquema Kahi.
- Ninguna entidad importada agrega campos de nivel superior al esquema actual
  de Kahi. `authorship_status` y `source_metadata` permanecen como evidencia en
  DAM y no se copian a `works`.
- Works reutiliza el campo existente `bibliographic_info`, con la estructura
  observada en `kahi_dev_062026.works` y la misma semántica de enriquecimiento
  de los plugins actuales: incorporar únicamente valores no vacíos que falten,
  preservar los existentes y alojar la evidencia ScienTI dentro de
  `bibliographic_info.minciencias`.
- El documento de ejecución de Kahi debe registrar contrato, release, auditoría,
  colección exacta y conteo observado para reproducibilidad.

## Resolución de referencias

Los `authors.id` no vacíos de las colecciones finales son `COD_RH`, no `_id` de
Kahi. Se resuelven únicamente contra `person.external_ids.id.COD_RH`. Un nombre
sin identificador conserva `id: ""`; está prohibido asignarle una persona solo
por nombre.

Los `groups.id` son códigos `COL...` y se resuelven mediante
`affiliations.external_ids.id`. La evidencia de afiliación incluida en works
(`institution`, `group_code`, periodo y pertenencia) no tiene todavía la forma
de una afiliación Kahi: primero se resuelve el grupo y luego una institución
única mediante coincidencia exacta o similitud estricta.

Una referencia no vacía que no pueda resolverse es una anomalía crítica. No se
crean personas o grupos silenciosamente durante la ingestión de productos.

## Materializador de personas

Yuku publica `scienti_persons_final_<run>` desde los 437.606 `COD_RH` válidos
del manifiesto CVLAC congelado y añade los `COD_RH` válidos evidenciados por el
snapshot GrupLAC auditado. Así conserva miembros de grupos que no tienen perfil
CVLAC descargado. Consolidará directorio, datos abiertos, convocatorias, perfil
CvLAC, membresías GrupLAC y autores de las cuatro entidades.
La salida conservará nombres y alias, identificadores externos validados,
ranking histórico, áreas, formación, experiencia y afiliaciones explícitas.

`related_works` será una estructura compacta exclusivamente para identidad:
un elemento por DOI canónico y persona, con `provenance`, `source: "doi"`, `id`
y opcionalmente `author_count`. No se guardarán productos sin identificador,
títulos ni metadatos completos. DOI incompletos o inválidos se excluyen y se
reportan como calidad de la fuente, sin convertirse en evidencia de identidad.
Los documentos nacionales quedan excluidos por defecto y requerirán una decisión
explícita de privacidad.

El materializador `scienti-persons-v1` exige snapshots CvLAC y GrupLAC
auditados con parser `3.2.0`, el snapshot publicado de afiliaciones y una
release final explícita de works/projects/patents/events. Recalcula el SHA-256
del manifiesto, valida conteos antes de leer la release, registra checkpoints y
rechaza cualquier `authors.id` no vacío que no sea un `COD_RH` del manifiesto.
La normalización CvLAC conserva datos generales, identificadores públicos,
formación y experiencia explícita; la proyección usa únicamente campos ya
existentes en `person`.

## Materializador de afiliaciones

Yuku publicará `scienti_affiliations_final_<run>` a partir de la unión de
`recognized_groups` y `gruplac_groups_data`, cuyo baseline es 9.537 grupos.
Consolidará nombres, creación, ubicación, estado, URL, áreas, programas,
clasificaciones históricas, instituciones avaladoras y membresías GrupLAC.

El materializador `scienti-affiliations-v1` complementa esa unión con el
snapshot GrupLAC normalizado y auditado. Conserva instituciones avaladoras en
`relations` con `id: ""`: son evidencia para que Kahi resuelva el identificador,
no relaciones canónicas inventadas. La salida usa exclusivamente campos ya
existentes en `affiliations`.

Yuku agrupa evidencia institucional por nombre normalizado, pero no inventa un
`COD_INST` ni decide equivalencias con ROR, SNIES u otras fuentes. Kahi conserva
esa responsabilidad porque dispone de todas las afiliaciones ya integradas.
Un identificador `IUA` solo puede crearse después de fallar la coincidencia
exacta y la similitud estricta inequívoca.

## Operación e idempotencia

- Lectura por cursor y escritura masiva acotada, con checkpoint por `_id`.
- DAM es de solo lectura: los consumidores no crean índices ni colecciones allí.
- Una reanudación usa la misma release; nunca cambia al nuevo valor de `current`.
- Los snapshots se procesan después de afiliaciones, personas y unicidad.
- Works se carga como base antes de OpenAlex, ScienTI privado, CIARP, Scholar y
  DSpace para que esas fuentes lo enriquezcan y no repetir millones de búsquedas.
- Projects se carga antes de SIIU; patents y events usan su snapshot final.

## Criterios de aceptación para las siguientes etapas

1. Toda referencia `COD_RH` no vacía existe en el snapshot de personas.
2. Todo código de grupo existe en el snapshot de afiliaciones.
3. Cada DOI de `related_works` es canónico, único por persona y existe en works.
4. La unicidad de personas conserva afiliaciones al absorber documentos.
5. Los conteos leídos coinciden con la auditoría de la release.
6. Ningún consumidor reintroduce productos desde `person.related_works`.
7. Una corrida interrumpida se reanuda sin duplicados ni cambio de release.
8. La importación no introduce campos nuevos en las entidades Kahi.

## Ejecución del materializador de afiliaciones

```bash
bin/yuku_run --mongo_dbname dam --materialize_scienti_affiliations \
  --scienti_affiliations_run_name affiliations_YYYYMMDD \
  --scienti_affiliations_recognized_collection recognized_groups \
  --scienti_affiliations_open_data_collection gruplac_groups_data \
  --scienti_affiliations_gruplac_collection GRUPLAC_NORMALIZADO_AUDITADO \
  --scienti_affiliations_gruplac_audit_name AUDITORIA_GRUPLAC \
  --scienti_affiliations_target_collection scienti_affiliations_final_YYYYMMDD
```

El proceso registra checkpoints, huellas SHA-256, cobertura y anomalías en
`scienti_affiliation_materialization_runs` y
`scienti_affiliation_materialization_audits`; solo después de aprobar renombra
la colección temporal y actualiza `scienti_affiliation_publications.current`.

## Publicación atómica de seis entidades

La release Kahi usa una release explícita y auditada de las cuatro entidades
como base, más los snapshots publicados de personas y afiliaciones. Antes de
cambiar `scienti_final_release_publications.current`, recorre las seis
colecciones y valida: autores → personas, grupos → afiliaciones, afiliaciones de
persona → afiliaciones y DOI relacionados → works. Cualquier identificador
inválido, referencia ausente o DOI inexistente impide el cambio del puntero.
Un DOI incompleto en la obra fuente no se considera identificador: se excluye
de la evidencia de identidad y se reporta como métrica de calidad.

## Ejecución del materializador de personas

```bash
nohup bin/yuku_run --mongo_dbname dam --materialize_scienti_persons \
  --scienti_persons_run_name persons_YYYYMMDD \
  --scienti_persons_manifest_run_name MANIFIESTO_CONGELADO \
  --scienti_persons_manifest_collection COLECCION_MANIFIESTO \
  --scienti_persons_cvlac_collection CVLAC_3_2_AUDITADO \
  --scienti_persons_cvlac_audit_name AUDITORIA_CVLAC \
  --scienti_persons_gruplac_collection GRUPLAC_3_2_AUDITADO \
  --scienti_persons_gruplac_audit_name AUDITORIA_GRUPLAC \
  --scienti_persons_affiliation_run_name RUN_AFILIACIONES \
  --scienti_persons_affiliation_collection SNAPSHOT_AFILIACIONES \
  --scienti_persons_final_release_name RELEASE_FINAL \
  --scienti_persons_final_release_audit_name AUDITORIA_RELEASE \
  --scienti_persons_target_collection scienti_persons_final_YYYYMMDD \
  > persons_YYYYMMDD.log 2>&1 &
tail -f persons_YYYYMMDD.log
```

La ejecución crea `scienti_person_materialization_runs`,
`scienti_person_materialization_audits` y `scienti_person_publications`; solo
publica tras una auditoría sin anomalías críticas.

```bash
nohup bin/yuku_run --mongo_dbname dam --publish_scienti_kahi_release \
  --scienti_kahi_release_name RELEASE_SEIS_ENTIDADES \
  --scienti_kahi_base_release_name RELEASE_CUATRO_ENTIDADES \
  --scienti_kahi_person_run_name RUN_PERSONAS \
  --scienti_kahi_affiliation_run_name RUN_AFILIACIONES \
  > release_seis_entidades.log 2>&1 &
tail -f release_seis_entidades.log
```
