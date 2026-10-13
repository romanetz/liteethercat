# Colorlight 5A-75E EtherCAT Slave Controller

FPGA-based EtherCAT Slave Controller for Colorlight 5A-75E (Lattice ECP5, LFE5U-25F),
written in Migen/LiteX from scratch. Targets the same niche as LiteX-CNC and Mesa —
open EtherCAT I/O for the LinuxCNC community.

![Status](https://img.shields.io/badge/status-in%20development-yellow)
![License](https://img.shields.io/badge/license-GPL--3.0-blue)
![Platform](https://img.shields.io/badge/platform-Lattice%20ECP5-green)

## Status

- [x] Ethernet PHY (RGMII, RTL8211F) on both ports
- [x] PHY auto-config over shared MDIO
- [x] EtherCAT frame forwarding between ports
- [x] Datagram parser (on-the-fly, single cycle)
- [x] Register file (Type, AL Control/Status, DL Status, etc.)
- [x] FMMU (4 channels)
- [x] SyncManager (4 channels, 3-buffer shadow)
- [x] SII EEPROM emulation (in progress)
- [ ] AL state machine INIT → PREOP → SAFEOP → OP
- [ ] Distributed Clocks
- [ ] Mailbox / CoE

## Why

Give the LinuxCNC community another open EtherCAT slave option alongside LiteX-CNC and Mesa.
Similar in spirit to what those projects do, but targeting a cheap Colorlight board.

## Structure

- `ethercat_pu.py` — datagram parser (processing unit)
- `ethercat_memory.py` — unified register + DPRAM memory
- `ethercat_registers.py` — ESC register file
- `ethercat_fmmu.py` — FMMU
- `ethercat_sm.py` — SyncManager
- `ethercat_sii.py` — SII EEPROM emulation
- `ethercat_dl_router.py` — port router
- `ethercat_soc.py` — top-level SoC
- `siitool/` — SII binary generation tool

## License

GPL-3.0 (see LICENSE)

## Current status
=== Master 0, Slave 0 ===
Device: Main
State: INIT
Flag: E
Identity:
  Vendor Id:       0xdeadbabe
  Product code:    0xdeadbabe
  Revision number: 0x00c0ffee
  Serial number:   0x00000000
DL information:
  FMMU bit operation: no
  Distributed clocks: no
Port  Type  Link  Loop    Signal  NextSlave
   0  MII   up    open    yes             -
   1  MII   down  closed  no              -
   2  N/A   down  closed  no              -
   3  N/A   down  closed  no              -
Mailboxes:
  Bootstrap RX: 0x0000/0, TX: 0x0000/0
  Standard  RX: 0x0000/0, TX: 0x1000/128
  Supported protocols: (should be CoE)

## Roadmap

- [x] RGMII PHY (RTL8211F) with auto-config over shared MDIO
- [x] EtherCAT frame forwarding between ports
- [x] On-the-fly datagram parser
- [x] FMMU (4 channels)
- [x] SyncManager (4 channels, 3-buffer shadow)
- [ ] SII EEPROM interface
- [ ] AL state machine INIT → PREOP → SAFEOP → OP
- [ ] Distributed Clocks
- [ ] Mailbox / CoE
- [ ] Multi-slave bus
