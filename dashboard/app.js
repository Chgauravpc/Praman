/* Pramaan dashboard renderer.
 *
 * This file formats and lays out. It does not compute. Every rate, total and
 * contrast in the JSON was derived in `pramaan/report/snapshot.py`, which has
 * tests; nothing is divided, summed or estimated here. The only arithmetic
 * below is bar geometry -- a width as a share of the row maximum -- which is
 * layout, not a finding.
 *
 * Values are inserted with textContent, never innerHTML, so a string from the
 * ledger cannot become markup. Ledger payloads contain model-authored text
 * (plan rationales, call transcripts); rendering that as HTML would make the
 * dashboard the one place where an LLM's output is executed rather than
 * displayed.
 */

'use strict';

var SCHEMA_VERSION = 1;
var SOURCE = '../build/dashboard.json';

/* -- formatting ------------------------------------------------------- */

/* Indian digit grouping: the last three digits, then twos. 744967 paise reads
 * as 7,449.67 and 74496763 as 7,44,967.63 -- lakh-grouped, because the README
 * and the pitch quote the figure that way and a dashboard that renders it
 * 744,967 makes a reviewer stop and re-read. */
function groupIndian(digits) {
  if (digits.length <= 3) return digits;
  var head = digits.slice(0, -3);
  var tail = digits.slice(-3);
  return head.replace(/\B(?=(\d{2})+(?!\d))/g, ',') + ',' + tail;
}

function rupees(paise) {
  if (paise === null || paise === undefined) return '--';
  var negative = paise < 0;
  var abs = Math.abs(paise);
  var whole = Math.floor(abs / 100);
  var pais = String(abs % 100).padStart(2, '0');
  return (negative ? '-' : '') + '₹' + groupIndian(String(whole)) + '.' + pais;
}

function count(n) {
  if (n === null || n === undefined) return '--';
  return groupIndian(String(n));
}

/* A rate as percentage points. Contrasts are already differences of rates, so
 * they carry an explicit sign -- "+17.49pp" and "17.49pp" mean different
 * things when the reader is scanning for direction. */
function pp(rate, signed) {
  if (rate === null || rate === undefined) return '--';
  var value = rate * 100;
  var text = value.toFixed(2) + 'pp';
  return signed && value > 0 ? '+' + text : text;
}

function percent(rate) {
  if (rate === null || rate === undefined) return '--';
  return (rate * 100).toFixed(1) + '%';
}

/* A duration, in the same shape `cli._hours` prints it. The observation window
 * is 259,200 seconds; running that through the lakh grouping above renders it
 * "2,59,200s", which is both ugly and wrong-headed -- digit grouping for money
 * and counts, not for a span of time. The CLI already calls this 3d, so the
 * dashboard calls it 3d. */
function duration(seconds) {
  if (seconds === null || seconds === undefined) return '--';
  if (seconds % 86400 === 0 && seconds >= 86400) return (seconds / 86400) + 'd';
  return Math.floor(seconds / 3600) + 'h';
}

/* -- DOM helpers ------------------------------------------------------- */

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function show(id) { document.getElementById(id).hidden = false; }

/* -- provenance -------------------------------------------------------- */

function renderProvenance(p) {
  var dl = document.getElementById('provenance');
  var pairs = [
    ['ledger', p.ledger_file],
    ['rows', count(p.ledger_rows)],
    /* The same 64-hex chain head `make demo-full` prints. A reviewer can
     * compare the two strings and know this page is rendering the run they
     * were shown rather than some other one. */
    ['head', (p.ledger_head_hash || '').slice(0, 16)],
    ['metrics', p.metrics_file || 'absent'],
    ['why', p.golden_file]
  ];
  pairs.forEach(function (pair) {
    dl.appendChild(el('dt', null, pair[0]));
    dl.appendChild(el('dd', null, pair[1]));
  });
}

/* -- tiles ------------------------------------------------------------- */

function tile(label, value, sub, tone, population) {
  var node = el('div', 'tile');
  node.appendChild(el('div', 'label', label));
  node.appendChild(el('div', 'value' + (tone ? ' ' + tone : ''), value));
  if (sub) node.appendChild(el('div', 'sub', sub));
  if (population) {
    var foot = el('div', 'population');
    foot.appendChild(el('span', 'flag' + (population.full ? ' full' : ''), population.tag));
    foot.appendChild(document.createTextNode(population.text));
    node.appendChild(foot);
  }
  return node;
}

/* The contrast tiles. Every one states its arms and its population, because
 * `Contrast.actionable_only` exists precisely so a caller cannot show a
 * subgroup figure as though it were the headline -- and a tile is a caller. */
function renderContrasts(headline) {
  var host = document.getElementById('contrast-tiles');
  var order = ['C-A', 'C-B', 'B-A', 'B-A actioned'];
  var captions = {
    'C-A': 'LLM-planned vs. holdout',
    'C-B': 'LLM-planned vs. rules-only',
    'B-A': 'Rules-only vs. holdout',
    'B-A actioned': 'Rules-only, actioned subset'
  };

  order.forEach(function (name) {
    var contrast = headline.contrasts[name];
    if (!contrast) return;
    var rate = contrast.intervals.rate;
    var money = contrast.intervals.money_per_event;

    var sub = '95% CI [' + pp(rate.low, true) + ', ' + pp(rate.high, true) + '] '
            + rate.method;
    if (rate.fallback_reason) sub += ' (fell back: ' + rate.fallback_reason + ')';
    sub += rate.excludes_zero ? ' — excludes zero' : ' — includes zero';
    if (money) sub += '\nΔ money/event ' + rupees(Math.round(money.point));

    host.appendChild(tile(
      captions[name] + '  (' + contrast.treatment + '−' + contrast.control + ')',
      pp(rate.point, true),
      sub,
      rate.point > 0 ? 'pos' : (rate.point < 0 ? 'neg' : null),
      contrast.actionable_only
        ? { tag: 'subgroup', full: false,
            text: 'actioned subset only — not the headline. n=' +
                  count(contrast.n_treatment) + ' vs ' + count(contrast.n_control) }
        : { tag: 'full batch', full: true,
            text: 'every event, including those the policy declines to act on. n=' +
                  count(contrast.n_treatment) + ' vs ' + count(contrast.n_control) }
    ));
  });

  var note = el('p', 'section-note',
    'Resamples: ' + count(headline.resamples) + '. Observation window: ' +
    duration(headline.window_seconds) + '. Events: ' + count(headline.n_events) + '. ' +
    'Money per event is a point estimate on heavy-tailed amounts and can differ ' +
    'in sign from the rate — the arms are balanced on event count, not on value ' +
    'at risk, so a lift in recovery rate need not be a lift in mean rupees.');
  host.parentNode.appendChild(note);
}

var ARM_CAPTIONS = {
  A: 'holdout — takes no action, by construction',
  B: 'rules-only — deterministic lookup table',
  C: 'LLM-planned — planner proposes, envelope disposes'
};

function renderArms(arms) {
  var host = document.getElementById('arm-tiles');
  Object.keys(arms).sort().forEach(function (name) {
    var arm = arms[name];
    host.appendChild(tile(
      'Arm ' + name,
      percent(arm.rate),
      count(arm.recovered) + ' of ' + count(arm.n) + ' recovered\n' +
      rupees(arm.recovered_paise) + ' of ' + rupees(arm.at_risk_paise) + ' at risk\n' +
      count(arm.contacted) + ' contacted · ' + count(arm.escalations) + ' escalated',
      null,
      { tag: 'observed', full: true, text: ARM_CAPTIONS[name] || 'arm ' + name }
    ));
  });
}

/* -- bars -------------------------------------------------------------- */

