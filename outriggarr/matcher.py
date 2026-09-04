"""Pure matching: wanted episodes × listed videos × overrides → matches. No I/O.

Strategies run in a fixed order — override, regex, title, date — and for each still-
unmatched episode the first strategy that yields exactly one candidate video wins.
Zero or several candidates fall through to the next strategy; if every strategy falls
through the episode is reported as unmatched with what each strategy saw, which is
what the GUI's match preview shows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

STRATEGY_ORDER: tuple[str, ...] = ("override", "regex", "title", "date")
OPTIONAL_STRATEGIES: frozenset[str] = frozenset({"regex", "title", "date"})
MIN_CONTAINMENT_LEN = 6
MIN_CONTAINMENT_TOKENS = 2
# A video whose title carries one of these words while the episode title does not is
# a promo for the episode, not the episode.
PROMO_TOKENS = frozenset({"trailer", "teaser", "promo", "preview", "sneak", "recap"})
# An ellipsis run inside an episode title is TVDB's wildcard for the words it does not
# know yet: Hot Ones is "Caleb Williams .... While Eating Spicy Wings" against the
# upload "Caleb Williams Goes “Iceman” Mode While Eating Spicy Wings". Such a title is
# matched fragment by fragment, in order, each on word boundaries. A placeholder
# fragment (an episode TVDB has not named) never matches anything.
_ELLIPSIS = re.compile(r"\s*(?:\.{3,}|…+)\s*")
PLACEHOLDER_FRAGMENTS = frozenset({"tba", "tbd"})


@dataclass(frozen=True)
class Episode:
    id: int
    season: int
    number: int
    title: str
    air_date: date | None
    runtime_minutes: int | None = None  # the *arr's runtime (TVDB); 0/None = unknown


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    url: str
    upload_date: date | None = None
    duration: int | None = None  # seconds, from the flat listing when it carries one


@dataclass(frozen=True)
class Override:
    video_id: str
    season: int
    episode: int


@dataclass(frozen=True)
class MatchConfig:
    strategies: tuple[str, ...] = ("title",)
    date_tolerance_days: int = 2
    date_offset_days: int = 0
    title_regex: str | None = None
    # A phrase every candidate's title must contain, for a channel that carries several
    # shows; None → every listed video is a candidate. Pins are exempt: a pin is the
    # user's word, whatever the title says.
    title_require: str | None = None


@dataclass(frozen=True)
class Match:
    episode: Episode
    video: Video
    strategy: str
    tier: str = ""  # "exact"/"contains" for the title strategy, else the strategy name


@dataclass(frozen=True)
class Held:
    """A pairing a strategy made but the length check contradicts: reported, never
    queued. The user can pin the video if it is right (pins are never held)."""

    episode: Episode
    video: Video
    strategy: str
    tier: str
    reason: str


@dataclass(frozen=True)
class Unmatched:
    episode: Episode
    # strategy → candidate video ids it saw (0 or several); absent = strategy not run
    candidates: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class MatchResult:
    matches: tuple[Match, ...]
    unmatched: tuple[Unmatched, ...]
    held: tuple[Held, ...] = ()

    @property
    def matched_video_ids(self) -> frozenset[str]:
        return frozenset(m.video.id for m in self.matches) | frozenset(
            h.video.id for h in self.held
        )


LENGTH_SLACK_SECONDS = 300  # a difference under five minutes is never evidence
LENGTH_RATIO = 2.0  # beyond half/double the runtime the video is probably something else


def length_mismatch(runtime_minutes: int | None, duration_seconds: int | None) -> str | None:
    """A reason when a video's length contradicts the episode's runtime; None when they
    agree or either is unknown (an unknown runtime is no evidence, not a veto).
    Calibrated on 282 real matches: every correct one sat within 0.39–2.31x the TVDB
    runtime, and the outliers were exact-title matches whose flat TVDB runtime was the
    wrong number, not the wrong video."""
    if not runtime_minutes or not duration_seconds:
        return None
    runtime = runtime_minutes * 60
    if abs(duration_seconds - runtime) <= LENGTH_SLACK_SECONDS:
        return None
    if runtime / LENGTH_RATIO <= duration_seconds <= runtime * LENGTH_RATIO:
        return None
    return (
        f"video runs {mmss(duration_seconds)}, Sonarr says the episode runs {runtime_minutes} min"
    )


def mmss(seconds: int) -> str:
    """A length the way a player shows it: 4:08, 23:02, 1:05:09."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_WS = re.compile(r"\s+")
