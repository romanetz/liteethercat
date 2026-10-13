#!/usr/bin/env python3
"""
Auto PHY Configuration and Link Monitor for Colorlight 5A-75E.

Uses LiteEthPHYMDIO to:
  1. Scan MDIO bus (addresses 0-31)
  2. Find two RTL8211F PHYs (OUI 0x001CC9)
  3. Configure them (disable EEE, set RGMII delays)
  4. Continuously poll BMSR register 1 for link status

Signals exposed:
    link0_up : link status of first PHY found
    link1_up : link status of second PHY found
    mdio_phy_addr0 : address of first PHY
    mdio_phy_addr1 : address of second PHY
"""

from migen import *
from liteeth.phy.common import LiteEthPHYMDIO
from migen.genlib.cdc import MultiReg
from migen.fhdl.specials import Tristate

#!/usr/bin/env python3
"""
Simplified MDIO Master for RTL8211F.

Assumptions:
  - PHY accepts shortened preamble (8 bits instead of 32)
  - No turnaround phase checking
  - Fixed timing
"""

from migen import *


#!/usr/bin/env python3
"""
MDIO Master (IEEE 802.3 Clause 22).

Implements full MDIO protocol with read/write operations.
No CPU required — pure FSM bit-banging.

Interface:
    write : start write transaction
    read  : start read transaction
    done  : transaction complete
    addr  : PHY address (5 bits)
    reg   : register address (5 bits)
    data_w : write data (16 bits)
    data_r : read data (16 bits)
"""

from migen import *


