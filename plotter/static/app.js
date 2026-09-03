/* Plotter front end. Plain Leaflet + vanilla JS, no build step. */
(function () {
'use strict';

const $ = (id) => document.getElementById(id);
const API = (p) => (window.PLOTTER_BASE || '') + '/api' + p;

let META = null;
let map, layerCtl;
// The polar value grid of the last coverage run, kept so the map readout can
// answer a mouse move without a round trip.
let coverageGrid = null;
let aiming = false;          // clicking the map aims the beam
let beamLine = null;         // where the beam points, drawn from the station
const GRID_UNIT = { signal: 'dBm', loss: 'dB loss', margin: 'dB margin', los: '' };
const markers = {};
let coverageOverlay = null, horizonLayer = null, linkLine = null,
    hfRings = null, mastLayer = null, txLayer = null;
let picking = null;
let lastProfile = null, lastTerrain = null;
let baseLayers = {}, currentBaseKey = null, currentMode = 'coverage',
    baseBeforeLink = null;

/* ------------------------------------------------------------ basemaps */
const BASEMAPS = {
  osm: () => L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '© OpenStreetMap' }),
  opentopo: () => L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
    maxZoom: 17, attribution: '© OpenTopoMap (CC-BY-SA)' }),
  maaamet_kaart: () => L.tileLayer(
    'https://tiles.maaamet.ee/tm/tms/1.0.0/kaart@GMC/{z}/{x}/{-y}.png', {
    maxZoom: 18, attribution: '© Maa- ja Ruumiamet' }),
  maaamet_reljeef: () => L.tileLayer(
    'https://tiles.maaamet.ee/tm/tms/1.0.0/reljeef@GMC/{z}/{x}/{-y}.png', {
    maxZoom: 18, attribution: '© Maa- ja Ruumiamet (relief)' }),
  maaamet_foto: () => L.tileLayer(
    'https://tiles.maaamet.ee/tm/tms/1.0.0/foto@GMC/{z}/{x}/{-y}.png', {
    maxZoom: 18, attribution: '© Maa- ja Ruumiamet (ortho)' }),
  mml_topo: () => L.tileLayer(
    'https://tiles.kartat.kapsi.fi/peruskartta/{z}/{x}/{y}.jpg', {
    maxZoom: 18, attribution: '© Maanmittauslaitos' }),
  esri_sat: () => L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    maxZoom: 19, attribution: 'Esri, Maxar, Earthstar Geographics' }),
};
const BASEMAP_LABELS = {
  osm: 'OpenStreetMap', opentopo: 'OpenTopoMap',
  maaamet_kaart: 'Maa-amet base map', maaamet_reljeef: 'Maa-amet relief',
  maaamet_foto: 'Maa-amet orthophoto', mml_topo: 'Finland topographic',
  esri_sat: 'Satellite',
};

/* -------------------------------------------------------------- helpers */
function toast(msg, isErr) {
  const t = $('toast');
  t.textContent = msg;
  t.className = isErr ? 'err' : '';
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add('hidden'), isErr ? 8000 : 3500);
}

async function api(path, opts) {
  const r = await fetch(API(path), Object.assign({
    headers: { 'Content-Type': 'application/json' } }, opts || {}));
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch (e) { data = { detail: text }; }
  if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
  return data;
}

const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body) });

function num(id, dflt) {
  const v = parseFloat($(id).value);
  return isFinite(v) ? v : dflt;
}
function fmt(v, d) {
  return (v === null || v === undefined || !isFinite(v)) ? '—' : Number(v).toFixed(d === undefined ? 1 : d);
}
function kv(rows) {
  return '<table class="kv">' + rows.filter(Boolean).map(
    r => `<tr><td>${r[0]}</td><td>${r[1]}</td></tr>`).join('') + '</table>';
}
function sect(t) { return `<div class="section-t">${t}</div>`; }

/* Each planning site gets its own colour and a letter, so which end is A and
   which is B is obvious at a glance on the map. */
const SITE_STYLE = {
  a:   { cls: 'marker-a',    label: 'A', name: 'End A' },
  b:   { cls: 'marker-b',    label: 'B', name: 'End B' },
  cov: { cls: 'marker-site', label: 'S', name: 'Station' },
  hf:  { cls: 'marker-hf',   label: 'H', name: 'HF station' },
};

function pin(cls, label) {
  const sz = label ? 22 : 14, a = sz / 2;
  return L.divIcon({ className: '',
    html: `<div class="marker-pin ${cls}${label ? ' labeled' : ''}">${label || ''}</div>`,
    iconSize: [sz, sz], iconAnchor: [a, a] });
}

function markerPopup(key, lat, lon) {
  const s = SITE_STYLE[key] || { name: key };
  return `<b>${s.name}</b><br>` +
    `<span class="k">lat</span> ${lat.toFixed(5)}<br>` +
    `<span class="k">lon</span> ${lon.toFixed(5)}<br>` +
    `<span style="color:var(--dim);font-size:11px">drag to move</span>`;
}

function setMarker(key, lat, lon) {
  const s = SITE_STYLE[key] || { cls: 'marker-site', label: '', name: key };
  if (markers[key]) map.removeLayer(markers[key]);
  const m = L.marker([lat, lon], { icon: pin(s.cls, s.label), draggable: true,
    autoPan: true, title: s.name });
  m.bindTooltip(s.name, { permanent: true, direction: 'right',
    offset: [10, 0], className: 'marker-label' });
  m.bindPopup(markerPopup(key, lat, lon));
  m.on('dragend', (e) => {
    const p = e.target.getLatLng();
    writeSite(key, p.lat, p.lng);
  });
  markers[key] = m;
  m.addTo(map);
}

const SITE_FIELDS = {
  cov: ['cov-lat', 'cov-lon'], a: ['a-lat', 'a-lon'],
  b: ['b-lat', 'b-lon'], hf: ['hf-lat', 'hf-lon'],
};

function writeSite(key, lat, lon) {
  const f = SITE_FIELDS[key];
  if (!f) return;
  $(f[0]).value = lat.toFixed(5);
  $(f[1]).value = lon.toFixed(5);
  setMarker(key, lat, lon);
  if (key === 'cov') { lookupElevation(lat, lon); warmForCoverage(); drawBeam(); }
  if (key === 'a' || key === 'b') drawLinkLine();
}

async function lookupElevation(lat, lon) {
  try {
    const r = await api(`/elevation?lat=${lat}&lon=${lon}`);
    $('cov-amsl').value = `${r.elevation_m} (${r.source})`;
  } catch (e) { $('cov-amsl').value = '—'; }
}

function drawLinkLine() {
  const a = [num('a-lat'), num('a-lon')], b = [num('b-lat'), num('b-lon')];
  if (!isFinite(a[0]) || !isFinite(b[0])) return;
  if (linkLine) map.removeLayer(linkLine);
  linkLine = L.polyline([a, b], { color: '#4ea1ff', weight: 2, dashArray: '6 5' })
    .addTo(map);
}

/* ------------------------------------------------------- map readout */
// Distance and bearing from the station to the cursor, plus the coverage
// value there. Great-circle on a sphere is plenty for a hover hint: it is
// within a few metres of the Vincenty answer the backend uses.
const R_EARTH_M = 6371008.8;
const rad = d => d * Math.PI / 180, deg = r => r * 180 / Math.PI;

function bearingDistance(lat1, lon1, lat2, lon2) {
  const p1 = rad(lat1), p2 = rad(lat2), dl = rad(lon2 - lon1);
  const dp = p2 - p1;
  const a = Math.sin(dp / 2) ** 2 +
            Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  const dist = 2 * R_EARTH_M * Math.asin(Math.min(1, Math.sqrt(a)));
  const y = Math.sin(dl) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return { distance_m: dist, bearing_deg: (deg(Math.atan2(y, x)) + 360) % 360 };
}

