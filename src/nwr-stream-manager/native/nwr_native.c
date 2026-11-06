/*
 * NWR Stream Manager's native sample processing.
 *
 * Plain C, needing only the C library, loaded from Python through ctypes like liquid-dsp
 * and libsoxr. 16-bit samples are in the computer's own byte order, which is little-endian
 * everywhere this runs.
 *
 * Written for weak CPUs, such as a Raspberry Pi's: single-precision float throughout, no
 * library calls inside loops, and loops without branches that the compiler can turn into
 * vector instructions where the CPU has them.
 *
 * Written to be hard to misuse: every function is told how big each buffer is and checks
 * it here, even though the Python side checks too; sizes are checked before they're
 * multiplied, so they can't overflow; an input and output that overlap are refused. A
 * function that refuses returns NWR_ERROR and writes nothing.
 *
 * Memory: nothing here allocates or frees anything. Every function writes only into the
 * buffers it's given, which Python owns and frees.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <stddef.h>
#include <stdint.h>

/* Raised whenever a function's arguments or results change, so a stale build is refused
 * rather than called wrongly. */
#define NWR_NATIVE_ABI 2

#define NWR_OK 0
#define NWR_ERROR (-1)

enum {
    NWR_ROUND_NEAREST = 0, /* to the nearest, halves to even */
    NWR_ROUND_TRUNCATE = 1 /* toward zero */
};

/* 1.5 * 2^23. Adding then subtracting it rounds a float smaller than 2^22 to a whole number,
 * to the nearest with halves to even, in plain float arithmetic: the same as rint(), but
 * without a library call, so it works in vector registers on every CPU. */
#define NWR_ROUNDING_CONSTANT 12582912.0f

int nwr_native_abi(void)
{
    return NWR_NATIVE_ABI;
}

/* Whether two buffers share any memory. */
static int overlaps(const void *a, size_t a_bytes, const void *b, size_t b_bytes)
{
    uintptr_t a_start = (uintptr_t)a, b_start = (uintptr_t)b;
    return a_start < b_start + b_bytes && b_start < a_start + a_bytes;
}

/* RTL-SDR samples, unsigned 8-bit I then Q, to interleaved float I/Q from -1 to 1: pairs
 * samples, from in_bytes of input into out_floats of output. */
int nwr_u8_iq_to_f32(const uint8_t *restrict in, size_t in_bytes, float *restrict out, size_t out_floats, size_t pairs)
{
    if (pairs == 0) {
        return NWR_OK;
    }
    if (in == NULL || out == NULL || pairs > SIZE_MAX / 2 / sizeof(float)) {
        return NWR_ERROR;
    }
    size_t values = pairs * 2;
    if (in_bytes < values || out_floats < values || overlaps(in, values, out, values * sizeof(float))) {
        return NWR_ERROR;
    }
    for (size_t i = 0; i < values; i++) {
        out[i] = ((float)in[i] - 127.5f) / 127.5f;
    }
    return NWR_OK;
}

/* Float audio to signed 16-bit: count samples, multiplied by scale, clipped to the 16-bit
 * range, then rounded the given way. Something that isn't a number becomes silence. */
int nwr_f32_to_s16(const float *restrict in, size_t in_count, int16_t *restrict out, size_t out_count, size_t count,
                   float scale, int rounding)
{
    if (count == 0) {
        return NWR_OK;
    }
    if (in == NULL || out == NULL || count > SIZE_MAX / sizeof(float) || in_count < count || out_count < count
        || overlaps(in, count * sizeof(float), out, count * sizeof(int16_t))
        || (rounding != NWR_ROUND_NEAREST && rounding != NWR_ROUND_TRUNCATE)) {
        return NWR_ERROR;
    }
    /* One loop for each way of rounding, so neither has a decision inside it. */
    if (rounding == NWR_ROUND_NEAREST) {
        for (size_t i = 0; i < count; i++) {
            float value = in[i] * scale;
            value = value > 32767.0f ? 32767.0f : value;
            value = value < -32768.0f ? -32768.0f : value;
            value = value == value ? value : 0.0f; /* not a number */
            value = (value + NWR_ROUNDING_CONSTANT) - NWR_ROUNDING_CONSTANT;
            out[i] = (int16_t)(int32_t)value;
        }
    } else {
        for (size_t i = 0; i < count; i++) {
            float value = in[i] * scale;
            value = value > 32767.0f ? 32767.0f : value;
            value = value < -32768.0f ? -32768.0f : value;
            value = value == value ? value : 0.0f;
            out[i] = (int16_t)(int32_t)value; /* a conversion to a whole number truncates */
        }
    }
    return NWR_OK;
}

/* Signed 16-bit audio to float: count samples, divided by scale. */
int nwr_s16_to_f32(const int16_t *restrict in, size_t in_count, float *restrict out, size_t out_count, size_t count,
                   float scale)
{
    if (count == 0) {
        return NWR_OK;
    }
    if (in == NULL || out == NULL || count > SIZE_MAX / sizeof(float) || in_count < count || out_count < count
        || overlaps(in, count * sizeof(int16_t), out, count * sizeof(float))
        || scale != scale /* not a number */ || scale == 0.0f) {
        return NWR_ERROR;
    }
    for (size_t i = 0; i < count; i++) {
        out[i] = (float)in[i] / scale;
    }
    return NWR_OK;
}
