"""Run locally to sign into the dedicated campaign browser profile yourself."""
from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto('https://contentrewards.com/c/campaigns/33c3d5a9-84a9-40c8-b6e4-9904e4816339')
    input('Sign in yourself in this dedicated browser. Press Enter here once the campaign is visible: ')
    context.storage_state(path=str(root / 'secrets/campaign-session.json'))
    browser.close()
print('Dedicated campaign session saved locally. Eligibility and form verification are still required.')
