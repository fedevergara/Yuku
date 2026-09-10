# Arquitectura ScienTI integral de Yuku

## Objetivo

Yuku debe poder reconstruir y actualizar, sin depender de los dumps privados
`scienti_*`, el mayor subconjunto verificable de personas, grupos y trabajos de
Minciencias. La solución combina datos abiertos, convocatorias históricas y las
interfaces públicas de búsqueda, CvLAC y GrupLAC.

La prioridad es precisión y trazabilidad. Una fuente adicional puede reforzar o
cuestionar un metadato, pero no puede convertir automáticamente propietarios de
perfiles, líderes o integrantes de grupos en autores de un trabajo.

## Capas

```text
Fuentes                    Bronce                   Plata
---------------------     --------------------     -----------------------
JSON convocatorias ----> JSON histórico --------> recognized_researchers
PDF texto nativo (opcional) --^
                                              \--> recognized_groups
Directorio público ------> páginas cacheadas ----> all_researchers
CvLAC -------------------> cvlac_stage_raw ------> cvlac_related_works
GrupLAC -----------------> gruplac_stage_raw ----> gruplac_related_works
Datos.gov.co ------------> *_data --------------> relaciones históricas

Plata + autoridades bibliográficas
                 |
                 v
          ocurrencias canónicas
                 |
                 v
       grafo de identidad de trabajos
                 |
                 v
       colección oro compatible con Kahi
```

### Bronce: evidencia inmutable y reanudable

- `scienti_researcher_directory_pages`: páginas validadas del directorio de
  investigadores.
- `scienti_identifier_search_cache`: resultados normalizados de búsquedas por
  documento o código de grupo.
- `cvlac_stage_raw`: HTML, URL, hash y fecha de descarga por `cod_rh`.
- `gruplac_stage_raw`: HTML, URL, hash y fecha por código `COL...`.
- `scienti_profile_downloads`: estado, intentos y último error de cada descarga.

El HTML nunca se mezcla directamente con documentos finales. Un cambio de
parser puede reprocesar la capa bronce sin volver a consultar el portal.

### Plata: entidades y ocurrencias normalizadas

- `all_researchers`: catálogo único por `cod_rh`.
- `recognized_researchers`: investigadores de convocatorias resueltos por
  documento, conservando estados y candidatos.
- `recognized_groups`: grupos históricos resueltos por código.
- `cvlac_related_works`: producción organizada por perfil.
- `gruplac_related_works`: metadatos, integrantes y producción organizada por
  grupo. Los integrantes están separados de los autores declarados en cada
  producto.
- `minciencias_bibliographic_authorities`: correcciones verificadas mediante
  una fuente bibliográfica identificable.

### Oro: trabajos únicos

La colección oro debe construirse primero con un nombre de prueba, por ejemplo
`minciencias_works_next`. Sólo se publica para Kahi después de validar el informe
de diferencias. La publicación definitiva debe usar renombre atómico y generar
una tabla `legacy_id -> new_id` cuando cambie la versión del grafo.

El constructor definitivo recibe exclusivamente las colecciones normalizadas
de CvLAC y GrupLAC. El HTML de la capa bronce sólo sirve para reproducir la
normalización y no se vuelve a interpretar durante la construcción del grafo.

## Módulos

### Implementados

- `researcher_directory.py`: paginación completa, caché en MongoDB, validación
  cruzada de rangos y sincronización idempotente de `all_researchers`.
- `scienti_search.py`: lectura de JSON históricos y resolución de personas y
  grupos mediante las aplicaciones públicas.
- `scienti_profiles.py`: descarga concurrente, limitada globalmente,
  reanudable y actualizable de CvLAC y GrupLAC.
- `cvlac_related_works.py`: normalización de CvLAC.
- `gruplac_related_works.py`: normalización de datos básicos, integrantes y
  productos GrupLAC, incluida la marca de producto validado.
- `scienti_normalization.py`: corridas versionadas y reanudables de
  normalización CvLAC, procedencia por perfil y auditorías integrales de CvLAC
  y GrupLAC.
- `cvlac_work_graph.py`: grafo conjunto de ocurrencias CvLAC/GrupLAC,
  conciliación conservadora de autoría y autoridades verificadas.
- `Yuku.py`: fachada pública para usar cada componente como método.
- `bin/yuku_run`: interfaz de comandos compatible con las opciones históricas.

