// The light/dark choice (design of 2026-10-06): kept in this browser, otherwise the computer's setting.
export type Theme = 'light' | 'dark'
export const THEME_KEY = 'ledgersync-theme'

// Runs in the page's head before it paints, so a saved choice never flashes the other theme first.
export const themeScript =
  `(function(){try{var t=localStorage.getItem('${THEME_KEY}');` +
  `if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}})()`

export function startTheme(saved: string | null, prefersDark: boolean): Theme {
  return saved === 'light' || saved === 'dark' ? saved : prefersDark ? 'dark' : 'light'
}
