/* Pannello file della pagina del terminale: SFTP sull'in-out attraverso il broker.
 * GET  /__remote/files/<id>/list?path=      elenco della cartella (default: home dell'utente)
 * GET  /__remote/files/<id>/download?path=  download
 * POST /__remote/files/<id>/upload?path=    upload (multipart, campo "file") nella cartella */
(function () {
  "use strict";
  var cfg = window.__terminal;
  var base = "/__remote/files/" + encodeURIComponent(cfg.device);
  var panel = document.getElementById("files");
  var toggle = document.getElementById("files-toggle");
  var pathInput = document.getElementById("files-path");
  var list = document.getElementById("files-list");
  var state = document.getElementById("files-state");
  var upload = document.getElementById("files-upload");
  var current = "";
  var loaded = false;

  function size(n) {
    if (n == null) return "";
    var u = ["B", "KB", "MB", "GB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + u[i];
  }

  function join(dir, name) {
    var sep = dir.indexOf("\\") >= 0 && dir.indexOf("/") < 0 ? "\\" : "/";
    return dir.replace(/[\\/]+$/, "") + sep + name;
  }

  function parent(dir) {
    var trimmed = dir.replace(/[\\/]+$/, "");
    var i = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
    if (i <= 0) return trimmed.charAt(0) === "/" ? "/" : trimmed;
    return trimmed.slice(0, i) || "/";
  }

  function emptyRow(text) {
    var li = document.createElement("li");
    li.className = "empty";
    li.textContent = text;
    list.replaceChildren(li);
  }

  async function load(path) {
    state.textContent = "Lettura…";
    try {
      var r = await fetch(base + "/list?path=" + encodeURIComponent(path || ""), { cache: "no-store" });
      var j = await r.json().catch(function () { return {}; });
      if (!r.ok) throw new Error(j.detail || "errore " + r.status);
      current = j.path;
      pathInput.value = j.path;
      list.replaceChildren();
      j.entries.forEach(function (e) {
        var li = document.createElement("li");
        li.className = e.dir ? "dir" : "file";
        var name = document.createElement("span");
        name.className = "name";
        name.textContent = (e.dir ? "📁 " : "") + e.name + (e.link ? " →" : "");
        name.title = e.name;
        var sz = document.createElement("span");
        sz.className = "size";
        sz.textContent = e.dir ? "" : size(e.size);
        li.append(name, sz);
        li.addEventListener("click", function () {
          var target = join(current, e.name);
          if (e.dir || e.link) {
            load(target);
          } else {
            location.href = base + "/download?path=" + encodeURIComponent(target);
          }
        });
        list.append(li);
      });
      if (!j.entries.length) emptyRow("Cartella vuota");
      state.textContent = j.entries.length + " elementi";
    } catch (err) {
      state.textContent = "";
      emptyRow("Errore: " + err.message);
    }
  }

  toggle.addEventListener("click", function () {
    var open = panel.hidden;
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open && !loaded) { loaded = true; load(""); }
    window.dispatchEvent(new Event("resize"));
  });
  document.getElementById("files-up").addEventListener("click", function () { if (current) load(parent(current)); });
  document.getElementById("files-go").addEventListener("click", function () { load(pathInput.value.trim()); });
  pathInput.addEventListener("keydown", function (e) { if (e.key === "Enter") load(pathInput.value.trim()); });

  upload.addEventListener("change", async function () {
    var file = upload.files[0];
    if (!file) return;
    state.textContent = "Caricamento di " + file.name + "…";
    var form = new FormData();
    form.append("file", file);
    try {
      var r = await fetch(base + "/upload?path=" + encodeURIComponent(current), { method: "POST", body: form });
      var j = await r.json().catch(function () { return {}; });
      if (!r.ok) throw new Error(j.detail || "errore " + r.status);
      await load(current);
      state.textContent = file.name + " caricato";
    } catch (err) {
      state.textContent = "Caricamento non riuscito: " + err.message;
    }
    upload.value = "";
  });
})();
