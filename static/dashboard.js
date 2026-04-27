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
const _compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
function fmtShort(n) { if (n < 0.01 && n > 0) return n.toExponential(1); return n < 1000 ? fmt(n, n < 1 ? 4 : 1) : _compact.format(n); }
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

// -- Daily breakdown: bar chart + heatmap --
let _latestDaily = [];
let _latestHeatmap = [];

// Build query for heatmap: ignore time range, keep source filter only
function buildHeatmapQuery() {
  const params = new URLSearchParams();
  if (activeSource) params.set('source', activeSource);
  const qs = params.toString();
  return qs ? '?' + qs : '';
}

function getDailyView() { return localStorage.getItem('dailyView') === 'heatmap' ? 'heatmap' : 'chart'; }
function getHeatmapMetric() { return localStorage.getItem('heatmapMetric') || 'co2'; }

function applyDailyViewUI() {
  const view = getDailyView();
  document.getElementById('dailyChartCard').style.display = view === 'chart' ? '' : 'none';
  document.getElementById('heatmapCard').style.display = view === 'heatmap' ? '' : 'none';
  document.getElementById('dailyViewToggle').textContent = view === 'chart' ? 'Heatmap' : 'Bar chart';
  document.getElementById('heatmapMetric').style.display = view === 'heatmap' ? '' : 'none';
  document.getElementById('heatmapMetric').value = getHeatmapMetric();
}

function toggleDailyView() {
  localStorage.setItem('dailyView', getDailyView() === 'chart' ? 'heatmap' : 'chart');
  applyDailyViewUI();
  renderDaily();
}

function updateDailyView() {
  localStorage.setItem('heatmapMetric', document.getElementById('heatmapMetric').value);
  renderDaily();
}

function renderDaily() {
  if (getDailyView() === 'heatmap') renderHeatmap(_latestHeatmap, getHeatmapMetric());
  else renderDailyBarChart(_latestDaily);
}

