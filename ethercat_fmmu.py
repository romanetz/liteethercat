#!/usr/bin/env python3
"""
FMMU (Fieldbus Memory Management Unit) для EtherCAT ESC.

Менеджер FMMU сопоставляет логические адреса на шине EtherCAT и физические
адреса в пространстве DPRAM.

Карта регистров канала (16 байт, ETG.1000.4 Table 60):
  0x00-0x03 (4 байта): Logical Start Address  (например 0x11000000)
  0x04-0x05 (2 байта): Length                 (длина окна в байтах)
  0x06      (1 байт):  Logical Start Bit      (обычно 0)
  0x07      (1 байт):  Logical End Bit        (обычно 7)
  0x08-0x09 (2 байта): Physical Start Address (внутри DPRAM 0x1000-0x1FFF)
  0x0A      (1 байт):  Physical Start Bit
  0x0B      (1 байт):  Type (бит0 = read, бит1 = write)
  0x0C      (1 байт):  Activate (бит0 = канал активен)
  0x0D-0x0F (3 байта): Reserved (в железе читаются как 0)

ОГРАНИЧЕНИЕ РЕАЛИЗАЦИИ: поддерживается только байтовое отображение
(start_bit == 0, end_bit == 7). Битовое отображение отдельных бит внутри
байта не реализовано; при попытке такой конфигурации канал взводит
bit_mapping_unsupported и не даёт hit.

ИНТЕРФЕЙС ДЛЯ FSM ПАРСЕРА:
  would_hit  - адрес попадает в окно, БЕЗ учёта фазы. Опрашивается в фазе
               заголовка датаграммы для выбора маршрута байта.
  hit        - рабочий строб доступа, только в фазе данных.
  access_ok  - доступ успешен (права есть, адрес внутри DPRAM). Строб на
               такт; FSM накапливает его в регистре за всю фазу данных и
               инкрементирует WKC один раз на датаграмму.
"""

from functools import reduce
from operator import add, or_

from migen import *

# =============================================================================
# Константы адресного пространства ESC
# =============================================================================
ESC_DPRAM_START = 0x1000
ESC_DPRAM_END   = 0x1FFF
ESC_MEM_BITS    = 13          # 0x0000-0x1FFF


