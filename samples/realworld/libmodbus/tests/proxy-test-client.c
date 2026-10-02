/*
 * Copyright © Stéphane Raimbault <stephane.raimbault@gmail.com>
 *
 * SPDX-License-Identifier: BSD-3-Clause
 */

/* Proxy test client: connects to the proxy server on port 1503 and runs
   a series of read/write tests to validate that modbus_proxy correctly
   forwards requests to the backend and relays responses. */

#include <errno.h>
#include <modbus.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "unit-test.h"

#ifndef PROXY_PORT
#define PROXY_PORT 1503
#endif

#define BUG_REPORT(_cond, _format, _args...) \
    printf(                                  \
        "\nLine %d: assertion error for '%s': " _format "\n", __LINE__, #_cond, ##_args)

#define ASSERT_TRUE(_cond, _format, __args...)    \
    {                                             \
        if (_cond) {                              \
            printf("OK\n");                       \
        } else {                                  \
            BUG_REPORT(_cond, _format, ##__args); \
            goto close;                           \
        }                                         \
    };

int main(int argc, char *argv[])
{
    modbus_t *ctx = NULL;
    uint8_t *tab_rp_bits = NULL;
    uint16_t *tab_rp_registers = NULL;
    int rc;
    int i;
    int nb_points;
    int success = FALSE;
    char *ip = "127.0.0.1";

    if (argc > 1) {
        ip = argv[1];
    }

    ctx = modbus_new_tcp(ip, PROXY_PORT);
    if (ctx == NULL) {
        fprintf(stderr, "Unable to allocate libmodbus context\n");
        return -1;
    }

    modbus_set_debug(ctx, TRUE);
    modbus_set_error_recovery(
        ctx, MODBUS_ERROR_RECOVERY_LINK | MODBUS_ERROR_RECOVERY_PROTOCOL);

    if (modbus_connect(ctx) == -1) {
        fprintf(stderr, "Connection to proxy failed: %s\n",
                modbus_strerror(errno));
        modbus_free(ctx);
        return -1;
    }

    /* Allocate memory for results */
    nb_points = (UT_BITS_NB > UT_INPUT_BITS_NB) ? UT_BITS_NB : UT_INPUT_BITS_NB;
    tab_rp_bits = (uint8_t *) malloc(nb_points * sizeof(uint8_t));
    memset(tab_rp_bits, 0, nb_points * sizeof(uint8_t));

    nb_points = (UT_REGISTERS_NB > UT_INPUT_REGISTERS_NB) ? UT_REGISTERS_NB
                                                          : UT_INPUT_REGISTERS_NB;
    tab_rp_registers = (uint16_t *) malloc(nb_points * sizeof(uint16_t));
    memset(tab_rp_registers, 0, nb_points * sizeof(uint16_t));

    printf("** PROXY UNIT TESTING **\n");

    /** Test 1: Write and read a single coil through the proxy **/
    printf("\nTEST COILS THROUGH PROXY:\n");

    rc = modbus_write_bit(ctx, UT_BITS_ADDRESS, ON);
    printf("1/2 modbus_write_bit via proxy: ");
    ASSERT_TRUE(rc == 1, "FAILED (rc %d)\n", rc);

    rc = modbus_read_bits(ctx, UT_BITS_ADDRESS, 1, tab_rp_bits);
    printf("2/2 modbus_read_bits via proxy: ");
    ASSERT_TRUE(rc == 1, "FAILED (rc %d)\n", rc);
    ASSERT_TRUE(tab_rp_bits[0] == ON, "FAILED (%0X != %0X)\n", tab_rp_bits[0], ON);

    /** Test 2: Write and read multiple coils **/
    printf("\nTEST MULTIPLE COILS THROUGH PROXY:\n");
    {
        uint8_t tab_value[UT_BITS_NB];

        modbus_set_bits_from_bytes(tab_value, 0, UT_BITS_NB, UT_BITS_TAB);
        rc = modbus_write_bits(ctx, UT_BITS_ADDRESS, UT_BITS_NB, tab_value);
        printf("1/2 modbus_write_bits via proxy: ");
        ASSERT_TRUE(rc == UT_BITS_NB, "FAILED (rc %d)\n", rc);
    }

    rc = modbus_read_bits(ctx, UT_BITS_ADDRESS, UT_BITS_NB, tab_rp_bits);
    printf("2/2 modbus_read_bits via proxy: ");
    ASSERT_TRUE(rc == UT_BITS_NB, "FAILED (rc %d)\n", rc);

    i = 0;
    nb_points = UT_BITS_NB;
    while (nb_points > 0) {
        int nb_bits = (nb_points > 8) ? 8 : nb_points;
        uint8_t value = modbus_get_byte_from_bits(tab_rp_bits, i * 8, nb_bits);
        ASSERT_TRUE(
            value == UT_BITS_TAB[i], "FAILED (%0X != %0X)\n", value, UT_BITS_TAB[i]);
        nb_points -= nb_bits;
        i++;
    }
    printf("OK\n");

    /** Test 3: Write and read holding registers **/
    printf("\nTEST REGISTERS THROUGH PROXY:\n");

    rc = modbus_write_register(ctx, UT_REGISTERS_ADDRESS, 0x1234);
    printf("1/2 modbus_write_register via proxy: ");
    ASSERT_TRUE(rc == 1, "FAILED (rc %d)\n", rc);

    rc = modbus_read_registers(ctx, UT_REGISTERS_ADDRESS, 1, tab_rp_registers);
    printf("2/2 modbus_read_registers via proxy: ");
    ASSERT_TRUE(rc == 1, "FAILED (rc %d)\n", rc);
    ASSERT_TRUE(tab_rp_registers[0] == 0x1234,
                "FAILED (%0X != %0X)\n",
                tab_rp_registers[0],
                0x1234);

    /** Test 4: Write and read multiple registers **/
    printf("\nTEST MULTIPLE REGISTERS THROUGH PROXY:\n");

    rc = modbus_write_registers(
        ctx, UT_REGISTERS_ADDRESS, UT_REGISTERS_NB, UT_REGISTERS_TAB);
    printf("1/2 modbus_write_registers via proxy: ");
    ASSERT_TRUE(rc == UT_REGISTERS_NB, "FAILED (rc %d)\n", rc);

    rc = modbus_read_registers(
        ctx, UT_REGISTERS_ADDRESS, UT_REGISTERS_NB, tab_rp_registers);
    printf("2/2 modbus_read_registers via proxy: ");
    ASSERT_TRUE(rc == UT_REGISTERS_NB, "FAILED (rc %d)\n", rc);

    for (i = 0; i < UT_REGISTERS_NB; i++) {
        ASSERT_TRUE(tab_rp_registers[i] == UT_REGISTERS_TAB[i],
                    "FAILED (%0X != %0X)\n",
                    tab_rp_registers[i],
                    UT_REGISTERS_TAB[i]);
    }

    /** Test 5: Read input registers (read-only data set by server) **/
    printf("\nTEST INPUT REGISTERS THROUGH PROXY:\n");

    rc = modbus_read_input_registers(
        ctx, UT_INPUT_REGISTERS_ADDRESS, UT_INPUT_REGISTERS_NB, tab_rp_registers);
    printf("1/1 modbus_read_input_registers via proxy: ");
    ASSERT_TRUE(rc == UT_INPUT_REGISTERS_NB, "FAILED (rc %d)\n", rc);

    for (i = 0; i < UT_INPUT_REGISTERS_NB; i++) {
        ASSERT_TRUE(tab_rp_registers[i] == UT_INPUT_REGISTERS_TAB[i],
                    "FAILED (%0X != %0X)\n",
                    tab_rp_registers[i],
                    UT_INPUT_REGISTERS_TAB[i]);
    }

    /** Test 6: Read discrete inputs **/
    printf("\nTEST DISCRETE INPUTS THROUGH PROXY:\n");

    rc = modbus_read_input_bits(
        ctx, UT_INPUT_BITS_ADDRESS, UT_INPUT_BITS_NB, tab_rp_bits);
    printf("1/1 modbus_read_input_bits via proxy: ");
    ASSERT_TRUE(rc == UT_INPUT_BITS_NB, "FAILED (rc %d)\n", rc);

    i = 0;
    nb_points = UT_INPUT_BITS_NB;
    while (nb_points > 0) {
        int nb_bits = (nb_points > 8) ? 8 : nb_points;
        uint8_t value = modbus_get_byte_from_bits(tab_rp_bits, i * 8, nb_bits);
        ASSERT_TRUE(value == UT_INPUT_BITS_TAB[i],
                    "FAILED (%0X != %0X)\n",
                    value,
                    UT_INPUT_BITS_TAB[i]);
        nb_points -= nb_bits;
        i++;
    }
    printf("OK\n");

    /** Test 7: Write and read registers in a single operation **/
    printf("\nTEST WRITE AND READ REGISTERS THROUGH PROXY:\n");

    /* Reset registers to known values first */
    rc = modbus_write_registers(
        ctx, UT_REGISTERS_ADDRESS, UT_REGISTERS_NB, UT_REGISTERS_TAB);

    memset(tab_rp_registers, 0, UT_REGISTERS_NB * sizeof(uint16_t));
    rc = modbus_write_and_read_registers(ctx,
                                         UT_REGISTERS_ADDRESS + 1,
                                         UT_REGISTERS_NB - 1,
                                         tab_rp_registers,
                                         UT_REGISTERS_ADDRESS,
                                         UT_REGISTERS_NB,
                                         tab_rp_registers);
    printf("1/1 modbus_write_and_read_registers via proxy: ");
    ASSERT_TRUE(rc == UT_REGISTERS_NB, "FAILED (rc %d != %d)\n", rc, UT_REGISTERS_NB);
    ASSERT_TRUE(tab_rp_registers[0] == UT_REGISTERS_TAB[0],
                "FAILED (%0X != %0X)\n",
                tab_rp_registers[0],
                UT_REGISTERS_TAB[0]);
    for (i = 1; i < UT_REGISTERS_NB; i++) {
        ASSERT_TRUE(
            tab_rp_registers[i] == 0, "FAILED (%0X != %0X)\n", tab_rp_registers[i], 0);
    }

    /** Test 8: Mask write register **/
    printf("\nTEST MASK WRITE REGISTER THROUGH PROXY:\n");

    rc = modbus_write_register(ctx, UT_REGISTERS_ADDRESS, 0x12);
    rc = modbus_mask_write_register(ctx, UT_REGISTERS_ADDRESS, 0xF2, 0x25);
    printf("1/1 modbus_mask_write_register via proxy: ");
    ASSERT_TRUE(rc != -1, "FAILED (%x == -1)\n", rc);
    rc = modbus_read_registers(ctx, UT_REGISTERS_ADDRESS, 1, tab_rp_registers);
    ASSERT_TRUE(
        tab_rp_registers[0] == 0x17, "FAILED (%0X != %0X)\n", tab_rp_registers[0], 0x17);

    /** Test 9: Exception forwarding (illegal data address) **/
    printf("\nTEST EXCEPTION FORWARDING THROUGH PROXY:\n");

    rc = modbus_read_registers(ctx, 0, 1, tab_rp_registers);
    printf("1/1 Exception forwarding via proxy: ");
    ASSERT_TRUE(rc == -1 && errno == EMBXILADD, "FAILED (rc=%d, errno=%d)\n", rc, errno);

    printf("\nALL PROXY TESTS PASS WITH SUCCESS.\n");
    success = TRUE;

close:
    free(tab_rp_bits);
    free(tab_rp_registers);
    modbus_close(ctx);
    modbus_free(ctx);

    return (success) ? 0 : -1;
}