### Evolución recomendada

La interfaz de banderas debe conservarse por compatibilidad, pero una siguiente
versión puede añadir subcomandos equivalentes:

```text
yuku researchers directory
yuku researchers recognize
yuku groups recognize
yuku cvlac download
yuku gruplac download
yuku cvlac normalize
yuku gruplac normalize
yuku works build
yuku pipeline run
```

Los subcomandos deben llamar los mismos métodos; no deben contener lógica de
scraping.

## Contrato de una ocurrencia de trabajo

Cada adaptador debe producir, como mínimo:

```json
{
  "source_kind": "cvlac | gruplac | opendata | authority",
  "source_id": "identificador estable de la fuente",
  "source_url": "URL consultada",
  "source_hash": "SHA-256 del contenido",
  "profile_id": "COD_RH o vacío",
  "profile_author": "propietario que reporta, no autor implícito",
  "group_code": "COL... o vacío",
  "group_name": "nombre del grupo",
  "product_type": "tipo original",
  "type_family": "familia canónica",
  "title": "título original",
  "title_key": "título normalizado",
  "year": 2024,
  "authors": ["autores explícitos del producto"],
  "doi": [],
  "isbn": [],
  "issn": [],
  "publisher": "",
  "book_title": "",
  "edition": "",
  "volume": "",
  "pages": "",
  "start_page": "",
  "end_page": "",
  "publication_place": "",
  "language": "",
  "dissemination_medium": "",
  "validated": true
}
```

`profile_author`, `group_code`, líder e integrantes son procedencia o relación;
no pertenecen a `authors` salvo declaración explícita con rol compatible.

## Reglas de identidad de trabajos

1. Un DOI válido y específico es la evidencia principal, con controles de
   título, año y familia documental.
2. Para un libro completo se usa ISBN válido, título, año y edición/editorial.
3. Para un capítulo, el ISBN sólo identifica el contenedor. La identidad exige
   título del capítulo y, cuando existen, páginas y año.
4. Libro y capítulo nunca se unen aunque compartan ISBN.
5. Título y año exactos son evidencia media. Para libros requieren ISBN común o
   evidencia explícita adicional de autores.
6. DOI o ISBN contradictorios impiden la unión automática.
7. Una componente insegura se conserva como documentos independientes y se
   registra en la colección de revisión; ninguna ocurrencia se descarta.

## Reglas de autoría

1. Primero se resuelven personas; después se comparan declaraciones completas
   de autoría por fuente.
2. Los alias cortos sólo se colapsan cuando corresponden a un único nombre más
   completo dentro de la misma obra o a un único `cod_rh` en el registro de
   investigadores.
3. Declaraciones diferentes para un libro producen
   `authorship_status: conflict`. Los candidatos y su evidencia se conservan,
   pero no se publican como autores canónicos.
4. Una autoridad bibliográfica verificada puede resolver el conflicto. Debe
   tener título, año, tipo, identificador cuando exista, autores y URL o
   referencia de evidencia.
5. El caso “Pedagogía de proyectos opción de cambio social” es una prueba de
   regresión permanente: las declaraciones CvLAC no pueden generar once
   autores. Una autoridad institucional puede publicar a María Elvira
   Rodríguez Luna como autora y conservar las declaraciones contradictorias.

## Descarga masiva y actualización

### Universo de investigadores

La unión de identificadores debe provenir de:

- `all_researchers.cod_rh`;
- `recognized_researchers.cod_rh` y sus coincidencias cuando corresponda;
- `gruplac_production_data.id_persona_pd`;
- `cvlac_data.id_persona_pr`;
- líderes e integrantes identificados en GrupLAC.

La unión es única por `cod_rh`. El orden de prioridad recomendado es:

1. Personas de convocatorias o con producción conocida.
2. Líderes e integrantes de grupos reconocidos.
3. Resto del directorio público.

Esto permite producir primero el conjunto de mayor valor sin renunciar a
descargar posteriormente el catálogo completo.

### Actualización por niveles

- Nivel A: perfiles con producción o en convocatorias; actualización cada
  30-90 días.
- Nivel B: integrantes de grupos sin producción observada; cada 180 días.
- Nivel C: resto de `all_researchers`; descarga inicial completa y actualización
  anual o por demanda.
