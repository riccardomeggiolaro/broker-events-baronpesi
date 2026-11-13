// Righe cliccabili e conferme sui form distruttivi
document.addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-href]");
  if (row && !e.target.closest("a, button, form, input")) location.href = row.dataset.href;
});
document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString("it-IT"); };
const fmtSize = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;

// Aggiorna i badge online/offline nelle pagine con elementi [data-presence]
function paintPresence(online) {
  document.querySelectorAll("[data-presence]").forEach((el) => {
    if (el.dataset.channel === "0") return;
    const on = Object.prototype.hasOwnProperty.call(online, el.dataset.presence);
    el.innerHTML = on ? '<span class="badge on">online</span>' : '<span class="badge off">offline</span>';
  });
}

if (document.querySelector("[data-presence]") && !window.location.pathname.startsWith("/live")) {
  const refresh = () => fetch("/api/status").then((r) => r.ok ? r.json() : null).then((s) => {
    if (!s) return;
    paintPresence(s.online);
    const st = document.getElementById("st-online");
    if (st) st.innerHTML = `${s.devices_online}<span class="of">/ ${s.devices}</span>`;
  }).catch(() => {});
  setInterval(refresh, 5000);
}

// Monitor live
function startLive() {
  const tbody = document.querySelector("#live-table tbody");
  const state = document.getElementById("ws-state");
  const flt = document.getElementById("flt-device");
  const showPresence = document.getElementById("show-presence");
  const btnPause = document.getElementById("btn-pause");
  const onlineList = document.getElementById("online-list");
  const names = window.DEVICE_NAMES || {};
  const online = {};
  let paused = false;
  const buffer = [];
  const MAX_ROWS = 300;

  const name = (id) => id ? (names[id] || id) : "—";

  function renderOnline() {
    const ids = Object.keys(online).sort();
    onlineList.innerHTML = ids.length ? ids.map((id) =>
      `<li><a href="/devices/${encodeURIComponent(id)}">${esc(name(id))}</a>
           <span class="muted small">${esc(online[id].ip || "")} · dalle ${fmtTime(online[id].since)}</span></li>`
    ).join("") : '<li class="empty">Nessun dispositivo collegato.</li>';
  }

  function rowFor(ev) {
    const tr = document.createElement("tr");
    tr.dataset.device = ev.device_id || "";
    tr.dataset.kind = ev.type;
    if (ev.type === "message") {
      tr.className = "clickable flash-new";
      tr.dataset.href = `/messages/${ev.id}`;
      const files = (ev.files || []).map((f) => f.kind).reduce((a, k) => (a[k] = (a[k] || 0) + 1, a), {});
      const filesTxt = Object.entries(files).map(([k, n]) => `${n} ${k}`).join(", ");
      tr.innerHTML = `<td class="nowrap">${fmtTime(ev.received_at)}</td>
        <td>${esc(name(ev.device_id))}</td>
        <td><span class="ev ev-message">messaggio</span></td>
        <td><a href="/messages/${ev.id}">${esc(ev.event_type || ev.topic)}</a>
            ${ev.weigher_name ? ` · ${esc(ev.weigher_name)}` : ""}
            ${filesTxt ? `<div class="muted small">${esc(filesTxt)}</div>` : ""}</td>
        <td class="num nowrap">${fmtSize(ev.size)}</td>`;
    } else {
      tr.className = "flash-new";
      const label = { connect: "connesso", disconnect: "disconnesso", denied: "rifiutato", admin: "admin" }[ev.kind] || ev.kind;
      tr.innerHTML = `<td class="nowrap">${fmtTime(ev.ts)}</td>
        <td>${esc(name(ev.device_id))}</td>
        <td><span class="ev ev-${esc(ev.kind)}">${esc(label)}</span></td>
        <td>${esc(ev.detail)}</td><td></td>`;
    }
    return tr;
  }

  function visible(tr) {
    if (flt.value && tr.dataset.device !== flt.value) return false;
    if (!showPresence.checked && tr.dataset.kind === "presence") return false;
    return true;
  }
  const applyFilter = () => tbody.querySelectorAll("tr[data-kind]").forEach((tr) => { tr.hidden = !visible(tr); });
  flt.addEventListener("change", applyFilter);
  showPresence.addEventListener("change", applyFilter);

  function add(ev) {
    tbody.querySelector(".placeholder")?.remove();
    const tr = rowFor(ev);
    tr.hidden = !visible(tr);
    tbody.prepend(tr);
    while (tbody.children.length > MAX_ROWS) tbody.lastElementChild.remove();
  }

  btnPause.addEventListener("click", () => {
    paused = !paused;
    btnPause.textContent = paused ? `Riprendi` : "Pausa";
    if (!paused) { buffer.splice(0).forEach(add); }
  });
  document.getElementById("btn-clear").addEventListener("click", () => {
    tbody.innerHTML = '<tr class="placeholder"><td colspan="5" class="empty">In attesa di messaggi…</td></tr>';
  });

  function connect() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`);
    ws.onopen = () => { state.textContent = "in ascolto"; state.className = "pill ok"; };
    ws.onclose = (e) => {
      state.textContent = "disconnesso, riprovo…"; state.className = "pill bad";
      if (e.code === 4401) { location.href = "/login"; return; }
      setTimeout(connect, 2000);
    };
    ws.onmessage = (m) => {
      const ev = JSON.parse(m.data);
      if (ev.type === "hello") {
        Object.keys(online).forEach((k) => delete online[k]);
        Object.assign(online, ev.online);
        renderOnline();
        return;
      }
      if (ev.type === "presence") {
        if (ev.kind === "connect" && ev.device_id) online[ev.device_id] = { since: ev.ts, ip: (ev.detail || "").replace(/^da /, "") };
        if (ev.kind === "disconnect" && ev.device_id) delete online[ev.device_id];
        if (ev.kind === "admin" && !ev.device_id) Object.keys(online).forEach((k) => delete online[k]);
        renderOnline();
      }
      if (paused) { buffer.push(ev); btnPause.textContent = `Riprendi (${buffer.length})`; return; }
      add(ev);
    };
  }
  connect();
}
