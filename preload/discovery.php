<?php
declare(strict_types=1);

namespace Polychrome;

/** Read JSON metadata only: never execute Composer's autoloader or plugins. */
function readJson(string $path): array
{
    $data = json_decode(file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
    if (!is_array($data)) {
        throw new \RuntimeException("Expected a JSON object in $path");
    }
    return $data;
}

function within(string $path, string $directory): bool
{
    return $path === $directory || str_starts_with($path, rtrim($directory, '/') . '/');
}

/** Composer's exclude-from-classmap patterns: * excludes slashes, ** does not. */
function exclusionPattern(string $pattern, string $base): string
{
    $pattern = ltrim(str_replace('\\', '/', $pattern), '/');
    $quoted = preg_quote($pattern, '~');
    $quoted = str_replace(['\*\*', '\*'], ["\x01", '[^/]*'], $quoted);
    $quoted = str_replace("\x01", '.*', $quoted);
    return '~^' . preg_quote(rtrim($base, '/') . '/', '~') . $quoted . '.*$~';
}

/** @return array{files: list<string>, skipped: int} */
function discover(string $root): array
{
    $root = realpath($root) ?: throw new \RuntimeException('Application root does not exist');
    $composer = is_file("$root/composer.json") ? readJson("$root/composer.json") : [];
    $vendorSetting = $composer['config']['vendor-dir'] ?? 'vendor';
    $vendor = realpath(str_starts_with($vendorSetting, '/') ? $vendorSetting : "$root/$vendorSetting");
    $packages = [];
    if ($vendor && is_file("$vendor/composer/installed.json")) {
        $installed = readJson("$vendor/composer/installed.json");
        $development = array_fill_keys($installed['dev-package-names'] ?? [], true);
        foreach ($installed['packages'] ?? $installed as $package) {
            if (isset($development[$package['name']]) || str_starts_with($package['type'] ?? '', 'drupal-')) {
                continue;
            }
            $installPath = $package['install-path'] ?? "../{$package['name']}";
            $base = realpath(str_starts_with($installPath, '/') ? $installPath : "$vendor/composer/$installPath");
            if ($base) $packages[] = [$base, $package['autoload'] ?? []];
        }
    }
    $packages[] = [$root, $composer['autoload'] ?? []];
    // Drupal registers core and module namespaces during bootstrap. Only core
    // is a v0.1 preload target; enabled-module discovery requires bootstrapping.
    foreach (["$root/web/core/lib", "$root/docroot/core/lib", "$root/core/lib"] as $core) {
        if (is_dir($core)) $packages[] = [$core, ['classmap' => ['.']]];
    }
    $files = [];
    $skipped = 0;
    foreach ($packages as [$base, $autoload]) {
        $paths = $autoload['classmap'] ?? [];
        foreach (['psr-0', 'psr-4'] as $standard) {
            foreach ($autoload[$standard] ?? [] as $directories) {
                array_push($paths, ...(array) $directories);
            }
        }
        $exclusions = array_map(fn ($pattern) => exclusionPattern($pattern, $base), $autoload['exclude-from-classmap'] ?? []);
        foreach ($paths as $relative) {
            $path = str_starts_with($relative, '/') ? $relative : "$base/$relative";
            // Composer classmap entries support filesystem globs.
            foreach (glob($path, GLOB_NOSORT | GLOB_BRACE) ?: [] as $candidate) {
                $real = realpath($candidate);
                if (!$real || !within($real, $root)) { $skipped++; continue; }
                if (is_dir($real)) {
                    $directory = new \RecursiveDirectoryIterator($real, \FilesystemIterator::SKIP_DOTS);
                    $filtered = new \RecursiveCallbackFilterIterator($directory, static function ($entry) {
                        if ($entry->isLink()) return false;
                        return !preg_match('~^(?:tests?|fixtures?|modules|themes|node_modules|vendor|\.git)$~i', $entry->getFilename());
                    });
                    $entries = new \RecursiveIteratorIterator($filtered);
                } else {
                    $entries = [new \SplFileInfo($real)];
                }
                foreach ($entries as $entry) {
                    $file = $entry->getRealPath();
                    if (!$file || !$entry->isFile() || !str_ends_with($file, '.php') || !within($file, $root)) continue;
                    // Explicit classmap targets also pass through the exclusions.
                    $relativeFile = substr($file, strlen($root) + 1);
                    if (preg_match('~(?:^|/)(?:tests?|fixtures?|modules|themes)(?:/|$)~i', $relativeFile) || basename($file) === 'index.php') {
                        $skipped++;
                        continue;
                    }
                    foreach ($exclusions as $pattern) {
                        if (preg_match($pattern, $file)) { $skipped++; continue 2; }
                    }
                    $files[$file] = true;
                }
            }
        }
    }
    $files = array_keys($files);
    sort($files, SORT_STRING);
    return ['files' => $files, 'skipped' => $skipped];
}
