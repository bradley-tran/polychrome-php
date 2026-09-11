FROM composer:2.9.5 AS composer
FROM polychrome:local AS application
USER root
RUN apt-get update && apt-get install -y --no-install-recommends mariadb-client \
    && rm -rf /var/lib/apt/lists/*
COPY --from=composer /usr/bin/composer /usr/local/bin/composer
COPY --chown=1000:1000 examples/drupal/app/composer.json examples/drupal/app/composer.lock /app/
USER 1000:1000
RUN composer install --no-dev --prefer-dist --no-interaction --optimize-autoloader \
    && mkdir -p web/sites/default/files private config/sync
COPY --chown=1000:1000 examples/drupal/settings.php /app/web/sites/default/settings.php
COPY --chown=1000:1000 examples/drupal/benchmark.php /app/web/polychrome-benchmark.php
CMD ["--root", "/app", "--listen", "0.0.0.0:9000", "--workers", "2"]

FROM nginxinc/nginx-unprivileged:1.28-bookworm AS web
COPY --from=application /app/web /app/web
COPY examples/drupal/nginx.conf /etc/nginx/conf.d/default.conf
