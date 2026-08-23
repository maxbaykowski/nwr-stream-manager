from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"


def load_dsp_module():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    return importlib.import_module("nwr_stream_manager.dsp")


class DspTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dsp = load_dsp_module()

    def test_iq_dc_blocker_slowly_tracks_dc_over_about_one_second(self) -> None:
        sample_rate = 1000
        blocker = self.dsp.IqDcBlocker(sample_rate=sample_rate, time_constant_seconds=1.0)
        chunks = [np.ones(50, dtype=np.complex64) for _index in range(20)]

        outputs = [blocker.process(chunk) for chunk in chunks]
        output = np.concatenate(outputs)

        self.assertGreater(abs(output[0]), 0.99)
        self.assertGreater(abs(output[-1]), 0.36)
        self.assertLess(abs(output[-1]), 0.40)

    def test_iq_dc_blocker_is_sample_rate_aware(self) -> None:
        slow = self.dsp.IqDcBlocker(sample_rate=1000, time_constant_seconds=1.0)
        fast = self.dsp.IqDcBlocker(sample_rate=2000, time_constant_seconds=1.0)

        slow_output = np.concatenate(
            [slow.process(np.ones(50, dtype=np.complex64)) for _index in range(20)]
        )
        fast_output = np.concatenate(
            [fast.process(np.ones(50, dtype=np.complex64)) for _index in range(20)]
        )

        self.assertLess(abs(slow_output[-1]), abs(fast_output[-1]))

    def test_iq_dc_blocker_preserves_audio_rate_offset_energy(self) -> None:
        sample_rate = 240_000
        seconds = 0.1
        count = round(sample_rate * seconds)
        time_axis = np.arange(count, dtype=np.float32) / sample_rate
        tone = np.exp(1j * 2.0 * np.pi * 1000.0 * time_axis).astype(np.complex64)
        blocker = self.dsp.IqDcBlocker(sample_rate=sample_rate, time_constant_seconds=1.0)

        output = blocker.process(tone)

        self.assertGreater(float(np.mean(np.abs(output[-1000:]))), 0.99)

    def test_integer_decimator_matches_filter_then_downsample_reference(self) -> None:
        rng = np.random.default_rng(123)
        decimator = self.dsp.IntegerDecimator.create(
            96_000,
            24_000,
            transition_hz=4_000,
            attenuation_db=60,
        )
        reference_filter = self.dsp.FirFilter(decimator.fir.taps.copy())
        reference_seen = 0
        actual_parts = []
        expected_parts = []

        for size in (17, 301, 409):
            samples = (
                rng.normal(size=size) + 1j * rng.normal(size=size)
            ).astype(np.complex64)
            actual_parts.append(decimator.process(samples))
            filtered = reference_filter.process(samples)
            offset = (-reference_seen) % decimator.factor
            expected_parts.append(filtered[offset :: decimator.factor])
            reference_seen += int(filtered.size)

        actual = np.concatenate(actual_parts)
        expected = np.concatenate(expected_parts)
        np.testing.assert_allclose(actual, expected, rtol=1e-5, atol=1e-5)

    def test_rational_resampler_chunking_matches_single_pass(self) -> None:
        rng = np.random.default_rng(456)
        resampler = self.dsp.RationalResampler(
            100_000,
            24_000,
            transition_hz=4_000,
            attenuation_db=60,
        )
        single_pass = self.dsp.RationalResampler(
            100_000,
            24_000,
            transition_hz=4_000,
            attenuation_db=60,
        )
        samples = (
            rng.normal(size=861) + 1j * rng.normal(size=861)
        ).astype(np.complex64)
        actual_parts = []
        offset = 0
        for size in (101, 503, 257):
            actual_parts.append(resampler.process(samples[offset : offset + size]))
            offset += size

        actual = np.concatenate(actual_parts)
        expected = single_pass.process(samples)
        count = min(actual.size, expected.size)
        self.assertGreater(count, 100)
        np.testing.assert_allclose(actual[:count], expected[:count], rtol=1e-5, atol=1e-5)

    def test_create_decimator_uses_staged_numpy_decimator_for_high_rates(self) -> None:
        decimator = self.dsp.create_decimator(1_024_000, 24_000)

        self.assertIsInstance(decimator, self.dsp.StagedDecimator)
        self.assertIsInstance(decimator.first_stage, self.dsp.IntegerDecimator)
        self.assertIsInstance(decimator.final_stage, self.dsp.RationalResampler)
        self.assertGreaterEqual(decimator.intermediate_rate, self.dsp.STAGED_DECIMATOR_MIN_INTERMEDIATE_RATE)
        self.assertLessEqual(decimator.intermediate_rate, self.dsp.STAGED_DECIMATOR_MAX_INTERMEDIATE_RATE)

    def test_fixed_rtl_rate_uses_integer_staged_decimation(self) -> None:
        decimator = self.dsp.create_decimator(1_536_000, 24_000)

        self.assertIsInstance(decimator, self.dsp.StagedDecimator)
        self.assertIsInstance(decimator.first_stage, self.dsp.IntegerDecimator)
        self.assertIsInstance(decimator.final_stage, self.dsp.IntegerDecimator)
        self.assertEqual(decimator.intermediate_rate % 24_000, 0)
        self.assertTrue(decimator.is_integer_decimation)

    def test_wide_spectrum_decimators_do_not_use_narrowband_staging(self) -> None:
        for output_rate in (192_000, 256_000, 384_000, 512_000, 768_000, 1_024_000):
            with self.subTest(output_rate=output_rate):
                decimator = self.dsp.create_decimator(1_536_000, output_rate)
                self.assertNotIsInstance(decimator, self.dsp.StagedDecimator)
                taps = getattr(getattr(decimator, "fir", None), "taps", None)
                self.assertIsNotNone(taps)
                self.assertLess(taps.size, 1_200)
                samples = np.ones(4096, dtype=np.complex64)
                output = decimator.process(samples)
                self.assertGreater(output.size, 0)

    def test_high_ratio_fractional_wide_decimator_uses_staged_path(self) -> None:
        decimator = self.dsp.create_decimator(2_980_000, 192_000)

        self.assertIsInstance(decimator, self.dsp.StagedDecimator)
        self.assertIsInstance(decimator.first_stage, self.dsp.IntegerDecimator)
        self.assertIsInstance(decimator.final_stage, self.dsp.RationalResampler)
        self.assertGreaterEqual(decimator.intermediate_rate, 192_000)
        self.assertLessEqual(decimator.intermediate_rate, 288_000)

    def test_high_ratio_fractional_wide_decimator_preserves_nwr_sidebands(self) -> None:
        sample_rate = 2_980_000
        output_rate = 192_000
        count = round(sample_rate * 0.08)
        time_axis = np.arange(count, dtype=np.float32) / sample_rate
        edge_sideband = np.exp(1j * 2.0 * np.pi * 87_000.0 * time_axis).astype(np.complex64)
        out_of_band = np.exp(1j * 2.0 * np.pi * 150_000.0 * time_axis).astype(np.complex64)

        edge_output = self.dsp.create_decimator(sample_rate, output_rate).process(edge_sideband)
        rejected_output = self.dsp.create_decimator(sample_rate, output_rate).process(out_of_band)

        self.assertGreater(float(np.mean(np.abs(edge_output[-4096:]))), 0.55)
        self.assertLess(float(np.mean(np.abs(rejected_output[-4096:]))), 0.08)

    def test_shared_intermediate_decimator_preserves_outer_nwr_sidebands(self) -> None:
        sample_rate = 1_536_000
        output_rate = 192_000
        count = round(sample_rate * 0.1)
        time_axis = np.arange(count, dtype=np.float32) / sample_rate
        edge_sideband = np.exp(1j * 2.0 * np.pi * 87_000.0 * time_axis).astype(np.complex64)
        out_of_band = np.exp(1j * 2.0 * np.pi * 120_000.0 * time_axis).astype(np.complex64)

        edge_output = self.dsp.create_decimator(sample_rate, output_rate).process(edge_sideband)
        rejected_output = self.dsp.create_decimator(sample_rate, output_rate).process(out_of_band)

        self.assertGreater(float(np.mean(np.abs(edge_output[-4096:]))), 0.65)
        self.assertLess(float(np.mean(np.abs(rejected_output[-4096:]))), 0.05)

    def test_channel_decimator_preserves_wide_nwr_fm_sideband(self) -> None:
        sample_rate = 192_000
        output_rate = 24_000
        count = round(sample_rate * 0.1)
        time_axis = np.arange(count, dtype=np.float32) / sample_rate
        desired_sideband = np.exp(1j * 2.0 * np.pi * 11_000.0 * time_axis).astype(np.complex64)
        rejected = np.exp(1j * 2.0 * np.pi * 16_000.0 * time_axis).astype(np.complex64)

        desired_output = self.dsp.create_decimator(
            sample_rate,
            output_rate,
            transition_hz=1_000,
        ).process(desired_sideband)
        rejected_output = self.dsp.create_decimator(
            sample_rate,
            output_rate,
            transition_hz=1_000,
        ).process(rejected)

        self.assertGreater(float(np.mean(np.abs(desired_output[-2048:]))), 0.65)
        self.assertLess(float(np.mean(np.abs(rejected_output[-2048:]))), 0.05)

    def test_channelizer_alias_filter_update_retunes_existing_decimator(self) -> None:
        channelizer = self.dsp.IqChannelizer(
            input_rate=192_000,
            center_frequency_hz=162_475_000,
            target_frequency_hz=162_475_000,
            output_rate=24_000,
            transition_hz=1_000,
        )
        decimator = channelizer.decimator
        fir = decimator.fir
        original_taps = fir.taps.copy()

        channelizer.update_alias_filter(transition_hz=4_000)

        self.assertIs(channelizer.decimator, decimator)
        self.assertIs(decimator.fir, fir)
        self.assertFalse(np.array_equal(fir.taps, original_taps))

    def test_channelizer_target_frequency_update_keeps_existing_dsp_stages(self) -> None:
        channelizer = self.dsp.IqChannelizer(
            input_rate=192_000,
            center_frequency_hz=162_475_000,
            target_frequency_hz=162_475_000,
            output_rate=24_000,
        )
        shifter = channelizer.shifter
        decimator = channelizer.decimator
        shifter._phase = 1.25

        channelizer.set_target_frequency(162_550_000)

        self.assertIs(channelizer.shifter, shifter)
        self.assertIs(channelizer.decimator, decimator)
        self.assertEqual(channelizer.target_frequency_hz, 162_550_000)
        self.assertEqual(channelizer.shifter.offset_hz, -75_000.0)
        self.assertEqual(channelizer.shifter._phase, 1.25)

    def test_identity_decimator_for_matching_spectrum_rate(self) -> None:
        decimator = self.dsp.create_decimator(1_536_000, 1_536_000)

        self.assertIsInstance(decimator, self.dsp.IdentityDecimator)
        samples = np.array([1 + 2j, 3 + 4j], dtype=np.complex64)
        np.testing.assert_array_equal(decimator.process(samples), samples)

    def test_staged_decimator_preserves_baseband_and_rejects_out_of_band_aliases(self) -> None:
        sample_rate = 1_024_000
        count = round(sample_rate * 0.25)
        time_axis = np.arange(count, dtype=np.float32) / sample_rate

        desired = np.exp(1j * 2.0 * np.pi * 1_000.0 * time_axis).astype(np.complex64)
        adjacent = np.exp(1j * 2.0 * np.pi * 100_000.0 * time_axis).astype(np.complex64)

        desired_output = self.dsp.create_decimator(sample_rate, 24_000).process(desired)
        adjacent_output = self.dsp.create_decimator(sample_rate, 24_000).process(adjacent)

        self.assertGreater(float(np.mean(np.abs(desired_output[-1000:]))), 0.5)
        self.assertLess(float(np.mean(np.abs(adjacent_output[-1000:]))), 0.05)

    def test_channelizer_shift_scales_with_fixed_rtl_sample_rate(self) -> None:
        sample_rate = 1_536_000
        count = round(sample_rate * 0.1)
        time_axis = np.arange(count, dtype=np.float32) / sample_rate
        rf_offset_hz = -50_000.0
        samples = np.exp(1j * 2.0 * np.pi * rf_offset_hz * time_axis).astype(np.complex64)
        channelizer = self.dsp.IqChannelizer(
            input_rate=sample_rate,
            center_frequency_hz=162_475_000,
            target_frequency_hz=162_425_000,
        )

        output = channelizer.process_complex(samples)
        settled = output[-2048:]
        spectrum = np.fft.fftshift(np.fft.fft(settled))
        frequencies = np.fft.fftshift(np.fft.fftfreq(settled.size, d=1 / 24_000))
        peak_frequency = float(frequencies[int(np.argmax(np.abs(spectrum)))])

        self.assertLess(abs(peak_frequency), 25.0)
        self.assertGreater(float(np.mean(np.abs(settled))), 0.5)

    def test_full_nwr_channelizer_path_recovers_nfm_audio_on_all_channels(self) -> None:
        nwr_channels = (
            162_400_000,
            162_425_000,
            162_450_000,
            162_475_000,
            162_500_000,
            162_525_000,
            162_550_000,
        )
        sample_rate = 1_536_000
        center_frequency_hz = 162_475_000
        count = round(sample_rate * 0.15)
        time_axis = np.arange(count, dtype=np.float64) / sample_rate
        audio_frequency_hz = 1_000.0
        deviation_hz = 3_500.0

        for target_frequency_hz in nwr_channels:
            with self.subTest(target_frequency_hz=target_frequency_hz):
                rf_offset_hz = float(target_frequency_hz - center_frequency_hz)
                phase = (
                    2.0 * np.pi * rf_offset_hz * time_axis
                    + (deviation_hz / audio_frequency_hz)
                    * np.sin(2.0 * np.pi * audio_frequency_hz * time_axis)
                )
                samples = np.exp(1j * phase).astype(np.complex64)
                dc_blocker = self.dsp.IqDcBlocker(sample_rate=sample_rate)
                channelizer = self.dsp.IqChannelizer(
                    input_rate=sample_rate,
                    center_frequency_hz=center_frequency_hz,
                    target_frequency_hz=target_frequency_hz,
                )
                demodulated_iq = channelizer.process_complex(dc_blocker.process(samples))
                previous = demodulated_iq[:-1]
                current = demodulated_iq[1:]
                audio = (np.angle(current * np.conj(previous)) / np.pi * 1.5).astype(np.float32)
                settled = audio[-2048:]
                spectrum = np.fft.rfft(settled * np.hanning(settled.size))
                frequencies = np.fft.rfftfreq(settled.size, d=1 / 24_000)
                peak_frequency = float(frequencies[int(np.argmax(np.abs(spectrum[1:])) + 1)])

                self.assertLess(abs(peak_frequency - audio_frequency_hz), 25.0)
                self.assertGreater(float(np.sqrt(np.mean(settled * settled))), 0.05)


if __name__ == "__main__":
    unittest.main()
