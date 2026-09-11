#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
compose_project=${1:-polychrome-alpha}
compose=(docker compose -p "$compose_project" -f "$project_dir/examples/drupal/compose.yaml")
"${compose[@]}" exec -T runtime php -d memory_limit=512M vendor/drush/drush/drush.php site:install standard \
    --yes --account-name=admin --account-pass=polychrome-example \
    --site-name='Polychrome integration'
"${compose[@]}" exec -T runtime php vendor/drush/drush/drush.php \
    recipe /app/web/core/recipes/article_content_type --yes
# Installing Drupal creates code and changes configuration: rebuild the zygote.
"${compose[@]}" restart runtime
python3 "$project_dir/tests/drupal.py" --base-url "${DRUPAL_TEST_URL:-http://127.0.0.1:8080}"
"${compose[@]}" exec -T runtime php vendor/drush/drush/drush.php cache:rebuild
python3 "$project_dir/tests/drupal.py" --base-url "${DRUPAL_TEST_URL:-http://127.0.0.1:8080}" --anonymous-only
