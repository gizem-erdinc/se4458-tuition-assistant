import json
import shutil
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from conftest import ROOT
from mcp_server.indexer import DEFAULT_DOC, PolicyIndex, chunk_markdown


def test_chunks_follow_numbered_sections():
    chunks = chunk_markdown(DEFAULT_DOC.read_text(encoding="utf-8"))
    sections = [c.section for c in chunks]
    assert sections[:4] == ["1", "2.1", "2.2", "2.3"] and "9" in sections
    c = next(c for c in chunks if c.section == "3.2")
    assert c.title == "Number and size of installments" and c.parent_title == "Installment policy"
    assert "up to three installments" in c.text
    assert "```json" in next(c for c in chunks if c.section == "9").text   # code fence kept intact


@pytest.mark.parametrize("query,expected", [
    ("When is the payment deadline?", {"2.1", "2.2"}),
    ("fall term payment deadline", {"2.1"}),
    ("Who is eligible for an installment arrangement?", {"3.1"}),
    ("is a partial payment an approved installment plan", {"3.4"}),
    ("how much is the late fee", {"4.1"}),
    ("refund if I withdraw in the first two weeks", {"6.1"}),
    ("what happens if I retry and submit the payment twice", {"7.4"}),
    ("how long until my payment shows up in my account", {"2.3"}),
])
def test_distinct_queries_hit_expected_sections(query, expected):
    idx, _ = PolicyIndex.load_or_build()
    top = [r["section"] for r in idx.search(query, top_k=3)]
    assert expected <= set(top), top


def test_result_shape_follows_policy_section_9():
    idx, _ = PolicyIndex.load_or_build()
    hit = idx.search("installment eligibility", top_k=1)[0]
    assert {"section", "heading", "text", "reference", "score"} <= set(hit)
    assert idx.document_id == "UNI-TUITION-2026-01"


def test_irrelevant_query_returns_nothing():
    idx, _ = PolicyIndex.load_or_build()
    assert idx.search("pizza recipe tomato") == []


def test_index_is_persisted_and_only_rebuilt_on_change(tmp_path):
    doc = tmp_path / "policy.md"
    shutil.copy(DEFAULT_DOC, doc)
    path = tmp_path / "idx.json"
    _, rebuilt = PolicyIndex.load_or_build(doc, path)
    assert rebuilt
    _, rebuilt = PolicyIndex.load_or_build(doc, path)
    assert not rebuilt
    doc.write_text(doc.read_text(encoding="utf-8") + "\n## 10. New Rule\nSomething new.\n", encoding="utf-8")
    idx, rebuilt = PolicyIndex.load_or_build(doc, path)
    assert rebuilt and any(c.section == "10" for c in idx.chunks)


@pytest.mark.anyio
async def test_real_mcp_server_over_stdio():
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"], cwd=str(ROOT))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
            assert [t.name for t in tools] == ["search_tuition_policies"]
            res = await s.call_tool("search_tuition_policies", {"query": "installment rules", "top_k": 2})
            payload = json.loads(res.content[0].text)
            assert payload["document_id"] == "UNI-TUITION-2026-01" and payload["match_count"] == 2
            assert payload["matches"][0]["section"].startswith("3.")
