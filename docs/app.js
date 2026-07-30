/* =============================================================
   미너비니 KR · 트레이딩 터미널 프론트엔드
   정적 JSON만 읽습니다. 서버 로직 없음.
   차트는 canvas에 직접 그립니다. 외부 라이브러리 없음.
   ============================================================= */

const S = {
  data: null,
  view: 'setup',      // 'setup' | 'stage1'
  market: 'ALL',
  query: '',
  selected: null,     // 선택된 종목코드
  rows: [],           // 현재 표시 중인 행 (키보드 이동용)
};

const $ = (id) => document.getElementById(id);
const nf = new Intl.NumberFormat('ko-KR');

const num = (v, d = 0) => (v == null || Number.isNaN(v)) ? '–' : nf.format(Number(Number(v).toFixed(d)));
const sign = (v, d = 1) => (v == null || Number.isNaN(v)) ? '–' : (v > 0 ? '+' : '') + Number(v).toFixed(d);
const cls = (v) => v > 0 ? 'up' : v < 0 ? 'down' : '';
const eok = (v) => v == null ? '–' : Math.abs(v) >= 10000 ? (v/10000).toFixed(1)+'조' : nf.format(Math.round(v))+'억';

// 축 눈금을 사람 눈에 자연스러운 값(1·2·5의 배수)으로 배치
function niceTicks(lo, hi, count) {
  const range = hi - lo; if (range <= 0) return [lo];
  const raw = range / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const start = Math.ceil(lo/step)*step;
  const out = [];
  for (let v=start; v<=hi; v+=step) out.push(v);
  return out;
}

/* ---------- 부팅 ---------- */

async function boot() {
  try {
    await loadDates();
    await load('data/latest.json');
  } catch (e) { fail(e); }
}

function fail(e) {
  $('loading').hidden = true;
  const box = $('error'); box.hidden = false;
  box.textContent = '데이터를 불러오지 못했습니다: ' + e.message +
    ' — 로컬이면 docs 폴더에서 http.server를 띄우고 localhost로 접속하세요.';
}

async function loadDates() {
  try {
    const r = await fetch('data/history/index.json');
    if (!r.ok) return;
    const { dates } = await r.json();
    const sel = $('datePicker'); sel.innerHTML = '';
    // 첫 옵션은 안내 문구. 과거를 고를 때만 이동.
    const head = document.createElement('option');
    head.value = '';
    head.textContent = dates.length > 1 ? `과거 조회 (${dates.length}일)` : '과거 기록 없음';
    head.disabled = false; head.selected = true;
    sel.appendChild(head);
    dates.forEach((d, i) => {
      const o = document.createElement('option');
      o.value = i === 0 ? 'data/latest.json' : `data/history/${d}.json`;
      o.textContent = d + (i === 0 ? ' · 최신' : '');
      sel.appendChild(o);
    });
    if (dates.length <= 1) sel.disabled = true;
    sel.onchange = (e) => { if (e.target.value) load(e.target.value).catch(fail); };
  } catch {}
}

async function load(url) {
  $('loading').hidden = false; $('app').hidden = true;
  const r = await fetch(url, { cache: 'no-cache' });
  if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
  S.data = await r.json();
  render();
  $('loading').hidden = true; $('app').hidden = false;
}

/* ---------- 렌더 ---------- */

function render() {
  $('demoBanner').hidden = !S.data.demo;
  $('stamp').textContent = '갱신 ' + (S.data.generated_at || '').replace('T', ' ').slice(5, 16);
  $('asofDate').textContent = S.data.as_of || '–';
  renderRegime();
  renderInternals();
  renderSectors();
  renderList();

  // 첫 종목 자동 선택. 없으면(하락장) 중앙에 시장폭 대형 차트를 띄운다.
  if (S.rows.length && !S.rows.find(r => r.code === S.selected)) {
    select(S.rows[0].code);
  } else if (S.selected && S.rows.find(r => r.code === S.selected)) {
    renderChart(S.selected);
  } else {
    renderMarketHero();
  }
}

/* 종목이 없을 때 중앙을 채우는 시장폭 대형 차트.
   하락장에서 사용자가 응시해야 할 바로 그 지표를 죽은 공간 대신 놓는다. */
