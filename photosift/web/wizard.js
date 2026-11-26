// Defining a contest by conversation: fixed questions, model follow-ups, and a look at real photos.
import { html, useState, useEffect, useRef, api, fail, toast, useApp, Icon, n0, thumb } from './ui.js';
import { PhotoViewer } from './library.js';

const STEPS = ['比賽', '評審與目標', '規則', '追問', '用照片討論', '確認'];
const LABEL = { yes: '符合', maybe: '普通', no: '不符合' };
const EMPTY = { name: '', theme: '', rules: '', judging: '', max_entries: 0, profile: {}, constraints: {}, qa: [], scope: {}, labels: {}, insight: '' };

function Choice({ q, value, onChange }) {
  const picked = q.multi ? (value || []) : value;
  const toggle = (label) => {
    if (!q.multi) return onChange(picked === label ? '' : label);
    onChange(picked.includes(label) ? picked.filter((x) => x !== label) : [...picked, label]);
  };
  return html`<div class="wq">
    <h2>${q.title}${q.multi ? html` <span class="small muted">可複選</span>` : ''}</h2>
    ${q.why && html`<p class="small muted">${q.why}</p>`}
    <div class="wopts">${q.options.map((o) => html`<button class="wopt ${(q.multi ? picked.includes(o.label) : picked === o.label) ? 'on' : ''}"
      onClick=${() => toggle(o.label)}><b>${o.label}</b>${o.hint && html`<span>${o.hint}</span>`}</button>`)}</div>
  </div>`;
}

