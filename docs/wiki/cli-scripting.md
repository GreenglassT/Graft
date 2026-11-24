# Scripting & Environment

A script drives `graft` with `--json`, as the Mac app does. The environment
variables `graft` reads include the ones for running behind a proxy.

## Scripting

`--json` turns a command's output into newline-delimited JSON events on
stdout. Every stdout line is one JSON object, and any command's narration and
warnings go to stderr. This is how the Mac app talks to the CLI.

- Notices from before the command runs (a directory moved to its new name, an
  old environment variable still set) are `log` events with
  `"source": "startup"`.
- `--list` starts with one `account` event, `{"type":"account","account":"free"}`
  or `"paid"`, even when no apps are configured. The `app` events after it and
  the `preflight-end` event from `--check` carry the same `account` field.
- `graft --describe` prints the contract the app relies on: every flag and
  every event type. `--help` doesn't list it.

A few commands don't answer in events. `--init` without `--account` refuses
`--json`. `--version`, `--setup-portal` and the password prompts ignore it,
and so do three whose output is fixed already: `--build-log` prints the saved
log as it is (JSON Lines, since only builds run with `--json` save one),
`--describe` always prints JSON, and `--reveal-build` prints nothing.

## Environment variables

| Variable | Meaning |
|---|---|
| the one named by `signing.cert_password_env` | `.p12` password: when set and not empty, used instead of the Keychain's |
| `GRAFT_SSH_USER`, `GRAFT_SSH_PASS`, `GRAFT_SSH_KEY` | SSH to a jailbroken device, for a dump |
| `GRAFT_FRIDA_IOS_DUMP` | where frida-ios-dump is cloned |
| `UNFAIRPLAY_MODEL_DIR` | UnfairPlay's own: where its model lives |
| `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME` | move Graft's directories |
| `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` (and the lower-case forms) | send Graft's requests through a proxy |
| `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`, `CURL_CA_BUNDLE`, `SSL_CERT_DIR` | trust another CA, such as a TLS-inspecting proxy's root |

Graft.app reads these from your login shell when it starts, and again when you
press Check again (on the setup screen or beside Settings' PATH line) or Re-run
in Preflight, at most once every 10 seconds.

- It runs the shell as an interactive login shell, given 5 seconds. csh and
  tcsh run as interactive shells, which read `~/.tcshrc` or `~/.cshrc` but not
  `~/.login`.
- So a variable exported in `~/.zshrc` or `~/.zprofile` reaches the CLI it
  runs, as do your `PATH` additions and `XDG_*` directories.
- `PYTHON*` variables and the color switches (`FORCE_COLOR`, `TTY_COMPATIBLE`,
  `TTY_INTERACTIVE`, `PY_COLORS`, `CLICOLOR_FORCE`) are never passed on.
- If the shell doesn't answer, the app carries on without them (or, on a later
  Check again, with what it read before), and a startup file that hangs is
  stopped along with everything it started.
- On a Mac without the Xcode Command Line Tools, a startup file that runs one
  of their commands (`git`, say) can make macOS offer to install them when
  Graft starts, as opening a Terminal window would. Graft doesn't need them:
  cancel, or install them.

### Behind a proxy

Graft's own requests (iTunes lookups, Canister, repos, LiveContainer's
releases) honor the proxy variables. Graft.app's bundled Python trusts the CA
certificates it ships (certifi) and `/etc/ssl/cert.pem`, never the macOS
Keychain, so a proxy that re-signs TLS needs its root named in a CA variable:
export `REQUESTS_CA_BUNDLE=/path/to/corp-ca.pem` (a PEM file holding the
proxy's root, plus the public roots if you want the rest of the internet too).

In Graft.app, and in the `graft` it links into `~/.local/bin`, each of
`REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE` and `CURL_CA_BUNDLE` is used only when
it names an existing, readable file by an absolute path, and `SSL_CERT_DIR`
only when it names an existing folder. Otherwise it's ignored, so a stale
export can't break every connection. `OPENSSL_CONF` and `OPENSSL_ENGINES`
are always ignored there: they're meant for another OpenSSL. The pipx CLI
passes all of these through untouched. ipatool and UnfairPlay are Go programs
and use the macOS trust settings, Keychain included.
