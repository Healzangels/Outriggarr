from __future__ import annotations

from datetime import date

import pytest

from outriggarr.matcher import (
    LENGTH_RATIO,
    LENGTH_SLACK_SECONDS,
    MIN_CONTAINMENT_LEN,
    Episode,
    Match,
    MatchConfig,
    Override,
    Video,
    compile_title_regex,
    has_part_marker,
    in_scope,
    length_mismatch,
    match,
    mmss,
    normalise_title,
    parse_with_regex,
    show_number,
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
    assert r.matches == (Match(eps[0], videos[1], "override", "override"),)
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
    assert r.matches == (Match(eps[0], videos[0], "title", "contains"),)


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
    assert match(eps, videos, [], cfg).matches == (Match(eps[0], videos[1], "date", "date"),)
    # offset +4 days moves the window onto neither... tolerance 0 → only exact
    cfg2 = MatchConfig(("date",), date_tolerance_days=0, date_offset_days=-4)
    assert match(eps, videos, [], cfg2).matches == (Match(eps[0], videos[0], "date", "date"),)
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


def test_unavailable_entries_are_never_candidates_for_any_strategy() -> None:
    # A private/deleted entry lists as title == id. Digits in its id must not satisfy a
    # regex, a cached date must not satisfy the date strategy, and it is never worth a
    # per-video date fetch.
    eps = [ep(29, 7, 29, "Some Title", date(2026, 1, 8))]
    dead = vid("VMUQIF29yPc", "VMUQIF29yPc", date(2026, 1, 8))
    cfg = MatchConfig(("regex", "date"), title_regex=r"(?P<episode>\d+)")
    r = match(eps, [dead], [], cfg)
    assert r.matches == ()
    (u,) = r.unmatched
    assert u.candidates["regex"] == () and u.candidates["date"] == ()
    undated = [vid("VMUQIF29yPc", "VMUQIF29yPc")]
    assert videos_needing_dates(r, undated, MatchConfig(("date",))) == []


def test_a_strategy_reruns_after_an_exact_claim_frees_a_candidate() -> None:
    # Two editions of one title: the LA edition is S07E37's exact match; once it is
    # taken, S07E36's containment candidates collapse to the NY edition. One round
    # would leave S07E36 "2 candidates" for ever.
    eps = [
        ep(36, 7, 36, "The Alt-Pasta Revolution"),
        ep(37, 7, 37, "The Alt-Pasta Revolution LA Edition"),
    ]
    videos = [
        vid("ny", "THE ALT-PASTA REVOLUTION... NEW YORK EDITION | FTD"),
        vid("la", "The Alt-Pasta Revolution LA Edition"),
    ]
    r = match(eps, videos, [], MatchConfig(("title",)))
    assert {(m.episode.id, m.video.id) for m in r.matches} == {(36, "ny"), (37, "la")}


@pytest.mark.parametrize(
    ("runtime", "duration", "held"),
    [
        (None, 180, False),  # unknown runtime is no evidence
        (0, 180, False),
        (24, None, False),  # unknown duration is no evidence
        (24, 1400, False),  # within the slack
        (24, 900, False),  # 0.62x: within the ratio
        (24, 2700, False),  # 1.9x: within the ratio
        (24, 180, True),  # a 3-minute clip for a 24-minute episode
        (24, 3600, True),  # an hour-long stream for a 24-minute episode
        (5, 600, False),  # short-form: 10 min for 5 is within the 5-minute slack
        (5, 660, True),  # 11 min for 5: beyond both the slack and the ratio
        (2, 300, False),  # 2.5x, but only three minutes apart: the slack alone saves it
        (1, 400, True),  # 6.7x and 5m40s apart: beyond the slack too
    ],
)
def test_length_mismatch_rule(runtime, duration, held) -> None:
    reason = length_mismatch(runtime, duration)
    assert (reason is not None) is held
    if held:
        assert "Sonarr says the episode runs" in reason and f"{runtime} min" in reason


def test_length_check_holds_non_exact_tiers_only_and_pins_never() -> None:
    # Containment and date pairings are held when the length contradicts the runtime;
    # an exact title and a pin are never held (TVDB runtimes are wrong far more often
    # than a same-titled video is the wrong one).
    eps = [
        Episode(1, 1, 1, "Alpha Beta", date(2026, 1, 8), runtime_minutes=24),
        Episode(2, 1, 2, "Gamma Delta", date(2026, 1, 9), runtime_minutes=24),
        Episode(3, 1, 3, "Epsilon Zeta", date(2026, 1, 10), runtime_minutes=24),
        Episode(4, 1, 4, "Eta Theta", date(2026, 1, 11), runtime_minutes=24),
    ]
    videos = [
        Video("a", "Alpha Beta | Show clip", "https://x/a", duration=120),  # contains, 2 min
        Video("b", "Gamma Delta", "https://x/b", duration=120),  # exact, 2 min
        Video("c", "Something else", "https://x/c", date(2026, 1, 10), duration=120),  # date, 2 min
        Video("d", "Whatever", "https://x/d", duration=120),  # pinned, 2 min
    ]
    cfg = MatchConfig(("title", "date"), date_tolerance_days=0)
    r = match(eps, videos, [Override("d", 1, 4)], cfg)
    assert {(m.episode.id, m.video.id, m.tier) for m in r.matches} == {
        (2, "b", "exact"),
        (4, "d", "override"),
    }
    assert {(h.episode.id, h.video.id, h.tier) for h in r.held} == {
        (1, "a", "contains"),
        (3, "c", "date"),
    }
    assert all(h.reason == "video runs 2:00, Sonarr says the episode runs 24 min" for h in r.held)
    assert r.unmatched == (), "a held episode is neither matched nor unmatched"
    assert r.matched_video_ids == frozenset({"a", "b", "c", "d"}), "a held video is off the table"
    # a later strategy may still find the right video for a held episode
    videos.append(Video("e", "The real thing", "https://x/e", date(2026, 1, 8), duration=1500))
    r2 = match(eps, videos, [Override("d", 1, 4)], cfg)
    assert {(m.episode.id, m.video.id, m.tier) for m in r2.matches} >= {(1, "e", "date")}
    assert {h.episode.id for h in r2.held} == {3}


def test_held_episodes_still_get_date_fetches() -> None:
    eps = [Episode(1, 1, 1, "Alpha Beta", date(2026, 1, 8), runtime_minutes=24)]
    videos = [
        Video("a", "Alpha Beta | clip", "https://x/a", duration=120),
        Video("u", "Undated", "https://x/u"),
    ]
    r = match(eps, videos, [], MatchConfig(("title", "date")))
    assert r.held and not r.unmatched
    assert [v.id for v in videos_needing_dates(r, videos, MatchConfig(("title", "date")))] == ["u"]


def test_show_number_in_titles_is_a_guard_and_a_vouch() -> None:
    # Kill Tony: Sonarr titles carry the show's own count, so do the uploads; a guest
    # who appears in several episodes must not make every one of them a candidate
    from outriggarr.matcher import show_number

    assert show_number("#751 - JOE ROGAN + SHANE GILLIS") == 751
    assert show_number("KT#778 - JIMMY CARR") == 778 and show_number("Plain title") is None
    eps = [ep(1, 2026, 1, "#751 - JOE ROGAN + SHANE GILLIS"), ep(2, 2026, 2, "#752 - TIM DILLON")]
    videos = [
        vid("a", "KT #751 - JOE ROGAN + SHANE GILLIS"),
        vid("b", "KT #680 - MADISON SQUARE GARDEN (NIGHT ONE) - JOE ROGAN + SHANE GILLIS + ..."),
        vid("c", "KILL TONY #574 - JOE ROGAN + SHANE GILLIS + ARI SHAFFIR"),
        vid("d", "TIM DILLON"),  # no number on the upload: the guard does not apply
    ]
    r = match(eps, videos, [], MatchConfig(("title",)))
    assert {(m.episode.id, m.video.id, m.tier) for m in r.matches} == {
        (1, "a", "numbered"),
        (2, "d", "exact"),
    }
    # the same number with no shared name is still not a candidate
    r2 = match(
        [ep(3, 2026, 3, "#753 - MARK NORMAND")],
        [vid("e", "KT #753 - LIVE FROM AUSTIN")],
        [],
        MatchConfig(("title",)),
    )
    assert r2.matches == () and r2.unmatched[0].candidates["title"] == ()


def test_clashing_show_numbers_never_pair_even_on_an_exact_name() -> None:
    # "#752 - TIM DILLON" vs an upload "KT #99 - TIM DILLON": same name, different
    # show number — a different episode, not a candidate at all
    eps = [ep(1, 2026, 2, "#752 - TIM DILLON")]
    r = match(eps, [vid("x", "KT #99 - TIM DILLON")], [], MatchConfig(("title",)))
    assert r.matches == () and r.unmatched[0].candidates["title"] == ()


@pytest.mark.parametrize(
    ("title", "marked"),
    [
        ("Alpha Beta (Part 1/5)", True),
        ("Alpha Beta (Part 2)", True),
        ("Alpha Beta (1/5)", True),
        ("Alpha Beta Part 1 of 2", True),
        ("Alpha Beta Pt. 3/17", True),
        ("Alpha Beta 1 of 4", True),
        ("Alpha Beta Part 2", True),
        ("Alpha Beta pt 2", True),
        ("Alpha Beta part1", True),
        ("Top 10 of 2024", False),  # four digits are a year, not a count
        ("Lecture 12 of 40", True),
        ("5 of 3", False),  # N past M is not a part of anything
        ("0 of 3", False),
        ("Party 2", False),
        ("Departure 3", False),
        ("Dept. 5", False),
        ("Alpha Beta (2024)", False),
        ("Alpha Beta (1)", False),  # a bare bracketed number is a re-upload suffix, not a part
    ],
)
def test_has_part_marker(title: str, marked: bool) -> None:
    assert has_part_marker(title) is marked


def test_part_upload_is_held_unless_the_episode_names_a_part() -> None:
    # "X (Part 1)" contains "X": importing it would file a fragment as the whole episode
    # and flip hasFile, so it is held like a length mismatch; a pin releases it. An
    # episode that names a part itself takes a part (exact title, same marker both sides).
    eps = [
        Episode(1, 1, 1, "Alpha Beta", date(2026, 1, 8)),
        Episode(2, 1, 2, "Gamma Delta Part 2", date(2026, 1, 9)),
        Episode(3, 1, 3, "#751 - JOE ROGAN", date(2026, 1, 10)),
        Episode(4, 1, 4, "Something Else Entirely", date(2026, 1, 11)),
    ]
    videos = [
        Video("a", "Alpha Beta (Part 1)", "https://x/a"),
        Video("b", "Gamma Delta (Part 2)", "https://x/b"),
        Video("c", "KT #751 - JOE ROGAN (Part 1)", "https://x/c"),  # number-agreed containment
        Video("d", "Unrelated title (1/3)", "https://x/d", date(2026, 1, 11)),  # a date pairing
    ]
    cfg = MatchConfig(("title", "date"), date_tolerance_days=0)
    r = match(eps, videos, [], cfg)
    reason = "video is one part of a split upload; the episode is whole in Sonarr"
    assert {(h.episode.id, h.video.id, h.strategy, h.tier) for h in r.held} == {
        (1, "a", "title", "contains"),
        (3, "c", "title", "numbered"),  # the show-number promotion is still containment
        (4, "d", "date", "date"),
    }
    assert all(h.reason == reason for h in r.held)
    assert {(m.episode.id, m.video.id) for m in r.matches} == {(2, "b")}
    assert r.unmatched == ()
    # a pin is the release valve
    r2 = match(eps, videos, [Override("a", 1, 1)], cfg)
    assert (1, "a") in {(m.episode.id, m.video.id) for m in r2.matches}
    assert 1 not in {h.episode.id for h in r2.held}
    # the length check speaks first when it has evidence
    long_ep = [Episode(1, 1, 1, "Alpha Beta", date(2026, 1, 8), runtime_minutes=60)]
    r3 = match(long_ep, [Video("a", "Alpha Beta (Part 1)", "https://x/a", duration=120)], [], cfg)
    assert r3.held[0].reason == "video runs 2:00, Sonarr says the episode runs 60 min"
    # both parts listed: two candidates, nobody claims, the episode is plainly unmatched
    r4 = match(eps[:1], videos[:1] + [Video("a2", "Alpha Beta (Part 2)", "https://x/a2")], [], cfg)
    assert [u.episode.id for u in r4.unmatched] == [1] and r4.held == ()


@pytest.mark.parametrize(
    ("title", "phrase", "ok"),
    [
        ("Scam School 194: Balls of Fire", "scam school", True),
        ("SCAM SCHOOL: the basics", "Scam School", True),
        ("Scam-School 12", "scam school", True),  # punctuation is not a difference
        ("Brian's other show", "Scam School", False),
        ("anything", None, True),
        ("anything", "   ", True),
    ],
)
def test_in_scope(title: str, phrase: str | None, ok: bool) -> None:
    assert in_scope(title, phrase) is ok


def test_title_scope_hides_other_shows_on_a_shared_channel_but_never_a_pin() -> None:
    # A channel carrying two shows: the guest's name matches an upload of the OTHER show.
    # With the phrase required, only the right show's uploads are candidates for any
    # automatic strategy; a pin is the user's word and ignores the scope.
    eps = [
        Episode(1, 1, 1, "Max Schaaf", date(2026, 1, 8)),
        Episode(2, 1, 2, "Some Title Here", date(2026, 1, 9)),
    ]
    videos = [
        Video("wrong", "Max Schaaf | Let It Kill You", "https://x/wrong"),
        Video("right", "Epicly Later'd: Max Schaaf", "https://x/right"),
        Video("bydate", "Unrelated upload", "https://x/bydate", date(2026, 1, 9)),
    ]
    open_cfg = MatchConfig(("title", "date"), date_tolerance_days=0)
    r = match(eps, videos, [], open_cfg)
    assert 1 not in {m.episode.id for m in r.matches}, "two containment candidates: ambiguous"
    scoped = MatchConfig(("title", "date"), date_tolerance_days=0, title_require="Epicly Later'd")
    r2 = match(eps, videos, [], scoped)
    assert {(m.episode.id, m.video.id) for m in r2.matches} == {(1, "right")}
    assert [u.episode.id for u in r2.unmatched] == [2], "the date pairing is out of scope too"
    assert r2.unmatched[0].candidates["date"] == (), "an out-of-scope video is invisible to it"
    r3 = match(eps, videos, [Override("bydate", 1, 2)], scoped)
    assert (2, "bydate") in {(m.episode.id, m.video.id) for m in r3.matches}, "pins are exempt"


def test_numbered_containment_settles_claims_but_still_faces_the_length_check() -> None:
    # "KT #751 - JOE ROGAN (clip)" contains "#751 - JOE ROGAN" with the number agreeing:
    # good enough to win a claim against a plain containment, but it is still
    # containment, so a 2-minute clip against a 3-hour episode is held, not filed
    ep = Episode(1, 1, 1, "#751 - JOE ROGAN + SHANE GILLIS", date(2026, 1, 8), runtime_minutes=180)
    clip = Video("c", "KT #751 - JOE ROGAN + SHANE GILLIS (clip)", "https://x/c", duration=120)
    r = match([ep], [clip], [], MatchConfig(("title",)))
    assert r.matches == () and [(h.video.id, h.tier) for h in r.held] == [("c", "numbered")]
    assert r.held[0].reason.startswith("video runs 2:00")
    full = Video("f", "KT #751 - JOE ROGAN + SHANE GILLIS", "https://x/f", duration=180 * 60)
    r2 = match([ep], [full], [], MatchConfig(("title",)))
    assert [(m.video.id, m.tier) for m in r2.matches] == [("f", "numbered")]
    # and it beats a plain containment claim on the same video
    other = Episode(2, 1, 2, "JOE ROGAN + SHANE GILLIS", date(2026, 1, 9))
    r3 = match([other, ep], [full], [], MatchConfig(("title",)))
    assert [(m.episode.id, m.tier) for m in r3.matches] == [(1, "numbered")]


@pytest.mark.parametrize(
    ("title", "number"),
    [
        ("#751 - JOE ROGAN", 751),
        ("KT #751 - JOE ROGAN", 751),
        ("  #12 Foo", 12),
        ("Foo Bar | Best of #2024", None),  # a hashtag in the tail is not a show number
        ("Kill Tony Live #7 - Foo", None),  # more than one word before it: not the head
    ],
)
def test_show_number_reads_the_head_only(title: str, number: int | None) -> None:
    assert show_number(title) == number


def test_hashtag_in_a_tail_does_not_veto_a_candidate() -> None:
    r = match(
        [Episode(1, 1, 1, "#12 - Foo Bar Baz", date(2026, 1, 8))],
        [Video("v", "Foo Bar Baz | Best of #2024", "https://x/v")],
        [],
        MatchConfig(("title",)),
    )
    assert [(m.video.id, m.tier) for m in r.matches] == [("v", "contains")]


@pytest.mark.parametrize("phrase", ["!!!", " - ", "…"])
def test_scope_phrase_that_normalises_to_nothing_requires_nothing(phrase: str) -> None:
    assert in_scope("anything", phrase) is True


@pytest.mark.parametrize(
    ("title", "marked"),
    [
        ("Interview Part One", True),
        ("Interview (Part II)", True),
        ("Interview Part Two of Three", True),
        ("Interview pt. IV", True),
        ("Interview Part Eleven", False),  # beyond the words we read
        ("Partial recall", False),
        ("Party of Five", False),
    ],
)
def test_part_marker_reads_words_and_roman_numerals(title: str, marked: bool) -> None:
    assert has_part_marker(title) is marked


def test_two_strategies_can_hold_one_episode_and_both_videos_stay_off_the_table() -> None:
    # the matcher keeps every hold (accounting: both videos are taken); the scan report
    # shows one row per episode, which test_scheduler pins
    ep = Episode(1, 1, 1, "Alpha Beta", date(2026, 1, 8), runtime_minutes=60)
    videos = [
        Video("a", "Alpha Beta (Part 1)", "https://x/a"),  # title: part hold
        Video("b", "Unrelated", "https://x/b", date(2026, 1, 8), duration=120),  # date: length hold
    ]
    r = match([ep], videos, [], MatchConfig(("title", "date"), date_tolerance_days=0))
    assert [(h.video.id, h.strategy) for h in r.held] == [("a", "title"), ("b", "date")]
    assert r.matched_video_ids == frozenset({"a", "b"}), "both videos are off the table"


def test_duplicate_video_ids_do_not_disable_a_pin() -> None:
    ep = Episode(1, 1, 1, "Whatever", date(2026, 1, 8))
    twins = [Video("x", "Copy one", "https://x/1"), Video("x", "Copy two", "https://x/2")]
    r = match([ep], twins, [Override("x", 1, 1)], MatchConfig(("title",)))
    assert [(m.video.id, m.strategy) for m in r.matches] == [("x", "override")]


def test_videos_needing_dates_skip_out_of_scope_and_pinned_videos() -> None:
    eps = [
        Episode(1, 1, 1, "Some Title", date(2026, 1, 8)),
        Episode(2, 1, 2, "Other", date(2026, 1, 9)),
    ]
    videos = [
        Video("out", "Other Show ep", "https://x/out"),
        Video("in", "My Show ep", "https://x/in"),
        Video("pin", "My Show pinned", "https://x/pin"),
    ]
    cfg = MatchConfig(("title", "date"), title_require="My Show")
    pins = [Override("pin", 9, 9)]  # pinned to an episode Sonarr does not list as wanted
    r = match(eps, videos, pins, cfg)
    assert "pin" not in r.matched_video_ids, "the pin found no episode: still a pin"
    assert [v.id for v in videos_needing_dates(r, videos, cfg, pins)] == ["in"]


@pytest.mark.parametrize(
    ("runtime", "duration", "held"),
    [
        (60, 60 * 60 // LENGTH_RATIO, False),  # exactly half: passes
        (60, 60 * 60 // LENGTH_RATIO - 1, True),  # a second under: held
        (60, 60 * 60 * LENGTH_RATIO, False),  # exactly double: passes
        (60, 60 * 60 * LENGTH_RATIO + 1, True),  # a second over: held
        (5, 5 * 60 + LENGTH_SLACK_SECONDS, False),  # within the slack whatever the ratio
        (5, 5 * 60 + LENGTH_SLACK_SECONDS + 1, True),
    ],
)
def test_length_mismatch_boundaries(runtime: int, duration: int, held: bool) -> None:
    assert (length_mismatch(runtime, duration) is not None) is held


def test_date_tolerance_is_inclusive_at_the_edge() -> None:
    ep = Episode(1, 1, 1, "Zzz", date(2026, 1, 10))
    cfg = MatchConfig(("date",), date_tolerance_days=2)
    edge = match([ep], [Video("e", "x", "https://x/e", date(2026, 1, 12))], [], cfg)
    beyond = match([ep], [Video("b", "x", "https://x/b", date(2026, 1, 13))], [], cfg)
    assert [m.video.id for m in edge.matches] == ["e"] and beyond.matches == ()


def test_containment_length_floor_is_a_boundary() -> None:
    long_enough = Episode(1, 1, 1, "Ab Cde", date(2026, 1, 8))  # 6 chars, two tokens
    too_short = Episode(2, 1, 2, "Ab Cd", date(2026, 1, 9))  # 5 chars
    assert len(normalise_title(long_enough.title)) == MIN_CONTAINMENT_LEN
    r = match(
        [long_enough, too_short],
        [Video("a", "Ab Cde tonight", "https://x/a"), Video("b", "Ab Cd tonight", "https://x/b")],
        [],
        MatchConfig(("title",)),
    )
    assert [(m.episode.id, m.tier) for m in r.matches] == [(1, "contains")]


def test_mmss_reads_like_a_player() -> None:
    assert mmss(0) == "0:00"
    assert mmss(248) == "4:08"
    assert mmss(1382) == "23:02"
    assert mmss(3600) == "1:00:00"
    assert mmss(3909) == "1:05:09"
    assert length_mismatch(24, 3909) == "video runs 1:05:09, Sonarr says the episode runs 24 min"
