from __future__ import annotations
import argparse
import json
import logging
import os
from pathlib import Path
from .browser import AcquireBrowser, AccessBlocked
from .http import HttpError, PoliteHttpClient
from .models import SourceProduct
from .state import State
from .batch import BatchGenerator, BulkPublisher, Ledger, Paused

MODELS = ('gemini-3.8-flash','gemini-3.7-flash','gemini-3.6-flash','gemini-3.5-flash','gemini-3-flash','gemini-2.5-flash')
LOG = logging.getLogger(__name__)


def load_env():
    path = Path('.env')
    if path.exists():
        for raw in path.read_text(encoding='utf-8-sig').splitlines():
            line = raw.strip()
            if line and not line.startswith('#') and '=' in line:
                key,value = line.split('=',1)
                os.environ.setdefault(key.strip(),value.strip().strip('\"\''))


def client(attempts=3):
    return PoliteHttpClient(user_agent='AcquireMagicResearch/0.1',product_hunt_delay_seconds=8,
        product_hunt_jitter_seconds=4,external_delay_seconds=8,external_jitter_seconds=2,
        timeout_seconds=45,max_attempts=attempts)


def positive(value):
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return n


def parser():
    p = argparse.ArgumentParser(description='Acquire SaaS → Magic Catalog (R2 + sharded FTS + shared intents)')
    p.add_argument('--state-dir',type=Path,default=Path('.state'))
    p.add_argument('--verbose',action='store_true')
    sub = p.add_subparsers(dest='command',required=True)
    sub.add_parser('login',help='Sign in once in a local browser; securely reuse its profile')
    sub.add_parser('status')
    sub.add_parser('quota-status')
    sub.add_parser('retry-failed',help='Clear errors while retaining all successful stages')
    export = sub.add_parser('export')
    export.add_argument('--output',type=Path)
    run = sub.add_parser('run')
    run.add_argument('--limit',type=positive,default=3,help='Total first N eligible listings in this checkpoint, including completed ones')
    run.add_argument('--stage',choices=['all','scrape','generate','publish'],default='all')
    run.add_argument('--batch-size',type=positive,default=25)
    run.add_argument('--max-batch-size',type=positive,default=100)
    run.add_argument('--publish-batch-size',type=positive,default=25)
    run.add_argument('--daily-row-budget',type=positive,default=80000)
    run.add_argument('--wait-minutes',type=float,default=2)
    run.add_argument('--scrape-only',action='store_true',help='Test scraping without Gemini or publishing')
    run.add_argument('--publish',action='store_true',help='Publish generated products to scalable ingest')
    run.add_argument('--offline',action='store_true',help='Process saved sources only; never open Acquire')
    run.add_argument('--headless',action='store_true')
    run.add_argument('--delay',type=float,default=8,help='Source navigation/scroll delay; minimum 5 seconds')
    run.add_argument('--jitter',type=float,default=4)
    run.add_argument('--max-rounds',type=positive,default=5000)
    run.add_argument('--types',default='saas,ai,shopify app',help='Comma-separated card types; add mobile,crypto if wanted')
    return p


def _body_excerpt(body, limit=800):
    if not body:
        return ''
    text = ' '.join(str(body).split())
    return text if len(text) <= limit else text[:limit]+'…'


def catalog_http_message(action, url, error):
    status = error.status
    if status == 404:
        hint = ('Magic Catalog intentionally returns 404 when the admin token is missing/wrong. '
            'Check MAGIC_CATALOG_URL and make MAGIC_CATALOG_IMPORT_TOKEN exactly match the deployed ADMIN_REINDEX_TOKEN; '
            'redeploy Magic Catalog after changing that secret.')
    elif status in (401,403):
        hint = 'Authentication was rejected; verify the deployed ADMIN_REINDEX_TOKEN and MAGIC_CATALOG_IMPORT_TOKEN.'
    elif status == 503:
        hint = 'Scalable catalog resources are missing; in Magic Catalog run npm run scale:setup, then npm run deploy.'
    elif status == 429:
        hint = 'Catalog rate/write limit reached; the checkpoint is preserved and the same command is safe to rerun later.'
    elif status == 400:
        hint = 'Magic Catalog rejected the request; verify the deployed ingest schema matches this scraper.'
    elif status is None:
        hint = 'No HTTP response was received; check DNS/TLS/network connectivity and the configured catalog origin.'
    else:
        hint = 'The catalog request failed; checkpoints are preserved and rerunning is safe after the server issue is fixed.'
    body = _body_excerpt(error.body)
    details = f' Response: {body}' if body else ''
    retry = f' Retry-After: {error.retry_after:g}s.' if error.retry_after is not None else ''
    return f'Catalog {action} failed at {url}: HTTP {status if status is not None else "network error"}. {hint}{retry}{details}'


def scrape_queue(state,args,browser):
    failures = 0
    for index,row in enumerate(state.rows(args.limit),1):
        if row['source'] or row['error'] or row['published_at']:
            continue
        try:
            LOG.info('[%d/%d] Scraping %s',index,args.limit,row['id'])
            source = browser.scrape(row)
            state.update(row['id'],source=json.dumps(source.to_dict(),ensure_ascii=False))
        except AccessBlocked:
            raise
        except Exception as error:
            state.fail(row,'scrape',error)
            failures += 1
    return failures


