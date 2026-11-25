import { html, useState, useEffect, useCallback, api, fail, toast, useApp, Icon, Dialog, Bars, usePickFolder, n0, thumb } from './ui.js';
import { PhotoViewer } from './library.js';

const BLANK = { name: '', theme: '', rules: '', notes: '', scope: {}, sizes: { pool: 150, calibration: 16, finals: 24, picks: 5 }, exclude_flawed: true, max_per_event: 0 };

export function Contests({ id }) {
  const { go, jobTick } = useApp();
  const [list, setList] = useState(null);
  const [form, setForm] = useState(null);
  const load = () => api('/contests').then(setList).catch(fail);
  useEffect(() => { load(); }, [jobTick, id]);
  if (!list) return null;
  const cur = id || (list[0] && list[0].id);
  return html`<div class="page">
    <div class="page-head"><h1>比賽選片</h1>
      <span class="sub">地端模型負責大量初選，外部模型只在需要判斷力的地方出手。</span>
      <span class="grow"></span>
      <button class="btn primary" onClick=${() => setForm(BLANK)}>新增比賽</button></div>
    ${!list.length
      ? html`<div class="empty"><h2>還沒有比賽</h2>
          <p>告訴 PhotoSift 比賽的主題和規則，它會從你的相片庫裡一層一層篩出最適合投稿的照片，並說明每一張入選的理由。</p>
          <button class="btn primary" onClick=${() => setForm(BLANK)}>新增比賽</button></div>`
      : html`<div class="split">
          <div class="clist">${list.map((c) => html`<div class="it ${c.id === cur ? 'on' : ''}" onClick=${() => go('contest', c.id)}>
            <div class="ellipsis"><b>${c.name}</b></div>
            <div class="small muted">${c.done ? '已完成決選' : c.has_rubric ? '進行中' : '尚未開始'}</div></div>`)}</div>
          ${cur && html`<${Contest} key=${cur} id=${cur} onEdit=${setForm} onDeleted=${() => { go('contest'); load(); }} />`}
        </div>`}
    ${form && html`<${ContestForm} initial=${form} onClose=${() => setForm(null)}
      onSaved=${(c) => { setForm(null); load(); go('contest', c.id); }} />`}
  </div>`;
}

