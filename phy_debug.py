        self.comb += [
            # J2: состояние RX PHY0 после DDR capture / gearbox.

            # Есть байт на LiteEth stream-интерфейсе PHY.
            debug_j2[0].eq(self.ethphy0.source.valid),

            # Последний байт Ethernet-кадра.
            debug_j2[1].eq(self.ethphy0.source.last),

            # Ready от следующего downstream-модуля.
            debug_j2[2].eq(self.ethphy0.source.ready),

            # Определённая PHY скорость.
            # 00 = 10M, 01 = 100M, 10 = 1000M.
            debug_j2[3].eq(self.ethphy0.speed[0]),
            debug_j2[4].eq(self.ethphy0.speed[1]),

            # RX_CTL, захваченный по falling edge.
            debug_j2[5].eq(self.ethphy0.rx.rx_ctl[1]),
        ]