function renderBars(hostId, counts, toneOf) {
  var host = document.getElementById(hostId);
  var names = Object.keys(counts).sort(function (a, b) {
    return counts[b] - counts[a] || a.localeCompare(b);
  });
  /* Bar geometry only. The widest bar is full width and the rest are drawn in
   * proportion to it; this scales the picture, it does not scale a number. */
  var max = 0;
  names.forEach(function (n) { if (counts[n] > max) max = counts[n]; });

  names.forEach(function (name) {
    var row = el('div', 'bar-row');
    row.appendChild(el('div', 'name', name));
    var track = el('div', 'track');
    var fill = el('div', 'fill' + (counts[name] === 0 ? ' zero' : ''));
    var tone = toneOf ? toneOf(name) : null;
    if (tone) fill.className = 'fill ' + tone;
    fill.style.width = (max > 0 ? (counts[name] / max) * 100 : 0) + '%';
    track.appendChild(fill);
    row.appendChild(track);
    row.appendChild(el('div', 'count', count(counts[name])));
    host.appendChild(row);
  });
}

/* -- categories -------------------------------------------------------- */

function renderCategories(categories) {
  var host = document.getElementById('categories');
  var note = document.getElementById('category-note');

  /* Honest about however many groups the batch actually has. The full batch
   * spans five source types and the dev batch is payment-only, so this renders
   * N groups and says so when N is 1 -- drawing four empty bars to match the
   * brief's mockup would be inventing categories the run did not produce. */
  note.textContent = categories.length === 1
    ? 'This batch contains one source type. The renderer handles any number; ' +
      'the committed batch has exactly one, and drawing empty bars for the ' +
      'other four adapters would imply data that is not there.'
    : 'Grouped by the source adapter that detected the event.';

  categories.forEach(function (category) {
    var card = el('div', 'category');
    card.appendChild(el('h3', null, category.source_type));

    var table = el('table', 'arms');
    var head = el('tr');
    ['Arm', 'Events', 'Recovered', 'Rate', 'At risk', 'Recovered'].forEach(function (h) {
      head.appendChild(el('th', null, h));
    });
    table.appendChild(head);

    Object.keys(category.arms).sort().forEach(function (name) {
      var arm = category.arms[name];
      var row = el('tr');
      [name, count(arm.n), count(arm.recovered), percent(arm.rate),
       rupees(arm.at_risk_paise), rupees(arm.recovered_paise)].forEach(function (cell) {
        row.appendChild(el('td', null, cell));
      });
      table.appendChild(row);
    });

    /* Six monospace columns of rupee amounts have a min-content width far
     * wider than a phone. Without its own scroller the table forces the whole
     * body wide and every paragraph on the page gets clipped. Wide content
     * scrolls inside its own box; the page never scrolls sideways. */
    var scroller = el('div', 'table-scroll');
    scroller.appendChild(table);
    card.appendChild(scroller);
    host.appendChild(card);
  });
}

/* -- why panels -------------------------------------------------------- */

var PANEL_TONE = { RECEIPT_AUDIT: 'receipt', ACTION: 'action' };

/* Field names are rendered as they appear in the payload rather than
 * prettified. A reviewer reading `may_plan_action: false` on screen can grep
 * that exact string in the repo; "Allowed to act: no" cannot be grepped. */
function renderField(dd, value) {
  if (value === null || value === undefined) {
    dd.textContent = 'null';
    return;
  }
  if (typeof value === 'boolean') {
    var span = el('span', value ? 'true' : 'false', String(value));
    dd.appendChild(span);
    return;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) { dd.textContent = '[]'; return; }
    var list = el('ul');
    value.forEach(function (item) {
      list.appendChild(el('li', null,
        typeof item === 'object' ? JSON.stringify(item) : String(item)));
    });
    dd.appendChild(list);
    return;
  }
  if (typeof value === 'object') {
    dd.textContent = JSON.stringify(value);
    return;
  }
  dd.textContent = String(value);
}

function renderWhy(panels) {
  var host = document.getElementById('why-panels');
  panels.forEach(function (panel) {
    var card = el('div', 'why' + (PANEL_TONE[panel.kind] ? ' ' + PANEL_TONE[panel.kind] : ''));

    var head = el('div', 'why-head');
    head.appendChild(el('span', 'why-kind', panel.kind));
    head.appendChild(el('span', 'why-caption', panel.caption));
    head.appendChild(el('span', 'why-ref',
      'seq ' + panel.seq + ' · ' + panel.row_hash.slice(0, 12)));
    card.appendChild(head);

    var fields = el('dl', 'fields');
    Object.keys(panel.fields).forEach(function (name) {
      var value = panel.fields[name];
      fields.appendChild(el('dt', null, name));
      var dd = el('dd', name === 'verbatim' ? 'quote' : null);
      if (name === 'verbatim' && value) {
        dd.textContent = '“' + value + '”';
      } else if (name.indexOf('paise') !== -1 && typeof value === 'number') {
        dd.textContent = rupees(value) + '  (' + value + ' paise)';
      } else {
        renderField(dd, value);
      }
      fields.appendChild(dd);
    });
    card.appendChild(fields);
    host.appendChild(card);
  });
}

/* -- what came in ------------------------------------------------------ */

var FACET_CAPTION = {
  source_type: 'Source adapter — which of the five detected it',
  reason_class: 'Reason class — what the taxonomy made of the raw code',
  segment: 'Segment',
  legal_context: 'Legal context — service, collection or promotional',
  channel_eligibility: 'Channel eligibility — what the hour and consent allow',
  hour_bucket: 'Hour bucket — each boundary is a regulatory edge',
  decay_profile: 'Decay profile — how fast the money goes cold',
};

function renderDetected(detected, provenance) {
  if (!detected) return false;

  var summary = document.getElementById('detected-summary');
  summary.textContent = '';
  [
    ['events detected', count(detected.count)],
    ['value at risk', rupees(detected.at_risk_paise)],
    ['resolved to an outcome', count(detected.resolved)],
    ['unresolved', count(detected.unresolved_count)],
  ].forEach(function (pair) {
    var wrap = el('div', 'pair');
    wrap.appendChild(el('dt', null, pair[0]));
    wrap.appendChild(el('dd', null, pair[1]));
    summary.appendChild(wrap);
  });

  var host = document.getElementById('detected-facets');
  host.textContent = '';
  Object.keys(FACET_CAPTION).forEach(function (name) {
    var counts = detected.facets[name];
    if (!counts || !Object.keys(counts).length) return;
    var card = el('div', 'facet');
    card.appendChild(el('h3', null, name));
    card.appendChild(el('p', 'section-note', FACET_CAPTION[name]));
    var bars = el('div', 'bars compact');
    bars.id = 'facet-' + name;
    card.appendChild(bars);
    host.appendChild(card);
    renderBars('facet-' + name, counts, null);
  });

  var foot = document.getElementById('detected-foot');
  foot.textContent = '';
  /* The reconciliation, stated either way round. Equal counts are the claim;
   * an inequality names the events that went missing rather than rounding the
   * difference away. */
  if (detected.unresolved_count === 0) {
    foot.appendChild(document.createTextNode(
      'Every one of the ' + count(detected.count) + ' detected events resolved to an ' +
      'OUTCOME row — detections and outcomes reconcile exactly, so nothing was ' +
      'dropped between the two. '));
  } else {
    foot.appendChild(document.createTextNode(
      count(detected.unresolved_count) + ' detected events never reached an OUTCOME ' +
      'row (' + detected.unresolved.slice(0, 5).join(', ') + '…). '));
  }
  if (provenance.detected_file) {
    foot.appendChild(document.createTextNode('The full input export is '));
    var link = el('a', 'log-path', provenance.detected_file);
    link.href = '../build/' + provenance.detected_file;
    link.target = '_blank';
    link.rel = 'noopener';
    foot.appendChild(link);
    foot.appendChild(document.createTextNode(
      ' — ' + provenance.detected_columns.length + ' columns per event, including the ' +
      'raw cause signal and the action menu each event declares, which the ' +
      'outcome-spined export drops.'));
  }
  return true;
}

/* -- row-level data ---------------------------------------------------- */

/* Rendering 6,000 rows x 20 columns is 120,000 DOM nodes and a visibly janky
 * page, so the table renders at most this many and says so. Filtering, the
 * summary and the CSV export all run over the *full* filtered set regardless --
 * only the drawing is capped. */
