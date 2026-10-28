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

## EQ on the ATH-M50xBT2

"EQ V2", 5 band GEQ at 82 / 247 / 1000 / 4000 / 12500 Hz (Q 0.8/0.7/0.7/0.7/0.8,
global gain −6 dB). Presets are EQ banks:

| bank | preset         |
|------|----------------|
| 3    | Original       |
| 5    | Bass Boost     |
| 6    | Clear Vocal    |
| 4    | V-shaped       |
| 7    | Treble Enhance |
| 1    | MySetting1 (custom) |

Writing custom band gains requires the biquad coefficient blob produced by
`DspConfig.calculateEqNative` in `libDspConfig.so`; not implemented yet.
