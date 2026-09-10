/*
 * God's Eye View — Home Assistant path-prefix shim.
 *
 * Vite runs with `--base <ingress path>/`, so everything Vite itself emits
 * (module imports, index.html asset URLs, the Cesium static mount) is already
 * prefixed. What Vite cannot rewrite is the app's own root-absolute strings —
 * `fetch('/api/opensky')`, `img.src = '/logo.svg'`, `/models/airplane.glb` —
 * which a browser resolves against the ORIGIN, not against the page. Under
 * ingress that lands on Home Assistant instead of this add-on.
 *
 * So this file prefixes root-absolute URLs at the few places the app can hand
 * one to the browser. It is injected into <head> by nginx as a classic script,
 * which runs during parse and therefore before any deferred module.
 *
 * `__GEV_BASE__` is replaced with the live ingress path at add-on start.
 */
(function () {
  'use strict';

  /** Ingress path, no trailing slash, e.g. "/api/hassio_ingress/AbC123". */
  var BASE = '__GEV_BASE__';
  if (!BASE || BASE.charAt(0) !== '/' || BASE === '/') return;
  var BASE_SLASH = BASE + '/';

  window.__GEV_HA_BASE__ = BASE;

  /**
   * Prefix a root-absolute, same-origin URL. Everything else — relative URLs,
   * absolute URLs with a scheme, protocol-relative URLs, and anything already
   * carrying the prefix — is returned untouched.
   */
  function fix(url) {
    if (typeof url !== 'string' || url.length === 0) return url;
    if (url.charCodeAt(0) !== 47 /* "/" */) return url;
    if (url.charCodeAt(1) === 47 /* "//host/..." */) return url;
    if (url === BASE || url.lastIndexOf(BASE_SLASH, 0) === 0) return url;
    return BASE + url;
  }

  // ── fetch ────────────────────────────────────────────────────────────────
  var nativeFetch = window.fetch;
  if (typeof nativeFetch === 'function') {
    window.fetch = function (input, init) {
      if (typeof input === 'string') {
        input = fix(input);
      } else if (typeof Request === 'function' && input instanceof Request) {
        try {
          var parsed = new URL(input.url);
          if (parsed.origin === window.location.origin) {
            var patched = fix(parsed.pathname);
            if (patched !== parsed.pathname) {
              parsed.pathname = patched;
              input = new Request(parsed.href, input);
            }
          }
        } catch (error) { /* leave the Request as-is */ }
      }
      return nativeFetch.call(this, input, init);
    };
  }

  // ── XMLHttpRequest ───────────────────────────────────────────────────────
  if (window.XMLHttpRequest) {
    var nativeOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function () {
      var args = Array.prototype.slice.call(arguments);
      if (args.length > 1) args[1] = fix(args[1]);
      return nativeOpen.apply(this, args);
    };
  }

  // ── WebSocket / EventSource / Worker ─────────────────────────────────────
  ['WebSocket', 'EventSource', 'Worker'].forEach(function (name) {
    var Native = window[name];
    if (typeof Native !== 'function') return;
    var Wrapped = function (url, options) {
      return options === undefined ? new Native(fix(url)) : new Native(fix(url), options);
    };
    Wrapped.prototype = Native.prototype;
    ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(function (key) {
      if (key in Native) {
        try { Wrapped[key] = Native[key]; } catch (error) { /* read-only */ }
      }
    });
    window[name] = Wrapped;
  });

  // ── navigator.sendBeacon ─────────────────────────────────────────────────
  if (window.navigator && typeof navigator.sendBeacon === 'function') {
    var nativeBeacon = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function (url, data) {
      return data === undefined ? nativeBeacon(fix(url)) : nativeBeacon(fix(url), data);
    };
  }

  // ── Element URL attributes ───────────────────────────────────────────────
  // Covers both `el.src = '/logo.svg'` (property) and
  // `el.setAttribute('src', '/logo.svg')`, which take different paths.
  [
    [window.HTMLImageElement, 'src'],
    [window.HTMLScriptElement, 'src'],
    [window.HTMLLinkElement, 'href'],
    [window.HTMLSourceElement, 'src'],
    [window.HTMLMediaElement, 'src'],
    [window.HTMLIFrameElement, 'src'],
    [window.HTMLAnchorElement, 'href'],
    [window.HTMLObjectElement, 'data'],
    [window.HTMLTrackElement, 'src'],
  ].forEach(function (entry) {
    var ctor = entry[0];
    var prop = entry[1];
    if (!ctor || !ctor.prototype) return;
    var descriptor = Object.getOwnPropertyDescriptor(ctor.prototype, prop);
    if (!descriptor || typeof descriptor.set !== 'function') return;
    Object.defineProperty(ctor.prototype, prop, {
      configurable: true,
      enumerable: descriptor.enumerable,
      get: function () { return descriptor.get.call(this); },
      set: function (value) {
        descriptor.set.call(this, typeof value === 'string' ? fix(value) : value);
      },
    });
  });

  var URL_ATTRIBUTES = { src: 1, href: 1, data: 1, poster: 1, action: 1, 'data-logo-src': 1 };
  var nativeSetAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    if (typeof value === 'string' && URL_ATTRIBUTES[String(name).toLowerCase()] === 1) {
      value = fix(value);
    }
    return nativeSetAttribute.call(this, name, value);
  };

  // ── HTML strings ─────────────────────────────────────────────────────────
  // Markup assigned through innerHTML is parsed by the browser, which fetches
  // `src="/mic.svg"` itself — no property or setAttribute call to intercept.
  var HTML_URL_ATTRIBUTE = /(\s(?:src|href|poster|data)\s*=\s*)(["'])(\/(?!\/)[^"']*)\2/gi;
  function fixHtml(html) {
    if (typeof html !== 'string') return html;
    if (html.indexOf('="/') === -1 && html.indexOf("='/") === -1) return html;
    return html.replace(HTML_URL_ATTRIBUTE, function (match, lead, quote, url) {
      return lead + quote + fix(url) + quote;
    });
  }

  [window.Element, window.ShadowRoot].forEach(function (ctor) {
    if (!ctor || !ctor.prototype) return;
    ['innerHTML', 'outerHTML'].forEach(function (prop) {
      var descriptor = Object.getOwnPropertyDescriptor(ctor.prototype, prop);
      if (!descriptor || typeof descriptor.set !== 'function') return;
      Object.defineProperty(ctor.prototype, prop, {
        configurable: true,
        enumerable: descriptor.enumerable,
        get: function () { return descriptor.get.call(this); },
        set: function (value) { descriptor.set.call(this, fixHtml(value)); },
      });
    });
  });

  var nativeInsertAdjacentHTML = Element.prototype.insertAdjacentHTML;
  if (typeof nativeInsertAdjacentHTML === 'function') {
    Element.prototype.insertAdjacentHTML = function (position, html) {
      return nativeInsertAdjacentHTML.call(this, position, fixHtml(html));
    };
  }

  // ── History ──────────────────────────────────────────────────────────────
  // A root-absolute pushState would move the page out of the ingress path and
  // break every relative URL resolved after it.
  ['pushState', 'replaceState'].forEach(function (method) {
    var native = window.history && window.history[method];
    if (typeof native !== 'function') return;
    window.history[method] = function (state, title, url) {
      if (arguments.length < 3) return native.call(window.history, state, title);
      return native.call(window.history, state, title, fix(url));
    };
  });
})();
