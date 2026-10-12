import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { envelopeOf, mockFetch, rejectFetch } from '../test/mock'
import Layout from './Layout'

afterEach(() => vi.unstubAllGlobals())

function renderLayout() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Layout />
    </MemoryRouter>,
  )
}

describe('Layout', () => {
  it('renders all five destinations', async () => {
    mockFetch({
      '/healthz': envelopeOf({ status: 'ok', uptime_seconds: 12, sources: {} }),
    })
    renderLayout()
    expect(screen.getByTestId('nav-overview')).toBeInTheDocument()
    expect(screen.getByTestId('nav-markets')).toBeInTheDocument()
    expect(screen.getByTestId('nav-forecasts')).toBeInTheDocument()
    expect(screen.getByTestId('nav-decisions')).toBeInTheDocument()
    expect(screen.getByTestId('nav-system')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('api-status')).toBeInTheDocument())
  })

  it('shows the mobile drawer on demand (375px navigation path)', async () => {
    mockFetch({
      '/healthz': envelopeOf({ status: 'ok', uptime_seconds: 1, sources: {} }),
    })
    renderLayout()
    expect(screen.queryByTestId('mobile-drawer')).not.toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('打开菜单'))
    expect(screen.getByTestId('mobile-drawer')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('关闭菜单'))
    expect(screen.queryByTestId('mobile-drawer')).not.toBeInTheDocument()
  })

  it('banners fixture data loudly', async () => {
    const fixtureEnvelope = envelopeOf({ status: 'ok', uptime_seconds: 1, sources: {} })
    fixtureEnvelope.dataset = 'fixture'
    mockFetch({ '/healthz': fixtureEnvelope })
    renderLayout()
    await waitFor(() => expect(screen.getByTestId('fixture-badge')).toBeInTheDocument())
    expect(screen.getByTestId('fixture-banner')).toBeInTheDocument()
  })

  it('shows an offline banner when healthz fails', async () => {
    rejectFetch()
    renderLayout()
    await waitFor(() => expect(screen.getByTestId('offline-banner')).toBeInTheDocument())
    expect(screen.getByTestId('offline-banner')).toHaveTextContent('无法连接 Dashboard API')
  })
})
