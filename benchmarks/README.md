# Benchmarks

The [initial results](RESULTS.md) include raw samples for trivial PHP
and two Drupal workloads, with measurement conditions and reproduction commands.

Build Polychrome, then run the stdlib-only harness:

```sh
python3 benchmarks/run.py --requests 1000 --repeat 3 --workers 2 \
  --concurrency 1,2,4 --output benchmarks/results/trivial.json
```

It runs six configurations: Polychrome, ordinary FPM, and FPM with
`pm.max_requests=1`, each with preloading enabled and disabled. They use the same
PHP build, worker count, OPcache capacity, memory limit, JIT setting, and preload
helper. All use fresh FastCGI connections and warm the workload before timing.
FPM requires no existing service or system configuration.

Results include throughput, p50/p95/p99 latency, process-tree CPU time, and peak
summed proportional set size (PSS). PSS avoids counting shared OPcache pages as
private memory in every child. Sampling uses Linux `/proc`; CPU includes reaped
children. Very short runs have coarse CPU accounting and can miss memory peaks.
Use longer runs on an otherwise idle machine for comparisons. Performance is
reported without a pass/fail threshold.

For a local installed Drupal site, copy the fixture benchmark endpoint into its
document root, then run:

```sh
python3 benchmarks/run.py --root /path/to/drupal \
  --script web/polychrome-benchmark.php --query mode=bootstrap \
  --output benchmarks/results/drupal-bootstrap.json
python3 benchmarks/run.py --root /path/to/drupal --script web/index.php \
  --uri /user/login --output benchmarks/results/drupal-login.json
python3 benchmarks/run.py --root /path/to/drupal --script web/index.php \
  --uri /node/1 --output benchmarks/results/drupal-anonymous.json
```

Pass `--cookie 'SESS…=…'` for an authenticated workload; the cookie is not saved.
Run cached anonymous and uncached/authenticated pages separately. The site's
database must be reachable by the host CLI/FastCGI processes. The Compose fixture
also includes this endpoint for HTTP load tools.

Use `--prefix` for a non-default source install. Keep PHP, Drupal, dependency
versions, cache state, and request concurrency with any published results.