_EP_PREFIX = re.compile(
    r"^(?:ep(?:isode)?\.?\s*\d+|#\s*\d+|\d+\.(?=\s))\s*[-:|–—]?\s*", re.IGNORECASE
)
# The show's own count sits at the head ("#751 - …", "KT #751 - …", "KT#751 …"): at most
# one word before it, so a "#2024" hashtag in a title's tail is not a show number.
_HASH_NUMBER = re.compile(r"^\s*(?:[^\s#]+\s*)?#\s*(\d+)")


def show_number(title: str) -> int | None:
    """A show's own episode count in a title: "#751 - JOE ROGAN" / "KT #751 - …" → 751.
    Not Sonarr's numbering (that is S2026E01), so it never drives the regex strategy; it
    is a guard: two titles that both carry a number can only pair when the numbers agree."""
    m = _HASH_NUMBER.search(title)
    return int(m.group(1)) if m else None


def normalise_title(text: str) -> str:
    text = text.lower()
    text = _EP_PREFIX.sub("", text)
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def wildcard_fragments(title: str) -> tuple[str, ...]:
    """The normalised pieces either side of an ellipsis wildcard, or () when the title
    has no gap to fill: a trailing "..." leaves one piece, which is plain containment."""
    pieces = [normalise_title(piece) for piece in _ELLIPSIS.split(title)]
    pieces = [piece for piece in pieces if piece]
    return tuple(pieces) if len(pieces) >= 2 else ()


def _contains_in_order(have: str, fragments: tuple[str, ...]) -> bool:
    """Every fragment appears in `have`, in order, on word boundaries; the gaps between
    them may be empty or any number of words."""
    padded = f" {have} "
    pos = 0
    for fragment in fragments:
        idx = padded.find(f" {fragment} ", pos)
        if idx < 0:
            return False
        pos = idx + len(fragment) + 1  # the next fragment starts after this one's words
    return True


# "Part N" markers in the shapes uploaders use: "(Part 1/5)", "(Part 2)", "(1/5)",
# "Part 1 of 2", "Pt. 3/17", "1 of 4", "Part 2". Read on the RAW title: normalisation
# keeps the words but drops the brackets that make "(1/5)" unmistakable.
_PART_MARKER = re.compile(
    r"""
    # (Part 1/5) (Part II) (1/5)
    \(\s*(?:(?:part|pt\.?)\s*(?P<n1>\d{1,2}|WORDS)(?:\s*[/⧸]\s*(?P<m1>\d{1,2}))?
          |(?P<n2>\d{1,2})\s*[/⧸]\s*(?P<m2>\d{1,2}))\s*\)
    # Part 1 of 2, Pt. 1/17, Part Two of Three
  | \b(?:part|pt\.?)\s*(?P<n3>\d{1,2}|WORDS)\s*(?:[/⧸]|\s+of\s+)\s*(?P<m3>\d{1,2}|WORDS)\b
    # 1 of 4
  | \b(?P<n4>\d{1,2})\s+of\s+(?P<m4>\d{1,2})\b
    # Part 2, Part One, Part IV
  | \b(?:part|pt\.?)\s*(?P<n5>\d{1,2}|WORDS)\b
    """.replace(
        "WORDS", "one|two|three|four|five|six|seven|eight|nine|ten|i{1,3}|iv|v|vi{1,3}|ix|x"
    ),
    re.IGNORECASE | re.VERBOSE,
)
_PART_WORDS = {
    w: i + 1
    for i, w in enumerate(
        ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
    )
}
_PART_ROMAN = {
    r: i + 1 for i, r in enumerate(["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"])
}


def _part_number(token: str) -> int:
    t = token.lower()
    return int(t) if t.isdigit() else _PART_WORDS.get(t) or _PART_ROMAN.get(t) or 0


