/**
 * Session login for the published port.
 *
 * Home Assistant already authenticates the sidebar, so this guards only the
 * port you publish or tunnel. nginx puts the two on separate listeners: the
 * ingress listener never reaches this service, and this one cannot be bypassed
 * by forging a header, because the separation is a socket rather than a check.
 *
 * Three endpoints:
 *   GET  /verify            nginx auth_request — 200 with a valid session, else 401
 *   GET  /__gev_ha/login    the login page
 *   POST /__gev_ha/login    credentials in, Set-Cookie out
 *   GET  /__gev_ha/logout   clears the session
 *
 * The session cookie is a signed token, not a stored session: payload plus an
 * HMAC over it. The signing key mixes a persistent random key with the
 * configured credentials, so changing the username or password invalidates
 * every outstanding session without any bookkeeping.
 *
 * Worth being clear about what this protects. The app's Google Maps key is
 * embedded in the page by design, so anyone who logs in can read it. This
 * stops anonymous use of your quota; the provider-side referrer restriction is
 * what stops the key being used elsewhere.
 */
import http from 'node:http';
import crypto from 'node:crypto';
import fs from 'node:fs';

const PORT = Number(process.env.GEV_AUTH_PORT || 4181);
const USERNAME = process.env.GEV_AUTH_USERNAME || '';
const PASSWORD = process.env.GEV_AUTH_PASSWORD || '';
const KEY_FILE = process.env.GEV_AUTH_KEY_FILE || '/data/session.key';
const COOKIE_NAME = 'gev_session';

const SESSION_DAYS = Math.min(
  Math.max(Number.parseInt(process.env.GEV_AUTH_SESSION_DAYS || '7', 10) || 7, 1),
  365,
);
const SESSION_MS = SESSION_DAYS * 24 * 60 * 60 * 1000;

/**
 * Signing key = persistent random key, bound to the current credentials. A
 * password change therefore expires every session that was issued under the
 * old one.
 */
const SIGNING_KEY = crypto
  .createHmac('sha256', fs.readFileSync(KEY_FILE))
  .update(`${USERNAME.length}:${USERNAME}:${PASSWORD}`)
  .digest();

const digest = (value) => crypto.createHash('sha256').update(String(value)).digest();

/** Constant-time equality that also hides length differences. */
const matches = (a, b) => crypto.timingSafeEqual(digest(a), digest(b));

function sign(payload) {
  return crypto.createHmac('sha256', SIGNING_KEY).update(payload).digest('base64url');
}

function issueToken() {
  const payload = Buffer.from(
    JSON.stringify({ u: USERNAME, exp: Date.now() + SESSION_MS }),
  ).toString('base64url');
  return `${payload}.${sign(payload)}`;
}

function tokenIsValid(token) {
  if (!USERNAME || !PASSWORD) return false;
  if (typeof token !== 'string') return false;
  const split = token.lastIndexOf('.');
  if (split < 1) return false;
  const payload = token.slice(0, split);
  const provided = Buffer.from(token.slice(split + 1));
  const expected = Buffer.from(sign(payload));
  if (provided.length !== expected.length) return false;
  if (!crypto.timingSafeEqual(provided, expected)) return false;
  try {
    const data = JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'));
    return data.u === USERNAME && typeof data.exp === 'number' && data.exp > Date.now();
  } catch {
    return false;
  }
}

function readCookie(header, name) {
  for (const part of String(header || '').split(';')) {
    const eq = part.indexOf('=');
    if (eq === -1) continue;
    if (part.slice(0, eq).trim() === name) return decodeURIComponent(part.slice(eq + 1).trim());
  }
  return null;
}

/**
 * Failed attempts per client address. A published login page gets found and
 * guessed at, so back off hard rather than answering at line rate.
 */
const failures = new Map();
const MAX_ATTEMPTS = 5;

function lockedUntil(ip) {
  const entry = failures.get(ip);
  return entry && entry.until > Date.now() ? entry.until : 0;
}

