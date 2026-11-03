Las nuevas publicaciones siguen el [contrato de nombres y retención](minciencias_publications.md).

# Colección opcional de trabajos identificados

El selector lee las colecciones `works` y `persons` de una release publicada
explícita y con auditoría aprobada. Genera un subconjunto independiente para
configurar posteriormente una ingestión controlada en Kahi. No ejecuta el
pipeline completo ni modifica el puntero `current` de la release.

## Admisión y validación local

Una obra entra si cumple **DOI válido OR ISBN válido OR par ScienTI válido**.
Cada `_id` original aparece una sola vez, aunque cumpla varias condiciones.
Un ISSN válido se conserva como metadata y nunca es suficiente para admitirla.
La validación es sintáctica y local; no comprueba registros ni resuelve URLs.

* DOI: admite representación canónica, URL DOI, `doi:` o identificador desnudo;
  exige `10.` y entre 4 y 9 dígitos de registrante, con sufijo sin espacios.
  Rechaza registrantes formados solo por ceros, placeholders, query, fragmento,
  caracteres nulos y reparaciones que cambien la identidad.
* ISBN: exige una entrada textual con dígitos, separadores y `X` permitidos;
  comprueba checksum de ISBN-10/13, excluye ceros y exige namespace `978/979`
  para ISBN-13. No borra letras arbitrarias para fabricar un ISBN.
* ISSN: ocho posiciones, checksum módulo 11, exclusión de ceros y formato
  `NNNN-NNNX` o `NNNN-NNNN`.
* Par: `COD_RH` de diez dígitos no nulos y `COD_PRODUCTO` de uno a siete
  dígitos, ambos textuales y en el mismo elemento `external_ids` de fuente
  `scienti`. Exige una persona con ese `_id` en la release y un producto oficial
  de la obra terminado exactamente en `-COD_RH-COD_PRODUCTO`, declarado en
  `external_ids` de fuente `minciencias` o `bibliographic_info.minciencias.product_ids`.
  No combina listas de propietarios/productos ni atribuye autoría al titular.

Se reutilizan `Kahi_impactu_utils.Work.canonical_work_doi`,
`Kahi_impactu_utils.Mongo.order_document` y
`yuku.cvlac_work_graph.normalize_isbn_identity`. Las versiones publicadas de
utils que no incluyen `Work.py` o `Mongo.py` utilizan una ruta compatible:
`Utils.doi_processor` con comprobación de identidad y ordenamiento de las claves
presentes mediante la misma plantilla de Kahi. Cada run guarda hashes del código
de los helpers, la política y el esquema; cambiarlos obliga a usar un nuevo run.

## Esquema y capítulos

Los campos presentes siguen **exactamente el orden del `empty_work()` instalado**;
`_id` se conserva primero como identidad de MongoDB. La proyección admite:

```text
_id, titles, updated, doi, keywords, types, external_ids, year_published,
bibliographic_info, author_count, authors, source, ranking, subjects, groups
```

Se omiten valores `None`, texto vacío, listas vacías y diccionarios sin contenido,
también dentro de la metadata. Se conserva `author_count: 0`, los nombres y las
relaciones declaradas. No se añaden defaults ni se resuelven autores por nombre.
Los identificadores DOI/ISBN/ISSN y los pares inválidos se retiran del documento
derivado; también se sanea el ISBN/ISSN/DOI del contenedor `source`. Se eliminan
fingerprints de títulos. La evidencia original permanece en la colección fuente.

El ISBN puede admitir un capítulo y se considera identidad de su **libro
contenedor**, no una clave única del capítulo. Se conservan su tipo, título,
autores y contexto del libro. La auditoría registra cantidades por rol
`container`/`work`; no añade campos ajenos a `empty_work()` a los documentos.
Ejemplo: dos capítulos con ISBN `9780306406157` permanecen como dos `_id`
distintos. El consumidor en Kahi debe evitar fusionar capítulos por ISBN.

## Dependencias y ejecución

La dependencia es opcional y los flujos anteriores mantienen sus requisitos:

```bash
pip install -e '.[identified_works]'
# Para utilizar los helpers de los árboles locales:
pip install -e ../Kahi -e ../Kahi_impactu_utils
```

El extra fija `Kahi==0.1.2a0`, `Kahi_impactu_utils==0.1.5` y `thefuzz==0.22.1`,
necesario para importar el módulo local `Work`. Los helpers nuevos
pueden estar sin publicar en el checkout local: no se asume que el número de
versión por sí solo garantice su presencia. El run registra su implementación
real. Importar los helpers no inicializa modelos de lenguaje.

