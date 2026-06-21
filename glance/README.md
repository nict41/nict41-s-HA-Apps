# Glance Dashboard for Home Assistant

![Glance logo](logo.png)

A self-hosted dashboard that puts all your feeds in one place, running as a
Home Assistant app.

[Glance](https://github.com/glanceapp/glance) supports RSS feeds, weather,
bookmarks, calendars, Hacker News, stocks, Twitch, YouTube, Reddit, and many
more widget types.

## Configuration

On first start the app creates a default `glance.yml` configuration file.
You can edit this file to customize your dashboard.

### Finding the configuration file

The configuration file lives in your main HA config folder alongside
`configuration.yaml`. You can access it through:

- **File Editor app** — open `glance/glance.yml` from the root of your config
- **Samba share** — the file is at `config/glance/glance.yml`
- **SSH** — the file is at `/config/glance/glance.yml`

### Configuration reference

Refer to the upstream Glance documentation for the full list of widgets and
options:

<https://github.com/glanceapp/glance/blob/main/docs/configuration.md>

### Environment variable substitution

Glance supports `${VAR_NAME}` syntax in its configuration file. The app
automatically sets `GLANCE_BASE_URL` for ingress support. You can reference
any other environment variable in your configuration.

## Options

| Option | Default | Description |
|---|---|---|
| `log_level` | `info` | App log verbosity. One of `trace`, `debug`, `info`, `warning`, `error`, `fatal`. |

## Ingress

The app supports Home Assistant ingress, which means it appears in your
sidebar and you can access it without exposing any ports. This is enabled by
default.

If you prefer direct access, you can map port 8099 to a host port in the
app's network configuration.

## Updating the dashboard

After editing `glance/glance.yml`, restart the Glance app to apply changes.

## Installation

See the [repository README](https://github.com/nict41/nict41-s-HA-Apps)
to add this repository to Home Assistant, then install **Glance
Dashboard** from the app store.
