"""Parámetros a ajustar con TRAIN (responsable: P3).

Cada número de aquí es una hipótesis: compruébalo con golden_allocations.csv y POLICIES.md.
"""

# Diferencia (en divisa) por debajo de la cual el pago "cuadra" sin excepción.
EXACT_EPS = 0.01

# Descuento por pronto pago: % y días salen de masters/payment_terms.csv (POLICIES §3);
# se acepta hasta fecha factura + discount_days + 3 días de gracia.
DISCOUNT_GRACE_DAYS = 3
# Retención en origen (MX, BR, MA): POLICIES §3.
WITHHOLDING_RATES = [0.0125, 0.015, 0.10]

# Comisiones de banco intermediario (SHA/BEN): <= 60 EUR equivalentes y <= 2 % de la partida.
BANK_CHARGES_MAX_EUR = 60.0
BANK_CHARGES_MAX_REL = 0.02
# Palabras que delatan transferencia internacional / SWIFT en el concepto.
FOREIGN_HINTS = ["EN DIV", "SWIFT", "INTERNATIONAL", "TRANSFER EN DIV", "OUR", "SHA", "BEN"]

# Diferencia relativa admitida cuando la partida está en otra divisa (FX_DIFFERENCE).
FX_MAX_REL = 0.03

# Palabras de devolución SEPA.
RETURN_HINTS = ["DEVOL", "DEVUELT", "RETURN", "IMPAGAD", "RECHAZ", "REJECT", "IMPAYE", "RUECKLASTSCHRIFT",
                "RUCKLASTSCHRIFT", "RETOUR", "UNPAID", "NSF"]

# Búsqueda de combinaciones (1:N sin referencias)
SUBSET_MAX_CANDIDATES = 25
SUBSET_MAX_SIZE = 4

# Días de margen: una partida puede casar con un movimiento hasta N días antes de su fecha de emisión.
ISSUE_SLACK_DAYS = 3

# Modelo de categorías: confianza mínima para usarlo.
CATEGORY_MIN_SHARE_STRICT = 0.97
CATEGORY_MIN_SHARE = 0.80
CATEGORY_MIN_COUNT = 5

# Claude para casos ambiguos (opcional; necesita ANTHROPIC_API_KEY).
LLM_ENABLED = False
LLM_MAX_CALLS = 300
LLM_MODEL = "claude-sonnet-5-5"

# Sobrepago aceptado sin referencia (party_near): exceso máximo relativo.
OVERPAY_MAX_REL = 0.02
# Partidas ya liquidadas que se conservan (con abierto 0) para duplicados y devoluciones.
KEEP_CLOSED_DAYS = 365

# FX sin importe original (party_exact): el cobro convertido queda entre -1,6 % y +0,05 % del de referencia.
FX_SPREAD_MAX = 0.016
FX_SPREAD_TOL = 0.0005

# Países cuyos clientes retienen impuestos en origen (POLICIES §3).
WITHHOLDING_COUNTRIES = {"MX", "BR", "MA"}

# Completar cobros con las facturas no citadas (rules/complete.py)
COMPLETE_WINDOW_DAYS = 60     # sólo partidas emitidas ±N días alrededor de las citadas
COMPLETE_MAX_POOL = 20
COMPLETE_MAX_K = 10
COMPLETE_BUDGET = 20_000
COMPLETE_MIN_REST = 0.15     # lo que falta debe ser >= 15 % del cobro

# FIFO de facturas no viejas (rules/fifo.py)
FIFO_STALE_DAYS = 400   # más antiguas = señuelos que nunca se cobran (FV21... en 2025)
FIFO_MAX_ITEMS = 60

# Identificación del pagador (context._parties_in)
NAME_MIN_PREFIX = 12   # caracteres de prefijo común con el nombre completo para aceptar un nombre cortado
# Factoría de pagos (POLICIES §5.2): ES00 paga facturas de proveedores de ES01, ES02 y PT01
PAYMENT_FACTORY = {"ES00": {"ES01", "ES02", "PT01"}}