// S-meter reading, IARU Region 1: one S unit is 6 dB, and S9 is -73 dBm on HF
// but -93 dBm above 30 MHz. The server sends which reference applies, so the
// readout shows what a correctly calibrated rig on that band would show.
function sMeter(dbm, s9) {
  if (!isFinite(s9)) return null;
  const over = dbm - s9;
  if (over >= 0) return over >= 1 ? `S9+${Math.round(over)} dB` : 'S9';
  const s = 9 + over / 6;
  return s < 0.5 ? 'below S1' : `S${Math.round(Math.max(1, s))}`;
}

// Same bilinear read of the polar grid that render_png does server side, so
// the number under the cursor is the number under the pixel.
function gridValueAt(g, bearingDeg, distanceM) {
  const ai = ((bearingDeg % 360) + 360) % 360 / g.azimuth_step_deg;
  const ri = g.range_step_m > 0 ? distanceM / g.range_step_m : 0;
  if (ri > g.n_r - 1) return null;
  const a0 = Math.floor(ai) % g.n_az, a1 = (a0 + 1) % g.n_az, fa = ai - Math.floor(ai);
  const r0 = Math.min(Math.max(Math.floor(ri), 0), g.n_r - 1);
  const r1 = Math.min(r0 + 1, g.n_r - 1), fr = Math.min(Math.max(ri - Math.floor(ri), 0), 1);
  const v = [g.values[a0][r0], g.values[a1][r0], g.values[a0][r1], g.values[a1][r1]];
  if (v.some(x => x === null || x === undefined)) return null;
  return (v[0] * (1 - fa) + v[1] * fa) * (1 - fr) + (v[2] * (1 - fa) + v[3] * fa) * fr;
}

function updateReadout(e) {
  const box = $('readout');
  if (!coverageGrid) { box.classList.add('hidden'); return; }
  const c = coverageGrid;
  const bd = bearingDistance(c.lat, c.lon, e.latlng.lat, e.latlng.lng);
  const parts = [`${(bd.distance_m / 1000).toFixed(1)} km`,
                 `${bd.bearing_deg.toFixed(0)}\u00b0`];
  if (bd.distance_m <= c.maxRangeM) {
    const v = gridValueAt(c.grid, bd.bearing_deg, bd.distance_m);
    if (v === null) {
      parts.push('no data');
    } else if (c.grid.metric === 'los') {
      parts.push(v >= 0.5 ? 'line of sight' : 'obstructed');
    } else {
      parts.push(`${v.toFixed(1)} ${c.unit}`);
      if (c.grid.metric === 'signal') {
        const s = sMeter(v, c.s9);
        if (s) parts.push(s);
        // The S reading alone does not say whether the mode can copy it:
        // -119 dBm FM is still S5 on 2 m, and that is the threshold.
        if (v < c.sensitivity) {
          const m = c.modeLabel.replace(/\s*\(.*\)$/, '');   // "FM (12 kHz)" -> "FM"
          parts.push(c.sensitivityS ? `under ${m}, needs ${c.sensitivityS}`
                                    : `under ${m}`);
        }
      }
    }
  } else {
    parts.push('outside the sweep');
  }
  box.textContent = parts.join('  \u00b7  ');
  box.classList.remove('hidden');
}

/* ------------------------------------------------------------ init form */
function fillSelect(el, entries, selected) {
  el.innerHTML = entries.map(([v, l]) =>
    `<option value="${v}"${v === selected ? ' selected' : ''}>${l}</option>`).join('');
}

function antennaOptions(groups) {
  return Object.entries(META.antennas)
    .filter(([, v]) => groups.includes(v.group) || v.group === 'any')
    .map(([k, v]) => [k, v.label]);
}

// What the "Gain / size" box means for a preset, and what the preset's own
// value is. Dishes take a diameter in metres, wires a height in metres,
// everything else a peak gain in dBi.
function antennaFieldValue(preset) {
  const def = META.antennas[preset];
  if (!def) return null;
  const p = def.params || {};
  if (def.type === 'dish') return p.diameter_m ?? null;
  // Wires take their height from the mast height field, so the box is unused.
  if (def.type === 'wire') return null;
  if (def.type === 'isotropic') return p.gain_dbi ?? 0;
  return p.peak_gain_dbi ?? null;
}

// Keep that box in step with the selected antenna. Without this it kept
// whatever number was there before, and antennaSpec() sent it as an override,
// so every beam quietly ran at the last antenna's gain: pick a 7 element yagi
// and it still radiated the collinear's 6 dBi.
function bindAntennaGain(prefix) {
  const sel = $(prefix + '-ant');
  const box = $(prefix + '-gain');
  if (!sel || !box) return;
  const sync = () => {
    const v = antennaFieldValue(sel.value);
    if (v !== null) box.value = v;
  };
  sel.addEventListener('change', sync);
  sync();
}

async function loadMeta() {
  META = await api('/meta');

  const hfBands = META.bands.filter(b => b.group === 'hf').map(b => [b.key, b.label]);
  const allBands = META.bands.map(b => [b.key, b.label]);
  const vhfUp = META.bands.filter(b => b.group !== 'hf').map(b => [b.key, b.label]);

  fillSelect($('cov-band'), vhfUp, '2m');
  fillSelect($('lnk-band'), allBands, '6cm');
  fillSelect($('hf-band'), hfBands, '80m');

  const modes = Object.entries(META.modes).map(([k, v]) => [k, v.label]);
  fillSelect($('cov-mode'), modes, 'fm');
  fillSelect($('lnk-mode'), modes, 'wifi_11n_20');
  fillSelect($('hf-mode'), modes, 'ssb');

  const grounds = Object.keys(META.grounds).map(k => [k, k.replace(/_/g, ' ')]);
  ['cov-ground', 'lnk-ground', 'hf-ground'].forEach(id =>
    fillSelect($(id), grounds, 'average'));

  const climates = Object.entries(META.climates).map(([k, v]) => [k, v]);
  fillSelect($('cov-climate'), climates, '6');
  fillSelect($('lnk-climate'), climates, '6');

  fillSelect($('cov-ant'), antennaOptions(['vhf', 'uhf', 'shf']), 'collinear_6');
  fillSelect($('a-ant'), antennaOptions(['vhf', 'uhf', 'shf', 'hf']), 'dish_600');
  fillSelect($('b-ant'), antennaOptions(['vhf', 'uhf', 'shf', 'hf']), 'dish_600');
  fillSelect($('hf-ant'), antennaOptions(['hf']), 'dipole');
  ['cov', 'a', 'b'].forEach(bindAntennaGain);

  const bandFreq = {};
  META.bands.forEach(b => bandFreq[b.key] = b.centre_mhz);
  const link = (bandSel, freqSel) => $(bandSel).addEventListener('change', e => {
    $(freqSel).value = bandFreq[e.target.value];
  });
  link('cov-band', 'cov-freq');
  link('lnk-band', 'lnk-freq');

  const now = new Date();
  $('hf-when').value = new Date(now.getTime() - now.getTimezoneOffset() * 0)
    .toISOString().slice(0, 16);
}

// Draw where the beam points. A directional antenna that is silently aimed at
// true north looks like a terrain artefact rather than a setting, so show it.
function drawBeam() {
  if (beamLine) { map.removeLayer(beamLine); beamLine = null; }
  const def = META.antennas[$('cov-ant').value];
  if (!def || def.type === 'omni' || def.type === 'isotropic') return;
  const lat = num('cov-lat'), lon = num('cov-lon');
  if (!isFinite(lat) || !isFinite(lon)) return;
  const bear = num('cov-bear', 0);
  const R = 6371008.8, rad = d => d * Math.PI / 180, deg = r => r * 180 / Math.PI;
  const reach = Math.max(num('cov-range', 60), 5) * 1000;
  const p1 = rad(lat), l1 = rad(lon), b = rad(bear), dr = reach / R;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(dr) + Math.cos(p1) * Math.sin(dr) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(dr) * Math.cos(p1),
                             Math.cos(dr) - Math.sin(p1) * Math.sin(p2));
  beamLine = L.polyline([[lat, lon], [deg(p2), deg(l2)]], {
    color: '#ffb454', weight: 2, opacity: 0.9, dashArray: '7 6',
  }).addTo(map).bindTooltip(`beam ${Math.round(bear)}\u00b0 true`);
}

