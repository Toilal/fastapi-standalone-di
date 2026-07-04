"""PEP 561: the package must ship a py.typed marker so consumers' type
checkers honour the inline types instead of treating the package as Any.
"""

import re
from importlib.resources import files

import fastapi_standalone_di


def test_py_typed_marker_ships_with_package() -> None:
    marker = files("fastapi_standalone_di") / "py.typed"
    assert marker.is_file()


def test_all_names_are_importable() -> None:
    for name in fastapi_standalone_di.__all__:
        assert hasattr(fastapi_standalone_di, name), name


def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", fastapi_standalone_di.__version__)
