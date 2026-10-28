# Audio-Technica Connect protocol notes

Reverse engineered from *Audio-Technica | Connect* 2.8.0 for Android
(`jp.co.audiotechnica.connect`, React Native + Hermes bytecode v96) and verified
against an ATH-M50xBT2.

## App architecture

The app supports ~25 models built on three chip families. Each model definition
(Hermes bundle) names a `protocolType`:

| protocol  | stack in the APK                                            | example models          |
|-----------|-------------------------------------------------------------|-------------------------|
| `GaiaV2`  | `com.qualcomm.gaiacontrol`, `com.audiotechnica.gaiav2`      | ATH-M50xBT, ANC900BT    |
| `GaiaV3`  | `com.qualcomm.qti.gaiaclient`, `com.audiotechnica.gaiav3`   | newer Qualcomm TWS      |
| `Airoha`  | `com.airoha.sdk` (RACE protocol), `com.audiotechnica.airoha`| Airoha AB156x/158x      |
| `RTK`     | `com.realsil.sdk.bbpro` ("BeePro"), `com.audiotechnica.rtk` | **ATH-M50xBT2**         |

All UI and most of the command logic lives in JavaScript; the Java side
(`RNRtkModule`, `RNRtkEqModule`) only wraps the Realtek SDK. The JS `Rtk` class
issues Audio-Technica vendor commands through `RtkModule.sendCommand(subId, bytes)`.

## Transport

RFCOMM. Realtek parts normally expose the vendor service
`6a24eeab-4b65-4693-986b-3c26c352264f`; the ATH-M50xBT2 does not advertise it
and speaks the protocol on its plain **Serial Port** record (`0x1101`, RFCOMM
channel 6 on the tested unit).

### Framing (`TransportLayerPacket`)

```
AA  seq:u8  len:u16le  opcode:u16le  params[len-2]
```

* No checksum. `seq` counts 1..255 and wraps to 1; every packet we send,
  including ACKs, takes the next number.
* Opcode `0x0000` is an ACK: `params = acked_opcode:u16le status:u8`
  (0 complete, 1 disallow, 2 unknown command, 3 bad params, 4 busy, 5 failed).
* The receiver ACKs every non-ACK packet (except opcode `0x0301`).
  The SDK resends a command up to 2 times if no ACK arrives within 1 s.
* A packet repeating the previous `seq` is a retransmission: ACK it again but
  do not process it.

### Realtek generic commands used

| request                      | params            | answer event                                         |
|------------------------------|-------------------|------------------------------------------------------|
| `0x0004` MMI                 | `00 action`       | – (`0x51` pairing, `0x52` stop pairing, `0x5a` factory reset, `0x5b` clear pairings) |
| `0x000c` get cmd set info    | `00`              | `0x0011`: `00 00 cmdset:u16be [eqspec:u16be]`        |
| `0x0012` set cfg             | `type len data`   | – (type 1 = BR/EDR name, 0 = LE name)                |
| `0x0017` get cfg             | `type`            | `0x0018`: `type len data`                            |
| `0x0018` get status          | `index`           | `0x0019`: `index v1 v2` (index 2 = battery %, 0xa1 = volume) |
| `0x0200` EQ query            | `00`              | `0x0200`: `00 enabled`                               |
| `0x0201` / `0x0202`          | –                 | EQ enable / disable                                  |
| `0x0206` EQ get index        | `[mode]` if spec≥4| `0x0202`: `index [mode] [scene]`                     |
| `0x0205` EQ set index        | `index [mode]`    | –                                                    |
| `0x0308` get FW version      | `04` or `00`      | `0x0909` – rejected (status 2) by the M50xBT2; the app reads the version through the DFU SDK instead |

EQ spec version comes from the cmd set info: if the 6-byte form is absent,
cmdset `0x0100..0x0104` maps to spec 1..5. The M50xBT2 reports cmdset `0x0101`
(spec 2), so the index commands take no `mode` byte.

## Audio-Technica vendor commands (opcode `0x0800` / `0x8800`)

```
request   0x0800  sub:u16be  payload...
response  0x8800  sub:u16be  status:u8  payload...
notify    0x8800  sub:u16be  payload...           (sub in the notification table)
```

The full sub command table is `At`/`AtNotify` in `atconnect/protocol.py`.
Commands relevant to the ATH-M50xBT2 (all getters answer one byte unless noted):

| get / set   | meaning           | values                                                    |
|-------------|-------------------|-----------------------------------------------------------|
| 21 / 22     | codec preference  | 0 auto, 2 AAC, 3 SBC, 4 LDAC (applies after reconnect)    |
| 23          | streaming codec   | 1 SBC, 2 AAC, 3 aptX, 4 aptX LL, 5 aptX HD, 6 LDAC, 7 aptX Adaptive, 8 LHDC, 9 LC3 |
| 41 / 42     | voice assistant   | 0 phone default, 1 Alexa, 2 Google                        |
| 47 / 48     | volume step       | raw byte                                                  |
| 52 / 53     | volume balance    | signed byte                                               |
| 54 / 55     | multipoint        | 0/1                                                       |
| 59 / 60     | voice prompts     | 0 voice, 1 beep                                           |
| 64 / 65     | low latency       | 0/1; unavailable with codec auto/LDAC/LHDC                |
| 96 / 97     | sidetone          | 2 bytes: `on gain` (gain 0 low, 1 high)                   |

## EQ on the ATH-M50xBT2 ("EQ V2")

