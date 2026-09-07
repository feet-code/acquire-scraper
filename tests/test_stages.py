import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from acquire_scraper.cli import main
from acquire_scraper.state import State
from acquire_scraper.models import SourceProduct
from acquire_scraper.batch import Paused

class StageTests(unittest.TestCase):
    def test_generation_pause_still_publishes_saved_products_and_keeps_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            state=State(Path(directory))
            state.add({'id':'one','url':'https://app.acquire.com/startup/a/b','title':'Sample product title','kind':'SaaS'})
            source=SourceProduct('one','https://app.acquire.com/startup/a/b','',description='Saved source')
            state.update('one',source=json.dumps(source.to_dict()))
            state.db.close()
            ledger=Mock()
            with patch('acquire_scraper.cli.Ledger',return_value=ledger),patch('acquire_scraper.cli.generate_queue',side_effect=Paused('daily limit')) as generate,patch('acquire_scraper.cli.publish_queue') as publish,patch('acquire_scraper.cli.AcquireBrowser') as browser:
                code=main(['--state-dir',directory,'run','--limit','1','--offline','--publish'])
                self.assertEqual(code,1)
                generate.assert_called_once();publish.assert_called_once();browser.assert_not_called()
            state=State(Path(directory));self.assertIsNotNone(state.rows(1)[0]['source']);self.assertIsNone(state.rows(1)[0]['error']);state.db.close()

    def test_publish_only_never_creates_generator_or_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            state=State(Path(directory));state.add({'id':'one','url':'https://app.acquire.com/startup/a/b','title':'Example product','kind':'SaaS'});state.update('one',source='{}');state.db.close()
            with patch('acquire_scraper.cli.Ledger',return_value=Mock()),patch('acquire_scraper.cli.generate_queue') as generate,patch('acquire_scraper.cli.publish_queue') as publish,patch('acquire_scraper.cli.AcquireBrowser') as browser:
                self.assertEqual(main(['--state-dir',directory,'run','--stage','publish','--limit','1']),0)
                generate.assert_not_called();browser.assert_not_called();publish.assert_called_once()
