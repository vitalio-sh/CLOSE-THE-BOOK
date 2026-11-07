# Handover: estado del trabajo (2026-11-07, 14:50 Madrid)

Léelo antes de tocar nada. Las reglas del reto y los comandos están en `CLAUDE.md`; aquí está **el estado actual y lo
que falta**.

## Plazos (hora de Madrid)

| Hora | Qué |
|---|---|
| 18:00 | Se publica FINAL (`/data/v1/bankrec-final.tar.gz`). Hay que descargarlo y extraerlo en `data/final/`. |
| 19:30 | **Cierre de envíos.** FINAL = un único envío puntuado (el primero válido cuenta). |
| 19:45 | Cierre de proyecto (vídeo de 5 minutos como máximo, repositorio, descripción). |
| 20:30 | Reveal. |

Congelar el código **a las 17:00**. Lo que no esté probado en dev a esa hora no entra en FINAL.

## Puntuaciones

| Medición | Score | Notas |
|---|---|---|
| dev, base de la mañana | 39,15 | `scripts/eval.py --phase dev` |
| dev + `remittance.py` | 40,31 | agente A |
| dev + `payments.py` | 42,02 | agente C (ap_direct_debit 94,5 %, ap_supplier_refund 99,5 %) |
| sample con `receipts.py` a medias + `docref.py` | 64,94 | acierto por línea 0,83 (antes 44,45) |
| **dev con todo junto (receipts optimizado + docref)** | **53,93** | 13:30. F1 0,581, acierto línea 0,656, exc. F1 0,260. receipts n=70.870 acc 0,920; docref acc 0,998. Total 461 s (receipts 227 s) con la máquina cargada (load ~13) |
| **dev tras los 4 agentes en paralelo (commit f7fec83 "a")** | **69,88** | 14:12. F1 0,718 (precisión 0,985, recall 0,565), acierto línea 0,875, exc. F1 0,388. **Runtime ~22 min** (receipts 830 s, ar_multi 251 s): hay que bajarlo. `ar_multi.py` tiene un cambio sin commit (refs de 6 dígitos solo en la familia del pagador), sin medir en dev completo |
| LIVE leaderboard "Team 10" | 82,14 | **es de otro pipeline (ChatGPT, otro compañero)**, no de este código |

Dev es nuestro simulador de LIVE (tiene golden). **No se itera contra el leaderboard.**

## Estado 14:50 (lo más reciente: empieza aquí)

- Último dev completo: **69,88** (log `out/C_deveval.log`, run `out/C_devrun.log`), con el commit `f7fec83` ("a").
  Por matcher (líneas): payment_runs 53.919, receipts 86.795, ar_multi 7.183, payments 8.629, nonitem 13.719,
  remittance 1.641, direct_debits 4.964, cheques 1.950, cards 1.252, docref 3.
- Orden actual (`__init__.py`): direct_debits, card_settlements, cheque_deposits, payment_runs, remittance, receipts,
  ar_multi, payments, nonitem, docref.
- Trabajo hecho por 4 agentes `segment-fixer` en paralelo (A `receipts.py`, B `ar_multi.py` nuevo, C `batches.py`,
  D `payments.py`). Sus scripts de análisis están en `out/A_*`, `out/B/`, `out/C_*`, `out/D/` (desechables).
- D: `ap_run_transfers` + `ap_paid_by_holding` (índice por proveedor) + `ap_partial` + `ap_fx` en `payments.py`;
  aislado en dev: precisión de pares 0,9998 sobre ~25.900 líneas (`out/D/iso.log`).
- **Sin commit**: `bankrec/matchers/ar_multi.py` (B: referencias de 6 dígitos solo dentro de la familia del pagador,
  ignora `PAY-nnnn-n`) y `CLAUDE.md`. No medido en dev completo; `out/B/dev_eval.log` da 69,80 (14:13).

### Pendiente, por prioridad (nueva sesión)

1. **Velocidad**: dev completo ~22 min (receipts 830 s, ar_multi 251 s, direct_debits 95 s). Perfilar con
   `cProfile` (script de ejemplo en el historial: `run(ph, ["receipts"])` con `signal.alarm`). Objetivo < 8 min.
2. **Recall** (0,565) es la palanca: la precisión ya es 0,985. Ver segmentos con más fallos en `out/C_deveval.log`.
3. Decidir si el cambio sin commit de `ar_multi.py` se queda (medir en dev completo) y hacer commit (pedir permiso).
4. LIVE v3: `.venv/bin/python scripts/run.py --phase live --out out/sub_live_v3.csv.gz` (VALID). Lo envía el usuario.
5. Guion de FINAL (ver más abajo) probado con sample y dev antes de las 18:00.

## Historial: qué cambió por la mañana en `bankrec/matchers/`

**La primera línea que se reclama gana.**

- `remittance.py` (nuevo, terminado): avisos de pago en pdf/csv/xlsx/txt, con pypdf a ~10 ms por PDF y texto en caché en
  `out/cache`. Reclama unas 1.600 líneas de dev sin regresiones. Queda abierto: unas 350 líneas tienen referencia
  pero ningún aviso, unas 540 tienen aviso cuyo total no cuadra y unas 900 no tienen referencia numérica.
- `payments.py` (nuevo, terminado): cargos directos de proveedor y abonos de proveedor.
  `ap_paid_by_holding` (unas 4.800 líneas) está **escrito pero no registrado**, porque su búsqueda de subconjuntos es
  demasiado lenta. Falta un índice por proveedor. `ap_partial` (unas 2.500 líneas) no está implementado.
