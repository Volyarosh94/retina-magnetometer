"""The driver against a scripted bus: every byte it puts on the wire is asserted.

These tests pin the I2C framing to the datasheet (UM16 §4.3, §5). The same
driver is run against the register model in test_driver_against_model.py.
"""

import pytest
from fakes import FakeClock, ScriptedBus, nack

from retina_magnetometer.rm3100 import registers as reg
from retina_magnetometer.rm3100.driver import (
    RM3100,
    DataReadyTimeout,
    NotAnRM3100,
    RM3100Error,
    decode_measurement,
    find_rm3100,
)

A = 0x20


def sensor(bus, address=A):
    clock = FakeClock()
    return RM3100(bus, address, clock=clock.monotonic, sleep=clock.sleep)


def result_bytes(x, y, z):
    return reg.encode_int24(x) + reg.encode_int24(y) + reg.encode_int24(z)


class TestIdentity:
    def test_probe_reads_revid_without_the_spi_read_bit(self):
        bus = ScriptedBus().expect(A, b"\x36", 1, b"\x22")
        assert sensor(bus).probe() == 0x22
        bus.assert_done()

    def test_wrong_revid_is_not_an_rm3100(self):
        bus = ScriptedBus().expect(A, b"\x36", 1, b"\x19")
        with pytest.raises(NotAnRM3100, match="0x19"):
            sensor(bus).probe()

    def test_find_skips_addresses_that_nack(self):
        bus = ScriptedBus().expect(0x20, b"\x36", 1, error=nack()).expect(0x21, b"\x36", 1, b"\x22")
        found = find_rm3100(bus)
        assert found.address == 0x21
        bus.assert_done()

    def test_find_skips_a_device_that_is_not_an_rm3100(self):
        bus = (
            ScriptedBus()
            .expect(0x20, b"\x36", 1, b"\x00")
            .expect(0x21, b"\x36", 1, error=nack())
            .expect(0x22, b"\x36", 1, b"\x22")
        )
        assert find_rm3100(bus).address == 0x22

    def test_find_raises_when_nothing_answers(self):
        bus = ScriptedBus()
        for address in reg.I2C_ADDRESSES:
            bus.expect(address, b"\x36", 1, error=nack())
        with pytest.raises(OSError):
            find_rm3100(bus)
        bus.assert_done()

    def test_find_needs_addresses(self):
        with pytest.raises(ValueError):
            find_rm3100(ScriptedBus(), addresses=())


class TestCycleCount:
    def test_one_auto_increment_write_then_read_back(self):
        bus = (
            ScriptedBus()
            .expect(A, bytes([0x04, 0x01, 0x90, 0x01, 0x90, 0x01, 0x90]))  # 400 = 0x0190
            .expect(A, b"\x04", 6, bytes([0x01, 0x90] * 3))
        )
        s = sensor(bus)
        s.set_cycle_count(400)
        assert s.cycle_count == 400
        bus.assert_done()

    def test_read_back_mismatch_is_an_error_and_keeps_the_old_gain(self):
        bus = (
            ScriptedBus()
            .expect(A, bytes([0x04, 0x01, 0x90, 0x01, 0x90, 0x01, 0x90]))
            .expect(A, b"\x04", 6, bytes([0x01, 0x90, 0x00, 0xC8, 0x01, 0x90]))
        )
        s = sensor(bus)
        with pytest.raises(RM3100Error, match="did not take"):
            s.set_cycle_count(400)
        assert s.cycle_count == 200

    @pytest.mark.parametrize("bad", [0, 29, 1001, 65535])
    def test_out_of_range_never_reaches_the_bus(self, bad):
        bus = ScriptedBus()
        with pytest.raises(ValueError):
            sensor(bus).set_cycle_count(bad)
        assert bus.log == []


