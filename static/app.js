// Data Copilot — the UI layer. It displays what the backend sends; it remembers nothing itself.
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { user: null, conversations: [], current: null, image: null, streaming: false,
    datasets: [], datasetId: null, meta: { added: 0, max_added: 3, max_mb: 25 } };
  const activeDataset = () => state.datasets.find((d) => d.id === state.datasetId) || null;
  const hasInvoices = () => { const d = activeDataset(); return !!d && d.features.includes("invoices"); };

  // ---------- helpers ----------
  async function api(path, options = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, credentials: "same-origin", ...options });
    if (res.status === 401) { showLogin(); throw new Error("Please sign in."); }
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json();
  }
  function render(md) {
    if (window.marked) { try { return marked.parse(md, { breaks: true }); } catch (_) {} }
    const div = document.createElement("div"); div.textContent = md; return div.innerHTML.replace(/\n/g, "<br>");
  }
  function escapeHtml(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
  function setStatus(text) { $("status").textContent = text || ""; }
  function scrollDown() { const m = $("messages"); m.scrollTop = m.scrollHeight; }

  // ---------- auth ----------
  function showLogin() { $("login").classList.remove("hidden"); $("app").classList.add("hidden"); }
  function showApp() { $("login").classList.add("hidden"); $("app").classList.remove("hidden"); $("who").textContent = state.user ? `signed in as ${state.user.name}` : ""; }

  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("login-error").textContent = "";
    try {
      state.user = await api("/api/login", { method: "POST", body: JSON.stringify({ name: $("login-name").value.trim(), password: $("login-password").value }) });
      $("login-password").value = "";
      showApp();
      await loadDatasets();
    } catch (err) { $("login-error").textContent = err.message; }
  });
  $("logout").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }).catch(() => {}); state.user = null; state.current = null; state.datasetId = null; showLogin(); });

  // ---------- conversations ----------
  async function loadConversations() {
    state.conversations = await api(conversationsUrl());
    renderConversations();
    if (state.current) await openConversation(state.current); else renderEmpty();
  }
  function conversationsUrl() { return state.datasetId ? `/api/conversations?dataset_id=${state.datasetId}` : "/api/conversations"; }
  function renderConversations() {
    const nav = $("conversations"); nav.innerHTML = "";
    for (const c of state.conversations) {
      const b = document.createElement("button");
      b.textContent = c.title; b.title = c.title;
      if (c.id === state.current) b.classList.add("active");
      b.addEventListener("click", () => openConversation(c.id));
      nav.appendChild(b);
    }
  }
  async function openConversation(id) {
    state.current = id;
    const conv = state.conversations.find((c) => c.id === id);
    $("conv-title").textContent = conv ? conv.title : "Chat";
    renderConversations();
    const messages = await api(`/api/conversations/${id}/messages`);
    const box = $("messages"); box.innerHTML = "";
    for (const m of messages) addBubble(m.role, m.content, { hasImage: m.has_image, time: m.created_at });
    if (!messages.length) renderEmpty();
    scrollDown();
  }
  function starterQuestions(d) {
    if (!d) return [];
    if (d.starters && d.starters.length) return d.starters;
    const t = d.tables[0];
    const qs = ["What columns does this dataset have?"];
    if (t) { qs.push(`How many rows are in ${t.name}?`, `Show me 5 example rows of ${t.name}`); }
    return qs;
  }
  function renderEmpty() {
    const d = activeDataset();
    const lead = d ? `Ask anything about ${escapeHtml(d.name)}${hasInvoices() ? ", or attach an invoice photo" : ""}.` : "Ask about your data.";
    const hints = starterQuestions(d).map((q) => `<span class="hint" data-q="${escapeHtml(q)}">${escapeHtml(q)}</span>`).join("");
    $("messages").innerHTML = `<div class="empty"><p>${lead}</p>${hints}</div>`;
    $("messages").querySelectorAll(".hint").forEach((h) => h.addEventListener("click", () => { $("input").value = h.dataset.q; $("input").focus(); }));
  }
  $("new-chat").addEventListener("click", () => { state.current = null; $("conv-title").textContent = "New chat"; renderConversations(); renderEmpty(); $("input").focus(); });

  // ---------- messages ----------
  function addBubble(role, text, { hasImage = false, imageDataUrl = null, time = null } = {}) {
    const div = document.createElement("div");
    div.className = `msg ${role}`;
    const who = role === "user" ? (state.user ? state.user.name : "You") : "Data Copilot";
    const initial = role === "user" ? who.trim().charAt(0).toUpperCase() : "";
    const when = time ? new Date(time) : new Date();
    const stamp = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    let inner = "";
    if (imageDataUrl) inner += `<img class="img" src="${imageDataUrl}" alt="attached invoice">`;
    else if (hasImage) inner += `<span class="chip">📎 image</span><br>`;
    inner += role === "user" ? `<p>${escapeHtml(text).replace(/\n/g, "<br>")}</p>` : render(text);
    div.innerHTML = `<div class="avatar ${role}">${initial}</div><div class="msg-body"><div class="msg-head"><span class="author">${escapeHtml(who)}</span>${role === "assistant" ? '<span class="tag">BOT</span>' : ""}<span class="time">${stamp}</span></div><div class="body">${inner}</div></div>`;
    $("messages").appendChild(div);
    scrollDown();
    return div;
  }

  // ---------- image attach ----------
  $("image").addEventListener("change", () => {
    const file = $("image").files[0];
    if (!file) { state.image = null; $("attachment").classList.add("hidden"); return; }
    if (file.size > 5 * 1024 * 1024) { alert("Image is larger than 5 MB."); $("image").value = ""; return; }
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = reader.result;
      state.image = { media_type: file.type, data: dataUrl.split(",")[1], dataUrl, name: file.name };
      $("attachment").textContent = `Attached: ${file.name}`; $("attachment").classList.remove("hidden");
    };
    reader.readAsDataURL(file);
  });

  // ---------- send + stream ----------
  $("input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); } });
  $("input").addEventListener("input", () => { const t = $("input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 160) + "px"; });

  $("composer").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (state.streaming) return;
    const text = $("input").value.trim();
    if (!text && !state.image) return;
    const image = state.image;
    $("input").value = ""; $("input").style.height = "auto"; state.image = null; $("image").value = ""; $("attachment").classList.add("hidden");
    if ($("messages").querySelector(".empty")) $("messages").innerHTML = "";
    addBubble("user", text || "(attachment)", { imageDataUrl: image ? image.dataUrl : null });

    const bot = addBubble("assistant", "");
    const body = bot.querySelector(".body"); body.classList.add("cursor");
    const meta = document.createElement("div"); meta.className = "meta"; bot.querySelector(".msg-body").appendChild(meta);
    state.streaming = true; $("send").disabled = true; setStatus("thinking…");
    let full = "";
    try {
      const res = await fetch("/api/chat", {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: state.current, dataset_id: state.datasetId, message: text, image: image ? { media_type: image.media_type, data: image.data } : null }),
      });
      if (!res.ok) { let d = res.statusText; try { d = (await res.json()).detail || d; } catch (_) {} throw new Error(d); }
      const reader = res.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
      while (true) {
        const { value, done } = await reader.read(); if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          const raw = buffer.slice(0, idx); buffer = buffer.slice(idx + 2);
          const line = raw.split("\n").find((l) => l.startsWith("data: ")); if (!line) continue;
          const ev = JSON.parse(line.slice(6));
          if (ev.type === "meta") { state.current = ev.conversation_id; if (ev.title) $("conv-title").textContent = ev.title; }
          else if (ev.type === "text") { full += ev.text; body.innerHTML = render(full); scrollDown(); }
          else if (ev.type === "tool") { const c = document.createElement("span"); c.className = `chip tool ${ev.status}`; c.textContent = `${ev.status === "start" ? "⚙" : "✓"} ${ev.name}${ev.summary ? ": " + ev.summary : ""}`; meta.appendChild(c); setStatus(`using ${ev.name}…`); }
          else if (ev.type === "done") { const u = ev.usage || {}; const c = document.createElement("span"); c.className = "chip"; c.textContent = `${u.input_tokens || 0} in · ${u.output_tokens || 0} out · $${(ev.cost_usd || 0).toFixed(4)}`; meta.appendChild(c); }
          else if (ev.type === "error") { full += `\n\n**Error:** ${ev.message}`; body.innerHTML = render(full); }
        }
      }
    } catch (err) {
      full += `\n\n**Error:** ${err.message}`; body.innerHTML = render(full);
    } finally {
      body.classList.remove("cursor"); state.streaming = false; $("send").disabled = false; setStatus("");
      await loadConversationsQuiet();
      renderTabs();
    }
  });
  async function loadConversationsQuiet() { try { state.conversations = await api(conversationsUrl()); renderConversations(); } catch (_) {} }

  // ---------- datasets (tabs) ----------
  async function loadDatasets() {
    const res = await api("/api/datasets");
    state.datasets = res.datasets;
    state.meta = { added: res.added, max_added: res.max_added, max_mb: res.max_mb, max_rows: res.max_rows };
    if (!state.datasets.some((d) => d.id === state.datasetId)) state.datasetId = res.default_id;
    renderTabs();
    updateFeatureUi();
    await loadConversations();
  }
  function renderTabs() {
    const nav = $("tabs"); nav.innerHTML = "";
    for (const d of state.datasets) {
      const b = document.createElement("button");
      b.className = "tab" + (d.id === state.datasetId ? " active" : "");
      b.title = d.description || d.name;
      b.append(d.name);
      if (d.deletable) {
        const x = document.createElement("span");
        x.className = "x"; x.textContent = "✕"; x.title = "Delete this dataset";
        x.addEventListener("click", (e) => { e.stopPropagation(); deleteDataset(d); });
        b.appendChild(x);
      }
      b.addEventListener("click", () => selectDataset(d.id));
      nav.appendChild(b);
    }
    const add = document.createElement("button");
    add.className = "tab add";
    const full = state.meta.added >= state.meta.max_added;
    add.textContent = "+ Add dataset";
    const count = document.createElement("small"); count.textContent = `${state.meta.added}/${state.meta.max_added}`; add.appendChild(count);
    add.disabled = full || state.streaming;
    add.title = full ? `You already have ${state.meta.max_added} added datasets. Delete one to add another.` : "Upload a file or paste a link";
    add.addEventListener("click", openAddDialog);
    nav.appendChild(add);
  }
  function updateFeatureUi() {
    const d = activeDataset();
    $("attach").classList.toggle("hidden", !hasInvoices());
    $("toggle-invoices").classList.toggle("hidden", !hasInvoices());
    if (!hasInvoices()) { $("invoices").classList.add("hidden"); state.image = null; $("image").value = ""; $("attachment").classList.add("hidden"); }
    $("input").placeholder = d ? `Ask about ${d.name}${hasInvoices() ? ", or attach an invoice" : ""}…` : "Ask about your data…";
  }
  async function selectDataset(id) {
    if (state.streaming || id === state.datasetId) return;
    state.datasetId = id; state.current = null;
    $("conv-title").textContent = "New chat";
    renderTabs(); updateFeatureUi();
    await loadConversations();
  }
  async function deleteDataset(d) {
    if (!confirm(`Delete "${d.name}" and all chats about it? This cannot be undone.`)) return;
    try { await api(`/api/datasets/${d.id}`, { method: "DELETE" }); } catch (err) { alert(err.message); return; }
    if (state.datasetId === d.id) { state.datasetId = null; state.current = null; $("conv-title").textContent = "New chat"; }
    await loadDatasets();
  }

  // ---------- add dataset dialog ----------
  let addMode = "file";
  function setAddMode(mode) {
    addMode = mode;
    $("mode-file").classList.toggle("active", mode === "file"); $("mode-link").classList.toggle("active", mode === "link");
    $("file-field").classList.toggle("hidden", mode !== "file"); $("link-field").classList.toggle("hidden", mode !== "link");
    $("add-error").textContent = "";
  }
  function openAddDialog() {
    $("add-form").reset(); setAddMode("file");
    $("add-hint").textContent = `Up to ${state.meta.max_mb} MB and ${(state.meta.max_rows || 200000).toLocaleString()} rows. ${state.meta.max_added - state.meta.added} of ${state.meta.max_added} slots left. Google Sheets links work if the sheet is shared as "anyone with the link".`;
    $("add-dataset").classList.remove("hidden");
  }
  const closeAddDialog = () => $("add-dataset").classList.add("hidden");
  $("mode-file").addEventListener("click", () => setAddMode("file"));
  $("mode-link").addEventListener("click", () => setAddMode("link"));
  $("add-cancel").addEventListener("click", closeAddDialog);
  $("add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("add-error").textContent = "";
    const name = $("add-name").value.trim();
    const submit = $("add-submit"); submit.disabled = true; submit.textContent = "Loading…";
    try {
      let created;
      if (addMode === "file") {
        const file = $("add-file").files[0];
        if (!file) throw new Error("Choose a CSV or Excel file first.");
        if (file.size > state.meta.max_mb * 1024 * 1024) throw new Error(`That file is larger than ${state.meta.max_mb} MB.`);
        const res = await fetch(`/api/datasets/upload?filename=${encodeURIComponent(file.name)}&name=${encodeURIComponent(name)}`,
          { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/octet-stream" }, body: file });
        if (!res.ok) { let d = res.statusText; try { d = (await res.json()).detail || d; } catch (_) {} throw new Error(d); }
        created = await res.json();
      } else {
        const url = $("add-url").value.trim();
        if (!url) throw new Error("Paste a link first.");
        created = await api("/api/datasets/link", { method: "POST", body: JSON.stringify({ url, name }) });
      }
      closeAddDialog();
      state.datasetId = created.id; state.current = null; $("conv-title").textContent = "New chat";
      await loadDatasets();
    } catch (err) { $("add-error").textContent = err.message; }
    finally { submit.disabled = false; submit.textContent = "Add dataset"; }
  });

  // ---------- invoices panel ----------
  async function loadInvoices() {
    const rows = await api("/api/invoices");
    $("invoice-rows").innerHTML = rows.map((r) => `<tr><td>${r.invoice_date || (r.created_at || "").slice(0, 10)}</td><td>${escapeHtml(r.supplier)}<br><small>${escapeHtml(r.invoice_number || "")}</small></td><td>${r.amount == null ? "—" : r.amount.toLocaleString() + " " + r.currency}</td><td><span class="badge ${r.status}">${r.status.replace(/_/g, " ")}</span>${r.flagged ? '<span class="badge flag">flag</span>' : ""}</td></tr>`).join("") || `<tr><td colspan="4"><small>No invoices yet.</small></td></tr>`;
  }
  $("toggle-invoices").addEventListener("click", async () => { $("invoices").classList.toggle("hidden"); if (!$("invoices").classList.contains("hidden")) await loadInvoices(); });
  $("close-invoices").addEventListener("click", () => $("invoices").classList.add("hidden"));

  // ---------- theme ----------
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  const currentTheme = () => document.documentElement.dataset.theme || (systemDark.matches ? "dark" : "light");
  function showTheme() { $("toggle-theme").textContent = currentTheme() === "dark" ? "☀ Light" : "☾ Dark"; }
  $("toggle-theme").addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (_) {}
    showTheme();
  });
  systemDark.addEventListener("change", showTheme);
  showTheme();

  // ---------- boot ----------
  (async () => {
    try { state.user = await api("/api/me"); showApp(); await loadDatasets(); }
    catch (_) { showLogin(); }
  })();
})();
