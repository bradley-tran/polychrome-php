#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$project_dir/scripts/versions.env"
build_dir=${POLYCHROME_BUILD_DIR:-"$project_dir/.build"}
prefix=${POLYCHROME_PREFIX:-"$project_dir/dist"}
jobs=${JOBS:-4}
mkdir -p "$build_dir/downloads" "$prefix"
build_dir=$(cd "$build_dir" && pwd)
prefix=$(cd "$prefix" && pwd)
archive="$build_dir/downloads/php-$PHP_VERSION.tar.xz"
if [[ ! -f "$archive" ]]; then
    curl --fail --location --retry 3 --silent --show-error \
        "https://www.php.net/distributions/php-$PHP_VERSION.tar.xz" -o "$archive.tmp"
    mv "$archive.tmp" "$archive"
fi
printf '%s  %s\n' "$PHP_SHA256" "$archive" | sha256sum --check --status
php_source="$build_dir/php-$PHP_VERSION"
if [[ ! -d "$php_source" ]]; then tar -xf "$archive" -C "$build_dir"; fi

cd "$project_dir"
cargo build --release --locked
rust_target=${CARGO_TARGET_DIR:-"$project_dir/target"}
export POLYCHROME_RUST_LIB="$(cd "$rust_target/release" && pwd)/libpolychrome_php.a"
mkdir -p "$php_source/sapi/polychrome"
cp "$project_dir"/native/* "$php_source/sapi/polychrome/"
cd "$php_source"
# PHP's generated configure script must discover the added SAPI. PHP's engine
# and OPcache sources are kept unchanged; generated files stay under .build.
./buildconf --force > "$build_dir/buildconf.log" 2>&1

flags=(
    "--prefix=$prefix" "--with-config-file-path=$prefix/etc"
    "--with-config-file-scan-dir=$prefix/etc/conf.d"
    --enable-polychrome --enable-cli --enable-fpm --disable-cgi
    --disable-phpdbg --without-pear --disable-zts
    --enable-mbstring --enable-intl --enable-bcmath --enable-exif
    --enable-pcntl --enable-sockets --enable-soap --enable-gd
    --with-openssl --with-curl --with-zlib --with-zip
    --with-pdo-mysql --with-pdo-sqlite --with-mysqli --with-jpeg --with-webp
)
if [[ ${SANITIZE:-0} == 1 ]]; then
    export CFLAGS="-O1 -g -fno-omit-frame-pointer -fsanitize=address,undefined"
    export LDFLAGS="-fsanitize=address,undefined"
    flags+=(--disable-opcache-jit)
fi
./configure "${flags[@]}" > "$build_dir/configure.log" 2>&1 || {
    tail -60 "$build_dir/configure.log" >&2
    exit 1
}
make -j"$jobs" > "$build_dir/make.log" 2>&1 || {
    tail -80 "$build_dir/make.log" >&2
    exit 1
}
make install > "$build_dir/install.log" 2>&1
install -d "$prefix/share/polychrome" "$prefix/etc/conf.d"
install -m 0644 "$project_dir"/preload/*.php "$prefix/share/polychrome/"
install -m 0644 "$php_source/LICENSE" "$prefix/share/polychrome/PHP-LICENSE"
install -m 0644 "$project_dir/LICENSE" "$prefix/share/polychrome/LICENSE"
"$prefix/bin/polychrome" --version
printf 'Built runtime and PHP CLI/FPM companions in %s\n' "$prefix"
