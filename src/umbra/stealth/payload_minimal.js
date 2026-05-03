/* umbra stealth payload — MINIMAL mode.
 *
 * Insight from creepjs benchmarking (vs real Chrome with no extensions):
 * vanilla Chrome 138 with no extensions scores 0% headless / 0% stealth on
 * creepjs. The full payload.js scored 33% / 40% — NOT because of being
 * headless but because the patches themselves look like stealth-lib signatures.
 *
 * This minimal payload only does what nodriver doesn't already cover OR what
 * is architecturally required (delete tells, isTrusted via CDP, Page.enable
 * pipeline). Everything fingerprint-related is REMOVED — real Chrome leaks
 * its real fingerprint, and that's what we want to look like.
 *
 * Use minimal when: production stealth, sites with real fingerprint detectors.
 * Use full payload.js when: anti-tracking / privacy mode, tracker blocking
 * is more important than per-site bot evasion.
 */
(() => {
  'use strict';

  if (window.__umbra_stealth_loaded) return;
  Object.defineProperty(window, '__umbra_stealth_loaded', {
    value: 'minimal', writable: false, enumerable: false, configurable: false,
  });

  // ─────────────────────── 1. delete automation tells ──────────────────────
  // Pure delete, no shadow getters (those would CREATE the property and fail
  // `in` operator checks). nodriver already strips most of these; we re-strip
  // for defense-in-depth in case something injects them late.
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
  const DOC_TELLS = [
    '__webdriver_evaluate', '__selenium_evaluate',
    '__webdriver_script_function', '__webdriver_script_func', '__webdriver_script_fn',
    '__fxdriver_evaluate',
    '__driver_unwrapped', '__webdriver_unwrapped', '__driver_evaluate',
    '__selenium_unwrapped', '__fxdriver_unwrapped',
  ];
  for (const k of WINDOW_TELLS) { try { delete window[k]; } catch (_) {} }
  for (const k of DOC_TELLS) { try { delete document[k]; } catch (_) {} }

  // documentElement attributes selenium grids set — strip once DOM is up.
  const stripDocElement = () => {
    if (!document.documentElement) return;
    for (const attr of ['selenium', 'webdriver', 'driver']) {
      try { document.documentElement.removeAttribute(attr); } catch (_) {}
    }
  };
  if (document.documentElement) stripDocElement();
  else document.addEventListener('DOMContentLoaded', stripDocElement, { once: true });

  // ────────────── 2. navigator.webdriver = undefined (defense in depth) ─────
  // nodriver does this at launch via --disable-blink-features=AutomationControlled.
  // We re-confirm at the JS layer in case anything overrides it later.
  // CRITICAL: do NOT use defineProperty on Navigator.prototype.webdriver — that
  // creates the property and fails `'webdriver' in window` checks. Just verify.
  // (Native Chrome behavior: webdriver isn't a property of Navigator.prototype
  // when --disable-blink-features=AutomationControlled is set.)

  // ────────────── 3. plugins/permissions: DO NOTHING ─────────────────────────
  // Real Chrome 138+ ships with a real plugins array (often just 1-2 entries
  // for PDF Viewer; may be 0 if PDF disabled). The 5-fake-PDF pattern is the
  // EXACT fingerprint of undetected-chromedriver / puppeteer-extra-stealth and
  // creepjs flags it instantly. Let real Chrome's array stand.

  // ────────────── 4. canvas/audio/WebGL: DO NOTHING ─────────────────────────
  // Real Chrome leaks its real fingerprint — that IS the user's identity. Our
  // noise made the fingerprint UNIQUE per session (anti-tracking) but ALSO
  // made it inconsistent with the GPU we claim, which IS a stealth tell.
  //
  // For privacy/anti-tracking → use the full payload.js (--mode=full).
  // For bot evasion → vanilla Chrome's real fingerprint passes everywhere.

  // ────────────── 5. Intl timezone consistency (if CDP override set) ────────
  // Cheap, non-detectable, only kicks in when caller sets a timezone.
  try {
    if (window.__umbra_tz) {
      const _origResolved = Intl.DateTimeFormat.prototype.resolvedOptions;
      const patched = function () {
        const r = _origResolved.call(this);
        if (r.timeZone === 'UTC') r.timeZone = window.__umbra_tz;
        return r;
      };
      // Mask toString — Function.prototype.toString trap below.
      const _origToString = Function.prototype.toString;
      Object.defineProperty(patched, 'name', { value: 'resolvedOptions', configurable: true });
      Function.prototype.toString = function () {
        if (this === patched) return `function resolvedOptions() { [native code] }`;
        return _origToString.call(this);
      };
      Intl.DateTimeFormat.prototype.resolvedOptions = patched;
    }
  } catch (_) {}

  // ────────────── 6. WebRTC: DO NOTHING ─────────────────────────────────────
  // Real Chrome (no extensions) leaks raw LAN IPs in createOffer SDP — that's
  // confirmed real-Chrome behavior (creepjs data: 192.168.x.x typ host). Our
  // SDP filter zeroed out all host candidates → that's MORE suspicious than
  // leaking. The acceptable mitigation is mDNS-obfuscation, but Chrome
  // toggles that based on configuration; faking it consistently is hard.
  //
  // Recommendation: route the browser through a proxy/VPN if LAN IP exposure
  // matters. The IP leak isn't a bot signal — it's a privacy signal.

  // ────────────── 7. event.isTrusted: NOT PATCHED (architectural) ───────────
  // umbra dispatches events via CDP Input.dispatchKeyEvent / dispatchMouseEvent.
  // Those are routed through the OS-input pipeline → Chrome marks them
  // isTrusted=true at the C++ binding layer. JS-side dispatchEvent calls
  // remain isTrusted=false (cannot be patched from JS — see payload.js notes).
})();
