#!/usr/bin/env python3
"""
ESCMemory — память ESC с интеграцией FMMU и SyncManager.

Адресное пространство 16KB:
  0x0000-0x0FFF  регистры (FMMU 0x0600+, SM 0x0800+ — перехватываются мимо BRAM)
  0x1000-0x1FFF  DPRAM Buffer 0
  0x2000-0x3FFF  shadow-буферы SM (доступны только через SM-трансляцию)

Латентность чтения BRAM = 1 такт (async_read=False).
Латентность записи BRAM = 2 такта (двухступенчатый конвейер адрес/данные/we).
"""
from migen import *
from ethercat_const import *
from ethercat_fmmu import EtherCATFMMUManager
from ethercat_sm import EtherCATSyncManagerManager
from ethercat_registers import ESCRegisterFile
from ethercat_sii import *
from litex.soc.integration.common import get_mem_data



class ESCMemory(Module):
    def __init__(self, fmmu_start=EC_FMMU_START, fmmu_count=EC_FMMU_COUNT,
                 sm_start=EC_SM_START, sm_count=EC_SM_COUNT, bram_latency_2=True):
        # --- PU: физический доступ ---
        self.address_read  = Signal(13)
        self.data_read     = Signal(8)
        self.address_write = Signal(13)
        self.data_write    = Signal(8)
        self.write_en      = Signal()
        # --- PU: логический доступ ---
        self.logical_address_read  = Signal(32)
        self.logical_data_read     = Signal(8)
        self.logical_address_write = Signal(32)
        self.logical_data_write    = Signal(8)
        self.logical_write_en      = Signal()
        # --- PU: стробы ---
        self.data_phase       = Signal()
        self.phys_read_stb    = Signal()
        self.logical_read_stb = Signal()
        self.last_data_byte   = Signal()

        # --- статусы к PU ---
        self.logical_would_hit = Signal()
        self.logical_early_would_hit = Signal()
        self.logical_access_ok = Signal()
        # --- диагностика наружу ---
        self.sm_irq         = Signal()
        self.fp_address_out = Signal(16)
        self.transfer       = Signal()
        fmmu_end = fmmu_start + EC_FMMU_SIZE * fmmu_count
        sm_end   = sm_start + EC_SM_SIZE * sm_count

        def in_window(addr, base, end):
            return (addr >= base) & (addr < end)

        # ------------------------------------------------------------------
        # Core ESC регистры
        # ------------------------------------------------------------------
        self.submodules.regs = regs = ESCRegisterFile(fmmu_start=0x0600, fmmu_count=EC_FMMU_COUNT, sm_start=0x0800, sm_count=EC_SM_COUNT)
        #eeprom_words = get_mem_data(
        #    "./siitool/examples/colorlight4fmmu.bin",
        #    data_width = 32,          # SII — 16-битные слова
        #    endianness = "little",    # little-endian, как в спецификации
        #)
        eeprom_words = load_sii_bin("./siitool/examples/colorlight4fmmu.bin")
        self.submodules.sii = sii = SIIInterface(eeprom_words=eeprom_words)

        self.station_alias_out = Signal(16)
        self.alias_enable_out  = Signal()
        self.comb += [
            self.station_alias_out.eq(regs.station_alias_out),
            self.alias_enable_out.eq(regs.alias_enable_out),
            self.fp_address_out.eq(regs.fp_address_out),
        ]

        # ------------------------------------------------------------------
        # FMMU + SM
        # ------------------------------------------------------------------
        self.submodules.fmmu = fmmu = EtherCATFMMUManager(
            start_addr=fmmu_start, size=EC_FMMU_SIZE, count=fmmu_count)
        self.submodules.sm = sm = EtherCATSyncManagerManager(
            start_addr=sm_start, size=EC_SM_SIZE, count=sm_count)

        # FMMU: логический адрес
        self.comb += [
            fmmu.logical_addr_in.eq(
                Mux(self.logical_write_en,
                    self.logical_address_write,
                    self.logical_address_read)),
            fmmu.data_phase.eq(self.data_phase),
            fmmu.cmd_read.eq(self.logical_read_stb),
            fmmu.cmd_write.eq(self.logical_write_en),
            fmmu.reg_write_addr.eq(self.address_write),
            fmmu.reg_write_data.eq(self.data_write),
            fmmu.reg_write_en.eq(
                self.write_en & in_window(self.address_write, fmmu_start, fmmu_end)),
            fmmu.reg_read_addr.eq(self.address_read),
            self.logical_would_hit.eq(fmmu.any_would_hit),
            self.logical_early_would_hit.eq(fmmu.any_would_hit),
            self.logical_access_ok.eq(fmmu.any_hit & fmmu.access_ok),

        ]
        # ------------------------------------------------------------------
        # SII
        # ------------------------------------------------------------------

        self.comb += [
            sii.address_read.eq(self.address_read),
            sii.address_write.eq(self.address_write),
            sii.data_write.eq(self.data_write),
            sii.write_en.eq(self.write_en),
            sii.dgram_end.eq(self.last_data_byte),     # ← новое
        ]

        # ------------------------------------------------------------------
        # Объединённый сетевой адрес
        # ------------------------------------------------------------------
        net_write = Signal()
        rd_addr13 = Signal(13)
        wr_addr13 = Signal(13)
        self.comb += [
            net_write.eq(self.write_en |
                         (self.logical_write_en & fmmu.access_ok)),
            rd_addr13.eq(Mux(self.logical_read_stb,
                             fmmu.final_phys_addr, self.address_read)),
            wr_addr13.eq(Mux(self.logical_write_en,
                             fmmu.final_phys_addr, self.address_write)),
        ]

        # SM: сетевая сторона
        self.comb += [
            sm.ecat_addr.eq(Mux(net_write, wr_addr13, rd_addr13)),
            sm.ecat_data_phase.eq(self.data_phase),
            sm.ecat_write_en.eq(net_write),
            sm.ecat_read_en.eq(self.phys_read_stb |
                               (self.logical_read_stb & fmmu.access_ok)),
            sm.ecat_last_data_byte.eq(self.last_data_byte),
            sm.ecat_reg_addr.eq(Mux(self.write_en,
                                    self.address_write, self.address_read)),
            sm.ecat_reg_write_data.eq(self.data_write),
            sm.ecat_reg_write_en.eq(
                self.write_en & in_window(self.address_write, sm_start, sm_end)),
            # PDI-заглушки
            sm.proc_addr.eq(0),
            sm.proc_read_en.eq(0),
            sm.proc_write_en.eq(0),
            sm.proc_data_valid.eq(0),
            sm.proc_last_data_byte.eq(0),
            sm.pdi_reg_addr.eq(0),
            sm.pdi_reg_write_data.eq(0),
            sm.pdi_reg_write_en.eq(0),
            self.sm_irq.eq(sm.irq_out),
            regs.sm0_activated.eq(sm.channels[0].activate),
            regs.sm1_activated.eq(sm.channels[1].activate)
        ]

        # ------------------------------------------------------------------
        # BRAM 16KB: один порт чтения (sync), один порт записи (sync).
        # ------------------------------------------------------------------
        self.mem= mem = Memory(8, 16384)
        self.rd_port = rd_port= mem.get_port()
        self.wr_port = wr_port = mem.get_port(write_capable=True)
        self.specials += mem, rd_port, wr_port

        # ------------------------------------------------------------------
        # Чтение BRAM: адрес — КОМБИНАТОРНО.
        # rd_port сам защёлкивает адрес внутри => латентность = 1 такт.
        # ------------------------------------------------------------------
        bram_read_addr_reg = Signal(14)

        self.sync += [
            If(self.logical_read_stb,
                bram_read_addr_reg.eq(fmmu.final_phys_addr),
            ).Else(
                bram_read_addr_reg.eq(Mux(sm.any_ecat_sel,
                                        sm.final_ecat_bram_addr,
                                        self.address_read)),
            ),
        ]
        self.comb += rd_port.adr.eq(bram_read_addr_reg)


        #self.comb += [
        #    If(self.logical_read_stb,
        #        bram_read_addr.eq(fmmu.final_phys_addr),
        #    ).Else(
        #        bram_read_addr.eq(Mux(sm.any_ecat_sel,
        #                              sm.final_ecat_bram_addr,
        #                              self.address_read)),
        #    ),
        #    rd_port.adr.eq(bram_read_addr),
        #]

        # ------------------------------------------------------------------
        # Запись BRAM: конвейер S1 -> S2 -> S3 (адрес/данные/we синхронно).
        # ------------------------------------------------------------------
        bram_write_addr_s2 = Signal(14)
        bram_write_addr_s3 = Signal(14)
        wr_we_s2  = Signal()
        wr_dat_s2 = Signal(8)
        wr_we_s3  = Signal()
        wr_dat_s3 = Signal(8)

        self.sync += [
            # S1 → S2
            bram_write_addr_s2.eq(
                Mux(sm.any_ecat_sel, sm.final_ecat_bram_addr,
                Mux(self.logical_write_en, fmmu.final_phys_addr, self.address_write))
            ),
            wr_we_s2.eq(net_write & (wr_addr13 >= ESC_DPRAM_START)),
            wr_dat_s2.eq(Mux(self.logical_write_en,
                             self.logical_data_write, self.data_write)),
            # S2 → S3
            bram_write_addr_s3.eq(bram_write_addr_s2),
            wr_we_s3.eq(wr_we_s2),
            wr_dat_s3.eq(wr_dat_s2),
        ]

        self.comb += [
            wr_port.adr.eq(bram_write_addr_s3),
            wr_port.we.eq(wr_we_s3),
            wr_port.dat_w.eq(wr_dat_s3),
        ]

        # ------------------------------------------------------------------
        # Мультиплексор чтения: выравнивание не-BRAM веток под sync-порт BRAM.
        # async_read=True:  всё комбинаторно.
        # async_read=False: не-BRAM ветки задержать на 1 такт.
        # ------------------------------------------------------------------
        # Мультиплексор чтения: выравнивание не-BRAM веток.
        # bram_latency_2=True:  регистры + FMMU + SM задержать на 2 такта
        # bram_latency_2=False: комбинаторная выдача (для unit-тестов)
        # ------------------------------------------------------------------
        if not bram_latency_2:
            # BRAM латентность 1 в sim-тесте → берём данные сразу
            regs_hit_q  = regs.hit
            regs_data_q = regs.data_read
            fmmu_sel_q  = in_window(self.address_read, fmmu_start, fmmu_end)
            fmmu_data_q = fmmu.reg_read_data
            sm_sel_q    = in_window(self.address_read, sm_start, sm_end)
            sm_data_q   = sm.ecat_reg_read_data
            bram_sel_q  = Signal(reset=1)
            sii_hit_q  = sii.hit
            sii_data_q = sii.data_read
        else:
            # BRAM: bram_read_addr_reg (1) + rd_port sync (1) = 2 такта
            # regs/fmmu/sm комбинаторны от address_read => нужны ДВЕ ступени
            regs_hit_d1  = Signal();     regs_hit_q  = Signal()
            regs_data_d1 = Signal(8);    regs_data_q = Signal(8)
            fmmu_sel_d1  = Signal();     fmmu_sel_q  = Signal()
            fmmu_data_d1 = Signal(8);    fmmu_data_q = Signal(8)
            sm_sel_d1    = Signal();     sm_sel_q    = Signal()
            sm_data_d1   = Signal(8);    sm_data_q   = Signal(8)
            sii_hit_d1  = Signal();  sii_hit_q  = Signal()
            sii_data_d1 = Signal(8); sii_data_q = Signal(8)

            self.sync += [
                If(self.transfer,
                    # ступень 1
                    regs_hit_d1.eq(regs.hit),
                    regs_data_d1.eq(regs.data_read),
                    fmmu_sel_d1.eq(in_window(self.address_read, fmmu_start, fmmu_end)),
                    fmmu_data_d1.eq(fmmu.reg_read_data),
                    sm_sel_d1.eq(in_window(self.address_read, sm_start, sm_end)),
                    sm_data_d1.eq(sm.ecat_reg_read_data),
                    sii_hit_d1.eq(sii.hit),
                    sii_data_d1.eq(sii.data_read),
                    # ступень 2
                    regs_hit_q.eq(regs_hit_d1),
                    regs_data_q.eq(regs_data_d1),
                    fmmu_sel_q.eq(fmmu_sel_d1),
                    fmmu_data_q.eq(fmmu_data_d1),
                    sm_sel_q.eq(sm_sel_d1),
                    sm_data_q.eq(sm_data_d1),
                    sii_hit_q.eq(sii_hit_d1),
                    sii_data_q.eq(sii_data_d1),
                )
            ]


        self.comb += [
            self.data_read.eq(
                Mux(sii_hit_q,   sii_data_q,
                Mux(regs_hit_q,  regs_data_q,
                Mux(fmmu_sel_q,  fmmu_data_q,
                Mux(sm_sel_q,    sm_data_q,
                    rd_port.dat_r))))),
            self.logical_data_read.eq(rd_port.dat_r),
            ]


        # Регистры: подключение
        self.comb += [
            regs.address_read.eq(self.address_read),
            regs.address_write.eq(self.address_write),
            regs.data_write.eq(self.data_write),
            regs.write_en.eq(self.write_en),
        ]