def has_part_marker(title: str) -> bool:
    """Whether a title says it is one part of a longer whole. Counts are two digits at
    most (so "Top 10 of 2024" is a year, not a part) and need 1 ≤ N ≤ M: "0 of 3" and
    "5 of 3" are not parts of anything."""
    for m in _PART_MARKER.finditer(title):
        groups = m.groupdict()
        n = next(_part_number(v) for k, v in groups.items() if k.startswith("n") and v)
        total = next((_part_number(v) for k, v in groups.items() if k.startswith("m") and v), None)
        if n < 1 or (total is not None and n > total):
            continue
        return True
    return False


def part_mismatch(episode_title: str, video_title: str) -> str | None:
    """A reason when the video says it is one part of a split upload and the episode is
    whole in Sonarr: importing it would file a fragment as the episode, flip hasFile,
    and the other parts would never be fetched. An episode that names a part itself
    may take a part; an exact title carries the same marker on both sides anyway."""
    if has_part_marker(video_title) and not has_part_marker(episode_title):
        return "video is one part of a split upload; the episode is whole in Sonarr"
    return None


def _loose(text: str) -> str:
    """Lower-case, punctuation to spaces, whitespace collapsed: the comparison form for
    the title scope. Not `normalise_title`, which also strips episode prefixes."""
    return _WS.sub(" ", _PUNCT.sub(" ", text.lower())).strip()


def in_scope(title: str, phrase: str | None) -> bool:
    """Whether a video title contains the subscription's required phrase (case- and
    punctuation-insensitive substring). No phrase → everything is in scope."""
    wanted = _loose(phrase or "")
    if not wanted:
        return True  # nothing (or only punctuation) to require
    return wanted in _loose(title)


def compile_title_regex(pattern: str) -> re.Pattern[str]:
    """Validate a user regex: it must have an `episode` group; `season` is optional."""
    rx = re.compile(pattern, re.IGNORECASE)
    if "episode" not in rx.groupindex:
        raise ValueError("title_regex needs a named group (?P<episode>...)")
    return rx


def parse_with_regex(rx: re.Pattern[str], title: str) -> tuple[int | None, int] | None:
    m = rx.search(title)
    if not m:
        return None
    try:
        episode = int(m.group("episode"))
        season = int(m.group("season")) if "season" in rx.groupindex and m.group("season") else None
    except (TypeError, ValueError):
        return None
    return season, episode


def _candidates(
    strategy: str,
    ep: Episode,
    videos: list[Video],
    overrides: dict[str, Override],
    cfg: MatchConfig,
    rx: re.Pattern[str] | None,
) -> list[Video]:
    if strategy == "override":
        return [
            v
            for v in videos
            if (o := overrides.get(v.id)) is not None
            and o.season == ep.season
            and o.episode == ep.number
        ]
    if strategy == "regex":
        if rx is None:
            return []
        out = []
        for v in videos:
            parsed = parse_with_regex(rx, v.title)
            if parsed is None:
                continue
            season, number = parsed
            if number == ep.number and (season is None or season == ep.season):
                out.append(v)
        return out
    if strategy == "title":
        return _title_candidates(ep, videos)[1]
    if strategy == "date":
        if ep.air_date is None:
            return []
        expected = ep.air_date + timedelta(days=cfg.date_offset_days)
        tol = timedelta(days=cfg.date_tolerance_days)
        return [
            v for v in videos if v.upload_date is not None and abs(v.upload_date - expected) <= tol
        ]
    raise ValueError(f"unknown strategy {strategy!r}")


def is_unavailable(video: Video) -> bool:
    """A private/deleted/removed entry: the listing carries only its id as the title.
    It can never be downloaded, so it is never a candidate for any strategy."""
    return video.title == video.id


