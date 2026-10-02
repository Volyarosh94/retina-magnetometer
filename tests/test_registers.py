"""The datasheet tables and conversions the driver is built on."""

import pytest

from retina_magnetometer.rm3100 import registers as reg


class TestGainAndConversion:
    @pytest.mark.parametrize("cycle_count", [50, 100, 200])
    def test_pni_formula_reproduces_table_3_1(self, cycle_count):
        # The tolerance the docstring states: 0.8 % (the table's 20 and 38 are
        # rounded), 0.1 % at the default 200.
        table_gain = reg.DATASHEET_TABLE[cycle_count][0]
        assert reg.gain_lsb_per_ut(cycle_count) == pytest.approx(table_gain, rel=0.008)

    def test_default_cycle_count_gain(self):
        assert reg.gain_lsb_per_ut(200) == pytest.approx(74.92)
        assert reg.gain_lsb_per_ut(200) == pytest.approx(reg.DATASHEET_TABLE[200][0], rel=0.0011)

    def test_counts_to_nt_uses_the_gain(self):
        # 74.92 counts is one microtesla at 200 cycles.
        assert reg.counts_to_nt(round(74.92 * 50), 200) == pytest.approx(50_000.0, abs=15.0)
        assert reg.counts_to_nt(-7492, 200) == pytest.approx(-100_000.0, rel=1e-6)

    def test_lsb_is_13_nt_at_default(self):
        assert reg.lsb_nt(200) == pytest.approx(13.35, abs=0.01)


class TestNoise:
    @pytest.mark.parametrize("cycle_count", [50, 100, 200])
    def test_table_points_exact(self, cycle_count):
        assert reg.noise_nt(cycle_count) == pytest.approx(reg.DATASHEET_TABLE[cycle_count][1])

    def test_extrapolation_agrees_with_regoli_2018(self):
        # Regoli et al. measured 8.73 nT RMS at 800 cycles inside shielding.
        assert reg.noise_nt(800) == pytest.approx(8.73, rel=0.05)
        assert 10.5 < reg.noise_nt(400) < 12.0

    def test_extrapolated_values_the_docstring_and_readme_quote(self):
        # Each doubling of the cycle count beyond 200 takes a quarter off, as
        # from 100 to 200: 11.25 nT at 400 and 8.44 at 800.
        assert reg.noise_nt(400) == pytest.approx(11.25)
        assert reg.noise_nt(800) == pytest.approx(8.4375)

    def test_noise_falls_monotonically_with_cycle_count(self):
        values = [reg.noise_nt(cc) for cc in (30, 50, 75, 100, 150, 200, 400, 800, 1000)]
        assert values == sorted(values, reverse=True)


class TestTiming:
    @pytest.mark.parametrize("cycle_count", [50, 100, 200])
    def test_axis_conversion_matches_max_single_axis_rate(self, cycle_count):
        max_rate = reg.DATASHEET_TABLE[cycle_count][2]
        assert reg.axis_conversion_s(cycle_count) == pytest.approx(1.0 / max_rate, rel=0.01)

    def test_three_axis_rate_at_default_is_about_147_hz(self):
        assert reg.max_xyz_rate_hz(200) == pytest.approx(146.7, abs=0.5)


class TestTmrc:
    @pytest.mark.parametrize(
        "code,rate",
        [(0x92, 600.0), (0x93, 300.0), (0x96, 37.5), (0x9B, 1.171875), (0x9E, 0.146484375), (0x9F, 0.0732421875)],
    )
    def test_table_5_4(self, code, rate):
        assert reg.tmrc_rate_hz(code) == pytest.approx(rate)

    @pytest.mark.parametrize("code", [0x91, 0xA0, 0x00, 0x03])
    def test_out_of_range_rejected(self, code):
        with pytest.raises(ValueError):
            reg.tmrc_rate_hz(code)

    @pytest.mark.parametrize("rate,code", [(37.0, 0x96), (1.0, 0x9B), (600.0, 0x92), (0.01, 0x9F), (10.0, 0x98)])
    def test_nearest_code_for_a_rate(self, rate, code):
        assert reg.tmrc_for_rate(rate) == code

    def test_rate_is_capped_by_conversion_time(self):
        assert reg.effective_continuous_rate_hz(0x92, 200) == pytest.approx(reg.max_xyz_rate_hz(200))
        assert reg.effective_continuous_rate_hz(0x96, 200) == pytest.approx(37.5)


class TestInt24:
    @pytest.mark.parametrize("value", [0, 1, -1, 12345, -12345, reg.COUNTS_MAX, reg.COUNTS_MIN, 0x7FFF00, -0x7FFF00])
    def test_roundtrip(self, value):
        assert reg.decode_int24(*reg.encode_int24(value)) == value

    def test_sign_bit(self):
        assert reg.decode_int24(0x80, 0x00, 0x00) == -8_388_608
        assert reg.decode_int24(0xFF, 0xFF, 0xFF) == -1
        assert reg.decode_int24(0x7F, 0xFF, 0xFF) == 8_388_607

    def test_encode_rejects_overflow(self):
        with pytest.raises(ValueError):
            reg.encode_int24(reg.COUNTS_MAX + 1)


class TestRegisterMap:
    def test_cycle_count_registers_are_consecutive_words(self):
        # One six-byte write from CCX must land on all three (UM16 p.30).
        assert (reg.CCY - reg.CCX, reg.CCZ - reg.CCY) == (2, 2)

    def test_result_registers_are_consecutive_triplets(self):
        # One nine-byte read from MX must return X, Y then Z (UM16 p.34).
        assert (reg.MY - reg.MX, reg.MZ - reg.MY) == (3, 3)
        assert reg.MZ + 3 - reg.MX == reg.MEASUREMENT_BYTES

    def test_cmm_continuous_value_is_pnis_0x79(self):
        assert reg.CMM_CONTINUOUS_XYZ == 0x79
