# Reference documents — already downloaded & still to fetch manually

## Status update 2026-09-22 (late): user fetched most manual items

Obtained and processed (findings folded into ../PROTOCOL-NOTES.md):
- ✅ SAKO pairing video (`300A lithium battery communication setting.mp4`) — battery-side "RS485-SAKO" selection, port labels; frames analyzed
- ✅ SUNON IV manual PDF (scanned; not yet mined — SUNON V manual on manuals.plus turned out to be an abridged summary without the program table)
- ✅ Seplos compatibility list (`BMS_SEPLOS_Inverter_Compatibility_Types_and_Models_V3.pdf` → `seplos-compat.txt`) — no SAKO row; Voltronic needs dedicated firmware
- ✅ Seplos BMS 3.0 firmware V1.5D [20251118] RAR — "Added inverter communication protocols"; V10/V11 hardware warning
- ✅ Mason 280L (3.0) pack spec (→ `mason280l-spec.txt`) — official port pinout (conflicts with forum lore; see PROTOCOL-NOTES)
- ✅ tt-sun "新增协议设计" PDF — byte-identical to the Akkudoktor mirror we had; extracted to `pylontech-v35-design.txt` = full 6x-family spec incl. ADR 0x12 rule and 0x60 layout
- ➖ openenergymonitor emulator PDF (`starting-new-work.pdf`) — dud: CAN/Victron emulator notes, no RS485 protocol content

Still open: the DIY Solar Forum / Photovoltaikforum threads below (browser only), and the SUNON V's real program table (best source now: the SUNON IV scanned manual, or the inverter's own menu once on the bench).

## Already in this folder (scripted download, 2026-09-22)

| File | What it is |
|---|---|
| `Pylontech-RS485-protocol-low-voltage-V3.3-20180821.pdf` | The de-facto Pylontech RS485 reference (unofficial), 4x command family |
| `Pylontech-RS485-V2.8-20161216.pdf` | Older V2.8 of the same protocol |
| `Pylontech-RS485-akkudoktor-mirror.pdf` | 25-page forum mirror (version-history table) |
| `BASEN-BMS-protocol.pdf` | Same YD/T 1363.1 frame family, readable RTN/frame tables |
| `Seplos-BMS3.0-Modbus-RTU-Protocol.pdf` + `Seplos-BMSv3-modbus-registers.xlsx` + `seplosv3-pia.md`/`pib.md` | **Seplos BMS 3.0 native protocol** (Modbus RTU, 19200, slave 0) |
| `Voltronic-Inverter-BMS-485-protocol-20201202.docx` (+ extracted `.txt`) | **The authoritative Voltronic BMS-485 spec** (Modbus-style, 9600, node 1) — what "LIB" most likely is |
| `Voltronic-BMS-Modbus-RS485-V1.1-2018.pdf` | Earlier Voltronic BMS Modbus doc |
| `fahmula_pylontech_rs485.cpp/.h` | Field-tested Pylontech battery emulator (0x61/62/63 layouts, RTN semantics) |
| `battery_emulator_PYLON-LV-RS485.cpp/.h` | Battery-Emulator's implementation (note: this checkout echoes CID2 — the very bug PR #3003 fixed; kept as a dialect example) |
| `python_pylontech_pylontech.py` | python-pylontech master-side schemas (0x42/47/51/92/93 layouts) |
| `daren-485-README.md` | Best plain-language write-up of this frame family |

## Fetch manually in a browser (captcha / registration / bot-blocked)

1. **SAKO SUNON V user manual** — https://manuals.plus/asin/B0FBRNPSZF
   (Cloudflare check). Fallback: SUNON IV manual (closest sibling, direct):
   https://sakopower.com/wp-content/uploads/2024/04/510-058000-003_SUNON-IV-4.2KVA-6.2KVA-User-Manual.pdf
2. **YouTube: "SAKO Sunon-V 6.5Kva … SAKO Li-Sun battery communication
   setting"** — https://www.youtube.com/watch?v=Wk0SlLMHXHU — likely shows
   the exact SAKO menu numbers, protocol code and socket. **Highest-value
   single item for this problem.**
3. **Seplos inverter compatibility list** (check SAKO row + interface):
   XLS: https://device.report/m/66c0281dcf281acfd02e3aca76114d25a94d5272229c99616948e3b8eb4ce2a6
   PDF mirror (image scan): https://bezpeka.club/system/storage/download/BMS_SEPLOS_Inverter_Compatibility_Types_and_Models_V3.pdf
4. **Seplos BMS 3.0 firmware page** (newer builds may fix PYLON/Voltronic
   RS485): https://www.seplos.com/bms-firmware.html — was DNS-blocked from
   this machine's sandbox; also
   https://www.seplos.com/how-to-upgrade-the-seplos-bms-3.0-firmware.html
5. **Seplos Mason 280L (3.0) pack specification** (port/pin diagram,
   image-based PDF): https://qion.nl/wp-content/uploads/2024/07/SEPLOS-MASON-280L（3.0）BATTERY-PACK-SPECIFICATION.pdf
6. **openenergymonitor "PylonTech Battery Emulator — Function
   Documentation with RS485 Overview"** (0x60–0x63 payload doc):
   https://community.openenergymonitor.org/uploads/short-url/9SxgOy5ZdWvhj8VQmsx7yO60rIK.pdf
7. **tt-sun.com Chinese V3.5-era design note** ("…新增协议设计…9600.pdf",
   closest thing to an official 6x-family doc):
   https://tt-sun.com/wp-content/uploads/2025/03/PYLON-RS485-protocol-pylon-low-voltage-新增协议设计-dy12号9600.pdf
8. **DIY Solar Forum threads** (bot-blocked, readable in browser):
   - https://diysolarforum.com/threads/seplos-3-0-bms-maybe-and-voltronic-rs485.70566/
   - https://diysolarforum.com/threads/seplos-rs485-inverter-voltronic.47948/
   - https://diysolarforum.com/threads/battery-bms-for-sako-inverter.93892/
   - https://diysolarforum.com/threads/pylontech-rs485-protocol.37510/
9. **Photovoltaikforum (German)**: FSP/Voltronic + Seplos thread —
   https://www.photovoltaikforum.com/thread/223093-fsp-wechselrichter-voltronic-und-seplos-batterie/

## Key upstream repos (for updates / deeper reference)

- https://github.com/Fahmula/esphome-pylontech-rs485 (battery emulator)
- https://github.com/nkinnan/esphome-pace-bms (most rigorous frame-family impl, VER 0x20/0x25 dialects)
- https://github.com/Frankkkkk/python-pylontech (master side)
- https://github.com/Tomcat42/pylontech-python (clean layering)
- https://github.com/dalathegreat/Battery-Emulator (PR #3003 = RTN/CID2 fix)
- https://github.com/marcelrv/seplosBMSv3 (Seplos V3 Modbus map)
- https://github.com/syssi/esphome-seplos-bms (Seplos V2+V3)
- https://github.com/ardupic/voltronic-inverter-communication-protocols
- https://github.com/jblance/mpp-solar (Voltronic inverter-side PI protocols)
- https://github.com/cpttinkering/daren-485 (frame-family write-up)
- https://github.com/sk8tz/Pylontech-Battery-Suite (C#/.NET simulator suite, young)
