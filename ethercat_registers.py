#!/usr/bin/env python3
"""
ESC Register File — основные управляющие регистры EtherCAT Slave Controller.

Покрывает адресное пространство 0x0000-0x0FFF, кроме:
  - 0x0600-0x067F (FMMU) — обрабатывается EtherCATFMMUManager
  - 0x0800-0x081F (SM)   — обрабатывается EtherCATSyncManagerManager
  - 0x1000+       (DPRAM) — BRAM

Реализованные регистры (ETG.1000.4 Section 6):
  0x0000-0x0001  Type (read-only, идентификация чипа)
  0x0002         Revision (read-only)
  0x0003         Build (read-only)
  0x0010-0x0011  Configured Station Address (Station Alias в 0x0012-0x0013 пока не реализован)
  0x0120-0x0121  AL Control (мастер пишет команду смены состояния)
  0x0130-0x0131  AL Status (слейв отвечает текущим состоянием)
  0x0134-0x0135  AL Status Code (код ошибки при отказе перехода)
  0x0200-0x0203  IRQ Mask (пока read-only 0)
  0x0300-0x030F  RX Error Counters (Port 0-3, пока read-only 0)
  0x0910-0x0917  DC System Time (Distributed Clocks, пока read-only 0)
  0x0981         DC Activation (SYNC0/1, пока read-only 0)

Не реализовано (будущее):
  - DL Status/Control (0x0110+)
  - SII EEPROM Interface (0x0500+)
  - Полная машина состояний AL (пока держим Init)
  - DC Time Offset/Delay (0x0920+)
"""

from migen import *

from ethercat_al import ALStateMachine





