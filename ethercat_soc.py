#!/usr/bin/env python3
"""
EtherCAT Slave SoC for Colorlight 5A-75E rev 8.2 (same pinout as 6.0).
"""

from migen import *
from litex.soc.cores.clock import *
from litex.soc.integration.soc_core import *
from litex.soc.integration.builder import *
from litex_boards.platforms import colorlight_5a_75e
#from liteeth.phy.ecp5rgmii import LiteEthPHYRGMII
#from liteeth.phy.parallel.rgmii.ecp5rgmii import LiteEthPHYRGMII
from liteeth.phy.parallel.rgmii.ecp5 import LiteEthPHYRGMII
from litex.build.io import DDROutput, DDRInput
from ethercat_top import EtherCATSlave
from ethercat_dl_router import EtherCATLoopbackRouter
from colorlight_phy_init import ColorlightAutoPHYConfig
from litescope import LiteScopeAnalyzer
from migen.genlib.cdc import MultiReg

class CRG(Module):
    def __init__(self, platform, clk_in):
        self.clock_domains.cd_sys = ClockDomain()
        self.submodules.pll = pll = ECP5PLL()
        pll.register_clkin(clk_in, 25e6)
        pll.create_clkout(self.cd_sys, 50e6)


class EtherCATSoC(SoCMini):
    def __init__(self, platform, **kwargs):
        sys_clk_freq = int(50e6)

        # --- Без CPU: чистый ESC + отладочный мост ---
        #kwargs["cpu_type"]             = None
        #kwargs["integrated_rom_size"]  = 0
        #kwargs["integrated_sram_size"] = 0
        #kwargs["with_uart"]            = False   # UART заменяем UARTBone
        #kwargs["with_timer"]           = False

        #SoCCore.__init__(self, platform, sys_clk_freq, ident="EtherCAT ESC SoC", **kwargs)
        SoCMini.__init__(self, platform, sys_clk_freq, ident="EtherCAT ESC SoC", **kwargs)
        clk25 = platform.request("clk25")
        self.submodules.crg = CRG(platform, clk25)

        # Отладочный мост: доступ к CSR с хоста
        self.add_uartbone(name="serial", baudrate=921600)
        #self.add_csr_bridge()
        # ===================================================================
        # MDIO: общая шина для обоих PHY (пины T4/R5)
        # ===================================================================
        from litex.build.generic_platform import Pins, Subsignal, IOStandard

        platform.add_extension([
            ("eth_mdio_shared", 0,
                Subsignal("mdio", Pins("T4")),
                Subsignal("mdc",  Pins("R5")),
                Subsignal("rst_n",  Pins("R6")),
                IOStandard("LVCMOS33")
            ),
        ])

        mdio_pads = platform.request("eth_mdio_shared")
        self.submodules.phy_init = ColorlightAutoPHYConfig(mdio_pads)
        #LiteEthPHYRGMII.tx_clk_freq = 25e6
        #LiteEthPHYRGMII.rx_clk_freq = 25e6

        add_debug_hub75_resources(platform)

        debug_j1 = platform.request("debug_j1", 0)
        debug_j2 = platform.request("debug_j2", 0)
        debug_j3 = platform.request("debug_j3", 0)
        debug_j4 = platform.request("debug_j4", 0)
        debug_j5 = platform.request("debug_j5", 0)
        debug_j6 = platform.request("debug_j6", 0)
        debug_j7 = platform.request("debug_j7", 0)
        debug_j8 = platform.request("debug_j8", 0)

        # ===================================================================
        # Два PHY: отключаем встроенную MDIO-инициализацию
        # ===================================================================
        self.submodules.ethphy0 = LiteEthPHYRGMII(
            clock_pads=platform.request("eth_clocks", 0),
            pads=platform.request("eth", 0),
            tx_delay=1.5e-9,
            rx_delay=1.5e-9,
            with_hw_init_reset=False,  # Не трогаем MDIO
            with_inband_status=True,
            with_dynamic_link=True
        )

        self.submodules.ethphy1 = LiteEthPHYRGMII(
            clock_pads=platform.request("eth_clocks", 1),
            pads=platform.request("eth", 1),
            tx_delay=1.5e-9,
            rx_delay=1.5e-9,
            with_hw_init_reset=False,
            with_inband_status=True,
            with_dynamic_link=True
        )

        self.platform.add_period_constraint(
            self.ethphy0.crg.cd_eth_rx.clk, 40.0
        )
        self.platform.add_period_constraint(
            self.ethphy1.crg.cd_eth_rx.clk, 40.0
        )
        self.platform.add_period_constraint(
            self.crg.cd_sys.clk, 20.0
        )
        self.platform.add_false_path_constraints(
            self.crg.cd_sys.clk,
            self.ethphy0.crg.cd_eth_rx.clk,
            self.ethphy0.crg.cd_eth_tx.clk,
            self.ethphy1.crg.cd_eth_rx.clk,
            self.ethphy1.crg.cd_eth_tx.clk,
        )

        self.platform.add_false_path_constraints(
            self.ethphy0.crg.cd_eth_rx.clk,
            self.ethphy1.crg.cd_eth_rx.clk,
            self.ethphy1.crg.cd_eth_tx.clk,
        )

        self.platform.add_false_path_constraints(
            self.ethphy0.crg.cd_eth_tx.clk,
            self.ethphy1.crg.cd_eth_rx.clk,
            self.ethphy1.crg.cd_eth_tx.clk,
        )


        # ===================================================================
        # ESC + Router
        # ===================================================================
        self.submodules.esc_core = EtherCATSlave()

        self.submodules.router = EtherCATLoopbackRouter(
            self.ethphy0,
            self.ethphy1,
            self.esc_core,
        )

        # ===================================================================
        # LiteScope: Анализатор внутренних сигналов для отладки EtherCAT
        # ===================================================================
        analyzer_signals = {
            # Группа 0: тракт данных PU (как было)
            0: [
                self.esc_core.pu.sink.valid,
                self.esc_core.pu.sink.ready,
                self.esc_core.pu.sink.data,
                self.esc_core.pu.sink.last,
                self.esc_core.pu.source.valid,
                self.esc_core.pu.source.ready,
                self.esc_core.pu.source.data,
                self.esc_core.pu.source.last,
                self.esc_core.pu.wkc,
                self.esc_core.pu.dbg_cmd_match,
                self.esc_core.pu.dbg_state,
                self.esc_core.pu.dg_addr_reg,
                self.esc_core.pu.mem_logical_access_ok,
                self.esc_core.pu.mem_logical_would_hit,
                self.esc_core.mem.address_read,
                self.esc_core.mem.data_read,
                self.esc_core.mem.write_en,
                self.esc_core.mem.sii.address_read,     # адрес, который читает PU
                self.esc_core.mem.sii.data_read,        # что SII отдаёт
                self.esc_core.mem.sii.hit,              # SII перехватил адрес
                self.esc_core.mem.sii.address_write,    # адрес записи
                self.esc_core.mem.sii.data_write,       # данные записи
                self.esc_core.mem.sii.write_en,         # строб записи
                self.esc_core.mem.sii.dgram_end,        # конец датаграммы
                self.esc_core.mem.sii_hit_q,            # ← если экспортирован
                self.esc_core.mem.sii_data_q,           # ← если экспортирован
                self.esc_core.mem.sii.busy,             # FSM занята
                self.esc_core.mem.sii.start_read,       # команда чтения
                self.esc_core.mem.sii.word_idx,         # 0..3 — какое слово
                self.esc_core.mem.sii.base_addr,        # зафиксированный адрес
                self.esc_core.mem.sii.eeprom_addr,      # адрес от мастера
                self.esc_core.mem.sii.data_buffer, # старшие байты, чтобы видеть
                                                        # заполнение по мере чтения

            ],
            # Группа 1: FMMU
            1: [
                self.esc_core.mem.fmmu.any_hit,
                self.esc_core.mem.fmmu.any_would_hit,
                self.esc_core.mem.fmmu.final_phys_addr,
                self.esc_core.mem.fmmu.access_ok,
            ],
            # Группа 2: SyncManager
            2: [
                self.esc_core.mem.sm.any_ecat_sel,
                self.esc_core.mem.sm.final_ecat_bram_addr,
                self.esc_core.mem.sm.ecat_write_en,
                self.esc_core.mem.sm.ecat_read_en,
            ]
        }
        self.submodules.analyzer = LiteScopeAnalyzer(
            groups       = analyzer_signals,
            depth        = 2048,          # Глубина захвата (можно уменьшить, если не хватает BRAM)
            clock_domain = "sys",         # Домен, в котором работают сигналы
            samplerate   = sys_clk_freq,  # 50 МГц
            csr_csv      = "analyzer.csv" # Файл для софта
        )

        analyzer_rx_signals = {
            
            0: [
            # Группа A: выход PHY (после SDR-реконструкции, до FIFO)
                self.ethphy1.source.valid,
                self.ethphy1.source.ready,
                self.ethphy1.source.data,
                self.ethphy1.source.last,
            #],
            # Группа B: сырой RGMII до DDR-сэмплирования
            #1: [
                self.ethphy1.rx.datapath.rx_ctl,
                self.ethphy1.rx.datapath.rx_data,
                self.ethphy1.rx.datapath.source.valid,
            #],
            # Группа C: состояние линка
            #2: [
                self.ethphy1.link_state_rx.link_up,
                self.ethphy1.link_state_rx.link_100M,
                self.ethphy1.link_state_rx.link_1G,
            ],
        }

        #self.submodules.analyzer_rx = LiteScopeAnalyzer(
        #    groups       = analyzer_rx_signals,
        #    depth        = 2048,
        #    clock_domain = "ethphy1_eth_rx",   # ← ключевой параметр
        #    samplerate   = 25e6,
        #    csr_csv      = "analyzer_rx.csv"
        #)



        link0_sys = Signal()
        link1_sys = Signal()

        self.specials += [
            MultiReg(self.ethphy0.link_state_rx.link_up, link0_sys, odomain="sys"),
            MultiReg(self.ethphy1.link_state_rx.link_up, link1_sys, odomain="sys"),
        ]

        self.comb += [
            self.router.link0.eq(link0_sys),
            self.router.link1.eq(link1_sys),
        ]

        #self.comb += [
        #    self.router.link0.eq(self.ethphy0.link_state_rx.link_up), #self.phy_init.link0_up),
        #    self.router.link1.eq(self.ethphy1.link_state_rx.link_up), #self.phy_init.link1_up),
        #]

        self.comb += [
            self.esc_core.mem.regs.link0_up.eq(self.router.link0),
            self.esc_core.mem.regs.link1_up.eq(self.router.link1),
            self.esc_core.mem.regs.loop0_closed.eq(self.router.loop0_closed),
            self.esc_core.mem.regs.loop1_closed.eq(self.router.loop1_closed),
        ]

        #self.comb += [
        #    debug_j1[0].eq(self.ethphy0.tx.tx_phase),   # RXC, FPGA pin J1
        #    debug_j1[1].eq(self.ethphy0.tx.tx_active),     # RX_CTL, FPGA pin J2
        #    debug_j1[2].eq(self.ethphy0.tx.tx_nibble[0]), # RXD0, FPGA pin K2
        #    debug_j1[3].eq(self.ethphy0.tx.tx_nibble[1]), # RXD1, FPGA pin J3
        #    debug_j1[4].eq(self.ethphy0.tx.tx_nibble[2]), # RXD2, FPGA pin K1
        #    debug_j1[5].eq(self.ethphy0.tx.tx_nibble[3]), # RXD3, FPGA pin K3
        #]

        self.comb += [
            # RX PHY stream
            debug_j2[0].eq(self.ethphy1.source.valid),
            debug_j2[1].eq(self.ethphy1.source.ready),
            debug_j2[2].eq(
                self.ethphy1.source.valid & self.ethphy1.source.ready
            ),

            # speed: 00 = 10M, 01 = 100M, 10 = 1000M
            #debug_j2[3].eq(self.ethphy1.speed[0]),
            #debug_j2[4].eq(self.ethphy1.speed[1]),

            # Последний байт Ethernet-кадра
            debug_j2[5].eq(self.ethphy1.source.last),
        ]


        #self.comb += [
            # J3: попадание кадра внутрь ESC.
        #    debug_j3[0].eq(self.esc_core.sink.valid),
        #    debug_j3[1].eq(self.esc_core.sink.ready),
        #    debug_j3[2].eq(self.esc_core.sink.valid & self.esc_core.sink.ready),
        #    debug_j3[3].eq(self.esc_core.sink.last),

            # J3: ESC действительно выдаёт кадр после обработки.
         #   debug_j3[4].eq(self.esc_core.source.valid),
         #   debug_j3[5].eq(self.esc_core.source.last),



            # J4: какой PHY реально получает TX stream.
          #  debug_j4[0].eq(self.ethphy1.sink.valid),
          #  debug_j4[1].eq(self.ethphy1.sink.ready),
          #  debug_j4[2].eq(self.ethphy1.sink.valid & self.ethphy1.sink.ready),

           # debug_j4[3].eq(self.ethphy0.sink.valid),
           # debug_j4[4].eq(self.ethphy0.sink.ready),
           # debug_j4[5].eq(self.ethphy0.sink.valid & self.ethphy1.sink.ready),
       # ]

        self.comb += [
            # --- Выводим младшие 4 бита команды (DG_CMD) ---
            debug_j3[0].eq(self.esc_core.pu.dbg_cmd[0]),
            debug_j3[1].eq(self.esc_core.pu.dbg_cmd[1]),
            debug_j3[2].eq(self.esc_core.pu.dbg_cmd[2]),
            debug_j3[3].eq(self.esc_core.pu.dbg_cmd[3]),

            # --- Выводим младшие 4 бита считанной длины (DG_LEN) ---
            debug_j4[0].eq(self.esc_core.pu.dbg_dg_len[0]),
            debug_j4[1].eq(self.esc_core.pu.dbg_dg_len[1]),
            debug_j4[2].eq(self.esc_core.pu.dbg_dg_len[2]),
            debug_j4[3].eq(self.esc_core.pu.dbg_dg_len[3]),
        ]


        self.comb += [
            # Фактический TX handshake PHY1
            debug_j5[0].eq(self.ethphy1.sink.valid),
            debug_j5[1].eq(self.ethphy1.sink.ready),
            debug_j5[2].eq(
                self.ethphy1.sink.valid & self.ethphy1.sink.ready
            ),

            # Фактический TX handshake PHY0
            debug_j5[3].eq(self.ethphy0.sink.valid),
            debug_j5[4].eq(self.ethphy0.sink.ready),
            debug_j5[5].eq(
                self.ethphy0.sink.valid & self.ethphy0.sink.ready
            ),
        ]


        self.comb += [
            debug_j6[0].eq(self.router.link0),
            debug_j6[1].eq(self.router.link1),
            debug_j6[2].eq(self.router.loop0_closed),
            debug_j6[3].eq(self.router.loop1_closed),
        ]

        self.specials += [
            DDROutput(
                clk = ClockSignal("sys"),            # Тактовый домен чтения FIFO
                i1  = 1,
                i2  = 0,
                o   = debug_j7[4]                    # Вывод чистого клока на пин 5
            )
        ]

        self.comb += [
            # Выводим младшие 4 бита РЕАЛЬНЫХ данных на входе в ESC
            debug_j7[0].eq(self.esc_core.pu.dbg_state[0]),
            debug_j7[1].eq(self.esc_core.pu.dbg_state[1]),
            debug_j7[2].eq(self.esc_core.pu.dbg_state[2]),
            debug_j7[3].eq(self.esc_core.pu.dbg_state[3]),
        ]

        self.comb += [
            debug_j8[0].eq(self.phy_init.mdio.mdc),
            debug_j8[1].eq(self.phy_init.mdio.mdio_i)
        ]


