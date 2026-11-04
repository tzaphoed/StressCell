// Blockly-Blöcke. Jeder Block entspricht einem op aus backend/core/sequence.py. Zahlenfelder
// sind Text, weil dort auch Ausdrücke stehen dürfen ("2*F0 + 1").
const COL = { cryo: 200, piezo: 20, force: 340, meas: 120, ac: 60, flow: 260 }

// [Feld, Beschriftung, Vorgabe] oder [Feld, Beschriftung, Vorgabe, [Auswahl]]
const DEF = {
  set_temperature: { cat: 'cryo', label: 'set temperature', f: [['T', 'T [K]', '300'], ['rate', 'rate [K/min]', '5']] },
  wait_temperature: { cat: 'cryo', label: 'wait for temperature', f: [['tol', 'tol [K]', '0.1'], ['hold', 'hold [s]', '60'], ['timeout', 'timeout [s]', '14400']] },
  set_field: { cat: 'cryo', label: 'set field', f: [['B', 'B [T]', '0'], ['rate', 'rate [T/min]', '0.3']] },
  wait_field: { cat: 'cryo', label: 'wait for field', f: [['tol', 'tol [T]', '0.001'], ['timeout', 'timeout [s]', '7200']] },
  set_chamber: { cat: 'cryo', label: 'chamber', f: [['mode', '', 'purge_seal', ['seal', 'purge_seal', 'vent_seal', 'pump_continuous', 'vent_continuous', 'high_vacuum']]] },
  output: { cat: 'piezo', label: 'outputs', f: [['on', '', 'true', ['true', 'false']]] },
  set_drive: { cat: 'piezo', label: 'set drive', f: [['D', 'D [V]', '0'], ['slew', 'slew [V/s]', '1']] },
  set_voltages: { cat: 'piezo', label: 'set voltages', f: [['tension', 'V_T [V]', '0'], ['compression', 'V_C [V]', '0'], ['slew', 'slew [V/s]', '1']] },
  wait_voltage: { cat: 'piezo', label: 'wait for voltage', f: [['timeout', 'timeout [s]', '600']] },
  zero_drive: { cat: 'piezo', label: 'zero drive', f: [['slew', 'slew [V/s]', '1']] },
  tare: { cat: 'force', label: 'tare force', f: [['accept', 'accept offset', 'false', ['false', 'true']]] },
  goto_force: { cat: 'force', label: 'go to force', f: [['F', 'F [N]', '0'], ['tol', 'tol [N]', '0.05'], ['hold', 'hold [s]', '10'], ['timeout', 'timeout [s]', '1800']] },
  release_force: { cat: 'force', label: 'release force loop', f: [] },
  force_sweep: { cat: 'force', label: 'force sweep', f: [['from', 'from [N]', '0'], ['to', 'to [N]', '10'], ['points', 'points', '6'], ['n', 'readings', '5'], ['dt', 'dt [s]', '1'], ['label', 'label', '']] },
  measure: { cat: 'meas', label: 'measure', f: [['n', 'readings', '5'], ['dt', 'dt [s]', '1'], ['label', 'label', '']] },
  measure_gain: { cat: 'ac', label: 'measure dF/dD', f: [['step', 'step [V]', '4'], ['settle', 'settle [s]', '20']] },
  ac_on: { cat: 'ac', label: 'AC drive on', f: [['f', 'f [Hz]', '21'], ['V', 'V peak [V]', '2']] },
  ac_off: { cat: 'ac', label: 'AC drive off', f: [] },
  frequency_sweep: { cat: 'ac', label: 'frequency sweep', f: [['from', 'from [Hz]', '2'], ['to', 'to [Hz]', '200'], ['points', 'points', '15'], ['settle', 'settle [s]', '10'], ['n', 'readings', '5'], ['dt', 'dt [s]', '2'], ['label', 'label', '']] },
  temperature_sweep: { cat: 'ac', label: 'temperature sweep', f: [['to', 'to [K]', '280'], ['rate', 'rate [K/min]', '0.5'], ['dt', 'dt [s]', '5'], ['label', 'label', '']] },
  for_range: { cat: 'flow', label: 'for', box: true, f: [['var', 'variable', 'x'], ['from', 'from', '1'], ['to', 'to', '10'], ['points', 'points', '5'], ['spacing', 'spacing', 'lin', ['lin', 'log']]] },
  repeat: { cat: 'flow', label: 'repeat', box: true, f: [['times', 'times', '3']] },
  set_variable: { cat: 'flow', label: 'set variable', f: [['name', 'name', 'x'], ['value', 'value', '0']] },
  wait: { cat: 'flow', label: 'wait', f: [['seconds', 'seconds', '60']] },
  log: { cat: 'flow', label: 'log', f: [['message', 'message', '']] },
  pause: { cat: 'flow', label: 'pause', f: [['message', 'message', '']] },
}

