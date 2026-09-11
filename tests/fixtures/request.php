<?php
$action = $_GET['action'] ?? 'inspect';
if ($action === 'opcache') {
    echo json_encode(opcache_get_status(false));
    return;
}
if ($action === 'fatal') {
    polychrome_intentionally_missing_function();
}
if ($action === 'crash') {
    posix_kill(getmypid(), SIGKILL);
}
if ($action === 'hang') {
    file_put_contents(__DIR__ . '/started', (string) getmypid());
    sleep(60);
}
if ($action === 'slow') {
    file_put_contents(__DIR__ . '/started', (string) getmypid());
    usleep(500000);
    echo 'completed';
    return;
}
if ($action === 'finish') {
    echo 'finished';
    fastcgi_finish_request();
    file_put_contents(__DIR__ . '/started', (string) getmypid());
    usleep(500000);
    file_put_contents(__DIR__ . '/after-finish', 'done');
    echo 'must not reach client';
    return;
}
if ($action === 'finish-hang') {
    echo 'finished';
    fastcgi_finish_request();
    sleep(60);
}
if ($action === 'stream') {
    header('Content-Type: text/plain');
    echo 'first';
    flush();
    usleep(300000);
    echo 'second';
    return;
}
if ($action === 'shutdown') {
    register_shutdown_function(static function () { echo ' shutdown'; });
    echo 'body';
    exit;
}
if ($action === 'headers') {
    header('Location: /next', true, 303);
    header('Set-Cookie: a=1', false);
    header('Set-Cookie: b=2', false);
    echo 'redirect';
    return;
}
if ($action === 'session') {
    session_start();
    $_SESSION['count'] = ($_SESSION['count'] ?? 0) + 1;
    echo $_SESSION['count'];
    return;
}
if ($action === 'preload') {
    $before = class_exists('PreloadedDemo', false);
    require __DIR__ . '/lib/Demo.php';
    echo json_encode(['before' => $before, 'value' => PreloadedDemo::VALUE, 'side_effects' => $GLOBALS['side_effects'] ?? 0]);
    return;
}
if ($action === 'isolation') {
    static $counter = 0;
    echo json_encode(['pid' => getmypid(), 'counter' => ++$counter,
        'environment' => getenv('POLYCHROME_TEST_MUTATION'), 'cwd' => getcwd(),
        'precision' => ini_get('precision'), 'global' => $GLOBALS['dirty'] ?? null]);
    putenv('POLYCHROME_TEST_MUTATION=dirty');
    chdir('/');
    ini_set('precision', '3');
    $GLOBALS['dirty'] = true;
    return;
}
$files = [];
foreach ($_FILES as $key => $file) {
    $files[$key] = ['name' => $file['name'], 'error' => $file['error'],
        'size' => $file['size'], 'uploaded' => is_uploaded_file($file['tmp_name']),
        'content' => file_get_contents($file['tmp_name'])];
}
header('Content-Type: application/json');
echo json_encode(['pid' => getmypid(), 'sapi' => PHP_SAPI, 'get' => $_GET,
    'post' => $_POST, 'cookies' => $_COOKIE, 'files' => $files,
    'input' => file_get_contents('php://input'), 'server' => $_SERVER,
    'env_method' => getenv('REQUEST_METHOD')]);