function ContestForm({ initial, onClose, onSaved }) {
  const [f, setF] = useState(JSON.parse(JSON.stringify(initial)));
  const [facets, setFacets] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api('/facets').then(setFacets).catch(fail); }, []);
  const set = (k, v) => setF({ ...f, [k]: v });
  const size = (k, v) => setF({ ...f, sizes: { ...f.sizes, [k]: Math.max(1, +v || 1) } });
  const toggle = (kind, val) => {
    const cur = new Set(f.scope[kind] || []);
    cur.has(val) ? cur.delete(val) : cur.add(val);
    setF({ ...f, scope: { ...f.scope, [kind]: [...cur] } });
  };
  const save = async () => {
    if (!f.name.trim()) { toast('請輸入比賽名稱。', 'err'); return; }
    setBusy(true);
    try {
      const c = f.id ? await api('/contests/' + f.id, { method: 'PUT', body: f }) : await api('/contests', { body: f });
      onSaved(c);
    } catch (e) { fail(e); setBusy(false); }
  };
  const chips = (kind, items, idKey, label) => html`<div class="row wrap gap-s">${items.map((it) => html`
    <span class="chip pick ${(f.scope[kind] || []).includes(it[idKey]) ? 'on' : ''}" onClick=${() => toggle(kind, it[idKey])}>${label(it)}</span>`)}</div>`;
  return html`<${Dialog} wide title=${f.id ? '編輯比賽' : '新增比賽'} onClose=${onClose}
    footer=${html`<button class="btn" onClick=${onClose}>取消</button><button class="btn primary" disabled=${busy} onClick=${save}>${f.id ? '儲存' : '建立'}</button>`}>
    <div class="field"><span>比賽名稱</span><input type="text" value=${f.name} onInput=${(e) => set('name', e.target.value)} placeholder="公司年度攝影比賽" /></div>
    <div class="field"><span>主題</span><input type="text" value=${f.theme} onInput=${(e) => set('theme', e.target.value)} placeholder="例如：旅途中的寧靜時刻。沒有指定主題可以留空" /></div>
    <div class="field"><span>規則（張數限制、是否允許後製等）</span><textarea rows="2" value=${f.rules} onInput=${(e) => set('rules', e.target.value)}></textarea></div>
    <div class="field"><span>主辦單位或評審重視什麼（知道的話寫下來，評分標準會更貼近）</span><textarea rows="2" value=${f.notes} onInput=${(e) => set('notes', e.target.value)} placeholder="例如：重視意境與故事性，勝過純粹的技術"></textarea></div>
    ${facets && html`<div class="stack gap-s"><span class="small muted">參賽範圍（都不選代表整個相片庫）</span>
      ${facets.events.length > 0 && html`<div><div class="small faint" style="margin-bottom:4px">事件</div>${chips('events', facets.events, 'id', (e) => `${e.title}（${e.count}）`)}</div>`}
      ${facets.categories.length > 0 && html`<div><div class="small faint" style="margin-bottom:4px">主題</div>${chips('categories', facets.categories, 'key', (c) => `${c.label}（${c.count}）`)}</div>`}
    </div>`}
    <div class="row wrap top gap-l">
      <div class="field" style="width:130px"><span>地端初選張數</span><input type="number" min="1" value=${f.sizes.pool} onInput=${(e) => size('pool', e.target.value)} /></div>
      <div class="field" style="width:130px"><span>外部校準張數</span><input type="number" min="4" value=${f.sizes.calibration} onInput=${(e) => size('calibration', e.target.value)} /></div>
      <div class="field" style="width:130px"><span>外部決選張數</span><input type="number" min="1" value=${f.sizes.finals} onInput=${(e) => size('finals', e.target.value)} /></div>
      <div class="field" style="width:130px"><span>要投稿幾張</span><input type="number" min="1" value=${f.sizes.picks} onInput=${(e) => size('picks', e.target.value)} /></div>
      <div class="field" style="width:150px"><span>每個事件最多入圍</span><input type="number" min="0" value=${f.max_per_event} onInput=${(e) => set('max_per_event', Math.max(0, +e.target.value || 0))} /><span class="faint">0 代表不限</span></div>
    </div>
    <label class="check"><input type="checkbox" checked=${f.exclude_flawed} onChange=${(e) => set('exclude_flawed', e.target.checked)} />排除有技術瑕疵的照片（你親自挑選的照片不受影響）</label>
    <p class="small muted">只有「外部校準」和「外部決選」會把照片送到外部模型，張數就是上面設定的數字；其他步驟都在這台電腦上完成。</p>
  <//>`;
}

function Funnel({ c }) {
  const u = c.usage;
  const steps = [
    ['none', '範圍內的照片', c.scope_photos, '', true],
    ['local', '去重複與瑕疵後的候選', c.pool, '地端', true],
    ['local', '地端模型評審', c.scored_local, '地端', c.scored_local > 0],
    ['cloud', '外部模型校準', c.scored_teacher, '外部', c.scored_teacher > 0],
    ['cloud', '外部模型決選', c.scored_final, '外部', c.scored_final > 0],
    ['cloud', '建議投稿', c.summary ? c.summary.picks.length : 0, '', !!c.summary],
  ];
  return html`<div>
    <div class="funnel">${steps.map(([kind, label, n, who, done]) => html`<div class="fstep ${kind} ${done ? '' : 'todo'}">
      <div class="who">${who || ' '}</div><div class="n">${done ? n0(n) : '—'}</div><div class="l">${label}</div></div>`)}</div>
    ${u.total_tokens > 0 && html`<p class="small muted" style="margin-top:8px">
      外部模型到目前用了 <b class="num" style="color:var(--text)">${n0(u.total_tokens)}</b> tokens、看了 ${u.images_sent} 張次照片。
      ${u.saved_pct > 0 ? html`如果把範圍內 ${n0(u.scope_photos)} 張都送給外部模型看一次，大約要 ${n0(u.baseline_tokens)} tokens，這樣做省了約 <b style="color:var(--local)">${u.saved_pct}%</b>。` : ''}</p>`}
  </div>`;
}

