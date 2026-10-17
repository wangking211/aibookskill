const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let uploadFile = null;
let jobsTimer = null;

async function jget(url) {
  const r = await fetch(url);
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    const e = new Error(j.error || j.detail || r.status);
    e.status = r.status;
    throw e;
  }
  return r.json();
}
async function jpost(url, body) {
  const r = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    const e = new Error(j.error || j.detail || r.status);
    e.status = r.status;
    throw e;
  }
  return r.json();
}
function fail(e) {
  if (e.status === 401) { openAuth("login"); toast("请先登录"); return; }
  toast("失败: " + e.message);
}

let me = null;
let authMode = "login";
function renderAuth() {
  $("#auth-anon").hidden = !!me;
  $("#auth-user").hidden = !me;
  if (me) $("#auth-name").textContent = me.display_name || me.identifier;
  $("#nav-admin").hidden = !(me && me.role === "admin");
}
function openAuth(mode) {
  authMode = mode;
  $("#auth-modal").hidden = false;
  $("#auth-title").textContent = mode === "login" ? "登录" : "注册";
  $("#au-submit").textContent = mode === "login" ? "登录" : "注册";
  $("#au-name-row").hidden = mode === "login";
  $("#au-switch").textContent = mode === "login" ? "没有账号？去注册" : "已有账号？去登录";
  $("#auth-err").textContent = "";
  $("#au-password").value = "";
}
async function submitAuth() {
  const identifier = $("#au-identifier").value.trim();
  const password = $("#au-password").value;
  try {
    const r = await fetch(authMode === "login" ? "/api/login" : "/api/register", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ identifier, password, display_name: $("#au-display").value.trim() }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { $("#auth-err").textContent = data.error || ("错误 " + r.status); return; }
    me = data;
    renderAuth();
    $("#auth-modal").hidden = true;
    toast(authMode === "login" ? "已登录" : "注册成功，已自动登录");
    loadBooks();
  } catch (err) {
    $("#auth-err").textContent = "网络错误，请重试";
  }
}
$("#btn-login").addEventListener("click", () => openAuth("login"));
$("#btn-register").addEventListener("click", () => openAuth("register"));
$("#au-switch").addEventListener("click", () => openAuth(authMode === "login" ? "register" : "login"));
$("#au-cancel").addEventListener("click", () => { $("#auth-modal").hidden = true; });
$("#au-submit").addEventListener("click", submitAuth);
["au-identifier", "au-password"].forEach((id) => {
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !$("#auth-modal").hidden && document.activeElement?.id === id)
      submitAuth();
  });
});
$("#btn-logout").addEventListener("click", async () => {
  try { await jpost("/api/logout", {}); } catch (e) { /* ignore */ }
  me = null;
  renderAuth();
  toast("已退出");
});

// ---- API 令牌管理 ----
function mcpSnippet(tok) {
  return `{\n  "mcp": {\n    "aibook": {\n      "type": "local",\n` +
    `      "command": ["python", "mcp_server.py"],\n` +
    `      "environment": {\n        "AIBOOK_BASE_URL": "${location.origin}",\n` +
    `        "AIBOOK_TOKEN": "${tok}"\n      },\n      "enabled": true\n    }\n  }\n}`;
}
async function loadTokens() {
  const list = $("#tk-list");
  list.innerHTML = '<li class="hint">加载中…</li>';
  try {
    const r = await fetch("/api/tokens");
    const toks = await r.json();
    if (!r.ok) { list.innerHTML = `<li class="err">${esc(toks.detail || "加载失败")}</li>`; return; }
    if (!toks.length) { list.innerHTML = '<li class="hint">暂无令牌，创建一个给 agent 用。</li>'; return; }
    list.innerHTML = toks.map((t) => `<li>` +
      `<span class="tk-mask">${esc(t.token_masked)}</span>` +
      `<span class="tk-meta">${esc(t.name || "未命名")} · 创建 ${fmtTime(t.created_at)}` +
      `${t.last_used ? " · 最近使用 " + fmtTime(t.last_used) : ""}</span>` +
      `<button class="ghost tk-del" data-tok="${esc(t.token)}">删除</button></li>`).join("");
  } catch (e) {
    list.innerHTML = '<li class="err">网络错误</li>';
  }
}
$("#tk-list").addEventListener("click", async (e) => {
  const b = e.target.closest(".tk-del");
  if (!b) return;
  if (!confirm("删除该令牌？使用它的 agent 将立即失效。")) return;
  try {
    const r = await fetch("/api/tokens/" + encodeURIComponent(b.dataset.tok), { method: "DELETE" });
    if (r.ok) { toast("已删除"); loadTokens(); }
    else toast("删除失败");
  } catch (err) { toast("网络错误"); }
});
$("#btn-tokens").addEventListener("click", () => {
  $("#token-modal").hidden = false;
  $("#tk-just").hidden = true;
  loadTokens();
});
$("#tk-close").addEventListener("click", () => { $("#token-modal").hidden = true; });
$("#tk-create").addEventListener("click", async () => {
  const name = $("#tk-name").value.trim();
  try {
    const r = await fetch("/api/tokens", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { toast(d.detail || "创建失败"); return; }
    $("#tk-full").textContent = d.token;
    $("#tk-mcp").textContent = mcpSnippet(d.token);
    $("#tk-just").hidden = false;
    $("#tk-name").value = "";
    toast("令牌已创建，请复制保存");
    loadTokens();
  } catch (e) { toast("网络错误"); }
});
$("#tk-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#tk-full").textContent); toast("已复制"); }
  catch (e) { toast("复制失败，请手动选中"); }
});
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => { t.hidden = true; }, 2600);
}
function fmtDur(sec) {
  if (sec == null) return "-";
  if (sec < 60) return sec + "s";
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return `${m}m${String(s).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}m`;
}
function fmtTok(n) {
  if (n == null) return "-";
  return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
}
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function renderMd(s) {
  if (!s) return "";
  if (window.marked && window.DOMPurify) {
    return DOMPurify.sanitize(marked.parse(String(s)));
  }
  return esc(s).replace(/\n/g, "<br>");
}

