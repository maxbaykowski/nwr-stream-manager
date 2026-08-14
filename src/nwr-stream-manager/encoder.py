from __future__ import annotations

import ctypes
import ctypes.util
import random
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from .config import IQ_SAMPLE_RATE, IcecastConfig


class EncoderError(RuntimeError):
    """Raised when an audio encoder cannot be initialized or used."""


class AudioEncoder(Protocol):
    def encode(self, pcm: bytes) -> bytes:
        ...

    def flush(self) -> bytes:
        ...

    def close(self) -> None:
        ...


ogg_int64_t = ctypes.c_int64


class OggPackBuffer(ctypes.Structure):
    _fields_ = [
        ("endbyte", ctypes.c_long),
        ("endbit", ctypes.c_int),
        ("buffer", ctypes.POINTER(ctypes.c_ubyte)),
        ("ptr", ctypes.POINTER(ctypes.c_ubyte)),
        ("storage", ctypes.c_long),
    ]


class OggPage(ctypes.Structure):
    _fields_ = [
        ("header", ctypes.POINTER(ctypes.c_ubyte)),
        ("header_len", ctypes.c_long),
        ("body", ctypes.POINTER(ctypes.c_ubyte)),
        ("body_len", ctypes.c_long),
    ]


class OggStreamState(ctypes.Structure):
    _fields_ = [
        ("body_data", ctypes.POINTER(ctypes.c_ubyte)),
        ("body_storage", ctypes.c_long),
        ("body_fill", ctypes.c_long),
        ("body_returned", ctypes.c_long),
        ("lacing_vals", ctypes.POINTER(ctypes.c_int)),
        ("granule_vals", ctypes.POINTER(ogg_int64_t)),
        ("lacing_storage", ctypes.c_long),
        ("lacing_fill", ctypes.c_long),
        ("lacing_packet", ctypes.c_long),
        ("lacing_returned", ctypes.c_long),
        ("header", ctypes.c_ubyte * 282),
        ("header_fill", ctypes.c_int),
        ("e_o_s", ctypes.c_int),
        ("b_o_s", ctypes.c_int),
        ("serialno", ctypes.c_long),
        ("pageno", ctypes.c_long),
        ("packetno", ogg_int64_t),
        ("granulepos", ogg_int64_t),
    ]


class OggPacket(ctypes.Structure):
    _fields_ = [
        ("packet", ctypes.POINTER(ctypes.c_ubyte)),
        ("bytes", ctypes.c_long),
        ("b_o_s", ctypes.c_long),
        ("e_o_s", ctypes.c_long),
        ("granulepos", ogg_int64_t),
        ("packetno", ogg_int64_t),
    ]


class VorbisInfo(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_int),
        ("channels", ctypes.c_int),
        ("rate", ctypes.c_long),
        ("bitrate_upper", ctypes.c_long),
        ("bitrate_nominal", ctypes.c_long),
        ("bitrate_lower", ctypes.c_long),
        ("bitrate_window", ctypes.c_long),
        ("codec_setup", ctypes.c_void_p),
    ]


class VorbisDspState(ctypes.Structure):
    pass


class VorbisBlock(ctypes.Structure):
    pass


class AllocChain(ctypes.Structure):
    pass


VorbisDspState._fields_ = [
    ("analysisp", ctypes.c_int),
    ("vi", ctypes.POINTER(VorbisInfo)),
    ("pcm", ctypes.POINTER(ctypes.POINTER(ctypes.c_float))),
    ("pcmret", ctypes.POINTER(ctypes.POINTER(ctypes.c_float))),
    ("pcm_storage", ctypes.c_int),
    ("pcm_current", ctypes.c_int),
    ("pcm_returned", ctypes.c_int),
    ("preextrapolate", ctypes.c_int),
    ("eofflag", ctypes.c_int),
    ("lW", ctypes.c_long),
    ("W", ctypes.c_long),
    ("nW", ctypes.c_long),
    ("centerW", ctypes.c_long),
    ("granulepos", ogg_int64_t),
    ("sequence", ogg_int64_t),
    ("glue_bits", ogg_int64_t),
    ("time_bits", ogg_int64_t),
    ("floor_bits", ogg_int64_t),
    ("res_bits", ogg_int64_t),
    ("backend_state", ctypes.c_void_p),
]


