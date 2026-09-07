from __future__ import annotations
import json
from urllib.parse import urlsplit
from .http import HttpError
from .intents import INTENTS
from .state import now


def product_record(draft, source, created_at):
    product = draft.to_dict()
    # Slug derives solely from source identity, so even a lost checkpoint + regenerated
    # brand cannot create a second product or overwrite another importer's slug.
    product.update(slug='acq-'+source.external_id, id='acquire-'+source.external_id, createdAt=created_at)
    return product


class Publisher:
    def __init__(self, client, state, url, token, daily_limit=5000):
        p = urlsplit(url)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('','/'):
            raise ValueError('MAGIC_CATALOG_URL must be an HTTPS origin without credentials or a path.')
        if not token:
            raise ValueError('Set MAGIC_CATALOG_IMPORT_TOKEN to the Worker ADMIN_REINDEX_TOKEN.')
        self.client,self.state,self.url,self.token,self.daily_limit = client,state,url.rstrip('/'),token,daily_limit

    def publish(self, row):
        product = json.loads(row['product'])
        key = product['intentKey']
        registered = self.state.db.execute('SELECT 1 FROM published_intents WHERE key=?',(key,)).fetchone()
        payload = {'products':[product], 'intents':[] if registered else [INTENTS[key]]}
        # One HTTP attempt per publish call: charge retries too, including partial writes.
        self.state.claim_budget(self.daily_limit)
        try:
            response = self.client.post_json(self.url+'/api/admin/catalog/ingest',payload,
                max_bytes=100000,headers={'Authorization':'Bearer '+self.token})
        except HttpError as error:
            hint = {404:'Check URL, deployment, and ADMIN_REINDEX_TOKEN (not ADMIN_IMPORT_TOKEN).',
                    400:'Catalog rejected the payload; verify the deployed schema.',
                    503:'Run npm run scale:setup in Magic Catalog, then npm run deploy.',
                    429:'Catalog rate limit reached; resume later.'}.get(error.status,'Transient ingest failure; rerun to safely retry.')
            raise ValueError(f'Catalog HTTP {error.status}: {hint}') from None
        result = json.loads(response.text())
        if (result.get('ok') is not True or result.get('products') != 1 or
            product['slug'] not in [v.get('slug') for v in result.get('written',[]) if isinstance(v,dict)]):
            raise ValueError('Catalog returned an unexpected acknowledgement; product remains pending.')
        with self.state.db:
            self.state.db.execute('UPDATE items SET published_at=?,error=NULL WHERE id=?',(now(),row['id']))
            self.state.db.execute('INSERT OR IGNORE INTO published_intents(key) VALUES(?)',(key,))