- GrupLAC reconocido: actualizar después de cada convocatoria y luego de forma
  trimestral para detectar producción nueva.

El parámetro `refresh_days` selecciona únicamente HTML ausente o vencido. El
hash permite omitir la renormalización si el contenido no cambió.

### Paralelismo

- El número de trabajadores controla concurrencia de CPU/espera.
- `requests_per_second` es un límite global, no por trabajador.
- Los valores predeterminados son conservadores; deben aumentarse sólo después
  de medir errores 429/5xx y latencia.
- Los reintentos respetan `Retry-After` y usan retroceso exponencial.
- MongoDB es el checkpoint; reiniciar el proceso no vuelve a descargar
  documentos vigentes.

No conviene lanzar cientos de conexiones: el cuello de botella es el portal y
una paralelización agresiva reduce la estabilidad. Entre 4 y 8 trabajadores,
con 2-5 solicitudes globales por segundo, es un punto inicial medible.

## Normalización y auditoría versionadas

La normalización completa sólo acepta una corrida de descarga marcada como
`complete` y exige cobertura exacta tanto del HTML como de sus estados. Cada
documento normalizado guarda `profile_status`, `parser` y `source`, incluyendo
la versión del parser, la huella SHA-256, la URL y la fecha de captura. Al
reiniciar, se omite únicamente un perfil cuya huella y versión coincidan; los
fallos individuales se aíslan en `<destino>_errors`.

```bash
bin/yuku_run \
  --mongo_dbname dam \
  --normalize_scienti_cvlac_snapshot \
  --scienti_normalization_run_name cvlac_normalization_full_corrected_20260823 \
  --scienti_normalization_source_run_name cvlac_full_corrected_20260823 \
  --scienti_normalization_source_state_collection cvlac_full_corrected_20260823_downloads \
  --scienti_normalization_collection cvlac_related_works_full_corrected_20260823 \
  --scienti_normalization_workers 8
```

Los workers ejecutan exclusivamente el parser en procesos aislados. El proceso
principal conserva el cursor, los lotes de escritura, los checkpoints y el
registro de errores; además limita a `workers * 4` las tareas en vuelo para no
acumular HTML en memoria. Cambiar el número de workers no altera la identidad
de la corrida y permite reanudarla sobre la misma colección.

Cuando finalice, la auditoría CvLAC compara cobertura, identificadores, estado,
huella y versión del parser contra la captura congelada. También verifica los
contadores internos y registra ejemplos acotados de títulos, años, DOI, ISBN y
autorías anómalas.

```bash
bin/yuku_run \
  --mongo_dbname dam \
  --audit_scienti_cvlac_normalization \
  --scienti_normalization_run_name cvlac_normalization_full_corrected_20260823 \
  --scienti_cvlac_audit_name cvlac_normalization_audit_corrected_20260823
```

La normalización GrupLAC se ejecuta desde la captura HTML congelada. El parser
2.0.2 trata de forma explícita las estrategias pedagógicas, las estrategias de
comunicación y la participación ciudadana: en estas secciones conserva como
título el primer texto resaltado y separa el período en `start_date` y
`end_date`. Cada documento guarda la huella del HTML, el estado de descarga y
la versión del parser, por lo que una reanudación sólo omite resultados que
coincidan exactamente con la fuente y la versión actuales.

```bash
bin/yuku_run \
  --mongo_dbname dam \
  --normalize_scienti_gruplac_snapshot \
  --scienti_gruplac_normalization_run_name gruplac_normalization_v202_20260825 \
  --gruplac_raw_collection gruplac_stage_raw_corrected_20260823 \
  --gruplac_download_state_collection gruplac_downloads_corrected_20260823 \
  --gruplac_related_works_collection gruplac_related_works_corrected_20260823 \
  --scienti_gruplac_normalization_workers 8
```

La auditoría GrupLAC exige igualdad entre grupos reconocidos, HTML y registros
normalizados; comprueba huellas, versión del parser, estado, integrantes,
producción y errores persistidos. También exige que cada estado `incomplete`
pertenezca a la corrida congelada y haya sido confirmado sin cambio de
contenido.

