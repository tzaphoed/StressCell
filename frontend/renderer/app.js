import { registerBlocks, toolbox, toSequence, fromSequence, looseBlocks } from './blocks.js'

const $ = s => document.querySelector(s)
const $$ = s => [...document.querySelectorAll(s)]
const TOKEN = document.querySelector('meta[name=stresscell-token]').content
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))

async function api(method, path, body) {
  const r = await fetch(path, {
    method, headers: { 'Content-Type': 'application/json', 'X-StressCell-Token': TOKEN },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`)
  return j
}
const get = p => api('GET', p)
const post = (p, b = {}) => api('POST', p, b)

function toast(msg, err = false) {
  const t = document.createElement('div')
  t.className = 'toast' + (err ? ' err' : '')
  t.textContent = msg
  $('#toasts').append(t)
  setTimeout(() => t.remove(), err ? 9000 : 4000)
}
const guarded = fn => async (...a) => { try { return await fn(...a) } catch (e) { toast(e.message, true) } }

const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim()
const layout = (y, x = 'Zeit [min]') => ({
  datarevision: Date.now(), uirevision: y, margin: { l: 52, r: 12, t: 6, b: 38 }, paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: css('--bg-secondary'),
  font: { color: css('--text-primary'), size: 11 }, showlegend: true, legend: { orientation: 'h', y: 1.12 },
  xaxis: { title: x, gridcolor: css('--border'), zerolinecolor: css('--border-light') },
  yaxis: { title: y, gridcolor: css('--border'), zerolinecolor: css('--border-light') },
})
const CONFIG = { responsive: true, displaylogo: false }

// ── Live-Anzeige ─────────────────────────────────────────────────────────
// [Schlüssel, Name, Einheit, Nachkommastellen, Faktor, Erklärung]
const READOUTS = [
  ['T', 'Temperatur', 'K', 3, 1, 'PPMS-Thermometer. Die Zelle hinkt beim Abkühlen hinterher, darum rechnet das Fenster mit dem langsameren Wert.'],
  ['B', 'Feld', 'T', 3, 1, 'Magnetfeld laut MultiVu'],
  ['F', 'Kraft', 'N', 3, 1, 'Aus der Kapazität über die Kalibrierformel. Positiv ist Zug. Erst nach Tare im Vakuum absolut richtig.'],
  ['stress_MPa', 'Spannung', 'MPa', 1, 1, 'F geteilt durch den Probenquerschnitt aus config.toml [sample]'],
  ['C_pF', 'Kapazität', 'pF', 6, 1, 'Rohwert des Kraftsensors. Fällt unter Zug (#0260: rund 0,97 fF/N bei 290 K).'],
  ['V_T', 'Zugstapel', 'V', 2, 1, 'Kommandowert der Quelle am Zugstapel (rot +, weiß −), keine Messung am Stapel'],
  ['V_C', 'Druckstapel', 'V', 2, 1, 'Kommandowert der Quelle am Druckstapel (braun +, blau −)'],
  ['I_T', 'I Zug', 'mA', 3, 1e3, 'Strom in den Zugstapel, ohne Bleederstrom. Über current_trip_A: Lichtbogen angenommen.'],
  ['I_C', 'I Druck', 'mA', 3, 1e3, 'Strom in den Druckstapel, ohne Bleederstrom'],
  ['T_ac_K', 'ΔT AC', 'mK', 2, 1e3, 'Temperaturamplitude der Probe aus der Lock-in-Spannung, Vorverstärker und Type-E-Seebeck'],
  ['theta_stack', 'Phase', '°', 1, 1, 'Phase des Thermoelement-Signals gegen die Spannung AM STAPEL (RC-Tiefpass herausgerechnet). Im Plateau nahe 0° oder 180°.'],
  ['f_ac', 'AC-Frequenz', 'Hz', 1, 1, 'Anregungsfrequenz'],
  ['V_ac', 'AC Spitze', 'V', 2, 1, 'Spitzenspannung am Stapel'],
  ['dT_dsigma_K_MPa', 'dT/dσ', 'mK/MPa', 3, 1e3, 'Elastokalorischer Koeffizient: ΔT_ac / (F_ac/A). F_ac = dF/dD · V_ac, dF/dD aus „dF/dD messen“ bei dieser Temperatur.'],
  ['dT_dc_K', 'ΔT DC', 'mK', 2, 1e3, 'Thermospannung vom Nanovoltmeter, für Laden und Entladen'],
]
$('#readouts').innerHTML = READOUTS.map(([k, n, u, , , h]) =>
  `<div class="ro" id="ro-${k}" title="${esc(h)}"><div class="k">${n}</div><div><span class="v">–</span><span class="u">${u}</span></div></div>`).join('') +
  `<div class="ro" id="ro-chamber" title="Volles Spannungsfenster nur in Hochvakuum"><div class="k">Kammer</div><div class="small" id="chamber-text">–</div></div>
   <div class="ro" id="ro-window" title="Erlaubte Spannung je Stapel bei der aktuellen (effektiven) Temperatur und Kammer"><div class="k">Fenster</div><div class="small" id="window-text">–</div></div>
   <div class="ro" id="ro-loop" title="Kraftregler: Ziel und geschätzte Steigung dF/dD"><div class="k">Kraftregler</div><div class="small" id="loop-text">–</div></div>`

const hist = { t: [], T: [], F: [], V_T: [], V_C: [], vmin: [], vmax: [] }
let last = null, t0 = null, lastDraw = 0

function push(d) {
  if (t0 === null) t0 = d.t
  hist.t.push((d.t - t0) / 60)
  for (const k of ['T', 'F', 'V_T', 'V_C', 'vmin', 'vmax']) hist[k].push(d[k] ?? null)
  if (hist.t.length > 5000) for (const k in hist) hist[k].splice(0, 500)
}

function showMonitor(d) {
  last = d
  for (const [k, , , dec, mul] of READOUTS) {
    const v = d[k]
    document.querySelector(`#ro-${k} .v`).textContent = v == null ? '–' : (v * (mul || 1)).toFixed(dec)
  }
  $('#chamber-text').textContent = d.chamber ?? '–'
  $('#ro-chamber').classList.toggle('bad', d.chamber_ok === false)
  $('#window-text').textContent = d.vmin == null ? '–' : `${d.vmin.toFixed(1)} … ${d.vmax.toFixed(1)} V`
  const f = d.force
  $('#loop-text').textContent = f?.enabled ? `an, ${f.target.toFixed(2)} N, G ${f.G.toFixed(2)} N/V` : 'aus'
  pushEce(d)
  draw()
}

function draw() {
  const now = performance.now()
  if (now - lastDraw < 1000 || !$('#page-cell').classList.contains('active') && !$('#page-limits').classList.contains('active')) return
  lastDraw = now
  const t = hist.t
  Plotly.react('plot-force', [{ x: t, y: hist.F, name: 'F', line: { color: css('--highlight') } }], layout('F [N]'), CONFIG)
  Plotly.react('plot-volt', [
    { x: t, y: hist.V_T, name: 'V_T', line: { color: css('--accent-text') } },
    { x: t, y: hist.V_C, name: 'V_C', line: { color: css('--success') } },
    { x: t, y: hist.vmax, name: 'Fenster', line: { color: css('--danger'), dash: 'dot', width: 1 } },
    { x: t, y: hist.vmin, showlegend: false, line: { color: css('--danger'), dash: 'dot', width: 1 } },
  ], layout('V'), CONFIG)
  Plotly.react('plot-temp', [{ x: t, y: hist.T, name: 'T', line: { color: css('--warning') } }], layout('T [K]'), CONFIG)
  if (curve && last) {
    Plotly.react('plot-window', [
      { x: curve.T, y: curve.vmax, name: 'Vmax', line: { color: css('--danger') } },
      { x: curve.T, y: curve.vmin, name: 'Vmin', line: { color: css('--danger') } },
      { x: [last.T], y: [last.V_T], name: 'V_T', mode: 'markers', marker: { size: 11, color: css('--accent-text') } },
      { x: [last.T], y: [last.V_C], name: 'V_C', mode: 'markers', marker: { size: 11, color: css('--success') } },
    ], layout('V', 'T [K]'), CONFIG)
  }
}

function showAlarms(list) {
  const bar = $('#alarm-bar')
  bar.classList.toggle('hidden', !list.length)
  bar.replaceChildren(...list.map(a => {
    const row = document.createElement('div'); row.className = 'alarm'
    const s = document.createElement('span'); s.textContent = a.text
    const b = document.createElement('button'); b.textContent = 'Quittieren'
    b.title = 'Erst die Ursache prüfen. Danach sind wieder Schritte erlaubt.'
    b.onclick = guarded(() => post('/alarms/ack', { key: a.key }))
    row.append(s, b)
    return row
  }))
}

function showRun(r) {
  const p = $('#run-pill')
  p.className = 'pill ' + r.state
  p.textContent = r.state
  $('#run-text').textContent = [r.name, r.message, r.state === 'idle' ? r.result : `${r.points} Punkte`].filter(Boolean).join(' · ')
}

const points = {}
function addPoint(d) {
  for (const k in d) (points[k] ??= []).push(d[k])
  const keys = Object.keys(points).filter(k => k !== 'label')
  fill($('#pt-x'), keys, 'F'); fill($('#pt-y'), keys, 'C_pF')
  drawPoints()
}
function fill(sel, keys, dflt) {
  if (sel.options.length === keys.length) return
  const keep = sel.value || dflt
  sel.replaceChildren(...keys.map(k => new Option(k, k)))
  sel.value = keys.includes(keep) ? keep : keys[0]
}
function drawPoints() {
  if (!$('#page-run').classList.contains('active')) return
  const x = $('#pt-x').value, y = $('#pt-y').value
  if (x && y) Plotly.react('plot-points', [{ x: points[x], y: points[y], mode: 'markers+lines', line: { color: css('--highlight') } }],
    { ...layout(y, x), showlegend: false }, CONFIG)
}
$('#pt-x').onchange = $('#pt-y').onchange = drawPoints

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`)
  ws.onopen = () => $('#conn').classList.add('ok')
  ws.onclose = () => { $('#conn').classList.remove('ok'); setTimeout(connect, 1500) }
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data)
    if (m.type === 'hello') {
      m.history.forEach(push)
      if (m.last?.t !== undefined) showMonitor(m.last)
      showAlarms(m.alarms); showRun(m.run)
    } else if (m.type === 'monitor') { push(m.data); showMonitor(m.data) }
    else if (m.type === 'alarms') showAlarms(m.active)
    else if (m.type === 'run') { showRun(m.data); if (m.data.state === 'running') for (const k in points) delete points[k] }
    else if (m.type === 'point') addPoint(m.data)
    else if (m.type === 'log') { const l = $('#run-log'); l.textContent += m.line + '\n'; l.scrollTop = l.scrollHeight }
    else if (m.type === 'reload') { toast('Backend mit neuen Einstellungen neu gestartet, Seite lädt neu'); setTimeout(() => location.reload(), 1500) }
  }
}

// ── Navigation ───────────────────────────────────────────────────────────
function show(page) {
  $$('.nav').forEach(x => x.classList.toggle('active', x.dataset.page === page))
  $$('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + page))
  lastDraw = 0
  if (page === 'editor') Blockly.svgResize(ws)
  if (page === 'run') { reloadSeq(); drawPoints() }
  if (page === 'data') loadRuns()
  if (page === 'setup') loadChecklist()
  if (page === 'limits') loadCalibration()
  if (page === 'calc') runCalc()
  if (page === 'settings') loadSettings()
  if (page === 'ece') { lastEce = 0; drawEce() }
  window.dispatchEvent(new Event('resize'))
  draw()
}
function openHelp(chapter = '') {
  const f = $('#help-frame')
  const target = '/help' + (chapter ? '#' + chapter : '')
  if (!f.src.endsWith(target)) f.src = target
  show('help')
}
$$('.nav').forEach(b => b.onclick = () => b.dataset.page === 'help' ? openHelp() : show(b.dataset.page))
$('#help-btn').onclick = () => openHelp()
document.addEventListener('keydown', e => { if (e.key === 'F1') { e.preventDefault(); openHelp() } })

// ── Bedienung ────────────────────────────────────────────────────────────
$('#stop-all').onclick = guarded(async () => { await post('/stop_all'); toast('STOP ALL: alles auf 0 V, bitte quittieren') })
const step = guarded(async st => { await post('/step', st); toast(`${st.op}: erledigt`) })
$$('[data-step]').forEach(b => b.onclick = () => step(JSON.parse(b.dataset.step)))
$$('form[data-op]').forEach(f => f.onsubmit = e => {
  e.preventDefault()
  const st = { op: f.dataset.op }
  for (const i of f.querySelectorAll('input')) if (i.value !== '') st[i.name] = Number(i.value)
  for (const s of f.querySelectorAll('select')) st[s.name] = s.value
  step(st)
})

// Elastokalorik live
const ECE_RO = [
  ['T_ac_K', 'ΔT', 'mK', 3, 1e3], ['theta_stack', 'Phase am Stapel', '°', 1, 1], ['f_ac', 'f', 'Hz', 1, 1],
  ['V_ac', 'V Spitze', 'V', 2, 1], ['F', 'Offset F', 'N', 2, 1], ['stress_MPa', 'σ', 'MPa', 1, 1], ['D_set', 'Offset D', 'V', 1, 1],
  ['G_used', 'dF/dD', 'N/V', 3, 1], ['F_ac_N', 'F_ac', 'N', 3, 1], ['dT_dsigma_K_MPa', 'dT/dσ', 'mK/MPa', 3, 1e3], ['T', 'T', 'K', 2, 1],
]
$('#ece-readouts').innerHTML = ECE_RO.map(([k, n, u]) =>
  `<div class="ro" id="ece-${k}"><div class="k">${n}</div><div><span class="v">–</span><span class="u">${u}</span></div></div>`).join('')
const ece = { t: [], dT: [], th: [], F: [], f: [] }
const fbin = new Map()            // f -> [Summe ΔT/V, Anzahl]
let lastEce = 0
function pushEce(d) {
  for (const [k, , , dec, mul] of ECE_RO) {
    const v = d[k]
    document.querySelector(`#ece-${k} .v`).textContent = v == null ? '–' : (v * mul).toFixed(dec)
  }
  if (d.T_ac_K == null || !d.V_ac) return
  ece.t.push((d.t - (t0 ?? d.t)) / 60); ece.dT.push(d.T_ac_K * 1e3); ece.th.push(d.theta_stack); ece.F.push(d.F); ece.f.push(d.f_ac)
  if (ece.t.length > 4000) for (const k in ece) ece[k].splice(0, 400)
  const b = fbin.get(d.f_ac) || [0, 0]; b[0] += d.T_ac_K * 1e3 / d.V_ac; b[1] += 1; fbin.set(d.f_ac, b)
  drawEce()
}
function drawEce() {
  const now = performance.now()
  if (!$('#page-ece').classList.contains('active') || now - lastEce < 1000) return
  lastEce = now
  Plotly.react('plot-ece-t', [
    { x: ece.t, y: ece.dT, name: 'ΔT [mK]', line: { color: css('--highlight') } },
    { x: ece.t, y: ece.th, name: 'Phase [°]', yaxis: 'y2', line: { color: css('--accent-text'), width: 1 } },
  ], { ...layout('ΔT [mK]'), yaxis2: { title: 'Phase [°]', overlaying: 'y', side: 'right', range: [-180, 180], gridcolor: 'rgba(0,0,0,0)' } }, CONFIG)
  Plotly.react('plot-ece-f', [{ x: ece.F, y: ece.dT, mode: 'markers', marker: { size: 5, color: ece.t, colorscale: 'Viridis' } }],
    { ...layout('ΔT [mK]', 'Offset F [N]'), showlegend: false }, CONFIG)
  const fs = [...fbin.keys()].sort((a, b) => a - b)
  Plotly.react('plot-ece-fr', [{ x: fs, y: fs.map(f => fbin.get(f)[0] / fbin.get(f)[1]), mode: 'markers+lines', line: { color: css('--success') } }],
    { ...layout('ΔT/V [mK/V]', 'f [Hz]'), xaxis: { ...layout('', 'f [Hz]').xaxis, type: 'log' }, showlegend: false }, CONFIG)
}
$('#ece-clear').onclick = () => { for (const k in ece) ece[k].length = 0; fbin.clear(); lastEce = 0; drawEce() }