class TestSingleMeasurement:
    def test_poll_wait_for_drdy_then_burst_read(self):
        bus = (
            ScriptedBus()
            .expect(A, b"\x00\x70")  # POLL all three axes
            .expect_status(A, 0x00, 0x00, 0x80)  # DRDY rises on the third look
            .expect(A, b"\x24", 9, result_bytes(1_000, -2_000, 3_000_000))
        )
        m = sensor(bus).single_measurement()
        assert (m.x_counts, m.y_counts, m.z_counts) == (1_000, -2_000, 3_000_000)
        assert m.cycle_count == 200
        assert m.z_nt == pytest.approx(3_000_000 * 1000 / 74.92)
        bus.assert_done()

    def test_status_bits_other_than_drdy_are_ignored(self):
        bus = (
            ScriptedBus()
            .expect(A, b"\x00\x70")
            .expect_status(A, 0x7F, 0xFF)
            .expect(A, b"\x24", 9, result_bytes(0, 0, 0))
        )
        sensor(bus).single_measurement()
        bus.assert_done()

    def test_drdy_that_never_rises_times_out(self):
        class NeverReady(ScriptedBus):
            def transfer(self, address, write, read_length=0):
                if write == b"\x34":
                    return b"\x00"
                return super().transfer(address, write, read_length)

        bus = NeverReady().expect(A, b"\x00\x70")
        clock = FakeClock()
        s = RM3100(bus, A, clock=clock.monotonic, sleep=clock.sleep)
        start = clock.now
        with pytest.raises(DataReadyTimeout):
            s.single_measurement()
        waited = clock.now - start
        budget = reg.xyz_conversion_s(200) + RM3100.DRDY_MARGIN_S
        assert budget <= waited < budget + 2 * RM3100.DRDY_POLL_S

    def test_bus_errors_propagate_as_oserror(self):
        bus = ScriptedBus().expect(A, b"\x00\x70", error=nack())
        with pytest.raises(OSError):
            sensor(bus).single_measurement()

    def test_decode_rejects_short_reads(self):
        with pytest.raises(RM3100Error):
            decode_measurement(b"\x00" * 8, 200)


class TestContinuousMode:
    def test_stop_then_tmrc_then_cmm_0x79(self):
        bus = (
            ScriptedBus()
            .expect(A, b"\x01\x00")  # stop first: writing TMRC would end continuous mode
            .expect(A, b"\x0b\x96")
            .expect(A, b"\x01\x79")
        )
        sensor(bus).start_continuous(0x96)
        bus.assert_done()

    def test_rejects_invalid_tmrc(self):
        with pytest.raises(ValueError):
            sensor(ScriptedBus()).start_continuous(0x91)

    def test_read_if_ready_only_reads_results_after_drdy(self):
        bus = ScriptedBus().expect_status(A, 0x00).expect_status(A, 0x80).expect(A, b"\x24", 9, result_bytes(5, 6, 7))
        s = sensor(bus)
        assert s.read_if_ready() is None
        m = s.read_if_ready()
        assert (m.x_counts, m.y_counts, m.z_counts) == (5, 6, 7)
        bus.assert_done()

    def test_never_reads_cmm(self):
        # Reading CMM terminates continuous mode (UM16 p.31); the driver has no
        # reason to, and must not.
        bus = ScriptedBus().expect(A, b"\x01\x00").expect(A, b"\x0b\x9b").expect(A, b"\x01\x79")
        sensor(bus).start_continuous(0x9B)
        assert all(not (w[:1] == b"\x01" and r) for _, w, r in bus.log)


class TestSelfTest:
    def test_fig_5_1_sequence_and_pass(self):
        bus = (
            ScriptedBus()
            .expect(A, b"\x33\x8f")  # arm: STE, 120 us timeout, 4 LR periods
            .expect(A, b"\x00\x70")  # run on the next POLL
            .expect_status(A, 0x00, 0x80)
            .expect(A, b"\x33", 1, b"\xff")
            .expect(A, b"\x00\x00")
            .expect(A, b"\x33\x00")  # leave BIST off
        )
        result = sensor(bus).self_test()
        assert result.passed and result.ran
        bus.assert_done()

    def test_reports_the_failed_axis(self):
        bus = (
            ScriptedBus()
            .expect(A, b"\x33\x8f")
            .expect(A, b"\x00\x70")
            .expect_status(A, 0x80)
            .expect(A, b"\x33", 1, bytes([0x80 | 0x20 | 0x10 | 0x0F]))  # Z bit clear
            .expect(A, b"\x00\x00")
            .expect(A, b"\x33\x00")
        )
        result = sensor(bus).self_test()
        assert (result.x_ok, result.y_ok, result.z_ok) == (True, True, False)
        assert not result.passed

    def test_result_bits_mean_nothing_without_ste(self):
        bus = (
            ScriptedBus()
            .expect(A, b"\x33\x8f")
            .expect(A, b"\x00\x70")
            .expect_status(A, 0x80)
            .expect(A, b"\x33", 1, b"\x7f")
            .expect(A, b"\x00\x00")
            .expect(A, b"\x33\x00")
        )
        result = sensor(bus).self_test()
        assert not result.ran and not result.passed

    def test_reads_bist_even_if_drdy_never_rises(self):
        # PX4 #19207: DRDY sometimes stays low after BIST on fast hosts.
        class Quiet(ScriptedBus):
            def transfer(self, address, write, read_length=0):
                if write == b"\x34":
                    return b"\x00"
                return super().transfer(address, write, read_length)

        bus = (
            Quiet()
            .expect(A, b"\x33\x8f")
            .expect(A, b"\x00\x70")
            .expect(A, b"\x33", 1, b"\xff")
            .expect(A, b"\x00\x00")
            .expect(A, b"\x33\x00")
        )
        assert sensor(bus).self_test().passed
        bus.assert_done()