export function Wizard({ onClose, onCreated }) {
  const { status } = useApp();
  const [step, setStep] = useState(0);
  const [d, setD] = useState(EMPTY);
  const [questions, setQuestions] = useState([]);
  const [busy, setBusy] = useState('');
  const [paste, setPaste] = useState('');
  const [showPaste, setShowPaste] = useState(false);
  const [follow, setFollow] = useState(null);       // model-written questions
  const [rounds, setRounds] = useState([]);         // [[photo ids], [photo ids]]
  const [ins, setIns] = useState(null);             // the model's reading of the choices
  const [pv, setPv] = useState(null);               // counts and suggested sizes
  const [sizes, setSizes] = useState(null);
  const [facets, setFacets] = useState(null);
  const [view, setView] = useState(null);
  const local = status ? status.local : {};
  const cloud = status ? status.cloud : {};
  const set = (patch) => setD((cur) => ({ ...cur, ...patch }));
  const setCons = (k, v) => setD((cur) => ({ ...cur, constraints: { ...cur.constraints, [k]: v } }));
  const draft = () => ({ ...d, sizes: sizes || undefined });

  useEffect(() => {
    api('/wizard/questions').then((r) => setQuestions(r.questions)).catch(fail);
    api('/facets').then(setFacets).catch(() => {});
  }, []);

  // Live count of what survives the rules, so a rule that excludes everything is obvious at once.
  const consKey = JSON.stringify([d.constraints, d.scope, d.max_entries]);
  useEffect(() => {
    const t = setTimeout(() => api('/wizard/preview', { body: { draft: d } }).then((r) => { setPv(r); setSizes((s) => s || r.sizes); }).catch(() => {}), 350);
    return () => clearTimeout(t);
  }, [consKey]);
  useEffect(() => { if (pv) setSizes(pv.sizes); }, [pv && pv.eligible, d.max_entries]);

  const parse = async () => {
    setBusy('parse');
    try {
      const r = await api('/wizard/parse', { body: { text: paste } });
      set({ name: r.name || d.name, theme: r.theme || d.theme, rules: r.rules || d.rules, judging: r.judging, max_entries: r.max_entries || d.max_entries,
        constraints: { ...d.constraints, ...r.constraints } });
      setShowPaste(false);
      toast('已從簡章填入主題、規則和限制，請在接下來的步驟確認。');
    } catch (e) { fail(e); }
    setBusy('');
  };

  const askFollow = async (more) => {
    setBusy('follow');
    try {
      const r = await api('/wizard/followups', { body: { draft: d } });
      setFollow((cur) => (more && cur ? [...cur, ...r.questions.filter((q) => !cur.some((c) => c.question === q.question))] : r.questions));
    } catch (e) { fail(e); setFollow((cur) => cur || []); }
    setBusy('');
  };
  useEffect(() => { if (step === 3 && follow === null && local.ok) askFollow(false); }, [step]);
  const answer = (q, a) => setD((cur) => ({ ...cur, qa: [...cur.qa.filter((x) => x.q !== q), ...(a ? [{ q, a }] : [])] }));
  const answerOf = (q) => (d.qa.find((x) => x.q === q) || {}).a || '';

  const loadRound = async (n) => {
    setBusy('photos');
    try {
      const r = await api('/wizard/examples', { body: { draft: d, round: n, labels: d.labels } });
      set({ probes: r.probes });
      setRounds((cur) => [...cur.slice(0, n - 1), r.photo_ids]);
    } catch (e) { fail(e); }
    setBusy('');
  };
  useEffect(() => { if (step === 4 && !rounds.length) loadRound(1); }, [step]);
  const mark = (pid, v) => setD((cur) => ({ ...cur, labels: { ...cur.labels, [pid]: cur.labels[pid] === v ? undefined : v } }));
  const counts = Object.values(d.labels).reduce((a, v) => (v ? { ...a, [v]: (a[v] || 0) + 1 } : a), {});
  const summarise = async () => {
    setBusy('insight');
    try {
      const labels = Object.fromEntries(Object.entries(d.labels).filter(([, v]) => v));
      const r = await api('/wizard/insight', { body: { draft: d, labels } });
      setIns(r); set({ insight: r.hypothesis });
    } catch (e) { fail(e); }
    setBusy('');
  };

  const create = async (useCloud) => {
    if (!d.name.trim() && !d.theme.trim()) { toast('請至少填寫比賽名稱或主題。', 'err'); setStep(0); return; }
    setBusy('create');
    try {
      const labels = Object.fromEntries(Object.entries(d.labels).filter(([, v]) => v));
      const c = await api('/wizard/create', { body: { draft: { ...draft(), labels } } });
      try { await api(`/contests/${c.id}/coach`, { body: { use_cloud: useCloud } }); }
      catch (e) { fail(e); }  // the contest exists; the rubric can be requested again from its page
      onCreated(c);
    } catch (e) { fail(e); setBusy(''); }
  };

  const chips = (kind, items, idKey, label) => html`<div class="row wrap gap-s">${items.map((it) => html`
    <span class="chip pick ${(d.scope[kind] || []).includes(it[idKey]) ? 'on' : ''}" onClick=${() => {
      const cur = new Set(d.scope[kind] || []); cur.has(it[idKey]) ? cur.delete(it[idKey]) : cur.add(it[idKey]);
      set({ scope: { ...d.scope, [kind]: [...cur] } });
    }}>${label(it)}</span>`)}</div>`;
  const c = d.constraints;
  const needLocal = !local.ok && html`<div class="note bad">${local.error || '地端模型尚未就緒'}。這一步需要地端模型，可以先跳過。</div>`;

  const body = [
    // 0 ---------------------------------------------------------------- basics
    () => html`<div class="wq"><h2>先說說這場比賽</h2>
      <p class="muted">接下來幾步會問一些選擇題，幫 AI 弄清楚要用什麼標準看你的照片。每一步都可以跳過，之後也能修改。</p>
      <div class="field"><span>比賽名稱</span><input type="text" value=${d.name} onInput=${(e) => set({ name: e.target.value })} placeholder="公司年度攝影比賽" /></div>
      <div class="field"><span>主題</span><input type="text" value=${d.theme} onInput=${(e) => set({ theme: e.target.value })} placeholder="例如：旅途中的寧靜時刻。沒有指定主題可以留空" /></div>
      ${!showPaste ? html`<div><button class="btn" disabled=${!local.ok} title=${local.ok ? '' : local.error} onClick=${() => setShowPaste(true)}>
          <${Icon} name="edit" size=${14} /> 我有比賽簡章，直接貼上</button>
          <span class="small muted" style="margin-left:10px">地端模型會讀簡章，幫你填好主題、規則和限制</span></div>`
        : html`<div class="field"><span>貼上簡章或公告的文字</span>
          <textarea rows="8" value=${paste} onInput=${(e) => setPaste(e.target.value)} placeholder="把比賽辦法整段貼在這裡"></textarea>
          <div class="row"><button class="btn local" disabled=${busy || paste.trim().length < 20} onClick=${parse}>
            ${busy === 'parse' ? html`<span class="spin"></span>` : ''} 讀取簡章</button>
            <button class="btn ghost" onClick=${() => setShowPaste(false)}>取消</button></div></div>`}
    </div>`,
    // 1 ---------------------------------------------------- who judges, and why enter
    () => html`${d.judging && html`<div class="note local"><b>簡章上寫的評審方式</b><br />${d.judging}</div>`}
      ${questions.map((q) => html`<${Choice} key=${q.id} q=${q} value=${d.profile[q.id] || (q.multi ? [] : '')}
        onChange=${(v) => set({ profile: { ...d.profile, [q.id]: v } })} />`)}`,
    // 2 ------------------------------------------------------------------ rules
    () => html`<div class="wq"><h2>比賽有哪些硬性規定？</h2>
      <p class="muted">這些條件會直接從照片的拍攝資訊過濾，不需要任何模型。沒有規定的就留空。</p>
      <div class="row wrap top gap-l">
        <div class="field" style="width:170px"><span>拍攝期間，從</span><input type="text" placeholder="2026-01-01" value=${c.date_from || ''} onChange=${(e) => setCons('date_from', e.target.value.trim())} /></div>
        <div class="field" style="width:170px"><span>到</span><input type="text" placeholder="2026-11-30" value=${c.date_to || ''} onChange=${(e) => setCons('date_to', e.target.value.trim())} /></div>
        <div class="field" style="width:150px"><span>直式或橫式</span><select value=${c.orientation || ''} onChange=${(e) => setCons('orientation', e.target.value)}>
          <option value="">不限</option><option value="landscape">限橫式</option><option value="portrait">限直式</option><option value="square">限正方形</option></select></div>
        <div class="field" style="width:170px"><span>長邊至少幾個像素</span><input type="number" min="0" step="100" value=${c.min_long_edge || ''} placeholder="不限" onChange=${(e) => setCons('min_long_edge', +e.target.value || 0)} /></div>
        <div class="field" style="width:150px"><span>拍攝器材</span><select value=${c.device || ''} onChange=${(e) => setCons('device', e.target.value)}>
          <option value="">不限</option><option value="phone">限手機</option><option value="camera">限相機</option></select></div>
        <div class="field" style="width:210px"><span>畫面中的人物</span><select value=${c.people || ''} onChange=${(e) => setCons('people', e.target.value)}>
          <option value="">不限</option><option value="none">不可有清楚可見的人</option></select></div>
        <div class="field" style="width:150px"><span>每人最多投稿幾張</span><input type="number" min="0" value=${d.max_entries || ''} placeholder="5" onChange=${(e) => set({ max_entries: Math.max(0, +e.target.value || 0) })} /></div>
      </div>
      <div class="field"><span>其他規則（後製限制、授權等，會交給評審模型參考）</span>
        <textarea rows="3" value=${d.rules} onInput=${(e) => set({ rules: e.target.value })} placeholder="例如：可調整亮度與裁切，不得合成"></textarea></div>
      ${pv && html`<div class="note ${pv.after_rules ? 'local' : 'bad'}">相片庫有 ${n0(pv.library)} 張，符合這些規定的有 <b>${n0(pv.after_rules)}</b> 張${pv.after_rules ? `；去掉連拍重複和技術瑕疵後，有 ${n0(pv.eligible)} 張可以參加初選。` : '。沒有照片符合，請檢查日期或其他條件。'}
        ${(c.date_from || c.date_to) ? '沒有記錄拍攝時間的照片無法確認日期，所以不會被納入。' : ''}</div>`}
    </div>`,
    // 3 -------------------------------------------------------------- follow-ups
    () => html`<div class="wq"><h2>再確認幾件事</h2>
      <p class="muted">地端模型讀了你前面的回答，針對還不清楚、而且會影響選片的地方提出幾個問題。點選項回答，或自己寫。</p>
      ${needLocal}
      ${busy === 'follow' && html`<div class="row muted"><span class="spin"></span> 地端模型正在想問題…</div>`}
      ${(follow || []).map((q) => html`<div class="wfollow" key=${q.question}>
        <b>${q.question}</b>${q.why && html`<p class="small muted">${q.why}</p>`}
        <div class="row wrap gap-s">${q.options.map((o) => html`<span class="chip pick ${answerOf(q.question) === o ? 'on' : ''}" onClick=${() => answer(q.question, answerOf(q.question) === o ? '' : o)}>${o}</span>`)}</div>
        <input type="text" placeholder="或自己寫下答案" value=${q.options.includes(answerOf(q.question)) ? '' : answerOf(q.question)}
          onChange=${(e) => answer(q.question, e.target.value.trim())} />
      </div>`)}
      ${follow && follow.length > 0 && html`<div><button class="btn" disabled=${!!busy || !local.ok} onClick=${() => askFollow(true)}>再問我幾題</button></div>`}
      ${follow && !follow.length && !busy && local.ok && html`<p class="muted">模型沒有其他問題了。</p>`}
    </div>`,
    // 4 ---------------------------------------------------------------- photos
    () => html`<div class="wq"><h2>用你的照片來確認</h2>
      <p class="muted">文字很難說清楚「感覺」。下面是從你的相片庫挑出來的幾張，請標出哪些接近你想投的感覺。你的選擇會用來找出其他類似的照片，也會讓評分標準更貼近你的想法。點照片可以放大。</p>
      ${busy === 'photos' && !rounds.length && html`<div class="row muted"><span class="spin"></span> 正在從相片庫挑照片…</div>`}
      ${rounds.map((ids, r) => html`<div key=${r}>
        <h3>${r === 0 ? '第一輪：風格各異的候選' : '第二輪：比較難判斷的幾張'}</h3>
        <div class="wphotos">${ids.map((pid, i) => html`<div class="wphoto ${d.labels[pid] || ''}" key=${pid}>
          <img loading="lazy" src=${thumb(pid)} alt="" onClick=${() => setView({ ids, index: i })} />
          <div class="seg">${['yes', 'maybe', 'no'].map((v) => html`<button class=${d.labels[pid] === v ? 'on ' + v : ''} onClick=${() => mark(pid, v)}>${LABEL[v]}</button>`)}</div>
        </div>`)}</div></div>`)}
      ${rounds.length > 0 && html`<div class="row wrap">
        <span class="small muted">已標記：符合 ${counts.yes || 0}、普通 ${counts.maybe || 0}、不符合 ${counts.no || 0}</span>
        ${rounds.length === 1 && html`<button class="btn" disabled=${!!busy || (counts.yes || 0) < 1} onClick=${() => loadRound(2)}>
          ${busy === 'photos' ? html`<span class="spin"></span>` : ''} 看第二輪</button>`}
        <button class="btn local" disabled=${!!busy || (counts.yes || 0) < 2 || !local.ok} title=${(counts.yes || 0) < 2 ? '至少標記兩張符合的照片' : ''} onClick=${summarise}>
          ${busy === 'insight' ? html`<span class="spin"></span>` : html`<${Icon} name="chip" size=${14} />`} 請地端模型歸納我的偏好</button>
      </div>`}
      ${busy === 'insight' && html`<p class="small muted">地端模型正在比較你選的和沒選的照片，大約十幾秒到一分鐘。</p>`}
      ${ins && html`<div class="sugg">
        <span class="chip local">地端模型的觀察</span>
        <ul>${ins.observations.map((o) => html`<li>${o}</li>`)}</ul>
        <p><b>所以你想找的是：</b></p>
        <textarea rows="2" value=${d.insight} onInput=${(e) => set({ insight: e.target.value })}></textarea>
        <span class="small muted">說得不對的話可以直接改上面這段文字。</span>
        ${ins.question && html`<div class="wfollow"><b>${ins.question}</b>
          <div class="row wrap gap-s">${ins.options.map((o) => html`<span class="chip pick ${answerOf(ins.question) === o ? 'on' : ''}" onClick=${() => answer(ins.question, answerOf(ins.question) === o ? '' : o)}>${o}</span>`)}</div></div>`}
      </div>`}
    </div>`,
    // 5 --------------------------------------------------------------- confirm
    () => html`<div class="wq"><h2>確認比賽定義</h2>
      <div class="kv wide">
        <span>比賽</span><span>${d.name || '（未命名）'}</span>
        <span>主題</span><span>${d.theme || '沒有指定主題'}</span>
        ${Object.entries(d.profile).filter(([, v]) => v && v.length).map(([k, v]) => html`<span>${({ occasion: '性質', judges: '評審', strictness: '切題程度', goal: '目標', styles: '風格' })[k]}</span><span>${Array.isArray(v) ? v.join('、') : v}</span>`)}
        ${d.rules && html`<span>規則</span><span>${d.rules}</span>`}
        ${d.qa.map((x) => html`<span>問答</span><span>${x.q}　<b>${x.a}</b></span>`)}
        ${d.insight && html`<span>偏好</span><span>${d.insight}</span>`}
        ${(counts.yes || counts.no) && html`<span>範例</span><span>你標了 ${counts.yes || 0} 張符合、${counts.no || 0} 張不符合</span>`}
      </div>
      ${facets && (facets.events.length > 1 || facets.categories.length > 1) && html`<details><summary class="muted" style="cursor:pointer">只從部分相簿裡選（預設是整個相片庫）</summary>
        <div class="stack gap-s" style="margin-top:8px">
          ${chips('events', facets.events, 'id', (e) => `${e.title}（${e.count}）`)}
          ${chips('categories', facets.categories, 'key', (x) => `${x.label}（${x.count}）`)}</div></details>`}
      ${pv && sizes && html`<div><h3>接下來會這樣篩選</h3>
        <div class="funnel">
          <div class="fstep none"><div class="who"> </div><div class="n">${n0(pv.after_rules)}</div><div class="l">符合規定的照片</div></div>
          <div class="fstep local"><div class="who">地端</div><div class="n">${n0(Math.min(sizes.pool, pv.eligible))}</div><div class="l">地端模型逐張評審</div></div>
          <div class="fstep cloud"><div class="who">外部</div><div class="n">${n0(sizes.calibration)}</div><div class="l">校準地端評審</div></div>
          ${sizes.triage > 0 && html`<div class="fstep cloud"><div class="who">外部</div><div class="n">${n0(Math.min(sizes.triage, sizes.pool))}</div><div class="l">看縮圖總表初選</div></div>`}
          <div class="fstep cloud"><div class="who">外部</div><div class="n">${n0(sizes.finals)}</div><div class="l">原圖決選與評語</div></div>
          <div class="fstep cloud"><div class="who"> </div><div class="n">${n0(sizes.picks)}</div><div class="l">建議投稿</div></div>
        </div>
        <details style="margin-top:8px"><summary class="muted small" style="cursor:pointer">調整每一層的張數</summary>
          <div class="row wrap top" style="margin-top:8px">${[['pool', '地端評審'], ['calibration', '校準'], ['triage', '總表初選（0 為不做）'], ['finals', '決選'], ['picks', '投稿']].map(([k, l]) => html`
            <div class="field" style="width:150px"><span>${l}</span><input type="number" min="0" value=${sizes[k]} onInput=${(e) => setSizes({ ...sizes, [k]: Math.max(0, +e.target.value || 0) })} /></div>`)}</div></details>
      </div>`}
      <p class="small muted">按下建立後，${cloud.has_key ? `整份問答紀錄會以文字交給 ${cloud.label} 寫出評分標準（不送照片，約 3,000 到 5,000 tokens）。它會先用白話告訴你打算怎麼評，你確認後才開始。` : '因為還沒設定外部模型，會先使用內建的評分標準；之後可以在比賽頁面改請外部模型設計。'}</p>
    </div>`,
  ];

  const last = step === STEPS.length - 1;
  return html`<div class="overlay wizard">
    <div class="wrail">
      <div class="brand"><${Icon} name="trophy" size=${18} /><span>新增比賽</span></div>
      ${STEPS.map((s, i) => html`<button class="nav ${i === step ? 'on' : ''}" onClick=${() => setStep(i)}>
        <span class="ix ${i < step ? 'done' : ''}">${i < step ? html`<${Icon} name="check" size=${12} />` : i + 1}</span>${s}</button>`)}
      <span class="grow"></span>
      <button class="btn ghost" onClick=${() => { if (window.confirm('放棄這次的比賽設定？')) onClose(); }}>取消</button>
    </div>
    <div class="wmain">
      <div class="wbody">${body[step]()}</div>
      <div class="wfoot">
        <button class="btn" disabled=${step === 0} onClick=${() => setStep(step - 1)}>上一步</button>
        <span class="grow"></span>
        ${!last && html`<button class="btn ghost" onClick=${() => setStep(step + 1)}>跳過</button>
          <button class="btn primary" onClick=${() => setStep(step + 1)}>下一步</button>`}
        ${last && html`${cloud.has_key && html`<button class="btn" disabled=${!!busy} onClick=${() => create(false)}>用內建標準建立</button>`}
          <button class="btn primary" disabled=${!!busy} onClick=${() => create(!!cloud.has_key)}>
            ${busy === 'create' ? html`<span class="spin"></span>` : ''} ${cloud.has_key ? `建立，並請 ${cloud.label} 寫評分標準` : '建立比賽'}</button>`}
      </div>
    </div>
    ${view && html`<${PhotoViewer} ids=${view.ids} index=${view.index} onClose=${() => setView(null)} />`}
  </div>`;
}
