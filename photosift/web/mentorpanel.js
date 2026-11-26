// Mentor mode, the parts shared by the photo viewer and the mentor page:
// the per-photo panel and the small charts.
import { html, useState, useEffect, api, fail, thumb, Icon, useApp } from './ui.js';

const toMin = (s) => { const [h, m] = s.split(':'); return +h * 60 + +m; };
const WINDOW = {
  morning_blue: ['blue', '清晨藍調'], morning_golden: ['golden', '清晨黃金時刻'], harsh: ['harsh', '正午頂光'],
  evening_golden: ['golden', '傍晚黃金時刻'], evening_blue: ['blue', '傍晚藍調'],
};
const LEVEL = { fix: '要修正', tip: '可以更好', good: '做得好', info: '參考' };

// A day laid out left to right, with the good light marked on it.
// windows: [{ key, start: 'HH:MM', end: 'HH:MM', label? }], mark: 'HH:MM' of the capture, if any.
export function DayTrack({ windows, mark, markLabel = '拍攝時間' }) {
  const segs = windows.map((w) => {
    const a = toMin(w.start);
    let b = toMin(w.end);
    if (b <= a) b = 1440; // a window that runs up to midnight
    const [kind, name] = WINDOW[w.key] || ['harsh', w.label];
    return { kind, name: w.label || name, a, b, text: `${w.start}–${w.end}` };
  });
  const kinds = [...new Set(segs.map((s) => s.kind))];
  return html`<div class="daytrack">
    <div class="track">
      ${segs.map((s) => html`<i class=${s.kind} title=${`${s.name} ${s.text}`}
        style=${`left:calc(${(s.a / 1440) * 100}% + 1px);width:calc(${((s.b - s.a) / 1440) * 100}% - 2px)`}></i>`)}
      ${mark && html`<b class="mark" title=${`${markLabel} ${mark}`} style=${`left:${(toMin(mark) / 1440) * 100}%`}></b>`}
    </div>
    <div class="ticks">${[0, 6, 12, 18, 24].map((h) => html`<span style=${`left:${(h / 24) * 100}%`}>${h}</span>`)}</div>
    <div class="legend">
      ${kinds.includes('blue') && html`<span><i class="blue"></i>藍調時刻</span>`}
      ${kinds.includes('golden') && html`<span><i class="golden"></i>黃金時刻</span>`}
      ${kinds.includes('harsh') && html`<span><i class="harsh"></i>正午頂光</span>`}
      ${mark && html`<span><b class="mark"></b>${markLabel} ${mark}</span>`}
    </div>
  </div>`;
}

// One named bar per row, all on the same baseline. rows: [{ name, n, share }]
export function Meter({ rows, unit = '張' }) {
  if (!rows || !rows.length) return html`<p class="small faint">沒有資料</p>`;
  return html`<div class="meter">${rows.map((r) => html`
    <span class="n" title=${r.name}>${r.name}</span>
    <div class="mbar" title=${`${r.name}：${r.n} ${unit}`}><i style=${`width:${Math.max(r.share * 100, 1)}%`}></i></div>
    <span class="v num">${Math.round(r.share * 100)}%<span class="faint">　${r.n} ${unit}</span></span>`)}</div>`;
}

// Photos per hour of the day.
export function Hours({ hours }) {
  const max = Math.max(...hours, 1);
  const peak = hours.indexOf(max);
  return html`<div class="hours">
    <div class="cols">${hours.map((n, h) => html`<div class="col" title=${`${h} 點：${n} 張`}>
      ${h === peak && n > 0 && html`<span class="cap num">${n}</span>`}
      <i style=${`height:${(n / max) * 100}%`}></i></div>`)}</div>
    <div class="ticks">${[0, 6, 12, 18].map((h) => html`<span style=${`left:${((h + 0.5) / 24) * 100}%`}>${h} 點</span>`)}</div>
  </div>`;
}

export function Finding({ f }) {
  return html`<div class="find ${f.level}">
    <div class="row gap-s"><span class="lv">${LEVEL[f.level]}</span><span class="small faint">${f.area}</span></div>
    <b>${f.title}</b>
    <p class="small muted">${f.detail}</p>
  </div>`;
}

export function Pair({ pair, onOpen, onExplained }) {
  const { status } = useApp();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState(pair.explained || null);
  useEffect(() => { setNote(pair.explained || null); }, [pair.better, pair.worse]);
  const local = status ? status.local : {};
  const explain = async () => {
    setBusy(true);
    try {
      const r = await api('/mentor/pair', { body: { better: pair.better, worse: pair.worse } });
      setNote(r); onExplained && onExplained(r);
    } catch (e) { fail(e); }
    setBusy(false);
  };
  return html`<div class="pair">
    <div class="two">
      <figure onClick=${() => onOpen && onOpen(pair.better)}><img loading="lazy" src=${thumb(pair.better)} alt="" /><figcaption>比較好的那張</figcaption></figure>
      <figure onClick=${() => onOpen && onOpen(pair.worse)}><img loading="lazy" src=${thumb(pair.worse)} alt="" /><figcaption>同一個場景的另一張</figcaption></figure>
    </div>
    <div class="small muted">${pair.diffs.join('　・　')}</div>
    <b>${pair.lesson}</b>
    ${note
      ? html`<p class="small">${note.difference}</p>${note.lesson && html`<p class="small muted">記得：${note.lesson}</p>`}`
      : html`<div><button class="btn sm" disabled=${busy || !local.ok} title=${local.ok ? '' : local.error} onClick=${explain}>
          ${busy ? html`<span class="spin"></span>` : html`<${Icon} name="chip" size=${13} />`} 請地端模型說明畫面上的差別</button></div>`}
  </div>`;
}

