// Step 5 of a contest: turn the picks into something that can be handed in.
// A small package for the first round, the full files only for what passes.
import { html, useState, useEffect, api, fail, toast, useApp, Icon, usePickFolder, thumb } from './ui.js';

export const mb = (n) => (n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB` : `${(n / 1e6).toFixed(1)} MB`);
const EXIF = [['all', '全部保留（含 GPS 位置）'], ['no_gps', '保留，但移除 GPS 位置'], ['none', '全部移除']];
const PATTERN = { '{no}_{title}': '01_作品名稱', '{no}_{title}_{author}': '01_作品名稱_作者', '{author}_{title}': '作者_作品名稱', '{author}_{no}': '作者_01', '{original}': '沿用原始檔名' };
const MARK = { ok: '✓', warn: '!', error: '✕' };
const when = (ts) => new Date(ts * 1000).toLocaleString('zh-TW', { hour12: false }).slice(0, -3);

function Entry({ e, i, last, spec, local, asking, onPatch, onAsk, onMove, onRemove, onOpen, onEdit }) {
  const [title, setTitle] = useState(e.title || '');
  const [desc, setDesc] = useState(e.description || '');
  useEffect(() => { setTitle(e.title || ''); }, [e.title]);
  useEffect(() => { setDesc(e.description || ''); }, [e.description]);
  const answer = (ix, a) => onPatch({ questions: e.questions.map((q, k) => (k === ix ? { ...q, a } : q)) });
  const waiting = e.status === 'draft' || e.status === 'sent';
  const src = e.use_edit ? `/api/edit/${e.photo_id}/render?r=saved&px=1024` : thumb(e.photo_id);

  return html`<div class="entry">
    <div class="no">${i + 1}
      <div class="stack gap-s" style="margin-top:8px;align-items:center">
        <button class="btn ghost sm" title="往前移" disabled=${i === 0} onClick=${() => onMove(-1)}><${Icon} name="up" size=${13} /></button>
        <button class="btn ghost sm" title="往後移" disabled=${last} onClick=${() => onMove(1)} style="transform:rotate(180deg)"><${Icon} name="up" size=${13} /></button>
        <button class="btn ghost sm danger" title="移出投稿名單" onClick=${onRemove}><${Icon} name="x" size=${13} /></button>
      </div>
    </div>
    <div class="stack gap-s">
      <img loading="lazy" src=${src} alt="" onClick=${onOpen} />
      ${e.missing && html`<div class="note bad">原始檔已經不在原本的位置</div>`}
      ${e.edited
        ? html`<div class="seg"><button class=${e.use_edit ? '' : 'on'} onClick=${() => onPatch({ use_edit: false })}>投原圖</button>
            <button class=${e.use_edit ? 'on' : ''} onClick=${() => onPatch({ use_edit: true })}>投修圖後的版本</button></div>`
        : html`<button class="btn sm" style="align-self:flex-start" onClick=${onEdit}><${Icon} name="edit" size=${13} /> 後製</button>`}
      <div class="kv small facts">${e.facts.map(([k, v]) => html`<span>${k}</span><span>${v}</span>`)}
        ${e.raw_size > 0 && html`<span>RAW 檔</span><span>有（${mb(e.raw_size)}），完整包會一起附上</span>`}</div>
    </div>
    <div class="stack">
      <div class="row">
        <input type="text" class="grow ttl" maxlength="60" placeholder="作品名稱" value=${title}
          onInput=${(ev) => setTitle(ev.target.value)} onBlur=${() => title.trim() !== (e.title || '') && onPatch({ title })} />
        <div class="seg">
          <button class=${waiting ? 'on' : ''} onClick=${() => !waiting && onPatch({ status: 'sent' })}>${e.status === 'draft' ? '尚未送出' : '等待結果'}</button>
          <button class=${e.status === 'passed' ? 'on yes' : ''} onClick=${() => onPatch({ status: 'passed' })}>通過初審</button>
          <button class=${e.status === 'failed' ? 'on no' : ''} onClick=${() => onPatch({ status: 'failed' })}>未通過</button>
        </div>
      </div>
      <div class="bgq">
        <div class="row"><b class="grow">拍攝背景 <span class="small muted" style="font-weight:400">　只有你知道的事。回答了，作品說明才寫得出你的觀點</span></b>
          <button class="btn sm ${e.questions.length ? '' : 'local'}" disabled=${!local.ok || asking} title=${local.ok ? '' : local.error} onClick=${onAsk}>
            ${asking ? html`<span class="spin"></span>` : html`<${Icon} name="chip" size=${13} />`} ${e.questions.length ? '換一組問題' : '請地端模型問我'}</button></div>
        ${e.questions.map((q, k) => html`<div class="stack gap-s" key=${q.q}>
          <span>${q.q}</span>
          <div class="row wrap gap-s">${q.options.map((o) => html`<span class="chip pick ${q.a === o ? 'on' : ''}" onClick=${() => answer(k, q.a === o ? '' : o)}>${o}</span>`)}</div>
          <input type="text" placeholder="或用自己的話回答" value=${q.options.includes(q.a) ? '' : q.a}
            onChange=${(ev) => answer(k, ev.target.value.trim())} />
        </div>`)}
      </div>
      <div class="field">
        <div class="row"><span class="grow small muted">作品說明</span>
          <span class="count ${desc.length > spec.desc_max ? 'over' : ''}">${desc.length} / ${spec.desc_max} 字</span></div>
        <textarea rows="4" placeholder="按下方的按鈕請模型起草，或自己寫" value=${desc}
          onInput=${(ev) => setDesc(ev.target.value)} onBlur=${() => desc.trim() !== (e.description || '') && onPatch({ description: desc })}></textarea>
      </div>
    </div>
  </div>`;
}

export function Submission({ cid, c, sub, setSub, onOpen, onEdit }) {
  const { status, running, startJob } = useApp();
  const cloud = status ? status.cloud : {};
  const local = status ? status.local : {};
  const [busy, setBusy] = useState('');
  const [asking, setAsking] = useState(0);
  const [zip, setZip] = useState(false);
  const [pickFolder, folderDlg] = usePickFolder();
  const base = `/contests/${cid}/submission`;
  const es = sub.entries;
  const sp = sub.spec;
  const ids = es.map((e) => e.photo_id);

  const save = (body) => api(base, { method: 'PUT', body }).then(setSub).catch(fail);
  const patch = (pid, body) => api(`${base}/${pid}`, { method: 'PATCH', body }).then(setSub).catch(fail);
  const setSpec = (k, v) => save({ spec: { [k]: v } });
  const move = (i, by) => { const next = [...ids]; [next[i], next[i + by]] = [next[i + by], next[i]]; save({ photo_ids: next }); };
  const ask = async (pid) => {
    setAsking(pid);
    try { await api(`${base}/${pid}/questions`, { method: 'POST' }); setSub(await api(base)); } catch (e) { fail(e); }
    setAsking(0);
  };
  const askAll = async () => {
    for (const e of es.filter((x) => !x.questions.length)) await ask(e.photo_id);
  };
  const write = async (use_cloud, only_empty) => {
    if (!only_empty && es.some((e) => e.description) && !window.confirm('會重寫所有作品的說明，包含你已經改過的。確定嗎？')) return;
    setBusy(use_cloud ? 'cloud' : 'local');
    try {
      const r = await api(`${base}/copy`, { body: { use_cloud, only_empty } });
      setSub(r);
      toast(`寫好了 ${r.written} 段作品說明` + (r.tokens ? `，用了 ${r.tokens.toLocaleString('en-US')} tokens（只送文字）` : '（地端模型，不花 token）')
        + (r.over_limit ? `。其中 ${r.over_limit} 段仍超過字數上限，請手動刪減` : '。'));
    } catch (e) { fail(e); }
    setBusy('');
  };
  const pack = async (kind) => {
    if (kind === 'prelim' && sub.checks.some((x) => x.level === 'error') && !window.confirm('檢查清單裡還有可能被退件的問題，仍然要打包嗎？')) return;
    const dest = await pickFolder(kind === 'prelim' ? '初審包要放在哪裡？' : '完整包要放在哪裡？', '放在這裡');
    if (dest) startJob(`${base}/package`, { kind, dest, zip: kind === 'full' && zip });
  };

  const answered = es.filter((e) => e.questions.some((q) => q.a)).length;
  const empty = es.filter((e) => !e.description).length;
  const differs = sub.suggested.length > 0 && (sub.suggested.length !== ids.length || sub.suggested.some((x) => !ids.includes(x)));
  const sent = sub.packages.some((p) => p.kind === 'prelim');
  const writer = cloud.label || '外部模型';

  return html`<section class="step ${sent ? 'done' : ''}">
    <header><span class="ix">${sent ? html`<${Icon} name="check" size=${14} />` : '5'}</span>
      <div class="grow"><h2>投稿　<span class="chip local">地端模型提問</span> <span class="chip cloud">外部模型只讀文字</span></h2>
        <div class="sub">原始檔動輒數十 MB，所以分兩次交：先交縮小的初審檔和作品說明，通過初審的作品再打包原始檔。原始檔只會被複製，不會被更動。</div></div>
    </header>
    <div class="content">
      ${!es.length && html`<div class="note">還沒有投稿名單。${c.summary ? '' : '完成決選後，評審建議的作品會自動帶到這裡；'}也可以在下方的排名按「加入投稿」自己選。
        ${!c.summary && html`<div style="margin-top:8px"><button class="btn sm" onClick=${() => api(base).then(setSub).catch(fail)}>先用目前排名最前面的 ${sub.limit} 張</button></div>`}</div>`}
      ${differs && es.length > 0 && html`<div class="note">目前的投稿名單和評審這次建議的 ${sub.suggested.length} 張不一樣。
        <a onClick=${() => save({ photo_ids: sub.suggested })}>改用評審建議的名單</a>（已經寫好的名稱和說明會保留）</div>`}

      ${es.length > 0 && html`
        <div class="row wrap">
          <span class="grow small muted">${es.length} 件作品${sub.limit ? `（每人最多 ${sub.limit} 件）` : ''}，原始檔合計 ${mb(sub.original_bytes)}。已回答拍攝背景的有 ${answered} 件。</span>
          ${es.some((e) => !e.questions.length) && html`<button class="btn local sm" disabled=${!local.ok || !!asking} onClick=${askAll}>
            <${Icon} name="chip" size=${13} /> 請地端模型為每件作品提問</button>`}
        </div>
        <div>${es.map((e, i) => html`<${Entry} key=${e.photo_id} e=${e} i=${i} last=${i === es.length - 1} spec=${sp} local=${local} asking=${asking === e.photo_id}
          onPatch=${(b) => patch(e.photo_id, b)} onAsk=${() => ask(e.photo_id)} onMove=${(by) => move(i, by)}
          onRemove=${() => save({ photo_ids: ids.filter((x) => x !== e.photo_id) })}
          onOpen=${() => onOpen(ids, i)} onEdit=${() => onEdit(ids, i)} />`)}</div>

        <div class="subbox">
          <div class="row wrap">
            <h3 class="grow">作品說明</h3>
            <select value=${sp.tone} onChange=${(ev) => setSpec('tone', ev.target.value)} title="語氣">${sub.tones.map((t) => html`<option value=${t}>語氣：${t}</option>`)}</select>
            <button class="btn primary" disabled=${!!busy || !cloud.has_key} onClick=${() => write(true, false)}>
              ${busy === 'cloud' ? html`<span class="spin"></span>` : html`<${Icon} name="cloud" size=${14} />`} 請 ${writer} 寫作品說明</button>
            ${empty > 0 && empty < es.length && html`<button class="btn" disabled=${!!busy || !cloud.has_key} onClick=${() => write(true, true)}>只補還沒寫的 ${empty} 件</button>`}
            <button class="btn ghost" disabled=${!!busy || !local.ok} onClick=${() => write(false, empty > 0 && empty < es.length)}>
              ${busy === 'local' ? html`<span class="spin"></span>` : html`<${Icon} name="chip" size=${14} />`} 改用地端模型寫</button>
          </div>
          <p class="small muted">只送文字，照片不會再上傳：地端模型對畫面的描述、評審寫的優點、拍攝時間地點，以及你對拍攝背景的回答。模型被要求不可以編造沒被提到的經歷；沒有回答背景問題的作品，說明只會寫畫面本身和它與主題的關係。寫好後請務必自己讀過、改成你的話。</p>
          ${!cloud.has_key && html`<p class="small muted">還沒有設定外部模型，可以先用地端模型起草，或自己寫。</p>`}
        </div>

        <div class="subbox">
          <h3>初審檔規格${c.brief.spec && Object.keys(c.brief.spec).length ? html`<span class="small faint" style="font-weight:400">　部分數值來自簡章，可以修改</span>` : ''}</h3>
          <div class="row wrap top gap-l">
            <div class="field" style="width:130px"><span>長邊像素</span><input type="number" min="600" step="100" value=${sp.long_edge} onChange=${(ev) => setSpec('long_edge', +ev.target.value)} /></div>
            <div class="field" style="width:130px"><span>每個檔案上限（MB）</span><input type="number" min="0.2" step="0.5" value=${sp.max_mb} onChange=${(ev) => setSpec('max_mb', +ev.target.value)} /></div>
            <div class="field" style="width:130px"><span>說明字數上限</span><input type="number" min="20" step="10" value=${sp.desc_max} onChange=${(ev) => setSpec('desc_max', +ev.target.value)} /></div>
            <div class="field" style="width:220px"><span>照片內的拍攝資訊（EXIF）</span><select value=${sp.exif} onChange=${(ev) => setSpec('exif', ev.target.value)}>
              ${EXIF.map(([v, l]) => html`<option value=${v}>${l}</option>`)}</select></div>
            <div class="field" style="width:180px"><span>檔名</span><select value=${sp.name} onChange=${(ev) => setSpec('name', ev.target.value)}>
              ${sub.patterns.map((p) => html`<option value=${p}>${PATTERN[p] || p}</option>`)}</select></div>
            <div class="field" style="width:150px"><span>作者姓名</span><input type="text" maxlength="40" value=${sp.author} placeholder="選填" onChange=${(ev) => setSpec('author', ev.target.value)} /></div>
          </div>
          <p class="small muted">初審檔一律轉成 sRGB，避免手機拍的廣色域照片在評審的螢幕上顏色變淡。${sp.exif === 'all' ? '拍攝資訊會完整保留，包含 GPS 位置；如果不想讓主辦單位看到拍攝地點的座標，可以改成移除 GPS。' : ''}</p>
        </div>

        <ul class="checks">${sub.checks.map((x) => html`<li class=${x.level}><b>${MARK[x.level]}</b><span>${x.text}</span></li>`)}</ul>

        <div class="pkgs">
          <div class="pkg">
            <h3>第一步　初審包</h3>
            <p class="small muted grow">${es.length} 張縮小到長邊 ${sp.long_edge}px、每張 ${sp.max_mb} MB 以內的 JPEG，加上一份「作品說明.html」（用瀏覽器開啟，可以直接列印成 PDF），並另外壓成一個 zip 方便上傳或寄出。</p>
            <div><button class="btn primary" disabled=${running} onClick=${() => pack('prelim')}><${Icon} name="out" size=${14} /> 建立初審包…</button></div>
          </div>
          <div class="pkg">
            <h3>第二步　完整包</h3>
            <p class="small muted grow">只包含上面標記為「通過初審」的作品：原封不動的原始檔、RAW 檔（有的話）、修圖後的全尺寸版本，以及同一份作品說明。
              ${sub.full.count ? html`<br /><b style="color:var(--text)">${sub.full.count} 件作品，預估 ${mb(sub.full.bytes)}</b>${sub.full.raws ? `（其中 RAW ${mb(sub.full.raws)}）` : ''}` : html`<br />目前還沒有作品標記為通過初審。`}</p>
            <div class="row"><button class="btn ${sub.full.count ? 'primary' : ''}" disabled=${running || !sub.full.count} onClick=${() => pack('full')}><${Icon} name="out" size=${14} /> 建立完整包…</button>
              <label class="check small"><input type="checkbox" checked=${zip} onChange=${(ev) => setZip(ev.target.checked)} /> 同時打包成一個 zip（會多占一份空間）</label></div>
          </div>
        </div>

        ${sub.packages.length > 0 && html`<div>
          <h3 style="margin-bottom:6px">已建立的投稿包</h3>
          ${[...sub.packages].reverse().map((p) => html`<div class="row small pkgrow" key=${p.at}>
            <span class="chip ${p.kind === 'prelim' ? 'cloud' : 'local'}">${p.kind === 'prelim' ? '初審包' : '完整包'}</span>
            <span>${p.count} 件・${mb(p.bytes)}</span><span class="muted">${when(p.at)}</span>
            <span class="grow mono ellipsis ${p.exists ? '' : 'faint'}" title=${p.folder}>${p.folder}${p.exists ? '' : '（已移走或刪除）'}</span>
            ${p.exists && html`<a onClick=${() => api(`${base}/reveal`, { body: { at: p.at } }).catch(fail)}>打開資料夾</a>`}
          </div>`)}
        </div>`}`}
    </div>
    ${folderDlg}
  </section>`;
}
