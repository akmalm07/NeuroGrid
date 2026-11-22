import { readFileSync } from 'node:fs'
import { expect, test, type Page } from '@playwright/test'

export function testAccounts(): { email: string; password: string }[] {
  const path = process.env.NEUROGRID_TEST_ACCOUNTS
  test.skip(!path, 'Set NEUROGRID_TEST_ACCOUNTS to a private JSON file with DeepSpace test accounts')
  return JSON.parse(readFileSync(path!, 'utf8').replace(/^\uFEFF/, ''))
}

export async function signIn(page: Page, account: { email: string; password: string }) {
  const response = await page.request.post('/api/auth/sign-in/email', {
    data: account,
    headers: { Origin: new URL(page.url()).origin },
  })
  expect(response.status()).toBe(200)
  await page.reload()
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible()
}