// What the mentor has to say about one photo, inside the viewer's side panel.
export function MentorPanel({ id, onJump }) {
  const { status } = useApp();
  const [d, setD] = useState(null);
  const [busy, setBusy] = useState(false);
  const local = status ? status.local : {};
  useEffect(() => { setD(null); api('/mentor/photo/' + id).then(setD).catch(fail); }, [id]);
  if (!d) return html`<div class="row muted"><span class="spin"></span> 讀取中…</div>`;
  const s = d.summary;
  const advise = async () => {
    setBusy(true);
    try { const r = await api(`/mentor/photo/${id}/advise`, { method: 'POST' }); setD({ ...d, advice: r.advice, advice_model: r.model }); } catch (e) { fail(e); }
    setBusy(false);
  };
  const day = d.light && d.light.day;
  const windows = day ? Object.keys(WINDOW).filter((k) => day[k]).map((k) => ({ key: k, start: day[k][0], end: day[k][1] })) : [];
  const facts = [s.focal35 && `等效 ${Math.round(s.focal35)}mm${s.focal35_exact ? '' : '（以實際焦距估計）'}`, s.mode,
    s.bias ? `曝光補償 ${s.bias > 0 ? '+' : ''}${s.bias} EV` : null, s.metering, s.flash ? '閃光燈有閃' : null].filter(Boolean);
  const a = d.advice;
  return html`<div class="mentor">
    <div>
      <h3>這張是怎麼拍的</h3>
      ${s.settings
        ? html`<div class="shot">${s.settings}</div>
          <p class="small muted">${facts.join('・')}</p>
          <p class="small muted">${s.scene ? `現場的亮度相當於${s.scene}（EV ${s.ev}）。` : ''}${s.safe ? `這個焦段手持的安全快門大約是 ${s.safe}。` : ''}</p>`
        : html`<p class="small muted">這張照片沒有記錄光圈、快門和 ISO，所以只能就畫面和光線給建議。</p>`}
      ${s.device === 'phone' && html`<p class="small muted">手機的光圈固定、快門和 ISO 由手機決定，所以下面的建議著重在你能控制的事。</p>`}
    </div>

    ${d.next && html`<div class="nextshot">
      <span class="small muted">下次遇到同樣的光線，試試</span>
      <div class="shot">${d.next}</div>
      <span class="small muted">曝光量和這張相同${d.shortfall >= 0.4 ? `，但還差約 ${d.shortfall} 級的光，需要腳架或補光` : ''}。原因見下方。</span>
    </div>`}

    ${d.findings.length > 0
      ? html`<div class="stack gap-s">${d.findings.map((f) => html`<${Finding} key=${f.key} f=${f} />`)}</div>`
      : html`<p class="small muted">程式沒有量到設定、水平或曝光上的問題。</p>`}

    ${d.light && html`<div>
      <h3>當天的光線</h3>
      <p class="small muted">你在 ${d.light.time} 拍，太陽高度 ${Math.round(d.light.elev)}°${d.light.compass ? `，在${d.light.compass}方` : ''}。
        ${day && day.sunrise ? `日出 ${day.sunrise}、日落 ${day.sunset}。` : ''}${d.light.exact ? '' : '相機沒有記錄時區，時間可能有一小時的誤差。'}</p>
      ${windows.length > 0 && html`<${DayTrack} windows=${windows} mark=${d.light.time} />
        <div class="kv small times">${windows.map((w) => html`<span>${WINDOW[w.key][1]}</span><span class="num">${w.start}–${w.end}</span>`)}</div>`}
    </div>`}

    ${d.pair && html`<div><h3>和同一個場景的另一張比</h3>
      <${Pair} pair=${d.pair} onOpen=${onJump} onExplained=${(n) => setD({ ...d, pair: { ...d.pair, explained: n } })} /></div>`}

    <div>
      <h3>構圖、站位和時機</h3>
      ${a
        ? html`<div class="advice">
            ${a.one_thing && html`<div class="one"><span class="small muted">下次最該做的一件事</span><b>${a.one_thing}</b></div>`}
            ${a.works && html`<p class="small"><b>做得好</b>　${a.works}</p>`}
            ${a.composition.map((c) => html`<p class="small"><b>構圖</b>　${c}</p>`)}
            ${a.position && html`<p class="small"><b>站位</b>　${a.position}</p>`}
            ${a.light && html`<p class="small"><b>光線</b>　${a.light}</p>`}
            ${a.intent && html`<p class="small faint">模型認為這張想表達：${a.intent}</p>`}
            <div><button class="btn ghost sm" disabled=${busy || !local.ok} onClick=${advise}>${busy ? html`<span class="spin"></span>` : ''} 重新講評</button>
              <span class="small faint">　${d.advice_model}，不花 token</span></div>
          </div>`
        : html`<p class="small muted">這部分要看畫面才知道。地端模型會看這張照片，告訴你下次站在哪裡、把什麼放進或移出畫面。</p>
          <button class="btn local" disabled=${busy || !local.ok} title=${local.ok ? '' : local.error} onClick=${advise}>
            ${busy ? html`<span class="spin"></span>` : html`<${Icon} name="chip" size=${14} />`} 請地端模型看這張</button>
          ${!local.ok && html`<p class="small faint">${local.error}</p>`}`}
    </div>
  </div>`;
}