class MDIOMaster(Module):
    """
    MDIO Master, IEEE 802.3 Clause 22.

    Частота sys_clk предполагается 50 MHz.
    MDC:
        half-period = 10 sys_clk cycles
        period      = 20 sys_clk cycles
        frequency    = 2.5 MHz
    """

    def __init__(self, pads, simulation=False):
        # Интерфейс транзакции
        self.write = Signal()
        self.read = Signal()
        self.done = Signal()

        self.addr = Signal(5)
        self.reg = Signal(5)
        self.data_w = Signal(16)
        self.data_r = Signal(16)

        # MDIO internal signals.
        # Делаем их публичными, чтобы тест мог наблюдать линию.
        self.mdio_o = Signal(reset=1)
        self.mdio_oe = Signal(reset=0)
        self.mdio_i = Signal(reset=1)

        mdio_o = self.mdio_o
        mdio_oe = self.mdio_oe
        mdio_i = self.mdio_i
        zero=Signal(reset=0)
        mdio_drive_low = Signal(reset=0)
        self.comb+=mdio_drive_low.eq(mdio_oe & ~mdio_o)
        if simulation:
            # В тесте pads.mdio формируется внешней моделью PHY.
            self.comb += mdio_i.eq(pads.mdio)
        else:
            # В железе — настоящий двунаправленный MDIO pin.
            self.specials += [
                Tristate(
                    pads.mdio,
                    zero,
                    mdio_drive_low,
                    mdio_i
                )
            ]

        # MDC
        self.mdc = mdc = Signal(reset=0)
        self.comb += pads.mdc.eq(mdc)

        # 32-битный фрейм после преамбулы
        shift_reg = Signal(32)

        # 01 = write, 10 = read
        op_code = Signal(2)

        # Для преамбулы: 32 ... 1
        # Для frame:     32 ... 1
        bit_count = Signal(6)

        # Делитель MDC: 25 MHz / (2 * 10) = 1.25 MHz
        clk_div = Signal(7, reset=0)
        mdc_tick = Signal()
        MDC_DIV = 10
        self.sync += [
            clk_div.eq(clk_div + 1),

            If(clk_div == MDC_DIV-1,
                clk_div.eq(0),
                mdc.eq(~mdc)
            )
        ]

        # Тик на восходящем фронте MDC.
        # В этом же sys-такте mdc переключается 0 -> 1.
        self.comb += [
            mdc_tick.eq((clk_div == MDC_DIV-1) & mdc)
        ]

        mdc_rise = Signal()
        mdc_fall = Signal()

        self.comb += [
            mdc_rise.eq((clk_div == MDC_DIV - 1) & ~mdc),
            mdc_fall.eq((clk_div == MDC_DIV - 1) &  mdc),
        ]

        self.submodules.fsm = FSM(reset_state="IDLE")

        # ---------------------------------------------------------
        # IDLE
        # ---------------------------------------------------------
        self.fsm.act("IDLE",
            self.done.eq(0),
            mdio_oe.eq(0),
            mdio_o.eq(1),

            If(self.write,
                # Clause 22 write:
                #
                #   START  OP  PHYAD  REGAD  TA  DATA
                #     01    01   5      5     10   16
                #
                # Cat: первый аргумент занимает младшие биты.
                NextValue(op_code, 0b01),
                NextValue(
                    shift_reg,
                    Cat(
                        self.data_w,       # DATA, 16 бит
                        C(0b10, 2),        # TA, 2 бита
                        self.reg,          # REGAD, 5 бит
                        self.addr,         # PHYAD, 5 бит
                        C(0b01, 2),        # OP = write
                        C(0b01, 2),        # START
                    )
                ),
                NextValue(bit_count, 40),
                NextState("PREAMBLE")
            ).Elif(self.read,
                # Clause 22 read:
                #
                #   START  OP  PHYAD  REGAD  TA  DATA
                #     01    10   5      5     Z0   16
                #
                # TA заполняем нулями, но в этом месте master
                # отпустит MDIO.
                NextValue(op_code, 0b10),
                NextValue(
                    shift_reg,
                    Cat(
                        C(0, 16),          # место для DATA
                        C(0, 2),           # место для TA
                        self.reg,          # REGAD
                        self.addr,         # PHYAD
                        C(0b10, 2),        # OP = read
                        C(0b01, 2),        # START
                    )
                ),
                NextValue(bit_count, 40),
                NextState("PREAMBLE")
            )
        )

        # ---------------------------------------------------------
        # PREAMBLE
        # ---------------------------------------------------------
        self.fsm.act("PREAMBLE",
            # Держим MDIO в единице всю преамбулу.
            mdio_oe.eq(1),
            mdio_o.eq(1),

            If(mdc_fall,
                If(bit_count == 1,
                    NextValue(bit_count, 32),
                    NextState("FRAME")
                ).Else(
                    NextValue(bit_count, bit_count - 1)
                )
            )
        )

        # ---------------------------------------------------------
        # FRAME
        # ---------------------------------------------------------
        self.fsm.act("FRAME",
            # Передающий бит должен быть установлен после прошлого falling edge
            # и оставаться стабильным к следующему rising edge MDC.
            If(op_code == 0b01,
                # Write: master передаёт весь frame.
                mdio_oe.eq(1),
                mdio_o.eq(shift_reg[31])
            ).Else(
                # Read:
                # 32..19: START + OP + PHYAD + REGAD.
                # 18..17: turnaround, master отпускает MDIO.
                # 16..1:  PHY передаёт data.
                If(bit_count > 18,
                    mdio_oe.eq(1),
                    mdio_o.eq(shift_reg[31])
                ).Else(
                    mdio_oe.eq(0),
                    mdio_o.eq(1)
                )
            ),

            # Падающий фронт заканчивает период текущего передаваемого бита.
            # На нём сдвигается передающая часть transaction и уменьшается счётчик.
            If(mdc_fall,
                If(op_code == 0b01,
                    # Write: сдвигаем очередной передаваемый bit.
                    NextValue(
                        shift_reg,
                        Cat(C(0, 1), shift_reg[0:31])
                    )
                ).Else(
                    # Read: сдвигаем header и turnaround.
                    # Во время data phase shift_reg меняется ниже, на mdc_rise.
                    If(bit_count > 16,
                        NextValue(
                            shift_reg,
                            Cat(C(0, 1), shift_reg[0:31])
                        )
                    )
                ),

                If(bit_count == 1,
                    NextState("DONE")
                ).Else(
                    NextValue(bit_count, bit_count - 1)
                )
            ),

            # PHY меняет очередной read-bit после falling edge,
            # master считывает его на следующем rising edge.
            #
            # Первому data-bit соответствует bit_count == 16.
            If((op_code == 0b10) & mdc_rise & (bit_count <= 16),
                NextValue(
                    shift_reg,
                    Cat(mdio_i, shift_reg[0:31])
                ),

                # На rising edge при bit_count == 1 принимается последний bit.
                If(bit_count == 1,
                    NextValue(
                        self.data_r,
                        Cat(mdio_i, shift_reg[0:15])
                    )
                )
            )
        )


        # ---------------------------------------------------------
        # DONE
        # ---------------------------------------------------------
        self.fsm.act("DONE",
            self.done.eq(1),
            mdio_oe.eq(0),
            mdio_o.eq(1),
            NextState("IDLE")
        )



