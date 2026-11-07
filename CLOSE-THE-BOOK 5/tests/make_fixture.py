"""Datos mínimos con los casos de /ejemplos para comprobar que el esqueleto corre de punta a punta."""
import csv
from pathlib import Path
D = Path(__file__).parent / "fixture"
(D / "masters").mkdir(parents=True, exist_ok=True)
(D / "data").mkdir(parents=True, exist_ok=True)
def w(p, header, rows):
    with open(p, "w", newline="") as f:
        x = csv.writer(f); x.writerow(header); x.writerows(rows)
w(D/"masters/customers.csv", ["customer_id","name","tax_id"], [
 ["C1","FMC CONSTRUCCIONES S.COOP.","F20603619"],["C2","VILLAMAR DEL RIO DEPURACION S.L.",""],
 ["C3","AGUAS CALVO PLUS S.L.U.","B92196290"],["C4","PFH HIDROTECNIA S.L.",""],["C5","RBA AGUAS S.A.",""],
 ["C6","CONSTRUCCIONES LOMASECA S.L.",""],["C7","CBH HOTELES S.L.","B00120375"]])
w(D/"masters/suppliers.csv", ["supplier_id","name","tax_id"], [])
inv = [ # item_id, number, party, amount, issue
 ["ARI-2025-0000001","FV25-0400572","C1","226.81","2025-04-01"],["ARI-2025-0000002","FV25-0412105","C1","344.57","2025-04-20"],
 ["ARI-2025-0000003","FV23-0505483","C2","913.19","2023-04-01"],["ARI-2025-0000004","FV22-0520977","C2","155.84","2022-05-01"],
 ["ARI-2025-0000005","FV24-0302232","C3","2810.83","2024-02-01"],["ARI-2025-0000006","FV24-0300040","C3","2810.83","2024-01-05"],
 ["ARI-2025-0000007","FV24-0406423","C4","1247.34","2024-03-20"],["ARI-2025-0000008","FV22-0516064","C5","7694.41","2022-11-01"],
 ["ARI-2025-0000009","FV22-0519411","C5","8973.58","2022-12-01"],["ARI-2025-0000010","FV23-0203311","C6","1858.21","2023-02-23"],
 ["ARI-2025-0000011","FV22-0214746","C7","822.51","2022-07-01"]]
w(D/"data/ar_invoices.csv", ["item_id","document_number","customer_id","total_amount","currency","issue_date","payment_terms"],
  [[a,b,c,d,"EUR",e,"D2N30" if a == "ARI-2025-0000010" else "T60"] for a,b,c,d,e in inv])
w(D/"masters/payment_terms.csv", ["code","days","early_payment_discount_pct","discount_days","description"],
  [["T60","60","","",""],["D2N30","30","2.00","10",""]])
w(D/"data/credit_notes.csv", ["item_id","document_number","customer_id","total_amount","currency","issue_date"],
  [["ARC-2025-0000001","AB24-0400149","C4","187.09","EUR","2024-04-01"],["ARC-2025-0000002","AB23-0500008","C2","155.84","EUR","2023-04-02"]])
tx = [
 ["BT-1","2025-05-23","226.81","EUR","TRANSFERENCIA SEPA | 20603619-FMC CONSTRUCCIONES S.COOP. | PAGO FRA. FV25-0400572"],
 ["BT-2","2023-05-17","913.19","EUR","TRANSFERENCIAS VILLAMAR DEL RIO DEPUR | VILLAMAR D FACTURA FV23-0505"],
 ["BT-3","2024-03-22","2810.83","EUR","TRANSFERENCIA SEPA | 92196290-AGUAS CALVO PLUS S.L.U."],
 ["BT-4","2024-04-24","1060.25","EUR","TRANSFERENCIA DE PFH HIDROTECNIA S.L. | CONCEPTO: FRAS FV24-04064"],
 ["BT-5","2023-02-14","16667.99","EUR","TRANSFERENCIAS RBA AGUAS S.A. | RBA AGUAS FATURA FV22-0519411 0516064"],
 ["BT-6","2023-02-28","1821.05","EUR","TRANSFERENCIAS CONSTRUCCIONES LOMASEC | CONSTRUCCI PAGO FRA. FV23-0203311"],
 ["BT-7","2022-08-04","205.62","EUR","TRANSFERENCIA SEPA | 00120375-CBH HOTELES S.L. | FRA. FV22-021"],
 ["BT-8","2025-05-30","-12.40","EUR","COMISION MANTENIMIENTO CUENTA"]]
w(D/"data/bank_transactions.csv", ["bank_txn_id","booking_date","amount","currency","description"], tx)
w(D/"data/golden_allocations.csv", ["bank_txn_id","item_id","amount","category","exception"], [
 ["BT-1","ARI-2025-0000001","226.81","",""],["BT-2","ARI-2025-0000003","913.19","",""],["BT-3","ARI-2025-0000006","2810.83","",""],
 ["BT-4","ARC-2025-0000001","187.09","",""],["BT-4","ARI-2025-0000007","1247.34","",""],["BT-5","ARI-2025-0000008","7694.41","",""],
 ["BT-5","ARI-2025-0000009","8973.58","",""],["BT-6","ARI-2025-0000010","1858.21","","SHORT_DISCOUNT"],
 ["BT-7","ARI-2025-0000011","205.62","","PARTIAL"],["BT-8","","","BANK_FEES",""]])
w(D/"masters/categories.csv", ["category","description","gl_account"],
  [[c, "", ""] for c in ["BANK_FEES", "UNIDENTIFIED", "CASH_POOL", "PAYROLL", "TAX_PAYMENT"]])