/* --------------------------------------------------- terrain prefetch */
// Picking a station kicks off the elevation download for the area a sweep
// would need. Without this the first coverage run pays for it inline: a cold
// 250 km sweep spent ~50 s downloading before it computed anything, and a
// proxy in front of the app would cut the connection long before it finished.
let warmToken = 0;

async function warmTerrain(lat, lon, radiusKm) {
  const token = ++warmToken;                 // a newer pick cancels this one
  const box = $('warm-status');
  try {
    for (let i = 0; i < 200; i++) {
      const r = await post('/terrain/warm',
        { lat, lon, radius_km: radiusKm, max_tiles: 4 });
      if (token !== warmToken) return;       // superseded, stop quietly
      if (!r.total) { box.classList.add('hidden'); return; }
      const done = r.total - r.remaining;
      if (r.remaining <= 0) {
        box.textContent = `Terrain ready (${r.total} tiles)`;
        setTimeout(() => { if (token === warmToken) box.classList.add('hidden'); }, 2500);
        return;
      }
      box.textContent = `Fetching terrain ${done}/${r.total} tiles`;
      box.classList.remove('hidden');
    }
  } catch (e) {
    // Not fatal: the coverage run will fetch whatever is still missing.
    if (token === warmToken) box.classList.add('hidden');
  }
}

function warmForCoverage() {
  const lat = num('cov-lat'), lon = num('cov-lon');
  if (!isFinite(lat) || !isFinite(lon)) return;
  warmTerrain(lat, lon, num('cov-range', 60));
}

/* ------------------------------------------------------------- coverage */
function antennaSpec(prefix, kind) {
  const preset = $(prefix + '-ant').value;
  const def = META.antennas[preset];
  const spec = { preset };
  if (def.type === 'dish') {
    const g = num(prefix + '-gain');
    // treat the field as a diameter when it looks like one, else keep preset
    if (g > 0 && g < 6) spec.diameter_m = g;
  } else if (def.type === 'wire') {
    spec.height_m = num(prefix + '-h', 12);
    if ($(prefix + '-orient')) spec.orientation_deg = num(prefix + '-orient', 90);
  } else if ($(prefix + '-gain')) {
    // An override, not a default. Left empty, the preset's own gain stands.
    const g = num(prefix + '-gain');
    if (g !== undefined) {
      spec.peak_gain_dbi = g;
      spec.gain_dbi = g;
    }
  }
  return spec;
}

const DETAIL = {
  fast:   { azimuth_step_deg: 2, range_step_m: 1000 },
  normal: { azimuth_step_deg: 2, range_step_m: 500 },
  fine:   { azimuth_step_deg: 1, range_step_m: 250 },
};

// Drives the /<kind>/start and /<kind>/job/{id} pair. Each request is short,
// so no proxy or CDN timeout applies to the work itself. Both the sweep and
// the path analysis run for minutes on a cold cache, and both used to die on
// whatever gave up first.
async function runJob(kind, body, btn, verb) {
  const job = await post(kind + '/start', body);
  if (job.result) return job.result;
  for (let i = 0; i < 3600; i++) {
    const st = await api(`${kind}/job/${job.id}`);
    if (st.state === 'done') { btn.textContent = verb + '\u2026'; return st.result; }
    const pct = st.progress ? ` ${Math.round(st.progress * 100)}%` : '\u2026';
    btn.textContent = verb + pct;
    await new Promise(r => setTimeout(r, i < 10 ? 300 : 1000));
  }
  throw new Error('the run did not finish');
}

async function runCoverage() {
  const btn = $('cov-run');
  btn.disabled = true; btn.textContent = 'Computing…';
  try {
    const d = DETAIL[$('cov-detail').value];
    const body = {
      site: {
        name: 'Station', lat: num('cov-lat'), lon: num('cov-lon'),
        height_agl_m: num('cov-h', 20), tx_power_w: num('cov-pwr', 25),
        feedline_loss_db: num('cov-feed', 1.5),
        antenna: antennaSpec('cov'),
      },
      freq_mhz: num('cov-freq', 145), mode: $('cov-mode').value,
      rx_height_agl_m: num('cov-rxh', 2),
      max_range_km: num('cov-range', 60),
      antenna_bearing_deg: num('cov-bear', 0),
      ground: $('cov-ground').value, climate: parseInt($('cov-climate').value),
      reliability: parseFloat($('cov-rel').value),
      clutter_m: num('cov-clutter', 0), k_factor: num('cov-k', 1.333),
      metric: $('cov-metric').value,
      azimuth_step_deg: d.azimuth_step_deg, range_step_m: d.range_step_m,
    };
    const t0 = performance.now();
    // Start it in the background and poll. A fine-detail long-range sweep is
    // minutes of ITM solving, and holding one connection open for that is
    // what produced the bare "NetworkError" through the proxy.
    const r = await runJob('/coverage', body, btn, 'Computing');
    const secs = ((performance.now() - t0) / 1000).toFixed(1);

    if (coverageOverlay) map.removeLayer(coverageOverlay);
    coverageOverlay = L.imageOverlay(r.image_url, r.bounds, { opacity: 0.75 })
      .addTo(map);
    coverageGrid = r.grid ? {
      grid: r.grid, lat: body.site.lat, lon: body.site.lon,
      maxRangeM: body.max_range_km * 1000,
      sensitivity: r.sensitivity_dbm, unit: GRID_UNIT[r.grid.metric] || 'dBm',
      s9: r.s9_dbm,
      // Name the threshold that is being failed. "S3, below sensitivity" only
      // prompts the question "below what?": S3 is a perfectly good signal on
      // CW and hopeless on FM, and the difference is this setting.
      modeLabel: (META.modes[$('cov-mode').value] || {}).label || 'the receiver',
      sensitivityS: r.sensitivity_s_meter || '',
    } : null;
    map.fitBounds(r.bounds, { padding: [30, 30] });

    // One swatch per S band, each labelled with the S units it covers. The
    // exact level is the cursor readout's job, so the legend stays readable.
    const legend = '<div class="legend">' + r.legend.map(b =>
      `<div style="background:rgba(${b.rgba.join(',')})" title="${b.dbm} dBm and up"></div>`
      ).join('') + '</div><div class="legend-labels">' +
      r.legend.map(b => `<span>${b.label}</span>`).join('') + '</div>' +
      `<div class="sub">S9 = ${r.s9_dbm} dBm on this band` +
      (r.sensitivity_s_meter
        ? ` · receiver copies down to ${r.sensitivity_s_meter}` : '') + '</div>';

    $('cov-result').innerHTML =
      sect('Result') +
      kv([
        ['Covered area', fmt(r.stats.covered_area_km2, 0) + ' km²'],
        ['Furthest coverage', fmt(r.stats.max_range_reached_km, 1) + ' km'],
        ['Radio horizon', fmt(r.horizon_km, 1) + ' km'],
        ['Antenna', `${r.antenna.name} · ${fmt(r.antenna.peak_gain_dbi, 1)} dBi`],
        ['Threshold', fmt(r.sensitivity_dbm, 0) + ' dBm'],
        ['Paths evaluated', r.stats.itm_calls.toLocaleString()],
        ['Compute time', secs + ' s'],
      ]) + legend +
      (r.notes || []).map(n => `<div class="hint">${n}</div>`).join('');
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = 'Predict coverage';
  }
}

async function runHorizon() {
  try {
    const q = `?lat=${num('cov-lat')}&lon=${num('cov-lon')}` +
      `&height_agl_m=${num('cov-h', 20)}&k_factor=${num('cov-k', 1.333)}` +
      `&max_range_km=${Math.min(num('cov-range', 60), 120)}`;
    const g = await api('/horizon' + q);
    if (horizonLayer) map.removeLayer(horizonLayer);
    horizonLayer = L.geoJSON(g, { style: {
      color: '#ffb454', weight: 2, fillOpacity: 0.07, dashArray: '4 4' } })
      .addTo(map);
    horizonLayer.bindPopup(
      `Terrain horizon from ${g.properties.height_agl_m} m AGL<br>` +
      `Geometric horizon over flat ground: ${g.properties.geometric_horizon_km} km`);
    map.fitBounds(horizonLayer.getBounds(), { padding: [30, 30] });
  } catch (e) { toast(e.message, true); }
}

