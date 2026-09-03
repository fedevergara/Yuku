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

## Build unified CVLAC works

After CVLAC profiles are downloaded, Yuku builds `cvlac_works` automatically
from the HTML in `cvlac_stage_raw`. The collection contains one dehydrated
Kahi-compatible document per unified work instead of one document per profile.
Documents are sparse, but keep the eventually enrichable work fields `doi`,
`keywords`, `year_published` and `subjects` with their Kahi-compatible empty
values when CVLAC has no data. Work fields that this flow cannot enrich are
omitted.

The unicity process uses a sparse evidence graph:

* Every occurrence of a product in a CVLAC profile is a graph node.
* A normalized DOI only proposes an edge when it is product-specific and the
  titles, years and product families are bibliographically compatible. DOI
  placeholders, journal-level prefixes and conflicting candidates are not
  identity evidence, but their values remain available as metadata.
* Equal normalized title and publication year create automatic edges only when
  product families are compatible and no DOI, year or transitive-title conflict
  is present.
* Generic titles require supporting author evidence.
* Type conflicts and unsafe candidates are stored for review instead of being
  merged automatically.
* A connected high-confidence component becomes one work. CVLAC owners and
  GrupLAC groups remain reporting sources; only explicitly declared people are
  author assertions.
* Conflicting author declarations for books are retained as evidence and
  candidates, but are not published as canonical authors unless a verified
  bibliographic authority resolves the conflict.
* Composite publisher strings remain unchanged. Exact curated cases expose
  resolved `bibliographic_info.scienti.publisher_entities`; explicit spaced
  separators between publisher-like names are only medium-confidence
  candidates until an authority confirms them.
* A node without a safe edge is still materialized as an independent work; no
  occurrence is discarded merely because it could not be unified.

Authors are dehydrated to `id`, `full_name`, `affiliations` and optional `type`.
Only product-specific GrupLAC affiliation evidence is copied; ranking, external
identifiers and CVLAC professional trajectories are not copied.
For theses and directed works, the CVLAC profile owner is emitted as
`type: "advisor"` and each parsed oriented person as `type: "author"`, matching
the current `works` convention. For other products the profile owner is not an
implicit author. A person's `id` is populated only when its normalized full
name resolves to exactly one known profile; otherwise the required `id` remains
an empty string. Author display names use consistent title capitalization while
identity comparisons remain case-insensitive.

The process can also be run independently:

```
yuku_run --mongo_dbname dam --create_cvlac_works
```

To process a controlled subset:

```
yuku_run \
  --mongo_dbname dam \
  --create_cvlac_works \
  --cvlac_works_profile_ids 0000177733,0000227447
```

The destination and source collections are configurable with
`--cvlac_works_collection` and `--cvlac_works_source_collection`. Use
`--download_cvlac_profiles_skip_works` to omit the automatic final build.

Each run stores summary and review information in
`cvlac_works_graph_runs` and `cvlac_works_graph_review`. The source raw HTML is
never modified. The destination is published only after a complete temporary
build succeeds.

To include normalized GrupLAC occurrences in the same graph:

```
yuku_run \
  --mongo_dbname dam \
  --create_cvlac_works \
  --cvlac_works_collection minciencias_works_next \
  --cvlac_works_group_source_collection gruplac_related_works
```

## Integral public Scienti pipeline

Yuku now exposes the historical identifier and public profile workflow as
package methods and commands. Existing external scripts can remain available
during comparison, but new executions can use the following stages.

Download or resume the complete public researcher directory:

```
yuku_run --mongo_dbname dam --download_all_researchers
```

Resolve the historical JSON files from the calls for applications:

```
yuku_run --mongo_dbname dam \
  --recognize_researchers_json /path/investigadores.json

yuku_run --mongo_dbname dam \
  --recognize_groups_json /path/grupos.json
```

Download the union of known CVLAC profiles and all recognized GrupLAC pages:

```
yuku_run --mongo_dbname dam \
  --download_scienti_cvlac_profiles \
  --scienti_workers 4 \
  --scienti_requests_per_second 2

yuku_run --mongo_dbname dam \
  --download_scienti_gruplac_profiles \
  --scienti_workers 4 \
  --scienti_requests_per_second 2
```

Normalize GrupLAC before including it in the work graph:

```
yuku_run --mongo_dbname dam --create_gruplac_related_works
```