function renderMarketHero() {
  const it = S.data.internals;
  $('chartHead').innerHTML = '';
  $('detailStrip').innerHTML = '';
  const ro = $('ohlcReadout'); if (ro) ro.innerHTML = '';
  const empty = $('chartEmpty');
  const wrap = $('chartWrap');
  // 기존 캔버스 숨기고 히어로 컨테이너 확보
  $('priceChart').style.display = 'none';
  $('volChart').style.display = 'none';
  const ch = $('crosshair'); if (ch) ch.style.display = 'none';

  let host = document.getElementById('marketHero');
  if (!host) {
    host = document.createElement('div');
    host.id = 'marketHero';
    host.className = 'market-hero';
    wrap.appendChild(host);
  }
  host.style.display = 'flex';
  empty.hidden = true;

  if (!it || !it.series || it.series.dates.length < 5) {
    host.innerHTML = `<div class="mh-empty">시장 내부 데이터가 없습니다.</div>`;
    return;
  }

  const s = it.series, sm = it.summary, th = it.thresholds;
  const phaseLabel = { healthy:'양호', caution:'주의', risk_off:'위험', unknown:'—' };
  const phaseCol = { healthy:'var(--sig-ok)', caution:'var(--sig-warn)', risk_off:'var(--sig-risk)', unknown:'var(--text-3)' };
  const arrow = (v) => v==null ? '' :
    v>0 ? `<span class="up">▲ ${v.toFixed(1)}</span>` :
    v<0 ? `<span class="down">▼ ${Math.abs(v).toFixed(1)}</span>` : '─';

  host.innerHTML = `
    <div class="mh-head">
      <div class="mh-title">
        <span class="mh-dot" style="background:${phaseCol[sm.phase]}"></span>
        <div>
          <div class="mh-label">시장 폭 · 200일선 위 종목 비율</div>
          <div class="mh-verdict" style="color:${phaseCol[sm.phase]}">${phaseLabel[sm.phase]} 국면</div>
        </div>
      </div>
      <div class="mh-metric">
        <div class="mh-big">${sm.above_ma200 ?? '–'}<span class="mh-unit">%</span></div>
        <div class="mh-sub">20일 ${arrow(sm.above_ma200_20d)}</div>
      </div>
    </div>
    <div class="mh-chart" id="mhChart"></div>
    <div class="mh-foot">
      <div class="mh-legend">
        <span><i style="background:var(--sig-warn)"></i>주의선 ${th.caution}%</span>
        <span><i style="background:var(--sig-risk)"></i>위험선 ${th.risk_off}%</span>
      </div>
      <div class="mh-note">이 비율이 바닥에서 돌아서는 게 지수보다 앞선 반등 신호입니다. 종목을 선택하면 개별 차트로 전환됩니다.</div>
    </div>`;

  // 대형 라인 차트 (SVG, 반응형)
  drawBreadthHero($('mhChart'), s.dates, s.above_ma200, s.above_ma200_smooth, th);
}

