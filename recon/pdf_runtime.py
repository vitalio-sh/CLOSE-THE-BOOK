"""Opt-in PDF evidence readiness shared by release and temporal evaluation."""
import hashlib
import json
from pathlib import Path
from .pdf_facts import PdfFacts, VERSION


def add_options(parser, base=True):
    if base:
        parser.add_argument('--pdf-facts', action='store_true')
        parser.add_argument('--pdf-max-documents', type=int, default=100)
        parser.add_argument('--pdf-budget-seconds', type=float, default=30)
    parser.add_argument('--pdf-cache-dir')
    parser.add_argument('--pdf-prewarm', action='store_true')
    parser.add_argument('--require-pdf-complete', action='store_true')
    parser.add_argument('--pdf-max-party-documents', type=int, default=64)


def check_options(args):
    if (args.pdf_prewarm or args.require_pdf_complete or args.pdf_cache_dir) and not args.pdf_facts:
        raise ValueError('PDF cache/prewarm/completeness options require --pdf-facts')
    if args.pdf_max_documents < 0 or args.pdf_max_party_documents < 0 or args.pdf_budget_seconds < 0:
        raise ValueError('PDF budgets must be nonnegative')


def hash_file(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''): h.update(block)
    return h.hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class PdfSession:
    def __init__(self, ctx, args, output_dir):
        self.ctx, self.args = ctx, args
        self.output = Path(output_dir).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        cache = Path(args.pdf_cache_dir or self.output/'pdf_cache').resolve()
        self.provider = PdfFacts(ctx, cache_dir=cache, max_documents=args.pdf_max_documents,
                                 max_party_documents=args.pdf_max_party_documents,
                                 max_seconds=args.pdf_budget_seconds)
        ctx.pdf_facts = self.provider
        p = self.provider
        self.roots = [Path(ctx.phase_dir).resolve()] + [Path(r).resolve() for r in getattr(ctx, 'history_dirs', [])]
        records = {Path(r['path']).resolve(): r for groups in (p.by_item, p.by_notice)
                   for group in groups.values() for r in group}
        self.records = sorted(records.items(), key=lambda pair: self.key(pair[0]))
        self.indexes = [r/'pdfs/index.csv' for r in self.roots if (r/'pdfs/index.csv').is_file()]
        # Reject an index entry escaping its PDF source tree before prewarming.
        for path, _ in self.records:
            if not any(self.within(path, root/'pdfs') for root in self.roots):
                raise ValueError('PDF index path escapes its source directory')
        self.paths = sorted(set(self.indexes) | set(records))
        self.before = self.fingerprint()
        self.config = {'cache_dir': str(cache), 'cache_version': VERSION, 'backend': p._backend,
                       'max_documents': args.pdf_max_documents, 'max_seconds': args.pdf_budget_seconds,
                       'max_party_documents': args.pdf_max_party_documents,
                       'strict': args.require_pdf_complete, 'prewarm': args.pdf_prewarm or args.require_pdf_complete}
        self.truncated = sum(len(rs)>p.max_party_documents for groups in (p.by_party,p.by_notice) for rs in groups.values())
        self.save('pdf_inputs.json', {'config':self.config, 'files':self.before, 'sha256':digest(self.before)})
        if self.config['prewarm']:
            for _, record in self.records:
                p._read(record)
        self.finish()

    @staticmethod
    def within(path, root):
        try: path.relative_to(root); return True
        except ValueError: return False

    def key(self, path):
        for i, root in enumerate(self.roots):
            if self.within(path, root): return str(i)+'/'+str(path.relative_to(root))
        return str(path)

    def fingerprint(self):
        return {self.key(p): hash_file(p) if p.is_file() else None for p in self.paths}

    def save(self, name, value):
        (self.output/name).write_text(json.dumps(value, indent=2)+'\n')

    def finish(self):
        p=self.provider
        facts={self.key(path): {k:v for k,v in p._memory[record['path']].items() if k!='source'}
               for path,record in self.records if record['path'] in p._memory}
        reasons=[]
        if not p.tool: reasons.append('missing_pdf_backend')
        if not self.indexes: reasons.append('missing_pdf_index')
        if any(v is None for v in self.before.values()): reasons.append('missing_source')
        if self.fingerprint()!=self.before: reasons.append('pdf_sources_changed')
        for counter in ('budget_skips','time_budget_skips','failures','timeouts'):
            if p.stats[counter]: reasons.append(counter)
        if p.stats['time_budget_exhausted']: reasons.append('time_budget_exhausted')
        if self.truncated: reasons.append('party_or_notice_limit')
        if len(facts)!=len(self.records): reasons.append('unread_sources')
        self.report={'config':self.config, 'artifact_dir':str(self.output), 'stats':dict(p.stats), 'required_sources':len(self.records),
                     'loaded_sources':len(facts), 'truncated_groups':self.truncated,
                     'inputs_sha256':digest(self.before), 'facts_sha256':digest(facts),
                     'complete':not reasons, 'reasons':reasons}
        self.save('pdf_facts_snapshot.json', {'facts':facts,'sha256':digest(facts)})
        self.save('pdf_readiness.json', self.report)
        if self.args.require_pdf_complete and reasons:
            raise ValueError('PDF evidence incomplete: '+', '.join(reasons))
        return self.report


def configure(ctx, args, output_dir):
    check_options(args)
    return PdfSession(ctx,args,output_dir) if args.pdf_facts else None


def verify_snapshot(report):
    """Verify the persisted facts/source identities bound into a release report."""
    if not report or not report.get('complete'):
        raise ValueError('Strict PDF evidence is incomplete')
    root = Path(report['artifact_dir'])
    for name, key, field in [('pdf_inputs.json','files','inputs_sha256'),
                             ('pdf_facts_snapshot.json','facts','facts_sha256')]:
        payload = json.loads((root/name).read_text())
        if digest(payload[key]) != report[field] or payload['sha256'] != report[field]:
            raise ValueError('PDF provenance snapshot changed: '+name)
