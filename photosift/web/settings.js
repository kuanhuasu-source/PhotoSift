import { html, useState, useEffect, api, fail, toast, useApp, Icon, usePickFolder, n0 } from './ui.js';

export function Settings() {
  const { status, refresh, startJob, running, jobTick } = useApp();
  const [data, setData] = useState(null);
  const [folders, setFolders] = useState([]);
  const [local, setLocal] = useState(null);
  const [usage, setUsage] = useState(null);
  const [pickFolder, folderDlg] = usePickFolder();

  const load = () => {
    api('/settings').then(setData).catch(fail);
    api('/folders').then(setFolders).catch(fail);
    api('/usage').then(setUsage).catch(() => {});
  };
  useEffect(() => { load(); }, [jobTick]);
  useEffect(() => { api('/models/local').then(setLocal).catch(() => {}); }, []);
  if (!data) return null;
  const s = data.settings;

  const save = async (next, quiet) => {
    try {
      const r = await api('/settings', { method: 'PUT', body: next });
      setData({ ...data, settings: next });
      refresh();
      if (!quiet) toast('已儲存。');
      return r;
    } catch (e) { fail(e); }
  };
  const upd = (section, patch, quiet = true) => save({ ...s, [section]: { ...s[section], ...patch } }, quiet);

  const addFolder = async () => {
    const path = await pickFolder('加入相片資料夾', '加入這個資料夾');
    if (!path) return;
    try { await api('/folders', { body: { path } }); load(); refresh(); toast('已加入。按「開始分析」來處理裡面的照片。'); } catch (e) { fail(e); }
  };
  const removeFolder = async (f) => {
    if (!window.confirm(`從 PhotoSift 移除「${f.path}」？\n只會清掉這個資料夾的分析結果，磁碟上的照片不會被刪除。`)) return;
    try { await api('/folders/' + f.id, { method: 'DELETE' }); load(); refresh(); } catch (e) { fail(e); }
  };

  return html`<div class="page" style="max-width:980px">
    <div class="page-head"><h1>設定</h1></div>
    <div class="stack gap-l">

      <div class="card stack">
        <div class="row"><div class="grow"><h2>相片資料夾</h2>
          <div class="sub">可以加入多個資料夾，子資料夾會一併掃描。PhotoSift 只讀取，不會搬動、修改或刪除你的檔案。</div></div>
          <button class="btn" onClick=${addFolder}><${Icon} name="folder" size=${14} /> 加入資料夾</button>
          <button class="btn primary" disabled=${running || !folders.length} onClick=${() => startJob('/analyze')}><${Icon} name="play" size=${14} /> 開始分析</button></div>
        ${folders.length > 0 && html`<table><tbody>${folders.map((f) => html`<tr>
          <td><span class="mono">${f.path}</span>${!f.exists ? html` <span class="chip bad">找不到</span>` : ''}</td>
          <td class="num muted" style="white-space:nowrap">${n0(f.photos)} 張</td>
          <td style="width:40px"><button class="btn ghost sm danger" title="移除" onClick=${() => removeFolder(f)}><${Icon} name="trash" size=${14} /></button></td></tr>`)}</tbody></table>`}
        <p class="small muted">支援 JPEG、HEIC、PNG、TIFF 與常見的 RAW 格式。同名的 RAW 和 JPEG 會視為同一張。</p>
      </div>

      <${LocalCard} s=${s.local} local=${local} upd=${upd} reload=${() => api('/models/local').then(setLocal).catch(fail)} />
      <${CloudCard} s=${s.cloud} keys=${data.keys} providers=${data.providers} upd=${upd} reload=${load} />

      <div class="card stack">
        <div><h2>分組與瑕疵判定</h2><div class="sub">調整後按「套用」會重新分組，不需要重新分析照片。</div></div>
        <${AnalysisForm} a=${s.analysis} disabled=${running} onApply=${async (a) => {
          const r = await save({ ...s, analysis: a }, true);
          if (r && status.analysed) startJob('/regroup'); else if (r) toast('已儲存。');
        }} />
      </div>

      <div class="card stack">
        <div><h2>外部模型用量</h2><div class="sub">每一次呼叫外部模型都有紀錄。</div></div>
        ${usage && usage.models.length
          ? html`<table><thead><tr><th>模型</th><th>呼叫次數</th><th>照片張次</th><th>輸入 tokens</th><th>輸出 tokens</th>${usage.models[0].cost != null ? html`<th>估計費用</th>` : ''}</tr></thead>
              <tbody>${usage.models.map((m) => html`<tr><td>${m.model}</td><td class="num">${m.calls}</td><td class="num">${m.images}</td>
                <td class="num">${n0(m.i)}</td><td class="num">${n0(m.o)}</td>${m.cost != null ? html`<td class="num">US$ ${m.cost}</td>` : ''}</tr>`)}</tbody></table>`
          : html`<p class="muted">還沒有呼叫過外部模型。</p>`}
        <div class="row wrap">
          <div class="field" style="width:190px"><span>輸入單價（美元 / 百萬 tokens）</span><input type="number" step="0.01" min="0" value=${s.cloud.price_in} onChange=${(e) => upd('cloud', { price_in: +e.target.value || 0 }).then(load)} /></div>
          <div class="field" style="width:190px"><span>輸出單價（美元 / 百萬 tokens）</span><input type="number" step="0.01" min="0" value=${s.cloud.price_out} onChange=${(e) => upd('cloud', { price_out: +e.target.value || 0 }).then(load)} /></div>
          <span class="small muted" style="align-self:flex-end;padding-bottom:7px">填入你所用模型的定價，才會顯示估計費用。</span></div>
      </div>

      <p class="small faint">分析結果與縮圖存放在：<span class="mono">${status ? status.home : ''}</span></p>
    </div>
    ${folderDlg}
  </div>`;
}

