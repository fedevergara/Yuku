# Publicaciones Minciencias

Las nuevas ejecuciones publican `minciencias_{entidad}_vXX_aaaammdd`.
Las entidades son `works`, `person`, `projects`, `events`, `patents` y
`affiliations`. Kahi sigue usando el rol interno `persons`; sólo el nombre de
la colección usa `person`. Cada entidad tiene su propio contador.

La proyección de trabajos se llama exactamente como el grafo completo más
`_filtered`: por ejemplo, `minciencias_works_v03_20261009_filtered`.
No es otra versión ni otro grafo. Conserva el criterio DOI, ISBN o par
COD_RH–COD_PRODUCTO; un ISSN por sí solo no admite un trabajo.

## Configuración

```json
{
  "publication": {
    "date": "20261009",
    "versions": {
      "works": "auto", "person": "auto", "projects": "auto",
      "events": "auto", "patents": "auto", "affiliations": "auto"
    }
  },
  "processing": {
    "generate_identified_works": true,
    "confirm_conflicting_dois": false,
    "cleanup_transient": true
  }
}
```

`auto` toma la siguiente versión de esa entidad, considerando `current` y
colecciones canónicas ya construidas. También admite una versión explícita,
como `v03`. Las versiones resueltas se guardan en el checkpoint: reanudar no
asigna otra versión. Conviene fijar `publication.date` en el JSON para conservar
la fecha incluso si la ejecución atraviesa varios días.

La generación filtrada es un parámetro separado. Si ya existe un par publicado,
no se puede reemplazar sólo el grafo completo dejando su filtro anterior como
`current`: el cierre exige generar el filtro sucesor. Desactivar la limpieza
publica los datos validados y conserva los artefactos.

Las materializaciones individuales de la API/CLI resuelven el destino con el
mismo contrato. Aceptan `version` y `publication_date`, o los argumentos
`--scienti_entity_version v03 --scienti_publication_date 20261009`.
Una colección explícita debe respetar el contrato y la entidad solicitada.
La selección independiente deriva el destino del release fijado; `--target`
es opcional y, cuando se usa, debe coincidir con ese nombre.
Si el release fijado es el `current` del grafo completo, la selección validada
actualiza también el `current` del par. Seleccionar un release histórico conserva
su publicación por nombre y no desplaza el par vigente.

## Orden del cierre

1. Construir y auditar los cuatro grafos finales con mediciones.
2. Guardar el release de cuatro entidades como prueba, sin cambiar `current`.
3. Materializar y auditar `affiliations` y `person`, sin cambiar sus punteros.
4. Validar referencias y guardar el release de seis entidades.
5. Contar y construir los trabajos filtrados, cuando el parámetro está activo.
6. Verificar pruebas y conteos, publicar `current` y retirar finales anteriores.

Los releases y auditorías son controles: no son otras colecciones de trabajos.
Durante una construcción coexistirán el resultado anterior, temporales y el
nuevo resultado; la eliminación se ejecuta después de validar y publicar.

## Dónde está `current`

El principal es `scienti_final_release_publications`, documento `_id: current`:
`current_release`, `audit` y `collections` apuntan al release validado.
Kahi resuelve sus entradas desde este documento; no necesita nombres finales
fijos. Los punteros por entidad continúan en:

| Entidad | Colección de control | Documento |
|---|---|---|
| works | scienti_work_graph_publications | current |
| works filtrados | scienti_identified_work_publications | current |
| person | scienti_person_publications | current |
| affiliations | scienti_affiliation_publications | current |
| projects, patents, events | scienti_final_entity_publications | current_projects, current_patents, current_events |

`minciencias_publication.publish_current` compara versión y fecha y utiliza
comparación e intercambio para rechazar publicaciones atrasadas concurrentes.
Cambiar un destino exige aumentar su versión y no retroceder la fecha.
El documento principal publica las seis entradas en una sola escritura.
Los punteros individuales se actualizan después; si hay una interrupción, se
reanuda el cierre. MongoDB sin transacciones no hace atómica la escritura de
todos los documentos individuales.

## Retención

`minciencias_entity_versions` registra explícitamente el propietario de cada
control, temporal y fuente. Los finales anteriores se retiran tras validar el
sucesor. Los controles y temporales conservan la versión vigente y su anterior,
por entidad, aunque existan saltos en la numeración. Las fuentes HTML y los datos
abiertos originales no se eliminan.

Una prueba antigua todavía necesaria para una entidad vigente se conserva hasta
que se sustituya esa dependencia. Las revisiones y comprobaciones DOI en
colecciones compartidas se eliminan por `_id` y ejecución propietaria, usando
únicamente esquemas conocidos; no se vacía la colección compartida.
Los artefactos sin propiedad demostrada y los de ejecuciones pendientes que
dependen de candidatos de limpieza requieren revisión y no se borran por prefijo.

La adopción de publicaciones históricas registra las pruebas existentes sin
reescribirlas ni renombrar datos. Su orden se determina por fecha, porque los
contadores anteriores mezclaban, por ejemplo, v11 de septiembre y v2 de octubre.
Los registros históricos sin `materialization_runs` completos se omiten del
catálogo de retención y se identifican en el log; sus datos y auditorías se
conservan. Una publicación nueva no puede registrarse ni convertirse en
`current` sin la prueba de materialización para cada entidad.
Las colecciones existentes mantienen sus nombres hasta una nueva construcción
auditada con este contrato. Este cambio de código no ejecuta esa reconstrucción
ni modifica por sí solo la base `dam`.