All download stages are idempotent and use MongoDB checkpoints. Use
`--scienti_limit` or `--all_researchers_limit_pages` for controlled tests, and
`--scienti_refresh_days` to update only profiles whose HTML is older than the
specified age.

Before a large CVLAC refresh, run an isolated evaluation containing an equal
number of previously unseen and already downloaded priority profiles:

```
yuku_run --mongo_dbname dam \
  --evaluate_scienti_cvlac_profiles \
  --scienti_evaluation_name cvlac_eval_1000 \
  --scienti_evaluation_new_count 500 \
  --scienti_evaluation_refresh_count 500 \
  --scienti_workers 4 \
  --scienti_requests_per_second 2
```

The deterministic cohort, downloaded HTML, downloader checkpoints and unified
works are stored in separate collections prefixed with the evaluation name.
The production raw collection is read only and used to compare content hashes.
Re-running the same command resumes the cohort and skips completed downloads.

Compare the normalized metadata of the refreshed half against the legacy HTML:

```
yuku_run --mongo_dbname dam \
  --compare_scienti_cvlac_evaluation \
  --scienti_evaluation_name cvlac_eval_1000
```

The comparison distinguishes new, removed and modified records, ignores HTML
presentation order, records status transitions and writes one auditable result
per COD_RH to `<evaluation_name>_refresh_comparison`.

After the evaluation passes, freeze the strong-priority universe and download a
versioned snapshot without including ambiguous candidates or directory-only
identifiers:

```
yuku_run --mongo_dbname dam \
  --download_scienti_cvlac_priority_snapshot \
  --scienti_priority_run_name cvlac_priority_20260823 \
  --scienti_priority_reference_collection cvlac_stage_raw \
  --scienti_priority_raw_collection cvlac_stage_raw_20260823 \
  --scienti_workers 4 \
  --scienti_requests_per_second 2
```

The immutable `<run_name>_targets` manifest makes restarts deterministic. Raw
HTML and downloader state use isolated versioned collections, and up to three
passes retry individual failures without overwriting the reference snapshot.

Expand that verified snapshot to the complete researcher directory without
redownloading its HTML:

```
yuku_run --mongo_dbname dam \
  --download_scienti_cvlac_full_snapshot \
  --scienti_full_run_name cvlac_full_20260823 \
  --scienti_full_seed_collection cvlac_stage_raw_20260823 \
  --scienti_full_raw_collection cvlac_stage_raw_full_20260823 \
  --scienti_workers 4 \
  --scienti_requests_per_second 2
```

The seed copy is performed inside MongoDB with `$merge` and `keepExisting`, so
it is idempotent and never overwrites HTML already downloaded by the full run.
Use `--scienti_full_no_seed_copy` when the source snapshot must remain available
for audit but cannot be trusted as input (for example, after an encoding fix).
For an accelerated run, `--scienti_fallback_requests_per_second 2` and
`--scienti_fallback_after_errors 5` automatically reduce a 4 requests/s run if
five requests still fail after the HTTP retry policy is exhausted.

Normalize the priority snapshot independently while the full download runs:

```
yuku_run --mongo_dbname dam \
  --create_cvlac_related_works \
  --cvlac_related_works_source_collection cvlac_stage_raw_20260823 \
  --cvlac_related_works_collection cvlac_related_works_priority_20260823 \
  --cvlac_related_works_resume
```

Resume mode skips completed profiles and stores isolated parsing failures in
`<destination>_errors`; it does not trigger any additional Scienti requests.

A versioned GrupLAC download can run concurrently at a lower request rate and
normalize itself before the final graph build:

```
yuku_run --mongo_dbname dam \
  --download_scienti_gruplac_profiles \
  --gruplac_raw_collection gruplac_stage_raw_20260823 \
  --gruplac_download_state_collection gruplac_downloads_20260823 \
  --create_gruplac_related_works \
  --gruplac_related_works_collection gruplac_related_works_20260823 \
  --scienti_workers 2 \
  --scienti_requests_per_second 0.5
```

The final build consumes only the audited normalized CVLAC and GrupLAC
collections; it never reparses raw HTML. Its hard gate refuses to run unless
the complete CVLAC snapshot and both named normalization audits are `passed`,
with exact source/destination coverage and no critical or parser errors.

