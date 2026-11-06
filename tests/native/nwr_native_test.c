/*
 * Checks native/nwr_native.c from C, for what only matters in C: lengths, null pointers,
 * overlapping buffers, sizes that would overflow, and exact results. Built and run by
 * tests/test_native.py, under AddressSanitizer and UndefinedBehaviorSanitizer when they're
 * installed, with buffers allocated to their exact size so a read or write one past the end
 * is caught.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int nwr_native_abi(void);
int nwr_u8_iq_to_f32(const uint8_t *in, size_t in_bytes, float *out, size_t out_floats, size_t pairs);
int nwr_f32_to_s16(const float *in, size_t in_count, int16_t *out, size_t out_count, size_t count, float scale,
                   int flags);
int nwr_f32_to_u8(const float *in, size_t in_count, uint8_t *out, size_t out_count, size_t count);
int nwr_f32_downmix(const float *in, size_t in_count, float *out, size_t out_count, size_t frames, size_t channels);
int nwr_s16_to_f32(const int16_t *in, size_t in_count, float *out, size_t out_count, size_t count, float scale);

static int failures = 0;

#define CHECK(condition)                                                                    \
    do {                                                                                    \
        if (!(condition)) {                                                                 \
            fprintf(stderr, "%s:%d: failed: %s\n", __FILE__, __LINE__, #condition);         \
            failures++;                                                                     \
        }                                                                                   \
    } while (0)

static void *exactly(size_t bytes)
{
    void *memory = malloc(bytes);
    if (memory == NULL) {
        fprintf(stderr, "out of memory\n");
        exit(2);
    }
    return memory;
}

static void test_rtl_conversion(void)
{
    uint8_t *in = exactly(512);
    float *out = exactly(512 * sizeof(float));
    for (int i = 0; i < 512; i++) {
        in[i] = (uint8_t)(i & 0xff);
    }
    CHECK(nwr_u8_iq_to_f32(in, 512, out, 512, 256) == 0);
    for (int i = 0; i < 512; i++) {
        CHECK(out[i] == ((float)in[i] - 127.5f) / 127.5f);
    }
    CHECK(out[0] == -1.0f && out[255] == 1.0f);

    /* Refused, writing nothing: too little input or output, null, overlapping, overflowing. */
    memset(out, 0x7f, 512 * sizeof(float));
    CHECK(nwr_u8_iq_to_f32(in, 511, out, 512, 256) == -1);
    CHECK(nwr_u8_iq_to_f32(in, 512, out, 511, 256) == -1);
    CHECK(nwr_u8_iq_to_f32(NULL, 512, out, 512, 256) == -1);
    CHECK(nwr_u8_iq_to_f32(in, 512, NULL, 512, 256) == -1);
    CHECK(nwr_u8_iq_to_f32((const uint8_t *)out, 512 * sizeof(float), out, 512, 64) == -1);
    CHECK(nwr_u8_iq_to_f32(in, SIZE_MAX, out, SIZE_MAX, SIZE_MAX / 2 + 1) == -1);
    CHECK(nwr_u8_iq_to_f32(in, SIZE_MAX, out, SIZE_MAX, SIZE_MAX / 8 + 1) == -1);
    for (int i = 0; i < 512; i++) {
        uint32_t bits;
        memcpy(&bits, &out[i], sizeof(bits));
        CHECK(bits == 0x7f7f7f7fu);
    }
    /* Nothing to do is fine, even with no buffers. */
    CHECK(nwr_u8_iq_to_f32(NULL, 0, NULL, 0, 0) == 0);
    free(in);
    free(out);
}

static void test_float_to_16_bit(void)
{
    /* Every quarter step across, and past, the 16-bit range, halves included. */
    size_t count = 0;
    for (float value = -40000.0f; value <= 40000.0f; value += 0.25f) {
        count++;
    }
    float *in = exactly(count * sizeof(float));
    int16_t *nearest = exactly(count * sizeof(int16_t));
    int16_t *truncated = exactly(count * sizeof(int16_t));
    int16_t *symmetric = exactly(count * sizeof(int16_t));
    size_t i = 0;
    for (float value = -40000.0f; value <= 40000.0f; value += 0.25f) {
        in[i++] = value;
    }
    CHECK(nwr_f32_to_s16(in, count, nearest, count, count, 1.0f, 0) == 0);
    CHECK(nwr_f32_to_s16(in, count, truncated, count, count, 1.0f, 1) == 0);
    CHECK(nwr_f32_to_s16(in, count, symmetric, count, count, 1.0f, 2) == 0);
    for (i = 0; i < count; i++) {
        float clipped = fminf(fmaxf(in[i], -32768.0f), 32767.0f);
        CHECK(nearest[i] == (int16_t)nearbyintf(clipped));
        CHECK(truncated[i] == (int16_t)truncf(clipped));
        CHECK(symmetric[i] == (int16_t)nearbyintf(fmaxf(clipped, -32767.0f)));
    }

    float special[4] = {NAN, INFINITY, -INFINITY, -NAN};
    int16_t out[4] = {1, 1, 1, 1};
    CHECK(nwr_f32_to_s16(special, 4, out, 4, 4, 32767.0f, 0) == 0);
    CHECK(out[0] == 0 && out[1] == 32767 && out[2] == -32768 && out[3] == 0);

    /* Refused, writing nothing. */
    int16_t untouched[4] = {7, 7, 7, 7};
    CHECK(nwr_f32_to_s16(special, 3, untouched, 4, 4, 1.0f, 0) == -1);
    CHECK(nwr_f32_to_s16(special, 4, untouched, 3, 4, 1.0f, 0) == -1);
    CHECK(nwr_f32_to_s16(special, 4, untouched, 4, 4, 1.0f, 4) == -1);
    CHECK(nwr_f32_to_s16(NULL, 4, untouched, 4, 4, 1.0f, 0) == -1);
    CHECK(nwr_f32_to_s16(special, SIZE_MAX, untouched, SIZE_MAX, SIZE_MAX / 2, 1.0f, 0) == -1);
    CHECK(nwr_f32_to_s16((const float *)untouched, 2, untouched, 4, 2, 1.0f, 0) == -1);
    CHECK(untouched[0] == 7 && untouched[1] == 7 && untouched[2] == 7 && untouched[3] == 7);
    free(in);
    free(nearest);
    free(truncated);
    free(symmetric);
}