const CATS = [['Cryostat', 'cryo'], ['Piezo', 'piezo'], ['Force', 'force'], ['Measure', 'meas'], ['Elastocaloric', 'ac'], ['Flow', 'flow']]

export function registerBlocks() {
  for (const [op, d] of Object.entries(DEF)) {
    Blockly.Blocks['sc_' + op] = {
      init() {
        const head = this.appendDummyInput().appendField(d.label)
        for (const [name, label, value, opts] of d.f) {
          if (label) head.appendField(label)
          head.appendField(opts ? new Blockly.FieldDropdown(opts.map(o => [o, o])) : new Blockly.FieldTextInput(value), name)
          if (opts) this.setFieldValue(value, name)
        }
        if (d.box) this.appendStatementInput('DO').appendField('do')
        this.setPreviousStatement(true)
        this.setNextStatement(true)
        this.setColour(COL[d.cat])
        this.setInputsInline(true)
      },
    }
  }
}

export const toolbox = {
  kind: 'categoryToolbox',
  contents: CATS.map(([name, cat]) => ({
    kind: 'category', name, colour: String(COL[cat]),
    contents: Object.entries(DEF).filter(([, d]) => d.cat === cat).map(([op]) => ({ kind: 'block', type: 'sc_' + op })),
  })),
}

function field(v) {
  if (v === 'true') return true
  if (v === 'false') return false
  return v !== '' && !isNaN(Number(v)) ? Number(v) : v
}

function chain(block) {
  const steps = []
  for (let b = block; b; b = b.getNextBlock()) {
    if (!b.type.startsWith('sc_')) continue
    const op = b.type.slice(3)
    const st = { op }
    for (const [name] of DEF[op].f) {
      const v = b.getFieldValue(name)
      if (v !== '' && v !== null) st[name] = (name === 'var' || name === 'name' || name === 'label' || name === 'message') ? v : field(v)
    }
    if (DEF[op].box) st.do = chain(b.getInputTargetBlock('DO'))
    steps.push(st)
  }
  return steps
}

export const looseBlocks = ws => Math.max(0, ws.getTopBlocks(true).length - 1)

export function toSequence(ws, name) {
  // Nur der oberste Stapel: lose abgelegte Blöcke sind Notizen, keine Messung
  const tops = ws.getTopBlocks(true)
  const steps = tops.length ? chain(tops[0]) : []
  return { format: 'stresscell-sequence', version: 1, name, steps, editor: Blockly.serialization.workspaces.save(ws) }
}

export function fromSequence(ws, seq) {
  ws.clear()
  if (seq.editor) { Blockly.serialization.workspaces.load(seq.editor, ws); return true }
  // Datei ohne Workspace, z.B. aus Python geschrieben: Blöcke aus den Schritten bauen
  const build = steps => {
    let first = null, prev = null
    for (const st of steps) {
      const b = ws.newBlock('sc_' + st.op)
      for (const [name] of DEF[st.op].f) if (st[name] !== undefined) b.setFieldValue(String(st[name]), name)
      if (st.do) { const k = build(st.do); if (k) b.getInput('DO').connection.connect(k.previousConnection) }
      b.initSvg()
      if (prev) prev.nextConnection.connect(b.previousConnection); else first = b
      prev = b
    }
    return first
  }
  build(seq.steps)
  ws.render()
  return false
}