```
yuku_run --mongo_dbname dam \
  --create_scienti_full_graph \
  --scienti_full_run_name cvlac_full_corrected_20260823 \
  --scienti_full_graph_run_name scienti_full_graph_v1_20260825 \
  --scienti_full_graph_collection cvlac_works_graph_full_v1_20260825 \
  --scienti_full_cvlac_related_collection cvlac_related_works_full_corrected_20260823 \
  --scienti_full_cvlac_audit_name cvlac_normalization_audit_corrected_20260823 \
  --scienti_full_gruplac_raw_collection gruplac_stage_raw_corrected_20260823 \
  --scienti_full_gruplac_related_collection gruplac_related_works_corrected_20260823 \
  --scienti_full_gruplac_audit_name gruplac_normalization_audit_v202_20260825 \
  --scienti_full_graph_partitions 64
```

The build checkpoints `extract_nodes`, `connect_doi`, `connect_title_year`,
`materialize`, `audit` and `publish`. Re-running the exact command and run name
continues after the last completed checkpoint. Failed artifacts are retained
for diagnosis. Publication renames an audited temporary collection atomically,
refuses to overwrite an existing target, preserves the previous graph and
updates the `scienti_work_graph_publications` pointer only after success.

The definitive builder assigns every occurrence a dense integer `node_seq` and
uses fixed-width NumPy arrays for Union-Find state. Parent, component-size and
activity arrays require nine bytes per extracted node (about 57.3 MiB for the
6,669,695 normalized production records currently audited), plus summaries
only for nodes participating in candidate groups. Title/year candidates are
split into deterministic partitions with individual checkpoints and indexed by
`title_partition, title_key, year`. Identity collisions are checked against
MongoDB in write batches, so the process does not retain every work identifier
in a global Python set.

Work-author affiliations use only a closed GrupLAC evidence triangle: the
product must be reported by the group, the author must have an exact `cod_rh`
membership covering the complete publication year, and the group's historical
institution must be exact for that year or identical in the closest bracketing
convocatorias. CVLAC professional trajectories and name-only matches are never
used. Every author always exposes `affiliations` (an empty list when evidence is
insufficient); every group relation exposes a string `affiliations` value (an
empty string when the historical institution is not temporally safe). The rule
is versioned as `gruplac-product-group-membership-v1`, so older checkpoints
cannot silently resume with the new materialization contract.

### Official measurement history and Kahi-compatible final entities

The official `gruplac_production_data` rows are normalized without treating
`id_persona_pd` (the product owner/claimant) as a bibliographic author. The
output preserves types, rankings and the complete group/convocatoria
relationship. Exact catalog routing sends every product only to `works`,
`projects`, `patents` or `events`.

Normalization, exact linkage and final materialization are independently
checkpointed in `minciencias_measurement_runs`. The link and materialization
commands accept `target_entity` through the Python API; the complete pipeline
runs them for all four entities and publishes the four outputs atomically.

```
yuku_run --mongo_dbname dam \
  --normalize_minciencias_measured_products \
  --minciencias_measurement_run_name measurements_20260827 \
  --minciencias_measurement_source_collection gruplac_production_data \
  --minciencias_measured_products_collection minciencias_measured_products_20260827

yuku_run --mongo_dbname dam \
  --link_minciencias_measured_products \
  --minciencias_measurement_run_name measurements_20260827 \
  --minciencias_measured_products_collection minciencias_measured_products_20260827 \
  --minciencias_measurement_links_collection minciencias_measured_product_links_20260827 \
  --minciencias_measurement_graph_collection cvlac_works_graph_full_v2_20260826

yuku_run --mongo_dbname dam \
  --materialize_minciencias_enriched_graph \
  --minciencias_measurement_run_name measurements_20260827 \
  --minciencias_measurement_graph_collection cvlac_works_graph_full_v2_20260826 \
  --minciencias_measured_products_collection minciencias_measured_products_20260827 \
  --minciencias_measurement_links_collection minciencias_measured_product_links_20260827 \
  --minciencias_enriched_graph_collection cvlac_works_graph_full_v3_20260827
```

Automatic links require exact normalized title and year, a compatible target,
and at least one exact owner or group anchor. Generic titles require both
anchors. Multiple acceptable candidates remain `ambiguous` and missing matches
remain `unlinked`; both become standalone official records in their own entity.
They receive no inferred authors or dates. Uniquely linked products enrich the
scraped record without changing its authorship or temporal fields. Products
without a usable title remain in the original open-data source and are counted.
The final release pointer is `scienti_final_release_publications.current`.