class ColorlightAutoPHYConfig(Module):
    def __init__(self, mdio_pads):
        self.submodules.mdio = mdio = MDIOMaster(mdio_pads)

        self.link0_up = Signal(reset=0)
        self.link1_up = Signal(reset=0)
        self.mdio_broadcast_addr = Signal(5, reset=0)
        self.mdio_phy_addr0 = Signal(5, reset=1)
        self.mdio_phy_addr1 = Signal(5, reset=2)
        self.phy_found = Signal(reset=0)
        current_addr = Signal(5)
        phy_count = Signal(2)
        poll_addr = Signal(5)
        phy_id_hi = Signal(16)
        phy_id_lo = Signal(16)
        config_step = Signal(4)
        timeout = Signal(28,reset=0)
        #phy_reset = Signal(reset=1)
        bmsr_first = Signal(16)
        bmcr_first = Signal(16)
        self.autoneg0_done = Signal()
        self.autoneg1_done = Signal()

        #self.comb+=mdio_pads.rst_n.eq(phy_reset)
        self.submodules.fsm = FSM(reset_state="RESET_PHYS")

        # (остальной код FSM без изменений — только замена интерфейса)


        self.fsm.act("RESET_PHYS",
            #NextValue(phy_reset, 0),
            NextValue(timeout, timeout+1),
            If(timeout > 16777215,
            NextState("CONFIG_PHY0_START"),
            #phy_reset.eq(1)
            )
        )

        self.fsm.act("SCAN_START",
            NextValue(current_addr, 1),
            NextValue(phy_count, 0),
            NextValue(timeout, 0),
            NextState("SCAN_READ_ID_HI")
        )

        self.fsm.act("SCAN_READ_ID_HI",
            mdio.addr.eq(current_addr),
            mdio.reg.eq(2),
            mdio.read.eq(1),
            If(mdio.done,
                NextValue(phy_id_hi, mdio.data_r),
                NextValue(timeout, 0),
                NextState("SCAN_READ_ID_LO")
            ).Elif(timeout > 100000,
                NextValue(current_addr, current_addr + 1),
                NextValue(timeout, 0),
                If(current_addr == 31,
                    NextState("CONFIG_PHY0_START")
                ).Else(
                    NextState("SCAN_READ_ID_HI")
                )
            ).Else(
                NextValue(timeout, timeout + 1)
            )
        )

        self.fsm.act("SCAN_READ_ID_LO",
            mdio.addr.eq(current_addr),
            mdio.reg.eq(3),
            mdio.read.eq(1),
            If(mdio.done,
                NextValue(phy_id_lo, mdio.data_r),
                NextValue(timeout, 0),
                NextState("SCAN_CHECK")
            ).Elif(timeout > 100000,
                NextValue(current_addr, current_addr + 1),
                NextValue(timeout, 0),
                If(current_addr == 31,
                    NextState("CONFIG_PHY0_START")
                ).Else(
                    NextState("SCAN_READ_ID_HI")
                )
            ).Else(
                NextValue(timeout, timeout + 1)
            )
        )

        self.fsm.act("SCAN_CHECK",
            NextValue(timeout, 0),
            If((phy_id_hi == 0x001C) & ((phy_id_lo & 0xFFF0) == 0xC910),
                If(phy_count == 0,
                    NextValue(self.mdio_phy_addr0, current_addr),
                    NextValue(phy_count, 1)
                ).Elif(phy_count == 1,
                    NextValue(self.mdio_phy_addr1, current_addr),
                    NextValue(phy_count, 2)
                )
            ),
            NextValue(current_addr, current_addr + 1),
            If(current_addr == 31,
                    If(phy_count == 2,
                        NextValue(self.phy_found, 1),
                        NextState("CONFIG_PHY0_START")
                    ).Else(
                        # Ничего не конфигурируем по адресам 0/1 «наугад».
                        # Повторяем scan спустя некоторое время.
                        NextState("SCAN_START")
                    )
            ).Else(
                NextState("SCAN_READ_ID_HI")
            )
        )

        # CONFIG PHY0
        self.fsm.act("CONFIG_PHY1_START",
            NextValue(config_step, 0),
            NextValue(timeout, 0),
            NextState("CONFIG_PHY1_WRITE")
        )

        self.fsm.act("CONFIG_PHY1_WRITE",
            mdio.addr.eq(self.mdio_phy_addr0),
            mdio.read.eq(1),
            Case(config_step, {
                #0: [mdio.reg.eq(0x1F), mdio.data_w.eq(0xD08)],
                #1: [mdio.reg.eq(0x11), mdio.data_w.eq(0x0000)],
                #2: [mdio.reg.eq(0x15), mdio.data_w.eq(0x0000)],
                #3: [mdio.reg.eq(0x1F), mdio.data_w.eq(0x0000)],
                #4: [mdio.reg.eq(0x09), mdio.data_w.eq(0x0000)],
                #5: [mdio.reg.eq(0x04), mdio.data_w.eq(0x0101)], # 100Base-TX Full Duplex
                #6: [mdio.reg.eq(0x00), mdio.data_w.eq(0x9200)],

                #0: [mdio.reg.eq(0x04), mdio.data_w.eq(0x0101)],
                #1: [mdio.reg.eq(0x09), mdio.data_w.eq(0x0000)],
                #2: [mdio.reg.eq(0x00), mdio.data_w.eq(0x1200)],
                # --- ОТКЛЮЧЕНИЕ ЭНЕРГОСБЕРЕЖЕНИЯ (EEE / Green Ethernet) ---
                #3: [mdio.reg.eq(31), mdio.data_w.eq(0x0d04)], # 1. Переключаемся на страницу расширенных регистров 0xd04
                #4: [mdio.reg.eq(17), mdio.data_w.eq(0x0000)], # 2. Зануляем регистр 17 на этой странице (отключаем EEE для 100 Мбит)
                #5: [mdio.reg.eq(31), mdio.data_w.eq(0x0000)], # 3. Обязательно возвращаемся на стандартную страницу 0
                # --- ШАГ 1: Стандартная конфигурация скоростей ---
                0: [mdio.reg.eq(0x04), mdio.data_w.eq(0x0101)], # Рекламируем только 100FD
                1: [mdio.reg.eq(0x09), mdio.data_w.eq(0x0000)], # Запрещаем гигабит

                # --- ШАГ 2: Официальное отключение Green Ethernet (стр. 25 даташита) ---
                2: [mdio.reg.eq(31),   mdio.data_w.eq(0x0a43)], # Переходим на страницу 0xa43
                3: [mdio.reg.eq(27),   mdio.data_w.eq(0x8011)], # Ломаем логику энергосбережения
                4: [mdio.reg.eq(28),   mdio.data_w.eq(0x573f)], # Выключаем Green Ethernet полностью

                # --- ШАГ 3: Возврат на страницу 0 и запуск автосогласования ---
                5: [mdio.reg.eq(31),   mdio.data_w.eq(0x0000)], # Возвращаемся на базовую страницу 0
                6: [mdio.reg.eq(0x00), mdio.data_w.eq(0x1200)], # Перезапускаем AN с новыми правилами

                #3: [mdio.reg.eq(0x1F), mdio.data_w.eq(0x0000)],
                #4: [mdio.reg.eq(0x09), mdio.data_w.eq(0x0000)],
                #5: [mdio.reg.eq(0x04), mdio.data_w.eq(0x0101)], # 100Base-TX Full Duplex
                #6: [mdio.reg.eq(0x00), mdio.data_w.eq(0x1200)],

                #7: [mdio.reg.eq(0x00), mdio.data_w.eq(0x9200)],

                "default": [mdio.reg.eq(0x00), mdio.data_w.eq(0x0000)]
            }),
            If(mdio.done,
                NextValue(config_step, config_step + 1),
                NextValue(timeout, 0),
                If(config_step == 5,
                    NextState("CONFIG_PHY0_START")
                )
            ).Elif(timeout > 100000,
                NextValue(config_step, config_step + 1),
                NextValue(timeout, 0),
                If(config_step == 5,
                    NextState("CONFIG_PHY0_START")
                )
            ).Else(
                NextValue(timeout, timeout + 1)
            )
        )

        # CONFIG PHY1
        self.fsm.act("CONFIG_PHY0_START",
            NextValue(config_step, 0),
            NextValue(timeout, 0),
            NextState("CONFIG_PHY0_WRITE")
        )

        self.fsm.act("CONFIG_PHY0_WRITE",
            mdio.addr.eq(self.mdio_broadcast_addr),
            mdio.write.eq(1),
            Case(config_step, {
                #0: [mdio.reg.eq(0x1F), mdio.data_w.eq(0xD08)],
                #1: [mdio.reg.eq(0x11), mdio.data_w.eq(0x0000)],
                #2: [mdio.reg.eq(0x15), mdio.data_w.eq(0x0000)],
                #3: [mdio.reg.eq(0x1F), mdio.data_w.eq(0x0000)],
                #4: [mdio.reg.eq(0x09), mdio.data_w.eq(0x0000)],
                #5: [mdio.reg.eq(0x04), mdio.data_w.eq(0x0101)], # 100Base-TX Full Duplex
                #6: [mdio.reg.eq(0x00), mdio.data_w.eq(0x9200)],
                #0: [mdio.reg.eq(0x04), mdio.data_w.eq(0x0101)],
                #1: [mdio.reg.eq(0x09), mdio.data_w.eq(0x0000)],
                #2: [mdio.reg.eq(0x00), mdio.data_w.eq(0x1200)],
                # --- ОТКЛЮЧЕНИЕ ЭНЕРГОСБЕРЕЖЕНИЯ (EEE / Green Ethernet) ---
                #3: [mdio.reg.eq(31), mdio.data_w.eq(0x0d04)], # 1. Переключаемся на страницу расширенных регистров 0xd04
                #4: [mdio.reg.eq(17), mdio.data_w.eq(0x0000)], # 2. Зануляем регистр 17 на этой странице (отключаем EEE для 100 Мбит)
                #5: [mdio.reg.eq(31), mdio.data_w.eq(0x0000)], # 3. Обязательно возвращаемся на стандартную страницу 0

                # --- ШАГ 1: Стандартная конфигурация скоростей ---
                #0: [mdio.reg.eq(0x04), mdio.data_w.eq(0x0101)], # Рекламируем только 100FD
                0: [mdio.reg.eq(0x09), mdio.data_w.eq(0x0000)], # Запрещаем гигабит

                # --- ШАГ 2: Официальное отключение Green Ethernet (стр. 25 даташита) ---
                #2: [mdio.reg.eq(31),   mdio.data_w.eq(0x0a43)], # Переходим на страницу 0xa43
                #3: [mdio.reg.eq(25),   mdio.data_w.eq(0x0003)], # Ломаем логику энергосбережения
                #4: [mdio.reg.eq(28),   mdio.data_w.eq(0x573f)], # Выключаем Green Ethernet полностью

                # --- ШАГ 3: Возврат на страницу 0 и запуск автосогласования ---
                #5: [mdio.reg.eq(31),   mdio.data_w.eq(0x0000)], # Возвращаемся на базовую страницу 0
                #6: [mdio.reg.eq(0x00), mdio.data_w.eq(0x1200)], # Перезапускаем AN с новыми правилами

                #7: [mdio.reg.eq(0x00), mdio.data_w.eq(0x9200)],
                "default": [mdio.reg.eq(0x00), mdio.data_w.eq(0x0000)]
            }),
            If(mdio.done,
                NextValue(config_step, config_step + 1),
                NextValue(timeout, 0),
                If(config_step == 0,
                    NextValue(poll_addr, self.mdio_phy_addr0),
                    NextState("MONITOR_WAIT")
                )
            ).Elif(timeout > 100000,
                NextValue(config_step, config_step + 1),
                NextValue(timeout, 0),
                If(config_step == 0,
                    NextValue(poll_addr, self.mdio_phy_addr0),
                    NextState("MONITOR_WAIT")
                )
            ).Else(
                NextValue(timeout, timeout + 1)
            )
        )

        # MONITOR
        self.fsm.act("MONITOR_WAIT",
            NextValue(timeout, timeout + 1),
            If(timeout > 50000,
                NextValue(timeout, 0),
                NextState("MONITOR_READ")
            )
        )

        self.fsm.act("MONITOR_READ",
            mdio.addr.eq(poll_addr),
            mdio.reg.eq(0),
            mdio.read.eq(1),
            If(mdio.done,
                NextValue(bmcr_first, mdio.data_r),
                NextValue(timeout, 0),
                NextState("MONITOR_READ_ST2")
            ).Elif(timeout > 100000,
                NextValue(timeout, 0),
                NextState("MONITOR_WAIT")
            ).Else(
                NextValue(timeout, timeout + 1)
            )
        )

        self.fsm.act("MONITOR_READ_ST2",
            mdio.addr.eq(poll_addr),
            mdio.reg.eq(1),
            mdio.read.eq(1),
            If(mdio.done,
                NextValue(bmsr_first, mdio.data_r),
                NextValue(timeout, 0),
                NextState("MONITOR_READ_AGAIN")
            ).Elif(timeout > 100000,
                NextValue(timeout, 0),
                NextState("MONITOR_WAIT")
            ).Else(
                NextValue(timeout, timeout + 1)
            )
        )

        self.fsm.act("MONITOR_READ_AGAIN",
            mdio.addr.eq(poll_addr),
            mdio.reg.eq(1),
            mdio.read.eq(1),

            If(mdio.done,
                If(poll_addr == self.mdio_phy_addr0,
                    NextValue(self.link0_up, mdio.data_r[2]),
                    NextValue(self.autoneg0_done, mdio.data_r[5])
                ).Else(
                    NextValue(self.link1_up, mdio.data_r[2]),
                    NextValue(self.autoneg1_done, mdio.data_r[5])
                ),

                NextValue(
                    poll_addr,
                    Mux(
                        poll_addr == self.mdio_phy_addr0,
                        self.mdio_phy_addr1,
                        self.mdio_phy_addr0
                    )
                ),
                NextValue(timeout, 0),
                NextState("MONITOR_WAIT")
            ).Elif(timeout > 100000,
                NextValue(timeout, 0),
                NextState("MONITOR_WAIT")
            ).Else(
                NextValue(timeout, timeout + 1)
            )
        )
