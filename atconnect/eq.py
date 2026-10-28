"""Realtek DSP equalizer coefficient generator.

A Python port of the peaking-filter path of `calculateEq` from Realtek's
libDspConfig (used by the official app through `DspConfig.calculateEqNative`).
The output is the `eqData` blob the headphones expect for one sample rate:

    u32  (SignedQuant(global_gain * prod(stage scales), 24, 12) << 8) | 0x40 | stages
    then per stage: b0 b1 b2 a1 a2 as signed Q2.30 integers (little endian)

The filter is the "simple" peaking design (Orfanidis), not the RBJ cookbook one.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

TWO_PI = 6.283185307179586
Q30 = 30


def _c_round(x: float) -> float:
    """C round(): halfway cases away from zero."""
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


def _signed_quant(x: float, bits: int, frac: int) -> int:
    if frac >= bits:
        frac = bits - 1
    v = _c_round(x * math.ldexp(1.0, frac))
    bits = min(bits, 32)
    limit = math.ldexp(1.0, bits - 1)
    if v >= limit:
        v = limit - 1
    elif v <= -limit:
        v = -limit
    return int(v)


def _q30_round(x: float) -> float:
    scale = math.ldexp(1.0, Q30)
    return _c_round(x * scale) / scale


def db_to_linear(db: float) -> float:
    return math.pow(10.0, db / 20.0)


@dataclass(frozen=True)
class Biquad:
    scale: float
    b0: float
    b1: float
    b2: float
    a1: float
    a2: float


def peak_filter(gain_db: float, freq: float, q: float, fs: float) -> Biquad:
    """Mirror of libDspConfig PeakFilter() followed by FilterCoefFix(…, 30)."""
    g = db_to_linear(gain_db)
    w0 = (freq * TWO_PI) / fs
    k = g + g if g < 1.0 else 2.0
    t = w0 / (k * q)
    beta = ((1.0 - t) * -0.5) / (t + 1.0)
    gm1 = g + -1.0
    half_minus_beta = 0.5 - beta
    d12 = 0.25 + beta * 0.5
    c = math.cos(w0)
    d11 = 0.5 + gm1 * d12
    d18 = (-gm1) * d12 - beta
    b1 = (c * half_minus_beta) * -2.0
    a2 = beta * -2.0
    b0 = d11 + d11
    b2 = d18 + d18
    a1 = b1
    scale = 1.0
    if abs(b1) >= 2.0 or abs(b0) >= 2.0 or abs(b2) >= 2.0:
        scale = abs(b0)
        if abs(b1) >= scale:
            scale = abs(b1)
        if abs(b2) >= scale:
            scale = abs(b2)
        b0, b1, b2 = b0 / scale, b1 / scale, b2 / scale
    return Biquad(scale, b0, _q30_round(b1), _q30_round(b2), _q30_round(a1), _q30_round(a2))


def calculate_eq(
    fs: float,
    global_gain_db: float,
    gains_db: list[float],
    freqs: list[float],
    qs: list[float],
) -> bytes:
    """eqData for one sample rate (equivalent to calculateEq()[12:] in the SDK)."""
    stages = len(gains_db)
    if not 1 <= stages <= 10 or len(freqs) != stages or len(qs) != stages:
        raise ValueError("gains, freqs and qs must have the same length (1..10)")
    gain = db_to_linear(global_gain_db)
    filters = []
    for g, f, q in zip(gains_db, freqs, qs):
        bq = peak_filter(g, f, q, fs)
        filters.append(bq)
        gain *= bq.scale
    word0 = ((_signed_quant(gain, 24, 12) << 8) | (stages & 0xF)) + 0x40
    out = bytearray(struct.pack("<I", word0 & 0xFFFFFFFF))
    for bq in filters:
        out += struct.pack(
            "<5i", *(_signed_quant(v, 32, Q30) for v in (bq.b0, bq.b1, bq.b2, bq.a1, bq.a2))
        )
    return bytes(out)


def decode_eq(data: bytes, fs: float) -> tuple[float, list[tuple[float, float, float]]]:
    """Approximate inverse of calculate_eq: (global gain dB, [(gain dB, freq Hz, Q), ...]).

    The Q returned is the one sent to the DSP, i.e. after corrected_q().
    """
    if len(data) < 4:
        raise ValueError("eq data too short")
    (word0,) = struct.unpack_from("<I", data)
    stages = word0 & 0xF
    if len(data) < 4 + 20 * stages:
        raise ValueError("eq data shorter than its stage count")
    gq = word0 >> 8
    if gq & 0x800000:
        gq -= 1 << 24
    gain = gq / 4096.0
    out = []
    for i in range(stages):
        b0, b1, b2, a1, a2 = (v / (1 << Q30) for v in struct.unpack_from("<5i", data, 4 + 20 * i))
        scale = a1 / b1 if b1 else 1.0
        b0 *= scale
        beta = -a2 / 2.0
        d12 = 0.25 + beta * 0.5
        g = 1.0 + (b0 / 2.0 - 0.5) / d12 if d12 else 1.0
        cos_w0 = max(-1.0, min(1.0, -a1 / (2.0 * (0.5 - beta))))
        w0 = math.acos(cos_w0)
        t = (1.0 + 2.0 * beta) / (1.0 - 2.0 * beta)
        k = g + g if g < 1.0 else 2.0
        q = w0 / (k * t) if t else 0.0
        gain /= scale
        out.append((20.0 * math.log10(max(g, 1e-9)), w0 * fs / TWO_PI, q))
    return 20.0 * math.log10(max(gain, 1e-9)), out


def corrected_q(q: float, gain_db: float) -> float:
    """Q adjustment the official app applies before calling the DSP library."""
    return _c_round(q * math.pow(10.0, abs(gain_db) / 40.0) * 10.0) / 10.0


@dataclass(frozen=True)
class EqCurve:
    """A user-facing EQ setting (what the app calls GEQ/PEQ params)."""

    gains: tuple[float, ...]
    freqs: tuple[float, ...]
    qs: tuple[float, ...]
    global_gain: float = -6.0

    def eq_data(self, fs: float) -> bytes:
        qs = [corrected_q(q, g) for q, g in zip(self.qs, self.gains)]
        return calculate_eq(fs, self.global_gain, list(self.gains), list(self.freqs), qs)


GEQ_FREQS = (82.0, 247.0, 1000.0, 4000.0, 12500.0)
GEQ_QS = (0.8, 0.7, 0.7, 0.7, 0.8)

# EQ "V2" presets of the official app for the ATH-M50xBT2 (graphic EQ variants).
PRESETS: dict[str, EqCurve] = {
    "original": EqCurve((0, 0, 0, 0, 0), GEQ_FREQS, GEQ_QS),
    "bass-boost": EqCurve((4.5, -3, -1, 0, 1.5), GEQ_FREQS, GEQ_QS),
    "clear-vocal": EqCurve((-3, 1.5, 1, 4, 1.5), GEQ_FREQS, GEQ_QS),
    "v-shaped": EqCurve((3, 1, -2, 1, 3), GEQ_FREQS, GEQ_QS),
    "treble-enhance": EqCurve((-2, 0.5, -0.5, 2, 4), GEQ_FREQS, GEQ_QS),
}

# Sample-rate codes used by the Realtek EQ protocol.
SAMPLE_RATES = {3: 44100.0, 4: 48000.0}
