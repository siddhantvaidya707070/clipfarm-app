// Uses the existing private local login; never prints credentials or cookies.
const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const login = JSON.parse(fs.readFileSync('secrets/native_login.json', 'utf8'));
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.name));
    const response = await page.request.post('http://localhost:3107/api/auth/sign-in/email', {
      data: login, headers: { Origin: 'http://localhost:3107' },
    });
    if (!response.ok()) throw new Error(`Local sign-in failed (${response.status()})`);
    await page.goto('http://localhost:3107/highlights', { waitUntil: 'networkidle' });
    await page.getByRole('heading', { name: 'Highlights', exact: true }).waitFor();
    await page.getByRole('button', { name: 'Find highlights', exact: true }).waitFor();
    const streamsResponse = await page.request.get('http://localhost:3107/api/highlights');
    if (!streamsResponse.ok()) throw new Error(`Signed highlight proxy failed (${streamsResponse.status()})`);
    const streams = await streamsResponse.json();
    if (streams.config.model !== 'gpt-transcribe') throw new Error('Wrong transcription model configured');
    const invalid = await page.request.post('http://localhost:3107/api/highlights', {
      data: { source: 'https://example.com/not-youtube' }, headers: { Origin: 'http://localhost:3107' },
    });
    if (invalid.status() !== 422) throw new Error(`Unsupported source validation failed (${invalid.status()}): ${(await invalid.text()).slice(0, 250)}`);
    await page.screenshot({ path: 'data/highlights-desktop.png', fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: 'data/highlights-mobile.png', fullPage: true });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    if (overflow) throw new Error('Highlights screen overflows the mobile viewport');
    if (errors.length) throw new Error('Browser runtime errors: ' + errors.join(', '));
    console.log('Highlights sign-in, model configuration, signed proxy, source validation, and desktop/mobile layouts passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
