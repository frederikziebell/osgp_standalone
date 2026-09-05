# OSGP standalone meter reader — Python port

Python 3 port of the standalone Java tool, which itself ports the protocol logic from the
openHAB `smartmeterosgp` binding. Same wire protocol, same defaults, same config file
format — just no JVM.

## Install on a Raspberry Pi

```bash
sudo apt install python3-serial      # or: python3 -m pip install -r requirements.txt
sudo usermod -a -G dialout $USER     # needed once for /dev/ttyUSB0 access; log out and back in
cp config.properties.example config.properties
nano config.properties               # set your 20-character meter password
./run.sh
```

`./run.sh` picks up `venv/bin/python3` if you'd rather use a virtualenv. You can also run
it directly:

```bash
python3 main.py config.properties
```

Ctrl+C (or `systemctl stop`) triggers a clean LOGOFF/TERMINATE before exiting.

## Running as a service

To have it start on boot and restart on failure:

```bash
sudo ./service.sh install          # generates and enables the systemd unit
sudo ./service.sh start
sudo ./service.sh status           # or: journalctl -u osgp-meter-reader -f
sudo ./service.sh stop
sudo ./service.sh restart
sudo ./service.sh uninstall
```

The generated unit runs `run.sh` directly out of **this checkout** — there's no
separate copy-to-`/opt` step, so this directory becomes the live deployment once
installed. That has one consequence: don't delete or move it while the service is
installed.

- To update: `git pull` here, then `sudo ./service.sh restart`.
- To relocate or remove this checkout: `sudo ./service.sh uninstall` first, then
  move/delete the directory (and `install` again from the new location if needed).

`install` refuses to run as root directly (use `sudo` from the account that's in the
`dialout` group) and refuses to take over a service that's already installed pointing
at a *different* checkout, so a stray second clone can't silently hijack it.

## Live dashboard

The reader also serves a live-readings dashboard over plain HTTP. With the default
config, visit `http://<pi-ip-or-hostname>:8080/` from any device on the same LAN to see
current power, per-phase current/voltage and cumulative energy, refreshed every 2
seconds.

It's built on Python's standard library only (no Flask etc.), so there's nothing extra
to install. There's also no authentication — anyone who can reach the port can view live
readings — so either trust your LAN, set `webBind=127.0.0.1` and reach it over an SSH
tunnel/VPN instead, or set `webEnabled=false` to turn it off entirely. See
`config.properties.example` for the `web*` keys.

### History and charts

The dashboard also has a history chart (24h / week / month / year, switchable per
metric) backed by a single SQLite file (`history.sqlite3` by default) — no separate
database server. A row is logged once a minute (`historySampleSeconds`), independent of
how often the meter itself is polled: logging every poll (every 2s by default) would be
~15M rows/year for no real benefit, while once-a-minute is ~525k rows/year (tens of MB)
and every chart range query still comes back in well under a second, even after years of
accumulated history — queries only ever scan the rows inside the requested window, not
the whole table.

To keep the file bounded over many years without losing the shape of old data, once a
calendar year becomes more than a year old its rows are automatically consolidated from
1-minute to 5-minute resolution (checked once a day; safe to run more than once). For
example, once 2027 starts, 2025 gets coarsened — 2026 stays at full 1-minute resolution
for another year, since it isn't a full year old yet. This is a one-way operation: the
underlying 1-minute detail for that year is gone afterwards, just like the classic
RRDtool/Graphite/Prometheus approach to bounding long-term monitoring storage, but
implemented as a couple of plain SQL statements in `history.py` instead of a separate
round-robin-database engine.

Set `historyEnabled=false` to turn logging off entirely (the live dashboard still
works without it).

### System health

The dashboard also shows a small, muted CPU-load / memory / temperature readout next
to the connection status dot — deliberately out of the way, since it's meant for
spotting a problem (e.g. the Pi running hot in an enclosure), not for staring at.
Numbers only turn orange/red if they cross a threshold (temp >70°C/80°C, load
average > 1x/2x core count, memory >85%/95% used) — otherwise they stay small and
grey. No dependency beyond the standard library: reads `/proc/meminfo`,
`os.getloadavg()`, and `/sys/class/thermal/thermal_zone*/temp` (Linux/Raspberry Pi OS
only).

## Files

| File | Ported from |
| --- | --- |
| `main.py` | `Main.java` |
| `meter_reader.py` | `OsgpMeterReader.java` |
| `c1218.py` | `C1218Constants.java` |
| `crc16.py` | `CRC16.java` |
| `dashboard.py` | new — LAN live-readings dashboard |
| `history.py` | new — SQLite history logging, coarsening, and chart queries |
| `sysinfo.py` | new — CPU load / memory / temperature stats for the dashboard |
| `service.sh` | new — systemd service install/start/stop/restart/uninstall |
| `tests/` | new — see below |

## Config

Identical to the Java version's `config.properties`, plus one optional key:

- `logLevel` — `ERROR`, `WARNING`, `INFO` (default), `DEBUG` or `TRACE`. This is the
  equivalent of the Java version's `-Dorg.slf4j.simpleLogger.defaultLogLevel` system
  property; `TRACE` logs every frame on the wire. Use it if the meter doesn't respond.

## Deliberate differences from the Java version

Everything on the wire is byte-for-byte identical. These are the only behavioural changes,
all of them in the "the Java version would have crashed or misbehaved here" category:

1. **Truncated replies no longer kill the process.** Java's `ByteBuffer` throws an
   unchecked `BufferUnderflowException` if a reply is shorter than the parser expects,
   which would take down the whole program. Here it raises `BufferUnderflow`, which is
   caught, logged as a warning, and treated as a failed read cycle. A frame shorter than
   its 6-byte header is NACKed instead of parsed from garbage.
2. **Shutdown actually completes the logoff.** Java's shutdown hook sets the stop flag and
   returns, after which the JVM may exit while the main thread is still inside a
   `Thread.sleep`, skipping the `finally` block that logs off. Here `SIGINT`/`SIGTERM` set
   a `threading.Event` that also interrupts the long between-poll sleeps, so the
   LOGOFF/TERMINATE exchange reliably runs. The short in-protocol delays (10/20/80 ms) are
   deliberately *not* interruptible, so a shutdown can't cut a frame exchange in half.
3. **Session age uses a monotonic clock** instead of wall-clock time, so an NTP step can't
   make the session look hours old (or never expire).
4. **`idleStartTime` is parsed once at startup**, so a malformed value is reported
   immediately rather than throwing on the first poll cycle.
5. `logLevel` in the config file, as described above.

Two Java quirks were kept on purpose because they're part of the behaviour that's already
confirmed working against a real meter:

- The received-frame payload is taken as `frame[6:len(frame)-2]` rather than
  `frame[6:6+declared_length]`. These are the same for a well-formed frame.
- Table 28's declared length is read big-endian, before the meter's own byte order from
  Table 0 is applied to the value fields.

## Tests

No hardware or `pyserial` needed — the suite injects a fake `serial` module backed by a
simulated C12.18 meter.

```bash
python3 tests/test_port.py        # 25 unit tests
python3 tests/run_integration.py  # full session against the simulated meter (~7s)
```

Coverage includes the CRC-16/X-25 check value, exact request-payload bytes for
NEGOTIATE/LOGON/SECURITY/READ/READ-PARTIAL, the control-byte toggle, NACK-and-retry on a
bad CRC, multi-packet reassembly, both meter byte orders, the midnight-wrapping idle
window, and `.properties` parsing.
