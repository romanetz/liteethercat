#!/usr/bin/env python3
"""
EtherCAT Slave Controller for Colorlight 5A-75E
With Clock Domain Crossing (CDC) FIFOs and full ESC functionality.

Architecture (P0 -> P1):
    PHY0 RX (ethphy0_eth_rx) --[cdc0]--> EtherCATSlave (ethphy1_eth_tx domain)
        --> PHY1 TX

Architecture (P1 -> P0):
    PHY1 RX (ethphy1_eth_rx) --[cdc1]--> (ethphy0_eth_tx) --> PHY0 TX
    (transparent)

The ESC detects EtherType 0x88A4, parses datagrams (APRD/APWR/FPRD/FPWR/...),
manages ESC register space, FMMU, AL state machine, DC registers, and exposes:
    - is_ethercat    : high during EtherCAT frame
    - ecat_header    : 16-bit EtherCAT frame header
    - ethercat_count : total detected EtherCAT frames
    - al_state       : current AL state
    - wkc            : working counter (last)

Build:
    python3 ethercat_forwarder_cdc.py --build
Load:
    python3 ethercat_forwarder_cdc.py --load
"""

from migen import *
from litex.build.generic_platform import *
from litex_boards.platforms import colorlight_5a_75e
from litex.soc.cores.clock import *
from litex.soc.integration.soc_core import *
from litex.soc.integration.builder import *
from litex.soc.cores.led import LedChaser
from litex.soc.interconnect import stream
from litex.soc.interconnect.stream import ClockDomainCrossing
from liteeth.phy.ecp5rgmii import LiteEthPHYRGMII
from liteeth.common import eth_phy_description

from ethercat_top import EtherCATSlave


# CRG --------------------------------------------------------------------------

class _CRG(Module):
    def __init__(self, platform, sys_clk_freq):
        self.rst = Signal()
        self.clock_domains.cd_sys = ClockDomain()
        clk25 = platform.request("clk25")
        platform.add_period_constraint(clk25, 1e9/25e6)
        self.submodules.pll = pll = ECP5PLL()
        self.comb += pll.reset.eq(self.rst)
        pll.register_clkin(clk25, 25e6)
        pll.create_clkout(self.cd_sys, sys_clk_freq)
        platform.add_false_path_constraint(self.cd_sys.clk, clk25)


# EtherCAT Frame Forwarder with CDC + ESC --------------------------------------

class EtherCATFrameForwarder(Module):
    def __init__(self, phy0, phy1):
        self.frames_p0_to_p1 = Signal(32)
        self.frames_p1_to_p0 = Signal(32)
        #self.is_ethercat = Signal()
        #self.ecat_header = Signal(16)
        #self.ethercat_count = Signal(32)
        self.al_state = Signal(8)

        fifo_layout = [("data", 8), ("last_be", 1), ("error", 1)]

        self.submodules.cdc0 = ClockDomainCrossing(
            fifo_layout, cd_from="ethphy0_eth_rx", cd_to="ethphy1_eth_tx", depth=16)
        self.submodules.cdc1 = ClockDomainCrossing(
            fifo_layout, cd_from="ethphy1_eth_rx", cd_to="ethphy0_eth_tx", depth=16)

        self.submodules.slave = ClockDomainsRenamer("ethphy1_eth_tx")(EtherCATSlave())

        self.comb += [
            phy0.source.connect(self.cdc0.sink),
            self.cdc0.source.connect(self.slave.sink),
            self.slave.source.connect(phy1.sink),
            #self.is_ethercat.eq(self.slave.is_ethercat),
            #self.ecat_header.eq(self.slave.ecat_header),
            #self.ethercat_count.eq(self.slave.ethercat_count),
            #self.al_state.eq(self.slave.al_state),
            phy1.source.connect(self.cdc1.sink),
            self.cdc1.source.connect(phy0.sink),
        ]

        cnt_tx = Signal(32)
        self.sync.ethphy1_eth_tx += If(
            self.cdc0.source.valid & self.cdc0.source.ready & self.cdc0.source.last_be,
            cnt_tx.eq(cnt_tx + 1))
        self.specials += MultiReg(cnt_tx, self.frames_p0_to_p1, "sys")

        #self.sync += [
        #    If(self.cdc0.source.valid & self.cdc0.source.ready & self.cdc0.source.last_be,
        #        self.frames_p0_to_p1.eq(self.frames_p0_to_p1 + 1)),
        #    If(self.cdc1.source.valid & self.cdc1.source.ready & self.cdc1.source.last_be,
        #        self.frames_p1_to_p0.eq(self.frames_p1_to_p0 + 1)),
        #]