/* ----------------------------------------------------------------- link */
function verdictClass(v) {
  return { 'solid': 'solid', 'workable': 'workable', 'marginal': 'marginal',
           'on the edge': 'edge', 'will not work': 'bad' }[v] || '';
}

async function runLink() {
  const btn = $('lnk-run');
  btn.disabled = true; btn.textContent = 'Analysing…';
  try {
    const body = {
      tx: { name: 'A', lat: num('a-lat'), lon: num('a-lon'),
            height_agl_m: num('a-h', 20), tx_power_w: num('a-p', 0.5),
            feedline_loss_db: num('a-f', 1.5), antenna: antennaSpec('a') },
      rx: { name: 'B', lat: num('b-lat'), lon: num('b-lon'),
            height_agl_m: num('b-h', 20), tx_power_w: num('b-p', 0.5),
            feedline_loss_db: num('b-f', 1.5), antenna: antennaSpec('b') },
      freq_mhz: num('lnk-freq', 5760), mode: $('lnk-mode').value,
      ground: $('lnk-ground').value, polarisation: $('lnk-pol').value,
      k_factor: num('lnk-k', 1.333), clutter_m: num('lnk-clutter', 0),
      use_buildings: $('lnk-buildings').checked,
      availability_pct: parseFloat($('lnk-avail').value),
      // ITM time availability. Over-horizon paths swing tens of dB across
      // this, so it is the difference between an average day and a lift.
      reliability: parseFloat($('lnk-rel').value),
      climate: parseInt($('lnk-climate').value),
      high_res_terrain: $('lnk-hires').checked,
    };
    // make sure both ends are on the map, so A/B are visible after a run
    writeSite('a', body.tx.lat, body.tx.lon);
    writeSite('b', body.rx.lat, body.rx.lon);
    const r = await runJob('/link', body, btn, 'Analysing');
    lastProfile = r; lastTerrain = null;
    renderLink(r);
    drawProfile(r);
    if (linkLine) map.removeLayer(linkLine);
    linkLine = L.polyline([[body.tx.lat, body.tx.lon], [body.rx.lat, body.rx.lon]],
      { color: r.is_los ? '#3fd68a' : '#ff6b6b', weight: 3 }).addTo(map);
    map.fitBounds(linkLine.getBounds(), { padding: [60, 60] });
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = 'Analyse path';
  }
}

function renderLink(r) {
  const vc = verdictClass(r.verdict);
  let html = `<div class="verdict ${vc}">
    <div class="big">${r.link_margin_db > 0 ? '+' : ''}${fmt(r.link_margin_db, 1)} dB</div>
    <div><div class="lbl">link margin</div><div>${r.verdict}</div></div></div>`;

  html += sect('Geometry');
  html += kv([
    ['Distance', fmt(r.distance_km, 3) + ' km'],
    ['Bearing A → B', fmt(r.tx_bearing_true_deg, 1) + '° true'],
    ['Bearing B → A', fmt(r.rx_bearing_true_deg, 1) + '° true'],
    ['Tilt at A', fmt(r.tx_tilt_deg, 2) + '°'],
    ['Tilt at B', fmt(r.rx_tilt_deg, 2) + '°'],
    ['A antenna top', fmt(r.tx_amsl_m, 1) + ' m AMSL'],
    ['B antenna top', fmt(r.rx_amsl_m, 1) + ' m AMSL'],
    ['Line of sight', r.is_los ? '<span style="color:var(--ok)">clear</span>'
                               : '<span style="color:var(--bad)">obstructed</span>'],
    ['Fresnel clearance', fmt(r.min_fresnel_fraction * 100, 0) + '% of F1' +
      (r.min_fresnel_fraction >= 0.6 ? '' : ' ⚠')],
    ['Worst point', fmt(r.worst_clearance_at_km, 2) + ' km · ' +
      fmt(r.worst_clearance_m, 1) + ' m'],
    ['F1 radius (max)', fmt(r.first_fresnel_radius_m, 1) + ' m'],
    ['Terrain data', `${r.terrain_source} @ ${fmt(r.terrain_resolution_m, 0)} m`],
    r.buildings && r.buildings.source === 'etak'
      ? ['Buildings (ETAK)', `${r.buildings.intersecting_points} pts · max ` +
         `${fmt(r.buildings.max_height_m, 0)} m`] : null,
  ]);

  html += sect('Path loss');
  html += kv([
    ['Free space', fmt(r.free_space_db, 1) + ' dB'],
    r.itm_loss_db !== null ? ['Longley-Rice', fmt(r.itm_loss_db, 1) + ' dB'] : null,
    r.itm_loss_db !== null ? ['ITM mode', `<span class="pill">${r.itm_mode}</span>`] : null,
    ['Diffraction', fmt(r.diffraction_db, 1) + ' dB'],
    r.gas_loss_db > 0.05 ? ['Atmospheric gases', fmt(r.gas_loss_db, 2) + ' dB'] : null,
    r.rain_loss_db > 0.05 ? [`Rain (${r.availability_pct}%)`, fmt(r.rain_loss_db, 2) + ' dB'] : null,
    ['<b>Total</b>', '<b>' + fmt(r.total_path_loss_db, 1) + ' dB</b>'],
  ]);

  html += sect('Budget A → B');
  html += kv([
    ['TX power', fmt(r.tx_power_dbm, 1) + ' dBm'],
    ['A antenna gain', fmt(r.tx_gain_dbi, 1) + ' dBi'],
    ['EIRP', fmt(r.eirp_dbm, 1) + ' dBm'],
    ['B antenna gain', fmt(r.rx_gain_dbi, 1) + ' dBi'],
    ['<b>Received level</b>', '<b>' + fmt(r.rx_level_dbm, 1) + ' dBm</b>' +
      (r.s_meter ? ' <span class="dim">' + r.s_meter + '</span>' : '')],
    ['Thermal noise floor', fmt(r.noise_floor_dbm, 1) + ' dBm'],
    ['SNR', fmt(r.snr_db, 1) + ' dB'],
    ['Sensitivity', fmt(r.sensitivity_dbm, 1) + ' dBm'],
    r.multipath_fade_margin_db > 0
      ? ['Multipath margin needed', fmt(r.multipath_fade_margin_db, 1) + ' dB'] : null,
  ]);

  if (r.reverse) {
    html += sect('Budget B → A');
    html += kv([
      ['EIRP', fmt(r.reverse.eirp_dbm, 1) + ' dBm'],
      ['Received level', fmt(r.reverse.rx_level_dbm, 1) + ' dBm' +
        (r.s9_dbm !== undefined
          ? ' ' + (sMeter(r.reverse.rx_level_dbm, r.s9_dbm) || '') : '')],
      ['Margin', fmt(r.reverse.link_margin_db, 1) + ' dB · ' + r.reverse.verdict],
    ]);
  }

  if (r.diffraction_edges && r.diffraction_edges.length) {
    html += sect('Obstacles');
    html += '<table class="grid"><tr><th>at km</th><th>height m</th><th>v</th><th>loss dB</th></tr>' +
      r.diffraction_edges.map(e =>
        `<tr><td>${fmt(e.distance_m / 1000, 2)}</td><td>${fmt(e.height_m, 0)}</td>` +
        `<td>${fmt(e.v, 2)}</td><td>${fmt(e.loss_db, 1)}</td></tr>`).join('') +
      '</table>';
  }

  if (r.recommended_tx_height_m || r.recommended_rx_height_m) {
    html += sect('To clear the path');
    html += kv([
      r.recommended_tx_height_m ? ['Raise A to', fmt(r.recommended_tx_height_m, 0) + ' m'] : null,
      r.recommended_rx_height_m ? ['or raise B to', fmt(r.recommended_rx_height_m, 0) + ' m'] : null,
    ]);
  }

  if (r.notes && r.notes.length) {
    html += '<ul class="notes">' + r.notes.map(n => `<li>${n}</li>`).join('') + '</ul>';
  }
  $('lnk-result').innerHTML = html;
}

