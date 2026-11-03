# Tipos ScienTI y confirmación DOI

`Kahi_impactu_type_catalog` define los tipos ImpactU canónicos y las equivalencias
de los dumps ScienTI y los datos abiertos. El scraping utiliza su propio
[catálogo revisable y Excel](minciencias_scraping_types.md), sin modificar ese
repositorio. Se heredan sólo equivalencias inequívocas de etiquetas y contexto;
las nuevas asignaciones quedan explícitas y los casos dudosos, pendientes.

`types.source = impactu` utiliza exclusivamente los nombres canónicos del
catálogo. Los tipos originales de Yuku usan `source: minciencias`, tanto para
HTML como para datos abiertos; se validan conservando su clasificación completa.
`scienti` se reserva a los consumidores de los dumps `scienti_***_2024`. Por
ejemplo, `Producción bibliográfica - Artículo - Caso clínico` conserva su subtipo
nativo y se clasifica como `Artículo de revista` para ImpactU. Una categoría
ambigua no se convierte automáticamente en `Otro`.

Los parsers 3.5.0 verifican el encabezado antes de retirar texto del título:
`Women in Academia and Research: An Overview of the Challenges Toward Gender
Equality in Colombia and How to Move Forward`, en negrita, se conserva como
título y no como tipo. La evidencia original queda en `raw_text` y los rechazos
en `type_findings`. El grafo y la materialización final conservan los rechazos
de vocabulario en `type_audit`, separado de `types`; la selección filtrada copia
los campos previstos de `empty_work` y no exporta este control interno.

La publicación del grafo final y de la selección filtrada exige tipos válidos.
Los consumidores deben usar los punteros `current` sólo después de esas
auditorías. Las colecciones existentes no se reparan en el lugar: requieren
una nueva extracción y publicación. El controlador de revista/DOI también
vuelve a extraer los HTML si sus entradas usan parsers anteriores a la versión actual.

Para consultar únicamente tipos ImpactU, se filtran los elementos del arreglo:

```python
list(doi_works.aggregate([
    {"$unwind": "$types"},
    {"$match": {"types.source": "impactu"}},
    {"$group": {"_id": "$types.type"}},
    {"$sort": {"_id": 1}},
]))
```

`distinct("types.type", {"types.source": "impactu"})` selecciona documentos
que contienen un tipo ImpactU, pero devuelve también los tipos de otras fuentes
de esos mismos documentos. Esa consulta no demuestra por sí sola contaminación.

El historial permanente `minciencias_doi_resolution_history` almacena evidencia
mínima para confirmar identidad y se consulta antes de HTTP. Las comparaciones
dependen del título, año, familia y política de comprobación, no del ID de un
grafo. Las comprobaciones antiguas se migran sin nuevas solicitudes; cuando
carecen de metadatos de confirmación, sólo sirven para el mismo contexto local.
Los 404 vencen en siete días y los pendientes se reintentan. La retención migra
las pruebas antiguas antes de retirarlas y protege expresamente este historial.
Los datos de registros DOI no enriquecen los metadatos publicados del trabajo.
