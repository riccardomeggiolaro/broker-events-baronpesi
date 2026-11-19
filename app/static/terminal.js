/* Terminale SSH verso un in-out attraverso il broker.
 * Websocket /__remote/ssh/<id>: frame binari = byte del terminale (nei due sensi),
 * frame di testo = controllo in JSON ({type: resize|status|ready|error|exit}). */
(function () {
  "use strict";
  var cfg = window.__terminal;
  var dot = document.getElementById("dot");
  var state = document.getElementById("state");
  var reconnect = document.getElementById("reconnect");
  var encoder = new TextEncoder();

  var term = new Terminal({
    cursorBlink: true,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
    fontSize: 14,
    scrollback: 5000,
    theme: { background: "#0f141b", foreground: "#e3e8ef", cursor: "#5b8ff0", selectionBackground: "#2f4a7a" }
  });
  var fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(document.getElementById("term"));
  fit.fit();
  term.focus();

  var ws = null;
  var ready = false;

  function setState(kind, text) {
    dot.className = "dot" + (kind ? " " + kind : "");
    state.textContent = text;
  }

  function send(data) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(data);
  }

  function sendSize() {
    if (ready) send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  }

  function connect() {
    reconnect.hidden = true;
    ready = false;
    setState("", "Collegamento…");
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var url = proto + "//" + location.host + "/__remote/ssh/" + encodeURIComponent(cfg.device) +
      "?cols=" + term.cols + "&rows=" + term.rows;
    ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    ws.onmessage = function (ev) {
      if (typeof ev.data !== "string") {
        term.write(new Uint8Array(ev.data));
        return;
      }
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === "status") {
        setState("", msg.message);
      } else if (msg.type === "ready") {
        ready = true;
        setState("ok", "Collegato come " + msg.user +
          (msg.idle_minutes ? " · si chiude dopo " + msg.idle_minutes + " min senza attività" : ""));
        sendSize();
        term.focus();
      } else if (msg.type === "error") {
        setState("bad", msg.message);
        term.write("\r\n\x1b[31m" + msg.message + "\x1b[0m\r\n");
      } else if (msg.type === "exit") {
        setState("bad", msg.message);
      }
    };
    ws.onclose = function (ev) {
      if (ready || dot.className === "dot") {
        setState("bad", ev.code === 4401 ? "Sessione del pannello scaduta: accedi di nuovo" : "Disconnesso");
      }
      ready = false;
      reconnect.hidden = false;
      term.write("\r\n\x1b[90m[connessione chiusa]\x1b[0m\r\n");
    };
  }

  term.onData(function (d) { if (ready) send(encoder.encode(d)); });
  term.onBinary(function (d) {
    if (!ready) return;
    var buf = new Uint8Array(d.length);
    for (var i = 0; i < d.length; i++) buf[i] = d.charCodeAt(i) & 255;
    send(buf);
  });
  term.onResize(sendSize);

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { fit.fit(); }, 80);
  });

  reconnect.addEventListener("click", function () {
    term.reset();
    connect();
  });

  connect();
})();
