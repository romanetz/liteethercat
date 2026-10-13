#!/usr/bin/env python3
"""Функциональный тест SII FSM: запись address → control → busy → data."""

from migen import *
from migen.sim import run_simulation
from ethercat_sii import SIIInterface

def bench(dut):
    # 1. Прочитать начальное состояние
    #yield dut.address_read.eq(0x0502)
    #yield
    #yield
    #status = yield dut.data_read
    #assert status == 0x00, f"initial status = {status:#x}, expected 0x00"
    #print(f"[OK] initial status = {status:#x}")

    for addr in range(0x0500, 0x0510):
        yield dut.address_read.eq(addr)
        yield
        yield
        v = yield dut.data_read
        print(f"  0x{addr:04x} = 0x{v:02x}")

    # 1. Стартовый статус (0x0502/0x0503 — 16-битное слово)
    yield dut.address_read.eq(0x0502)
    yield; yield
    lo = yield dut.data_read
    yield dut.address_read.eq(0x0503)
    yield; yield
    hi = yield dut.data_read
    status = lo | (hi << 8)
    assert status == 0x00a0, f"initial status = {status:#06x}"
    print(f"[OK] initial status = 0x{status:04x}")

    # 2. Установить cfg_read_size = 1 (bit 6 control) → 8 байт = 4 слова
    yield dut.address_write.eq(0x0502)
    yield dut.data_write.eq(0x40)
    yield dut.write_en.eq(1)
    yield
    yield dut.write_en.eq(0)
    yield

    # 3. Адрес слова 0x40
    for b, v in [(0x0504, 0x40), (0x0505, 0x00),
                 (0x0506, 0x00), (0x0507, 0x00)]:
        yield dut.address_write.eq(b)
        yield dut.data_write.eq(v)
        yield dut.write_en.eq(1)
        yield
    yield dut.write_en.eq(0)
    yield

    # 4. Команда Read в 0x0503 bit 0
    yield dut.address_write.eq(0x0503)
    yield dut.data_write.eq(0x01)
    yield dut.write_en.eq(1)
    yield
    yield dut.write_en.eq(0)

    # 5. Ждём busy=0
    for i in range(20):
        yield
        yield dut.address_read.eq(0x0503)
        yield; yield
        hi = yield dut.data_read
        if (hi & 0x80) == 0:
            print(f"[OK] busy cleared after {i} cycles")
            break
    else:
        raise AssertionError("busy never cleared")

    # 6. Читаем 4 слова — берём их из реального .bin
    expected = [0x000A, 0x0031, 0x0F06, 0x6F43]
    for i in range(4):
        addr = 0x0508 + 2 * i
        yield dut.address_read.eq(addr)
        yield; yield
        lo = yield dut.data_read
        yield dut.address_read.eq(addr + 1)
        yield; yield
        hi = yield dut.data_read
        word = lo | (hi << 8)
        print(f"  word[{i}] = 0x{word:04x}")
        assert word == expected[i], \
            f"word[{i}]: expected 0x{expected[i]:04x}, got 0x{word:04x}"

    print("\n=== SII protocol test PASSED ===")

from ethercat_sii import SIIInterface, load_sii_bin

if __name__ == "__main__":
    eeprom = load_sii_bin("./siitool/examples/colorlight4fmmu.bin")
    dut = SIIInterface(eeprom_words=eeprom)
    run_simulation(dut, bench(dut), clocks={"sys": 10}, vcd_name="sii_test.vcd")
