# fastapi-standalone-di

[![Latest Version](https://img.shields.io/pypi/v/fastapi-standalone-di.svg)](https://pypi.python.org/pypi/fastapi-standalone-di)
[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/Toilal/fastapi-standalone-di/blob/develop/LICENSE)
[![Build Status](https://img.shields.io/github/actions/workflow/status/Toilal/fastapi-standalone-di/ci.yml?branch=develop)](https://github.com/Toilal/fastapi-standalone-di/actions/workflows/ci.yml)
[![Codecov](https://img.shields.io/codecov/c/github/Toilal/fastapi-standalone-di)](https://codecov.io/gh/Toilal/fastapi-standalone-di)
[![semantic-release](https://img.shields.io/badge/%20%20%F0%9F%93%A6%F0%9F%9A%80-semantic--release-e10079.svg)](https://github.com/relekang/python-semantic-release)

Use [FastAPI](https://fastapi.tiangolo.com/)'s dependency injection **outside of any web/ASGI context**.

FastAPI ships a powerful dependency injection system — `Depends`, sub-dependencies,
`yield` teardown, per-resolution caching — but it is tightly coupled to the
request/response cycle. `fastapi-standalone-di` reuses that exact machinery so you
can resolve and invoke your dependencies from plain Python: CLI scripts, workers,
cron jobs, tests — no HTTP server required.

## Install

```bash
pip install fastapi-standalone-di
# or
uv add fastapi-standalone-di
```

## Usage

Resolve any callable that uses `Depends()`, exactly as FastAPI would:

```python
import asyncio

from fastapi import Depends

from fastapi_standalone_di import FastAPIContainer


def get_settings() -> dict[str, str]:
    return {"db_url": "postgres://localhost/app"}


class Database:
    def __init__(self, settings: dict[str, str] = Depends(get_settings)) -> None:
        self.url = settings["db_url"]


async def main() -> None:
    async with FastAPIContainer() as container:
        db = await container.get(Database)
        print(db.url)  # postgres://localhost/app


asyncio.run(main())
```

Sub-dependencies, `yield` teardown, caching, scopes, dependency overrides,
registrable interfaces, per-package binding discovery, shared app state,
application-lifetime singletons, standalone `Request`/`Response` stubs, and
query/path/header/cookie parameters are all covered in the documentation.

## Documentation

Full documentation is available at [toilal.github.io/fastapi-standalone-di](https://toilal.github.io/fastapi-standalone-di/).
The in-development docs (built from `develop`) are previewed at
[toilal.github.io/fastapi-standalone-di/dev/](https://toilal.github.io/fastapi-standalone-di/dev/).

## Requirements

- Python ≥ 3.11
- FastAPI ≥ 0.61

## License

[MIT](./LICENSE) © Rémi Alvergnat
