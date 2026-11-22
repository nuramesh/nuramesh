"""Autonomy policies and approvals (PLAN H.1, H.2; decision A6 and default deny).

A **policy** belongs to one role and lists rules; every agent of the role follows it. An agent may
only do what its policy allows; everything else is refused or asks a human.

**Actions** (`Action`) are what an agent wants to do: `delegate` (target: the agent it delegates
to),
`call_model` (target `provider/model`), `use_tool` (tool name), `run_command` (the command),
`write_file` (a path), `spend` (an amount in USD), `external_message` (a recipient or channel),
`merge` (a branch or change) and `network` (a destination host, bare, with an optional port).

**Rules** (`PolicyRule`) say `allow`, `ask` or `deny` for one kind of action whose target matches a
glob. A `spend` rule can carry `over`: it applies only to amounts above it ("ask above $5").

**Evaluation** (`evaluate`) is strict and order-independent:

1. A rule *matches* if its kind equals the action's kind, its target pattern matches the target
   and, for `spend`, the amount is above `over` (if the rule has one).
2. Any matching `deny` wins; otherwise any matching `ask`; otherwise any matching `allow`.
3. **No matching rule, or no policy for the role: deny** (default deny); the one exception is
   `network`: a role with a policy and no matching network rule asks a human (no policy: deny).

Target patterns: `*` matches any characters and `?` any one character, except for `write_file`,
where `*` and `?` stay inside one path segment (they don't match `/`) and `**` matches across
segments, and except for `run_command` (below). Matching is case-sensitive and must cover the whole
target.

**`run_command` (policy contract 2, ND54).** A command is matched as an **argument list**, never as
text, and commands run as argument lists, never through a shell. The command text is split into
words (POSIX quoting, no expansion); a rule pattern is split the same way and compared word by word
(`*` and `?` stay inside one word; a last word `**` accepts any number of further words; a pattern
that is exactly `*` is any simple command). A command containing a shell control operator
(`; | & ` ( ) < >` or a line break, even quoted) or that is malformed is **compound**: it is denied
unless a rule's pattern is that exact text with no wildcard (for `deny` and `ask` rules, a pattern
of exactly `*` also matches). So `allow python -m pytest*` no longer allows
`python -m pytest; curl ... | sh`. The full specification is `spec/policy/RUN_COMMAND.md`;
`POLICY_CONTRACT` is recorded in every `policy.decided` entry.

An **approval** is a human's answer to an `ask`. It is for one agent, one task and one action
(same kind and target, same amount for spend), **single use**: once an approved approval lets an
action through it is consumed. Only a human (`human:<id>`), and only one the policy names if it
lists `approvers`, can decide, and must give a reason (H.2: who and why).
"""

import ipaddress
import re
import shlex
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetCoreSchemaHandler,
    SerializerFunctionWrapHandler,
    StringConstraints,
    ValidationError,
    WithJsonSchema,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError, core_schema

from nuramesh.core.content import DEFAULT_REASON_DAYS, LedgerHasher, LedgerHashKind
from nuramesh.core.money import Currency, Money
from nuramesh.core.netaddr import (
    POLICY_DECIDED,
    classify_address,
    has_exact_host,
    host_of_target,
)
from nuramesh.core.org import OrgGraph, Slug
from nuramesh.core.people import HUMAN_ID_PATTERN, is_human_id

HumanId = Annotated[str, StringConstraints(pattern=HUMAN_ID_PATTERN)]
MAX_TARGET = 2_000
MAX_PATTERN = 500
MAX_PATTERN_WORDS = 64  # a `run_command` pattern
MAX_POLICY_PATTERN_WORDS = 1_000  # all `run_command` patterns of one policy: bounds a decision
# The version of the rules above. 1: every pattern was a glob over the whole target (a `run_command`
# pattern could match `x; curl ... | sh`). 2 (ND54): `run_command` is matched as an argument list.
# 3 (E13, T134): a check can end as an earned-autonomy `allow` (`policy.decided` then carries
# `rule_hash` and `autonomy_grant`); entries under another contract are not evidence for a grant.
# Recorded in every `policy.decided` ledger entry so an auditor knows which rules decided.
POLICY_CONTRACT = 3
# Characters a shell reads as more than one command (or as redirection or grouping), anywhere in a
# command text, even quoted, plus every control character except the tab: the command is then
# "compound" and needs a rule for that exact text (an allow), or is seen inside (a refusal).
CONTROL_CHARACTERS = frozenset(";|&`()<>") | frozenset(
    chr(c) for c in [*range(0x00, 0x09), *range(0x0A, 0x20), 0x7F]
)
_CONTROL_SPLIT = re.compile("[" + re.escape("".join(sorted(CONTROL_CHARACTERS))) + "]")


# A path with a control character (newline, NUL, DEL, U+0085) or a line or paragraph separator is a
# validation error like '..': `src/**` would otherwise allow `src/app.py\n/etc/passwd`.
_NO_PATH_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})


_HIDDEN = "<hidden>"


def _hide_input(value: Any, handler: Any) -> Any:
    scrubbed: ValidationError | None = None
    try:
        return handler(value)
    except ValidationError as exc:
        scrubbed = _scrubbed(exc)
    raise scrubbed


def _scrubbed(exc: ValidationError) -> ValidationError:
    """The same error with every line's input replaced; the context keeps only plain values."""
    lines: list[InitErrorDetails] = []
    for line in exc.errors(include_url=False, include_context=True, include_input=False):
        kind = line["type"]
        loc = tuple(line["loc"])
        if kind in ("extra_forbidden", "frozen_instance", "frozen_field") and loc:
            loc = (*loc[:-1], _HIDDEN)  # the key or attribute name may be a secret
        detail: dict[str, Any] = {"loc": loc, "input": _HIDDEN}
        if kind in ("value_error", "assertion_error"):
            detail["type"] = PydanticCustomError(kind, line["msg"])
        else:
            detail["type"] = kind
            if line.get("ctx"):
                detail["ctx"] = line["ctx"]
        lines.append(cast(InitErrorDetails, detail))
    try:
        return ValidationError.from_exception_data(exc.title, lines)
    except (TypeError, ValueError):
        return ValidationError.from_exception_data(
            exc.title,
            [
                {
                    "type": PydanticCustomError("invalid_value", "invalid value"),
                    "loc": (),
                    "input": _HIDDEN,
                }
            ],
        )


