<?php
// Local integration fixture only; these credentials never belong in production.
$databases['default']['default'] = [
    'database' => getenv('DRUPAL_DB_NAME') ?: 'drupal',
    'username' => getenv('DRUPAL_DB_USER') ?: 'drupal',
    'password' => getenv('DRUPAL_DB_PASSWORD') ?: 'polychrome-example',
    'host' => getenv('DRUPAL_DB_HOST') ?: 'database',
    'port' => getenv('DRUPAL_DB_PORT') ?: '3306',
    'driver' => 'mysql',
    'namespace' => 'Drupal\\mysql\\Driver\\Database\\mysql',
    'autoload' => 'core/modules/mysql/src/Driver/Database/mysql/',
];
$settings['hash_salt'] = 'polychrome-local-fixture-not-for-production';
$settings['trusted_host_patterns'] = ['^localhost$', '^127\\.0\\.0\\.1$', '^web$'];
$settings['file_public_path'] = 'sites/default/files';
$settings['file_private_path'] = '/app/private';
$settings['config_sync_directory'] = '/app/config/sync';
$settings['file_temp_path'] = '/tmp';
$settings['update_free_access'] = false;