from litex.build.generic_platform import Subsignal, Pins, IOStandard


def strip_mdio(platform, resource_name="eth"):
    """Удаляет Subsignal'ы mdio/mdc из ресурса, чтобы PHY не создавал свой MDIO."""
    new_available = []
    for res in platform.constraint_manager.available:
        if res[0] == resource_name:
            res = tuple(
                x for x in res
                if not (isinstance(x, Subsignal) and x.name in ("mdio", "mdc", "rst_n"))
            )
        new_available.append(res)
    platform.constraint_manager.available = new_available

from litex.build.generic_platform import Pins, IOStandard

DEBUG_PINS = (0, 1, 2, 4, 5, 6)

def add_debug_hub75_resources(platform):
    debug_io = []

    for connector_number in range(1, 9):
        connector = "j{}".format(connector_number)

        pins = " ".join(
            "{}:{}".format(connector, pin_number)
            for pin_number in DEBUG_PINS
        )

        debug_io.append((
            "debug_{}".format(connector),
            0,
            Pins(pins),
            IOStandard("LVCMOS33"),
        ))

    platform.add_extension(debug_io)



def main():
    # Revision 8.2 (идентична 6.0 по распиновке)
    platform = colorlight_5a_75e.Platform(revision="8.2")
    strip_mdio(platform)          # <-- ДО создания SoC
    soc = EtherCATSoC(platform)
    builder = Builder(soc, output_dir="build_esc")
    builder.build()


if __name__ == "__main__":
    main()
