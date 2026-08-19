"""Offline unit tests for umbra.search (dorks, enforcement, merge/rank)."""

from umbra.search.dorks import build_dorks, classify_intent, hit_satisfies, operators_of
from umbra.search.results import merge, normalize


def test_classify_intent():
    assert classify_intent("fastmcp tool decorator docs") == "docs"
    assert classify_intent("thinkpad x230 bios firmware") == "firmware"
    assert classify_intent("cats") == "generic"


def test_build_dorks_raw_last_and_capped():
    dorks = build_dorks("x230 coreboot", "firmware")
    assert dorks[-1] == {"query": "x230 coreboot", "operators": []}
    assert len(dorks) <= 9
    assert any("site:github.com" in d["operators"] for d in dorks)


def test_hit_satisfies():
    ops = operators_of("q (site:github.com OR site:gitlab.com) filetype:pdf -amazon.com")
    assert hit_satisfies(ops, {"url": "https://www.github.com/a/b.pdf", "title": ""})
    assert not hit_satisfies(ops, {"url": "https://github.com/a/b.html", "title": ""})
    assert not hit_satisfies(ops, {"url": "https://example.com/a.pdf", "title": ""})
    assert not hit_satisfies(["-amazon.com"], {"url": "https://www.amazon.com/x", "title": ""})
    assert not hit_satisfies(['intitle:"guide"'], {"url": "https://a.com/", "title": "Nope"})
    assert hit_satisfies(['intitle:"guide"'], {"url": "https://a.com/", "title": "A Guide"})


def test_normalize_collapses_variants():
    assert normalize("HTTPS://www.Example.com/a/") == normalize("https://example.com/a#frag")


def test_merge_scores_multi_engine_and_filters():
    raw = {"query": "q", "operators": []}
    spec = {"query": "q site:a.com", "operators": ["site:a.com"]}
    hits_a = [{"url": "https://a.com/1", "title": "one", "snippet": "s"},
              {"url": "https://pinterest.com/x", "title": "junk", "snippet": ""}]
    hits_b = [{"url": "https://www.a.com/1/", "title": "one", "snippet": "longer snippet"},
              {"url": "https://b.com/2", "title": "two", "snippet": ""}]
    out = merge([(spec, 0, "bing", hits_a), (raw, 1, "duckduckgo", hits_b)],
                weights={"bing": 1.0, "duckduckgo": 1.0}, max_results=10, max_per_host=3)
    assert [r["url"] for r in out] == ["https://a.com/1", "https://b.com/2"]
    assert sorted(out[0]["engines"]) == ["bing", "duckduckgo"]
    assert out[0]["snippet"] == "longer snippet"
    assert out[0]["dork"] == "q site:a.com"
    # domain filters + per-host cap
    assert [r["url"] for r in merge([(raw, 0, "bing", hits_b)], weights={}, max_results=10,
                                     max_per_host=3, blocked_domains=["b.com"])] == ["https://www.a.com/1/"]
    assert [r["url"] for r in merge([(raw, 0, "bing", hits_b)], weights={}, max_results=10,
                                     max_per_host=3, allowed_domains=["b.com"])] == ["https://b.com/2"]
