#!/usr/bin/env python3
"""
SII (Slave Information Interface) / EEPROM emulation for ESC.

Регистры 0x0500-0x050F (ETG.1000.4, section 6.4).

Карта SII Control/Status (0x0502-0x0503, 16 бит):
  bit  0 : Write Access         (RW)
  bit  5 : EEPROM emulation     (RO, =1 у нас)
  bit  6 : Read size  0=4 байта / 1=8 байт   (RW)
  bit  7 : Address algorithm    (RW)
  bit  8 : Read Operation       (команда, self-clearing)
  bit  9 : Write Operation      (команда, self-clearing)
  bit 10 : Reload Operation     (команда, self-clearing)
  bit 11 : Checksum Error       (RO)
  bit 12 : Device Info Error    (RO)
  bit 13 : Command Error        (RO)
  bit 14 : Write Error          (RO)
  bit 15 : Busy                 (RO)   <-- мастер поллит ЭТОТ бит

Протокол чтения мастером:
  1. FPWR 0x0504 <- word address (4 байта)
  2. FPWR 0x0502 <- 0x0100 (бит 8 = Read)
  3. FPRD 0x0502 -> ждать, пока бит 15 (Busy) не станет 0
  4. FPRD 0x0508 -> 4 или 8 байт данных

Регистровое чтение полностью комбинаторное (как в ESCRegisterFile).
Внутренняя FSM выборки работает в темпе sys-такта, независимо от transfer.
"""

from migen import *