var RENDER_CAP = 300;

/* Columns shown in the browser, in this order. The CSV keeps all 20; the table
 * drops the ones that are either constant per run or too wide to scan, and
 * `exception_reason` is last because it is prose. */
var TABLE_COLUMNS = [
  'seq', 'arm', 'event_id', 'counterparty_id', 'source_type', 'reason_class',
  'segment', 'gate_decision', 'gate_rule', 'action', 'channel',
  'amount_at_risk_paise', 'amount_recovered_paise', 'recovered', 'contacted',
  'cause', 'exception_reason',
];

var PAISE_COLUMNS = { amount_at_risk_paise: 1, amount_recovered_paise: 1 };

var allRows = [];        // every parsed row
var filteredRows = [];   // the current filter's result
var sortKey = 'seq';
var sortAsc = true;

/* RFC 4180, minus the parts this file cannot contain.
 *
 * A split on commas would corrupt `exception_reason`, which carries the
 * envelope's refusal prose and is full of them -- so this walks characters and
 * honours quoting. The writer (`report/snapshot.py`) raises on a bare CR, so
 * only "\n" needs handling as a terminator, and a doubled "" inside a quoted
 * field is an escaped quote. */
function parseCSV(text) {
  var rows = [];
  var row = [];
  var field = '';
  var quoted = false;
  var i = 0;
  while (i < text.length) {
    var ch = text[i];
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') { field += '"'; i += 2; continue; }
        quoted = false; i++; continue;
      }
      field += ch; i++; continue;
    }
    if (ch === '"') { quoted = true; i++; continue; }
    if (ch === ',') { row.push(field); field = ''; i++; continue; }
    if (ch === '\n') {
      row.push(field); rows.push(row); row = []; field = ''; i++; continue;
    }
    field += ch; i++;
  }
  /* A trailing newline leaves an empty pending field; anything else is a real
   * last line with no terminator. */
  if (field !== '' || row.length) { row.push(field); rows.push(row); }
  if (!rows.length) return [];

  var header = rows[0];
  return rows.slice(1).map(function (cells) {
    var out = {};
    header.forEach(function (name, idx) { out[name] = cells[idx] === undefined ? '' : cells[idx]; });
    return out;
  });
}

function optionsFor(select, values) {
  values.sort().forEach(function (v) {
    var opt = document.createElement('option');
    opt.value = v;
    opt.textContent = v === '' ? '(none)' : v;
    select.appendChild(opt);
  });
}

function distinct(rows, key) {
  var seen = {};
  rows.forEach(function (r) { seen[r[key]] = true; });
  return Object.keys(seen);
}

function currentFilters() {
  return {
    arm: document.getElementById('f-arm').value,
    source_type: document.getElementById('f-source').value,
    action: document.getElementById('f-action').value,
    gate_decision: document.getElementById('f-gate').value,
    recovered: document.getElementById('f-recovered').value,
    exceptionOnly: document.getElementById('f-exception').checked,
    search: document.getElementById('f-search').value.trim().toLowerCase(),
  };
}

function applyFilters() {
  var f = currentFilters();
  filteredRows = allRows.filter(function (r) {
    if (f.arm && r.arm !== f.arm) return false;
    if (f.source_type && r.source_type !== f.source_type) return false;
    if (f.action && r.action !== f.action) return false;
    if (f.gate_decision && r.gate_decision !== f.gate_decision) return false;
    if (f.recovered && r.recovered !== f.recovered) return false;
    if (f.exceptionOnly && !r.exception_reason) return false;
    if (f.search) {
      var hay = (r.event_id + ' ' + r.counterparty_id).toLowerCase();
      if (hay.indexOf(f.search) === -1) return false;
    }
    return true;
  });

  filteredRows.sort(function (a, b) {
    var x = a[sortKey], y = b[sortKey];
    var nx = Number(x), ny = Number(y);
    if (x !== '' && y !== '' && !isNaN(nx) && !isNaN(ny)) { x = nx; y = ny; }
    if (x < y) return sortAsc ? -1 : 1;
    if (x > y) return sortAsc ? 1 : -1;
    return 0;
  });

  renderSummary();
  renderTable();
}

/* The one place this page adds up numbers -- and it is not a finding.
 *
 * Everywhere else the browser only formats, because a headline the page derived
 * could disagree with the headline the run printed. This is different: it is a
 * total over whatever subset the *viewer* just chose with the filters, which no
 * offline producer could have precomputed. It is labelled as a filtered
 * subtotal, never as a metric, and carries no interval -- a difference between
 * two of these subtotals is not a contrast. */
function renderSummary() {
  var atRisk = 0, recoveredPaise = 0, recovered = 0, contacted = 0, refused = 0;
  filteredRows.forEach(function (r) {
    atRisk += Number(r.amount_at_risk_paise) || 0;
    recoveredPaise += Number(r.amount_recovered_paise) || 0;
    if (r.recovered === 'true') recovered++;
    if (r.contacted === 'true') contacted++;
    if (r.exception_reason) refused++;
  });
  var n = filteredRows.length;
  var dl = document.getElementById('rows-summary');
  dl.textContent = '';
  [
    ['rows', count(n) + ' of ' + count(allRows.length)],
    ['at risk', rupees(atRisk)],
    /* Two distinct quantities, so two distinct labels. Both were called
     * "recovered" at first, which put two different numbers under one word. */
    ['recovered value', rupees(recoveredPaise)],
    ['recovered events',
      count(recovered) + (n ? ' (' + percent(recovered / n) + ')' : '')],
    ['contacted', count(contacted)],
    ['envelope refused', count(refused)],
  ].forEach(function (pair) {
    var wrap = el('div', 'pair');
    wrap.appendChild(el('dt', null, pair[0]));
    wrap.appendChild(el('dd', null, pair[1]));
    dl.appendChild(wrap);
  });
}

function renderTable() {
  var table = document.getElementById('rows-table');
  table.textContent = '';

  var head = el('tr');
  TABLE_COLUMNS.forEach(function (name) {
    var th = el('th', 'sortable' + (name === sortKey ? (sortAsc ? ' asc' : ' desc') : ''),
                name + (name === sortKey ? (sortAsc ? ' ▲' : ' ▼') : ''));
    th.addEventListener('click', function () {
      if (sortKey === name) { sortAsc = !sortAsc; } else { sortKey = name; sortAsc = true; }
      applyFilters();
    });
    head.appendChild(th);
  });
  table.appendChild(head);

  filteredRows.slice(0, RENDER_CAP).forEach(function (r) {
    var tr = el('tr');
    TABLE_COLUMNS.forEach(function (name) {
      var raw = r[name];
      var cell;
      if (PAISE_COLUMNS[name]) {
        cell = el('td', 'num', rupees(Number(raw) || 0));
      } else if (raw === 'true' || raw === 'false') {
        cell = el('td', null);
        cell.appendChild(el('span', raw === 'true' ? 'true' : 'false', raw));
      } else if (name === 'exception_reason') {
        /* The text goes in an inner span so print can line-clamp it. Clamping
         * the <td> itself needs `display:-webkit-box`, which stops the element
         * being a table-cell and collapses the column to a sliver -- which is
         * exactly what it did. */
        cell = el('td', 'prose');
        cell.appendChild(el('span', 'clamp', raw));
        if (raw) cell.title = raw;
      } else {
        cell = el('td', name === 'seq' ? 'num' : null, raw);
      }
      tr.appendChild(cell);
    });
    table.appendChild(tr);
  });

  var note = document.getElementById('rows-truncated');
  if (filteredRows.length > RENDER_CAP) {
    note.hidden = false;
    /* Worded to read correctly on paper as well as on screen. It used to say
     * "use Download CSV", which is fine under a visible button and confusing in
     * a printed PDF where every control is hidden -- and this note prints, on
     * purpose: the summary above totals all matching rows while the table shows
     * only the capped subset, so a printed page without this line would invite
     * exactly the wrong reading. */
    note.textContent = 'Showing the first ' + count(RENDER_CAP) + ' of ' +
      count(filteredRows.length) + ' matching rows — the summary above covers ' +
      'all ' + count(filteredRows.length) + '. The CSV export contains every ' +
      'matching row, not just the ones drawn here.';
  } else {
    note.hidden = true;
  }
}

