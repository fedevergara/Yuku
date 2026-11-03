# Calidad de proyectos y patentes

La política `minciencias-project-patent-quality-v1` se aplica a las entidades
`projects` y `patents`, tanto en scraping como en datos abiertos. No cambia las
llaves ni su orden en KahiBase, ni modifica el catálogo compartido.

- Fechas reales: calendario válido, desde 1901 y hasta la fecha de ejecución
  en Bogotá. El año 1900 y las fechas futuras no son evidencia válida de un
  inicio, finalización real o presentación de una patente. Los valores
  originales siguen disponibles en las ocurrencias, las fuentes o los
  `normalization_findings` de las mediciones.
- Una fecha anual no se convierte en enero 1. Si varias ocurrencias discrepan
  en el mes pero comparten año, se publica solamente el año y timestamp null.
  Si discrepan en el año, ambos campos quedan null. Se recalcula al fusionar;
  no depende del orden de lectura. Un fin proyectado no es un fin real.
- `titles.lang` queda vacío sin una declaración explícita de idioma.
- Los títulos se limpian antes de generar identidades: entidades HTML
  anidadas, composición Unicode NFC y espacios. No se adivina el texto dañado.
- Se excluyen coincidencias completas con la lista `PLACEHOLDER_TITLES` del
  módulo `scienti_product_quality`. Títulos cortos como GEIO y títulos como
  «Prueba de concepto para…» no se excluyen por esa regla. Un producto oficial
  sin título útil conserva su evidencia normalizada con destino `excluded`;
  no entra a los enlaces ni a los grafos finales.
- `Pendiente` y otros registros sin clave utilizable no son identificadores
  para conciliar. El texto original queda como evidencia.
- Las patentes con títulos cortos pueden unirse con registro y país únicos,
  autor identificado compartido y título normalizado exacto. Se mantienen las
  barreras por país, registro, año y, en esta regla, mes/día de presentación.
  Un grupo compartido no sustituye al autor. Se conservan las ocurrencias.
- Se incorporan coautores explícitos de los registros. Los integrantes de un
  grupo y el propietario de una fila de Sócrata no se convierten en autores.
  Sin evidencia suficiente se mantiene `authors: []`.
- Los tipos ImpactU usan una entrada por etiqueta, sin niveles ni parent
  nativos; los tipos de origen conservan sus contextos diferentes.

El normalizador es `scienti-entity-normalizer-v6`, el pipeline es v18 y los
grafos de proyectos y patentes son v2. Los controles de mediciones incluyen la versión de calidad
en su configuración para impedir reanudar checkpoints con la política previa.
La publicación final rechaza títulos de relleno, fechas inválidas y falta de
consenso normalizado; no reemplaza `current` cuando falla la validación.

Los cambios requieren una nueva construcción desde las fuentes conservadas.
No corrigen en sitio las colecciones publicadas ni exigen nuevos workflows.