```bash
bin/yuku_run \
  --mongo_dbname dam \
  --audit_scienti_gruplac_normalization \
  --recognized_groups_collection recognized_groups \
  --gruplac_raw_collection gruplac_stage_raw_corrected_20260823 \
  --gruplac_download_state_collection gruplac_downloads_corrected_20260823 \
  --gruplac_related_works_collection gruplac_related_works_corrected_20260823 \
  --scienti_gruplac_audit_verification_run_name gruplac_incomplete_check_corrected_20260823 \
  --scienti_gruplac_audit_name gruplac_normalization_audit_v2_20260825
```

## Idempotencia y publicación

- Personas: upsert por `cod_rh` o `investigador_id`.
- Grupos: upsert por `codigo_grupo`.
- HTML: replace por identificador estable, conservando hash y fecha.
- Páginas del directorio: replace por número de página y versión de filtro.
- Autoridades: upsert por hash de la clave bibliográfica.
- Trabajos: identificador estable basado en DOI o ISBN/título/año. Los
  singletons inseguros permanecen ligados a la ocurrencia de origen.
- Construcciones oro: etapas persistentes `extract_nodes`, `connect_doi`,
  `connect_title_year`, `materialize`, `audit` y `publish`. Una reejecución con
  la misma configuración continúa después del último checkpoint completo.
- Una corrida fallida conserva nodos, aristas y salida temporal para diagnóstico.
- La publicación exige auditoría sin anomalías críticas, rehúsa sobrescribir el
  destino versionado, lo crea por renombre atómico y sólo entonces actualiza
  `scienti_work_graph_publications`. La colección anterior permanece intacta.

### Afiliaciones de autores y grupos

La afiliación de un autor a un producto se materializa exclusivamente cuando se
cierra la relación `producto -> grupo -> integrante -> institución`: el producto
aparece en el GrupLAC, el `cod_rh` del autor figura como integrante durante todo
el año de publicación y la institución histórica es exacta para ese año o
coincide en las convocatorias inmediatamente anterior y posterior. No se usan
trayectorias profesionales ni coincidencias de nombre. Los casos incompletos,
parciales o contradictorios quedan vacíos.

Los autores exponen `affiliations` como lista de evidencias GrupLAC. La relación
del grupo dentro del trabajo expone solamente `affiliations` como cadena con la
institución, o `""` cuando no es demostrable. La versión de esta regla forma
parte de la configuración reanudable de la corrida.

### Puerta del constructor definitivo

Antes de extraer un nodo se comprueba que la captura CvLAC esté completa, que
su HTML tenga cobertura exacta y que las dos auditorías nombradas estén en
estado `passed`. Cada auditoría debe corresponder exactamente a su colección
normalizada, reconciliar origen y destino y reportar cero errores críticos y
de parser. GrupLAC exige además igualdad entre grupos reconocidos con URL,
capturas HTML y documentos normalizados.

### Orquestación completa desde cero

La ejecución integral se controla con `scienti_pipeline_runs` y un bloqueo
único en `scienti_pipeline_locks`. Cada etapa tiene configuración inmutable,
intentos, fechas, resumen y error. Una repetición con la misma configuración
reanuda; una configuración diferente debe usar otro `run_name`.

El orden de dependencias es deliberado:

```
datos abiertos + JSON histórico + directorio público
                 ^
       PDF con texto nativo (opcional)
                 |
                 v
       resolución de personas y grupos
                 |
                 v
 GrupLAC -> normalización -> verificación -> auditoría
                 |
                 | aporta members.cod_rh
                 v
 manifiesto CVLAC completo -> HTML -> normalización -> auditoría
                 |
                 v
 tesis/proyectos/patentes/eventos -> rutas exactas -> cuatro colecciones Kahi
                 |
                 v
       auditorías semánticas -> publicación atómica
                 |
                 v
        grafo bibliográfico base auditado
                 |
gruplac_production_data -> productos medidos -> enlaces seguros
                 |
                 v
             grafo final enriquecido
                 |
                 v
 auditoría bibliográfica -> publicación conjunta -> limpieza exacta
```

GrupLAC precede al manifiesto CVLAC para no perder perfiles privados o poco
visibles cuyos identificadores solo aparecen como miembros de grupo. El
manifiesto también incorpora las coincidencias múltiples conservadas por la
búsqueda histórica. Los datos abiertos se escriben primero en colecciones de
construcción deterministas y solo se renombran después de comparar exactamente
el número de filas con los metadatos de Socrata.

