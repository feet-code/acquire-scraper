from __future__ import annotations
import argparse
import json
import logging
import os
from pathlib import Path
from .browser import AcquireBrowser, AccessBlocked
from .gemini import GeminiTransformer, TransformationError
from .http import PoliteHttpClient
from .models import SourceProduct
from .publisher import Publisher, product_record
from .state import State

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
    sub.add_parser('retry-failed',help='Clear errors while retaining all successful stages')
    export = sub.add_parser('export')
    export.add_argument('--output',type=Path)
    run = sub.add_parser('run')
    run.add_argument('--limit',type=positive,default=3,help='Total first N eligible listings in this checkpoint, including completed ones')
    run.add_argument('--scrape-only',action='store_true',help='Test scraping without Gemini or publishing')
    run.add_argument('--publish',action='store_true',help='Publish generated products to scalable ingest')
    run.add_argument('--offline',action='store_true',help='Process saved sources only; never open Acquire')
    run.add_argument('--headless',action='store_true')
    run.add_argument('--delay',type=float,default=8,help='Source navigation/scroll delay; minimum 5 seconds')
    run.add_argument('--jitter',type=float,default=4)
    run.add_argument('--max-rounds',type=positive,default=5000)
    run.add_argument('--types',default='saas,ai,shopify app',help='Comma-separated card types; add mobile,crypto if wanted')
    run.add_argument('--daily-publish-limit',type=positive,default=5000,help='Local per-UTC-day ingest attempts, including retries')
    return p


def process(state,args,browser,transformer,publisher):
    failures = 0
    for index,row in enumerate(state.rows(args.limit),1):
        if row['error']:
            failures += 1
            continue
        if row['published_at'] or (args.scrape_only and row['source']):
            continue
        LOG.info('[%d/%d] %s',index,args.limit,row['id'])
        stage = 'scrape'
        try:
            if not row['source']:
                if browser is None:
                    continue
                source = browser.scrape(row)
                state.update(row['id'],source=json.dumps(source.to_dict(),ensure_ascii=False))
            else:
                source = SourceProduct.from_dict(json.loads(row['source']))
            if args.scrape_only:
                continue
            stage = 'generate'
            if not row['product']:
                result = transformer.transform(source,None,state.name_exists)
                product = product_record(result.draft,source,row['created_at'])
                state.update(row['id'],product=json.dumps(product,ensure_ascii=False,separators=(',',':')),model=result.generation_model)
                state.export()
            if publisher:
                stage = 'publish'
                refreshed = state.db.execute('SELECT * FROM items WHERE id=?',(row['id'],)).fetchone()
                publisher.publish(refreshed)
        except AccessBlocked:
            raise
        except TransformationError:
            # A quota outage should not poison thousands of pending items. Keep current
            # row's scrape and stop; a normal rerun retries generation on this row.
            LOG.error('All Gemini models failed. Source saved; resume after checking key/model access or quota.')
            return 1
        except Exception as error:
            if stage == 'publish':
                # Leave generated row pending; a rerun retries exact slug and payload.
                raise ValueError(str(error) if isinstance(error,ValueError) else 'Publish failed; generated payload preserved.') from None
            state.fail(row,stage,error)
            LOG.error('%s failed for %s (%s); use retry-failed after fixing it',stage,row['id'],type(error).__name__)
            failures += 1
    state.export()
    return 1 if failures else 0


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s')
    load_env()
    state = State(args.state_dir)
    try:
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
        transformer = None
        if not args.scrape_only:
            models = tuple(v.strip() for v in os.getenv('GEMINI_MODELS',','.join(MODELS)).split(',') if v.strip())
            if not models:
                raise ValueError('GEMINI_MODELS is empty')
            transformer = GeminiTransformer(client=client(),api_key=os.getenv('GEMINI_API_KEY',''),models=models)
        publisher = None
        if args.publish:
            publisher = Publisher(client(1),state,
                os.getenv('MAGIC_CATALOG_URL','https://magic-catalog.cloudwebsites.workers.dev'),
                os.getenv('MAGIC_CATALOG_IMPORT_TOKEN',''),args.daily_publish_limit)
        needs_browser = not args.offline and (state.summary()['discovered'] < args.limit or
            any(not row['source'] and not row['error'] for row in state.rows(args.limit)))
        if needs_browser:
            with AcquireBrowser(args.state_dir,args.delay,args.jitter,args.headless) as browser:
                browser.discover(state,args.limit,{v.strip().lower() for v in args.types.split(',')},args.max_rounds)
                result = process(state,args,browser,transformer,publisher)
        else:
            result = process(state,args,None,transformer,publisher)
        print(json.dumps(state.summary(),indent=2))
        if args.offline and any(not row['source'] for row in state.rows(args.limit)):
            LOG.warning('Some saved rows have not been scraped; run again without --offline.')
            result = 1
        if not state.rows(args.limit):
            raise ValueError('No saved listings to process.')
        return result
    except KeyboardInterrupt:
        print('\nStopped safely; rerun the same command to resume.')
        return 130
    except (ValueError,AccessBlocked) as error:
        LOG.error('%s',error)
        return 1
    except Exception as error:
        LOG.error('%s: browser/runtime operation failed. Check installation and run login; checkpoints preserved.',type(error).__name__)
        return 1
    finally:
        state.db.close()
