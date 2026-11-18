"""Native-session browser smoke. Credentials stay in a private local file."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

login = json.loads(Path('secrets/native_login.json').read_text())
with sync_playwright() as p:
    browser = p.chromium.launch(channel='chrome', headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    response = page.request.post('http://localhost:3107/api/auth/sign-in/email', data=login,
                                 headers={'Origin': 'http://localhost:3107'})
    assert response.ok, f'Native login failed: {response.status}'
    page.goto('http://localhost:3107', wait_until='networkidle')
    page.get_by_text('Capture a livestream', exact=True).click()
    assert page.get_by_role('button', name='Start bounded capture').is_visible()
    assert page.request.get('http://localhost:3107/api/capture').ok
    page.screenshot(path='data/native-supoclip.png', full_page=True)
    page.goto('http://localhost:3107/list', wait_until='networkidle')
    assert page.locator('a[href*="/tasks/"]').count() > 0
    page.goto('http://localhost:3107/settings/social', wait_until='networkidle')
    assert 'Stream Highlights Fan' in page.locator('body').inner_text()
    page.goto('http://localhost:3107/changelog', wait_until='networkidle')
    assert 'bounded livestream' in page.locator('body').inner_text()
    print('Native login, capture controls, task library, YouTube connection and changelog passed.')
    browser.close()