function LocalCard({ s, local, upd, reload }) {
  const vision = local ? local.models.filter((m) => m.vision) : [];
  const rec = local ? local.recommended : null;
  const installed = (name) => local && local.models.some((m) => m.name === name);
  const opt = (m) => html`<option value=${m.name}>${m.name}${m.size_gb ? `（${m.size_gb} GB）` : ''}</option>`;
  return html`<div class="card stack">
    <div class="row"><div class="grow"><h2>地端模型 <span class="chip local">在這台電腦上執行</span></h2>
      <div class="sub">負責為照片寫描述、打標籤，以及比賽的初選評審。照片不會離開電腦，也不花 token。</div></div>
      <button class="btn sm" onClick=${reload}>重新偵測</button></div>
    ${local && html`<div class="note ${local.ok ? 'local' : 'bad'}">
      ${local.ok ? html`已連線，找到 ${vision.length} 個可以看圖的模型。目前標註用 <b>${local.model}</b>，評審用 <b>${local.judge}</b>。` : local.error}
      ${local.ok && !local.judge_ok ? html`<br />評審模型 ${local.judge} 還沒有安裝。` : ''}</div>`}
    <div class="row wrap top">
      <div class="field" style="width:260px"><span>標註模型（處理大量照片，速度優先）</span>
        <select value=${s.model} onChange=${(e) => upd('local', { model: e.target.value }).then(reload)}>
          <option value="">自動選擇</option>${vision.map(opt)}
          ${s.model && !installed(s.model) ? html`<option value=${s.model}>${s.model}（未安裝）</option>` : ''}</select></div>
      <div class="field" style="width:260px"><span>評審模型（比賽初選，判斷力優先）</span>
        <select value=${s.judge_model} onChange=${(e) => upd('local', { judge_model: e.target.value }).then(reload)}>
          <option value="">與標註模型相同</option>${vision.map(opt)}
          ${s.judge_model && !installed(s.judge_model) ? html`<option value=${s.judge_model}>${s.judge_model}（未安裝）</option>` : ''}</select></div>
    </div>
    ${rec && html`<div class="small muted">這台電腦有 ${local.ram_gb} GB 記憶體，建議標註用 <code>${rec.annotator}</code>${installed(rec.annotator) ? '（已安裝）' : ''}，
      評審用 <code>${rec.judge}</code>${installed(rec.judge) ? '（已安裝）' : ''}。
      ${!installed(rec.annotator) || !installed(rec.judge) ? html`在終端機執行下面的指令下載，完成後按「重新偵測」：
        <div class="mono" style="margin-top:6px;line-height:1.9">${[...new Set([rec.annotator, rec.judge])].filter((m) => !installed(m)).map((m) => html`<div><code>ollama pull ${m}</code></div>`)}</div>` : ''}</div>`}
    <details><summary class="muted" style="cursor:pointer">進階</summary>
      <div class="row wrap top" style="margin-top:10px">
        <div class="field" style="width:150px"><span>服務類型</span>
          <select value=${s.api} onChange=${(e) => upd('local', { api: e.target.value, base_url: e.target.value === 'openai' ? 'http://localhost:1234' : 'http://localhost:11434' }).then(reload)}>
            <option value="ollama">Ollama</option><option value="openai">LM Studio 或其他 OpenAI 相容服務</option></select></div>
        <div class="field" style="width:260px"><span>服務位址</span>
          <input type="text" class="mono" value=${s.base_url} onChange=${(e) => upd('local', { base_url: e.target.value.trim() }).then(reload)} /></div>
        <div class="field" style="width:170px"><span>送給模型的圖片長邊（px）</span>
          <input type="number" min="336" max="2048" step="16" value=${s.image_px} onChange=${(e) => upd('local', { image_px: Math.max(336, +e.target.value || 896) })} /></div>
      </div></details>
  </div>`;
}

