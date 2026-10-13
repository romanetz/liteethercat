#!/usr/bin/env python3
"""
Полносимметричный EtherCAT Data Link Layer Router с матричной коммутацией.
Разработано для Colorlight 5A-75E (Lattice ECP5).
"""

from migen import *
from litex.soc.interconnect import stream
from liteeth.common import eth_phy_description
from migen.genlib.resetsync import AsyncResetSynchronizer



class EtherCATLoopbackRouter(Module):
    """
    Матричный роутер EtherCAT кадров.
    Автоматически перестраивает внутреннюю топологию под живые порты.
    """
    def __init__(self, phy0, phy1, esc_slave):
        # Входные сигналы аппаратного статуса линков
        self.link0 = Signal()
        self.link1 = Signal()

        # Статус состояния петель
        self.loop0_closed = Signal()
        self.loop1_closed = Signal()

        # Счетчики пакетов (статистика в домене sys)
        self.frames_p0_rx  = Signal(32)
        self.frames_p1_rx  = Signal(32)
        self.frames_p0_tx  = Signal(32)
        self.frames_p1_tx  = Signal(32)

        layout = eth_phy_description(8)

        route_raw     = Signal()   # 1 = PHY1 является upstream
        route_sel     = Signal()
        frame_active  = Signal()

        self.comb += route_raw.eq(~self.link0 & self.link1)

        self.sync += [
            # отслеживаем, идёт ли кадр через ESC
            If(esc_slave.sink.valid & esc_slave.sink.ready,
                frame_active.eq(~esc_slave.sink.last)
            ),
            # переключаем топологию только когда кадра в полёте нет
            If(~frame_active, route_sel.eq(route_raw)),
        ]


        # ------------------------------------------------------------------
        # 1. Логика управления петлями (Loop Control)
        # ------------------------------------------------------------------
        self.comb += [
            self.loop0_closed.eq(~self.link0),
            self.loop1_closed.eq(~self.link1)
        ]

        # Локальные stream endpoint-ы между ESC и портами.
        #esc_to_p1_direct   = stream.Endpoint(layout)
        #esc_to_p0_loopback = stream.Endpoint(layout)

        # ------------------------------------------------------------------
        # 2. RX AsyncFIFO: PHY RX clock domain -> sys.
        # ------------------------------------------------------------------
        #self.submodules.fifo_p0_rx = ClockDomainsRenamer({
        #    "write": "ethphy0_eth_rx",
        #    "read":  "sys",
        #})(
        #    stream.AsyncFIFO(layout=layout, depth=4, buffered=False)
        #)

        self.submodules.fifo_p0_rx = stream.ClockDomainCrossing(layout,
                    cd_from  = "ethphy0_eth_rx",
                    cd_to    = "sys",
                    depth    = 64,
                    buffered = True,
                    with_common_rst=True
                )

        #fifo_p0_rx_raw = ClockDomainsRenamer({
        #    "write": "ethphy0_eth_rx",
        #    "read":  "sys"
        #})(stream.AsyncFIFO(layout, depth=512, buffered=True))

        #self.submodules.fifo_p0_rx = fifo_p0_rx_raw

        # Общий синхронный сброс для обоих доменов (требует проброски rst в платформе)
        #self.specials += [
        #    AsyncResetSynchronizer(phy0.crg.cd_eth_rx, ResetSignal("sys")),
        #]

        #self.submodules.fifo_p1_rx = ClockDomainsRenamer({
        #    "write": "ethphy1_eth_rx",
        #    "read":  "sys",
        #})(
        #    stream.AsyncFIFO(layout=layout, depth=4, buffered=False)
        #)

        self.submodules.fifo_p1_rx = stream.ClockDomainCrossing(layout,
                    cd_from  = "ethphy1_eth_rx",
                    cd_to    = "sys",
                    depth    = 64,
                    buffered = True,
                    with_common_rst=True
                )

        # ------------------------------------------------------------------
        # 4. TX AsyncFIFO: sys -> PHY TX clock domain.
        # ------------------------------------------------------------------
        self.submodules.fifo_p0_tx = stream.ClockDomainCrossing(layout,
                    cd_from  = "sys",
                    cd_to    = "ethphy0_eth_tx",
                    depth    = 64,
                    buffered = True,
                    with_common_rst=True
                )

        self.submodules.fifo_p1_tx = stream.ClockDomainCrossing(layout,
                    cd_from  = "sys",
                    cd_to    = "ethphy1_eth_tx",
                    depth    = 64,
                    buffered = True,
                    with_common_rst=True
                )


        #self.submodules.fifo_p1_tx = ClockDomainsRenamer({
        #    "write": "sys",
        #    "read":  "ethphy1_eth_tx",
        #})(
        #    stream.AsyncFIFO(layout=layout, depth=4, buffered=False)
        #)


        #fifo_p1_rx_raw = ClockDomainsRenamer({
        #    "write": "ethphy1_eth_rx",
        #    "read":  "sys"
        #})(stream.AsyncFIFO(layout, depth=512, buffered=True))

        #self.submodules.fifo_p1_rx = fifo_p1_rx_raw

        # Общий синхронный сброс для обоих доменов (требует проброски rst в платформе)
        #self.specials += [
        #    AsyncResetSynchronizer(phy1.crg.cd_eth_rx, ResetSignal("sys")),
        #]

        self.comb += [
            phy0.source.connect(self.fifo_p0_rx.sink),
            phy1.source.connect(self.fifo_p1_rx.sink),
            self.fifo_p0_tx.source.connect(phy0.sink),
            self.fifo_p1_tx.source.connect(phy1.sink)
        ]

        # ------------------------------------------------------------------
        # 3. Stream routers in sys clock domain.
        # ------------------------------------------------------------------
        # PHY1 RX:
        #   source0: PHY1 RX -> PHY0 TX, return from downstream chain.
        #   source1: PHY1 RX -> ESC, when PHY0 link is absent.
        self.submodules.p1_rx_route = stream.Demultiplexer(layout, 2)

        # ESC input:
        #   sink0: PHY0 RX.
        #   sink1: PHY1 RX, when PHY1 is the upstream/master port.
        self.submodules.esc_in_mux = stream.Multiplexer(layout, 2)

        # ESC output:
        #   source0: ESC -> PHY1 TX.
        #   source1: ESC -> PHY0 TX, local loopback if PHY1 is down.
        self.submodules.esc_out_demux = stream.Demultiplexer(layout, 2)

        # PHY0 TX:
        #   sink0: return PHY1 RX -> PHY0 TX.
        #   sink1: ESC -> PHY0 TX local loopback.
        self.submodules.phy0_tx_mux = stream.Multiplexer(layout, 2)

        #Правило: один вызов, в направлении producer → consumer
        #self.comb += producer.source.connect(consumer.sink)

        self.comb += [
            # --------------------------------------------------------------
            # PHY1 RX routing.
            # --------------------------------------------------------------
            #producer: fifo_p1_rx
            #consumer: demux p1_rx_route
            self.fifo_p1_rx.source.connect(self.p1_rx_route.sink),

            # 1: PHY1 RX -> PHY0 TX.
            # 0: PHY1 RX -> ESC (PHY0 disconnected, PHY1 is upstream).
            self.p1_rx_route.sel.eq(self.link0),
            #producer: demux p1_rx_route
            #consumer: mux phy0_tx_mux
            self.p1_rx_route.source1.connect(self.phy0_tx_mux.sink1),
            #producer: demux p1_rx_route
            #consumer: mux esc_in_mux
            self.p1_rx_route.source0.connect(self.esc_in_mux.sink0),

            # --------------------------------------------------------------
            # ESC input selection.
            # --------------------------------------------------------------
            #producer: fifo_p0_rx
            #consumer: mux esc_in_mux
            self.fifo_p0_rx.source.connect(self.esc_in_mux.sink1),
            # 0: приём из демультиплексора на выходе PHY1 RX, 1: приём из PHY0
            self.esc_in_mux.sel.eq(self.link0),
            # 0: PHY0 RX.
            # 1: PHY1 RX.
            self.esc_in_mux.source.connect(esc_slave.sink),

            # --------------------------------------------------------------
            # ESC output selection.
            # --------------------------------------------------------------
            esc_slave.source.connect(self.esc_out_demux.sink),

            # 0: ESC -> PHY1 TX, if link1 is up.
            # 1: ESC -> PHY0 TX, if link1 is down.
            self.esc_out_demux.sel.eq(self.link1),
            #producer: esc_out_demux
            #consumer: mux phy0_tx_mux
            self.esc_out_demux.source0.connect(self.phy0_tx_mux.sink0),
            # PHY1: only ESC output.
            self.esc_out_demux.source1.connect(self.fifo_p1_tx.sink),
            # --------------------------------------------------------------
            # PHY0 TX selection.
            # --------------------------------------------------------------


            # 1: return path from PHY1 RX.
            # 0: local ESC loopback.
            self.phy0_tx_mux.sel.eq(self.link1),

            # PHY0: return path or ESC local loopback.
            #producer: phy0_tx_mux
            #consumer: fifo_p0_tx
            self.phy0_tx_mux.source.connect(self.fifo_p0_tx.sink),
        ]


        # ------------------------------------------------------------------
        # 5. Мониторинг и статистика кадров (в тактовом домене sys)
        # ------------------------------------------------------------------
        self.sync += [
            If(
                self.fifo_p0_rx.source.valid &
                self.fifo_p0_rx.source.ready &
                self.fifo_p0_rx.source.last,
                self.frames_p0_rx.eq(self.frames_p0_rx + 1)
            ),

            If(
                self.fifo_p1_rx.source.valid &
                self.fifo_p1_rx.source.ready &
                self.fifo_p1_rx.source.last,
                self.frames_p1_rx.eq(self.frames_p1_rx + 1)
            ),

            If(
                self.fifo_p0_tx.sink.valid &
                self.fifo_p0_tx.sink.ready &
                self.fifo_p0_tx.sink.last,
                self.frames_p0_tx.eq(self.frames_p0_tx + 1)
            ),

            If(
                self.fifo_p1_tx.sink.valid &
                self.fifo_p1_tx.sink.ready &
                self.fifo_p1_tx.sink.last,
                self.frames_p1_tx.eq(self.frames_p1_tx + 1)
            )
        ]