class EtherCATDL(Module):
    """Data Link: 2 порта, loop control, разворот при закрытой петле."""
    def __init__(self, phy0, phy1, slave):
        self.loop0_cfg  = Signal(2)   # 0x0100[9:8]
        self.loop1_cfg  = Signal(2)   # 0x0100[11:10]
        self.link0_up   = Signal()
        self.link1_up   = Signal()
        self.loop0_closed = Signal()
        self.loop1_closed = Signal()

        # Auto(00): closed при link down. Open(10)/Closed(11) — принудительно.
        for cfg, link, closed in [(self.loop0_cfg, self.link0_up, self.loop0_closed),
                                  (self.loop1_cfg, self.link1_up, self.loop1_closed)]:
            self.comb += Case(cfg, {
                0b00: closed.eq(~link),
                0b01: closed.eq(~link),      # auto close: упрощённо как auto
                0b10: closed.eq(0),
                0b11: closed.eq(1),
            })

        # Выход PU: в порт 1, либо разворот в порт 0 если петля 1 закрыта
        self.submodules.pu_out = pu_out = stream.Demultiplexer(
            eth_phy_description(8), 2)
        self.comb += [
            slave.source.connect(pu_out.sink),
            pu_out.sel.eq(self.loop1_closed),        # 0 → phy1, 1 → обратно в phy0
            pu_out.source0.connect(phy1.sink),
        ]

        # PHY0.TX: арбитраж между транзитом (cdc1) и развёрнутым кадром PU
        self.submodules.tx0_arb = tx0_arb = stream.Multiplexer(
            eth_phy_description(8), 2)
        self.comb += [
            cdc1.source.connect(tx0_arb.sink0),
            pu_out.source1.connect(tx0_arb.sink1),
            tx0_arb.sel.eq(self.loop1_closed),
            tx0_arb.source.connect(phy0.sink),
        ]



# BaseSoC ----------------------------------------------------------------------

class BaseSoC(SoCCore):
    def __init__(self, revision="6.0", sys_clk_freq=int(50e6), **kwargs):
        platform = colorlight_5a_75e.Platform(revision=revision)
        self._create_eth1_no_shared(platform, revision)

        kwargs["cpu_type"] = None
        kwargs["integrated_sram_size"] = 0
        kwargs["integrated_rom_size"] = 0
        kwargs["with_uart"] = False
        kwargs["with_timer"] = False

        SoCCore.__init__(self, platform, sys_clk_freq,
            ident=f"EtherCAT Slave on Colorlight 5A-75E rev{revision}", **kwargs)

        self.submodules.crg = _CRG(platform, sys_clk_freq)

        self.submodules.ethphy0 = LiteEthPHYRGMII(
            clock_pads=platform.request("eth_clocks", 0),
            pads=platform.request("eth", 0),
            tx_delay=0e-9, rx_delay=0e-9, with_hw_init_reset=True)

        self.submodules.ethphy1 = LiteEthPHYRGMII(
            clock_pads=platform.request("eth_clocks", 1),
            pads=platform.request("eth1_no_shared", 0),
            tx_delay=0e-9, rx_delay=0e-9, with_hw_init_reset=False)

        self.submodules.forwarder = EtherCATFrameForwarder(self.ethphy0, self.ethphy1)

        self.submodules.leds = LedChaser(
            pads=platform.request_all("user_led_n"),
            sys_clk_freq=sys_clk_freq)

    def _create_eth1_no_shared(self, platform, revision):
        try:
            if hasattr(platform, "constraint_manager"):
                resources = platform.constraint_manager.available
            else:
                resources = platform.resources
            for resource in resources:
                if isinstance(resource, tuple) and len(resource) >= 2:
                    if resource[0] == "eth" and resource[1] == 1:
                        filtered = []
                        for item in resource[2:]:
                            if isinstance(item, Subsignal):
                                if item.name not in ("mdc", "mdio", "rst_n"):
                                    filtered.append(item)
                            else:
                                filtered.append(item)
                        platform.add_extension([
                            ("eth1_no_shared", 0) + tuple(filtered)
                        ])
                        print("[INFO] Created eth1_no_shared")
                        return
        except Exception as e:
            print(f"[WARN] Automatic pin filtering failed: {e}")

        if revision in ("6.0", "7.0", "7.1"):
            platform.add_extension([
                ("eth1_no_shared", 0,
                    Subsignal("tx_data", Pins("G1 G2 G3 G4")),
                    Subsignal("tx_ctl",  Pins("H4")),
                    Subsignal("rx_data", Pins("J1 J3 K1 K2")),
                    Subsignal("rx_ctl",  Pins("J2")),
                    IOStandard("LVCMOS33")),
            ])
        elif revision == "8.0":
            platform.add_extension([
                ("eth1_no_shared", 0,
                    Subsignal("tx_data", Pins("G1 G2 G3 G4")),
                    Subsignal("tx_ctl",  Pins("H4")),
                    Subsignal("rx_data", Pins("K2 L1 K1 K3")),
                    Subsignal("rx_ctl",  Pins("J1")),
                    IOStandard("LVCMOS33")),
            ])
        else:
            raise ValueError(f"Unsupported revision: {revision}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="EtherCAT Slave for Colorlight 5A-75E")
    parser.add_argument("--revision", default="6.0", help="PCB revision: 6.0, 7.0, 7.1, 8.0")
    parser.add_argument("--sys-clk-freq", default=50e6, type=float, help="System clock in Hz")
    parser.add_argument("--build", action="store_true", help="Build bitstream")
    parser.add_argument("--load", action="store_true", help="Load to SRAM via OpenOCD")
    parser.add_argument("--flash", action="store_true", help="Flash to SPI")
    parser.add_argument("--output-dir", default="build", help="Build directory")
    args = parser.parse_args()

    soc = BaseSoC(revision=args.revision, sys_clk_freq=int(args.sys_clk_freq))
    builder = Builder(soc, output_dir=args.output_dir, csr_csv="csr.csv")

    if args.build:
        builder.build()
    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(builder.get_bitstream_filename(mode="sram"))
    if args.flash:
        prog = soc.platform.create_programmer()
        prog.flash(0, builder.get_bitstream_filename(mode="flash"))


if __name__ == "__main__":
    main()
