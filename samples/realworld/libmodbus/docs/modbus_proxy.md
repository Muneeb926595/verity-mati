# modbus_proxy

## Name

modbus_proxy - forward a request to a backend device and relay the response

## Synopsis

```c
int modbus_proxy(modbus_t *frontend_ctx, modbus_t *backend_ctx, const uint8_t *req, int req_length);
```

## Description

The *modbus_proxy()* function shall forward a Modbus request received on
`frontend_ctx` to a device connected through `backend_ctx`, and relay the
response back to the original requester on `frontend_ctx`.

This function is intended for building Modbus gateways or proxies that bridge
two different backends. A typical use case is a TCP-to-RTU gateway: a client
sends a TCP request which is forwarded to an RTU device on a serial bus, and the
RTU response is relayed back as a TCP response.

The request `req` of length `req_length` should be a raw request as returned by
[modbus_receive](modbus_receive.md). The slave address is extracted from `req`
and set on `backend_ctx` before forwarding.

The function handles protocol translation between backends automatically:

- the PDU (Protocol Data Unit) is extracted from the frontend framing
- it is re-framed for the backend protocol
- the backend response PDU is extracted and re-framed for the frontend protocol
- the transaction identifier from the original request is preserved

On communication errors, an appropriate Modbus gateway exception response is
sent back to the frontend client:

- `MODBUS_EXCEPTION_GATEWAY_PATH` (0x0A) if the request cannot be forwarded or
  if a non-timeout communication error occurs
- `MODBUS_EXCEPTION_GATEWAY_TARGET` (0x0B) if the backend device does not
  respond (timeout) or sends an invalid response

## Return value

The function shall return the length of the response sent on `frontend_ctx` if
successful. Otherwise it shall return -1 and set errno.

## Errors

- *EINVAL*, `frontend_ctx` or `backend_ctx` is NULL, or `req` is NULL, or
  `req_length` is less than 1.
- *EMBBADDATA*, the PDU extracted from the request is invalid (too short or too
  long).

## Example

```c
modbus_t *tcp_ctx;
modbus_t *rtu_ctx;
uint8_t query[MODBUS_TCP_MAX_ADU_LENGTH];
int rc;

tcp_ctx = modbus_new_tcp("0.0.0.0", 1502);
rtu_ctx = modbus_new_rtu("/dev/ttyUSB0", 38400, 'E', 8, 1);

int s = modbus_tcp_listen(tcp_ctx, 1);
modbus_connect(rtu_ctx);

for (;;) {
    modbus_tcp_accept(tcp_ctx, &s);

    for (;;) {
        rc = modbus_receive(tcp_ctx, query);
        if (rc < 0)
            break;
        if (rc == 0)
            continue;

        /* Forward the TCP request to the RTU device and relay the response */
        modbus_proxy(tcp_ctx, rtu_ctx, query, rc);
    }

    modbus_close(tcp_ctx);
}

modbus_close(rtu_ctx);
modbus_free(tcp_ctx);
modbus_free(rtu_ctx);
```

## See also

- [modbus_receive](modbus_receive.md)
- [modbus_reply](modbus_reply.md)
- [modbus_reply_exception](modbus_reply_exception.md)
