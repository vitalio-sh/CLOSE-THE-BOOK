"""Movimientos sin partida reconocibles por texto/código (POLICIES §4). Alta precisión: se aplica
antes de las reglas difusas. Orden importante: lo más específico primero."""
import re

from ..context import Row

# (regex sobre texto normalizado, signo: '+', '-' o '', categoría)
KW = [
    (r"ANULACION OPERACION INDEBIDA", "", "BANK_REVERSAL"),
    (r"ZBA SWEEP|CASH POOL|NIVELACION SALDO|TRASPASO ENTRE CUENTAS|TRASPASO PROPIO", "", "CASH_POOL"),
    (r"TRASPASO (A|DE) CTA|VIREMENT INTERNE|UMBUCHUNG", "", "INTERNAL_TRANSFER"),
    (r"FINANCIACION IC|PRESTAMO INTRAGRUPO|PRESTAMO IC|TRANSF IC|INTERCOMPANY|INTRAGROUPE", "", "INTERCOMPANY"),
    (r"LIQUIDACION INTERESES|INTERETS|ZINSEN|INTEREST", "+", "INTEREST_INCOME"),
    (r"LIQUIDACION INTERESES|INTERETS|ZINSEN|INTEREST", "-", "INTEREST_EXPENSE"),
    (r"CUOTA (RENTING|LEASING|ARRENDAMIENTO|LSG)|\bLEASING\b|\bRENTING\b", "-", "LEASING"),
    (r"AMORTIZACION DE PRESTAMO|\bPRES DE\b|LOAN REPAYMENT|ECHEANCE PRET|TILGUNG", "-", "LOAN_REPAYMENT"),
    (r"DISPOSICION (DE )?(PRESTAMO|POLIZA)|LOAN DRAWDOWN|DEBLOCAGE PRET", "+", "LOAN_DRAWDOWN"),
    (r"\bNOMINAS?\b|PAYROLL|SALAIRES|GEHAELTER|GEHALT", "-", "PAYROLL"),
    (r"SOCIAL CHARGES|SEGURIDAD SOCIAL|SEG SOCIAL|URSSAF|SOZIALVERS", "-", "SOCIAL_SECURITY"),
    (r"IMPUESTOS MOD|TAX PAYMENT|VAT RETURN|\bAEAT\b|DGFIP|FINANZAMT", "-", "TAX_PAYMENT"),
    (r"DEVOLUCION (IVA|IMPUESTOS)|TAX REFUND|VAT REFUND|\bAEAT\b|DGFIP|FINANZAMT", "+", "TAX_REFUND"),
    (r"ADEUDO MENSUAL DE TARJETA|CARD STATEMENT|RELEVE CARTE", "-", "CARD_EXPENSES"),
    (r"PRIMA SEGURO|INSURANCE PREMIUM|\bASSURANCE\b|VERSICHERUNG", "-", "INSURANCE"),
    (r"COMPRAVENTA DIVISA|FX DEAL|\bFORWARD\b|SPOT FX", "", "FX_DEAL"),
    (r"^COM\b|\bCOMISION|\bFRAIS\b|SERV EM TRANSF|BANK CHARGES|GEBUEHR|ENTGELT|\bFEES?\b|COMMISSION", "-", "BANK_FEES"),
    (r"PROFORMA|\bPROFO", "+", "CUSTOMER_ADVANCE"),
    (r"REEMBOLSO|REMBOURSEMENT|ERSTATTUNG", "+", "SUPPLIER_REFUND"),
]
KW = [(re.compile(r), s, c) for r, s, c in KW]
# códigos de banco que sólo usan las comisiones (MT940 NCHG; N43 17 = comisiones/intereses)
FEE_CODES = {"NCHG"}


def keywords(t, ctx):
    if t.id in ctx.reversals:
        return [Row("", 0.0, "BANK_REVERSAL", "")]
    sign = "+" if t.amount > 0 else "-"
    for rx, s, cat in KW:
        if (not s or s == sign) and rx.search(t.ntext):
            return [Row("", 0.0, cat, "")]
    if t.amount < 0 and str(getattr(t, "bank_code", "")).split("/")[0] in FEE_CODES:
        return [Row("", 0.0, "BANK_FEES", "")]
    return None


def find_reversals(txns) -> set:
    """Apunte erróneo + su anulación: ambos BANK_REVERSAL. El erróneo es el de importe opuesto
    en la misma cuenta, anterior y cercano."""
    out = set()
    rev = txns[txns["ntext"].str.contains("ANULACION OPERACION INDEBIDA", regex=False)]
    if not len(rev):
        return out
    byacc = {a: g for a, g in txns.groupby("account")}
    for r in rev.itertuples(index=False):
        out.add(r.id)
        g = byacc.get(r.account)
        if g is None:
            continue
        c = g[(g["amount"].round(2) == round(-r.amount, 2)) & (g["date"] <= r.date)
              & (g["date"] >= r.date - __import__("pandas").Timedelta(days=15)) & (g["id"] != r.id)]
        if len(c):
            out.add(c.sort_values("date").iloc[-1]["id"])
    return out
