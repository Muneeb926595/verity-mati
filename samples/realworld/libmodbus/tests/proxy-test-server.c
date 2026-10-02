/*
 * Copyright © Stéphane Raimbault <stephane.raimbault@gmail.com>
 *
 * SPDX-License-Identifier: BSD-3-Clause
 */

/* Proxy test server: listens on TCP port 1503 and forwards all requests
   to a backend server on TCP port 1502 using modbus_proxy(). */

#include <errno.h>
#include <modbus.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef PROXY_PORT
#define PROXY_PORT 1503
#endif

#ifndef BACKEND_PORT
#define BACKEND_PORT 1502
#endif

int main(int argc, char *argv[])
{
    modbus_t *frontend_ctx;
    modbus_t *backend_ctx;
    int s = -1;
    int rc;
    uint8_t query[MODBUS_TCP_MAX_ADU_LENGTH];
    char *backend_ip = "127.0.0.1";

    if (argc > 1) {
        backend_ip = argv[1];
    }

    /* Frontend: accept connections from clients */
    frontend_ctx = modbus_new_tcp("0.0.0.0", PROXY_PORT);
    if (frontend_ctx == NULL) {
        fprintf(stderr, "Failed to create frontend context: %s\n",
                modbus_strerror(errno));
        return -1;
    }

    /* Backend: connect to the real Modbus server */
    backend_ctx = modbus_new_tcp(backend_ip, BACKEND_PORT);
    if (backend_ctx == NULL) {
        fprintf(stderr, "Failed to create backend context: %s\n",
                modbus_strerror(errno));
        modbus_free(frontend_ctx);
        return -1;
    }

    modbus_set_debug(frontend_ctx, TRUE);
    modbus_set_debug(backend_ctx, TRUE);

    /* Connect to the backend server */
    if (modbus_connect(backend_ctx) == -1) {
        fprintf(stderr, "Backend connection failed: %s\n",
                modbus_strerror(errno));
        modbus_free(frontend_ctx);
        modbus_free(backend_ctx);
        return -1;
    }

    /* Listen for frontend client connections */
    s = modbus_tcp_listen(frontend_ctx, 1);
    if (s == -1) {
        fprintf(stderr, "Frontend listen failed: %s\n",
                modbus_strerror(errno));
        modbus_close(backend_ctx);
        modbus_free(frontend_ctx);
        modbus_free(backend_ctx);
        return -1;
    }

    modbus_tcp_accept(frontend_ctx, &s);

    for (;;) {
        do {
            rc = modbus_receive(frontend_ctx, query);
            /* Filtered queries return 0 */
        } while (rc == 0);

        if (rc == -1) {
            /* Connection closed or error */
            break;
        }

        /* Forward request to backend and relay response */
        rc = modbus_proxy(frontend_ctx, backend_ctx, query, rc);
        if (rc == -1) {
            fprintf(stderr, "Proxy error: %s\n", modbus_strerror(errno));
            /* Continue serving, the exception was already sent */
        }
    }

    printf("Proxy server exiting: %s\n", modbus_strerror(errno));

    if (s != -1) {
        close(s);
    }
    modbus_close(backend_ctx);
    modbus_close(frontend_ctx);
    modbus_free(backend_ctx);
    modbus_free(frontend_ctx);

    return 0;
}