// Einstellungen
const SETTING_LABEL = { simulate: 'Simulation', sim_speed: 'Zeitraffer der Simulation', read_only: 'Nur lesen',
  ppms_read: 'PPMS-Daten lesen', ppms_control: 'PPMS steuern (T, B, Kammer)', ppms_emergency_hold: 'Notfall-Halt beim Aufwärmen' }
const loadSettings = guarded(async () => {
  const s = await get('/settings')
  $('#settings-form').innerHTML = Object.entries(s.values).map(([k, v]) => typeof v === 'boolean'
    ? `<label class="switch" title="${esc(s.help[k])}"><input type="checkbox" name="${k}" ${v ? 'checked' : ''}> ${SETTING_LABEL[k] ?? k}<span class="muted small">${esc(s.help[k])}</span></label>`
    : `<label title="${esc(s.help[k])}">${SETTING_LABEL[k] ?? k}<input name="${k}" value="${v}"></label>`).join('')
  $('#settings-msg').textContent = s.file
})
$('#settings-save').onclick = guarded(async () => {
  const values = {}
  for (const i of $('#settings-form').querySelectorAll('input')) values[i.name] = i.type === 'checkbox' ? i.checked : Number(i.value)
  if (!values.simulate && !confirm('Echte Geräte: wirklich umschalten? Erst nach Ersatzkondensator und check_instruments.py.')) return
  $('#settings-msg').textContent = 'starte neu …'
  await post('/settings', { values })
})

