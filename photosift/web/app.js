import { html, render, useState, useEffect, useRef, useCallback, api, fail, toast, Toasts, AppCtx, Icon, n0 } from './ui.js';
import { Library } from './library.js';
import { Organize } from './organize.js';
import { Contests } from './contest.js';
import { Settings } from './settings.js';

function parseHash() {
  const [view = 'library', arg] = location.hash.replace(/^#\/?/, '').split('/');
  return { view: view || 'library', arg };
}

function jobSummary(j) {
  const r = j.result || {};
  if (j.status === 'cancelled') return '已取消，完成的部分已保留。';
  if (j.kind === 'analyze') {
    const parts = [`${n0(r.photos || 0)} 張照片`, `${r.events || 0} 個事件`, `${r.groups || 0} 組連拍或重複`];
    if (r.annotated) parts.push(`地端模型標註 ${r.annotated} 張`);
    return `分析完成：${parts.join('、')}。` + (r.annotate_error ? `（標註未執行：${r.annotate_error}）` : '');
  }
  if (j.kind === 'annotate') return `地端模型標註了 ${r.annotated} 張` + (r.failed ? `，${r.failed} 張失敗` : '') + '。';
  if (j.kind === 'contest_local') return `地端評審完成：${r.scored} 張` + (r.agreement != null ? `，與外部評審的一致度 ${r.agreement}` : '') + '。';
  if (j.kind === 'contest_calibrate') return `校準完成：地端與外部評審的一致度 ${r.agreement_before ?? '—'}。`;
  if (j.kind === 'contest_finals') return '決選完成。';
  return '完成。';
}

function JobBar({ job }) {
  if (!job || job.status !== 'running') return null;
  const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
  return html`<div class="jobbar">
    <span class="spin"></span>
    <div style="min-width:210px"><b>${job.stage || job.title}</b><div class="small muted ellipsis" style="max-width:340px">${job.message}</div></div>
    <div class="prog ${job.total ? '' : 'ind'}"><i style=${`width:${pct}%`}></i></div>
    <span class="num muted small" style="min-width:92px;text-align:right">${job.total ? `${n0(job.done)} / ${n0(job.total)}` : ''}</span>
    <button class="btn sm" onClick=${() => api('/job/cancel', { method: 'POST' }).catch(fail)}>取消</button>
  </div>`;
}

function App() {
  const [route, setRoute] = useState(parseHash());
  const [status, setStatus] = useState(null);
  const [libFilters, setLibFilters] = useState({});
  const lastJob = useRef(null);
  const [jobTick, setJobTick] = useState(0); // bumps whenever a job ends, so views reload

  const refresh = useCallback(() => api('/status').then(setStatus).catch(() => {}), []);

  useEffect(() => {
    const on = () => setRoute(parseHash());
    window.addEventListener('hashchange', on);
    refresh();
    return () => window.removeEventListener('hashchange', on);
  }, []);

  // Poll quickly while a job runs, slowly otherwise.
  const running = status && status.job && status.job.status === 'running';
  useEffect(() => {
    const t = setInterval(refresh, running ? 900 : 6000);
    return () => clearInterval(t);
  }, [running]);

  useEffect(() => {
    const j = status && status.job;
    if (!j) return;
    const prev = lastJob.current;
    if (prev && prev.id === j.id && prev.status === 'running' && j.status !== 'running') {
      if (j.status === 'error') toast(j.error, 'err');
      else toast(jobSummary(j));
      setJobTick((x) => x + 1);
    }
    lastJob.current = { id: j.id, status: j.status };
  }, [status]);

  const go = (view, arg) => { location.hash = arg != null ? `#/${view}/${arg}` : `#/${view}`; };
  const openLibrary = (filters) => { setLibFilters(filters || {}); go('library'); };
  const startJob = (path, body) => api(path, { method: 'POST', body: body || {} }).then(() => refresh()).catch(fail);

  const ctx = { status, refresh, go, openLibrary, startJob, jobTick, running };
  const nav = [['library', '圖庫', 'grid'], ['organize', '整理', 'layers'], ['contest', '比賽選片', 'trophy'], ['settings', '設定', 'gear']];
  const s = status || {};
  const local = s.local || {};
  const cloud = s.cloud || {};

  return html`<${AppCtx.Provider} value=${ctx}>
    <div class="app">
      <aside class="side">
        <div class="brand"><${Icon} name="sift" size=${20} /><span>PhotoSift</span></div>
        ${nav.map(([v, label, icon]) => html`<button class="nav ${route.view === v ? 'on' : ''}" onClick=${() => go(v)}>
          <${Icon} name=${icon} /><span class="label">${label}</span></button>`)}
        <div class="side-sec">相片庫</div>
        <div class="funnel-mini">
          <div><span>全部照片</span><b>${n0(s.photos)}</b></div>
          <div><span>去除連拍重複後</span><b>${n0((s.photos || 0) - (s.duplicates || 0))}</b></div>
          <div><span>地端模型已標註</span><b>${n0(s.annotated)}</b></div>
          <div><span>你挑選的</span><b>${n0(s.picks)}</b></div>
        </div>
        <div class="side-sec">模型</div>
        <div class="dotline" title=${local.error || ''}>
          <i class="dot ${local.ok ? 'ok' : 'bad'}"></i><span class="ellipsis">地端 ${local.ok ? local.model : '未就緒'}</span></div>
        <div class="dotline">
          <i class="dot ${cloud.has_key ? 'ok' : 'warn'}"></i><span class="ellipsis">外部 ${cloud.label || ''}${cloud.has_key ? (cloud.via === 'cli' ? '（訂閱）' : '') : '（未設定）'}</span></div>
      </aside>
      <main>
        ${route.view === 'library' && html`<${Library} filters=${libFilters} setFilters=${setLibFilters} />`}
        ${route.view === 'organize' && html`<${Organize} tab=${route.arg || 'events'} />`}
        ${route.view === 'contest' && html`<${Contests} id=${route.arg ? +route.arg : null} />`}
        ${route.view === 'settings' && html`<${Settings} />`}
      </main>
      <${JobBar} job=${s.job} />
      <${Toasts} />
    </div>
  <//>`;
}

render(html`<${App} />`, document.getElementById('app'));
