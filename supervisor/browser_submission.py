"""Verified-selector campaign adapter; never guesses a live form or bypasses login."""
import json
import os
import time
from pathlib import Path


def perform(config, url=None):
    if not config.get('submission_verified') or not config.get('ai_editing_allowed'):
        raise ValueError('Campaign eligibility and supervised submission must be verified first')
    required = ('campaign_url', 'ready_selector', 'url_selector', 'submit_selector', 'receipt_template')
    if not all(config.get(k) for k in required):
        raise ValueError('Record selectors from the supervised campaign submission first')
    if not config['campaign_url'].startswith('https://contentrewards.com/'):
        raise ValueError('Only the verified Content Rewards destination is enabled')
    state = Path(os.getenv('SECRETS_DIR', '/secrets')) / 'campaign-session.json'
    if not state.exists():
        raise ValueError('Campaign browser session is not connected')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(storage_state=str(state))
            page = context.new_page()
            page.goto(config['campaign_url'], wait_until='domcontentloaded', timeout=30000)
            if config.get('open_form_selector'):
                page.locator(config['open_form_selector']).click(timeout=10000)
            page.locator(config['ready_selector']).wait_for(state='visible', timeout=10000)
            if not url:
                return {'healthy_until': time.time() + 45}
            video_id = url.rstrip('/').rsplit('/', 1)[-1]
            receipt = page.locator(config['receipt_template'].replace('{video_id}', video_id))
            if receipt.count() and receipt.is_visible():
                return {'already_submitted': True, 'url': url, 'receipt': receipt.inner_text()}
            page.locator(config['url_selector']).fill(url)
            page.locator(config['submit_selector']).click()
            receipt.wait_for(state='visible', timeout=20000)
            evidence = Path(os.getenv('DATA_DIR', '/data')) / 'receipts'
            evidence.mkdir(parents=True, exist_ok=True)
            screenshot = evidence / (video_id + '.png')
            page.screenshot(path=str(screenshot), full_page=True)
            return {'url': url, 'receipt': receipt.inner_text(), 'screenshot': str(screenshot), 'submitted_at': time.time()}
        finally:
            browser.close()
