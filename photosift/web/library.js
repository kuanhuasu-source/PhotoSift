import { html, useState, useEffect, useRef, useCallback, api, fail, toast, useApp, Icon, Dialog, Bars, usePickFolder,
  n0, fmtDate, FLAW, CATS, VLM_KEYS, thumb, qs } from './ui.js';
import { Editor, useRendered } from './editor.js';
import { MentorPanel } from './mentorpanel.js';

const SORTS = [['time_desc', '拍攝時間（新到舊）'], ['time', '拍攝時間（舊到新）'], ['score', '分數（地端模型評過的在前）'], ['aesthetic', '美感初評'],
  ['tech', '技術品質'], ['rating', '我的星等'], ['taste', '像我會選的']];

export function Tile({ p, onClick, showScore = true }) {
  const ar = p.width && p.height ? Math.max(0.5, Math.min(2.4, p.width / p.height)) : 1.5;
  const score = p.vlm_score != null ? p.vlm_score : p.prefilter;
  const cls = [p.flag === 'reject' ? 'rejected' : '', !p.is_best && p.flag !== 'pick' ? 'dim' : ''].join(' ');
  return html`<div class="tile ${cls}" style=${`--ar:${ar}`} onClick=${onClick} title=${p.filename}>
    <img loading="lazy" src=${thumb(p.id)} alt="" />
    <div class="ov t">
      ${p.flag === 'pick' && html`<span class="badge pick">挑選</span>`}
      ${p.flag === 'reject' && html`<span class="badge rej">淘汰</span>`}
      ${p.rating > 0 && html`<span class="badge" style="color:var(--accent)">${'★'.repeat(p.rating)}</span>`}
      ${p.edited ? html`<span class="badge" title="這張照片有修圖">已修</span>` : ''}
      ${(p.flags || []).map((f) => html`<span class="badge flaw">${FLAW[f] || f}</span>`)}
      ${p.error && html`<span class="badge flaw">無法讀取</span>`}
      <span class="grow"></span>
      ${p.group_size > 1 && html`<span class="badge" title="這組連拍或重複的張數">${p.is_best ? '最佳 ' : ''}×${p.group_size}</span>`}
    </div>
    <div class="ov b">
      <span class="cap grow">${p.caption || ''}</span>
      ${showScore && score != null && html`<span class="badge score" title=${p.vlm_score != null ? '地端模型評分' : '初步分數（美感初評與技術品質）'}>${Math.round(score)}</span>`}
    </div>
  </div>`;
}

