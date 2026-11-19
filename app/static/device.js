/* Pagina del dispositivo: stato della macchina letto dall'in-out attraverso il proxy remoto
 * (GET /__remote/info/<id> sulla porta dell'accesso remoto, con il cookie del pannello). */
(function () {
  "use strict";
  const box = document.getElementById("machine");
  if (!box) return;
  const kv = document.getElementById("machine-kv");
  const state = document.getElementById("machine-state");
  const url = box.dataset.infoUrl;

  const size = (n) => {
    if (n == null) return "–";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
  };
  const duration = (s) => {
    if (s == null) return "–";
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d} g ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
  };
  const usage = (o) => o && o.total ? `${size(o.used)} di ${size(o.total)} (${Math.round(o.percent)}%)` : "–";

  function sshText(ssh, enabledOnBroker) {
    if (!enabledOnBroker) return ["terminale disattivato sul broker (SSH_ENABLED=0)", "warn"];
    if (!ssh) return ["non disponibile su questa versione dell'in-out", "warn"];
    if (!ssh.supported) return ["sistema operativo non supportato", "bad"];
    if (!ssh.enabled) return ["disattivato nelle impostazioni dell'in-out", "warn"];
    if (ssh.installing) return ["installazione di OpenSSH Server in corso", "warn"];
    if (!ssh.listening) return [`server SSH non attivo sulla porta ${ssh.port}: va installato o avviato sull'in-out`, "bad"];
    return [`pronto · utente ${ssh.user}`, "ok"];
  }

  function row(label, value, cls) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    if (cls) {
      const b = document.createElement("span");
      b.className = `badge ${{ ok: "on", bad: "closed", warn: "warn" }[cls]}`;
      b.textContent = cls === "ok" ? "ok" : cls === "bad" ? "no" : "attenzione";
      dd.append(b, " ");
    }
    dd.append(value);
    kv.append(dt, dd);
  }

  async function load() {
    state.hidden = false;
    state.textContent = "Lettura dello stato dall'in-out…";
    try {
      const r = await fetch(url, { credentials: "include", cache: "no-store" });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || `errore ${r.status}`);
      const d = j.data || {};
      kv.replaceChildren();
      row("Versione in-out", d.version || "–");
      row("Sistema", `${d.os || "–"} · ${d.hostname || ""}`);
      row("Acceso da", duration(d.uptime));
      row("Servizio in-out attivo da", duration(d.service_uptime));
      row("CPU", d.cpu_percent != null ? `${Math.round(d.cpu_percent)}% · ${d.cpu_count} core` : "–");
      row("Memoria", usage(d.memory));
      row("Disco", usage(d.disk), d.disk && d.disk.percent > 90 ? "bad" : null);
      const [text, cls] = sshText(d.ssh, j.ssh_enabled);
      row("Terminale SSH", text, cls);
      row("Accesso remoto web", d.remote_access === false ? "disattivato sull'in-out" : "attivo");
      kv.hidden = false;
      state.hidden = true;
    } catch (e) {
      kv.hidden = true;
      state.textContent = `Stato non disponibile: ${e.message}`;
    }
  }

  document.getElementById("machine-refresh").addEventListener("click", load);
  load();
  setInterval(() => { if (!document.hidden) load(); }, 60000);
})();
