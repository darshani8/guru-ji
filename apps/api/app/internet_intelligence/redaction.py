"""Take individuals' names out of the text the internet intelligence stores.

Under India's DPDP Act a person's name is personal data, and neither the
internet map nor the P0 reports need one: they are about institutions. The
text they keep still carries names, from the web and from people ("Review by
Anil Kumar, 2nd year CSE", "Ramesh Kumar S/O Late Gowda vs BGS College",
"Dr. Ravi Kumar G K (the principal)"), so it goes through
``strip_person_names`` before it is written, and the map store runs it again
over free-text evidence and review items as a backstop.

A name is recognised only by what marks it as a person's: an honorific, a
relation (S/O, D/O, W/O), a party to a case that is not an institution, a
role beside it, or an @handle it introduces. A phrase with an institutional
word in it is an institution. The names in ``keep`` (the map's entities and
their aliases, the profile's names) are never touched, and neither is a
phrase made only of their words. Lower-case text, URLs, e-mail addresses,
@handles and ALL-CAPS acronyms are left alone (capitals are read as a name
only right beside S/O, where court papers write names that way). The rules
are narrow on purpose: "[name]" in place of an institution's or the
Swamiji's name misleads everyone who reads the map, so a doubtful phrase is
left as it is.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from typing import Any

PLACEHOLDER = "[name]"

# A capitalised word ("Ravi", "D'Souza", "Anne-Marie") or an initial ("G", "K.").
_WORD = r"(?:[A-Z]['’])?[A-Z][a-z]+(?:-[A-Z]?[a-z]+)*"
_INITIAL = r"[A-Z](?:\.|(?![\w/'’-]))"
_CAPS = r"[A-Z]{2,}(?![\w@/])"
_TOKEN = rf"(?:{_WORD}|{_INITIAL})"
_SEP = r"(?:[ \t]+|(?<=\.))"
_START = r"(?<![\w@#/.&-])"
# A name ends at anything but more of a word; a possessive "'s" is not more of it.
_END = r"(?![\w@/-]|['’][A-Za-z]{2})"
_HON = r"(?:Dr|Prof|Mrs|Mr|Ms|Smt|Shri|Sri)\.?|Kum\."
_REL = r"(?:[SDW]/[Oo]|[sd]/o)\.?"
_VS = r"(?:[Vv][Ss]\.?|VS\.?|v\.|[Vv]/[Ss]|[Vv]ersus|VERSUS)"


def _run(token: str, least: int, most: int) -> str:
    return rf"{token}(?:{_SEP}{token}){{{least - 1},{most - 1}}}"


_HONORIFIC = re.compile(rf"{_START}(?:(?:{_HON})[ \t]+)+(?P<name>{_run(_TOKEN, 1, 6)}){_END}")
_RELATION = re.compile(
    rf"{_START}(?P<before>{_run(rf'(?:{_WORD}|{_CAPS}|{_INITIAL})', 1, 5)})[ \t]*,?[ \t]+{_REL}[ \t]+(?:(?i:late)\.?[ \t]+)?"
    rf"(?P<after>(?:(?:{_HON})[ \t]+)*{_run(rf'(?:{_WORD}|{_CAPS}|{_INITIAL})', 1, 5)}){_END}"
)
_PARTY_TOKEN = rf"(?!{_VS}(?!\w))(?:(?:{_HON})(?=[ \t])|{_REL}|M/[Ss]\.?|{_WORD}|{_CAPS}|{_INITIAL}|&|(?i:of|and|the|for)(?!\w))"
_CASE = re.compile(rf"{_START}(?P<left>{_run(_PARTY_TOKEN, 1, 8)})[ \t]+{_VS}[ \t]+(?P<right>{_run(_PARTY_TOKEN, 1, 8)})(?![\w@/])")
_ROLE_BEFORE = r"(?i:\b(?:review(?:ed)?[ \t]+by|posted[ \t]+by|written[ \t]+by|principal|hod|lecturer|professor|faculty|student|petitioner|respondent|applicant|accused|alumnus|alumna|alumni)\b)"
_BEFORE_ROLE = re.compile(rf"{_ROLE_BEFORE}[ \t]*[:,-]?[ \t]+(?P<name>{_run(_TOKEN, 2, 4)}){_END}(?![ \t]+{_TOKEN})")
_ROLE_AFTER = (
    r"(?i:(?:(?:a|an|the|our)[ \t]+)?(?:(?:\d(?:st|nd|rd|th)|first|second|third|fourth|final|pre-final)[ \t]+year|(?:asst\.?|assistant|associate)[ \t]+professor"
    r"|students?|principal|hod|lecturer|professor|faculty|petitioner|respondent|applicant|accused|alumnus|alumna|alumni)\b)"
)
_AFTER_ROLE = re.compile(rf"{_START}(?P<name>{_run(_TOKEN, 2, 4)}){_END}(?:[ \t]*[,:][ \t]*|[ \t]+[-–—|][ \t]+|[ \t]*\([ \t]*){_ROLE_AFTER}")
# "Priya Sharma (@bgscet_cse) • Instagram": a display name that opens a snippet and introduces a handle.
_HANDLE_LEAD = re.compile(rf"(?:^|(?<=\n)|(?<=: )|(?<=['\"“‘]))[ \t]*(?P<name>{_run(_TOKEN, 2, 4)}){_END}[ \t]*(?:[(\[][ \t]*|[•·|-][ \t]*)?@[A-Za-z0-9_.]+")
# Never touched: links, e-mail addresses and handles.
_UNTOUCHABLE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|(?<![\w])@[A-Za-z0-9_.]+")
_NAME_TOKEN = re.compile(rf"(?<![\w/'’-])(?:{_WORD}|{_CAPS}|{_INITIAL})")
_REAL_WORD = re.compile(rf"(?:{_WORD}|{_CAPS})")
# Capitalised words and joiners that carry an institution's name on past the name itself.
_TAIL = re.compile(r"(?:[ \t]+(?:and|&|[A-Z][\w'’.&-]*)){0,6}")
_CONNECTORS = re.compile(r"^(?:(?:the|of|and|for|&)[ \t]+)+|(?:[ \t]+(?:(?:and|&)[ \t]+(?:ors?|anr|others?|another)\.?|the|of|and|for|&))+$", re.IGNORECASE)

# A phrase with one of these is an organisation, not a person.
_INSTITUTIONAL = frozenset("""
college colleges institute institutes institution institutions university universities trust trusts math matha mutt samsthana mahasamsthana state union board
government govt society societies ltd limited pvt council authority school schools academy academia hospital hospitals foundation polytechnic association sangha
parishat samaja company corporation bank ministry commission committee court temple centre center group organisation organization club forum services solutions
technologies industries enterprises llp inc party congress vidyalaya vidyapeetha shikshana
""".split())
# Words that are never part of a person's name and say the phrase is about something else:
# a festival, a film, or a deity ("Sri Kalabhairava Swami" is the Math's, not a devotee).
_NOT_PERSON = frozenset("""
gurudev namah bhagavan bhagwan navami jayanti jayanthi chaturthi janmashtami ashtami ekadashi utsav utsava mahotsava rathotsava jatre jatra habba puja pooja vrata
festival temple mandir kshetra peetha film movie album song songs trailer episode kalabhairava kalabhairaveshwara bhairava bhairaveshwara gangadhareshwara
ganesha ganapathi venkateshwara chamundeshwari
""".split())
# Titles, roles, places and ordinary words that sit beside a name but are not part of it.
_STOP = frozenset("""
the a an of and for in on at to by with from our your my his her their its this that these those is are was were be has have had new all not no who what when where
why how dr prof mr mrs ms smt sri shri kum late ors anr others another
principal hod lecturer professor faculty student students petitioner respondent applicant accused alumnus alumna alumni review reviews reviewed posted written
chairman chairperson director secretary president vice dean registrar officer coordinator head staff member members team chief executive manager trustee founder
chancellor pontiff seer justice judge department dept engineering technology science sciences arts commerce management computer mechanical civil electrical
electronics communication communications information data artificial intelligence machine learning mathematics physics chemistry biology biotechnology
architecture pharmacy nursing medical medicine dental law education research development innovation programme program course degree bachelor bachelors master
masters semester batch class year final first second third fourth examination exam exams results result admission admissions placement placements scholarship
fee fees hostel library campus sports cultural technical workshop seminar conference webinar hackathon fest lab laboratory block building auditorium convocation
graduation orientation induction day week month annual national international global public open meet event events news notice circular update updates official
page account channel profile photos videos posts post reels followers subscribers message desk speaks welcome congratulations greetings happy best dear thanks
thank birthday wishes visit visited inaugurated inauguration celebration celebrations award awards winner winners topper toppers rank ranking yesterday today
india indian karnataka bengaluru bangalore mysuru mysore mandya tumakuru tumkur kengeri nagamangala mahalakshmipuram chikkaballapur chikkaballapura chickballapur
chikkamagaluru shivamogga shimoga mangaluru mangalore delhi mumbai chennai hyderabad kerala tamil nadu andhra pradesh telangana maharashtra goa lanka london usa
america uae dubai nagar road street layout cross main stage phase district taluk city town village north south east west
instagram facebook youtube linkedin twitter telegram whatsapp google reddit github threads quora wikipedia maps play store app
january february march april may june july august september october november december jan feb mar apr jun jul aug sep sept oct nov dec
monday tuesday wednesday thursday friday saturday sunday
""".split())
# Words that may stand in a party to a case without making it anything but a person.
_PARTY_GLUE = frozenset({"dr", "prof", "mr", "mrs", "ms", "smt", "sri", "shri", "kum", "late", "the", "of", "and", "for", "ors", "anr", "others", "another"})
_PARTY_STOP = _STOP - _PARTY_GLUE
_CAPS_WORD = re.compile(_CAPS)
# An honorific or a relation marks a one-word party as a person ("Smt. Lakshmi vs State").
_MARKED = re.compile(rf"(?:^|\s)(?:{_HON}|{_REL})(?=\s|$)")


def names_of(entities: Iterable[Mapping[str, Any]]) -> list[str]:
    """Every name and alias of the given map entities: what ``keep`` protects."""

    return [str(name) for entity in entities for name in [entity.get("name"), *(entity.get("names") or [])] if name]


@lru_cache(maxsize=64)
def _keep_rules(keep: tuple[str, ...]) -> tuple[re.Pattern[str] | None, frozenset[str]]:
    """A pattern finding any kept name (any case, any spacing) and the words kept names are made of."""

    parts = sorted((r"\s+".join(re.escape(word) for word in name.split()) for name in keep), key=len, reverse=True)
    pattern = re.compile(rf"(?<!\w)(?:{'|'.join(parts)})(?!\w)", re.IGNORECASE) if parts else None
    return pattern, frozenset(word.lower() for name in keep for word in re.findall(r"[A-Za-z]+", name) if len(word) >= 3)


def _plain(token: str) -> str:
    return token.strip(".'’").lower()


def _words(text: str) -> list[str]:
    return [word.lower() for word in re.findall(r"[A-Za-z]+", text)]


def _stop_runs(tokens: list[re.Match[str]]) -> list[list[re.Match[str]]]:
    """The tokens split at every title, role or ordinary word: what is left are the stretches that can be a name."""

    runs: list[list[re.Match[str]]] = [[]]
    for token in tokens:
        if _plain(token.group()) in _STOP:
            runs.append([])
        else:
            runs[-1].append(token)
    return [run for run in runs if run]


class _Redactor:
    def __init__(self, text: str, keep: tuple[str, ...]) -> None:
        self.text = text
        pattern, self.keep_words = _keep_rules(keep)
        self.protected = [match.span() for match in _UNTOUCHABLE.finditer(text)]
        if pattern is not None:
            self.protected += [match.span() for match in pattern.finditer(text)]
        self.spans: list[tuple[int, int]] = []

    def _clear(self, start: int, end: int) -> bool:
        return all(end <= left or start >= right for left, right in self.protected)

    def _not_a_person(self, start: int, end: int, *, tail: bool) -> bool:
        """An institution ("Sri Siddhartha Institute of Technology"), a festival or a film: words beside the name say so."""

        words = _words(self.text[start:end]) + (_words(found.group()) if tail and (found := _TAIL.match(self.text, end)) else [])
        return bool(_INSTITUTIONAL.intersection(words) or _NOT_PERSON.intersection(words))

    def _kept_word(self, word: str) -> bool:
        # A long word may also end a kept one: "Sri Nirmalanandanatha Swamiji" is the Mahaswamiji.
        return word in self.keep_words or (len(word) >= 6 and any(kept.endswith(word) for kept in self.keep_words))

    def _person(self, run: list[re.Match[str]], least: int) -> bool:
        """Enough of a name, with a real word in it, and not made only of kept names' words."""

        if len(run) < least or not any(_REAL_WORD.fullmatch(token.group()) for token in run):
            return False
        words = [word for token in run for word in _words(token.group()) if len(word) >= 3]
        return not (words and all(self._kept_word(word) for word in words))

    def _add(self, start: int, end: int) -> None:
        if start < end and self._clear(start, end):
            self.spans.append((start, end))

    def _tokens(self, start: int, end: int) -> list[re.Match[str]]:
        return list(_NAME_TOKEN.finditer(self.text, start, end))

    def honorifics(self) -> None:
        for match in _HONORIFIC.finditer(self.text):
            start, end = match.span("name")
            tokens = self._tokens(start, end)
            runs = _stop_runs(tokens)
            # "Sri Lanka", "Sri Rama Navami", "Sri Siddhartha Institute": not a person after all.
            if not runs or runs[0][0] is not tokens[0] or self._not_a_person(start, end, tail=True):
                continue
            words = [index for index, token in enumerate(runs[0]) if _REAL_WORD.fullmatch(token.group())]
            run = runs[0][: words[4]] if len(words) > 4 else runs[0]  # one to four words, and any initials among them
            if self._person(run, 1):
                self._add(match.start(), run[-1].end())

    def relations(self) -> None:
        for match in _RELATION.finditer(self.text):
            before = _stop_runs(self._tokens(*match.span("before")))
            after = _stop_runs(self._tokens(*match.span("after")))
            if before and self._person(before[-1], 1) and not self._not_a_person(*match.span("before"), tail=False):
                self._add(before[-1][0].start(), before[-1][-1].end())
            if after and self._person(after[0], 1) and not self._not_a_person(*match.span("after"), tail=False):
                self._add(match.start("after"), after[0][-1].end())

    def parties(self) -> None:
        for match in _CASE.finditer(self.text):
            for side in ("left", "right"):
                self._party(*match.span(side))

    def _party(self, start: int, end: int) -> None:
        raw = self.text[start:end]
        trimmed = _CONNECTORS.sub(lambda found: " " * len(found.group()), raw)
        lead, body = len(trimmed) - len(trimmed.lstrip()), trimmed.strip()
        if not body or "m/s" in body.lower() or self._not_a_person(start, end, tail=False):
            return
        start = start + lead
        end = start + len(body)
        tokens = self._tokens(start, end)
        # An acronym or an ordinary word ("BGSCET", "India", "Principal") says it is not simply a person.
        if any(_CAPS_WORD.fullmatch(token.group()) or _plain(token.group()) in _PARTY_STOP for token in tokens):
            return
        names = [token for token in tokens if _plain(token.group()) not in _PARTY_GLUE]
        if self._person(names, 1 if _MARKED.search(body) else 2):
            self._add(start, end)

    def roles(self) -> None:
        for pattern, nearest in ((_BEFORE_ROLE, 0), (_AFTER_ROLE, -1), (_HANDLE_LEAD, -1)):
            for match in pattern.finditer(self.text):
                start, end = match.span("name")
                runs = _stop_runs(self._tokens(start, end))
                if runs and not self._not_a_person(start, end, tail=pattern is _BEFORE_ROLE) and self._person(runs[nearest], 2):
                    self._add(runs[nearest][0].start(), runs[nearest][-1].end())

    def result(self) -> str:
        merged: list[list[int]] = []
        for start, end in sorted(self.spans):
            if merged and start < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        text = self.text
        for start, end in reversed(merged):
            text = text[:start] + PLACEHOLDER + text[end:]
        return text


def strip_person_names(text: str, *, keep: Iterable[str] = ()) -> str:
    """``text`` with individuals' names replaced by "[name]"; the ``keep`` names (any case) are never touched."""

    if not text or not isinstance(text, str):
        return text
    kept = tuple(dict.fromkeys(cleaned for cleaned in (" ".join(str(name).split()) for name in keep if name) if len(cleaned) >= 3))
    redactor = _Redactor(text, kept)
    redactor.honorifics()
    redactor.relations()
    redactor.parties()
    redactor.roles()
    return redactor.result()


__all__ = ["PLACEHOLDER", "names_of", "strip_person_names"]
