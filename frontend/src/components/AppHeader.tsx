'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'
import { getHealth, type Health } from '@/lib/api'
import { startTheme, THEME_KEY, type Theme } from '@/lib/theme'

// The bar on every page: the app's name (back to the clients), whether the AI model can be reached, and the
// light/dark switch. The switch's choice is kept in this browser; the layout applies it before painting.
export default function AppHeader() {
  const [health, setHealth] = useState<Health | null>(null)
  const [healthError, setHealthError] = useState('')
  const [theme, setTheme] = useState<Theme>('light')

  useEffect(() => {
    let alive = true
    const check = () => getHealth()
      .then(h => { if (alive) { setHealth(h); setHealthError('') } })
      .catch((e: unknown) => { if (alive) { setHealth(null); setHealthError(e instanceof Error ? e.message : String(e)) } })
    check()
    const timer = setInterval(check, 30_000)
    return () => { alive = false; clearInterval(timer) }
  }, [])

  useEffect(() => {
    let saved: string | null = null
    try { saved = localStorage.getItem(THEME_KEY) } catch { /* storage blocked: follow the computer */ }
    setTheme(startTheme(saved, window.matchMedia('(prefers-color-scheme: dark)').matches))
  }, [])

  const flip = () => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark'
    document.documentElement.dataset.theme = next
    try { localStorage.setItem(THEME_KEY, next) } catch { /* applied, just not remembered */ }
    setTheme(next)
  }

  const online = !!health?.model_available
  const status = health ? (online ? `AI online · ${health.model}` : 'AI offline') : healthError ? 'API offline' : 'Checking…'
  const switchTo = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'
  return (
    <header className="app-header">
      <Link href="/" className="brand"><span className="brand-mark" aria-hidden="true">£</span>LedgerSync</Link>
      <div className="header-end">
        <span className={`ai-status ${online ? 'ai-on' : 'ai-off'}`} title={health?.ai_error || healthError}>{status}</span>
        <button type="button" className="icon-btn" onClick={flip} aria-label={switchTo} title={switchTo}>
          {theme === 'dark' ? '☀' : '☾'}
        </button>
      </div>
    </header>
  )
}
