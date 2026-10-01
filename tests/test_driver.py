"""The driver against a scripted bus: every byte it puts on the wire is asserted.

These tests pin the I2C framing to the datasheet (UM16 §4.3, §5). The same
driver is run against the register model in tests/sim/test_device.py.
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


class Chip(ScriptedBus):
    """A scripted bus whose STATUS follows the clock rather than a script.

    DRDY is up while ``drdy`` is set, or from ``ready_after`` seconds after
    the last POLL that started something. Every other transfer is scripted as
    usual, and each transfer's time is kept in ``times``.
    """

    def __init__(self, clock, ready_after=None):
        super().__init__()
        self.clock = clock
        self.ready_after = ready_after
        self.drdy = False
        self.polled_at = None
        self.times = []
        # Called after a STATUS read has been answered, before the driver
        # sees the answer: where a thread held up by the host would lose time.
        self.after_status = None

    def transfer(self, address, write, read_length=0):
        self.times.append((bytes(write), read_length, self.clock.now))
        if write == b"\x34" and read_length == 1:
            reply = b"\x80" if self._ready() else b"\x00"
            if self.after_status is not None:
                self.after_status()
            return reply
        if write[:1] == b"\x00" and write[1:2] not in (b"", b"\x00"):
            self.polled_at = self.clock.now
        return super().transfer(address, write, read_length)

    def _ready(self):
        if self.drdy:
            return True
        if self.ready_after is None or self.polled_at is None:
            return False
        return self.clock.now - self.polled_at >= self.ready_after

    def time_of(self, write, read_length=0):
        """When the first transfer of ``write`` (and ``read_length``) happened."""
        return next(t for w, r, t in self.times if w == write and r == read_length)

    def status_reads(self):
        return [t for w, r, t in self.times if w == b"\x34" and r == 1]


def expect_cycle_count(bus, cycle_count):
    word = cycle_count.to_bytes(2, "big")
    return bus.expect(A, b"\x04" + word * 3).expect(A, b"\x04", 6, word * 3)


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

    def test_a_wrong_revid_is_reported_over_the_silence_after_it(self):
        # The usual strapping puts the chip at 0x20, probed first: its REVID
        # must reach the health page, not the NACK from 0x23.
        bus = ScriptedBus().expect(0x20, b"\x36", 1, b"\x23")
        for address in (0x21, 0x22, 0x23):
            bus.expect(address, b"\x36", 1, error=nack())
        with pytest.raises(NotAnRM3100, match="0x20 reports REVID 0x23"):
            find_rm3100(bus)
        bus.assert_done()

    def test_every_wrong_revid_is_named(self):
        bus = (
            ScriptedBus()
            .expect(0x20, b"\x36", 1, b"\x23")
            .expect(0x21, b"\x36", 1, error=nack())
            .expect(0x22, b"\x36", 1, b"\x00")
            .expect(0x23, b"\x36", 1, error=nack())
        )
        with pytest.raises(NotAnRM3100) as info:
            find_rm3100(bus)
        assert "0x20 reports REVID 0x23" in str(info.value) and "0x22 reports REVID 0x00" in str(info.value)

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

    @pytest.mark.parametrize("cycle_count", [50, 200, 1000])
    def test_status_is_first_read_when_the_conversion_should_be_over(self, cycle_count):
        # Reading STATUS through the conversion only adds bus traffic: a
        # measurement is the POLL, one STATUS read and the result read.
        clock = FakeClock()
        conversion = reg.xyz_conversion_s(cycle_count)
        bus = expect_cycle_count(Chip(clock, ready_after=conversion * 0.99), cycle_count)
        bus.expect(A, b"\x00\x70").expect(A, b"\x24", 9, result_bytes(1, 2, 3))
        s = RM3100(bus, A, clock=clock.monotonic, sleep=clock.sleep)
        s.set_cycle_count(cycle_count)
        s.single_measurement()
        polled = bus.time_of(b"\x00\x70")
        (first,) = bus.status_reads()
        assert first - polled >= conversion - 1e-9
        bus.assert_done()

    def test_a_slow_chip_is_polled_until_drdy(self):
        clock = FakeClock()
        conversion = reg.xyz_conversion_s(200)
        bus = Chip(clock, ready_after=conversion * 1.3)  # 30 % slower than Table 3-1
        bus.expect(A, b"\x00\x70").expect(A, b"\x24", 9, result_bytes(1, 2, 3))
        s = RM3100(bus, A, clock=clock.monotonic, sleep=clock.sleep)
        s.single_measurement()
        reads = bus.status_reads()
        assert len(reads) == 4  # at the nominal end, then every millisecond
        assert reads[-1] - bus.time_of(b"\x00\x70") >= conversion * 1.3
        bus.assert_done()

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

    @staticmethod
    def started(tmrc=0x96):
        clock = FakeClock()
        bus = Chip(clock).expect(A, b"\x01\x00").expect(A, b"\x0b" + bytes([tmrc])).expect(A, b"\x01\x79")
        s = RM3100(bus, A, clock=clock.monotonic, sleep=clock.sleep)
        s.start_continuous(tmrc)
        window = 3 / reg.effective_continuous_rate_hz(tmrc, 200) + RM3100.DRDY_MARGIN_S
        return s, bus, clock, window

    def test_drdy_low_for_three_sample_periods_is_a_timeout(self):
        # A brown-out ends continuous mode without failing a transfer, and at
        # the default 200 cycles the cycle-count check cannot see it either.
        s, bus, clock, window = self.started()
        assert window == pytest.approx(3 / 37.5 + 0.020)
        clock.advance(window - 0.001)
        assert s.read_if_ready() is None
        clock.advance(0.001)
        with pytest.raises(DataReadyTimeout, match="continuous mode"):
            s.read_if_ready()
        # One timeout per window, not one per look from then on.
        assert s.read_if_ready() is None
        clock.advance(window)
        with pytest.raises(DataReadyTimeout):
            s.read_if_ready()

    def test_every_sample_restarts_the_wait(self):
        s, bus, clock, window = self.started()
        for i in range(4):
            clock.advance(window * 0.9)
            bus.drdy = True
            bus.expect(A, b"\x24", 9, result_bytes(i, i, i))
            assert s.read_if_ready().x_counts == i
            bus.drdy = False
        clock.advance(window * 0.9)
        assert s.read_if_ready() is None
        bus.assert_done()

    def test_the_wait_follows_the_rate_the_chip_can_deliver(self):
        # 600 Hz is asked for, but 200 cycles cap it at ~147 Hz: the window is
        # three of those periods, neither shorter nor longer.
        s, bus, clock, window = self.started(0x92)
        assert window == pytest.approx(3 * reg.xyz_conversion_s(200) + RM3100.DRDY_MARGIN_S)
        clock.advance(window - 0.0005)
        assert s.read_if_ready() is None
        clock.advance(0.0005)
        with pytest.raises(DataReadyTimeout):
            s.read_if_ready()

    @pytest.mark.parametrize("tmrc", [0x92, 0x96, 0x9B])
    def test_a_thread_held_up_after_a_not_ready_read_is_not_a_timeout(self, tmrc):
        # STATUS says "not ready" just before the deadline, and the thread
        # gets the CPU back only after it (a busy host, a slow simulator
        # reply). The chip may have measured on all along: only a look that
        # began after the deadline and still saw DRDY low is a timeout.
        s, bus, clock, window = self.started(tmrc)
        clock.advance(window - 0.001)

        def held_up():
            bus.after_status = None
            clock.advance(window)  # back long after the deadline

        bus.after_status = held_up
        assert s.read_if_ready() is None
        assert bus.after_status is None  # the hold-up happened
        # The next look begins after the deadline: now it is a timeout.
        with pytest.raises(DataReadyTimeout):
            s.read_if_ready()

    def test_stopped_continuous_mode_is_not_watched(self):
        s, bus, clock, window = self.started()
        bus.expect(A, b"\x01\x00")
        s.stop_continuous()
        clock.advance(10 * window)
        assert s.read_if_ready() is None


class TestSelfTest:
    def test_fig_5_1_sequence_and_pass(self):
        bus = (
            ScriptedBus()
            .expect(A, b"\x01\x00")  # continuous mode off, or the POLL below is NACKed and ignored
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

    def test_continuous_mode_is_stopped_before_bist_is_armed(self):
        # A run that ended in continuous mode (or a restart into poll mode
        # after one) leaves the chip measuring. UM16 p.28 and p.31: a POLL
        # written then is NACKed and ignored, so the test would never run.
        bus = (
            ScriptedBus()
            .expect(A, b"\x01\x00")
            .expect(A, b"\x33\x8f")
            .expect(A, b"\x00\x70")
            .expect_status(A, 0x80)
            .expect(A, b"\x33", 1, b"\xff")
            .expect(A, b"\x00\x00")
            .expect(A, b"\x33\x00")
        )
        sensor(bus).self_test()
        writes = [w for _, w, r in bus.log if not r]
        assert writes.index(b"\x01\x00") < writes.index(b"\x33\x8f") < writes.index(b"\x00\x70")

    def test_reports_the_failed_axis(self):
        bus = (
            ScriptedBus()
            .expect(A, b"\x01\x00")
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
            .expect(A, b"\x01\x00")
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
        clock = FakeClock()
        bus = (
            Chip(clock)
            .expect(A, b"\x01\x00")
            .expect(A, b"\x33\x8f")
            .expect(A, b"\x00\x70")
            .expect(A, b"\x33", 1, b"\xff")
            .expect(A, b"\x00\x00")
            .expect(A, b"\x33\x00")
        )
        assert RM3100(bus, A, clock=clock.monotonic, sleep=clock.sleep).self_test().passed
        waited = bus.time_of(b"\x33", 1) - bus.time_of(b"\x00\x70")
        assert RM3100.BIST_WAIT_S <= waited < RM3100.BIST_WAIT_S + 2 * RM3100.DRDY_POLL_S
        bus.assert_done()

    @pytest.mark.parametrize("cycle_count", [200, 600, 905, 1000])
    def test_waits_as_long_as_a_measurement_takes_at_the_cycle_count(self, cycle_count):
        # A chip that spends a full conversion on the test (the simulator's
        # model does) is not read before it ends: at 1000 cycles that is
        # 33 ms, and an unfinished test reads as a failure on every axis.
        clock = FakeClock()
        conversion = reg.xyz_conversion_s(cycle_count)
        bus = expect_cycle_count(Chip(clock, ready_after=conversion), cycle_count)
        bus.expect(A, b"\x01\x00").expect(A, b"\x33\x8f").expect(A, b"\x00\x70")
        bus.expect(A, b"\x33", 1, b"\xff").expect(A, b"\x00\x00").expect(A, b"\x33\x00")
        s = RM3100(bus, A, clock=clock.monotonic, sleep=clock.sleep)
        s.set_cycle_count(cycle_count)
        assert s.self_test().passed
        assert bus.time_of(b"\x33", 1) - bus.time_of(b"\x00\x70") >= conversion
        bus.assert_done()

    def test_the_wait_without_drdy_covers_a_measurement_at_high_cycle_counts(self):
        clock = FakeClock()
        bus = expect_cycle_count(Chip(clock), 1000)
        bus.expect(A, b"\x01\x00").expect(A, b"\x33\x8f").expect(A, b"\x00\x70")
        bus.expect(A, b"\x33", 1, b"\xff").expect(A, b"\x00\x00").expect(A, b"\x33\x00")
        s = RM3100(bus, A, clock=clock.monotonic, sleep=clock.sleep)
        s.set_cycle_count(1000)
        s.self_test()
        waited = bus.time_of(b"\x33", 1) - bus.time_of(b"\x00\x70")
        assert waited >= reg.xyz_conversion_s(1000) + RM3100.DRDY_MARGIN_S > RM3100.BIST_WAIT_S


class TestPollRate:
    @pytest.mark.parametrize("cycle_count", [30, 50, 200, 400, 1000])
    def test_a_sample_costs_more_than_its_conversion(self, cycle_count):
        ceiling = RM3100.max_poll_rate_hz(cycle_count)
        assert ceiling < reg.max_xyz_rate_hz(cycle_count)
        conversion = reg.xyz_conversion_s(cycle_count)
        assert 1 / ceiling == pytest.approx(conversion * 1.05 + RM3100.MEASUREMENT_OVERHEAD_S)

    def test_the_budget_holds_a_slower_chip_two_status_reads_and_the_host(self):
        # Bus time at 100 kHz, nine bit times a byte: POLL write 3 bytes, a
        # STATUS read 4 (repeated start), the result read 12.
        bus = (3 + 2 * 4 + 12) * 9 / 100_000
        host = 0.001
        assert bus + host <= RM3100.MEASUREMENT_OVERHEAD_S - RM3100.DRDY_POLL_S
        assert RM3100.CONVERSION_TOLERANCE >= 0.05

    def test_about_87_hz_at_the_default_cycle_count(self):
        assert RM3100.max_poll_rate_hz(200) == pytest.approx(87.3, abs=0.2)
        assert RM3100.max_poll_rate_hz(1000) == pytest.approx(25.5, abs=0.1)
