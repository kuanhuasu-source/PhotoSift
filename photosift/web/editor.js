// The editing panel inside the photo viewer: suggestions, sliders, export.
import { html, useState, useEffect, useRef, api, fail, toast, useApp, Icon, usePickFolder, n0 } from './ui.js';

const GROUPS = [
  ['光線', [['exposure', '曝光', -1.5, 1.5, 0.05], ['contrast', '對比', -100, 100, 1], ['highlights', '亮部', -100, 100, 1], ['shadows', '暗部', -100, 100, 1]]],
  ['色彩', [['temperature', '色溫', -100, 100, 1], ['tint', '色調', -100, 100, 1], ['vibrance', '自然飽和度', -100, 100, 1], ['saturation', '飽和度', -100, 100, 1]]],
  ['質感', [['clarity', '清晰度', -100, 100, 1], ['sharpen', '銳利化', 0, 100, 1], ['vignette', '暗角', -100, 100, 1]]],
];
const RATIOS = [['原始', 0], ['1:1', 1], ['4:5', 4 / 5], ['3:2', 3 / 2], ['16:9', 16 / 9]];
const VERDICT = { good: '修得剛好', more: '還可以再多一點', less: '修過頭了，已收斂', worse: '比原圖差，已減半' };
const same = (a, b) => JSON.stringify(a || {}) === JSON.stringify(b || {});

// Drop neutral values so that "nothing changed" is always the empty recipe.
export function tidy(r) {
  const out = {};
  for (const [k, v] of Object.entries(r || {})) {
    if (k === 'crop') {
      if (v && (v.left > 0.004 || v.top > 0.004 || v.right < 0.996 || v.bottom < 0.996)) out.crop = v;
    } else if (k === 'bw') {
      if (v) out.bw = true;
    } else if (v) out[k] = v;
  }
  return out;
}

function Slider({ label, value, min, max, step, onChange, unit = '' }) {
  const shown = step < 1 ? (value > 0 ? '+' : '') + value.toFixed(2) : (value > 0 ? '+' : '') + Math.round(value);
  return html`<div class="slider">
    <span class="n" title="按兩下歸零" onDblClick=${() => onChange(0)}>${label}</span>
    <input type="range" min=${min} max=${max} step=${step} value=${value}
      onInput=${(e) => onChange(+e.target.value)} onDblClick=${() => onChange(0)} />
    <b class="num ${value ? '' : 'faint'}">${shown}${unit}</b>
  </div>`;
}

