#!/usr/bin/env python3
"""Проверка инференса BRAM для ECP5. Только Yosys, без nextpnr."""
from migen import *
from litex.build.lattice import LatticePlatform
from litex.build.generic_platform import Pins, IOStandard

# Пины неважны: nextpnr не запускаем, .lpf не используется.
_io = [
    ("clk25",   0, Pins("P6"),      IOStandard("LVCMOS33")),
    ("rd_data", 0, Pins(8),         IOStandard("LVCMOS33")),
    ("rd_adr",  0, Pins(14),        IOStandard("LVCMOS33")),
    ("wr_data", 0, Pins(8),         IOStandard("LVCMOS33")),
    ("wr_en",   0, Pins(1),         IOStandard("LVCMOS33")),
]


class MinimalBRAM(Module):
    def __init__(self, platform):
        clk25 = platform.request("clk25")
        self.clock_domains.cd_sys = ClockDomain()
        self.comb += self.cd_sys.clk.eq(clk25)

        rd_data = platform.request("rd_data")
        rd_adr  = platform.request("rd_adr")
        wr_data = platform.request("wr_data")
        wr_en   = platform.request("wr_en")

        # Ровно та же память, что в ESCMemory
        mem = Memory(8, 16384)
        self.specials += mem

        rd_port = mem.get_port(async_read=False)
        wr_port = mem.get_port(write_capable=True, async_read=False)
        self.specials += rd_port, wr_port

        self.comb += [
            rd_port.adr.eq(rd_adr),
            rd_data.eq(rd_port.dat_r),
            wr_port.adr.eq(rd_adr),
            wr_port.dat_w.eq(wr_data),
            wr_port.we.eq(wr_en),
        ]


platform = LatticePlatform("LFE5U-25F-6BG381C", _io, toolchain="trellis")
platform.build(MinimalBRAM(platform), build_name="bram_test", run=False)
print("Verilog: build/gateware/bram_test.v")