def _title_candidates(ep: Episode, videos: list[Video]) -> tuple[str, list[Video]]:
    """(tier, candidates): exact normalised equality first; otherwise containment on
    word boundaries, for episode titles with at least two words, skipping promos.
    Containment where both titles carry the show's own number and it agrees is tier
    "numbered": it settles a claim like an exact title, but it is still containment
    ("KT #751 - … (clip)" contains "#751 - …"), so the length check still applies."""
    want = normalise_title(ep.title)
    if not want:
        return ("none", [])
    want_no = show_number(ep.title)

    def numbers_clash(v: Video) -> bool:
        have_no = show_number(v.title)
        return want_no is not None and have_no is not None and have_no != want_no

    videos = [v for v in videos if not numbers_clash(v)]
    exact = [v for v in videos if normalise_title(v.title) == want]
    if exact:
        return ("exact", exact)
    want_tokens = want.split()
    if len(want) < MIN_CONTAINMENT_LEN or len(want_tokens) < MIN_CONTAINMENT_TOKENS:
        return ("none", [])
    fragments = wildcard_fragments(ep.title)
    if fragments and PLACEHOLDER_FRAGMENTS & set(fragments):
        return ("none", [])
    out = []
    numbered = []  # containment plus the show's own number agreeing: as good as exact
    for v in videos:
        have = normalise_title(v.title)
        if fragments:
            if not _contains_in_order(have, fragments):
                continue
        elif f" {want} " not in f" {have} ":
            continue
        if PROMO_TOKENS & (set(have.split()) - set(want_tokens)):
            continue
        out.append(v)
        if want_no is not None and show_number(v.title) == want_no:
            numbered.append(v)
    if numbered:
        return ("numbered", numbered)
    return ("contains", out)


def match(
    episodes: list[Episode],
    videos: list[Video],
    overrides: list[Override],
    cfg: MatchConfig,
) -> MatchResult:
    enabled = ("override",) + tuple(
        s for s in STRATEGY_ORDER if s in cfg.strategies and s != "override"
    )
    rx = compile_title_regex(cfg.title_regex) if cfg.title_regex and "regex" in enabled else None
    by_video = {o.video_id: o for o in overrides}
    pool: list[Video] = []
    for v in videos:  # one entry per id: a duplicate would make a pin see two candidates
        if not is_unavailable(v) and all(p.id != v.id for p in pool):
            pool.append(v)
    matched: dict[int, Match] = {}
    held: list[Held] = []
    seen: dict[int, dict[str, tuple[str, ...]]] = {ep.id: {} for ep in episodes}

    for strategy in enabled:
        while True:  # to a fixed point: a claim this round may free a candidate for another
            # A pinned video is exactly one episode's; it is never a candidate for another.
            # The title scope applies to every automatic strategy, never to a pin.
            eligible = (
                pool
                if strategy == "override"
                else [
                    v for v in pool if v.id not in by_video and in_scope(v.title, cfg.title_require)
                ]
            )
            claims: dict[str, list[tuple[Episode, str]]] = {}  # video id → (episode, tier)
            before = (len(matched), len(pool))
            for ep in episodes:
                if ep.id in matched:
                    continue
                if strategy == "title":
                    tier, cands = _title_candidates(ep, eligible)
                else:
                    tier, cands = strategy, _candidates(strategy, ep, eligible, by_video, cfg, rx)
                seen[ep.id][strategy] = tuple(v.id for v in cands)
                if len(cands) == 1:
                    claims.setdefault(cands[0].id, []).append((ep, tier))
            # Resolve claims per video: one claimant wins; among several, a single exact-title
            # claim beats containment claims ("The Return" must not take "The Return of the
            # King"); otherwise the video is ambiguous and nobody gets it this round.
            for video_id, claimants in claims.items():
                winner: Episode | None = None
                if len(claimants) == 1:
                    winner = claimants[0][0]
                else:
                    exact = [ep for ep, tier in claimants if tier in ("exact", "numbered")]
                    if len(exact) == 1:
                        winner = exact[0]
                if winner is None:
                    continue
                video = next(v for v in pool if v.id == video_id)
                pool.remove(video)
                tier = next(t for e, t in claimants if e is winner)
                # Pins are the user's word and an exact title vouches for itself (a wrong
                # runtime on TVDB is far commoner than a same-titled wrong video); the
                # other tiers are held when the video's length contradicts the runtime.
                reason = (
                    None
                    if strategy == "override" or tier == "exact"
                    else length_mismatch(winner.runtime_minutes, video.duration)
                )
                if reason is None and strategy != "override":
                    reason = part_mismatch(winner.title, video.title)
                if reason:
                    held.append(Held(winner, video, strategy, tier, reason))
                    continue  # the episode stays open for the later strategies
                matched[winner.id] = Match(winner, video, strategy, tier)
            if (len(matched), len(pool)) == before:
                break

    held_out = tuple(h for h in held if h.episode.id not in matched)  # a later match wins
    held_ids = {h.episode.id for h in held_out}
    return MatchResult(
        matches=tuple(matched[ep.id] for ep in episodes if ep.id in matched),
        unmatched=tuple(
            Unmatched(ep, seen[ep.id])
            for ep in episodes
            if ep.id not in matched and ep.id not in held_ids
        ),
        held=held_out,
    )


