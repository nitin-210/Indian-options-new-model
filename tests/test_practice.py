"""Past days panel: listing saved days and refusing bad requests (no subprocess is started here)."""
import asyncio
import json

import app.practice as pr


def test_days_and_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "DATA_DIR", tmp_path)
    s = tmp_path / "sessions"
    (s / "2026-11-10").mkdir(parents=True)
    (s / "2026-11-10" / "ticks.jsonl.gz").write_bytes(b"x")
    (s / "2026-11-12-rebuilt").mkdir()
    (s / "2026-11-12-rebuilt" / "meta.json").write_text(json.dumps({"day": "2026-11-12"}))
    (s / "2026-11-11-rebuilt").mkdir()                     # a rebuild that never finished: not listed
    (s / "2026-11-09").mkdir()                             # empty folder: not listed
    p = pr.Practice(9999)
    days = p.days()
    assert [(d["day"], d["kind"]) for d in days] == [("2026-11-12", "rebuilt"), ("2026-11-10", "recorded")]
    st = p.status()
    assert st["available"] and st["state"] == "idle" and st["url"] == "http://127.0.0.1:9999/"

    async def go():
        bad = [await p.start("", None, False), await p.start("2026-13-45", None, False),
               await p.start("2999-01-01", None, False), await p.start("2026-11-12", "../../etc", False),
               await p.start("2026-11-12", "2026-11-11-nope", False)]
        return bad
    for r in asyncio.run(go()):
        assert r["ok"] is False and r["error"]
    assert p.state == "idle"