def generate_queue(state,args,ledger):
    jobs = [{'id':r['id'],'source':SourceProduct.from_dict(json.loads(r['source'])),'created_at':r['created_at']}
        for r in state.rows(args.limit) if r['source'] and not r['product'] and not r['error'] and not r['published_at']]
    if not jobs:
        return
    models = tuple(v.strip() for v in os.getenv('GEMINI_MODELS',','.join(MODELS)).split(',') if v.strip())
    if not models:
        raise ValueError('GEMINI_MODELS cannot be empty')
    generator = BatchGenerator(client(1),os.getenv('GEMINI_API_KEY',''),models,ledger,args.batch_size,args.max_batch_size,args.wait_minutes)
    def save(job,product,model):
        # Preserve the same identity scheme as v0.1, including previous previews.
        product.update(slug='acq-'+job['id'],id='acquire-'+job['id'],createdAt=job['created_at'])
        state.update(job['id'],product=json.dumps(product,ensure_ascii=False,separators=(',',':')),model=model)
    try:
        generator.generate(jobs,save,state.name_exists)
    finally:
        state.export()


def publish_queue(state,args,ledger):
    rows = [r for r in state.rows(args.limit) if r['product'] and not r['published_at']]
    if not rows:
        LOG.info('Publish stage: no pending generated products in the selected limit.')
        return
    products = [json.loads(r['product']) for r in rows]
    ids = {p['slug']:r['id'] for r,p in zip(rows,products)}
    url = os.getenv('MAGIC_CATALOG_URL','https://magic-catalog.cloudwebsites.workers.dev')
    publisher = BulkPublisher(client(1),ledger,url,os.getenv('MAGIC_CATALOG_IMPORT_TOKEN',''),args.daily_row_budget)
    LOG.info('Publish stage: %d pending product(s); catalog=%s; requested batch=%d',len(products),publisher.url,args.publish_batch_size)
    from .state import now
    try:
        publisher.publish(products,lambda p:state.update(ids[p['slug']],published_at=now(),error=None),args.publish_batch_size)
    except HttpError as error:
        # BulkPublisher handles POST failures itself. An HttpError escaping here is normally
        # the authenticated preflight GET; keep the operation/context instead of mislabeling
        # it as a browser failure.
        raise ValueError(catalog_http_message('preflight/request',publisher.url+'/api/admin/catalog/ingest',error)) from None
    except json.JSONDecodeError as error:
        raise ValueError('Magic Catalog returned a non-JSON response during publish/preflight at '
            +publisher.url+f': {error}. Check the deployment/proxy and rerun with --verbose.') from None


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s')
    load_env()
    state = State(args.state_dir)
    LOG.debug('Starting command=%s state_dir=%s verbose=%s',args.command,args.state_dir,args.verbose)
    try:
        if args.command == 'quota-status':
            ledger = Ledger()
            try:
                print(json.dumps(ledger.summary(tuple(os.getenv('GEMINI_MODELS',','.join(MODELS)).split(','))),indent=2))
            finally:
                ledger.db.close()
            return 0
        if args.command == 'status':
            print(json.dumps(state.summary(),indent=2))
            for row in state.db.execute('SELECT id,error FROM items WHERE error IS NOT NULL LIMIT 20'):
                print(row['id'],row['error'])
            return 0
        if args.command == 'retry-failed':
            with state.db:
                state.db.execute('UPDATE items SET error=NULL')
            print('Failed items enabled for retry; completed stages retained.')
            return 0
        if args.command == 'export':
            print(state.export(args.output))
            return 0
        if args.command == 'login':
            with AcquireBrowser(args.state_dir) as browser:
                browser.login()
            return 0
        if args.scrape_only and args.publish:
            raise ValueError('--scrape-only cannot be combined with --publish')
        if args.scrape_only:
            args.stage = 'scrape'
        failures = 0
        needs_browser = args.stage in ('all','scrape') and not args.offline and (
            state.count()<args.limit or any(not r['source'] and not r['error'] for r in state.rows(args.limit)))
        if needs_browser:
            with AcquireBrowser(args.state_dir,args.delay,args.jitter,args.headless) as browser:
                browser.discover(state,args.limit,{v.strip().lower() for v in args.types.split(',')},args.max_rounds)
                failures = scrape_queue(state,args,browser)
        paused = False
        ledger = Ledger()
        try:
            if args.stage in ('all','generate'):
                try:
                    generate_queue(state,args,ledger)
                except Paused as error:
                    LOG.warning('%s',error)
                    paused = True
            if args.stage == 'publish' or (args.stage=='all' and args.publish):
                publish_queue(state,args,ledger)
        finally:
            ledger.db.close()
            state.export()
        print(json.dumps(state.summary(),indent=2))
        if not state.rows(args.limit):
            raise ValueError('No saved listings to process. Run --stage scrape first.')
        unfinished = any(not r['source'] for r in state.rows(args.limit))
        return 1 if failures or paused or unfinished else 0
    except KeyboardInterrupt:
        print('\nStopped safely; rerun the same command to resume.')
        return 130
    except (ValueError,AccessBlocked,Paused) as error:
        LOG.error('%s',error)
        return 1
    except Exception as error:
        context = 'command='+args.command
        if getattr(args,'command',None) == 'run':
            context += ' stage='+getattr(args,'stage','unknown')
        if args.verbose:
            LOG.exception('Unexpected %s during %s: %s',type(error).__name__,context,error)
        else:
            LOG.error('Unexpected %s during %s: %s. Checkpoints preserved. Rerun the same command with --verbose for a traceback.',
                type(error).__name__,context,error)
        return 1
    finally:
        state.db.close()
