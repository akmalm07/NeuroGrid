import { test, expect } from '@playwright/test'

test('auth proxy forwards to DeepSpace', async ({ request }) => {
  expect((await request.get('/api/auth/ok')).ok()).toBe(true)
})

test('the records WebSocket receives the public grid projection', async ({ page }) => {
  const frames: string[] = []
  page.on('websocket', (socket) => {
    if (decodeURIComponent(new URL(socket.url()).pathname).startsWith('/ws/app:')) {
      socket.on('framereceived', ({ payload }) => frames.push(String(payload)))
    }
  })
  await page.goto('/network')
  await expect(page.getByRole('heading', { name: 'Network', exact: true, level: 1 })).toBeVisible()
  await expect.poll(() => frames.some((frame) => frame.includes('grid_status'))).toBe(true)
})

test('private and retired endpoints stay closed to guests', async ({ request }) => {
  expect((await request.get('/api/grid/user/account')).status()).toBe(401)
  expect((await request.post('/api/grid/user/checkout', { data: { credits: 100 } })).status()).toBe(401)
  expect((await request.get('/api/integrations')).status()).toBe(404)
  expect((await request.get('/ws/canvas/unused')).status()).toBe(404)
})
