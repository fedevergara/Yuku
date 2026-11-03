# Catálogo de tipos del scraping de Minciencias

La autoridad editorial es el catálogo compartido `Kahi_impactu_type_catalog`,
versión `1.2.0`. Su Excel tiene una sola hoja con las 800 correspondencias
originales intactas, dos tipos originales sin asignación y 180 tipos de scraping.
Yuku consume sus asignaciones publicadas. El recurso local
`yuku/data/minciencias_scraping_types_v1.json` (inventario `1.0.1`) conserva las
observaciones, frecuencias, ejemplos y decisiones, sin reemplazar los destinos
del catálogo compartido. La copia de revisión está en
`docs/Catalogo_tipos_scraping_minciencias_v1.xlsx`.

Los tipos con fuente `scienti` del catálogo original de `Tipos_ImpactU_Definitivo.xlsx`
pertenecen a los dumps `scienti_***_2024`, no a los HTML. Las etiquetas de los
HTML pueden coincidir con esas rutas o con tipologías de datos abiertos. Yuku
hereda una equivalencia sólo si la etiqueta y su contexto permiten obtener un
único par entidad/tipo ImpactU. Las diferencias de presentación y de nombres
de sección se declaran explícitamente en `scraping_type_catalog.py`; no hay
coincidencias difusas ni inferencias desde palabras del título.

## Etiquetas publicadas

| Información | provenance | source |
|---|---|---|
| Tipo original extraído de HTML | minciencias | minciencias |
| Tipo homologado | minciencias | impactu |
| Tipo original de datos abiertos | minciencias | minciencias |

Los identificadores conservan su espacio de nombres. La procedencia de una
equivalencia (`scienti`, `minciencias` o `impactu`) se registra en la evidencia
del catálogo, sin atribuir al HTML el origen de los dumps. El canal CvLAC o
GrupLAC sigue registrado en las ocurrencias y controles existentes. No cambia
KahiBase ni el orden de las claves de sus entidades.

Ejemplos reales de clasificaciones observadas:

* `Articulos` / `Producción bibliográfica - Artículo - Caso clínico` hereda
  `works` / `Artículo de revista`.
* `Generación de Contenido Impreso` / `Manual` hereda `works` /
  `Publicaciones editoriales no especializadas`.
* `Proyectos` / `Investigación-Creación` recibe una asignación explícita a
  `projects` / `Proyecto`: el contexto corresponde a un proyecto y no a una obra.

En eventos, una declaración repetida como `Congreso Tipo de evento: Otro` se
normaliza a `Otro`, manteniendo `Evento` como tipo ImpactU. Se utiliza la última
declaración explícita sólo si contiene un tipo de evento conocido. El texto
original permanece en `raw_text`; los valores desconocidos no se adivinan.

Un tipo desconocido no hereda la clasificación de su sección. Si el parser
descarta un texto en negrita, conserva esa evidencia sin incorporarla como tipo.
Los casos pendientes o ambiguos no reciben un tipo ImpactU; se conservan en el
catálogo para revisión y en los controles de extracción/materialización.

## Revisar el Excel

El archivo tiene una sola hoja, **Catalogo**, con todas las correspondencias del
catálogo original (ALL, COAR, REDCOL e INFO-EU-REPO, incluidas dos sin asignación)
y los tipos del scraping. Usa exactamente estas siete columnas:

| Columna | Contenido |
|---|---|
| Fuente | Vocabulario original; los tipos del scraping usan minciencias. |
| Origen | socrata, scraping o dumps para Minciencias; el proveedor original para las otras fuentes. |
| Parent | Categoría o jerarquía; desglosada de los tipos compuestos originales. |
| Type | Tipo original, sin su prefijo de categoría cuando se desglosa. |
| Tipo ImpactU | Asignación original; vacía para tipos nuevos del scraping. |
| Entidad | works, projects, patents o events. |
| Tipo ImpactU Sugerido | Propuesta para el scraping o corrección que revisará el equipo. |

`socrata` corresponde a datos abiertos de `gruplac_production_data`; `scraping`,
a HTML CvLAC/GrupLAC; `dumps`, a `scienti_***_2024`. Cuando coinciden Fuente,
Parent, Type y Entidad, se combinan los orígenes como `socrata; scraping`.
Compartir una traducción ImpactU no basta para combinar filas ni orígenes.
Las fuentes `scienti` y `minciencias` permanecen separadas.

