#include "php.h"
#include "SAPI.h"
#include "php_main.h"
#include "php_ini.h"
#include "php_ini_builder.h"
#include "php_variables.h"
#include "php_globals.h"
#include "php_network.h"
#include "php_output.h"
#include "fopen_wrappers.h"
#include "ext/standard/info.h"
#include "fastcgi.h"
#include "polychrome.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pwd.h>
#include <signal.h>
#include <stdint.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#ifdef ZTS
#error Polychrome requires non-thread-safe PHP
#endif
#if PHP_VERSION_ID < 80500 || PHP_VERSION_ID >= 80600
#error Polychrome v0.1 supports PHP 8.5 only
#endif

static const char *application_root;
static const char *preload_path;
static const char *preload_user;
static struct php_ini_builder ini_builder;
static fcgi_request *request;
static int event_fd = -1;
static volatile sig_atomic_t connection_active;
static volatile sig_atomic_t drain_requested;
static bool response_finished;
static bool runtime_initialized;
static void (*original_import_environment)(zval *);
static void (*original_load_environment)(zval *);

static void log_message(const char *message, int type)
{
    (void) type;
    fprintf(stderr, "polychrome: PHP pid=%ld: %s\n", (long) getpid(), message);
}

static size_t write_output(const char *data, size_t length)
{
    if (!request) return fwrite(data, 1, length, stderr);
    if (response_finished) return length;
    size_t written = 0;
    while (written < length) {
        int part = (int) ((length - written) > INT_MAX ? INT_MAX : length - written);
        int result = fcgi_write(request, FCGI_STDOUT, data + written, part);
        if (result <= 0) {
            php_handle_aborted_connection();
            break;
        }
        written += (size_t) result;
    }
    return written;
}

static int send_headers(sapi_headers_struct *headers)
{
    if (!request || response_finished) return SAPI_HEADER_SENT_SUCCESSFULLY;
    char status[80];
    int length = snprintf(status, sizeof(status), "Status: %d\r\n", headers->http_response_code);
    write_output(status, (size_t) length);
    zend_llist_position position;
    sapi_header_struct *header = zend_llist_get_first_ex(&headers->headers, &position);
    for (; header; header = zend_llist_get_next_ex(&headers->headers, &position)) {
        if (!header->header_len || !strncasecmp(header->header, "Status:", 7)) continue;
        write_output(header->header, header->header_len);
        write_output("\r\n", 2);
    }
    write_output("\r\n", 2);
    return SAPI_HEADER_SENT_SUCCESSFULLY;
}

static void flush_output(void *context)
{
    (void) context;
    if (!request || response_finished) return;
    sapi_send_headers();
    if (!fcgi_flush(request, 0)) php_handle_aborted_connection();
}

static size_t read_post(char *buffer, size_t length)
{
    if (!request || response_finished) return 0;
    size_t remaining = SG(request_info).content_length > SG(read_post_bytes)
        ? SG(request_info).content_length - SG(read_post_bytes) : 0;
    if (length > remaining) length = remaining;
    int result = fcgi_read(request, buffer, (int) (length > INT_MAX ? INT_MAX : length));
    return result > 0 ? (size_t) result : 0;
}

static char *request_getenv(const char *name, size_t length)
{
    if (request) {
        char *value = fcgi_getenv(request, name, (int) length);
        if (value) return value;
    }
    return getenv(name);
}

static char *read_cookies(void)
{
    return request ? FCGI_GETENV(request, "HTTP_COOKIE") : NULL;
}

static void load_variable(const char *name, unsigned int name_length,
                          char *value, unsigned int value_length, void *arg)
{
    (void) name_length;
    size_t filtered_length;
    if (sapi_module.input_filter(PARSE_SERVER, name, &value, value_length, &filtered_length)) {
        php_register_variable_safe(name, value, filtered_length, arg);
    }
}

static void import_environment(zval *array)
{
    original_import_environment(array);
    if (request) fcgi_loadenv(request, load_variable, array);
}