function renderDailyBarChart(dailyData) {
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

// Aggregate daily data by date for the heatmap (collapses sources)
function aggregateByDate(dailyData, metric) {
  const m = {};
  dailyData.forEach(row => {
    const date = row.date;
    if (!date) return;
    let v = 0;
    if (metric === 'co2') v = (row.co2_kg || 0) * 1000;       // grams
    else if (metric === 'tokens') v = row.total_tokens || 0;
    else v = row.requests || 0;
    m[date] = (m[date] || 0) + v;
  });
  return m;
}

function fmtMetric(v, metric) {
  if (metric === 'co2') return fmt(v, 2) + ' g';
  if (metric === 'tokens') return v.toLocaleString();
  return String(v);
}

function metricLabel(metric) {
  return metric === 'co2' ? 'CO2' : metric === 'tokens' ? 'tokens' : 'requests';
}

const HEATMAP_WEEKS = 53;
const HEATMAP_DAYS = 7;
const CELL = 11;
const CELL_GAP = 3;

function renderHeatmap(dailyData, metric) {
  const container = document.getElementById('heatmap');
  if (!container) return;
  const byDate = aggregateByDate(dailyData, metric);

  // Build the past HEATMAP_WEEKS*7 days, ending with today, aligned so each column is a week (Sun-Sat).
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const end = new Date(today);
  // Find the most recent Saturday (or today if Saturday) so the rightmost column is the current week
  end.setDate(end.getDate() + (6 - end.getDay()));
  const start = new Date(end);
  start.setDate(start.getDate() - (HEATMAP_WEEKS * HEATMAP_DAYS - 1));

  // Compute max for color scale
  let max = 0;
  for (let i = 0; i < HEATMAP_WEEKS * HEATMAP_DAYS; i++) {
    const d = new Date(start);
    d.setDate(d.getDate() + i);
    if (d > today) continue;
    const k = d.toISOString().slice(0, 10);
    if (byDate[k] > max) max = byDate[k];
  }

  // Color bucket: 0-4 based on log scale (so small values still show)
  const bucket = (v) => {
    if (v <= 0) return 0;
    if (max <= 0) return 0;
    const r = Math.log(1 + v) / Math.log(1 + max);
    if (r < 0.25) return 1;
    if (r < 0.5) return 2;
    if (r < 0.75) return 3;
    return 4;
  };

  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const dayLabels = ['', 'Mon', '', 'Wed', '', 'Fri', ''];

  const labelW = 28;
  const monthH = 14;
  const innerW = HEATMAP_WEEKS * (CELL + CELL_GAP);
  const innerH = HEATMAP_DAYS * (CELL + CELL_GAP);
  const w = labelW + innerW;
  const h = monthH + innerH;

  let svg = `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" xmlns="http://www.w3.org/2000/svg">`;

  // Day-of-week labels
  for (let r = 0; r < HEATMAP_DAYS; r++) {
    if (!dayLabels[r]) continue;
    const y = monthH + r * (CELL + CELL_GAP) + CELL - 2;
    svg += `<text x="0" y="${y}" class="heatmap-axis">${dayLabels[r]}</text>`;
  }

  // Cells + month labels (label appears at first column of each new month,
  // skipped when too close to the previous label to avoid overlap).
  const MIN_LABEL_SPACING_PX = 30;
  let lastMonth = -1;
  let lastLabelX = -Infinity;
  for (let col = 0; col < HEATMAP_WEEKS; col++) {
    for (let row = 0; row < HEATMAP_DAYS; row++) {
      const d = new Date(start);
      d.setDate(d.getDate() + col * HEATMAP_DAYS + row);
      const future = d > today;
      const k = d.toISOString().slice(0, 10);
      const v = byDate[k] || 0;
      const cls = future ? 'heatmap-cell heatmap-future' : `heatmap-cell heatmap-l${bucket(v)}`;
      const x = labelW + col * (CELL + CELL_GAP);
      const y = monthH + row * (CELL + CELL_GAP);
      const tip = future ? '' : `${k}: ${fmtMetric(v, metric)}`;
      svg += `<rect x="${x}" y="${y}" width="${CELL}" height="${CELL}" rx="2" class="${cls}">`;
      if (tip) svg += `<title>${tip}</title>`;
      svg += `</rect>`;

      // Month label only on the first row of the column when the month changes
      if (row === 0) {
        const m = d.getMonth();
        if (m !== lastMonth) {
          if (x - lastLabelX >= MIN_LABEL_SPACING_PX) {
            svg += `<text x="${x}" y="${monthH - 4}" class="heatmap-axis">${months[m]}</text>`;
            lastLabelX = x;
          }
          lastMonth = m;
        }
      }
    }
  }

  svg += '</svg>';
  container.innerHTML = svg;
}

function updateDailyChart(dailyData) {
  _latestDaily = dailyData || [];
  renderDaily();
}

// -- Auto-offset --
async function refreshAutoOffsetStatus() {
  let s;
  try {
    const r = await fetch('/carbon/auto_offset');
    s = await r.json();
  } catch (e) {
    return;
  }
  // Banner
  const banner = document.getElementById('autoOffsetBanner');
  const text = document.getElementById('autoOffsetBannerText');
  if (s.cap_exceeded) {
    const pendingG = (s.pending_grams || 0).toFixed(1);
    const cap = (s.daily_cap_cents / 100).toFixed(2);
    text.textContent = `Auto-offset hit $${cap} daily cap — ${pendingG} g of debt awaiting manual offset.`;
    banner.style.display = '';
  } else {
    banner.style.display = 'none';
  }
  // Settings card (only visible when modal is open, but safe to update either way)
  const cb = document.getElementById('autoOffsetEnabled');
  if (cb) cb.checked = !!s.enabled;
  const meta = document.getElementById('autoOffsetMeta');
  if (meta) {
    const spent = (s.today_spent_cents / 100).toFixed(2);
    const cap = (s.daily_cap_cents / 100).toFixed(2);
    meta.textContent = `Today: $${spent} / $${cap}`;
  }
}

async function toggleAutoOffset() {
  const cb = document.getElementById('autoOffsetEnabled');
  const desired = cb.checked;
  const key = getOffsetKey();
  if (!key) { cb.checked = !desired; return; }
  try {
    const r = await fetch('/carbon/auto_offset/toggle', {
      method: 'POST',
      headers: {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: desired}),
    });
    if (!r.ok) {
      if (r.status === 401) localStorage.removeItem('offset_api_key');
      cb.checked = !desired;
      const status = document.getElementById('offsetStatus');
      if (status) {
        status.textContent = 'Auto-offset toggle failed (auth?)';
        status.style.color = 'var(--red)';
      }
      return;
    }
    refreshAutoOffsetStatus();
  } catch (e) {
    cb.checked = !desired;
  }
}

// Equivalents carousel state
let _carouselItems = [];
let _carouselGreen = false;
let _carouselColor = 'var(--muted)';

// Build the carousel DOM once with empty placeholders. Subsequent refreshes only
// update text/class on existing nodes, so scroll position and momentum aren't disturbed.
function ensureCarouselDom() {
  const track = document.getElementById('equivTrack');
  const carousel = document.getElementById('equivCarousel');
  if (!track || !carousel || !_carouselItems.length) return;
  const needed = _carouselItems.length * 3;
  if (track.children.length === needed) return;
  const cell = `<div class="equiv">
    <div class="equiv-icon"><span class="mdi"></span></div>
    <div class="equiv-number"></div>
    <div class="equiv-desc"></div>
  </div>`;
  track.innerHTML = cell.repeat(needed);
  // Start centered in the middle copy so dragging either way has buffer
  carousel.scrollLeft = track.scrollWidth / 3;
}

