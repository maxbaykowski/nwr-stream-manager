/*
 * NWR Stream Manager's native sample processing.
 *
 * Plain C, needing only the C library, loaded from Python through ctypes like liquid-dsp
 * and libsoxr. 16-bit samples are in the computer's own byte order, which is little-endian
 * everywhere this runs.
 *
 * Written for weak CPUs, such as a Raspberry Pi's: single-precision float wherever it's
 * accurate enough, library calls inside loops only where there's no other way, and loops without branches that the compiler can turn into
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

#include <math.h>
#include <stddef.h>
#include <stdint.h>

/* Raised whenever a function's arguments or results change, so a stale build is refused
 * rather than called wrongly. */
#define NWR_NATIVE_ABI 5

#define NWR_OK 0
#define NWR_ERROR (-1)

/* How nwr_f32_to_s16 converts, combined with |. */
enum {
    NWR_ROUND_TRUNCATE = 1, /* toward zero; without it, to the nearest, halves to even */
    NWR_CLIP_SYMMETRIC = 2  /* clipped to -32767 rather than -32768, as a signal clipped to
                               -1.0 to 1.0, then scaled by 32767, is */
};
#define NWR_F32_TO_S16_FLAGS (NWR_ROUND_TRUNCATE | NWR_CLIP_SYMMETRIC)

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
 * range, then rounded the way flags say. Something that isn't a number becomes silence. */
int nwr_f32_to_s16(const float *restrict in, size_t in_count, int16_t *restrict out, size_t out_count, size_t count,
                   float scale, int flags)
{
    if (count == 0) {
        return NWR_OK;
    }
    if (in == NULL || out == NULL || count > SIZE_MAX / sizeof(float) || in_count < count || out_count < count
        || overlaps(in, count * sizeof(float), out, count * sizeof(int16_t)) || (flags & ~NWR_F32_TO_S16_FLAGS) != 0) {
        return NWR_ERROR;
    }
    const float low = (flags & NWR_CLIP_SYMMETRIC) ? -32767.0f : -32768.0f;
    /* One loop for each way of rounding, so neither has a decision inside it. */
    if ((flags & NWR_ROUND_TRUNCATE) == 0) {
        for (size_t i = 0; i < count; i++) {
            float value = in[i] * scale;
            value = value > 32767.0f ? 32767.0f : value;
            value = value < low ? low : value;
            value = value == value ? value : 0.0f; /* not a number */
            value = (value + NWR_ROUNDING_CONSTANT) - NWR_ROUNDING_CONSTANT;
            out[i] = (int16_t)(int32_t)value;
        }
    } else {
        for (size_t i = 0; i < count; i++) {
            float value = in[i] * scale;
            value = value > 32767.0f ? 32767.0f : value;
            value = value < low ? low : value;
            value = value == value ? value : 0.0f;
            out[i] = (int16_t)(int32_t)value; /* a conversion to a whole number truncates */
        }
    }
    return NWR_OK;
}

/* Float I/Q from -1 to 1 to unsigned 8-bit, the RTL-SDR's own format: count values, clipped
 * to -1 to 1, then 0 to 255 with 127.5 as zero, rounded to the nearest with halves to even.
 * Something that isn't a number becomes zero (128). */
int nwr_f32_to_u8(const float *restrict in, size_t in_count, uint8_t *restrict out, size_t out_count, size_t count)
{
    if (count == 0) {
        return NWR_OK;
    }
    if (in == NULL || out == NULL || count > SIZE_MAX / sizeof(float) || in_count < count || out_count < count
        || overlaps(in, count * sizeof(float), out, count)) {
        return NWR_ERROR;
    }
    for (size_t i = 0; i < count; i++) {
        float value = in[i];
        value = value > 1.0f ? 1.0f : value;
        value = value < -1.0f ? -1.0f : value;
        value = value == value ? value : 0.0f;
        value = value * 127.5f + 127.5f;
        value = (value + NWR_ROUNDING_CONSTANT) - NWR_ROUNDING_CONSTANT;
        out[i] = (uint8_t)(int32_t)value;
    }
    return NWR_OK;
}

/* Interleaved float audio of any number of channels to mono: frames frames of channels
 * samples each, averaged. An average that isn't a finite number (from a sample that's
 * infinite or not a number) becomes silence. */
