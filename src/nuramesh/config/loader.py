"""Safe parsing of YAML and JSON configuration.

Every failure is a `ConfigError` naming the source (and line where known).

Rules for both formats, checked on the parsed data:

- the top level is a mapping; **keys are text**; duplicate keys are errors (PyYAML and `json`
  silently keep the last one);
- nesting is at most `MAX_DEPTH` levels; files are at most `MAX_BYTES`;
- non-integer numbers become `Decimal` (never float: decision A5 and canonical hashing), finite and
  within `MAX_DECIMAL_EXPONENT`; NaN and Infinity are refused;
- strings must be valid Unicode (no lone surrogates).

YAML specifics (a strict `SafeLoader` subclass):

- **YAML 1.2-style plain scalars**: only `true`/`false` (any case) are booleans, only
  `null`/`~`/empty are null, only plain decimal integers are integers, and decimals need digits
  with a point or exponent (`1.5`, `1e-7`); `017` is the integer 17 (no octal). Everything else
  is text: `NO`, `yes`, `on`, `0x1F`, `1:30`, `.nan`, dates. (YAML 1.1 would turn `country: NO`
  into `False`.)
- Write non-BMP characters as `\U0001f600` in double-quoted YAML: PyYAML reads an escaped
  surrogate pair (`\\ud83d\\ude00`) as two lone halves, which are refused.
- Only the core tags are allowed (`!!str`, `!!int`, `!!float`, `!!bool`, `!!null`, `!!map`,
  `!!seq`); no Python tags, `!!binary`, `!!set`, `!!omap` or custom tags.
- **Anchors and aliases are refused** (they allow exponential "billion laughs" expansion); more
  than one document is refused (PyYAML's single-document load).
"""

import json
import re
from collections.abc import Hashable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import yaml

MAX_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 64
MAX_DECIMAL_EXPONENT = 100