- `receipts.py` (**trabajo de B a medias**: se paró a las 12:45 sin informe final. La sintaxis es válida y en sample
  da 64,94, pero no está medido en dev completo): cobros de cliente. Era el mayor fallo, porque solo buscaba referencias entre
  las facturas del pagador identificado: si pagaba un tercero o no reconocía el nombre, la línea quedaba sin reclamar.
- `docref.py` (nuevo, último en la cadena): si los números de documento del texto suman exactamente el importe
  (misma sociedad y divisa, partidas abiertas y emitidas antes del apunte), asigna sin excepción. Con la cadena
  anterior acertaba el 98 % sobre unas 19.500 líneas de dev que quedaban sin reclamar.
- `pdfs.py`: **no existe**. Plazos, retenciones, anticipos y "anula y sustituye" siguen sin cubrir.

## Estado 13:30

- `.venv` venía de otra máquina (`/Users/eivr.mac/...`): repuntado a `/opt/homebrew/bin/python3.12` (symlink + `pyvenv.cfg`).
- `receipts.py` optimizado sin cambiar reglas: índice global de claves de documento (antes `keys_for` por partida y línea),
  listas por pagador podadas de partidas cerradas, caché de `_name_rank` por conjunto de palabras, memo de importes por
  línea, búsqueda difusa de referencias solo si hay ≤ 3.000 candidatos (`FUZZY_MAX`).
- Pendiente: bajar de 5 min (hoy 461 s) y generar LIVE v3.

## Bloqueo anterior (resuelto: receipts optimizado; luego los agentes lo volvieron a hacer lento)

La versión nueva de `receipts.py` **es muy lenta en dev**. La etapa `receipts` pasaba de 84 segundos a más de 6 minutos
sin terminar, y el pipeline completo no acaba. Sample sí funciona (25 s). Antes de mezclar más cambios:

1. Perfilar `receipts.match` en dev (por ejemplo con `--only` y un cronómetro por bloque, o `cProfile`). Sospechosos:
   `_subset` (programación dinámica de hasta 40.000 estados por línea), `_name_rank` y cualquier bucle nuevo sobre
   todas las partidas.
2. Poner límites al tamaño de los candidatos y a la búsqueda de subconjuntos hasta que dev completo tarde menos de
   5 minutos.
3. Ejecutar dev completo y comparar con 42,02:
   `.venv/bin/python scripts/run.py --phase dev --out out/sub_dev.csv.gz && .venv/bin/python scripts/eval.py out/sub_dev.csv.gz --phase dev`
4. Si sube, generar LIVE v3: `.venv/bin/python scripts/run.py --phase live --out out/sub_live_v3.csv.gz` (tiene que
   imprimir VALID).

Si `receipts.py` no se arregla a tiempo: `git diff bankrec/matchers/receipts.py` y volver a la versión anterior
(`git checkout -- bankrec/matchers/receipts.py`). Con `docref.py` ya se recupera buena parte de lo que fallaba.

## Pendiente de la mañana (superado por "Estado 14:50")

1. Arreglar la lentitud de `receipts.py`, hacer la medición de dev completa y generar LIVE v3 (ver arriba).
2. Guion de FINAL: ejecutar `run.py --phase final`, validar y enviar con confirmación. `scripts/submit.py` ya exige
   `--i-know-final-is-one-shot` para FINAL. Probarlo con sample y dev **antes de las 18:00**, sin tocar FINAL.
3. Registrar `ap_paid_by_holding` en `payments.py` con un índice por proveedor (unas 4.800 líneas).
4. `ap_partial` (unas 2.500 líneas) y `pdfs.py`.
5. Resto de segmentos, con `.venv/bin/python scripts/eval.py out/sub_dev.csv.gz --phase dev` (ordenados por fallos)
   y `out/errors/<segment>.csv`.

## Envíos

- El token **nunca** va en ficheros ni en comandos escritos. Se pide por teclado:
  `read -rs HK_TOKEN && export HK_TOKEN`, y luego `.venv/bin/python scripts/submit.py LIVE out/sub_live_vN.csv.gz`.
- **FINAL lo envía el usuario**, no un agente.
- Límites: CLAUDE.md dice un envío cada 180 s y 20 al día. La API (`/api/state`) devuelve 10 s y 100.000. Nos
  guiamos por lo más restrictivo.
- El último LIVE generado con este código es `out/sub_live_v2.csv.gz` (582.182 filas, VALID). Lo de hoy (remittance,
  payments, docref, receipts) todavía no está en LIVE.

## Otros artefactos

- `dashboard/`: dashboard HTML local (`index.html` + `data.js`, que genera `build_data.py`) y los CSV de
  `dashboard/datasets/` (que genera `build_datasets.py`) con los datos de `out/sub_live_v2.csv.gz`.
- Dashboard publicado en Claude (privado): https://claude.ai/artifact/UUc7LRitHZHJrn5nQDXTzN
- `team_tasks.md`: registro de la sesión de la mañana (plan de reparto A/B/C/D).

## Normas de trabajo

- Solo `.venv/bin/python`. Ninguna llamada de red en el pipeline.
- Un matcher nunca debe tumbar la ejecución y debe primar la precisión: una asignación errónea cuesta un par falso y la
  línea; una línea sin reclamar recibe al menos el fallback.
- Nada de procesos largos en segundo plano: se pierden si la sesión se reinicia.
- Cada persona o agente escribe sus salidas con nombre propio (`out/sub_dev_<x>.csv.gz`) para no pisarse.
