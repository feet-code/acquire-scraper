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
from .extract import LISTING_SELECTOR, TITLE_SELECTOR, DESCRIPTION_SELECTOR, card_record, extract_source, canonical_url

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

    def discovery_snapshot(self):
        # Keep extraction scoped to the results, excluding recommendation carousels.
        scope = self.page.locator('.marketplace-body .projects-list').filter(visible=True)
        if not scope.count():
            scope = self.page.locator('body')
        cards = scope.locator(LISTING_SELECTOR).evaluate_all('''es=>es
            .filter(e=>e.getClientRects().length)
            .map(e=>({href:e.getAttribute('href'),text:e.innerText}))''')
        return cards

    def advance_discovery(self, retry=False):
        # Acquire's tablet layout uses "See more", not "Load more". Scope this
        # to pagination so filter-chip "Show more" and description controls are untouched.
        controls = self.page.locator(
            '.marketplace-body .see-more-button-wrap button, '
            '.marketplace-body .see-more-button-wrap [role="button"], '
            '.marketplace-body button[rel="next"], '
            '.marketplace-body a[rel="next"]')
        for button in controls.all():
            if button.is_visible() and button.is_enabled() and button.get_attribute('aria-disabled') != 'true':
                button.click()
                return 'see-more'
        # Named fallbacks are limited to results, never global "Show more".
        for button in self.page.locator('.marketplace-body').get_by_role(
                'button', name=re.compile(r'^(?:Next(?: page)?|Load more|Show more|See more)$',re.I)).all():
            if button.is_visible() and button.is_enabled() and button.get_attribute('aria-disabled') != 'true':
                button.click()
                return 'next-button'
        # A zero-height observer sentinel is valid; only require its results parent to be visible.
        sentinels = self.page.locator('.marketplace-body:visible .load-more-block')
        if sentinels.count():
            target = sentinels.last
            # Desktop loads only after a real window scroll AND sentinel intersection.
            # Scrolling straight to the document bottom can overshoot it into the footer.
            if retry:
                self.page.evaluate('window.scrollBy(0, -Math.max(700, window.innerHeight))')
                self.page.wait_for_timeout(150)
            target.evaluate("e=>e.scrollIntoView({block:'center',behavior:'instant'})")
            self.page.mouse.wheel(0, 150)
            return 'scroll-sentinel'
        cards = self.page.locator(LISTING_SELECTOR).filter(visible=True)
        if cards.count():
            cards.last.evaluate("e=>e.scrollIntoView({block:'center',behavior:'instant'})")
            self.page.mouse.wheel(0, 600)
            return 'scroll-fallback'
        return 'wait-for-cards'

    def wait_for_discovery(self, before, timeout=20000):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        try:
            # Changes in text matter too: card data hydrates after the URL appears.
            self.page.wait_for_function('''({selector,before})=>{
                const roots=[...document.querySelectorAll('.marketplace-body .projects-list')]
                    .filter(e=>e.getClientRects().length);
                const cards=(roots.length?roots:[document.body]).flatMap(root=>
                    [...root.querySelectorAll(selector)]).filter(e=>e.getClientRects().length)
                    .map(e=>({href:e.getAttribute('href'),text:e.innerText}));
                return JSON.stringify(cards)!==JSON.stringify(before);
            }''', arg={'selector':LISTING_SELECTOR,'before':before}, timeout=timeout)
            # Let React mount sibling cards before taking the next checkpoint.
            self.page.wait_for_timeout(750)
        except PlaywrightTimeout:
            pass
        self.guard(self.page)

    def discover(self,state,limit,types,max_rounds=5000):
        if state.count() >= limit:
            return {'reason':'limit-reached','saved':state.count()}
        self.goto(self.page,START_URL)
        try:
            self.page.locator(LISTING_SELECTOR).first.wait_for(state='visible',timeout=60000)
        except Exception:
            self.guard(self.page)
            raise ValueError('No listing cards found. Run login or inspect the all-listing page; no empty success.') from None
        seen = set()
        idle = 0
        action = 'initial'
        report = {}
        for step in range(max_rounds):
            self.guard(self.page)
            cards = self.discovery_snapshot()
            new_urls = set()
            eligible = 0
            added = 0
            for card in cards:
                try:
                    url = canonical_url(card['href'])
                    new_urls.add(url)
                    record = card_record(url,card['text'],types)
                except ValueError:
                    continue
                if record:
                    eligible += 1
                    added += state.add(record)
                    if state.count() >= limit:
                        break
            unseen = new_urls - seen
            seen.update(new_urls)
            # Count global progress, not just a changed screen (a looping paginator
            # must eventually stop). Allow hydration of the same URLs to settle.
            idle = 0 if unseen or added else idle+1
            report = {'reason':'running','round':step+1,'seen_this_run':len(seen),
                      'visible_cards':len(cards),'eligible_cards':eligible,
                      'saved':state.count(),'requested_limit':limit,'last_action':action,
                      'unchanged_rounds':idle,'types':sorted(types),'updated_at':datetime.now(timezone.utc).isoformat()}
            LOG.info('Discovery round %d: %d cards, %d eligible on screen; %d unique seen; %d saved / %d limit; action=%s',
                     step+1,len(cards),eligible,len(seen),state.count(),limit,action)
            if state.count() >= limit:
                report['reason'] = 'limit-reached'
            elif idle >= 5:
                report['reason'] = 'no-more-progress'
            elif step+1 == max_rounds:
                report['reason'] = 'max-rounds'
            self.save_discovery_report(report)
            if report['reason'] != 'running':
                break
            self.pace()
            action = self.advance_discovery(retry=idle>0)
            self.wait_for_discovery(cards)
        if report.get('reason') != 'limit-reached':
            LOG.warning('Discovery stopped: %s; %d eligible saved, %d unique listings seen this run. '
                        'This does not confirm all Acquire inventory was exhausted. Check All listings filters '
                        'and the open browser; diagnostics: %s. Rerun to resume without duplicates.',
                        report.get('reason'),state.count(),len(seen),self.directory / 'discovery.json')
        if not state.count():
            raise ValueError('No eligible SaaS cards found; check session and listing layout.')
        return report

    def save_discovery_report(self, report):
        path = self.directory / 'discovery.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(report,indent=2),encoding='utf-8')
        tmp.replace(path)

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
