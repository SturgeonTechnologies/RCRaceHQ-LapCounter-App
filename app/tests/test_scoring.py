from lapcounter.scoring import rank_qualifying


def st(reg, name, best, at=0):
    return {"registrationId": reg, "racerName": name, "transponder": 1,
            "bestLapMs": best, "bestLapAtMs": at}


def test_best_lap_across_rounds_fastest_first():
    q1 = [st("r1", "Riley", 20000, 10), st("r3", "Sam", 19000, 10), st("r2", "Kid", None)]
    q2 = [st("r1", "Riley", 18500, 50), st("r3", "Sam", 19500, 50), st("r2", "Kid", None)]
    out = rank_qualifying([q1, q2])
    assert [(r["place"], r["racerName"], r["bestLapMs"]) for r in out] == [
        (1, "Riley", 18500), (2, "Sam", 19000), (3, "Kid", None)]
    assert out[0]["heats"] == 2


def test_tie_goes_to_the_lap_set_first():
    out = rank_qualifying([[st("r1", "Riley", 19000, 500), st("r3", "Sam", 19000, 100)]])
    assert [r["racerName"] for r in out] == ["Sam", "Riley"]


def test_no_lap_sorted_by_name_last():
    out = rank_qualifying([[st("a", "zed", None), st("b", "Amy", None), st("c", "Bo", 30000)]])
    assert [r["racerName"] for r in out] == ["Bo", "Amy", "zed"]
