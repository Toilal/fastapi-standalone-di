# CHANGELOG

<!-- version list -->

## v0.5.0 (2026-07-08)

### Features

- Make lazy singletons usable as ASGI route dependencies
  ([#42](https://github.com/Toilal/fastapi-standalone-di/pull/42),
  [`bebe3e8`](https://github.com/Toilal/fastapi-standalone-di/commit/bebe3e8fdc94e1fde75fe073dcfc9179cee95566))


## v0.4.1 (2026-07-08)

### Bug Fixes

- Patch fastapi.params.Depends in place for registrable support (#40)
  ([#41](https://github.com/Toilal/fastapi-standalone-di/pull/41),
  [`08927a5`](https://github.com/Toilal/fastapi-standalone-di/commit/08927a574f58a6b2cee778abfc62f7e90011cb6f))


## v0.4.0 (2026-07-08)

### Features

- AppState-backed application singletons (ASGI + standalone)
  ([#39](https://github.com/Toilal/fastapi-standalone-di/pull/39),
  [`a87d11f`](https://github.com/Toilal/fastapi-standalone-di/commit/a87d11fbf51d2e2555173203a6a11f5dba209840))


## v0.3.0 (2026-07-07)

### Features

- Discover per-package di.py modules to auto-register bindings
  ([#37](https://github.com/Toilal/fastapi-standalone-di/pull/37),
  [`02fc05b`](https://github.com/Toilal/fastapi-standalone-di/commit/02fc05bd52b65a02b8cd3de5c740e80ef7000007))


## v0.2.0 (2026-07-05)

### Features

- Support Python 3.11 ([#35](https://github.com/Toilal/fastapi-standalone-di/pull/35),
  [`f399c8e`](https://github.com/Toilal/fastapi-standalone-di/commit/f399c8edf5f341d18e1af86761a79b86df260c4a))


Changelog entries are generated automatically by
[python-semantic-release](https://python-semantic-release.readthedocs.io/) from
[Conventional Commits](https://www.conventionalcommits.org/) on release.
