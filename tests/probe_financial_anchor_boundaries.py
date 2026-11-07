"""Behavior probes for revised financial helpers, independent of generic matcher bugs."""
from pathlib import Path
import argparse,json,sys
from types import SimpleNamespace
p=argparse.ArgumentParser();p.add_argument('--candidate',required=True);p.add_argument('--plans',action='store_true');a=p.parse_args()
root=Path(a.candidate).resolve();sys.path[:0]=[str(root),str(root/'tests')]
from test_matching import invoice,txn,context
from recon.financial_sequences import FinancialSequences
results={}
def retention(text):
 c=context([invoice('I1','FV25-50000'),invoice('I2','FV25-60000')]);facts={i:dict(source=i+'.pdf',net_due='90.00',retention_pct='10') for i in ['I1','I2']}
 c.pdf_facts=SimpleNamespace(enabled=True,get=lambda i:facts.get(i,{}),resolve=lambda i:i)
 return FinancialSequences(c).reconcile(txn('90','Alice Water Works '+text))
for text in ['TOTAL 50000','FV25-500009','FV25-50000 60000']:
 rows=retention(text);assert rows is None,(text,rows);results[text]='abstained'
rows=retention('FV25-50000');assert rows and rows[0]['item_id']=='I1',rows;results['exact_positive_anchor']='I1'
if a.plans:
 for text in ['TOTAL 50000','FV25-500009']:
  c=context([invoice('I1','FV25-50000','300'),invoice('I2','FV25-60000','100')]);facts={'I1':dict(source='plan.pdf',instalments_count=3)}
  c.pdf_facts=SimpleNamespace(enabled=True,get=lambda i:facts.get(i,{}),resolve=lambda i:i)
  rows=FinancialSequences(c).reconcile_referenced(txn('100','Alice Water Works '+text));assert rows is None,(text,rows);results['referenced_plan_'+text]='abstained'
print(json.dumps({'candidate':str(root),'plans':a.plans,'checks':results},indent=2))
