#!/usr/bin/env python3
from migen import *
from ethercat_const import *
from ethercat_pu import EtherCATSlavePU
from ethercat_memory import ESCMemory


class EtherCATSlave(Module):
    def __init__(self,async_read = False):
        self.submodules.pu  = pu  = EtherCATSlavePU(read_latency=2)
        self.submodules.mem = mem = ESCMemory(bram_latency_2=True)

        self.comb += [
            mem.address_read.eq(pu.mem_address_read),
            pu.mem_data_read.eq(mem.data_read),
            mem.address_write.eq(pu.mem_address_write),
            mem.data_write.eq(pu.mem_data_write),
            mem.write_en.eq(pu.mem_write_en),

            mem.logical_address_read.eq(pu.mem_logical_address_read),
            pu.mem_logical_data_read.eq(mem.logical_data_read),
            mem.logical_address_write.eq(pu.mem_logical_address_write),
            mem.logical_data_write.eq(pu.mem_logical_data_write),
            mem.logical_write_en.eq(pu.mem_logical_write_en),

            mem.data_phase.eq(pu.mem_data_phase),
            mem.phys_read_stb.eq(pu.mem_phys_read_stb),
            mem.logical_read_stb.eq(pu.mem_logical_read_stb),
            mem.last_data_byte.eq(pu.mem_last_data_byte),
            pu.mem_logical_would_hit.eq(mem.logical_would_hit),
            pu.mem_logical_early_would_hit.eq(mem.logical_early_would_hit),
            pu.mem_logical_access_ok.eq(mem.logical_access_ok),
            pu.station_alias.eq(mem.station_alias_out),
            pu.alias_enable.eq(mem.alias_enable_out),
            # fp_address из регистров
            pu.fp_address.eq(mem.fp_address_out),
            mem.transfer.eq(pu.transfer)
        ]

        self.sink = pu.sink
        self.source = pu.source
        self.fp_address = pu.fp_address
        self.wkc = pu.wkc
        self.ethercat_count = pu.ethercat_count
        self.datagram_count = pu.datagram_count
        self.sm_irq = mem.sm_irq


if __name__ == "__main__":
    from migen.fhdl import verilog
    slave = EtherCATSlave()
    print(verilog.convert(slave, ios={
        slave.sink.valid, slave.sink.ready, slave.sink.data, slave.sink.last,
        slave.source.valid, slave.source.ready, slave.source.data,
        slave.source.last,
        slave.fp_address, slave.wkc, slave.ethercat_count, slave.sm_irq,
    }))
