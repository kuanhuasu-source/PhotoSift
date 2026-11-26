// Mentor mode: habits across the library, a review of one outing, a plan for the next one, and practice.
import { html, useState, useEffect, useCallback, api, fail, toast, useApp, Icon, Dialog, usePickFolder, thumb, n0, fmtDay } from './ui.js';
import { PhotoViewer } from './library.js';
import { DayTrack, Meter, Hours, Pair } from './mentorpanel.js';

const TABS = [['habits', '拍攝習慣'], ['outings', '拍攝檢討'], ['plan', '下次拍攝'], ['practice', '練習']];
const LIGHT = [['golden', '黃金時刻'], ['blue', '藍調時刻'], ['day', '白天'], ['night', '夜晚']];
const STATE = {
  better: ['有進步', 'good'], worse: ['比之前差', 'bad'], same: ['和之前差不多', ''], waiting: ['等新照片', ''],
  measured: ['已量測', ''], manual: ['自己確認', ''],
};
const show = (v, unit) => (v == null ? '—' : unit === '%' ? `${Math.round(v * 100)}%` : `${v} ${unit}`);
const when = (ts) => new Date(ts * 1000).toLocaleDateString('zh-TW');
let prefill = null; // a place handed from an outing's review to the plan form

export function Mentor({ arg }) {
  const { go, jobTick, status } = useApp();
  const [o, setO] = useState(null);
  const load = useCallback(() => api('/mentor/overview').then(setO).catch(fail), []);
  useEffect(() => { load(); }, [jobTick, arg]);
  const [tab, sub] = (arg || 'habits').split('-');
  const cur = tab === 'outing' ? 'outings' : tab;
  return html`<div class="page">
    <div class="page-head"><h1>大師</h1>
      <span class="sub">其他頁面回答「哪一張最好」，這裡回答「下次怎麼拍得更好」。數字由程式量測和計算，講評才交給模型。</span></div>
    <div class="tabs">${TABS.map(([k, l]) => html`<button class="tab ${cur === k ? 'on' : ''}" onClick=${() => go('mentor', k)}>${l}
      ${k === 'practice' && o && o.exercises.filter((e) => e.status === 'open').length > 0 ? html` <span class="chip">${o.exercises.filter((e) => e.status === 'open').length}</span>` : ''}</button>`)}</div>
    ${status && !status.analysed ? html`<div class="empty"><p>先到「圖庫」加入資料夾並分析，這裡才有東西可以看。</p></div>`
      : !o ? null
      : tab === 'habits' ? html`<${Review} scope="library" name="整個相片庫" count=${o.photos} />`
      : tab === 'outings' ? html`<${Outings} events=${o.events} />`
      : tab === 'outing' ? html`<${Review} key=${sub} scope="event" id=${+sub} event=${o.events.find((e) => e.id === +sub)} />`
      : tab === 'plan' ? html`<${Plans} o=${o} id=${sub ? +sub : null} reload=${load} />`
      : html`<${Practice} list=${o.exercises} setList=${(list) => setO({ ...o, exercises: list })} />`}
  </div>`;
}

// ------------------------------------------------------------------ reviews

function Outings({ events }) {
  const { go } = useApp();
  if (!events.length) return html`<div class="empty"><p>還沒有事件。分析照片後，同一次外出拍的照片會自動歸成一個事件。</p></div>`;
  return html`<div>
    <p class="muted" style="margin-bottom:14px">選一次外出拍攝來檢討：這次哪些做得好、哪些問題反覆出現、下次再來同一個地方該怎麼拍。</p>
    <div class="outings">${events.map((e) => html`<div class="outing" key=${e.id} onClick=${() => go('mentor', 'outing-' + e.id)}>
      ${e.cover_id ? html`<img loading="lazy" src=${thumb(e.cover_id)} alt="" />` : html`<div class="noimg"></div>`}
      <div class="stack gap-s grow">
        <b class="ellipsis">${e.title}</b>
        <span class="small muted">${fmtDay(e.start_ts)}　${n0(e.count)} 張</span>
        ${e.review
          ? html`<span class="small">${e.review.headline || '已有統計，還沒有講評'}</span>`
          : html`<span class="small faint">還沒檢討</span>`}
      </div></div>`)}</div>
  </div>`;
}

