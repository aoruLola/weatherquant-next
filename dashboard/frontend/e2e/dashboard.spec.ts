import { expect, test } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'

/**
 * Browser acceptance: the five pages must render real content at every
 * required viewport, the fixture banner must be visible (fixture honesty),
 * and screenshots land in dashboard/screenshots/ for review.
 */

const SHOTS = resolve(process.cwd(), '../screenshots')
mkdirSync(SHOTS, { recursive: true })

const PAGES = [
  { name: 'overview', path: '/' },
  { name: 'markets', path: '/markets' },
  { name: 'forecasts', path: '/forecasts' },
  { name: 'decisions', path: '/decisions' },
  { name: 'system', path: '/system' },
] as const

test.beforeEach(async ({ page }) => {
  // Every page waits for its first payload before we judge it.
  await page.goto('/')
  await expect(page.getByTestId('fixture-badge')).toBeVisible()
})

for (const p of PAGES) {
  test(`${p.name} renders and is captured`, async ({ page }, testInfo) => {
    await page.goto(p.path)
    await expect(page.getByTestId('page-header').first()).toBeVisible()

    // Page-specific truth checks.
    if (p.name === 'overview') {
      await expect(page.getByTestId('stat-tokens')).toBeVisible()
      await expect(page.getByTestId('decision-distribution-card')).toBeVisible()
      await expect(page.getByTestId('decision-distribution-card')).toContainText(
        '不代表已验证的盈利机会',
      )
      await expect(page.getByTestId('system-health-card')).toBeVisible()
    }
    if (p.name === 'markets') {
      await expect(page.getByTestId('market-filters')).toBeVisible()
      await expect(page.getByTestId('market-card').first()).toBeVisible()
      await expect(page.getByTestId('market-card').first()).toContainText('0.62')
    }
    if (p.name === 'forecasts') {
      await expect(page.getByTestId('forecast-chart')).toBeVisible()
      await expect(page.getByTestId('model-toggles')).toBeVisible()
      await expect(page.getByTestId('observation-banner')).toContainText('暂未接入')
    }
    if (p.name === 'decisions') {
      await expect(page.getByTestId('accept-disclaimer')).toBeVisible()
      await expect(page.getByTestId('decisions-table')).toBeVisible()
      await expect(page.getByTestId('decision-filters')).toBeVisible()
    }
    if (p.name === 'system') {
      await expect(page.getByText('Collector', { exact: true })).toBeVisible()
      await expect(page.getByText('WS 队列积压')).toBeVisible()
      await expect(page.getByText('暂未接入', { exact: false }).first()).toBeVisible()
    }

    const shot = resolve(SHOTS, `${testInfo.project.name}-${p.name}.png`)
    await page.screenshot({ path: shot, fullPage: true })
    testInfo.annotations.push({ type: 'screenshot', description: shot })
  })
}

test('mobile navigation opens the drawer', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'drawer path is for phone viewports')
  await page.goto('/')
  await page.locator('[aria-label="打开菜单"]').click()
  const drawer = page.getByTestId('mobile-drawer')
  await expect(drawer).toBeVisible()
  await drawer.getByTestId('nav-decisions').click()
  await expect(page).toHaveURL(/\/decisions$/)
  await expect(page.getByTestId('accept-disclaimer')).toBeVisible()
})

test('market detail opens from a card', async ({ page }) => {
  await page.goto('/markets')
  const card = page.getByTestId('market-card').first()
  await expect(card).toBeVisible()
  await card.click()
  await expect(page).toHaveURL(/\/markets\/.+/)
  await expect(page.getByTestId('page-header').first()).toContainText('市场详情')
})

test('the fixture banner is impossible to miss', async ({ page }) => {
  await page.goto('/')
  const banner = page.getByTestId('fixture-banner')
  await expect(banner).toBeVisible()
  await expect(banner).toContainText('FIXTURE')
})

test('deep links resolve directly (SPA fallback)', async ({ page }) => {
  await page.goto('/decisions')
  await expect(page).toHaveURL(/\/decisions$/)
  await expect(page.getByTestId('accept-disclaimer')).toBeVisible()
})
