# Polychrome PHP

A PHP FastCGI runtime inspired by Android's Zygote architecture. A single-threaded
Rust manager initializes PHP through a custom C SAPI, preloads code, and forks
disposable children. Each child handles one request, completes PHP request
shutdown, and exits. The parent never handles application requests.

Version 0.1 targets **Linux, PHP 8.5 NTS, and Drupal 11**. It is an alpha for
applications that cannot use persistent application workers. Performance is
measured against PHP-FPM; faster execution is not assumed. See
[the benchmark guide](benchmarks/README.md).

## Build from source

Install Rust through rustup and PHP's development dependencies. On Debian/Ubuntu:

```sh
sudo apt-get install build-essential autoconf bison re2c pkg-config curl xz-utils \
  libxml2-dev libsqlite3-dev libssl-dev libcurl4-openssl-dev libonig-dev \
  libicu-dev libzip-dev libpng-dev libjpeg-dev libwebp-dev zlib1g-dev
./scripts/build.sh
```

The build verifies the pinned PHP 8.5.10 archive and uses Rust 1.90.0 and the
Cargo lockfile. It produces `dist/bin/polychrome`, `dist/bin/php`, and
`dist/sbin/php-fpm` from the same PHP source. Logs stay under `.build/`.
Set `JOBS`, `POLYCHROME_BUILD_DIR`, or `POLYCHROME_PREFIX` to override defaults.
Keep the installed `share/polychrome` directory alongside `bin`.

```sh
dist/bin/polychrome --root /path/to/drupal
```

Point an existing web server at `127.0.0.1:9000`. Polychrome needs no configuration
file; the web server still supplies routing, static files, TLS, and FastCGI
parameters. Supply an absolute `SCRIPT_FILENAME`, set `DOCUMENT_ROOT`, and disable
upstream FastCGI connection reuse. An [Nginx example](examples/drupal/nginx.conf)
is included. FastCGI listeners should be reachable only by trusted web servers.

## Runtime options

```text
polychrome [--root DIR] [--listen ENDPOINT] [--workers N]
           [--request-timeout SECONDS] [--no-preload]
           [-c PHP_INI] [-d KEY=VALUE]
```

| Option | Default / behavior |
| --- | --- |
| `--root` | Current directory; one application per manager |
| `--listen` | `127.0.0.1:9000`; also `unix:/absolute/path.sock` |
| `--workers` | Available CPU parallelism; fixed maximum live children |
| `--request-timeout` | 30 seconds from connection acceptance, including shutdown and post-response work |
| `--no-preload` | Disable automatic preloading; retain request isolation |
| `-c`, `-d` | PHP INI loading and repeatable command-line overrides |
| `--help`, `--version` | Usage and Polychrome/embedded PHP versions |

The runtime stays in the foreground under the invoking user's identity. Unix
sockets have mode `0660`; existing sockets are never removed to take over their
address. Diagnostics go to stderr, with a `ready` line for the initialized pool.
SIGTERM/SIGINT drain requests for up to 30 seconds; a second signal ends the
grace period. Timed-out children are killed and reaped. Normal operation replaces
exited children from the original zygote.

Defaults enable OPcache, disable JIT, and set 256 MB each for the PHP memory limit
and OPcache capacity. INI files can override these. Automatic preloading requires
OPcache. Polychrome owns `opcache.preload` and `opcache.preload_user`; conflicting
settings fail startup. Per-directory `.user.ini` and FastCGI
`PHP_VALUE`/`PHP_ADMIN_VALUE` overrides are not implemented in v0.1.

## Preloading and compatibility

The helper reads Composer JSON metadata, scans production classmap/PSR autoload
paths, and adds Drupal's `core/lib`. It honors classmap exclusions and omits
development packages, test/fixture paths, Drupal module/theme trees, and
`index.php`. It never loads Composer's autoloader, executes application files,
or bootstraps Drupal in the parent. Symlinked directories are not traversed;
targets outside the application root are skipped.

PHP compiles candidates with `opcache_compile_file()`, then links preloadable
symbols. Optional dependencies can cause warnings about unlinked classes, which
remain subject to ordinary request-time loading. Compilation or startup failures
prevent serving. Preloading makes symbols globally available and can affect
`class_exists()`/`function_exists()` checks; use `--no-preload` when diagnosing
compatibility. ([PHP preload semantics](https://www.php.net/manual/en/opcache.preloading.php))

**Restart after deploying code or changing preloaded files.** Drupal bootstrap,
database connections, module loading, and request handling happen in each child.
PHP globals and process mutations do not flow back into the parent. External
state such as databases, session files, and shared OPcache persists as intended.
Extensions that start threads in the zygote are rejected.

Children also share code cached by earlier requests, including with `--no-preload`.
See the [OPcache investigation](docs/opcache-mutable-zygote.md) for verified
behavior, limits, and a reproducible probe.

## Docker and Drupal

```sh
docker build -t polychrome:local .
docker compose -p polychrome-alpha -f examples/drupal/compose.yaml build
docker compose -p polychrome-alpha -f examples/drupal/compose.yaml up -d --wait
./scripts/test-drupal.sh polychrome-alpha
```

This builds the locked Drupal fixture, installs the standard profile and Drupal's
Article content type recipe, and tests
login, content creation/editing, image upload, anonymous rendering, logout, and
cache rebuilds. Open <http://127.0.0.1:8080>; the fixture login is `admin` /
`polychrome-example`. The test script reinstalls this example database.

The application and Nginx run as unprivileged users; MariaDB runs as `mysql`.
The example has local-only credentials and a benchmark endpoint. Public files
and the database persist in Compose volumes. Remove the fixture with:

```sh
docker compose -p polychrome-alpha -f examples/drupal/compose.yaml down -v
```

## Development checks

```sh
cargo fmt --all --check
cargo test --locked
cargo clippy --all-targets --locked -- -D warnings
dist/bin/php tests/preload.php
python3 tests/integration.py
```

For a separate ASan/UBSan build:

```sh
ASAN_OPTIONS=detect_leaks=0 SANITIZE=1 \
  POLYCHROME_BUILD_DIR="$PWD/.build/sanitized" \
  POLYCHROME_PREFIX="$PWD/.build/sanitized/dist" ./scripts/build.sh
ASAN_OPTIONS=detect_leaks=0:abort_on_error=1 \
  UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 USE_ZEND_ALLOC=0 \
  POLYCHROME_BIN="$PWD/.build/sanitized/dist/bin/polychrome" \
  python3 tests/integration.py
```

Leak detection is disabled because disposable children intentionally use `_exit`
after request shutdown. Address and undefined-behavior checks stay enabled.
CI covers native checks, sanitizers, and the Drupal Compose fixture.

Multiple application pools, live reload, persistent FastCGI connections,
multiplexing, application-state snapshots, Drupal module preloading, other PHP
minors/operating systems, and prebuilt packages are outside this alpha.