function Tiles({ s }) {
  const m = s.metrics;
  const tile = (label, value, sub) => html`<div class="tile"><span class="l">${label}</span><b class="v num">${value}</b>${sub && html`<span class="s">${sub}</span>`}</div>`;
  const of = (k) => (m[k].value == null ? null : tile(m[k].label, show(m[k].value, m[k].unit), `依據 ${m[k].n} 張`));
  return html`<div class="tiles">
    ${tile('照片', n0(s.count), s.span ? (s.span[0] === s.span[1] ? s.span[0] : `${s.span[0]} 到 ${s.span[1]}`) : '')}
    ${tile('值得留下', `${Math.round((s.keepers / Math.max(s.count, 1)) * 100)}%`, `${n0(s.keepers)} 張`)}
    ${of('blur_rate')}${of('golden_share')}${of('midday_share')}${of('tilt_rate')}${of('slow_shutter_rate')}
  </div>`;
}

function Review({ scope, id = null, name, count, event }) {
  const { status, jobTick, running, startJob, go } = useApp();
  const [r, setR] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const [view, setView] = useState(null);
  const cloud = status ? status.cloud : {};
  const local = status ? status.local : {};
  const q = `scope=${scope}${id != null ? `&id=${id}` : ''}`;
  useEffect(() => { api('/mentor/review?' + q).then(setR).catch(fail); }, [q, jobTick]);
  if (!r) return null;
  const title = name || (event ? event.title : '這次拍攝');
  const run = (mode) => startJob('/mentor/review', { scope, id, mode });
  const ask = async () => { try { setConfirm(await api('/mentor/outgoing?' + q)); } catch (e) { fail(e); } };
  const open = (ids, pid) => setView({ ids, index: Math.max(ids.indexOf(pid), 0) });
  const planHere = async () => {
    try {
      const p = await api('/mentor/places?q=' + encodeURIComponent(event.place || ''));
      prefill = p.past[0] || null;
    } catch { prefill = null; }
    go('mentor', 'plan');
  };
  const s = r.stats;
  const rv = r.review;
  const who = r.provider === 'local' ? '地端模型' : cloud.label || '外部模型';
  const buttons = html`<div class="row wrap">
    <button class="btn ${rv ? '' : 'primary'}" disabled=${running || !cloud.has_key} onClick=${ask}>
      <${Icon} name="cloud" size=${14} /> ${rv ? '請外部模型重新講評' : `請 ${cloud.label || '外部模型'} 講評`}</button>
    <button class="btn" disabled=${running || !local.ok} title=${local.ok ? '' : local.error} onClick=${() => run('local')}>
      <${Icon} name="chip" size=${14} /> 用地端模型講評</button>
    <button class="btn ghost" disabled=${running} onClick=${() => run('stats')}>${s ? '只更新統計' : '只看統計，不用模型'}</button>
  </div>`;

  return html`<div class="stack" style="gap:18px">
    ${scope === 'event' && html`<div class="row">
      <button class="btn ghost sm" onClick=${() => go('mentor', 'outings')}><${Icon} name="left" size=${13} /> 所有拍攝</button>
      <h2 class="grow">${title}</h2>
      ${event && event.place && html`<button class="btn sm" onClick=${planHere}>規劃下次來這裡</button>`}
    </div>`}

    ${!s && html`<div class="card stack">
      <h2>${scope === 'library' ? `看看這 ${n0(count)} 張照片透露了什麼習慣` : '檢討這次拍攝'}</h2>
      <p class="muted">程式會先量測每張照片的設定、清晰度、水平和光線時段，統計出反覆出現的問題；這一步不需要模型。
        接著把分數最高和最有代表性的 12 張拼成一張縮圖總表，連同統計數字交給模型，請它像老師一樣講評並出一兩個練習。</p>
      <p class="small muted">外部模型只會看到那一張縮圖總表（縮小且不含拍攝資訊），送出前會先讓你看是哪幾張。${scope === 'library' && count > 1500 ? '第一次統計要量測每張照片的水平，照片多的話需要幾分鐘。' : ''}</p>
      ${buttons}
      ${!cloud.has_key && html`<p class="small faint">還沒有設定外部模型，可以先用地端模型講評，或只看統計。</p>`}
    </div>`}

    ${s && rv && html`<div class="card stack review">
      <div class="headline">${rv.headline}</div>
      <span class="small faint">${who} 的講評　${when(r.created_at)}　看了下面 ${r.photo_ids.length} 張縮圖和統計數字。講評裡說的「照片 3」就是編號 3 的這張。</span>
      <div class="numbered">${r.photo_ids.map((pid, i) => html`<div title="看這張的診斷" onClick=${() => open(r.photo_ids, pid)}>
        <img loading="lazy" src=${thumb(pid)} alt="" /><b>${i + 1}</b></div>`)}</div>
      ${rv.strengths.length > 0 && html`<div><h3>做得好的</h3>
        ${rv.strengths.map((x) => html`<p><b>${x.title}</b>　<span class="muted">${x.evidence}</span></p>`)}</div>`}
      ${rv.problems.length > 0 && html`<div><h3>最該改的</h3>
        <ol class="problems">${rv.problems.map((x) => html`<li>
          <b>${x.title}</b>
          <p class="muted">${x.evidence}</p>
          <p><span class="chip local">下次</span>　${x.fix}</p>
          ${x.photos.length > 0 && html`<div class="minis">${x.photos.map((pid) => html`<img loading="lazy" src=${thumb(pid)} alt="" title="看這張的診斷" onClick=${() => open(x.photos, pid)} />`)}</div>`}
        </li>`)}</ol></div>`}
      ${rv.next_time.length > 0 && html`<div><h3>${scope === 'event' ? '下次再來這裡' : '接下來一個月'}</h3>
        <ul class="plain">${rv.next_time.map((x) => html`<li>${x}</li>`)}</ul></div>`}
      ${rv.photo_notes.length > 0 && html`<div><h3>幾張值得講的照片</h3>
        <div class="notes">${rv.photo_notes.map((x) => html`<div class="note-photo" onClick=${() => open(rv.photo_notes.map((y) => y.photo_id), x.photo_id)}>
          <img loading="lazy" src=${thumb(x.photo_id)} alt="" /><span class="small">${x.note}</span></div>`)}</div></div>`}
      ${rv.exercises.length > 0 && html`<div><h3>練習</h3>
        ${rv.exercises.map((x) => html`<p><b>${x.title}</b>　${x.how}</p>`)}
        <a class="small" onClick=${() => go('mentor', 'practice')}>已加到「練習」，之後拍的照片會自動檢查有沒有進步</a></div>`}
      ${buttons}
    </div>`}

    ${s && !rv && html`<div class="card stack"><p class="muted">下面是程式量測的結果，還沒有請模型講評。</p>${buttons}</div>`}

    ${s && html`
      <${Tiles} s=${s} />

      ${s.insights.length > 0 && html`<div class="card"><h2>從你自己的照片看出來的事</h2>
        <ul class="plain">${s.insights.map((x) => html`<li>${x}</li>`)}</ul></div>`}

      ${(s.problems.length > 0 || s.good.length > 0) && html`<div class="card"><h2>反覆出現的狀況</h2>
        <p class="small muted" style="margin-bottom:10px">由規則從拍攝設定、清晰度、水平和太陽位置判斷，沒有用到模型。點縮圖看那一張的診斷和建議設定。</p>
        <div class="issues">${[...s.problems, ...s.good].map((f) => html`
          <div class="issue ${f.level}">
            <div class="row"><b class="grow">${f.title}</b><span class="small muted num">${f.n} 張・${Math.round(f.share * 100)}%</span></div>
            <div class="mbar"><i style=${`width:${Math.max(f.share * 100, 1)}%`}></i></div>
            <div class="minis">${f.examples.slice(0, 6).map((pid) => html`<img loading="lazy" src=${thumb(pid)} alt="" onClick=${() => open(f.examples, pid)} />`)}</div>
          </div>`)}</div></div>`}

      ${s.pairs.length > 0 && html`<div class="card"><h2>你自己拍的對照</h2>
        <p class="small muted" style="margin-bottom:10px">同一個場景你拍了不只一張。把留下的那張和失敗的那張放在一起，差別最清楚。</p>
        <div class="pairs">${s.pairs.map((p) => html`<${Pair} key=${p.worse} pair=${p} onOpen=${(pid) => open([p.better, p.worse], pid)} />`)}</div></div>`}

      <div class="dists">
        <div class="card"><h2>什麼時候拍</h2><${Hours} hours=${s.hours} />
          <div style="margin-top:14px"><${Meter} rows=${LIGHT.map(([k, l]) => ({ name: l, n: s.light[k], share: s.light[k] / Math.max(s.count - s.light.unknown, 1) })).filter((x) => x.n)} /></div>
          ${s.light.unknown > 0 && html`<p class="small faint" style="margin-top:6px">${s.light.unknown} 張沒有位置或時區資料，無法判斷光線時段。</p>`}</div>
        <div class="card"><h2>焦段（等效 35mm）</h2><${Meter} rows=${s.focal} /></div>
        <div class="card"><h2>光圈</h2><${Meter} rows=${s.aperture} />
          <h2 style="margin-top:16px">ISO</h2><${Meter} rows=${s.iso} /></div>
        <div class="card"><h2>曝光模式</h2><${Meter} rows=${s.program} />
          <h2 style="margin-top:16px">器材</h2><${Meter} rows=${s.cameras} /></div>
        <div class="card"><h2>題材</h2><${Meter} rows=${s.categories} /></div>
        ${s.techniques.length > 0 && html`<div class="card"><h2>構圖手法</h2><${Meter} rows=${s.techniques} />
          <p class="small faint" style="margin-top:6px">依地端模型對 ${s.annotated} 張照片的標註。</p></div>`}
      </div>`}

    ${confirm && html`<${Dialog} wide title=${`送出 ${confirm.photo_ids.length} 張照片的縮圖總表給 ${confirm.provider}`} sub=${`模型：${confirm.model || '（尚未選擇）'}`}
      onClose=${() => setConfirm(null)}
      footer=${html`<span class="small muted grow">預估約 ${n0(confirm.est_tokens)} tokens${confirm.via === 'cli' ? '，透過命令列工具送出，計入訂閱用量' : ''}</span>
        <button class="btn" onClick=${() => setConfirm(null)}>取消</button>
        <button class="btn primary" onClick=${() => { setConfirm(null); run('cloud'); }}><${Icon} name="cloud" size=${14} /> 送出</button>`}>
      <p class="muted">${confirm.name}共 ${n0(confirm.count)} 張，其中下面這 ${confirm.photo_ids.length} 張會拼成一張編號的縮圖總表送出（每格 ${confirm.image_px}px，不含拍攝時間、相機與 GPS），
        連同程式量測的統計數字和每張的拍攝設定。前半是分數最高的，後半是各種問題的代表。原始檔不會上傳。</p>
      <div class="thumbs">${confirm.photo_ids.map((pid) => html`<img loading="lazy" src=${thumb(pid)} alt="" />`)}</div>
    <//>`}
    ${view && html`<${PhotoViewer} ids=${view.ids} index=${view.index} startMode="mentor" onClose=${() => setView(null)} />`}
  </div>`;
}

// -------------------------------------------------------------------- plans

function Plans({ o, id, reload }) {
  const { go, status } = useApp();
  const local = status ? status.local : {};
  const [f, setF] = useState({ place: prefill, day: '', genre: 'landscape', subject: '', kit: '' });
  const [q, setQ] = useState(prefill ? prefill.label : '');
  const [found, setFound] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { prefill = null; }, []);
  useEffect(() => {
    const t = setTimeout(() => api('/mentor/places?q=' + encodeURIComponent(q)).then(setFound).catch(() => {}), 220);
    return () => clearTimeout(t);
  }, [q]);
  if (id) return html`<${Plan} id=${id} onBack=${() => go('mentor', 'plan')} onDeleted=${() => { reload(); go('mentor', 'plan'); }} />`;
  const pick = (p) => { setF({ ...f, place: p }); setQ(p.label); };
  const create = async () => {
    const kit = o.gear[+f.kit] || {};
    setBusy(true);
    try {
      const plan = await api('/mentor/plan', { body: { lat: f.place.lat, lon: f.place.lon, place: f.place.label, day: f.day, genre: f.genre,
        subject: f.subject, camera: f.kit === '' ? '' : kit.camera, lens: f.kit === '' ? '' : kit.lens || '', use_model: local.ok } });
      reload(); go('mentor', 'plan-' + plan.id);
    } catch (e) { fail(e); }
    setBusy(false);
  };
  const options = found ? [...found.past.map((p) => ({ ...p, past: true })), ...found.cities] : [];
  return html`<div class="split-plan">
    <div class="card stack">
      <h2>規劃一次拍攝</h2>
      <p class="small muted">光線時刻表和太陽方位是用地點和日期在這台電腦上算出來的，不需要連網。起始設定會依你選的鏡頭調整，提醒則來自你過去拍這類題材時常出現的問題。</p>
      <div class="field"><span>地點（輸入城市名稱、「緯度, 經度」，或選一個去過的地方）</span>
        <input type="text" value=${q} placeholder="例如：京都、Reykjavik、25.03, 121.56"
          onInput=${(e) => { setQ(e.target.value); setF({ ...f, place: null }); }} /></div>
      ${!f.place && options.length > 0 && html`<div class="row wrap gap-s">${options.map((p) => html`
        <span class="chip pick" onClick=${() => pick(p)}>${p.past ? '去過　' : ''}${p.label}${p.past ? `（${p.photos} 張）` : ''}</span>`)}</div>`}
      ${f.place && html`<p class="small muted"><${Icon} name="check" size=${12} /> ${f.place.label}　<span class="faint num">${f.place.lat}, ${f.place.lon}</span></p>`}
      <div class="row wrap top gap-l">
        <div class="field" style="width:170px"><span>日期</span><input type="date" value=${f.day} onInput=${(e) => setF({ ...f, day: e.target.value })} /></div>
        <div class="field" style="width:170px"><span>題材</span><select value=${f.genre} onChange=${(e) => setF({ ...f, genre: e.target.value })}>
          ${Object.entries(o.genres).map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select></div>
        <div class="field grow" style="min-width:220px"><span>器材</span><select value=${f.kit} onChange=${(e) => setF({ ...f, kit: e.target.value })}>
          <option value="">不指定</option>
          ${o.gear.map((g, i) => html`<option value=${i}>${g.camera}${g.lens ? `　${g.lens}` : ''}（${g.photos} 張）</option>`)}</select></div>
      </div>
      <div class="field"><span>想拍什麼（選填，會交給地端模型列拍攝清單）</span>
        <input type="text" value=${f.subject} maxlength="300" placeholder="例如：楓葉和河邊的橋，希望畫面安靜" onInput=${(e) => setF({ ...f, subject: e.target.value })} /></div>
      <div class="row"><button class="btn primary" disabled=${busy || !f.place || !f.day} onClick=${create}>
        ${busy ? html`<span class="spin"></span>` : ''} 產生計畫</button>
        <span class="small muted">${local.ok ? '拍攝清單由地端模型撰寫，不花 token。' : '地端模型沒有連上，拍攝清單會用內建的通用版本。'}</span></div>
    </div>
    <div class="stack">
      <h3>已經做好的計畫</h3>
      ${o.plans.length === 0 && html`<p class="small faint">還沒有。</p>`}
      ${o.plans.map((p) => html`<div class="card planrow" key=${p.id} onClick=${() => go('mentor', 'plan-' + p.id)}>
        <b>${p.place}</b><span class="small muted">${p.day}　${p.genre_zh}${p.subject ? `　${p.subject}` : ''}</span></div>`)}
    </div>
  </div>`;
}

function Plan({ id, onBack, onDeleted }) {
  const [p, setP] = useState(null);
  const [sheet, setSheet] = useState(false);
  const [view, setView] = useState(null);
  const [pickFolder, folderDlg] = usePickFolder();
  useEffect(() => { api('/mentor/plan/' + id).then(setP).catch(fail); }, [id]);
  if (!p) return null;
  const save = async () => {
    const dest = await pickFolder('小抄要存在哪裡？', '存在這裡');
    if (!dest) return;
    try { const r = await api(`/mentor/plan/${id}/export`, { body: { dest } }); toast(`小抄已存成 ${r.path}，可以用 AirDrop 傳到手機或直接列印。`); } catch (e) { fail(e); }
  };
  const remove = async () => {
    if (!window.confirm('刪除這份計畫？')) return;
    try { await api('/mentor/plan/' + id, { method: 'DELETE' }); onDeleted(); } catch (e) { fail(e); }
  };
  return html`<div class="stack" style="gap:16px">
    <div class="row wrap">
      <button class="btn ghost sm" onClick=${onBack}><${Icon} name="left" size=${13} /> 所有計畫</button>
      <div class="grow"><h2>${p.place}</h2><span class="small muted">${p.day}${p.subject ? `　${p.subject}` : ''}${p.camera ? `　${p.camera}${p.lens ? ` ${p.lens}` : ''}` : ''}</span></div>
      <button class="btn" onClick=${() => setSheet(true)}>預覽小抄</button>
      <button class="btn primary" onClick=${save}><${Icon} name="out" size=${14} /> 存成小抄…</button>
      <button class="btn sm danger" onClick=${remove}><${Icon} name="trash" size=${13} /></button>
    </div>

    <div class="card stack">
      <h2>光線時刻表</h2>
      ${p.timetable.length > 0
        ? html`<p class="small muted">日出 ${p.sunrise}　日落 ${p.sunset}　正午太陽高度 ${Math.round(p.noon_elev)}°${p.tz_exact ? '' : '　時區是依經度估計的，可能差一小時'}</p>
          <${DayTrack} windows=${p.timetable} />
          <div class="windows">${p.timetable.map((w) => html`<div class="win ${w.best ? 'best' : ''}">
            <b class="num">${w.start}–${w.end}</b>
            <div><b>${w.label}</b>${w.best ? html`　<span class="chip cloud">最適合這次的題材</span>` : ''}<p class="small muted">${w.note}</p></div></div>`)}</div>`
        : html`<p class="muted">這一天在這個緯度沒有一般的日出日落，無法列出時刻表。</p>`}
      ${p.directions.length > 0 && html`<ul class="plain small">${p.directions.map((d) => html`<li>${d}</li>`)}</ul>`}
    </div>

    <div class="split-plan">
      <div class="card stack"><h2>起始設定</h2>
        <p class="small muted">到現場先這樣設，再看結果調整。${p.phone ? '' : '光圈依你選的鏡頭在相片庫裡出現過的最大值。'}</p>
        ${p.settings.map((s) => html`<div class="setcard"><b>${s.when}</b>
          ${(s.aperture || s.shutter || s.iso) && html`<div class="dials">
            ${s.aperture && html`<div><span>光圈</span><b>${s.aperture}</b></div>`}
            ${s.shutter && html`<div><span>快門</span><b>${s.shutter}</b></div>`}
            ${s.iso && html`<div><span>ISO</span><b>${s.iso}</b></div>`}</div>`}
          ${s.mode && html`<span class="small muted">${s.mode}</span>`}<p class="small">${s.note}</p></div>`)}</div>
      <div class="card stack"><h2>拍攝清單</h2>
        ${p.shots.concept && html`<p><b>這次的想法</b>　${p.shots.concept}</p>`}
        ${p.shots.shots.map((s, i) => html`<div class="shotrow"><span class="ix">${i + 1}</span>
          <div><b>${s.title}</b>${s.when ? html`　<span class="small muted">${s.when}</span>` : ''}<p class="small muted">${s.how}</p></div></div>`)}
        ${p.shots.avoid.length > 0 && html`<p class="small muted">容易失誤的地方：${p.shots.avoid.join('；')}</p>`}
        <span class="small faint">${p.shots.source === 'builtin' ? `內建的通用清單${p.shots.error ? `（${p.shots.error}）` : ''}` : `由地端模型 ${p.shots.source} 撰寫，沒有去過現場，請當成提示而不是指南`}</span>
      </div>
    </div>

    ${(p.reminders.length > 0 || p.exercises.length > 0) && html`<div class="card"><h2>給你自己的提醒</h2>
      <ul class="plain">${p.reminders.map((r) => html`<li>${r}</li>`)}
        ${p.exercises.map((e) => html`<li><span class="chip local">練習</span>　<b>${e.title}</b>　${e.how}</li>`)}</ul></div>`}

    ${p.history.map((h) => html`<div class="card stack"><h2>上次來這裡　${h.day}</h2>
      <p>${h.note}</p>${h.headline && html`<p class="small muted">當時的講評：${h.headline}</p>`}
      ${h.best.length > 0 && html`<div><span class="small muted">那次拍得最好的幾張</span>
        <div class="minis big">${h.best.map((pid) => html`<img loading="lazy" src=${thumb(pid)} alt="" onClick=${() => setView({ ids: h.best, index: h.best.indexOf(pid) })} />`)}</div></div>`}</div>`)}

    ${sheet && html`<${Dialog} wide title="出門小抄" sub="一個不需要連網的檔案，排成手機螢幕的寬度，也可以列印。" onClose=${() => setSheet(false)}
      footer=${html`<button class="btn" onClick=${() => setSheet(false)}>關閉</button>
        <button class="btn primary" onClick=${() => { setSheet(false); save(); }}><${Icon} name="out" size=${14} /> 存成小抄…</button>`}>
      <iframe class="sheet" title="出門小抄" src=${`/api/mentor/plan/${id}/sheet`}></iframe>
    <//>`}
    ${view && html`<${PhotoViewer} ids=${view.ids} index=${view.index} startMode="mentor" onClose=${() => setView(null)} />`}
    ${folderDlg}
  </div>`;
}

// ----------------------------------------------------------------- practice

function Practice({ list, setList }) {
  const set = (id, status) => api('/mentor/exercises/' + id, { method: 'PATCH', body: { status } }).then(setList).catch(fail);
  if (!list.length) return html`<div class="empty"><p>還沒有練習。到「拍攝習慣」或「拍攝檢討」請模型講評，它會針對你的狀況出一兩個練習。</p></div>`;
  const open = list.filter((e) => e.status === 'open');
  const closed = list.filter((e) => e.status !== 'open');
  const row = (e) => {
    const [label, tone] = STATE[e.state] || ['', ''];
    const measured = e.label && e.status === 'open';
    return html`<div class="card exercise ${e.status}" key=${e.id}>
      <div class="row top">
        <div class="grow stack gap-s">
          <b class="ttl">${e.title}</b>
          ${e.how && html`<p>${e.how}</p>`}
          ${e.why && html`<p class="small muted">${e.why}</p>`}
        </div>
        ${e.status === 'open'
          ? html`<button class="btn sm" onClick=${() => set(e.id, 'done')}><${Icon} name="check" size=${13} /> 完成</button>
              <button class="btn ghost sm" onClick=${() => set(e.id, 'dropped')}>放棄</button>`
          : html`<span class="chip">${e.status === 'done' ? '已完成' : '已放棄'}</span>
              <button class="btn ghost sm" onClick=${() => set(e.id, 'open')}>重新開始</button>`}
      </div>
      ${measured && html`<div class="progress">
        <div><span class="l">${e.label}</span>
          <div class="ba"><span><span class="l">開始時</span><b class="num">${show(e.baseline, e.unit)}</b></span>
            <${Icon} name="right" size=${16} />
            <span><span class="l">之後拍的 ${e.n} 張</span><b class="num">${e.n ? show(e.current, e.unit) : '—'}</b></span></div></div>
        <div class="stack gap-s" style="align-items:flex-end">
          <span class="chip ${tone === 'good' ? 'local' : tone === 'bad' ? 'bad' : ''}">${e.state === 'better' ? '✓ ' : e.state === 'worse' ? '! ' : ''}${label}</span>
          <span class="small faint">${e.state === 'waiting' ? `至少要 5 張新照片才比較（目前 ${e.n} 張）` : e.better === 'lower' ? '越低越好' : '越高越好'}</span></div>
      </div>`}
      ${e.metric === 'manual' && e.status === 'open' && html`<p class="small faint">這個練習沒有對應的量測，做到了就自己按「完成」。</p>`}
    </div>`;
  };
  return html`<div class="stack">
    <p class="muted">每個練習都對應一個程式量得到的數字。之後拍的照片匯入並分析後，這裡會拿新照片和開始時的數字比較。</p>
    ${open.map(row)}
    ${closed.length > 0 && html`<h3 style="margin-top:10px">結束的練習</h3>${closed.map(row)}`}
  </div>`;
}