Recheck only GrupLAC pages classified as incomplete, preserving both versions
and promoting only pages that become structurally complete:

```
yuku_run --mongo_dbname dam \
  --verify_scienti_gruplac_incomplete \
  --gruplac_verification_run_name gruplac_incomplete_check_20260823 \
  --gruplac_verification_source_raw_collection gruplac_stage_raw_corrected_20260823 \
  --gruplac_verification_source_state_collection gruplac_downloads_corrected_20260823 \
  --gruplac_verification_normalized_collection gruplac_related_works_corrected_20260823 \
  --scienti_workers 1 \
  --scienti_requests_per_second 0.25
```

Verified bibliographic evidence can be loaded before the graph build with
`--load_bibliographic_authorities_json`. Its records require a `match` object,
an `authors` array and source `evidence`.

The complete collection layout, data contracts, refresh tiers and publication
controls are documented in
[docs/architecture_scienti_pipeline.md](docs/architecture_scienti_pipeline.md).

### Complete, ordered Scienti pipeline

`--run_scienti_pipeline` executes or resumes the complete workflow from a JSON
configuration. The default input is the already consolidated
`investigadores.json` and `grupos.json`. Pipeline configurations and source
JSON files remain local and are intentionally excluded from Git. To regenerate
the historical JSON files from native-text PDFs, select PDF input mode in the
configuration. Use a new
`run_name` and `snapshot_tag` for every frozen snapshot; every derived dataset,
including measured products and their links, is versioned with that tag.

Validate the JSON inputs (or the optional PDF source) and display the immutable plan without making
requests or processing data:

```
yuku_run --mongo_dbname dam \
  --run_scienti_pipeline /path/to/scienti_pipeline.json \
  --scienti_pipeline_dry_run
```

Run or resume it:

```
yuku_run --mongo_dbname dam \
  --run_scienti_pipeline /path/to/scienti_pipeline.json
```

Inspect its checkpointed state from another terminal:

```
yuku_run --mongo_dbname dam \
  --scienti_pipeline_status scienti_full_YYYYMMDD
```

The strict order is: atomically download the three original open-data
collections; validate the two historical JSON files (or first regenerate them
from native-text PDFs); download the complete researcher directory; resolve
historical people and groups; download, normalize, verify and audit GrupLAC;
freeze and download the complete CVLAC universe (including group members and
ambiguous candidate codes); normalize and audit CVLAC; normalize and audit the
four strict entity collections; compare them with the current release when one
exists; atomically publish their audited snapshot; build the project and patent
graphs; atomically materialize events; publish the base works graph; normalize
and link the versioned official measured products; materialize all four final
entities; audit explicit publisher and book metadata; atomically publish their
joint release; then remove only exact run-owned intermediates and superseded
final collections. A stage cannot run before all its dependencies pass. The
current joint release, original sources, frozen HTML, manifests, audits and run
records are always retained.
Legacy per-entity pointers remain unchanged during final materialization and
move only after the joint release gate passes.
The current normalized entity snapshot is retained for the next exhaustive
version comparison; only its superseded predecessor becomes a cleanup candidate.

## Normalize CVLAC Related Works

Yuku can build a normalized collection from the raw CVLAC HTML stored in
`cvlac_stage_raw`. The normalized collection groups the author output by
profile and separates the currently supported entities:

* `production`: articles, books, book chapters, theses, editorial outputs and
  other CVLAC production records supported by the parser.
* `patents`: patent records. Patent applicants are stored in `applicant`, not in
  `authors`, because applicants can be companies or other legal entities.
* `events`: scientific event participation records.
* `projects`: project records.

Example:

```
yuku_run --create_cvlac_related_works
```

To process a subset of profiles:

```
yuku_run \
  --create_cvlac_related_works \
  --cvlac_related_works_profile_ids 0000177733,0000227447
```

The destination collection defaults to `cvlac_related_works`, but can be
changed with:

```
yuku_run \
  --create_cvlac_related_works \
  --cvlac_related_works_collection cvlac_related_works_sample
```

Each normalized document has this top-level structure:

```
{
  "_id": "0000177733",
  "id_persona_pr": "0000177733",
  "url_persona": "https://scienti.minciencias.gov.co/cvlac/visualizador/generarCurriculoCv.do?cod_rh=0000177733",
  "production_counts": 95,
  "production": [],
  "patents_counts": 0,
  "patents": [],
  "events_counts": 51,
  "events": [],
  "projects_counts": 26,
  "projects": []
}
```

