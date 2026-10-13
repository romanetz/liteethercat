#!/usr/bin/env python3
"""
EtherCAT Data Link CDC forwarder.

Все AsyncFIFO обязаны иметь переименованные домены write/read,
иначе migen выбросит 'Unresolved clock domain read'.
"""

from migen import *
from migen.genlib.cdc import MultiReg
from litex.soc.interconnect import stream
from liteeth.common import eth_phy_description


def cdc_fifo(wr_cd, rd_cd, depth=32):
    """Помощник: AsyncFIFO с корректным переименованием доменов."""
    return ClockDomainsRenamer({"write": wr_cd, "read": rd_cd})(
        stream.AsyncFIFO(eth_phy_description(8), depth, buffered=True)
    )


class EtherCATFrameForwarder(Module):
    """
    Приводит оба PHY в домен sys и обратно.

    Экспортирует:
        rx0, rx1 : stream.Endpoint в домене sys (данные из PHY)
        tx0, tx1 : stream.Endpoint в домене sys (данные в PHY)
    """
    def __init__(self, phy0, phy1, depth=32):
        # --- RX: домен PHY -> sys ---
        self.submodules.rx0_fifo = rx0_fifo = cdc_fifo("ethphy0_eth_rx", "sys", depth)
        self.submodules.rx1_fifo = rx1_fifo = cdc_fifo("ethphy1_eth_rx", "sys", depth)

        # --- TX: sys -> домен PHY ---
        self.submodules.tx0_fifo = tx0_fifo = cdc_fifo("sys", "ethphy0_eth_tx", depth)
        self.submodules.tx1_fifo = tx1_fifo = cdc_fifo("sys", "ethphy1_eth_tx", depth)

        self.comb += [
            phy0.source.connect(rx0_fifo.sink),
            phy1.source.connect(rx1_fifo.sink),
            tx0_fifo.source.connect(phy0.sink),
            tx1_fifo.source.connect(phy1.sink),
        ]

        # Всё, что ниже — уже чистый sys
        self.rx0 = rx0_fifo.source
        self.rx1 = rx1_fifo.source
        self.tx0 = tx0_fifo.sink
        self.tx1 = tx1_fifo.sink
