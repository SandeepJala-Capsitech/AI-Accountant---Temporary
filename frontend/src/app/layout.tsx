import type { Metadata } from 'next'
import { IBM_Plex_Mono, Inter, Source_Serif_4 } from 'next/font/google'
import AppHeader from '@/components/AppHeader'
import { themeScript } from '@/lib/theme'
import './globals.css'

const sans = Inter({ subsets: ['latin'], variable: '--font-sans', display: 'swap' })
const serif = Source_Serif_4({ subsets: ['latin'], variable: '--font-serif', display: 'swap' })
const mono = IBM_Plex_Mono({ subsets: ['latin'], weight: ['400', '500'], variable: '--font-mono', display: 'swap' })

export const metadata: Metadata = {
  title: 'LedgerSync',
  description: "Clients' documents read by AI, booked, matched and saved, with their trial balances",
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en-GB" className={`${sans.variable} ${serif.variable} ${mono.variable}`} suppressHydrationWarning>
      <head>
        {/* Applies a saved light/dark choice before the page paints. */}
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body suppressHydrationWarning>
        <AppHeader />
        <main className="page">{children}</main>
      </body>
    </html>
  )
}
