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


@dataclass(frozen=True)
class Episode:
    id: int
    season: int
    number: int
    title: str
    air_date: date | None


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    url: str
    upload_date: date | None = None


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


@dataclass(frozen=True)
class Match:
    episode: Episode
    video: Video
    strategy: str


@dataclass(frozen=True)
class Unmatched:
    episode: Episode
    # strategy → candidate video ids it saw (0 or several); absent = strategy not run
    candidates: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class MatchResult:
    matches: tuple[Match, ...]
    unmatched: tuple[Unmatched, ...]

    @property
    def matched_video_ids(self) -> frozenset[str]:
        return frozenset(m.video.id for m in self.matches)


_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_WS = re.compile(r"\s+")
_EP_PREFIX = re.compile(
    r"^(?:ep(?:isode)?\.?\s*\d+|#\s*\d+|\d+\.(?=\s))\s*[-:|–—]?\s*", re.IGNORECASE
)


def normalise_title(text: str) -> str:
    text = text.lower()
    text = _EP_PREFIX.sub("", text)
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


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
    word boundaries, for episode titles with at least two words, skipping promos."""
    want = normalise_title(ep.title)
    if not want:
        return ("none", [])
    exact = [v for v in videos if normalise_title(v.title) == want]
    if exact:
        return ("exact", exact)
    want_tokens = want.split()
    if len(want) < MIN_CONTAINMENT_LEN or len(want_tokens) < MIN_CONTAINMENT_TOKENS:
        return ("none", [])
    out = []
    for v in videos:
        have = normalise_title(v.title)
        if f" {want} " not in f" {have} ":
            continue
        if PROMO_TOKENS & (set(have.split()) - set(want_tokens)):
            continue
        out.append(v)
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
    pool: list[Video] = [v for v in videos if not is_unavailable(v)]
    matched: dict[int, Match] = {}
    seen: dict[int, dict[str, tuple[str, ...]]] = {ep.id: {} for ep in episodes}

    for strategy in enabled:
        while True:  # to a fixed point: a claim this round may free a candidate for another
            # A pinned video is exactly one episode's; it is never a candidate for another.
            eligible = pool if strategy == "override" else [v for v in pool if v.id not in by_video]
            claims: dict[str, list[tuple[Episode, str]]] = {}  # video id → (episode, tier)
            before = len(matched)
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
                    exact = [ep for ep, tier in claimants if tier == "exact"]
                    if len(exact) == 1:
                        winner = exact[0]
                if winner is None:
                    continue
                video = next(v for v in pool if v.id == video_id)
                matched[winner.id] = Match(winner, video, strategy)
                pool.remove(video)
            if len(matched) == before:
                break

    return MatchResult(
        matches=tuple(matched[ep.id] for ep in episodes if ep.id in matched),
        unmatched=tuple(Unmatched(ep, seen[ep.id]) for ep in episodes if ep.id not in matched),
    )


def videos_needing_dates(result: MatchResult, videos: list[Video], cfg: MatchConfig) -> list[Video]:
    """Videos worth a per-video fetch: only when the date strategy is on, something is
    still unmatched, and the video is unassigned and undated."""
    if "date" not in cfg.strategies or not result.unmatched:
        return []
    used = result.matched_video_ids
    return [
        v for v in videos if v.id not in used and v.upload_date is None and not is_unavailable(v)
    ]
