# Colorlight 5A-75E EtherCAT Slave Controller

FPGA-based EtherCAT Slave Controller for Colorlight 5A-75E (Lattice ECP5, LFE5U-25F),
written in Migen/LiteX from scratch.

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
