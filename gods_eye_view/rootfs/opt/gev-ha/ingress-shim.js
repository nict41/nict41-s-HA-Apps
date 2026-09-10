/*
 * God's Eye View — Home Assistant ingress shim.
 *
 * Two jobs, both of which have to happen before any app module runs. nginx
 * injects this into <head> as a classic script, so it executes during parse,
 * ahead of every deferred module.
 *
 * 1. PATH PREFIXING. Vite runs with `--base <ingress path>/`, so everything
 *    Vite itself emits (module imports, index.html asset URLs, the Cesium
 *    static mount) is already prefixed. What Vite cannot rewrite is the app's
 *    own root-absolute strings — `fetch('/api/opensky')`, `img.src =
 *    '/logo.svg'`, `/models/airplane.glb` — which a browser resolves against
 *    the ORIGIN, not against the page. Under ingress that lands on Home
 *    Assistant instead of this add-on.
 *
 * 2. DIAGNOSTICS. The app does its real work in the browser, so a stall there
 *    leaves nothing in the add-on log and cannot be diagnosed from Home
 *    Assistant. Since this file is already wrapping every request the page
 *    makes, it also reports what only the page can see — uncaught errors,
 *    console warnings, requests that never finish, and which boot phase the
 *    splash is showing — to /__gev_ha/log, which prints it to the add-on log.
 *
 * `__GEV_BASE__` is replaced with the live ingress path at add-on start.
 */
