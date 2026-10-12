# Hardware path 1: Linux module on a custom carrier board

Draft 2026-09-23. Companion to `architecture.md`. This describes the purpose-built
hardware that runs the `beo.networklink` and `beo.masterlink` libraries **unchanged**: a
Raspberry Pi Compute Module on a carrier that carries the MasterLink transceiver,
the audio codecs and wired Ethernet. Prices were looked up on 2026-09-23 and are
marked *verified* (seen on a shop page) or *est.* (my estimate, check before ordering).

## 1. What the box has to do

| Function | Library | Hardware it needs |
|---|---|---|
| Read the BeoSound 9000's status, send Beo4 presses | `beo.masterlink` | ML transceiver + 9-bit UART framing |
| Capture the 9000's audio | `beo.networklink.audio` (Fanout) | ADC on the ML audio pairs |
| Stream to ASE speakers, sync on PTP | `beo.networklink.roles.SourceRole`, `fps` | wired Gigabit Ethernet, a PTP-capable clock |
| Be a Mozart product with Classics Adapter for the app | `beo.mozart` | nothing extra |
| (optional) Play other sources through the 9000's speakers | `beo.networklink.roles.ListenerRole`, `audio.AlsaPlayback` | DAC + Power Link out |

Everything above is a Linux job. The one thing a Linux SoC does badly is the
MasterLink line discipline (19 200 baud, 9-bit frames done as mark/space parity,
strict inter-byte timing), so the board gets a small MCU for that. That is also
what MDTv2 does: its own microcontroller takes care of the MasterLink and
DataLink line handling and hands the Pi ordinary serial messages on the GPIO
UART ([MasterDataTool v2](https://labs.polyvection.com/mdt)).

## 2. Block diagram

```
                       ┌──────────────────────────── carrier board, 4-layer, ~100 × 80 mm ────────────────────────────┐
                       │                                                                                            │
   ML bus ──RJ45──►│ ML transceiver │──► RP2040 / STM32G0 ──UART0 (3.3 V)──►┌───────────────────────────────┐        │
   (16-core, 11    │ (line driver / │   9-bit framing, timing,             │  Compute Module 4 or 5 Lite   │        │
    used; balanced │  receiver,     │   telegram checksum                  │  Linux, Python: networklink,  │        │
    L/R + data)    │  from MDT)     │◄─────────────────────────────────────│  masterlink, mozart           │        │
                   │                │                                      │                               │        │
   ML audio pairs ─┴─► balanced ────► PCM1863 ADC ──I2S (BCLK/LRCLK/DIN)──►│  hifiberry-dacplusadcpro      │        │
                        input stage    (differential inputs)              │  overlay, 44.1 kHz S16 stereo │        │
                                                                          │                               │        │
   Power Link out ◄──8-pin DIN◄── PCM5122 DAC ◄─I2S (DOUT)────────────────│  (ListenerRole, optional)     │        │
   (to BeoLab / 9000 AUX)          + "PL on" GPIO                          │                               │        │
                                                                          │  GbE PHY on the module        │        │
   LAN ──RJ45 magjack──┬── PoE 802.3af module ── 5 V / 2.5 A ── PMIC ───►│  RGMII pads → magjack         │        │
                       └── (or USB-C 5 V)                                 └───────────────────────────────┘        │
                                                                                                                    │
   IR rx/tx (optional) ── GPIO 9/10        microSD (CM Lite)        status LEDs         USB-C (rpiboot / console)   │
                       └────────────────────────────────────────────────────────────────────────────────────────────┘
```

Why the MCU sits *between* the transceiver and the CM: `beo.masterlink` already
abstracts the transport (`MlTransport`). Today it is `SerialTransport` on the MDT
v1's FTDI USB-UART; on this board it becomes `SerialTransport("/dev/ttyAMA0")`
reading the same hex telegrams. Nothing above the transport changes. The MCU can also own
the timing windows (3.5 s, 0.3 s) later, but it does not have to.

## 3. Board areas

1. **Compute Module socket.** Two 100-pin Hirose DF40 connectors. CM4 and CM5 are
   pin-compatible for what we use (UART0, I2S, I2C, GPIO, RGMII to the on-module
   PHY, SD). Use the Lite variant and a microSD slot; eMMC only adds rpiboot
   flashing.
2. **Ethernet + power.** The PHY is on the module, so the carrier needs only the
   magjack with integrated magnetics and, for PoE, an 802.3af module
   (Silvertel Ag99xx class or equivalent, 5 V out) on the spare pairs. One cable
   to the box. Keep a USB-C 5 V input as the non-PoE alternative.
3. **MasterLink.** The original connector is a 16-pin Molex system; B&O later
   sold RJ45 adapter cables, and MDT ships a passive ML↔RJ45 adapter. Plan the
   same: RJ45 on the board, adapter in the box. The electrical spec, from the
   [MDT v1 documentation](https://masterdatatool.gitlab.io/documentation/masterlink.md):
   - **Data:** one balanced pair, **±0.25 V (0.5 Vpp)** to avoid crosstalk with
     the audio pairs. The *master* (music system or TV) supplies the logic-1
     voltage on the pair; any device, master or slave, **shorts data+ and data−
     together for a logic 0**. So the transmitter is a wired-AND switch across
     the pair (an analog switch or a MOSFET), not a line driver, and the receiver
     is a comparator on the pair. Half-duplex UART, **19 200 baud**, Tx and Rx
     joined on the same pair.
   - **Framing:** space parity on every byte except the first and last of a
     telegram, which carry mark parity. That is the telegram delimiter.
   - **ML power / detect:** a separate line the master drives at **7–15 V**
     (original manual); slaves use it to detect a plugged cable. MDT drives it at
     **5 V**, which is enough to trigger the detect circuit. For the 9000 the box
     is a *slave* (the 9000 is audio master), so the board must *sense* this line,
     and only drive it if the box should also act as master towards link speakers.
   - **MCU:** RP2040 with a PIO UART doing 9-bit frames natively, replacing MDT
     v1's FTDI + software parity switching. About a dollar.
4. **Audio in.** ML audio is **balanced, bidirectional, ±2 Vrms standard**, which
   is exactly what integrated codecs with differential inputs expect. MDT v1 uses
   the **PCM1862**, whose analog multiplexer has inputs 1–2 configured
   differential on ML and 3–4 single-ended on DataLink; PCM1863 is the same
   family with the same differential inputs. Input stage: two RC-filtered pairs
   into VINxP/VINxM, no op-amps. Clocking as on HiFiBerry DAC+ADC Pro (two
   crystals, 22.5792 / 24.576 MHz, codec as clock master) so the stock
   `hifiberry-dacplusadcpro` overlay works and the ADC delivers exactly the
   listener's rate.
5. **Audio out (optional).** Two different targets, two different DACs:
   - *Power Link out* (8-pin DIN, single-ended L/R plus "Power Link on" from a
     GPIO): PCM5122 is enough. That makes the box a BeoLink listener feeding
     BeoLab speakers or the 9000's AUX.
   - *Audio onto the ML bus* (to feed link speakers such as BeoLab 3500 the way
     MDT does): needs a **differential** DAC that goes **high-impedance in
     standby**, because ML audio is bidirectional and the bus is shared. MDT v1
     uses the **PCM5242** for exactly this: its outputs go high-impedance in
     standby, so it can sit directly on the ML connector with no analogue
     switches in between (see the MDT v1 documentation linked below). Fit PCM5242 if the
     board should ever be an ML source; it is pin-compatible in the pcm512x
     driver family.

   *A trap when reading the parts off a running Pi:* the device-tree
   compatible strings in the hifiberry overlay name pcm1863/pcm5122 regardless
   of the fitted part, and the kernel driver cannot tell 1862 from 1863 or
   5122 from 5242. The MDT documentation states PCM1862 + PCM5242.
6. **Small stuff.** IR receiver/transmitter on GPIO 9/10,
   two LEDs (link, streaming), RTC with coin cell (nice for logs, not required),
   USB-C wired to the CM's USB for rpiboot and a serial console.

## 4. Physical layout and form

```
      rear edge (all cables)                                  ~100 mm
   ┌────────────────────────────────────────────────────────────────────┐
   │ [RJ45 LAN/PoE] [RJ45 ML] [8-pin DIN PL out] [USB-C] [IR]  LED LED │
   │                                                                    │
   │   ┌───────────────────────────┐   ┌──────────┐  ┌──────────┐      │
   │   │  CM4 / CM5 (55 × 40 mm)   │   │ PoE mod. │  │ RP2040 + │      │ ~80 mm
   │   │  on DF40 sockets          │   │          │  │ ML xcvr  │      │
   │   └───────────────────────────┘   └──────────┘  └──────────┘      │
   │   [PCM1863 + input stage]   [PCM5122 + line stage]   [PMIC]       │
   │   [microSD]                                                        │
   └────────────────────────────────────────────────────────────────────┘
```

- **Size:** ~100 × 80 mm, four layers (the RGMII and I2S want a solid ground
  plane; the analog input stage wants its own ground pour tied at one point).
- **Enclosure:** extruded aluminium case, ~105 × 85 × 30 mm, both end plates
  drilled; the rear plate carries every connector so the front is blank. Aluminium
  because the CM4 runs warm at sustained 1.4 Mbit/s UDP plus ADC work and a
  fanless box wants a heat spreader from the CM to the lid.
- **Where it sits:** behind or beside the BeoSound 9000, one ML cable to the
  9000, one Ethernet cable to the switch. With PoE there is no power supply at
  the box.
- **Mounting:** two keyhole slots for wall mounting next to a wall-hung 9000c,
  rubber feet for the shelf.

## 5. Bill of materials with prices

Per unit, at a batch of 5. Prices as seen on 2026-09-23; currency as the shop shows it.

| # | Part | Qty | Price | Status | Source |
|---|---|---|---|---|---|
| 1 | Raspberry Pi CM4 Lite 1 GB (no wireless) | 1 | from $41.25 | verified (official "from" price) | raspberrypi.com |
| 1' | …or CM4 Lite 2 GB (CM4002000) | 1 | $57.50 | verified | pishop.us |
| 1'' | …or CM5 Lite 2 GB | 1 | $45 (official); $80 at SparkFun, out of stock | verified | raspberrypi.com, sparkfun.com |
| 2 | PCM1863 ADC (TSSOP-30), differential inputs; PCM1862 as on MDT v1 is the same family | 1 | ~€4.00 | verified | mouser (hr) |
| 3 | PCM5122 DAC (TSSOP-28) for Power Link out; PCM5242 (differential, HiZ in standby) if the box must drive ML audio | 1 | ~$6.54 (5122) | verified | mouser |
| 4 | RP2040 + 2 MB QSPI flash + 12 MHz crystal | 1 | ~$2.50 | est. | — |
| 5 | ML transceiver: comparator receiver on the ±0.25 V pair, analog-switch/MOSFET wired-AND transmitter, 5 V ML-power sense/drive, ESD protection | 1 | ~$4 | est. | spec from MDT v1 docs |
| 6 | Balanced input stage + DAC line stage (op-amps, passives) | 1 | ~$6 | est. | — |
| 7 | Crystals 22.5792 + 24.576 MHz, clock switch | 1 set | ~$3 | est. | — |
| 8 | Gigabit magjack RJ45 with magnetics | 1 | ~$4 | est. | — |
| 9 | PoE 802.3af module, 5 V out (Silvertel Ag99xx class) | 1 | ~$12 | est. | — |
| 10 | RJ45 for ML + 8-pin DIN + USB-C + microSD + 2× DF40 | 1 set | ~$10 | est. | — |
| 11 | 5 V → 3.3 V / 1.8 V regulators, PMIC, protection | 1 set | ~$5 | est. | — |
| 12 | PCB 4-layer ~100 × 80 mm + SMT assembly, 5 pcs | 1 | ~$25–40 per board | est. | jlcpcb.com quote tool |
| 13 | Aluminium enclosure ~105 × 85 × 30 mm, machined end plates | 1 | ~$20 | est. | — |
| 14 | microSD 16 GB | 1 | ~$6 | est. | — |
| | **Per unit, CM4 Lite 1 GB, batch of 5** | | **≈ $150–165** | | |
| | **Per unit, CM5 Lite 2 GB, batch of 5** | | **≈ $155–170** | | |

Not included: the ML cable, the ML→RJ45 passive adapter if the 9000 side has the
old plug, EDA time, and one prototype spin that goes wrong.

### Reference: the buy-instead path

| Part | Price | Status | Source |
|---|---|---|---|
| MasterDataTool v2 HAT (PCB, housing, ML→RJ45 adapter; ADC+DAC on board, HiFiBerry DAC+ADC Pro compatible) | €180 excl. VAT outside DE | verified | labs.polyvection.com/mdt |
| Raspberry Pi 4 2 GB or Pi 5 2 GB | ~$45–50 | est. | — |
| HiFiBerry DAC+ ADC Pro (only if you do not use MDTv2's codecs) | $74.90 | verified | hifiberry.com |
| Pi Zero 2 W (no Ethernet; not recommended) | $15 official, sold out everywhere | verified | raspberrypi.com |

So MDTv2 + Pi 4 ≈ €230, complete and shipped, versus ≈ $160 per unit for a custom
board that you still have to design, verify and case. The custom board only pays
off for a small series (10+), for PoE and a single-cable install, or for the
Power Link out.

## 6. Two tiers of "custom"

| Tier | What is custom | Effort | Software change |
|---|---|---|---|
| **A: stack** | nothing; CM5 IO board ($20) or Pi 5 + MDTv2 + PoE HAT ($20.95) | days | none (`SerialTransport` on the MDT's UART) |
| **B: carrier** | the board in §2–§4 | a design spin, 2–3 months elapsed | `SerialTransport("/dev/ttyAMA0")`; `linuxptp` for a real PTP clock |

Tier A is the way to get PoE and a tidy box *now*. Tier B is the way to get a
product.

## 7. Risks, in order

1. **ML electrical details beyond the MDT v1 text.** The levels (±0.25 V data,
   wired-AND logic 0, ±2 Vrms balanced audio, 7–15 V ML power) are documented;
   the exact pin-to-pair assignment on the 16-pin Molex / RJ45 adapter, the
   pull-up impedance the master presents, and the comparator threshold are not.
   One session with a scope on the existing MDT v1 RJ45 (data pair, ML power,
   one audio pair) closes all three before the transceiver is drawn. Not a
   design risk any more, a measurement task.
2. **9-bit UART timing.** Per-byte parity switching on an FTDI works but is slow. RP2040 PIO removes that, but the framing must
   be validated against a recorded Master Link session before the FTDI path is retired.
3. **Clocking the codec.** The ADC must deliver exactly 44 100 frames/s — the
   only rate sent, to every product; the listener has no tolerance for a slow
   drift. Codec as master with its own crystal, as HiFiBerry does.
4. **PTP.** `beo.networklink.ptp.PtpSlaveClock` already follows the products'
   own PTPv2 in software, good to a few milliseconds. On this board `linuxptp`
   (`ptp4l`) with hardware timestamps could replace it. Whether `ptp4l` can
   follow the products' grandmaster on its non-standard ports and group is
   unmeasured; the software slave is the fallback, and works today.
5. **Supply.** Zero 2 W is sold out; CM5 Lite is out of stock at some resellers.
   Design for CM4 *and* CM5 (same socket) so either can be fitted.

## 8. What changes in the software

- `beo.masterlink.bus.SerialTransport` on `/dev/ttyAMA0`: same hex telegrams.
- `beo.networklink.ptp.PtpSlaveClock` backed by `linuxptp` (`/dev/ptp0` or the system
  clock disciplined by `ptp4l`), same `FpsClock` interface.
- Device tree: `hifiberry-dacplusadcpro` overlay unchanged; `dtoverlay=uart0`
  for the MCU; `gpio-ir` for the IR pair.
- Nothing in `beo.networklink.roles`, `beo.mozart` or the adapter.

Sources: [MDT v1 documentation: MasterLink](https://masterdatatool.gitlab.io/documentation/masterlink.md) ·
[MDT v1: Functionality](https://masterdatatool.gitlab.io/documentation/functionality.md) ·
[MDT v1: DataLink](https://masterdatatool.gitlab.io/documentation/datalink.md) ·
[Compute Module 4](https://www.raspberrypi.com/products/compute-module-4/) ·
[CM4 2GB Lite at PiShop](https://www.pishop.us/product/raspberry-pi-compute-module-4-2gb-lite-cm4002000/) ·
[CM5 on sale from $45](https://www.raspberrypi.com/news/compute-module-5-on-sale-now/) ·
[CM5 Lite 2GB at SparkFun](https://www.sparkfun.com/raspberry-pi-compute-module-5-lite-2gb-ram.html) ·
[PCM1863 at Mouser](https://hr.mouser.com/ProductDetail/Texas-Instruments/PCM1863DBTR?qs=p08Uhw9w2Fw6cu3HZCtErQ%3D%3D) ·
[PCM5122 at Mouser](https://www.mouser.com/ProductDetail/Texas-Instruments/PCM5122PWR?qs=9Zb5cNRUa0a9XiLHvnPMsg%3D%3D) ·
[MasterDataTool v2](https://labs.polyvection.com/mdt) ·
[HiFiBerry DAC+ ADC Pro](https://www.hifiberry.com/shop/boards/hifiberry-dac-adc-pro/) ·
[Waveshare CM4 PoE base board](https://www.waveshare.com/compute-module-4-poe-board.htm) ·
[CM5 IO Board](https://www.raspberrypi.com/products/compute-module-5-io-board/) ·
[PoE HAT (G) for Pi 5/CM5](https://www.pishop.us/product/poe-hat-g-compatible-with-raspberry-pi-5-cm5-5v-5a-output-802-3af-at/) ·
[Pi Zero 2 W](https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/) ·
[MasterLink (Beoworld)](https://beoworld.org/masterlink/) ·
[JLCPCB](https://jlcpcb.com/)
