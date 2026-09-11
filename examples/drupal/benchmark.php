<?php
// Fixture endpoint. Never put this benchmark helper into a real application.
if (($_GET['mode'] ?? '') === 'bootstrap') {
    $autoloader = require __DIR__ . '/autoload.php';
    $request = \Symfony\Component\HttpFoundation\Request::createFromGlobals();
    $kernel = \Drupal\Core\DrupalKernel::createFromRequest($request, $autoloader, 'prod');
    $kernel->boot();
}
header('Content-Type: text/plain');
echo 'polychrome benchmark';
