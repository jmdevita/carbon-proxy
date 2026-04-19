const CIRC = 2 * Math.PI * 62;
const MAX_POWER_POINTS = 120; // 2 minutes at 1s intervals

// -- State --
let activeRange = 'today';
let activeSource = '';
let liveChartEnabled = true;

// -- Helpers --
function esc(s) { const e = document.createElement('span'); e.textContent = s; return e.innerHTML; }
function safeUrl(u) { try { const x = new URL(u); return (x.protocol==='https:'||x.protocol==='http:') ? x.href : ''; } catch(e) { return ''; } }
function fmt(n, d=2) { return n == null || isNaN(n) ? '0' : Number(n).toFixed(d); }
function fmtShort(n) { if (n >= 1000) return (n/1000).toFixed(1)+'k'; return n < 0.01 && n > 0 ? n.toExponential(1) : fmt(n, n < 1 ? 4 : 1); }
function fmtTime(iso) { if (!iso) return '--'; return new Date(iso).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}); }
function fmtDur(ms) { return ms < 1000 ? ms+'ms' : (ms/1000).toFixed(1)+'s'; }
function srcTag(s) { return `<span class="tag tag-${s||'none'}">${s||'none'}</span>`; }

function setGauge(id, pct) {
  const offset = CIRC * (1 - Math.min(1, pct));
  document.getElementById(id).setAttribute('stroke-dashoffset', offset);
}

function logPct(val, base) { return val <= 0 ? 0 : Math.min(1, Math.log(1+val) / Math.log(1+base)); }

function toggleSection(id) {
  const el = document.getElementById(id);
  const arrow = document.getElementById(id + '-arrow');
  el.classList.toggle('collapsed');
  arrow.style.transform = el.classList.contains('collapsed') ? 'rotate(-90deg)' : '';
}

// -- Filters --
function getTimeRange() {
  const now = new Date();
  if (activeRange === 'today') {
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return { since: start.toISOString(), until: null };
  } else if (activeRange === '7d') {
    const start = new Date(now.getTime() - 7*24*60*60*1000);
    return { since: start.toISOString(), until: null };
  } else if (activeRange === '30d') {
    const start = new Date(now.getTime() - 30*24*60*60*1000);
    return { since: start.toISOString(), until: null };
  }
  return { since: null, until: null };
}

function buildQuery(extra={}) {
  const range = getTimeRange();
  const params = new URLSearchParams();
  if (range.since) params.set('since', range.since);
  if (range.until) params.set('until', range.until);
  if (activeSource) params.set('source', activeSource);
  for (const [k,v] of Object.entries(extra)) {
    if (v != null) params.set(k, v);
  }
  const qs = params.toString();
  return qs ? '?' + qs : '';
}

