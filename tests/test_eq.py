import pytest

from atconnect.eq import PRESETS, EqCurve, calculate_eq, corrected_q, decode_eq

# Reference blobs produced by running Realtek's libDspConfig calculateEq() (armeabi-v7a)
# under an emulator with the same arguments.
GOLDEN = [
    (
        (44100.0, -6.0, [0, 0, 0, 0, 0], [82, 247, 1000, 4000, 12500], [0.8, 0.7, 0.7, 0.7, 0.8]),
        "4505080000000040fec0ef805877123ffec0ef805877123f00000040ee4637839783dc3cee4637839783dc3c"
        "000000401314008da0462d341314008da0462d3400000040fe8e68b38f0bf81afe8e68b38f0bf81a00000040"
        "0beba20c042193fc0beba20c042193fc",
    ),
    (
        (48000.0, -6.0, [4.5, -3, -1, 0, 1.5], [82, 247, 1000, 4000, 12500], [1.1, 0.8, 0.7, 0.7, 0.9]),
        "4505080093ff354019faa08038e72a3f19faa080cbe6603feb347b3f5c069e83486bf73c5c069e8333a0723c"
        "d8d3563f76f6248d6bed813476f6248d42c1d83300000040817a52afe2a1281d817a52afe2a1281d029fbe45"
        "51a16204f5274efd51a16204f7c60c03",
    ),
    (
        (44100.0, 0.0, [12, 12, -12], [20, 1000, 20000], [0.3, 1.0, 4.0]),
        "43001000fbdce640b9049b8031407e3eb9049b802c1d653f3903b04c9408b98999e5cc2a9408b989d2e87c37"
        "9a45e5237dabb132ca540a117dabb132649aeff4",
    ),
]


@pytest.mark.parametrize(("args", "expected"), GOLDEN)
def test_matches_native_library(args, expected):
    assert calculate_eq(*args).hex() == expected


def test_corrected_q_matches_app():
    assert corrected_q(0.8, 4.5) == 1.0
    assert corrected_q(0.7, 12) == 1.4
    assert corrected_q(0.7, -3) == 0.8
    assert corrected_q(0.7, 0) == 0.7


def test_decode_roundtrip():
    curve = EqCurve((6, -3, 0, 2.5, -8), (82, 247, 1000, 4000, 12500), (0.8, 0.7, 0.7, 0.7, 0.8))
    global_gain, stages = decode_eq(curve.eq_data(44100.0), 44100.0)
    assert global_gain == pytest.approx(-6, abs=0.05)
    for (g, f, q), eg, ef, eq in zip(stages, curve.gains, curve.freqs, curve.qs):
        assert g == pytest.approx(eg, abs=0.05)
        assert f == pytest.approx(ef, rel=0.01)
        assert q == pytest.approx(corrected_q(eq, eg), rel=0.02)


def test_presets_are_distinct():
    blobs = {name: c.eq_data(44100.0) for name, c in PRESETS.items()}
    assert len(set(blobs.values())) == len(blobs)
