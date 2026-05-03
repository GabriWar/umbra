/* umbra stealth payload — runs once, before any page script.
 *
 * Inject via Page.addScriptToEvaluateOnNewDocument BEFORE navigation. Patches
 * detectable surfaces in real Chrome. Designed for nodriver / undetected-chromedriver
 * stack — does NOT replace globals (would break the page); patches in place.
 *
 * Layered detection coverage:
 *   1. Automation tells       (cdc_*, $cdc_, $wdc_*, _selenium, navigator.webdriver, ...)
 *   2. Fingerprint surfaces   (canvas/audio/WebGL noise, plugins, fonts, mediaDevices)
 *   3. UA-Client-Hints        (consistent with the sent User-Agent)
 *   4. Permissions consistency (notifications=prompt, not denied)
 *   5. WebRTC ICE filtering   (drop host candidates that leak local IPs)
 *   6. Function masking       (toString reports [native code] for our patches)
 *   7. event.isTrusted        (synthetic events from the driver report trusted)
 *   8. Intl/timezone consistency
 *
 * Ports concepts from h4ckf0r0day/obscura (Apache-2.0) bootstrap.js:
 *   - _markNative / Function.prototype.toString masking
 *   - Per-session deterministic fingerprint via _fpRand
 *   - GPU/screen pools, WebGL UNMASKED_RENDERER mock
 *   - mediaDevices, plugins, mimeTypes shapes
 *   - Intl.DateTimeFormat resolvedOptions wrap
 */
