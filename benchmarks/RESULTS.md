# Initial benchmark results

These are diagnostic measurements from 2026-09-11 on an AMD Ryzen 7 5700U
(16 logical CPUs), Linux x86_64, PHP 8.5.10 NTS, and Drupal 11.4.6 with SQLite
3.53.4. Background builds were active. Short runs and shared machine load limit
comparability; use the [harness](README.md) for longer, controlled measurements.
The MariaDB Compose fixture was validated separately for functional behavior.

Every configuration used two workers, the same PHP source build, 256 MiB OPcache,
256 MiB request memory limit, JIT disabled, and the same automatic preload helper
when preloading was enabled. Each process was warmed with 20 requests before
measurement. Tables show the median of three runs at concurrency 2, with 300
requests per trivial run and 100 per Drupal run. The p95 column is the median of
per-run p95 values, not a percentile pooled across runs. PSS sums the live process
tree; CPU includes reaped children and excludes the client and external services.

Persistent FPM had higher throughput in these samples. Polychrome had higher
throughput than FPM with one request per child. Automatic preloading increased
Drupal memory use and did not improve its throughput here. These observations
are specific to this setup and are not performance release gates.

## Trivial PHP

| Runtime | Preload | Requests/s | p95 (ms) | Peak PSS (MiB) | CPU s / 1,000 requests |
| --- | --- | ---: | ---: | ---: | ---: |
| Polychrome | Off | 662.8 | 3.64 | 23.5 | 2.77 |
| Polychrome | On | 710.7 | 3.59 | 24.1 | 2.60 |
| FPM | Off | 6,737.4 | 0.45 | 23.6 | 0.10 |
| FPM | On | 5,950.4 | 0.41 | 23.9 | 0.13 |
| FPM (`pm.max_requests=1`) | Off | 191.1 | 12.41 | 29.9 | 9.90 |
| FPM (`pm.max_requests=1`) | On | 193.5 | 11.98 | 30.9 | 10.10 |

[Raw samples](samples/trivial.json), including concurrency 1 and all latency percentiles.

## Drupal login page

| Runtime | Preload | Requests/s | p95 (ms) | Peak PSS (MiB) | CPU s / 1,000 requests |
| --- | --- | ---: | ---: | ---: | ---: |
| Polychrome | Off | 159.9 | 14.21 | 32.5 | 11.80 |
| Polychrome | On | 138.2 | 17.02 | 161.5 | 13.30 |
| FPM | Off | 391.0 | 6.27 | 34.3 | 4.80 |
| FPM | On | 398.4 | 6.23 | 158.5 | 4.40 |
| FPM (`pm.max_requests=1`) | Off | 115.7 | 19.73 | 35.0 | 16.90 |
| FPM (`pm.max_requests=1`) | On | 76.3 | 30.24 | 173.2 | 25.20 |

[Raw samples](samples/drupal-login.json), including concurrency 1 and all latency percentiles.

## Drupal kernel bootstrap

| Runtime | Preload | Requests/s | p95 (ms) | Peak PSS (MiB) | CPU s / 1,000 requests |
| --- | --- | ---: | ---: | ---: | ---: |
| Polychrome | Off | 225.3 | 10.85 | 31.8 | 8.30 |
| Polychrome | On | 194.3 | 11.64 | 150.3 | 9.70 |
| FPM | Off | 592.4 | 4.20 | 33.5 | 2.80 |
| FPM | On | 503.0 | 5.38 | 158.2 | 3.20 |
| FPM (`pm.max_requests=1`) | Off | 147.6 | 15.49 | 35.3 | 13.10 |
| FPM (`pm.max_requests=1`) | On | 85.6 | 26.19 | 188.9 | 22.30 |

[Raw samples](samples/drupal-bootstrap.json), including concurrency 1 and all latency percentiles.

## Reproduction commands

Build the pinned runtime with `./scripts/build.sh`. For Drupal, install the locked
application dependencies from `examples/drupal/app` with `dist/bin/php` on PATH,
then create a local benchmark site. These commands reinstall the local SQLite
benchmark database; the Docker example has its own database.

```sh
export PATH="$PWD/dist/bin:$PATH"
(cd examples/drupal/app && composer install --no-dev --prefer-dist)
php -d memory_limit=512M examples/drupal/app/vendor/drush/drush/drush.php \
  site:install standard --root="$PWD/examples/drupal/app/web" \
  --db-url="sqlite://localhost/$PWD/.build/drupal.sqlite" \
  --account-name=admin --account-pass=polychrome-example \
  --site-name='Polychrome integration' --yes
cp examples/drupal/benchmark.php examples/drupal/app/web/polychrome-benchmark.php
python3 benchmarks/run.py --requests 300 --repeat 3 --workers 2 \
  --concurrency 1,2 --output benchmarks/results/trivial.json
python3 benchmarks/run.py --root examples/drupal/app --script web/index.php \
  --uri /user/login --requests 100 --repeat 3 --workers 2 --concurrency 1,2 \
  --output benchmarks/results/drupal-login.json
python3 benchmarks/run.py --root examples/drupal/app \
  --script web/polychrome-benchmark.php --query mode=bootstrap \
  --requests 100 --repeat 3 --workers 2 --concurrency 1,2 \
  --output benchmarks/results/drupal-bootstrap.json
```

The included JSON preserves the measured values and normalizes only the checkout
root paths. Composer 2.9.5 was used to install the locked application. The browser
suite covers authenticated Drupal behavior; these benchmark samples do not measure
an authenticated page or a cached anonymous content page.
