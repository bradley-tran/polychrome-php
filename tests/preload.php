<?php
declare(strict_types=1);
require __DIR__ . '/../preload/discovery.php';

$root = sys_get_temp_dir() . '/polychrome-discovery-' . bin2hex(random_bytes(6));
mkdir($root, 0700);
function fixture(string $name, string $contents): void {
    global $root;
    $path = "$root/$name";
    if (!is_dir(dirname($path))) mkdir(dirname($path), 0700, true);
    file_put_contents($path, $contents);
}
try {
    fixture('composer.json', json_encode(['autoload' => [
        'psr-4' => ['App\\' => ['src/', 'src/']],
        'classmap' => ['src/Allowed.php', 'web/modules/custom/example/src/Module.php'],
        'exclude-from-classmap' => ['/src/Excluded/', '/src/**/Ignored.php'],
    ]], JSON_THROW_ON_ERROR));
    fixture('vendor/composer/installed.json', json_encode([
        'packages' => [
            ['name' => 'demo/library', 'install-path' => '../demo/library', 'autoload' => ['psr-4' => ['Demo\\' => 'src/']]],
            ['name' => 'demo/dev', 'install-path' => '../demo/dev', 'autoload' => ['classmap' => ['src/']]],
            ['name' => 'drupal/example', 'type' => 'drupal-module', 'install-path' => '../../web/modules/contrib/example', 'autoload' => ['classmap' => ['src/']]],
        ], 'dev-package-names' => ['demo/dev'],
    ], JSON_THROW_ON_ERROR));
    $candidates = [
        'src/Allowed.php', 'src/Excluded/NotLoaded.php', 'src/Nested/Ignored.php',
        'src/tests/Test.php', 'src/fixtures/Fixture.php', 'src/index.php',
        'vendor/demo/library/src/Library.php', 'vendor/demo/dev/src/Dev.php',
        'web/core/lib/Drupal/Core/Example.php', 'web/modules/custom/example/src/Module.php',
        'web/modules/contrib/example/src/Contrib.php',
    ];
    foreach ($candidates as $file) fixture($file, '<?php throw new Exception("discovery must not execute candidate files");');
    fixture('vendor/autoload.php', '<?php throw new Exception("must not bootstrap Composer");');
    symlink("$root/src", "$root/src/loop");
    $expected = ["$root/src/Allowed.php", "$root/vendor/demo/library/src/Library.php", "$root/web/core/lib/Drupal/Core/Example.php"];
    $first = \Polychrome\discover($root);
    if ($first['files'] !== $expected) throw new RuntimeException('Wrong discovery results: ' . json_encode($first));
    if ($first !== \Polychrome\discover($root)) throw new RuntimeException('Discovery is not deterministic');
    echo "Preload discovery: exclusions, production dependencies, Drupal core, deduplication, symlinks and side effects passed.\n";
} finally {
    $entries = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS), RecursiveIteratorIterator::CHILD_FIRST);
    foreach ($entries as $entry) {
        if ($entry->isDir() && !$entry->isLink()) rmdir($entry->getPathname());
        else unlink($entry->getPathname());
    }
    rmdir($root);
}