export function Library({ filters, setFilters }) {
  const { status, startJob, jobTick, running, go } = useApp();
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [facets, setFacets] = useState(null);
  const [open, setOpen] = useState(null);
  const [query, setQuery] = useState(filters.q || '');
  const [exporting, setExporting] = useState(false);
  const [pickFolder, folderDlg] = usePickFolder();
  const sentinel = useRef(null);
  const state = useRef({ offset: 0, done: false, key: '' });

  const f = { sort: 'time_desc', best: false, hide_rejects: false, ...filters };
  const key = JSON.stringify(f) + jobTick;

  const load = useCallback(async (reset) => {
    const st = state.current;
    if (reset) { st.offset = 0; st.done = false; st.key = key; st.loading = false; setTotal(null); }
    if (st.done || st.loading) return;
    st.loading = true; setLoading(true);
    const mine = st.key;
    try {
      const d = await api('/photos?' + qs({ ...f, offset: st.offset, limit: 120 }));
      if (mine !== state.current.key) return; // filters changed while this page was loading
      st.offset += d.items.length;
      st.done = d.items.length < 120;
      setTotal(d.total);
      setItems((cur) => (reset ? d.items : [...cur, ...d.items]));
    } catch (e) { fail(e); st.done = true; } finally { st.loading = false; setLoading(false); }
  }, [key]);

  useEffect(() => { load(true); }, [key]);
  useEffect(() => { api('/facets').then(setFacets).catch(() => {}); }, [jobTick]);
  useEffect(() => {
    if (!sentinel.current) return;
    const io = new IntersectionObserver((es) => es[0].isIntersecting && load(false), { rootMargin: '600px' });
    io.observe(sentinel.current);
    return () => io.disconnect();
  }, [load]);

  const set = (patch) => setFilters({ ...filters, ...patch });
  const patchItem = (id, patch) => setItems((cur) => cur.map((x) => (x.id === id ? { ...x, ...patch } : x)));

  const exportPicks = async () => {
    const dest = await pickFolder('匯出挑選的照片到…', '匯出到這裡');
    if (!dest) return;
    setExporting(true);
    try {
      const d = await api('/photos?' + qs({ ...f, flag: 'pick', limit: 500, offset: 0 }));
      if (!d.items.length) { toast('目前的篩選範圍內沒有標記為「挑選」的照片。', 'err'); return; }
      const r = await api('/export', { body: { dest, photo_ids: d.items.map((x) => x.id) } });
      toast(`已複製 ${r.copied} 張到 ${r.dest}（原始檔沒有變動）`);
    } catch (e) { fail(e); } finally { setExporting(false); }
  };

  if (status && !status.photos && !running) {
    return html`<div class="page"><div class="empty">
      <h2>${status.folders ? '還沒有分析過的照片' : '先加入相片資料夾'}</h2>
      <p>PhotoSift 只會讀取你的照片，分類、評分與挑選都記錄在自己的資料庫裡，不會搬動、修改或刪除任何原始檔。</p>
      ${status.folders
        ? html`<button class="btn primary" onClick=${() => startJob('/analyze')}><${Icon} name="play" size=${14} /> 開始分析</button>`
        : html`<button class="btn primary" onClick=${() => go('settings')}><${Icon} name="folder" size=${14} /> 加入資料夾</button>`}
    </div></div>`;
  }

  const fc = { folders: [], events: [], categories: [], flaws: {}, lights: [], ...(facets || {}) };
  const pending = status ? status.photos - status.analysed - status.unreadable : 0;
  return html`<div class="page">
    <div class="page-head">
      <h1>圖庫</h1>
      <form class="search" onSubmit=${(e) => { e.preventDefault(); set({ q: query.trim() }); }}>
        <${Icon} name="search" />
        <input type="search" placeholder="用文字找照片，例如：夕陽下的海邊、穿紅衣服的人" value=${query}
          onInput=${(e) => { setQuery(e.target.value); if (!e.target.value && f.q) set({ q: '' }); }} />
      </form>
      <span class="grow"></span>
      <button class="btn" disabled=${running} onClick=${() => startJob('/analyze')} title="重新掃描資料夾，只處理新增或變更的照片">
        <${Icon} name="play" size=${14} /> ${pending > 0 ? `分析新照片（${n0(pending)}）` : '重新掃描'}</button>
      <button class="btn local" disabled=${running || !(status && status.local.ok)} onClick=${() => setOpen({ annotate: true })}
        title=${status && !status.local.ok ? status.local.error : '用地端模型產生描述、標籤與評分'}>
        <${Icon} name="chip" size=${14} /> 地端模型標註</button>
    </div>

    <div class="toolbar">
      <select value=${f.sort} onChange=${(e) => set({ sort: e.target.value })} disabled=${!!f.q}
        title=${status && !status.taste_ready ? '「像我會選的」需要你先標記至少 8 張照片（挑選和淘汰各 3 張以上）' : ''}>
        ${SORTS.filter(([v]) => v !== 'taste' || (status && status.taste_ready) || (status && status.picks + status.rejects >= 8)).map(([v, l]) => html`<option value=${v}>${l}</option>`)}</select>
      <select value=${f.event || ''} onChange=${(e) => set({ event: e.target.value })}>
        <option value="">所有事件</option>
        ${fc.events.map((e) => html`<option value=${e.id}>${e.title}（${e.count}）</option>`)}</select>
      <select value=${f.category || ''} onChange=${(e) => set({ category: e.target.value })}>
        <option value="">所有主題</option>
        ${fc.categories.map((c) => html`<option value=${c.key}>${c.label}（${c.count}）</option>`)}</select>
      ${fc.folders.length > 1 && html`<select value=${f.folder || ''} onChange=${(e) => set({ folder: e.target.value })}>
        <option value="">所有資料夾</option>
        ${fc.folders.map((d) => html`<option value=${d.id}>${d.path.split('/').pop()}（${d.photos}）</option>`)}</select>`}
      ${fc.lights.length > 0 && html`<select value=${f.light || ''} onChange=${(e) => set({ light: e.target.value })} title="由拍攝時間和 GPS 算出的太陽位置">
        <option value="">不限光線時段</option>
        ${fc.lights.map((l) => html`<option value=${l.key}>${l.label}（${l.count}）</option>`)}</select>`}
      <select value=${f.quality || ''} onChange=${(e) => set({ quality: e.target.value })}>
        <option value="">不限品質</option><option value="ok">沒有技術瑕疵</option><option value="flawed">有技術瑕疵</option>
        ${Object.entries(FLAW).map(([k, l]) => html`<option value=${k}>${l}（${fc.flaws[k] || 0}）</option>`)}</select>
      <select value=${f.flag || ''} onChange=${(e) => set({ flag: e.target.value })}>
        <option value="">不限標記</option><option value="pick">我挑選的</option><option value="reject">我淘汰的</option><option value="none">未標記</option></select>
      <label class="check"><input type="checkbox" checked=${!!f.best} onChange=${(e) => set({ best: e.target.checked })} />連拍只看最佳</label>
      <label class="check"><input type="checkbox" checked=${!!f.hide_rejects} onChange=${(e) => set({ hide_rejects: e.target.checked })} />隱藏淘汰</label>
      <span class="grow"></span>
      <span class="count num">${total == null ? (f.q ? '搜尋中…' : '') : f.q ? `「${f.q}」找到 ${n0(total)} 張` : `${n0(total)} 張`}</span>
      <button class="btn sm" disabled=${exporting} onClick=${exportPicks} title="把標記為「挑選」的照片複製到另一個資料夾">
        <${Icon} name="out" size=${13} /> 匯出挑選的照片</button>
    </div>

    <div class="grid">
      ${items.map((p, i) => html`<${Tile} key=${p.id} p=${p} onClick=${() => setOpen({ index: i })} />`)}
    </div>
    ${!items.length && !loading && total != null && html`<div class="empty"><p>${f.q ? `找不到符合「${f.q}」的照片。可以換個說法，或描述得更具體一點。` : '沒有符合條件的照片。'}</p>
      <button class="btn" onClick=${() => { setQuery(''); setFilters({}); }}>清除篩選</button></div>`}
    <div ref=${sentinel} style="height:1px"></div>
    ${loading && html`<div class="row muted" style="justify-content:center;padding:20px"><span class="spin"></span>
      ${f.q && !items.length ? '搜尋中…程式啟動後的第一次搜尋要先載入影像模型，會久一點。' : '載入中…'}</div>`}

    ${open && open.index != null && html`<${PhotoViewer} ids=${items.map((x) => x.id)} index=${open.index}
      onClose=${() => setOpen(null)} onPatch=${patchItem} />`}
    ${open && open.annotate && html`<${AnnotateDialog} onClose=${() => setOpen(null)} />`}
    ${folderDlg}
  </div>`;
}