class EtherCATFMMUManager(Module):
    """
    Параметризуемый менеджер FMMU для EtherCAT ESC.

    Генерирует массив каналов и выполняет их приоритетный арбитраж.
    Все выходы трансляции КОМБИНАТОРНЫЕ (0 тактов задержки) — это требование
    on-the-fly парсера: адрес должен быть готов в том же такте, в котором байт
    данных пришёл из PHY.
    """
    def __init__(self, start_addr=0x0600, size=0x0010, count=4):
        # ---------------------------------------------------------------------
        # Верификация параметров (защита на этапе генерации)
        # ---------------------------------------------------------------------
        if count < 2:
            raise ValueError(
                f"Ошибка конфигурации ESC: количество FMMU (EC_FMMU_COUNT = {count}) "
                f"не может быть меньше 2 по спецификации EtherCAT!"
            )
        if size < 0x10:
            raise ValueError(
                f"Ошибка конфигурации ESC: размер записи FMMU должен быть >= 16 байт "
                f"(получено {size})!"
            )
        if start_addr % 0x10:
            raise ValueError(
                f"Ошибка конфигурации ESC: базовый адрес FMMU {hex(start_addr)} "
                f"должен быть выровнен по 16 байт!"
            )

        max_address = start_addr + (size * count)
        if max_address > 0x1000:
            raise ValueError(
                f"Ошибка конфигурации ESC: регистры FMMU выходят за границы "
                f"адресного пространства 0x0FFF (конечный адрес: {hex(max_address - 1)})!"
            )

        self.start_addr = start_addr
        self.size       = size
        self.count      = count

        # ---------------------------------------------------------------------
        # Интерфейс трансляции (со стороны парсера датаграмм)
        # ---------------------------------------------------------------------
        self.logical_addr_in = Signal(32)
        self.data_phase      = Signal()
        self.cmd_read        = Signal()
        self.cmd_write       = Signal()

        # Выходы к памяти — комбинаторные
        self.any_hit          = Signal()
        self.any_would_hit    = Signal()   # ранний детект маршрута для FSM
        self.final_phys_addr  = Signal(ESC_MEM_BITS)
        self.any_perm_error   = Signal()
        self.access_ok        = Signal()   # признак для инкремента WKC

        # Диагностика конфигурации
        self.multi_hit_error  = Signal()   # >1 канала перекрываются: мастер ошибся
        self.range_error      = Signal()   # трансляция вышла за пределы DPRAM
        self.bit_mapping_unsupported = Signal()  # активен канал с битовым окном
        self.hit_channel      = Signal(max=max(count, 2))  # индекс сработавшего канала

        # ---------------------------------------------------------------------
        # Конфигурационная шина (запись от мастера / локальной шины)
        # ---------------------------------------------------------------------
        self.reg_write_addr = Signal(16)
        self.reg_write_data = Signal(8)
        self.reg_write_en   = Signal()

        # Чтение конфигурации (мастер читает FMMU обратно через FPRD)
        self.reg_read_addr  = Signal(16)
        self.reg_read_data  = Signal(8)

        # ---------------------------------------------------------------------
        # Генерация каналов
        # ---------------------------------------------------------------------
        self.channels = []

        for idx in range(self.count):
            channel_base_addr = self.start_addr + (idx * self.size)
            ch = EtherCATFMMUChannel(base_register_address=channel_base_addr)
            self.channels.append(ch)
            self.submodules += ch

            self.comb += [
                ch.logical_addr_in.eq(self.logical_addr_in),
                ch.data_phase.eq(self.data_phase),
                ch.cmd_read.eq(self.cmd_read),
                ch.cmd_write.eq(self.cmd_write),
                ch.reg_write_addr.eq(self.reg_write_addr),
                ch.reg_write_data.eq(self.reg_write_data),
                ch.reg_write_en.eq(self.reg_write_en),
                ch.reg_read_addr.eq(self.reg_read_addr),
            ]

        # ---------------------------------------------------------------------
        # Приоритетный арбитраж (канал 0 — высший приоритет)
        # ---------------------------------------------------------------------
        # Важно: цепочка If/Elif, а не набор независимых If. Набор независимых
        # If в одном comb-блоке даёт приоритет ПОСЛЕДНЕМУ каналу, что делает
        # поведение при перекрытии окон неочевидным.
        #
        # access_ok берётся у ПОБЕДИТЕЛЯ арбитража, а не ИЛИ по всем каналам:
        # доступ физически обслуживает один канал, и WKC должен отражать
        # результат именно его проверок.
        arbiter = If(self.channels[0].hit,
            self.any_hit.eq(1),
            self.final_phys_addr.eq(self.channels[0].physical_addr_out),
            self.any_perm_error.eq(self.channels[0].perm_error),
            self.range_error.eq(self.channels[0].range_error),
            self.access_ok.eq(self.channels[0].access_ok),
            self.hit_channel.eq(0),
        )
        for idx, ch in enumerate(self.channels[1:], start=1):
            arbiter = arbiter.Elif(ch.hit,
                self.any_hit.eq(1),
                self.final_phys_addr.eq(ch.physical_addr_out),
                self.any_perm_error.eq(ch.perm_error),
                self.range_error.eq(ch.range_error),
                self.access_ok.eq(ch.access_ok),
                self.hit_channel.eq(idx),
            )
        self.comb += arbiter

        # ---------------------------------------------------------------------
        # Агрегация признаков
        # ---------------------------------------------------------------------
        # Детект перекрытия окон: мастер настроил два FMMU на один логический
        # адрес. Трансляция при этом НЕ блокируется — приоритетный канал
        # обслуживает доступ, остальные проигрывают арбитраж. Мастер обязан не
        # допускать перекрытия; флаг нужен для диагностики через AL Status Code.
        hit_count = Signal(max=self.count + 1)
        self.comb += [
            hit_count.eq(reduce(add, [ch.hit for ch in self.channels])),
            self.multi_hit_error.eq(hit_count > 1),

            # would_hit агрегируется простым ИЛИ: FSM важен сам факт попадания
            self.any_would_hit.eq(
                reduce(or_, [ch.would_hit for ch in self.channels])
            ),

            # Конъюнкция с activate обязательна: после сброса все регистры в
            # нуле, а условие поддержки требует end_bit == 7, поэтому неактивные
            # каналы иначе держали бы флаг постоянно взведённым.
            self.bit_mapping_unsupported.eq(
                reduce(or_, [ch.bit_mapping_unsupported & ch.activate
                             for ch in self.channels])
            ),
        ]

        # ---------------------------------------------------------------------
        # Мультиплексор чтения конфигурации
        # ---------------------------------------------------------------------
        self.comb += self.reg_read_data.eq(
            reduce(or_, [ch.reg_read_data for ch in self.channels])
        )


