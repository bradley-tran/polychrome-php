/* Compile PHP's unmodified FastCGI implementation for this SAPI. Its on_read
 * hook runs AFTER an initial socket wait. Intercept accept so the manager's
 * deadline includes clients that connect but never send even their first byte.
 * FPM's companion binary still uses the stock FastCGI object. */
#include <sys/socket.h>
#include "polychrome.h"

static int poly_accept(int socket, struct sockaddr *address, socklen_t *length)
{
    int fd = accept(socket, address, length);
    if (fd >= 0) poly_connection_accepted(fd);
    return fd;
}

#define accept poly_accept
#include "main/fastcgi.c"