function Step({ ix, kind, done, title, hint, action, children }) {
  return html`<section class="step ${kind} ${done ? 'done' : ''}">
    <header><span class="ix">${done ? html`<${Icon} name="check" size=${14} />` : ix}</span>
      <div class="grow"><h2>${title}　<span class="chip ${kind === 'cloud' ? 'cloud' : 'local'}">${kind === 'cloud' ? '外部模型' : '地端模型'}</span></h2>
        <div class="sub">${hint}</div></div>
      ${action}</header>
    ${children && html`<div class="content">${children}</div>`}
  </section>`;
}

function Contest({ id, onEdit, onDeleted }) {
  const { status, jobTick, running, startJob, go } = useApp();
  const [c, setC] = useState(null);
  const [results, setResults] = useState([]);
  const [busy, setBusy] = useState('');
  const [confirm, setConfirm] = useState(null);
  const [view, setView] = useState(null);
  const [showAll, setShowAll] = useState(false);
  const [pickFolder, folderDlg] = usePickFolder();

  const load = useCallback(async () => {
    try {
      const [cc, rr] = await Promise.all([api('/contests/' + id), api(`/contests/${id}/results`)]);
      setC(cc); setResults(rr);
    } catch (e) { fail(e); }
  }, [id]);
  useEffect(() => { load(); }, [id, jobTick]);
  if (!c) return html`<div></div>`;

  const cloud = status ? status.cloud : {};
  const local = status ? status.local : {};
  const rubric = c.rubric;
  const cal = c.calibration;
  const names = rubric ? Object.fromEntries(rubric.criteria.map((x) => [x.key, x.name])) : {};
  // The models refer to criteria by key (c1, c2 ...); show people the names instead.
  const named = (t) => String(t || '').replace(/\bc(\d)\b/g, (m) => (names[m] ? `「${names[m]}」` : m));

  const coach = async (use_cloud) => {
    if (c.scored_local && !window.confirm('重新產生評分標準會清除這場比賽目前所有的評分，確定嗎？')) return;
    setBusy('coach');
    try { setC(await api(`/contests/${id}/coach`, { body: { use_cloud } })); setResults([]); } catch (e) { fail(e); }
    setBusy('');
  };
  const ask = async (step) => {
    try { setConfirm({ step, ...(await api(`/contests/${id}/outgoing/${step}`)) }); } catch (e) { fail(e); }
  };
  const exportOut = async () => {
    const dest = await pickFolder('匯出入圍照片到…', '匯出到這裡');
    if (!dest) return;
    try {
      const r = await api(`/contests/${id}/export`, { body: { dest } });
      toast(`已複製 ${r.copied} 張${r.edited ? `、${r.edited} 張修圖後的版本` : ''}與評審報告到 ${r.dest}（原始檔沒有變動）`);
    } catch (e) { fail(e); }
  };
  const remove = async () => {
    if (!window.confirm(`刪除比賽「${c.name}」和它的所有評分？照片不受影響。`)) return;
    try { await api('/contests/' + id, { method: 'DELETE' }); onDeleted(); } catch (e) { fail(e); }
  };

  const needKey = !cloud.has_key && html`<div class="note">${cloud.via === 'cli' ? `找不到 ${cloud.label} 的命令列工具` : `還沒有設定 ${cloud.label} 的 API key`}，外部模型的步驟無法執行。
    <a onClick=${() => go('settings')}>前往設定</a>。不設定也可以只用地端模型完成初選。</div>`;
  const shown = showAll ? results : results.slice(0, Math.max(c.scored_final || 0, 12));
  const picks = new Set(c.summary ? c.summary.picks : []);
  const why = Object.fromEntries((c.summary ? c.summary.ranking : []).map((x) => [x.id, x.why]));
  const rounds = !cal ? [] : cal.rounds && cal.rounds.length ? cal.rounds
    : [{ label: '第一輪', agreement: cal.agreement_before, mae: cal.mae ? cal.mae.identity : '—' }];
  const lastRound = rounds[rounds.length - 1];

  return html`<div>
    <div class="row" style="margin-bottom:12px">
      <div class="grow"><h1>${c.name}</h1>
        <div class="sub">${c.brief.theme ? `主題：${c.brief.theme}` : '未指定主題'}${c.brief.rules ? `　規則：${c.brief.rules}` : ''}</div></div>
      <button class="btn sm" onClick=${() => onEdit({ id: c.id, name: c.name, ...c.brief })}><${Icon} name="edit" size=${13} /> 編輯</button>
      <button class="btn sm danger" onClick=${remove}><${Icon} name="trash" size=${13} /></button>
    </div>
    <${Funnel} c=${c} />

    <${Step} ix="1" kind="cloud" done=${!!rubric} title="評分標準"
      hint="外部模型依這場比賽的主題寫一份評分標準，並把它改寫成地端小模型看得懂的評審須知。只送文字，不送照片。"
      action=${html`<div class="row">
        ${rubric && html`<span class="small muted">${rubric.source === 'builtin' ? '內建標準' : rubric.source === 'edited' ? '已手動修改' : rubric.source}</span>`}
        <button class="btn ${rubric ? '' : 'primary'}" disabled=${busy || running || !cloud.has_key} onClick=${() => coach(true)}>
          ${busy === 'coach' ? html`<span class="spin"></span>` : html`<${Icon} name="cloud" size=${14} />`} ${rubric ? '重新產生' : `請 ${cloud.label || '外部模型'} 設計`}</button>
        ${!rubric && html`<button class="btn" disabled=${busy || running} onClick=${() => coach(false)}>用內建標準</button>`}</div>`}>
      ${!rubric && needKey}
      ${rubric && html`
        <div class="rubric">${rubric.criteria.map((x) => html`<div><span class="w">${x.weight}%</span> <b>${x.name}</b></div>
          <div><div>${x.description}</div><div class="small muted">高分：${x.high}</div><div class="small muted">低分：${x.low}</div></div>`)}</div>
        <div class="note"><b>給地端模型的評審須知</b><br />${rubric.judge_brief}</div>
        ${rubric.ideal && html`<p class="small muted">理想的得獎作品：${rubric.ideal}</p>`}`}
    <//>

    <${Step} ix="2" kind="local" done=${c.scored_local > 0 && c.scored_local >= c.pool} title="地端評審"
      hint=${`地端模型照著評分標準，把候選的 ${n0(c.pool)} 張逐張評過。在這台電腦上執行，不花 token，可以中途取消再接著跑。`}
      action=${html`<button class="btn ${c.scored_local >= c.pool ? '' : 'local'}" disabled=${!rubric || running || !local.ok}
        title=${!local.ok ? local.error : ''} onClick=${() => startJob(`/contests/${id}/local`).then(load)}>
        <${Icon} name="chip" size=${14} /> ${c.scored_local ? (c.scored_local >= c.pool ? '已完成' : `繼續（${c.scored_local}/${c.pool}）`) : '開始'}</button>`}>
      ${rubric && !local.ok && html`<div class="note bad">${local.error}　<a onClick=${() => go('settings')}>前往設定</a></div>`}
      ${c.scored_local > 0 && html`<p class="small muted">已評 ${c.scored_local} 張，使用 ${local.judge || local.model}。下方的名次目前${c.scored_final ? '以外部決選為準' : cal ? '已依外部評審校準' : '依地端評審排列'}。</p>`}
    <//>

    <${Step} ix="3" kind="cloud" done=${!!cal} title="校準"
      hint=${`外部模型用同一份標準評 ${c.brief.sizes.calibration} 張樣本。比較兩邊的分數，就知道地端評審準不準，並由外部模型告訴它哪裡看錯了。`}
      action=${html`<button class="btn ${cal ? '' : 'primary'}" disabled=${running || c.scored_local < 4 || !cloud.has_key} onClick=${() => ask('calibrate')}>
        <${Icon} name="cloud" size=${14} /> ${cal ? '重新校準' : '選出樣本'}</button>`}>
      ${cal && html`
        <div class="row wrap gap-l">
          <div class="agree"><span class="big" style=${`color:${agreeColor(lastRound.agreement)}`}>${lastRound.agreement ?? '—'}</span>
            <div><b>${agreeText(lastRound.agreement)}</b><div class="small muted">排序一致度（1 代表兩邊排名完全相同），樣本 ${cal.n} 張，平均相差 ${lastRound.mae} 分</div></div></div>
        </div>
        ${rounds.length > 1 && html`<p class="small muted">歷次：${rounds.map((r) => `${r.label} ${r.agreement}`).join('　→　')}</p>`}
        ${cal.observations && cal.observations.length > 0 && html`<div><h3>${cloud.label}觀察到的差異</h3>
          <ul style="margin:6px 0 0;padding-left:20px">${cal.observations.map((o) => html`<li>${named(o)}</li>`)}</ul></div>`}
        ${cal.notes && html`<div class="note"><b>外部模型給地端模型的修正提醒</b><br />${named(cal.notes)}</div>
          <div class="row"><button class="btn local" disabled=${running || !local.ok} onClick=${() => startJob(`/contests/${id}/local?rescore=true`).then(load)}>
            <${Icon} name="chip" size=${14} /> 依提醒重新評分</button>
            <span class="small muted">地端模型帶著這份提醒把候選重評一次，再和外部評審已有的分數比對，可以看到一致度有沒有提高。不花 token。</span></div>`}`}
      ${!cal && c.scored_local >= 4 && needKey}
    <//>

    <${Step} ix="4" kind="cloud" done=${!!c.summary} title="決選"
      hint=${`只有排名最前面的 ${c.brief.sizes.finals} 張會送給外部模型，逐張寫評語，再把最好的幾張放在一起比較、排出名次。`}
      action=${html`<button class="btn ${c.summary ? '' : 'primary'}" disabled=${running || !c.scored_local || !cloud.has_key} onClick=${() => ask('finals')}>
        <${Icon} name="cloud" size=${14} /> ${c.summary ? '重新決選' : '選出入圍'}</button>`}>
      ${c.summary && c.summary.overall && html`<div class="note"><b>總評與投稿建議</b><br />${c.summary.overall}</div>`}
      ${c.summary && html`<div class="row"><button class="btn" onClick=${exportOut}><${Icon} name="out" size=${14} /> 匯出入圍照片與評審報告</button>
        <span class="small muted">複製到你指定的資料夾，檔名依名次編號。修過圖的照片會多匯出一份修圖後的版本。原始檔不會變動。</span></div>`}
    <//>

    ${results.length > 0 && html`<div class="card" style="margin-top:14px">
      <div class="row"><h2 class="grow">${c.summary ? '名次' : '目前的排名'}</h2>
        <span class="small muted">${n0(results.length)} 張已評</span></div>
      ${c.summary && c.summary.ranking.length > 0 && html`<p class="small muted">前 ${c.summary.ranking.length} 名的順序來自並排比較，所以不一定和各自的分數高低相同；其餘依個別分數排列。</p>`}
      ${shown.map((r, i) => html`<${Result} key=${r.photo_id} r=${r} i=${i} names=${names} pick=${picks.has(r.photo_id)} why=${why[r.photo_id]}
        onOpen=${() => setView({ ids: shown.map((x) => x.photo_id), index: i })}
        onEdit=${() => setView({ ids: shown.map((x) => x.photo_id), index: i, mode: 'edit' })} />`)}
      ${results.length > shown.length && html`<div style="text-align:center;padding-top:12px"><button class="btn" onClick=${() => setShowAll(true)}>顯示全部 ${results.length} 張</button></div>`}
    </div>`}

    ${confirm && html`<${ConfirmUpload} c=${confirm} onClose=${() => setConfirm(null)}
      onGo=${() => { const step = confirm.step; setConfirm(null); startJob(`/contests/${id}/${step}`).then(load); }} />`}
    ${view && html`<${PhotoViewer} ids=${view.ids} index=${view.index} startMode=${view.mode || 'info'} contestId=${id}
      onClose=${() => { setView(null); load(); }} />`}
    ${folderDlg}
  </div>`;
}