// Inbetriebnahme
const STATE_TEXT = { ok: 'erledigt', open: 'offen', warn: 'prüfen', info: 'Info' }
const CHAPTER = { 'Software starten': 'software', 'Erster Kontakt': 'erster-kontakt', 'Anschlussbox': 'anschlussbox',
  'Kalibrierung': 'kalibrierung', 'AC-Kette': 'ac-kette', 'Vakuum': 'vakuum', 'Probe': 'probe', 'Grenzwerte': 'grenzwerte',
  'PPMS': 'ppms' }
const loadChecklist = guarded(async () => {
  const { items } = await get('/setup/checklist')
  $('#checklist').replaceChildren(...items.map(i => {
    const li = document.createElement('li'); li.className = 'ck ' + i.state
    li.innerHTML = `<span class="badge">${STATE_TEXT[i.state]}</span><div><b>${esc(i.title)}</b><div class="small">${esc(i.detail)}</div>` +
      (i.why ? `<div class="small muted">${esc(i.why)}</div>` : '') + '</div>'
    if (i.chapter) { li.tabIndex = 0; li.title = 'Kapitel im Handbuch öffnen'; li.onclick = () => openHelp(CHAPTER[i.chapter] || '') }
    return li
  }))
})
$('#checklist-reload').onclick = loadChecklist