// Filter event listeners
document.getElementById('timeFilter').addEventListener('click', (e) => {
  const btn = e.target.closest('.filter-btn');
  if (!btn) return;
  document.querySelectorAll('#timeFilter .filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  activeRange = btn.dataset.range;
  refreshAll();
});

document.getElementById('sourceFilter').addEventListener('change', (e) => {
  activeSource = e.target.value;
  refreshAll();
});

// -- Charts --
const chartDefaults = {
  responsive: true,
  maintainAspectRatio: false,
  animation: { duration: 300 },
  plugins: { legend: { labels: { color: '#888', boxWidth: 12, padding: 16, font: { size: 11 } } } },
  scales: {
    x: { ticks: { color: '#888', font: { size: 10 } }, grid: { color: '#2e2e2e' } },
    y: { ticks: { color: '#888', font: { size: 10 } }, grid: { color: '#2e2e2e' }, beginAtZero: true },
  },
};

// Power chart
const powerCtx = document.getElementById('powerChart').getContext('2d');
const powerChart = new Chart(powerCtx, {
  type: 'line',
  data: {
    labels: [],
    datasets: [
      { label: 'CPU (W)', data: [], borderColor: '#6bb3ff', backgroundColor: 'rgba(107,179,255,0.1)', borderWidth: 1.5, pointRadius: 0, fill: true, tension: 0.3 },
      { label: 'GPU (W)', data: [], borderColor: '#bffb4f', backgroundColor: 'rgba(191,251,79,0.1)', borderWidth: 1.5, pointRadius: 0, fill: true, tension: 0.3 },
      { label: 'Total (W)', data: [], borderColor: '#ff6b6b', backgroundColor: 'rgba(255,107,107,0.05)', borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.3, borderDash: [4,2] },
    ],
  },
  options: {
    ...chartDefaults,
    plugins: { ...chartDefaults.plugins, title: { display: false } },
    scales: {
      ...chartDefaults.scales,
      x: { ...chartDefaults.scales.x, display: true, ticks: { ...chartDefaults.scales.x.ticks, maxTicksLimit: 8 } },
      y: { ...chartDefaults.scales.y, title: { display: true, text: 'Watts', color: '#888', font: { size: 10 } } },
    },
  },
});

// Daily chart
const dailyCtx = document.getElementById('dailyChart').getContext('2d');
const dailyChart = new Chart(dailyCtx, {
  type: 'bar',
  data: { labels: [], datasets: [] },
  options: {
    ...chartDefaults,
    animation: false,
    plugins: { ...chartDefaults.plugins, title: { display: false } },
    scales: {
      ...chartDefaults.scales,
      x: { ...chartDefaults.scales.x, stacked: true },
      y: { ...chartDefaults.scales.y, stacked: true, title: { display: true, text: 'g CO2', color: '#888', font: { size: 10 } } },
    },
  },
});

const SOURCE_COLORS = [
  '#bffb4f', '#6bb3ff', '#ff6b6b', '#ffb432', '#c084fc',
  '#34d399', '#f472b6', '#fbbf24', '#67e8f9', '#a78bfa',
];

function getSourceColor(index) {
  return SOURCE_COLORS[index % SOURCE_COLORS.length];
}

// -- Data refresh --
async function loadSources() {
  try {
    const sources = await fetch('/carbon/sources').then(r => r.json());
    const select = document.getElementById('sourceFilter');
    const current = select.value;
    select.innerHTML = '<option value="">All Sources</option>';
    sources.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s;
      opt.textContent = s;
      if (s === current) opt.selected = true;
      select.appendChild(opt);
    });
  } catch(e) {}
}

function toggleLiveChart() {
  liveChartEnabled = !liveChartEnabled;
  const btn = document.getElementById('liveToggle');
  btn.textContent = liveChartEnabled ? 'Pause' : 'Resume';
  btn.style.borderColor = liveChartEnabled ? '' : 'var(--primary)';
  btn.style.color = liveChartEnabled ? '' : 'var(--primary)';
}

function updatePowerChart(live) {
  if (!liveChartEnabled) return;

  const now = new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
  const cpu = live.cpu_watts || 0;
  const gpu = live.gpu_watts || 0;
  const total = live.total_watts || 0;

  powerChart.data.labels.push(now);
  powerChart.data.datasets[0].data.push(cpu);
  powerChart.data.datasets[1].data.push(gpu);
  powerChart.data.datasets[2].data.push(total);

  // Trim to max points
  if (powerChart.data.labels.length > MAX_POWER_POINTS) {
    powerChart.data.labels.shift();
    powerChart.data.datasets.forEach(ds => ds.data.shift());
  }

  powerChart.update('none');
}

function updateDailyChart(dailyData) {
  // Group by date, split by source
  const dateMap = {};
  const sourceSet = new Set();

  dailyData.forEach(row => {
    const date = row.date || 'unknown';
    const source = row.source || 'unknown';
    sourceSet.add(source);
    if (!dateMap[date]) dateMap[date] = {};
    dateMap[date][source] = (row.co2_kg || 0) * 1000; // convert to grams
  });

  const dates = Object.keys(dateMap).sort();
  const sources = Array.from(sourceSet).sort();

  dailyChart.data.labels = dates;
  dailyChart.data.datasets = sources.map((source, i) => ({
    label: source,
    data: dates.map(d => dateMap[d][source] || 0),
    backgroundColor: getSourceColor(i) + '88',
    borderColor: getSourceColor(i),
    borderWidth: 1,
    borderRadius: 3,
  }));

  dailyChart.update('none');
}

// Equivalents rotation state
let _eqSetIndex = 0;
let _latestEqSets = [];
let _latestEqGreen = false;
let _latestEqColor = 'var(--muted)';

