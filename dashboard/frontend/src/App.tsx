import { Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import Decisions from './pages/Decisions'
import Forecasts from './pages/Forecasts'
import MarketDetail from './pages/MarketDetail'
import Markets from './pages/Markets'
import Overview from './pages/Overview'
import Performance from './pages/Performance'
import System from './pages/System'

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Overview />} />
        <Route path="/markets" element={<Markets />} />
        <Route path="/markets/:marketId" element={<MarketDetail />} />
        <Route path="/forecasts" element={<Forecasts />} />
        <Route path="/decisions" element={<Decisions />} />
        <Route path="/performance" element={<Performance />} />
        <Route path="/system" element={<System />} />
        <Route path="*" element={<Overview />} />
      </Route>
    </Routes>
  )
}
