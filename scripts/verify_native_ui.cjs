// Run with NODE_PATH set to the bundled browser-automation packages.
const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const login = JSON.parse(fs.readFileSync('secrets/native_login.json', 'utf8'));
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const response = await page.request.post('http://localhost:3107/api/auth/sign-in/email', {
      data: login, headers: { Origin: 'http://localhost:3107' },
    });
    if (!response.ok()) throw new Error(`Native login failed: ${response.status()}`);
    await page.goto('http://localhost:3107', { waitUntil: 'networkidle' });
    await page.getByText('Capture a livestream', { exact: true }).click();
    await page.getByRole('button', { name: 'Start bounded capture' }).waitFor();
    if (!(await page.request.get('http://localhost:3107/api/capture')).ok()) throw new Error('Capture proxy failed');
    await page.screenshot({ path: 'data/native-supoclip.png', fullPage: true });
    await page.goto('http://localhost:3107/list', { waitUntil: 'networkidle' });
    await page.locator('a[href*="/tasks/"]').first().waitFor();
    await page.goto('http://localhost:3107/tasks/2c54c15c-f125-418b-9db2-31441e87ef5e', { waitUntil: 'networkidle' });
    const video = page.locator('video').first();
    await video.waitFor();
    await video.evaluate(v => { v.muted = true; return v.play(); });
    await page.waitForFunction(() => [...document.querySelectorAll('video')].some(v => v.currentTime > 0 && !v.error));
    await page.goto('http://localhost:3107/tasks/2c54c15c-f125-418b-9db2-31441e87ef5e/edit', { waitUntil: 'networkidle' });
    await page.getByText('Preparing your editing canvas', { exact: true }).waitFor({ state: 'hidden', timeout: 120000 });
    await page.screenshot({ path: 'data/native-editor.png', fullPage: true });
    await page.goto('http://localhost:3107/settings/social', { waitUntil: 'networkidle' });
    if (!(await page.locator('body').innerText()).includes('Stream Highlights Fan')) throw new Error('YouTube connection not visible');
    await page.goto('http://localhost:3107/changelog', { waitUntil: 'networkidle' });
    if (!(await page.locator('body').innerText()).includes('bounded livestream')) throw new Error('Changelog missing');
    console.log('Native login, capture controls, library, video playback, editor route, channel connection and changelog passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