// Rechner
const NAMES = {
  I_stack_mA: ['Strom in den Stapel', 'mA', 3], V_peak_max_V: ['größte Spitze bei der Stromreserve', 'V', 3],
  out_rms_V: ['nötige Ausgangsspannung', 'V rms', 4], corner_Hz: ['RC-Ecke', 'Hz', 1], phase_RC_deg: ['RC-Phase bei f', '°', 1],
  stress_MPa: ['mechanische Spannung', 'MPa', 1], strain_est: ['Dehnung (Obergrenze)', '', 6], seebeck_uV_K: ['Seebeck Type E bei T', 'µV/K', 2],
  dT_mK: ['Temperatur aus Thermospannung', 'mK', 3], drive_max_V: ['größter Drive bei T', 'V', 0],
}
const runCalc = guarded(async () => {
  const q = new URLSearchParams(new FormData($('#calc-form')))
  const r = await get('/calc?' + q)
  $('#calc-out').innerHTML = Object.entries(NAMES).map(([k, [n, u, d]]) =>
    `<tr><td>${n}</td><td>${r[k] == null ? '–' : Number(r[k]).toFixed(d)} ${u}</td></tr>`).join('') +
    `<tr><td>Spannungsfenster je Stapel</td><td>${r.window_V[0].toFixed(1)} … ${r.window_V[1].toFixed(1)} V</td></tr>`
})
$('#calc-form').onsubmit = e => { e.preventDefault(); runCalc() }

