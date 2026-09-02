from __future__ import annotations

from datetime import date

import pytest

from outriggarr.matcher import (
    Episode,
    Match,
    MatchConfig,
    Override,
    Video,
    compile_title_regex,
    match,
    normalise_title,
    parse_with_regex,
    videos_needing_dates,
)


def ep(i: int, s: int, n: int, title: str, air: date | None = None) -> Episode:
    return Episode(i, s, n, title, air)


def vid(i: str, title: str, upload: date | None = None) -> Video:
    return Video(i, title, f"https://x/{i}", upload)


@pytest.mark.parametrize(
    ("raw", "norm"),
    [
        ("Hello, World!", "hello world"),
        ("  Many   Spaces ", "many spaces"),
        ("Ep. 5 - The Thing", "the thing"),
        ("Episode 12: Rain", "rain"),
        ("#7 | Storm", "storm"),
        ("3. Third", "third"),
        ("3.5 Things", "3 5 things"),
        ("Café Olé", "café olé"),
        ("Title (Part 2)", "title part 2"),
    ],
)
def test_normalise_title(raw: str, norm: str) -> None:
    assert normalise_title(raw) == norm


def test_override_wins_over_everything() -> None:
    eps = [ep(1, 1, 1, "Exact")]
    videos = [vid("a", "Exact"), vid("b", "Something else")]
    r = match(eps, videos, [Override("b", 1, 1)], MatchConfig(("title",)))
    assert r.matches == (Match(eps[0], videos[1], "override"),)
    assert r.unmatched == ()


def test_title_exact_beats_containment_and_records_candidates() -> None:
    eps = [ep(1, 1, 1, "The Big One")]
    videos = [vid("a", "The Big One | Show"), vid("b", "the big one"), vid("c", "the BIG one!")]
    r = match(eps, videos, [], MatchConfig(("title",)))
    # two exact normalised matches → ambiguous → unmatched, candidates recorded
    assert r.matches == ()
    (u,) = r.unmatched
    assert u.candidates == {"override": (), "title": ("b", "c")}


def test_title_containment_when_no_exact() -> None:
    eps = [ep(1, 2, 3, "Marcello Hernández Goes Into Fight or Flight While Eating Spicy Wings")]
    videos = [
        vid(
            "a", "Marcello Hernández Goes Into Fight or Flight While Eating Spicy Wings | Hot Ones"
        ),
        vid("b", "Sean Evans Reveals the Season 30 Hot Sauce Lineup | Hot Ones"),
    ]
    r = match(eps, videos, [], MatchConfig(("title",)))
    assert r.matches == (Match(eps[0], videos[0], "title"),)


def test_short_titles_never_match_by_containment() -> None:
    eps = [ep(1, 1, 1, "TBA"), ep(2, 1, 2, "Rain")]
    videos = [vid("a", "TBA - the full episode"), vid("b", "Rain and more rain")]
    r = match(eps, videos, [], MatchConfig(("title",)))
    assert r.matches == ()
    assert [u.candidates["title"] for u in r.unmatched] == [(), ()]


def test_two_episodes_claiming_one_video_is_ambiguous_for_both() -> None:
    eps = [ep(1, 1, 1, "Same Title"), ep(2, 1, 2, "Same Title")]
    videos = [vid("a", "Same Title")]
    r = match(eps, videos, [], MatchConfig(("title",)))
    assert r.matches == (), "no guessing: neither episode gets it"
    assert [u.candidates["title"] for u in r.unmatched] == [("a",), ("a",)]


def test_exact_claim_beats_containment_claim_for_the_same_video() -> None:
    eps = [ep(3, 1, 3, "The Return"), ep(5, 1, 5, "The Return of the King")]
    videos = [vid("king", "The Return of the King | Show")]
    r = match(eps, videos, [], MatchConfig(("title",)))
    assert r.matches == (), "two containment claims: ambiguous, nobody takes it"
    videos = [vid("king", "The Return of the King")]
    r = match(eps, videos, [], MatchConfig(("title",)))
    assert [(m.episode.id, m.video.id) for m in r.matches] == [(5, "king")], "exact wins"


def test_pinned_video_is_never_a_candidate_for_other_episodes() -> None:
    # S2E5 is already imported (not wanted), but its pin is still on file; the pinned
    # video must not be offered to S2E6 by title or date.
    eps = [ep(6, 2, 6, "Alpha Beta", date(2026, 1, 8))]
    videos = [vid("p", "Alpha Beta", date(2026, 1, 8))]
    r = match(eps, videos, [Override("p", 2, 5)], MatchConfig(("title", "date")))
    assert r.matches == ()
    (u,) = r.unmatched
    assert u.candidates["title"] == () and u.candidates["date"] == ()


def test_containment_is_word_bounded_needs_two_words_and_skips_promos() -> None:
    eps = [ep(1, 1, 1, "The Thing"), ep(2, 1, 2, "Torture Chamber Special"), ep(3, 1, 3, "Finale")]
    videos = [
        vid("a", "Breathe Things Out | Show"),  # substring, not a word match
        vid("b", "Season 5 Episode 2 Trailer - Torture Chamber Special"),  # promo
        vid("c", "Torture Chamber Special | Show"),
        vid("d", "Season 3 Finale"),
    ]
    r = match(eps, videos, [], MatchConfig(("title",)))
    assert [(m.episode.id, m.video.id) for m in r.matches] == [(2, "c")]
    seen = {u.episode.id: u.candidates["title"] for u in r.unmatched}
    assert seen[1] == () and seen[3] == (), "one-word titles never contain-match"


