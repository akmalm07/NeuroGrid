import { test, expect } from '@playwright/test'
import { signIn, testAccounts } from './helpers/auth'

test('node keys are scoped to the account that created them', async ({ browser }) => {
  const [first, second] = testAccounts()
  expect(second).toBeDefined()
  const owner = await browser.newPage()
  const other = await browser.newPage()
  try {
    await Promise.all([owner.goto('/downloads'), other.goto('/downloads')])
    await Promise.all([signIn(owner, first), signIn(other, second)])
    const enrollmentRequest = owner.waitForResponse((response) =>
      new URL(response.url()).pathname === '/api/grid/user/enroll')
    await owner.getByRole('button', { name: 'Create node key' }).click()
    const enrollment = await enrollmentRequest
    expect(enrollment.ok()).toBe(true)
    const { id } = await enrollment.json()
    const nodeId = id.split('-')[0]
    const ownerRow = owner.getByRole('listitem').filter({ hasText: nodeId })
    try {
      await expect(ownerRow).toBeVisible()
      await expect(other.getByRole('listitem').filter({ hasText: nodeId })).toHaveCount(0)
      const tokenResponse = await other.request.post('/api/auth/token', {
        data: {}, headers: { Origin: new URL(other.url()).origin },
      })
      const { token } = await tokenResponse.json()
      const denied = await other.request.post('/api/grid/user/revoke', {
        data: { id }, headers: { Authorization: 'Bearer ' + token },
      })
      expect(denied.status()).toBe(404)
    } finally {
      await ownerRow.getByRole('button', { name: 'Revoke key' }).click()
      await expect(ownerRow).toHaveCount(0)
    }
  } finally {
    await Promise.all([owner.close(), other.close()])
  }
})