function langBadge(lang) {
  if (lang === "zh") return '<span class="badge">中文</span>';
  if (lang === "en") return '<span class="badge">EN</span>';
  if (lang === "bi") return '<span class="badge">中英对照</span>';
  return "";
}

const readState = { slug: "", no: null, lang: "orig", translations: [] };

function applyBilingual(content) {
  const parts = String(content).split(/^===zh===\s*$/m);
  let html = "";
  for (let i = 0; i < parts.length; i += 2) {
    const orig = (parts[i] || "").trim();
    const zh = (parts[i + 1] || "").trim();
    if (!orig && !zh) continue;
    html += '<div class="bi-pair"><div class="bi-orig">' + renderMd(orig) + '</div>'
      + '<div class="bi-zh">' + (zh ? renderMd(zh) : "") + '</div></div>';
  }
  return html || "<p class='hint'>（空章节）</p>";
}

function renderChapter(content) {
  const body = $("#d-read-body");
  body.removeAttribute("data-view");
  body.innerHTML = String(content).includes("===zh===")
    ? applyBilingual(content) : renderMd(content);
}

function highlightSnippet(snippet) {
  const body = $("#d-read-body");
  if (!body) return;
  body.querySelectorAll(".src-hl").forEach((el) => {
    const t = document.createTextNode(el.textContent);
    el.replaceWith(t);
  });
  if (!snippet) return;
  // snippet is raw markdown; the rendered chapter has no emphasis tokens.
  let probe = snippet
    .replace(/…/g, "")
    .replace(/[`*_#>|]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 36);
  if (!probe) return;
  // Collect all text nodes; build a whitespace-normalized view with an
  // original-offset map so probes survive markdown node splits and
  // newline/space differences.
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let full = "", node;
  while ((node = walker.nextNode())) {
    nodes.push({ node, start: full.length, text: node.textContent });
    full += node.textContent;
  }
  const norm = [], map = [];
  let lastSpace = true;
  for (let i = 0; i < full.length; i++) {
    if (/\s/.test(full[i])) {
      if (lastSpace) continue;
      norm.push(" "); map.push(i); lastSpace = true;
    } else {
      norm.push(full[i]); map.push(i); lastSpace = false;
    }
  }
  const normStr = norm.join("");
  const probeNorm = probe.replace(/\s+/g, " ");
  let idx = full.indexOf(probe);
  let endIdx = idx + probe.length;
  if (idx < 0) {
    const ni = normStr.indexOf(probeNorm);
    if (ni >= 0) {
      idx = map[ni];
      endIdx = map[ni + probeNorm.length - 1] + 1;
    }
  }
  if (idx < 0) {
    // last resort: probe tail (index snippets may carry a leading prefix)
    const tail = probeNorm.slice(-18);
    const ni = tail.length >= 8 ? normStr.indexOf(tail) : -1;
    if (ni >= 0) {
      idx = map[ni];
      endIdx = map[ni + tail.length - 1] + 1;
    }
  }
  if (idx < 0) {
    $("#d-read").scrollIntoView({ behavior: "smooth" });
    return;
  }
  // Wrap [idx, endIdx) across possibly multiple text nodes.
  let firstSpan = null;
  for (const it of nodes) {
    const nStart = it.start, nEnd = it.start + it.text.length;
    if (nEnd <= idx || nStart >= endIdx) continue;
    const from = Math.max(idx, nStart) - nStart;
    const to = Math.min(endIdx, nEnd) - nStart;
    if (to <= from) continue;
    const range = document.createRange();
    range.setStart(it.node, from);
    range.setEnd(it.node, to);
    const span = document.createElement("span");
    span.className = "src-hl";
    try { range.surroundContents(span); }
    catch (e) { span.appendChild(range.extractContents()); range.insertNode(span); }
    if (!firstSpan) firstSpan = span;
  }
  if (firstSpan) firstSpan.scrollIntoView({ behavior: "smooth", block: "center" });
  else $("#d-read").scrollIntoView({ behavior: "smooth" });
}

async function jumpToSource(slug, chapterNo, snippet) {
  if (!slug) return;
  switchTab("library");
  if (readState.slug !== slug) {
    await openBook(slug);
  }
  if (chapterNo == null) return;
  readState.no = chapterNo;
  readState.lang = "orig";
  await loadChapter();
  setTimeout(() => highlightSnippet(snippet), 80);
}

document.addEventListener("click", (e) => {
  const el = e.target.closest(".cite-link");
  if (!el) return;
  e.preventDefault();
  const slug = el.dataset.slug;
  const ch = el.dataset.ch ? parseInt(el.dataset.ch, 10) : null;
  jumpToSource(slug, ch, el.dataset.snip || "");
});

function linkifyCitations(html, hits, defaultSlug) {
  return html.replace(/【引用:([^】]+)】/g, (m, inner) => {
    inner = String(inner).trim();
    let match = null;
    const libM = inner.match(/^《([^》]+)》[·・]?\s*(.*)$/);
    if (libM) {
      const bookTitle = libM[1], chapTitle = (libM[2] || "").trim();
      match = (hits || []).find((h) =>
        (h.book_title === bookTitle || h.book_slug === bookTitle) &&
        (!chapTitle || h.chapter_title === chapTitle
          || (h.chapter_title && h.chapter_title.includes(chapTitle))));
    } else {
      match = (hits || []).find((h) =>
        h.chapter_title === inner
        || (h.chapter_title && h.chapter_title.includes(inner)));
    }
    if (!match || match.chapter_no == null) return m;
    const slug = match.book_slug || defaultSlug;
    const snip = (match.lines && match.lines[0] && match.lines[0].snippet) || "";
    return `<span class="cite-link" data-slug="${esc(slug)}"`
      + ` data-ch="${match.chapter_no}" data-snip="${esc(snip)}">`
      + `【引用:${esc(inner)}】</span>`;
  });
}

function renderLangBar() {
  const bar = $("#d-langs");
  const has = readState.translations || [];
  if (!has.includes("zh")) { bar.hidden = true; bar.innerHTML = ""; return; }
  const opts = [["orig", "原文"], ["zh", "中文"], ["bi", "中英对照"]];
  bar.hidden = false;
  bar.innerHTML = opts.map(([v, t]) =>
    `<button data-lang="${v}" class="${readState.lang === v ? "on" : ""}">${t}</button>`).join("");
}

async function loadChapter() {
  if (readState.no == null) return;
  const q = readState.lang && readState.lang !== "orig" ? "?lang=" + readState.lang : "";
  try {
    const c = await jget(`/api/books/${encodeURIComponent(readState.slug)}/chapters/${readState.no}${q}`);
    $("#d-read").hidden = false;
    $("#d-read-title").textContent = `ch${String(c.no).padStart(2, "0")} ${c.title}`;
    renderLangBar();
    renderChapter(c.content);
    $("#d-read").scrollIntoView({ behavior: "smooth" });
  } catch (err) { toast("读取失败: " + err.message); }
}

function switchTab(name) {
  $$("#nav button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  $$(".tab").forEach((s) => s.classList.toggle("on", s.id === "tab-" + name));
  clearInterval(jobsTimer); jobsTimer = null;
  if (name === "library") loadBooks();
  if (name === "jobs") { loadJobs(); jobsTimer = setInterval(loadJobs, 2500); }
  if (name === "faq") loadFaq();
  if (name === "admin") loadAdminStats();
}
$("#nav").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (b) switchTab(b.dataset.tab);
});

async function loadBooks() {
  const q = $("#q").value.trim();
  try {
    const rows = await jget("/api/books?q=" + encodeURIComponent(q));
    $("#books").innerHTML = rows.map((b) => `
      <div class="card" data-slug="${esc(b.slug)}">
        ${b.cover ? `<img class="cover" src="${esc(b.cover)}" alt="">`
                  : `<div class="ph">${esc((b.title || "?").slice(0, 2))}</div>`}
        <div class="body">
          <h3>${esc(b.title)}</h3>
          <p class="author">${esc(b.author || "未知作者")}</p>
          <div class="badges">
            <span class="badge">${b.chapter_count || 0} 章</span>
            <span class="badge">${fmtTok(b.token_estimate)} tok</span>
            ${langBadge(b.language)}
            ${b.has_source ? '<span class="badge ok">有源文件</span>' : ""}
          </div>
        </div>
      </div>`).join("");
    $("#books-empty").hidden = rows.length > 0;
  } catch (e) { toast("加载失败: " + e.message); }
}
$("#btn-search").addEventListener("click", loadBooks);
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") loadBooks(); });
$("#books").addEventListener("click", (e) => {
  const c = e.target.closest(".card[data-slug]");
  if (c) openBook(c.dataset.slug);
});

async function openBook(slug) {
  try {
    const b = await jget("/api/books/" + encodeURIComponent(slug));
    $("#detail").hidden = false;
    $("#d-title").textContent = b.title || slug;
    $("#d-meta").textContent = [b.author, b.publisher, b.pages ? b.pages + " 页" : ""]
      .filter(Boolean).join(" · ");
    $("#d-badges").innerHTML = [
      `<span class="badge">${b.chapter_count || 0} 章</span>`,
      `<span class="badge">${fmtTok(b.token_estimate)} tok</span>`,
      langBadge(b.language),
      b.book_type ? `<span class="badge">${esc(b.book_type)}</span>` : "",
      b.depth ? `<span class="badge">${esc(b.depth)}</span>` : "",
      b.model_used ? `<span class="badge">${esc(b.model_used)}</span>` : "",
      b.scan_status === "pass" ? '<span class="badge ok">scan 通过</span>' : "",
    ].join("");
    $("#d-intro").textContent = b.intro || b.description || "";
    const cov = $("#d-cover");
    if (b.cover) { cov.src = b.cover; cov.hidden = false; } else { cov.hidden = true; }
    const src = $("#d-src"), sk = $("#d-skill");
    src.hidden = !b.has_source;
    src.href = `/api/books/${encodeURIComponent(slug)}/download/source`;
    sk.hidden = false;
    sk.href = `/api/books/${encodeURIComponent(slug)}/download/skill`;
    $("#d-process").disabled = !b.has_source;
    $("#d-process").style.display = b.can_process ? "" : "none";
    $("#d-process").dataset.slug = slug;
    const visBtn = $("#d-visibility");
    visBtn.hidden = !b.can_manage;
    visBtn.textContent = b.visibility === "public" ? "设为私有" : "设为公开";
    visBtn.dataset.slug = slug;
    visBtn.dataset.visibility = b.visibility || "";
    readState.slug = slug;
    readState.no = null;
    readState.lang = "orig";
    readState.translations = b.translations || [];
    $("#d-langs").hidden = true;
    $("#d-read").hidden = true;
    $("#d-chapters").innerHTML = (b.chapters || []).map((c) => `
      <li data-no="${c.no}"><span class="no">ch${String(c.no).padStart(2, "0")}</span>
      <span>${esc(c.title)}</span></li>`).join("");
    $("#d-chapters").dataset.slug = slug;
    chatState.slug = slug;
    chatState.sessionId = null;
    $("#d-hits").innerHTML = "";
    $("#d-sessions").value = "";
    loadChatSessions($("#d-sessions"), slug);
    loadBookQA(slug);
  } catch (e) { toast("打开失败: " + e.message); }
}

async function loadBookQA(slug) {
  const faqBox = $("#d-faq"), hotBox = $("#d-hot");
  faqBox.innerHTML = hotBox.innerHTML = '<p class="hint">加载中…</p>';
  try {
    const [faq, hot] = await Promise.all([
      jget("/api/faq?slug=" + encodeURIComponent(slug)),
      jget("/api/hot-questions?slug=" + encodeURIComponent(slug)),
    ]);
    const answered = faq.filter((f) => f.answered).slice(0, 5);
    faqBox.innerHTML = answered.length ? answered.map((f) => `
      <div class="qa-item">
        <div class="qa-q">${esc(f.question)}<span class="cnt">×${f.count}</span></div>
        <div class="qa-a md-body" hidden>${renderMd(f.answer)}</div>
      </div>`).join("")
      : '<p class="hint">暂无已收录的常见问题</p>';
    hotBox.innerHTML = hot.length ? hot.map((h) => `
      <div class="qa-item askable" data-q="${esc(h.question)}">
        <div class="qa-q">${esc(h.question)}<span class="cnt">×${h.count}</span></div>
      </div>`).join("")
      : '<p class="hint">还没有热门问题</p>';
  } catch (e) {
    faqBox.innerHTML = hotBox.innerHTML = '<p class="hint">加载失败</p>';
  }
}

$("#d-faq").addEventListener("click", (e) => {
  const item = e.target.closest(".qa-item");
  if (!item) return;
  const a = item.querySelector(".qa-a");
  if (a) a.hidden = !a.hidden;
});
$("#d-hot").addEventListener("click", (e) => {
  const item = e.target.closest(".qa-item[data-q]");
  if (!item) return;
  $("#d-question").value = item.dataset.q;
  $("#d-question").focus();
  toast("已填入，可直接提问");
});
$("#d-close").addEventListener("click", () => { $("#detail").hidden = true; });
$("#d-process").addEventListener("click", async (e) => {
  const slug = e.target.dataset.slug;
  const lang_mode = $("#d-reproc-lang") ? $("#d-reproc-lang").value : "";
  try {
    const r = await jpost(`/api/books/${encodeURIComponent(slug)}/process`, { lang_mode });
    toast("已加入任务队列 " + r.job_id);
    $("#detail").hidden = true;
    switchTab("jobs");
  } catch (err) { fail(err); }
});
$("#d-visibility").addEventListener("click", async () => {
  const btn = $("#d-visibility");
  const slug = btn.dataset.slug;
  if (!slug) return;
  const next = btn.dataset.visibility === "public" ? "private" : "public";
  btn.disabled = true;
  try {
    const r = await jpost(`/api/books/${encodeURIComponent(slug)}/visibility`, { visibility: next });
    btn.dataset.visibility = r.visibility;
    btn.textContent = r.visibility === "public" ? "设为私有" : "设为公开";
    toast(r.visibility === "public" ? "已设为公开，所有人都能访问" : "已设为私有，仅自己可见");
  } catch (err) { fail(err); }
  finally { btn.disabled = false; }
});
$("#d-chapters").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-no]");
  if (!li) return;
  readState.no = li.dataset.no;
  loadChapter();
});$("#d-langs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-lang]");
  if (!b) return;
  readState.lang = b.dataset.lang;
  loadChapter();
});
// ---- 会话式问答（/api/chat SSE） ----
const chatState = { slug: "", sessionId: null };
const libChatState = { sessionId: null };

async function streamChat(payload, onToken) {
  const resp = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok || !resp.body) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || err.detail || ("HTTP " + resp.status));
  }
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "", done = null;
  for (;;) {
    const { value, done: d } = await reader.read();
    if (d) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop();
    for (const part of parts) {
      const line = part.trim();
      if (!line.startsWith("data:")) continue;
      let obj;
      try { obj = JSON.parse(line.slice(5).trim()); } catch { continue; }
      if (obj.type === "token" && obj.text) onToken(obj.text);
      else if (obj.type === "done") done = obj;
      else if (obj.type === "error") throw new Error(obj.error || "stream error");
    }
  }
  if (!done) throw new Error("stream ended without done");
  return done;
}

function chatHitsHtml(hits, slug) {
  if (!hits || !hits.length) return "";
  return `<div class="chat-hits">` + hits.map((h) => `
    <div class="hit">
      <div class="src">${h.book_title ? "《" + esc(h.book_title) + "》· " : ""}${esc(h.chapter_title || h.path)} · ${h.lines.length} 处匹配</div>
      ${h.lines.map((l) => `<div class="line cite-link" data-slug="${esc(h.book_slug || slug)}"`
        + ` data-ch="${h.chapter_no != null ? h.chapter_no : ""}" data-snip="${esc(l.snippet)}">`
        + `${esc(l.snippet)}</div>`).join("")}
    </div>`).join("") + `</div>`;
}

function renderChatMsgs(box, msgs, defaultSlug) {
  box.innerHTML = (msgs || []).map((m) => {
    if (m.role === "user")
      return `<div class="chat-msg user"><div class="chat-text">${esc(m.content)}</div></div>`;
    const html = linkifyCitations(renderMd(m.content), m.hits || [], defaultSlug);
    return `<div class="chat-msg assistant answer-block"><div class="chat-src">AI 回答</div>`
      + `<div class="line md-body">${html}</div>${chatHitsHtml(m.hits, defaultSlug)}</div>`;
  }).join("");
  box.scrollTop = box.scrollHeight;
}

async function loadChatSessions(sel, slug) {
  try {
    const rows = await jget("/api/chat/sessions?slug=" + encodeURIComponent(slug));
    sel.innerHTML = '<option value="">当前对话</option>' + rows.map((s) =>
      `<option value="${esc(s.id)}">${esc(s.title || "会话")} · ${fmtTime(s.updated_at)}</option>`).join("");
  } catch (e) { /* 未登录：保持只有“当前对话” */ }
}

let bookAskBusy = false;
async function askBookChat() {
  if (bookAskBusy) return;
  bookAskBusy = true;
  const slug = $("#d-chapters").dataset.slug;
  const question = $("#d-question").value.trim();
  if (!question) return toast("先输入问题");
  const deep = $("#d-deep").checked;
  const btn = $("#d-ask");
  btn.textContent = deep ? "深度思考中…" : "回答中…";
  btn.classList.add("busy");
  const box = $("#d-hits");
  try {
    box.insertAdjacentHTML("beforeend",
      `<div class="chat-msg user"><div class="chat-text">${esc(question)}</div></div>`);
    if (deep) {
      const r = await jpost("/api/ask", { slug, question, deep: true });
      box.insertAdjacentHTML("beforeend",
        `<div class="chat-msg assistant answer-block"><div class="chat-src">AI 回答 · 深度模式</div>`
        + `<div class="line md-body">${linkifyCitations(renderMd(r.answer || ""), r.hits || [], slug)}</div>`
        + chatHitsHtml(r.hits, slug) + `</div>`);
    } else {
      box.insertAdjacentHTML("beforeend",
        `<div class="chat-msg assistant answer-block">`
        + `<div class="chat-src">AI 回答 · 流式</div>`
        + `<div class="line md-body" id="stream-body"></div>`
        + `<div class="chat-hits" id="stream-hits" hidden></div></div>`);
      const bubble = box.lastElementChild;
      const bodyEl = bubble.querySelector("#stream-body");
      const hitsEl = bubble.querySelector("#stream-hits");
      const chunks = [];
      let r = null;
      try {
        r = await streamChat({ slug, question, session_id: chatState.sessionId || "" },
          (tok) => { chunks.push(tok); bodyEl.textContent = chunks.join(""); });
      } catch (se) {
        if (chunks.length) throw se;   // 半途失败：保留已流出的文本
        bubble.remove();
        r = await jpost("/api/ask", { slug, question, deep: false });
        box.insertAdjacentHTML("beforeend",
          `<div class="chat-msg assistant answer-block"><div class="chat-src">AI 回答</div>`
          + `<div class="line md-body">${linkifyCitations(renderMd(r.answer || ""), r.hits || [], slug)}</div>`
          + chatHitsHtml(r.hits, slug) + `</div>`);
      }
      if (r && chunks.length) {
        const answer = chunks.join("");
        const hits = r.hits || [];
        bodyEl.innerHTML = linkifyCitations(renderMd(answer), hits, slug);
        if (hits.length) {
          hitsEl.hidden = false;
          hitsEl.innerHTML = hits.map((h) => `
            <div class="hit">
              <div class="src">${esc(h.chapter_title || h.path)} · ${h.lines.length} 处匹配</div>
              ${h.lines.map((l) => `<div class="line cite-link" data-slug="${esc(slug)}"`
                + ` data-ch="${h.chapter_no != null ? h.chapter_no : ""}" data-snip="${esc(l.snippet)}">`
                + `${esc(l.snippet)}</div>`).join("")}
            </div>`).join("");
        }
      }
      bodyEl.removeAttribute("id");
      hitsEl.removeAttribute("id");
      if (r && r.session_id) {
        chatState.sessionId = r.session_id;
        await loadChatSessions($("#d-sessions"), slug);
        $("#d-sessions").value = r.session_id;
      }
    }
    box.scrollTop = box.scrollHeight;
    loadBookQA(slug);
  } catch (e) { fail(e); }
  finally {
    btn.textContent = "提问";
    btn.classList.remove("busy");
    bookAskBusy = false;
  }
}

$("#d-ask").addEventListener("click", askBookChat);
$("#d-question").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#d-ask").click(); });
$("#d-sessions").addEventListener("change", async (e) => {
  const sid = e.target.value;
  const box = $("#d-hits");
  if (!sid) { chatState.sessionId = null; box.innerHTML = ""; return; }
  try {
    const r = await jget("/api/chat/sessions/" + encodeURIComponent(sid));
    chatState.sessionId = sid;
    renderChatMsgs(box, r.messages, chatState.slug);
  } catch (err) { fail(err); }
});
$("#d-newchat").addEventListener("click", () => {
  chatState.sessionId = null;
  $("#d-hits").innerHTML = "";
  $("#d-sessions").value = "";
});

const drop = $("#drop"), fileInput = $("#file");
function setFile(f) {
  if (!f) return;
  uploadFile = f;
  $("#up-name").textContent = `已选择: ${f.name} (${(f.size / 1048576).toFixed(1)} MB)`;
}
drop.addEventListener("click", (e) => { if (e.target === drop) fileInput.click(); });
fileInput.addEventListener("change", () => setFile(fileInput.files[0]));
["dragover", "dragenter"].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.add("over");
}));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.remove("over");
}));
drop.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0]));
function xhrPut(url, file, onPct) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("PUT", url);
    x.upload.onprogress = (ev) => {
      if (ev.lengthComputable) onPct(Math.round((ev.loaded / ev.total) * 100));
    };
    x.onload = () => (x.status >= 200 && x.status < 300 ? resolve()
      : reject(new Error("COS 上传失败 " + x.status)));
    x.onerror = () => reject(new Error("COS 上传网络错误"));
    x.send(file);
  });
}

async function sha256Hex(file) {
  const buf = await file.arrayBuffer();
  const d = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(d))
    .map((b) => b.toString(16).padStart(2, "0")).join("");
}

$("#up-go").addEventListener("click", async (e) => {
  e.preventDefault();
  if (!uploadFile) return toast("先选择 PDF");
  const meta = {
    slug: $("#up-slug").value.trim(),
    book_type: $("#up-type").value,
    depth: $("#up-depth").value,
    translate_zh: $("#up-lang").value !== "",
    lang_mode: $("#up-lang").value,
    model: $("#up-model").value,
  };
  const btn = e.target;
  try {
    let out;
    const pres = await fetch("/api/upload/presign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: uploadFile.name, size: uploadFile.size }),
    }).then((r) => r.json());
    if (pres && pres.mode === "cos") {
      const shaP = sha256Hex(uploadFile);
      await xhrPut(pres.put_url, uploadFile,
        (pct) => { btn.textContent = `上传中 ${pct}%`; });
      const rr = await fetch("/api/upload/complete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          key: pres.key, filename: uploadFile.name,
          sha256: await shaP, ...meta,
        }),
      });
      if (!rr.ok) throw new Error((await rr.json().catch(() => ({}))).error || rr.status);
      out = await rr.json();
    } else {
      const fd = new FormData();
      fd.append("file", uploadFile);
      fd.append("slug", meta.slug);
      fd.append("book_type", meta.book_type);
      fd.append("depth", meta.depth);
      fd.append("translate_zh", meta.translate_zh ? "1" : "");
      fd.append("lang_mode", meta.lang_mode || "");
      fd.append("model", meta.model || "");
      const r = await fetch("/api/upload", { method: "POST", body: fd });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
      out = await r.json();
    }
    if (out.duplicate) {
      toast(`检测到相同书籍《${out.title || out.slug}》，已秒传复用`);
      uploadFile = null; fileInput.value = "";
      $("#up-name").textContent = ""; $("#up-slug").value = "";
      switchTab("library");
      return;
    }
    toast(`已上传，任务 ${out.job_id} 已排队`);
    uploadFile = null; fileInput.value = "";
    $("#up-name").textContent = ""; $("#up-slug").value = "";
    switchTab("jobs");
  } catch (err) { fail(err); }
  finally { btn.textContent = "上传并开始转换"; }
});

async function loadJobs() {
  try {
    const rows = await jget("/api/jobs");
    $("#jobs tbody").innerHTML = rows.map((r) => {
      const st = { queued: "排队中", running: "转换中", success: "完成", failed: "失败" }[r.state] || r.state;
      const cls = { queued: "warn", running: "run", success: "ok", failed: "err" }[r.state] || "";
      return `<tr data-id="${esc(r.id)}">
        <td>${fmtTime(r.created_at)}</td>
        <td>${esc(r.id)} <span class="muted">${esc(r.kind)}</span></td>
        <td>${esc(r.slug || "-")}</td>
        <td><span class="badge ${cls}">${st}</span></td>
        <td>${fmtDur(r.elapsed_sec)}</td>
        <td>${fmtTok(r.token_actual)}${r.token_estimate ? " / " + fmtTok(r.token_estimate) : ""}</td>
        <td>${esc(r.model || "-")}</td>
        <td title="${esc(r.error || r.detail || "")}">${esc(r.error || r.detail || "-")}</td>
      </tr>`;
    }).join("");
  } catch (e) { /* 轮询失败静默 */ }
}
$("#btn-jobs-refresh").addEventListener("click", loadJobs);
$("#jobs tbody").addEventListener("click", async (e) => {
  const tr = e.target.closest("tr[data-id]");
  if (!tr) return;
  try {
    const r = await fetch(`/api/jobs/${tr.dataset.id}/log`);
    const log = $("#job-log");
    if (r.ok) { log.textContent = await r.text(); log.hidden = false; }
    else toast("暂无日志");
  } catch (err) { toast("日志读取失败"); }
});

async function loadFaq() {
  try {
    const books = await jget("/api/books");
    const sel = $("#faq-scope");
    const cur = sel.value;
    sel.innerHTML = '<option value="">全部书籍</option>' +
      books.map((b) => `<option value="${esc(b.slug)}">${esc(b.title)}</option>`).join("");
    sel.value = cur;
    const rows = await jget("/api/faq?slug=" + encodeURIComponent(sel.value));
    $("#faq-list").innerHTML = rows.length ? rows.map((f) => `
      <div class="item">
        <div class="q">${esc(f.question)}<span class="cnt">×${f.count}</span>
          <span class="muted">${esc(f.scope_slug || "全库")}</span></div>
        ${f.answer ? `<div class="a">${esc(f.answer)}</div>` : `
        <div class="faq-actions">
          <button class="ghost" data-gen="1" data-q="${esc(f.question)}"
                  data-slug="${esc(f.scope_slug)}">生成答案</button>
          <button class="ghost" data-edit="1" data-q="${esc(f.question)}"
                  data-slug="${esc(f.scope_slug)}">手动补充</button>
        </div>`}
      </div>`).join("")
      : '<p class="empty">还没有记录的提问</p>';
  } catch (e) { toast("加载失败: " + e.message); }
}
let libAskBusy = false;
async function askLibrary() {
  if (libAskBusy) return;
  libAskBusy = true;
  const q = $("#lib-question").value.trim();
  if (!q) return toast("先输入问题");
  const deep = $("#lib-deep").checked;
  const btn = $("#lib-ask");
  btn.textContent = deep ? "深度检索中…" : "检索中…";
  btn.classList.add("busy");
  const box = $("#lib-result");
  try {
    box.insertAdjacentHTML("beforeend",
      `<div class="chat-msg user"><div class="chat-text">${esc(q)}</div></div>`);
    if (deep) {
      const r = await jpost("/api/library/ask", { question: q, deep });
      box.insertAdjacentHTML("beforeend",
        `<div class="chat-msg assistant answer-block"><div class="chat-src">跨书回答 · 深度模式</div>`
        + `<div class="line md-body">${linkifyCitations(renderMd(r.answer || ""), r.hits || [], "")}</div>`
        + chatHitsHtml(r.hits, "") + `</div>`);
    } else {
      box.insertAdjacentHTML("beforeend",
        `<div class="chat-msg assistant answer-block">`
        + `<div class="chat-src">跨书回答 · 流式</div>`
        + `<div class="line md-body" id="stream-body"></div>`
        + `<div class="chat-hits" id="stream-hits" hidden></div></div>`);
      const bubble = box.lastElementChild;
      const bodyEl = bubble.querySelector("#stream-body");
      const hitsEl = bubble.querySelector("#stream-hits");
      const chunks = [];
      let r = null;
      try {
        r = await streamChat({ slug: "", question: q, session_id: libChatState.sessionId || "" },
          (tok) => { chunks.push(tok); bodyEl.textContent = chunks.join(""); });
      } catch (se) {
        if (chunks.length) throw se;
        bubble.remove();
        r = await jpost("/api/library/ask", { question: q, deep: false });
        box.insertAdjacentHTML("beforeend",
          `<div class="chat-msg assistant answer-block"><div class="chat-src">跨书回答</div>`
          + `<div class="line md-body">${linkifyCitations(renderMd(r.answer || ""), r.hits || [], "")}</div>`
          + chatHitsHtml(r.hits, "") + `</div>`);
      }
      if (r && chunks.length) {
        const answer = chunks.join("");
        const hits = r.hits || [];
        bodyEl.innerHTML = linkifyCitations(renderMd(answer), hits, "");
        if (hits.length) {
          hitsEl.hidden = false;
          hitsEl.innerHTML = hits.map((h) => `
            <div class="hit">
              <div class="src">《${esc(h.book_title || h.book_slug)}》· ${esc(h.chapter_title || h.path)} · ${h.lines.length} 处匹配</div>
              ${h.lines.map((l) => `<div class="line cite-link" data-slug="${esc(h.book_slug || "")}"`
                + ` data-ch="${h.chapter_no != null ? h.chapter_no : ""}" data-snip="${esc(l.snippet)}">`
                + `${esc(l.snippet)}</div>`).join("")}
            </div>`).join("");
        }
      }
      bodyEl.removeAttribute("id");
      hitsEl.removeAttribute("id");
      if (r && r.session_id) {
        libChatState.sessionId = r.session_id;
        await loadChatSessions($("#lib-sessions"), "");
        $("#lib-sessions").value = r.session_id;
      }
    }
    box.scrollTop = box.scrollHeight;
    loadFaq();
  } catch (e) {
    fail(e);
  } finally {
    btn.textContent = "问书库";
    btn.classList.remove("busy");
    libAskBusy = false;
  }
}
$("#lib-ask").addEventListener("click", askLibrary);
$("#lib-question").addEventListener("keydown", (e) => {
  if (e.key === "Enter") askLibrary();
});
$("#lib-sessions").addEventListener("change", async (e) => {
  const sid = e.target.value;
  const box = $("#lib-result");
  if (!sid) { libChatState.sessionId = null; box.innerHTML = ""; return; }
  try {
    const r = await jget("/api/chat/sessions/" + encodeURIComponent(sid));
    libChatState.sessionId = sid;
    renderChatMsgs(box, r.messages, "");
  } catch (err) { fail(err); }
});
$("#lib-newchat").addEventListener("click", () => {
  libChatState.sessionId = null;
  $("#lib-result").innerHTML = "";
  $("#lib-sessions").value = "";
});
$("#faq-scope").addEventListener("change", loadFaq);$("#faq-list").addEventListener("click", async (e) => {
  const gen = e.target.closest("button[data-gen]");
  const edit = e.target.closest("button[data-edit]");
  if (gen) {
    gen.textContent = "生成中…";
    gen.classList.add("busy");
    try {
      await jpost("/api/faq/answer", { scope_slug: gen.dataset.slug, question: gen.dataset.q });
      toast("已生成答案");
      loadFaq();
    } catch (err) {
      gen.textContent = "生成答案";
      gen.classList.remove("busy");
      fail(err);
    }
  } else if (edit) {
    const ans = prompt("手动补充答案:", "");
    if (!ans) return;
    try {
      await jpost("/api/faq", { scope_slug: edit.dataset.slug, question: edit.dataset.q, answer: ans });
      toast("已保存");
      loadFaq();
    } catch (err) { fail(err); }
  }
});

async function loadAdminStats() {
  const box = $("#admin-stats");
  const days = parseInt($("#admin-days").value, 10) || 7;
  box.innerHTML = '<p class="hint">加载中…</p>';
  try {
    const s = await jget("/api/admin/stats?days=" + days);
    const fmtMs = (v) => v == null ? "—" : (v >= 1000 ? (v / 1000).toFixed(1) + "s" : v + "ms");
    const row = (a) => `<tr><td>${esc(a.day || a.model)}</td><td>${a.count}</td>`
      + `<td>${a.ok}</td><td>${(a.ok_rate * 100).toFixed(1)}%</td>`
      + `<td>${fmtMs(a.p50_ms)}</td><td>${fmtMs(a.p95_ms)}</td></tr>`;
    const tbl = (rows) => `<table class="stat-table"><thead><tr>`
      + `<th>${days ? "日期" : ""}</th><th>调用</th><th>成功</th><th>成功率</th>`
      + `<th>P50</th><th>P95</th></tr></thead><tbody>`
      + rows.map(row).join("") + `</tbody></table>`;
    box.innerHTML = `
      <div class="stat-summary">
        <div class="stat-card"><div class="stat-num">${s.total}</div><div class="stat-label">${days} 天调用</div></div>
        <div class="stat-card"><div class="stat-num">${(s.ok_rate * 100).toFixed(1)}%</div><div class="stat-label">成功率</div></div>
        <div class="stat-card"><div class="stat-num">${s.ok}</div><div class="stat-label">成功回答</div></div>
      </div>
      <div class="qa-col"><div class="qa-title">按模型</div>${(s.by_model || []).length ? tbl(s.by_model) : '<p class="hint">暂无数据</p>'}</div>
      <div class="qa-col"><div class="qa-title">按天</div>${(s.by_day || []).length ? tbl(s.by_day) : '<p class="hint">暂无数据</p>'}</div>`;
  } catch (e) {
    box.innerHTML = `<p class="hint">加载失败: ${esc(e.message)}</p>`;
  }
}
$("#admin-days").addEventListener("change", loadAdminStats);

fetch("/api/me")
  .then((r) => (r.ok ? r.json() : null))
  .then((u) => { me = u || null; renderAuth(); })
  .catch(() => {});
loadBooks();