/* Re-serialise the filtered rows, with the same quoting rules the writer used,
 * so a downloaded slice opens in a spreadsheet exactly like the full file. */
function toCSV(rows, columns) {
  var lines = [columns.join(',')];
  rows.forEach(function (r) {
    lines.push(columns.map(function (c) {
      var v = r[c] === undefined || r[c] === null ? '' : String(r[c]);
      return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
    }).join(','));
  });
  return lines.join('\n') + '\n';
}

function downloadFiltered(columns) {
  var blob = new Blob([toCSV(filteredRows, columns)], {
    type: 'text/csv;charset=utf-8',
  });
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = 'pramaan-actions-filtered.csv';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  /* Revoked on the next tick rather than immediately: Safari has historically
   * cancelled the download if the URL dies in the same frame as the click. */
  setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
}

function initRows(provenance) {
  var section = document.getElementById('rows-section');
  var status = document.getElementById('rows-status');
  var button = document.getElementById('rows-load');

  if (!provenance.rows_file) {
    status.textContent = 'No row export in this snapshot. Re-run `make dashboard`.';
    button.disabled = true;
    show('rows-section');
    return;
  }

  button.textContent = 'Load ' + count(provenance.rows_exported) + ' rows';
  status.textContent = provenance.rows_file + ' · ' +
    provenance.rows_columns.length + ' columns';

  button.addEventListener('click', function () {
    button.disabled = true;
    status.textContent = 'loading ' + provenance.rows_file + '…';
    fetch('../build/' + provenance.rows_file, { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      })
      .then(function (text) {
        allRows = parseCSV(text);
        if (allRows.length !== provenance.rows_exported) {
          status.textContent = 'warning: parsed ' + count(allRows.length) +
            ' rows but the snapshot declares ' + count(provenance.rows_exported) +
            ' — the CSV and the snapshot are out of step; re-run `make dashboard`.';
        } else {
          status.textContent = count(allRows.length) + ' rows loaded from ' +
            provenance.rows_file;
        }
        optionsFor(document.getElementById('f-arm'), distinct(allRows, 'arm'));
        optionsFor(document.getElementById('f-source'), distinct(allRows, 'source_type'));
        optionsFor(document.getElementById('f-action'), distinct(allRows, 'action'));
        optionsFor(document.getElementById('f-gate'), distinct(allRows, 'gate_decision'));

        ['f-arm', 'f-source', 'f-action', 'f-gate', 'f-recovered', 'f-exception']
          .forEach(function (id) {
            document.getElementById(id).addEventListener('change', applyFilters);
          });
        document.getElementById('f-search').addEventListener('input', applyFilters);
        document.getElementById('f-reset').addEventListener('click', function () {
          ['f-arm', 'f-source', 'f-action', 'f-gate', 'f-recovered'].forEach(function (id) {
            document.getElementById(id).value = '';
          });
          document.getElementById('f-exception').checked = false;
          document.getElementById('f-search').value = '';
          applyFilters();
        });
        document.getElementById('rows-csv').addEventListener('click', function () {
          downloadFiltered(provenance.rows_columns);
        });
        document.getElementById('rows-pdf').addEventListener('click', function () {
          window.print();
        });

        document.getElementById('rows-ui').hidden = false;
        applyFilters();
      })
      .catch(function (error) {
        button.disabled = false;
        status.textContent = 'could not load ' + provenance.rows_file + ' (' +
          error.message + '). Run `make dashboard`, and serve the repo root so ' +
          'that ../build/ is reachable.';
      });
  });

  show('rows-section');
}

/* -- running the pipeline --------------------------------------------- */
/*
 * The page can start a run and watch it. Everything below talks to
 * `pramaan/report/server.py`, which shells out to the real CLI -- so this code
 * renders progress and never derives a result. If the server is not there (the
 * page was opened under a plain `http.server`), the whole section stays hidden
 * and the dashboard behaves exactly as it did before.
 */

var STAGE_LABEL = {
  sense: 'sensing — normalising events, writing DETECT rows',
  envelope: 'envelope — judging every proposed action',
  ledger: 'ledger — hashing and verifying the chain',
  measure: 'measuring — arms, contrasts, bootstrap',
  done: 'complete',
  fail: 'checks failed',
};

var logCursor = 0;
var pollTimer = null;

/* Paths the run mentions, turned into links.
 *
 * The CLI names the files it writes -- build/ledger-full.jsonl,
 * build/dashboard.json, assets/voice-demo.mp3 -- and until now they were inert
 * text in a log. The server already serves the repo root, so every one of them
 * is one fetch away; making them clickable turns "it says it wrote a ledger"
 * into "here is the ledger", which is the difference between a claim and a
 * receipt.
 *
 * Anchored to build/, assets/, tests/ and fixtures/ so a stray word cannot
 * become a link, and the text is still inserted as text -- only the wrapping
 * element changes. */
var PATH_PATTERN = /((?:build|assets|tests|fixtures|dashboard)\/[A-Za-z0-9._\/-]+)/g;

function renderLogLine(line) {
  var wrapper = el('span', line.kind ? 'log-' + line.kind : null);
  var text = line.text + '\n';
  var lastIndex = 0;
  var match;
  PATH_PATTERN.lastIndex = 0;
  while ((match = PATH_PATTERN.exec(text)) !== null) {
    if (match.index > lastIndex) {
      wrapper.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
    }
    var link = el('a', 'log-path', match[1]);
    link.href = '/' + match[1];
    link.target = '_blank';
    link.rel = 'noopener';
    link.title = 'open ' + match[1];
    wrapper.appendChild(link);
    lastIndex = match.index + match[1].length;
  }
  wrapper.appendChild(document.createTextNode(text.slice(lastIndex)));
  return wrapper;
}

function setRunStatus(status) {
  var badge = document.getElementById('run-status');
  badge.textContent = status;
  badge.className = 'run-badge ' + status;
}

function renderRunButtons(runs) {
  var host = document.getElementById('run-buttons');
  runs.forEach(function (run) {
    var card = el('button', 'run-card');
    card.type = 'button';
    card.appendChild(el('span', 'run-id', run.id));
    card.appendChild(el('span', 'run-label', run.label));
    /* The estimate matters on camera: `execute-full` is an eight-minute
     * bootstrap, and finding that out mid-take is the wrong time. */
    card.appendChild(el('span', 'run-cost',
      run.seconds >= 120 ? '~' + Math.round(run.seconds / 60) + ' min · writes ' + run.writes
                         : '~' + run.seconds + 's · writes ' + run.writes));
    /* The one button that spends money and leaves the machine. Marked, because
     * "everything here is keyless and free" is a claim the page makes
     * elsewhere and this is its single exception. */
    if (run.live) {
      card.classList.add('live');
      card.appendChild(el('span', 'run-live',
        'LIVE · calls the Sarvam API · needs SARVAM_API_KEY'));
    }
    card.addEventListener('click', function () { startRun(run.id, card); });
    host.appendChild(card);
  });
}

function startRun(runId, card) {
  var log = document.getElementById('run-log');
  log.hidden = false;
  log.textContent = '';
  logCursor = 0;
  document.getElementById('run-bar').style.width = '0%';
  setRunStatus('running');
  document.querySelectorAll('.run-card').forEach(function (b) { b.disabled = true; });
  if (card) card.classList.add('active');

  fetch('/api/run/' + encodeURIComponent(runId), { method: 'POST' })
    .then(function (r) { return r.json(); })
    .then(function (body) {
      if (body.error) {
        log.textContent = body.error;
        setRunStatus('failed');
        document.querySelectorAll('.run-card').forEach(function (b) { b.disabled = false; });
        return;
      }
      pollLog();
    })
    .catch(function (e) {
      log.textContent = 'could not reach the run server: ' + e.message +
        '\nStart it with:  python -m pramaan.report.server';
      setRunStatus('failed');
      document.querySelectorAll('.run-card').forEach(function (b) { b.disabled = false; });
    });
}