La entrada histórica predeterminada son `investigadores.json` y `grupos.json`.
El modo PDF es opcional y únicamente acepta documentos con texto nativo: primero
regenera de forma determinista el Excel y los dos JSON, y después continúa por
la misma secuencia. No existen dos ramas posteriores de procesamiento.

Cada `snapshot_tag` versiona las capturas HTML, las normalizaciones por perfil,
las cuatro entidades, el grafo base, los productos oficiales normalizados, sus
enlaces y el grafo final. Las cuatro entidades se publican en
`scienti_entity_publications` solo cuando su auditoría interna y las cuatro
auditorías semánticas terminan sin hallazgos críticos. Una convocatoria nueva
se publica como `audited_snapshot`; las comparaciones exhaustivas se reservan
para migraciones de parser sobre una misma captura.

La normalización de entidades usa `scienti-exact-router-v5` y el catálogo JSON
reproducible de `Kahi_impactu_type_catalog`, generado desde `ALL`, `COAR`,
`REDCOL` e `INFO-EU-REPO` de `Tipos_ImpactU_Definitivo.xlsx`. La ejecución no
necesita el Excel ni mantiene una copia propia del catálogo. Una ruta requiere
la combinación exacta de fuente, canal, sección y tipo conocida. No usa
subcadenas, distancia de edición ni semejanza de títulos. Los tipos no
catalogados permanecen en las colecciones normalizadas por perfil y se reportan
como `unmapped`. En tesis, la persona orientada es autor y tutor, cotutor o
asesor se conserva como `authors.type = "advisor"`. Los cuatro destinos son
versionados, reanudables y se auditan antes de marcar la etapa como completa.

El grafo `works` usa el mismo enrutador antes de materializar cualquier nodo:
proyectos, patentes y eventos se excluyen y contabilizan. Software, docencia,
diseños industriales, variedades, circuitos integrados y demás productos que
el catálogo asigna a `works` se conservan allí.

Después de publicar las cuatro normalizaciones auditadas se construyen dos
grafos independientes: `scienti-project-graph-v1` y
`scienti-patent-graph-v1`. Ambos trabajan sobre colecciones inmutables,
mantienen checkpoints y publican el destino mediante renombrado atómico. Solo
enlazan títulos y subtipos exactos con un `cod_rh` o código de grupo compartido.
Fechas, países y registros distintos bloquean la unión; títulos genéricos y
grupos de candidatos demasiado grandes permanecen separados. La auditoría
final exige cobertura uno-a-uno de todos los documentos de origen. Eventos no
tienen grafo en esta versión.

La publicación final es conjunta: `scienti_final_release_publications.current`
apunta simultáneamente a `works`, `projects`, `patents` y `events` solo cuando
las cuatro materializaciones y la auditoría bibliográfica terminaron sin
anomalías. Esta última impide publicar editoriales que sean ISBN o etiquetas
filtradas, volúmenes o páginas contaminados, ediciones que absorban otros
campos, idiomas que sean prosa y años usados como lugar de publicación. Los
grafos base y finales no sustituyen punteros públicos mientras se construyen:
los punteros anteriores se restauran hasta la publicación conjunta. Después,
la limpieza elimina por nombre exacto las
normalizaciones, enlaces y grafos intermedios del run y las salidas finales
sustituidas. Conserva fuentes abiertas, HTML congelados, manifiestos,
resoluciones de identificadores, auditorías, registros de ejecución y las
cuatro salidas actuales. También conserva la captura normalizada de entidades
actual y su puntero para compararla con la siguiente corrida; elimina la captura
normalizada anterior una vez sustituida.

Las cadenas editoriales compuestas se conservan literalmente en
`source.publisher.name`. Una autoridad exacta puede añadir entidades separadas
en `bibliographic_info.scienti.publisher_entities`; las divisiones sugeridas
por separadores explícitos quedan como candidatos y nunca reemplazan el valor
canónico sin verificación.

### Capa oficial de productos medidos

`gruplac_production_data` se conserva como evidencia oficial de medición, no
como autoridad bibliográfica de autores. La capa produce:

- `minciencias_measured_products_<snapshot_tag>`: un documento compatible con la estructura
  superior de `Kahi.works` por `id_producto_pd`, con todas sus convocatorias,
  categorías, grupos y propietarios en `bibliographic_info.minciencias`.
- una colección de decisiones `linked`, `ambiguous` o `unlinked` para cada
  destino exacto (`works`, `projects`, `patents`, `events`).