(() => {
  'use strict';

  // Idempotency guard — preload may run on every navigation/iframe.
  if (window.__umbra_stealth_loaded) return;
  Object.defineProperty(window, '__umbra_stealth_loaded', {
    value: true, writable: false, enumerable: false, configurable: false,
  });

  // ───────────────────────────── 0. helpers ─────────────────────────────

  const _origToString = Function.prototype.toString;
  const _nativeFns = new WeakSet();

  // Replace toString so our patched functions look native. Mark itself first.
  const patchedToString = function toString() {
    if (_nativeFns.has(this)) {
      return `function ${this.name || ''}() { [native code] }`;
    }
    return _origToString.call(this);
  };
  Object.defineProperty(Function.prototype, 'toString', {
    value: patchedToString, writable: true, configurable: true,
  });
  _nativeFns.add(patchedToString);

  function maskNative(fn) {
    if (typeof fn === 'function') _nativeFns.add(fn);
    return fn;
  }

  // Mask any function we install as a getter via defineProperty.
  function defineNative(target, prop, descriptor) {
    if (descriptor.get) maskNative(descriptor.get);
    if (descriptor.set) maskNative(descriptor.set);
    if (typeof descriptor.value === 'function') maskNative(descriptor.value);
    Object.defineProperty(target, prop, descriptor);
  }

  // Per-session deterministic fingerprint seed (xorshift). Stable for one
  // browsing session, randomized between sessions. Stored on window so iframes
  // share the seed → consistent fingerprint across frames (a real-browser tell).
  if (typeof window.__umbra_seed !== 'number') {
    Object.defineProperty(window, '__umbra_seed', {
      value: Math.floor(Math.random() * 0xFFFFFFFF),
      writable: false, enumerable: false, configurable: false,
    });
  }
  const SEED = window.__umbra_seed;

  function fpRand(salt) {
    let h = (SEED ^ (salt | 0)) | 0;
    h = Math.imul(h ^ (h >>> 16), 0x45d9f3b);
    h = Math.imul(h ^ (h >>> 13), 0x45d9f3b);
    return ((h ^ (h >>> 16)) >>> 0) / 0xFFFFFFFF;
  }
  function pick(arr, salt) { return arr[Math.floor(fpRand(salt) * arr.length)]; }

  // ─────────────────────── 1. strip automation tells ───────────────────────
  // List culled from undetected-chromedriver, puppeteer-extra-stealth, and
  // every public detector script (creepjs, fingerprintjs, sannysoft fpCollect).
  //
  // CRITICAL: do NOT defineProperty(window, k, {get:()=>undefined}) — that
  // CREATES the property, and detectors use the `in` operator (which returns
  // true for any defined property regardless of value). Pure delete-and-block
  // via deletion + prototype trap is the only correct path.
  const WINDOW_TELLS = [
    'cdc_adoQpoasnfa76pfcZLmcfl_Array',
    'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
    'cdc_adoQpoasnfa76pfcZLmcfl_Symbol',
    'cdc_adoQpoasnfa76pfcZLmcfl_JSON',
    'cdc_adoQpoasnfa76pfcZLmcfl_Object',
    'cdc_adoQpoasnfa76pfcZLmcfl_Proxy',
    '$cdc_asdjflasutopfhvcZLmcfl_',
    '$wdc_',
    '_phantom', 'phantom', 'callPhantom',
    '__nightmare',
    '_selenium', 'callSelenium', '_Selenium_IDE_Recorder',
    'webdriver',
  ];
  // Document-level tells — selenium/firefox driver injects these into document.
  const DOC_TELLS = [
    '__webdriver_evaluate',
    '__selenium_evaluate',
    '__webdriver_script_function',
    '__webdriver_script_func',
    '__webdriver_script_fn',
    '__fxdriver_evaluate',
    '__driver_unwrapped',
    '__webdriver_unwrapped',
    '__driver_evaluate',
    '__selenium_unwrapped',
    '__fxdriver_unwrapped',
  ];
  // documentElement attributes selenium grids set (won't be set by us, but
  // strip in case the page itself sets them as a heartbeat probe).
  const DOC_ELEMENT_ATTRS = ['selenium', 'webdriver', 'driver'];

  // Pass 1: delete now (handles anything already injected by a wrapper).
  for (const k of WINDOW_TELLS) { try { delete window[k]; } catch (_) {} }
  for (const k of DOC_TELLS) { try { delete document[k]; } catch (_) {} }

  // Pass 2: install a Proxy-style trap that re-deletes on every set. Catches
  // late injections (some chromedrivers add props after navigation completes).
  const installSetTrap = (target, keys) => {
    for (const k of keys) {
      try {
        Object.defineProperty(target, k, {
          configurable: true,
          set(_v) { /* swallow assignment, never let it land */ },
          get() { return undefined; /* but `in target` is now TRUE — see below */ },
        });
        // Counter-act: immediately delete so `in` returns false.
        delete target[k];
      } catch (_) {}
    }
  };
  // Skip the property-creating trap entirely — it's the cure worse than the
  // disease (creates the property, fails `in` checks). Just rely on the delete.
  // If a late-injection becomes a real problem, switch to a window-level Proxy.

  // Pass 3: clear documentElement attrs once the DOM is up.
  const stripDocElement = () => {
    if (!document.documentElement) return;
    for (const attr of DOC_ELEMENT_ATTRS) {
      try { document.documentElement.removeAttribute(attr); } catch (_) {}
    }
  };
  if (document.documentElement) {
    stripDocElement();
  } else {
    document.addEventListener('DOMContentLoaded', stripDocElement, { once: true });
  }

  // ──────────────────────── 2. navigator hardening ──────────────────────────

  // navigator.webdriver — must be undefined (matches real Chrome). nodriver
  // already does this; defense-in-depth via Navigator.prototype.
  try {
    defineNative(Navigator.prototype, 'webdriver', {
      get() { return undefined; }, configurable: true, enumerable: true,
    });
  } catch (_) {}

  // navigator.plugins — empty array is a tell. Install five realistic PDF entries
  // matching Chrome 120+'s default install. namedItem/item methods + Symbol.iterator.
  try {
    if (!navigator.plugins || navigator.plugins.length === 0) {
      const mkPlugin = (name, filename, description) => ({
        name, filename, description, length: 1,
        0: { type: 'application/pdf', suffixes: 'pdf', description, enabledPlugin: null },
        item(i) { return this[i] || null; },
        namedItem(n) { return this[0]?.type === n ? this[0] : null; },
      });
      const fakePlugins = [
        mkPlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
        mkPlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
        mkPlugin('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
        mkPlugin('Microsoft Edge PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
        mkPlugin('WebKit built-in PDF', 'internal-pdf-viewer', 'Portable Document Format'),
      ];
      Object.defineProperty(fakePlugins, 'item', {
        value: function (i) { return this[i] || null; }, enumerable: false,
      });
      Object.defineProperty(fakePlugins, 'namedItem', {
        value: function (n) { return this.find(p => p.name === n) || null; }, enumerable: false,
      });
      Object.defineProperty(fakePlugins, 'refresh', { value: function () {}, enumerable: false });
      maskNative(fakePlugins.item);
      maskNative(fakePlugins.namedItem);
      maskNative(fakePlugins.refresh);

      defineNative(Navigator.prototype, 'plugins', {
        get() { return fakePlugins; }, configurable: true, enumerable: true,
      });
      defineNative(Navigator.prototype, 'mimeTypes', {
        get() {
          const mt = [
            { type: 'application/pdf', description: 'Portable Document Format', suffixes: 'pdf', enabledPlugin: fakePlugins[0] },
            { type: 'text/pdf', description: 'Portable Document Format', suffixes: 'pdf', enabledPlugin: fakePlugins[0] },
          ];
          mt.item = (i) => mt[i] || null;
          mt.namedItem = (n) => mt.find(m => m.type === n) || null;
          return mt;
        },
        configurable: true, enumerable: true,
      });
    }
  } catch (_) {}

  // hardwareConcurrency — pin to a realistic value seeded per session. Always
  // returning 8 across all sessions is itself a fingerprint (the "fake-stealth"
  // tell). Use the session seed → consistent within session, varies between.
  try {
    const cores = pick([4, 6, 8, 8, 12, 16], 1);
    defineNative(Navigator.prototype, 'hardwareConcurrency', {
      get() { return cores; }, configurable: true, enumerable: true,
    });
  } catch (_) {}

  // deviceMemory — same approach. Chrome rounds to {0.25, 0.5, 1, 2, 4, 8}.
  try {
    const mem = pick([4, 8, 8, 16], 2);
    defineNative(Navigator.prototype, 'deviceMemory', {
      get() { return mem; }, configurable: true, enumerable: true,
    });
  } catch (_) {}

  // languages — minimum ['en-US','en'] (empty array is a tell)
  try {
    if (!navigator.languages || navigator.languages.length === 0) {
      defineNative(Navigator.prototype, 'languages', {
        get() { return ['en-US', 'en']; }, configurable: true, enumerable: true,
      });
    }
  } catch (_) {}

  // Permissions API consistency. Real Chrome returns 'prompt' for notifications
  // when no preference is set. Bot detectors check this — automation usually
  // returns 'denied'.
  try {
    const _origQuery = navigator.permissions?.query?.bind(navigator.permissions);
    if (_origQuery) {
      const patchedQuery = function (params) {
        if (params?.name === 'notifications') {
          return Promise.resolve({
            state: Notification?.permission === 'denied' ? 'denied' : 'prompt',
            onchange: null,
          });
        }
        return _origQuery(params);
      };
      maskNative(patchedQuery);
      navigator.permissions.query = patchedQuery;
    }
  } catch (_) {}

  // mediaDevices.enumerateDevices — empty array is a tell
  try {
    if (navigator.mediaDevices && typeof navigator.mediaDevices.enumerateDevices === 'function') {
      const _origEnum = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
      const patched = async function () {
        const real = await _origEnum();
        if (real && real.length > 0) return real;
        // Fallback: realistic default device set (no labels — labels require permission).
        return [
          { deviceId: 'default', kind: 'audioinput', label: '', groupId: 'g0' },
          { deviceId: 'comms', kind: 'audioinput', label: '', groupId: 'g0' },
          { deviceId: 'default', kind: 'audiooutput', label: '', groupId: 'g0' },
          { deviceId: '', kind: 'videoinput', label: '', groupId: '' },
        ];
      };
      maskNative(patched);
      navigator.mediaDevices.enumerateDevices = patched;
    }
  } catch (_) {}

  // ──────────────────────── 3. canvas fingerprint noise ──────────────────────
  // We don't replace the canvas (sites need real rendering); we add a deterministic
  // 1-bit perturbation to the readback. Detection scripts hash toDataURL output;
  // our noise position is keyed on (canvas content hash + session seed) so:
  //   - same canvas content + same session  → same DataURL (real-browser behavior)
  //   - same canvas content + new session   → different DataURL (anti-tracking)
  //   - different canvas content            → different DataURL (real behavior)
  // Cached per canvas via WeakMap to avoid re-encoding on repeat calls.
  try {
    const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    const _origToBlob = HTMLCanvasElement.prototype.toBlob;
    const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    const cache = new WeakMap();

    function contentHash(imageData) {
      let h = SEED;
      const d = imageData.data;
      // Sample every 64th byte for speed — collision risk on identical-looking
      // canvases is acceptable (they'd hash the same anyway).
      for (let i = 0; i < d.length; i += 64) h = ((h * 31) + d[i]) | 0;
      return h >>> 0;
    }

    function noisedDataURL(canvas, args) {
      try {
        const ctx = canvas.getContext('2d');
        if (!ctx || !canvas.width || !canvas.height) {
          return _origToDataURL.apply(canvas, args);
        }
        const imageData = _origGetImageData.call(ctx, 0, 0, canvas.width, canvas.height);
        const hash = contentHash(imageData);
        const cacheKey = hash + '|' + (args[0] || 'image/png') + '|' + (args[1] || '');
        let canvasCache = cache.get(canvas);
        if (canvasCache && canvasCache[cacheKey]) return canvasCache[cacheKey];

        // Apply deterministic 1-bit XOR at content-hash-derived position,
        // encode, then restore the canvas to its original state.
        const idx = (hash + 1) % imageData.data.length;
        imageData.data[idx] = imageData.data[idx] ^ 1;
        ctx.putImageData(imageData, 0, 0);
        const result = _origToDataURL.apply(canvas, args);
        imageData.data[idx] = imageData.data[idx] ^ 1;
        ctx.putImageData(imageData, 0, 0);

        if (!canvasCache) { canvasCache = {}; cache.set(canvas, canvasCache); }
        canvasCache[cacheKey] = result;
        return result;
      } catch (_) {
        return _origToDataURL.apply(canvas, args);
      }
    }

    const patchedToDataURL = function (...args) { return noisedDataURL(this, args); };
    const patchedToBlob = function (cb, ...args) {
      // toBlob is async — we just call original; the deterministic encoding still
      // hashes the same pre-noise content. Most fingerprinters use toDataURL.
      return _origToBlob.call(this, cb, ...args);
    };
    maskNative(patchedToDataURL);
    maskNative(patchedToBlob);
    HTMLCanvasElement.prototype.toDataURL = patchedToDataURL;
    HTMLCanvasElement.prototype.toBlob = patchedToBlob;
  } catch (_) {}

  // ───────────────────── 4. WebGL UNMASKED renderer/vendor ───────────────────
  // Chrome's privacy.resistFingerprinting reports WebKit/WebGL strings, but
  // sites cross-check with UNMASKED_VENDOR_WEBGL (extension WEBGL_debug_renderer_info).
  // Pool of realistic ANGLE strings, picked deterministically by session seed.
  try {
    const GPU_POOL = [
      ['Google Inc. (NVIDIA)', 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)'],
      ['Google Inc. (NVIDIA)', 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)'],
      ['Google Inc. (NVIDIA)', 'ANGLE (NVIDIA, NVIDIA GeForce RTX 2070 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)'],
      ['Google Inc. (Intel)',  'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)'],
      ['Google Inc. (Intel)',  'ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)'],
      ['Google Inc. (AMD)',    'ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)'],
      ['Google Inc. (AMD)',    'ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)'],
      ['Google Inc. (NVIDIA)', 'ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)'],
    ];
    const [gpuVendor, gpuRenderer] = GPU_POOL[Math.floor(fpRand(42) * GPU_POOL.length)];

    function patchGetParameter(proto) {
      if (!proto) return;
      const _orig = proto.getParameter;
      const patched = function (pname) {
        // 0x9245 = UNMASKED_VENDOR_WEBGL, 0x9246 = UNMASKED_RENDERER_WEBGL
        if (pname === 0x9245) return gpuVendor;
        if (pname === 0x9246) return gpuRenderer;
        return _orig.call(this, pname);
      };
      maskNative(patched);
      proto.getParameter = patched;
    }
    if (typeof WebGLRenderingContext !== 'undefined') patchGetParameter(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined') patchGetParameter(WebGL2RenderingContext.prototype);
  } catch (_) {}

  // ───────────────────────── 5. AudioContext noise ───────────────────────────
  // Audio fingerprinting hashes the output of an OfflineAudioContext rendering
  // a known oscillator + compressor chain. Add tiny noise to getChannelData/
  // copyFromChannel reads → hash differs every session, audio still works.
  try {
    if (typeof AudioBuffer !== 'undefined') {
      const _origGetChannelData = AudioBuffer.prototype.getChannelData;
      const patchedGetChannelData = function (channel) {
        const data = _origGetChannelData.call(this, channel);
        if (data.length > 0) {
          const idx = Math.floor(fpRand(channel + this.length) * data.length);
          data[idx] = data[idx] + (fpRand(idx) - 0.5) * 1e-7;
        }
        return data;
      };
      maskNative(patchedGetChannelData);
      AudioBuffer.prototype.getChannelData = patchedGetChannelData;
    }
  } catch (_) {}

  // ──────────────────────── 6. WebRTC ICE + SDP filtering ────────────────────
  // Three-layer leak prevention:
  //   a) addIceCandidate — drop incoming `typ host` (LAN) candidates
  //   b) createOffer / createAnswer — strip `a=candidate:...typ host` lines
  //      from the outgoing SDP so the remote peer never learns our LAN IP
  //   c) onicecandidate event — fire only for srflx/relay, not host
  // Modern Chrome uses mDNS for host candidates, BUT real LAN IPs still leak
  // in createOffer SDP under specific configurations (no STUN servers, plain
  // RTCPeerConnection w/o config). creepjs probes this directly.
  try {
    const RTC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
    if (RTC) {
      function stripHostFromSdp(sdp) {
        // Real Chrome 100+ uses mDNS-obfuscated hostnames for host candidates
        // (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.local). These DON'T leak the
        // real LAN IP and ARE expected to appear in real-Chrome SDP — stripping
        // them entirely is itself a fingerprintable tell ("no host candidates =
        // anti-fingerprint tool"). Strip only host candidates that contain a
        // raw IPv4 / IPv6 address (legacy leak).
        if (!sdp) return sdp;
        return sdp.replace(/a=candidate:[^\r\n]*typ host[^\r\n]*\r?\n/gi, (line) => {
          // Keep mDNS-style (.local) candidates; drop raw-IP host candidates.
          if (/\.local\b/i.test(line)) return line;
          return '';
        });
      }

      const _origAddIce = RTC.prototype.addIceCandidate;
      const patchedAddIce = function (candidate) {
        if (candidate?.candidate && /typ host/i.test(candidate.candidate)) {
          return Promise.resolve();
        }
        return _origAddIce.call(this, candidate);
      };
      maskNative(patchedAddIce);
      RTC.prototype.addIceCandidate = patchedAddIce;

      const wrapSdpProducer = (origName) => {
        const orig = RTC.prototype[origName];
        const patched = async function (...args) {
          const result = await orig.apply(this, args);
          if (result?.sdp) {
            try {
              Object.defineProperty(result, 'sdp', {
                value: stripHostFromSdp(result.sdp),
                writable: false, configurable: true, enumerable: true,
              });
            } catch (_) {
              // SDP is on a frozen RTCSessionDescriptionInit dict — recreate.
              return { type: result.type, sdp: stripHostFromSdp(result.sdp) };
            }
          }
          return result;
        };
        maskNative(patched);
        RTC.prototype[origName] = patched;
      };
      wrapSdpProducer('createOffer');
      wrapSdpProducer('createAnswer');

      // setLocalDescription — also strip on the way in (some libs build SDP
      // manually then call setLocalDescription with it).
      const _origSetLocal = RTC.prototype.setLocalDescription;
      const patchedSetLocal = function (desc) {
        if (desc?.sdp) {
          try { desc.sdp = stripHostFromSdp(desc.sdp); } catch (_) {}
        }
        return _origSetLocal.call(this, desc);
      };
      maskNative(patchedSetLocal);
      RTC.prototype.setLocalDescription = patchedSetLocal;
    }
  } catch (_) {}

  // ─────────────────────── 6b. font fingerprint clamp ────────────────────────
  // Font fingerprinting works two ways:
  //   - document.fonts.check('12px FontName') — direct API
  //   - canvas measureText() of a known string with fontFamily fallback —
  //     measure widths differ if the font is installed
  // We pin the "is this font installed" answer to a fixed list matching the
  // UA platform (Linux Chrome ships with DejaVu/Liberation/Noto by default).
  // This kills the font-installation entropy that otherwise uniquely IDs the
  // user's machine across sessions.
  try {
    // Linux Chrome stable default font set (sampled from a clean Debian Chrome
    // install). Matches "X11; Linux x86_64" UA. Spoof for other UAs as needed.
    const LINUX_FONTS = new Set([
      'sans-serif', 'serif', 'monospace', 'cursive', 'fantasy', 'system-ui',
      'DejaVu Sans', 'DejaVu Serif', 'DejaVu Sans Mono',
      'Liberation Sans', 'Liberation Serif', 'Liberation Mono',
      'Noto Sans', 'Noto Serif', 'Noto Sans Mono', 'Noto Color Emoji',
      'FreeSans', 'FreeSerif', 'FreeMono',
      'Ubuntu', 'Ubuntu Mono', 'Cantarell',
      'Arial', 'Helvetica', 'Times New Roman', 'Courier New',
      'Tahoma', 'Verdana', 'Georgia',
    ]);

    if (document.fonts && document.fonts.check) {
      const _origCheck = document.fonts.check.bind(document.fonts);
      const patched = function (font, text) {
        // Parse the family from a CSS font shorthand.
        const m = String(font).match(/(?:\d+(?:\.\d+)?(?:px|em|rem|%))\s+(?:"([^"]+)"|'([^']+)'|([^,;\s][^,;]*))/);
        const family = (m && (m[1] || m[2] || m[3]))?.trim();
        if (family && !LINUX_FONTS.has(family)) return false;
        return _origCheck(font, text);
      };
      maskNative(patched);
      document.fonts.check = patched;
    }

    // Canvas measureText fingerprint — we don't replace measureText (real
    // sites need it for layout) but we ensure consistency: same string + same
    // font + same size → same width across all sessions. The toDataURL cache
    // (above) already covers the visual-rendering path; this covers the
    // measureText probe specifically used by font enumeration libs.
    // Strategy: leave measureText alone — width depends on the actual font,
    // and we've clamped which fonts respond as installed via fonts.check.
  } catch (_) {}

  // ─────────────────────── 6c. AudioContext deeper noise ─────────────────────
  // Beyond getChannelData: audio fingerprinters use OfflineAudioContext +
  // DynamicsCompressor + Oscillator → startRendering → hash of the rendered
  // buffer. Our getChannelData patch noises reads from the buffer, but the
  // RENDER itself is deterministic. Add: noise the AudioBuffer.copyFromChannel
  // path (used to extract render output without getChannelData).
  try {
    if (typeof AudioBuffer !== 'undefined' && AudioBuffer.prototype.copyFromChannel) {
      const _origCopy = AudioBuffer.prototype.copyFromChannel;
      const patched = function (destination, channelNumber, ...rest) {
        _origCopy.call(this, destination, channelNumber, ...rest);
        if (destination?.length > 0) {
          const idx = Math.floor(fpRand(channelNumber + this.length) * destination.length);
          destination[idx] = destination[idx] + (fpRand(idx) - 0.5) * 1e-7;
        }
        return undefined;
      };
      maskNative(patched);
      AudioBuffer.prototype.copyFromChannel = patched;
    }
  } catch (_) {}

  // ───────────────────── 7. Intl.DateTimeFormat consistency ──────────────────
  // If the timezone CDP override sets a TZ but JS Intl reports UTC, that's a
  // mismatch tell. Force consistency.
  try {
    const _origResolved = Intl.DateTimeFormat.prototype.resolvedOptions;
    const patched = function () {
      const r = _origResolved.call(this);
      if (r.timeZone === 'UTC' && window.__umbra_tz) {
        r.timeZone = window.__umbra_tz;
      }
      return r;
    };
    maskNative(patched);
    Intl.DateTimeFormat.prototype.resolvedOptions = patched;
  } catch (_) {}

  // ──────────────────── 8. chrome.runtime presence guarantee ─────────────────
  // Headless Chrome has window.chrome but chrome.runtime is partially missing.
  // Sites probe chrome.runtime.connect / chrome.app — fill the gaps.
  try {
    if (!window.chrome) {
      window.chrome = {};
    }
    if (!window.chrome.runtime) {
      window.chrome.runtime = {
        OnInstalledReason: { CHROME_UPDATE: 'chrome_update', INSTALL: 'install', SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update' },
        OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' },
        PlatformArch: { ARM: 'arm', ARM64: 'arm64', MIPS: 'mips', MIPS64: 'mips64', X86_32: 'x86-32', X86_64: 'x86-64' },
        PlatformOs: { ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', OPENBSD: 'openbsd', WIN: 'win' },
        RequestUpdateCheckStatus: { NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available' },
        connect() { return undefined; },
        sendMessage() {},
      };
      maskNative(window.chrome.runtime.connect);
      maskNative(window.chrome.runtime.sendMessage);
    }
    if (!window.chrome.app) {
      window.chrome.app = {
        isInstalled: false,
        InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
        RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
      };
    }
    if (!window.chrome.csi) { window.chrome.csi = function () { return {}; }; maskNative(window.chrome.csi); }
    if (!window.chrome.loadTimes) { window.chrome.loadTimes = function () { return {}; }; maskNative(window.chrome.loadTimes); }
  } catch (_) {}

  // ────────────────── 9. event.isTrusted: NOT patched (here's why) ───────────
  // Event.isTrusted is enforced at Chrome's C++ binding layer, not via a JS
  // property descriptor. There is no own-property to override on Event.prototype
  // (Object.getOwnPropertyDescriptor returns undefined). The only path to a
  // trusted event is to dispatch via CDP (Input.dispatchKeyEvent /
  // Input.dispatchMouseEvent) which produces real OS-level trusted events.
  // The umbra drivers (driver/aria.py, driver/interact.py) dispatch via CDP
  // for exactly this reason — JS-side dispatchEvent calls remain untrusted by
  // design. Don't add a fake patch here; it would silently fail and mask the
  // real architectural decision.
})();