function pollLog() {
  fetch('/api/log?after=' + logCursor, { cache: 'no-store' })
    .then(function (r) { return r.json(); })
    .then(function (state) {
      var log = document.getElementById('run-log');
      var lastStage = null;
      state.lines.forEach(function (line) {
        log.appendChild(renderLogLine(line));
        if (line.kind) lastStage = line.kind;
      });
      if (state.lines.length) {
        logCursor = state.total;
        log.scrollTop = log.scrollHeight;
      }
      if (lastStage && STAGE_LABEL[lastStage]) {
        document.getElementById('run-stage').textContent = STAGE_LABEL[lastStage];
      }
      if (state.elapsed !== null && state.elapsed !== undefined) {
        document.getElementById('run-elapsed').textContent = state.elapsed + 's elapsed';
      }

      /* The bar is honest about being an estimate: it tracks lines emitted
       * against a nominal ceiling, because the CLI reports no percentage and
       * inventing one from elapsed time would stall visibly at the bootstrap. */
      var pct = state.status === 'running'
        ? Math.min(95, 4 * state.total)
        : 100;
      document.getElementById('run-bar').style.width = pct + '%';

      if (state.status === 'running') {
        pollTimer = setTimeout(pollLog, 400);
        return;
      }

      setRunStatus(state.status);
      document.querySelectorAll('.run-card').forEach(function (b) {
        b.disabled = false;
        b.classList.remove('active');
      });
      if (state.status === 'done') {
        document.getElementById('run-stage').textContent =
          'complete — reloading the snapshot this run just wrote';
        reloadEverything();
      }
    })
    .catch(function () { pollTimer = setTimeout(pollLog, 1500); });
}

/* Re-fetch and re-render, so the tiles visibly change to the run's own numbers
 * rather than needing a page reload the viewer has to be told about. */
function reloadEverything() {
  fetch(SOURCE + '?t=' + Date.now(), { cache: 'no-store' })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      ['provenance', 'contrast-tiles', 'arm-tiles', 'gate-decisions',
       'gate-rules', 'action-mix', 'cause-mix', 'categories', 'why-panels',
       'rows-summary', 'rows-table'].forEach(function (id) {
        var node = document.getElementById(id);
        if (node) node.textContent = '';
      });
      render(data);
      /* The row browser holds a stale CSV, so make it reload rather than
       * silently showing the previous run's rows next to new tiles. */
      allRows = [];
      filteredRows = [];
      document.getElementById('rows-ui').hidden = true;
      var button = document.getElementById('rows-load');
      button.disabled = false;
      button.textContent = 'Load ' + count(data.provenance.rows_exported) + ' rows';
      document.getElementById('rows-status').textContent =
        data.provenance.rows_file + ' — regenerated by this run';
      flashUpdated();
    })
    .catch(function (e) {
      document.getElementById('run-stage').textContent =
        'run finished but the snapshot could not be reloaded: ' + e.message;
    });
}

function flashUpdated() {
  ['headline-section', 'ledger-section', 'category-section'].forEach(function (id) {
    var node = document.getElementById(id);
    if (!node) return;
    node.classList.remove('updated');
    /* Reflow so the animation restarts on a second run. */
    void node.offsetWidth;
    node.classList.add('updated');
  });
}

function initRunner() {
  fetch('/api/runs', { cache: 'no-store' })
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (body) {
      renderRunButtons(body.runs);
      show('run-section');
      show('replay-section');
      initReplay();
      initLiveCall();
      /* A run may already be in flight -- the page was reloaded mid-run, or
       * started from the terminal. Without this the panel showed "idle" over a
       * live subprocess and the output was lost for the rest of the run. */
      return fetch('/api/log?after=0', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (state) {
          if (state.status === 'running') {
            document.getElementById('run-log').hidden = false;
            setRunStatus('running');
            document.querySelectorAll('.run-card').forEach(function (b) {
              b.disabled = true;
            });
            logCursor = 0;
            pollLog();
          }
        });
    })
    .catch(function () {
      /* No run server: stay a static report. Nothing is broken, so nothing is
       * announced beyond a hint where the buttons would have been -- but the
       * hint lives inside the run section, so that has to be revealed too, with
       * the meter and log left hidden because there is nothing to meter. */
      var hint = document.getElementById('run-hint');
      if (!hint) return;
      hint.hidden = false;
      show('run-section');
      var state = document.querySelector('#run-section .run-state');
      if (state) state.hidden = true;
    });
}

/* -- replay ------------------------------------------------------------ */

var replayTimer = null;
var replayIndex = 0;
var replayRows = [];
var replayTotals = null;
var FEED_MAX = 14;

function initReplay() {
  document.getElementById('replay-start').addEventListener('click', startReplay);
  document.getElementById('replay-stop').addEventListener('click', stopReplay);
}

function startReplay() {
  stopReplay();
  document.getElementById('replay-feed').textContent = '';
  replayIndex = 0;
  replayTotals = {
    events: 0, allow: 0, amend: 0, reject: 0,
    acted: 0, recovered: 0, at_risk: 0, recovered_paise: 0,
  };

  var proceed = function () {
    /* Order by seq so the replay is the chain's own order -- the sequence the
     * run actually produced, not a sort of this page's choosing. */
    replayRows = allRows.slice().sort(function (a, b) {
      return Number(a.seq) - Number(b.seq);
    });
    document.getElementById('replay-start').disabled = true;
    document.getElementById('replay-stop').disabled = false;
    tickReplay();
  };

  if (allRows.length) { proceed(); return; }
  document.getElementById('replay-progress').textContent = 'loading rows…';
  fetch('../build/actions.csv?t=' + Date.now(), { cache: 'no-store' })
    .then(function (r) { return r.text(); })
    .then(function (text) { allRows = parseCSV(text); proceed(); })
    .catch(function (e) {
      document.getElementById('replay-progress').textContent =
        'could not load actions.csv (' + e.message + ') — run `make dashboard`';
    });
}

function stopReplay() {
  if (replayTimer) { clearTimeout(replayTimer); replayTimer = null; }
  document.getElementById('replay-start').disabled = false;
  document.getElementById('replay-stop').disabled = true;
}

function tickReplay() {
  var speed = Number(document.getElementById('replay-speed').value) || 80;
  var row = replayRows[replayIndex];
  if (!row) {
    document.getElementById('replay-progress').textContent =
      'replay complete — ' + count(replayIndex) + ' events';
    stopReplay();
    return;
  }

  replayTotals.events++;
  if (row.gate_decision === 'ALLOW') replayTotals.allow++;
  if (row.gate_decision === 'AMEND') replayTotals.amend++;
  if (row.gate_decision === 'REJECT') replayTotals.reject++;
  if (row.action && row.action !== 'none' && row.action !== 'ACT_WAIT') replayTotals.acted++;
  if (row.recovered === 'true') replayTotals.recovered++;
  replayTotals.at_risk += Number(row.amount_at_risk_paise) || 0;
  replayTotals.recovered_paise += Number(row.amount_recovered_paise) || 0;

  appendFeedRow(row);
  renderReplayCounters();
  document.getElementById('replay-progress').textContent =
    count(replayIndex + 1) + ' of ' + count(replayRows.length);

  replayIndex++;
  replayTimer = setTimeout(tickReplay, speed);
}

