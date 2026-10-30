'use client'

import Link from 'next/link'
import { useParams } from 'next/navigation'
import { useClientYear } from '@/components/ClientYearContext'
import DocumentsSection from '@/components/DocumentsSection'
import TransactionsSection from '@/components/TransactionsSection'
import { isSection, SECTIONS, sectionHref } from '@/lib/years'

// One section of a client's year. Sections not built yet show their title.
export default function SectionPage() {
  const params = useParams<{ section: string }>()
  const { client, year } = useClientYear()

  if (!isSection(params.section)) {
    return (
      <div className="empty card">
        <h2>Section not found</h2>
        <p className="muted">There is no section called “{params.section}”.</p>
        <Link href={sectionHref(client.id, year.start, 'overview')} className="btn">Go to Overview</Link>
      </div>
    )
  }
  if (params.section === 'documents') return <DocumentsSection />
  if (params.section === 'transactions') return <TransactionsSection />
  const section = SECTIONS.find(s => s.slug === params.section)!
  return (
    <section>
      <h2>{section.title}</h2>
    </section>
  )
}
