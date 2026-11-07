"""Reglas de casado, de la más segura a la menos segura.

Cada regla: rule(t, ctx) -> list[Row] | None.  La primera que devuelve filas gana.
Para añadir una regla: crea un fichero aquí, impórtalo y colócalo en RULES.
Cada persona trabaja en sus propios ficheros -> sin conflictos de merge.
"""
from .evidence_rule import evidence
from .keywords import keywords
from .returns import returned
from .remittance import remittance
from .references import by_reference
from .categories import category_strict, category_loose
from .party import party_exact, party_subset
from .near import party_near
from .duplicates import duplicate
from .fifo import party_fifo
from .pdfrules import pdf_partial

RULES = [
    evidence,          # aviso / remesa SEPA / tarjeta / cheques / fichero de pagos enlazado (manda el documento)
    keywords,          # sin partida, por texto/código (comisiones, cash pool, nóminas...)
    returned,          # devoluciones: mandato / cheque / tarjeta -> partida original
    by_reference,      # P2  nº de factura en el concepto (1:1, 1:N, parciales, short)
    category_strict,   # P3  movimientos sin partida muy claros (comisiones, nóminas...)
    party_exact,       # P2  tercero + importe exacto, FIFO
    pdf_partial,       # plazo / retención / anticipo previsto en el PDF de la factura
    duplicate,         # partida ya liquidada que se vuelve a pagar
    party_fifo,        # varias facturas: tramo FIFO (sin señuelos viejos) que cuadra al céntimo
    party_subset,      # P2  tercero + combinación de partidas que suma el importe
    party_near,        # tercero + importe con diferencia explicada por política
    category_loose,    # P3  categoría con menos confianza
]
