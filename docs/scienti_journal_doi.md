Las nuevas publicaciones siguen el [contrato de nombres y retención](minciencias_publications.md). Los nombres de octubre de 2026 citados como resultados son históricos.

# Revistas y confirmación de DOI

La normalización CvLAC/GrupLAC ahora extrae el nombre de revista, la editorial,
el volumen y el fascículo de artículos. Conserva ISSN y páginas de la extracción
local. Las consultas DOI sólo confirman identidad: no enriquecen el trabajo.

## Extracción local

`scienti_journals.journal_context` reconoce etiquetas explícitas de revista y
el espacio bibliográfico acotado antes de `ISSN:`. El formato histórico CvLAC,
que no siempre separa país y revista, utiliza un catálogo explícito de nombres
de países; los formatos que no se pueden separar quedan sin nombre de revista.
Los normalizadores pasan a versión `3.3.0`; las entidades de identidad siguen
aceptando entradas auditadas `3.2.0` y `3.3.0`.

La extracción actual usa CvLAC `3.4.0` y GrupLAC `3.4.1`. Personas y
afiliaciones también aceptan las versiones `3.4.0` auditadas y, para GrupLAC,
la corrección `3.4.1`. Se mantienen los controles de procedencia, auditoría
aprobada y versiones conocidas.

En `Generación de Contenido Impreso`, GrupLAC obtiene `publication_place`
de la etiqueta `Lugar de publicación:` y se detiene antes del siguiente campo.
Una etiqueta vacía o ausente conserva un lugar vacío. El prefijo fecha/ámbito
no se interpreta como país ni lugar; libros y artículos conservan su formato
acotado de país seguido de coma. La evidencia HTML y `raw_text` se preservan.
Los valores explícitos que la auditoría considera inválidos (fechas, ISBN/ISSN
u otras etiquetas en lugar de una ubicación) quedan fuera del campo normalizado
y se conservan en `bibliographic_findings`, con su valor original y motivo.
Una ejecución fallida construida con un snapshot de código `3.4.0` conserva
su extracción anterior: cambiar el repositorio no corrige esas colecciones
ni invalida automáticamente las etapas ya completadas. Se deben regenerar
las entradas y dependencias afectadas con la versión corregida antes de
repetir la auditoría bibliográfica y continuar personas y afiliaciones.

El grafo conserva los valores y sus ocurrencias en
`bibliographic_info.scienti.fields`. Si el nombre de revista es consistente,
construye `source` con tipo `journal`, nombre, ISSN y editorial cuando existe
evidencia consistente. Los valores bibliográficos contradictorios se conservan
como conflictos. Un ISSN no se convierte en identificador de identidad del
artículo. Las reglas existentes para libros y capítulos continúan vigentes.

## Conciliación opcional

El pipeline incluye `doi_reconciliation` después de `cvlac_audit` y antes de
`entities_normalize`. Para activarla:

```json
{"processing": {"confirm_conflicting_dois": true, "doi_request_delay": 1.0}}
```

Desactivada por defecto, permite la extracción local de revistas sin consultas
DOI. Cuando está activa, las entidades normalizadas y el grafo consumen las
vistas conciliadas y sus auditorías de transformación.

1. Canonizar DOI con la política local y los normalizadores compartidos.
2. Indexar únicamente ocurrencias que el catálogo enruta a `works`, con DOI
   localmente válido y título normalizado de al menos 15 caracteres.
3. Buscar candidatos por título exacto o pares de los tres tokens más largos,
   dentro de la misma familia de trabajo y prefijo DOI. Estos bloques son una
   búsqueda conservadora, no una comparación de todos los títulos del universo.
4. Exigir similitud de título >= 0,97 y DOI distintos, con truncamiento en un
   separador o hasta dos ediciones y similitud relativa >= 0,90. El prefijo
   compartido por sí solo no basta. Los bloques mayores de 100 quedan registrados
   sin consultar ni corregir automáticamente.
5. Consultar sólo esos candidatos. El servicio Handle comprueba resolución;
   la agencia registradora proporciona únicamente los campos de confirmación.
   Crossref utiliza `filter=doi:…&select=DOI,title,published,type&rows=1`;
   DataCite utiliza `fields[dois]=doi,titles,publicationYear,types`.
6. Comparar título (>= 0,98), año y tipo con las dos ocurrencias locales.
   Corregir únicamente cuando un DOI confirma ambas y el otro devuelve
   `404/responseCode=100` en Handle. Dos DOI encontrados o una incompatibilidad
   permanecen para revisión. Una respuesta 429, un fallo temporal o una agencia
   no soportada queda pendiente; nunca prueba que el DOI sea inválido.
7. Consultar primero `minciencias_doi_resolution_history`, una colección permanente
   excluida de la retención de versiones. Su clave depende del DOI canónico, no
   del nombre del grafo ni de la corrida. Las comprobaciones antiguas se migran
   automáticamente, sin HTTP, antes de consultar candidatos o retirar controles.
   La migración conserva los resultados y sus huellas originales; no inventa
   títulos o fechas remotos ausentes en los controles antiguos.
8. Conservar únicamente DOI, título, año y tipo recibidos para comprobar identidad,
   agencia, códigos HTTP, fechas, política y comparaciones con el contexto local.
   Un contexto nuevo se compara con esa evidencia mínima; si sólo existe una
   comparación histórica y falta el contexto solicitado, se consulta ese DOI.
   Los positivos se reutilizan; los 404 vencen a los siete días; los pendientes se
   reintentan. Una exclusión temporal por DOI evita solicitudes concurrentes.
   Los datos remotos nunca enriquecen títulos, autores, revista o editorial del
   trabajo. Las métricas distinguen aciertos del historial y solicitudes HTTP.