static void load_environment(zval *array)
{
    original_load_environment(array);
    if (request) fcgi_loadenv(request, load_variable, array);
}

static void register_variables(zval *array)
{
    import_environment(array);
    if (!request) return;
    char *script = FCGI_GETENV(request, "SCRIPT_NAME");
    char *path = FCGI_GETENV(request, "PATH_INFO");
    char *self;
    spprintf(&self, 0, "%s%s", script ? script : "", path ? path : "");
    php_register_variable("PHP_SELF", self, array);
    efree(self);
}

ZEND_BEGIN_ARG_WITH_RETURN_TYPE_INFO_EX(arginfo_finish_request, 0, 0, _IS_BOOL, 0)
ZEND_END_ARG_INFO()

PHP_FUNCTION(fastcgi_finish_request)
{
    ZEND_PARSE_PARAMETERS_NONE();
    if (!request || response_finished) RETURN_FALSE;
    php_output_end_all();
    sapi_send_headers();
    fcgi_request_set_keep(request, 0);
    /* Keep the request environment alive for lazy superglobals and shutdown
     * callbacks, and half-close before draining to avoid truncating responses
     * with TCP/Unix resets when input records remain unread. */
    fcgi_end(request);
    fcgi_close(request, 0, 0);
    response_finished = true;
    RETURN_TRUE;
}

static const zend_function_entry functions[] = {
    PHP_FE(fastcgi_finish_request, arginfo_finish_request)
    PHP_FE_END
};

static void set_default(HashTable *configuration, const char *key, const char *value)
{
    zval entry;
    /* PHP's configuration hash owns persistent strings, not request memory. */
    ZVAL_NEW_STR(&entry, zend_string_init(value, strlen(value), 1));
    zend_hash_str_update(configuration, key, strlen(key), &entry);
}

static void ini_defaults(HashTable *configuration)
{
    set_default(configuration, "opcache.enable", "1");
    set_default(configuration, "opcache.jit", "disable");
    set_default(configuration, "opcache.memory_consumption", "256");
    set_default(configuration, "opcache.max_accelerated_files", "100000");
    set_default(configuration, "memory_limit", "256M");
    set_default(configuration, "opcache.preload", preload_path ? preload_path : "");
    set_default(configuration, "opcache.preload_user", preload_user);
    set_default(configuration, "display_errors", "0");
    set_default(configuration, "log_errors", "1");
    set_default(configuration, "expose_php", "0");
    set_default(configuration, "max_execution_time", "0");
    set_default(configuration, "variables_order", "GPCS");
}

PHP_MINIT_FUNCTION(polychrome)
{
    char *configured = NULL;
    zend_long opcache_enabled = 0;
    if (preload_path && (cfg_get_long("opcache.enable", &opcache_enabled) != SUCCESS || !opcache_enabled)) {
        fprintf(stderr, "polychrome: automatic preloading requires opcache.enable=1; use --no-preload when disabling OPcache\n");
        return FAILURE;
    }
    if (cfg_get_string("opcache.preload", &configured) == SUCCESS && configured &&
        strcmp(configured, preload_path ? preload_path : "")) {
        fprintf(stderr, "polychrome: opcache.preload is reserved; remove it from php.ini\n");
        return FAILURE;
    }
    if (cfg_get_string("opcache.preload_user", &configured) == SUCCESS && configured && strcmp(configured, preload_user)) {
        fprintf(stderr, "polychrome: preloading must run as the invoking user\n");
        return FAILURE;
    }
    return SUCCESS;
}

static zend_module_entry polychrome_module = {
    STANDARD_MODULE_HEADER,
    "polychrome", functions, PHP_MINIT(polychrome), NULL, NULL, NULL, NULL,
    "0.1.0", STANDARD_MODULE_PROPERTIES
};

static int startup(sapi_module_struct *module)
{
    return php_module_startup(module, &polychrome_module);
}