int nwr_f32_downmix(const float *restrict in, size_t in_count, float *restrict out, size_t out_count, size_t frames,
                    size_t channels)
{
    if (frames == 0) {
        return NWR_OK;
    }
    if (in == NULL || out == NULL || channels == 0 || channels > SIZE_MAX / sizeof(float) / frames
        || in_count < frames * channels || out_count < frames
        || overlaps(in, frames * channels * sizeof(float), out, frames * sizeof(float))) {
        return NWR_ERROR;
    }
    const float divisor = (float)channels;
    for (size_t frame = 0; frame < frames; frame++) {
        const float *samples = in + frame * channels;
        float sum = samples[0];
        for (size_t channel = 1; channel < channels; channel++) {
            sum += samples[channel];
        }
        float mean = sum / divisor;
        out[frame] = mean - mean == 0.0f ? mean : 0.0f; /* infinite or not a number */
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

/* Float audio clipped to low through high: count samples. Something that isn't a number
 * becomes silence. */
int nwr_f32_clip(const float *restrict in, size_t in_count, float *restrict out, size_t out_count, size_t count,
                 float low, float high)
{
    if (count == 0) {
        return NWR_OK;
    }
    if (in == NULL || out == NULL || count > SIZE_MAX / sizeof(float) || in_count < count || out_count < count
        || overlaps(in, count * sizeof(float), out, count * sizeof(float)) || !(low <= high)) {
        return NWR_ERROR;
    }
    for (size_t i = 0; i < count; i++) {
        float value = in[i];
        value = value > high ? high : value;
        value = value < low ? low : value;
        out[i] = value == value ? value : 0.0f;
    }
    return NWR_OK;
}

/* sin(pi x) / (pi x), and 1 at 0. */
static double sinc(double x)
{
    if (x == 0.0) {
        return 1.0;
    }
    double angle = 3.14159265358979323846 * x;
    return sin(angle) / angle;
}

/* The rational resampler's interpolation: count complex samples (interleaved float I then
 * Q) at positions start, start + step, ... in the input, each from the taps input samples
 * around it, weighted by a windowed sinc, the weights made to add up to one. Positions are
 * on the same scale as origin, the position of the first input sample. Input sample center + k - left is weighted for k from 0 to
 * taps - 1, center being the whole part of the position, so every position must have left
 * samples before it and taps - left - 1 after; a call that would read outside the input is
 * refused. */
int nwr_cf32_interpolate(const float *restrict in, size_t in_samples, const float *restrict window, size_t taps,
                         size_t left, double origin, double start, double step, float *restrict out, size_t out_samples,
                         size_t count)
{
    if (count == 0) {
        return NWR_OK;
    }
    if (in == NULL || out == NULL || window == NULL || taps == 0 || taps > 64 || left >= taps
        || in_samples > SIZE_MAX / 2 / sizeof(float) || count > SIZE_MAX / 2 / sizeof(float) || out_samples < count
        || !isfinite(origin) || !isfinite(start) || !isfinite(step) || !(step > 0.0) || start - origin < (double)left
        || overlaps(in, in_samples * 2 * sizeof(float), out, count * 2 * sizeof(float))) {
        return NWR_ERROR;
    }
    /* Positions only grow, so the first and last show whether every one is inside. */
    double last = (start + step * (double)(count - 1)) - origin;
    if (!isfinite(last) || floor(last) + (double)(taps - left) > (double)in_samples) {
        return NWR_ERROR;
    }
    float weights[64];
    for (size_t i = 0; i < count; i++) {
        double position = (start + step * (double)i) - origin;
        double center = floor(position);
        size_t first = (size_t)center - left;
        float total = 0.0f;
        for (size_t k = 0; k < taps; k++) {
            double relative = position - (double)(first + k);
            weights[k] = (float)(sinc(relative) * (double)window[k]);
            total += weights[k];
        }
        float real = 0.0f, imag = 0.0f;
        const float *samples = in + first * 2;
        for (size_t k = 0; k < taps; k++) {
            float weight = weights[k] / total;
            real += samples[k * 2] * weight;
            imag += samples[k * 2 + 1] * weight;
        }
        out[i * 2] = real;
        out[i * 2 + 1] = imag;
    }
    return NWR_OK;
}

/* Output samples worked on at once by nwr_cf32_fir, small enough to stay in the CPU's
 * fastest cache. */
#define NWR_FIR_BLOCK 512

/* Complex samples (interleaved float I then Q) filtered with real taps: count outputs, each
 * out[n] = taps[0] * in[n + taps - 1] + taps[1] * in[n + taps - 2] + ... + taps[taps - 1] *
 * in[n], so the input holds taps - 1 samples of history before the first output's own. Taps
 * are added across a block of outputs at a time, loops the compiler turns into vector
 * instructions. */
int nwr_cf32_fir(const float *restrict in, size_t in_samples, const float *restrict taps, size_t tap_count,
                 float *restrict out, size_t out_samples, size_t count)
{
    if (count == 0) {
        return NWR_OK;
    }
    if (in == NULL || out == NULL || taps == NULL || tap_count == 0 || tap_count > SIZE_MAX / sizeof(float)
        || count > SIZE_MAX / 2 / sizeof(float) - tap_count || out_samples < count
        || in_samples < count + tap_count - 1 || in_samples > SIZE_MAX / 2 / sizeof(float)
        || overlaps(in, in_samples * 2 * sizeof(float), out, count * 2 * sizeof(float))
        || overlaps(taps, tap_count * sizeof(float), out, count * 2 * sizeof(float))) {
        return NWR_ERROR;
    }
    for (size_t first = 0; first < count; first += NWR_FIR_BLOCK) {
        size_t values = 2 * (count - first < NWR_FIR_BLOCK ? count - first : NWR_FIR_BLOCK);
        float *restrict block = out + 2 * first;
        for (size_t j = 0; j < values; j++) {
            block[j] = 0.0f;
        }
        /* Four taps a pass, so each output is loaded and stored a quarter as often. */
        size_t k = 0;
        for (; k + 4 <= tap_count; k += 4) {
            const float t0 = taps[tap_count - 1 - k], t1 = taps[tap_count - 2 - k];
            const float t2 = taps[tap_count - 3 - k], t3 = taps[tap_count - 4 - k];
            const float *restrict source = in + 2 * (first + k);
            for (size_t j = 0; j < values; j++) {
                block[j] += ((t0 * source[j] + t1 * source[j + 2]) + (t2 * source[j + 4] + t3 * source[j + 6]));
            }
        }
        for (; k < tap_count; k++) {
            const float tap = taps[tap_count - 1 - k];
            const float *restrict source = in + 2 * (first + k);
            for (size_t j = 0; j < values; j++) {
                block[j] += tap * source[j];
            }
        }
    }
    return NWR_OK;
}
