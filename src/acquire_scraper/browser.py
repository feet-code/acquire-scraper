"""Direct rendered-page scraping, using a local, normally authenticated browser.

No private API requests, challenge solvers, stealth settings, or paywall interaction.
"""
from __future__ import annotations
import logging
import json
from datetime import datetime, timezone
import random
import re
import time
from urllib.parse import urlsplit
from .extract import LISTING_SELECTOR, TITLE_SELECTOR, DESCRIPTION_SELECTOR, card_record, extract_source

LOG = logging.getLogger(__name__)
START_URL = 'https://app.acquire.com/all-listing'


class AccessBlocked(RuntimeError):
    pass


class AcquireBrowser:
    def __init__(self, directory, delay=8, jitter=4, headless=False):
        self.directory,self.delay,self.jitter,self.headless = directory,max(5,delay),max(0,jitter),headless
        self.next_at = 0
        self.cooldown_path = directory / 'acquire-cooldown.json'
        self.rate_limited_until = 0
        if self.cooldown_path.exists():
            self.rate_limited_until = float(json.loads(self.cooldown_path.read_text())['until'])

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.runtime = sync_playwright().start()
        try:
            self.context = self.runtime.chromium.launch_persistent_context(
                str(self.directory / 'browser-profile'), headless=self.headless,
                viewport={'width':1400,'height':1000}, accept_downloads=False)
            self.context.set_default_timeout(20000)
            # Observe source 429s including the site's own background loading requests.
            self.context.on('response', self.observe_response)
            self.page = self.context.new_page()
            return self
        except BaseException:
            self.runtime.stop()
            raise

    def __exit__(self,*args):
        self.context.close()
        self.runtime.stop()

    def observe_response(self, response):
        if response.status == 429 and urlsplit(response.url).hostname in {'app.acquire.com','acquire.com'}:
            from .http import PoliteHttpClient
            delay = PoliteHttpClient._retry_after(response.headers) or 300
            self.rate_limited_until = max(self.rate_limited_until,time.time()+delay)
            self.cooldown_path.write_text(json.dumps({'until':self.rate_limited_until}))

    def pace(self):
        if self.rate_limited_until > time.time():
            until = datetime.fromtimestamp(self.rate_limited_until,timezone.utc).isoformat()
            raise AccessBlocked(f'Acquire returned 429. Resume after {until}; cooldown is saved locally.')
        time.sleep(max(0,self.next_at-time.monotonic()))
        self.next_at = time.monotonic()+self.delay+random.uniform(0,self.jitter)

    def guard(self,page):
        if self.rate_limited_until > time.time():
            self.pace()
        path = urlsplit(page.url).path
        if path.startswith(('/signin','/signup','/restore-password')):
            raise AccessBlocked('Buyer login required or expired. Run acquire-magic-import login, then resume.')
        title = page.title().lower()
        body = page.locator('body').inner_text()[:2500].lower()
        if any(x in title or x in body for x in ('verify you are human','checking your browser','unusual traffic','just a moment')):
            raise AccessBlocked('Acquire is showing a human verification challenge. Stopping without bypassing it.')
        if page.locator('input[type="password"]:visible').count():
            raise AccessBlocked('Buyer session expired. Run acquire-magic-import login, then resume.')

    def goto(self,page,url):
        p = urlsplit(url)
        if p.scheme != 'https' or p.hostname != 'app.acquire.com' or p.username or p.password:
            raise ValueError('Crawler navigation must stay on app.acquire.com.')
        self.pace()
        response = page.goto(url,wait_until='domcontentloaded',timeout=60000)
        if response and response.status in (401,403,429):
            raise AccessBlocked(f'Acquire HTTP {response.status}; stopping with progress saved.')
        if response and response.status >= 500:
            raise AccessBlocked(f'Acquire HTTP {response.status}; resume later.')
        self.guard(page)
        self.next_at = time.monotonic()+self.delay+random.uniform(0,self.jitter)

    def login(self):
        self.page.goto('https://app.acquire.com/signin',wait_until='domcontentloaded')
        print('Sign into Acquire in the opened browser. Then press Enter here. No password is stored by this script.')
        input()
        self.goto(self.page,START_URL)
        self.page.locator(LISTING_SELECTOR).first.wait_for(state='visible',timeout=60000)
        self.guard(self.page)
        print('Buyer session verified. Browser session is saved locally in .state/browser-profile.')

    def discover(self,state,limit,types,max_rounds=5000):
        if state.count() >= limit:
            return
        self.goto(self.page,START_URL)
        try:
            self.page.locator(LISTING_SELECTOR).first.wait_for(state='visible',timeout=60000)
        except Exception:
            self.guard(self.page)
            raise ValueError('No listing cards found. Run login or inspect the all-listing page; no empty success.') from None
        idle = 0
        previous = set()
        for step in range(max_rounds):
            self.guard(self.page)
            cards = self.page.locator(LISTING_SELECTOR).evaluate_all(
                '(es)=>es.filter(e=>e.getClientRects().length).map(e=>({href:e.getAttribute("href"),text:e.innerText}))')
            urls = {c['href'] for c in cards}
            for card in cards:
                record = card_record(card['href'],card['text'],types)
                if record:
                    state.add(record)
                    if state.count() >= limit:
                        return
            # Count changes to ALL cards, not just SaaS: other categories must not end a crawl.
            idle = idle+1 if urls == previous else 0
            previous = urls
            LOG.info('Discovery round %d: %d eligible listings saved',step+1,state.count())
            self.pace()
            next_button = self.page.get_by_role('button',name=re.compile(r'^(Next|Load more|Show more)$',re.I))
            if next_button.count() == 1 and next_button.is_visible() and next_button.is_enabled():
                next_button.click()
                idle = 0
            else:
                # Scroll the actual scrollable ancestor of listing cards, or the document.
                self.page.locator(LISTING_SELECTOR).last.evaluate('''e=>{
                  let p=e.parentElement;
                  while(p){
                    const o=getComputedStyle(p).overflowY;
                    if(/auto|scroll/.test(o) && p.scrollHeight>p.clientHeight){p.scrollTop=p.scrollHeight;return;}
                    p=p.parentElement;
                  }
                  document.scrollingElement.scrollTop=document.scrollingElement.scrollHeight;
                }''')
            if idle >= 5:
                LOG.info('No further listing cards after five paced checks; discovery stopped at current coverage.')
                break
            self.page.wait_for_timeout(1000)
        if not state.count():
            raise ValueError('No eligible SaaS cards found; check session and listing layout.')

    def scrape(self,record):
        self.goto(self.page,record['url'])
        try:
            self.page.locator(DESCRIPTION_SELECTOR).wait_for(state='visible',timeout=30000)
        except Exception:
            self.guard(self.page)
            raise ValueError('Product description selector missing; page may be removed or layout changed.') from None
        # Expand only the public product-description block, never financial/private sections.
        about = self.page.locator('.base-info-wrap__about')
        expand = about.get_by_text('See more',exact=True)
        if expand.count() == 1 and expand.is_visible():
            self.pace()
            expand.click()
        title = self.page.locator(TITLE_SELECTOR).inner_text()
        description = self.page.locator(DESCRIPTION_SELECTOR).inner_text()
        return extract_source(dict(record),title,description)
