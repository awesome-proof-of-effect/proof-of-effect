"""Parse config files (JSON/JSONC, TOML, YAML, INI) and diff them key by key."""
from __future__ import annotations

import configparser
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - older interpreters
    try:
        import tomli as _toml  # type: ignore[no-redef]
    except ModuleNotFoundError:
        _toml = None  # type: ignore[assignment]

try:
    import yaml as _yaml
except ModuleNotFoundError:  # pragma: no cover - PyYAML is optional
    _yaml = None  # type: ignore[assignment]

KeyPath = Tuple[Any, ...]


class _Missing:
    """Marks a key that is absent on one side of a diff."""

    def __repr__(self) -> str:
        return "<absent>"


MISSING: Any = _Missing()


@dataclass(frozen=True)
class Change:
    path: KeyPath
    before: Any
    after: Any

    @property
    def kind(self) -> str:
        if self.before is MISSING:
            return "added"
        if self.after is MISSING:
            return "removed"
        return "changed"


def yaml_available() -> bool:
    return _yaml is not None


def load_yaml_text(text: str) -> Any:
    if _yaml is None:
        return None
    try:
        return _yaml.safe_load(text)
    except _yaml.YAMLError:
        return None


def _copy_string(text: str, i: int, out: List[str]) -> int:
    """Copy the JSON string literal that starts at text[i]; return the index after it."""
    out.append(text[i])
    i += 1
    while i < len(text):
        ch = text[i]
        out.append(ch)
        i += 1
        if ch == "\\" and i < len(text):
            out.append(text[i])
            i += 1
        elif ch == '"':
            break
    return i


def strip_jsonc(text: str) -> str:
    """Drop // and /* */ comments and trailing commas; string contents are left alone."""
    out: List[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            i = _copy_string(text, i, out)
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    src = "".join(out)
    out = []
    i = 0
    while i < len(src):
        ch = src[i]
        if ch == '"':
            i = _copy_string(src, i, out)
            continue
        if ch == ",":
            j = i + 1
            while j < len(src) and src[j] in " \t\r\n":
                j += 1
            if j < len(src) and src[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


_JSON = re.compile(r"\.(json|jsonc|json5)$|^\.(eslintrc|babelrc|swcrc)$")


def load_structured(path: str, data: Optional[bytes]) -> Tuple[bool, Any]:
    """Parse a config file by its name.

    Returns (parsed_ok, value). A missing file is (True, MISSING); an unsupported or
    unparseable file is (False, None), which callers judge as a whole file.
    """
    if data is None:
        return True, MISSING
    name = os.path.basename(path)
    text = data.decode("utf-8", "replace").lstrip("\ufeff")
    try:
        if _JSON.search(name):
            try:
                return True, json.loads(strip_jsonc(text))
            except ValueError:
                if name == ".eslintrc" and _yaml is not None:
                    return True, _yaml.safe_load(text)
                return False, None
        if name.endswith(".toml"):
            return (True, _toml.loads(text)) if _toml is not None else (False, None)
        if name.endswith((".yml", ".yaml")):
            return (True, _yaml.safe_load(text)) if _yaml is not None else (False, None)
        if name.endswith((".ini", ".cfg")):
            parser = configparser.RawConfigParser(strict=False)
            parser.optionxform = str  # type: ignore[assignment,method-assign]
            parser.read_string(text)
            return True, {section: dict(parser.items(section)) for section in parser.sections()}
    except Exception:  # noqa: BLE001 - any parse failure means "judge the whole file"
        return False, None
    return False, None


def flatten(value: Any, prefix: KeyPath = ()) -> Dict[KeyPath, Any]:
    """Map every leaf to its key path. Lists of scalars are leaves; lists of mappings are indexed."""
    out: Dict[KeyPath, Any] = {}
    if value is MISSING:
        return out
    if isinstance(value, dict):
        if not value:
            out[prefix] = {}
        for key, item in value.items():
            out.update(flatten(item, prefix + (key,)))
        return out
    if isinstance(value, list) and any(isinstance(x, (dict, list)) for x in value):
        for idx, item in enumerate(value):
            out.update(flatten(item, prefix + (idx,)))
        return out
    out[prefix] = value
    return out


def _sort_key(path: KeyPath) -> Tuple:
    return tuple((0, seg) if isinstance(seg, int) and not isinstance(seg, bool) else (1, str(seg))
                 for seg in path)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def diff_flat(before: Dict[KeyPath, Any], after: Dict[KeyPath, Any]) -> List[Change]:
    out: List[Change] = []
    for path in sorted(set(before) | set(after), key=_sort_key):
        b, a = before.get(path, MISSING), after.get(path, MISSING)
        if b is MISSING or a is MISSING or not _same(b, a):
            out.append(Change(path, b, a))
    return out


def diff(before: Any, after: Any) -> List[Change]:
    return diff_flat(flatten(before), flatten(after))


def fmt_path(path: KeyPath) -> str:
    out = ""
    for seg in path:
        if isinstance(seg, bool):  # YAML 1.1 reads the key `on` as True
            out += ("." if out else "") + ("on" if seg else "off")
        elif isinstance(seg, int):
            out += f"[{seg}]"
        else:
            out += ("." if out else "") + str(seg)
    return out or "(root)"


def short(value: Any, limit: int = 60) -> str:
    if value is MISSING:
        return "absent"
    text = json.dumps(value, default=str, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def norm(seg: Any) -> str:
    return re.sub(r"[-_\s]", "", str(seg)).lower()


def last_named(path: KeyPath) -> Optional[str]:
    for seg in reversed(path):
        if isinstance(seg, str):
            return seg
    return None


def match_all(path: KeyPath, changes: Sequence[Change]) -> List[int]:
    """Indexes of the resolved-config changes that correspond to a source key (name-based)."""
    name = last_named(path)
    if name is None:
        return []
    key = norm(name)
    exact = [i for i, c in enumerate(changes) if last_named(c.path) is not None and norm(last_named(c.path)) == key]
    if exact:
        return exact
    return [i for i, c in enumerate(changes) if any(isinstance(s, str) and norm(s) == key for s in c.path)]


def match_change(path: KeyPath, changes: Sequence[Change]) -> Optional[Change]:
    """The first resolved-config change that corresponds to a source key, if any."""
    found = match_all(path, changes)
    return changes[found[0]] if found else None


def match_unknown(path: KeyPath, unknown: Sequence[str]) -> Optional[str]:
    """Return the tool-reported unknown name that matches this source key, if any."""
    name = last_named(path)
    if name is None:
        return None
    for item in unknown:
        if norm(item.split(".")[-1]) == norm(name):
            return item
    return None
