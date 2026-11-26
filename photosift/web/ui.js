// Shared helpers and small components.
import { html, render, useState, useEffect, useRef, useMemo, useCallback, createContext, useContext } from './vendor/htm-preact.js';
export { html, render, useState, useEffect, useRef, useMemo, useCallback, createContext, useContext };

export async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET', headers: {} };
  if (opts.body !== undefined) {
    init.method = opts.method || 'POST';
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  let r;
  try {
    r = await fetch('/api' + path, init);
  } catch {
    throw new Error('連不上 PhotoSift 的本機服務，請確認程式還在執行。');
  }
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  if (!r.ok) throw new Error((data && data.detail) || `請求失敗（${r.status}）`);
  return data;
}

// ---- toasts -----------------------------------------------------------
const toastSubs = new Set();
let toastId = 0;
export function toast(msg, kind = 'ok') {
  const t = { id: ++toastId, msg, kind };
  toastSubs.forEach((f) => f(t));
}
export const fail = (e) => toast(e.message || String(e), 'err');
export function Toasts() {
  const [list, setList] = useState([]);
  useEffect(() => {
    const add = (t) => {
      setList((l) => [...l, t]);
      setTimeout(() => setList((l) => l.filter((x) => x.id !== t.id)), t.kind === 'err' ? 9000 : 4500);
    };
    toastSubs.add(add);
    return () => toastSubs.delete(add);
  }, []);
  return html`<div class="toasts">${list.map((t) => html`<div key=${t.id} class="toast ${t.kind === 'err' ? 'err' : ''}">${t.msg}</div>`)}</div>`;
}

// ---- app-wide state -----------------------------------------------------
export const AppCtx = createContext(null);
export const useApp = () => useContext(AppCtx);

// ---- formatting -----------------------------------------------------------
export const n0 = (v) => (v == null ? '—' : Math.round(v).toLocaleString('en-US'));
export const fmtDate = (iso) => (iso ? iso.replace('T', ' ').slice(0, 16) : '—');
export const fmtDay = (ts) => (ts ? new Date(ts * 1000).toISOString().slice(0, 10) : '');
export const FLAW = { blurry: '模糊', overexposed: '過曝', underexposed: '過暗', low_contrast: '對比不足' };
export const CATS = {
  landscape: '風景', water: '水岸與海景', city: '城市與建築', street: '街拍', portrait: '人像', group: '合照與聚會',
  food: '美食', animal: '動物', plant: '花卉與植物', night: '夜景', sunset: '日出日落', indoor: '室內空間',
  event: '活動與表演', vehicle: '交通工具', macro: '微距與靜物', document: '文件與截圖',
};
export const VLM_KEYS = [['composition', '構圖'], ['lighting', '光線'], ['color', '色彩'], ['subject', '主體'], ['story', '意境故事'], ['overall', '整體']];
export const thumb = (id) => `/api/thumb/${id}`;
export const qs = (o) => Object.entries(o).filter(([, v]) => v !== '' && v != null && v !== false).map(([k, v]) => `${k}=${encodeURIComponent(v === true ? 1 : v)}`).join('&');

// ---- icons (inline, stroke) ------------------------------------------------
const P = {
  grid: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',
  layers: 'M12 3l9 5-9 5-9-5 9-5zM3 13l9 5 9-5M3 17l9 5 9-5',
  trophy: 'M7 4h10v5a5 5 0 01-10 0V4zM7 6H4v1a3 3 0 003 3M17 6h3v1a3 3 0 01-3 3M12 14v4M8 20h8',
  gear: 'M12 9a3 3 0 100 6 3 3 0 000-6zM19 12l2-1-1-3-2 .5-1.5-1.5.5-2-3-1-1 2h-2l-1-2-3 1 .5 2L7 8.5 5 8l-1 3 2 1v2l-2 1 1 3 2-.5L8.5 19l-.5 2 3 1 1-2h2l1 2 3-1-.5-2 1.5-1.5 2 .5 1-3-2-1v-2z',
  search: 'M11 4a7 7 0 100 14 7 7 0 000-14zM20 20l-4-4',
  folder: 'M3 6h6l2 2h10v11H3z',
  x: 'M6 6l12 12M18 6L6 18',
  left: 'M15 5l-7 7 7 7',
  right: 'M9 5l7 7-7 7',
  check: 'M5 12l5 5 9-10',
  up: 'M12 19V5M5 12l7-7 7 7',
  play: 'M7 5l12 7-12 7z',
  cloud: 'M7 18a4 4 0 010-8 5 5 0 019.6-1.5A4.5 4.5 0 0117 18H7z',
  chip: 'M8 8h8v8H8zM4 10h4M4 14h4M16 10h4M16 14h4M10 4v4M14 4v4M10 16v4M14 16v4',
  edit: 'M4 20h4L19 9l-4-4L4 16v4z',
  out: 'M12 3v12M7 8l5-5 5 5M4 15v5h16v-5',
  trash: 'M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13',
  sift: 'M3 5h18l-7 8v6l-4 2v-8L3 5z',
  frame: 'M3 4h18v16H3zM7 8h10v8H7z',
  cap: 'M2 9l10-5 10 5-10 5-10-5zM6 11.2V16c0 1.6 2.8 3 6 3s6-1.4 6-3v-4.8M22 9v6',
};
export function Icon({ name, size = 16 }) {
  return html`<svg width=${size} height=${size} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d=${P[name]} /></svg>`;
}

