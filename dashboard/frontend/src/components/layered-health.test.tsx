import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { LayeredHealthCard } from './layered-health'
import { fresh, layeredHealthFixture } from '../test/fixtures'

describe('LayeredHealthCard', () => {
  it('shows each layer status and the named lagging layer', () => {
    render(
      <LayeredHealthCard
        data={layeredHealthFixture()}
        freshness={fresh()}
        now={Date.now()}
      />,
    )

    expect(
      within(screen.getByTestId('layer-export')).getByText('OK'),
    ).toBeInTheDocument()
    expect(
      within(screen.getByTestId('layer-performance_consume')).getByText('OK'),
    ).toBeInTheDocument()
    expect(
      within(screen.getByTestId('layer-publish')).getByText('STALE'),
    ).toBeInTheDocument()
    expect(
      within(screen.getByTestId('layer-snapshot')).getByText('UNKNOWN'),
    ).toBeInTheDocument()

    const lagging = screen.getByTestId('layered-lagging')
    expect(lagging).toHaveTextContent('Publish 发布')
    expect(lagging).toHaveTextContent('2400s behind ingest')
  })

  it('renders N/A for missing watermark, age and service values', () => {
    render(
      <LayeredHealthCard
        data={layeredHealthFixture()}
        freshness={fresh()}
        now={Date.now()}
      />,
    )

    const snapshot = screen.getByTestId('layer-snapshot')
    // Missing watermark and age must not look like a real zero.
    expect(within(snapshot).getAllByText('N/A').length).toBeGreaterThanOrEqual(2)
    expect(
      within(screen.getByTestId('service-paper')).getAllByText('N/A').length,
    ).toBeGreaterThanOrEqual(1)
  })

  it('shows Collector / Shadow / Paper service verdicts', () => {
    render(
      <LayeredHealthCard
        data={layeredHealthFixture()}
        freshness={fresh()}
        now={Date.now()}
      />,
    )

    expect(
      within(screen.getByTestId('service-collector')).getByText('OK'),
    ).toBeInTheDocument()
    expect(
      within(screen.getByTestId('service-shadow')).getByText('LAGGING'),
    ).toBeInTheDocument()
    expect(
      within(screen.getByTestId('service-paper')).getByText('UNKNOWN'),
    ).toBeInTheDocument()
  })

  it('renders an N/A state when the endpoint has no data', () => {
    render(<LayeredHealthCard reason="boom" />)
    expect(screen.getByTestId('layered-health-unavailable')).toHaveTextContent(
      'N/A',
    )
    expect(screen.getByTestId('layered-health-unavailable')).toHaveTextContent(
      'boom',
    )
  })
})
