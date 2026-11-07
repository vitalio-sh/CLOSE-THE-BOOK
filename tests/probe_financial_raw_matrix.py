"""Independent evidence-boundary matrix; no labels or production data involved."""
import argparse, hashlib, json, sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--candidate',required=True);a=p.parse_args()
r=Path(a.candidate).resolve();sys.path[:0]=[str(r),str(r/'tests')]
from test_matching import context, invoice, txn
from recon.financial_references import finance_reference_sets
c=context([invoice('A','FV25-50000'),invoice('B','FV25-60000')])
negative=[
'TOTAL 50000','FV25-500009','INVOICE 50000,0000 EUR',
'INVOICE 50000,1234567','INVOICE 50000 %','INVOICE 50000%','INVOICE 50000 ‰',
'INVOICE 50000 HUF','INVOICE 50000 INR','INVOICE 50000 Kč',
'INVOICE 50000 000,00 EUR',"INVOICE 50000'000.00",'INVOICE 50000\u202f000,00 EUR',
'INVOICE 50000\u066b00','INVOICE 50000\uff0e00','INVOICE 50000\uff0c00',
'INVOICE 50000 , 00 EUR','INVOICE 50000 . 00 EUR',
'INVOICE 50000/2025','INVOICE 50000-2025',
'INVOICE 50000₹','INVOICE 50000 ₽','INVOICE 50000 R$',
'INVOICE 50000 EUR','INVOICE EUR 50000','INVOICE 50000.00',
]
rows=[]
for text in negative:
 refs=finance_reference_sets(txn('90','Alice Water Works '+text),c)['all']
 rows.append(dict(text=text,expected=[],actual=sorted(refs),passed=not refs))
for text,expected in [('INVOICE 50000',{'A'}),('FV25-50000',{'A'}),('FV25-50000 60000',{'A','B'}),('INVOICES 50000, 60000',{'A','B'})]:
 refs=finance_reference_sets(txn('90','Alice Water Works '+text),c)['all']
 rows.append(dict(text=text,expected=sorted(expected),actual=sorted(refs),passed=refs==expected))
print(json.dumps(dict(candidate=str(r),helper_sha256=hashlib.sha256((r/'recon/financial_references.py').read_bytes()).hexdigest(),checks=rows,failures=sum(not x['passed'] for x in rows)),indent=2,ensure_ascii=False))
