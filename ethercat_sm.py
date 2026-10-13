#!/usr/bin/env python3
"""
EtherCAT SyncManager (SM) для ESC.

АРХИТЕКТУРА ПАМЯТИ (критично):
  0x0000-0x0FFF (4KB)  — Регистры ESC
  0x1000-0x1FFF (4KB)  — DPRAM Buffer 0 (видим мастеру и процессору)
  0x2000-0x2FFF (4KB)  — Shadow Buffer 1 (скрыт от внешнего мира)
  0x3000-0x3FFF (4KB)  — Shadow Buffer 2 (скрыт от внешнего мира)
  ──────────────────────────────────────────────────────────────
  Итого: 16KB BRAM, 14-битная адресация

КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ V3:
- Добавлено комбинаторное предсказание ротации буферов для нулевой латентности
  доступа при захвате shadow-буфера процессором/сетью.
"""

from migen import *
from functools import reduce
from operator import or_

# Константы адресного пространства ESC
ESC_REGS_START   = 0x0000
ESC_REGS_END     = 0x0FFF
ESC_DPRAM_START  = 0x1000
ESC_DPRAM_END    = 0x1FFF
SHADOW_BUF1_BASE = 0x2000
SHADOW_BUF2_BASE = 0x3000
ESC_MEM_END      = 0x3FFF

ESC_MEM_BITS     = 14      # Полное адресное пространство 16KB