(function () {
  'use strict';

  /** Ingress path, no trailing slash, e.g. "/api/hassio_ingress/AbC123". */
  var BASE = '__GEV_BASE__';
  if (BASE === '/' || (BASE && BASE.charAt(0) !== '/')) BASE = '';
  var BASE_SLASH = BASE + '/';
  var PREFIXING = BASE !== '';

  window.__GEV_HA_BASE__ = BASE;

  /** Diagnostics reporting, switched by the `client_diagnostics` option. */
  var DIAG = '__GEV_DIAG__' === '1';
  /** Set once the splash clears; boot-time reporting goes quiet after it. */
  var booted = false;

  // Captured before anything below replaces them, so diagnostics never call
  // back into the wrappers they are reporting on.
  var nativeFetch = window.fetch;
  var nativeBeacon = window.navigator && navigator.sendBeacon
    ? navigator.sendBeacon.bind(navigator)
    : null;

  /**
   * Prefix a root-absolute, same-origin URL. Everything else — relative URLs,
   * absolute URLs with a scheme, protocol-relative URLs, and anything already
   * carrying the prefix — is returned untouched.
   */
  function fix(url) {
    if (!PREFIXING) return url;
    if (typeof url !== 'string' || url.length === 0) return url;
    if (url.charCodeAt(0) !== 47 /* "/" */) return url;
    if (url.charCodeAt(1) === 47 /* "//host/..." */) return url;
    if (url === BASE || url.lastIndexOf(BASE_SLASH, 0) === 0) return url;
    return BASE + url;
  }

  // ══ Diagnostics ══════════════════════════════════════════════════════════

  var REPORT_URL = BASE + '/__gev_ha/log';
  var BOOT = Date.now();
  var queue = [];
  var flushTimer = null;
  var recorded = 0;
  var MAX_RECORDED = 250;

  function elapsed() { return Date.now() - BOOT; }

  function seconds(ms) { return (ms / 1000).toFixed(1) + 's'; }

  /** Drop the origin and ingress prefix so log lines stay readable. */
  function shortUrl(url) {
    var text = String(url || '');
    if (text.lastIndexOf(window.location.origin, 0) === 0) {
      text = text.slice(window.location.origin.length);
    }
    if (PREFIXING && text.lastIndexOf(BASE_SLASH, 0) === 0) {
      text = text.slice(BASE.length);
    }
    return text.length > 160 ? text.slice(0, 160) + '…' : text;
  }

  function describe(value) {
    if (typeof value === 'string') return value;
    if (value instanceof Error) return value.message || String(value);
    try { return JSON.stringify(value); } catch (error) { return String(value); }
  }

  function post(events) {
    var body;
    try { body = JSON.stringify({ events: events }); } catch (error) { return; }
    try {
      if (nativeFetch) {
        nativeFetch.call(window, REPORT_URL, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: body,
          keepalive: true,
        }).catch(function () {});
      } else if (nativeBeacon) {
        nativeBeacon(REPORT_URL, body);
      }
    } catch (error) { /* diagnostics must never break the page */ }
  }

  function flush() {
    flushTimer = null;
    if (queue.length) post(queue.splice(0, queue.length));
  }

  function record(kind, text) {
    if (!DIAG || recorded >= MAX_RECORDED) return;
    recorded++;
    queue.push({ kind: kind, text: String(text).slice(0, 400), t: elapsed() });
    if (!flushTimer) flushTimer = setTimeout(flush, 500);
  }

  /** Requests the page has started, so a hang can be attributed to one. */
  var inflight = [];

  function trackRequest(method, url) {
    if (!DIAG || inflight.length >= 400) return null;
    var entry = {
      method: String(method || 'GET').toUpperCase(),
      url: String(url || ''),
      started: Date.now(),
      done: false,
      reported: 0,
    };
    inflight.push(entry);
    return entry;
  }

  function finishRequest(entry) { if (entry) entry.done = true; }

  /**
   * One unreachable host fails every tile it was asked for, which would bury
   * the log. Report the first few per host, then say nothing more about it —
   * the host name is the diagnosis, the repeat count is not.
   */
  var failuresByHost = {};
  var MAX_FAILURES_PER_HOST = 3;

  function shouldReportFailure(url) {
    var host;
    try { host = new URL(url, window.location.href).host; } catch (error) { host = '?'; }
    var count = (failuresByHost[host] || 0) + 1;
    failuresByHost[host] = count;
    if (count === MAX_FAILURES_PER_HOST) {
      record('suppressed', 'further failures from ' + host + ' will not be logged');
    }
    return count <= MAX_FAILURES_PER_HOST;
  }

  // ══ Request interception ═════════════════════════════════════════════════

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

      var url = typeof input === 'string' ? input : (input && input.url) || '';
      var method = (init && init.method) || (input && input.method) || 'GET';
      // The sink itself must not be tracked, or a slow log post looks like a
      // stuck app request.
      var entry = url.indexOf('/__gev_ha/log') === -1 ? trackRequest(method, url) : null;

      var pending;
      try {
        pending = nativeFetch.call(this, input, init);
      } catch (error) {
        finishRequest(entry);
        throw error;
      }
      return pending.then(function (response) {
        finishRequest(entry);
        if (entry && response && response.status >= 400) {
          record('http', response.status + ' ' + entry.method + ' ' + shortUrl(url));
        }
        return response;
      }, function (error) {
        finishRequest(entry);
        if (entry && shouldReportFailure(url)) {
          record('fetch-failed', entry.method + ' ' + shortUrl(url) + ' — ' + describe(error));
        }
        throw error;
      });
    };
  }

  if (window.XMLHttpRequest) {
    var nativeOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function () {
      var args = Array.prototype.slice.call(arguments);
      if (args.length > 1) args[1] = fix(args[1]);
      try { this.__gevRequest = { method: args[0], url: args[1] }; } catch (error) { /* frozen */ }
      return nativeOpen.apply(this, args);
    };

    var nativeSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function () {
      var meta = this.__gevRequest;
      if (meta) {
        var entry = trackRequest(meta.method, meta.url);
        if (entry) {
          this.addEventListener('loadend', function () {
            finishRequest(entry);
            if (this.status === 0) {
              if (shouldReportFailure(entry.url)) {
                record('xhr-failed', entry.method + ' ' + shortUrl(entry.url) + ' — no response');
              }
            } else if (this.status >= 400) {
              record('http', this.status + ' ' + entry.method + ' ' + shortUrl(entry.url));
            }
          });
        }
      }
      return nativeSend.apply(this, arguments);
    };
  }

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

  if (nativeBeacon) {
    navigator.sendBeacon = function (url, data) {
      return data === undefined ? nativeBeacon(fix(url)) : nativeBeacon(fix(url), data);
    };
  }

  // ══ Element URL attributes ═══════════════════════════════════════════════
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

  // ══ HTML strings ═════════════════════════════════════════════════════════
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

  // ══ History ══════════════════════════════════════════════════════════════
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

  // ══ Reporting ════════════════════════════════════════════════════════════

  record('boot', 'page loaded, base="' + (BASE || '(none)') + '", ua=' + navigator.userAgent.slice(0, 90));

  window.addEventListener('error', function (event) {
    var target = event.target;
    if (target && target !== window && target.tagName) {
      record('asset-failed', target.tagName + ' ' + shortUrl(target.src || target.href || '?'));
    } else {
      record('js-error', (event.message || 'error')
        + ' @ ' + shortUrl(event.filename || '?') + ':' + (event.lineno || 0));
    }
  }, true);

  window.addEventListener('unhandledrejection', function (event) {
    var reason = event.reason;
    record('unhandled-rejection', (reason && (reason.stack || reason.message)) || describe(reason));
  });

  ['warn', 'error'].forEach(function (level) {
    var native = window.console && console[level];
    if (typeof native !== 'function') return;
    console[level] = function () {
      try {
        // Warnings matter while the globe is coming up; afterwards the app
        // emits them routinely and they would just fill the add-on log.
        if (level === 'error' || !booted) {
          record('console.' + level, Array.prototype.map.call(arguments, describe).join(' '));
        }
      } catch (error) { /* never let logging break logging */ }
      return native.apply(console, arguments);
    };
  });

  // A request still open after 10s is the single most useful clue when the
  // splash never clears, because the app awaits several with no timeout.
  var MAX_PENDING_PER_SWEEP = 4;
  setInterval(function () {
    if (booted) return;
    var now = Date.now();
    var announced = 0;
    var withheld = 0;
    for (var i = 0; i < inflight.length; i++) {
      var entry = inflight[i];
      if (entry.done) continue;
      var age = now - entry.started;
      var due = (age > 10000 && entry.reported === 0) || (age > 40000 && entry.reported === 1);
      if (!due) continue;
      entry.reported += 1;
      // A blocked host stalls every request to it at once; a handful names the
      // problem and the rest are the same fact repeated.
      if (announced >= MAX_PENDING_PER_SWEEP) { withheld++; continue; }
      announced++;
      record('pending', entry.method + ' ' + shortUrl(entry.url)
        + (entry.reported > 1 ? ' STILL open after ' : ' open for ') + seconds(age));
    }
    if (withheld) record('pending', '+ ' + withheld + ' more request(s) also open');
    if (inflight.length > 200) {
      inflight = inflight.filter(function (entry) { return !entry.done; });
    }
  }, 5000);

  // Boot phase, straight off the splash. Says which await the app is sitting
  // on, and — crucially — whether anything is in flight while it sits there.
  var lastPhase = null;
  var stallReports = 0;
  var phaseTimer = setInterval(function () {
    var screen = document.getElementById('loading-screen');
    if (!screen) return;

    if (screen.classList.contains('hidden')) {
      record('ready', 'globe ready after ' + seconds(elapsed()));
      booted = true;
      clearInterval(phaseTimer);
      return;
    }

    var status = screen.querySelector('.loader-status');
    var text = status ? String(status.textContent || '').trim() : '';
    if (text && text !== lastPhase) {
      lastPhase = text;
      record('phase', text);
    }

    if (stallReports < 4 && elapsed() > (stallReports + 1) * 20000) {
      stallReports++;
      var open = [];
      for (var i = 0; i < inflight.length; i++) {
        if (!inflight[i].done) open.push(inflight[i].method + ' ' + shortUrl(inflight[i].url));
      }
      record('stalled', 'still on "' + (lastPhase || '?') + '" after ' + seconds(elapsed()) + '; '
        + (open.length
          ? open.length + ' request(s) open: ' + open.slice(0, 5).join(' | ')
          : 'NO requests open — waiting on something that is not network'));
    }
  }, 1500);

  window.addEventListener('pagehide', flush);
})();
