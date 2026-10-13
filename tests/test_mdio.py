#!/usr/bin/env python3

import unittest

from migen import *
from migen.sim import run_simulation

from colorlight_phy_init import MDIOMaster


class MDIOSimPads:
    """
    Минимальная модель MDIO-пинов для симуляции.

    В реальном проекте pads.mdio должен быть двунаправленным физическим
    пином, подключённым через Tristate внутри MDIOMaster.
    """
    def __init__(self):
        self.mdc  = Signal(reset=0)
        self.mdio = Signal(reset=0)


def bits_msb(value, width):
    """Возвращает список битов от MSB к LSB."""
    return [
        (value >> bit) & 1
        for bit in range(width - 1, -1, -1)
    ]


def clause22_write_bits(phy_addr, reg_addr, data):
    """
    Правильная IEEE 802.3 Clause 22 write-транзакция:

        32 бит preamble
         2 бита start = 01
         2 бита op    = 01
         5 бит PHYAD
         5 бит REGAD
         2 бита TA    = 10
        16 бит DATA
    """
    return (
        [1] * 32 +
        bits_msb(0b01, 2) +       # START
        bits_msb(0b01, 2) +       # OP: write
        bits_msb(phy_addr, 5) +   # PHY address
        bits_msb(reg_addr, 5) +   # Register address
        bits_msb(0b10, 2) +       # Turnaround for write
        bits_msb(data, 16)
    )


def bits_to_string(bits):
    return "".join(str(x) for x in bits)


def bits_to_hex(bits):
    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return f"0x{value:016X}"


class TestMDIOMaster(unittest.TestCase):

    def _capture_write_transaction(self, dut, pads,
                                   phy_addr, reg_addr, data,
                                   max_cycles=3000):
        """
        Запускает одну write-транзакцию и записывает MDIO на каждом
        восходящем фронте MDC.

        Важно: MDIO считывается ДО системного такта, на котором MDC
        переходит из 0 в 1. Это соответствует моменту физического
        sampling на rising edge MDC.
        """
        yield dut.addr.eq(phy_addr)
        yield dut.reg.eq(reg_addr)
        yield dut.data_w.eq(data)

        # Подаём write request ровно на один такт.
        yield dut.write.eq(1)
        yield
        yield dut.write.eq(0)

        captured_bits = []
        done_seen = False

        for _ in range(max_cycles):
            # Значения до следующего sys_clk edge.
            mdc_before = (yield pads.mdc)
            mdio_before = (yield pads.mdio)

            # Переход к следующему sys_clk edge.
            yield

            # Значения после sys_clk edge.
            mdc_after = (yield pads.mdc)

            # MDC rising edge: 0 -> 1.
            if (mdc_before == 0) and (mdc_after == 1):
                captured_bits.append(mdio_before & 1)

                if len(captured_bits) == 64:
                    # После последнего бита FSM должна попасть в DONE.
                    # Проверяем done в следующих тактах.
                    for _ in range(30):
                        if (yield dut.done):
                            done_seen = True
                            break
                        yield
                    break

        print()
        print("--------------------------------------------------")
        print("MDIO write transaction")
        print(f"PHY address : {phy_addr}")
        print(f"Register    : 0x{reg_addr:02X}")
        print(f"Data        : 0x{data:04X}")
        print(f"Captured    : {len(captured_bits)} bits")
        print(f"Bits        : {bits_to_string(captured_bits)}")

        if len(captured_bits) == 64:
            print(f"Captured hex: {bits_to_hex(captured_bits)}")

        expected_bits = clause22_write_bits(phy_addr, reg_addr, data)

        print(f"Expected    : {bits_to_string(expected_bits)}")
        print(f"Expected hex: {bits_to_hex(expected_bits)}")
        print(f"DONE        : {done_seen}")
        print("--------------------------------------------------")

        self.assertEqual(
            len(captured_bits),
            64,
            "Не удалось захватить полную MDIO-транзакцию"
        )

        self.assertEqual(
            captured_bits,
            expected_bits,
            "MDIO-последовательность не соответствует Clause 22"
        )

        self.assertTrue(
            done_seen,
            "MDIOMaster не сформировал импульс done"
        )

    def test_clause22_write_phy0_reg4(self):
        """
        Проверка записи:

            PHYAD = 3
            REGAD = 4
            DATA  = 0x0181

        Это одна из операций конфигурации из ColorlightAutoPHYConfig.
        """
        pads = MDIOSimPads()
        dut = MDIOMaster(pads, simulation=True)

        # Поведение внешнего PHY:
        # - когда master владеет шиной, на линии mdio_o;
        # - когда master отпустил линию, PHY пока держит 1.
        dut.comb += [
            pads.mdio.eq(
                Mux(
                    dut.mdio_oe,
                    dut.mdio_o,
                    1
                )
            )
        ]


        def tb():
            yield from self._capture_write_transaction(
                dut,
                pads,
                phy_addr=3,
                reg_addr=4,
                data=0x0181
            )

        run_simulation(
            dut,
            tb(),
            vcd_name="test_mdio_write_phy3_reg4.vcd"
        )

    def test_clause22_write_basic_control(self):
        """
        Проверка записи BMCR:

            PHYAD = 3
            REGAD = 0
            DATA  = 0x2100

        0x2100 обычно означает:
          - Auto-Negotiation Enable
          - Restart Auto-Negotiation
        """
        pads = MDIOSimPads()
        dut = MDIOMaster(pads, simulation=True)

        # Поведение внешнего PHY:
        # - когда master владеет шиной, на линии mdio_o;
        # - когда master отпустил линию, PHY пока держит 1.
        dut.comb += [
            pads.mdio.eq(
                Mux(
                    dut.mdio_oe,
                    dut.mdio_o,
                    1
                )
            )
        ]


        def tb():
            yield from self._capture_write_transaction(
                dut,
                pads,
                phy_addr=3,
                reg_addr=0,
                data=0x2100
            )

        run_simulation(
            dut,
            tb(),
            vcd_name="test_mdio_write_phy3_reg0.vcd"
        )

    def test_clause22_write_disable_gigabit_advertisement(self):
        """
        Проверка записи регистра 9:

            PHYAD = 3
            REGAD = 9
            DATA  = 0x0000

        В текущей конфигурации этот регистр используется для отключения
        advertisement 1000BASE-T.
        """
        pads = MDIOSimPads()
        dut = MDIOMaster(pads, simulation=True)

        # Поведение внешнего PHY:
        # - когда master владеет шиной, на линии mdio_o;
        # - когда master отпустил линию, PHY пока держит 1.
        dut.comb += [
            pads.mdio.eq(
                Mux(
                    dut.mdio_oe,
                    dut.mdio_o,
                    1
                )
            )
        ]


        def tb():
            yield from self._capture_write_transaction(
                dut,
                pads,
                phy_addr=3,
                reg_addr=9,
                data=0x0000
            )

        run_simulation(
            dut,
            tb(),
            vcd_name="test_mdio_write_phy3_reg9.vcd"
        )


if __name__ == "__main__":
    unittest.main()

