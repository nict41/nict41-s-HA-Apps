/**
 * Client diagnostics sink.
 *
 * God's Eye View does its real work in the browser, so a stall there leaves no
 * trace at all in the add-on log — which is why "it just hangs" was previously
 * undiagnosable from Home Assistant. The ingress shim posts what only the page
 * can see (uncaught errors, console warnings, requests that never finish, and
 * the boot phase the splash is showing); this prints it on stdout, where the
 * add-on log viewer picks it up.
 *
 * It listens on loopback only and is reachable through nginx at
 * /__gev_ha/log. Nothing here trusts the payload: it is browser-supplied text,
 * so it is length-capped, control characters are stripped, and every line is
 * prefixed so it can never be mistaken for an add-on log line.
 */
import http from 'node:http';

const PORT = Number(process.env.GEV_LOG_PORT || 4180);
const MAX_BODY = 64 * 1024;
const MAX_EVENTS = 50;
const MAX_TEXT = 400;
const DEDUPE_MS = 20000;

/** Recently printed lines, so a wedged page cannot flood the log. */
const recent = new Map();

function clean(value) {
  return String(value ?? '')
    // Control characters would let a page forge log lines or break the viewer.
    .replace(/[\u0000-\u001f\u007f]+/g, ' ')
    .slice(0, MAX_TEXT)
    .trim();
}

function emit(line) {
  const now = Date.now();
  const last = recent.get(line);
  if (last !== undefined && now - last < DEDUPE_MS) return;
  recent.set(line, now);
  if (recent.size > 200) {
    for (const [key, at] of recent) {
      if (now - at >= DEDUPE_MS) recent.delete(key);
    }
  }
  console.log(`[client] ${line}`);
}

function format(event) {
  if (!event || typeof event !== 'object') return null;
  const kind = clean(event.kind).slice(0, 40) || 'log';
  const text = clean(event.text);
  if (!text) return null;
  const at = Number.isFinite(event.t) ? `${(event.t / 1000).toFixed(1)}s ` : '';
  return `${at}${kind}: ${text}`;
}

http
  .createServer((req, res) => {
    if (req.method !== 'POST') {
      res.statusCode = 405;
      res.end();
      return;
    }
    let size = 0;
    const chunks = [];
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size <= MAX_BODY) chunks.push(chunk);
    });
    req.on('end', () => {
      // Answer first: the page must never block on its own diagnostics.
      res.statusCode = 204;
      res.end();
      let payload;
      try {
        payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      } catch {
        return;
      }
      const events = Array.isArray(payload?.events) ? payload.events.slice(0, MAX_EVENTS) : [];
      for (const event of events) {
        const line = format(event);
        if (line) emit(line);
      }
    });
    req.on('error', () => {});
  })
  .listen(PORT, '127.0.0.1', () => {
    console.log(`Client diagnostics sink listening on 127.0.0.1:${PORT}`);
  });