- cuatro colecciones finales: los enlaces seguros enriquecen la entidad
  scrapeada y los demás productos se preservan como identidades oficiales
  independientes.
- `minciencias_measurement_runs`: configuración, checkpoints, métricas y
  auditorías de las tres fases.

El propietario `id_persona_pd` nunca se añade automáticamente a `authors`. La
fecha `fcreacion_pd` se conserva como fecha de presentación y tampoco se usa
como `year_published`. Sirve sólo como año exacto de enlace cuando coincide con
el año scrapeado del destino. Cada producto solo participa en el destino
indicado por `target_entity`; nunca cruza hacia otra entidad.

La unión destinada a Kahi es inclusiva: todo trabajo scrapeado permanece en el
grafo. Los productos oficiales de tipo `works` con enlace seguro enriquecen el
trabajo scrapeado; los ambiguos y no enlazados se materializan como identidades
oficiales independientes. Los productos sin título permanecen como evidencia
normalizada y se contabilizan, pero no se publica una obra sin título. Los
productos cuyo destino es proyecto, patente o evento no pueden enlazarse con
`works`. La fecha oficial de presentación no se convierte en fecha real de
proyecto o evento. Todos los campos matriciales se publican como listas,
incluido `groups: []` cuando no existe evidencia directa de grupo.

Ejemplo para las capturas auditadas de agosto de 2026:

```bash
bin/yuku_run \
  --mongo_dbname dam \
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

El avance queda en
`cvlac_works_graph_full_v1_20260825_graph_runs`, bajo el documento
`scienti_full_graph_v1_20260825`. Para reanudar se usa el mismo comando; cambiar
fuentes, parámetros o destino exige un nombre de corrida nuevo.

### Estrategia de rendimiento del grafo

- Cada ocurrencia recibe un `node_seq` entero denso y un `cluster_seq`. El
  Union-Find usa arreglos NumPy `int32`, `uint32` y booleanos: nueve bytes fijos
  por nodo, en lugar de varios diccionarios Python indexados por SHA-256.
- Los resúmenes bibliográficos se crean sólo para nodos que participan en un
  grupo candidato; los singletons no ocupan estructuras Python adicionales.
- La búsqueda exacta por DOI usa únicamente `identity_dois`, evitando agrupar
  DOI inválidos que nunca podrían producir una arista.
- Título/año se divide por una partición SHA-256 determinista. Cada partición
  queda registrada dentro del checkpoint `connect_title_year`; una caída
  recalcula sólo la partición incompleta.
- Los índices de nodos se construyen después de la extracción: `node_seq`
  único, `cluster_seq`, `identity_dois` y el compuesto
  `title_partition, title_key, year`.
- La materialización consulta colisiones de `_id` por lote en MongoDB. No
  mantiene en RAM un conjunto con millones de identificadores de trabajos.
- La escritura masiva de `cluster_seq` se difiere hasta terminar todas las
  particiones. Los 6,7 millones de valores sólo se restauran si una caída
  ocurrió durante esa escritura final.

## Controles antes de publicar a Kahi

1. Recuento de entradas, nodos, componentes y trabajos.
2. Distribución de tamaño de componentes y autores.
3. Cantidad de conflictos DOI, ISBN, tipo y autoría.
4. Obras cuyo `author_count` aumenta o disminuye frente a la versión anterior.
5. Separación explícita libro/capítulo.
6. Grupos asociados sin que su líder o integrantes aparezcan como autores por
   esa sola relación.
7. Estabilidad de `_id` al añadir una ocurrencia GrupLAC.
8. Muestra estratificada de artículos, libros, capítulos y tesis.
9. Verificación de que los plugins Kahi consumen sólo la colección publicada y
   no las capas bronce/plata.

## Migración de los scripts de 2026

Los scripts externos deben conservarse temporalmente como referencia. Se
retiran únicamente cuando:

1. el comando Yuku procesa sus mismos archivos de entrada;
2. produce los mismos estados y campos;
3. reutiliza el caché o demuestra equivalencia de resultados;
4. pasa una comparación completa de conteos y hashes de registros;
5. existe documentación de sustitución del comando anterior.

Así se evita mantener dos implementaciones activas y, al mismo tiempo, no se
pierde una ruta de recuperación durante la transición.
