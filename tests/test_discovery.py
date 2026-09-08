import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from acquire_scraper.browser import AcquireBrowser
from acquire_scraper.extract import card_record
from acquire_scraper.state import State


def cards(start,count,kind='SaaS'):
    return [{'href':f'/startup/seller/item{i}','text':f'{kind}\nUseful product idea number {i}\nTTM REVENUE\n$500'} for i in range(start,start+count)]


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.directory=Path(self.tmp.name)
        self.state=State(self.directory); self.browser=AcquireBrowser(self.directory)
        for name in ['page','goto','guard','pace','wait_for_discovery']:
            setattr(self.browser,name,Mock())
        self.browser.advance_discovery=Mock(return_value='see-more')

    def tearDown(self):
        self.state.db.close(); self.tmp.cleanup()

    def run_pages(self,pages,limit=10000,rounds=100):
        iterator=iter(pages); current=[]
        def snapshot():
            nonlocal current
            current=next(iterator,current)
            return current
        self.browser.discovery_snapshot=snapshot
        return self.browser.discover(self.state,limit,{'saas'},rounds)

    def test_past_thirteen_across_nonmatching_pages_and_resume(self):
        first=cards(0,13)+cards(13,5,'Ecommerce')
        self.state.add(card_record(first[0]['href'],first[0]['text'],{'saas'}))
        result=self.run_pages([first,cards(18,18,'Ecommerce'),cards(36,18),cards(54,18)],limit=49)
        self.assertEqual(result['reason'],'limit-reached'); self.assertEqual(self.state.count(),49)
        self.assertEqual(self.browser.advance_discovery.call_count,3)
        self.assertEqual(self.browser.wait_for_discovery.call_count,3)
        self.assertEqual(json.loads((self.directory/'discovery.json').read_text())['saved'],49)

    def test_cumulative_pages_deduplicate(self):
        self.run_pages([cards(0,18),cards(0,36),cards(0,54)],limit=50)
        self.assertEqual(self.state.count(),50)
        self.assertEqual(len({r['url'] for r in self.state.rows(100)}),50)

    def test_looping_pagination_stops(self):
        result=self.run_pages([cards(0,13),cards(13,13)]*10)
        self.assertEqual(result['reason'],'no-more-progress')
        self.assertEqual(self.state.count(),26); self.assertLessEqual(result['round'],7)

    def test_slow_page_and_hydration_recover(self):
        initial=cards(0,13); pending=[{'href':c['href'],'text':''} for c in cards(13,18)]
        result=self.run_pages([initial,initial,initial,initial+pending,initial+cards(13,18)],limit=31)
        self.assertEqual(result['reason'],'limit-reached'); self.assertEqual(self.state.count(),31)
        self.assertTrue(any(c.kwargs['retry'] for c in self.browser.advance_discovery.call_args_list))

    def test_round_ceiling_preserves_work(self):
        result=self.run_pages([cards(0,13)],rounds=1)
        self.assertEqual(result['reason'],'max-rounds'); self.assertEqual(self.state.count(),13)
        self.browser.advance_discovery.assert_not_called()

    def test_at_limit_needs_no_navigation(self):
        c=cards(0,1)[0]; self.state.add(card_record(c['href'],c['text'],{'saas'}))
        self.assertEqual(self.browser.discover(self.state,1,{'saas'})['reason'],'limit-reached')
        self.browser.goto.assert_not_called()


class PaginationControlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.browser=AcquireBrowser(Path(self.tmp.name))
        self.page=self.browser.page=Mock(); self.controls=Mock(); self.controls.all.return_value=[]
        self.body=Mock(); self.body.get_by_role.return_value.all.return_value=[]
        self.sentinels=Mock(); self.sentinels.count.return_value=0
        self.cards=Mock(); self.cards.count.return_value=0
        def locate(selector):
            if 'see-more-button-wrap' in selector: return self.controls
            if selector=='.marketplace-body': return self.body
            if 'load-more-block' in selector: return self.sentinels
            obj=Mock(); obj.filter.return_value=self.cards
            return obj
        self.page.locator.side_effect=locate

    def tearDown(self): self.tmp.cleanup()

    def button(self,visible=True,enabled=True):
        b=Mock(); b.is_visible.return_value=visible; b.is_enabled.return_value=enabled
        b.get_attribute.return_value=None
        return b

    def test_see_more_ignores_hidden_and_disabled_duplicates(self):
        hidden,disabled,active=self.button(False),self.button(enabled=False),self.button()
        self.controls.all.return_value=[hidden,disabled,active]
        self.assertEqual(self.browser.advance_discovery(),'see-more')
        active.click.assert_called_once(); hidden.click.assert_not_called(); disabled.click.assert_not_called()
        self.page.mouse.wheel.assert_not_called(); self.page.get_by_role.assert_not_called()

    def test_named_fallback_includes_see_more_and_is_scoped(self):
        active=self.button(); self.body.get_by_role.return_value.all.return_value=[active]
        self.assertEqual(self.browser.advance_discovery(),'next-button')
        self.assertIsNotNone(self.body.get_by_role.call_args.kwargs['name'].fullmatch('See more'))
        active.click.assert_called_once()

    def test_desktop_targets_sentinel_not_document_bottom(self):
        self.sentinels.count.return_value=1
        self.assertEqual(self.browser.advance_discovery(retry=True),'scroll-sentinel')
        self.assertIn("block:'center'",self.sentinels.last.evaluate.call_args.args[0])
        self.page.mouse.wheel.assert_called_once_with(0,150); self.page.evaluate.assert_called_once()