class _Frozen(BaseModel):
    # A rejected value may carry a secret (a network target's userinfo or query): never echo the
    # input in str() and repr(), also when a model is nested in another. pydantic still puts the
    # input in errors() and json(), so a wrap validator replaces every line error's input inside
    # validation. That covers init, model_validate, model_validate_strings, nested models and a
    # TypeAdapter, and the error is raised from outside any except block, so
    # `__context__` and `__cause__` are None (the raw error is not reachable; #853 d1).
    # A frozen-instance error is scrubbed the same way in `__setattr__` and `__delattr__`. A secret
    # used as an unknown key name or an assigned attribute name is replaced in `loc` too
    # (`extra_forbidden`, `frozen_instance`, `frozen_field`). Malformed JSON fails as
    # `json_invalid` before the wrap
    # validator runs and keeps the whole document as input, so `model_validate_json` is overridden
    # below to scrub it. `model_copy(update=)` validates the merged fields, and an underscore
    # name that is not a declared private attribute is refused in `__setattr__`.
    # Not covered: a `dict[str, Action]` key in an outer model's `loc`, and a TypeAdapter built
    # for `list[Action]` given malformed JSON (no model class is involved).
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        # Outermost, so it also covers the subclass's own (after) validators.
        schema: dict[str, Any] = dict(handler(source))
        ref = schema.pop("ref", None)  # the model's ref moves outward, so JSON schema is unchanged
        wrapped = core_schema.no_info_wrap_validator_function(
            _hide_input, schema, serialization=schema.get("serialization")
        )
        if ref is not None:
            wrapped["ref"] = ref
        return wrapped

    @classmethod
    def model_validate_json(cls, json_data: str | bytes | bytearray, **kwargs: Any) -> Self:
        """Like pydantic's, but a malformed document is not kept as the error input."""
        scrubbed: ValidationError | None = None
        try:
            return super().model_validate_json(json_data, **kwargs)
        except ValidationError as exc:
            scrubbed = _scrubbed(exc)
        raise scrubbed

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """Like pydantic's, but an update is validated like any input (a copy is never unchecked).

        pydantic's `model_copy(update=)` and `__replace__` skip validation; here the merged fields
        go through the model's own validators, so an invalid or unknown field raises a scrubbed
        error. Only the fields set on the original are passed on (an unset default is never made
        explicit), so `model_fields_set` is the original's set plus the update keys, as in pydantic.
        Declared private attributes are not carried over to an updated copy (none exist today).
        """
        copied = super().model_copy(deep=deep)
        if not update:
            return copied
        fields = {name: getattr(copied, name) for name in copied.model_fields_set}
        return type(self).model_validate({**fields, **update})

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_") and name not in (self.__private_attributes__ or {}):
            # pydantic lets an underscore name through a frozen model: refuse it like any other.
            raise ValidationError.from_exception_data(
                type(self).__name__,
                [{"type": "frozen_instance", "loc": (_HIDDEN,), "input": _HIDDEN}],
            )
        scrubbed: ValidationError | None = None
        try:
            super().__setattr__(name, value)
        except ValidationError as exc:
            scrubbed = _scrubbed(exc)
        if scrubbed is not None:
            raise scrubbed

    def __delattr__(self, name: str) -> None:
        scrubbed: ValidationError | None = None
        try:
            super().__delattr__(name)
        except ValidationError as exc:
            scrubbed = _scrubbed(exc)
        if scrubbed is not None:
            raise scrubbed


class ActionKind(StrEnum):
    """What kind of thing an agent wants to do."""

    DELEGATE = "delegate"
    CALL_MODEL = "call_model"
    USE_TOOL = "use_tool"
    RUN_COMMAND = "run_command"
    WRITE_FILE = "write_file"
    SPEND = "spend"
    EXTERNAL_MESSAGE = "external_message"
    MERGE = "merge"
    NETWORK = "network"


# Targets that may hold personal data or secrets are recorded as hashes, never as text (A7, S2).
HASHED_TARGETS = frozenset({ActionKind.RUN_COMMAND, ActionKind.EXTERNAL_MESSAGE})


