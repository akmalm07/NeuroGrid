import { readFileSync } from 'node:fs'
import { expect, test } from '@playwright/test'

const urls = [
  ['/chat', 'Chat'],
  ['/network', 'Network'],
  ['/ledger', 'Ledger'],
  ['/downloads', 'Start computing'],
] as const

test('home has both entry points and every page fits a mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'One model. Many machines.' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Start Computing' })).toHaveAttribute('href', '/downloads')
  await expect(page.getByRole('link', { name: 'Try NeuroGrid' })).toHaveAttribute('href', '/chat')
  for (const [url, title] of urls) {
    await page.goto(url)
    await expect(page.getByRole('heading', { name: title, exact: true, level: 1 })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  }
})

test('ledger verifies in a fresh browser', async ({ page }) => {
  await page.goto('/ledger')
  await page.getByRole('button', { name: 'Verify ledger' }).click()
  await expect(page.getByRole('status').filter({ hasText: 'blocks from genesis' })).toBeVisible()
  await page.getByText('Genesis', { exact: true }).click()
  await expect(page.getByText('Previous hash', { exact: true })).toBeVisible()
})

test('a signed-in browser runs a live prompt and preserves the reply after reload', async ({ page }) => {
  const path = process.env.NEUROGRID_TEST_ACCOUNTS
  test.skip(!path, 'Needs a funded DeepSpace test account and active model workers')
  const [account] = JSON.parse(readFileSync(path!, 'utf8').replace(/^\uFEFF/, ''))
  await page.goto('/chat')
  const response = await page.request.post('/api/auth/sign-in/email', { data: account })
  expect(response.status()).toBe(200)
  await page.reload()
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible()
  await page.getByLabel('Maximum tokens').selectOption('4')
  const prompt = 'Reply with one word: blue.'
  await page.getByRole('textbox', { name: 'Message' }).fill(prompt)
  await page.getByRole('button', { name: 'Send prompt' }).click()
  await expect(page.locator('.chat-turn').filter({ hasText: prompt }).last()).toContainText('tokens ·', { timeout: 60_000 })
  const completed = await page.locator('.chat-turn').filter({ hasText: prompt }).last().textContent()
  await page.reload()
  await expect(page.locator('.chat-turn').filter({ hasText: prompt }).last()).toHaveText(completed ?? '')
})
