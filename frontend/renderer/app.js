import { registerBlocks, toolbox, toSequence, fromSequence, looseBlocks } from './blocks.js'

const $ = s => document.querySelector(s)
const $$ = s => [...document.querySelectorAll(s)]
const TOKEN = document.querySelector('meta[name=stresscell-token]').content

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
const layout = (y, x = 'time [min]') => ({
  datarevision: Date.now(), uirevision: y, margin: { l: 52, r: 12, t: 6, b: 38 }, paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: css('--bg-secondary'),
  font: { color: css('--text-primary'), size: 11 }, showlegend: true, legend: { orientation: 'h', y: 1.12 },
  xaxis: { title: x, gridcolor: css('--border'), zerolinecolor: css('--border-light') },
  yaxis: { title: y, gridcolor: css('--border'), zerolinecolor: css('--border-light') },
})
const CONFIG = { responsive: true, displaylogo: false }

// ── Live-Anzeige ─────────────────────────────────────────────────────────
const READOUTS = [
  ['T', 'Temperature', 'K', 3], ['B', 'Field', 'T', 3], ['F', 'Force', 'N', 3], ['stress_MPa', 'Stress', 'MPa', 1],
  ['C_pF', 'Capacitance', 'pF', 6], ['V_T', 'Tension stack', 'V', 2], ['V_C', 'Compression stack', 'V', 2],
  ['I_T', 'I tension', 'mA', 3, 1e3], ['I_C', 'I compression', 'mA', 3, 1e3],
  ['T_ac_K', 'T amplitude', 'mK', 2, 1e3], ['theta', 'Phase', '°', 1], ['f_ac', 'AC frequency', 'Hz', 1], ['V_ac', 'AC peak', 'V', 2],
]
$('#readouts').innerHTML = READOUTS.map(([k, n, u]) =>
  `<div class="ro" id="ro-${k}"><div class="k">${n}</div><div><span class="v">–</span><span class="u">${u}</span></div></div>`).join('') +
  `<div class="ro" id="ro-chamber"><div class="k">Chamber</div><div class="small" id="chamber-text">–</div></div>
   <div class="ro" id="ro-window"><div class="k">Voltage window</div><div class="small" id="window-text">–</div></div>
   <div class="ro" id="ro-loop"><div class="k">Force loop</div><div class="small" id="loop-text">–</div></div>`

const hist = { t: [], T: [], F: [], V_T: [], V_C: [], vmin: [], vmax: [] }
let last = null, plotted = false, t0 = null, lastDraw = 0

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
  $('#loop-text').textContent = f?.enabled ? `on, ${f.target.toFixed(2)} N, G ${f.G.toFixed(2)} N/V` : 'off'
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
    { x: t, y: hist.vmax, name: 'window', line: { color: css('--danger'), dash: 'dot', width: 1 } },
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
    const b = document.createElement('button'); b.textContent = 'Acknowledge'
    b.onclick = guarded(() => post('/alarms/ack', { key: a.key }))
    row.append(s, b)
    return row
  }))
}

function showRun(r) {
  const p = $('#run-pill')
  p.className = 'pill ' + r.state
  p.textContent = r.state
  $('#run-text').textContent = [r.name, r.message, r.state === 'idle' ? r.result : `${r.points} points`].filter(Boolean).join(' · ')
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
  }
}

// ── Bedienung ────────────────────────────────────────────────────────────
$$('.nav').forEach(b => b.onclick = () => {
  $$('.nav').forEach(x => x.classList.toggle('active', x === b))
  $$('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + b.dataset.page))
  lastDraw = 0
  if (b.dataset.page === 'editor') Blockly.svgResize(ws)
  if (b.dataset.page === 'run') { reloadSeq(); drawPoints() }
  if (b.dataset.page === 'data') loadRuns()
  window.dispatchEvent(new Event('resize'))
  draw()
})
$('#stop-all').onclick = guarded(() => post('/stop_all'))
const step = guarded(async st => { await post('/step', st); toast(`${st.op} done`) })
$$('[data-step]').forEach(b => b.onclick = () => step(JSON.parse(b.dataset.step)))
$$('form[data-op]').forEach(f => f.onsubmit = e => {
  e.preventDefault()
  const st = { op: f.dataset.op }
  for (const i of f.querySelectorAll('input')) if (i.value !== '') st[i.name] = Number(i.value)
  step(st)
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
  el.textContent = (r.ok ? 'OK' : 'FAILED: ' + r.error) + `\nestimated duration ${h.toFixed(2)} h, ${r.points ?? 0} points`
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
  if (looseBlocks(ws)) toast(`${looseBlocks(ws)} loose block group(s) ignored: only the top stack runs`, true)
  return toSequence(ws, $('#ed-name').value)
}
$('#ed-save').onclick = guarded(async () => {
  const n = $('#ed-name').value
  await post('/sequences/' + encodeURIComponent(n), edSeq())
  $('#ed-msg').textContent = `saved ${n}.seq.json`
  reloadSeq()
})
$('#ed-check').onclick = guarded(async () => report($('#ed-msg'), await post('/sequence/check', edSeq())))
$('#ed-load').onclick = guarded(async () => {
  const s = await selected()
  $('#ed-name').value = $('#seq-list').value.replace('.seq.json', '')
  fromSequence(ws, s)
})

// Daten
let current = null
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

// Start
let curve = null
guarded(async () => {
  const info = await get('/info')
  const m = $('#mode-pill')
  m.textContent = info.simulated ? 'simulation' : info.read_only ? 'real instruments, read only' : 'real instruments'
  m.classList.add(info.simulated ? 'sim' : 'real')
  curve = await get('/limits/curve')
  const rows = o => Object.entries(o).map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('')
  $('#limit-table').innerHTML = rows(info.limits)
  $('#cal-table').innerHTML = rows(info.calibration)
  reloadSeq()
  connect()
})()