class ESCRegisterFile(Module):
    """Core ESC registers."""

    # Константы идентификации (произвольные для FPGA-реализации)
    ESC_TYPE     = 0x04 # Custom FPGA ESC
    ESC_REVISION = 0x01
    ESC_BUILD    = 0xDEAD

    # AL State codes
    AL_STATE_INIT    = 0x0001
    AL_STATE_PREOP   = 0x0002
    AL_STATE_SAFEOP  = 0x0004
    AL_STATE_OP      = 0x0008

    RAM_SIZE_KB = 4
    PORT_DESC = 0b00001111
    ESC_FEATURES = 0x0000

    def __init__(self,fmmu_start=0x0600, fmmu_count=4, sm_start=0x0800, sm_count=4):
        # ---------------------------------------------------------------------
        # Интерфейс к ESCMemory
        # ---------------------------------------------------------------------
        self.address_read  = Signal(13)
        self.data_read     = Signal(8)
        self.hit           = Signal()  # адрес попадает в обрабатываемое окно
        self.address_write = Signal(13)
        self.data_write    = Signal(8)
        self.write_en      = Signal()

        # ---------------------------------------------------------------------
        # Регистры (синхронные)
        # ---------------------------------------------------------------------
        # 0x0010-0x0011: Configured Station Address
        self.station_address = Signal(16, reset=0x0000)
        self.reg_write_prot = Signal(8, reset=0x00)
        self.rw_offset = Signal(16, reset=0x0000)
        self.al_event_request = Signal(8, reset=0x00)
        self.lost_link_cnt = Array([Signal(8) for _ in range(4)])

        self.station_alias = Signal(16, reset=0x0000)
        self.station_alias_out = Signal(16)
        self.alias_enable_out  = Signal()
        # 0x0100-0x0103: DL Control/Status
        self.dl_control = Signal(32, reset=0x00000000)

        self.link0_up    = Signal(reset=0)   # reset=0: линк доказывается, а не предполагается
        self.link1_up    = Signal(reset=0)
        self.loop0_closed = Signal()
        self.loop1_closed = Signal()

        self.sm0_activated = Signal()
        self.sm1_activated = Signal()

        self.dl_status = Signal(16)

        # 0x0120-0x0121: AL Control (запрос от мастера)
        self.al_control = Signal(16, reset=self.AL_STATE_INIT)

        # 0x0130-0x0131: AL Status (текущее состояние)
        self.al_status = Signal(16, reset=self.AL_STATE_INIT)

        # 0x0134-0x0135: AL Status Code (код ошибки)
        self.al_status_code = Signal(16, reset=0x0000)

        self.submodules.al_fsm = al_fsm = ALStateMachine()


        self.al_control_write_count = Signal(32, reset = 0x00000000)
        # AL Control регистр — уже есть, но теперь его запись должна триггерить FSM
        al_control_write_d  = Signal()
        al_control_write_re = Signal()

        self.sync += al_control_write_d.eq(self.al_control_write_count[0])
        self.comb += al_control_write_re.eq(
            self.al_control_write_count[0] & ~al_control_write_d
        )

        self.comb += [
            al_fsm.al_control_write.eq(al_control_write_re),
            al_fsm.al_control_value.eq(self.al_control),
            # SM activation — протяни из ESCMemory/ESC SM manager
            al_fsm.sm0_activated.eq(self.sm0_activated),
            al_fsm.sm1_activated.eq(self.sm1_activated),
        ]

        # Замени хардкод:
        #   self.sync += [ self.al_status.eq(self.AL_STATE_INIT) ]
        # на:
        self.sync += [
            self.al_status.eq(al_fsm.al_status),
            self.al_status_code.eq(al_fsm.al_status_code),
        ]

        # ---------------------------------------------------------------------
        # Экспорт к PU
        # ---------------------------------------------------------------------
        self.fp_address_out = Signal(16)
        #self.comb += self.fp_address_out.eq(self.station_address)
        #self.dl_reset_position = Signal()  # импульс сброса position counter
        self.comb += [
            self.dl_status[0].eq(1),  # EEPROM loaded (хардкод пока)
            self.dl_status[4].eq(self.link0_up),
            self.dl_status[5].eq(self.link1_up),
            self.dl_status[8].eq(self.loop0_closed),
            self.dl_status[9].eq(self.link0_up),
            self.dl_status[10].eq(self.loop1_closed),
            self.dl_status[11].eq(self.link1_up),
            self.dl_status[12].eq(1),
            self.dl_status[14].eq(1)
        ]

        # ---------------------------------------------------------------------
        # Диагностика
        # ---------------------------------------------------------------------

        self.station_addr_write_count = Signal(32)

        # ---------------------------------------------------------------------
        # Запись (синхронная)
        # ---------------------------------------------------------------------
        self.sync += [
            If(self.write_en,
                # 0x0010-0x0011: Station Address (little-endian)
                If(self.address_write == 0x0010,
                    self.station_address[0:8].eq(self.data_write),
                    self.station_addr_write_count.eq(self.station_addr_write_count + 1),
                ),
                If(self.address_write == 0x0011,
                    self.station_address[8:16].eq(self.data_write),
                ),

                If(self.address_write == 0x0100,
                    self.dl_control[0:8].eq(self.data_write),
                ),
                If(self.address_write == 0x0101,
                    self.dl_control[8:16].eq(self.data_write),
                ),
                If(self.address_write == 0x0102,
                    self.dl_control[16:24].eq(self.data_write),
                ),
                If(self.address_write == 0x0103,
                    self.dl_control[24:32].eq(self.data_write),
                ),

                # 0x0120-0x0121: AL Control
                If(self.address_write == 0x0120,
                    self.al_control[0:8].eq(self.data_write),
                    self.al_control_write_count.eq(self.al_control_write_count + 1),
                ),
                If(self.address_write == 0x0121,
                    self.al_control[8:16].eq(self.data_write),
                ),

                # 0x0134-0x0135: AL Status Code (мастер может очистить)
                If(self.address_write == 0x0134,
                    self.al_status_code[0:8].eq(self.data_write),
                ),
                If(self.address_write == 0x0135,
                    self.al_status_code[8:16].eq(self.data_write),
                ),
            )
        ]

        # ---------------------------------------------------------------------
        # Простейшая AL State Machine: пока держим Init, игнорируем команды
        # ---------------------------------------------------------------------
        # В будущем здесь будет полноценная FSM Init→PreOP→SafeOP→OP с проверкой
        # конфигурации FMMU/SM и установкой al_status_code при ошибках.
        # Пока: всегда Init, чтобы не блокировать тестирование датаграмм.
        #self.sync += [
        #    self.al_status.eq(self.AL_STATE_INIT),
        #]

        self.comb += [
            self.station_alias_out.eq(self.station_alias),
            self.alias_enable_out.eq(self.dl_control[24]),
        ]

        # ---------------------------------------------------------------------
        # Чтение (комбинаторное)
        # ---------------------------------------------------------------------
        # Определяем, попадает ли адрес в обрабатываемые окна
        read_data_comb = Signal(8)

        #self.comb += [
        #    self.hit.eq(
        #        # 0x0000-0x0003: Type/Revision/Build
        #        (self.address_read <= 0x000a) |
        #        # 0x0010-0x0013: Station Address (+ Alias, пока не реализован)
        #        ((self.address_read >= 0x0010) & (self.address_read <= 0x0013)) |
        #        # 0x0100-0x0103: DL Control (ДОБАВЛЕНО)
        #        ((self.address_read >= 0x0100) & (self.address_read <= 0x0103)) |
        #        # 0x0120-0x0135: AL Control/Status/Code
        #        ((self.address_read >= 0x0120) & (self.address_read <= 0x0135)) |
        #        # 0x0200-0x0203: IRQ Mask
        #        ((self.address_read >= 0x0200) & (self.address_read <= 0x0203)) |
        #        # 0x0300-0x030F: RX Error Counters (4 порта × 4 счётчика)
        #        ((self.address_read >= 0x0300) & (self.address_read <= 0x030F)) |
        #        # 0x0910-0x0917: DC System Time
        #        ((self.address_read >= 0x0910) & (self.address_read <= 0x0917)) |
        #        # 0x0981: DC Activation
        #        (self.address_read == 0x0981)
        #    ),
        #]

        # Мультиплексор чтения (Case быстрее цепочки If для комбинаторики)
        self.comb += [
                # ВАЖНО: hit.eq(1) должен идти до Case — иначе "default" его не перебьёт.
                self.hit.eq(1),
            #If(self.hit,
                Case(self.address_read, {
                    # 0x0000-0x0003: Type/Revision/Build (read-only)
                    0x0000: read_data_comb.eq(self.ESC_TYPE),
                    0x0001: read_data_comb.eq(self.ESC_REVISION),
                    0x0002: read_data_comb.eq(self.ESC_BUILD & 0xFF),
                    0x0003: read_data_comb.eq(self.ESC_BUILD >> 8),
                    0x0004: read_data_comb.eq(fmmu_count),
                    0x0005: read_data_comb.eq(sm_count),
                    0x0006: read_data_comb.eq(self.RAM_SIZE_KB),
                    0x0007: read_data_comb.eq(self.PORT_DESC),
                    0x0008: read_data_comb.eq(self.ESC_FEATURES & 0xFF),
                    0x0009: read_data_comb.eq(self.ESC_FEATURES >> 8),
                    # 0x0010-0x0011: Station Address
                    0x0010: read_data_comb.eq(self.station_address[0:8]),
                    0x0011: read_data_comb.eq(self.station_address[8:16]),

                    # 0x0012-0x0013: Station Alias
                    0x0012: read_data_comb.eq(self.station_alias[0:8]),
                    0x0013: read_data_comb.eq(self.station_alias[8:16]),

                    0x0020: read_data_comb.eq(self.reg_write_prot),   # enable shadow=0
                    0x0021: read_data_comb.eq(self.reg_write_prot),

                    0x0100: read_data_comb.eq(self.dl_control[0:8]),
                    0x0101: read_data_comb.eq(self.dl_control[8:16]),
                    0x0102: read_data_comb.eq(self.dl_control[16:24]),
                    0x0103: read_data_comb.eq(self.dl_control[24:32]),
                    0x0108: read_data_comb.eq(self.rw_offset[0:8]),
                    0x0109: read_data_comb.eq(self.rw_offset[8:16]),

                    # 0x0110-0x0111: DL status
                    0x0110: read_data_comb.eq(self.dl_status[0:8]),
                    0x0111: read_data_comb.eq(self.dl_status[8:16]),
                    # 0x0120-0x0121: AL Control
                    0x0120: read_data_comb.eq(self.al_control[0:8]),
                    0x0121: read_data_comb.eq(self.al_control[8:16]),

                    # 0x0130-0x0131: AL Status
                    0x0130: read_data_comb.eq(self.al_status[0:8]),
                    0x0131: read_data_comb.eq(self.al_status[8:16]),

                    # 0x0134-0x0135: AL Status Code
                    0x0134: read_data_comb.eq(self.al_status_code[0:8]),
                    0x0135: read_data_comb.eq(self.al_status_code[8:16]),

                    0x0220: read_data_comb.eq(self.al_event_request),
                    0x0310: read_data_comb.eq(self.lost_link_cnt[0]),
                    0x0311: read_data_comb.eq(self.lost_link_cnt[1]),
                    0x0312: read_data_comb.eq(self.lost_link_cnt[2]),
                    0x0313: read_data_comb.eq(self.lost_link_cnt[3]),
                    # 0x0200-0x0203: IRQ Mask (пока read-only 0)
                    # 0x0300-0x030F: RX Error Counters (пока read-only 0)
                    # 0x0910-0x0917: DC System Time (пока read-only 0)
                    # 0x0981: DC Activation (пока read-only 0)
                    "default": [read_data_comb.eq(0x00),self.hit.eq(0)]
                }),
            #).Else(
            #    read_data_comb.eq(0x00),
            #),

            self.data_read.eq(read_data_comb),
        ]


if __name__ == "__main__":
    from migen.fhdl import verilog
    regs = ESCRegisterFile()
    print(verilog.convert(regs, ios={
        regs.address_read, regs.data_read,
        regs.address_write, regs.data_write, regs.write_en,
        regs.fp_address_out, regs.station_address,
        regs.al_control, regs.al_status, regs.al_status_code,
    }))