class Decision(StrEnum):
    """What a policy says about an action."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


_HOST_NAME = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
_HOST_PORT = re.compile(r"(?P<host>\[[0-9a-f:.]{2,45}\]|[^:\[\]]+)(?::(?P<port>[0-9]{1,5}))?")


def _numeric_host_ok(host: str) -> bool:
    """A host with an all-digit or `0x` label (`167772165`, `0x0a000005`, `010.0.0.5`: other
    spellings of an address) is only accepted as dotted-decimal IPv4 without leading zeros."""
    labels = host.split(".")
    if not any(label.isdigit() or label.startswith("0x") for label in labels):
        return True
    return len(labels) == 4 and all(
        label.isascii() and label.isdigit() and int(label) <= 255 and str(int(label)) == label
        for label in labels
    )


# IPv6 ranges that embed an IPv4 address (or are routed to one). The policy cannot canonicalise them
# to the IPv4 spelling, so a deny rule on the IPv4 address would be bypassed: refused (fail closed).
# IPv4-mapped ::ffff:0:0/96, IPv4-translated ::ffff:0:0:0/96 (SIIT), IPv4-compatible ::/96
# (`::` and `::1` are plain addresses and stay),
# NAT64 64:ff9b::/96 and 64:ff9b:1::/48, 6to4 2002::/16, Teredo 2001::/32.
_IPV4_EMBEDDING = tuple(
    ipaddress.IPv6Network(n)
    for n in (
        "::ffff:0:0/96",
        "::ffff:0:0:0/96",
        "::/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2002::/16",
        "2001::/32",
    )
)


def _ipv6_host_ok(host: str) -> bool:
    """`[addr]` in the single canonical spelling (`ipaddress` compressed form, lower case), and
    not an IPv4-embedding address."""
    try:
        addr = ipaddress.IPv6Address(host[1:-1])
    except ValueError:
        return False
    if addr.ipv4_mapped is not None:
        return False
    if int(addr) > 1 and any(addr in net for net in _IPV4_EMBEDDING):
        return False
    return host == f"[{addr.compressed}]"


# a rule target that names a port (`host:443`, `*:443`, `host:*`): it stays an exact match
_HOST_ONLY_RULE = re.compile(r".*:[0-9*?]+")


def is_network_destination(text: str) -> bool:
    """H-08: whether `text` is a bare destination: a host name, IPv4 address or `[IPv6]`, with an
    optional `:port` (1 to 65535), in lower case (a rule and an approval compare text, so the caller
    normalises first). A scheme, path, query, user information or blank is not one,
    so a secret in a URL can never become a policy target or an approval payload."""
    if len(text) > 260:
        return False
    found = _HOST_PORT.fullmatch(text)
    if found is None:
        return False
    port = found.group("port")
    if port is not None and not (1 <= int(port) <= 65535 and str(int(port)) == port):
        return False
    host = found.group("host")
    # a DNS name is at most 253 characters (labels are capped at 63 by the pattern)
    if host.startswith("["):
        return _ipv6_host_ok(host)
    return len(host) <= 253 and _HOST_NAME.fullmatch(host) is not None and _numeric_host_ok(host)


class Action(_Frozen):
    """One thing an agent wants to do. `spend` has an amount; every other kind has a target."""

    # A rejected network target may carry a secret (userinfo, query): never echo the input.
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    kind: ActionKind
    target: Annotated[str, StringConstraints(min_length=1, max_length=MAX_TARGET)] | None = None
    amount: Money | None = None
    # NG-18: the ledger records this target by its keyed hash and no reason text embeds it. Not part
    # of `key` or of approval matching; stored with the approval so every later writer agrees.
    redact_target: bool = False

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.kind is ActionKind.SPEND:
            if self.amount is None or self.target is not None:
                raise ValueError("a spend action has an amount and no target")
            if self.amount.is_negative or self.amount.currency is not Currency.USD:
                raise ValueError("a spend amount is a non-negative USD amount")
        elif self.target is None or self.amount is not None:
            raise ValueError(f"a {self.kind} action has a target and no amount")
        elif self.kind is ActionKind.NETWORK and not is_network_destination(self.target):
            raise ValueError(
                "a network target is a bare host (name, IPv4 or [IPv6]) with an optional :port; "
                "no scheme, path, query or credentials"
            )
        elif self.kind is ActionKind.WRITE_FILE and ".." in self.target.replace("\\", "/").split(
            "/"
        ):
            raise ValueError("a write_file path may not contain '..' (resolve it first)")
        elif self.kind is ActionKind.WRITE_FILE and any(
            unicodedata.category(ch) in _NO_PATH_CATEGORIES for ch in self.target
        ):
            raise ValueError("a write_file path may not contain control or line-break characters")
        return self

    @property
    def key(self) -> str:
        """Identifies the action for approvals: kind and target (or kind and the exact amount, so
        a spend approval is for one amount: any other amount needs its own approval)."""
        if self.kind is ActionKind.SPEND:
            assert self.amount is not None
            return f"spend:{self.amount.amount.normalize():f}"  # 5 and 5.00 are the same amount
        return f"{self.kind.value}:{self.target}"

    def ledger_fields(self, hasher: LedgerHasher) -> dict[str, str]:
        """What the ledger records about the action: the target, or only its keyed hash if
        sensitive (`hasher` is called only for `HASHED_TARGETS` and for a `redact_target` action,
        PH-KEY R5, NG-18)."""
        if self.kind is ActionKind.SPEND:
            assert self.amount is not None
            return {"action": self.kind.value, "amount": str(self.amount.amount)}
        assert self.target is not None
        if self.kind in HASHED_TARGETS or self.redact_target:
            return {
                "action": self.kind.value,
                "target_hash": hasher(LedgerHashKind.ACTION_TARGET, self.target.encode()),
            }
        return {"action": self.kind.value, "target": self.target}


@lru_cache(maxsize=4096)
@lru_cache(maxsize=4096)
def _compile_glob(pattern: str, path: bool) -> tuple[dict[str, int], int, int, int, int]:
    """Masks for a bit-parallel matcher: (literal masks, `?` mask, star-no-slash mask, star-any
    mask, accepting bit). Consecutive stars collapse into one (`**` anywhere in a run makes it a
    star-any). Position `i` of the pattern is bit `i`."""
    tokens: list[str] = []  # a literal character, "?", "*" (stays in a segment) or "**" (crosses)
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            wide = path and i + 1 < len(pattern) and pattern[i + 1] == "*"
            while i < len(pattern) and pattern[i] == "*":
                wide = wide or (path and i + 1 < len(pattern) and pattern[i + 1] == "*")
                i += 1
            star = "**" if (wide or not path) else "*"
            if tokens and tokens[-1] in ("*", "**"):
                tokens[-1] = "**" if "**" in (tokens[-1], star) else "*"
            else:
                tokens.append(star)
            continue
        tokens.append(ch)
        i += 1
    literals: dict[str, int] = {}
    q = star1 = star2 = 0
    for pos, tok in enumerate(tokens):
        bit = 1 << pos
        if tok == "?":
            q |= bit
        elif tok == "*":
            star1 |= bit
        elif tok == "**":
            star2 |= bit
        else:
            literals[tok] = literals.get(tok, 0) | bit
    return literals, q, star1, star2, 1 << len(tokens)


@lru_cache(maxsize=8192)
def command_words(text: str) -> tuple[str, ...] | None:
    """The words of a command text (POSIX quoting, no expansion of any kind), or None if the text is
    malformed: a quote left open, a lone trailing backslash, or no words at all."""
    try:
        words = shlex.split(text, posix=True)
    except ValueError:
        return None
    return tuple(words) if words else None


def has_control_character(text: str) -> bool:
    """Whether `text` has a character that makes a command compound (CONTROL_CHARACTERS)."""
    return not CONTROL_CHARACTERS.isdisjoint(text)


def is_simple_command(text: str) -> bool:
    """Whether `text` is one command: well formed and without a control character. Anything else is
    compound (policy contract 2)."""
    return not has_control_character(text) and command_words(text) is not None


def command_text(argv: tuple[str, ...] | list[str]) -> str:
    """The command text for an argument list, quoted so that it splits back into the same words."""
    return shlex.join(argv)


def command_pattern_problem(pattern: str, decision: "Decision") -> str | None:
    """Why a `run_command` pattern cannot be a rule (policy contract 2), or None if it can: a
    pattern without control characters must be well formed; an `allow` pattern with a control
    character and a wildcard could never match anything."""
    if has_control_character(pattern):
        if decision is Decision.ALLOW and ("*" in pattern or "?" in pattern):
            return (
                "an allow pattern with a shell control character is an exact command text: "
                "it cannot contain * or ?"
            )
        return None
    words = command_words(pattern)
    if words is None:
        return "the pattern has an unclosed quote, a trailing backslash or no words"
    if len(words) > MAX_PATTERN_WORDS:
        return f"the pattern has more than {MAX_PATTERN_WORDS} words"
    return None


def _blank_split(text: str) -> tuple[str, ...]:
    """Split at spaces and tabs only (quote characters stay in the words)."""
    return tuple(word for word in re.split(r"[ \t]+", text) if word)


def _expand(words: tuple[str, ...]) -> tuple[str, ...]:
    """`words` with every word that holds a space or a tab replaced by its own words, repeatedly:
    the script inside `sh -c 'rm -rf /'` (a quoted word) becomes `rm -rf /` again."""
    out: list[str] = []
    for word in words:
        if " " not in word and "\t" not in word:
            out.append(word)
            continue
        inner = command_words(word)
        if inner is None or inner == (word,):  # malformed, or a word that is its own text (`a\ b`)
            inner = _blank_split(word)
        out.extend(_expand(inner) if inner != (word,) else (word,))
    return tuple(out)


def _segments(text: str) -> list[tuple[str, ...]]:
    """A command text cut at every control character into word lists (a malformed piece is cut at
    spaces and tabs only), each also in its expanded form (`_expand`). Used by refusals, which
    look inside a compound command and inside quoted words."""
    out: list[tuple[str, ...]] = []
    for piece in _CONTROL_SPLIT.split(text):
        words = command_words(piece) or _blank_split(piece)
        if words:
            out.append(words)
            expanded = _expand(words)
            if expanded != words:
                out.append(expanded)
    return out


def _words_match(pattern: tuple[str, ...], words: tuple[str, ...]) -> bool:
    return len(pattern) == len(words) and all(
        glob_match(p, w, ActionKind.RUN_COMMAND) for p, w in zip(pattern, words, strict=True)
    )


@lru_cache(maxsize=16384)
def _match_mask(pattern_word: str, words: tuple[str, ...]) -> int:
    """Bit `j` is set when `pattern_word` matches `words[j]` (the word test)."""
    mask = 0
    for position, word in enumerate(words):
        if glob_match(pattern_word, word, ActionKind.RUN_COMMAND):
            mask |= 1 << position
    return mask


def _has_run(pattern: tuple[str, ...], words: tuple[str, ...]) -> bool:
    """Whether `pattern` matches a run of consecutive `words`, starting anywhere. Shift-and over
    per-word match masks: the cost is the pattern's words times the command's words, however the
    run lines up (no restart at each start position)."""
    starts = len(words) - len(pattern) + 1
    if starts <= 0:
        return False
    alive = (1 << starts) - 1  # bit s: the run may start at word s
    for offset, pattern_word in enumerate(pattern):
        alive &= _match_mask(pattern_word, words) >> offset
        if not alive:
            return False
    return True


def command_matches(pattern: str, text: str, decision: "Decision") -> bool:
    """Whether a `run_command` rule's `pattern` matches the command `text` (policy contract 2, see
    the module docstring and `spec/policy/RUN_COMMAND.md`). `decision` is the rule's decision.

    Strict for permissions, wide for refusals: an `allow` covers the whole parsed command (or the
    exact text of a compound one); a `deny` or `ask` matches any run of consecutive words, in any
    segment of the command, so a refusal never silently gets narrower."""
    if decision is Decision.ALLOW:
        if not is_simple_command(text):
            return pattern == text and "*" not in pattern and "?" not in pattern
        if pattern == "*":
            return True
        pattern_words = command_words(pattern)
        words = command_words(text)
        if has_control_character(pattern) or pattern_words is None or words is None:
            return False
        if pattern_words[-1] == "**":  # zero or more further words
            head = pattern_words[:-1]
            return len(words) >= len(head) and _words_match(head, words[: len(head)])
        return _words_match(pattern_words, words)
    # a refusal (deny or ask)
    if pattern == "*":
        return True
    if has_control_character(
        pattern
    ):  # a shape pattern: a plain character glob over the whole text
        return glob_match(pattern, text, ActionKind.RUN_COMMAND)
    pattern_words = command_words(pattern)
    if pattern_words is None:
        return False
    if len(pattern_words) > 1 and pattern_words[-1] == "**":
        pattern_words = pattern_words[:-1]  # a refusal already covers the words that follow
    return any(_has_run(pattern_words, words) for words in _segments(text))


def glob_match(pattern: str, target: str, kind: ActionKind) -> bool:
    """Whether the whole `target` matches `pattern` (module docstring), in time proportional to the
    target's length: no backtracking, so no pattern can make a decision hang."""
    path = kind is ActionKind.WRITE_FILE
    literals, q, star1, star2, accept = _compile_glob(pattern, path)
    stars = star1 | star2

    def closure(states: int) -> int:
        return states | ((states & stars) << 1)  # a star may match nothing

    current = closure(1)
    for ch in target:
        slash = path and ch == "/"
        step = (current & (literals.get(ch, 0) | (0 if slash else q))) << 1
        stay = current & (star2 | (0 if slash else star1))
        current = closure(step | stay)
        if not current:
            return False
    return bool(current & accept)


