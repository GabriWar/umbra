"""End-to-end audit: hammer every umbra MCP tool, log pass/fail/edges.

Hits 60+ tools across browser/aria/input/extraction/visual/JS/devtools/stealth/
network/files/sessions/multi-browser/batch. Each tool gets at least one happy
path + at least one edge case (missing arg, bad selector, dedup hit, etc).

Skipped: handoff_start/handoff_wait/request_user_input (need human interaction).
Skipped by default: check_detection (slow, ~7s).

Run from anywhere:
    UMBRA_CONTAINER=1 python tests/test_full_audit.py

Requires umbra installed (pip install -e .[all]) + Chrome on PATH.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))
os.environ.setdefault("UMBRA_CONTAINER", "1")

from umbra import server  # noqa: E402


PASSED = 0
FAILED = 0
RESULTS = []


async def call(tool_name, **kwargs):
    tool = await server.mcp.get_tool(tool_name)
    return await tool.fn(**kwargs)


async def t(label, coro, expect_ok=True, preview_chars=200):
    """Run a test. Logs ms/size + preview of the actual response."""
    global PASSED, FAILED
    t0 = time.perf_counter()
    try:
        result = await coro()
        ms = int((time.perf_counter() - t0) * 1000)
        # Truncate base64 image fields aggressively in preview
        as_json = json.dumps(result, default=str, separators=(',', ':'))
        size = len(as_json)
        # For preview, replace giant base64 with marker
        preview = as_json
        if '"b64":"' in preview:
            import re as _re
            preview = _re.sub(r'"b64":"[^"]{50,}"', '"b64":"<...binary...>"', preview)
        if len(preview) > preview_chars:
            preview = preview[:preview_chars] + f'...[+{len(as_json)-preview_chars}c]'
        ok = (result is not None) if expect_ok else True
        if ok:
            PASSED += 1
            print(f'  PASS [{ms:4}ms {size:5}b] {label}', flush=True)
            print(f'    → {preview}', flush=True)
        else:
            FAILED += 1
            print(f'  FAIL [{ms:4}ms] {label}', flush=True)
            print(f'    → {preview}', flush=True)
        RESULTS.append((label, "PASS" if ok else "FAIL", ms, size, preview))
    except Exception as e:
        ms = int((time.perf_counter() - t0) * 1000)
        if not expect_ok:
            PASSED += 1
            RESULTS.append((label, "PASS-err", ms, 0, str(e)[:200]))
            print(f'  PASS [{ms:4}ms] {label} (expected err)', flush=True)
            print(f'    → {str(e)[:preview_chars]}', flush=True)
        else:
            FAILED += 1
            RESULTS.append((label, "EXC", ms, 0, str(e)[:200]))
            print(f'  EXC  [{ms:4}ms] {label}', flush=True)
            print(f'    → {str(e)[:preview_chars]}', flush=True)


async def main():
    print('=== boot ===', flush=True)
    await t('spawn(default)', lambda: call('spawn', url='https://example.com'))
    await asyncio.sleep(1)

    print('\n=== A. browser/tab mgmt ===', flush=True)
    await t('list_tabs', lambda: call('list_tabs'))
    await t('list_browsers', lambda: call('list_browsers'))
    await t('switch_tab(t0)', lambda: call('switch_tab', tab_id='t0'))
    await t('navigate(httpbin)', lambda: call('navigate', tab_id='t0', url='https://httpbin.org/forms/post'))
    await asyncio.sleep(1)
    await t('reload', lambda: call('reload', tab_id='t0'))
    await asyncio.sleep(1)
    await t('back', lambda: call('back', tab_id='t0'))
    await asyncio.sleep(1)
    await t('forward', lambda: call('forward', tab_id='t0'))
    await asyncio.sleep(1)
    # edge
    await t('close(missing)', lambda: call('close', tab_id='nonexistent'))

    print('\n=== B. ARIA ===', flush=True)
    await t('aria_snapshot', lambda: call('aria_snapshot', tab_id='t0'))
    await t('aria_snapshot(dedup hit)', lambda: call('aria_snapshot', tab_id='t0'))
    await t('aria_snapshot(force_refresh)', lambda: call('aria_snapshot', tab_id='t0', force_refresh=True))
    await t('current_state', lambda: call('current_state', tab_id='t0'))
    await t('current_state(dedup)', lambda: call('current_state', tab_id='t0'))
    await t('find_by_text', lambda: call('find_by_text', tab_id='t0', text='Customer name'))
    await t('aria_click(0)', lambda: call('aria_click', tab_id='t0', idx=0))
    await t('aria_type(humanize=False)', lambda: call('aria_type', tab_id='t0', idx=1, text='test', humanize=False))
    await t('fill_form', lambda: call('fill_form', tab_id='t0', fields={'Customer name': 'X', 'Telephone': '111'}))
    # edge
    await t('aria_click(99999)', lambda: call('aria_click', tab_id='t0', idx=99999))

    print('\n=== C. input ===', flush=True)
    await t('press_key(Tab)', lambda: call('press_key', tab_id='t0', key='Tab'))
    await t('press_key(ctrl+a)', lambda: call('press_key', tab_id='t0', key='a', modifiers=['ctrl']))
    await t('scroll(by 200)', lambda: call('scroll', tab_id='t0', dy=200))
    await t('scroll(to_bottom)', lambda: call('scroll', tab_id='t0', to_bottom=True))
    await t('paste_text', lambda: call('paste_text', tab_id='t0', text='paste-test'))
    await t('hover', lambda: call('hover', tab_id='t0', x=200, y=200))
    await t('click_at', lambda: call('click_at', tab_id='t0', x=100, y=100))
    await t('drag', lambda: call('drag', tab_id='t0', x1=50, y1=50, x2=200, y2=200))
    await t('wait_for(timeout)', lambda: call('wait_for', tab_id='t0', timeout_s=0.5))
    await t('wait_for_text', lambda: call('wait_for_text', tab_id='t0', text='Customer', timeout_s=2))
    await t('select_option(missing)', lambda: call('select_option', tab_id='t0', selector='#nope', value='x'))

    print('\n=== D. extraction ===', flush=True)
    await t('extract_text', lambda: call('extract_text', tab_id='t0', max_chars=200))
    await t('extract_text(dedup)', lambda: call('extract_text', tab_id='t0', max_chars=200))
    await t('extract_text(denoise=False)', lambda: call('extract_text', tab_id='t0', max_chars=200, denoise=False))
    await t('extract_links', lambda: call('extract_links', tab_id='t0', max_links=10))
    await t('grep_text(httpbin)', lambda: call('grep_text', tab_id='t0', pattern='Customer', max_matches=3))
    await t('dom_query(input)', lambda: call('dom_query', tab_id='t0', selector='input', max_results=10))
    await t('dom_query(missing)', lambda: call('dom_query', tab_id='t0', selector='#nonexistent', max_results=10))
    await t('inspect_element(legend)', lambda: call('inspect_element', tab_id='t0', selector='legend'))
    await t('inspect_element(missing)', lambda: call('inspect_element', tab_id='t0', selector='#nonexistent'))
    try:
        await t('extract_markdown', lambda: call('extract_markdown', tab_id='t0', max_chars=2000))
    except Exception:
        pass
    await t('clone_element(form)', lambda: call('clone_element', tab_id='t0', selector='form', max_doc_chars=5000))
    await t('clone_element(missing)', lambda: call('clone_element', tab_id='t0', selector='#nope'))

    print('\n=== E. visual ===', flush=True)
    await t('screenshot(q40)', lambda: call('screenshot', tab_id='t0', quality=40))
    await t('screenshot_region', lambda: call('screenshot_region', tab_id='t0', x=0, y=0, w=200, h=100, quality=40))

    print('\n=== F. JS ===', flush=True)
    await t('evaluate(simple)', lambda: call('evaluate', tab_id='t0', expression='1+1'))
    await t('evaluate(syntax err)', lambda: call('evaluate', tab_id='t0', expression='not.valid.js[)'))
    await t('inject_css', lambda: call('inject_css', tab_id='t0', css='body{background:#eee}'))

    print('\n=== G. devtools ===', flush=True)
    await t('get_console_logs', lambda: call('get_console_logs', tab_id='t0'))
    await t('get_network_requests', lambda: call('get_network_requests', tab_id='t0', max_n=5))
    await t('memory_metrics', lambda: call('memory_metrics', tab_id='t0'))
    await t('get_cookies', lambda: call('get_cookies', tab_id='t0'))
    await t('set_cookies', lambda: call('set_cookies', tab_id='t0', cookies=[{'name': 'umbra', 'value': 'v', 'domain': '.httpbin.org'}]))
    await t('get_cookies(after set)', lambda: call('get_cookies', tab_id='t0'))
    await t('set_cookies(missing field)', lambda: call('set_cookies', tab_id='t0', cookies=[{'name': 'x'}]), expect_ok=False)
    await t('clear_cookies', lambda: call('clear_cookies', tab_id='t0'))
    await t('clear_logs', lambda: call('clear_logs', tab_id='t0'))

    print('\n=== H. stealth ===', flush=True)
    await t('set_verbosity(full)', lambda: call('set_verbosity', level='full'))
    await t('set_verbosity(compact)', lambda: call('set_verbosity', level='compact'))
    await t('rotate_fingerprint', lambda: call('rotate_fingerprint', tab_id='t0'))
    # check_detection is slow (~7s) — skip default, only on demand
    print('  (skip check_detection — slow ~7s)')

    print('\n=== I. network ctrl ===', flush=True)
    await t('block_urls', lambda: call('block_urls', tab_id='t0', patterns=['*ads*']))
    await t('block_urls([])', lambda: call('block_urls', tab_id='t0', patterns=[]))
    await t('set_extra_headers', lambda: call('set_extra_headers', tab_id='t0', headers={'X-Test': 'umbra'}))
    await t('set_viewport', lambda: call('set_viewport', tab_id='t0', width=1280, height=720))
    await t('dynamic_hook(block)', lambda: call('dynamic_hook', tab_id='t0', url_pattern='/never-fires', action='block'))

    print('\n=== J. files ===', flush=True)
    await t('setup_downloads', lambda: call('setup_downloads', tab_id='t0', download_dir='/tmp/umbra_dl_test'))
    await t('upload_file(missing)', lambda: call('upload_file', tab_id='t0', selector='input[type=file]', paths=['/tmp/_no_such_file']))
    # wait_for_download will time out — short test only
    await t('wait_for_download(timeout)', lambda: call('wait_for_download', tab_id='t0', download_dir='/tmp/umbra_dl_test', timeout_s=1))

    print('\n=== K. TLS fetch ===', flush=True)
    try:
        await t('tls_fetch(httpbin)', lambda: call('tls_fetch', url='https://httpbin.org/get', max_chars=500))
    except Exception:
        pass

    print('\n=== L. sessions ===', flush=True)
    await t('session_save', lambda: call('session_save', tab_id='t0', name='test-name', passphrase='test-pass'))
    await t('session_list', lambda: call('session_list'))
    await t('session_load', lambda: call('session_load', tab_id='t0', name='test-name', passphrase='test-pass'))
    await t('session_load(wrong pass)', lambda: call('session_load', tab_id='t0', name='test-name', passphrase='wrong'), expect_ok=False)
    await t('session_save(path-traversal-attempt)', lambda: call('session_save', tab_id='t0', name='../../etc/passwd', passphrase='x'))
    await t('session_delete', lambda: call('session_delete', name='test-name'))

    print('\n=== M. multi-browser ===', flush=True)
    await t('spawn(alice)', lambda: call('spawn', url='https://example.com', browser_id='alice'))
    await asyncio.sleep(1)
    await t('list_browsers(2)', lambda: call('list_browsers'))
    await t('close_browser(alice)', lambda: call('close_browser', browser_id='alice'))
    await t('close_browser(missing)', lambda: call('close_browser', browser_id='nope'))

    print('\n=== N. batch ===', flush=True)
    await t('batch(5 reads)', lambda: call('batch', calls=[
        {'tool': 'current_state', 'args': {'tab_id': 't0', 'force_refresh': True}},
        {'tool': 'extract_links', 'args': {'tab_id': 't0', 'max_links': 5, 'force_refresh': True}},
        {'tool': 'list_tabs', 'args': {}},
        {'tool': 'evaluate', 'args': {'tab_id': 't0', 'expression': 'document.title'}},
        {'tool': 'memory_metrics', 'args': {'tab_id': 't0', 'force_refresh': True}},
    ]))
    await t('batch(unknown tool)', lambda: call('batch', calls=[
        {'tool': 'no_such_tool', 'args': {}}
    ], stop_on_error=False))
    await t('batch(stop_on_error)', lambda: call('batch', calls=[
        {'tool': 'no_such_tool', 'args': {}},
        {'tool': 'list_tabs', 'args': {}},
    ], stop_on_error=True))

    print('\n=== N2. GIANT batch (15 calls, mixed read+write) ===', flush=True)
    await t('batch(GIANT 15-call workflow)', lambda: call('batch', calls=[
        # Realistic agent workflow: load page → orient → extract → modify → re-orient
        {'tool': 'navigate', 'args': {'tab_id': 't0', 'url': 'https://example.com'}},
        {'tool': 'wait_for', 'args': {'tab_id': 't0', 'selector': 'h1', 'timeout_s': 5}},
        {'tool': 'current_state', 'args': {'tab_id': 't0', 'force_refresh': True}},
        {'tool': 'aria_snapshot', 'args': {'tab_id': 't0', 'max_items': 30, 'force_refresh': True}},
        {'tool': 'extract_text', 'args': {'tab_id': 't0', 'selector': 'h1', 'max_chars': 200, 'force_refresh': True}},
        {'tool': 'extract_links', 'args': {'tab_id': 't0', 'max_links': 10, 'force_refresh': True}},
        {'tool': 'dom_query', 'args': {'tab_id': 't0', 'selector': 'a, p', 'max_results': 10, 'force_refresh': True}},
        {'tool': 'inspect_element', 'args': {'tab_id': 't0', 'selector': 'h1', 'force_refresh': True}},
        {'tool': 'grep_text', 'args': {'tab_id': 't0', 'pattern': 'Example', 'max_matches': 3}},
        {'tool': 'evaluate', 'args': {'tab_id': 't0', 'expression': 'document.title'}},
        {'tool': 'inject_css', 'args': {'tab_id': 't0', 'css': 'body{outline:2px solid red}'}},
        {'tool': 'screenshot_region', 'args': {'tab_id': 't0', 'x': 0, 'y': 0, 'w': 200, 'h': 100, 'quality': 30}},
        {'tool': 'memory_metrics', 'args': {'tab_id': 't0', 'force_refresh': True}},
        {'tool': 'list_tabs', 'args': {}},
        {'tool': 'list_browsers', 'args': {}},
    ]), preview_chars=400)

    print('\n=== Z. teardown ===', flush=True)
    await t('close(t0)', lambda: call('close', tab_id='t0'))
    await t('kill_all', lambda: call('kill_all'))

    print(f'\n=========================================')
    print(f'  PASSED: {PASSED}   FAILED: {FAILED}')
    print(f'=========================================')


asyncio.run(main())
