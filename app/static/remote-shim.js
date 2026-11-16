/* Accesso remoto all'in-out sotto /<id>/: iniettato dal proxy in testa a ogni pagina HTML.
 *
 * L'interfaccia dell'in-out usa percorsi assoluti (/api, /static, /login...). Questo script
 * aggiunge il prefisso /<id> alle richieste fatte da JavaScript (fetch, XHR, WebSocket,
 * EventSource, window.open, history) e tiene separato il localStorage di ogni in-out
 * (il token di login dell'uno non sovrascrive quello dell'altro).
 * Link e risorse statiche senza prefisso li sistema comunque il proxy (Referer -> redirect).
 * Il prefisso arriva da window.__inoutPrefix, impostato dal proxy prima di questo codice.
 */
(function () {
  "use strict";
  var P = window.__inoutPrefix;
  if (!P || window.__inoutShim) return;
  window.__inoutShim = true;

  // location.pathname senza prefisso: il proxy riscrive le letture di location.pathname
  // nelle pagine e negli script dell'in-out in __inoutPath()
  window.__inoutPath = function () {
    var p = location.pathname;
    if (p === P) return "/";
    return p.indexOf(P + "/") === 0 ? p.slice(P.length) : p;
  };

  function fix(u) {
    if (u === null || u === undefined) return u;
    var url;
    try { url = new URL(String(u), location.href); } catch (e) { return u; }
    if (url.host !== location.host || !/^(https?|wss?):$/.test(url.protocol)) return u;
    var p = url.pathname;
    if (p === P || p.indexOf(P + "/") === 0 || p.indexOf("/__remote/") === 0) return u;
    url.pathname = P + p;
    return url.href;
  }
  window.__inoutFix = fix;

  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      if (typeof input === "string" || input instanceof URL) {
        input = fix(input);
      } else if (input && input.url) {
        var fixed = fix(input.url);
        if (fixed !== input.url) input = new Request(fixed, input);
      }
      return origFetch.call(this, input, init);
    };
  }

  var origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    var args = Array.prototype.slice.call(arguments);
    args[1] = fix(url);
    return origOpen.apply(this, args);
  };

  function wrap(name) {
    var Orig = window[name];
    if (!Orig) return;
    var Wrapped = function (url, opts) {
      return arguments.length > 1 ? new Orig(fix(url), opts) : new Orig(fix(url));
    };
    Wrapped.prototype = Orig.prototype;
    ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach(function (k) {
      if (k in Orig) Wrapped[k] = Orig[k];
    });
    window[name] = Wrapped;
  }
  wrap("WebSocket");
  wrap("EventSource");

  var origWinOpen = window.open;
  window.open = function (url) {
    var args = Array.prototype.slice.call(arguments);
    if (url) args[0] = fix(url);
    return origWinOpen.apply(this, args);
  };

  ["pushState", "replaceState"].forEach(function (k) {
    var orig = history[k];
    history[k] = function (state, title, url) {
      return url === undefined || url === null ? orig.call(this, state, title) : orig.call(this, state, title, fix(url));
    };
  });

  // localStorage separato per ogni in-out (stesso indirizzo, chiavi con il prefisso)
  try {
    var ls = window.localStorage;
    var K = P + ":";
    var S = Storage.prototype;
    var get = S.getItem, set = S.setItem, remove = S.removeItem, clear = S.clear, key = S.key;
    S.getItem = function (k) { return this === ls ? get.call(this, K + k) : get.call(this, k); };
    S.setItem = function (k, v) { return this === ls ? set.call(this, K + k, v) : set.call(this, k, v); };
    S.removeItem = function (k) { return this === ls ? remove.call(this, K + k) : remove.call(this, k); };
    S.clear = function () {
      if (this !== ls) return clear.call(this);
      for (var i = this.length - 1; i >= 0; i--) {
        var name = key.call(this, i);
        if (name && name.indexOf(K) === 0) remove.call(this, name);
      }
    };
  } catch (e) { /* localStorage non disponibile */ }

  // Link e form con percorso assoluto: prefisso aggiunto al clic/invio (evita un redirect)
  function fixAttr(el, attr) {
    var v = el.getAttribute(attr);
    if (v && v.charAt(0) === "/" && v.charAt(1) !== "/") el.setAttribute(attr, fix(v));
  }
  document.addEventListener("click", function (e) {
    var a = e.target && e.target.closest && e.target.closest("a[href]");
    if (a) fixAttr(a, "href");
  }, true);
  document.addEventListener("submit", function (e) {
    if (e.target && e.target.getAttribute) fixAttr(e.target, "action");
  }, true);
})();