const agreeColor = (v) => (v == null ? 'var(--muted)' : v >= 0.7 ? 'var(--good)' : v >= 0.4 ? 'var(--accent)' : 'var(--bad)');
const agreeText = (v) => (v == null ? '樣本太少，無法判斷'
  : v >= 0.8 ? '地端評審和外部評審的眼光很接近'
  : v >= 0.6 ? '大方向一致，細節有出入'
  : v >= 0.4 ? '只有部分一致，建議依提醒重新評分'
  : '兩邊看法差很多，建議換一個地端評審模型，或加大決選張數');

function Result({ r, i, names, pick, why, onOpen, onEdit }) {
  const f = r.final;
  const scores = (f || r.teacher || r.local || {}).scores || {};
  const kind = f || r.teacher ? '' : 'local';
  return html`<div class="result ${pick ? 'pick' : ''}">
    <div class="rank">${r.final_rank || i + 1}${pick ? html`<div class="chip cloud" style="margin-top:6px">投稿</div>` : ''}</div>
    <img loading="lazy" src=${thumb(r.photo_id)} alt="" onClick=${onOpen} />
    <div class="stack gap-s">
      <div class="row wrap"><span class="ttl">${f && f.title ? f.title : r.filename}</span>${f && f.title ? html`<span class="small faint">${r.filename}</span>` : ''}</div>
      <div class="scoreline">
        ${r.final_total != null && html`<span><span class="chip cloud">外部決選</span> <b>${Math.round(r.final_total)}</b></span>`}
        ${r.teacher_total != null && html`<span><span class="chip cloud">外部校準</span> <b>${Math.round(r.teacher_total)}</b></span>`}
        <span><span class="chip local">地端評審</span> <b>${Math.round(r.local_total)}</b></span>
      </div>
      ${why && html`<p>${why}</p>`}
      ${f && html`<p class="small"><b>優點</b>　${f.strengths}</p><p class="small muted"><b>可能扣分</b>　${f.weaknesses}</p>
        ${f.edit && f.edit !== '不需調整' ? html`<p class="small muted"><b>後製方向</b>　${f.edit}</p>` : ''}`}
      <div class="row gap-s"><button class="btn sm" onClick=${onEdit}><${Icon} name="edit" size=${13} /> ${r.edited ? '繼續後製' : '後製'}</button>
        ${r.edited ? html`<span class="chip local">已修圖，匯出時會多一份修圖後的版本</span>` : f ? html`<span class="small faint">外部評審的調整建議已經準備好，開啟後按「套用」</span>` : ''}</div>
      ${!f && r.teacher && html`<p class="small">${r.teacher.comment}</p>`}
      ${!f && r.local && html`<p class="small muted">${r.local.reason}</p>`}
      <div style="max-width:420px"><${Bars} kind=${kind} rows=${Object.entries(scores).map(([k, v]) => [names[k] || k, v])} /></div>
    </div>
  </div>`;
}