function renderEquivSet() {
  if (!_latestEqSets.length) return;
  const set = _latestEqSets[_eqSetIndex % _latestEqSets.length];
  set.forEach((item, i) => {
    document.getElementById('eq'+i+'Icon').innerHTML = '<span class="mdi '+item.icon+'"></span>';
    document.getElementById('eq'+i+'Icon').style.color = _latestEqColor;
    document.getElementById('eq'+i+'Val').textContent = fmtEq(item.val || 0);
    document.getElementById('eq'+i+'Desc').textContent = _latestEqGreen ? item.green : item.red;
  });
}

function fmtEq(v) { return v < 0.01 && v > 0 ? '<0.01' : v >= 1000 ? (v/1000).toFixed(1)+'k' : fmt(v, v < 1 ? 2 : 1); }

async function refreshAll() {
  const [summary, equiv, balance, live, daily, reqs, offsets] = await Promise.all([
    fetch('/carbon/summary' + buildQuery()).then(r=>r.json()).catch(()=>({})),
    fetch('/carbon/equivalents' + buildQuery()).then(r=>r.json()).catch(()=>({})),
    fetch('/carbon/balance').then(r=>r.json()).catch(()=>({})),
    fetch('/carbon/live').then(r=>r.json()).catch(()=>({})),
    fetch('/carbon/daily' + buildQuery()).then(r=>r.json()).catch(()=>[]),
    fetch('/carbon/requests' + buildQuery({limit:50})).then(r=>r.json()).catch(()=>[]),
    fetch('/carbon/offsets?limit=20').then(r=>r.json()).catch(()=>[]),
  ]);

  // Show energy gauge only when carbon intensity is dynamic (electricityMap configured)
  const showEnergy = live.dynamic_carbon_intensity || false;
  document.getElementById('energyGauge').style.display = showEnergy ? '' : 'none';
  document.querySelector('.gauges').style.gap = showEnergy ? '24px' : '64px';

  // Gauges
  const kwh = summary.total_energy_kwh || 0;
  const co2 = summary.total_co2_grams || 0;
  const totalReqs = summary.total_requests || 0;
  if (showEnergy) {
    document.getElementById('gEnergy').textContent = fmtShort(kwh);
    setGauge('gaugeEnergy', logPct(kwh, 10));
  }
  document.getElementById('gEmissions').textContent = fmtShort(co2);
  document.getElementById('gRequests').textContent = totalReqs;
  setGauge('gaugeEmissions', logPct(co2, 1000));
  setGauge('gaugeRequests', logPct(totalReqs, 1000));

  // Equivalents (rotates between two sets, context-aware: red vs green)
  const isGreen = (balance.balance_grams || 0) <= 0 && (balance.total_offset_grams || 0) > 0;
  const eqColor = isGreen ? 'var(--primary)' : 'var(--red)';
  const eqSets = [
    [
      { icon: 'mdi-car', val: equiv.cars_per_year, green: 'cars off the road*', red: 'cars on the road*' },
      { icon: 'mdi-home-lightning-bolt', val: equiv.homes_energy_per_year, green: 'homes energy offset*', red: 'homes energy equivalent*' },
      { icon: 'mdi-airplane', val: equiv.flights_la_nyc, green: 'flights LA-NYC offset', red: 'flights LA-NYC equivalent' },
    ],
    [
      { icon: 'mdi-tree', val: equiv.trees_to_offset_yearly, green: 'trees/yr offset', red: 'trees/yr to neutralize' },
      { icon: 'mdi-cellphone', val: equiv.smartphone_charges, green: 'phone charges offset', red: 'phone charges equivalent' },
      { icon: 'mdi-magnify', val: equiv.google_searches, green: 'Google searches offset', red: 'Google searches equivalent' },
    ],
  ];
  _latestEqSets = eqSets;
  _latestEqGreen = isGreen;
  _latestEqColor = eqColor;
  renderEquivSet();

  // Balance
  const emitted = balance.total_co2_grams || 0;
  const off = balance.total_offset_grams || 0;
  const bal = balance.balance_grams || 0;
  const trees = balance.trees_planted || 0;
  const pct = emitted > 0 ? Math.min(100, (off / emitted) * 100) : 0;

  const balBar = document.getElementById('balanceBar');
  balBar.style.width = pct + '%';
  balBar.className = 'balance-bar-fill' + (bal > 0 ? ' deficit' : '');

  document.getElementById('balanceText').textContent =
    bal > 0 ? `${fmt(bal,2)}g remaining debt`
    : bal < 0 ? `Carbon negative (+${fmt(Math.abs(bal),2)}g surplus)`
    : 'Net zero';
  document.getElementById('balanceText').style.color = bal > 0 ? 'var(--red)' : 'var(--primary)';
  document.getElementById('balanceLeft').textContent =
    `${fmt(off,2)}g neutralized` + (trees > 0 ? ` | ${trees} trees` : '');
  document.getElementById('balanceRight').textContent = `${fmt(pct,0)}% neutralized`;

  // Live power
  document.getElementById('cpuW').textContent = fmt(live.cpu_watts,1) + 'W';
  document.getElementById('gpuW').textContent = fmt(live.gpu_watts,1) + 'W';
  document.getElementById('totalW').textContent = fmt(live.total_watts,1) + 'W';
  document.getElementById('pSource').textContent = `${live.power_source} (${live.cpu_method}/${live.gpu_method})`;
  document.getElementById('activeR').textContent = live.active_requests || 0;
  updatePowerChart(live);

  // Daily chart
  updateDailyChart(daily);

  // Daily table
  const dTb = document.getElementById('dailyTb');
  dTb.innerHTML = daily.length ? daily.slice(0,30).map(r => `<tr>
    <td>${esc(r.date||'')}</td><td>${esc(r.source)}</td><td>${r.requests}</td>
    <td>${(r.total_tokens||0).toLocaleString()}</td><td>${fmt(r.energy_kwh,4)}</td><td>${fmt(r.co2_kg,6)}</td>
  </tr>`).join('') : '<tr><td colspan="6" style="color:var(--muted);text-align:center;">No data</td></tr>';

  // Requests table
  const rTb = document.getElementById('reqTb');
  rTb.innerHTML = reqs.length ? reqs.map(r => `<tr>
    <td>${fmtTime(r.timestamp)}</td><td>${esc(r.source)}</td><td>${esc(r.model)}</td>
    <td>${r.tokens_in}</td><td>${r.tokens_out}</td><td>${fmtDur(r.duration_ms)}</td>
    <td>${fmt(r.energy_joules/1000,2)}kJ</td><td>${fmt(r.co2_grams,4)}g</td><td>${srcTag(r.power_source)}</td>
  </tr>`).join('') : '<tr><td colspan="9" style="color:var(--muted);text-align:center;">No data</td></tr>';

  // Offsets table
  const oTb = document.getElementById('offTb');
  oTb.innerHTML = offsets.length ? offsets.map(r => `<tr>
    <td>${fmtTime(r.timestamp)}</td><td>${esc(r.provider)}</td><td>${fmt(r.co2_grams_offset,2)}</td>
    <td>${r.cost_cents ? '$'+(r.cost_cents/100).toFixed(2) : '--'}</td>
    <td>${r.tree_count||'--'}</td>
    <td>${r.certificate_url&&safeUrl(r.certificate_url) ? '<a href="'+esc(safeUrl(r.certificate_url))+'" target="_blank" rel="noopener" style="color:var(--primary);">View</a>' : '--'}</td>
  </tr>`).join('') : '<tr><td colspan="6" style="color:var(--muted);text-align:center;">No offsets</td></tr>';
}