class EtherCATFMMUChannel(Module):
    """
    Один канал FMMU. Занимает 16 байт в конфигурационном пространстве.

    Трансляция адреса — полностью комбинаторная. Синхронными остаются только
    конфигурационные регистры.
    """
    def __init__(self, base_register_address):
        if base_register_address % 0x10:
            raise ValueError(
                f"Базовый адрес канала FMMU {hex(base_register_address)} "
                f"должен быть выровнен по 16 байт!"
            )
        self.base_addr = base_register_address

        # ---------------------------------------------------------------------
        # Интерфейс трансляции
        # ---------------------------------------------------------------------
        self.logical_addr_in   = Signal(32)  # Логический адрес из датаграммы
        self.data_phase        = Signal()    # FSM парсера в фазе данных
        self.cmd_read          = Signal()    # LRD или LRW
        self.cmd_write         = Signal()    # LWR или LRW

        self.hit               = Signal()             # Доступ в фазе данных
        self.would_hit         = Signal()             # Попадание без учёта фазы
        self.access_ok         = Signal()             # Доступ успешен -> WKC
        self.physical_addr_out = Signal(ESC_MEM_BITS) # Физический адрес DPRAM
        self.perm_error        = Signal()             # Нарушение прав доступа
        self.range_error       = Signal()             # Выход за границу DPRAM
        self.bit_mapping_unsupported = Signal()       # Конфиг требует битового окна

        # ---------------------------------------------------------------------
        # Конфигурационные регистры
        # ---------------------------------------------------------------------
        self.logical_start_addr  = Signal(32)
        self.length              = Signal(16)
        self.logical_start_bit   = Signal(3)
        self.logical_end_bit     = Signal(3)
        self.physical_start_addr = Signal(ESC_MEM_BITS)
        self.physical_start_bit  = Signal(3)
        self.operation_type      = Signal(2)   # бит0 = read, бит1 = write
        self.activate            = Signal()

        # Конфигурационная шина
        self.reg_write_addr = Signal(16)
        self.reg_write_data = Signal(8)
        self.reg_write_en   = Signal()
        self.reg_read_addr  = Signal(16)
        self.reg_read_data  = Signal(8)

        # ---------------------------------------------------------------------
        # Запись конфигурации (синхронная)
        # ---------------------------------------------------------------------
        def wr(offset):
            """Строб записи по смещению внутри записи FMMU."""
            return self.reg_write_en & (self.reg_write_addr == (self.base_addr + offset))

        for i in range(4):
            self.sync += If(wr(0x00 + i),
                self.logical_start_addr[i * 8:(i + 1) * 8].eq(self.reg_write_data)
            )
        for i in range(2):
            self.sync += If(wr(0x04 + i),
                self.length[i * 8:(i + 1) * 8].eq(self.reg_write_data)
            )

        # physical_start_addr — 13 бит: младший байт полностью, старший только 5 бит
        self.sync += [
            If(wr(0x08), self.physical_start_addr[0:8].eq(self.reg_write_data)),
            If(wr(0x09), self.physical_start_addr[8:13].eq(self.reg_write_data[0:5])),
            If(wr(0x06), self.logical_start_bit.eq(self.reg_write_data[0:3])),
            If(wr(0x07), self.logical_end_bit.eq(self.reg_write_data[0:3])),
            If(wr(0x0A), self.physical_start_bit.eq(self.reg_write_data[0:3])),
            If(wr(0x0B), self.operation_type.eq(self.reg_write_data[0:2])),
            If(wr(0x0C), self.activate.eq(self.reg_write_data[0])),
        ]

        # ---------------------------------------------------------------------
        # Чтение конфигурации (комбинаторное)
        # ---------------------------------------------------------------------
        # base_addr выровнен по 16, поэтому селектор — младшие 4 бита,
        # а попадание в окно проверяем по старшим битам без вычитателя.
        addr_in_window = Signal()
        self.comb += addr_in_window.eq(
            self.reg_read_addr[4:16] == (self.base_addr >> 4)
        )
        self.comb += If(addr_in_window,
            Case(self.reg_read_addr[0:4], {
                0x0: self.reg_read_data.eq(self.logical_start_addr[0:8]),
                0x1: self.reg_read_data.eq(self.logical_start_addr[8:16]),
                0x2: self.reg_read_data.eq(self.logical_start_addr[16:24]),
                0x3: self.reg_read_data.eq(self.logical_start_addr[24:32]),
                0x4: self.reg_read_data.eq(self.length[0:8]),
                0x5: self.reg_read_data.eq(self.length[8:16]),
                0x6: self.reg_read_data.eq(self.logical_start_bit),
                0x7: self.reg_read_data.eq(self.logical_end_bit),
                0x8: self.reg_read_data.eq(self.physical_start_addr[0:8]),
                0x9: self.reg_read_data.eq(self.physical_start_addr[8:13]),
                0xA: self.reg_read_data.eq(self.physical_start_bit),
                0xB: self.reg_read_data.eq(self.operation_type),
                0xC: self.reg_read_data.eq(self.activate),
                "default": self.reg_read_data.eq(0),   # 0x0D-0x0F reserved
            })
        )

        # ---------------------------------------------------------------------
        # Трансляция адреса — КОМБИНАТОРНАЯ
        # ---------------------------------------------------------------------
        window_end   = Signal(33)   # 33 бита: start(32) + length(16) без переполнения
        is_in_range  = Signal()
        offset       = Signal(16)
        phys_full    = Signal(ESC_MEM_BITS + 4)  # запас на переполнение суммы
        config_valid = Signal()

        # Байтовое отображение: старт бит 0, конец бит 7. Иначе конфиг не поддержан.
        self.comb += self.bit_mapping_unsupported.eq(
            (self.logical_start_bit != 0) | (self.logical_end_bit != 7)
        )

        self.comb += [
            window_end.eq(self.logical_start_addr + self.length),

            # length == 0 означает неиспользуемый канал (ETG: окно нулевой длины
            # недопустимо), поэтому попадания быть не должно.
            is_in_range.eq(
                (self.length != 0) &
                (self.logical_addr_in >= self.logical_start_addr) &
                (self.logical_addr_in < window_end)
            ),

            offset.eq(self.logical_addr_in - self.logical_start_addr),
            phys_full.eq(self.physical_start_addr + offset),

            config_valid.eq(self.activate & ~self.bit_mapping_unsupported),

            # would_hit не зависит от фазы: FSM опрашивает его в фазе заголовка,
            # чтобы заранее выбрать маршрут байта (DPRAM или регистры ESC).
            self.would_hit.eq(config_valid & is_in_range),

            # hit — рабочий строб доступа: фаза данных + реальная операция
            self.hit.eq(self.would_hit & self.data_phase & (self.cmd_read | self.cmd_write)),

            self.physical_addr_out.eq(phys_full[0:ESC_MEM_BITS]),

            # Трансляция ушла за пределы DPRAM — молчаливое усечение
            # превратило бы это в запись по чужому адресу.
            self.range_error.eq(
                self.hit &
                ((phys_full > ESC_DPRAM_END) | (phys_full < ESC_DPRAM_START))
            ),
        ]

        # Права доступа. Для LRW cmd_read и cmd_write активны ОДНОВРЕМЕННО,
        # поэтому проверки объединены по ИЛИ, а не двумя независимыми If.
        read_violation  = Signal()
        write_violation = Signal()
        self.comb += [
            read_violation.eq(self.cmd_read & ~self.operation_type[0]),
            write_violation.eq(self.cmd_write & ~self.operation_type[1]),
            self.perm_error.eq(self.hit & (read_violation | write_violation)),

            # Успешный доступ: попали в окно, права есть, адрес внутри DPRAM.
            # ВАЖНО: это строб на такт доступа. WKC инкрементируется один раз
            # на датаграмму, поэтому FSM должна накапливать признак в регистре
            # за всю фазу данных, а не складывать access_ok по байтам.
            self.access_ok.eq(self.hit & ~self.perm_error & ~self.range_error),
        ]