function AnnotateDialog({ onClose }) {
  const { status, startJob } = useApp();
  const todo = (status.photos - status.duplicates) - status.annotated + (status.annotations_outdated || 0);
  const [limit, setLimit] = useState(Math.min(200, Math.max(todo, 1)));
  const [redo, setRedo] = useState(false);
  return html`<${Dialog} title="地端模型標註" sub=${`使用 ${status.local.model}，在這台電腦上執行，不花任何 token。`} onClose=${onClose}
    footer=${html`<button class="btn" onClick=${onClose}>取消</button>
      <button class="btn local" onClick=${() => { startJob('/annotate', { limit: +limit || 0, redo }); onClose(); }}>開始</button>`}>
    <p class="muted">地端模型會為每張照片寫一句描述、幾個標籤，並給構圖、光線、意境等分數。依初步分數由高到低處理，所以先跑一部分就能涵蓋最有潛力的照片；中途可以取消，已完成的會保留。</p>
    ${status.annotations_outdated > 0 && html`<p class="note local">有 ${n0(status.annotations_outdated)} 張是用舊版格式標註的，缺少主體、光線、構圖手法這些新欄位，這次會一併更新。</p>`}
    <div class="field" style="max-width:260px"><span>這次處理幾張（還有 ${n0(Math.max(todo, 0))} 張待處理）</span>
      <input type="number" min="1" value=${limit} onInput=${(e) => setLimit(e.target.value)} /></div>
    <label class="check"><input type="checkbox" checked=${redo} onChange=${(e) => setRedo(e.target.checked)} />已標註過的也重新標註（換了模型時使用）</label>
  <//>`;
}