function appendFeedRow(row) {
  var feed = document.getElementById('replay-feed');
  var verdict = (row.gate_decision || 'none').toLowerCase();
  var card = el('div', 'feed-row ' + verdict);

  card.appendChild(el('span', 'feed-seq', '#' + row.seq));
  card.appendChild(el('span', 'feed-arm', row.arm));
  card.appendChild(el('span', 'feed-type', row.source_type));
  card.appendChild(el('span', 'feed-reason', row.reason_class));
  card.appendChild(el('span', 'feed-amount', rupees(Number(row.amount_at_risk_paise) || 0)));

  var ruling = el('span', 'feed-verdict ' + verdict,
    (row.gate_decision || '—') + (row.gate_rule ? ' · ' + row.gate_rule : ''));
  card.appendChild(ruling);

  card.appendChild(el('span', 'feed-action', row.action || 'none'));

  if (row.recovered === 'true') {
    card.appendChild(el('span', 'feed-recovered',
      '✓ ' + rupees(Number(row.amount_recovered_paise) || 0) +
      (row.cause ? ' (' + row.cause + ')' : '')));
  } else if (row.exception_reason) {
    var why = el('span', 'feed-why', row.exception_reason);
    why.title = row.exception_reason;
    card.appendChild(why);
  }

  feed.insertBefore(card, feed.firstChild);
  while (feed.childNodes.length > FEED_MAX) feed.removeChild(feed.lastChild);
}

function renderReplayCounters() {
  var host = document.getElementById('replay-counters');
  host.textContent = '';
  var t = replayTotals;
  [
    ['events', count(t.events)],
    ['allow', count(t.allow)],
    ['amend', count(t.amend)],
    ['reject', count(t.reject)],
    ['acted', count(t.acted)],
    ['recovered', count(t.recovered)],
    ['at risk', rupees(t.at_risk)],
    ['recovered value', rupees(t.recovered_paise)],
  ].forEach(function (pair) {
    var box = el('div', 'counter');
    box.appendChild(el('span', 'counter-label', pair[0]));
    box.appendChild(el('span', 'counter-value', pair[1]));
    host.appendChild(box);
  });
}

/* -- the planner ------------------------------------------------------- */

function renderPlans(plans) {
  if (!plans) return false;
  var c = plans.counts;

  var summary = document.getElementById('plans-summary');
  summary.textContent = '';
  [
    ['distinct signatures', count(plans.signatures)],
    ['model was called', count(plans.called)],
    ['plans the model wrote', count(c.llm_authored)],
    ['replies unusable', count(c.unreadable)],
    ['no cached reply', count(c.no_call)],
  ].forEach(function (pair) {
    var wrap = el('div', 'pair');
    wrap.appendChild(el('dt', null, pair[0]));
    wrap.appendChild(el('dd', null, pair[1]));
    summary.appendChild(wrap);
  });

  /* Three buckets, in pipeline order: never asked, asked and unusable, asked
   * and used. Two would hide the middle one, which is the interesting number. */
  renderBars('plans-authorship', {
    'no cached reply': c.no_call,
    'reply unusable': c.unreadable,
    'model authored': c.llm_authored,
  }, function (name) {
    if (name === 'model authored') return 'allow';
    if (name === 'reply unusable') return 'amend';
    return null;
  });

  var rate = document.getElementById('plans-rate');
  rate.textContent = plans.usable_reply_rate === null
    ? 'The model was never called on this batch.'
    : 'Of the ' + count(plans.called) + ' signatures where the model was asked, ' +
      percent(plans.usable_reply_rate) + ' produced a plan the schema accepted. ' +
      'The rest fell back to the deterministic reason-class table — the same one ' +
      'arm B uses, so a cache miss cannot become a third, untested policy. A ' +
      'planner that degrades silently looks identical to one that never degrades, ' +
      'which is why this is counted rather than assumed.';

  /* Both groups are the same actions with different counts, so unlabelled they
   * read as one list with duplicate rows -- ACT_RETRY 18 above ACT_RETRY 73 and
   * nothing to say which is which. */
  document.getElementById('plans-label-llm').textContent =
    'model authored — ' + count(c.llm_authored) + ' signatures';
  document.getElementById('plans-label-fb').textContent =
    'deterministic table — ' + count(c.no_call + c.unreadable) + ' signatures';
  renderBars('plans-actions-llm', plans.first_step_actions.llm, null);
  renderBars('plans-actions-fb', plans.first_step_actions.fallback, null);

  return true;
}

/* -- the counterfactual ------------------------------------------------ */

function renderCounterfactual(headline) {
  if (!headline || !headline.summaries) return false;
  var arms = headline.summaries;
  /* Written by an older execute run, before the field existed. Absent is
   * reported by omitting the section rather than by drawing zeroes. */
  if (arms.A && arms.A.would_recover_unaided === undefined) return false;

  var host = document.getElementById('counterfactual-tiles');
  host.textContent = '';
  Object.keys(arms).sort().forEach(function (name) {
    var arm = arms[name];
    var unaided = arm.would_recover_unaided;
    var net = arm.recovered - unaided;
    host.appendChild(tile(
      'Arm ' + name + ' — what the action added',
      (net > 0 ? '+' : '') + count(net),
      count(arm.recovered) + ' recovered\n' +
      count(unaided) + ' would have recovered unaided\n' +
      (arm.recovered ? percent(unaided / arm.recovered) + ' of them' : '—') +
      ' needed nothing',
      net > 0 ? 'pos' : null,
      { tag: name === 'A' ? 'holdout' : 'observed', full: name !== 'A',
        text: name === 'A'
          ? 'takes no action, so recovered and unaided are the same number by ' +
            'construction — the estimator returning +0 here is the sanity check'
          : 'recoveries minus the ones the latent world says needed no help' }
    ));
  });

  var c = arms.C;
  document.getElementById('counterfactual-foot').textContent = c
    ? 'Arm C recovered ' + count(c.recovered) + ' events and ' +
      count(c.would_recover_unaided) + ' of those would have recovered on their ' +
      'own — so the real contribution is ' +
      count(c.recovered - c.would_recover_unaided) + ' events, not ' +
      count(c.recovered) + '. Arm A shows +0 by construction, which is how you ' +
      'know the estimator is not inventing effects.'
    : '';
  return true;
}

/* -- the canary -------------------------------------------------------- */

function renderCanary(canary) {
  if (!canary) return false;
  var host = document.getElementById('canary-card');
  host.textContent = '';

  var verdict = (canary.verdict || '').toLowerCase();
  var head = el('div', 'canary-head');
  head.appendChild(el('span', 'canary-verdict ' + verdict, canary.verdict || '—'));
  head.appendChild(el('span', 'why-ref',
    'seq ' + canary.seq + ' · ' + canary.source));
  host.appendChild(head);

  var table = el('dl', 'fields');
  [
    ['rate shift segment', canary.observed_rate_segment, canary.truth_rate_segment,
     canary.rate_matches],
    ['mix shift segment', canary.observed_mix_segment, canary.truth_mix_segment,
     canary.mix_matches],
  ].forEach(function (row) {
    table.appendChild(el('dt', null, row[0]));
    var dd = el('dd', null);
    dd.appendChild(el('span', null, 'observed ' + row[1] + ' · truth ' + row[2] + '  '));
    dd.appendChild(el('span', row[3] ? 'true' : 'false', row[3] ? 'match' : 'MISMATCH'));
    table.appendChild(dd);
  });
  host.appendChild(table);

  /* Absence of a retraction is a result, not a blank. Most canary checks
   * confirm; saying so is what makes the refutation path meaningful when it
   * does fire. */
  var note = el('p', 'canary-note');
  note.textContent = canary.retraction
    ? 'A RETRACTION was written at seq ' + canary.retraction.seq +
      ' — the system withdrew its own verdict (' +
      canary.retraction.retracted_verdict + ') against contradicting evidence.'
    : 'No RETRACTION row: this verdict was confirmed, not refuted. The ' +
      'refutation path is exercised separately in tests/test_canary.py — ' +
      'confirmation is the common case and is recorded just as deliberately.';
  host.appendChild(note);
  return true;
}

/* -- the voice call ---------------------------------------------------- */