function ConfirmUpload({ c, onClose, onGo }) {
  const what = c.step === 'calibrate' ? '校準' : '決選';
  return html`<${Dialog} wide title=${`送出 ${c.photo_ids.length} 張照片給 ${c.provider} ${what}`}
    sub=${`模型：${c.model || '（尚未選擇）'}`} onClose=${onClose}
    footer=${html`<span class="small muted grow">預估約 ${n0(c.est_tokens)} tokens${c.via === 'cli' ? '，透過命令列工具送出，計入訂閱用量（工具本身還會多用一些）' : ''}</span>
      <button class="btn" onClick=${onClose}>取消</button>
      <button class="btn primary" disabled=${!c.photo_ids.length} onClick=${onGo}><${Icon} name="cloud" size=${14} /> 送出這 ${c.photo_ids.length} 張</button>`}>
    <p class="muted">只有下面這些照片會離開這台電腦。上傳的是縮小到長邊 ${c.image_px}px 的複本，已移除拍攝時間、相機與 GPS 等資訊；原始檔不會上傳。</p>
    <div class="thumbs">${c.photo_ids.map((id) => html`<img loading="lazy" src=${thumb(id)} alt="" />`)}</div>
    ${c.step === 'finals' && html`<p class="small muted">逐張評語之後，分數最高的幾張會再一起送出一次做比較排名，所以實際的張次會比入圍張數多一些。</p>`}
  <//>`;
}