Ejemplo: `minciencias | scraping | Generación de Contenido Impreso | Manual |
[vacío] | works | Publicaciones editoriales no especializadas`.
La versión 1.0.1 no tiene asignaciones pendientes. Las nueve variantes con etiquetas
repetidas de eventos se asignan a Evento; Programa radial, Iniciación Científica
y Regulaciones y Normas, en los contextos revisados, se asignan provisionalmente
a Otro por decisión del equipo. Los futuros pendientes aparecerán vacíos y en amarillo.
Los ejemplos, variantes, frecuencias y evidencias permanecen en el JSON de
inventario; no son columnas ni pestañas del Excel simplificado. El JSON del
catálogo compartido conserva las rutas originales completas, incluidos sus códigos.

Para corregir una asignación, editar **Tipo ImpactU Sugerido**. Se conservan
las otras columnas para identificar la fila y mantener la asignación original
visible. Para agregar un tipo, añadir una fila con Fuente `minciencias`, Origen
`scraping`, Parent, Type, Entidad y la sugerencia; dejar Tipo ImpactU vacío.
No eliminar filas ni cambiar sus columnas de identidad.

Importar a un archivo nuevo, indicando una versión nueva:

```bash
python scripts/manage_scraping_type_catalog.py import \
  --catalog yuku/data/minciencias_scraping_types_v1.json \
  --excel docs/Catalogo_tipos_scraping_minciencias_v1.xlsx \
  --version 1.0.2 --output /tmp/minciencias_scraping_types_reviewed.json
```

La importación comprueba revisión del libro, filas completas, conservación de
las columnas originales y pares canónicos de entidad/tipo. Las correcciones del
scraping quedan en el catálogo revisado; las sugerencias para vocabularios
originales quedan en `shared_catalog_suggestions` para una revisión independiente.
El comando no modifica el catálogo compartido ni publica colecciones. El libro
conserva su revisión en sus propiedades, sin pestañas ni columnas técnicas.
También se admite reimportar los libros detallados exportados anteriormente.

El JSON importado es una propuesta de revisión. Para publicar un cambio se debe
actualizar el Excel compartido y regenerar su artefacto con
`scripts/generate_impactu_type_catalog.py --catalog-version NUEVA_VERSION` en
`Kahi_impactu_type_catalog`; comprobarlo con `--check` y ejecutar las pruebas.
Actualizar únicamente el inventario local no cambia los destinos publicados.
La incorporación de un tipo ImpactU nuevo o cambios al catálogo compartido
requieren discutirlos previamente. La sesión de reconstrucción fija una copia
del paquete compartido y de Yuku para no mezclar reglas durante la ejecución.

## Reproducibilidad y nuevas sesiones

El parser CvLAC 3.6.0 incorpora las secciones Documentos de trabajo, Traducciones,
Traducciones Filológicas y Edición de Fuentes y Manuales y Guías Especializadas,
cuyos tipos ya estaban revisados en el catálogo desde GrupLAC.

El parser CvLAC es `3.6.0` y el de GrupLAC, `3.6.0`; la normalización de entidades
es v5 y el router es v7.
Normalización, grafo, materialización y pipeline registran la versión y huella
del catálogo. Una sesión iniciada con otra huella no se reanuda con reglas nuevas.
El cambio no modifica colecciones existentes ni sus punteros `current`.

El inventario fija las fuentes de `scienti_entity_publications.current` al
iniciarse y recorre únicamente proyecciones de los campos necesarios:

```bash
python scripts/launch_scraping_type_inventory.py --output /ruta/reporte_nuevo
tail -f /ruta/reporte_nuevo/inventory.log
```

El controlador genera `pid`, `status.json`, log, inventario JSON, Excel y reporte;
no escribe en MongoDB. Para actualizar propuestas desde un inventario ya
capturado, sin volver a consultar la base:

```bash
python scripts/manage_scraping_type_catalog.py consolidate \
  --inventory /ruta/reporte_nuevo/catalog.json --output /ruta/consolidado
```
