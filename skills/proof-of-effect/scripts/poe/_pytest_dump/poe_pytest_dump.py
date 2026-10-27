"""pytest plugin used by proof-of-effect.

Dumps the resolved ini options (and the ones pytest does not know) to $POE_PYTEST_DUMP,
then stops before collection, so no test is imported or run.
"""
import json
import os
import pathlib

import pytest


def _plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, pathlib.PurePath):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return str(value)


def pytest_sessionstart(session):
    config = session.config
    names = sorted(getattr(config._parser, "_inidict", {}))
    ini = {}
    for name in names:
        try:
            ini[name] = _plain(config.getini(name))
        except Exception as exc:  # noqa: BLE001 - report, never crash the dump
            ini[name] = f"<error: {exc}>"
    try:
        unknown = sorted(config._get_unknown_ini_keys())
    except Exception:  # noqa: BLE001 - private API; fall back to a plain comparison
        unknown = sorted(k for k in getattr(config, "inicfg", {}) if k not in names)
    inipath = getattr(config, "inipath", None)
    payload = {"inifile": str(inipath) if inipath else None, "ini": ini, "unknown": unknown}
    with open(os.environ["POE_PYTEST_DUMP"], "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    pytest.exit("proof-of-effect: config dumped", returncode=0)