AllocChain._fields_ = [
    ("ptr", ctypes.c_void_p),
    ("next", ctypes.POINTER(AllocChain)),
]


VorbisBlock._fields_ = [
    ("pcm", ctypes.POINTER(ctypes.POINTER(ctypes.c_float))),
    ("opb", OggPackBuffer),
    ("lW", ctypes.c_long),
    ("W", ctypes.c_long),
    ("nW", ctypes.c_long),
    ("pcmend", ctypes.c_int),
    ("mode", ctypes.c_int),
    ("eofflag", ctypes.c_int),
    ("granulepos", ogg_int64_t),
    ("sequence", ogg_int64_t),
    ("vd", ctypes.POINTER(VorbisDspState)),
    ("localstore", ctypes.c_void_p),
    ("localtop", ctypes.c_long),
    ("localalloc", ctypes.c_long),
    ("totaluse", ctypes.c_long),
    ("reap", ctypes.POINTER(AllocChain)),
    ("glue_bits", ctypes.c_long),
    ("time_bits", ctypes.c_long),
    ("floor_bits", ctypes.c_long),
    ("res_bits", ctypes.c_long),
    ("internal", ctypes.c_void_p),
]


class VorbisComment(ctypes.Structure):
    _fields_ = [
        ("user_comments", ctypes.POINTER(ctypes.c_char_p)),
        ("comment_lengths", ctypes.POINTER(ctypes.c_int)),
        ("comments", ctypes.c_int),
        ("vendor", ctypes.c_char_p),
    ]


def create_audio_encoder(
    config: IcecastConfig,
    *,
    input_sample_rate: int = IQ_SAMPLE_RATE,
) -> AudioEncoder:
    if config.format == "mp3":
        return Mp3Encoder(config, input_sample_rate=input_sample_rate)
    if config.format == "ogg":
        return OggVorbisEncoder(config, input_sample_rate=input_sample_rate)
    raise EncoderError(f"unsupported icecast format: {config.format}")


class PcmResampler:
    def __init__(self, input_rate: int, output_rate: int) -> None:
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.stream = None
        if input_rate != output_rate:
            try:
                import soxr
            except ImportError as exc:
                raise EncoderError(
                    "sample-rate conversion requires the 'soxr' Python package"
                ) from exc
            self.stream = soxr.ResampleStream(
                input_rate,
                output_rate,
                1,
                dtype="int16",
            )

    def process(self, pcm: bytes) -> bytes:
        if not pcm:
            return b""
        if self.stream is None:
            return pcm
        return self._resample(pcm, last=False)

    def flush(self) -> bytes:
        if self.stream is None:
            return b""
        return self._resample(b"", last=True)

    def _resample(self, pcm: bytes, last: bool) -> bytes:
        samples = np.frombuffer(pcm, dtype="<i2")
        output = self.stream.resample_chunk(samples, last=last)
        return output.astype("<i2", copy=False).tobytes()


@dataclass
class Mp3Encoder:
    config: IcecastConfig
    input_sample_rate: int = IQ_SAMPLE_RATE

    def __post_init__(self) -> None:
        try:
            import lameenc
        except ImportError as exc:
            raise EncoderError(
                "MP3 output requires the 'lameenc' Python package"
            ) from exc

        self.resampler = PcmResampler(self.input_sample_rate, self.config.sample_rate)
        self.header = b""
        self.encoder = lameenc.Encoder()
        self.encoder.set_bit_rate(self.config.bitrate)
        self.encoder.set_in_sample_rate(self.config.sample_rate)
        self.encoder.set_out_sample_rate(self.config.sample_rate)
        self.encoder.set_channels(1)
        self.encoder.set_quality(2)

    def encode(self, pcm: bytes) -> bytes:
        pcm = self.resampler.process(pcm)
        if not pcm:
            return b""
        return bytes(self.encoder.encode(pcm))

    def flush(self) -> bytes:
        output = bytearray()
        pcm = self.resampler.flush()
        if pcm:
            output.extend(self.encoder.encode(pcm))
        output.extend(self.encoder.flush())
        return bytes(output)

    def close(self) -> None:
        pass