function renderVoice(voice) {
  if (!voice) return false;

  var audio = document.getElementById('voice-audio');
  if (voice.audio) {
    audio.src = voice.audio;
    audio.hidden = false;
  } else {
    audio.hidden = true;
  }

  var meta = document.getElementById('voice-meta');
  meta.textContent = '';
  [
    ['envelope', (voice.gate_decision || '—') +
                 (voice.gate_rule ? ' · ' + voice.gate_rule : '')],
    ['call placed', voice.call_placed],
    /* R10's actual requirement, and the reason it is a boolean on the row
     * rather than a claim in a README: the disclosure has to be the *first*
     * utterance, not merely present somewhere in the call. */
    ['AI disclosure first', voice.disclosure_first],
    ['stood down (S7)', voice.stood_down],
    ['turns', voice.turns.length],
    ['audio', voice.audio_bytes
      ? Math.round(voice.audio_bytes / 1024) + ' KB, Sarvam TTS'
      : 'not synthesised — run voice-live'],
  ].forEach(function (pair) {
    meta.appendChild(el('dt', null, pair[0]));
    var dd = el('dd', null);
    renderField(dd, pair[1]);
    meta.appendChild(dd);
  });

  var turns = document.getElementById('voice-turns');
  turns.textContent = '';
  voice.turns.forEach(function (turn, index) {
    var row = el('div', 'turn ' + (turn.speaker || 'agent'));
    row.appendChild(el('span', 'turn-who', turn.speaker || '—'));
    row.appendChild(el('span', 'turn-text', turn.text || ''));
    /* The first agent line is the R10 disclosure. Marking it makes the rule
     * checkable by eye instead of taken on trust. */
    if (index === 0 && voice.disclosure_first) {
      row.appendChild(el('span', 'turn-flag', 'R10 disclosure'));
    }
    turns.appendChild(row);
  });

  var promiseHost = document.getElementById('voice-promise');
  promiseHost.textContent = '';
  if (voice.promise) {
    var p = voice.promise;
    promiseHost.appendChild(el('div', 'promise-label',
      'Extracted from speech into the ledger'));
    promiseHost.appendChild(el('div', 'promise-quote',
      '“' + (p.verbatim || '') + '”'));
    var facts = el('dl', 'fields');
    [
      ['state', p.state],
      ['promised_date', p.promised_date],
      ['confidence', p.confidence],
      ['channel', p.channel],
      ['amount_paise', p.amount_paise],
    ].forEach(function (pair) {
      facts.appendChild(el('dt', null, pair[0]));
      var dd = el('dd', null);
      renderField(dd, pair[1]);
      facts.appendChild(dd);
    });
    promiseHost.appendChild(facts);
  }

  return true;
}

/* -- the live call: a person talks, the agent answers ------------------ */
/*
 * Records from the microphone, posts WAV to /api/call/turn, plays what comes
 * back. Deliberately WAV rather than MediaRecorder's webm/opus: Sarvam's STT
 * endpoint is called with an audio/wav part, and shipping a container it may or
 * may not accept -- with no ffmpeg on the machine to convert -- is a failure
 * that would only show up live. Sixteen-bit PCM at the context's own rate is
 * about forty lines and has no such question hanging over it.
 */

var mediaStream = null;
var audioCtx = null;
var recorderNode = null;
var silence = null;
var recordedChunks = [];
var recording = false;

function wavBlob(chunks, sampleRate) {
  var length = chunks.reduce(function (n, c) { return n + c.length; }, 0);
  var samples = new Float32Array(length);
  var offset = 0;
  chunks.forEach(function (c) { samples.set(c, offset); offset += c.length; });

  var buffer = new ArrayBuffer(44 + samples.length * 2);
  var view = new DataView(buffer);
  function writeString(pos, text) {
    for (var i = 0; i < text.length; i++) view.setUint8(pos + i, text.charCodeAt(i));
  }
  writeString(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeString(8, 'WAVE');
  writeString(12, 'fmt ');
  view.setUint32(16, 16, true);        // PCM chunk size
  view.setUint16(20, 1, true);         // format = PCM
  view.setUint16(22, 1, true);         // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);         // block align
  view.setUint16(34, 16, true);        // bits per sample
  writeString(36, 'data');
  view.setUint32(40, samples.length * 2, true);

  for (var j = 0; j < samples.length; j++) {
    var s = Math.max(-1, Math.min(1, samples[j]));
    view.setInt16(44 + j * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([view], { type: 'audio/wav' });
}

function callStatus(text) {
  document.getElementById('call-status').textContent = text;
}

/*
 * The microphone is acquired once, when the call starts, and held for the
 * whole call. The obvious shape -- ask for it inside mousedown -- has a race
 * you only hit on camera: getUserMedia resolves asynchronously, so a short
 * press releases the button before the stream exists, `stopRecording` runs
 * against zero chunks, and the turn is silently lost. Arming up front also
 * means the browser's permission prompt appears once, at a moment the operator
 * expects it, instead of interrupting the first thing they try to say.
 */
function armMicrophone() {
  if (audioCtx) return Promise.resolve();
  return navigator.mediaDevices.getUserMedia({ audio: true })
    .then(function (stream) {
      mediaStream = stream;
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      var source = audioCtx.createMediaStreamSource(stream);
      recorderNode = audioCtx.createScriptProcessor(4096, 1, 1);
      recorderNode.onaudioprocess = function (e) {
        /* Fires for the whole call; only a held button makes it keep anything. */
        if (recording) recordedChunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
      };
      /* A ScriptProcessor only gets pulled if it reaches the destination, but
       * routing a live microphone to the speakers for the length of a call is
       * a feedback loop on a laptop. So it terminates in a muted gain node:
       * the graph still runs, nothing is ever heard. */
      silence = audioCtx.createGain();
      silence.gain.value = 0;
      source.connect(recorderNode);
      recorderNode.connect(silence);
      silence.connect(audioCtx.destination);
    });
}

function releaseMicrophone() {
  recording = false;
  recordedChunks = [];
  if (recorderNode) { recorderNode.disconnect(); recorderNode = null; }
  if (silence) { silence.disconnect(); silence = null; }
  if (mediaStream) { mediaStream.getTracks().forEach(function (t) { t.stop(); }); mediaStream = null; }
  if (audioCtx) { audioCtx.close(); audioCtx = null; }
}

function playReply(data) {
  if (!data.audio_b64) return;
  var audio = document.getElementById('call-audio');
  audio.src = 'data:audio/mpeg;base64,' + data.audio_b64;
  audio.hidden = false;
  audio.play().catch(function () { /* autoplay may need a gesture */ });
}

/* -- the agent opens ---------------------------------------------------- */
/*
 * A separate endpoint from a turn, because the agent's first utterance answers
 * nothing -- it is the R10 disclosure, and R10's requirement is that it comes
 * first, not that it appears somewhere. Waiting for the human to speak into
 * silence inverts that and makes the demo feel broken besides.
 */
function startCall() {
  var startBtn = document.getElementById('call-start');
  var talk = document.getElementById('call-talk');
  startBtn.disabled = true;
  talk.disabled = true;
  callStatus('asking for the microphone, then dialling…');

  armMicrophone()
    .then(function () {
      callStatus('the agent is introducing itself (Sarvam TTS)…');
      return fetch('/api/call/start', { method: 'POST' }).then(function (r) { return r.json(); });
    })
    .then(function (data) {
      if (data.error) {
        callStatus('could not start: ' + data.error);
        startBtn.disabled = false;
        return;
      }
      renderCallTurns(data);
      playReply(data);
      talk.disabled = false;
      talk.classList.add('primary');
      callStatus('the agent has spoken — hold the button and reply');
    })
    .catch(function (e) {
      callStatus('could not start: ' + e.message);
      startBtn.disabled = false;
    });
}

function startRecording() {
  if (recording) return;
  if (!audioCtx) { callStatus('press Start call first'); return; }
  recordedChunks = [];
  recording = true;
  document.getElementById('call-talk').classList.add('recording');
  callStatus('listening…');
}

function stopRecording() {
  if (!recording) return;
  recording = false;
  document.getElementById('call-talk').classList.remove('recording');

  var rate = audioCtx ? audioCtx.sampleRate : 44100;
  if (!recordedChunks.length) { callStatus('nothing recorded — hold it a moment longer'); return; }
  var blob = wavBlob(recordedChunks, rate);
  recordedChunks = [];
  callStatus('transcribing with Sarvam, then asking the model…');

  fetch('/api/call/turn', {
    method: 'POST',
    headers: { 'Content-Type': 'audio/wav' },
    body: blob,
  })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.error) { callStatus('failed: ' + data.error); return; }
      renderCallTurns(data);
      playReply(data);
      callStatus(data.from_llm
        ? 'replied via the turn-policy LLM' +
          (data.llm_call_ids.length ? ' (' + data.llm_call_ids[0].slice(0, 12) + ')' : '')
        /* Said plainly rather than passed off as the model: generate_reply
         * degrades to the deterministic turn on any LLM failure, and a demo
         * that hides which one answered is the exact confusion this section
         * was built to remove. */
        : 'replied from the deterministic fallback — the LLM call did not land');
    })
    .catch(function (e) { callStatus('failed: ' + e.message); });
}

