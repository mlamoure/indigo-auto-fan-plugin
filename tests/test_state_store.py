"""Tests for the JSON/JSONL state store."""
from datetime import datetime

from auto_fan.state_store import StateStore


class TestJsonDocuments:
    def test_load_missing_returns_default(self, tmp_path):
        store = StateStore(str(tmp_path))
        assert store.load("locks", default={}) == {}

    def test_save_then_load_roundtrip(self, tmp_path):
        store = StateStore(str(tmp_path))
        store.save("locks", {"Bedroom": {"until": "2026-10-04T08:00:00"}})
        assert store.load("locks") == {"Bedroom": {"until": "2026-10-04T08:00:00"}}
        assert (tmp_path / "locks.json").exists()
        # no temp files left behind
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []

    def test_corrupt_file_returns_default(self, tmp_path):
        (tmp_path / "locks.json").write_text("{not json")
        store = StateStore(str(tmp_path))
        assert store.load("locks", default="fallback") == "fallback"

    def test_memory_store_without_dir(self):
        store = StateStore(None)
        store.save("x", [1, 2])
        assert store.load("x") == [1, 2]
        assert store.load("y", default=None) is None

    def test_datetime_values_serialized(self, tmp_path):
        store = StateStore(str(tmp_path))
        store.save("t", {"when": datetime(2026, 10, 4, 8, 0)})
        assert store.load("t")["when"].startswith("2026-10-04 08:00")


class TestJsonl:
    def test_append_and_iterate_in_order(self, tmp_path):
        store = StateStore(str(tmp_path))
        store.append_jsonl("overrides", {"n": 1}, when=datetime(2026, 9, 30))
        store.append_jsonl("overrides", {"n": 2}, when=datetime(2026, 10, 1))
        store.append_jsonl("overrides", {"n": 3}, when=datetime(2026, 10, 2))
        assert [r["n"] for r in store.iter_jsonl("overrides")] == [1, 2, 3]
        assert (tmp_path / "overrides-2026-09.jsonl").exists()
        assert (tmp_path / "overrides-2026-10.jsonl").exists()

    def test_bad_line_skipped(self, tmp_path):
        store = StateStore(str(tmp_path))
        store.append_jsonl("overrides", {"n": 1}, when=datetime(2026, 10, 1))
        with open(tmp_path / "overrides-2026-10.jsonl", "a") as f:
            f.write("garbage\n")
        store.append_jsonl("overrides", {"n": 2}, when=datetime(2026, 10, 1))
        assert [r["n"] for r in store.iter_jsonl("overrides")] == [1, 2]

    def test_prune_removes_only_old_months(self, tmp_path):
        store = StateStore(str(tmp_path))
        # cutoff is 2025-08-30: July 2025 is entirely older, August 2025 is not
        store.append_jsonl("overrides", {"n": 0}, when=datetime(2025, 7, 15))
        store.append_jsonl("overrides", {"n": 1}, when=datetime(2025, 8, 15))
        store.append_jsonl("overrides", {"n": 2}, when=datetime(2026, 9, 15))
        removed = store.prune_jsonl("overrides", keep_days=400, now=datetime(2026, 10, 4))
        assert len(removed) == 1 and removed[0].endswith("overrides-2025-07.jsonl")
        assert [r["n"] for r in store.iter_jsonl("overrides")] == [1, 2]

    def test_memory_jsonl(self):
        store = StateStore(None)
        store.append_jsonl("overrides", {"n": 1}, when=datetime(2026, 10, 1))
        assert list(store.iter_jsonl("overrides")) == [{"n": 1}]
        store.prune_jsonl("overrides", keep_days=1, now=datetime(2027, 1, 1))
        assert list(store.iter_jsonl("overrides")) == []


class TestFakeClock:
    def test_fake_clock_drives_plugin_time(self, fake_clock):
        from auto_fan import clock
        from datetime import datetime as dt
        assert clock.now() == dt(2026, 10, 3, 21, 0, 0)
        fake_clock.advance(hours=11)
        assert clock.now() == dt(2026, 10, 4, 8, 0, 0)
