import json

import pytest

from codex_auto_title.history import Deferred, capture, representation


def turn(n, text="Build a parser", kind="userMessage"):
    return {
        "id": str(n),
        "status": "completed",
        "itemsView": "full",
        "unknown": {"future": True},
        "items": [
            {"id": f"i{n}", "type": kind, "content": [{"type": "text", "text": text}]}
        ],
    }


class HistoryRPC:
    def __init__(self, turns):
        self.turns = turns
        self.calls = []

    async def request(self, method, params):
        self.calls.append((method, params))
        index = int(params.get("cursor") or 0)
        return {
            "data": [self.turns[index]],
            "nextCursor": str(index + 1) if index + 1 < len(self.turns) else None,
            "unknown_page_field": "retained",
        }


async def test_complete_snapshot_unknown_fields_and_later_activity():
    rpc = HistoryRPC([turn(2), turn(1)])
    snap = await capture(rpc, "source")
    assert [t["id"] for t in snap.turns] == ["1", "2"]
    assert "unknown_page_field" in snap.raw_json
    assert snap.turns[0]["unknown"]["future"]
    before = snap.raw_json
    rpc.turns.append(turn(3, "later change"))
    snap.turns[0]["unknown"] = "mutated copy"
    assert snap.raw_json == before
    assert snap.sha256 and len(snap.boundaries) == 2
    assert all(p["itemsView"] == "full" for _, p in rpc.calls)
    assert before in representation(snap, len, 100000)


async def test_truncation_preserves_original_and_language_evidence():
    rpc = HistoryRPC(
        [
            turn(3, "日本語で検索画面を作ってください"),
            turn(2, "log " * 10000, "commandExecution"),
            turn(1, "Create a search interface " * 3000),
        ]
    )
    snap = await capture(rpc, "s")
    before = snap.raw_json
    result = representation(snap, len, 3000)
    assert len(result) <= 3000
    assert "日本語で検索画面" in result
    assert "omitted" in result
    assert result == representation(snap, len, 3000)
    assert snap.raw_json == before


async def test_incomplete_retrieval_is_not_snapshot():
    with pytest.raises(Deferred, match="page bound"):
        await capture(HistoryRPC([turn(2), turn(1)]), "s", max_pages=1)
    running = turn(1)
    running["status"] = "inProgress"
    with pytest.raises(Deferred, match="still running"):
        await capture(HistoryRPC([running]), "s")


async def test_unknown_item_is_preserved():
    t = turn(1)
    t["items"].append({"type": "futureItem", "id": "future", "arbitrary": [1, 2]})
    snap = await capture(HistoryRPC([t]), "s")
    assert json.loads(snap.raw_json)[0]["data"][0]["items"][-1]["arbitrary"] == [1, 2]


async def test_running_capture_keeps_full_validation():
    running = turn(2)
    running["status"] = "inProgress"
    snap = await capture(HistoryRPC([running, turn(1)]), "s", allow_running=True)
    assert len(snap.turns) == 2
    assert snap.turns[-1]["status"] == "inProgress"
    assert snap.turns[-1]["unknown"] == {"future": True}
    with pytest.raises(Deferred, match="page bound"):
        await capture(
            HistoryRPC([running, turn(1)]), "s", max_pages=1, allow_running=True
        )
    with pytest.raises(Deferred, match="duplicate turn"):
        await capture(HistoryRPC([running, running]), "s", allow_running=True)
    running["itemsView"] = "summary"
    with pytest.raises(Deferred, match="full history"):
        await capture(HistoryRPC([running]), "s", allow_running=True)


async def test_running_capture_rejects_repeated_cursor():
    class RepeatedRPC:
        async def request(self, method, params):
            return {
                "data": [{**turn(params["cursor"] or "first"), "status": "inProgress"}],
                "nextCursor": "repeat",
            }

    with pytest.raises(Deferred, match="repeated pagination cursor"):
        await capture(RepeatedRPC(), "s", allow_running=True)
