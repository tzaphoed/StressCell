// HTML -> PDF für Handbuch und Geräteauswahl. Braucht Node und Playwright mit Chromium.
//   node docs/render_pdf.js
const { chromium } = require('playwright')
const path = require('path')

const jobs = [
  ['handbuch/handbuch.html', 'StressCell_Handbuch.pdf', 'StressCell Handbuch'],
  ['geraeteauswahl/geraeteauswahl.html', 'FC100_Geraeteauswahl.pdf', 'FC100 Geräteauswahl'],
]

;(async () => {
  const b = await chromium.launch()
  for (const [src, out, title] of jobs) {
    const p = await b.newPage()
    await p.goto('file://' + path.join(__dirname, src), { waitUntil: 'networkidle' })
    await p.emulateMedia({ media: 'print' })
    await p.pdf({
      path: path.join(__dirname, out), format: 'A4', printBackground: true, preferCSSPageSize: true,
      displayHeaderFooter: true, headerTemplate: '<span></span>',
      footerTemplate: `<div style="font-size:7pt;color:#888;width:100%;text-align:center;font-family:Inter,sans-serif">${title} · Seite <span class="pageNumber"></span> von <span class="totalPages"></span></div>`,
    })
    console.log('wrote', out)
    await p.close()
  }
  await b.close()
})()
