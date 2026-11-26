import { expect, test } from '@playwright/test'
import type { Account } from '../src/neurogrid/api'
import type { Block, JobSummary, NetworkSnapshot, QuoteSummary } from '../src/neurogrid/protocol'
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
  const older = page.getByRole('button', { name: 'Show older blocks' })
  while (await older.isVisible()) await older.click()
  const genesis = page.locator('details').filter({ hasText: 'Genesis' })
  await genesis.getByText('Genesis', { exact: true }).click()
  await expect(genesis.getByText('Previous hash', { exact: true })).toBeVisible()
})

test('a signed-in browser runs a live prompt and preserves the reply after reload', async ({ page }) => {
  test.skip(process.env.NEUROGRID_TEST_INFERENCE !== '1', 'Start four Granite workers and the verifier first')
  const [account] = testAccounts()
  test.setTimeout(27 * 60_000)
  await page.goto('/chat')
  await signIn(page, account)
  await expect(page.getByLabel('Maximum tokens')).toHaveCount(0)
  const network = await page.request.get('/api/grid/public/network')
  const { height } = await network.json() as NetworkSnapshot
  const prompt = 'What is the capital of France? Reply with the city only. Reference ' + Date.now()
  await page.getByRole('textbox', { name: 'Message' }).fill(prompt)
  const quoted = page.waitForResponse(async (response) => response.url().endsWith('/api/grid/user/quote')
    && response.ok() && (await response.json() as QuoteSummary).status === 'ready')
  await page.getByRole('button', { name: 'Get quote' }).click()
  const quote = await (await quoted).json() as QuoteSummary
  const maximum = quote.credits!
  expect(maximum).toBeGreaterThan(0)
  await expect(page.getByRole('status').filter({ hasText: `Up to ${maximum} credits` })).toBeVisible()
  const proposed = page.waitForRequest((request) => request.url().endsWith('/api/grid/user/request'))
  await page.getByRole('button', { name: 'Send prompt' }).click()
  const submitted = await proposed
  const initial = await submitted.response()
  expect(initial?.ok()).toBe(true)
  const { id } = await initial!.json() as JobSummary
  const turn = page.locator('.chat-turn').filter({ hasText: prompt }).last()
  await expect(turn).toContainText('Paris', { timeout: 25 * 60_000 })
  const session = await page.request.post('/api/auth/token', { data: {}, headers: { Origin: new URL(page.url()).origin } })
  expect(session.ok()).toBe(true)
  const { token } = await session.json() as { token: string }
  const headers = { Authorization: `Bearer ${token}` }
  const accountView = await page.request.get('/api/grid/user/account', { headers })
  expect(accountView.ok()).toBe(true)
  const settled = (await accountView.json() as Account).jobs.find((job) => job.id === id)!
  expect(settled.status).toBe('settled')
  expect(settled.charge).toBeGreaterThan(0)
  expect(settled.charge).toBeLessThan(maximum)
  expect(settled.charge + settled.returned!).toBeCloseTo(maximum, 3)
  await expect(turn).toContainText(`${settled.charge} credits`)
  const replay = await page.request.post('/api/grid/user/request', { data: submitted.postDataJSON(), headers })
  expect(replay.ok()).toBe(true)
  expect(await replay.json()).toMatchObject({ id, status: 'settled', charge: settled.charge })
  const ledger = await page.request.get(`/api/grid/public/ledger?after=${height}`)
  const blocks = (await ledger.json() as { blocks: Block[] }).blocks.filter((block) => block.reference === id)
  expect(blocks.map((block) => block.kind)).toEqual(['reserve', 'settle'])
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
