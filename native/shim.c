/* libumbra_shim.so — LD_PRELOAD hooks for Chrome.
 *
 * Two jobs:
 *   1) Block dlopen() of shared libraries Chrome doesn't need in our use case
 *      (printing, Samba, keyring, system notifications, etc). Chrome handles
 *      "lib not found" gracefully — these features just become unavailable.
 *      Saves ~30-80MB resident RAM per Chrome process tree (counts each
 *      renderer, gpu process, utility process — adds up).
 *
 *   2) Mask getenv() probes that leak headless-mode presence. CHROME_HEADLESS
 *      is set in some environments to indicate headless launch; some sites
 *      probe for it via JS-bridged native calls (rare but happens).
 *
 * Build:
 *   make -C umbra/native
 *   → produces umbra/native/libumbra_shim.so
 *
 * Use:
 *   LD_PRELOAD=/abs/path/libumbra_shim.so chrome ...
 *   or set StealthOptions(ld_preload_shim="/abs/path/libumbra_shim.so")
 *
 * Rust target later: same hooks, ~10x smaller .so. Same effect.
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdarg.h>

/* ------------------------------------------------------------------------ *
 * Library block-list.
 *
 * Each entry is matched as a substring in the dlopen() name argument. So
 * "libcups" catches both "libcups.so.2" and "libcups-2.so.0".
 *
 * Blocking the WRONG lib crashes Chrome — these have been validated to be
 * safe to block in headless automation (no printing, no Samba browse, no
 * desktop notifications, no keyring access since we --use-mock-keychain).
 *
 * If you start seeing crashes, set UMBRA_SHIM_VERBOSE=1 to log every block.
 * ------------------------------------------------------------------------ */

static const char* BLOCKED_LIBS[] = {
    "libcups",          /* CUPS printing */
    "libsmbclient",     /* Samba/Windows file shares */
    "libgnome-keyring", /* gnome-keyring (--use-mock-keychain replaces) */
    "libsecret-1",      /* libsecret (--use-mock-keychain replaces) */
    "libcanberra",      /* Sound notifications */
    "libnotify",        /* Desktop notifications */
    "libpci",           /* PCI device enumeration (chrome hw discovery) */
    "libgudev",         /* udev wrapper (hw enumeration) */
    "libavahi",         /* Avahi mDNS / Zeroconf */
    "libdconf",         /* GSettings backend */
    "libudev",          /* udev itself — chrome can use without it */
    /* Note on what we deliberately keep:
     *   libGL/libGLX/libEGL — needed for real GPU rendering
     *   libvulkan          — needed for ANGLE Vulkan backend
     *   libdrm             — needed for direct rendering
     *   libwayland-client  — needed if Wayland session
     *   libX11             — needed for X11 session integration
     *   libdbus-1          — chrome IPCs over D-Bus, blocking it crashes
     *   libnss3 / libssl   — TLS, do not touch
     */
    NULL,
};

/* Env vars that leak headless / automation presence. NULL them out. */
static const char* MASKED_ENVS[] = {
    "CHROME_HEADLESS",
    "GOOGLE_API_KEY",       /* dev-build identifier */
    "PUPPETEER_EXECUTABLE_PATH",
    "PLAYWRIGHT_BROWSERS_PATH",
    NULL,
};

static int verbose = -1;

static int is_verbose(void) {
    if (verbose < 0) {
        const char* v = getenv("UMBRA_SHIM_VERBOSE");
        verbose = (v && v[0] && v[0] != '0') ? 1 : 0;
    }
    return verbose;
}

static int matches_block(const char* name, const char* const* list) {
    if (!name) return 0;
    for (int i = 0; list[i]; i++) {
        if (strstr(name, list[i])) return 1;
    }
    return 0;
}

/* ------------------------------------------------------------------------ *
 * dlopen() hook
 *
 * The trick is that we ARE inside a dlopen path when the dynamic linker
 * resolves us — so calling dlsym() too early may recurse. Use RTLD_NEXT
 * which is safe: it skips OUR symbol and finds the next one in the chain
 * (the real glibc dlopen).
 * ------------------------------------------------------------------------ */

typedef void* (*real_dlopen_t)(const char* filename, int flags);
typedef void* (*real_dlmopen_t)(long lmid, const char* filename, int flags);

void* dlopen(const char* filename, int flags) {
    static real_dlopen_t real = NULL;
    if (!real) real = (real_dlopen_t)dlsym(RTLD_NEXT, "dlopen");

    if (filename && matches_block(filename, BLOCKED_LIBS)) {
        if (is_verbose()) fprintf(stderr, "[umbra] dlopen blocked: %s\n", filename);
        return NULL;  /* Pretend lib not found — Chrome handles gracefully */
    }
    return real(filename, flags);
}

/* dlmopen — namespaced variant. Same logic. */
void* dlmopen(long lmid, const char* filename, int flags) {
    static real_dlmopen_t real = NULL;
    if (!real) real = (real_dlmopen_t)dlsym(RTLD_NEXT, "dlmopen");

    if (filename && matches_block(filename, BLOCKED_LIBS)) {
        if (is_verbose()) fprintf(stderr, "[umbra] dlmopen blocked: %s\n", filename);
        return NULL;
    }
    return real(lmid, filename, flags);
}

/* ------------------------------------------------------------------------ *
 * getenv() hook
 *
 * Mask environment variables that signal automation/headless launch. Most
 * Chrome flag-based detection happens via process command line, but a few
 * code paths read env vars directly.
 * ------------------------------------------------------------------------ */

typedef char* (*real_getenv_t)(const char* name);

char* getenv(const char* name) {
    static real_getenv_t real = NULL;
    if (!real) real = (real_getenv_t)dlsym(RTLD_NEXT, "getenv");

    /* glibc annotates `name` nonnull, so no explicit NULL guard. */
    for (int i = 0; MASKED_ENVS[i]; i++) {
        if (strcmp(name, MASKED_ENVS[i]) == 0) {
            if (is_verbose()) fprintf(stderr, "[umbra] getenv masked: %s\n", name);
            return NULL;
        }
    }
    return real(name);
}

/* ------------------------------------------------------------------------ *
 * Constructor: announce ourselves if verbose.
 * ------------------------------------------------------------------------ */

__attribute__((constructor))
static void umbra_shim_init(void) {
    if (is_verbose()) {
        fprintf(stderr, "[umbra] shim loaded (block %d libs, mask %d envs)\n",
                (int)(sizeof(BLOCKED_LIBS) / sizeof(*BLOCKED_LIBS)) - 1,
                (int)(sizeof(MASKED_ENVS) / sizeof(*MASKED_ENVS)) - 1);
    }
}