export function Editor({ photo, recipe, setRecipe, contestId, onSaved }) {
  const { status, go } = useApp();
  const [edit, setEdit] = useState(null); // { suggested, stats } from the server
  const [busy, setBusy] = useState('');
  const [review, setReview] = useState(null);
  const [selfCheck, setSelfCheck] = useState(true);
  const [saved, setSaved] = useState(true);
  const [pickFolder, folderDlg] = usePickFolder();
  const timer = useRef(null);
  const loaded = useRef(false);

  useEffect(() => {
    loaded.current = false;
    setEdit(null); setReview(null);
    api('/edit/' + photo.id).then((d) => { setEdit(d); setRecipe(tidy(d.recipe)); loaded.current = true; }).catch(fail);
    return () => clearTimeout(timer.current);
  }, [photo.id]);

  // Save a moment after the last change. The original file is never written to.
  useEffect(() => {
    if (!loaded.current || recipe == null) return;
    setSaved(false);
    clearTimeout(timer.current);
    const id = photo.id, r = recipe;
    timer.current = setTimeout(() => {
      api('/edit/' + id, { method: 'PUT', body: { recipe: r } })
        .then(() => { setSaved(true); onSaved && onSaved(id, Object.keys(r).length > 0); }).catch(fail);
    }, 500);
  }, [recipe]);

  if (!edit || recipe == null) return html`<div class="row muted"><span class="spin"></span> 載入中…</div>`;

  const set = (k, v) => setRecipe(tidy({ ...recipe, [k]: v }));
  const local = status ? status.local : {};
  const cloud = status ? status.cloud : {};
  const st = edit.stats;
  const sug = edit.suggested || {};
  const crop = recipe.crop || { left: 0, top: 0, right: 1, bottom: 1 };
  const setCrop = (patch) => {
    const c = { ...crop, ...patch };
    if (c.right - c.left < 0.3 || c.bottom - c.top < 0.3) return; // keep at least 30% of each side
    set('crop', c);
  };
  const ratio = (target) => {
    if (!target) return set('crop', null);
    const cur = photo.width / photo.height;
    let w = 1, h = 1;
    if (target < cur) w = target / cur; else h = cur / target;
    set('crop', { left: (1 - w) / 2, top: (1 - h) / 2, right: (1 + w) / 2, bottom: (1 + h) / 2 });
  };

  const askLocal = async () => {
    setBusy('local'); setReview(null);
    try {
      const s = await api(`/edit/${photo.id}/suggest`, { body: { rounds: selfCheck ? 2 : 0, contest_id: contestId || null } });
      setEdit({ ...edit, suggested: { ...sug, local: s } });
      setRecipe(tidy(s.recipe));
      toast(Object.keys(s.recipe).length ? '已套用地端模型的建議，可以再用滑桿微調。' : '地端模型認為這張照片不需要後製。');
    } catch (e) { fail(e); }
    setBusy('');
  };
  const askCloud = async () => {
    if (!window.confirm(`把這張照片修圖前後的對照縮圖送給 ${cloud.label} 複查？大約會用 1,500～3,000 tokens。`)) return;
    setBusy('cloud');
    try {
      const r = await api(`/edit/${photo.id}/review`, { body: { recipe } });
      setReview(r);
      if (!same(tidy(r.recipe), recipe)) setRecipe(tidy(r.recipe));
    } catch (e) { fail(e); }
    setBusy('');
  };
  const exportOne = async () => {
    const dest = await pickFolder('匯出修圖後的複本到…', '匯出到這裡');
    if (!dest) return;
    setBusy('export');
    try {
      await api('/edit/' + photo.id, { method: 'PUT', body: { recipe } }); // make sure the latest sliders are what gets rendered
      const r = await api('/edit/export', { body: { dest, photo_ids: [photo.id] } });
      toast(r.exported ? `已匯出 ${r.files[0]} 到 ${r.dest}（原始檔沒有變動）` : '沒有可匯出的修圖。', r.exported ? 'ok' : 'err');
    } catch (e) { fail(e); }
    setBusy('');
  };

  const Suggestion = ({ s, kind }) => html`<div class="sugg ${kind}">
    <div class="row"><span class="chip ${kind === 'cloud' ? 'cloud' : 'local'}">${kind === 'cloud' ? '外部評審的建議' : '地端模型的建議'}</span>
      <span class="grow"></span>
      ${same(tidy(s.recipe), recipe)
        ? html`<span class="small faint">目前套用中</span>`
        : html`<button class="btn sm" onClick=${() => setRecipe(tidy(s.recipe))}>套用</button>`}</div>
    <p>${s.summary || (Object.keys(s.recipe).length ? '' : '不需要後製。')}</p>
    ${s.steps && s.steps.length > 0 && html`<ul>${s.steps.map((x) => html`<li><b>${x.what}</b>　<span class="muted">${x.why}</span></li>`)}</ul>`}
    ${s.reviews && s.reviews.length > 0 && html`<p class="small muted">自我複查：${s.reviews.map((v) => `${VERDICT[v.verdict] || v.verdict}（${v.comment}）`).join('　→　')}</p>`}
  </div>`;

  const tiltKnown = st.tilt_confidence >= 0.6 && Math.abs(st.tilt) >= 0.4;
  return html`<div class="editor">
    <div class="row wrap gap-s">
      <button class="btn local" disabled=${!!busy || !local.ok} title=${local.ok ? '' : local.error} onClick=${askLocal}>
        ${busy === 'local' ? html`<span class="spin"></span>` : html`<${Icon} name="chip" size=${14} />`} ${sug.local ? '重新建議' : '請地端模型建議'}</button>
      <label class="check small"><input type="checkbox" checked=${selfCheck} onChange=${(e) => setSelfCheck(e.target.checked)} />套用後自我複查</label>
    </div>
    ${busy === 'local' && html`<p class="small muted">地端模型正在看照片${selfCheck ? '，並會檢查自己修出來的結果' : ''}，大約需要十幾秒到一分鐘。不花 token。</p>`}
    ${!local.ok && html`<p class="small muted">${local.error}　<a onClick=${() => go('settings')}>前往設定</a></p>`}
    ${sug.local && html`<${Suggestion} s=${sug.local} kind="local" />`}
    ${sug.cloud && html`<${Suggestion} s=${sug.cloud} kind="cloud" />`}
    ${review && html`<div class="sugg cloud"><span class="chip cloud">${cloud.label} 的複查</span>
      <p><b>${VERDICT[review.verdict] || review.verdict}</b>　${review.comment}</p>
      <p class="small muted">用了 ${n0(review.tokens)} tokens。</p></div>`}

    <p class="small muted">量測：平均亮度 ${st.brightness}${st.need.exposure ? `（偏${st.need.exposure > 0 ? '暗' : '亮'}）` : ''}${st.need.contrast ? '、畫面偏灰' : ''}
      ${tiltKnown ? html`、可能歪了 ${Math.abs(st.tilt).toFixed(1)}°　<a onClick=${() => set('rotate', st.tilt)}>自動轉正</a>` : ''}</p>

    ${GROUPS.map(([title, rows]) => html`<div><h3>${title}</h3>
      ${rows.map(([k, label, min, max, step]) => html`<${Slider} key=${k} label=${label} min=${min} max=${max} step=${step}
        value=${recipe[k] || 0} onChange=${(v) => set(k, v)} />`)}
      ${title === '色彩' && html`<label class="check small" style="margin-top:4px"><input type="checkbox" checked=${!!recipe.bw} onChange=${(e) => set('bw', e.target.checked)} />轉成黑白</label>`}
    </div>`)}

    <div><h3>構圖</h3>
      <${Slider} label="轉正" min=${-10} max=${10} step=${0.1} value=${recipe.rotate || 0} onChange=${(v) => set('rotate', v)} unit="°" />
      <${Slider} label="裁左" min=${0} max=${40} step=${1} value=${Math.round(crop.left * 100)} onChange=${(v) => setCrop({ left: v / 100 })} unit="%" />
      <${Slider} label="裁右" min=${0} max=${40} step=${1} value=${Math.round((1 - crop.right) * 100)} onChange=${(v) => setCrop({ right: 1 - v / 100 })} unit="%" />
      <${Slider} label="裁上" min=${0} max=${40} step=${1} value=${Math.round(crop.top * 100)} onChange=${(v) => setCrop({ top: v / 100 })} unit="%" />
      <${Slider} label="裁下" min=${0} max=${40} step=${1} value=${Math.round((1 - crop.bottom) * 100)} onChange=${(v) => setCrop({ bottom: 1 - v / 100 })} unit="%" />
      <div class="row wrap gap-s" style="margin-top:6px"><span class="small muted">比例</span>
        ${RATIOS.map(([l, v]) => html`<button class="btn sm" onClick=${() => ratio(v)}>${l}</button>`)}</div>
    </div>

    <div class="row wrap gap-s">
      <button class="btn" disabled=${!Object.keys(recipe).length} onClick=${() => { setRecipe({}); setReview(null); }}>重設</button>
      <button class="btn" disabled=${!!busy || !Object.keys(recipe).length} onClick=${exportOne}>
        ${busy === 'export' ? html`<span class="spin"></span>` : html`<${Icon} name="out" size=${14} />`} 匯出修圖後的複本</button>
      <span class="small faint">${saved ? '已自動儲存' : '儲存中…'}</span>
    </div>
    <div class="row wrap gap-s">
      <button class="btn sm" disabled=${!!busy || !cloud.has_key || !Object.keys(recipe).length} onClick=${askCloud}
        title=${cloud.has_key ? '' : '尚未設定外部模型的 API key'}>
        ${busy === 'cloud' ? html`<span class="spin"></span>` : html`<${Icon} name="cloud" size=${13} />`} 請 ${cloud.label || '外部模型'} 複查</button>
      <span class="small faint">會上傳一張前後對照的縮圖</span>
    </div>
    <p class="small faint">修圖只記錄成一組調整步驟，原始檔不會被更動。按住空白鍵可以看原圖。</p>
    ${folderDlg}
  </div>`;
}

// Shows the rendered edit, swapping only once the new frame has loaded so dragging a slider does not flicker.
export function useRendered(id, recipe, active) {
  const [src, setSrc] = useState(null);
  const seq = useRef(0);
  useEffect(() => {
    if (!active || recipe == null) { setSrc(null); return; }
    const mine = ++seq.current;
    const t = setTimeout(() => {
      const url = `/api/edit/${id}/render?px=1024&r=${encodeURIComponent(JSON.stringify(recipe))}`;
      const im = new Image();
      im.onload = () => { if (mine === seq.current) setSrc(url); };
      im.src = url;
    }, 90);
    return () => clearTimeout(t);
  }, [id, active, JSON.stringify(recipe)]);
  return src;
}
