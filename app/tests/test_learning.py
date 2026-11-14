import pytest

from lapcounter import learning as L


def ev(event, at, transponder=None, value=None):
    return {"event": event, "transponder": transponder, "value": value, "received_at_ms": at}


SESSION = {"label": 8954097, "started_at_ms": 1_000}


@pytest.mark.parametrize("raw,want", [("8954097", 8954097), (" 42 ", 42), ("10000", 10000)])
def test_validate_label_ok(raw, want):
    assert L.validate_label(raw) == want


@pytest.mark.parametrize("raw", ["", "abc", "-5", "0", "1000", "9999", "12.5", str(2**31)])
def test_validate_label_rejects(raw):
    with pytest.raises(L.LearnError):
        L.validate_label(raw)


def test_validate_name():
    assert L.validate_name("  Paul   Smith ") == "Paul Smith"
    assert L.validate_name("") is None
    with pytest.raises(L.LearnError):
        L.validate_name("x" * 65)


def test_waiting_then_learning_progress():
    assert L.describe(SESSION, [], None, 2_000).state == L.WAITING
    v = L.describe(SESSION, [ev("START", 2_000, value=-30.0)], None, 8_000)
    assert (v.state, v.elapsed_s, v.progress, v.can_record) == (L.LEARNING, 6.0, 50, False)
    late = L.describe(SESSION, [ev("START", 2_000)], None, 60_000)
    assert late.progress == 95  # never shows 100 before DONE


def test_interrupted_then_retry():
    events = [ev("START", 2_000), ev("INTERRUPTED", 5_000)]
    assert L.describe(SESSION, events, None, 6_000).state == L.INTERRUPTED
    events.append(ev("START", 7_000))
    assert L.describe(SESSION, events, None, 8_000).state == L.LEARNING


def test_done_with_auto_id_is_ready():
    v = L.describe(SESSION, [ev("START", 2_000), ev("DONE", 14_000, 1000, 40.0)], None, 15_000)
    assert (v.state, v.auto_id, v.payloads, v.progress, v.can_record) == (L.READY, 1000, 40, 100, True)


def test_first_done_wins():
    events = [ev("DONE", 2_000, 1000, 40.0), ev("DONE", 3_000, 1001, 40.0)]
    assert L.describe(SESSION, events, None, 4_000).auto_id == 1000


def test_done_with_label_or_passing_is_known():
    assert L.describe(SESSION, [ev("DONE", 2_000, 8954097, 1.0)], None, 3_000).state == L.KNOWN
    passing = {"transponder": 8954097, "rssi": -30.0, "received_at_ms": 2_000}
    v = L.describe(SESSION, [], passing, 3_000)
    assert (v.state, v.can_record) == (L.KNOWN, True)


def test_done_with_other_real_number_is_conflict():
    v = L.describe(SESSION, [ev("DONE", 2_000, 1234567, 1.0)], None, 3_000)
    assert v.state == L.CONFLICT and "1234567" in v.message and not v.can_record


def test_expires_when_nothing_happens():
    assert L.describe(SESSION, [], None, 1_000 + 601_000).state == L.EXPIRED
    # but not while it is actually learning
    assert L.describe(SESSION, [ev("START", 600_000)], None, 1_000 + 601_000).state == L.LEARNING


def test_lift_when_decoder_still_holds_last_learn():
    done = ev("DONE", 500, 1000, 40.0)
    v = L.describe(SESSION, [], None, 2_000, before=done)
    assert v.state == L.LIFT and "Lift" in v.message and not v.can_record
    # after a RESET (it went quiet) the plain waiting step is right
    assert L.describe(SESSION, [], None, 2_000, before=ev("RESET", 600)).state == L.WAITING
    # and as soon as the decoder reports anything in the session, the hint goes
    assert L.describe(SESSION, [ev("RESET", 1_500)], None, 2_000, before=done).state == L.WAITING
    assert L.describe(SESSION, [ev("START", 1_500)], None, 2_000, before=done).state == L.LEARNING


def test_rename_learned(tmp_path):
    (tmp_path / "1000.rc4").write_bytes(b"code")
    target = L.rename_learned(tmp_path, 1000, 8954097)
    assert target.name == "8954097.rc4" and target.read_bytes() == b"code"
    assert not (tmp_path / "1000.rc4").exists()


def test_rename_never_overwrites(tmp_path):
    (tmp_path / "1000.rc4").write_bytes(b"new")
    (tmp_path / "8954097.rc4").write_bytes(b"old")
    with pytest.raises(L.LearnError, match="already exists"):
        L.rename_learned(tmp_path, 1000, 8954097)
    assert (tmp_path / "8954097.rc4").read_bytes() == b"old"


def test_rename_missing_source_and_non_auto(tmp_path):
    with pytest.raises(L.LearnError, match="isn't in"):
        L.rename_learned(tmp_path, 1000, 8954097, wait_s=0.1)
    with pytest.raises(L.LearnError, match="not a decoder auto number"):
        L.rename_learned(tmp_path, 1234567, 8954097)


def test_folder_scans(tmp_path):
    for name in ("1000.rc4", "8954097.rc4", "notes.txt", "abc.rc4"):
        (tmp_path / name).write_text("x")
    assert L.stray_auto_files(tmp_path) == [1000]
    assert L.learned_numbers(tmp_path) == {8954097}
    assert L.learned_numbers(tmp_path / "missing") == set()