@dataclass(frozen=True)
class Check:
    """One line of the answer to "why did these two not pair?": the strategy or guard,
    whether it says yes, and the reason in the words the GUI shows."""

    name: str
    passed: bool | None  # None: the strategy is off, or has nothing to work with
    detail: str


def explain_pair(
    ep: Episode,
    video: Video,
    cfg: MatchConfig,
    overrides: list[Override] | tuple[Override, ...] = (),
) -> list[Check]:
    """Why this video does or does not pair with this episode, strategy by strategy, in
    the order the matcher runs them. It answers for the pair alone: a strategy that says
    yes here can still lose in a real scan, because a strategy needs EXACTLY one
    candidate and another episode may claim the same video."""
    checks: list[Check] = []
    pinned_here = next(
        (
            o
            for o in overrides
            if o.video_id == video.id and (o.season, o.episode) == (ep.season, ep.number)
        ),
        None,
    )
    pinned_elsewhere = next((o for o in overrides if o.video_id == video.id), None)
    if pinned_here is not None:
        checks.append(
            Check("pinned", True, "you pinned this video to this episode; a pin always wins")
        )
    elif pinned_elsewhere is not None:
        checks.append(
            Check(
                "pinned",
                False,
                f"this video is pinned to "
                f"S{pinned_elsewhere.season:02d}E{pinned_elsewhere.episode:02d}, so it is not a "
                "candidate for anything else",
            )
        )
    else:
        checks.append(Check("pinned", None, "not pinned either way"))

    if is_unavailable(video):
        checks.append(
            Check(
                "listed",
                False,
                "the listing carries only this video's id: private, removed or deleted",
            )
        )
        return checks
    if not in_scope(video.title, cfg.title_require):
        checks.append(
            Check(
                "title must contain",
                False,
                f"the video's title does not carry “{cfg.title_require}”, so no automatic "
                "strategy considers it (a pin still would)",
            )
        )
        return checks
    if cfg.title_require:
        checks.append(Check("title must contain", True, f"the title carries “{cfg.title_require}”"))

    if "regex" not in cfg.strategies:
        checks.append(Check("regex", None, "the regex strategy is off for this subscription"))
    elif not cfg.title_regex:
        checks.append(Check("regex", None, "no pattern is set"))
    else:
        rx = compile_title_regex(cfg.title_regex)
        parsed = parse_with_regex(rx, video.title) if rx else None
        if parsed is None:
            checks.append(
                Check("regex", False, "the pattern finds no episode number in the video's title")
            )
        else:
            season, number = parsed
            got = f"episode {number}" + (
                f", season {season}" if season is not None else ", no season"
            )
            if number == ep.number and (season is None or season == ep.season):
                checks.append(Check("regex", True, f"the pattern reads {got}"))
            else:
                checks.append(
                    Check(
                        "regex",
                        False,
                        f"the pattern reads {got}: not S{ep.season:02d}E{ep.number:02d}",
                    )
                )

    checks.append(_title_check(ep, video, cfg))

    if "date" not in cfg.strategies:
        checks.append(Check("date", None, "the date strategy is off for this subscription"))
    elif ep.air_date is None:
        checks.append(Check("date", None, "Sonarr has no air date for this episode"))
    elif video.upload_date is None:
        checks.append(
            Check(
                "date",
                None,
                "this video's upload date is not known yet (Fetch upload dates gets it)",
            )
        )
    else:
        expected = ep.air_date + timedelta(days=cfg.date_offset_days)
        off_by = abs((video.upload_date - expected).days)
        within = off_by <= cfg.date_tolerance_days
        checks.append(
            Check(
                "date",
                within,
                f"uploaded {video.upload_date}, aired {ep.air_date}"
                + (f" (offset {cfg.date_offset_days:+d} d)" if cfg.date_offset_days else "")
                + f": {off_by} day{'' if off_by == 1 else 's'} apart, "
                + f"tolerance {cfg.date_tolerance_days}",
            )
        )

    part = part_mismatch(ep.title, video.title)
    if part:
        checks.append(Check("split upload", False, part + "; a match would be held, not queued"))
    length = length_mismatch(ep.runtime_minutes, video.duration)
    if length:
        checks.append(Check("length", False, length + "; a match would be held, not queued"))
    elif ep.runtime_minutes and video.duration:
        checks.append(
            Check(
                "length",
                True,
                f"video runs {mmss(video.duration)}, Sonarr says {ep.runtime_minutes} min",
            )
        )
    else:
        checks.append(
            Check("length", None, "no length to compare (the video's, the episode's, or both)")
        )
    return checks


