"""Replays survive damaged recordings (a gzip part cut off by a hard stop, then more parts appended)."""
import gzip
import io
import json

from app.sources import gzip_lines


def _part(rows):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as f:
        for r in rows:
            f.write((json.dumps(r) + "\n").encode())
    return buf.getvalue()


def test_damaged_recording_carries_on(tmp_path):
    a = _part([{"rx": i} for i in range(0, 50)])
    cut = _part([{"rx": i} for i in range(50, 2000)])[: 300]       # the app was killed while writing this part
    c = _part([{"rx": i} for i in range(5000, 5100)])
    p = tmp_path / "ticks.jsonl.gz"
    p.write_bytes(a + cut + c)
    got = [json.loads(x)["rx"] for x in gzip_lines(p) if x.startswith("{") and x.endswith("}")]
    assert got[:50] == list(range(50))                              # everything before the damage
    assert got[-100:] == list(range(5000, 5100))                    # and everything after it
    # a normal file reads exactly like gzip.open
    q = tmp_path / "ok.jsonl.gz"
    q.write_bytes(a + c)
    assert list(gzip_lines(q)) == [ln.rstrip("\n") for ln in gzip.open(q, "rt")]
