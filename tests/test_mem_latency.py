#!/usr/bin/env python3
"""Микротест: измерение латентности чтения ESCMemory."""

from migen import *
from migen.sim import run_simulation

from ethercat_const import *
from ethercat_memory import ESCMemory

def probe(dut, addr, expect, tag, start_addr=0x3FFF):
    print(f"\n--- {tag}: addr=0x{addr:04x}, expect=0x{expect:02x} ---")
    # уводим адрес в заведомо другое место и ждём
    yield dut.address_read.eq(start_addr)
    for _ in range(4):
        yield
    base = yield dut.data_read
    print(f"  base (addr=0x{start_addr:04x}) = 0x{base:02x}")

    yield dut.address_read.eq(addr)
    found = None
    for i in range(6):
        yield
        val = yield dut.data_read
        mark = ""
        if val == expect and found is None:
            found = i
            mark = f"  <<< LATENCY = {i}"
        print(f"  cycle {i}: data_read = 0x{val:02x}{mark}")
    return found


def bench(dut):
    # ---------- запись в DPRAM ----------
    yield dut.address_write.eq(0x1100)
    yield dut.data_write.eq(0xA5)
    yield dut.write_en.eq(1)
    yield
    yield dut.address_write.eq(0x1101)
    yield dut.data_write.eq(0x5A)
    yield
    yield dut.write_en.eq(0)

    # даём конвейеру записи (S1->S2->S3) прожеваться
    for _ in range(6):
        yield

    # ---------- чтение BRAM ----------
    lat_bram = yield from probe(dut, 0x1100, 0xA5, "BRAM 0x1100")



    # пауза, чтобы не мешалось
    yield dut.address_read.eq(0x0000)
    for _ in range(4):
        yield

    # ---------- чтение регистра ----------
    # 0x0000 = ESC Type, read-only, обычно 0x11 (проверь своё значение
    # в ethercat_registers.py, если не 0x11 — поправь expect)
    lat_regs = yield from probe(dut, 0x0000, 0x04, "REG 0x0000 (Type)")

    print("\n" + "=" * 50)
    print(f"  BRAM latency : {lat_bram}")
    print(f"  REGS latency : {lat_regs}")
    if lat_bram is not None and lat_regs is not None:
        if lat_bram == lat_regs:
            print(f"  => ветки СОГЛАСОВАНЫ ({lat_bram} такт(а))")
        else:
            print(f"  => РАССОГЛАСОВАНИЕ: разница {lat_bram - lat_regs} такт(а)")
    print("=" * 50)


if __name__ == "__main__":
    dut = ESCMemory(bram_latency_2=True)
    run_simulation(dut, bench(dut), clocks={"sys": 10},
                   vcd_name="mem_latency.vcd")
    print("\nVCD: mem_latency.vcd")