class SIIInterface(Module):
    def __init__(self, eeprom_words=None, addr_width=13):
        if eeprom_words is None:
            # заглушка: пустой EEPROM
            eeprom_words = [0xFFFF] * 16
        eeprom_words = load_sii_bin("./siitool/examples/colorlight4fmmu.bin")
        # округляем глубину вверх до степени двойки
        depth = 1
        while depth < len(eeprom_words):
            depth <<= 1
        eeprom_words = list(eeprom_words) + [0xFFFF] * (depth - len(eeprom_words))
        self.eeprom_depth = depth
        aw = log2_int(depth)
        print (f'SII address width:{aw} bits, total {len(eeprom_words)} bytes')
        #aw = 8
        # ------------------------------------------------------------------
        # Регистровый интерфейс (как у ESCRegisterFile)
        # ------------------------------------------------------------------
        self.address_read  = Signal(addr_width)
        self.data_read     = Signal(8)
        self.hit           = Signal()

        self.address_write = Signal(addr_width)
        self.data_write    = Signal(8)
        self.write_en      = Signal()

        # ------------------------------------------------------------------
        # EEPROM ROM: 16-битные слова, латентность чтения 1 такт
        # ------------------------------------------------------------------
        self.mem      = Memory(16, depth, init=eeprom_words)
        self.mem_port = self.mem.get_port()
        self.specials += self.mem, self.mem_port

        self.dgram_end = Signal()

        cmd_pending_read   = Signal()
        cmd_pending_write  = Signal()
        cmd_pending_reload = Signal()

        # ------------------------------------------------------------------
        # Состояние
        # ------------------------------------------------------------------
        cfg_write_access = Signal()               # 0x0502 bit0
        cfg_read_size    = Signal()               # 0x0502 bit6  (0 = 4 байта)
        cfg_addr_algo    = Signal(reset=1)        # 0x0502 bit7  (1 = 2-байтная адресация)

        err_checksum     = Signal()               # bit11
        err_device_info  = Signal()               # bit12
        err_command      = Signal()               # bit13
        err_write        = Signal()               # bit14
        self.busy = busy = Signal()               # bit15

        self.config      = Signal(16)             # 0x0500-0x0501 (PDI access, не используем)
        self.eeprom_addr = Signal(32)             # 0x0504-0x0507
        self.data_buffer = Signal(64)             # 0x0508-0x050F

        self.start_read   = start_read =  Signal()
        self.start_write  = start_write = Signal()
        self.start_reload = start_reload = Signal()

        self.word_idx  = word_idx = Signal(2)
        self.n_words   = n_words = Signal(3)                     # 2 или 4 в зависимости от read size

        self.comb += n_words.eq(Mux(cfg_read_size, 4, 2))  # всегда отдаём 4 слова
        # ------------------------------------------------------------------
        # Сборка status-слова для чтения
        # ------------------------------------------------------------------
        sii_status = Signal(16)
        self.comb += [
            sii_status[0].eq(cfg_write_access),
            sii_status[5].eq(1),                  # EEPROM emulation
            sii_status[6].eq(cfg_read_size),
            sii_status[7].eq(cfg_addr_algo),
            # биты 8-10 (команды) читаются как 0 — они self-clearing
            sii_status[11].eq(err_checksum),
            sii_status[12].eq(err_device_info),
            sii_status[13].eq(err_command),
            sii_status[14].eq(err_write),
            sii_status[15].eq(busy),
        ]

        # ------------------------------------------------------------------
        # Адресный декодер: владеем 0x0500-0x050F
        # ------------------------------------------------------------------
        self.comb += self.hit.eq(
            (self.address_read >= 0x0500) & (self.address_read <= 0x050F)
        )

        # ------------------------------------------------------------------
        # Комбинаторное чтение регистров
        # ------------------------------------------------------------------
        self.comb += Case(self.address_read, {
            0x0500: self.data_read.eq(self.config[0:8]),
            0x0501: self.data_read.eq(self.config[8:16]),
            0x0502: self.data_read.eq(sii_status[0:8]),
            0x0503: self.data_read.eq(sii_status[8:16]),
            0x0504: self.data_read.eq(self.eeprom_addr[0:8]),
            0x0505: self.data_read.eq(self.eeprom_addr[8:16]),
            0x0506: self.data_read.eq(self.eeprom_addr[16:24]),
            0x0507: self.data_read.eq(self.eeprom_addr[24:32]),
            0x0508: self.data_read.eq(self.data_buffer[0:8]),
            0x0509: self.data_read.eq(self.data_buffer[8:16]),
            0x050A: self.data_read.eq(self.data_buffer[16:24]),
            0x050B: self.data_read.eq(self.data_buffer[24:32]),
            0x050C: self.data_read.eq(self.data_buffer[32:40]),
            0x050D: self.data_read.eq(self.data_buffer[40:48]),
            0x050E: self.data_read.eq(self.data_buffer[48:56]),
            0x050F: self.data_read.eq(self.data_buffer[56:64]),
            "default": self.data_read.eq(0x00),
        })
        self.dgram_end_d = dgram_end_d = Signal()
        # ------------------------------------------------------------------
        # Запись регистров
        # ------------------------------------------------------------------
        self.sync += [
            start_read.eq(0),
            start_write.eq(0),
            start_reload.eq(0),
            dgram_end_d.eq(self.dgram_end),
            If(self.write_en,
                Case(self.address_write, {
                    0x0500: self.config[0:8].eq(self.data_write),
                    0x0501: self.config[8:16].eq(self.data_write),

                    # младший байт control: конфигурационные биты
                    0x0502: [
                        cfg_write_access.eq(self.data_write[0]),
                        cfg_read_size.eq(self.data_write[6]),
                        cfg_addr_algo.eq(self.data_write[7]),
                    ],

                    # старший байт control: КОМАНДЫ (биты 8,9,10 слова)
                    0x0503: [
                        If(~busy,
                            If(self.data_write[0], cmd_pending_read.eq(1)),    # bit8  Read
                            If(self.data_write[1], cmd_pending_write.eq(1)),   # bit9  Write
                            If(self.data_write[2], cmd_pending_reload.eq(1)),  # bit10 Reload
                        )
                    ],

                    0x0504: self.eeprom_addr[0:8].eq(self.data_write),
                    0x0505: self.eeprom_addr[8:16].eq(self.data_write),
                    0x0506: self.eeprom_addr[16:24].eq(self.data_write),
                    0x0507: self.eeprom_addr[24:32].eq(self.data_write),
                })
            ),
            # запускаем только когда вся датаграмма отписана —
            # к этому моменту eeprom_addr уже актуален

            If(self.dgram_end_d,
                If(cmd_pending_read,   start_read.eq(1)),
                If(cmd_pending_write,  start_write.eq(1)),
                If(cmd_pending_reload, start_reload.eq(1)),
                cmd_pending_read.eq(0),
                cmd_pending_write.eq(0),
                cmd_pending_reload.eq(0),
            ),
        ]

        # ------------------------------------------------------------------
        # Адрес ROM: база + текущий индекс слова
        # ------------------------------------------------------------------
        self.comb += self.mem_port.adr.eq(
            (self.eeprom_addr[0:aw] + word_idx)[0:aw]
        )
        self.base_addr = base_addr = Signal(aw)
        self.comb += self.mem_port.adr.eq(base_addr + word_idx)
        # ------------------------------------------------------------------
        # FSM выборки: 4 слова, латентность памяти 1 такт
        #   SETUP   — адрес выставлен комбинаторно
        #   CAPTURE — dat_r валиден, вдвигаем в data_buffer
        # Порядок: первое слово окажется в [0:16], последнее в [48:64].
        # ------------------------------------------------------------------
        self.submodules.fsm = fsm = FSM(reset_state="IDLE")

        fsm.act("IDLE",
            If(start_read,
                NextValue(busy, 1),
                NextValue(word_idx, 0),
                NextValue(base_addr, self.eeprom_addr[0:aw]),  # ← фиксируем здесь
                NextValue(err_command, 0),
                NextState("SETUP"),
            ).Elif(start_write | start_reload,
                # EEPROM read-only: сразу завершаем, без ошибки записи
                NextValue(busy, 0),
                NextValue(err_write, start_write),
            )
        )
        self.comb += self.mem_port.adr.eq(base_addr + word_idx)

        fsm.act("SETUP",
            NextState("CAPTURE")
        )

        #fsm.act("CAPTURE",
        #    NextValue(self.data_buffer,
        #              Cat(self.data_buffer[16:64], self.mem_port.dat_r)),
        #    If(word_idx == (n_words - 1),
        #        NextValue(busy, 0),
        #        NextState("IDLE"),
        #    ).Else(
        #        NextValue(word_idx, word_idx + 1),
        #        NextState("SETUP"),
        #    )
        #)

        fsm.act("CAPTURE",
            # dat_r валиден для текущего word_idx
            Case(word_idx, {
                0: NextValue(self.data_buffer[0:16],  self.mem_port.dat_r),
                1: NextValue(self.data_buffer[16:32], self.mem_port.dat_r),
                2: NextValue(self.data_buffer[32:48], self.mem_port.dat_r),
                3: NextValue(self.data_buffer[48:64], self.mem_port.dat_r),
            }),
            #NextValue(self.data_buffer, Cat(self.data_buffer[16:64], self.mem_port.dat_r)),
            If(word_idx == (n_words - 1),
                NextValue(busy, 0),
                NextState("IDLE"),
            ).Else(
                NextValue(word_idx, word_idx + 1),
                NextState("SETUP"),   # новый word_idx → новый adr → снова ждём такт
            )
        )




# ----------------------------------------------------------------------
# Хелпер: загрузка .bin от siitool в список 16-битных слов
# ----------------------------------------------------------------------
def load_sii_bin(path):
    import struct
    data = open(path, "rb").read()
    if len(data) % 2:
        data += b"\xff"
    return list(struct.unpack("<%dH" % (len(data) // 2), data))


if __name__ == "__main__":
    from migen.fhdl import verilog
    from litex.soc.integration.common import get_mem_data
    #eeprom_words = get_mem_data(
    #    "./siitool/examples/colorlight4fmmu.bin",
    #    data_width = 32,          # SII — 16-битные слова
    #    endianness = "little",    # little-endian, как в спецификации
    #)
    eeprom_words = load_sii_bin("./siitool/examples/colorlight4fmmu.bin")
    sii = SIIInterface(eeprom_words)
    #print(verilog.convert(sii, ios={
    #    sii.address_read, sii.data_read, sii.hit,
    #    sii.address_write, sii.data_write, sii.write_en,
    #}))