static sapi_module_struct polychrome_sapi = {
    .name = "polychrome",
    .pretty_name = "Polychrome FastCGI",
    .startup = startup,
    .shutdown = php_module_shutdown_wrapper,
    .ub_write = write_output,
    .flush = flush_output,
    .getenv = request_getenv,
    .sapi_error = php_error,
    .send_headers = send_headers,
    .read_post = read_post,
    .read_cookies = read_cookies,
    .register_server_variables = register_variables,
    .log_message = log_message,
    .php_ini_ignore_cwd = 1,
    .ini_defaults = ini_defaults,
    .phpinfo_as_text = 0,
};

const char *poly_php_version(void) { return PHP_VERSION; }

int poly_initialize(const char *root, const char *ini, const char *const *defines,
                    size_t count, const char *preload)
{
    application_root = root;
    preload_path = preload;
    struct passwd *user = getpwuid(geteuid());
    if (!user) { fprintf(stderr, "polychrome: cannot resolve invoking user\n"); return 1; }
    preload_user = strdup(user->pw_name);
    setenv("POLYCHROME_ROOT", root, 1);
    php_ini_builder_init(&ini_builder);
    for (size_t i = 0; i < count; i++) php_ini_builder_define(&ini_builder, defines[i]);
    polychrome_sapi.ini_entries = php_ini_builder_finish(&ini_builder);
    polychrome_sapi.php_ini_path_override = (char *) ini;
    signal(SIGPIPE, SIG_IGN);
    volatile int result = 1;
    zend_first_try {
        sapi_startup(&polychrome_sapi);
        if (startup(&polychrome_sapi) == SUCCESS) {
            runtime_initialized = true;
            fcgi_init();
            original_import_environment = php_import_environment_variables;
            original_load_environment = php_load_environment_variables;
            php_import_environment_variables = import_environment;
            php_load_environment_variables = load_environment;
            result = 0;
        }
    } zend_catch {
        fprintf(stderr, "polychrome: PHP startup bailed out\n");
    } zend_end_try();
    return result;
}

static void drain_handler(int signal_number)
{
    (void) signal_number;
    drain_requested = 1;
    if (!connection_active) _exit(0);
}

void poly_connection_accepted(int fd)
{
    if (drain_requested || connection_active) _exit(0);
    connection_active = 1;
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    uint64_t stamp = (uint64_t) now.tv_sec * 1000000000 + (uint64_t) now.tv_nsec;
    if (send(event_fd, &stamp, sizeof(stamp), MSG_NOSIGNAL) != sizeof(stamp)) _exit(1);
}

static void before_accept(void)
{
    // FastCGI retries malformed connections internally. Each child is allowed
    // only one connection, including management or malformed requests.
    if (connection_active || drain_requested) _exit(0);
}

static void protocol_error(int status, const char *message)
{
    char header[128];
    int length = snprintf(header, sizeof(header), "Status: %d\r\nContent-Type: text/plain\r\n\r\n", status);
    fcgi_write(request, FCGI_STDOUT, header, length);
    fcgi_write(request, FCGI_STDOUT, message, (int) strlen(message));
}

static bool permitted_script(const char *path)
{
    size_t root_length = strlen(application_root), length = strlen(path);
    if (strncmp(path, application_root, root_length) ||
        (root_length > 1 && path[root_length] != '/')) return false;
    if (length < 4 || strcmp(path + length - 4, ".php")) return false;
    struct stat info;
    return stat(path, &info) == 0 && S_ISREG(info.st_mode) && access(path, R_OK) == 0;
}