async function runHeightMatrix() {
  try {
    const body = {
      tx: { name: 'A', lat: num('a-lat'), lon: num('a-lon'), height_agl_m: num('a-h'),
            antenna: { preset: 'isotropic' } },
      rx: { name: 'B', lat: num('b-lat'), lon: num('b-lon'), height_agl_m: num('b-h'),
            antenna: { preset: 'isotropic' } },
      freq_mhz: num('lnk-freq', 5760), k_factor: num('lnk-k', 1.333),
      clutter_m: num('lnk-clutter', 0), use_buildings: $('lnk-buildings').checked,
      high_res_terrain: $('lnk-hires').checked,
    };
    const r = await post('/link/best-heights', body);
    const h = r.heights_m;
    let html = sect('Fresnel clearance vs antenna heights') +
      '<div class="hint">Rows = end A height, columns = end B. Green is ≥60% of F1.</div>' +
      '<table class="grid"><tr><th>A\\B</th>' +
      h.map(x => `<th>${x}</th>`).join('') + '</tr>';
    r.fresnel_fraction.forEach((row, i) => {
      html += `<tr><td>${h[i]}</td>` + row.map(v => {
        const c = v >= 1 ? '#3fd68a' : v >= 0.6 ? '#8fd6a8' : v >= 0.3 ? '#ffc857' : '#ff6b6b';
        return `<td style="color:${c}">${v >= 9 ? '9+' : v.toFixed(1)}</td>`;
      }).join('') + '</tr>';
    });
    $('lnk-result').innerHTML = html + '</table>';
  } catch (e) { toast(e.message, true); }
}

/* --------------------------------------------------------- profile plot */
/* Draw buildings as distinct blocks from the ground up to the surface, so the
   operator can see a structure sitting in the path rather than have it blend
   into the terrain fill. Returns true if any building was drawn. */
function drawBuildingBars(c, X, Y, dk, ground, top) {
  const n = dk.length;
  const halfPx = n > 1 ? Math.max(1.5, Math.abs(X(dk[1]) - X(dk[0])) * 0.6) : 3;
  let any = false;
  c.save();
  c.fillStyle = 'rgba(185,140,255,.6)';
  c.strokeStyle = '#c7a6ff';
  c.lineWidth = 1;
  for (let i = 0; i < n; i++) {
    if (top[i] - ground[i] > 0.5) {
      any = true;
      const x = X(dk[i]), yt = Y(top[i]), yb = Y(ground[i]);
      c.fillRect(x - halfPx, yt, halfPx * 2, yb - yt);
      c.strokeRect(x - halfPx, yt, halfPx * 2, yb - yt);
    }
  }
  c.restore();
  return any;
}

function drawProfile(r) {
  const p = r.profile;
  if (!p) return;
  $('profile').classList.remove('hidden');
  $('profile-title').textContent =
    `Path profile · ${fmt(r.distance_km, 2)} km · ${fmt(r.frequency_mhz, 0)} MHz · ` +
    `${r.terrain_source} @ ${fmt(r.terrain_resolution_m, 0)} m`;

  const cv = $('profile-canvas');
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  const c = cv.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, W, H);

  const pad = { l: 46, r: 12, t: 12, b: 24 };
  const x0 = pad.l, x1 = W - pad.r, y0 = pad.t, y1 = H - pad.b;
  const dmax = p.distance_km[p.distance_km.length - 1];

  const all = p.effective_terrain_m.concat(p.fresnel_upper_m, p.fresnel_lower_m,
                                           [r.tx_amsl_m, r.rx_amsl_m]);
  let lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
  const span = Math.max(hi - lo, 20);
  lo -= span * 0.08; hi += span * 0.12;

  const X = (d) => x0 + (d / dmax) * (x1 - x0);
  const Y = (m) => y1 - ((m - lo) / (hi - lo)) * (y1 - y0);

  // grid
  c.strokeStyle = '#232c36'; c.lineWidth = 1;
  c.fillStyle = '#8b98a8'; c.font = '10px ui-monospace, monospace';
  for (let i = 0; i <= 4; i++) {
    const m = lo + (hi - lo) * i / 4, y = Y(m);
    c.beginPath(); c.moveTo(x0, y); c.lineTo(x1, y); c.stroke();
    c.textAlign = 'right'; c.fillText(Math.round(m) + ' m', x0 - 6, y + 3);
  }
  const stepKm = dmax > 60 ? 20 : dmax > 20 ? 5 : dmax > 6 ? 2 : dmax > 2 ? 0.5 : 0.2;
  c.textAlign = 'center';
  for (let d = 0; d <= dmax + 1e-9; d += stepKm) {
    const x = X(d);
    c.beginPath(); c.moveTo(x, y0); c.lineTo(x, y1); c.stroke();
    c.fillText(d.toFixed(stepKm < 1 ? 1 : 0), x, y1 + 14);
  }

  const path = (ys, close) => {
    c.beginPath();
    p.distance_km.forEach((d, i) => {
      const x = X(d), y = Y(ys[i]);
      i ? c.lineTo(x, y) : c.moveTo(x, y);
    });
    if (close) { c.lineTo(X(dmax), y1); c.lineTo(X(0), y1); c.closePath(); }
  };

  // Fresnel ellipsoid
  c.fillStyle = 'rgba(78,161,255,.10)';
  c.beginPath();
  p.distance_km.forEach((d, i) => { const x = X(d), y = Y(p.fresnel_upper_m[i]); i ? c.lineTo(x, y) : c.moveTo(x, y); });
  for (let i = p.distance_km.length - 1; i >= 0; i--) c.lineTo(X(p.distance_km[i]), Y(p.fresnel_lower_m[i]));
  c.closePath(); c.fill();

  // 60% line
  c.strokeStyle = 'rgba(78,161,255,.45)'; c.setLineDash([4, 4]); c.lineWidth = 1;
  path(p.fresnel_60_lower_m); c.stroke(); c.setLineDash([]);

  // bare terrain with earth curvature applied. effective_terrain_m already
  // includes buildings; peel them off so the ground and the buildings can be
  // drawn as separate layers. bulge = effective - surface (buildings live in
  // surface), so bare-with-bulge = terrain + bulge.
  const hasSurf = Array.isArray(p.surface_m) && Array.isArray(p.terrain_m);
  const bareEff = hasSurf
    ? p.terrain_m.map((t, i) => t + (p.effective_terrain_m[i] - p.surface_m[i]))
    : p.effective_terrain_m;
  c.fillStyle = '#2b3541'; c.strokeStyle = '#4b5a6b'; c.lineWidth = 1.2;
  path(bareEff, true); c.fill();
  path(bareEff); c.stroke();

  // buildings, as distinct blocks rising from the ground to the surface
  if (hasSurf) drawBuildingBars(c, X, Y, p.distance_km, bareEff, p.effective_terrain_m);

  // line of sight
  c.strokeStyle = r.is_los ? '#3fd68a' : '#ff6b6b';
  c.lineWidth = 1.8; path(p.los_m); c.stroke();

  // masts
  [[0, r.tx_amsl_m], [dmax, r.rx_amsl_m]].forEach(([d, top], i) => {
    const x = X(d), yt = Y(top);
    const base = Y(p.effective_terrain_m[i ? p.effective_terrain_m.length - 1 : 0]);
    c.strokeStyle = i ? '#ffb454' : '#4ea1ff'; c.lineWidth = 2;
    c.beginPath(); c.moveTo(x, base); c.lineTo(x, yt); c.stroke();
    c.fillStyle = i ? '#ffb454' : '#4ea1ff';
    c.beginPath(); c.arc(x, yt, 3.5, 0, Math.PI * 2); c.fill();
  });

  // worst point
  const wi = p.distance_km.reduce((best, d, i) =>
    Math.abs(d - r.worst_clearance_at_km) < Math.abs(p.distance_km[best] - r.worst_clearance_at_km) ? i : best, 0);
  c.strokeStyle = '#ffc857'; c.setLineDash([2, 3]);
  c.beginPath(); c.moveTo(X(p.distance_km[wi]), Y(p.effective_terrain_m[wi]));
  c.lineTo(X(p.distance_km[wi]), Y(p.los_m[wi])); c.stroke(); c.setLineDash([]);

  c.fillStyle = '#8b98a8'; c.textAlign = 'right';
  c.fillText('km', x1, y1 + 14);
}