class EtherCATSyncManagerChannel(Module):
    """
    Один канал SyncManager.

    ТРЕБОВАНИЯ К КОНФИГУРАЦИИ:
    - phys_start_addr должен быть внутри 0x1000-0x1FFF.
    - length не должен превышать 4KB (размер буфера).
    - phys_start_addr + length не должен выходить за 0x2000.
    """
    def __init__(self, base_register_address):
        self.base_addr = base_register_address

        # ---------------------------------------------------------------------
        # Интерфейсы
        # ---------------------------------------------------------------------
        # Сетевая сторона (парсер датаграмм)
        self.ecat_addr           = Signal(13)  # Входящий адрес из датаграммы (0x0000-0x1FFF)
        self.ecat_data_phase     = Signal()
        self.ecat_write_en       = Signal()
        self.ecat_read_en        = Signal()
        self.ecat_last_data_byte = Signal()

        # Процессорная сторона (PDI / Wishbone)
        self.proc_addr       = Signal(13)      # Адрес от процессора (0x0000-0x1FFF)
        self.proc_read_en    = Signal()
        self.proc_write_en   = Signal()
        self.proc_data_valid = Signal()
        self.proc_last_data_byte = Signal()  # ← новое



        # Выходы к dual-port BRAM (14-битные, включают shadow-буферы)
        self.ecat_bram_addr  = Signal(ESC_MEM_BITS)
        self.proc_bram_addr  = Signal(ESC_MEM_BITS)

        # Диагностика
        self.irq             = Signal()
        self.buffer_error    = Signal()

        # ---------------------------------------------------------------------
        # Конфигурационные регистры
        # ---------------------------------------------------------------------
        self.phys_start_addr = Signal(13)
        self.length          = Signal(16)
        self.control         = Signal(8)
        self.status          = Signal(8)
        self.activate        = Signal()
        self.pdi_control     = Signal(8)

        # Конфигурационная шина
        #self.reg_write_addr  = Signal(16)
        #self.reg_write_data  = Signal(8)
        #self.reg_write_en    = Signal()
        #self.reg_read_addr   = Signal(16)
        #self.reg_read_data   = Signal(8)

        # ---- Регистровый интерфейс: сторона сети (ECAT) ----
        # Владеет 0x00-0x04, 0x06. Читает всё.
        self.ecat_reg_addr       = Signal(16)
        self.ecat_reg_write_data = Signal(8)
        self.ecat_reg_write_en   = Signal()
        self.ecat_reg_read_data  = Signal(8)

        # ---- Регистровый интерфейс: сторона процессора (PDI) ----
        # Владеет только 0x07. Читает всё.
        self.pdi_reg_addr        = Signal(16)
        self.pdi_reg_write_data  = Signal(8)
        self.pdi_reg_write_en    = Signal()
        self.pdi_reg_read_data   = Signal(8)
        reconfig_write = Signal()
        # Диагностика: попытка записи в чужой байт
        self.reg_access_violation = Signal()

        self.ecat_sel = Signal()
        self.proc_sel = Signal()
        # ---------------------------------------------------------------------
        # Декодирование адресов регистров
        # ---------------------------------------------------------------------
        ecat_reg_hit = Signal()
        pdi_reg_hit  = Signal()
        ecat_off     = Signal(3)
        pdi_off      = Signal(3)

        # ---------------------------------------------------------------------
        # Указатели буферов для 3-Buffer режима
        # ---------------------------------------------------------------------
        sub_buffer_ecat   = Signal(2, reset=0)
        sub_buffer_proc   = Signal(2, reset=1)
        sub_buffer_shadow = Signal(2, reset=2)
        shadow_has_new_data = Signal()

        # ---------------------------------------------------------------------
        # Mailbox состояние
        # ---------------------------------------------------------------------
        mailbox_buffer_locked = Signal()
        mailbox_irq_pending   = Signal()

        self.comb += [
            ecat_reg_hit.eq((self.ecat_reg_addr >= self.base_addr) &
                            (self.ecat_reg_addr <  self.base_addr + 8)),
            pdi_reg_hit.eq((self.pdi_reg_addr  >= self.base_addr) &
                           (self.pdi_reg_addr  <  self.base_addr + 8)),
            ecat_off.eq(self.ecat_reg_addr - self.base_addr),
            pdi_off.eq(self.pdi_reg_addr  - self.base_addr),
        ]

        # ---------------------------------------------------------------------
        # Запись конфигурации: ECAT владеет 0x00-0x04, 0x06; PDI владеет 0x07
        # ---------------------------------------------------------------------
        self.comb += reconfig_write.eq(
            self.ecat_reg_write_en & ecat_reg_hit & (ecat_off <= 0x04) & ~self.activate
        )

        self.sync += [
            self.reg_access_violation.eq(0),

            If(self.ecat_reg_write_en & ecat_reg_hit,
                Case(ecat_off, {
                    # 0x00-0x04: только при деактивированном SM, иначе молча игнорируем
                    0x00: If(~self.activate, self.phys_start_addr[0:8].eq(self.ecat_reg_write_data)),
                    0x01: If(~self.activate, self.phys_start_addr[8:13].eq(self.ecat_reg_write_data[0:5])),
                    0x02: If(~self.activate, self.length[0:8].eq(self.ecat_reg_write_data)),
                    0x03: If(~self.activate, self.length[8:16].eq(self.ecat_reg_write_data)),
                    0x04: If(~self.activate, self.control.eq(self.ecat_reg_write_data)),
                    # 0x06 Activate: писать можно ВСЕГДА — это единственный способ
                    # включить/выключить канал
                    0x06: self.activate.eq(self.ecat_reg_write_data[0]),
                    "default": self.reg_access_violation.eq(1),
                })
            ),

            If(self.pdi_reg_write_en & pdi_reg_hit,
                If(pdi_off == 0x07,
                    # PDI Control — тоже только при деактивированном SM (даташит 2.11)
                    If(~self.activate, self.pdi_control.eq(self.pdi_reg_write_data))
                ).Else(
                    self.reg_access_violation.eq(1)
                )
            ),

            If(reconfig_write,
                sub_buffer_ecat.eq(0),
                sub_buffer_proc.eq(1),
                sub_buffer_shadow.eq(2),
                shadow_has_new_data.eq(0),
                self.status.eq(0),
                mailbox_buffer_locked.eq(0),
                mailbox_irq_pending.eq(0),
            ),
        ]
        activate_d = Signal()
        self.sync += activate_d.eq(self.activate)
        self.activated_now = Signal()    # строб: SM только что включён
        self.comb += self.activated_now.eq(self.activate & ~activate_d)
        # ---------------------------------------------------------------------
        # Чтение: обе стороны видят все 8 байт
        # ---------------------------------------------------------------------
        def _reg_mux(off):
            return Array([
                self.phys_start_addr[0:8],
                Cat(self.phys_start_addr[8:13], Constant(0, 3)),
                self.length[0:8],
                self.length[8:16],
                self.control,
                self.status,
                self.activate,
                self.pdi_control,
            ])[off]

        self.comb += [
            If(ecat_reg_hit, self.ecat_reg_read_data.eq(_reg_mux(ecat_off))),
            If(pdi_reg_hit,  self.pdi_reg_read_data.eq(_reg_mux(pdi_off))),
        ]


        # ---------------------------------------------------------------------
        # Декодирование режима из Control
        # ---------------------------------------------------------------------
        is_mailbox_mode = Signal()
        direction_write = Signal()

        self.comb += [
            is_mailbox_mode.eq(self.control[0:2] == 0b10),
            direction_write.eq(self.control[2]),
        ]

        # ---------------------------------------------------------------------
        # Проверка границ (комбинаторно)
        # ---------------------------------------------------------------------
        buffer_end_addr = Signal(17)
        ecat_inside_buffer = Signal()
        proc_inside_buffer = Signal()

        self.comb += [
            buffer_end_addr.eq(self.phys_start_addr + self.length),

            self.buffer_error.eq(
                (self.phys_start_addr < ESC_DPRAM_START) |
                (buffer_end_addr > (ESC_DPRAM_END + 1)) |
                (self.length > 0x1000)
            ),

            ecat_inside_buffer.eq(
                (self.ecat_addr >= self.phys_start_addr) &
                (self.ecat_addr < buffer_end_addr) &
                (self.ecat_addr >= ESC_DPRAM_START)
            ),
            proc_inside_buffer.eq(
                (self.proc_addr >= self.phys_start_addr) &
                (self.proc_addr < buffer_end_addr) &
                (self.proc_addr >= ESC_DPRAM_START)
            ),
        ]

        ecat_window_last = Signal()
        self.comb += [
            ecat_window_last.eq(ecat_inside_buffer &
                                (self.ecat_addr == (buffer_end_addr - 1))),
            self.ecat_sel.eq(self.activate & ~self.buffer_error & ecat_inside_buffer),
            self.proc_sel.eq(self.activate & ~self.buffer_error & proc_inside_buffer),
        ]

        # ---------------------------------------------------------------------
        # FSM ротации буферов
        # ---------------------------------------------------------------------
        proc_access_first_byte = Signal()
        ecat_access_first_byte = Signal()

        self.comb += [
            proc_access_first_byte.eq(
                proc_inside_buffer & (self.proc_addr == self.phys_start_addr)
            ),
            ecat_access_first_byte.eq(
                ecat_inside_buffer & (self.ecat_addr == self.phys_start_addr)
            ),
        ]

        self.sync += [
            If(self.activate & ~self.buffer_error,
                If(is_mailbox_mode,
                    # ========================== MAILBOX MODE ==========================
                    If(direction_write,
                        If(self.ecat_write_en & ecat_inside_buffer & (self.ecat_last_data_byte | ecat_window_last),
                            mailbox_buffer_locked.eq(1),
                            self.status[0].eq(1),
                            If(self.pdi_control[0], mailbox_irq_pending.eq(1))
                        ),
                        If(self.pdi_control[0] & mailbox_buffer_locked,
                            mailbox_buffer_locked.eq(0),
                            self.status[0].eq(0),
                            mailbox_irq_pending.eq(0)
                        )
                    ).Else(
                        # Mailbox TX: ИСПРАВЛЕНО — взвод по завершении записи
                        If(self.proc_write_en & proc_inside_buffer &
                           self.proc_data_valid & self.proc_last_data_byte,
                            mailbox_buffer_locked.eq(1),
                            self.status[3].eq(1),
                            If(self.pdi_control[3], mailbox_irq_pending.eq(1))
                        ),
                        If(self.ecat_read_en & ecat_inside_buffer & (self.ecat_last_data_byte | ecat_window_last),
                            mailbox_buffer_locked.eq(0),
                            self.status[3].eq(0),
                            mailbox_irq_pending.eq(0)
                        )
                    ),
                    self.irq.eq(mailbox_irq_pending)

                ).Else(
                    # ========================== 3-BUFFER MODE ==========================
                    If(direction_write,
                        # RxPDO: сеть пишет, процессор читает (без изменений)
                        If(self.ecat_write_en & ecat_inside_buffer & (self.ecat_last_data_byte | ecat_window_last),
                            sub_buffer_ecat.eq(sub_buffer_shadow),
                            sub_buffer_shadow.eq(sub_buffer_ecat),
                            shadow_has_new_data.eq(1),
                            self.status[0].eq(1),
                            self.status[2:4].eq(sub_buffer_ecat)
                        ),

                        If(self.proc_read_en & proc_access_first_byte,
                            If(shadow_has_new_data,
                                sub_buffer_proc.eq(sub_buffer_shadow),
                                sub_buffer_shadow.eq(sub_buffer_proc),
                                shadow_has_new_data.eq(0),
                                self.status[0].eq(0)
                            )
                        )
                    ).Else(
                        # TxPDO: процессор пишет, сеть читает
                        # ИСПРАВЛЕНО: ротация только по завершении записи
                        If(self.proc_write_en & proc_inside_buffer &
                           self.proc_data_valid & self.proc_last_data_byte,
                            sub_buffer_proc.eq(sub_buffer_shadow),
                            sub_buffer_shadow.eq(sub_buffer_proc),
                            shadow_has_new_data.eq(1),
                            self.status[1].eq(1)
                        ),

                        If(self.ecat_read_en & ecat_access_first_byte,
                            If(shadow_has_new_data,
                                sub_buffer_ecat.eq(sub_buffer_shadow),
                                sub_buffer_shadow.eq(sub_buffer_ecat),
                                shadow_has_new_data.eq(0),
                                self.status[1].eq(0)
                            )
                        )
                    ),

                    self.irq.eq(shadow_has_new_data & self.pdi_control[0])
                )
            ).Else(
                mailbox_buffer_locked.eq(0),
                mailbox_irq_pending.eq(0),
                sub_buffer_ecat.eq(0),
                sub_buffer_proc.eq(1),
                sub_buffer_shadow.eq(2),
                shadow_has_new_data.eq(0),
                self.status.eq(0),
                self.irq.eq(0)
            )
        ]

        # ---------------------------------------------------------------------
        # Трансляция адресов к BRAM (комбинаторная с предсказанием ротации)
        # ---------------------------------------------------------------------
        ecat_offset = Signal(16)
        proc_offset = Signal(16)

        # Предсказание ротации для нулевой латентности
        proc_will_capture_shadow = Signal()
        ecat_will_capture_shadow = Signal()
        proc_effective_buffer = Signal(2)
        ecat_effective_buffer = Signal(2)

        self.comb += [
            ecat_offset.eq(self.ecat_addr - self.phys_start_addr),
            proc_offset.eq(self.proc_addr - self.phys_start_addr),

            # Комбинаторное предсказание: если процессор читает первый байт
            # и есть новые данные, направляем его в shadow НЕМЕДЛЕННО
            proc_will_capture_shadow.eq(
                self.proc_read_en & proc_access_first_byte &
                shadow_has_new_data & ~is_mailbox_mode & direction_write
            ),

            ecat_will_capture_shadow.eq(
                self.ecat_read_en & ecat_access_first_byte &
                shadow_has_new_data & ~is_mailbox_mode & ~direction_write
            ),

            proc_effective_buffer.eq(
                Mux(proc_will_capture_shadow, sub_buffer_shadow, sub_buffer_proc)
            ),

            ecat_effective_buffer.eq(
                Mux(ecat_will_capture_shadow, sub_buffer_shadow, sub_buffer_ecat)
            ),
        ]

        # Маршрутизация сетевой стороны
        self.comb += [
            If(is_mailbox_mode,
                self.ecat_bram_addr.eq(self.ecat_addr)
            ).Else(
                If(ecat_effective_buffer == 0,
                    self.ecat_bram_addr.eq(self.phys_start_addr + ecat_offset)
                ).Elif(ecat_effective_buffer == 1,
                    self.ecat_bram_addr.eq(SHADOW_BUF1_BASE + ecat_offset)
                ).Else(
                    self.ecat_bram_addr.eq(SHADOW_BUF2_BASE + ecat_offset)
                )
            )
        ]

        # Маршрутизация процессорной стороны
        self.comb += [
            If(is_mailbox_mode,
                self.proc_bram_addr.eq(self.proc_addr)
            ).Else(
                If(proc_effective_buffer == 0,
                    self.proc_bram_addr.eq(self.phys_start_addr + proc_offset)
                ).Elif(proc_effective_buffer == 1,
                    self.proc_bram_addr.eq(SHADOW_BUF1_BASE + proc_offset)
                ).Else(
                    self.proc_bram_addr.eq(SHADOW_BUF2_BASE + proc_offset)
                )
            )
        ]


