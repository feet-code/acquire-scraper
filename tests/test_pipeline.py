import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import Request
from acquire_scraper.extract import canonical_url, listing_id, card_record, extract_source
from acquire_scraper.gemini import validate_draft, GeminiTransformer, transformation_issues
from acquire_scraper.intents import INTENTS
from acquire_scraper.http import HttpResponse, HttpError, _ValidatingRedirectHandler
from acquire_scraper.publisher import Publisher, product_record
from acquire_scraper.state import State
from acquire_scraper.cli import main

URL = 'https://app.acquire.com/startup/seller1/listing1'
CARD = 'SaaS\nAdd to My deals\nCreate list\nSave\nInvoice reminders for small teams\nTTM REVENUE\n$50k'
DESCRIPTION = 'Small teams use invoice reminders to keep track of late payments. They organize follow ups and review open invoices in a single dashboard.'
DRAFT = dict(name='LedgerBeacon',category='Receivables',audience='Freelance consultants',
    problem='Independent consultants lose track of outstanding client balances.',
    promise='Keep collection tasks organized.',differentiator='A focused queue highlights the next client balance to review.',
    workflow=['Add outstanding balances','Choose the next account','Record the follow up'],
    keywords=['billing','collections','invoices','reminders'],metrics=['Open balances','Time spent'],intentKey='invoice-operations')


def response(payload):
    return HttpResponse(200,'https://example.com',{},json.dumps(payload).encode())


class ExtractionTests(unittest.TestCase):
    def test_identity_discards_tracking_not_listing_id(self):
        self.assertEqual(canonical_url(URL+'?source=x#y'),URL)
        self.assertEqual(listing_id(URL),listing_id(URL+'?source=y'))
        self.assertNotEqual(listing_id(URL),listing_id(URL.replace('listing1','listing2')))

    def test_rejects_other_origins_and_non_listings(self):
        for url in ['http://app.acquire.com/startup/a/b','https://app.acquire.com.evil.com/startup/a/b',
                    'https://user:pass@app.acquire.com/startup/a/b','https://app.acquire.com:444/startup/a/b','/signin']:
            with self.assertRaises(ValueError): canonical_url(url)

    def test_only_requested_categories_and_no_financials(self):
        self.assertEqual(card_record(URL,CARD,{'saas'})['title'],'Invoice reminders for small teams')
        self.assertIsNone(card_record(URL,CARD.replace('SaaS','Ecommerce'),{'saas'}))

    def test_anonymous_description_without_website(self):
        record = card_record(URL,CARD,{'saas'})
        source = extract_source(record,record['title'],DESCRIPTION)
        self.assertIsNone(source.website_url)
        self.assertEqual(source.source_name,'')
        self.assertEqual(source.description,DESCRIPTION)

    def test_access_prompt_not_usable_product(self):
        record = card_record(URL,CARD,{'saas'})
        with self.assertRaises(ValueError): extract_source(record,record['title'],'Upgrade to unlock '*12)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name))
        self.record = card_record(URL,CARD,{'saas'})
        self.state.add(self.record)
        self.source = extract_source(self.record,self.record['title'],DESCRIPTION)
        self.product = product_record(validate_draft(DRAFT),self.source,'2026-09-07T00:00:00Z')
        self.state.update(self.record['id'],source=json.dumps(self.source.to_dict()),product=json.dumps(self.product))

    def tearDown(self):
        self.state.db.close()
        self.tmp.cleanup()

    def test_stable_slug_across_regeneration(self):
        other = validate_draft(dict(DRAFT,name='NewBrand'))
        self.assertEqual(self.product['slug'],product_record(other,self.source,'2026-09-08T00:00:00Z')['slug'])

    def test_unknown_intent_rejected(self):
        with self.assertRaises(ValueError): validate_draft(dict(DRAFT,intentKey='new-vector-for-every-product'))

    def test_gemini_falls_through_unavailable_model(self):
        client = Mock()
        payload = {'candidates':[{'content':{'parts':[{'text':json.dumps(DRAFT)}]}}]}
        client.post_json.side_effect = [HttpError('missing',status=404),response(payload)]
        result = GeminiTransformer(client=client,api_key='test',models=('missing','working')).transform(self.source,None,lambda _:False)
        self.assertEqual(result.generation_model,'working')
        self.assertEqual(client.post_json.call_count,2)

    def test_brand_copy_rejected(self):
        from dataclasses import replace
        self.assertTrue(transformation_issues(replace(self.source,source_name='LedgerBeacon'),None,validate_draft(DRAFT)))

    def test_publish_contract_and_intent_reuse(self):
        client = Mock()
        client.post_json.return_value = response({'ok':True,'products':1,'written':[{'slug':self.product['slug']}]})
        publisher = Publisher(client,self.state,'https://catalog.example','secret')
        publisher.publish(self.state.rows(1)[0])
        call = client.post_json.call_args
        self.assertTrue(call.args[0].endswith('/api/admin/catalog/ingest'))
        self.assertEqual(call.args[1]['intents'],[INTENTS['invoice-operations']])
        self.assertNotIn('sourceUrl',call.args[1]['products'][0])
        publisher.publish(self.state.rows(1)[0])
        self.assertEqual(client.post_json.call_args.args[1]['intents'],[])
        self.assertEqual(self.state.summary()['published'],1)

    def test_partial_failure_retains_same_pending_payload(self):
        client = Mock()
        client.post_json.side_effect = HttpError('failure',status=500)
        with self.assertRaises(ValueError): Publisher(client,self.state,'https://catalog.example','secret').publish(self.state.rows(1)[0])
        self.assertFalse(self.state.rows(1)[0]['published_at'])
        self.assertEqual(json.loads(self.state.rows(1)[0]['product']),self.product)
        self.assertEqual(self.state.db.execute('SELECT attempts FROM budget').fetchone()[0],1)

    def test_bad_ack_is_not_success(self):
        client = Mock()
        client.post_json.return_value = response({'ok':True,'products':1,'written':[{'slug':'wrong'}]})
        with self.assertRaises(ValueError): Publisher(client,self.state,'https://catalog.example','secret').publish(self.state.rows(1)[0])
        self.assertFalse(self.state.rows(1)[0]['published_at'])

    def test_daily_budget_persists(self):
        self.state.claim_budget(1)
        other = State(Path(self.tmp.name))
        try:
            with self.assertRaises(ValueError): other.claim_budget(1)
        finally: other.db.close()

    def test_resume_does_not_duplicate_discovery(self):
        self.assertFalse(self.state.add(self.record))
        self.assertEqual(self.state.summary()['discovered'],1)
        self.assertTrue(self.state.rows(1)[0]['product'])

    def test_offline_scrape_only_needs_no_key_or_browser(self):
        with patch.dict('os.environ',{},clear=True):
            self.assertEqual(main(['--state-dir',self.tmp.name,'run','--limit','1','--offline','--scrape-only']),0)

    def test_export_excludes_research(self):
        exported = json.loads(self.state.export().read_text())
        self.assertNotIn('source_url',exported)
        self.assertEqual(exported['intentKey'],'invoice-operations')

    def test_post_redirect_refused(self):
        with self.assertRaises(HttpError):
            _ValidatingRedirectHandler(False).redirect_request(Request('https://catalog.example',data=b'{}'),None,302,'',{},'https://evil.example')


if __name__ == '__main__': unittest.main()
