// Isolated local preview: no real accounts, mail delivery, payments or deployment.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const { once } = require('node:events');
const { chromium } = require('playwright');

async function main() {
  const root = path.resolve(__dirname, '..');
  const runtime = fs.mkdtempSync(path.join(os.tmpdir(), 'netcare-web-review-'));
  const python = process.env.NETCARE_TEST_PYTHON || path.join(root, 'build/netcare-backend-venv/bin/python');
  const origin = 'http://127.0.0.1:8017';
  const env = { ...process.env, RELAY_ENV: 'test', RELAY_RUNTIME_DIR: runtime, PUBLIC_URL: origin,
    DATABASE_URL: '', EMAIL_BACKEND: 'django.core.mail.backends.locmem.EmailBackend' };
  const migrate = spawnSync(python, ['manage.py', 'migrate', '--noinput'],
    { cwd: path.join(root, 'backend'), env, encoding: 'utf8', timeout: 60000 });
  assert.equal(migrate.status, 0, migrate.stderr);
  const server = spawn(python, ['manage.py', 'runserver', '127.0.0.1:8017', '--noreload'],
    { cwd: path.join(root, 'backend'), env, stdio: ['ignore', 'pipe', 'pipe'] });
  let output = '';
  server.stdout.on('data', data => { output += data; });
  server.stderr.on('data', data => { output += data; });
  const exit = once(server, 'exit');
  let browser;
  try {
    let ready = false;
    for (let i = 0; i < 100; i++) {
      if (server.exitCode !== null) throw new Error(output);
      try { ready = (await fetch(origin + '/healthz')).ok; } catch {}
      if (ready) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.ok(ready, output);
    browser = await chromium.launch({ headless: true, channel: 'chrome' });
    const shots = path.join(root, 'build/ui-preview');
    fs.mkdirSync(shots, { recursive: true });
    let checked = 0;
    for (const [label, viewport] of [['desktop', { width: 1280, height: 900 }], ['mobile', { width: 390, height: 844 }]]) {
      const page = await browser.newPage({ viewport });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      for (const route of ['/', '/features/', '/pricing/', '/download/', '/login/', '/account/', '/orders/', '/desktop/authorize/', '/support/', '/privacy/', '/terms/']) {
        await page.goto(origin + route);
        await page.locator('main h1').waitFor();
        assert.match(await page.title(), /NetCare/);
        assert.doesNotMatch(await page.locator('body').innerText(), /Relay/);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        assert.equal(await page.locator('.brand').first().innerText(), 'NetCare');
        if (route === '/' || route === '/download/') {
          await page.screenshot({ path: path.join(shots, `netcare-web-${route === '/' ? 'home' : 'download'}-${label}.png`), fullPage: true });
        }
        checked++;
      }
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log(JSON.stringify({ checked, brand: 'NetCare', viewports: 2, networkChanges: false }));
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
    await exit;
    fs.rmSync(runtime, { recursive: true, force: true });
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
