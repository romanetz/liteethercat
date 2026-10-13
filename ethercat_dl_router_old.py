#!/usr/bin/env python3
"""
EtherCAT Data Link Layer Router with Loop Control.

Implements:
  - Cut-through forwarding P0→ESC→P1, P1→P0 transparent
  - Clock Domain Crossing (AsyncFIFO) between PHY RX and ESC/TX domains
  - Automatic loop closure when link down
  - Frame loopback when port 1 closed
  - TX port arbitration (transit vs loopback)

Topology (normal mode, both links up):
    PHY0.RX → CDC0 → ESC → PHY1.TX
    PHY1.RX → CDC1 → PHY0.TX

Topology (port 1 link down):
    PHY0.RX → CDC0 → ESC → PHY0.TX (loopback)
    PHY1.RX → dropped
"""

from migen import *
from litex.soc.interconnect import stream
from litex.soc.interconnect.stream import AsyncFIFO as StreamAsyncFIFO
from liteeth.common import eth_phy_description


#!/usr/bin/env python3
"""
EtherCAT Data Link Layer Router with Loop Control.

Implements:
  - Cut-through forwarding P0→ESC→P1, P1→P0 transparent
  - Clock Domain Crossing (AsyncFIFO) between PHY RX and ESC/TX domains
  - Automatic loop closure when link down
  - Frame loopback when port 1 closed
  - TX port arbitration (transit vs loopback)

Topology (normal mode, both links up):
    PHY0.RX → CDC0 → ESC → PHY1.TX
    PHY1.RX → CDC1 → PHY0.TX

Topology (port 1 link down):
    PHY0.RX → CDC0 → ESC → PHY0.TX (loopback)
    PHY1.RX → dropped
"""

from migen import *
from litex.soc.interconnect import stream
from liteeth.common import eth_phy_description


class EtherCATLoopbackRouter(Module):
    """
    EtherCAT Data Link Router.

    Routes frames between two PHYs and ESC with automatic loop control.
    """
    def __init__(self, phy0, phy1, esc_slave):
        # Внешние входы (от MDIO-монитора)
        self.link0 = Signal()
        self.link1 = Signal()

        # Статус петель (для ESC регистров)
        self.loop0_closed = Signal()
        self.loop1_closed = Signal()

        # Статистика
        self.frames_p0_rx = Signal(32)
        self.frames_p1_rx = Signal(32)
        self.frames_esc_tx = Signal(32)
        self.frames_loopback = Signal(32)

        # ------------------------------------------------------------------
        # Loop control: автоматическое закрытие при link down
        # ------------------------------------------------------------------
        # Loop 0: всегда открыта (кадры принимаются с порта 0)
        self.comb += self.loop0_closed.eq(0)

        # Loop 1: закрывается при падении линка
        self.comb += self.loop1_closed.eq(~self.link1)

        # ------------------------------------------------------------------
        # CDC FIFOs: PHY RX → ESC/TX домен
        # ------------------------------------------------------------------
        # PHY0.RX (ethphy0_eth_rx) → sys (ESC работает в sys)
        self.submodules.cdc0 = ClockDomainsRenamer(
            {"write": "ethphy0_eth_rx", "read": "sys"}
        )(stream.AsyncFIFO(
            layout=eth_phy_description(8),
            depth=32,
            buffered=True,
        ))
        self.comb += phy0.source.connect(self.cdc0.sink)

        # PHY1.RX (ethphy1_eth_rx) → sys
        self.submodules.cdc1 = ClockDomainsRenamer(
            {"write": "ethphy1_eth_rx", "read": "sys"}
        )(stream.AsyncFIFO(
            layout=eth_phy_description(8),
            depth=32,
            buffered=True,
        ))
        self.comb += phy1.source.connect(self.cdc1.sink)

        # ------------------------------------------------------------------
        # ESC в домене sys
        # ------------------------------------------------------------------
        # ESC уже в sys (из ethercat_top.py), ничего не переименовываем
        #self.submodules.esc = esc_slave

        # CDC0 → ESC
        self.comb += self.cdc0.source.connect(esc_slave.sink)

        # ------------------------------------------------------------------
        # Демультиплексор выхода ESC: P1 или loopback (в sys)
        # ------------------------------------------------------------------
        self.submodules.esc_demux = stream.Demultiplexer(
            eth_phy_description(8), 2
        )

        self.comb += [
            esc_slave.source.connect(self.esc_demux.sink),
            self.esc_demux.sel.eq(self.loop1_closed),  # 0→P1, 1→loopback
        ]

        # ------------------------------------------------------------------
        # CDC для выходных веток: sys → PHY TX домены
        # ------------------------------------------------------------------
        # Ветка 0: ESC → PHY1.TX (sys → ethphy1_eth_tx)
        self.submodules.cdc_esc_to_p1 = ClockDomainsRenamer(
            {"write": "sys", "read": "ethphy1_eth_tx"}
        )(stream.AsyncFIFO(
            layout=eth_phy_description(8),
            depth=32,
            buffered=True,
        ))
        self.comb += [
            self.esc_demux.source0.connect(self.cdc_esc_to_p1.sink),
            self.cdc_esc_to_p1.source.connect(phy1.sink),
        ]

        # Ветка 1: ESC → loopback буфер (sys → sys, но нужен для мультиплексора)
        self.submodules.loopback_fifo = stream.SyncFIFO(
            layout=eth_phy_description(8),
            depth=32,
            buffered=True,
        )
        self.comb += self.esc_demux.source1.connect(self.loopback_fifo.sink)

        # ------------------------------------------------------------------
        # Мультиплексор PHY0.TX: транзит (CDC1) или loopback
        # ------------------------------------------------------------------
        self.submodules.phy0_mux = stream.Multiplexer(
            eth_phy_description(8), 2
        )

        self.comb += [
            self.cdc1.source.connect(self.phy0_mux.sink0),      # Транзит P1→P0
            self.loopback_fifo.source.connect(self.phy0_mux.sink1),  # Loopback ESC→P0
            self.phy0_mux.sel.eq(self.loop1_closed),
        ]

        # sys → ethphy0_eth_tx
        self.submodules.cdc_to_p0 = ClockDomainsRenamer(
            {"write": "sys", "read": "ethphy0_eth_tx"}
        )(stream.AsyncFIFO(
            layout=eth_phy_description(8),
            depth=32,
            buffered=True,
        ))
        self.comb += [
            self.phy0_mux.source.connect(self.cdc_to_p0.sink),
            self.cdc_to_p0.source.connect(phy0.sink),
        ]

        # ------------------------------------------------------------------
        # Статистика (в домене sys)
        # ------------------------------------------------------------------
        self.sync += [
            If(self.cdc0.source.valid & self.cdc0.source.ready & self.cdc0.source.last_be,
                self.frames_p0_rx.eq(self.frames_p0_rx + 1)
            ),
            If(self.cdc1.source.valid & self.cdc1.source.ready & self.cdc1.source.last_be,
                self.frames_p1_rx.eq(self.frames_p1_rx + 1)
            ),
            If(self.esc_demux.source0.valid & self.esc_demux.source0.ready & self.esc_demux.source0.last_be,
                self.frames_esc_tx.eq(self.frames_esc_tx + 1)
            ),
            If(self.esc_demux.source1.valid & self.esc_demux.source1.ready & self.esc_demux.source1.last_be,
                self.frames_loopback.eq(self.frames_loopback + 1)
            ),
        ]

