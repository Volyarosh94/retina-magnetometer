# What still needs a real RM3100

Everything in this repository was developed and tested without the sensor: the
driver against a scripted bus and against the simulator's register model, the
app against the simulator over TCP. Both of those encode the manual. This page
lists what only hardware can confirm, in the order it is worth checking on
first power-up, with how to check it and what the code assumes meanwhile.

The manual is PNI's *RM3100 & RM2100 Magneto-Inductive Magnetometer User
Manual*, Doc 1017252 V16.0 (UM16). It contradicts itself in places, and the
open-source drivers (ArduPilot, PX4, the Linux IIO driver, Zephyr, INAV,
HamSCI's runMag) sometimes do what it forbids and work anyway; where the code
had to pick, it picked what those drivers prove in the field.

## First power-up

| # | Check | How | The code assumes |
| --- | --- | --- | --- |
| 1 | The bus exists and the chip answers | `i2cdetect -y 1` shows 0x20–0x23 | nothing; it probes all four |
| 2 | Identity | `curl -s localhost:3030/api/health \| jq .sensor.revid` shows `"0x22"` | REVID 0x22. The manual gives no value; every driver that checks uses 0x22. ArduPilot does not trust it and checks the cycle-count defaults instead |
| 3 | Self test passes | `self_test.passed` in the health document | BIST as UM16 Fig 5-1: write 0x8F, POLL, wait for DRDY (up to 30 ms), read. PX4 saw DRDY stay low after BIST on fast hosts, so the app reads the result anyway after the wait |
| 4 | Framing | samples flow with the default `repeated-start`; if the chip NACKs reads, set `MAGNETOMETER_I2C_FRAMING=stop` | a repeated start between the register pointer and the read (I2C_RDWR), as ArduPilot's Linux HAL, the kernel's regmap and Zephyr do. PNI's manual draws a STOP there (PX4 and HamSCI do that); both are proven on other hosts, neither on the Pi 5's RP1 adapter |
| 5 | Bus speed and clock stretching | run at 100 kHz first, then `dtparam=i2c_arm_baudrate=400000` | nothing faster than 100 kHz is needed at these rates; the manual says ≤1 MHz and says nothing about clock stretching |

## Timing and DRDY

| # | Check | How | The code assumes |
| --- | --- | --- | --- |
| 6 | Conversion time per cycle count | `MAGNETOMETER_MODE=continuous` at the highest rate for a cycle count; compare the measured rate on the health card with the table | 3 × (75.7 µs + 11.0 µs × CC), fitted to Table 3-1's maximum single-axis rates; DRDY polling allows 20 ms over that |
| 7 | Continuous-mode rate | TMRC steps from 600 Hz halving; the health card's measured rate | ±7 % per the manual; the cap at 200 cycles is ~147 Hz for three axes (the manual's "~430 Hz" on p.32 is the single-axis figure) |
| 8 | What clears DRDY | the app polls STATUS then reads; if it ever reads stale data the samples repeat exactly | reading the results clears it (DRC1), a register write clears it (DRC0), and a pointer-only write, the first half of every read, does not |
| 9 | CMM value | continuous mode delivers samples | 0x79 (PNI's own examples). Most drivers write 0x71; the manual's two layouts of CMM disagree on bit 3, and 0x79 asks for a full X/Y/Z sequence under both |
| 10 | Reset values | read HSHAKE (0x35) after power-up | not relied on; the manual says 0x1B, PX4 treats 0x0B as the default |

## Accuracy

| # | Check | How | The code assumes |
| --- | --- | --- | --- |
| 11 | Noise at the chosen cycle count | leave the sensor still for ten minutes; the standard deviation of the 10-minute view's quiet stretch | 30/20/15 nT at 50/100/200 cycles (Table 3-1), about 11 and 8 nT at 400 and 800. Regoli et al. measured 8.7 nT at 800 in shielding and 34 nT unshielded: the site's own disturbances may dominate |
| 12 | Gain | the orientation card's magnitude against WMM2025, ideally with the sensor away from everything | PNI's formula, 0.3671·CC + 1.5 counts/µT. ArduPilot found boards up to ~1.3x off it; a scale error shows as a magnitude mismatch |
| 13 | Axis polarity and labelling | point the breakout's marked +X north, level: X should read about +22,400 nT at Greenville and Z about +43,000 (down) | the chip's axes as documented; ArduPilot keeps a reversal mask because "some RM3100 builds get the polarity wrong" |
| 14 | Hard iron | the magnitude mismatch with the sensor in place, then moved a metre away | none is corrected. A fixed installation cannot be rotated for a sphere fit, so a large offset is best fixed by moving the sensor |
| 15 | Temperature | a day's data against an outdoor thermometer | nothing is corrected; the RM3100 has no temperature sensor and its coil tempco is 0.4 %/°C (HamSCI adds an MCP9808 beside it) |
| 16 | Orientation | a phone compass or a sighting along the mounting | the card's heading is the heading of the horizontal axis if the downward axis is vertical, ±0.35° from the model at Greenville plus the tilt and mismatch terms |

## On the node

| # | Check | How |
| --- | --- | --- |
| 17 | The container can open the bus with every capability dropped | the health state becomes `ok`, not `no_bus` with a permissions message. If it does not, check `ls -l /dev/i2c-1` is owned by root, and that the cgroup rule `c 89:1 rmw` matches the device's numbers (`ls -l` shows `89, 1`) |
| 18 | A bus that appears after start-up is picked up | with `dtparam=i2c_arm=on` set, `modprobe i2c-dev` on the running node; the state leaves `no_bus` within 30 s, with no container restart |
| 19 | Unplugging and replugging the sensor | the state goes `degraded`, `no_sensor`, then `ok`; `reinitialisations` goes up by one and the cycle count is set again |
| 20 | SD card writes | `iostat` or the card's wear counters over a day. At 1 Hz, every 5 s: one small database transaction (`MAGNETOMETER_FLUSH_INTERVAL_S`), and `status.json` replaced (written to a new file, then renamed over the old one). About once a minute, a transaction with the minute just completed (the roll-up looks every 30 s). Every 10 minutes, the retention delete and a checkpoint that truncates the WAL; SQLite also checkpoints whenever the WAL reaches 1,000 pages |
| 21 | The container sees none of the host's shared memory, message queues or console | `docker exec retina-magnetometer ls -A /dev/shm /dev/mqueue` lists both directories empty, whatever `ls /dev/shm` shows on the host (blah2 and retina-spectrum keep the SDRplay API's files there); `docker exec retina-magnetometer ls -l /dev/console` shows `1, 3`, the null device, where the host's is `5, 1`. Anything else means the entry's `tmpfs:` lines or its `/dev/null:/dev/console` line are missing. `docker exec retina-magnetometer findmnt -R /dev` then lists what the bind still brings: the host's `/dev/pts` and any other filesystem the host mounts under `/dev` |

Not verified here either: the IIO kernel driver route. Linux has had an
RM3100 IIO driver since 5.0, but Raspberry Pi OS kernels do not build it (and
it does no identity check, no BIST and no DRDY handshake handling), so the app
talks to the chip from user space through i2c-dev instead.