def _title_check(ep: Episode, video: Video, cfg: MatchConfig) -> Check:
    if "title" not in cfg.strategies:
        return Check("title", None, "the title strategy is off for this subscription")
    want, have = normalise_title(ep.title), normalise_title(video.title)
    if not want:
        return Check("title", False, "the episode's title is only a placeholder once tidied")
    want_no, have_no = show_number(ep.title), show_number(video.title)
    if want_no is not None and have_no is not None and have_no != want_no:
        return Check("title", False, f"the show's own numbers disagree: #{want_no} and #{have_no}")
    if want == have:
        return Check("title", True, f"the titles are the same once tidied: “{want}”")
    want_tokens = want.split()
    if len(want) < MIN_CONTAINMENT_LEN or len(want_tokens) < MIN_CONTAINMENT_TOKENS:
        return Check(
            "title",
            False,
            f"“{want}” is too short to look for inside another title "
            f"(needs {MIN_CONTAINMENT_LEN} characters and {MIN_CONTAINMENT_TOKENS} words)",
        )
    fragments = wildcard_fragments(ep.title)
    if fragments and PLACEHOLDER_FRAGMENTS & set(fragments):
        return Check(
            "title",
            False,
            "the episode's title is still a placeholder (TBA), so it matches nothing",
        )
    if fragments:
        if not _contains_in_order(have, fragments):
            missing = [f for f in fragments if f" {f} " not in f" {have} "]
            return Check(
                "title",
                False,
                "the episode's title has a “…” wildcard; "
                + (
                    f"the video's title lacks {', '.join(repr(x) for x in missing)}"
                    if missing
                    else "the video's title has the pieces in the wrong order"
                ),
            )
    elif f" {want} " not in f" {have} ":
        return Check("title", False, f"“{want}” does not appear inside “{have}”")
    promos = PROMO_TOKENS & (set(have.split()) - set(want_tokens))
    if promos:
        return Check(
            "title",
            False,
            f"the video's title says {', '.join(sorted(promos))}: a promo, not the episode",
        )
    if want_no is not None and have_no == want_no:
        return Check(
            "title", True, f"the episode's title is inside the video's, and both say #{want_no}"
        )
    return Check("title", True, f"“{want}” appears inside “{have}”")


def videos_needing_dates(
    result: MatchResult,
    videos: list[Video],
    cfg: MatchConfig,
    overrides: list[Override] | tuple[Override, ...] = (),
) -> list[Video]:
    """Videos worth a per-video fetch: only when the date strategy is on, something is
    still unmatched, and the video is unassigned, undated, in the title scope and not
    pinned — the date strategy can never see the other two, so dating them is waste."""
    if "date" not in cfg.strategies or not (result.unmatched or result.held):
        return []
    used = result.matched_video_ids | {o.video_id for o in overrides}
    return [
        v
        for v in videos
        if v.id not in used
        and v.upload_date is None
        and not is_unavailable(v)
        and in_scope(v.title, cfg.title_require)
    ]
