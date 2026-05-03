<p align="center">
  <img src="./assets/icon.svg" alt="umbra" width="160"/>
</p>

# umbra

```
██╗   ██╗███╗   ███╗██████╗ ██████╗  █████╗
██║   ██║████╗ ████║██╔══██╗██╔══██╗██╔══██╗      stealth chrome
██║   ██║██╔████╔██║██████╔╝██████╔╝███████║      for AI agents
██║   ██║██║╚██╔╝██║██╔══██╗██╔══██╗██╔══██║      v0.4
╚██████╔╝██║ ╚═╝ ██║██████╔╝██║  ██║██║  ██║
 ╚═════╝ ╚═╝     ╚═╝╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝
```

> *the darkest part of the shadow — where light is fully blocked.*

> The merged best-of [obscura](https://github.com/h4ckf0r0day/obscura) (per-session fingerprint payload) + [fantoma](https://github.com/Huzy85/fantoma) (zero-mouse ARIA driver) + [stealth-browser-mcp](https://github.com/vibheksoni/stealth-browser-mcp) (nodriver + MCP surface), with the gaps each one had filled in.

---

## ⚡ in numbers

| | umbra |
|---|---|
| **bot.sannysoft.com** | **31 / 31** ✓ (perfect) |
| **creepjs `headless`** | **0 %** (matches vanilla Chrome) |
| **creepjs `stealth`** | **0 %** |
| **CDP automation tells** stripped | `webdriver` `cdc_*` `$cdc_` `_phantom` `_selenium` `__webdriver_*` `__nightmare` ... |
| **MCP tools** | **64** (broad primitives, not 95 narrow ones) |
| **headless** | real GPU via `--headless=new + ANGLE Vulkan` (no SwiftShader tell) |
| **TLS / JA3** | real Chrome stack + optional `curl_cffi` for raw HTTP |
| **WebRTC** | mDNS-aware SDP filter (real-Chrome behavior, no LAN IP leak) |
| **handoff** | live remote-view via `cloudflared` Quick Tunnel (works VPS → home laptop) |

---

## 🚀 install

```bash
git clone <this-repo> && cd umbra
pip install -e ".[all]"          # all optional deps (markdown, tls, sessions)

# optional but recommended for the handoff feature:
sudo pacman -S cloudflared       # arch
brew install cloudflared         # macos
apt install cloudflared          # debian/ubuntu
```

requires Python 3.10+, Chrome / Chromium / Edge (auto-detected).

---

## 🤖 use as an MCP server (the main use case)

```bash
claude mcp add-json umbra '{
  "type":"stdio",
  "command":"/home/you/.venv/bin/python",
  "args":["-m","umbra.server"],
  "env":{"UMBRA_CONTAINER":"1","PYTHONPATH":"/path/to/umbra/src"}
}'
```

Now your agent has 64 MCP tools for stealth Chrome automation. Cursor, Claude Desktop, Claude Code — anything MCP.

---

## 🧰 the 64 tools

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
                  ├─ handoff            handoff_start / handoff_wait / request_user_input
                  │  (live remote view, cloudflared tunnel)
                  │
                  ├─ sessions           session_save / session_load / session_list /
                  │  (encrypted)        session_delete
                  │
                  ├─ files              upload_file / setup_downloads / wait_for_download
                  │
                  └─ TLS                tls_fetch  (raw HTTP w/ Chrome JA3+JA4)
```

---

## 🎯 highlight tools

### `handoff_start` — when you hit a captcha, hand the wheel back

```
agent → handoff_start("t0", "solve recaptcha")
         → returns https://random.trycloudflare.com/h-XYZ/
agent → tells user: "open this URL"
user  → opens URL on phone/laptop, sees live page, clicks/types
user  → hits "I'M DONE"
agent → handoff_wait("t0")  blocks until done, returns post-handoff URL+title
agent → continues automation
```

Built on Cloudflare Quick Tunnels (no signup, no auth, instant).
URL contains a 192-bit auth token in the path → URL knowledge = auth.
Forces HTTP/2 protocol for sustained WebSocket reliability.

### `extract_markdown` — page → clean markdown (firecrawl-style)

```python
extract_markdown('t0')
# → {"_untrusted": True,
#    "title": "Web Scraping - Wikipedia",
#    "markdown": "# Web Scraping\n\nMethod of extracting data...",
#    "source_html_len": 87432}
```

Mozilla Readability + markdownify. Falls back to `<body>` for list pages (HN, reddit) where readability gives up.

### `session_save` / `session_load` — log in once

```python
# First time: log in manually via handoff
session_save('t0', 'github-me', passphrase='hunter2')
# → encrypted blob in ~/.local/share/umbra/sessions/github.com/github-me.fern

# Next time: skip login entirely
session_load('t0', 'github-me', passphrase='hunter2')
# → cookies + localStorage injected, you're logged in
```

Fernet (AES-128-CBC + HMAC-SHA256) + PBKDF2-HMAC-SHA256 200k iterations. Per-(domain, name) namespace, path-traversal-safe.

### `tls_fetch` — skip the DOM entirely for JSON APIs

```python
tls_fetch('https://api.example.com/users')
# → {"status": 200, "body": "{...}"}
```

curl_cffi pinned to the running Chrome version — JA3+JA4+HTTP/2 SETTINGS frames match Chrome exactly. ~50ms vs ~500ms via spawn+navigate.

### multi-browser

```python
spawn(url='...', browser_id='alice')
spawn(url='...', browser_id='bob')
# alice and bob have fully isolated cookies, profiles, identities
list_browsers()
# → [{"browser_id":"alice","tab_count":3}, {"browser_id":"bob","tab_count":1}]
```

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
| MCP tool surface | ✗ | ✗ | ✓ (95 narrow) | ✓ (64 broad) |
| prompt-injection signaling | ✗ | ✗ | ✗ | ✓ (`_untrusted: true` on all extraction) |

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

---

## 🔬 token efficiency

every MCP tool response goes through `_compact()`:

- `None` dropped (empty `[]` / `""` / `0` / `False` KEPT — they're informative)
- columnar layout for 4+ homogeneous-dict arrays: `{"_columnar":true,"keys":[...],"rows":[[...]]}`
- constant-column hoist: shared values factored to `_constant: {col: val}`
- word-boundary string truncation w/ explicit `...[+Nc, raise max_str to see full]` marker
- list truncation w/ `{_truncated, shown, total, more_via}` marker

**24% average wire-byte savings** on real-world pages (test data on HN/wikipedia). flip with `set_verbosity('full')` when you need raw.

---

## 🏗️ architecture

```
                  ┌────────────────────────────────────────────┐
                  │  FastMCP server  (umbra.server, 64 tools)  │
                  │  + _compact() minification                 │
                  │  + _untrusted prompt-injection signaling   │
                  └────────────────────────────────────────────┘
                                       │
       ┌─────────────────────┬─────────┴──────────┬─────────────────────┐
       ▼                     ▼                    ▼                     ▼
  ┌─────────┐          ┌──────────┐         ┌────────────┐        ┌──────────┐
  │ Browser │          │  Drivers │         │  Stealth   │        │  Misc    │
  │  multi  │          │  ARIA    │         │  payload   │        │  session │
  │  inst.  │          │  CDP     │         │  3520 list │        │  handoff │
  └────┬────┘          │  humanizer        │  detection │        │  tls     │
       │               └────┬─────┘         └─────┬──────┘        └──────────┘
       ▼                    ▼                     ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  nodriver  (real Chrome via CDP) + Page.addScriptToEvaluateOnNewDocument│
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
pytest -m e2e -v -s
```

runs `bot.sannysoft.com` + `creepjs` + UA-CH consistency + automation-tell checks. Catches drift if Chrome / nodriver update breaks something.

---

## 📜 license

**MIT + Attribution Requirement.** Free for any use (commercial, research, hobby) — but if you ship it in a product or publish research using it, please credit:

```markdown
Powered by [umbra](https://github.com/GabriWar/umbra) by Gabriel Duarte Guerra.
```

(in your README, docs, about page, or paper acknowledgements — anywhere a human reading your project can see it).

Third-party attributions in `LICENSE`:

- `stealth/payload.js` patterns from h4ckf0r0day/obscura (Apache-2.0)
- `stealth/tracker_domains.txt` from obscura (Peter Lowe ad/tracker host file)
- `driver/aria.py` + `humanizer.py` patterns from Huzy85/fantoma (MIT)
- MCP tool surface convention from vibheksoni/stealth-browser-mcp (MIT)