function getOffsetKey() {
  let key = localStorage.getItem('offset_api_key');
  if (!key) {
    key = prompt('Enter your offset API key:');
    if (key) localStorage.setItem('offset_api_key', key.trim());
  }
  return key ? key.trim() : null;
}

async function openOffsetModal() {
  const modal = document.getElementById('offsetModal');
  const input = document.getElementById('offsetAmount');
  const status = document.getElementById('offsetStatus');
  status.textContent = '';
  document.getElementById('quoteSection').style.display = 'none';
  document.getElementById('confirmOffsetBtn').style.display = 'none';
  document.getElementById('quoteBtn').style.display = '';

  // Pre-fill with current balance
  try {
    const r = await fetch('/carbon/balance');
    const bal = await r.json();
    const grams = Math.ceil(bal.balance_grams || 0);
    input.value = grams > 0 ? grams : '';
    if (grams <= 0) status.textContent = 'No carbon debt to offset.';
  } catch(e) { input.value = ''; }

  modal.classList.add('active');
  input.focus();

  // Auto-fetch quote if there's a balance
  if (input.value && parseFloat(input.value) > 0) fetchQuote();
}

function closeOffsetModal() {
  document.getElementById('offsetModal').classList.remove('active');
}

function clearQuote() {
  document.getElementById('quoteSection').style.display = 'none';
  document.getElementById('confirmOffsetBtn').style.display = 'none';
  document.getElementById('quoteBtn').style.display = '';
}

