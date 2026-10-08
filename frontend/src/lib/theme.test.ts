import { test } from 'node:test'
import assert from 'node:assert/strict'
import { startTheme, themeScript, THEME_KEY } from './theme.ts'

test('a saved choice wins, otherwise the computer setting', () => {
  assert.equal(startTheme('dark', false), 'dark')
  assert.equal(startTheme('light', true), 'light')
  assert.equal(startTheme(null, true), 'dark')
  assert.equal(startTheme('purple', false), 'light')
})

test('the early script reads the key the switch writes', () => {
  assert.ok(themeScript.includes(`localStorage.getItem('${THEME_KEY}')`))
})
