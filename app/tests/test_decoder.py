import pytest

from lapcounter import decoder


def test_passing_from_bench_run():
    m = decoder.parse("P 109613 AMB 8954097 -30.86 4096 0 10.7")
    assert m == decoder.Passing(109613, "AMB", 8954097, -30.86, 4096)


def test_passing_without_hits():
    assert decoder.parse("P 1 AMB 42 -40.0").hits is None


def test_stats_line():
    assert decoder.parse("S 160240 -47.50 1.02 701 608 0.04") == decoder.Stats(160240, -47.5, 701, 608)


@pytest.mark.parametrize("raw,expected", [
    ("L 228855 DONE 1000 36", decoder.Learn(228855, "DONE", 1000, 36.0)),
    ("L 215939 START -35.8", decoder.Learn(215939, "START", value=-35.8)),
    ("L 1 INTERRUPTED", decoder.Learn(1, "INTERRUPTED")),
    ("L 1 RESET", decoder.Learn(1, "RESET")),
])
def test_learn_lines(raw, expected):
    assert decoder.parse(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "P", "P x AMB 1 -3", "P 1 AMB notanid -3", "S 1 2 3", "L 1 DONE", "L 1 WAT",
    "F RC4 TS:23557 TC:57188874 M:0.68", "Listening on tcp://*:5556",
])
def test_garbage_is_ignored(raw):
    assert decoder.parse(raw) is None