function renderCallTurns(data) {
  var host = document.getElementById('call-turns');
  host.textContent = '';
  (data.turns || []).forEach(function (turn, index) {
    var row = el('div', 'turn ' + (turn.speaker || 'agent'));
    row.appendChild(el('span', 'turn-who', turn.speaker));
    row.appendChild(el('span', 'turn-text', turn.text));
    if (index === 0) row.appendChild(el('span', 'turn-flag', 'R10 disclosure'));
    host.appendChild(row);
  });

  var ruling = document.getElementById('call-ruling');
  if (data.ruling) {
    ruling.hidden = false;
    ruling.textContent = 'envelope preflight: ' + data.ruling.verdict +
      ' · ' + data.ruling.rule_id + ' — ' + (data.ruling.reason || '');
  }

  var promise = document.getElementById('call-promise');
  if (data.promise) {
    promise.hidden = false;
    promise.textContent = '';
    promise.appendChild(el('div', 'promise-label',
      'Promise extracted from what you said, into the state machine'));
    promise.appendChild(el('div', 'promise-quote', '“' + data.promise.verbatim + '”'));
    promise.appendChild(el('div', 'rows-status',
      'promised_date ' + data.promise.promised_date +
      ' · confidence ' + data.promise.confidence +
      ' · state ' + data.promise.state));
  }
}

function initLiveCall() {
  var talk = document.getElementById('call-talk');
  /* Push-to-talk: no silence detection, no guessing when a sentence ended.
   * The operator decides, which on camera is the difference between a demo
   * that works and one that cuts you off mid-word. */
  talk.addEventListener('mousedown', startRecording);
  talk.addEventListener('mouseup', stopRecording);
  talk.addEventListener('mouseleave', function () { if (recording) stopRecording(); });
  talk.addEventListener('touchstart', function (e) { e.preventDefault(); startRecording(); });
  talk.addEventListener('touchend', function (e) { e.preventDefault(); stopRecording(); });

  document.getElementById('call-start').addEventListener('click', startCall);

  document.getElementById('call-reset').addEventListener('click', function () {
    releaseMicrophone();
    talk.disabled = true;
    talk.classList.remove('primary', 'recording');
    document.getElementById('call-start').disabled = false;
    fetch('/api/call/reset', { method: 'POST' })
      .then(function () {
        document.getElementById('call-turns').textContent = '';
        document.getElementById('call-ruling').hidden = true;
        document.getElementById('call-promise').hidden = true;
        document.getElementById('call-audio').hidden = true;
        callStatus('call ended — press Start call to dial again');
      });
  });

  show('live-call-section');
}

/* -- failure ----------------------------------------------------------- */

function fail(title, detail, commands) {
  var box = document.getElementById('error');
  box.hidden = false;
  box.appendChild(el('h2', null, title));
  box.appendChild(el('p', null, detail));
  if (commands) {
    var pre = el('pre', null, commands);
    box.appendChild(pre);
  }
}

/* -- entry ------------------------------------------------------------- */

/* One render pass over a snapshot. Extracted so a completed run can re-render
 * the page in place -- the tiles change to the numbers the run just produced,
 * with no reload for the viewer to be told about. */
function render(data) {
    /* Fail loudly on drift rather than rendering a page of blanks. A dashboard
     * quietly showing zeroes against a stale file is worse than one that says
     * it is stale, because only one of those two is noticed on demo day. */
    if (data.schema_version !== SCHEMA_VERSION) {
      fail('Snapshot schema mismatch',
        'This page renders schema_version ' + SCHEMA_VERSION + '; the file is ' +
        data.schema_version + '. Regenerate it.',
        'make dashboard');
      return;
    }

    renderProvenance(data.provenance);

    if (data.headline) {
      renderContrasts(data.headline);
      show('headline-section');
    } else {
      /* Absent is reported, not filled in. The ledger's own arm-to-arm
       * difference is a different quantity from the bootstrapped contrast over
       * the actioned subset; showing it here under the headline's label is the
       * exact category error the README warns about. */
      fail('No headline metrics in this snapshot',
        'The contrast tiles need the BatchMetrics the execute run writes. ' +
        'The observed per-arm numbers below are still real, but they are not ' +
        'the headline and are not labelled as it.',
        'make execute-full && make dashboard');
    }

    renderArms(data.ledger.arms);
    renderBars('gate-decisions', data.ledger.gate_decisions, function (name) {
      return name.toLowerCase();
    });
    renderBars('gate-rules', data.ledger.gate_rules, null);

    /* One action mix across arms would pool the holdout's inaction with the
     * planner's actions. Arm C is the one that acts, so its mix is the one
     * worth drawing. */
    var acting = data.ledger.arms.C || data.ledger.arms.B;
    if (acting) {
      renderBars('action-mix', acting.action_counts, null);
      renderBars('cause-mix', acting.causes, null);
    }
    show('ledger-section');

    renderCategories(data.by_category);
    show('category-section');

    if (renderDetected(data.detected, data.provenance)) show('detected-section');

    initRows(data.provenance);

    if (renderPlans(data.plans)) show('plans-section');

    if (renderCounterfactual(data.headline)) show('counterfactual-section');

    if (renderCanary(data.canary)) show('canary-section');

    if (renderVoice(data.voice)) show('voice-section');

    renderWhy(data.why);
    show('why-section');
}

fetch(SOURCE, { cache: 'no-store' })
  .then(function (response) {
    if (!response.ok) throw new Error('HTTP ' + response.status);
    return response.json();
  })
  .then(function (data) {
    /* Render failures get their own report.
     *
     * Without this they fell into the fetch chain's catch and were announced as
     * "Could not load ../build/dashboard.json" -- which sends the reader to
     * check the server and the file path when the file loaded perfectly and the
     * bug is in this script. An error message that points at the wrong
     * subsystem is worse than a stack trace. */
    try {
      render(data);
    } catch (error) {
      fail('The snapshot loaded, but rendering it failed',
        'This is a bug in dashboard/app.js, not a missing or stale file — ' +
        SOURCE + ' fetched and parsed fine. ' + (error && error.message || error),
        (error && error.stack)
          ? String(error.stack).split(/\r?\n/).slice(0, 4).join('\n')
          : '');
      return;
    }
    /* Ask the server whether it can run anything. If it cannot -- because the
     * page is being served by a plain `http.server` -- the run and replay
     * sections stay hidden and this remains the static report it was. */
    initRunner();
  })
  .catch(function (error) {
    fail('Could not load ' + SOURCE,
      'Two things cause this. Either the snapshot has not been generated yet, ' +
      'or the page was opened as a file:// URL — Chrome blocks fetch() ' +
      'there, so it needs a static server rooted at the repo, not at dashboard/. ' +
      '(' + error.message + ')',
      'python -m pramaan.report.server\n# then open http://127.0.0.1:8000/dashboard/');
  });
