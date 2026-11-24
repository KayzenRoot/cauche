import { defineConfig } from '@playwright/test';

export default defineConfig({
  timeout: 150_000,
  expect: { timeout: 12_000 },
  workers: 1,
  use: {
    baseURL: process.env.CAUCHE_TEST_ORIGIN ?? 'http://localhost:8080',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  reporter: [['list'], ['html', { open: 'never' }]],
});
