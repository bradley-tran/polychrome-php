<?php
declare(strict_types=1);

require __DIR__ . '/discovery.php';

$root = getenv('POLYCHROME_ROOT');
if (!$root) throw new RuntimeException('POLYCHROME_ROOT is required for automatic preloading');
$started = hrtime(true);
$discovery = \Polychrome\discover($root);
foreach ($discovery['files'] as $file) {
    if (!opcache_compile_file($file)) {
        throw new RuntimeException("Could not preload $file");
    }
}
error_log(sprintf(
    'polychrome: preload compiled=%d excluded=%d elapsed_ms=%.1f root=%s; PHP reports unresolved classes during linking',
    count($discovery['files']), $discovery['skipped'], (hrtime(true) - $started) / 1e6, $root
));
unset($root, $started, $discovery, $file);
