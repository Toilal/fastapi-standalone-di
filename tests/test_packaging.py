"""PEP 561: the package must ship a py.typed marker so consumers' type
checkers honour the inline types instead of treating the package as Any.
"""

from importlib.resources import files


def test_py_typed_marker_ships_with_package() -> None:
    marker = files("fastapi_standalone_di") / "py.typed"
    assert marker.is_file()