Common normalized fields:

* `production` and `patents` include `profile_id`, `type_impactu`, `title`,
  `year`, `affiliation`, `country`, `keywords`, `areas`, `doi`, `issn` and
  `isbn` when available.
* `production` uses `authors`.
* `patents` uses `applicant`. If the applicant shares a significant normalized
  name token with the CVLAC profile author, the applicant is left as an empty
  string to avoid repeating the author as applicant.
* `events` use `type_impactu: "Evento"` and include event metadata such as
  `event_type`, `scope`, dates, location, associated products, institutions and
  participants.
* `projects` use `type_impactu: "Proyecto"` and include project type, title,
  start/end dates, duration, year and summary.

The `type_impactu` values for `events` and `projects` follow the mappings in
`Tipos_ImpactU_Definitivo.xlsx`: entity `events` maps to `Evento`, and entity
`projects` maps to `Proyecto`.

## Strict Kahi entity normalization

The profile-level collections above remain source evidence. A second,
checkpointed layer publishes works (including theses, software, teaching and
non-bibliographic products), projects, patents and events into four
Kahi-compatible collections. The authoritative `ALL` sheet from
`Tipos_ImpactU_Definitivo.xlsx` is converted reproducibly into the bundled,
versioned `yuku/data/tipos_impactu_v1.csv`; runtime processing never depends
on the external workbook. Only exact source channel, section and product-type
pairs are accepted; case, HTML entities and whitespace are normalized, while
accents and words remain significant. Unknown variants stay in source evidence
and are counted as `unmapped` instead of being guessed.

The bibliographic works graph applies the same router before creating nodes.
Projects, patents and events are counted as explicit routing exclusions and
cannot enter the graph. Industrial designs, plant/animal varieties, integrated
circuit layouts, software and teaching products remain in `works`, following
the catalog; distinctive signs are routed to `patents`.

Projects and patents then receive independent exact graphs. They never use
fuzzy titles or names. A candidate needs the same accent-sensitive title and
subtype/namespace, a shared stable `cod_rh` or group code, and compatible
dates, countries and registration numbers. Missing evidence may attach to a
stronger record, but contradictory dates or registrations create separate
components and an auditable review finding. Generic titles remain separate.
The complete pipeline writes `scienti_projects_final_<tag>` and
`scienti_patents_final_<tag>` atomically after verifying that every normalized
source document occurs in exactly one graph component. Events do not use
identity fusion: they are copied exactly into `scienti_events_final_<tag>` only
after publication and an exhaustive identifier/count audit.

The standalone command is:

```bash
yuku_run --mongo_dbname dam \
  --create_scienti_entity_graph \
  --scienti_entity_graph_entity projects \
  --scienti_entity_graph_run_name projects_graph_YYYYMMDD \
  --scienti_entity_graph_source_collection scienti_projects_normalized_YYYYMMDD \
  --scienti_entity_graph_target_collection scienti_projects_final_YYYYMMDD \
  --scienti_entity_graph_entity_run_name scienti_full_YYYYMMDD_entities
```

Thesis students are ordinary authors. The CVLAC profile owner, or an explicit
GrupLAC tutor/cotutor, is kept in `authors` with `type: "advisor"`, matching the
current Kahi convention. A `cod_rh` is assigned from GrupLAC only when the
complete normalized name has exactly one group-member match. The same strict
policy is used for entity identity: native registration number, or exact
entity-specific title/date/type/student anchors. Insufficient evidence creates
a source-scoped record and never a fuzzy merge.

When the exact accent-sensitive name is simultaneously reported as student
and advisor for one thesis, the student role takes precedence and the discarded
advisor claim is recorded in `source_metadata.role_resolutions`. Similar names
are never resolved fuzzily. Patent fields that are absent remain absent and are
reported by the semantic audit rather than inferred. Event dates must be valid
ISO calendar dates: malformed values become `null`, and a final date earlier
than the start also becomes `null`. Event scope is limited to an explicit
`Nacional` or `Internacional`; parser-contaminated tails are removed only when
they contain an identifiable structural event marker.

The entity normalizer preserves the real month in explicit Spanish or English
month/year dates (`Septiembre 2009`, `February 2010`) by encoding the first day
of that month in Kahi's Unix timestamp field. The original value remains in
`source_metadata.occurrences`; incomplete dates are never inferred.

