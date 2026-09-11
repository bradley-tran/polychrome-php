#ifndef POLYCHROME_H
#define POLYCHROME_H
#include <stddef.h>

/* All PHP/Zend values and bailout frames stay behind this ABI. */
const char *poly_php_version(void);
int poly_initialize(const char *root, const char *ini, const char *const *defines,
                    size_t count, const char *preload);
int poly_serve_one(int listener, int events);
void poly_shutdown(void);
void poly_connection_accepted(int fd);
#endif
