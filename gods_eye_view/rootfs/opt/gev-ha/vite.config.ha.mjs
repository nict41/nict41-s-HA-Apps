/*
 * God's Eye View — Vite configuration for Home Assistant.
 *
 * Wraps upstream's own config and replaces exactly one plugin.
 *
 * Upstream registers a catch-all so an unmatched API route answers JSON
 * rather than falling through to the HTML page:
 *
 *     server.middlewares.use('/api', () => 404 {"error":"Unknown API route"})
 *
 * Home Assistant serves an add-on from `/api/hassio_ingress/<token>/`, and the
 * app runs with that path as Vite's `--base`, so every document, module and
 * asset request reaches the dev server under `/api/...`. Middleware a plugin
 * adds in `configureServer` runs ahead of Vite's own — upstream depends on
 * that for its Host check — so the catch-all answers the request before
 * Vite's base middleware has stripped the prefix. The collision is total:
 * with the catch-all in place the entire app, splash screen included, is
 * `{"error":"Unknown API route"}`.
 *
 * The replacement below keeps upstream's behaviour for real API calls — nginx
 * sends those with the base already stripped, so they are unaffected — and
 * steps aside for anything addressed under the base, which belongs to Vite.
 *
 * This is a wrapper rather than a patch to upstream's file so that bumping
 * GEV_COMMIT cannot silently drop it. If upstream renames or removes the
 * plugin the swap below finds nothing and says so in the add-on log.
 */
import upstreamConfig from './vite.config.js';

/** Upstream's plugin name, and the name the replacement keeps. */
const API_NOT_FOUND = 'api-not-found';

/**
 * Upstream's catch-all, minus the requests that are the app itself.
 *
 * `middlewares.use('/api', ...)` makes connect strip `/api` from `req.url`
 * before the handler runs, so the full request line is read from
 * `req.originalUrl`.
 */
function ingressAwareApiNotFound() {
  const install = (server) => {
    server.middlewares.use('/api', (req, res, next) => {
      const base = server.config?.base || '/';
      const url = req.originalUrl || req.url || '';
      if (base !== '/' && url.startsWith(base)) return next();
      res.writeHead(404, {
        'Content-Type': 'application/json',
        'Cache-Control': 'no-store',
      });
      res.end(JSON.stringify({ error: 'Unknown API route' }));
    });
  };
  return {
    name: API_NOT_FOUND,
    configureServer: install,
    configurePreviewServer: install,
  };
}

let replaced = 0;

/**
 * Swap the plugin in place, keeping its position: it has to stay after the
 * provider routes it backstops and before Vite's HTML fallback. The array
 * holds nested arrays, because several entries are spread plugin groups.
 */
function swapPlugin(plugins) {
  if (!Array.isArray(plugins)) return plugins;
  return plugins.map((entry) => {
    if (Array.isArray(entry)) return swapPlugin(entry);
    if (entry && entry.name === API_NOT_FOUND) {
      replaced += 1;
      return ingressAwareApiNotFound();
    }
    return entry;
  });
}

export default async (env) => {
  const config =
    typeof upstreamConfig === 'function'
      ? await upstreamConfig(env)
      : upstreamConfig;

  replaced = 0;
  const plugins = swapPlugin(config.plugins);

  if (replaced === 0) {
    // Not fatal either way: upstream may simply have dropped the catch-all,
    // which needs no fix. But if it was renamed, the app serves nothing under
    // ingress, and that must not be a silent failure.
    console.warn(
      "[gev-ha] no '%s' plugin in the upstream config. If the app answers " +
        '{"error":"Unknown API route"} under ingress, this wrapper needs ' +
        'updating for the current upstream.',
      API_NOT_FOUND,
    );
  }

  return { ...config, plugins };
};
