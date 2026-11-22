import { expect, test } from '@playwright/test'
import { signIn, testAccounts } from './helpers/auth'

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
  const genesis = page.locator('details').filter({ hasText: 'Genesis' })
  await genesis.getByText('Genesis', { exact: true }).click()
  await expect(genesis.getByText('Previous hash', { exact: true })).toBeVisible()
})

test('a signed-in browser runs a live prompt and preserves the reply after reload', async ({ page }) => {
  test.skip(process.env.NEUROGRID_TEST_INFERENCE !== '1', 'Start the three workers and verifier first')
  const [account] = testAccounts()
  test.setTimeout(90_000)
  await page.goto('/chat')
  await signIn(page, account)
  await page.getByLabel('Maximum tokens').selectOption('4')
  const prompt = 'Reply with one word: blue. Reference ' + Date.now()
  await page.getByRole('textbox', { name: 'Message' }).fill(prompt)
  await page.getByRole('button', { name: 'Send prompt' }).click()
  await expect(page.locator('.chat-turn').filter({ hasText: prompt }).last()).toContainText('tokens ·', { timeout: 60_000 })
  const completed = await page.locator('.chat-turn').filter({ hasText: prompt }).last().textContent()
  await page.reload()
  await expect(page.locator('.chat-turn').filter({ hasText: prompt }).last()).toHaveText(completed ?? '')
  await page.getByRole('button', { name: 'Sign out' }).click()
  await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible()
  await expect(page.getByText(prompt)).toHaveCount(0)
})

test('a visitor can quote whole credits but needs to sign in to pay', async ({ page }) => {
  await page.route('**/api/grid/public/checkout', (route) => route.fulfill({ json: { mode: 'test' } }))
  await page.goto('/chat')
  const amount = page.getByLabel('Credits to buy')
  const checkout = page.getByRole('button', { name: 'Sign in to buy credits' })
  await amount.fill('99')
  await expect(checkout).toBeDisabled()
  await amount.fill('253')
  await expect(page.locator('output')).toHaveText('$2.53')
  await checkout.click()
  await expect(page.getByText('Sign in to NeuroGrid', { exact: true })).toBeVisible()
  await page.reload()
  await expect(amount).toHaveValue('253')
})