def test_regex_with_and_without_season_group() -> None:
    eps = [ep(1, 3, 7, "x"), ep(2, 3, 8, "y")]
    videos = [vid("a", "Show S03E07 something"), vid("b", "Show S03E08"), vid("c", "Show S04E07")]
    cfg = MatchConfig(("regex",), title_regex=r"S(?P<season>\d+)E(?P<episode>\d+)")
    r = match(eps, videos, [], cfg)
    assert [(m.episode.id, m.video.id, m.strategy) for m in r.matches] == [
        (1, "a", "regex"),
        (2, "b", "regex"),
    ]

    cfg2 = MatchConfig(("regex",), title_regex=r"#(?P<episode>\d+)")
    r2 = match(
        [ep(1, 2026, 783, "t")], [vid("a", "KT #783 - guests"), vid("b", "KT #782")], [], cfg2
    )
    assert [(m.video.id) for m in r2.matches] == ["a"]


def test_regex_not_run_unless_enabled_or_pattern_given() -> None:
    eps = [ep(1, 3, 7, "nothing alike")]
    videos = [vid("a", "S03E07")]
    assert (
        match(eps, videos, [], MatchConfig(("title",), title_regex=r"E(?P<episode>\d+)")).matches
        == ()
    )
    assert match(eps, videos, [], MatchConfig(("regex",))).matches == ()


def test_compile_title_regex_requires_episode_group() -> None:
    with pytest.raises(ValueError, match="episode"):
        compile_title_regex(r"S(?P<season>\d+)")
    rx = compile_title_regex(r"(?P<episode>\d+)")
    assert parse_with_regex(rx, "ep 12") == (None, 12)
    assert parse_with_regex(rx, "no digits") is None


def test_date_strategy_tolerance_and_offset() -> None:
    eps = [ep(1, 1, 1, "TBA", date(2026, 3, 10))]
    videos = [
        vid("early", "a", date(2026, 3, 6)),
        vid("near", "b", date(2026, 3, 11)),
        vid("undated", "c", None),
    ]
    cfg = MatchConfig(("date",), date_tolerance_days=1, date_offset_days=0)
    assert match(eps, videos, [], cfg).matches == (Match(eps[0], videos[1], "date"),)
    # offset +4 days moves the window onto neither... tolerance 0 → only exact
    cfg2 = MatchConfig(("date",), date_tolerance_days=0, date_offset_days=-4)
    assert match(eps, videos, [], cfg2).matches == (Match(eps[0], videos[0], "date"),)
    wide = MatchConfig(("date",), date_tolerance_days=10)
    r = match(eps, videos, [], wide)
    assert r.matches == () and r.unmatched[0].candidates["date"] == ("early", "near")


def test_strategy_order_is_fixed_regardless_of_config_order() -> None:
    eps = [ep(1, 1, 1, "Alpha", date(2026, 1, 1))]
    videos = [vid("t", "Alpha"), vid("d", "Beta", date(2026, 1, 1))]
    r = match(eps, videos, [], MatchConfig(("date", "title")))
    assert r.matches[0].video.id == "t" and r.matches[0].strategy == "title"


def test_fall_through_to_next_strategy() -> None:
    eps = [ep(1, 1, 1, "Alpha", date(2026, 1, 1))]
    videos = [vid("t1", "Alpha"), vid("t2", "alpha"), vid("d", "Gamma", date(2026, 1, 2))]
    r = match(eps, videos, [], MatchConfig(("title", "date"), date_tolerance_days=2))
    assert r.matches[0].video.id == "d" and r.matches[0].strategy == "date"


def test_videos_needing_dates_only_when_useful() -> None:
    eps = [ep(1, 1, 1, "Alpha", date(2026, 1, 1)), ep(2, 1, 2, "Zzz", date(2026, 1, 8))]
    videos = [vid("a", "Alpha"), vid("b", "Beta"), vid("c", "Gamma", date(2026, 2, 1))]
    cfg = MatchConfig(("title", "date"))
    r = match(eps, videos, [], cfg)
    assert videos_needing_dates(r, videos, cfg) == [videos[1]]  # a used, c dated
    assert videos_needing_dates(r, videos, MatchConfig(("title",))) == []
    all_matched = match([eps[0]], [videos[0]], [], cfg)
    assert videos_needing_dates(all_matched, [videos[0]], cfg) == []


def test_no_air_date_means_no_date_candidates() -> None:
    eps = [ep(1, 1, 1, "x", None)]
    r = match(eps, [vid("a", "y", date(2026, 1, 1))], [], MatchConfig(("date",)))
    assert r.unmatched[0].candidates["date"] == ()


def test_unavailable_videos_never_title_match() -> None:
    eps = [ep(1, 1, 1, "dQw4w9WgXcQ Two Words")]
    r = match(eps, [vid("dQw4w9WgXcQ", "dQw4w9WgXcQ")], [], MatchConfig(("title",)))
    assert r.matches == () and r.unmatched[0].candidates["title"] == ()