function CloudCard({ s, keys, providers, upd, reload }) {
  const [key, setKey] = useState('');
  const [models, setModels] = useState({});
  const [busy, setBusy] = useState(false);
  const p = s.provider;
  const src = keys[p];
  const srcText = { env: '來自環境變數', keychain: '存在系統鑰匙圈', file: '存在本機設定檔' };
  const saveKey = async (value) => {
    try {
      await api('/keys', { body: { provider: p, key: value } });
      setKey(''); reload(); toast(value ? 'API key 已儲存。' : 'API key 已移除。');
    } catch (e) { fail(e); }
  };
  const list = async () => {
    setBusy(true);
    try { const d = await api('/models/cloud/' + p); setModels({ ...models, [p]: d.models }); toast(`找到 ${d.models.length} 個可用模型。`); } catch (e) { fail(e); }
    setBusy(false);
  };
  const setModel = (v) => upd('cloud', { models: { ...s.models, [p]: v } });
  return html`<div class="card stack">
    <div><h2>外部模型 <span class="chip cloud">需要 API key，會花 token</span></h2>
      <div class="sub">只在比賽選片時使用：設計評分標準、校準地端評審、為入圍照片寫評語並排名。每次送出照片之前都會先讓你看過。</div></div>
    <div class="row wrap gap-s">${Object.entries(providers).map(([k, label]) => html`
      <span class="chip pick ${p === k ? 'on' : ''}" onClick=${() => upd('cloud', { provider: k })}>${label}${keys[k] ? ' ✓' : ''}</span>`)}</div>
    <div class="row wrap top">
      <div class="field" style="width:300px"><span>${providers[p]} 模型</span>
        <input type="text" class="mono" list="cloud-models" value=${s.models[p] || ''} placeholder="按右邊的按鈕列出可用模型"
          onChange=${(e) => setModel(e.target.value.trim())} />
        <datalist id="cloud-models">${(models[p] || []).map((m) => html`<option value=${m} />`)}</datalist></div>
      <button class="btn" style="margin-top:23px" disabled=${busy} onClick=${list}>${busy ? html`<span class="spin"></span>` : ''} 列出可用模型</button>
      <div class="field" style="width:180px"><span>上傳的圖片長邊（px）</span>
        <select value=${s.image_px} onChange=${(e) => upd('cloud', { image_px: +e.target.value })}>
          <option value="768">768（最省）</option><option value="1024">1024（建議）</option><option value="1568">1568（最細緻）</option></select></div>
    </div>
    <div class="row wrap top">
      <div class="field" style="width:420px"><span>${providers[p]} API key　${src ? html`<span class="chip local">已設定，${srcText[src]}</span>` : html`<span class="chip bad">尚未設定</span>`}</span>
        <input type="password" autocomplete="off" value=${key} onInput=${(e) => setKey(e.target.value)} placeholder=${src ? '輸入新的 key 可以取代目前的' : '貼上 API key'} /></div>
      <button class="btn primary" style="margin-top:23px" disabled=${!key.trim()} onClick=${() => saveKey(key)}>儲存</button>
      ${src && src !== 'env' && html`<button class="btn danger" style="margin-top:23px" onClick=${() => saveKey('')}>移除</button>`}
    </div>
    <p class="small muted">API key 會存進系統鑰匙圈（系統沒有鑰匙圈時，存成只有你能讀取的本機檔案），不會寫進程式的資料夾。${p === 'claude' ? '已經用 ant auth login 登入的話，可以不用填。' : ''}${p === 'openai' && !s.models.openai ? 'GPT 的模型名稱更新得很快，請先儲存 key 再按「列出可用模型」挑選。' : ''}</p>
  </div>`;
}

function AnalysisForm({ a, disabled, onApply }) {
  const [f, setF] = useState(a);
  useEffect(() => setF(a), [a]);
  const num = (k, label, hint, step, min, max) => html`<div class="field" style="width:200px"><span>${label}</span>
    <input type="number" step=${step} min=${min} max=${max} value=${f[k]} onInput=${(e) => setF({ ...f, [k]: +e.target.value })} />
    <span class="faint">${hint}</span></div>`;
  const dirty = JSON.stringify(f) !== JSON.stringify(a);
  return html`<div class="stack">
    <div class="row wrap top">
      ${num('event_gap_hours', '事件間隔（小時）', '超過這麼久沒拍照就算新的事件', 0.5, 0.5, 72)}
      ${num('burst_window_s', '連拍時間範圍（秒）', '這段時間內相似的照片歸成一組', 10, 5, 3600)}
      ${num('burst_sim', '連拍相似度門檻', '0.8～0.98，越高越嚴格', 0.01, 0.8, 0.99)}
      ${num('blur_below', '模糊判定門檻', '清晰度低於這個數值視為模糊', 1, 0, 100)}
    </div>
    <div><button class="btn" disabled=${disabled || !dirty} onClick=${() => onApply(f)}>套用</button></div>
  </div>`;
}