/* --------------------------------------------------- terrain-only tool */
async function runTerrain() {
  const btn = $('lnk-terrain');
  btn.disabled = true; btn.textContent = 'Loading…';
  try {
    // ensure both ends are placed and labelled on the map
    writeSite('a', num('a-lat'), num('a-lon'));
    writeSite('b', num('b-lat'), num('b-lon'));
    const body = {
      lat1: num('a-lat'), lon1: num('a-lon'),
      lat2: num('b-lat'), lon2: num('b-lon'),
      high_res: $('lnk-hires').checked,
      use_buildings: $('lnk-buildings').checked,
    };
    const r = await post('/profile', body);
    lastTerrain = r; lastProfile = null;
    drawTerrainProfile(r);
    if (linkLine) map.removeLayer(linkLine);
    linkLine = L.polyline([[body.lat1, body.lon1], [body.lat2, body.lon2]],
      { color: '#8b98a8', weight: 2, dashArray: '6 5' }).addTo(map);
    map.fitBounds(linkLine.getBounds(), { padding: [60, 60] });
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = 'Terrain profile';
  }
}

function drawTerrainProfile(r) {
  $('profile').classList.remove('hidden');
  const hasB = r.buildings && r.buildings.source === 'etak' &&
               r.buildings.intersecting_points;
  $('profile-title').textContent =
    `Terrain · ${fmt(r.distance_km, 2)} km · ${r.source} @ ` +
    `${fmt(r.resolution_m, 0)} m` +
    (hasB ? ` · buildings on ${r.buildings.intersecting_points} pts (max ` +
            `${fmt(r.buildings.max_height_m, 0)} m)` : '');

  const cv = $('profile-canvas');
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  const c = cv.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, W, H);

  const dk = r.distance_km_series, terr = r.elevation_m, surf = r.surface_m || terr;
  const pad = { l: 46, r: 12, t: 12, b: 24 };
  const x0 = pad.l, x1 = W - pad.r, y0 = pad.t, y1 = H - pad.b;
  const dmax = dk[dk.length - 1] || 1;

  let lo = Math.min.apply(null, terr), hi = Math.max.apply(null, surf);
  const span = Math.max(hi - lo, 20);
  lo -= span * 0.08; hi += span * 0.12;

  const X = (d) => x0 + (d / dmax) * (x1 - x0);
  const Y = (m) => y1 - ((m - lo) / (hi - lo)) * (y1 - y0);

  c.strokeStyle = '#232c36'; c.lineWidth = 1;
  c.fillStyle = '#8b98a8'; c.font = '10px ui-monospace, monospace';
  for (let i = 0; i <= 4; i++) {
    const m = lo + (hi - lo) * i / 4, y = Y(m);
    c.beginPath(); c.moveTo(x0, y); c.lineTo(x1, y); c.stroke();
    c.textAlign = 'right'; c.fillText(Math.round(m) + ' m', x0 - 6, y + 3);
  }
  const stepKm = dmax > 60 ? 20 : dmax > 20 ? 5 : dmax > 6 ? 2 : dmax > 2 ? 0.5 : 0.2;
  c.textAlign = 'center';
  for (let d = 0; d <= dmax + 1e-9; d += stepKm) {
    const x = X(d);
    c.beginPath(); c.moveTo(x, y0); c.lineTo(x, y1); c.stroke();
    c.fillText(d.toFixed(stepKm < 1 ? 1 : 0), x, y1 + 14);
  }

  const line = (ys, close) => {
    c.beginPath();
    dk.forEach((d, i) => { const x = X(d), y = Y(ys[i]); i ? c.lineTo(x, y) : c.moveTo(x, y); });
    if (close) { c.lineTo(X(dmax), y1); c.lineTo(X(0), y1); c.closePath(); }
  };

  // bare terrain
  c.fillStyle = '#2b3541'; c.strokeStyle = '#4b5a6b'; c.lineWidth = 1.2;
  line(terr, true); c.fill();
  line(terr); c.stroke();
  // buildings as distinct blocks rising from the terrain
  if (hasB) drawBuildingBars(c, X, Y, dk, terr, surf);

  c.fillStyle = '#8b98a8'; c.textAlign = 'right';
  c.fillText('km', x1, y1 + 14);
}

/* ------------------------------------------------------------------- HF */
async function runHF() {
  const btn = $('hf-run');
  btn.disabled = true; btn.textContent = 'Predicting…';
  try {
    const preset = $('hf-ant').value;
    const body = {
      site: { name: 'Station', lat: num('hf-lat'), lon: num('hf-lon'),
              height_agl_m: num('hf-h', 12), tx_power_w: num('hf-pwr', 100),
              antenna: { preset, height_m: num('hf-h', 12),
                         orientation_deg: num('hf-orient', 90) } },
      band: $('hf-band').value, mode: $('hf-mode').value,
      ground: $('hf-ground').value,
      use_live_ionosphere: $('hf-live').checked,
      when: $('hf-when').value ? $('hf-when').value + ':00' : null,
    };
    const r = await post('/hf', body);
    renderHF(r);
    drawHFRings(r, body.site);
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = 'Predict';
  }
}

function renderHF(r) {
  const io = r.ionosphere;
  let html = sect('Ionosphere');
  html += kv([
    ['foF2', fmt(io.fo_f2_mhz, 2) + ' MHz'],
    ['foE', fmt(io.fo_e_mhz, 2) + ' MHz'],
    ['F2 height', fmt(io.h_f2_km, 0) + ' km'],
    ['MUF(3000)', fmt(io.muf_3000_mhz, 1) + ' MHz'],
    ['Daylight', io.is_day ? 'yes' : 'no'],
    ['Source', `<span class="pill">${io.source}</span>`],
  ]);

  html += sect('Antenna');
  html += kv([
    ['Type', r.antenna.name],
    ['Height', fmt(r.antenna.height_m, 1) + ' m = ' +
      fmt(r.antenna.height_wavelengths, 2) + ' λ'],
    ['Peak gain', fmt(r.antenna.peak_gain_dbi, 1) + ' dBi'],
    ['Takeoff angle', fmt(r.antenna.takeoff_deg, 0) + '°'],
    ['Gain at zenith', fmt(r.nvis.gain_at_zenith_dbi, 1) + ' dBi'],
    ['Polarisation', r.polarisation],
  ]);
  html += '<canvas class="pattern" id="hf-pattern"></canvas>';

  html += sect('Reach');
  html += kv([
    ['Ground wave', fmt(r.ground_wave_range_km, 0) + ' km'],
    ['NVIS', r.nvis.usable
      ? `usable to ${fmt(r.nvis.range_km, 0)} km`
      : '<span style="color:var(--warn)">not usable now</span>'],
  ]);

  html += sect('By distance');
  html += '<table class="grid"><tr><th>km</th><th>mode</th><th>TOA</th>' +
    '<th>gain</th><th>dBm</th><th>S</th></tr>' +
    r.distances.map(d => {
      const cls = d.usable ? 'usable' : 'weak';
      return `<tr class="${cls}"><td>${d.distance_km}</td><td>${d.mode}</td>` +
        `<td>${d.mode === 'none' ? '—' : fmt(d.takeoff_deg, 0) + '°'}</td>` +
        `<td>${d.mode === 'none' ? '—' : fmt(d.antenna_gain_dbi, 1)}</td>` +
        `<td>${d.mode === 'none' ? '—' : fmt(d.rx_level_dbm, 0)}</td>` +
        `<td>${d.s_meter}</td></tr>`;
    }).join('') + '</table>';

  if (r.notes.length) {
    html += '<ul class="notes">' + r.notes.map(n => `<li>${n}</li>`).join('') + '</ul>';
  }
  $('hf-result').innerHTML = html;
  drawPattern(r.elevation_pattern, r.distances);
}