// Kalibrierung und Geräte
const rows = o => Object.entries(o).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(typeof v === 'number' ? +v.toPrecision(7) : typeof v === 'object' ? JSON.stringify(v) : v)}</td></tr>`).join('')
const loadCalibration = guarded(async () => {
  const c = await get('/calibration')
  $('#cal-table').innerHTML = rows(c.info)
  $('#cal-formula').textContent = c.info.model === 'razorbill'
    ? 'F = κ(T) · (α / (C − Cp) − f₀(T)),  κ = 1/(0,91 + 5·10⁻⁵ T + 9·10⁻⁷ T²),  f₀(T) = f₀,ref + Δf₀(T) − Δf₀(T_ref)'
    : 'Lineares Modell: F = sign · (C − C_zero − dC(T)) / (S · α(T))'
  $('#cal-T').innerHTML = c.table ? '<tr><td>T [K]</td><td>κ</td><td>f₀ [N]</td><td>C₀ [pF]</td><td>S [fF/N]</td></tr>' +
    c.table.map(r => `<tr><td>${r.T}</td><td>${r.kappa.toFixed(4)}</td><td>${r.f0_N.toFixed(2)}</td><td>${r.C0_pF.toFixed(5)}</td><td>${r.S_fF_N.toFixed(3)}</td></tr>`).join('') : ''
})

// Sequenzen
const reloadSeq = guarded(async () => {
  const { files } = await get('/sequences')
  const sel = $('#seq-list'), keep = sel.value
  sel.replaceChildren(...files.map(f => new Option(f, f)))
  if (files.includes(keep)) sel.value = keep
})
$('#seq-reload').onclick = reloadSeq
const selected = () => get('/sequences/' + encodeURIComponent($('#seq-list').value))
function report(el, r) {
  const h = r.duration_s / 3600
  el.textContent = (r.ok ? 'OK' : 'FEHLER: ' + r.error) + `\ngeschätzte Dauer ${h.toFixed(2)} h, ${r.points ?? 0} Punkte`
}
$('#seq-check').onclick = guarded(async () => report($('#seq-report'), await post('/sequence/check', await selected())))
$('#seq-start').onclick = guarded(async () => { $('#run-log').textContent = ''; await post('/run/start', await selected()) })
$('#seq-pause').onclick = guarded(() => post('/run/pause'))
$('#seq-resume').onclick = guarded(() => post('/run/resume'))
$('#seq-stop').onclick = guarded(() => post('/run/stop'))
$('#session-close').onclick = guarded(() => post('/session/close'))

// Editor
registerBlocks()
const ws = Blockly.inject('blockly', {
  toolbox, renderer: 'zelos', trashcan: true, sounds: false, media: '/static/vendor/media/', zoom: { controls: true, wheel: true, startScale: 0.9 },
  theme: Blockly.Theme.defineTheme('sc', { base: Blockly.Themes.Classic, componentStyles: {
    workspaceBackgroundColour: css('--bg-secondary'), toolboxBackgroundColour: css('--bg-card'), toolboxForegroundColour: css('--text-primary'),
    flyoutBackgroundColour: css('--bg-card'), flyoutForegroundColour: css('--text-primary'), scrollbarColour: css('--border-light') } }),
})
window.__ws = ws
const edSeq = () => {
  if (looseBlocks(ws)) toast(`${looseBlocks(ws)} lose Blockgruppe(n) ignoriert: nur der oberste Stapel läuft`, true)
  return toSequence(ws, $('#ed-name').value)
}
$('#ed-save').onclick = guarded(async () => {
  const n = $('#ed-name').value
  await post('/sequences/' + encodeURIComponent(n), edSeq())
  $('#ed-msg').textContent = `gespeichert als ${n}.seq.json`
  reloadSeq()
})
$('#ed-check').onclick = guarded(async () => report($('#ed-msg'), await post('/sequence/check', edSeq())))
$('#ed-load').onclick = guarded(async () => {
  const s = await selected()
  $('#ed-name').value = $('#seq-list').value.replace('.seq.json', '')
  fromSequence(ws, s)
})

// Daten
let current = null, currentName = null
const loadRuns = guarded(async () => {
  const { runs } = await get('/runs')
  $('#run-list').replaceChildren(...runs.map(r => {
    const li = document.createElement('li'); li.textContent = r
    li.tabIndex = 0
    li.onclick = li.onkeydown = e => { if (e.type === 'click' || e.key === 'Enter') openRun(r, li) }
    return li
  }))
})
const openRun = guarded(async (r, li) => {
  $$('#run-list li').forEach(x => x.classList.toggle('sel', x === li))
  $('#data-title').textContent = r
  currentName = r
  current = await get(`/runs/${encodeURIComponent(r)}/data`)
  const keys = current.columns.filter(k => k !== 'label')
  $('#dt-x').replaceChildren(...keys.map(k => new Option(k, k))); $('#dt-y').replaceChildren(...keys.map(k => new Option(k, k)))
  if (keys.includes('F')) $('#dt-x').value = 'F'
  if (keys.includes('C_pF')) $('#dt-y').value = 'C_pF'
  drawData()
  $('#data-log').textContent = (await get(`/runs/${encodeURIComponent(r)}/log`)).lines.join('\n')
})
function drawData() {
  const x = $('#dt-x').value, y = $('#dt-y').value
  if (current && x && y) Plotly.react('plot-data', [{ x: current.data[x], y: current.data[y], mode: 'markers+lines', line: { color: css('--highlight') } }],
    { ...layout(y, x), showlegend: false }, CONFIG)
}
$('#dt-x').onchange = $('#dt-y').onchange = drawData
$('#dt-csv').onclick = () => { if (currentName) location.href = `/runs/${encodeURIComponent(currentName)}/data.csv` }

// Start
let curve = null
guarded(async () => {
  const info = await get('/info')
  const m = $('#mode-pill')
  m.textContent = info.simulated ? 'Simulation' : info.read_only ? 'echte Geräte, nur lesen' : 'echte Geräte'
  m.classList.add(info.simulated ? 'sim' : 'real')
  curve = await get('/limits/curve')
  $('#limit-table').innerHTML = rows(info.limits)
  const i = info.instruments
  const dev = { Netzteil: i.supply, Zugstapel: i.tension?.source, Druckstapel: i.compression ? `${i.compression.source} ${i.compression.model ?? ''}` : '',
    Kapazität: i.cap, 'Lock-in': i.lockin_model, 'AC-Quelle': info.ac?.source, Zusatz: Array.isArray(i.sample) ? i.sample.join(', ') : i.sample }
  for (const [k, v] of Object.entries(info.identify || {})) dev['Antwort ' + k] = v
  $('#dev-table').innerHTML = rows(dev)
  $('#chain-table').innerHTML = rows(info.ac_chain || {})
  reloadSeq()
  connect()
  loadChecklist()
})()
