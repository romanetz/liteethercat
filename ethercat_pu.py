#!/usr/bin/env python3
"""
EtherCAT Slave Processing Unit — парсер датаграмм, on-the-fly.
... (комментарии как были)
"""
from migen import *
from litex.soc.interconnect import stream
from liteeth.common import eth_phy_description
from ethercat_const import *

state_map = {"IDLE": 0, "ETH_HEADER": 1, "ECAT_HDR": 2, "DG_CMD": 3,
                "DG_IDX": 4, "DG_ADDR": 5, "DG_LEN": 6, "DG_IRQ": 7,
                "DG_DATA": 8, "DG_WKC_LO": 9, "DG_WKC_HI": 10,
                "PASSTHROUGH": 11, "END": 12,"PREAMBLE":13}

class EtherCATSlavePU(Module):
    def __init__(self, read_latency=2):
        """..."""
        self.sink = sink = stream.Endpoint(eth_phy_description(8))
        self.source = source = stream.Endpoint(eth_phy_description(8))

        # --- физический интерфейс памяти ---
        self.mem_address_read  = Signal(13)
        self.mem_data_read     = Signal(8)
        self.mem_address_write = Signal(13)
        self.mem_data_write    = Signal(8)
        self.mem_write_en      = Signal()

        # --- логический интерфейс памяти ---
        self.mem_logical_address_read  = Signal(32)
        self.mem_logical_data_read     = Signal(8)
        self.mem_logical_address_write = Signal(32)
        self.mem_logical_data_write    = Signal(8)
        self.mem_logical_write_en      = Signal()

        # --- стробы к памяти (FMMU/SM) ---
        self.mem_data_phase       = Signal()
        self.mem_phys_read_stb    = Signal()
        self.mem_logical_read_stb = Signal()
        self.mem_last_data_byte   = Signal()

        # --- статусы от памяти ---
        self.mem_logical_would_hit = Signal()
        self.mem_logical_access_ok = Signal()

        # НОВОЕ: ранний детект FMMU для использования в DG_LEN
        self.mem_logical_early_would_hit = Signal()

        # --- конфигурация ---
        self.fp_address    = Signal(16, reset=0x0000) #fixme -
        self.alias_enable  = Signal()
        self.station_alias = Signal(16)

        # ... остальные сигналы как были ...
        self.wkc            = Signal(16)
        self.ethercat_count = Signal(32)
        self.datagram_count = Signal(8)
        self.dbg_state      = Signal(8)
        self.dbg_cmd        = Signal(8)
        self.dbg_addr       = Signal(32)
        self.dbg_dg_len     = Signal(11)
        self.dbg_cmd_match  = Signal()

        fsm = FSM(reset_state="IDLE")
        self.submodules.fsm = fsm




        # ... все регистры FSM как были ...
        eth_byte_count  = Signal(4)
        addr_byte_count = Signal(3)
        ethertype_low   = Signal(8)
        ethertype_high  = Signal(8)

        dg_cmd_reg  = Signal(8,reset=0)
        dg_idx_reg  = Signal(8,reset=0)
        self.dg_addr_reg = dg_addr_reg = Signal(32,reset=0)
        dg_len_raw  = Signal(16,reset=0)

        cmd      = Signal(8,reset=0)
        idx      = Signal(8,reset=0)
        addr     = Signal(32,reset=0)
        dg_len   = Signal(11,reset=0)
        more_bit = Signal(reset=0)

        read_byte_idx  = Signal(11)
        write_byte_idx = Signal(11)
        data_byte_count = Signal(11)

        wkc_carry         = Signal()
        self.dg_access_ok = Signal()
        wkc_lo_saved      = Signal(8)

        # ... классификаторы команд как были ...
        cmd_is_read      = Signal()
        cmd_is_write     = Signal()
        cmd_is_logical   = Signal()
        cmd_is_broadcast = Signal()
        cmd_is_auto_inc  = Signal()
        cmd_match        = Signal()
        cmd_incr_adp     = Signal()

        addr_position    = Signal(16)
        addr_offset      = Signal(16)
        tx_data          = Signal(8)

        dg_cmd_is_broadcast = Signal()
        dg_cmd_is_auto_inc  = Signal()
        dg_cmd_is_logical   = Signal()

        self.transfer = transfer = Signal()

        total_ecat_len = Signal(16,reset=0)
        total_ecat_counter = Signal(16,reset=0)

        self.comb += [
            sink.ready.eq(source.ready),
            transfer.eq(sink.valid & source.ready),
        ]

        sfd_found = Signal()

        self.sync += [
            sfd_found.eq(
                fsm.ongoing("PREAMBLE") &
                sink.valid &
                (sink.data == 0xD5)
            )
        ]

        self.comb += [
            # Классификация от cmd
            cmd_is_read.eq((cmd == EC_CMD_APRD) | (cmd == EC_CMD_FPRD) |
                          (cmd == EC_CMD_BRD)  | (cmd == EC_CMD_LRD)),
            cmd_is_write.eq((cmd == EC_CMD_APWR) | (cmd == EC_CMD_FPWR) |
                           (cmd == EC_CMD_BWR)  | (cmd == EC_CMD_LWR)),
            cmd_is_logical.eq((cmd == EC_CMD_LRD) | (cmd == EC_CMD_LWR) |
                             (cmd == EC_CMD_LRW)),
            cmd_is_broadcast.eq((cmd == EC_CMD_BRD) | (cmd == EC_CMD_BWR) |
                               (cmd == EC_CMD_BRW)),
            cmd_is_auto_inc.eq((cmd == EC_CMD_APRD) | (cmd == EC_CMD_APWR) |
                              (cmd == EC_CMD_APRW)),
            cmd_incr_adp.eq(cmd_is_auto_inc ),

            # Классификация от dg_cmd_reg
            dg_cmd_is_broadcast.eq((dg_cmd_reg == EC_CMD_BRD) | (dg_cmd_reg == EC_CMD_BWR) |
                                   (dg_cmd_reg == EC_CMD_BRW)),
            dg_cmd_is_auto_inc.eq((dg_cmd_reg == EC_CMD_APRD) | (dg_cmd_reg == EC_CMD_APWR) |
                                  (dg_cmd_reg == EC_CMD_APRW)),
            dg_cmd_is_logical.eq((dg_cmd_reg == EC_CMD_LRD) | (dg_cmd_reg == EC_CMD_LWR) |
                                 (dg_cmd_reg == EC_CMD_LRW)),

            addr_position.eq(addr[0:16]),
            addr_offset.eq(addr[16:32]),
        ]

        self.comb += [
            # По умолчанию ядро ВСЕГДА готово принимать байты из FIFO,
            # чтобы не затыкать сетевой поток Crossbar-матрицы
            #sink.ready.eq(source.ready),

            # По умолчаниюvalid и данные сквозняком транслируются со входа на выход
            source.valid.eq(sink.valid),
            source.last.eq(sink.last),
            source.last.eq(sink.last),
            # Безопасные дефолты для внутренней памяти
            self.mem_write_en.eq(0),
            self.mem_logical_write_en.eq(0),
            self.mem_data_phase.eq(0),
            self.mem_last_data_byte.eq(0),
            self.fp_address.eq(1) #fixme
        ]

        # ------------------------------------------------------------------
        # Адреса памяти: раздельные для чтения и записи
        # ------------------------------------------------------------------
        self.comb += [
            # Адрес ЧТЕНИЯ: с префетчем
            self.mem_address_read.eq(addr_offset + read_byte_idx),
            self.mem_logical_address_read.eq(addr + read_byte_idx),

            # Адрес ЗАПИСИ: синхронизирован с sink.data
            self.mem_address_write.eq(addr_offset + write_byte_idx),
            self.mem_logical_address_write.eq(addr + write_byte_idx),
        ]

        #rd_strobe = Signal()
        #self.comb += [
        #    rd_strobe.eq(cmd_match & cmd_is_read & (dg_len != 0) &
        #                (fsm.ongoing("DG_IRQ") | fsm.ongoing("DG_DATA"))),
        #    self.mem_phys_read_stb.eq(rd_strobe & ~cmd_is_logical),
        #    self.mem_logical_read_stb.eq(rd_strobe & cmd_is_logical),
        #]

        # СТАЛО:
        rd_strobe = Signal()
        early_rd_strobe = Signal()
        self.comb += [
            # BRAM имеет 2 цикла задержки (bram_read_addr_reg + sync порт).
            # Байт 0 нужно начать читать в DG_IRQ[0] — до подтверждения cmd_match.
            # cmd_is_logical и cmd_is_read уже корректны (установлены в DG_IDX).
            #early_rd_strobe.eq(
            #    cmd_is_logical & cmd_is_read & (dg_len != 0) &
            #    fsm.ongoing("DG_IRQ") & (eth_byte_count == 0)
            #)
            early_rd_strobe.eq(
                cmd_is_read & (dg_len != 0) &
                fsm.ongoing("DG_IRQ") & (eth_byte_count == 0) &
                cmd_match
            ),

            #early_rd_strobe.eq(
            #    cmd_is_read & (dg_len != 0) &
            #    fsm.ongoing("DG_LEN") & (eth_byte_count == 1)   # последний такт DG_LEN
            #),
            rd_strobe.eq(
                (cmd_match & cmd_is_read & (dg_len != 0) &
                (fsm.ongoing("DG_IRQ") | fsm.ongoing("DG_DATA"))) |
                early_rd_strobe
            ),
            self.mem_phys_read_stb.eq(rd_strobe & ~cmd_is_logical),
            self.mem_logical_read_stb.eq(rd_strobe & cmd_is_logical),
        ]


        wkc_inc    = Signal()
        wkc_lo_sum = Signal(9)
        wkc_hi_sum = Signal(9)

        self.comb += [
            wkc_inc.eq(cmd_match & (dg_len != 0) &
                      (~cmd_is_logical | self.dg_access_ok)),
            wkc_lo_sum.eq(sink.data + wkc_inc),
            wkc_hi_sum.eq(sink.data + wkc_carry),
        ]


        self.comb += [
            self.dbg_cmd.eq(dg_cmd_reg),
            self.dbg_addr.eq(addr),
            self.dbg_dg_len.eq(dg_len),
            self.dbg_cmd_match.eq(cmd_match),
            source.data.eq(tx_data),
            source.last.eq(sink.last),
        ]


        for name, val in state_map.items():
            self.comb += If(fsm.ongoing(name), self.dbg_state.eq(val))

        self.sync += [
            If(fsm.ongoing("DG_WKC_HI") & transfer,
                self.wkc.eq(Cat(wkc_lo_saved, wkc_hi_sum[0:8])))
        ]

        # ============================ FSM ============================

        fsm.act("IDLE",
            self.mem_write_en.eq(0),
            self.mem_logical_write_en.eq(0),
            self.mem_data_phase.eq(0),
            self.mem_last_data_byte.eq(0),

            #sink.ready.eq(1),
            source.valid.eq(sink.valid),
            tx_data.eq(sink.data),
            If(transfer,
                NextValue(eth_byte_count, 1),
                NextValue(cmd_match, 0),
                NextValue(self.dg_access_ok, 0),
                NextValue(wkc_carry, 0),
                NextValue(self.datagram_count, 0),
                NextState("PREAMBLE")
            )
        )

        # Добавляем новое состояние PREAMBLE:
        fsm.act("PREAMBLE",
            source.valid.eq(sink.valid),
            tx_data.eq(sink.data),
            If(transfer,
                If(sink.data == 0xD5,
                    NextValue(eth_byte_count, 0),  # ← ДОБАВИТЬ
                    NextState("ETH_HEADER")
                ).Else(
                    NextValue(eth_byte_count, eth_byte_count + 1)  # ← ПЕРЕМЕСТИТЬ
                ),
                If(sink.last,
                    NextState("IDLE")
                )
            )
        )

        fsm.act("ETH_HEADER",
            #sink.ready.eq(1),
            source.valid.eq(sink.valid),
            tx_data.eq(sink.data),
            If(transfer,
                If(eth_byte_count == 12, NextValue(ethertype_high, sink.data)),
                If(eth_byte_count == 13, NextValue(ethertype_low, sink.data)),
                NextValue(eth_byte_count, eth_byte_count + 1),
                If(eth_byte_count == 13,
                    If((sink.data == (ETHERTYPE_ECAT & 0xFF )) &
                       (ethertype_high == (ETHERTYPE_ECAT >> 8)),
                        NextValue(eth_byte_count, 0),
                        NextState("ECAT_HDR")
                    ).Else(
                        NextState("PASSTHROUGH")
                    )
                )
        #    )#.Else(
             #   NextState("IDLE")
            )
        )

        #fsm.act("ETH_HEADER",
        #    sink.ready.eq(1),
        #    source.valid.eq(sink.valid),
        #    tx_data.eq(sink.data),

        #    If(transfer,
                # Сдвигаем байты в скользящем окне:
                # ethertype_low запоминает то, что было на прошлом такте valid
        #        NextValue(ethertype_low, sink.data),

                # Проверяем: был ли на прошлом такте valid 0x88,
                # а сейчас прилетел 0xA4?
        #        If((ethertype_low == 0x88) & (sink.data == 0xA4),
        #            NextValue(eth_byte_count, 0),
        #            NextState("ECAT_HDR") # УРА, ПРАВИЛЬНЫЙ ETHERCAT! (Состояние 2)
        #        ),

                # Логика безопасности: если кадр внезапно кончился, а мы
                # так и не нашли 0x88A4 — возвращаемся в IDLE
        #        If(sink.last,
        #            NextState("IDLE")
        #        )
        #    )
        #)


        fsm.act("ECAT_HDR",
            #sink.ready.eq(1),
            source.valid.eq(sink.valid),
            tx_data.eq(sink.data),
            If(transfer,
                If(eth_byte_count == 0,
                    NextValue(eth_byte_count, 1)
                ).Elif(eth_byte_count == 1,
                    NextValue(eth_byte_count, 0),
                    NextState("DG_CMD")
                )
            )#.Else(
             #   NextState("IDLE")
            #)
        )

        fsm.act("DG_CMD",
            #sink.ready.eq(1),
            source.valid.eq(sink.valid),
            tx_data.eq(sink.data),
            If(transfer,
                NextValue(dg_cmd_reg, sink.data),
                NextValue(self.dg_access_ok, 0),
                NextValue(self.datagram_count, self.datagram_count + 1),
                NextState("DG_IDX")
            )#.Else(
             #   NextState("IDLE")
            #)
        )

        fsm.act("DG_IDX",
            #sink.ready.eq(1),
            source.valid.eq(sink.valid),
            tx_data.eq(sink.data),
            If(transfer,
                NextValue(dg_idx_reg, sink.data),
                NextValue(cmd, dg_cmd_reg),
                NextValue(addr_byte_count, 0),
                NextValue(dg_addr_reg, 0),
                NextState("DG_ADDR")
            )#.Else(
             #   NextState("IDLE")
            #)
        )

        adp_lo_sum = Signal(9)
        adp_carry  = Signal()
        self.comb += adp_lo_sum.eq(sink.data + 1)

        #fsm.act("DG_ADDR",
        #    #sink.ready.eq(1),
        #    source.valid.eq(sink.valid),
        #    tx_data.eq(sink.data),
        #    If(transfer,
        #       If(cmd_incr_adp & (addr_byte_count == 0),
        #            tx_data.eq(adp_lo_sum[0:8]),
        #            NextValue(adp_carry, adp_lo_sum[8]),
        #        ).Elif(cmd_incr_adp & (addr_byte_count == 1),
        #            tx_data.eq(sink.data + adp_carry),
        #            NextValue(adp_carry, 0),
        #        ),
        #        NextValue(dg_addr_reg,
        #                  dg_addr_reg | (sink.data << (addr_byte_count << 3))),
        #        NextValue(addr_byte_count, addr_byte_count + 1),
        #        If(addr_byte_count == 3,
        #            #NextValue(addr,dg_addr_reg),
        #            NextValue(eth_byte_count, 0),
        #            NextState("DG_LEN"))
        #    )#.Else(
             #   NextState("IDLE")
            #)
        #)

        fsm.act("DG_ADDR",
            source.valid.eq(sink.valid),
            tx_data.eq(sink.data),
            If(transfer,
                If(cmd_incr_adp & (addr_byte_count == 0),
                    tx_data.eq(adp_lo_sum[0:8]),
                    NextValue(adp_carry, adp_lo_sum[8]),
                ).Elif(cmd_incr_adp & (addr_byte_count == 1),
                    tx_data.eq(sink.data + adp_carry),
                    NextValue(adp_carry, 0),
                ),

                # вдвигаем байт в старшую позицию, остальное сдвигаем вправо
                #NextValue(dg_addr_reg, Cat(dg_addr_reg[8:32], sink.data)),
                Case(addr_byte_count, {
                    0: NextValue(dg_addr_reg[0:8],   sink.data),
                    1: NextValue(dg_addr_reg[8:16],  sink.data),
                    2: NextValue(dg_addr_reg[16:24], sink.data),
                    3: NextValue(dg_addr_reg[24:32], sink.data),
                }),
                NextValue(addr_byte_count, addr_byte_count + 1),
                If(addr_byte_count == 3,
                    NextValue(eth_byte_count, 0),
                    NextState("DG_LEN"))
            )
        )


        # ============================== КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ ==============================
        fsm.act("DG_LEN",
            #sink.ready.eq(1),
            source.valid.eq(sink.valid),
            tx_data.eq(sink.data),
            If(transfer,
                If(eth_byte_count == 0,
                    NextValue(dg_len_raw, sink.data),
                    NextValue(eth_byte_count, 1)
                ).Elif(eth_byte_count == 1,
                    NextValue(dg_len_raw, dg_len_raw | (sink.data << 8)),
                    NextValue(dg_len,   (dg_len_raw | (sink.data << 8)) & 0x7FF),
                    NextValue(more_bit, (dg_len_raw | (sink.data << 8)) >> 15),

                    # КРИТИЧНО: СНАЧАЛА обновляем addr, ПОТОМ используем его для cmd_match
                    NextValue(addr, dg_addr_reg),

                    NextValue(data_byte_count, 0),
                    NextValue(eth_byte_count, 0),
                    NextValue(read_byte_idx, 0),
                    NextValue(write_byte_idx, 0),

                    # Раннее определение cmd_match
                    # Используем self.mem_logical_early_would_hit — он зависит напрямую от dg_addr_reg
                    If(dg_cmd_is_broadcast,
                        NextValue(cmd_match, 1)
                    ).Elif(dg_cmd_is_auto_inc,
                        NextValue(cmd_match, dg_addr_reg[0:16] == 0x0000)
                    ).Elif(dg_cmd_is_logical,
                        NextValue(cmd_match, self.mem_logical_early_would_hit)
                    ).Else(
                        NextValue(cmd_match,
                            (dg_addr_reg[0:16] == self.fp_address) |
                            (self.alias_enable & (dg_addr_reg[0:16] == self.station_alias)))
                    ),
                    NextState("DG_IRQ")
                )
            )#.Else(
             #   NextState("IDLE")
            #)
        )


        fsm.act("DG_IRQ",
            #sink.ready.eq(1),
            source.valid.eq(sink.valid),
            tx_data.eq(sink.data),
            If(transfer,
                If(eth_byte_count == 0,
                    # IRQ_lo: addr уже обновлён → фиксируем cmd_match для логических команд
                    # Проверка логического доступа для dg_access_ok
                    If(cmd_is_logical,
                        #cmd_match.eq(1),#self.mem_logical_would_hit),
                        NextValue(cmd_match, self.mem_logical_would_hit),  # ← ИСПРАВЛЕНИЕ
                        If(self.mem_logical_access_ok,
                            NextValue(self.dg_access_ok, 1),
                        )
                    ).Else(
                        NextValue(self.dg_access_ok, cmd_match)
                    ),
                    NextValue(read_byte_idx, read_byte_idx + 1),
                    NextValue(eth_byte_count, 1),
                    self.mem_data_phase.eq(1),
                    ).Elif(eth_byte_count == 1,
                    NextValue(eth_byte_count, 0),
                    NextValue(read_byte_idx, read_byte_idx + 1),
                    self.mem_data_phase.eq(1),
                    If(dg_len == 0,
                        NextState("DG_WKC_LO")
                    ).Else(
                        NextState("DG_DATA")
                    ),
                )
            )#.Else(
             #   NextState("IDLE")
            #)
        )


        # Флаг: используем ли данные из BRAM?
        #use_bram_data = Signal()
        #self.comb += [
            # Данные из BRAM готовы через read_latency байт после IRQ_lo
            # IRQ_lo (idx=0), IRQ_hi (idx=1), Data[0] (idx=2) → latency=2 → используем с Data[2]
        #    use_bram_data.eq(data_byte_count >= read_latency)
        #]

        fsm.act("DG_DATA",
            #sink.ready.eq(1),
            source.valid.eq(sink.valid),
            tx_data.eq(sink.data),
            self.mem_data_phase.eq(1),
            If(transfer,
                # ЧТЕНИЕ: подмена данных из BRAM (когда они готовы)
                If(cmd_match & cmd_is_read,
                    If(cmd_is_logical,
                        tx_data.eq(self.mem_logical_data_read)
                    ).Else(
                        tx_data.eq(self.mem_data_read)
                    ),
                ),

                # ЗАПИСЬ: немедленно (sink.data → BRAM через write pipeline)
                If(cmd_match & cmd_is_write,
                    If(cmd_is_logical,
                        # Адрес уже установлен через write_byte_idx комбинаторно
                        self.mem_logical_data_write.eq(sink.data),
                        self.mem_logical_write_en.eq(1),
                    ).Else(
                        self.mem_data_write.eq(sink.data),
                        self.mem_write_en.eq(1),
                    )
                ),

                # Обновление dg_access_ok для логических команд (по мере чтения)
                If(cmd_is_logical & self.mem_logical_access_ok,
                    NextValue(self.dg_access_ok, 1)
                ),

                # Инкремент счётчиков
                NextValue(read_byte_idx, read_byte_idx + 1),      # Префетч следующего
                NextValue(write_byte_idx, write_byte_idx + 1),    # Запись текущего
                NextValue(data_byte_count, data_byte_count + 1),  # Основной счётчик

                # Конец фазы данных
                If(data_byte_count == (dg_len - 1),
                    self.mem_last_data_byte.eq(1),
                    NextValue(data_byte_count, 0), # Сбрасываем счетчик данных
                    NextState("DG_WKC_LO")
                ),

                #If(data_byte_count == 1,
                #    self.mem_last_data_byte.eq(1),
                #    NextValue(data_byte_count, 0), # Сбрасываем счетчик данных
                #    NextState("DG_WKC_LO")         # Уходим в состояние 9!
                #)
            )#.Else(
             #   NextState("IDLE")
            #)
        )

        fsm.act("DG_WKC_LO",
            #sink.ready.eq(1),
            source.valid.eq(sink.valid),
            If(transfer,
                tx_data.eq(wkc_lo_sum[0:8]),
                NextValue(wkc_lo_saved, wkc_lo_sum[0:8]),
                NextValue(wkc_carry, wkc_lo_sum[8]),
                NextState("DG_WKC_HI")
            )#.Else(
             #   NextState("IDLE")
            #)
        )

        fsm.act("DG_WKC_HI",
            #sink.ready.eq(1),
            source.valid.eq(sink.valid),
            If(transfer,
                tx_data.eq(wkc_hi_sum[0:8]),
                NextValue(wkc_carry, 0),
                If(more_bit,
                    # Цепочка датаграмм: переход к следующей
                    NextValue(cmd_match, 0),
                    NextValue(self.dg_access_ok, 0),
                    NextValue(data_byte_count, 0),
                    NextState("DG_CMD")
                #)
                #.Elif(sink.last),
                #    NextValue(self.ethercat_count, self.ethercat_count + 1),
                #    NextState("IDLE")
                ).Else(
                    NextValue(self.ethercat_count, self.ethercat_count + 1),
                    NextState("PASSTHROUGH")
                )
            )#.Else(
             #   NextState("IDLE")
            #)
        )

        fsm.act("PASSTHROUGH",
            #sink.ready.eq(1),
            source.valid.eq(sink.valid),
            tx_data.eq(sink.data),
            If((sink.last | sink.last), NextState("IDLE"))
        )

        fsm.act("END",
            #sink.ready.eq(1),
            source.valid.eq(sink.valid),
            tx_data.eq(sink.data),
            If((sink.last | sink.last), NextState("IDLE"))
            #If(~sink.valid, NextState("IDLE"))
        )
