import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { forecastsFixture } from '../test/fixtures'
import { envelopeOf, mockFetch, rejectFetch } from '../test/mock'
import Forecasts from './Forecasts'

afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  return render(
    <MemoryRouter>
      <Forecasts />
    </MemoryRouter>,
  )
}

describe('Forecasts', () => {
  it('lists every model with prediction counts and marks the primary', async () => {
    mockFetch({ '/api/v1/forecasts': envelopeOf(forecastsFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('model-card-calibrated')).toBeInTheDocument())
    expect(screen.getByTestId('model-card-calibrated')).toHaveTextContent('PRIMARY')
    expect(screen.getByTestId('model-card-gfs_raw')).toHaveTextContent('120')
    expect(screen.getByTestId('model-card-ecmwf_raw')).toBeInTheDocument()
    expect(screen.getByTestId('model-card-fusion')).toBeInTheDocument()
  })

  it('renders the probability chart with the primary model selected by default', async () => {
    mockFetch({ '/api/v1/forecasts': envelopeOf(forecastsFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('forecast-chart')).toBeInTheDocument())
    const toggle = screen.getByTestId('toggle-calibrated')
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('forecast-chart')).toBeInTheDocument()
  })

  it('toggles a second model into the comparison', async () => {
    mockFetch({ '/api/v1/forecasts': envelopeOf(forecastsFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('toggle-gfs_raw')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('toggle-gfs_raw'))
    expect(screen.getByTestId('toggle-gfs_raw')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByTestId('toggle-calibrated')).toHaveAttribute('aria-pressed', 'true')
    // legend now shows both series
    expect(screen.getAllByText('gfs_raw').length).toBeGreaterThan(0)
  })

  it('refuses to invent observations when settlement data is absent', async () => {
    mockFetch({ '/api/v1/forecasts': envelopeOf(forecastsFixture()) })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('observation-banner')).toBeInTheDocument())
    expect(screen.getByTestId('observation-banner')).toHaveTextContent('暂未接入')
  })

  it('shows unavailable instead of fake distributions when the producer is absent', async () => {
    mockFetch({
      '/api/v1/forecasts': envelopeOf(
        forecastsFixture({
          distributions: [],
          distributions_status: 'unavailable',
          distributions_reason: 'forecast-snapshot not published yet',
        }),
      ),
    })
    renderPage()
    await waitFor(() => expect(screen.getByTestId('forecast-empty')).toBeInTheDocument())
    expect(screen.getByTestId('forecast-empty')).toHaveTextContent('forecast-snapshot not published')
  })

  it('renders an error state when the API is unreachable', async () => {
    rejectFetch()
    renderPage()
    await waitFor(() => expect(screen.getByTestId('forecasts-error')).toBeInTheDocument())
  })
})