function drawBreadthHero(host, dates, raw, smooth, th) {
  const W = host.clientWidth || 800, H = host.clientHeight || 300;
  const padL = 0, padR = 48, padT = 12, padB = 22;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const lo = 0, hi = 100;
  const yOf = (v) => padT + plotH * (1 - (v - lo) / (hi - lo));
  const xOf = (i) => padL + plotW * (i / (raw.length - 1));

  const line = (arr) => {
    let d = '', started = false;
    arr.forEach((v, i) => {
      if (v == null) { started = false; return; }
      d += `${started ? 'L' : 'M'}${xOf(i).toFixed(1)},${yOf(v).toFixed(1)}`;
      started = true;
    });
    return d;
  };
  const area = (arr) => {
    const l = line(arr);
    if (!l) return '';
    const first = arr.findIndex(v => v != null);
    const last = arr.length - 1 - [...arr].reverse().findIndex(v => v != null);
    return `${l}L${xOf(last).toFixed(1)},${yOf(0).toFixed(1)}L${xOf(first).toFixed(1)},${yOf(0).toFixed(1)}Z`;
  };

  // Y축 눈금
  const ticks = [0, 25, 40, 50, 75, 100];
  const grid = ticks.map(t => `
    <line x1="${padL}" y1="${yOf(t).toFixed(1)}" x2="${padL+plotW}" y2="${yOf(t).toFixed(1)}"
      stroke="var(--hairline)" stroke-width="1"/>
    <text x="${padL+plotW+8}" y="${yOf(t).toFixed(1)}" fill="var(--text-4)" font-size="10"
      font-family="var(--mono)" dominant-baseline="middle">${t}</text>`).join('');

  // 국면 밴드
  const band = (y1, y2, color) =>
    `<rect x="${padL}" y="${yOf(y2).toFixed(1)}" width="${plotW}" height="${(yOf(y1)-yOf(y2)).toFixed(1)}"
       fill="${color}" opacity="0.05"/>`;

  // X축 날짜 (양끝 + 중간)
  const xLabels = [0, Math.floor(raw.length/2), raw.length-1].map(i => {
    if (!dates[i]) return '';
    const anchor = i === 0 ? 'start' : i === raw.length-1 ? 'end' : 'middle';
    return `<text x="${xOf(i).toFixed(1)}" y="${H-6}" fill="var(--text-4)" font-size="10"
      font-family="var(--mono)" text-anchor="${anchor}">${dates[i]}</text>`;
  }).join('');

  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="100%" preserveAspectRatio="none">
    ${band(th.caution, 100, 'var(--sig-ok)')}
    ${band(th.risk_off, th.caution, 'var(--sig-warn)')}
    ${band(0, th.risk_off, 'var(--sig-risk)')}
    ${grid}
    <line x1="${padL}" y1="${yOf(th.caution).toFixed(1)}" x2="${padL+plotW}" y2="${yOf(th.caution).toFixed(1)}"
      stroke="var(--sig-warn)" stroke-width="1" stroke-dasharray="4 4" opacity="0.5"/>
    <line x1="${padL}" y1="${yOf(th.risk_off).toFixed(1)}" x2="${padL+plotW}" y2="${yOf(th.risk_off).toFixed(1)}"
      stroke="var(--sig-risk)" stroke-width="1" stroke-dasharray="4 4" opacity="0.5"/>
    <path d="${area(raw)}" fill="var(--line-200)" opacity="0.06"/>
    <path d="${line(raw)}" fill="none" stroke="var(--line-200)" stroke-width="1.5" opacity="0.9"/>
    ${xLabels}
  </svg>`;
}

function renderRegime() {
  const r = S.data.regime, s = S.data.stats;
  const label = { risk_on:'정상 진입', neutral:'중립', caution:'선별 진입', risk_off:'관망' };
  const note = {
    risk_on:'셋업이 나오면 계획대로 실행',
    neutral:'지수는 버티나 확신은 이르다',
    caution:'포지션 줄이고 손절 타이트하게',
    risk_off:'신규 진입 멈추고 현금 확보',
  };
  const kospi = r.kospi?.available ? stateWord(r.kospi.state) : '–';
  const kosdaq = r.kosdaq?.available ? stateWord(r.kosdaq.state) : '–';
  const mkt = (o) => o?.available
    ? `${o.above_short?'50↑':'50↓'} · ${o.above_long?'200↑':'200↓'}` : '';

  $('regimeStrip').innerHTML = `
    <div class="gauge gauge-verdict">
      <span class="verdict-pill v-${r.verdict}">
        <span class="verdict-dot"></span>${label[r.verdict] || r.verdict}
      </span>
      <span class="verdict-note">${note[r.verdict] || ''}</span>
    </div>
    <div class="gauge">
      <div class="gauge-metric">
        <span class="gauge-label">코스피</span>
        <span class="gauge-value sm">${kospi}</span>
        <span class="gauge-sub">${mkt(r.kospi)}</span>
      </div>
      <div class="gauge-metric">
        <span class="gauge-label">코스닥</span>
        <span class="gauge-value sm">${kosdaq}</span>
        <span class="gauge-sub">${mkt(r.kosdaq)}</span>
      </div>
    </div>
    <div class="gauge">
      <div class="gauge-metric">
        <span class="gauge-label">200일선 위</span>
        <span class="gauge-value">${r.breadth_above_ma200}%</span>
        <span class="gauge-sub">시장 폭</span>
      </div>
      <div class="gauge-metric">
        <span class="gauge-label">1단계 통과</span>
        <span class="gauge-value">${s.stage1}</span>
        <span class="gauge-sub">${r.tt_pass_pct}%</span>
      </div>
      <div class="gauge-metric">
        <span class="gauge-label">셋업 / 돌파</span>
        <span class="gauge-value">${s.stage2} / ${s.breakout}</span>
        <span class="gauge-sub">VCP</span>
      </div>
    </div>`;
}
function stateWord(s){ return {uptrend:'상승', neutral:'중립', downtrend:'하락'}[s] || s; }

/* ---------- 시장 내부 (우상단) ---------- */

function renderInternals() {
  const it = S.data.internals;
  const el = $('internals');
  if (!it || !it.series || !it.series.dates || it.series.dates.length < 5) {
    el.innerHTML = `<p class="dim" style="font-size:11px">
      시장 내부 지표를 계산할 데이터가 부족합니다.</p>`;
    return;
  }

  const s = it.series, sm = it.summary, th = it.thresholds;
  const phaseLabel = { healthy:'양호', caution:'주의', risk_off:'위험', unknown:'—' };
  const phaseColor = { healthy:'var(--sig-ok)', caution:'var(--sig-warn)', risk_off:'var(--sig-risk)', unknown:'var(--text-3)' };

  // 20일 방향 화살표
  const arrow = (v) => v == null ? '' :
    v > 0 ? `<span class="up">▲ ${v.toFixed(1)}</span>` :
    v < 0 ? `<span class="down">▼ ${Math.abs(v).toFixed(1)}</span>` :
    `<span class="dim">─</span>`;

  el.innerHTML = `
    <div class="int-phase" style="border-color:${phaseColor[sm.phase]}">
      <span class="int-phase-dot" style="background:${phaseColor[sm.phase]}"></span>
      <span class="int-phase-label">시장 국면 · ${phaseLabel[sm.phase]}</span>
      <span class="int-phase-sub">200일선 위 ${sm.above_ma200 ?? '—'}% · 20일 ${arrow(sm.above_ma200_20d)}</span>
    </div>

    ${intChart('200일선 위 %', s.dates, s.above_ma200, 'var(--line-200)',
      sm.above_ma200, '%', [{y:th.caution,c:'var(--sig-warn)'},{y:th.risk_off,c:'var(--sig-risk)'}], 0, 100)}

    ${intChart('50일선 위 %', s.dates, s.above_ma50, 'var(--line-50)',
      sm.above_ma50, '%', [], 0, 100)}

    ${intChart('신고가 − 신저가', s.dates, s.nh_nl, 'var(--line-nhnl)',
      sm.nh_nl, '', [{y:0,c:'var(--hairline-2)'}])}

    ${s.tt_count ? intChart('1단계 통과 종목', s.dates, s.tt_count, 'var(--line-tt)',
      sm.tt_count, '', []) : ''}

    <p class="int-note">200일선 위 비율이 ${th.risk_off}% 아래면 위험, ${th.caution}% 아래면 주의 구간입니다.
    이 비율이 바닥에서 돌아서는 게 지수보다 앞선 반등 신호입니다.</p>`;
}

function intChart(label, dates, arr, color, latest, unit, bands, forceLo, forceHi) {
  const W = 260, H = 46;
  const v = arr.map(x => x == null ? null : +x);
  const valid = v.filter(x => x != null);
  if (valid.length < 2) return '';

  let lo = forceLo != null ? forceLo : Math.min(...valid);
  let hi = forceHi != null ? forceHi : Math.max(...valid);
  // 밴드 임계선도 범위에 포함
  (bands || []).forEach(b => { lo = Math.min(lo, b.y); hi = Math.max(hi, b.y); });
  const span = (hi - lo) || 1;
  const y = (val) => H - ((val - lo) / span) * (H - 6) - 3;
  const step = W / (v.length - 1);

  // 결측 구간을 건너뛰는 라인
  let d = '', started = false;
  v.forEach((val, i) => {
    if (val == null) { started = false; return; }
    d += `${started ? 'L' : 'M'}${(i*step).toFixed(1)},${y(val).toFixed(1)}`;
    started = true;
  });

  const bandLines = (bands || []).map(b =>
    `<line x1="0" y1="${y(b.y).toFixed(1)}" x2="${W}" y2="${y(b.y).toFixed(1)}"
       stroke="${b.c}" stroke-width="1" stroke-dasharray="3 3" opacity="0.5"/>`).join('');

  const latestTxt = latest == null ? '—' : (Number.isInteger(latest) ? latest : latest.toFixed(1)) + unit;

  return `<div class="int-block">
    <div class="int-top">
      <span class="int-label">${label}</span>
      <span class="int-value">${latestTxt}</span>
    </div>
    <svg class="int-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      ${bandLines}
      <path d="${d}" fill="none" stroke="${color}" stroke-width="1.4"/>
    </svg>
  </div>`;
}

/* ---------- 섹터 히트맵 (우하단) ---------- */

function renderSectors() {
  const secs = S.data.sectors || [];
  const el = $('sectorHeat');
  if (!secs.length) { el.innerHTML = `<p class="dim" style="font-size:11px;padding:6px">섹터 데이터 없음</p>`; return; }
  const maxRs = Math.max(...secs.map(s => s.rs_median ?? 0), 1);
  el.innerHTML = secs.map(s => {
    const rs = s.rs_median ?? 0;
    const color = heatColor(rs);
    const w = Math.round((rs / maxRs) * 100);
    return `<div class="sec-row">
      <span class="sec-bar" style="width:${w}%;background:${color}"></span>
      <span class="sec-name">${s.sector}</span>
      <span class="sec-rs" style="color:${color}">${rs || '–'}</span>
      <span class="sec-pass">${s.stage1_count}/${s.count}</span>
    </div>`;
  }).join('');
}
function heatColor(rs){
  if (rs >= 75) return 'var(--sig-ok)';
  if (rs >= 55) return 'var(--sig-warn)';
  if (rs >= 40) return 'var(--text-2)';
  return 'var(--sig-risk)';
}

/* ---------- 워치리스트 (좌) ---------- */

function currentRows() {
  let rows = S.view === 'setup' ? (S.data.stage2 || []) : (S.data.stage1 || []);
  if (S.market !== 'ALL') rows = rows.filter(r => r.market === S.market);
  if (S.query) {
    const q = S.query.toLowerCase();
    rows = rows.filter(r => r.name.toLowerCase().includes(q) || r.code.includes(q));
  }
  return rows;
}

function renderList() {
  $('cntSetup').textContent = (S.data.stage2 || []).length;
  $('cntStage1').textContent = (S.data.stage1 || []).length;

  const rows = currentRows();
  S.rows = rows;
  const el = $('watchList');

  if (!rows.length) {
    if (S.view === 'setup') {
      const b = S.data.regime?.breadth_above_ma200 ?? '–';
      el.innerHTML = `
        <div class="empty-hero">
          <svg width="40" height="40" viewBox="0 0 40 40" fill="none" aria-hidden="true">
            <circle cx="20" cy="20" r="15" stroke="var(--hairline-2)" stroke-width="1.5"/>
            <path d="M20 11 L20 20 L26 23" stroke="var(--text-3)" stroke-width="1.5" stroke-linecap="round"/>
          </svg>
          <p class="empty-title">오늘은 기다리는 날입니다</p>
          <p class="empty-body">조건을 통과한 셋업이 없습니다.<br>
          시장 폭 ${b}%, 아직 살 자리가 아닙니다.</p>
          <p class="empty-quote">"현금도 포지션이다."</p>
        </div>`;
    } else {
      el.innerHTML = `
        <div class="empty-hero">
          <p class="empty-title">해당 종목 없음</p>
          <p class="empty-body">필터를 넓히거나<br>다른 날짜를 선택하세요.</p>
        </div>`;
    }
    return;
  }

  el.innerHTML = rows.map(r => {
    const v = r.vcp;
    const badge = v
      ? (r.signal === 'breakout'
          ? '<span class="badge badge-breakout">돌파</span>'
          : '<span class="badge badge-setup">셋업</span>')
      : '';
    const mkt = r.market === 'KOSPI' ? '코스피' : '코스닥';
    const chgCls = r.chg_pct > 0 ? 'up' : r.chg_pct < 0 ? 'down' : 'dim';
    const meta = v
      ? `피벗 ${sign(v.dist_to_pivot_pct)}% · VCP ${v.score}`
      : `고점 ${sign(r.from_high_pct)}% · ${eok(r.marcap_eok)}`;
    return `<div class="row ${r.code===S.selected?'sel':''}" data-code="${r.code}" role="option">
      <div class="row-name">${r.name} ${badge}</div>
      <div class="row-rs">${r.rs_rating ?? '–'}</div>
      <div class="row-meta">
        <span class="row-mkt">${mkt}</span>
        <span class="row-code">${r.code}</span>
        <span class="row-chg ${chgCls}">${sign(r.chg_pct, 2)}%</span>
      </div>
      <div class="row-meta dim" style="grid-column:1">${meta}</div>
    </div>`;
  }).join('');

  el.querySelectorAll('.row').forEach(node => {
    node.onclick = () => select(node.dataset.code);
  });
}

function select(code) {
  S.selected = code;
  document.querySelectorAll('.row').forEach(n =>
    n.classList.toggle('sel', n.dataset.code === code));
  const node = document.querySelector(`.row[data-code="${code}"]`);
  if (node) node.scrollIntoView({ block: 'nearest' });
  renderChart(code);
}

function findRow(code) {
  return (S.data.stage2 || []).find(r => r.code === code)
      || (S.data.stage1 || []).find(r => r.code === code);
}

/* ---------- 중앙 차트 ---------- */

function renderChart(code) {
  const row = findRow(code);
  const chart = (S.data.charts || {})[code];
  $('chartEmpty').hidden = true;
  // 시장폭 히어로 숨기고 캔들 캔버스 복원
  const hero = document.getElementById('marketHero');
  if (hero) hero.style.display = 'none';
  $('priceChart').style.display = 'block';
  $('volChart').style.display = 'block';
  const chx = $('crosshair'); if (chx) chx.style.display = 'block';

  // 헤더
  $('chartHead').innerHTML = row ? `
    <div class="ch-title">
      <div><span class="ch-name">${row.name}</span><span class="ch-code">${row.code} · ${row.market==='KOSPI'?'코스피':'코스닥'}</span></div>
      <div class="ch-price-wrap">
        <span class="ch-price">${num(row.close)}</span>
        <span class="ch-chg ${cls(row.chg_pct)}">${sign(row.chg_pct,2)}%</span>
      </div>
    </div>
    <div class="ch-stats">
      <div class="ch-stat"><span class="ch-stat-label">RS</span><span class="ch-stat-value">${row.rs_rating ?? '–'}</span></div>
      <div class="ch-stat"><span class="ch-stat-label">고점대비</span><span class="ch-stat-value ${cls(row.from_high_pct)}">${sign(row.from_high_pct)}%</span></div>
      <div class="ch-stat"><span class="ch-stat-label">저점대비</span><span class="ch-stat-value up">${sign(row.from_low_pct)}%</span></div>
      <div class="ch-stat"><span class="ch-stat-label">시총</span><span class="ch-stat-value">${eok(row.marcap_eok)}</span></div>
      ${row.vcp ? `<div class="ch-stat"><span class="ch-stat-label">수축</span><span class="ch-stat-value">${row.vcp.contractions.map(c=>c.depth_pct.toFixed(0)+'%').join('→')}</span></div>` : ''}
    </div>` : '';

  // 차트 위에 뜨는 OHLC 리드아웃 (호버 시 갱신)
  const ro = $('ohlcReadout');
  if (ro) ro.innerHTML = '';

  // 하단 지표 스트립
  renderDetailStrip(row);

  if (!chart) {
    $('priceChart').hidden = true; $('volChart').hidden = true;
    $('chartEmpty').hidden = false;
    $('chartEmpty').textContent = '이 종목은 차트 데이터가 없습니다.';
    return;
  }
  $('priceChart').hidden = false; $('volChart').hidden = false;
  drawCandles(chart, row);
}

function renderDetailStrip(row) {
  if (!row) { $('detailStrip').innerHTML = ''; return; }
  const f = row.flow || {}, v = row.vcp;
  const cells = [
    ['50일선', num(row.ma50)],
    ['150일선', num(row.ma150)],
    ['200일선', num(row.ma200)],
    ['수급 20일', f.smart_eok==null?'–':`<span class="${cls(f.smart_eok)}">${sign(f.smart_eok,0)}억</span>`],
  ];
  if (v) {
    cells.push(['피벗', num(v.pivot)]);
    cells.push(['손절', num(v.stop_price)]);
    cells.push(['리스크', v.risk_pct==null?'–':`${v.risk_pct}%`]);
  }
  $('detailStrip').innerHTML = cells.map(([l,val]) =>
    `<div class="ds-item"><div class="ds-label">${l}</div><div class="ds-value">${val}</div></div>`).join('');
}

/* ---------- 캔들 차트 엔진 (시그니처) ----------
   TradingView 급 인터랙션: 크로스헤어, 호버 OHLC 리드아웃,
   날짜/가격 축, 피벗·손절 오버레이. 정적 그림이 아니라 도구.
------------------------------------------------------- */

const CHART = { geom: null, chart: null, row: null };

function drawCandles(chart, row) {
  const priceCv = $('priceChart'), volCv = $('volChart');
  const wrap = $('chartWrap');
  const W = Math.floor(wrap.clientWidth - 32);
  const totalH = wrap.clientHeight - 32;
  const vH = 72, gap = 10;
  const pH = Math.max(240, totalH - vH - gap);
  const dpr = window.devicePixelRatio || 1;

  for (const [cv, h] of [[priceCv, pH], [volCv, vH]]) {
    cv.width = W * dpr; cv.height = h * dpr;
    cv.style.width = W + 'px'; cv.style.height = h + 'px';
    const g = cv.getContext('2d'); g.setTransform(dpr,0,0,dpr,0,0);
    g.clearRect(0,0,W,h);
  }
  volCv.style.marginTop = gap + 'px';

  const candles = chart.candles;
  const dates = chart.dates || [];
  const n = candles.length;
  const padR = 62, padT = 10, padB = 4;
  const plotW = W - padR;

  let lo = Infinity, hi = -Infinity;
  candles.forEach(c => { if(c[2]!=null) lo=Math.min(lo,c[2]); if(c[1]!=null) hi=Math.max(hi,c[1]); });
  [chart.ma50, chart.ma150, chart.ma200].forEach(arr =>
    arr && arr.forEach(x => { if(x!=null){ lo=Math.min(lo,x); hi=Math.max(hi,x);} }));
  if (chart.pivot) { hi=Math.max(hi,chart.pivot); lo=Math.min(lo,chart.pivot); }
  if (chart.stop)  { lo=Math.min(lo,chart.stop); }
  const padv = (hi-lo)*0.06; lo-=padv; hi+=padv;
  const span = (hi-lo)||1;
  const yOf = (p) => padT + (pH-padT-padB) * (1 - (p-lo)/span);
  const xOf = (i) => (i+0.5) * (plotW/n);
  const cw = Math.max(1.2, plotW/n * 0.66);

  const css = getComputedStyle(document.documentElement);
  const C = (name) => css.getPropertyValue(name).trim();
  const g = priceCv.getContext('2d');
  const gv = volCv.getContext('2d');

  // geom 저장 (크로스헤어가 참조)
  CHART.geom = { W, pH, vH, padR, padT, padB, plotW, lo, hi, span, yOf, xOf, cw, n, dpr };
  CHART.chart = chart; CHART.row = row;

  // --- 가로 그리드 + 우측 가격축 ---
  g.font = '10px "Spline Sans Mono", monospace';
  g.textBaseline = 'middle';
  const nice = niceTicks(lo, hi, 5);
  nice.forEach(p => {
    const y = yOf(p);
    if (y < padT || y > pH-padB) return;
    g.strokeStyle = C('--hairline'); g.lineWidth = 1;
    g.beginPath(); g.moveTo(0, y+0.5); g.lineTo(plotW, y+0.5); g.stroke();
    g.fillStyle = C('--text-3'); g.textAlign = 'left';
    g.fillText(nf.format(Math.round(p)), plotW+8, y);
  });

  // --- 날짜축 (하단, 거래량 캔버스 아래 여백에) ---
  g.textAlign = 'center'; g.fillStyle = C('--text-4');
  const tickEvery = Math.ceil(n/6);
  for (let i=0; i<n; i+=tickEvery) {
    if (!dates[i]) continue;
    const x = xOf(i);
    if (x > plotW-20) continue;
    g.fillText(dates[i].slice(2).replace(/-/g,'.'), x, pH-4);
  }

  // --- 피벗/손절/52주 고점 오버레이 ---
  const dashed = (yv, color, txt, align) => {
    const y = yOf(yv);
    g.save(); g.setLineDash([5,4]); g.strokeStyle = color; g.lineWidth = 1; g.globalAlpha = 0.85;
    g.beginPath(); g.moveTo(0,y+0.5); g.lineTo(plotW,y+0.5); g.stroke(); g.restore();
    g.fillStyle = color; g.textAlign = 'left'; g.textBaseline = 'bottom';
    g.font = '9px "Spline Sans Mono", monospace';
    g.fillText(txt, 3, y-3);
    g.textBaseline = 'middle';
  };
  if (chart.pivot) dashed(chart.pivot, C('--sig-warn'), '피벗 '+nf.format(Math.round(chart.pivot)));
  if (chart.stop)  dashed(chart.stop, C('--up'), '손절 '+nf.format(Math.round(chart.stop)));
  if (!chart.pivot && chart.hi52) dashed(chart.hi52, C('--text-3'), '52주 고점');

  // --- 캔들 ---
  const upC = C('--up'), downC = C('--down');
  candles.forEach((c,i) => {
    const [o,high,low,close] = c;
    if (close==null) return;
    const x = xOf(i);
    const rising = close >= o;
    const col = rising ? upC : downC;
    g.strokeStyle = col; g.fillStyle = col; g.lineWidth = 1;
    g.beginPath(); g.moveTo(x+0.5, yOf(high)); g.lineTo(x+0.5, yOf(low)); g.stroke();
    const y1 = yOf(o), y2 = yOf(close);
    const top = Math.min(y1,y2), bh = Math.max(1, Math.abs(y1-y2));
    if (rising) { g.lineWidth=1; g.strokeRect(x-cw/2+0.5, top+0.5, cw-1, bh); g.globalAlpha=0.25; g.fillRect(x-cw/2, top, cw, bh); g.globalAlpha=1; }
    else { g.fillRect(x-cw/2, top, cw, bh); }
  });

  // --- 이동평균 ---
  const maLine = (arr, color, wgt) => {
    if (!arr) return;
    g.strokeStyle = color; g.lineWidth = wgt; g.beginPath();
    let started = false;
    arr.forEach((v,i) => {
      if (v==null) return;
      const x = xOf(i), y = yOf(v);
      if (!started) { g.moveTo(x,y); started = true; } else g.lineTo(x,y);
    });
    g.stroke();
  };
  maLine(chart.ma50,  C('--line-50'), 1.4);
  maLine(chart.ma150, 'rgba(154,164,180,0.55)', 1.2);
  maLine(chart.ma200, C('--line-200'), 1.2);

  // --- 범례 (상단 좌측) ---
  g.textAlign='left'; g.textBaseline='middle'; g.font='9px "Spline Sans Mono", monospace';
  [['MA50',C('--line-50')],['MA150','rgba(154,164,180,0.8)'],['MA200',C('--line-200')]]
    .reduce((x,[t,c])=>{ g.fillStyle=c; g.fillText('—— '+t, x, padT+6); return x+58; }, 4);

  // --- 거래량 ---
  let vmax = 0; candles.forEach(c => vmax = Math.max(vmax, c[4]||0));
  vmax = vmax || 1;
  const avg50 = candles.slice(-51,-1).reduce((s,c)=>s+(c[4]||0),0)/50 || 1;
  candles.forEach((c,i) => {
    const vol = c[4]||0, x = xOf(i);
    const rising = c[3] >= c[0];
    gv.fillStyle = rising ? 'rgba(241,101,96,0.4)' : 'rgba(90,156,240,0.4)';
    const bh = (vol/vmax) * (vH-2);
    gv.fillRect(x-cw/2, vH-bh, cw, bh);
  });
  const lastVol = candles[n-1][4]||0;
  if (lastVol > avg50*1.5) {
    gv.fillStyle = C('--sig-warn');
    const bh = (lastVol/vmax)*(vH-2);
    gv.fillRect(xOf(n-1)-cw/2, vH-bh, cw, bh);
  }

  // 크로스헤어 오버레이 캔버스 준비
  ensureCrosshair(wrap, W, pH, vH, gap, dpr);
  updateReadout(n-1);  // 초기: 최신 봉
}

/* --- 크로스헤어 레이어 --- */
function ensureCrosshair(wrap, W, pH, vH, gap, dpr) {
  let ov = $('crosshair');
  if (!ov) {
    ov = document.createElement('canvas');
    ov.id = 'crosshair';
    ov.style.cssText = 'position:absolute;left:24px;top:24px;pointer-events:none;z-index:5';
    wrap.appendChild(ov);
    const host = $('priceChart').parentElement;
    host.addEventListener('mousemove', onChartMove);
    host.addEventListener('mouseleave', () => { clearCrosshair(); updateReadout(CHART.geom?CHART.geom.n-1:0); });
  }
  const H = pH + gap + vH;
  ov.width = W*dpr; ov.height = H*dpr;
  ov.style.width = W+'px'; ov.style.height = H+'px';
  const g = ov.getContext('2d'); g.setTransform(dpr,0,0,dpr,0,0);
  ov.__H = H;
}

function onChartMove(e) {
  const geo = CHART.geom; if (!geo) return;
  const wrap = $('chartWrap');
  const rect = wrap.getBoundingClientRect();
  const x = e.clientX - rect.left - 24;
  if (x < 0 || x > geo.plotW) { clearCrosshair(); return; }
  const i = Math.max(0, Math.min(geo.n-1, Math.round(x/(geo.plotW/geo.n) - 0.5)));
  drawCrosshair(i, e.clientY - rect.top - 24);
  updateReadout(i);
}

function drawCrosshair(i, my) {
  const ov = $('crosshair'); if (!ov) return;
  const geo = CHART.geom;
  const css = getComputedStyle(document.documentElement);
  const g = ov.getContext('2d');
  g.clearRect(0,0,ov.width,ov.height);
  const x = geo.xOf(i);
  g.strokeStyle = css.getPropertyValue('--text-3').trim();
  g.globalAlpha = 0.5; g.lineWidth = 1; g.setLineDash([3,3]);
  g.beginPath(); g.moveTo(x+0.5,0); g.lineTo(x+0.5,ov.__H); g.stroke();
  if (my!=null && my>0 && my<geo.pH) {
    g.beginPath(); g.moveTo(0,my+0.5); g.lineTo(geo.plotW,my+0.5); g.stroke();
    const p = geo.lo + geo.span*(1-(my-geo.padT)/(geo.pH-geo.padT-geo.padB));
    g.setLineDash([]); g.globalAlpha=1;
    g.fillStyle = css.getPropertyValue('--surface-3').trim();
    g.fillRect(geo.plotW, my-8, geo.padR, 16);
    g.fillStyle = css.getPropertyValue('--text').trim();
    g.font='10px "Spline Sans Mono", monospace'; g.textAlign='left'; g.textBaseline='middle';
    g.fillText(nf.format(Math.round(p)), geo.plotW+8, my);
  }
  g.globalAlpha=1; g.setLineDash([]);
}
function clearCrosshair(){ const ov=$('crosshair'); if(ov){ const g=ov.getContext('2d'); g.clearRect(0,0,ov.width,ov.height);} }

// 호버 봉의 OHLC를 헤더 우측 리드아웃에 표시
function updateReadout(i) {
  const c = CHART.chart, geo = CHART.geom;
  if (!c || !c.candles[i]) return;
  const [o,h,l,cl,v] = c.candles[i];
  const d = (c.dates||[])[i] || '';
  const el = $('ohlcReadout');
  if (!el) return;
  const chg = i>0 ? (cl/c.candles[i-1][3]-1)*100 : 0;
  const cls = chg>0?'up':chg<0?'down':'dim';
  el.innerHTML = `<span class="ro-date">${d}</span>
    <span class="ro-item">시 <b>${nf.format(Math.round(o))}</b></span>
    <span class="ro-item">고 <b>${nf.format(Math.round(h))}</b></span>
    <span class="ro-item">저 <b>${nf.format(Math.round(l))}</b></span>
    <span class="ro-item">종 <b class="${cls}">${nf.format(Math.round(cl))}</b></span>
    <span class="ro-item ${cls}">${sign(chg,2)}%</span>`;
}

/* ---------- 상세 시트 ---------- */

function openSheet(code) {
  const r = findRow(code); if (!r) return;
  const labels = S.data.condition_labels || {};
  const conds = Object.keys(labels).map(k => {
    const ok = r.tt[k];
    return `<div class="cond-item"><span class="cond-flag ${ok?'yes':'no'}">${ok?'✓':'✕'}</span>${labels[k]}</div>`;
  }).join('');
  const v = r.vcp, f = r.flow||{}, fu = r.fund||{};
  $('sheetBody').innerHTML = `
    <p class="sh-name">${r.name}</p><p class="sh-code">${r.code} · ${r.market}</p>
    <div class="sh-sec"><h3>Trend Template ${r.tt_passed}/8 · RS ${r.rs_rating??'–'}</h3>
      <div class="cond-grid">${conds}</div></div>
    ${v ? `<div class="sh-sec"><h3>VCP · 점수 ${v.score}</h3>
      <dl class="kv"><dt>수축</dt><dd>${v.contractions.map(c=>c.depth_pct.toFixed(1)+'%').join(' → ')}</dd></dl>
      <dl class="kv"><dt>베이스 길이</dt><dd>${v.base_days}일</dd></dl>
      <dl class="kv"><dt>피벗</dt><dd>${num(v.pivot)} (${sign(v.dist_to_pivot_pct)}%)</dd></dl>
      <dl class="kv"><dt>손절 라인</dt><dd>${num(v.stop_price)}</dd></dl>
      <dl class="kv"><dt>진입 대비 리스크</dt><dd>${v.risk_pct??'–'}%</dd></dl>
      <dl class="kv"><dt>신호</dt><dd class="${r.signal==='breakout'?'up':''}">${r.signal==='breakout'?'돌파 발생':'셋업 대기'}</dd></dl>
    </div>`:''}
    <div class="sh-sec"><h3>수급 20일 누적</h3>
      <dl class="kv"><dt>기관</dt><dd class="${cls(f.inst_eok)}">${sign(f.inst_eok,0)}억</dd></dl>
      <dl class="kv"><dt>외국인</dt><dd class="${cls(f.frgn_eok)}">${sign(f.frgn_eok,0)}억</dd></dl>
      <dl class="kv"><dt>합계</dt><dd class="${cls(f.smart_eok)}">${sign(f.smart_eok,0)}억</dd></dl></div>
    <div class="sh-sec"><h3>재무</h3>
      <dl class="kv"><dt>PER</dt><dd>${fu.per??'–'}</dd></dl>
      <dl class="kv"><dt>PBR</dt><dd>${fu.pbr??'–'}</dd></dl>
      <dl class="kv"><dt>흑자</dt><dd>${fu.profitable==null?'–':(fu.profitable?'예':'적자')}</dd></dl></div>`;
  $('sheet').hidden = false; $('scrim').hidden = false;
  $('sheetClose').focus();
}
function closeSheet(){ $('sheet').hidden = true; $('scrim').hidden = true; }

/* ---------- 이벤트 ---------- */

$('listSeg').onclick = (e) => {
  const b = e.target.closest('.seg-btn'); if (!b) return;
  document.querySelectorAll('#listSeg .seg-btn').forEach(x=>x.classList.remove('is-on'));
  b.classList.add('is-on'); S.view = b.dataset.view; renderList();
  if (S.rows.length) select(S.rows[0].code);
};
$('marketChips').onclick = (e) => {
  const b = e.target.closest('.chip'); if (!b) return;
  document.querySelectorAll('#marketChips .chip').forEach(x=>x.classList.remove('is-on'));
  b.classList.add('is-on'); S.market = b.dataset.market; renderList();
  if (S.rows.length) select(S.rows[0].code);
};
$('search').oninput = (e) => { S.query = e.target.value.trim(); renderList(); };

$('watchList').onkeydown = (e) => {
  if (!S.rows.length) return;
  const i = S.rows.findIndex(r => r.code === S.selected);
  if (e.key === 'ArrowDown') { e.preventDefault(); select(S.rows[Math.min(i+1,S.rows.length-1)].code); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); select(S.rows[Math.max(i-1,0)].code); }
  else if (e.key === 'Enter') { e.preventDefault(); if (S.selected) openSheet(S.selected); }
};

document.onkeydown = (e) => {
  if (e.key === '/' && document.activeElement.id !== 'search') { e.preventDefault(); $('search').focus(); }
  else if (e.key === 'Escape') closeSheet();
};

$('sheetClose').onclick = closeSheet;
$('scrim').onclick = closeSheet;
let _rzT;
window.addEventListener('resize', () => {
  clearTimeout(_rzT);
  _rzT = setTimeout(() => {
    if (S.selected && S.rows.find(r => r.code === S.selected)) renderChart(S.selected);
    else renderMarketHero();
  }, 120);
});

boot();