The app keeps its presets locally and writes the selected curve into **bank 5**
(`eqV2Bank`). Presets are 5-band graphic EQs at 82 / 247 / 1000 / 4000 / 12500 Hz,
Q 0.8 / 0.7 / 0.7 / 0.7 / 0.8, pre-gain −6 dB:

| preset         | gains (dB)               |
|----------------|--------------------------|
| Original       | 0 0 0 0 0                |
| Bass Boost     | 4.5 −3 −1 0 1.5          |
| Clear Vocal    | −3 1.5 1 4 1.5           |
| V-shaped       | 3 1 −2 1 3               |
| Treble Enhance | −2 0.5 −0.5 2 4          |

(The banks 1–4/6/7 table in the model definition belongs to the older "EQ V1"
flow and is not used with firmware > 0x1003.)

Writing a curve (EQ spec 2, what `RNRtkEqModule.setEqParams` ends up sending):

1. JS adjusts Q per band: `q' = round(q · 10^(|gain|/40), 1)`.
2. For 44.1 kHz (rate code 3) and 48 kHz (code 4) compute `eqData` with
   `DspConfig.calculateEqNative` and send
   `0x0203  bank 00 00 len:u16le  rate  eqData` (len = 1 + len(eqData)).
3. `0x0205 bank` selects the bank, AT `61` (`SAVE_EQ_PARAMETER`) `bank` persists it.

The EQ itself is switched with `0x0201` / `0x0202` and queried with
`0x0200 00` → `0x0200 00 enabled`. Toggling it makes the headphones restart the
A2DP stream (the app waits for a reconnect when the codec is Auto/LDAC).

`0x0204 bank` reads a bank back: `bank xx xx len:u16le rate eqData`, where the
data is padded to 10 stages and the rate byte is not meaningful (reads 1).

### eqData format (libDspConfig `calculateEq` → `getSpkEqData`)

```
u32  (SignedQuant(preGain · Π scale_i, 24 bits, 12 frac) << 8) | 0x40 | stages
per stage: b0 b1 b2 a1 a2   as i32 Q2.30 (saturating)
```

Each stage is the "simple" peaking filter (Orfanidis), not the RBJ cookbook one:

```
G  = 10^(gain/20),  w0 = 2π·f/fs,  t = w0 / (2Q·min(G, 1))
β  = −½·(1−t)/(1+t)
b1 = a1 = −2·cos(w0)·(½ − β),  a2 = −2β
b0 = 1 + 2(G−1)(¼ + β/2),      b2 = −2β − 2(G−1)(¼ + β/2)
if any |b| ≥ 2: divide b0..b2 by the largest |b| and multiply the pre-gain by it
b1, b2, a1, a2 are then rounded to the Q30 grid
```

`atconnect/eq.py` reproduces the native library byte for byte (checked against
the armeabi-v7a `libDspConfig.so` run under an emulator).

## Firmware version

`0x0308` is rejected by the M50xBT2. The app reads the version through the OTA
service instead (`RtkDfuModule.getAppUiVersion`):

* `0x0600` → target info; for DFU spec ≤ 5 bytes 8..11 are a 2-bit-per-image
  indicator.
* `0x0601` (no parameters) → 4-byte versions of the present images, in image
  order. Image 9 is the app config ("UI parameter") image.
* The app shows/compares `((v>>12)&0xF000) + ((v>>8)&0x0F00) + ((v>>4)&0xF0) + (v&0xF)`,
  e.g. `0x01000008` → `0x1008` → **1.0.0.8**. Feature gates in the model
  definition (sidetone, EQ) test `version > 0x1003`.

## Multipoint

What the ATH-M50xBT2 firmware 1.0.0.8 actually does:

| command                                   | result                                        |
|-------------------------------------------|-----------------------------------------------|
| AT 54 / 55 multipoint on/off              | works                                         |
| AT 77 number of app connections           | works                                         |
| AT 80–91 paired list/info/connect/…/pair  | ACKed, never answered (not implemented)       |
| Realtek `0x0315` multilink connection num | unknown command                               |
| MMI `0x51` enter pairing                  | works, **drops the link to this computer**    |
| MMI `0x53` link last device               | accepted, link kept                           |

The official app does not offer device management for this model either
(`multipointManagement` is absent from its feature list).

The headphones do report link changes on their own:

```
0x0003  profile_mask  addr[6] (little endian)              profile connected
0x0004  profile_mask  addr[6] (little endian)  reason:u16  profile disconnected
```

Profile mask `0x01` = A2DP (verified); the remaining bits follow the Realtek
profile mask (AVRCP 0x02, HFP 0x04, HSP 0x08, SPP 0x10, iAP 0x20).

For firmwares that implement it, the management protocol is:

| AT  | request payload                 | response payload                                   |
|-----|---------------------------------|----------------------------------------------------|
| 80  | –                               | `n addr[6]·n`                                      |
| 81  | `addr[6]`                       | `addr[6] name_len name conn_profiles active supported_profiles` |
| 82  | `addr[6]`                       | – (connect)                                        |
| 83  | `addr[6]`                       | – (disconnect)                                     |
| 84  | `addr[6] profile connect`       | – (profile 1 = A2DP, 2 = HFP)                      |
| 87  | `addr[6]`                       | – (forget)                                         |
| 89 / 90 | –                           | – (enter / cancel pairing without dropping links)  |
| 91  | –                               | `addr[6]` (address of the app's host)              |

Notifications: 85 `addr conn_profiles active?`, 86 `addr` (active device),
88 `addr` (removed). Addresses here are big endian (`AA:BB:…` order).