function drawPattern(pat, modes) {
  const cv = $('hf-pattern');
  if (!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = 200;
  cv.width = W * dpr; cv.height = H * dpr;
  const c = cv.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, W, H);

  const cx = W / 2, cy = H - 14, R = Math.min(W / 2 - 14, H - 26);
  const gmax = Math.max.apply(null, pat.gain_dbi);
  const top = Math.ceil(gmax / 5) * 5;
  const range = 30; // dB shown
  const rad = (g) => Math.max(0, (g - (top - range)) / range) * R;

  // rings
  c.strokeStyle = '#232c36'; c.fillStyle = '#5d6b7c';
  c.font = '9px ui-monospace, monospace'; c.textAlign = 'left';
  for (let i = 1; i <= 3; i++) {
    const rr = R * i / 3;
    c.beginPath(); c.arc(cx, cy, rr, Math.PI, 2 * Math.PI); c.stroke();
    c.fillText((top - range + range * i / 3).toFixed(0) + ' dBi', cx + 3, cy - rr + 9);
  }
  c.strokeStyle = '#232c36';
  [0, 15, 30, 45, 60, 75, 90].forEach(a => {
    const t = a * Math.PI / 180;
    c.beginPath(); c.moveTo(cx, cy);
    c.lineTo(cx + R * Math.cos(t), cy - R * Math.sin(t)); c.stroke();
    c.lineTo(cx - R * Math.cos(t), cy - R * Math.sin(t));
    if (a % 30 === 0 && a > 0) {
      c.fillStyle = '#5d6b7c'; c.textAlign = 'center';
      c.fillText(a + '°', cx + (R + 8) * Math.cos(t), cy - (R + 8) * Math.sin(t) + 3);
    }
  });

  // lobe
  c.beginPath();
  pat.angles_deg.forEach((a, i) => {
    const t = a * Math.PI / 180, rr = rad(pat.gain_dbi[i]);
    const x = cx + rr * Math.cos(t), y = cy - rr * Math.sin(t);
    i ? c.lineTo(x, y) : c.moveTo(x, y);
  });
  for (let i = pat.angles_deg.length - 1; i >= 0; i--) {
    const t = (180 - pat.angles_deg[i]) * Math.PI / 180, rr = rad(pat.gain_dbi[i]);
    c.lineTo(cx + rr * Math.cos(t), cy - rr * Math.sin(t));
  }
  c.closePath();
  c.fillStyle = 'rgba(78,161,255,.22)'; c.fill();
  c.strokeStyle = '#4ea1ff'; c.lineWidth = 1.5; c.stroke();

  // mark the takeoff angles the usable hops need
  c.fillStyle = '#ffb454';
  (modes || []).filter(m => m.usable && m.hops > 0).forEach(m => {
    const t = m.takeoff_deg * Math.PI / 180, rr = rad(
      pat.gain_dbi[Math.min(pat.gain_dbi.length - 1, Math.round(m.takeoff_deg))]);
    c.beginPath();
    c.arc(cx + rr * Math.cos(t), cy - rr * Math.sin(t), 2.5, 0, Math.PI * 2);
    c.fill();
  });
}

function drawHFRings(r, site) {
  if (hfRings) map.removeLayer(hfRings);
  const feats = [];
  if (r.ground_wave_range_km > 1) {
    feats.push(L.circle([site.lat, site.lon], {
      radius: r.ground_wave_range_km * 1000, color: '#3fd68a', weight: 2,
      fillOpacity: 0.10 }).bindPopup(
      `Ground wave: ${fmt(r.ground_wave_range_km, 0)} km`));
  }
  if (r.nvis.usable) {
    feats.push(L.circle([site.lat, site.lon], {
      radius: r.nvis.range_km * 1000, color: '#ffb454', weight: 2,
      dashArray: '5 5', fillOpacity: 0.06 }).bindPopup(
      `NVIS: out to ${fmt(r.nvis.range_km, 0)} km`));
  }
  r.footprints.forEach(f => {
    feats.push(L.polygon(f.ring.map(p => [p[1], p[0]]), {
      color: '#4ea1ff', weight: 1, fill: false, opacity: 0.55 })
      .bindPopup(`${f.distance_km} km via ${f.mode} · ${f.s_meter} (${f.rx_level_dbm} dBm)`));
  });
  hfRings = L.layerGroup(feats).addTo(map);
  if (feats.length) map.fitBounds(L.featureGroup(feats).getBounds(), { padding: [30, 30] });
}

/* ----------------------------------------------------------------- data */
async function loadDataLayers() {
  const b = map.getBounds();
  const bbox = `min_lat=${b.getSouth()}&min_lon=${b.getWest()}` +
    `&max_lat=${b.getNorth()}&max_lon=${b.getEast()}`;
  let msg = [];

  if ($('layer-masts').checked) {
    const r = await api('/sites?' + bbox + '&limit=2000');
    if (mastLayer) map.removeLayer(mastLayer);
    mastLayer = L.layerGroup(r.sites.map(s =>
      L.marker([s.lat, s.lon], { icon: pin('marker-mast') }).bindPopup(
        `<b>${s.name || s.kind}</b><br>` +
        `<span class="k">type</span> ${s.kind}<br>` +
        (s.height_m ? `<span class="k">height</span> ${s.height_m} m<br>` : '') +
        (s.operator ? `<span class="k">operator</span> ${s.operator}<br>` : '') +
        `<span class="k">source</span> ${s.source}`))).addTo(map);
    msg.push(`${r.sites.length} structures`);
  } else if (mastLayer) { map.removeLayer(mastLayer); mastLayer = null; }

  if ($('layer-tx').checked) {
    let q = '/transmitters?' + bbox + '&limit=2000';
    if ($('layer-tx-am').checked) q += '&amateur_only=true';
    if ($('data-search').value) q += '&search=' + encodeURIComponent($('data-search').value);
    const r = await api(q);
    if (txLayer) map.removeLayer(txLayer);
    txLayer = L.layerGroup(r.transmitters.map(t =>
      L.marker([t.lat, t.lon], { icon: pin('marker-tx') }).bindPopup(
        `<b>${t.callsign || t.name || 'transmitter'}</b><br>` +
        `<span class="k">TX</span> ${t.tx_mhz} MHz` +
        (t.rx_mhz ? ` <span class="k">RX</span> ${t.rx_mhz} MHz` : '') + '<br>' +
        (t.service ? `<span class="k">service</span> ${t.service}<br>` : '') +
        (t.erp_w ? `<span class="k">ERP</span> ${t.erp_w} W<br>` : '') +
        (t.antenna_height_m ? `<span class="k">antenna</span> ${t.antenna_height_m} m<br>` : '') +
        (t.licensee ? `<span class="k">licensee</span> ${t.licensee}<br>` : '') +
        `<span class="k">source</span> ${t.source}`))).addTo(map);
    msg.push(`${r.transmitters.length} transmitters`);
  } else if (txLayer) { map.removeLayer(txLayer); txLayer = null; }

  const st = await api('/registry/status');
  $('data-result').innerHTML = sect('In view') +
    (msg.length ? kv(msg.map(m => [m.split(' ')[1], m.split(' ')[0]])) : '<div class="hint">Nothing selected</div>') +
    sect('Database') +
    kv([['Structures', st.sites], ['Transmitters', st.transmitters],
        ['Amateur', st.amateur]]) +
    (st.last_runs.length ? sect('Last refreshes') + '<table class="grid">' +
      st.last_runs.slice(0, 6).map(r =>
        `<tr><td>${r.source}</td><td>${r.records}</td>` +
        `<td>${r.ok ? '✓' : '✗'}</td></tr>`).join('') + '</table>' : '');
}