_ALLOWED_TAGS = frozenset(
    f"tag:yaml.org,2002:{name}" for name in ("str", "int", "float", "bool", "null", "map", "seq")
)
_NULL = re.compile(r"^(?:~|null|Null|NULL|)$")
_BOOL = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")
_INT = re.compile(r"^[-+]?[0-9]+$")
_FLOAT = re.compile(r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?$")


class ConfigError(ValueError):
    """A configuration file couldn't be read. The message says where and why."""


def _check_size(text: str, source: str) -> None:
    size = len(text.encode("utf-8", "surrogatepass"))
    if size > MAX_BYTES:
        raise ConfigError(f"{source}: {size} bytes is over the {MAX_BYTES}-byte limit")


def _check_tree(value: Any, path: str = "$", depth: int = 0) -> None:
    """Rules shared by both formats, checked after parsing."""
    if depth > MAX_DEPTH:
        raise ConfigError(f"{path}: nested more than {MAX_DEPTH} levels")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ConfigError(f"{path}: keys must be text, found {key!r}")
            _check_text(key, path)
            _check_tree(item, f"{path}.{key}", depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_tree(item, f"{path}[{index}]", depth + 1)
    elif isinstance(value, str):
        _check_text(value, path)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ConfigError(f"{path}: NaN and Infinity are not allowed")
        exponent = value.as_tuple().exponent
        assert isinstance(exponent, int)
        if abs(exponent) > MAX_DECIMAL_EXPONENT:
            raise ConfigError(f"{path}: exponent beyond ±{MAX_DECIMAL_EXPONENT}")
    elif not (value is None or isinstance(value, bool | int)):
        raise ConfigError(f"{path}: unsupported value of type {type(value).__name__}")


def _check_text(text: str, path: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ConfigError(f"{path}: text contains a lone surrogate") from exc


def _finish(data: Any, source: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        kind = "empty" if data is None else type(data).__name__
        raise ConfigError(f"{source}: the top level must be a mapping, not {kind}")
    try:
        _check_tree(data)
    except ConfigError as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    return data


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ConfigError(f"duplicate key '{key}'")
        out[key] = value
    return out


def _reject_constant(name: str) -> None:
    raise ConfigError(f"'{name}' is not allowed")


_PARSE_FAILURES = (ValueError, TypeError, ArithmeticError, RecursionError, UnicodeError)


def parse_json(text: str, *, source: str = "<json>") -> dict[str, Any]:
    """Parse JSON configuration text into a dict (decimals as `Decimal`)."""
    _check_size(text, source)
    try:
        data = json.loads(
            text,
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicate_pairs,
        )
    except ConfigError as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{source}: line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    except RecursionError as exc:
        raise ConfigError(f"{source}: nested too deeply") from exc
    except _PARSE_FAILURES as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    return _finish(data, source)


def _yaml_module() -> ModuleType:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - exercised in the core-only CI job
        raise ConfigError(
            'YAML support needs the extra: pip install "nuramesh[local]" '
            "(the quickstart install; JSON works without it)"
        ) from exc
    return yaml


_LOADER: "type[yaml.SafeLoader] | None" = None


def _strict_loader() -> "type[yaml.SafeLoader]":
    global _LOADER
    if _LOADER is not None:
        return _LOADER
    pyyaml = _yaml_module()

    class StrictLoader(pyyaml.SafeLoader):  # type: ignore[name-defined,misc]
        """See the module docstring."""

        yaml_implicit_resolvers: dict[str, list[tuple[str, Any]]] = {}

        def __init__(self, stream: Any) -> None:
            super().__init__(stream)
            self._depth = 0

        def compose_node(self, parent: Any, index: Any) -> Any:
            event = self.peek_event()
            line = event.start_mark.line + 1
            if self.check_event(pyyaml.AliasEvent):
                raise ConfigError(f"line {line}: aliases are not allowed")
            if getattr(event, "anchor", None) is not None:
                raise ConfigError(f"line {line}: anchors are not allowed")
            self._depth += 1
            if self._depth > MAX_DEPTH:
                raise ConfigError(f"line {line}: nested more than {MAX_DEPTH} levels")
            try:
                return super().compose_node(parent, index)
            finally:
                self._depth -= 1

        def construct_object(self, node: Any, deep: bool = False) -> Any:
            if node.tag not in _ALLOWED_TAGS:
                line = node.start_mark.line + 1
                raise ConfigError(f"line {line}: tag '{node.tag}' is not allowed")
            return super().construct_object(node, deep=deep)

        def construct_mapping(self, node: Any, deep: bool = False) -> dict[Hashable, Any]:
            seen: set[Any] = set()
            for key_node, _ in node.value:
                line = key_node.start_mark.line + 1
                if key_node.tag != "tag:yaml.org,2002:str":
                    raise ConfigError(f"line {line}: keys must be text")
                key = self.construct_object(key_node, deep=deep)
                if key in seen:
                    raise ConfigError(f"line {line}: duplicate key '{key}'")
                seen.add(key)
            mapping: dict[Hashable, Any] = super().construct_mapping(node, deep=deep)
            return mapping

    def construct_decimal(loader: Any, node: Any) -> Decimal:
        text = str(loader.construct_scalar(node))
        try:
            value = Decimal(text)
        except InvalidOperation as exc:
            raise ConfigError(f"line {node.start_mark.line + 1}: '{text}' isn't a number") from exc
        if not value.is_finite():
            raise ConfigError(f"line {node.start_mark.line + 1}: NaN and Infinity are not allowed")
        return value

    def construct_int(loader: Any, node: Any) -> int:
        text = str(loader.construct_scalar(node))
        if not _INT.match(text):
            raise ConfigError(f"line {node.start_mark.line + 1}: '{text}' isn't a decimal integer")
        return int(text)

    def construct_null(loader: Any, node: Any) -> None:
        text = str(loader.construct_scalar(node))
        if not _NULL.match(text):
            raise ConfigError(f"line {node.start_mark.line + 1}: '{text}' isn't null")

    def construct_bool(loader: Any, node: Any) -> bool:
        text = str(loader.construct_scalar(node))
        if not _BOOL.match(text):
            raise ConfigError(f"line {node.start_mark.line + 1}: '{text}' isn't true or false")
        return text.lower() == "true"

    StrictLoader.add_constructor("tag:yaml.org,2002:float", construct_decimal)
    StrictLoader.add_constructor("tag:yaml.org,2002:int", construct_int)
    StrictLoader.add_constructor("tag:yaml.org,2002:bool", construct_bool)
    StrictLoader.add_constructor("tag:yaml.org,2002:null", construct_null)
    StrictLoader.add_implicit_resolver("tag:yaml.org,2002:bool", _BOOL, list("tTfF"))
    StrictLoader.add_implicit_resolver("tag:yaml.org,2002:null", _NULL, ["~", "n", "N", ""])
    StrictLoader.add_implicit_resolver("tag:yaml.org,2002:int", _INT, list("-+0123456789"))
    StrictLoader.add_implicit_resolver("tag:yaml.org,2002:float", _FLOAT, list("-+.0123456789"))
    _LOADER = StrictLoader
    return StrictLoader


def parse_yaml(text: str, *, source: str = "<yaml>") -> dict[str, Any]:
    """Parse YAML configuration text into a dict. Needs the `nuramesh[yaml]` extra."""
    _check_size(text, source)
    pyyaml = _yaml_module()
    try:
        data = pyyaml.load(text, Loader=_strict_loader())  # noqa: S506 - strict SafeLoader subclass
    except ConfigError as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    except pyyaml.YAMLError as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    except RecursionError as exc:
        raise ConfigError(f"{source}: nested too deeply") from exc
    except _PARSE_FAILURES as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    return _finish(data, source)


def load_config_file(path: str | Path) -> dict[str, Any]:
    """Read a `.yaml`, `.yml` or `.json` file (UTF-8, with or without a byte-order mark)."""
    file = Path(path)
    suffix = file.suffix.lower()
    if suffix not in (".yaml", ".yml", ".json"):
        raise ConfigError(f"{file}: use a .yaml, .yml or .json file")
    try:
        text = file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{file}: {exc}") from exc
    if suffix == ".json":
        return parse_json(text, source=str(file))
    return parse_yaml(text, source=str(file))
