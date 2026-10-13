#!/usr/bin/env python3
import sys
sys.path.insert(0, ".")
from migen import *
from ethercat_top import EtherCATSlave
from liteeth.common import eth_phy_description
from litex.soc.interconnect import stream

def testbench(dut):
    # Простейший BRD 0x0130, len=2
    frame = [
        # Ethernet header (14 байт, DA=broadcast)
        0xff, 0xff, 0xff, 0xff, 0xff, 0xff,  # DA
        0x10, 0x10, 0x10, 0x10, 0x10, 0x10,  # SA
        0x88, 0xa4,                          # EtherType
        # EtherCAT header (2 байт)
        0x10, 0x01,                          # Length=16, Type=1
        # Datagram: BRD 0x0130, len=2
        0x07,                                # CMD=BRD
        0x00,                                # IDX
        0x00, 0x13, 0x00, 0x01,             # ADDR=0x01300000 (broadcast)
        0x02, 0x00,                          # LEN=2, more=0
        0x00, 0x00,                          # IRQ
        0xaa, 0xbb,                          # DATA (2 байта)
        0x00, 0x00,                          # WKC
    ]

    # Отправка
    for i, b in enumerate(frame):
        yield dut.pu.sink.valid.eq(1)
        yield dut.pu.sink.data.eq(b)
        yield dut.pu.sink.last_be.eq(1 if i == len(frame)-1 else 0)
        yield
        while not (yield dut.pu.sink.ready):
            yield

    yield dut.pu.sink.valid.eq(0)
    yield dut.pu.sink.last_be.eq(0)

    # Ждём ответ
    for _ in range(10):
        yield

    wkc = yield dut.pu.wkc
    print(f"WKC = {wkc}")

dut = EtherCATSlave()
run_simulation(dut, testbench(dut), vcd_name="wkc_debug.vcd", clocks={"sys": 10})
