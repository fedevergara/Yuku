# Filas reales de eventos CvLAC

Filas HTML extraídas de dam.cvlac_stage_raw_full_corrected_20260823 por el
identificador cod_rh usado como nombre de archivo. Conservan las nueve variantes
con etiquetas Tipo de evento repetidas revisadas el 2026-10-17, incluida SOCLA.

original-records.txt contiene la salida del parser 3.5.0 para estas filas,
con un profile_author_name fijo «Fixture author». La regresión compara todos
los campos y permite únicamente la normalización autorizada de event_type.
raw_text conserva el texto íntegro de cada fila. No se necesitan solicitudes
HTTP ni MongoDB para ejecutar las pruebas.