def _load_shared_library(name: str, sonames: tuple[str, ...]) -> ctypes.CDLL:
    candidates = [ctypes.util.find_library(name), *sonames]
    errors = []
    for candidate in dict.fromkeys(filter(None, candidates)):
        try:
            return ctypes.CDLL(candidate)
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
    detail = "; ".join(errors) if errors else f"ctypes could not locate {name}"
    raise EncoderError(f"required shared library '{name}' could not be loaded: {detail}")


class OggVorbisEncoder:
    def __init__(
        self,
        config: IcecastConfig,
        *,
        input_sample_rate: int = IQ_SAMPLE_RATE,
    ) -> None:
        self.config = config
        self.input_sample_rate = int(input_sample_rate)
        self.libogg = _load_shared_library("ogg", ("libogg.so.0", "libogg.so"))
        self.libvorbis = _load_shared_library("vorbis", ("libvorbis.so.0", "libvorbis.so"))
        self.libvorbisenc = _load_shared_library(
            "vorbisenc", ("libvorbisenc.so.2", "libvorbisenc.so")
        )
        self.resampler = PcmResampler(self.input_sample_rate, config.sample_rate)
        self.closed = False
        self._configure_ctypes()
        self.vi = VorbisInfo()
        self.vc = VorbisComment()
        self.vd = VorbisDspState()
        self.vb = VorbisBlock()
        self.os = OggStreamState()
        self._init_encoder()

    def _configure_ctypes(self) -> None:
        self.libvorbis.vorbis_info_init.argtypes = [ctypes.POINTER(VorbisInfo)]
        self.libvorbis.vorbis_info_init.restype = None
        self.libvorbis.vorbis_info_clear.argtypes = [ctypes.POINTER(VorbisInfo)]
        self.libvorbis.vorbis_info_clear.restype = None
        self.libvorbis.vorbis_comment_init.argtypes = [ctypes.POINTER(VorbisComment)]
        self.libvorbis.vorbis_comment_init.restype = None
        self.libvorbis.vorbis_comment_add.argtypes = [
            ctypes.POINTER(VorbisComment),
            ctypes.c_char_p,
        ]
        self.libvorbis.vorbis_comment_add.restype = None
        self.libvorbis.vorbis_comment_clear.argtypes = [
            ctypes.POINTER(VorbisComment)
        ]
        self.libvorbis.vorbis_comment_clear.restype = None
        self.libvorbisenc.vorbis_encode_init.argtypes = [
            ctypes.POINTER(VorbisInfo),
            ctypes.c_long,
            ctypes.c_long,
            ctypes.c_long,
            ctypes.c_long,
            ctypes.c_long,
        ]
        self.libvorbisenc.vorbis_encode_init.restype = ctypes.c_int
        self.libvorbis.vorbis_analysis_init.argtypes = [
            ctypes.POINTER(VorbisDspState),
            ctypes.POINTER(VorbisInfo),
        ]
        self.libvorbis.vorbis_analysis_init.restype = ctypes.c_int
        self.libvorbis.vorbis_analysis_buffer.restype = ctypes.POINTER(
            ctypes.POINTER(ctypes.c_float)
        )
        self.libvorbis.vorbis_analysis_wrote.argtypes = [
            ctypes.POINTER(VorbisDspState),
            ctypes.c_int,
        ]
        self.libvorbis.vorbis_analysis_wrote.restype = ctypes.c_int
        self.libvorbis.vorbis_block_init.argtypes = [
            ctypes.POINTER(VorbisDspState),
            ctypes.POINTER(VorbisBlock),
        ]
        self.libvorbis.vorbis_block_init.restype = ctypes.c_int
        self.libvorbis.vorbis_block_clear.argtypes = [ctypes.POINTER(VorbisBlock)]
        self.libvorbis.vorbis_block_clear.restype = ctypes.c_int
        self.libvorbis.vorbis_dsp_clear.argtypes = [
            ctypes.POINTER(VorbisDspState)
        ]
        self.libvorbis.vorbis_dsp_clear.restype = None
        self.libvorbis.vorbis_analysis_headerout.argtypes = [
            ctypes.POINTER(VorbisDspState),
            ctypes.POINTER(VorbisComment),
            ctypes.POINTER(OggPacket),
            ctypes.POINTER(OggPacket),
            ctypes.POINTER(OggPacket),
        ]
        self.libvorbis.vorbis_analysis_headerout.restype = ctypes.c_int
        self.libvorbis.vorbis_analysis_blockout.argtypes = [
            ctypes.POINTER(VorbisDspState),
            ctypes.POINTER(VorbisBlock),
        ]
        self.libvorbis.vorbis_analysis_blockout.restype = ctypes.c_int
        self.libvorbis.vorbis_analysis.argtypes = [
            ctypes.POINTER(VorbisBlock),
            ctypes.POINTER(OggPacket),
        ]
        self.libvorbis.vorbis_analysis.restype = ctypes.c_int
        self.libvorbis.vorbis_bitrate_addblock.argtypes = [
            ctypes.POINTER(VorbisBlock)
        ]
        self.libvorbis.vorbis_bitrate_addblock.restype = ctypes.c_int
        self.libvorbis.vorbis_bitrate_flushpacket.argtypes = [
            ctypes.POINTER(VorbisDspState),
            ctypes.POINTER(OggPacket),
        ]
        self.libvorbis.vorbis_bitrate_flushpacket.restype = ctypes.c_int
        self.libogg.ogg_stream_init.argtypes = [
            ctypes.POINTER(OggStreamState),
            ctypes.c_int,
        ]
        self.libogg.ogg_stream_init.restype = ctypes.c_int
        self.libogg.ogg_stream_packetin.argtypes = [
            ctypes.POINTER(OggStreamState),
            ctypes.POINTER(OggPacket),
        ]
        self.libogg.ogg_stream_packetin.restype = ctypes.c_int
        self.libogg.ogg_stream_pageout.argtypes = [
            ctypes.POINTER(OggStreamState),
            ctypes.POINTER(OggPage),
        ]
        self.libogg.ogg_stream_pageout.restype = ctypes.c_int
        self.libogg.ogg_stream_flush.argtypes = [
            ctypes.POINTER(OggStreamState),
            ctypes.POINTER(OggPage),
        ]
        self.libogg.ogg_stream_flush.restype = ctypes.c_int
        self.libogg.ogg_stream_clear.argtypes = [ctypes.POINTER(OggStreamState)]
        self.libogg.ogg_stream_clear.restype = ctypes.c_int

    def _init_encoder(self) -> None:
        self.libvorbis.vorbis_info_init(ctypes.byref(self.vi))
        result = self.libvorbisenc.vorbis_encode_init(
            ctypes.byref(self.vi),
            1,
            self.config.sample_rate,
            -1,
            self.config.bitrate * 1000,
            -1,
        )
        if result != 0:
            raise EncoderError(
                "vorbis encoder managed-bitrate initialization failed: "
                f"{result} for {self.config.bitrate} Kbps at "
                f"{self.config.sample_rate} Hz"
            )

        self.libvorbis.vorbis_comment_init(ctypes.byref(self.vc))
        self.libvorbis.vorbis_comment_add(
            ctypes.byref(self.vc), b"ENCODER=rtl_weatherband"
        )
        self.libvorbis.vorbis_analysis_init(ctypes.byref(self.vd), ctypes.byref(self.vi))
        self.libvorbis.vorbis_block_init(ctypes.byref(self.vd), ctypes.byref(self.vb))
        self.libogg.ogg_stream_init(ctypes.byref(self.os), random.randint(1, 2**31 - 1))

        header = OggPacket()
        header_comment = OggPacket()
        header_code = OggPacket()
        result = self.libvorbis.vorbis_analysis_headerout(
            ctypes.byref(self.vd),
            ctypes.byref(self.vc),
            ctypes.byref(header),
            ctypes.byref(header_comment),
            ctypes.byref(header_code),
        )
        if result != 0:
            raise EncoderError(f"vorbis header creation failed: {result}")

        for packet in (header, header_comment, header_code):
            self.libogg.ogg_stream_packetin(ctypes.byref(self.os), ctypes.byref(packet))

        self.header = self._flush_pages()

    def encode(self, pcm: bytes) -> bytes:
        pcm = self.resampler.process(pcm)
        if not pcm:
            return b""
        return self._encode_resampled_pcm(pcm)

    def _encode_resampled_pcm(self, pcm: bytes) -> bytes:
        samples = _pcm_s16le_to_float(pcm)
        buffer = self.libvorbis.vorbis_analysis_buffer(
            ctypes.byref(self.vd), len(samples)
        )
        channel = buffer[0]
        channel_view = np.ctypeslib.as_array(channel, shape=(len(samples),))
        channel_view[:] = samples
        self.libvorbis.vorbis_analysis_wrote(ctypes.byref(self.vd), len(samples))
        return self._drain_packets()

    def flush(self) -> bytes:
        if self.closed:
            return b""
        output = bytearray()
        pcm = self.resampler.flush()
        if pcm:
            output.extend(self._encode_resampled_pcm(pcm))
        self.libvorbis.vorbis_analysis_wrote(ctypes.byref(self.vd), 0)
        output.extend(self._drain_packets())
        output += self._flush_pages()
        self.close()
        return bytes(output)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.libogg.ogg_stream_clear(ctypes.byref(self.os))
        self.libvorbis.vorbis_block_clear(ctypes.byref(self.vb))
        self.libvorbis.vorbis_dsp_clear(ctypes.byref(self.vd))
        self.libvorbis.vorbis_comment_clear(ctypes.byref(self.vc))
        self.libvorbis.vorbis_info_clear(ctypes.byref(self.vi))

    def _drain_packets(self) -> bytes:
        output = bytearray()
        packet = OggPacket()
        while self.libvorbis.vorbis_analysis_blockout(
            ctypes.byref(self.vd), ctypes.byref(self.vb)
        ):
            self.libvorbis.vorbis_analysis(ctypes.byref(self.vb), None)
            self.libvorbis.vorbis_bitrate_addblock(ctypes.byref(self.vb))
            while self.libvorbis.vorbis_bitrate_flushpacket(
                ctypes.byref(self.vd), ctypes.byref(packet)
            ):
                self.libogg.ogg_stream_packetin(
                    ctypes.byref(self.os), ctypes.byref(packet)
                )
                output.extend(self._pageout_pages())
        return bytes(output)

    def _pageout_pages(self) -> bytes:
        output = bytearray()
        page = OggPage()
        while self.libogg.ogg_stream_pageout(
            ctypes.byref(self.os), ctypes.byref(page)
        ):
            output.extend(_page_bytes(page))
        return bytes(output)

    def _flush_pages(self) -> bytes:
        output = bytearray()
        page = OggPage()
        while self.libogg.ogg_stream_flush(ctypes.byref(self.os), ctypes.byref(page)):
            output.extend(_page_bytes(page))
        return bytes(output)


def _pcm_s16le_to_float(pcm: bytes) -> NDArray[np.float32]:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    return samples / 32768.0


def _page_bytes(page) -> bytes:
    header = ctypes.string_at(page.header, page.header_len)
    body = ctypes.string_at(page.body, page.body_len)
    return header + body