For the frozen 2026 snapshot, the versioned rebuild plus semantic project
audit can be resumed safely with `bin/yuku_rebuild_scienti_entities_dates_v2`.

Two completed entity runs can be compared exhaustively with
`--compare_scienti_entity_versions`. The checkpointed comparison requires
identical identifiers and metadata and permits only reproducible date changes,
the normalizer-version marker and normalization timestamps. Its summary and
bounded anomaly examples are stored in `scienti_entity_version_comparisons`
and `scienti_entity_version_comparison_anomalies`.

For two snapshots produced by the same current normalizer and router, the
comparison records added, removed, modified and unchanged documents as release
drift. Structural validity remains enforced by the internal and four semantic
audits. The complete pipeline performs this comparison automatically before
every publication except the initial audited snapshot.

For the v2-to-v3 semantic transition, the comparator additionally proves that
only exact student/advisor role resolutions and conservative event sanitation
occurred. Events reidentified after an invalid start date must retain the exact
same occurrence identifiers and source evidence. A passed comparison plus the
four zero-critical semantic audits can be published atomically with
`--publish_scienti_entities`. The authoritative pointer and release history are
stored in `scienti_entity_publications`; source collections are never renamed
or deleted during publication.

Theses/degree works, patents and events have separate specialized semantic
audits. They validate role separation between students and advisors, ImpactU
thesis types, intellectual-property namespaces and registrations, and event
type/date consistency. The audits are read-only and resumable; exact counters
and bounded examples are stored in `scienti_auxiliary_semantic_audits` and
`scienti_auxiliary_semantic_audit_anomalies`.

Controlled example (the source limit is per profile collection):

```
bin/yuku_run \
  --mongo_dbname dam \
  --normalize_scienti_entities \
  --scienti_entities_run_name scienti_entities_sample_v1 \
  --scienti_entities_cvlac_collection cvlac_related_works_full_corrected_20260823 \
  --scienti_entities_gruplac_collection gruplac_related_works_corrected_20260823 \
  --scienti_entities_works_collection scienti_works_normalized_sample_v1 \
  --scienti_entities_projects_collection scienti_projects_normalized_sample_v1 \
  --scienti_entities_patents_collection scienti_patents_normalized_sample_v1 \
  --scienti_entities_events_collection scienti_events_normalized_sample_v1 \
  --scienti_entities_limit_sources 100
```

The run and its audit are stored in `scienti_entity_normalization_runs` and
`scienti_entity_normalization_audits`. Reusing the same run resumes safely;
`--scienti_entities_replace` drops only the four explicitly named destinations.

Normalized projects have an additional read-only, resumable semantic audit. It
checks complete coverage, identity and occurrence integrity, authors, groups,
types, summaries, heterogeneous CVLAC/GrupLAC dates and precision lost in the
top-level Kahi fields. Exact counters are stored in
`scienti_project_semantic_audits`; only bounded examples are stored in
`scienti_project_semantic_audit_anomalies`.

```
bin/yuku_run \
  --mongo_dbname dam \
  --audit_scienti_projects_semantics \
  --scienti_projects_audit_name projects_semantic_v1 \
  --scienti_projects_audit_collection scienti_projects_normalized_v1 \
  --scienti_projects_audit_entity_run_name scienti_entities_v1
```

The old `--download_cvlac_profiles` path is retained only for reproducibility
and now requires `--allow_legacy_cvlac_scraper`. The complete pipeline uses the
versioned Scienti snapshot downloader instead.

The normalized collection can also be summarized by `type_impactu`:

```
yuku_run \
  --cvlac_related_works_distribution \
  --cvlac_related_works_collection cvlac_related_works
```

The summary groups records by entity and `type_impactu`, and reports:

* `records_count`: number of records for that entity/type.
* `authors_count`: number of distinct CVLAC profiles with at least one record
  for that entity/type.

To save the summary in MongoDB:

```
yuku_run \
  --cvlac_related_works_distribution \
  --cvlac_related_works_collection cvlac_related_works \
  --cvlac_related_works_distribution_output_collection cvlac_related_works_type_impactu_distribution
```

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
cvlac_works
cvlac_works_graph_review
cvlac_works_graph_runs
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
* cvlac_works: graph-unified dehydrated works extracted from CVLAC profiles.
* cvlac_works_graph_review: candidates rejected from automatic unification.
* cvlac_works_graph_runs: execution summaries for the unification process.
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