class EtherCATSyncManagerManager(Module):
    """Менеджер SyncManager."""
    def __init__(self, start_addr=0x0800, size=0x0008, count=4):
        if count < 4:
            raise ValueError(f"Количество SyncManager не может быть < 4")
        if start_addr + size * count > 0x1000:
            raise ValueError(f"Регистры выходят за 0x0FFF")

        self.start_addr = start_addr
        self.size = size
        self.count = count

        self.ecat_addr           = Signal(13)
        self.ecat_data_phase     = Signal()
        self.ecat_write_en       = Signal()
        self.ecat_read_en        = Signal()
        self.ecat_last_data_byte = Signal()

        self.proc_addr       = Signal(13)
        self.proc_read_en    = Signal()
        self.proc_write_en   = Signal()
        self.proc_data_valid = Signal()
        self.proc_last_data_byte = Signal()

        # Шина со стороны сети (EtherCAT мастер)
        self.ecat_reg_addr       = Signal(16)
        self.ecat_reg_write_data = Signal(8)
        self.ecat_reg_write_en   = Signal()
        self.ecat_reg_read_data  = Signal(8)

        # Шина со стороны процессора (PDI)
        self.pdi_reg_addr        = Signal(16)
        self.pdi_reg_write_data  = Signal(8)
        self.pdi_reg_write_en    = Signal()
        self.pdi_reg_read_data   = Signal(8)

        # Агрегированные выходы
        self.reg_access_violation = Signal()
        self.irq_out              = Signal()


        self.channels = []
        irq_signals = []

        read_data_ecat = []
        read_data_pdi  = []
        violations     = []

        for idx in range(self.count):
            sm_base_addr = self.start_addr + (idx * self.size)
            ch = EtherCATSyncManagerChannel(base_register_address=sm_base_addr)
            self.channels.append(ch)
            self.submodules += ch

            self.comb += [
                ch.ecat_addr.eq(self.ecat_addr),
                ch.ecat_data_phase.eq(self.ecat_data_phase),
                ch.ecat_write_en.eq(self.ecat_write_en),
                ch.ecat_read_en.eq(self.ecat_read_en),
                ch.ecat_last_data_byte.eq(self.ecat_last_data_byte),

                ch.proc_addr.eq(self.proc_addr),
                ch.proc_read_en.eq(self.proc_read_en),
                ch.proc_write_en.eq(self.proc_write_en),
                ch.proc_data_valid.eq(self.proc_data_valid),
                ch.proc_last_data_byte.eq(self.proc_last_data_byte),

                ch.ecat_reg_addr.eq(self.ecat_reg_addr),
                ch.ecat_reg_write_data.eq(self.ecat_reg_write_data),
                ch.ecat_reg_write_en.eq(self.ecat_reg_write_en),

                ch.pdi_reg_addr.eq(self.pdi_reg_addr),
                ch.pdi_reg_write_data.eq(self.pdi_reg_write_data),
                ch.pdi_reg_write_en.eq(self.pdi_reg_write_en),
            ]

            read_data_ecat.append(ch.ecat_reg_read_data)
            read_data_pdi.append(ch.pdi_reg_read_data)
            violations.append(ch.reg_access_violation)
            irq_signals.append(ch.irq)

            # Агрегация (каждый канал вне окна отдаёт 0)
            self.comb += [
                self.ecat_reg_read_data.eq(reduce(or_, read_data_ecat)),
                self.pdi_reg_read_data.eq(reduce(or_, read_data_pdi)),
                self.reg_access_violation.eq(reduce(or_, violations)),
                self.irq_out.eq(reduce(or_, irq_signals)),
            ]

        self.any_ecat_sel = Signal()
        self.final_ecat_bram_addr = Signal(ESC_MEM_BITS)
        self.any_proc_sel = Signal()
        self.final_proc_bram_addr = Signal(ESC_MEM_BITS)

        arb = If(self.channels[0].ecat_sel,
            self.any_ecat_sel.eq(1),
            self.final_ecat_bram_addr.eq(self.channels[0].ecat_bram_addr))
        for ch in self.channels[1:]:
            arb = arb.Elif(ch.ecat_sel,
                self.any_ecat_sel.eq(1),
                self.final_ecat_bram_addr.eq(ch.ecat_bram_addr))
        arb2 = If(self.channels[0].proc_sel,
            self.any_proc_sel.eq(1),
            self.final_proc_bram_addr.eq(self.channels[0].proc_bram_addr))
        for ch in self.channels[1:]:
            arb2 = arb2.Elif(ch.proc_sel,
                self.any_proc_sel.eq(1),
                self.final_proc_bram_addr.eq(ch.proc_bram_addr))

        self.comb += arb
        self.comb += arb2


