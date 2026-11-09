export TERM=xterm-256color
export LANG=C.UTF-8

# A login shell (what the web terminal opens via `bash --login` or tmux) sources
# /etc/profile first, and on the Alpine base that resets PATH to a fixed default
# that excludes the npm global prefixes. Re-add them here so `claude` (installed
# under /data/npm-global or /opt/npm-global since 1.2.70, issues #15/#16) stays on
# PATH. .bashrc is sourced after /etc/profile via ~/.profile, so this prepend wins.
export PATH="/data/npm-global/bin:/opt/npm-global/bin:/root/.local/bin:$PATH"
PS1='\[\033[1;36m\]claude-code\[\033[0m\]:\[\033[1;34m\]\w\[\033[0m\]\$ '

# Aliases
#
# `c` / `cc` used to call an update_mcp_token function first. That function was
# dead code with a side effect: it wrote .mcpServers.homeassistant.env.HASS_TOKEN
# into settings.json, but the MCP server is registered in .claude.json (with no
# env block), and hass-mcp reads HA_TOKEN / HA_URL, which the boot script exports.
# So it refreshed nothing and left a stray mcpServers key in a file that does not
# use one. The boot script now removes that key if an older version wrote it.
alias ll='ls -la'
alias c='claude'
alias cc='claude --continue'
alias ha-config='cd /homeassistant'
alias ha-logs='cat /homeassistant/home-assistant.log 2>/dev/null || echo "Log not found"'