int poly_serve_one(int listener, int events)
{
    event_fd = events;
    struct sigaction action = {0};
    sigemptyset(&action.sa_mask);
    action.sa_handler = drain_handler;
    action.sa_flags = SA_RESTART;
    sigaction(SIGUSR1, &action, NULL);
    /* Terminal Ctrl-C and service managers may signal the entire process
     * group. Active children still drain; only the manager enforces the kill
     * deadline. SA_RESTART keeps their in-flight blocking I/O intact. */
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    signal(SIGCHLD, SIG_DFL);
    sigset_t mask;
    sigemptyset(&mask);
    sigprocmask(SIG_SETMASK, &mask, NULL);
    volatile int result = 0;
    volatile bool started = false;
    zend_first_try {
        /* PHP 8.5 refreshes the allocator and per-process execution timer after
         * fork, just as it does for FPM children. */
        php_child_init();
        request = fcgi_init_request(listener, before_accept, NULL, NULL);
        if (fcgi_accept_request(request) >= 0) {
            close(listener);
            fcgi_request_set_keep(request, 0);
            char resolved[PATH_MAX];
            char *filename = FCGI_GETENV(request, "SCRIPT_FILENAME");
            char *method = FCGI_GETENV(request, "REQUEST_METHOD");
            char *content_length = FCGI_GETENV(request, "CONTENT_LENGTH");
            zend_long body_length = 0;
            bool valid_length = true;
            if (content_length && *content_length) {
                char *end;
                errno = 0;
                body_length = strtol(content_length, &end, 10);
                valid_length = !errno && !*end && body_length >= 0 && *content_length >= '0' && *content_length <= '9';
            }
            if (!method || !*method || !valid_length) {
                protocol_error(400, "Invalid FastCGI request.\n");
            } else if (!filename || !realpath(filename, resolved)) {
                protocol_error(404, "Script not found.\n");
            } else if (!permitted_script(resolved)) {
                protocol_error(403, "Script is outside the application root or is not a readable PHP file.\n");
            } else {
                SG(server_context) = request;
                SG(request_info).request_method = method;
                SG(request_info).query_string = FCGI_GETENV(request, "QUERY_STRING");
                SG(request_info).request_uri = FCGI_GETENV(request, "REQUEST_URI");
                SG(request_info).content_type = FCGI_GETENV(request, "CONTENT_TYPE");
                SG(request_info).content_length = body_length;
                SG(request_info).path_translated = estrdup(resolved);
                SG(request_info).proto_num = 1001;
                SG(request_info).headers_only = !strcmp(method, "HEAD");
                SG(request_info).no_headers = 0;
                SG(request_info).argc = 0;
                SG(sapi_headers).http_response_code = 200;
                php_handle_auth_data(FCGI_GETENV(request, "HTTP_AUTHORIZATION"));
                if (php_request_startup() == SUCCESS) {
                    started = true;
                    EG(exit_status) = 0;
                    zend_file_handle file;
                    memset(&file, 0, sizeof(file));
                    if (php_fopen_primary_script(&file) == SUCCESS) {
                        php_execute_script(&file);
                        if (!file.in_list) zend_destroy_file_handle(&file);
                    } else {
                        SG(sapi_headers).http_response_code = 404;
                        PHPWRITE("Script not found.\n", sizeof("Script not found.\n") - 1);
                    }
                    result = EG(exit_status) == 255 ? 1 : 0;
                } else {
                    protocol_error(500, "PHP request startup failed.\n");
                    result = 1;
                }
            }
        }
    } zend_catch {
        result = 1;
    } zend_end_try();

    if (started) {
        zend_first_try {
            if (SG(request_info).path_translated) {
                efree(SG(request_info).path_translated);
                SG(request_info).path_translated = NULL;
            }
            php_request_shutdown(NULL);
        } zend_catch { result = 1; } zend_end_try();
    }
    if (request) {
        if (!response_finished) fcgi_finish_request(request, 0);
        fcgi_destroy_request(request);
    }
    request = NULL;
    SG(server_context) = NULL;
    return result;
}

void poly_shutdown(void)
{
    if (!runtime_initialized) return;
    zend_first_try {
        fcgi_shutdown();
        php_import_environment_variables = original_import_environment;
        php_load_environment_variables = original_load_environment;
        php_module_shutdown();
        sapi_shutdown();
    } zend_catch { } zend_end_try();
    php_ini_builder_deinit(&ini_builder);
    free((void *) preload_user);
    runtime_initialized = false;
}

extern int polychrome_main(void);
int main(int argc, char **argv)
{
    (void) argc;
    (void) argv;
    return polychrome_main();
}
