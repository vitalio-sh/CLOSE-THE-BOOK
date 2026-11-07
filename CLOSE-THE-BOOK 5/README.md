# Close the Books · conciliación bancaria por reglas

Esqueleto del equipo para el Claude Code Hackathon (Talky · La Nave, 7/11/2026).

## Lo esencial de las reglas (leído en /reglas y /datos)

- **Score = 100 × (0,5 · Allocation F1 + 0,3 · Transaction accuracy + 0,2 · Exception macro-F1)**
- Tolerancia de importe: `max(0,01; 0,5 %)` del importe golden.
- Referencias: naive ≈ 19 · heurística bien trabajada ≈ 61 · perfecto = 100.
- **FINAL: UN ÚNICO ENVÍO.** Cuenta el primer fichero válido; un fichero con errores de formato no gasta el envío. En caso de empate gana quien envía antes.
- LIVE: un envío por minuto y 100 al día; cuenta la mejor puntuación. Pedir la URL de subida ya consume un hueco.
- Envío: `bank_txn_id,item_id,amount,category,exception`. `amount` va en la divisa de la partida, con 2 decimales. `category` solo cuando no hay partida. Un movimiento que falte cuenta como fallo; ids desconocidos o pares duplicados invalidan el fichero.
- El zip `bankrec-docs-and-masters.zip` (10 MB) trae README/FORMAT/POLICIES, una muestra de un mes con golden, `starter.py` y **el scorer oficial**: usadlo como referencia.
- Para portátil, descargad **train-lite** (460 MB) y LIVE (87 MB).

## Progreso (puntuación con el scorer oficial)

| Paso | Sample 2025-06 | TRAIN 2025-06 | Qué cambió |
|---|---|---|---|
| Esqueleto original | no carga | — | Buscaba `.csv`; los datos reales son `.csv.gz` |
| Datos reales + POLICIES.md | 40.94 | — | Carga gz y columnas reales, `settle` según POLICIES, validador oficial antes de dar el fichero por bueno |
| Evidencia documental | 62.79 | — | Avisos de remesa (pdf/csv/xlsx/txt), remesas SEPA, tarjetas, cheques y ficheros de pagos enlazados antes del bucle por fechas; sus partidas quedan reservadas |
| Confirming + comisiones | 66.22 | — | Confirming: un cargo por vencimiento; una comisión que cita una remesa ya no se casa con ella |
| Categorías + devoluciones | 70.73 | 67.93 | Categorías por texto/código, BANK_REVERSAL en pareja, RETURNED por mandato / cheque / tarjeta |
| Rendimiento | 70.78 | 65.38 | Motor ~4x más rápido; FX sin importe original demasiado laxo (corregido después) |
| Importes casi exactos + duplicados | 75.22 | 69.27 | `party_near` (descuento, retención, comisiones), DUPLICATE_PAYMENT, partidas cerradas visibles |
| Precisión FX / retención | 76.16 | 72.69 | FX sólo con importe original; retención sólo clientes de MX/BR/MA |
| Confirming por transferencias, pagos FX | 77.66 | 73.15 | Un run de confirming se carga en varios apuntes = N transferencias completas; pagos en divisa por importe original |
| Referencias + completar textos cortados | 81.33 | 73.52 |
| Completar sólo AR + cláusulas PDF + un nº = una partida | 80.38 | **75.08** | Completar sólo cobros con facturas enteras ausentes; plazos/retención/anticipo/sustituciones del PDF (precisión 0,95); el mismo nº en varias sociedades ya no genera filas repetidas; motor ~2x más rápido | Prefijos RE/GS/FA..., sufijos sueltos, y el resto del pago con partidas recientes del tercero |

Cómo se mide: `python run.py --data data/sample --masters data/masters --eval` y
`python run.py --data data/train --masters data/masters --from 2025-06-01 --to 2025-06-30 --eval`
(en TRAIN lo abierto al inicio se reconstruye con el golden). Coincide con `starter/score_train.py`.

## Estructura

