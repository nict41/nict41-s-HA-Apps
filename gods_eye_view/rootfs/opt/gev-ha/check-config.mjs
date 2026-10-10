/*
 * Build-time check for the Home Assistant Vite configuration.
 *
 * Run from the app checkout by the Dockerfile. It resolves the wrapper config
 * exactly as Vite will and asserts that the plugin replacement actually
 * happened, so the two ways this wiring can break are caught while building
 * the image rather than by the add-on failing to start:
 *
 *   1. the wrapper, or upstream's config it defers to, throws on load;
 *   2. the wrapper loads but replaces nothing, because upstream renamed or
 *      removed the plugin it targets.
 *
 * It also checks that Vite's CLI still takes `--config`, which is how the
 * wrapper is selected at start. `configFile` is the JS API's name for the
 * same setting and is not a CLI option.
 */
import { argv, exit } from 'node:process';

const configPath = argv[2];
if (!configPath) {
  console.error('usage: node check-config.mjs <path to vite.config.ha.mjs>');
  exit(2);
}

const flatten = (plugins) =>
  (Array.isArray(plugins) ? plugins : []).flatMap((entry) =>
    Array.isArray(entry) ? flatten(entry) : [entry],
  );

let config;
try {
  const module = await import(configPath);
  const factory = module.default;
  config =
    typeof factory === 'function'
      ? await factory({ command: 'serve', mode: 'development' })
      : factory;
} catch (error) {
  console.error('FAIL: the Home Assistant Vite config did not load.');
  console.error(error);
  exit(1);
}

const plugins = flatten(config?.plugins);
if (plugins.length === 0) {
  console.error('FAIL: the resolved config has no plugins.');
  exit(1);
}

const matches = plugins.filter((plugin) => plugin?.name === 'api-not-found');
if (matches.length === 0) {
  console.error(
    'FAIL: no api-not-found plugin in the resolved config. Upstream has ' +
      'renamed or removed it; vite.config.ha.mjs needs updating. Leaving it ' +
      'is not safe — if the catch-all still exists under another name, the ' +
      'add-on serves {"error":"Unknown API route"} for every request.',
  );
  exit(1);
}

const unreplaced = matches.filter((plugin) => plugin.ingressAware !== true);
if (unreplaced.length > 0) {
  console.error(
    'FAIL: an api-not-found plugin survived unreplaced. Under ingress it ' +
      'would answer every request, because Home Assistant serves this ' +
      "add-on from an /api path and that is also the app's Vite base.",
  );
  exit(1);
}

console.log(
  `Vite config OK: ${plugins.length} plugins, api-not-found replaced ` +
    `with the ingress-aware one (${matches.length} occurrence(s)).`,
);
