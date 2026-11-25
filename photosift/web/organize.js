import { html, useState, useEffect, api, fail, useApp, Icon, n0, fmtDay, FLAW, thumb } from './ui.js';
import { PhotoViewer } from './library.js';

const TABS = [['events', '事件'], ['themes', '主題'], ['groups', '連拍與重複'], ['flaws', '技術瑕疵']];

export function Organize({ tab }) {
  const { go, status } = useApp();
  return html`<div class="page">
    <div class="page-head"><h1>整理</h1>
      <span class="sub">這裡的分類都是虛擬相簿，只存在 PhotoSift 的資料庫裡，原始檔的位置不會改變。</span></div>
    <div class="tabs">${TABS.map(([k, l]) => html`<button class="tab ${tab === k ? 'on' : ''}" onClick=${() => go('organize', k)}>${l}</button>`)}</div>
    ${status && !status.analysed
      ? html`<div class="empty"><p>分析照片之後，這裡會出現自動整理的結果。</p></div>`
      : html`${tab === 'events' && html`<${Events} />`}${tab === 'themes' && html`<${Themes} />`}
          ${tab === 'groups' && html`<${Groups} />`}${tab === 'flaws' && html`<${Flaws} />`}`}
  </div>`;
}

function Events() {
  const { openLibrary, jobTick } = useApp();
  const [list, setList] = useState(null);
  const [editing, setEditing] = useState(null);
  const load = () => api('/events').then(setList).catch(fail);
  useEffect(() => { load(); }, [jobTick]);
  if (!list) return null;
  const save = async (e, title) => {
    setEditing(null);
    if (!title.trim() || title === e.title) return;
    try { await api('/events/' + e.id, { method: 'PATCH', body: { title } }); load(); } catch (err) { fail(err); }
  };
  return html`<div class="albums">${list.map((e) => html`<div class="album" key=${e.id}>
    <div class="cover" onClick=${() => openLibrary({ event: e.id, sort: 'time' })}>${e.cover_id && html`<img loading="lazy" src=${thumb(e.cover_id)} alt="" />`}</div>
    <div class="meta">
      <div class="title">
        ${editing === e.id
          ? html`<input type="text" autofocus value=${e.title} onBlur=${(ev) => save(e, ev.target.value)}
              onKeyDown=${(ev) => { if (ev.key === 'Enter') ev.target.blur(); if (ev.key === 'Escape') setEditing(null); }} />`
          : html`<span class="grow ellipsis" title=${e.title} onClick=${() => openLibrary({ event: e.id, sort: 'time' })}>${e.title}</span>
              <button class="btn ghost sm" title="重新命名" onClick=${() => setEditing(e.id)}><${Icon} name="edit" size=${13} /></button>`}
      </div>
      <div class="small muted">${n0(e.count)} 張${e.start_ts && fmtDay(e.start_ts) !== fmtDay(e.end_ts) ? `　${fmtDay(e.start_ts)}～${fmtDay(e.end_ts).slice(5)}` : ''}
        ${e.user_title === 2 ? html`　<span class="chip local" title="這個名稱是地端模型依照片內容取的">地端模型命名</span>` : ''}</div>
    </div>
  </div>`)}</div>`;
}

function Themes() {
  const { openLibrary, jobTick } = useApp();
  const [list, setList] = useState(null);
  useEffect(() => { api('/categories').then(setList).catch(fail); }, [jobTick]);
  if (!list) return null;
  return html`<div class="albums">${list.map((c) => html`<div class="album" key=${c.key} onClick=${() => openLibrary({ category: c.key, sort: 'score' })}>
    <div class="cover">${c.cover && html`<img loading="lazy" src=${thumb(c.cover)} alt="" />`}</div>
    <div class="meta"><div class="title">${c.label}</div><div class="small muted">${n0(c.count)} 張</div></div>
  </div>`)}</div>`;
}

function Groups() {
  const { jobTick } = useApp();
  const [data, setData] = useState(null);
  const [view, setView] = useState(null);
  const load = () => api('/groups?limit=60').then(setData).catch(fail);
  useEffect(() => { load(); }, [jobTick]);
  if (!data) return null;
  const setBest = async (gid, pid) => {
    try { await api(`/groups/${gid}/best/${pid}`, { method: 'POST' }); load(); } catch (e) { fail(e); }
  };
  if (!data.groups.length) return html`<div class="empty"><p>沒有找到連拍或重複的照片。</p></div>`;
  return html`<div>
    <p class="note local" style="margin-bottom:14px">幾秒內連續拍下、畫面相近的照片會歸成一組，另外也會找出不同資料夾裡的重複檔。每組只有「保留」的那張會進入後續的標註與比賽選片；其他張仍在圖庫裡，沒有被刪除。點另一張的「改用這張」可以換掉。</p>
    <p class="small muted" style="margin-bottom:10px">共 ${n0(data.total)} 組${data.total > data.groups.length ? `，顯示最近的 ${data.groups.length} 組` : ''}</p>
    ${data.groups.map((g) => html`<div class="group" key=${g.id}>
      <div class="small muted">${g.photos.length} 張　${(g.photos[0].taken_at || '').replace('T', ' ').slice(0, 16)}</div>
      <div class="members">${g.photos.map((p) => html`<div class="member ${p.is_best ? 'best' : ''}">
        <img loading="lazy" src=${thumb(p.id)} alt="" onClick=${() => setView({ ids: g.photos.map((x) => x.id), index: g.photos.indexOf(p) })} />
        <div class="foot">
          ${p.is_best ? html`<span class="chip local">保留</span>` : html`<button class="btn sm" onClick=${() => setBest(g.id, p.id)}>改用這張</button>`}
          <span class="grow"></span>
          <span title="清晰度">清晰 <b class="num">${Math.round(p.sharp ?? 0)}</b></span>
          ${(p.flags || []).map((f) => html`<span class="chip bad">${FLAW[f]}</span>`)}
        </div></div>`)}</div>
    </div>`)}
    ${view && html`<${PhotoViewer} ids=${view.ids} index=${view.index} onClose=${() => { setView(null); load(); }} />`}
  </div>`;
}

function Flaws() {
  const { openLibrary, jobTick } = useApp();
  const [f, setF] = useState(null);
  useEffect(() => { api('/facets').then((d) => setF(d.flaws)).catch(fail); }, [jobTick]);
  if (!f) return null;
  const desc = { blurry: '整張都不清楚，可能是手震或失焦。', overexposed: '大面積過亮，細節已經消失。', underexposed: '整張太暗，而且沒有亮部。', low_contrast: '灰濛濛的，明暗差距很小。' };
  return html`<div class="stack">
    <p class="note local">這些是從畫面直接量出來的，不需要任何模型。剪影、長曝光這類刻意的拍法有時也會被標記，所以這裡只是標記，不會自動淘汰。你可以點進去逐張確認，按 <span class="kbd">X</span> 淘汰。</p>
    <div class="albums">${Object.entries(FLAW).map(([k, label]) => html`<div class="card" style="cursor:pointer" onClick=${() => openLibrary({ quality: k, sort: 'tech' })}>
      <div class="row"><h2 class="grow">${label}</h2><b class="num" style="font-size:20px">${n0(f[k])}</b></div>
      <p class="small muted">${desc[k]}</p></div>`)}</div>
  </div>`;
}