```
run.py                  ejecuta una fase, valida, escribe CSV y (con --eval) puntúa
train_categories.py     aprende de TRAIN la categoría de los movimientos sin partida
recon/
  load.py        P1  carga; nombres de columna por candidatos (revisad el mapeo impreso)
  context.py     P1  libro de partidas abiertas, índices por nº impreso y por tercero
  settle.py      P2  importes y excepciones de un casado (SHORT_*, PARTIAL, FX, OVERPAYMENT)
  config.py      P3  tasas y umbrales a ajustar con TRAIN
  rules/              una regla por fichero; el orden está en rules/__init__.py
    returns.py        P2  devoluciones SEPA
    remittance.py     P3  avisos de remesa (txt/csv; falta xlsx/pdf)
    references.py     P2  nº de factura en el concepto (1:1, 1:N, abonos, truncados)
    party.py          P2  tercero + importe exacto (FIFO) / combinaciones
    categories.py     P3  movimientos sin partida
  engine.py           bucle por fecha + validate() (mismas comprobaciones que el servidor)
  score.py            puntuación local + desglose por regla y por excepción
  llm_assist.py       Claude para los no identificados (opcional, --llm)
tests/make_fixture.py   casos de /ejemplos para probar de punta a punta
```

Cada regla tiene la forma `rule(t, ctx) -> list[Row] | None`. La primera que devuelve filas gana y el libro se actualiza, así que una factura liquidada no vuelve a casar.

## Comandos

```bash
pip install pandas openpyxl anthropic --break-system-packages   # anthropic solo si usáis --llm
# datos en data/{masters,sample,train,live,final}

python tests/make_fixture.py && python run.py --data tests/fixture/data --masters tests/fixture/masters --eval   # comprobación rápida
python train_categories.py --data data/train
python run.py --data data/sample --masters data/masters --eval                                    # 1 mes, segundos
python run.py --data data/train  --masters data/masters --from 2025-01-01 --to 2025-06-30 --eval  # validación seria
python run.py --data data/live   --masters data/masters --out out/live.csv
gzip -k out/live.csv   # subir .csv.gz
```

`out/*.debug.csv` añade la columna `rule`, para ver qué regla generó cada fila.

## Lo primero al abrir el zip (10:00–10:30)

1. Leer FORMAT.md y POLICIES.md y corregir los nombres de columna en `load.py` si el mapeo impreso no encaja.
2. Pasar el scorer oficial sobre `out/sample.csv` y comprobar que coincide con `score.py`.
3. POLICIES.md → `config.py`: tasas de descuento, retenciones, política de comisiones y tolerancias. Comprobad también si `fx_rates` está en EUR por unidad o al revés (`context.to_txn_ccy`).
4. Decidir con los ejemplos de TRAIN:
   - En OVERPAYMENT, ¿el importe es el abierto o el cobrado?
   - En RETURNED, ¿se indica la partida?
   - En FX_DIFFERENCE, ¿en qué fila va la excepción?

## Plan del equipo

| Hora | P1 · Datos | P2 · Motor | P3 · Evaluación y pitch |
|---|---|---|---|
| 10:00 | Descarga y mapeo de columnas | Leer POLICIES.md | Scorer oficial frente a `score.py` |
| 10:30–12:00 | Que `run.py` funcione con la muestra y con LIVE | `references.py` + `settle.py` | `train_categories.py`, `config.py` |
| **12:00** | **Primer envío LIVE** | | |
| 12:00–14:00 | Rendimiento con TRAIN de 6 meses | 1:N y parciales | Avisos de remesa (xlsx/pdf) |
| 14:30–17:00 | Ensayo completo con LIVE y medición del tiempo | Divisas, devoluciones, tarjeta, intercompañía | Análisis de errores por regla y Claude (`--llm`) |
| **17:00** | **Congelar el código** | Ajustar umbrales | README, vídeo, pitch |
| **18:00** | Descargar FINAL → `run.py` → **VALIDACIÓN OK** → un solo envío antes de las 19:00 | Revisar el desglose por regla | Terminar el vídeo |
| 19:30 | | | Entrega del proyecto antes de las 19:45 |

Antes del envío FINAL: `VALIDACIÓN: OK`, número de movimientos igual al de entrada y el mismo comando que dio la mejor puntuación en LIVE. No hay segunda oportunidad.