/* ------------------------------------------------------------------ init */
function initMap() {
  map = L.map('map', { center: [59.3, 25.0], zoom: 7, zoomControl: true });
  const layers = {};
  const keys = (META.tile_sources || ['osm']);
  keys.forEach((k, i) => {
    if (!BASEMAPS[k]) return;
    const l = BASEMAPS[k]();
    baseLayers[k] = l;
    layers[BASEMAP_LABELS[k] || k] = l;
    if (i === 0) { l.addTo(map); currentBaseKey = k; }
  });
  if (!layers['Finland topographic'] && BASEMAPS.mml_topo) {
    const l = BASEMAPS.mml_topo();
    baseLayers.mml_topo = l;
    layers['Finland topographic'] = l;
  }
  layerCtl = L.control.layers(layers, {}, { position: 'topright' }).addTo(map);
  L.control.scale({ imperial: false }).addTo(map);

  // keep our idea of the active base layer in sync when the user picks one
  map.on('baselayerchange', (e) => {
    for (const k in baseLayers) if (baseLayers[k] === e.layer) currentBaseKey = k;
  });

  map.on('click', (e) => {
    if (aiming) {
      // Aim the beam at whatever was clicked, so "which way does it point"
      // is a thing you do on the map rather than a number you guess.
      const bd = bearingDistance(num('cov-lat'), num('cov-lon'),
                                 e.latlng.lat, e.latlng.lng);
      $('cov-bear').value = Math.round(bd.bearing_deg);
      $('cov-bear-pick').classList.remove('armed');
      aiming = false;
      drawBeam();
      toast(`Beam aimed at ${Math.round(bd.bearing_deg)}\u00b0 true`);
      return;
    }
    if (!picking) return;
    writeSite(picking, e.latlng.lat, e.latlng.lng);
    document.querySelectorAll('button.pick').forEach(b => b.classList.remove('armed'));
    picking = null;
  });
  // Nothing is placed on the map until the user picks a point, edits a
  // coordinate, or runs an analysis.
}

/* ------------------------------------------------------- mode <-> map */
const MODE_MARKERS = { coverage: ['cov'], link: ['a', 'b'], hf: ['hf'], data: [] };

function toggle(layer, show) {
  if (!layer) return;
  if (show && !map.hasLayer(layer)) layer.addTo(map);
  else if (!show && map.hasLayer(layer)) map.removeLayer(layer);
}

function setBasemap(key) {
  if (!key || !baseLayers[key] || key === currentBaseKey) return;
  if (currentBaseKey && baseLayers[currentBaseKey])
    map.removeLayer(baseLayers[currentBaseKey]);
  baseLayers[key].addTo(map);
  baseLayers[key].bringToBack();
  currentBaseKey = key;
}

function linkBasemapKey() {
  return baseLayers.opentopo ? 'opentopo'
       : baseLayers.maaamet_reljeef ? 'maaamet_reljeef' : null;
}

/* Show only the map objects that belong to the active mode, and switch to a
   terrain basemap while planning a link. */
function setMode(mode) {
  currentMode = mode;
  Object.keys(SITE_STYLE).forEach(k => {
    if (markers[k]) toggle(markers[k], (MODE_MARKERS[mode] || []).includes(k));
  });
  toggle(coverageOverlay, mode === 'coverage');
  toggle(beamLine, mode === 'coverage');
  toggle(horizonLayer, mode === 'coverage');
  toggle(linkLine, mode === 'link');
  toggle(hfRings, mode === 'hf');
  toggle(mastLayer, mode === 'data');
  toggle(txLayer, mode === 'data');
  if (mode !== 'link') $('profile').classList.add('hidden');

  const linkKey = linkBasemapKey();
  if (mode === 'link' && linkKey) {
    if (currentBaseKey !== linkKey) { baseBeforeLink = currentBaseKey; setBasemap(linkKey); }
  } else if (baseBeforeLink) {
    setBasemap(baseBeforeLink); baseBeforeLink = null;
  }
}

function bindUI() {
  document.querySelectorAll('.tabs button').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('.tabs button').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      document.querySelectorAll('.tab').forEach(t => t.classList.add('hidden'));
      $('tab-' + b.dataset.tab).classList.remove('hidden');
      setMode(b.dataset.tab);
      if (b.dataset.tab === 'data') loadDataLayers().catch(e => toast(e.message, true));
    });
  });

  $('cov-bear-pick').addEventListener('click', () => {
    aiming = !aiming;
    $('cov-bear-pick').classList.toggle('armed', aiming);
    if (aiming) toast('Click the map to aim the beam');
  });
  $('cov-bear').addEventListener('change', drawBeam);
  $('cov-ant').addEventListener('change', drawBeam);

  document.querySelectorAll('button.pick').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('button.pick').forEach(x => x.classList.remove('armed'));
      aiming = false;
      $('cov-bear-pick').classList.remove('armed');
      picking = b.dataset.target;
      b.classList.add('armed');
      toast('Click the map to place ' + picking.toUpperCase());
    });
  });

  ['a-lat', 'a-lon', 'b-lat', 'b-lon'].forEach(id =>
    $(id).addEventListener('change', () => {
      const k = id[0];
      writeSite(k, num(k + '-lat'), num(k + '-lon'));
    }));
  ['cov-range'].forEach(id => $(id).addEventListener('change',
    () => { warmForCoverage(); drawBeam(); }));
  ['cov-lat', 'cov-lon'].forEach(id => $(id).addEventListener('change',
    () => writeSite('cov', num('cov-lat'), num('cov-lon'))));
  ['hf-lat', 'hf-lon'].forEach(id => $(id).addEventListener('change',
    () => writeSite('hf', num('hf-lat'), num('hf-lon'))));

  $('cov-run').addEventListener('click', runCoverage);
  $('cov-horizon').addEventListener('click', runHorizon);
  $('lnk-run').addEventListener('click', runLink);
  $('lnk-terrain').addEventListener('click', runTerrain);
  $('lnk-heights').addEventListener('click', runHeightMatrix);
  $('hf-run').addEventListener('click', runHF);
  $('profile-close').addEventListener('click', () => $('profile').classList.add('hidden'));
  $('data-reload').addEventListener('click', () => loadDataLayers().catch(e => toast(e.message, true)));
  ['layer-masts', 'layer-tx', 'layer-tx-am'].forEach(id =>
    $(id).addEventListener('change', () => loadDataLayers().catch(e => toast(e.message, true))));

  $('data-refresh-masts').addEventListener('click', async (e) => {
    e.target.disabled = true; e.target.textContent = 'Fetching from OSM…';
    try {
      const r = await post('/registry/refresh?source=masts', {});
      $('data-admin').textContent = JSON.stringify(r.masts);
      await loadDataLayers();
    } catch (err) { toast(err.message, true); }
    e.target.disabled = false; e.target.textContent = 'Refresh masts from OSM/ETAK';
  });

  $('data-discover').addEventListener('click', async () => {
    try {
      const d = await api('/registry/jvis/discover');
      $('data-admin').innerHTML = sect('JVIS discovery') + kv([
        ['Reachable', d.ok ? 'yes' : 'no'],
        ['URL', `<span style="font-size:10px">${d.url}</span>`],
        ['Form fields', Object.keys(d.fields || {}).length],
        ['Columns found', (d.columns || []).length],
        ['Exports', (d.export_links || []).length],
      ]) + `<div class="hint">${d.note}</div>` +
      ((d.columns || []).length ? '<div class="hint">' + d.columns.join(' · ') + '</div>' : '');
    } catch (e) { toast(e.message, true); }
  });

  $('data-refresh-jvis').addEventListener('click', async (e) => {
    e.target.disabled = true; e.target.textContent = 'Harvesting (slow)…';
    try {
      const r = await post('/registry/refresh?source=jvis', {});
      $('data-admin').textContent = JSON.stringify(r.jvis);
      await loadDataLayers();
    } catch (err) { toast(err.message, true); }
    e.target.disabled = false; e.target.textContent = 'Harvest JVIS register';
  });

  map.on('mousemove', updateReadout);
  map.on('mouseout', () => $('readout').classList.add('hidden'));
  window.addEventListener('resize', () => {
    if ($('profile').classList.contains('hidden')) return;
    if (lastTerrain) drawTerrainProfile(lastTerrain);
    else if (lastProfile) drawProfile(lastProfile);
  });
}

(async function boot() {
  try {
    await loadMeta();
    initMap();
    bindUI();
    setMode('coverage');
  } catch (e) {
    toast('Startup failed: ' + e.message, true);
    console.error(e);
  }
})();
})();