class PolicyRule(_Frozen):
    """allow, ask or deny one kind of action whose target matches `target`."""

    action: ActionKind
    target: Annotated[str, StringConstraints(min_length=1, max_length=MAX_PATTERN)] = "*"
    over: Money | None = None
    decision: Decision
    reason: Annotated[str, StringConstraints(max_length=500)] = ""

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.over is not None:
            if self.action is not ActionKind.SPEND:
                raise ValueError("'over' is only for spend rules")
            if self.over.is_negative or self.over.currency is not Currency.USD:
                raise ValueError("'over' is a non-negative USD amount")
        if self.action is ActionKind.SPEND and self.target != "*":
            raise ValueError("a spend rule has no target pattern")
        if self.action is ActionKind.NETWORK and self.target != self.target.lower():
            raise ValueError(
                "a network rule target is lower case (an action's host is, so an upper-case "
                "rule could never match)"
            )
        if (
            self.action is ActionKind.NETWORK
            and not any(ch in self.target for ch in "*?")
            and not is_network_destination(self.target)
        ):
            # a glob-free target that is not a canonical destination (`167772165`, `[0::1]`) could
            # never match an action: refuse it instead of leaving an inert rule
            raise ValueError("a network rule target without wildcards is a canonical destination")
        if self.action is ActionKind.RUN_COMMAND:
            problem = command_pattern_problem(self.target, self.decision)
            if problem is not None:
                raise ValueError(problem)
        return self

    def matches(self, action: Action) -> bool:
        """Whether this rule applies to `action`."""
        if action.kind is not self.action:
            return False
        if action.kind is ActionKind.SPEND:
            assert action.amount is not None
            return self.over is None or action.amount.amount > self.over.amount
        assert action.target is not None
        if self.action is ActionKind.RUN_COMMAND:
            return command_matches(self.target, action.target, self.decision)
        if self.action is ActionKind.NETWORK and self.decision is not Decision.ALLOW:
            found = _HOST_PORT.fullmatch(action.target)
            if found is not None:
                if _HOST_ONLY_RULE.fullmatch(self.target) is None:
                    # a host-only deny or ask rule covers the host on every port; allow never widens
                    rule_host = self.target
                elif found.group("port") is None:
                    # a rule that names a port also covers a target with no port (fail closed:
                    # the emitter picks the implicit port); the rule's host part must match
                    # note: so a deny `*:22` (any `*:<port>`) denies EVERY port-less target
                    rule_host = self.target.rsplit(":", 1)[0]
                else:
                    rule_host = None
                if rule_host is not None and glob_match(
                    rule_host, found.group("host"), self.action
                ):
                    return True
        return glob_match(self.target, action.target, self.action)