static void test_float_to_8_bit(void)
{
    /* Every 1/1024 across, and past, -1 to 1. */
    size_t count = 4097;
    float *in = exactly(count * sizeof(float));
    uint8_t *out = exactly(count);
    for (size_t i = 0; i < count; i++) {
        in[i] = (float)i / 1024.0f - 2.0f;
    }
    CHECK(nwr_f32_to_u8(in, count, out, count, count) == 0);
    for (size_t i = 0; i < count; i++) {
        float clipped = fminf(fmaxf(in[i], -1.0f), 1.0f);
        CHECK(out[i] == (uint8_t)nearbyintf(clipped * 127.5f + 127.5f));
    }
    CHECK(out[0] == 0 && out[count - 1] == 255);

    float special[3] = {NAN, INFINITY, -INFINITY};
    uint8_t converted[3];
    CHECK(nwr_f32_to_u8(special, 3, converted, 3, 3) == 0);
    CHECK(converted[0] == 128 && converted[1] == 255 && converted[2] == 0);

    uint8_t untouched[4] = {7, 7, 7, 7};
    CHECK(nwr_f32_to_u8(in, 3, untouched, 4, 4) == -1);
    CHECK(nwr_f32_to_u8(in, 4, untouched, 3, 4) == -1);
    CHECK(nwr_f32_to_u8(NULL, 4, untouched, 4, 4) == -1);
    CHECK(nwr_f32_to_u8(in, SIZE_MAX, untouched, SIZE_MAX, SIZE_MAX / 2) == -1);
    CHECK(nwr_f32_to_u8((const float *)untouched, 1, untouched, 4, 1) == -1);
    CHECK(untouched[0] == 7 && untouched[1] == 7 && untouched[2] == 7 && untouched[3] == 7);
    free(in);
    free(out);
}

static void test_downmix(void)
{
    float *stereo = exactly(6 * sizeof(float));
    float *mono = exactly(3 * sizeof(float));
    const float values[6] = {0.5f, 0.25f, -1.0f, 1.0f, INFINITY, 0.5f};
    memcpy(stereo, values, sizeof(values));
    CHECK(nwr_f32_downmix(stereo, 6, mono, 3, 3, 2) == 0);
    CHECK(mono[0] == 0.375f && mono[1] == 0.0f && mono[2] == 0.0f);

    /* One channel is a copy, apart from what isn't a finite number. */
    CHECK(nwr_f32_downmix(stereo, 6, mono, 3, 3, 1) == 0);
    CHECK(mono[0] == 0.5f && mono[1] == 0.25f && mono[2] == -1.0f);

    float untouched[3] = {7.0f, 7.0f, 7.0f};
    CHECK(nwr_f32_downmix(stereo, 5, untouched, 3, 3, 2) == -1);
    CHECK(nwr_f32_downmix(stereo, 6, untouched, 2, 3, 2) == -1);
    CHECK(nwr_f32_downmix(stereo, 6, untouched, 3, 3, 0) == -1);
    CHECK(nwr_f32_downmix(NULL, 6, untouched, 3, 3, 2) == -1);
    CHECK(nwr_f32_downmix(stereo, SIZE_MAX, untouched, SIZE_MAX, SIZE_MAX / 4, 8) == -1);
    CHECK(nwr_f32_downmix(untouched, 3, untouched, 3, 3, 1) == -1);
    CHECK(untouched[0] == 7.0f && untouched[1] == 7.0f && untouched[2] == 7.0f);
    free(stereo);
    free(mono);
}

static void test_16_bit_to_float(void)
{
    int16_t *in = exactly(65536 * sizeof(int16_t));
    float *out = exactly(65536 * sizeof(float));
    for (int i = 0; i < 65536; i++) {
        in[i] = (int16_t)(i - 32768);
    }
    CHECK(nwr_s16_to_f32(in, 65536, out, 65536, 65536, 32768.0f) == 0);
    for (int i = 0; i < 65536; i++) {
        CHECK(out[i] == (float)in[i] / 32768.0f);
    }
    CHECK(out[0] == -1.0f);

    float untouched[2] = {7.0f, 7.0f};
    CHECK(nwr_s16_to_f32(in, 2, untouched, 2, 2, 0.0f) == -1);
    CHECK(nwr_s16_to_f32(in, 2, untouched, 2, 2, NAN) == -1);
    CHECK(nwr_s16_to_f32(in, 1, untouched, 2, 2, 1.0f) == -1);
    CHECK(nwr_s16_to_f32(in, 2, untouched, 1, 2, 1.0f) == -1);
    CHECK(untouched[0] == 7.0f && untouched[1] == 7.0f);
    free(in);
    free(out);
}

int main(void)
{
    CHECK(nwr_native_abi() == 3);
    test_rtl_conversion();
    test_float_to_16_bit();
    test_16_bit_to_float();
    test_float_to_8_bit();
    test_downmix();
    if (failures) {
        fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }
    printf("all native checks passed\n");
    return 0;
}