Las correcciones usan exclusivamente DOI ya declarados por ScienTI: no completan
un sufijo por conjetura. Conservan el original, la decisión y la referencia al
candidato en la evidencia. Si existen propuestas incompatibles para una misma
identidad local, no se aplica ninguna.

El proceso tiene checkpoints por fase, reservas de destino, una exclusión
temporal por ejecución e historial permanente por DOI. Una fase ya confirmada
continúa su copia; una nueva corrida vuelve a evaluar candidatos usando primero
el historial y reintenta sólo pendientes, negativos vencidos o contextos sin
prueba suficiente. Los controles por corrida conservan referencias al historial.


## Reconstrucción desacoplada

El controlador verifica la versión de los parsers de las entradas auditadas.
Si es anterior a 3.4.0, vuelve a extraer y auditar los HTML originales antes de
conciliar DOI; así recupera títulos que una extracción anterior confundió con
tipos. Con entradas vigentes reutiliza sus textos locales. Crea vistas nuevas y verifica documento por documento
que sólo se aplicaron las transformaciones previstas, con SHA-256 de entrada y
salida. Las auditorías de transformación heredan la cobertura del parser y del
HTML de la auditoría original; no afirman que se haya vuelto a ejecutar el parser.

Después reconstruye el grafo de trabajos, normaliza y enlaza las mediciones,
materializa trabajos finales, audita contexto bibliográfico, crea una publicación
de cuatro entidades, reconstruye personas y valida las referencias de la nueva
publicación de seis entidades. Proyectos, patentes, eventos y afiliaciones se
reutilizan desde la publicación explícita anterior. Finalmente cuenta y genera
la colección identificada con la política de DOI/ISBN/par ScienTI existente y
el orden exacto de `empty_work`.

```bash
python scripts/run_scienti_journal_doi.py \
  --source-release scienti_kahi_release_journal_doi_v2_20261008 \
  --run-name journal_doi_20261009 \
  --publication-date 20261009 \
  --confirm-conflicting-dois \
  --output-dir /home/fvergara/reports/yuku-journal-doi-20261009
```

Se requieren las dependencias opcionales `Yuku[identified_works]`.
`YUKU_MONGO_URI` configura la conexión; la URI no se escribe en el reporte.
El controlador ejecuta las pruebas antes de reconstruir, usa `Popen` con sesión
nueva y `DEVNULL`, y genera `job.pid`, `status.json`, `job.log`, resultados por
fase y `report.json`. El mismo comando continúa los checkpoints si el proceso
termina con un fallo. No importa trabajos en Kahi. Mantiene los resultados actuales
hasta que el nuevo grafo completo y la nueva selección pasan sus auditorías.
Entonces actualiza los punteros `current`, publica el par en un único documento
`scienti_identified_work_publications.current` y retira las versiones anteriores
de trabajos y los tres intermediarios de mediciones/grafo base. Una interrupción
del cierre permite continuar sin volver a abrir el grafo anterior retirado.
Las fuentes HTML y las vistas normalizadas necesarias permanecen disponibles;
las auditorías, decisiones DOI y publicaciones históricas conservan la trazabilidad.
Las vistas pueden existir durante su construcción, pero sólo una auditoría
aprobada permite que los consumidores las usen.

Los únicos resultados finales de trabajos de esta ejecución son:

- `scienti_works_final_journal_doi_v2_20261008`: grafo completo con mediciones.
- `scienti_works_identified_v2_20261008`: proyección para Kahi, admitida por DOI,
  ISBN o el par coherente COD_RH–COD_PRODUCTO.

`scienti_works_base_journal_doi_v2_20261008` es un intermedio que se elimina al
cerrar, no un tercer resultado permanente. Durante la construcción coexisten
las versiones anteriores, las nuevas y los temporales: la regla de dos se aplica
al cierre exitoso. `scienti_work_graph_publications` y
`scienti_final_release_publications` contienen metadatos de publicación, no grafos.
El cierre se bloquea si encuentra otro grafo sin clasificar, una colección aún
protegida por un puntero o un proceso incompleto que depende de los candidatos;
no borra colecciones por una coincidencia de nombre.
Los controles `*_graph_runs` y `*_graph_review` se conservan y no cuentan como
resultados de trabajos cuando una publicación del grafo y su checkpoint completo
prueban esa función. Una terminación del nombre por sí sola no permite excluir
una colección desconocida de la revisión.

El fallo anterior al ordenar DOI se corrigió filtrando valores nulos, vacíos y
no canónicos antes de ordenar. El checkpoint de la implementación afectada se
puede migrar exclusivamente antes de confirmar DOI, sin consultas previas ni
cambios de configuración o auditorías de origen; la migración queda registrada.

Ejemplo: el DOI truncado `10.14254/2071-789X.2024` puede corregirse al DOI declarado
completo `10.14254/2071-789X.2024/17-1/7` tras confirmar título, año y tipo. Los HTML
del ejemplo declaran dos revistas distintas: ese conflicto se conserva incluso
después de confirmar el DOI; no se resuelve con metadatos de Crossref.

Referencias de las consultas limitadas:
[filtros Crossref](https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-filters/),
[selección de campos Crossref](https://www.crossref.org/documentation/retrieve-metadata/rest-api/tips-for-using-the-crossref-rest-api/),
[campos DataCite](https://support.datacite.org/docs/api-queries).