function renderCarousel() {
  ensureCarouselDom();
  const track = document.getElementById('equivTrack');
  if (!track || !_carouselItems.length) return;
  const n = _carouselItems.length;
  for (let k = 0; k < track.children.length; k++) {
    const item = _carouselItems[k % n];
    const node = track.children[k];
    const iconWrap = node.firstElementChild;
    iconWrap.style.color = _carouselColor;
    iconWrap.firstElementChild.className = 'mdi ' + item.icon;
    node.children[1].textContent = fmtEq(item.val || 0);
    node.children[2].textContent = _carouselGreen ? item.green : item.red;
  }
}

function fmtEq(v) { return v < 0.01 && v > 0 ? '<0.01' : v >= 1000 ? (v/1000).toFixed(1)+'k' : fmt(v, v < 1 ? 2 : 1); }

async function refreshAll() {
  const [summary, equiv, balance, live, daily, dailyAll, reqs, offsets] = await Promise.all([
    fetch('/carbon/summary' + buildQuery()).then(r=>r.json()).catch(()=>({})),
    fetch('/carbon/equivalents' + buildQuery()).then(r=>r.json()).catch(()=>({})),
    fetch('/carbon/balance').then(r=>r.json()).catch(()=>({})),
    fetch('/carbon/live').then(r=>r.json()).catch(()=>({})),
    fetch('/carbon/daily' + buildQuery()).then(r=>r.json()).catch(()=>[]),
    fetch('/carbon/daily' + buildHeatmapQuery()).then(r=>r.json()).catch(()=>[]),
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
  const totalTokens = summary.total_tokens || 0;
  if (showEnergy) {
    document.getElementById('gEnergy').textContent = fmtShort(kwh);
    setGauge('gaugeEnergy', logPct(kwh, 10));
  }
  document.getElementById('gEmissions').textContent = fmtShort(co2);
  document.getElementById('gRequests').textContent = totalReqs;
  document.getElementById('gTokens').textContent = fmtShort(totalTokens);
  setGauge('gaugeEmissions', logPct(co2, 1000));
  setGauge('gaugeRequests', logPct(totalReqs, 1000));
  setGauge('gaugeTokens', logPct(totalTokens, 1000000));

  // Equivalents carousel: red labels when in debt, green when carbon-negative
  const isGreen = (balance.balance_grams || 0) <= 0 && (balance.total_offset_grams || 0) > 0;
  const eqColor = isGreen ? 'var(--primary)' : 'var(--red)';
  // When carbon-negative, equivalents represent the surplus offset (not emissions).
  // All equivalents are linear in CO2 grams, so we scale by surplus/emitted.
  const emittedG = summary.total_co2_grams || 0;
  const surplusG = Math.abs(balance.balance_grams || 0);
  const eqScale = isGreen && emittedG > 0 ? surplusG / emittedG : 1;
  const scaleEq = (v) => (typeof v === 'number' ? v * eqScale : v);
  const items = [
    { icon: 'mdi-car', val: scaleEq(equiv.cars_per_year), green: 'cars off the road*', red: 'cars on the road*' },
    { icon: 'mdi-home-lightning-bolt', val: scaleEq(equiv.homes_energy_per_year), green: 'homes energy offset*', red: 'homes energy equivalent*' },
    { icon: 'mdi-airplane', val: scaleEq(equiv.flights_la_nyc), green: 'flights LA-NYC offset', red: 'flights LA-NYC equivalent' },
    { icon: 'mdi-tree', val: scaleEq(equiv.trees_to_offset_yearly), green: 'trees/yr offset', red: 'trees/yr to neutralize' },
    { icon: 'mdi-cellphone', val: scaleEq(equiv.smartphone_charges), green: 'phone charges offset', red: 'phone charges equivalent' },
    { icon: 'mdi-magnify', val: scaleEq(equiv.google_searches), green: 'Google searches offset', red: 'Google searches equivalent' },
    { icon: 'mdi-road-variant', val: scaleEq(equiv.km_driven), green: 'km not driven', red: 'km driven equivalent' },
    { icon: 'mdi-television-play', val: scaleEq(equiv.streaming_hours), green: 'streaming hrs offset', red: 'streaming hrs equivalent' },
    { icon: 'mdi-email', val: scaleEq(equiv.emails_sent), green: 'emails offset', red: 'emails equivalent' },
    { icon: 'mdi-coffee', val: scaleEq(equiv.coffee_cups), green: 'coffees offset', red: 'coffees equivalent' },
    { icon: 'mdi-kettle', val: scaleEq(equiv.kettle_boils), green: 'kettle boils offset', red: 'kettle boils equivalent' },
    { icon: 'mdi-washing-machine', val: scaleEq(equiv.laundry_loads), green: 'laundry loads offset', red: 'laundry loads equivalent' },
    { icon: 'mdi-food-steak', val: scaleEq(equiv.beef_burgers), green: 'beef burgers offset', red: 'beef burgers equivalent' },
    { icon: 'mdi-robot', val: scaleEq(equiv.chatgpt_queries), green: 'ChatGPT queries offset', red: 'ChatGPT queries equivalent' },
  ];
  _carouselItems = items;
  _carouselGreen = isGreen;
  _carouselColor = eqColor;
  renderCarousel();

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

  // Daily chart (bar = filtered; heatmap = all-time, source-filtered only)
  _latestHeatmap = dailyAll || [];
  updateDailyChart(daily);

  // Auto-offset banner + settings card
  refreshAutoOffsetStatus();

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
applyDailyViewUI();
loadSources();
refreshAll();
setInterval(refreshAll, 3000);       // Data + power chart every 3s
setInterval(loadSources, 60000);     // Refresh source list every minute
// Equivalents carousel: continuous auto-scroll with drag + wheel control
(function setupCarousel() {
  const carousel = document.getElementById('equivCarousel');
  if (!carousel) return;
  const AUTO_PX_PER_FRAME = 0.4; // ~24px/sec
  const PAUSE_AFTER_INTERACT_MS = 1500;
  const MOMENTUM_FRICTION = 0.94;     // multiplied each frame, blends toward ambient
  const MOMENTUM_MAX = 60;            // cap px/frame to avoid runaway flings
  // Ambient momentum is what auto-scroll looks like in momentum-space.
  // scrollLeft -= momentum, and forward auto-scroll is scrollLeft += AUTO_PX_PER_FRAME,
  // so the equivalent ambient momentum is the negative of the forward speed.
  const AMBIENT_MOMENTUM = -AUTO_PX_PER_FRAME;

  let isDown = false, startX = 0, startScroll = 0;
  let lastX = 0, lastT = 0, velocity = 0;
  let momentum = AMBIENT_MOMENTUM;
  let pausedUntil = 0; // wheel/trackpad uses this; drag uses momentum directly

  function wrap() {
    const third = carousel.scrollWidth / 3;
    if (!third) return;
    if (carousel.scrollLeft >= third * 2) carousel.scrollLeft -= third;
    else if (carousel.scrollLeft < third * 0.5) carousel.scrollLeft += third;
  }

  function tick() {
    if (!isDown) {
      if (Date.now() > pausedUntil) {
        // Decay momentum exponentially toward ambient drift, regardless of sign.
        // A forward fling slows to ambient; a reverse fling slows, reverses, and resumes ambient.
        momentum = AMBIENT_MOMENTUM + (momentum - AMBIENT_MOMENTUM) * MOMENTUM_FRICTION;
        carousel.scrollLeft -= momentum;
      } else {
        // Wheel/trackpad in progress — let the user drive; reset momentum so we resume cleanly.
        momentum = AMBIENT_MOMENTUM;
      }
    }
    wrap();
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  carousel.addEventListener('mousedown', (e) => {
    isDown = true;
    startX = e.pageX;
    startScroll = carousel.scrollLeft;
    lastX = e.pageX;
    lastT = performance.now();
    velocity = 0;
    momentum = 0;
    carousel.classList.add('dragging');
    e.preventDefault();
  });
  window.addEventListener('mouseup', () => {
    if (!isDown) return;
    isDown = false;
    carousel.classList.remove('dragging');
    // Convert velocity (px/ms) to px/frame (~16.67ms) and clamp.
    // Sign convention: scrollLeft -= momentum, so positive velocity (drag right) yields
    // positive momentum which decreases scrollLeft → reverse direction. Symmetric for fling-left.
    momentum = Math.max(-MOMENTUM_MAX, Math.min(MOMENTUM_MAX, velocity * 16.67));
    // No pause window — let momentum decay smoothly back to ambient.
    pausedUntil = 0;
  });
  window.addEventListener('mousemove', (e) => {
    if (!isDown) return;
    carousel.scrollLeft = startScroll - (e.pageX - startX);
    const now = performance.now();
    const dt = now - lastT;
    if (dt > 0) {
      // Smooth velocity with EMA for stability
      const instant = (e.pageX - lastX) / dt; // px/ms (positive = drag right)
      velocity = velocity * 0.6 + instant * 0.4;
    }
    lastX = e.pageX;
    lastT = now;
  });

  // Wheel/trackpad: pause auto-scroll briefly so user motion isn't fought
  carousel.addEventListener('wheel', (e) => {
    // Translate vertical wheel to horizontal scroll if user has no horizontal delta
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
      carousel.scrollLeft += e.deltaY;
      e.preventDefault();
    }
    pausedUntil = Date.now() + PAUSE_AFTER_INTERACT_MS;
  }, { passive: false });
})();
