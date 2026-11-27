// A solo exhibition: find a series in the library, have it curated, write the words, hand it in.
import { html, useState, useEffect, useCallback, api, fail, toast, useApp, Icon, Dialog, usePickFolder, thumb, n0 } from './ui.js';
import { PhotoViewer } from './library.js';

const mb = (n) => (n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB` : `${(n / 1e6).toFixed(1)} MB`);
const when = (ts) => new Date(ts * 1000).toLocaleString('zh-TW', { hour12: false }).slice(0, -3);
const ORIENT = { both: '橫式直式都可以', landscape: '只能橫式', portrait: '只能直式' };
const EXIF = [['all', '全部保留（含 GPS 位置）'], ['no_gps', '保留，但移除 GPS 位置'], ['none', '全部移除']];
const MARK = { ok: '✓', warn: '!', error: '✕' };
const BLANK = { name: '', spec: { count: 20, frame_w: 75, frame_h: 50, orientation: 'both', author: '', desc_max: 80 } };

function PrintChip({ p }) {
  const tone = p.level === 'ok' ? 'local' : p.level === 'soft' ? '' : 'bad';
  return html`<span class="chip ${tone}" title="印成畫框大小時每英吋的像素數。150 以上近看也清楚，100 到 150 站近會略軟。">
    ${p.level === 'low' ? '! ' : ''}${p.dpi} dpi${p.orientation === 'portrait' ? '・直式' : ''}${p.trim > 0.01 ? `・裁 ${Math.round(p.trim * 100)}%` : ''}</span>`;
}

function Form({ initial, onClose, onSaved }) {
  const [f, setF] = useState(JSON.parse(JSON.stringify(initial)));
  const [busy, setBusy] = useState(false);
  const sp = (k, v) => setF({ ...f, spec: { ...f.spec, [k]: v } });
  const save = async () => {
    setBusy(true);
    try {
      onSaved(f.id ? await api('/exhibitions/' + f.id, { method: 'PUT', body: { name: f.name, spec: f.spec } }) : await api('/exhibitions', { body: f }));
    } catch (e) { fail(e); setBusy(false); }
  };
  return html`<${Dialog} title=${f.id ? '展覽設定' : '新增展覽'} onClose=${onClose}
    footer=${html`<button class="btn" onClick=${onClose}>取消</button>
      <button class="btn primary" disabled=${busy || !f.name.trim()} onClick=${save}>${f.id ? '儲存' : '建立'}</button>`}>
    <div class="stack">
      <div class="field"><span>展覽名稱</span><input type="text" value=${f.name} placeholder="2026 員工攝影展" onInput=${(e) => setF({ ...f, name: e.target.value })} /></div>
      <div class="field"><span>創作者姓名（會印在每張說明牌上）</span><input type="text" maxlength="40" value=${f.spec.author} onInput=${(e) => sp('author', e.target.value)} /></div>
      <div class="row wrap top gap-l">
        <div class="field" style="width:120px"><span>作品張數</span><input type="number" min="1" value=${f.spec.count} onChange=${(e) => sp('count', +e.target.value)} /></div>
        <div class="field" style="width:120px"><span>畫框長邊（公分）</span><input type="number" min="5" value=${f.spec.frame_w} onChange=${(e) => sp('frame_w', +e.target.value)} /></div>
        <div class="field" style="width:120px"><span>畫框短邊（公分）</span><input type="number" min="5" value=${f.spec.frame_h} onChange=${(e) => sp('frame_h', +e.target.value)} /></div>
        <div class="field" style="width:170px"><span>畫框方向</span><select value=${f.spec.orientation} onChange=${(e) => sp('orientation', e.target.value)}>
          ${Object.entries(ORIENT).map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select></div>
        <div class="field" style="width:150px"><span>作品說明字數上限</span><input type="number" min="20" step="10" value=${f.spec.desc_max} onChange=${(e) => sp('desc_max', +e.target.value)} /></div>
      </div>
      <p class="small muted">畫框固定時，直式照片能不能展出要看主辦單位讓不讓畫框轉 90 度。不確定就先選「都可以」，直式的作品會標示出來。</p>
    </div>
  <//>`;
}

// Start a theme from examples: tick a few photos, and the rest of the series is found from them.
function SeedPicker({ base, count, onClose, onDone }) {
  const { status } = useApp();
  const [list, setList] = useState(null);
  const [sel, setSel] = useState([]);
  const [only, setOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const local = status ? status.local : {};
  useEffect(() => {
    api(base + '/choices').then((l) => { setList(l); setSel(l.filter((p) => p.picked).slice(0, count).map((p) => p.photo_id)); }).catch(fail);
  }, []);
  const toggle = (pid) => setSel((cur) => (cur.includes(pid) ? cur.filter((v) => v !== pid) : cur.length < count ? [...cur, pid] : cur));
  const start = async () => {
    setBusy(true);
    try { onDone(await api(base + '/theme', { body: { seeds: sel } })); } catch (e) { fail(e); setBusy(false); }
  };
  const picked = list ? list.filter((p) => p.picked).length : 0;
  const shown = (list || []).filter((p) => !only || p.picked || sel.includes(p.photo_id));
  return html`<${Dialog} wide title="從我預選的照片開始" sub="勾選幾張「我想要的就是這種感覺」的照片，建議 3 到 8 張。" onClose=${onClose}
    footer=${html`<span class="small muted grow">已選 ${sel.length} 張${local.ok ? '。地端模型會看這幾張，替系列命名並描述還有哪些畫面屬於它，大約需要十幾秒。' : '。地端模型沒有連上，會只用畫面相似度找照片，系列名稱請自己取。'}</span>
      <button class="btn" onClick=${onClose}>取消</button>
      <button class="btn local" disabled=${busy || !sel.length} onClick=${start}>${busy ? html`<span class="spin"></span>` : ''} 用這 ${sel.length} 張定義主題</button>`}>
    <div class="row wrap" style="margin-bottom:10px">
      <span class="small muted grow">${picked > 0 ? `你在圖庫挑選（按 P）過的 ${picked} 張排在最前面，已經先幫你勾起來。` : '想從很多照片裡找，可以先到「圖庫」用搜尋和篩選找到照片，按 P 挑選，再回到這裡。'}
        你選的照片一定會進候選，策展人也會被告知這幾張要優先保留。</span>
      ${picked > 0 && html`<label class="check small"><input type="checkbox" checked=${only} onChange=${(e) => setOnly(e.target.checked)} /> 只看我挑選過的</label>`}
      ${sel.length > 0 && html`<a class="small" onClick=${() => setSel([])}>全部取消</a>`}
    </div>
    ${!list && html`<div class="row muted"><span class="spin"></span> 讀取中…</div>`}
    <div class="pickgrid">${shown.map((p) => html`<div key=${p.photo_id} class=${sel.includes(p.photo_id) ? 'on' : ''} title=${p.caption || ''} onClick=${() => toggle(p.photo_id)}>
      <img loading="lazy" src=${thumb(p.photo_id)} alt="" />
      ${sel.includes(p.photo_id) && html`<b>${sel.indexOf(p.photo_id) + 1}</b>`}
      ${p.print.level !== 'ok' && html`<span class="chip ${p.print.level === 'low' ? 'bad' : ''}">${p.print.dpi} dpi</span>`}
      ${p.picked && html`<i title="你在圖庫挑選過">P</i>`}</div>`)}</div>
    ${list && list.length === 0 && html`<p class="muted">相片庫裡沒有夠印這個尺寸的照片。</p>`}
  <//>`;
}

export function Exhibitions({ id }) {
  const { go, jobTick, status } = useApp();
  const [list, setList] = useState(null);
  const [form, setForm] = useState(null);
  const load = useCallback(() => api('/exhibitions').then(setList).catch(fail), []);
  useEffect(() => { load(); }, [jobTick, id]);
  const dlg = form && html`<${Form} initial=${form} onClose=${() => setForm(null)} onSaved=${(x) => { setForm(null); load(); go('exhibit', x.id); }} />`;
  if (id) return html`<div class="page"><${Exhibition} key=${id} id=${id} onEdit=${setForm} onDeleted=${() => { load(); go('exhibit'); }} />${dlg}</div>`;
  return html`<div class="page">
    <div class="page-head"><div class="grow"><h1>展覽</h1>
      <span class="sub">個展要的不是二十張最好的照片，而是二十張放在一起成立的照片。這裡從相片庫找出系列、請策展人選片排序，再準備說明牌和個人簡介。</span></div>
      <button class="btn primary" onClick=${() => setForm(BLANK)}>新增展覽</button></div>
    ${status && !status.analysed && html`<div class="empty"><p>先到「圖庫」加入資料夾並分析。</p></div>`}
    ${list && list.length === 0 && html`<div class="empty"><p>還沒有展覽。按「新增展覽」開始，預設是 20 張作品、75×50 公分的畫框。</p></div>`}
    <div class="outings">${(list || []).map((x) => html`<div class="outing" key=${x.id} onClick=${() => go('exhibit', x.id)}>
      ${x.cover_id ? html`<img loading="lazy" src=${thumb(x.cover_id)} alt="" />` : html`<div class="noimg"></div>`}
      <div class="stack gap-s grow"><b class="ellipsis">${x.name}</b>
        <span class="small muted">${x.series ? `系列：${x.series}` : '還沒有選定主題'}</span>
        <span class="small ${x.works === x.count ? '' : 'faint'}">${x.works} / ${x.count} 張</span></div></div>`)}</div>
    ${dlg}
  </div>`;
}

function Step({ ix, kind, done, title, hint, action, children }) {
  return html`<section class="step ${kind || ''} ${done ? 'done' : ''}">
    <header><span class="ix">${done ? html`<${Icon} name="check" size=${14} />` : ix}</span>
      <div class="grow"><h2>${title}　${kind && html`<span class="chip ${kind}">${kind === 'cloud' ? '外部模型' : '地端模型'}</span>`}</h2>
        <div class="sub">${hint}</div></div>${action}</header>
    ${children && html`<div class="content">${children}</div>`}
  </section>`;
}

function Work({ w, x, last, local, asking, reserves, onPatch, onArrange, onAsk, onOpen, onEdit }) {
  const [title, setTitle] = useState(w.title || '');
  const [desc, setDesc] = useState(w.description || '');
  useEffect(() => { setTitle(w.title || ''); }, [w.title]);
  useEffect(() => { setDesc(w.description || ''); }, [w.description]);
  const answer = (ix, a) => onPatch({ questions: w.questions.map((q, k) => (k === ix ? { ...q, a } : q)) });
  const max = x.spec.desc_max;
  return html`<div class="entry">
    <div class="no">${w.no}
      <div class="stack gap-s" style="margin-top:8px;align-items:center">
        <button class="btn ghost sm" title="往前移" disabled=${w.no === 1} onClick=${() => onArrange({ by: -1 })}><${Icon} name="up" size=${13} /></button>
        <button class="btn ghost sm" title="往後移" disabled=${last} onClick=${() => onArrange({ by: 1 })} style="transform:rotate(180deg)"><${Icon} name="up" size=${13} /></button>
      </div></div>
    <div class="stack gap-s">
      <img loading="lazy" src=${w.use_edit ? `/api/edit/${w.photo_id}/render?r=saved&px=1024` : thumb(w.photo_id)} alt="" onClick=${onOpen} />
      <div class="row wrap gap-s"><${PrintChip} p=${w.print} />
        <button class="btn sm" onClick=${onEdit}><${Icon} name="edit" size=${13} /> ${w.edited ? '繼續後製' : '後製與裁切'}</button></div>
      ${w.edited && html`<div class="seg"><button class=${w.use_edit ? '' : 'on'} onClick=${() => onPatch({ use_edit: false })}>展原圖</button>
        <button class=${w.use_edit ? 'on' : ''} onClick=${() => onPatch({ use_edit: true })}>展修圖後的版本</button></div>`}
      ${w.missing && html`<div class="note bad">原始檔已經不在原本的位置</div>`}
      <div class="kv small facts">${w.facts.filter(([k]) => k !== '原始尺寸').map(([k, v]) => html`<span>${k}</span><span>${v}</span>`)}
        <span>輸出</span><span>${w.print.pixels[0]} × ${w.print.pixels[1]} 像素</span></div>
    </div>
    <div class="stack">
      <div class="row">
        <input type="text" class="grow ttl" maxlength="60" placeholder="作品名稱" value=${title}
          onInput=${(e) => setTitle(e.target.value)} onBlur=${() => title.trim() !== (w.title || '') && onPatch({ title })} />
        ${reserves.length > 0 && html`<select value="" title="用一張候補換下這張，位置不變" onChange=${(e) => e.target.value && onArrange({ swap_with: +e.target.value })}>
          <option value="">換成候補…</option>${reserves.map((r) => html`<option value=${r.photo_id}>${r.title || r.filename}</option>`)}</select>`}
        <button class="btn ghost sm" title="從牆上拿下來，放進候補" onClick=${() => onArrange({ role: 'reserve' })}>移到候補</button>
      </div>
      ${w.why && html`<p class="small muted"><span class="chip cloud">策展人</span>　${w.why}</p>`}
      <div class="bgq">
        <div class="row"><b class="grow">拍攝背景 <span class="small muted" style="font-weight:400">　只有你知道的事。回答了，說明才寫得出你的觀點</span></b>
          <button class="btn sm ${w.questions.length ? '' : 'local'}" disabled=${!local.ok || asking} title=${local.ok ? '' : local.error} onClick=${onAsk}>
            ${asking ? html`<span class="spin"></span>` : html`<${Icon} name="chip" size=${13} />`} ${w.questions.length ? '換一組問題' : '請地端模型問我'}</button></div>
        ${w.questions.map((q, k) => html`<div class="stack gap-s" key=${q.q}><span>${q.q}</span>
          <div class="row wrap gap-s">${q.options.map((o) => html`<span class="chip pick ${q.a === o ? 'on' : ''}" onClick=${() => answer(k, q.a === o ? '' : o)}>${o}</span>`)}</div>
          <input type="text" placeholder="或用自己的話回答" value=${q.options.includes(q.a) ? '' : q.a} onChange=${(e) => answer(k, e.target.value.trim())} /></div>`)}
      </div>
      <div class="field">
        <div class="row"><span class="grow small muted">作品說明（印在說明牌上）</span><span class="count ${desc.length > max ? 'over' : ''}">${desc.length} / ${max} 字</span></div>
        <textarea rows="3" placeholder="按下方的按鈕請模型起草，或自己寫" value=${desc}
          onInput=${(e) => setDesc(e.target.value)} onBlur=${() => desc.trim() !== (w.description || '') && onPatch({ description: desc })}></textarea>
      </div>
    </div>
  </div>`;
}

function Exhibition({ id, onEdit, onDeleted }) {
  const { status, jobTick, running, startJob, go } = useApp();
  const [x, setX] = useState(null);
  const [own, setOwn] = useState('');
  const [busy, setBusy] = useState('');
  const [asking, setAsking] = useState(0);
  const [confirm, setConfirm] = useState(null);
  const [view, setView] = useState(null);
  const [zip, setZip] = useState(false);
  const [picker, setPicker] = useState(false);
  const [posterAt, setPosterAt] = useState(0);      // non-zero while the poster preview is open; also busts the image cache
  const [pickFolder, folderDlg] = usePickFolder();
  const base = '/exhibitions/' + id;
  const load = useCallback(() => api(base).then(setX).catch(fail), [id]);
  useEffect(() => { load(); }, [id, jobTick]);
  if (!x) return html`<div></div>`;
  const cloud = status ? status.cloud : {};
  const local = status ? status.local : {};
  const sp = x.spec;
  const cur = x.curation;
  const call = (path, body, method) => api(base + path, { method: method || 'POST', body: body || {} }).then((r) => { setX(r); return r; }).catch(fail);
  const setSpec = (k, v) => call('', { spec: { [k]: v } }, 'PUT');
  const patch = (pid, body) => call(`/works/${pid}`, body, 'PATCH');
  const arrange = (pid, body) => call(`/works/${pid}/arrange`, body);
  const replacing = () => !x.works.length || window.confirm('換主題會清除目前的策展結果和候補名單，已經寫好的說明也會一起清掉。確定嗎？');
  const choose = async (body) => { if (replacing()) await call('/theme', body); };
  const ask = async (pid) => {
    setAsking(pid);
    try { await api(`${base}/works/${pid}/questions`, { method: 'POST' }); await load(); } catch (e) { fail(e); }
    setAsking(0);
  };
  const during = async (key, fn) => { setBusy(key); try { await fn(); } finally { setBusy(''); } };
  const copy = (use_cloud, only_empty) => during(use_cloud ? 'copy' : 'copy-local', async () => {
    if (!only_empty && x.works.some((w) => w.description) && !window.confirm('會重寫所有作品的說明，包含你已經改過的。確定嗎？')) return;
    const r = await call('/copy', { use_cloud, only_empty });
    if (r) toast(`寫好了 ${r.written} 段作品說明` + (r.tokens ? `，用了 ${n0(r.tokens)} tokens（只送文字）` : '') + (r.over_limit ? `。其中 ${r.over_limit} 段仍超過字數上限` : '。'));
  });
  const bio = (use_cloud) => during(use_cloud ? 'bio' : 'bio-local', async () => {
    if ((x.profile.bio || x.profile.statement) && !window.confirm('會重寫個人簡介和創作自述。確定嗎？')) return;
    const r = await call('/profile/write', { use_cloud });
    if (r) toast('簡介和自述寫好了' + (r.written.tokens ? `，用了 ${n0(r.written.tokens)} tokens（只送文字）` : '') + (r.written.answered ? '。' : '。你沒有回答任何問題，所以內容很簡短。'));
  });
  const interview = () => during('interview', () => call('/profile/questions'));
  const profile = (body) => call('/profile', body, 'PUT');
  const answerProfile = (ix, a) => profile({ questions: x.profile.questions.map((q, k) => (k === ix ? { ...q, a } : q)) });
  const pack = async () => {
    if (x.checks.some((c) => c.level === 'error') && !window.confirm('檢查清單裡還有問題，仍然要製作嗎？')) return;
    const dest = await pickFolder('交件資料夾要放在哪裡？', '放在這裡');
    if (dest) startJob(base + '/package', { dest, zip });
  };
  const remove = async () => {
    if (!window.confirm(`刪除展覽「${x.name}」？照片不受影響。`)) return;
    try { await api(base, { method: 'DELETE' }); onDeleted(); } catch (e) { fail(e); }
  };
  const openWorks = (pid, mode) => { const ids = x.works.map((w) => w.photo_id); setView({ ids, index: Math.max(ids.indexOf(pid), 0), mode }); };
  const chapters = cur ? cur.chapters : [];
  const empty = x.works.filter((w) => !w.description || !w.title).length;
  const frame = `${sp.frame_w}×${sp.frame_h} 公分`;

  return html`<div>
    <div class="row" style="margin-bottom:12px">
      <button class="btn ghost sm" onClick=${() => go('exhibit')}><${Icon} name="left" size=${13} /> 所有展覽</button>
      <div class="grow"><h1>${x.name}</h1>
        <div class="sub">${sp.count} 張作品　畫框 ${frame}　${ORIENT[sp.orientation]}${sp.author ? `　創作者：${sp.author}` : ''}</div></div>
      <button class="btn sm" onClick=${() => onEdit({ id: x.id, name: x.name, spec: sp })}><${Icon} name="edit" size=${13} /> 設定</button>
      <button class="btn sm danger" onClick=${remove}><${Icon} name="trash" size=${13} /></button>
    </div>

    <${Step} ix="1" kind="local" done=${!!x.theme} title="主題"
      hint=${`把相片庫裡夠印 ${frame} 的照片用幾種方式分組：畫面風格、地點、概念、題材，每一組就是一個可能的系列。分組不需要模型，地端模型只負責替每一組命名。`}
      action=${html`<button class="btn ${x.ideas ? '' : 'local'}" disabled=${running} onClick=${() => startJob(base + '/propose')}>
        <${Icon} name="chip" size=${14} /> ${x.ideas ? '重新提案' : '請地端模型提案'}</button>`}>
      ${x.theme && html`<div class="note local"><b>目前的主軸：${x.theme.title}</b>${x.theme.pitch && x.theme.pitch !== x.theme.title ? html`<br />${x.theme.pitch}` : ''}
        ${x.theme.thread ? html`<br /><span class="small">${x.theme.thread}</span>` : ''}
        ${x.theme.kind === 'seeds'
          ? html`<br /><span class="small">從你預選的 ${x.theme.pinned.length} 張出發，另外找到 ${x.pool.length - x.pool.filter((p) => p.pinned).length} 張建議的照片。
              ${x.theme.probes && x.theme.probes.length ? `除了畫面相近的，也找了這些畫面：${x.theme.probes.join('、')}。` : ''}系列名稱是模型取的，可以在策展後再改。</span>`
          : html`<br /><span class="small">符合的照片有 ${x.theme.available} 張，策展人會看到其中最好的 ${x.pool.length} 張。</span>`}</div>`}
      ${x.ideas && x.ideas.ideas.length === 0 && html`<div class="note bad">相片庫裡只有 ${n0(x.ideas.eligible)} 張照片夠清楚、也夠印 ${frame}，不足以組成系列。
        印這個尺寸，照片的長邊至少要 ${n0(Math.ceil((sp.min_dpi * sp.frame_w) / 2.54))} 像素；從網路下載或通訊軟體傳來的縮小檔通常不夠，請匯入相機或手機的原始檔。</div>`}
      ${x.ideas && x.ideas.ideas.length > 0 && html`<p class="small muted">相片庫裡有 ${n0(x.ideas.eligible)} 張照片夠清楚、也夠印 ${frame}。下面是從中找到的 ${x.ideas.ideas.length} 個方向：</p>
        <div class="ideas">${x.ideas.ideas.map((t) => html`<div class="idea ${x.theme && x.theme.key === t.key ? 'on' : ''}" key=${t.key}>
          <div class="strip8">${t.cover.map((pid) => html`<img loading="lazy" src=${thumb(pid)} alt="" />`)}</div>
          <div class="row"><b class="grow ttl">${t.title}</b><span class="chip">${t.kind_zh}${t.title !== t.hint ? `：${t.hint}` : ''}</span></div>
          ${t.pitch && html`<p class="small">${t.pitch}</p>`}
          ${t.thread && html`<p class="small muted">${t.thread}</p>`}
          <div class="row"><span class="small grow ${t.enough ? 'muted' : ''}">${t.enough ? `${t.count} 張可用` : `只有 ${t.count} 張，不足 ${sp.count} 張`}</span>
            <button class="btn sm ${x.theme && x.theme.key === t.key ? 'on' : ''}" onClick=${() => choose({ key: t.key })}>${x.theme && x.theme.key === t.key ? '目前的主軸' : '用這個當主軸'}</button></div>
        </div>`)}</div>`}
      <div class="row"><input type="text" class="grow" style="max-width:520px" value=${own} placeholder="或自己描述主題，例如：旅途中安靜的水面"
        onInput=${(e) => setOwn(e.target.value)} onKeyDown=${(e) => e.key === 'Enter' && own.trim() && choose({ text: own })} />
        <button class="btn" disabled=${!own.trim()} onClick=${() => choose({ text: own })}>用我的主題找照片</button></div>
      <div class="row"><button class="btn" onClick=${() => replacing() && setPicker(true)}><${Icon} name="check" size=${14} /> 從我預選的照片開始…</button>
        <span class="small muted">先挑幾張你心目中的代表作，由它們定義主題，再從相片庫找出其他適合的照片。</span></div>
    <//>

    <${Step} ix="2" kind="cloud" done=${!!cur} title="策展"
      hint=${`外部模型看候選照片的縮圖總表，像策展人一樣選出 ${sp.count} 張、分成幾個章節、排好展出順序，每個章節另外留 ${sp.reserves} 張候補，並指出系列的弱點和缺口。`}
      action=${html`<div class="row">
        <button class="btn ${cur ? '' : 'primary'}" disabled=${running || !x.pool.length || !cloud.has_key}
          onClick=${() => api(base + '/outgoing').then(setConfirm).catch(fail)}><${Icon} name="cloud" size=${14} /> ${cur ? '重新策展' : `請 ${cloud.label || '外部模型'} 策展`}</button>
        <button class="btn" disabled=${running || !x.pool.length || !local.ok} onClick=${() => startJob(base + '/curate', { mode: 'local' })}>用地端模型策展</button></div>`}>
      ${x.theme && html`<details open=${!cur}><summary class="muted small" style="cursor:pointer">候選照片 ${x.pool.length} 張（點一下把不想展出的移除）</summary>
        <div class="poolgrid">${x.pool.map((p) => html`<div key=${p.photo_id} title=${p.caption || ''}>
          <img loading="lazy" src=${thumb(p.photo_id)} alt="" onClick=${() => setView({ ids: x.pool.map((q) => q.photo_id), index: x.pool.findIndex((q) => q.photo_id === p.photo_id) })} />
          <button class="btn ghost sm" title="不要這張" onClick=${() => call('/pool', { excluded: [...x.theme.excluded, p.photo_id] }, 'PUT')}><${Icon} name="x" size=${12} /></button>
          ${p.print.level !== 'ok' && html`<span class="chip">${p.print.dpi} dpi</span>`}
          ${p.pinned && html`<i title="你預選的照片，策展人會優先保留">預選</i>`}</div>`)}</div>
        ${x.theme.excluded.length > 0 && html`<a class="small" onClick=${() => call('/pool', { excluded: [] }, 'PUT')}>放回被移除的 ${x.theme.excluded.length} 張</a>`}</details>`}
      ${cur && html`<div class="curation">
        <div class="headline">${cur.title}</div>
        <p>${cur.statement}</p>
        ${cur.sequence_note && html`<p class="small muted"><b>排序</b>　${cur.sequence_note}</p>`}
        <div class="row wrap gap-s">${chapters.map((c, i) => html`<span class="chip" title=${c.idea}>第 ${i + 1} 章　${c.title}　${x.works.filter((w) => w.chapter === i).length} 張</span>`)}</div>
        ${cur.weak.length > 0 && html`<div><h3>最該考慮更換的</h3>
          <div class="notes">${cur.weak.map((k) => html`<div class="note-photo" onClick=${() => openWorks(k.photo_id)}>
            <img loading="lazy" src=${thumb(k.photo_id)} alt="" /><span class="small">${k.why}</span></div>`)}</div></div>`}
        ${cur.gaps.length > 0 && html`<div><h3>系列還缺什麼</h3><ul class="plain">${cur.gaps.map((g) => html`<li>${g}</li>`)}</ul></div>`}
        <span class="small faint">${cur.provider === 'local' ? '地端模型' : cloud.label || cur.provider} 策展　${when(cur.at)}${cur.tokens ? `　${n0(cur.tokens)} tokens` : ''}</span>
      </div>`}
      ${!cur && x.theme && !cloud.has_key && html`<div class="note">還沒有設定外部模型，可以先用地端模型策展。<a onClick=${() => go('settings')}>前往設定</a></div>`}
    <//>

    ${x.works.length > 0 && html`<${Step} ix="3" done=${x.works.length === sp.count} title=${`作品　${x.works.length} / ${sp.count}`}
      hint="依展出順序排列。可以調整順序、用候補替換、為每張作品命名並回答拍攝背景。作品檔在最後一步輸出時才會裁成畫框的比例。">
      ${chapters.map((c, i) => html`<div key=${i}>
        <div class="chapter"><b>第 ${i + 1} 章　${c.title}</b><span class="small muted">${c.idea}</span></div>
        ${x.works.filter((w) => w.chapter === i).map((w) => html`<${Work} key=${w.photo_id} w=${w} x=${x} last=${w.no === x.works.length} local=${local} asking=${asking === w.photo_id}
          reserves=${x.reserves.filter((r) => r.chapter === i).concat(x.reserves.filter((r) => r.chapter !== i))}
          onPatch=${(b) => patch(w.photo_id, b)} onArrange=${(b) => arrange(w.photo_id, b)} onAsk=${() => ask(w.photo_id)}
          onOpen=${() => openWorks(w.photo_id)} onEdit=${() => openWorks(w.photo_id, 'edit')} />`)}
        ${x.reserves.filter((r) => r.chapter === i).length > 0 && html`<div class="reserves"><span class="small muted">這一章的候補</span>
          ${x.reserves.filter((r) => r.chapter === i).map((r) => html`<div class="reserve" key=${r.photo_id}>
            <img loading="lazy" src=${thumb(r.photo_id)} alt="" onClick=${() => setView({ ids: [r.photo_id], index: 0 })} />
            <div class="stack gap-s grow"><div class="row gap-s"><b>${r.title || r.filename}</b><${PrintChip} p=${r.print} /></div>
              ${r.why && html`<span class="small muted">${r.why}</span>`}</div>
            <button class="btn sm" onClick=${() => arrange(r.photo_id, { role: 'work' })}>加到這一章</button>
            <button class="btn ghost sm" onClick=${() => arrange(r.photo_id, { role: 'none' })}>移除</button></div>`)}</div>`}
      </div>`)}
    <//>`}

    <${Step} ix="4" kind="cloud" done=${empty === 0 && !!x.profile.bio} title="說明牌與個人簡介"
      hint="作品名稱、作品說明、個人簡介和創作自述都只用文字起草，照片不會再上傳。模型被要求不可以編造你沒說過的事，所以你回答得越多，寫出來的越像你。">
      <div class="row wrap"><h3 class="grow">作品名稱與說明</h3>
        <select value=${sp.tone} onChange=${(e) => setSpec('tone', e.target.value)}>${x.tones.map((t) => html`<option value=${t}>語氣：${t}</option>`)}</select>
        <button class="btn primary" disabled=${!!busy || !cloud.has_key} onClick=${() => copy(true, false)}>
          ${busy === 'copy' ? html`<span class="spin"></span>` : html`<${Icon} name="cloud" size=${14} />`} 請 ${cloud.label || '外部模型'} 寫</button>
        ${empty > 0 && empty < x.works.length && html`<button class="btn" disabled=${!!busy || !cloud.has_key} onClick=${() => copy(true, true)}>只補還沒寫的 ${empty} 張</button>`}
        <button class="btn ghost" disabled=${!!busy || !local.ok} onClick=${() => copy(false, empty > 0 && empty < x.works.length)}>
          ${busy === 'copy-local' ? html`<span class="spin"></span>` : html`<${Icon} name="chip" size=${14} />`} 改用地端模型寫</button></div>
      <p class="small muted">${x.works.length} 張作品${x.reserves.length ? `和 ${x.reserves.length} 張候補` : ''}一次寫完，候補也先寫好，替換時不必再等。寫完請回到上面逐張讀過。</p>

      <div class="subbox"><div class="row"><h3 class="grow">個人簡介與創作自述（展覽海報用）</h3>
        <button class="btn sm ${x.profile.questions ? '' : 'local'}" disabled=${!!busy || !local.ok} onClick=${interview}>
          ${busy === 'interview' ? html`<span class="spin"></span>` : html`<${Icon} name="chip" size=${13} />`} ${x.profile.questions ? '換一組問題' : '請地端模型訪問我'}</button></div>
        ${(x.profile.questions || []).map((q, k) => html`<div class="bgq" key=${q.q}><span>${q.q}</span>
          <div class="row wrap gap-s">${q.options.map((o) => html`<span class="chip pick ${q.a === o ? 'on' : ''}" onClick=${() => answerProfile(k, q.a === o ? '' : o)}>${o}</span>`)}</div>
          <input type="text" placeholder="或用自己的話回答" value=${q.options.includes(q.a) ? '' : q.a} onChange=${(e) => answerProfile(k, e.target.value.trim())} /></div>`)}
        <div class="row wrap"><button class="btn primary" disabled=${!!busy || !cloud.has_key} onClick=${() => bio(true)}>
          ${busy === 'bio' ? html`<span class="spin"></span>` : html`<${Icon} name="cloud" size=${14} />`} 請 ${cloud.label || '外部模型'} 起草</button>
          <button class="btn ghost" disabled=${!!busy || !local.ok} onClick=${() => bio(false)}>${busy === 'bio-local' ? html`<span class="spin"></span>` : ''} 改用地端模型寫</button>
          <span class="small muted">送出的只有你的回答、系列名稱和從照片檔得知的事實（拍攝時間、地點、器材）。</span></div>
        <div class="split-plan">
          <div class="field"><span>個人簡介</span><textarea rows="5" value=${x.profile.bio || ''} placeholder="可以自己寫，也可以請模型起草後再改"
            onChange=${(e) => profile({ bio: e.target.value })}></textarea></div>
          <div class="field"><span>創作自述</span><textarea rows="5" value=${x.profile.statement || ''} placeholder="這個系列在拍什麼、為什麼拍、希望觀眾看到什麼"
            onChange=${(e) => profile({ statement: e.target.value })}></textarea></div>
        </div></div>
    <//>

    <${Step} ix="5" done=${x.packages.length > 0} title="交件"
      hint=${`輸出主辦單位要的三樣東西：${sp.count} 張裁成畫框比例的作品檔、每張一頁的說明牌簡報（PowerPoint，校稿時可以直接改字），以及個人簡介；另外附一張放了全部作品的海報。原始檔只會被讀取，不會更動。`}>
      <div class="row wrap top gap-l">
        <div class="field" style="width:180px"><span>創作者姓名</span><input type="text" maxlength="40" value=${sp.author} onChange=${(e) => setSpec('author', e.target.value)} /></div>
        <div class="field" style="width:220px"><span>作品檔內的拍攝資訊（EXIF）</span><select value=${sp.exif} onChange=${(e) => setSpec('exif', e.target.value)}>
          ${EXIF.map(([v, l]) => html`<option value=${v}>${l}</option>`)}</select></div>
        <div class="field" style="width:150px"><span>作品說明字數上限</span><input type="number" min="20" step="10" value=${sp.desc_max} onChange=${(e) => setSpec('desc_max', +e.target.value)} /></div>
      </div>
      <div class="row wrap top gap-l">
        <div class="field" style="width:320px"><span>海報上的展期與地點（選填，排定後再填）</span>
          <input type="text" maxlength="80" value=${sp.poster_note} placeholder="例如：2027.01.05 – 01.30　一樓藝廊" onChange=${(e) => setSpec('poster_note', e.target.value)} /></div>
        <div class="field" style="width:130px"><span>海報底色</span><select value=${sp.poster_style} onChange=${(e) => setSpec('poster_style', e.target.value)}>
          <option value="light">白底</option><option value="dark">黑底</option></select></div>
        <div class="field"><span> </span><button class="btn" disabled=${!x.works.length} onClick=${() => setPosterAt(Date.now())}>預覽海報</button></div>
      </div>
      <p class="small muted">海報是 A3 直式（300 dpi 的 JPEG 和 PDF），放上系列名稱、創作者、創作自述和全部作品，依展出順序排列。正式的展覽海報由主辦單位製作，這一張可以當作參考或自己留存。</p>
      <ul class="checks">${x.checks.map((c) => html`<li class=${c.level}><b>${MARK[c.level]}</b><span>${c.text}</span></li>`)}</ul>
      <div class="row wrap"><button class="btn primary" disabled=${running || !x.works.length} onClick=${pack}><${Icon} name="out" size=${14} /> 製作交件資料夾…</button>
        <label class="check small"><input type="checkbox" checked=${zip} onChange=${(e) => setZip(e.target.checked)} /> 同時打包成一個 zip</label>
        <span class="small muted">作品檔以原解析度輸出，轉成 sRGB，一張可能有十幾 MB。</span></div>
      ${x.packages.length > 0 && html`<div>${[...x.packages].reverse().map((p) => html`<div class="row small pkgrow" key=${p.at}>
        <span class="chip local">交件</span><span>${p.count} 張・${mb(p.bytes)}</span><span class="muted">${when(p.at)}</span>
        <span class="grow mono ellipsis ${p.exists ? '' : 'faint'}" title=${p.folder}>${p.folder}${p.exists ? '' : '（已移走或刪除）'}</span>
        ${p.exists && html`<a onClick=${() => api(base + '/reveal', { body: { at: p.at } }).catch(fail)}>打開資料夾</a>`}</div>`)}</div>`}
    <//>

    ${confirm && html`<${Dialog} wide title=${`送出 ${confirm.photo_ids.length} 張候選的縮圖總表給 ${confirm.provider}`} sub=${`模型：${confirm.model || '（尚未選擇）'}`}
      onClose=${() => setConfirm(null)}
      footer=${html`<span class="small muted grow">預估約 ${n0(confirm.est_tokens)} tokens${confirm.via === 'cli' ? '，透過命令列工具送出，計入訂閱用量' : ''}</span>
        <button class="btn" onClick=${() => setConfirm(null)}>取消</button>
        <button class="btn primary" onClick=${() => { setConfirm(null); startJob(base + '/curate', { mode: 'cloud' }); }}><${Icon} name="cloud" size=${14} /> 送出</button>`}>
      <p class="muted">下面這 ${confirm.photo_ids.length} 張會拼成 ${confirm.sheets} 張編號的縮圖總表送出（每格 384px，不含拍攝時間、相機與 GPS），連同地端模型為每張寫的文字說明和列印解析度。
        策展人會從中選出 ${confirm.count} 張。原始檔不會上傳。</p>
      <div class="thumbs">${confirm.photo_ids.map((pid) => html`<img loading="lazy" src=${thumb(pid)} alt="" />`)}</div>
    <//>`}
    ${posterAt > 0 && html`<${Dialog} wide title="海報預覽" sub="A3 直式。這是縮小的預覽，輸出時是 3508 × 4961 像素。" onClose=${() => setPosterAt(0)}
      footer=${html`<span class="small muted grow">改了展期、底色、作品順序或創作自述之後，重新按「預覽海報」就會更新。</span>
        <button class="btn" onClick=${() => setPosterAt(0)}>關閉</button>`}>
      <img class="posterprev" src=${`/api${base}/poster?t=${posterAt}`} alt="海報預覽" />
    <//>`}
    ${picker && html`<${SeedPicker} base=${base} count=${sp.count} onClose=${() => setPicker(false)} onDone=${(r) => { setX(r); setPicker(false); }} />`}
    ${view && html`<${PhotoViewer} ids=${view.ids} index=${view.index} startMode=${view.mode || 'info'} onClose=${() => { setView(null); load(); }} />`}
    ${folderDlg}
  </div>`;
}
