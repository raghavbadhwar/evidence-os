"""Suggest agreement families from reviewer-supplied register entries and original passages."""

import re
from dataclasses import dataclass
from datetime import date
from functools import cached_property

from pydantic import Field, field_validator

from .contracts import Contract, Label

DASHES = dict.fromkeys(range(0x2010, 0x2015), "-")
SUFFIX = re.compile(r"\s+(limited|ltd|inc|llc|plc)\.?$", re.I)
REFERENCE = re.compile(r"(?<![\w-])([A-Z][A-Z0-9]{0,15}-[0-9][A-Z0-9-]*)(?![\w-])")
INDIRECT = re.compile(r"\b(does not (?:amend|relate to|apply to)|purchase (?:order|request)|invoice number)\b", re.I)
ILLUSTRATION = re.compile(r"\b(example only|for (?:comparison|illustration) only|training (?:guide|example))\b", re.I)


def norm(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


def mentioned(text, value):
    return bool(value) and f" {value} " in f" {text} "


class Agreement(Contract):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/-]*$")
    title: Label
    parties: list[Label] = Field(min_length=1, max_length=20)
    date: date

    @field_validator("parties")
    @classmethod
    def unique_parties(cls, names):
        if len({norm(SUFFIX.sub("", name)) for name in names}) != len(names):
            raise ValueError("duplicate party names")
        return names

    @cached_property
    def id_pattern(self):
        return re.compile(r"(?<![\w-])" + re.escape(self.id.upper()) + r"(?![\w-])")

    @cached_property
    def dates(self):
        d = self.date
        return [norm(d.isoformat()), norm(f"{d.day} {d:%B %Y}"), norm(f"{d:%B} {d.day}, {d.year}")]

    @cached_property
    def names(self):
        return {norm(SUFFIX.sub("", p)) for p in self.parties}


@dataclass(frozen=True)
class Suggestion:
    agreement: str | None
    reason: str
    passage_ids: tuple[str, ...] = ()


def suggest(passages, register):
    texts = [(p["id"], p["text"]) for p in passages]
    if any(ILLUSTRATION.search(text) for _, text in texts):
        return Suggestion(None, "Illustrative or comparison language requires review.")
    if any(INDIRECT.search(text) for _, text in texts):
        return Suggestion(None, "Indirect reference or operational document requires review.")
    known_ids = {entry.id.upper() for entry in register}
    prefixes = {key.split("-")[0] for key in known_ids}
    for _, text in texts:
        for ref in REFERENCE.findall(text.translate(DASHES).upper()):
            if ref.split("-")[0] in prefixes and ref not in known_ids:
                return Suggestion(None, "References an identifier absent from this register.")
    exact = {}
    for entry in register:
        hits = [pid for pid, t in texts if entry.id_pattern.search(t.translate(DASHES).upper())]
        if hits:
            exact[entry.id] = tuple(hits)
    if len(exact) > 1:
        return Suggestion(None, "References multiple registered agreements.")

    # Match identity within an original passage. A separate email in the export may concern a different deal.
    candidates = {}
    known_names = {name for entry in register for name in entry.names}
    leaders = set()
    for pid, text in texts:
        local_scores = {}
        normalized = norm(text)
        found = {n for n in known_names if mentioned(normalized, n)}
        # A parent's short name inside its subsidiary's name is weak evidence.
        names = {n for n in found if not any(n != longer and mentioned(longer, n) for longer in found)}
        for entry in register:
            if not any(mentioned(normalized, d) for d in entry.dates):
                continue
            matching = names & entry.names
            title = mentioned(normalized, norm(entry.title))
            if len(matching) >= 2 or (title and matching):
                score = len(matching) + int(title)
                local_scores[entry.id] = score
                prev, ids = candidates.get(entry.id, (0, []))
                candidates[entry.id] = (max(score, prev), [*ids, pid])
        if local_scores:
            best_local = max(local_scores.values())
            local_leaders = {key for key, score in local_scores.items() if score == best_local}
            if len(local_leaders) == 1:
                leaders.update(local_leaders)
    if len(leaders | set(exact)) > 1:
        return Suggestion(None, "Passages point to different agreement families.")
    if exact:
        agreement, ids = next(iter(exact.items()))
        return Suggestion(agreement, "Exact agreement reference.", ids)
    if not candidates:
        return Suggestion(None, "Insufficient identifying evidence.")
    best = max(score for score, _ in candidates.values())
    winners = [key for key, (score, _) in candidates.items() if score == best]
    if len(winners) != 1:
        return Suggestion(None, "Several agreements match the available evidence.")
    key = winners[0]
    return Suggestion(key, "Agreement date with matching parties or title.", tuple(candidates[key][1]))