export function PhotoViewer({ ids, index, onClose, onPatch, startMode = 'info', contestId = null }) {
  const [i, setI] = useState(index);
  const [p, setP] = useState(null);
  const [mode, setMode] = useState(startMode); // info | edit | mentor
  const [recipe, setRecipe] = useState(null);
  const [original, setOriginal] = useState(false); // hold to compare with the untouched photo
  const id = ids[i];
  const rendered = useRendered(id, recipe, mode === 'edit');

  const load = useCallback((pid) => api('/photos/' + pid).then(setP).catch(fail), []);
  useEffect(() => { setP(null); setRecipe(null); load(id); }, [id]);

  const patch = async (body) => {
    try {
      const r = await api('/photos/' + p.id, { method: 'PATCH', body });
      setP({ ...p, ...r });
      onPatch && onPatch(p.id, r);
    } catch (e) { fail(e); }
  };
  const setFlag = (flag) => patch({ flag: p.flag === flag ? '' : flag });
  const setBest = async (pid) => {
    try {
      await api(`/groups/${p.group_id}/best/${pid}`, { method: 'POST' });
      load(p.id);
      p.group.forEach((g) => onPatch && onPatch(g.id, { is_best: g.id === pid ? 1 : 0 }));
    } catch (e) { fail(e); }
  };

  useEffect(() => {
    const k = (e) => {
      const typing = e.target.tagName === 'TEXTAREA' || (e.target.tagName === 'INPUT' && e.target.type !== 'range' && e.target.type !== 'checkbox');
      if (typing) return;
      if (e.key === 'Escape') onClose();
      else if (e.key === ' ' && mode === 'edit') { e.preventDefault(); setOriginal(true); }
      else if (e.target.type === 'range') return; // arrow keys belong to the slider that has focus
      else if (e.key === 'ArrowRight') setI((x) => Math.min(ids.length - 1, x + 1));
      else if (e.key === 'ArrowLeft') setI((x) => Math.max(0, x - 1));
      else if (!p) return;
      else if (e.key === 'e' || e.key === 'E') setMode((m) => (m === 'edit' ? 'info' : 'edit'));
      else if (e.key === 'm' || e.key === 'M') setMode((m) => (m === 'mentor' ? 'info' : 'mentor'));
      else if (e.key === 'p' || e.key === 'P') setFlag('pick');
      else if (e.key === 'x' || e.key === 'X') setFlag('reject');
      else if (/^[0-5]$/.test(e.key)) patch({ rating: +e.key });
    };
    const up = (e) => e.key === ' ' && setOriginal(false);
    window.addEventListener('keydown', k);
    window.addEventListener('keyup', up);
    return () => { window.removeEventListener('keydown', k); window.removeEventListener('keyup', up); };
  }, [p, ids.length, mode]);

  const v = p && p.vlm;
  const exposure = p && [p.focal && `${Math.round(p.focal)}mm`, p.aperture && `f/${p.aperture}`, p.shutter, p.iso && `ISO ${p.iso}`].filter(Boolean).join('　');
  return html`<div class="overlay">
    <div class="viewer">
      <div class="stage" onClick=${(e) => e.target === e.currentTarget && onClose()}>
        <img key=${id} src=${mode === 'edit' && rendered && !original ? rendered : `/api/preview/${id}?px=1600`} alt="" />
        ${mode === 'edit' && html`<button class="hold" onMouseDown=${() => setOriginal(true)} onMouseUp=${() => setOriginal(false)}
          onMouseLeave=${() => setOriginal(false)}>${original ? '原圖' : '按住看原圖'}</button>`}
        ${i > 0 && html`<button class="arrow l" onClick=${() => setI(i - 1)}><${Icon} name="left" size=${22} /></button>`}
        ${i < ids.length - 1 && html`<button class="arrow r" onClick=${() => setI(i + 1)}><${Icon} name="right" size=${22} /></button>`}
      </div>
      <div class="info">
        <div class="row">
          <div class="grow"><b class="ellipsis" style="display:block">${p ? p.filename : '…'}</b>
            <span class="small muted num">${i + 1} / ${ids.length}</span></div>
          <button class="btn ghost sm" onClick=${onClose} title="關閉（Esc）"><${Icon} name="x" /></button>
        </div>
        ${p && html`
        <div class="row wrap gap-s">
          <button class="btn sm ${p.flag === 'pick' ? 'on' : ''}" onClick=${() => setFlag('pick')}><${Icon} name="check" size=${13} /> 挑選 <span class="kbd">P</span></button>
          <button class="btn sm danger ${p.flag === 'reject' ? 'on' : ''}" onClick=${() => setFlag('reject')}><${Icon} name="x" size=${13} /> 淘汰 <span class="kbd">X</span></button>
          <span class="stars">${[1, 2, 3, 4, 5].map((s) => html`<button class=${p.rating >= s ? 'on' : ''} onClick=${() => patch({ rating: p.rating === s ? 0 : s })}>★</button>`)}</span>
        </div>
        <div class="seg">
          <button class=${mode === 'info' ? 'on' : ''} onClick=${() => setMode('info')}>資訊</button>
          <button class=${mode === 'edit' ? 'on' : ''} disabled=${!!p.error} onClick=${() => setMode('edit')}>後製 <span class="kbd">E</span></button>
          <button class=${mode === 'mentor' ? 'on' : ''} disabled=${!!p.error} onClick=${() => setMode('mentor')}>大師 <span class="kbd">M</span></button>
        </div>
        ${mode === 'mentor' && html`<${MentorPanel} id=${p.id} onJump=${(pid) => { const j = ids.indexOf(pid); if (j >= 0) setI(j); }} />`}
        ${mode === 'edit' && html`<${Editor} photo=${p} recipe=${recipe} setRecipe=${setRecipe} contestId=${contestId}
          onSaved=${(pid, edited) => onPatch && onPatch(pid, { edited: edited ? 1 : 0 })} />`}
        ${mode === 'info' && html`

        ${p.caption && html`<div><h3>地端模型的描述</h3><p>${p.caption}</p>
          ${p.tags && html`<div class="row wrap gap-s" style="margin-top:8px">${p.tags.map((t) => html`<span class="chip">${t}</span>`)}</div>`}</div>`}

        <div><h3>分數</h3>
          <${Bars} kind="local" rows=${[['技術品質', Math.round(p.tech ?? 0), 100], ['美感初評', Math.round(p.aesthetic ?? 0), 100],
            ...(p.vlm_score != null ? [['地端模型', Math.round(p.vlm_score), 100]] : []),
            ...(p.taste != null ? [['像我會選的', Math.round(p.taste), 100]] : [])]} />
          ${(p.flags || []).length > 0 && html`<div class="row wrap gap-s" style="margin-top:8px">${p.flags.map((x) => html`<span class="chip bad">${FLAW[x] || x}</span>`)}</div>`}
        </div>

        ${v && html`<div><h3>地端模型的細項評分（${p.vlm_model}）</h3>
          <${Bars} kind="local" rows=${VLM_KEYS.map(([k, l]) => [l, v[k]])} />
          ${(v.main || v.shot) && html`<div class="kv" style="margin-top:10px">
            ${v.main && html`<span>主體</span><span>${v.main}</span>`}
            ${v.shot && html`<span>景別</span><span>${v.shot}${v.setting ? `・${v.setting}` : ''}</span>`}
            ${v.light_type && html`<span>光線</span><span>${v.light_type}</span>`}
            ${v.techniques && v.techniques.length > 0 && html`<span>構圖</span><span>${v.techniques.join('、')}</span>`}
            ${v.palette && html`<span>色調</span><span>${v.palette}</span>`}
            ${v.moment && html`<span>瞬間</span><span>捕捉到稍縱即逝的瞬間</span>`}</div>`}
          ${v.mood && html`<p class="small muted" style="margin-top:8px">氛圍：${v.mood}</p>`}
          ${v.flaws && v.flaws.length > 0 && html`<p class="small muted">缺點：${v.flaws.join('；')}</p>`}</div>`}

        ${p.contests.map((c) => html`<div><h3>比賽：${c.name}${c.final_rank ? `　第 ${c.final_rank} 名` : ''}</h3>
          <div class="scoreline">
            ${c.final_total != null && html`<span>外部決選 <b>${Math.round(c.final_total)}</b></span>`}
            ${c.teacher_total != null && html`<span>外部校準 <b>${Math.round(c.teacher_total)}</b></span>`}
            <span>地端評審 <b>${Math.round(c.local_total)}</b></span></div>
          ${c.final && html`<p class="small" style="margin-top:6px"><b>${c.final.title}</b>　${c.final.strengths}</p>
            <p class="small muted">可能扣分：${c.final.weaknesses}</p><p class="small muted">投稿前調整：${c.final.edit}</p>`}
          ${!c.final && c.local && html`<p class="small muted" style="margin-top:6px">${c.local.reason}</p>`}</div>`)}

        ${p.group.length > 1 && html`<div><h3>同一組連拍或重複（${p.group.length} 張）</h3>
          <div class="strip">${p.group.map((g) => html`<div class="mini ${g.id === p.id ? 'cur' : ''}" title="點一下切換到這張"
            onClick=${() => { const j = ids.indexOf(g.id); if (j >= 0) setI(j); else load(g.id); }}>
            <img src=${thumb(g.id)} alt="" />${g.is_best ? html`<span class="badge pick">最佳</span>` : ''}</div>`)}</div>
          ${!p.is_best && html`<button class="btn sm" style="margin-top:8px" onClick=${() => setBest(p.id)}>改用這張當最佳</button>`}</div>`}

        <div><h3>拍攝資訊</h3>
          <div class="kv">
            <span>時間</span><span>${fmtDate(p.taken_at)}${p.time_source === 'mtime' ? '（檔案時間）' : ''}</span>
            ${p.light && html`<span>光線時段</span><span>${({ night: '夜晚', blue: '藍調時刻', golden: '黃金時刻', day: '白天' })[p.light]}${p.sun_elev != null ? `（太陽高度 ${Math.round(p.sun_elev)}°）` : ''}</span>`}
            ${p.event_title && html`<span>事件</span><span>${p.event_title}</span>`}
            <span>主題</span><span>${p.category_label || '—'}${p.category_top && p.category_top[1] ? `、${CATS[p.category_top[1][0]] || ''}` : ''}</span>
            ${p.camera && html`<span>相機</span><span>${p.camera}${p.lens ? `　${p.lens}` : ''}</span>`}
            ${exposure && html`<span>曝光</span><span>${exposure}</span>`}
            <span>尺寸</span><span class="num">${p.width} × ${p.height}${p.raw_path ? '　含 RAW' : ''}</span>
          </div>
          <button class="btn sm" style="margin-top:10px" onClick=${() => api('/reveal/' + p.id, { method: 'POST' }).catch(fail)}>
            <${Icon} name="folder" size=${13} /> 在檔案管理員中顯示</button>
        </div>
        <p class="small faint">← → 切換照片　0–5 給星等　Esc 關閉</p>`}`}
      </div>
    </div>
  </div>`;
}