class Policy(_Frozen):
    """The rules for one role, and optionally which humans may answer its approval requests."""

    role: Slug
    rules: tuple[PolicyRule, ...] = Field(default=(), max_length=500)
    approvers: tuple[HumanId, ...] = ()

    @model_validator(mode="after")
    def _bounded_command_patterns(self) -> Self:
        """Bounds the time of one decision: a policy's `run_command` patterns have at most
        `MAX_POLICY_PATTERN_WORDS` words in all."""
        total = 0
        for rule in self.rules:
            if rule.action is ActionKind.RUN_COMMAND:
                total += len(command_words(rule.target) or ()) or 1
        if total > MAX_POLICY_PATTERN_WORDS:
            raise ValueError(
                f"the run_command patterns have {total} words in all; at most "
                f"{MAX_POLICY_PATTERN_WORDS}"
            )
        return self

    @field_validator("approvers")
    @classmethod
    def _unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("approvers lists a person twice")
        return value


def _no_default(schema: dict[str, Any]) -> None:
    """The key has no default in the schema: absent is the default, an explicit null an error."""
    schema.pop("default", None)


SOLO_APPROVALS_PER_HOUR_MAX = 30
"""Solo approves per person in any hour (SA-9); configuration may only lower it."""


class PolicySet(_Frozen):
    """Every role's policy. A role with no policy can do nothing (default deny)."""

    policies: tuple[Policy, ...] = ()
    # SA-9 (spec/rbac/solo-approval.md): solo approves per person per hour. Absent means 30; a
    # value can only lower it. Configuration only: no route, variable or licence raises it.
    solo_approvals_per_hour: Annotated[
        int | None,
        WithJsonSchema({"type": "integer", "minimum": 1, "maximum": 30}),
    ] = Field(default=None, json_schema_extra=_no_default)

    @model_validator(mode="before")
    @classmethod
    def _solo_limit(cls, data: object) -> object:
        """The key is absent or an integer from 1 to 30. Everything else is a configuration error
        and is never clamped or converted: 0, 31, `2.5`, `5.0`, `"10"`, a boolean, an explicit
        null or an empty value, a list or a mapping."""
        if isinstance(data, Mapping) and "solo_approvals_per_hour" in data:
            value = data["solo_approvals_per_hour"]
            if type(value) is not int or not 1 <= value <= SOLO_APPROVALS_PER_HOUR_MAX:
                raise ValueError(
                    "solo_approvals_per_hour is an integer from 1 to "
                    f"{SOLO_APPROVALS_PER_HOUR_MAX} (it can only lower the limit); "
                    "leave the key out for the default"
                )
        return data

    @model_serializer(mode="wrap")
    def _omit_unset_limit(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """An unset limit is not written, so a configuration without the key keeps the hash it
        had before the key existed; a set one is part of the hashed configuration."""
        dumped: dict[str, object] = handler(self)
        if self.solo_approvals_per_hour is None:
            dumped.pop("solo_approvals_per_hour", None)
        return dumped

    @property
    def solo_limit(self) -> int:
        """Solo approves one person may make in any hour (SA-9)."""
        if self.solo_approvals_per_hour is None:
            return SOLO_APPROVALS_PER_HOUR_MAX
        return self.solo_approvals_per_hour

    @model_validator(mode="after")
    def _unique_roles(self) -> Self:
        roles = [p.role for p in self.policies]
        if len(set(roles)) != len(roles):
            raise ValueError("a role has more than one policy")
        return self

    def policy_for(self, role: str) -> Policy | None:
        """The role's policy, or None."""
        for policy in self.policies:
            if policy.role == role:
                return policy
        return None

    def problems(self, org: OrgGraph) -> list[str]:
        """Policies for roles the organization doesn't have."""
        known = {r.id for r in org.roles}
        return [f"policy for unknown role '{p.role}'" for p in self.policies if p.role not in known]


class PolicyDecision(_Frozen):
    """The outcome of evaluating one action: the decision, which rule decided and why."""

    decision: Decision
    rule: int | None = None
    reason: str
    # a stable code for the ledger when the verdict has one (NG-10 (c)); None otherwise
    reason_code: str | None = None


# Directories that are control files wherever they sit (a hook or a repository config is code)
_CONTROL_DIRS = frozenset({".git", ".nuramesh"})
# The team folder's own policy, canon, budget and key directories: only at the workspace root,
# where the product loads its configuration from. `src/policy/x.py` is ordinary code.
_ROOT_CONFIG_DIRS = frozenset({"policy", "policies", "canon", "budget", "budgets", "keys"})
_ROOT_CONFIG_SUFFIXES = (".yaml", ".yml", ".json")
_PROTECTED_NAME = re.compile(
    r"^(nuramesh|policy|policies|canon|budget|budgets)([-_.][^/]*)?\.(ya?ml|json|toml)$"
    r"|^.*\.(key|pub)$"
)


def is_canonical_write_target(target: str) -> bool:
    """Whether a `write_file` target has its one spelling: workspace-relative, no leading `/`, no
    backslash, no empty, `.` or `..` segment, no control or line-break character and within
    `MAX_TARGET` (everything `Action` would reject is non-canonical too, so the engine refuses and
    ledgers it instead of raising before any check). A policy matches the text, so
    `./nuramesh.yaml` and `config\\x` must never reach it."""
    return not (
        not target
        or len(target) > MAX_TARGET
        or target.startswith("/")
        or "\\" in target
        or any(p in ("", ".", "..") for p in target.split("/"))
        or any(unicodedata.category(ch) in _NO_PATH_CATEGORIES for ch in target)
    )


def is_protected_path(target: str) -> bool:
    """True for a workspace-relative path an agent's file tool may never write, whatever a policy
    says: the team config (`nuramesh.yaml`), policy, canon and budget files, keys, and
    anything under `.git` or `.nuramesh` at any depth (a hook or a repository config is code), and
    anything under a `policy`, `policies`, `canon`, `budget`, `budgets` or `keys` directory at the
    workspace root, and any `.yaml`, `.yml` or `.json` file at the workspace root (the write
    workspace, not necessarily the team folder the product loads `--config <name>` files from;
    the rule fails closed, so a root `package.json` is denied too). Matching is case-insensitive;
    config file names apply at any depth."""
    parts = [p for p in target.casefold().replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return False
    if any(part in _CONTROL_DIRS for part in parts) or parts[0] in _ROOT_CONFIG_DIRS:
        return True
    if len(parts) == 1 and parts[0].endswith(_ROOT_CONFIG_SUFFIXES):
        return True  # any config-like file at the write workspace root (a superset of the loader's)
    return _PROTECTED_NAME.match(parts[-1]) is not None


NETWORK_APPROVAL_CHAIN = "network_approval_chain"
"""The `reason_code` of a deny written by `check_action(may_ask=False)` (NG-11)."""
NETWORK_APPROVAL_CHAIN_TEXT = "network access refused: this call needs a second approval"
"""Its fixed `reason` (NG-17)."""
WILDCARD_PRIVATE_ADDRESS = "wildcard_private_address"
"""The `reason_code` of the `ask` NG-10 (c) makes of a wildcard allow on a non-public address."""
REDACTED_RULE_REASON = "network access to a redacted target is decided by a rule"
"""The `reason` of a rule's deny or ask for a network action with `redact_target` (NG-18)."""


def _non_public_host(action: Action) -> bool:
    """A network action whose host is an IP address the policy decides (NG-10 (c))."""
    if action.kind is not ActionKind.NETWORK or action.target is None:
        return False
    return classify_address(host_of_target(action.target)) in POLICY_DECIDED


def _exact_allow(rule: PolicyRule) -> bool:
    return rule.decision is Decision.ALLOW and has_exact_host(rule.target)


def _wildcard_reason(action: Action) -> str:
    if action.redact_target:
        return "a wildcard allow rule does not cover a redacted non-public address: a human decides"
    return (
        f"a wildcard allow rule does not cover the non-public address {action.target}: "
        "a human decides"
    )


def evaluate(policy: Policy | None, action: Action) -> PolicyDecision:
    """Apply a role's policy to an action (module docstring). Order of rules doesn't matter."""
    if policy is None:
        return PolicyDecision(decision=Decision.DENY, reason="default deny: the role has no policy")
    if action.kind is ActionKind.WRITE_FILE:
        assert action.target is not None
        if is_protected_path(action.target):
            return PolicyDecision(
                decision=Decision.DENY,
                reason=(
                    "default deny: team config, policy, canon, key, budget and repository "
                    "control files are protected; no policy rule can allow an agent to write them"
                ),
            )
    matching = [(i, r) for i, r in enumerate(policy.rules) if r.matches(action)]
    for wanted in (Decision.DENY, Decision.ASK, Decision.ALLOW):
        for index, rule in matching:
            if rule.decision is wanted:
                if (
                    action.kind is ActionKind.NETWORK
                    and action.redact_target
                    and wanted is not Decision.ALLOW
                ):
                    return PolicyDecision(decision=wanted, rule=index, reason=REDACTED_RULE_REASON)
                if wanted is Decision.ALLOW and _non_public_host(action):
                    # NG-10 (c): an allow covers a non-public address only through a rule that
                    # names the exact host; the first such allow decides, else a human does
                    exact = next((i for i, r in matching if _exact_allow(r)), None)
                    if exact is None:
                        return PolicyDecision(
                            decision=Decision.ASK,
                            rule=index,
                            reason=_wildcard_reason(action),
                            reason_code=WILDCARD_PRIVATE_ADDRESS,
                        )
                    index, rule = exact, policy.rules[exact]
                return PolicyDecision(
                    decision=wanted,
                    rule=index,
                    reason=rule.reason or f"rule {index} says {wanted.value}",
                )
    if action.kind is ActionKind.NETWORK:
        # H-08 (P1b): the one kind whose default is `ask`, not deny. A role that has a policy but
        # no network rule asks a human, who sees the destination; a role with no policy at all
        # was already denied above, and an explicit deny rule won in the loop.
        shown = "a redacted target" if action.redact_target else action.target
        return PolicyDecision(
            decision=Decision.ASK,
            reason=f"no rule for network access to {shown}: a human decides",
        )
    if action.kind is ActionKind.RUN_COMMAND:
        assert action.target is not None
        if not is_simple_command(action.target):
            return PolicyDecision(
                decision=Decision.DENY,
                reason=(
                    "default deny: the command has a shell control character or is malformed; "
                    "only a rule for this exact command text can allow it"
                ),
            )
    return PolicyDecision(decision=Decision.DENY, reason="default deny: no rule matches")


class ApprovalStatus(StrEnum):
    """Where an approval request is."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CONSUMED = "consumed"
    EXPIRED = "expired"  # S4 AD-46: the deadline passed before it was used


# 13 months (ND53 D, ND195 a): how long a decided or consumed approval is kept. The same 396 days
# as a person's reason (T46); a pending approval is never counted. The sweep is in
# `nuramesh.engine.approvals_sweep`.
APPROVAL_RETENTION = timedelta(days=DEFAULT_REASON_DAYS)


class ApprovalError(ValueError):
    """An approval request or answer that isn't allowed."""


class ApprovalOpener(_Frozen):
    """What an approval keeps of the adapter tool-call check that opened it (S4 AD-46): the
    fields its expiry entry copies, and the attempt and denial limit of its guarded run, so an
    answer or an expiry is counted where it is recorded (AD-42)."""

    adapter: Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,32}$")]
    call_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
    tool_set_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    attempt: int = Field(default=0, strict=True, ge=0)
    denial_limit: int = Field(default=3, strict=True, ge=1, le=3)
    # the action as the opening check recorded it (`action` with `target`, or `target_hash` and
    # the hasher's marker fields for a hashed target): what the expiry entry repeats (AD-46)
    action_fields: dict[str, str] = {}


class ApprovalMode(StrEnum):
    """Who could have decided. It grants nothing and is never
    read to allow an action. `escalated` and `break_glass` are reserved words that nothing
    writes yet."""

    SOLO = "solo"  # the approver was the organization's only active decide-holder
    PEER = "peer"  # a person started the run and another person decided
    NO_ORIGINATOR = "no_originator"  # no person is named as the run's starter


class Approval(_Frozen):
    """A request for a human to approve one action by one agent on one task. Single use."""

    id: Slug
    run_id: Slug
    task_id: Slug | None = None
    agent_id: Slug
    action: Action
    asked_because: Annotated[str, StringConstraints(max_length=500)] = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime
    decided_by: HumanId | None = None
    decided_at: datetime | None = None
    # Since T47 the reason is a keyed hash of text kept in the content store (ND25, design decision
    # 2026-10-23). `decision_reason` is the pre-T47 plain text, kept only so old rows still load.
    decision_reason_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None
    decision_reason: Annotated[str, StringConstraints(max_length=1_000)] | None = None
    consumed_at: datetime | None = None
    # An approval opened for an adapter tool call is for one arguments digest (S4 AD-24): the keyed
    # ledger hash of that digest and the hasher's marker fields. Absent for every other check.
    args_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None
    args_hash_marker: dict[str, str] = {}
    # S4 AD-46: an approval opened for an adapter tool call has a deadline on the store clock and
    # keeps its opener's fields; `expired_at` is when its expiry was recorded. AD-24a: the keyed
    # content id of the arguments the approver may reveal (never the arguments themselves).
    deadline: datetime | None = None
    opener: ApprovalOpener | None = None
    expired_at: datetime | None = None
    args_content: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None

    # SA-15: set when decided; None while pending and for rows decided before solo approval.
    approval_mode: ApprovalMode | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if bool(self.args_hash_marker) != (self.args_hash is not None):
            raise ValueError("an arguments hash and its marker fields are recorded together")
        if (self.status is ApprovalStatus.EXPIRED) != (self.expired_at is not None):
            raise ValueError("only an expired approval has an expiry time")
        if self.status is ApprovalStatus.EXPIRED and self.deadline is None:
            raise ValueError("only an approval with a deadline expires")
        # an approval expires pending (nobody answered) or approved and unused (AD-46)
        decided = self.status is not ApprovalStatus.PENDING and not (
            self.status is ApprovalStatus.EXPIRED and self.decided_by is None
        )
        if not decided and self.approval_mode is not None:
            raise ValueError("a pending approval has no approval mode")
        if decided != (self.decided_by is not None and self.decided_at is not None):
            raise ValueError("a decided approval records who and when; a pending one doesn't")
        if decided and not (self.decision_reason_hash or (self.decision_reason or "").strip()):
            raise ValueError("a decided approval records why (a reason hash)")
        if self.decision_reason_hash is not None and self.decision_reason is not None:
            raise ValueError("an approval holds a reason hash or legacy text, not both")
        if (self.status is ApprovalStatus.CONSUMED) != (self.consumed_at is not None):
            raise ValueError("only a consumed approval has a consumption time")
        return self

    def retention_ends(self) -> datetime | None:
        """When this approval may be deleted (R26): the consumption time if it has one, else the
        decision time, in UTC cut to whole seconds, plus `APPROVAL_RETENTION`. None while pending
        (or for another status, or a row with neither time)."""
        if self.status not in (
            ApprovalStatus.APPROVED,
            ApprovalStatus.DENIED,
            ApprovalStatus.CONSUMED,
        ):
            return None  # pending never; expired and withdrawn are kept (R28, R48)
        anchor = self.consumed_at or self.decided_at
        if anchor is None:
            return None
        return anchor.astimezone(UTC).replace(microsecond=0) + APPROVAL_RETENTION

    def retention_over(self, now: datetime) -> bool:
        """Whether `now` (UTC, whole seconds) is at or past `retention_ends`."""
        ends = self.retention_ends()
        return ends is not None and now.astimezone(UTC).replace(microsecond=0) >= ends

    def answers(
        self, agent_id: str, task_id: str | None, action: Action, args_hash: str | None = None
    ) -> bool:
        """Whether this approval is for exactly this agent, task and action and, for a tool call,
        exactly these arguments (S4 AD-24: `args_hash` equal, also when both are absent)."""
        return (
            self.agent_id == agent_id
            and self.task_id == task_id
            and self.action.key == action.key
            and self.args_hash == args_hash
        )


def check_decidable(
    approval: Approval,
    *,
    approver: str,
    allowed: tuple[str, ...] = (),
    reason: str | None = None,
) -> None:
    """The refusals of `decide_approval` that need no reason hash, in the same order and with the
    same messages: pending, a human approver, one of `allowed`, and (if `reason` is given) a
    non-blank reason. Pure; `answer_approval` calls it BEFORE it captures the reason, so a refused
    answer never reaches the key ring (R1.37 K5)."""
    if approval.status is not ApprovalStatus.PENDING:
        raise ApprovalError(f"approval '{approval.id}' is already {approval.status}")
    if approval.decision_reason is not None:
        # A pre-T47 row with plain text and no decision: the answer would carry both forms.
        raise ApprovalError(f"approval '{approval.id}' carries a legacy plain-text reason")
    if not is_human_id(approver):
        raise ApprovalError("only a human (actor 'human:<id>') can answer an approval request")
    if allowed and approver not in allowed:
        raise ApprovalError(f"{approver} may not answer this approval request")
    if reason is not None and not reason.strip():
        raise ApprovalError("an answer needs a reason (who and why are recorded)")


def decide_approval(
    approval: Approval,
    *,
    approver: str,
    approve: bool,
    reason_hash: str,
    at: datetime,
    mode: ApprovalMode,
    allowed: tuple[str, ...] = (),
) -> Approval:
    """A human's answer. Refused unless the request is pending, the approver is a human (and one of
    `allowed` if that is not empty) and a reason is given. The reason is passed as its **keyed
    hash** (the text lives in the content store; `engine.policy.answer_approval` captures it).
    `mode` has no default: every new decision records who could have decided (SA-15)."""
    check_decidable(approval, approver=approver, allowed=allowed)
    if not re.fullmatch(r"[0-9a-f]{64}", reason_hash):
        raise ApprovalError("an answer needs a reason (who and why are recorded)")
    return approval.model_copy(
        update={
            "status": ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED,
            "decided_by": approver,
            "decided_at": at,
            "decision_reason_hash": reason_hash,
            "approval_mode": mode,
        }
    )


def approval_due(approval: Approval, at: datetime) -> bool:
    """Whether a pending or approved, unused approval has reached its deadline (S4 AD-46:
    `now >= deadline` on the store clock). An approval without a deadline never expires."""
    return (
        approval.deadline is not None
        and approval.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)
        and at >= approval.deadline
    )


def expire_approval(approval: Approval, *, at: datetime) -> Approval:
    """Mark an approval whose deadline has passed as expired (S4 AD-46)."""
    if not approval_due(approval, at):
        raise ApprovalError(f"approval '{approval.id}' has not reached a deadline")
    return approval.model_copy(update={"status": ApprovalStatus.EXPIRED, "expired_at": at})


def consume_approval(approval: Approval, *, at: datetime) -> Approval:
    """Use up an approved approval (single use)."""
    if approval.status is not ApprovalStatus.APPROVED:
        raise ApprovalError(f"approval '{approval.id}' is {approval.status}, not approved")
    return approval.model_copy(update={"status": ApprovalStatus.CONSUMED, "consumed_at": at})