async function fetchQuote() {
  const grams = parseFloat(document.getElementById('offsetAmount').value);
  if (!grams || grams <= 0) return;

  const status = document.getElementById('offsetStatus');
  const quoteBtn = document.getElementById('quoteBtn');
  quoteBtn.disabled = true; quoteBtn.textContent = 'Quoting...';
  status.textContent = '';

  try {
    const r = await fetch('/carbon/quote?co2_grams=' + grams);
    const d = await r.json();

    if (!d.quotes || !d.quotes.length) {
      status.textContent = 'No quotes available. Check provider configuration.';
      quoteBtn.disabled = false; quoteBtn.textContent = 'Get Quote';
      return;
    }

    let html = '';
    for (const q of d.quotes) {
      const cost = q.cost_cents ? '$' + (q.cost_cents / 100).toFixed(2) : 'Pre-funded';
      const requestedG = parseFloat(document.getElementById('offsetAmount').value);
      const roundedUp = q.amount_kg * 1000 > requestedG;
      const kgLabel = roundedUp ? `${q.amount_kg} kg (rounded up from ${requestedG}g)` : `${q.amount_kg} kg`;
      html += `<div class="modal-quote-row"><span>${esc(q.provider)}</span><span>${kgLabel}</span></div>`;
      html += `<div class="modal-quote-row"><span>Estimated cost</span><span class="modal-quote-cost">${cost}</span></div>`;
    }
    document.getElementById('quoteDetails').innerHTML = html;
    document.getElementById('quoteSection').style.display = '';
    document.getElementById('confirmOffsetBtn').style.display = '';
    quoteBtn.style.display = 'none';
  } catch(e) {
    status.textContent = 'Failed to get quote.';
  }
  quoteBtn.disabled = false; quoteBtn.textContent = 'Get Quote';
}

async function confirmOffset() {
  const key = getOffsetKey();
  if (!key) return;

  const grams = parseFloat(document.getElementById('offsetAmount').value);
  if (!grams || grams <= 0) return;

  const btn = document.getElementById('confirmOffsetBtn');
  const status = document.getElementById('offsetStatus');
  btn.disabled = true; btn.textContent = 'Purchasing...';
  status.textContent = '';

  try {
    const r = await fetch('/carbon/offset?co2_grams=' + grams, {
      method: 'POST',
      headers: {'Authorization': 'Bearer ' + key},
    });
    const d = await r.json();
    if (r.ok) {
      // Show cost in status
      const results = d.results || [];
      const costs = results.map(r => r.cost_cents ? '$' + (r.cost_cents / 100).toFixed(2) : 'pre-funded').join(', ');
      status.textContent = 'Offset purchased! Cost: ' + costs;
      status.style.color = 'var(--primary)';
      btn.textContent = 'Done!';
      refreshAll();
      setTimeout(() => { closeOffsetModal(); status.style.color = ''; }, 2000);
    } else {
      if (r.status === 401) localStorage.removeItem('offset_api_key');
      status.textContent = d.detail || d.message || 'Offset failed';
      status.style.color = 'var(--red)';
      btn.textContent = 'Purchase';
      btn.disabled = false;
    }
  } catch(e) {
    status.textContent = 'Network error';
    status.style.color = 'var(--red)';
    btn.textContent = 'Purchase';
    btn.disabled = false;
  }
}

// -- Init --
loadSources();
refreshAll();
setInterval(refreshAll, 3000);       // Data + power chart every 3s
setInterval(loadSources, 60000);     // Refresh source list every minute
setInterval(() => { _eqSetIndex++; renderEquivSet(); }, 10000); // Rotate equivalents every 10s
