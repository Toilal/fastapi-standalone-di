"""Tests for fastapi_standalone_di.discovery."""

import importlib
import logging
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from fastapi_standalone_di import register_bindings

_ROOT = "_disc_pkg"


@pytest.fixture
def make_package(tmp_path: Path) -> Iterator[Callable[[dict[str, str]], str]]:
    """Materialise a package tree from ``{relative/path.py: source}`` under a
    unique root package, put it on ``sys.path``, and clean everything up.

    Every subpackage's ``register()`` appends its own name to ``ROOT._calls``,
    so a test can assert which modules were triggered and in what order.
    """

    def build(tree: dict[str, str]) -> str:
        root_dir = tmp_path / _ROOT
        root_dir.mkdir(exist_ok=True)
        (root_dir / "__init__.py").write_text("_calls: list[str] = []\n")
        for rel, source in tree.items():
            target = root_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            for parent in target.parents:
                init = parent / "__init__.py"
                if parent == tmp_path:
                    break
                if not init.exists():
                    init.write_text("")
            target.write_text(source)
        sys.path.insert(0, str(tmp_path))
        return _ROOT

    yield build

    sys.path[:] = [p for p in sys.path if p != str(tmp_path)]
    for name in list(sys.modules):
        if name == _ROOT or name.startswith(f"{_ROOT}."):
            del sys.modules[name]


def _di(feature: str, *, module: str = "di") -> dict[str, str]:
    """A subpackage whose binding module records its own name when registered."""
    return {
        f"{feature}/__init__.py": "",
        f"{feature}/{module.replace('.', '/')}.py": (
            f"from {_ROOT} import _calls\n\n\ndef register() -> None:\n"
            f"    _calls.append({feature!r})\n"
        ),
    }


def _calls(root: str) -> list[str]:
    return list(importlib.import_module(root)._calls)


class TestRegisterBindings:
    def test_calls_register_in_each_subpackage(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package({**_di("orders"), **_di("users")})
        register_bindings(root)
        assert sorted(_calls(root)) == ["orders", "users"]

    def test_multiple_packages(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "group_a/__init__.py": "",
                **{f"group_a/{k}": v for k, v in _di("orders").items()},
                "group_b/__init__.py": "",
                **{f"group_b/{k}": v for k, v in _di("users").items()},
            }
        )
        register_bindings(f"{root}.group_a", f"{root}.group_b")
        assert sorted(_calls(root)) == ["orders", "users"]

    def test_relative_package_is_anchored_to_caller(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                **_di("orders"),
                "assemble.py": (
                    "from fastapi_standalone_di import register_bindings\n\n\n"
                    "def run() -> None:\n"
                    "    register_bindings('.')\n"
                ),
            }
        )
        importlib.import_module(f"{root}.assemble").run()
        assert _calls(root) == ["orders"]

    def test_relative_subpackage_name(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {
                "features/__init__.py": "",
                **{f"features/{k}": v for k, v in _di("orders").items()},
                "assemble.py": (
                    "from fastapi_standalone_di import register_bindings\n\n\n"
                    "def run() -> None:\n"
                    "    register_bindings('.features')\n"
                ),
            }
        )
        importlib.import_module(f"{root}.assemble").run()
        assert _calls(root) == ["orders"]

    def test_no_packages_is_noop(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(_di("orders"))
        register_bindings()
        assert _calls(root) == []

    def test_skips_subpackage_without_binding_module(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package({**_di("orders"), "plain/__init__.py": ""})
        register_bindings(root)
        assert _calls(root) == ["orders"]

    def test_ignores_top_level_modules(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package({**_di("orders"), "helpers.py": "x = 1\n"})
        register_bindings(root)
        assert _calls(root) == ["orders"]

    def test_accepts_module_object(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(_di("orders"))
        register_bindings(importlib.import_module(root))
        assert _calls(root) == ["orders"]

    def test_dotted_module_path(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(_di("orders", module="api.di"))
        register_bindings(root, module="api.di")
        assert _calls(root) == ["orders"]

    def test_custom_attr(self, make_package: Callable[[dict[str, str]], str]) -> None:
        root = make_package(
            {
                "orders/__init__.py": "",
                "orders/di.py": (
                    f"from {_ROOT} import _calls\n\n\ndef wire() -> None:\n"
                    "    _calls.append('orders')\n"
                ),
            }
        )
        register_bindings(root, attr="wire")
        assert _calls(root) == ["orders"]

    def test_warns_when_callable_missing(
        self,
        make_package: Callable[[dict[str, str]], str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = make_package(
            {"orders/__init__.py": "", "orders/di.py": "register = 42\n"}
        )
        with caplog.at_level(logging.WARNING):
            register_bindings(root)
        assert "no callable" in caplog.text
        assert f"{root}.orders.di" in caplog.text

    def test_warn_missing_false_stays_silent(
        self,
        make_package: Callable[[dict[str, str]], str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = make_package(
            {"orders/__init__.py": "", "orders/di.py": "register = 42\n"}
        )
        with caplog.at_level(logging.WARNING):
            register_bindings(root, warn_missing=False)
        assert caplog.text == ""

    def test_non_recursive_ignores_nested_subpackages(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {**_di("orders"), **{f"orders/{k}": v for k, v in _di("nested").items()}}
        )
        register_bindings(root)
        assert _calls(root) == ["orders"]

    def test_recursive_walks_nested_subpackages(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {**_di("orders"), **{f"orders/{k}": v for k, v in _di("nested").items()}}
        )
        register_bindings(root, recursive=True)
        assert sorted(_calls(root)) == ["nested", "orders"]

    def test_import_error_in_binding_module_propagates(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package(
            {"orders/__init__.py": "", "orders/di.py": "raise ValueError('boom')\n"}
        )
        with pytest.raises(ValueError, match="boom"):
            register_bindings(root)

    def test_non_package_raises_value_error(
        self, make_package: Callable[[dict[str, str]], str]
    ) -> None:
        root = make_package({"helpers.py": "x = 1\n"})
        with pytest.raises(ValueError, match="not a package"):
            register_bindings(f"{root}.helpers")