Conteo exacto sin destino:

```bash
python bin/yuku_run --mongo_dbname dam --create_scienti_identified_works \
  --scienti_identified_works_release release_20261009_kahi \
  --scienti_identified_works_run_name identified_works_v03_20261009_count \
  --scienti_identified_works_count_only
```

Generación, verificando el conteo previo:

```bash
python bin/yuku_run --mongo_dbname dam --create_scienti_identified_works \
  --scienti_identified_works_release release_20261009_kahi \
  --scienti_identified_works_run_name identified_works_v03_20261009 \
  --scienti_identified_works_collection minciencias_works_v03_20261009_filtered \
  --scienti_identified_works_expected_count_run identified_works_v03_20261009_count
```

El método equivalente es `Yuku.create_scienti_identified_works(...)`.
Para millones de documentos se recomienda el controlador desacoplado, que
ejecuta ambas fases y produce un reporte final:

```bash
python scripts/run_scienti_identified_works.py --database dam \
  --release release_20261009_kahi \
  --run-name identified_works_v03_20261009 \
  --target minciencias_works_v03_20261009_filtered \
  --output-dir /home/fvergara/reports/yuku-identified-works-20261007
tail -n 30 /home/fvergara/reports/yuku-identified-works-20261007/job.log
cat /home/fvergara/reports/yuku-identified-works-20261007/status.json
```

El controlador usa `subprocess.Popen(..., start_new_session=True)`, entrada
`DEVNULL`, log persistente, `job.pid` y `status.json`. `report.json` aparece al
publicarse la colección; `count.result.json` aparece tras el conteo.
La URI se toma de `YUKU_MONGO_URI`, o de `mongodb://localhost:27017/` por defecto.
El directorio del job tiene un bloqueo exclusivo que dura toda la ejecución.

## Reanudación, auditoría y publicación

Repetir los mismos argumentos reanuda desde el último `_id` confirmado. La
configuración fija release, evidencias de auditoría, política, modo y destino.
Se leen proyecciones mediante cursor y se consultan personas por lotes con caché
limitada. Los IDs de texto y `ObjectId` conservan su tipo BSON incluso al reanudar.
Un lote escrito antes de confirmar el checkpoint se reemplaza de forma
idempotente al repetirse. Se requieren colecciones versionadas que permanezcan
inmutables durante la ejecución; se comprueban nuevamente las evidencias y
cantidades de la release al finalizar.

Cada run tiene un lease renovado por lote; otra instancia no puede ejecutar el
mismo run simultáneamente. Si el proceso muere sin liberar el lease, puede
recuperarse después de 120 segundos. Un destino pertenece a un solo run y nunca
sobrescribe una colección existente. No hay opción destructiva `replace`.

La generación escribe en `<destino>__building_<run>`. Antes de publicar, relee
todos los documentos para verificar cantidad, campos, orden y estructura
dispersa, y compara una suma de hashes BSON con la proyección ya validada.
El conteo previo opcional debe coincidir en cantidad y digest de IDs/criterios.
Solo una auditoría aprobada permite el rename atómico, sin `dropTarget`.
También se recupera la ventana entre el rename y la actualización del control.

Colecciones de control independientes:

| Colección | Contenido |
|---|---|
| `scienti_identified_work_runs` | Configuración, lease, checkpoint, cantidades, combinaciones, rechazos, cobertura de campos y hashes |
| `scienti_identified_work_audits` | Auditoría de la proyección almacenada y evidencia de publicación |
| `scienti_identified_work_targets` | Reserva exclusiva del destino para un run |
| `scienti_identified_work_publications` | Relación destino–run–release–auditoría |

`by_criterion` cuenta obras por regla y contiene solapamientos; `selected` cuenta
la unión sin duplicados. `rejections` cuenta ocurrencias inválidas en `doi` y
`external_ids`, no necesariamente obras distintas; `rejection_examples` conserva
hasta cinco IDs de obras por motivo para consultar su evidencia original.
Una obra admitida por ISBN
puede tener un rechazo de DOI. `fields_populated` registra cobertura por campo en
la generación; el conteo utiliza una proyección menor y deja ese resumen vacío.

Pruebas ordinarias: `python -m pytest -q`. La integración con helpers y MongoDB
real es optativa y usa una base temporal propia que elimina al finalizar:

```bash
YUKU_IDENTIFIED_TEST_URI=mongodb://localhost:27017/ \
  python -m pytest tests/test_scienti_identified_works.py -q
```
