import { expect, test, type Page } from '@playwright/test';
import { mkdir } from 'node:fs/promises';

const email = process.env.CAUCHE_TEST_EMAIL ?? 'owner@example.test';
const password = process.env.CAUCHE_TEST_PASSWORD;

async function login(page: Page) {
  if (!password) throw new Error('Set CAUCHE_TEST_PASSWORD for the ephemeral test owner');
  await page.goto('/');
  await page.getByLabel('E-mail').fill(email);
  await page.getByLabel('Senha', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Entrar', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Visão geral' })).toBeVisible();
}

async function confirmIfShown(page: Page) {
  const approval = page.getByRole('dialog');
  if (await approval.isVisible()) await approval.getByRole('button', { name: 'Confirmar', exact: true }).click();
}

test('redirected owner login, protected views and mobile layout', async ({ page }) => {
  expect((await page.request.get('/api/admin/overview')).status()).toBe(401);
  await login(page);
  await mkdir('test-results/evidence', { recursive: true });
  await expect(page.getByRole('img', { name: /Candles/ })).toBeVisible();
  await page.screenshot({ path: 'test-results/evidence/overview.png', fullPage: true });
  await page.getByRole('link', { name: 'Administração', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Visão operacional' }).first()).toBeVisible();
  await page.goto('/');
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('heading', { name: 'Visão geral' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: 'test-results/evidence/mobile.png', fullPage: true });
});

test('fixture replay and paper orders, lifecycle and persisted event recovery', async ({ page, baseURL }) => {
  await login(page);
  const auth = await page.request.get('/api/auth/session');
  const session = await auth.json();
  const headers = { Origin: new URL(baseURL!).origin, 'X-CSRF-Token': session.csrf_token };
  const startRejected = await page.request.post('/api/bot/start', {
    headers, data: { mode: 'REAL', exchange: 'DERIV', strategy: 'DERIV-STRAT-01', instrument: 'EUR/USD' },
  });
  expect(startRejected.ok()).toBe(false);
  const noCsrf = await page.request.post('/api/bot/stop', { headers: { Origin: headers.Origin } });
  expect(noCsrf.status()).toBe(403);

  for (const mode of ['REPLAY', 'PAPER']) {
    const data = { mode, exchange: 'KALSHI', strategy: 'KALSHI-STRAT-01', instrument: 'FIXTURE-TEMP-NYC' };
    const starts = await Promise.all([
      page.request.post('/api/bot/start', { headers, data }),
      page.request.post('/api/bot/start', { headers, data }),
    ]);
    expect(starts.map(reply => reply.status()).sort()).toEqual([202, 409]);
    await expect.poll(async () => (await (await page.request.get('/api/bot')).json()).state,
      { timeout: 20_000 }).toBe('RUNNING');
    if (mode === 'PAPER') {
      const pause = await page.request.post('/api/bot/pause', { headers });
      expect(pause.ok(), await pause.text()).toBeTruthy();
      await expect.poll(async () => (await (await page.request.get('/api/bot')).json()).state).toBe('PAUSED');
      const resume = await page.request.post('/api/bot/resume', { headers });
      expect(resume.ok(), await resume.text()).toBeTruthy();
    }
    await expect.poll(async () => {
      const result = await (await page.request.get('/api/events?mode=' + mode + '&limit=100')).json();
      return result.items?.some((event: { type: string }) => event.type === 'SETTLEMENT');
    }, { timeout: 50_000 }).toBe(true);

    const stop = await page.request.post('/api/bot/stop', { headers });
    if (!stop.ok()) expect((await (await page.request.get('/api/bot')).json()).state).toBe('STOPPED');
    await expect.poll(async () => (await (await page.request.get('/api/bot')).json()).state).toBe('STOPPED');
  }

  const events = await (await page.request.get('/api/events?limit=10')).json();
  expect(events.items.length).toBeGreaterThan(1);
  const cursor = Math.min(...events.items.map((event: { id: number }) => event.id));
  const received = await page.evaluate((after) => new Promise<number>((resolve, reject) => {
    const url = new URL('/api/ws?after=' + after, location.origin);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(url);
    const timeout = setTimeout(() => {
      socket.close();
      reject(new Error('No replayed event arrived'));
    }, 10_000);
    socket.onerror = () => reject(new Error('Socket error'));
    socket.onmessage = (message) => {
      const { id } = JSON.parse(message.data);
      if (typeof id === 'number') {
        clearTimeout(timeout);
        socket.close();
        resolve(id);
      }
    };
  }), cursor);
  expect(received).toBeGreaterThan(cursor);

  await page.getByRole('link', { name: 'Terminal', exact: true }).click();
  await expect(page.getByRole('heading', { name: /Terminal/ }).first()).toBeVisible();
  await page.screenshot({ path: 'test-results/evidence/terminal.png', fullPage: true });
  await page.getByRole('link', { name: 'Histórico', exact: true }).click();
  await expect(page.getByRole('heading', { name: /Histórico/ }).first()).toBeVisible();
  await page.getByRole('link', { name: 'Eventos', exact: true }).click();
  await expect(page.getByRole('heading', { name: /Eventos/ }).first()).toBeVisible();
});

test('execution starts and stops through terminal controls', async ({ page }) => {
  await login(page);
  await page.getByRole('link', { name: 'Terminal', exact: true }).click();
  await page.getByLabel('Estratégia', { exact: true }).selectOption('DERIV-STRAT-02');
  await page.getByRole('button', { name: 'Iniciar operação', exact: true }).click();
  await confirmIfShown(page);
  await expect(page.getByRole('button', { name: 'Pausar operação', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Encerrar operação', exact: true }).click();
  await confirmIfShown(page);
  await expect(page.getByRole('button', { name: 'Iniciar operação', exact: true })).toBeEnabled();
});


test('backtest result and encrypted connection settings through the interface', async ({ page }) => {
  await login(page);
  await page.getByRole('link', { name: 'Backtests', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Backtests' })).toBeVisible();
  await page.getByLabel('Corretora', { exact: true }).selectOption('KALSHI');
  await page.getByRole('button', { name: 'Executar backtest', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Métricas do backtest' })).toBeVisible({ timeout: 30_000 });
  await page.screenshot({ path: 'test-results/evidence/backtest.png', fullPage: true });

  await page.getByRole('link', { name: 'Administração', exact: true }).click();
  await page.getByRole('link', { name: 'Conexões', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Conexões' })).toBeVisible();
  const deriv = page.locator('form').filter({ has: page.getByRole('heading', { name: 'Deriv', exact: true }) });
  await deriv.getByLabel(/ID do aplicativo/i).fill('synthetic-ui-app');
  await deriv.getByLabel(/token/i).fill('synthetic-ui-fixture');
  await deriv.getByRole('button', { name: 'Salvar conexão' }).click();
  await confirmIfShown(page);
  await expect(deriv.getByText('Configuração salva')).toBeVisible();
  const stored = await (await page.request.get('/api/admin/connections')).json();
  expect(JSON.stringify(stored)).not.toContain('synthetic-ui-fixture');
  expect(stored.items.find((row: { exchange: string }) => row.exchange === 'DERIV').configured).toBe(true);
  await deriv.getByRole('button', { name: 'Desconectar', exact: true }).click();
  await confirmIfShown(page);
  await expect(deriv.getByText('Conexão removida')).toBeVisible();
  await page.screenshot({ path: 'test-results/evidence/admin.png', fullPage: true });
});