// ---- dialog ---------------------------------------------------------------
export function Dialog({ title, sub, onClose, footer, wide, children }) {
  useEffect(() => {
    const k = (e) => e.key === 'Escape' && onClose && onClose();
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [onClose]);
  return html`<div class="dialog-bg" onMouseDown=${(e) => e.target === e.currentTarget && onClose && onClose()}>
    <div class="dialog ${wide ? 'wide' : ''}">
      <header><h2>${title}</h2>${sub && html`<div class="sub">${sub}</div>`}</header>
      <div class="body">${children}</div>
      ${footer && html`<footer>${footer}</footer>`}
    </div>
  </div>`;
}

// Folder chooser: the native dialog inside the desktop window, a simple browser otherwise.
export function FolderDialog({ title, confirmLabel = '選擇這個資料夾', onPick, onClose }) {
  const [cur, setCur] = useState(null);
  const [typed, setTyped] = useState('');
  const go = (path) => api('/browse?' + qs({ path })).then((d) => { setCur(d); setTyped(d.path); }).catch(fail);
  useEffect(() => { go(''); }, []);
  return html`<${Dialog} title=${title} onClose=${onClose}
    footer=${html`<button class="btn" onClick=${onClose}>取消</button>
      <button class="btn primary" disabled=${!typed} onClick=${() => onPick(typed)}>${confirmLabel}</button>`}>
    <div class="row">
      <input type="text" class="grow mono" value=${typed} onInput=${(e) => setTyped(e.target.value)}
        onKeyDown=${(e) => e.key === 'Enter' && go(typed)} placeholder="/Users/你/Pictures" />
      <button class="btn" onClick=${() => go(typed)}>前往</button>
    </div>
    ${cur && html`<div class="dirlist">
      ${cur.parent && html`<div onClick=${() => go(cur.parent)}><${Icon} name="up" size=${14} /> 上一層</div>`}
      ${cur.dirs.map((d) => html`<div key=${d} onClick=${() => go(cur.path + '/' + d)}><${Icon} name="folder" size=${14} /> ${d}</div>`)}
      ${!cur.dirs.length && html`<div class="muted" style="cursor:default">這裡沒有子資料夾</div>`}
    </div>`}
  <//>`;
}

// Asks for a folder using whichever chooser is available, then calls back with the path.
export function usePickFolder() {
  const { status } = useApp();
  const [dlg, setDlg] = useState(null);
  const pick = (title, confirmLabel) => new Promise((resolve) => {
    if (status && status.native_dialog) {
      api('/pick-folder', { method: 'POST' }).then((d) => resolve(d.path || null)).catch((e) => { fail(e); resolve(null); });
    } else {
      setDlg({ title, confirmLabel, resolve });
    }
  });
  const node = dlg && html`<${FolderDialog} title=${dlg.title} confirmLabel=${dlg.confirmLabel}
    onPick=${(p) => { dlg.resolve(p); setDlg(null); }} onClose=${() => { dlg.resolve(null); setDlg(null); }} />`;
  return [pick, node];
}

export function Bars({ rows, kind }) {
  return html`<div class="bars">${rows.map(([name, v, max = 10]) => html`
    <span class="n">${name}</span><div class="bar ${kind || ''}"><i style=${`width:${Math.max(0, Math.min(100, (v / max) * 100))}%`}></i></div><b class="num">${v}</b>`)}</div>`;
}
