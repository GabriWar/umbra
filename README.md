<p align="center">
  <img src="./assets/banner.png" alt="umbra" width="100%"/>
</p>

# umbra

> **Stealth Chrome MCP server for AI agents.**
> Real Chrome • 31/31 sannysoft • 0% creepjs • 77 tools • multi-browser • proxy pools • encrypted sessions • live human handoff over Cloudflare tunnel.

> *umbra — the darkest part of a shadow, where light is fully blocked.*

Built by merging the best parts of [obscura](https://github.com/h4ckf0r0day/obscura) [`536072b`](https://github.com/h4ckf0r0day/obscura/commit/536072b) (per-session fingerprint payload) + [fantoma](https://github.com/Huzy85/fantoma) [`86f20eb`](https://github.com/Huzy85/fantoma/commit/86f20eb) (zero-mouse ARIA driver) + [stealth-browser-mcp](https://github.com/vibheksoni/stealth-browser-mcp) [`def424d`](https://github.com/vibheksoni/stealth-browser-mcp/commit/def424d) (nodriver + MCP surface) — and filling in their gaps: the [`Page.enable()` injection bug](https://github.com/GabriWar/umbra/blob/main/src/umbra/stealth/inject.py), real-GPU headless via `--headless=new + ANGLE Vulkan`, dynamic UA-CH version pinning, mDNS-aware WebRTC SDP filter, MCP token-efficient minification, and the `_untrusted: true` cognitive-separation flag on every page-sourced response.

---

## ⚡ at a glance

| | umbra |
|---|---|
| **bot.sannysoft.com** | **31 / 31** ✓ (perfect) |
| **creepjs `headless`** | **0 %** (matches vanilla Chrome) |
| **creepjs `stealth`** | **0 %** |
| **headless** | real GPU via `--headless=new + ANGLE Vulkan` (no SwiftShader tell) |
| **CDP automation tells** stripped | `webdriver` `cdc_*` `$cdc_` `_phantom` `_selenium` `__webdriver_*` `__nightmare` ... |
| **TLS / JA3 / JA4** | real Chrome stack + optional `curl_cffi` for raw HTTP |
| **WebRTC** | mDNS-aware SDP filter (real-Chrome behavior, no LAN IP leak) |
| **MCP tools** | **77** — broad primitives + `batch` flagship + `proxy_pool_*` (not 95 narrow ones) |
| **token efficiency** | **75% tokens / 83% bytes saved** vs raw output, [measured](#-token-efficiency) over 79 calls |
| **proxy support** | pool w/ 5 rotation strategies, CDP auth (any provider), sticky sessions, geo filters |
| **handoff** | live remote-view via `cloudflared` Quick Tunnel (works VPS → home laptop) |
| **CDP schema drift** | resilient — survives Chrome field churn (e.g. dropped `sameParty`) without hangs |

---

## 🚀 quick start

```bash
git clone https://github.com/GabriWar/umbra.git
cd umbra
pip install -e ".[all]"
python -m umbra.server   # ctrl+c after a few seconds — verify tools register
```

**requirements:** Python 3.10+, a Chromium-based browser (Chrome / Chromium / Edge — auto-detected).

| extra | enables | install |
|---|---|---|
| (default) | core 50 tools, encrypted sessions, proxy pool | `pip install -e .` |
| `[markdown]` | `extract_markdown` (readability + markdownify) | `pip install -e ".[markdown]"` |
| `[tls]` | `tls_fetch` (curl_cffi w/ Chrome JA3+JA4) | `pip install -e ".[tls]"` |
| `[playwright]` | optional Playwright backend | `pip install -e ".[playwright]"` |
| `[test]` | pytest + asyncio for regression suite | `pip install -e ".[test]"` |
| `[all]` | everything above | `pip install -e ".[all]"` |

**recommended companion: cloudflared** — `handoff_*` exposes a live remote-view of the browser via a Cloudflare Quick Tunnel (no signup, no auth). Without it, handoff falls back to localhost-only.

```bash
sudo pacman -S cloudflared       # arch / cachyos
sudo apt install cloudflared     # debian / ubuntu
brew install cloudflared         # macos
# else: github.com/cloudflare/cloudflared/releases/latest
```

---

## 🤖 MCP setup

Wire into Claude Code (or any MCP client w/ the same shape):

```bash
claude mcp add-json umbra '{
  "type":"stdio",
  "command":"/full/path/to/your/python",
  "args":["-m","umbra.server"],
  "env":{
    "UMBRA_CONTAINER":"1",
    "PYTHONPATH":"/full/path/to/umbra/src"
  }
}'
```

Restart Claude Code → `/mcp` shows `umbra` w/ 77 tools. For Cursor / Claude Desktop / Cline / others, edit their `mcp_servers` config with the same shape.

---

## 🌐 HTTP API mode

stdio is for one MCP client per process. For remote agents, n8n, OpenAI function-calling, plain `curl`, or anything that isn't an MCP client — run umbra as an HTTPS server with API-key auth.

```bash
# local HTTPS (self-signed cert auto-generated + cached in ~/.cache/umbra/tls/)
UMBRA_API_KEYS="$(openssl rand -hex 32)" \
  uv run umbra-server --transport http --host 127.0.0.1 --port 8765 --tls-self-signed

# prod TLS (use a real cert from caddy/nginx/letsencrypt or pass directly)
UMBRA_API_KEYS=key1,key2 \
  uv run umbra-server --transport http --host 0.0.0.0 --port 443 \
    --tls-cert /etc/ssl/umbra.crt --tls-key /etc/ssl/umbra.key
```

Endpoints (all gated by `X-API-Key: <key>` or `Authorization: Bearer <key>`, except `/healthz`):

| route | method | purpose |
|---|---|---|
| `/healthz` | GET | liveness, no auth |
| `/api/tools` | GET | list all 84 tools + JSON schemas |
| `/api/tools/{name}` | POST | call tool, body = `{args...}` |
| `/api/call` | POST | generic dispatch, body = `{"tool":"...","args":{...}}` |
| `/mcp` | POST | native streamable-http MCP for proper MCP clients |

```bash
# discover tools
curl -k -H "x-api-key: $KEY" https://localhost:8765/api/tools | jq '[.tools[].name]'

# call a tool
curl -k -H "x-api-key: $KEY" -X POST https://localhost:8765/api/tools/spawn \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com"}'
```

### server flags

```
--transport stdio|sse|http      stdio = MCP only (default), http = REST + MCP + auth
--host 127.0.0.1                bind address (use 0.0.0.0 for LAN)
--port 8765
--path /mcp                     native MCP mount path
--api-key KEY                   repeatable; or set UMBRA_API_KEYS=k1,k2
--no-auth                       disable auth (dev only — bearer leak hazard)
--tls-cert PATH --tls-key PATH  enable HTTPS with your cert
--tls-self-signed               auto-generate + cache a self-signed cert
--idle-timeout 1800             reap tabs idle ≥ this many seconds (0 disables GC)
--gc-interval 60                how often the idle GC runs
--no-orphan-sweep               skip startup chrome cleanup
-v                              verbose logs
```

### lifecycle hygiene

- **idle GC** — tabs not touched in `--idle-timeout` get auto-closed. Browsers with no remaining tabs follow. Manual trigger: call the `cleanup_stale` tool with `idle_seconds`.
- **startup orphan sweep** — chrome procs from prior umbra-server crashes (matched by `--user-data-dir=/tmp/uc_*` w/ a parent pid that isn't us) are SIGTERM'd + their profile dirs `rmtree`d. Skip with `--no-orphan-sweep`.
- **graceful shutdown** — SIGTERM/SIGINT closes every browser + sweeps profile dirs before exit.

### TODOs

- **multi-tenancy.** Right now all API keys share one global `_state` — every key sees every browser/tab/proxy/session by id. For multiple users with isolation, we need to:
  - tag every browser/tab/route/hook/handoff/session entry with the calling key (`owner_key_id`).
  - scope `list_browsers` / `list_tabs` / `proxy_pool_list` / `session_list` to the caller's namespace.
  - reject cross-tenant `tab_id` / `browser_id` references with 403.
  - per-key proxy pools and quota (max tabs, max bandwidth, idle-timeout override).
  - audit log keyed by `owner_key_id`.

  For now: one server = one trust domain. Run separate `umbra-server` processes on different ports if you need real isolation.
- mTLS option (client-cert auth) instead of bearer keys.
- per-key rate limiting + quotas.
- **CloakBrowser integration** ✓ shipped — patched chromium is now the default. See [§ CloakBrowser](#-cloakbrowser-default-chromium). Deferred follow-ons:
  - **humanize layer** port (bezier mouse curves w/ aim points, per-char typing w/ typos+self-correct, scroll accel/decel). Opt-in on `aria_click`/`aria_type`/`click_at`/`scroll`/`drag`. Works on stock chromium too.
  - **geoip-from-proxy** → lookup proxy exit IP, derive timezone+locale, apply via CDP `Emulation.setTimezoneOverride` + `setLocaleOverride`. Opt-in `geoip: true` on spawn.
  - **deterministic fingerprint seed** — `spawn(fingerprint_seed='abc')` → seedable PRNG for `rotate_fingerprint` reproducibility.
  - **storage quota normalization** via CDP `Storage.overrideQuotaForOrigin`.
  - **WebRTC IP override** via CDP — covered by cloak's native patch when cloak is active; CDP fallback only useful on stock.

---

## 🎯 recipes

### `batch` ⭐ flagship — N tools in one MCP round-trip

```python
batch([
  {"tool": "navigate",        "args": {"tab_id": "t0", "url": "https://news.ycombinator.com"}},
  {"tool": "wait_for_text",   "args": {"tab_id": "t0", "text": "Hacker News"}},
  {"tool": "aria_snapshot",   "args": {"tab_id": "t0"}},
  {"tool": "extract_links",   "args": {"tab_id": "t0", "limit": 30}},
  {"tool": "extract_markdown","args": {"tab_id": "t0"}},
])
# → {"results":[...5 entries with ok/data/ms each...],
#    "elapsed_ms":1840, "ok_count":5, "fail_count":0}
```

Serial in declared order, single MCP round-trip. Saves protocol framing per call AND composes with cross-call dedup (identical re-calls inside the batch return `_unchanged_since` instead of full payloads). Use it whenever you have ≥2 calls in mind — it's almost always the right choice.

`stop_on_error=True` short-circuits on first failure (default: keep going + report fail_count).

### `proxy_pool_*` — multi-provider rotation, any provider, any format

5 rotation strategies, rolling health, geo + tag filters, sticky sessions. Plugs into `spawn(use_proxy_pool=True)` — picks one entry per browser process (Chrome locks proxy per-process; for parallel distinct egress IPs use multiple `browser_id`s).

**input formats** — auto-detected, mix-and-match in same load:

```text
# standard URL (auth optional, scheme optional, defaults to http://)
http://user:pass@gateway.provider.com:8080
socks5://1.2.3.4:1080

# provider IP-list export (host:port:user:pass — webshare, IPRoyal, Decodo, ...)
31.59.20.176:6754:user:pass

# sticky-session gateway (one URL, N session-suffixed users)
gw.bright.com:22225:user-session-abc123-country-US:pass

# inline metadata for filtering
http://gw.proxy.com:8080#country=US,tags=residential|sticky
```

**rotation strategies** — `round_robin` (default), `random`, `least_used`, `best_health`, `sticky_browser` (same `browser_id` always gets same entry).

**creds-stripped flag + CDP auth** — Chrome's `--proxy-server=` silently strips inline creds; umbra feeds Chrome a creds-free URL and answers proxy auth challenges via CDP `Fetch.authRequired`. Works for HTTP / HTTPS proxies w/ Basic auth — Bright Data, Oxylabs, Smartproxy/Decodo, IPRoyal, SOAX, NetNut, Webshare, ProxyMesh, etc.

**SOCKS5 + auth caveat** — Chromium has no support for SOCKS5 username/password auth (RFC 1929) — [open since 2014, effectively wontfix](https://issues.chromium.org/issues/40089088). CDP `Fetch.authRequired` is HTTP-layer only; SOCKS5 auth is a TCP-subnegotiation that completes BEFORE any HTTP fires, so the Fetch domain never sees it. Workaround matrix:

| transport | auth | works in umbra? |
|---|---|---|
| `http://host:port` | none | ✓ |
| `http://user:pass@host:port` | basic | ✓ (via CDP) |
| `socks5://host:port` | none | ✓ |
| `socks5://user:pass@host:port` | RFC 1929 | ✗ — unfixable in chrome |

If u need SOCKS5 + auth, run a local HTTP→SOCKS5 forwarder (`gost -L=http://:8080 -F=socks5://user:pass@upstream`) and point umbra at the local HTTP port instead.

```python
# MCP usage
proxy_pool_load(data="/path/to/proxies.txt", rotation="round_robin")
proxy_pool_health_check(timeout_s=8.0, parallel=8)   # parallel probe, updates rolling health

spawn(url="https://target.com", browser_id="us-1",
      use_proxy_pool=True, proxy_country="US", proxy_tag="residential")
# → {"tab_id":"t0", "proxy":{"id":"a3b1...", "host":"http://1.2.3.4:8080",
#                            "country":"US", "tags":["residential"], "health":1.0}}

proxy_pool_remove("a3b1...")   # bad rep? drop it
```

7 MCP tools: `proxy_pool_load`, `proxy_pool_add`, `proxy_pool_remove`, `proxy_pool_clear`, `proxy_pool_list`, `proxy_pool_health_check`, `proxy_pool_export`.

#### route through Tor (free, multi-exit, no provider)

Tor's SOCKS5 supports **stream isolation** — different SOCKS user/pass = different circuit = different exit IP. One `tor` daemon, N distinct exits, zero provider cost:

```bash
sudo systemctl enable --now tor   # binds 127.0.0.1:9050
```

```text
# ~/.umbra/proxies.txt — each line = one isolated circuit (user/pass arbitrary)
socks5://circ1:x@127.0.0.1:9050#tags=tor
socks5://circ2:x@127.0.0.1:9050#tags=tor
socks5://circ3:x@127.0.0.1:9050#tags=tor
socks5://circ4:x@127.0.0.1:9050#tags=tor
socks5://circ5:x@127.0.0.1:9050#tags=tor
```

```python
proxy_pool_load(data="~/.umbra/proxies.txt")
spawn(use_proxy_pool=True, proxy_tag="tor")
```

**caveats** — Tor exits are publicly listed (`check.torproject.org/exit-addresses`); most anti-bot stacks (Cloudflare Bot Mgmt, DataDome, Akamai, PerimeterX) blocklist them. Useful for archive sites / IP-leak testing / gov forms. Useless against hardened scraping targets. Slow: ~3–10s per first req per circuit, 1–3s after warm. Pin exit country via `ExitNodes {us}` in `/etc/tor/torrc` + `systemctl reload tor`.

### `handoff_start` — captcha / 2FA wall? hand the wheel back

```
agent → handoff_start("t0", "solve recaptcha")
         → returns https://random.trycloudflare.com/h-XYZ/
agent → tells user: "open this URL"
user  → opens URL on phone/laptop, sees live page, clicks/types
user  → hits "I'M DONE"
agent → handoff_wait("t0")  blocks until done, returns post-handoff URL+title
agent → continues automation
```

Built on Cloudflare Quick Tunnels (no signup, instant). URL contains a 192-bit auth token in the path → URL knowledge = auth. Forces HTTP/2 for sustained WebSocket reliability.

### `extract_markdown` — page → clean markdown (firecrawl-style)

```python
extract_markdown('t0')
# → {"_untrusted": True,
#    "title": "Web Scraping - Wikipedia",
#    "markdown": "# Web Scraping\n\nMethod of extracting data...",
#    "source_html_len": 87432}
```

Mozilla Readability + markdownify. Falls back to `<body>` for list pages (HN, reddit) where readability gives up.

### `session_save` / `session_load` — log in once, skip auth forever

```python
session_save('t0', 'github-me', passphrase='hunter2')
# → encrypted blob in ~/.local/share/umbra/sessions/github.com/github-me.fern

# Next time:
session_load('t0', 'github-me', passphrase='hunter2')
# → cookies + localStorage injected, you're logged in
```

Fernet (AES-128-CBC + HMAC-SHA256) + PBKDF2-HMAC-SHA256 200k iterations. Per-(domain, name) namespace, path-traversal-safe.

### `tls_fetch` — skip the DOM entirely for JSON APIs

```python
tls_fetch('https://api.example.com/users')
# → {"status": 200, "body": "{...}"}
```

curl_cffi pinned to running Chrome version — JA3+JA4+HTTP/2 SETTINGS frames match Chrome exactly. ~50ms vs ~500ms via spawn+navigate.

### multi-browser orchestration

```python
spawn(url='...', browser_id='alice')
spawn(url='...', browser_id='bob')
# alice and bob have fully isolated cookies, profiles, identities
list_browsers()
# → [{"browser_id":"alice","tab_count":3}, {"browser_id":"bob","tab_count":1}]
```

### request interception graph (`route_*` + HAR record/replay)

Match DSL: `url_pattern`, `url_regex`, `method`, `resource_type`, `header_match`, `status_min/max`. Actions: `block` (14 custom `error_reason`s; response-stage block synthesizes 5xx via fulfill), `fulfill` (status+headers+body|body_b64), `continue` (request rewrite: new_url/new_method/new_post_data/headers — headers MERGED w/ originals, not replaced), `modify` (response-stage `getResponseBody` → `body_replace=[[regex,repl],...]` or outright body/status/headers override), `tee` (pure spy: pass-through + capture body), `redirect` (synth 302 + Location). Per-rule `delay_ms` (latency injection), `times` (auto-disable after N hits), `priority` (higher fires first), `capture` (cross-stage body buffer for any action), `enabled` (pause without remove). HAR-1.2 record/replay (`loose` URL-only mode for query-string drift). Tracker/resource blocking from `StealthOptions(block_trackers=, block_resources=)` integrated into the same engine — single Fetch handler, no double-fire race. Engine: `src/umbra/driver/intercept.py`.

---

## 🧰 the 77 tools

```
                  ┌─ browser            spawn / close / list_browsers / close_browser /
                  │                     navigate / list_tabs / switch_tab / back / forward /
                  │                     reload / kill_all
                  │
                  ├─ ARIA               aria_snapshot / aria_click / aria_type / find_by_text
                  │  (zero mouse)       fill_form / current_state
                  │
                  ├─ input (CDP)        click_at / press_key / scroll / drag / hover /
                  │  humanized          paste_text / select_option / wait_for / wait_for_text
                  │
                  ├─ extraction         extract_text / extract_links / grep_text / dom_query /
                  │  _untrusted=true    inspect_element / extract_markdown / clone_element
                  │
                  ├─ visual             screenshot / screenshot_region
                  │
                  ├─ JS                 evaluate / inject_css
                  │
                  ├─ devtools           get_console_logs / get_network_requests / clear_logs /
                  │                     get_response_body / memory_metrics / get_cookies /
                  │                     set_cookies / clear_cookies
                  │
                  ├─ stealth ops        check_detection / warm_session / rotate_fingerprint /
                  │                     set_verbosity
                  │
                  ├─ network ctrl       block_urls / set_extra_headers / set_viewport /
                  │                     dynamic_hook
                  │
                  ├─ interception       route_add / route_add_many / route_remove /
                  │  (full graph)       route_set_enabled / route_block_set / route_list /
                  │                     route_captures / har_record_start / har_record_stop /
                  │                     har_dump / har_clear / har_replay_load
                  │
                  ├─ proxy pool ⭐      proxy_pool_load / proxy_pool_add / proxy_pool_remove /
                  │  multi-provider     proxy_pool_clear / proxy_pool_list /
                  │                     proxy_pool_health_check / proxy_pool_export
                  │
                  ├─ handoff            handoff_start / handoff_wait / request_user_input
                  │  (cloudflared)
                  │
                  ├─ sessions           session_save / session_load / session_list /
                  │  (encrypted)        session_delete
                  │
                  ├─ files              upload_file / setup_downloads / wait_for_download
                  │
                  ├─ TLS                tls_fetch  (raw HTTP w/ Chrome JA3+JA4)
                  │
                  └─ batch ⭐ flagship  batch  (N tools in one round-trip; composes w/ dedup)
```

---

## 🐍 use as a python library

```python
import asyncio
from umbra import stealth_browser

async def main():
    async with stealth_browser(timezone="America/New_York", block_trackers=True) as b:
        tab = await b.new_tab("https://news.ycombinator.com")
        await asyncio.sleep(2)
        await tab.save_screenshot("hn.png")

asyncio.run(main())
```

```python
# ARIA driver — zero mouse coords
from umbra import stealth_browser, AriaDriver

async with stealth_browser() as b:
    tab = await b.new_tab("https://github.com/login")
    drv = AriaDriver(tab)
    await drv.snapshot()
    print(drv.render_tree())
    # [0] textbox "Login or email"
    # [1] textbox "Password"
    # [2] button "Sign in"
    await drv.type(0, "me@example.com")
    await drv.type(1, "...")
    await drv.click(2)
```

```python
# Proxy pool — parallel browsers w/ distinct egress IPs
from umbra import StealthBrowser, StealthOptions
from umbra.proxypool import ProxyPool

pool = ProxyPool(rotation="round_robin")
pool.load_lines(open("proxies.txt").read())   # or load_json / load_csv / load_file

async def main():
    for i in range(3):
        b = StealthBrowser(StealthOptions(proxy_pool=pool))
        b._pool_browser_id = f"scraper-{i}"
        async with b:
            tab = await b.new_tab("https://api.ipify.org")
            print(await tab.evaluate("document.body.innerText"))
```

---

## 🔬 token efficiency

Counter-intuitively, umbra costs LESS context than minimal browser-MCPs (incl. playwright-mcp) on any real agent session — its 77-tool catalog adds ~13KB upfront, but per-call savings recover that within 3 calls and dominate after.

**measured** (79-call e2e session against real Chrome + httpbin, all 77 tools exercised, see [`tests/test_token_audit.py`](https://github.com/GabriWar/umbra/blob/main/tests/test_token_audit.py)):

| | uncompressed (`set_verbosity='full'`) | compressed (default) | saved |
|---|---|---|---|
| total tokens (cl100k_base) | 136,285 | **33,357** | **75 %** |
| total bytes (JSON) | 420,648 | **71,282** | **83 %** |
| median per call | — | **14 tokens / 4 ms** | — |

Top per-tool wins: `clone_element` 97 %, `dom_query` 52 %, `tls_fetch` 40 %, `proxy_pool_export` 45 %. The handful of zero-save tools (`screenshot*`, `aria_snapshot`, `inspect_element`) either ship base64 binaries (incompressible) or are already pre-RLE'd in the driver before `_compact` sees them.

### how the savings happen

Every MCP tool response goes through `_compact()`:

- **drops `None` only** — empty `[]` / `""` / `0` / `False` KEPT (they're informative)
- **columnar layout** for 4+ homogeneous-dict arrays: `{"_columnar":true,"keys":[...],"rows":[[...]]}` — 44% smaller on real `dom_query`/cookies/network
- **constant-column hoist** — shared values factored to `_constant: {col: val}`
- **word-boundary string truncation** w/ explicit `...[+Nc, raise max_str to see full]` marker
- **list truncation** w/ `{_truncated, shown, total, more_via}` marker — caller sees what was cut and how to lift the cap
- **`_untrusted: true` flag** (8 bytes) instead of wrapping content in `<external>...</external>` tags
- **cross-call dedup ledger** — identical repeat calls return `{"_unchanged_since": "cN", "_hash": "..."}` instead of full payload (`force_refresh=True` to bypass)
- **ARIA pattern grouping (RLE)** — long lists w/ repeating `(role,name)` cycles collapse to a single `[range] cycle×N (period P): ...` line. Real HN comments page: ~70% smaller snapshot.
- **URL footnoting in `extract_links`** — repeated hosts factored to `_hosts: {h1: "https://..."}` then referenced. ~50% smaller on link-heavy pages.

Toggle off via `set_verbosity('full')` when you need raw byte-exact output. Lossless: zero failures, zero inflations across all 79 audit calls.

---

## 🥷 CloakBrowser (default chromium)

`spawn` defaults to `chromium="cloak"` — when the
[CloakBrowser](https://github.com/CloakHQ/CloakBrowser) patched chromium
build is installed under `~/.umbra/cloak/<tag>/`, every browser uses it.
Cloak ships 49-57 C++ source patches against canvas, WebGL, audio, font,
GPU, WebRTC, screen, and timing fingerprint surfaces. Native patches beat
JS shims because detectors check the underlying API surface, not just
property values — so umbra auto-downgrades its own `stealth_mode` to
`minimal` (automation-tell cleanup only) when cloak is active, to avoid
double-fingerprinting.

### setup (one-time)

Cloak is **not** bundled (license: free use, no redistribute) and is
**not** silently auto-downloaded. Install once:

```bash
python -m umbra --setup           # download + verify + cache
python -m umbra --setup --force   # re-download
python -m umbra --setup --tag <t> # pin a specific release
python -m umbra --status          # show install state (no network)
python -m umbra --uninstall       # wipe ~/.umbra/cloak/
```

First `spawn()` with cloak missing **on an interactive TTY** prompts to
install. Non-TTY (MCP/HTTP server, CI, scripts) silently falls back to
stock chromium with a one-line warning — spawn never hangs on input.
A `.declined` marker is written if the user says no, suppressing future
prompts; delete `~/.umbra/cloak/.declined` to re-enable.

MCP tools: `cloak_status()`, `cloak_install(force=False, tag=None)`.

### opt-out

```bash
# per-spawn:
spawn(chromium="stock")          # MCP / python
# globally:
export UMBRA_NO_CLOAK=1          # kill-switch — every spawn uses stock
# or point at your own build:
export UMBRA_CLOAK_BINARY=/path/to/chrome
```

### platforms

| platform | cloak build | umbra behavior |
|---|---|---|
| linux x64   | ✓ | auto |
| linux arm64 | ✓ | auto |
| windows x64 | ✓ | auto |
| darwin arm64| ✓ (separate tag) | auto |
| darwin x64  | ✗ | fall back to stock + warn |
| windows arm64 | ✗ | fall back to stock + warn |

GitHub anon API rate limit is 60/h — set `GITHUB_TOKEN` to lift it.
Manifest cached 24h.

### measured impact (2026-05, linux-x64, headless)

Public aggregate detectors **do not visibly shift** with cloak — they mostly
probe the surfaces JS shims already cover (`navigator.webdriver`, basic
canvas hash, automation flags). The C++ patches harden deeper surfaces those
aggregates don't score:

| signal | stock + JS shim | + cloak | note |
|---|---|---|---|
| creepjs headless % | ≤5 | ≤5 | unchanged — aggregate baseline |
| creepjs stealth %  | ≤5 | ≤5 | unchanged |
| sannysoft pass     | 30+/31 | 29/31 | WebGL Vendor/Renderer now report "no webgl context" — cloak strips the uniquely-identifying GPU strings on purpose (intentional surface cut, not a regression) |
| automation tells   | 0 | 0 | unchanged |
| UA-CH brands.Chromium version | matches UA | **matches UA** | ✓ fixed — cloak's internal UA-CH stub clobbered our `Network.setUserAgentOverride`, so the minimal payload now re-asserts `navigator.userAgentData` via `defineProperty` from the Python-side detected Chrome version. Only injected under cloak. |

Where cloak actually helps (not aggregate-scored by the public detectors):
canvas/audio per-pixel noise *patterns*, font enumeration consistency,
exact GPU info strings, screen geometry edge cases, WebRTC IP leak at the
C++ level, timing-API quantization. If your adversary fingerprints those
specifically (FingerprintJS Pro, sift, akamai bot manager), cloak shifts
the needle in ways `sannysoft`/`creepjs` summaries won't show.

### license note (read this)

CloakBrowser's binary license permits free personal **and** commercial *use*
but forbids **redistribution**. umbra never bundles the binary — it always
pulls from upstream releases on your machine. Don't repackage `~/.umbra/cloak`
into your own product or container image you ship to third parties; the
auto-download flow exists exactly so each user fetches their own copy. See
[BINARY-LICENSE.md](https://github.com/CloakHQ/CloakBrowser/blob/main/BINARY-LICENSE.md)
upstream for the exact terms.

---

## 🛡️ stealth coverage matrix

| detection vector | obscura | fantoma | sb-mcp | **umbra** |
|---|---|---|---|---|
| canvas / audio / WebGL fp | ✓ | partial | ✗ | ✓ (per-session noise, deterministic w/in session) |
| `navigator.webdriver` | ✓ | ✓ | ✓ | ✓ |
| `cdc_*` / `_phantom` / `_selenium` | ✗ | n/a | ✓ (nodriver) | ✓ delete-only (no `in` operator tell) |
| `event.isTrusted` | ✗ | ✓ (no synth events) | ✗ | ✓ (CDP `Input.dispatch*` only) |
| mouse / scroll behavioral fp | n/a | ✓ | ✗ | ✓ (ARIA driver default) |
| keystroke timing fp | n/a | ✓ key-pair | ✗ flat 50ms | ✓ key-pair + log-normal jitter |
| Cloudflare turnstile (passive) | ✗ | partial | ✓ | ✓ (real Chrome) |
| TLS / JA3 / JA4 | ✗ | ✗ | ✓ (real Chrome) | ✓ + `tls_fetch` for raw HTTP |
| WebGL real GPU in headless | ✗ no GL | ✗ | ✗ SwiftShader | ✓ ANGLE Vulkan |
| WebRTC outgoing SDP `host` | partial | ✗ | ✗ | ✓ (mDNS-aware filter, real-Chrome behavior) |
| UA-CH version mismatch | ✗ | ✗ | ✗ | ✓ (dynamic Chrome version + `setUserAgentOverride`) |
| iframe + shadow DOM piercing | ✗ | ✓ | ✗ | ✓ |
| tracker/fp-script blocking | ✓ (3520) | ✗ | ✗ | ✓ (3520 + dynamic hooks) |
| session warming (cookie age) | ✗ | ✗ | ✗ | ✓ (4 profiles) |
| live human handoff | ✗ | ✗ | ✗ | ✓ (cloudflared tunnel) |
| proxy auth (CDP, any provider) | ✗ | ✗ | ✗ | ✓ + 5-strategy rotation pool |
| MCP tool surface | ✗ | ✗ | ✓ (95 narrow) | ✓ (77 broad) |
| prompt-injection signaling | ✗ | ✗ | ✗ | ✓ (`_untrusted: true` on all extraction) |

---

## 🩹 CDP schema resilience

nodriver's CDP parser hardcodes Chrome protocol field names — when Chrome changes the schema between releases, the parser KeyErrors. Worse, the listener task dies on the unhandled raise → every subsequent CDP call on that tab hangs forever (no awaiter ever wakes up).

umbra ships three monkey-patches in `umbra/nodriver_patch.py` to make this class of bug impossible:

1. **`Transaction.__call__`** — every parser exception becomes `future.set_exception(...)` so the awaiter gets a real error, never a hang.
2. **`Connection._listener`** — wraps the per-message dispatch so a single bad parse can't kill the listener task; future calls keep working.
3. **`Cookie.from_json`** — tolerant of Chrome 146+ dropping `sameParty` (matches the pattern already used in `CookieParam.from_json`; upstream inconsistency).

Patches are **idempotent** (per-class flag + module-level short-circuit, safe to call N times) and **partial-failure tolerant** (each patch runs in its own try/except — one failing doesn't block the others). Applied automatically at `umbra.browser` import — zero config.

---

## 🏗️ architecture

```
                  ┌────────────────────────────────────────────┐
                  │  FastMCP server  (umbra.server, 77 tools)  │
                  │  + _compact() minification                 │
                  │  + _untrusted prompt-injection signaling   │
                  │  + cross-call dedup ledger                 │
                  └────────────────────────────────────────────┘
                                       │
       ┌────────────┬──────────────────┼──────────────┬──────────────┐
       ▼            ▼                  ▼              ▼              ▼
  ┌─────────┐  ┌──────────┐      ┌────────────┐  ┌──────────┐  ┌──────────┐
  │ Browser │  │  Drivers │      │  Stealth   │  │  Proxy   │  │  Misc    │
  │  multi  │  │  ARIA    │      │  payload   │  │  pool    │  │  session │
  │  inst.  │  │  CDP     │      │  3520 list │  │  CDP auth│  │  handoff │
  └────┬────┘  │ humanizer│      │  detection │  │  rotation│  │    tls   │
       │       │ intercept│      └─────┬──────┘  └────┬─────┘  └──────────┘
       │       └────┬─────┘            │              │
       ▼            ▼                  ▼              ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │nodriver (real Chrome via CDP) + Page.addScriptToEvaluateOnNewDocument│
  │  --headless=new + --use-angle=vulkan + dynamic UA-CH version pinning │
  └──────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
                              ┌─────────────────┐
                              │   real Chrome   │
                              │  146.0.7680.x   │
                              └─────────────────┘
```

---

## 🧪 regression tests

```bash
pip install -e ".[test]"
pytest -m e2e -v -s                                      # full e2e
.venv/bin/python tests/test_token_audit.py               # token efficiency audit (79 calls)
UMBRA_PROXY_LIST=/path/to/proxies.txt \                  # opt-in: also exercise proxy pool
  .venv/bin/python tests/test_token_audit.py
```

Covers `bot.sannysoft.com` + `creepjs` + UA-CH consistency + automation-tell checks + (when enabled) end-to-end proxy pool spawn/auth/rotation. Catches drift if Chrome / nodriver update breaks something.

---

## 🛠️ roadmap

### distribution

- [ ] Submit to [Smithery.ai](https://smithery.ai) registry — add `smithery.yaml` + tag a release. Auto-indexes for Claude Desktop / Cursor / Cline users.
- [ ] Add `.claude-plugin/plugin.json` for Claude Code's plugin marketplace.
- [ ] Submit to Anthropic's official marketplace via `claude.ai/settings/plugins/submit`.

### features

- [x] **Proxy pool rotation** — shipped. `ProxyPool` w/ 5 rotation strategies, rolling health, geo + tag filters, sticky sessions, multi-format loaders (URL, `host:port:user:pass`, sticky-session gateway, JSON, CSV). CDP `Fetch.authRequired` handler so creds work on any provider despite Chrome's flag stripping. 7 MCP tools.
- [x] **Full request interception graph** — shipped as `route_*` + `har_*` (see recipes section).
- [ ] **Battle-test the ARIA tree on edge cases** — fantoma-derived snapshot covers the 95% case (forms, lists, dialogs, nav) but real-world weirdness still exposes gaps: shadow-DOM-inside-iframe-inside-shadow-DOM, custom elements w/ delegated focus, `<canvas>`-rendered "trees" (Figma/Notion), virtual-scroll lists where ARIA indexes shift mid-snapshot, `aria-owns` cross-references, RTL/i18n role inflections. Need a regression corpus (gmail, github, notion, figma, linear, jira, gov forms) + property-based tests.
- [ ] **Network API ergonomics** — current `route_add(...)` is declarative; Playwright's `route(pattern, async (route, request) => {...})` is callback-based. Add `route_handler(tab_id, pattern, js_handler_src)` that lets the caller register a JS expression evaluated per paused request — returns `{action: 'fulfill'|'continue'|...}` per-call. Tradeoffs: sandbox the JS, network round-trip per request (slow), but unbeatable for "fulfill only if request body contains X" / "rewrite based on prior response" / dynamic decisions.
- [ ] **HAR tooling polish** — current HAR record/replay is HAR-1.2 byte-exact + `loose` URL-only fallback. Add per-entry **matchers** (`matchUrl(regex)`, `matchPostData(json_path)`, `matchHeaders(...)` for query-drift / session-token tolerance), **body morphing** (`updateContent(transform)` to mutate a recorded body before serving), **strict vs fallback** modes, **HAR sanitization** (strip Authorization/Cookie/Set-Cookie/PII before commit). Unlocks committing HAR fixtures to test repos without leaking secrets.
- [ ] **Per-browser exit-node selection via Tailscale** — userspace `tailscaled` per-browser w/ distinct exit nodes for self-hosted residential proxy farms (alternative to paid providers).

---

## 📜 license

**MIT + Attribution Requirement.** Free for any use (commercial, research, hobby) — but if you ship it in a product or publish research using it, please credit:

```markdown
Powered by [umbra](https://github.com/GabriWar/umbra) by Gabriel Duarte Guerra.
```

(in your README, docs, about page, or paper acknowledgements — anywhere a human reading your project can see it).

Third-party attributions in `LICENSE`:

- `stealth/payload.js` patterns from h4ckf0r0day/obscura @ [`536072b`](https://github.com/h4ckf0r0day/obscura/commit/536072b) (Apache-2.0)
- `stealth/tracker_domains.txt` from obscura (Peter Lowe ad/tracker host file)
- `driver/aria.py` + `humanizer.py` patterns from Huzy85/fantoma @ [`86f20eb`](https://github.com/Huzy85/fantoma/commit/86f20eb) (MIT)
- MCP tool surface convention from vibheksoni/stealth-browser-mcp @ [`def424d`](https://github.com/vibheksoni/stealth-browser-mcp/commit/def424d) (MIT)
