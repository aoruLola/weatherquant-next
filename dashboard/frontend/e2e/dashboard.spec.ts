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

test('Paper page keeps simulation labels and quote-dependent N/A states', async ({ page }) => {
  const envelope = (api: string, data: Record<string, unknown>) => ({
    schema_version: 1,
    api,
    generated_at_utc: new Date().toISOString(),
    dataset: 'fixture',
    freshness: { state: 'fresh', age_seconds: 1 },
    data,
  })
  await page.route('**/api/v1/paper/summary', async (route) => {
    await route.fulfill({
      json: envelope('paper/summary', {
        status: 'ok',
        mode: 'PAPER',
        initial_capital_usdc: 100,
        available_usdc: 60,
        occupied_usdc: 40,
        cost_equity_usdc: 100,
        equity_usdc: null,
        realized_pnl_usdc: 0,
        unrealized_pnl_usdc: null,
        roi: null,
        max_drawdown: 0,
        drawdown_basis: 'ledger_cost',
        equity_curve: {
          basis: 'ledger_cost',
          points: [
            { t: '2026-11-17T01:00:00Z', v: 100, seq: 1 },
            { t: '2026-11-17T02:00:00Z', v: 100, seq: 2 },
          ],
        },
        positions: [
          {
            market_id: 'market-e2e',
            token_id: 'token-e2e',
            shares: 40,
            avg_cost_usd_per_share: 1,
            cost_basis_usdc: 40,
            mark_usd_per_share: null,
            market_value_usdc: null,
            unrealized_pnl_usdc: null,
            mark_status: 'MARK_UNAVAILABLE',
          },
        ],
        marks_status: 'unavailable',
        marks_reason: 'no fresh quote',
        rejection_summary: [{ reason: 'NOT_EXECUTABLE', count: 1 }],
        rejections: [],
        ledger_consistent: true,
        conservation_ok: true,
        fill_count: 1,
      }),
    })
  })
  await page.route(/\/api\/v1\/paper\/fills/, async (route) => {
    await route.fulfill({
      json: envelope('paper/fills', {
        status: 'ok',
        mode: 'PAPER',
        fills: [
          {
            decision_id: 'e2e-fill',
            market_id: 'market-e2e',
            token_id: 'token-e2e',
            direction: 'buy_yes',
            side: 'BUY',
            requested_shares: 10,
            filled_shares: 10,
            avg_price_usd_per_share: 0.55,
            fill_cost_usd: 5.5,
            fees_usd: 0.01,
            complete: true,
            filled_at_utc: '2026-11-17T02:00:00Z',
          },
        ],
        limit: 20,
        next_cursor: null,
        has_more: false,
      }),
    })
  })

  await page.goto('/paper')
  await expect(page.getByTestId('paper-mode-badge')).toContainText('模拟 / PAPER')
  await expect(page.getByTestId('paper-disclaimer')).toContainText('不代表真实交易或真实收益')
  await expect(page.getByTestId('paper-initial')).toContainText('100.00 USDC')
  await expect(page.getByTestId('paper-equity')).toContainText('N/A')
  await expect(page.getByTestId('paper-position-mark')).toContainText('N/A')
  await expect(page.getByTestId('paper-position-pnl')).toContainText('N/A')
  await expect(page.getByTestId('paper-fill-e2e-fill')).toBeVisible()
})

test('deep links resolve directly (SPA fallback)', async ({ page }) => {
  await page.goto('/decisions')
  await expect(page).toHaveURL(/\/decisions$/)
  await expect(page.getByTestId('accept-disclaimer')).toBeVisible()
})