function recordFailure(ip) {
  const entry = failures.get(ip) || { count: 0, until: 0 };
  entry.count += 1;
  if (entry.count >= MAX_ATTEMPTS) {
    // 1 min after the 5th failure, doubling to a 15 min ceiling.
    const over = entry.count - MAX_ATTEMPTS;
    entry.until = Date.now() + Math.min(60_000 * 2 ** over, 900_000);
  }
  failures.set(ip, entry);
  if (failures.size > 5000) {
    const now = Date.now();
    for (const [key, value] of failures) {
      if (value.until < now) failures.delete(key);
    }
  }
}

const clientIp = (req) =>
  String(req.headers['x-forwarded-for'] || '').split(',')[0].trim()
  || req.socket.remoteAddress
  || 'unknown';

/** Only same-site paths, so `next` cannot become an open redirect. */
function safeNext(value) {
  const next = String(value || '');
  if (!next.startsWith('/') || next.startsWith('//')) return '/';
  if (next.startsWith('/__gev_ha/login') || next.startsWith('/__gev_ha/logout')) return '/';
  return next;
}

const escapeHtml = (value) =>
  String(value).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

function loginPage({ next, message }) {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>God's Eye View</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    padding: 24px;
    background: radial-gradient(circle at 50% 30%, #0d1826 0%, #05080c 70%);
    color: #cfe6f5;
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  form {
    width: 100%; max-width: 360px;
    border: 1px solid #1d3348; border-radius: 10px;
    background: rgba(8, 16, 24, 0.85);
    padding: 32px 28px;
    box-shadow: 0 0 60px rgba(0, 246, 255, 0.06);
  }
  h1 {
    margin: 0 0 4px; font-size: 17px; letter-spacing: 0.16em; font-weight: 600;
    text-transform: uppercase; text-align: center;
  }
  h1 span { color: #00f6ff; }
  p.sub {
    margin: 0 0 26px; text-align: center; font-size: 10px; letter-spacing: 0.22em;
    text-transform: uppercase; color: #4d6a80;
  }
  label {
    display: block; font-size: 10px; letter-spacing: 0.18em; text-transform: uppercase;
    color: #6f93a8; margin: 0 0 6px;
  }
  input {
    width: 100%; margin: 0 0 18px; padding: 11px 12px;
    background: #050a0f;
    border: 1px solid #1d3348; border-radius: 6px;
    color: #dff2ff; font: inherit; font-size: 14px;
  }
  input:focus { outline: none; border-color: #00f6ff; box-shadow: 0 0 0 3px rgba(0, 246, 255, 0.12); }
  button {
    width: 100%; padding: 12px; border: 0; border-radius: 6px; cursor: pointer;
    background: #00f6ff; color: #04121a;
    font: inherit; font-size: 12px; font-weight: 700;
    letter-spacing: 0.2em; text-transform: uppercase;
  }
  button:hover { background: #6cfbff; }
  .error {
    margin: 0 0 18px; padding: 10px 12px; border-radius: 6px;
    border: 1px solid #5c2230; background: rgba(92, 34, 48, 0.35);
    color: #ff9aa8; font-size: 11px; line-height: 1.5;
  }
</style>
</head>
<body>
  <form method="post" action="/__gev_ha/login">
    <h1>God's Eye <span>View</span></h1>
    <p class="sub">Authentication required</p>
    ${message ? `<p class="error">${escapeHtml(message)}</p>` : ''}
    <input type="hidden" name="next" value="${escapeHtml(next)}">
    <label for="u">Username</label>
    <input id="u" name="username" autocomplete="username" autocapitalize="none" autocorrect="off" required autofocus>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
  </form>
</body>
</html>`;
}

function send(res, status, body, headers = {}) {
  res.writeHead(status, {
    'Content-Type': 'text/html; charset=utf-8',
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    ...headers,
  });
  res.end(body);
}

/**
 * Mark the cookie Secure only when the request really arrived over TLS. Behind
 * a tunnel that is the forwarded protocol; on a plain LAN it is not, and a
 * Secure cookie there would simply never come back.
 */
function cookieFor(token, req, expire = false) {
  const proto = String(req.headers['x-forwarded-proto'] || '').split(',')[0].trim();
  const secure = proto === 'https' ? '; Secure' : '';
  const age = expire ? 0 : Math.floor(SESSION_MS / 1000);
  return `${COOKIE_NAME}=${expire ? '' : token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${age}${secure}`;
}

function readBody(req, limit = 8192) {
  return new Promise((resolve) => {
    let size = 0;
    const chunks = [];
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size <= limit) chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', () => resolve(''));
  });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const path = url.pathname;

  // nginx auth_request: the whole point is to be cheap and say yes or no.
  if (path === '/verify') {
    if (tokenIsValid(readCookie(req.headers.cookie, COOKIE_NAME))) {
      res.writeHead(200).end();
    } else {
      res.writeHead(401).end();
    }
    return;
  }

  if (path === '/__gev_ha/logout') {
    send(res, 302, '', {
      Location: '/__gev_ha/login',
      'Set-Cookie': cookieFor('', req, true),
    });
    return;
  }

  if (path === '/__gev_ha/login' && req.method === 'GET') {
    if (tokenIsValid(readCookie(req.headers.cookie, COOKIE_NAME))) {
      send(res, 302, '', { Location: safeNext(url.searchParams.get('next')) });
      return;
    }
    send(res, 200, loginPage({ next: safeNext(url.searchParams.get('next')), message: '' }));
    return;
  }

  if (path === '/__gev_ha/login' && req.method === 'POST') {
    const ip = clientIp(req);
    const until = lockedUntil(ip);
    if (until) {
      const seconds = Math.ceil((until - Date.now()) / 1000);
      send(res, 429, loginPage({
        next: '/',
        message: `Too many failed attempts. Try again in ${seconds}s.`,
      }));
      return;
    }

    // SameSite=Lax already blocks a cross-site POST from carrying the session;
    // this refuses one outright rather than processing credentials for it.
    const origin = req.headers.origin;
    if (origin) {
      let originHost = null;
      try { originHost = new URL(origin).host; } catch { originHost = null; }
      if (originHost !== req.headers.host) {
        // A front proxy that rewrites Host (cloudflared's httpHostHeader, say)
        // makes these disagree for an entirely legitimate request, and the
        // refusal is otherwise indistinguishable from a wrong password. Say
        // which two values failed to match.
        console.log(
          `Refused sign-in: Origin host "${originHost}" does not match Host "${req.headers.host}". `
          + 'If a proxy in front rewrites the Host header, stop it doing so.',
        );
        send(res, 403, loginPage({
          next: '/',
          message: 'Sign-in refused: this request\'s origin does not match the address it arrived on.',
        }));
        return;
      }
    }

    const form = new URLSearchParams(await readBody(req));
    // Without configured credentials there is nothing to prove, and an empty
    // submission would otherwise compare equal to an empty expectation.
    const ok = Boolean(USERNAME) && Boolean(PASSWORD)
      && matches(form.get('username') || '', USERNAME)
      && matches(form.get('password') || '', PASSWORD);

    if (!ok) {
      recordFailure(ip);
      console.log(`Failed sign-in from ${ip}`);
      send(res, 401, loginPage({
        next: safeNext(form.get('next')),
        message: 'Incorrect username or password.',
      }));
      return;
    }

    failures.delete(ip);
    console.log(`Signed in from ${ip}`);
    send(res, 303, '', {
      Location: safeNext(form.get('next')),
      'Set-Cookie': cookieFor(issueToken(), req),
    });
    return;
  }

  res.writeHead(404).end();
});

server.listen(PORT, '127.0.0.1', () => {
  if (!USERNAME || !PASSWORD) {
    console.log('No credentials configured; refusing every request on the published port');
  }
  console.log(`Session login listening on 127.0.0.1:${PORT} (sessions last ${SESSION_DAYS}d)`);
});
