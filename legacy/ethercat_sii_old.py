#!/usr/bin/env python3
"""
SII (Slave Information Interface) / EEPROM emulation for ESC.

Owns the 0x0500-0x050F register range (ETG.1000.4 Section 6.6).

Protocol:
  - Master writes word address to 0x0504-0x0505
  - Master writes control to 0x0502:
      bit 0   : R/W (0 = read, 1 = write)
      bits 1-2: word offset within the 4-word block
      bit 3   : Busy (master sets 1 to start)
  - Slave sets status[3] = 1 (Busy), performs operation, clears Busy
  - Master reads 8 bytes from 0x0508-0x050F

Read path is fully combinational (same timing as ESCRegisterFile).
The EEPROM read FSM runs at BRAM pace (1 word / cycle, 4 words total).
"""

from migen import *


class SIIInterface(Module):
    def __init__(self, eeprom_words=None):
        if eeprom_words is None:
            eeprom_words = [0x0FFF, 0x0000]  # magic + size

        self.eeprom_size = len(eeprom_words)

        # ------------------------------------------------------------------
        # Регистровый интерфейс — как у ESCRegisterFile
        # ------------------------------------------------------------------
        self.address_read  = Signal(16)
        self.data_read     = Signal(8)
        self.hit           = Signal()

        self.address_write = Signal(16)
        self.data_write    = Signal(8)
        self.write_en      = Signal()

        # ------------------------------------------------------------------
        # EEPROM BRAM: 16-бит слова, 1 такт латентности
        # ------------------------------------------------------------------
        self.mem      = Memory(16, self.eeprom_size, init=eeprom_words)
        self.mem_port = self.mem.get_port()
        self.specials += self.mem, self.mem_port

        # ------------------------------------------------------------------
        # Регистры SII
        # ------------------------------------------------------------------
        self.config         = Signal(16, reset=0x0000)  # 0x0500-0x0501
        self.control        = Signal(8,  reset=0x00)    # 0x0502
        self.status         = Signal(8,  reset=0x00)    # 0x0503
        self.eeprom_address = Signal(16, reset=0x0000)  # 0x0504-0x0505
        self.data_buffer    = Array([Signal(16) for _ in range(4)])  # 0x0508-0x050F

        # ------------------------------------------------------------------
        # FSM состояния
        # ------------------------------------------------------------------
        self.counter = Signal(3, reset=0)
        self.busy    = Signal(reset=0)
        self.op_read = Signal(reset=0)

        # ------------------------------------------------------------------
        # Адресный декодер: мы владеем 0x0500-0x050F
        # ------------------------------------------------------------------
        self.comb += self.hit.eq(
            (self.address_read >= 0x0500) & (self.address_read <= 0x050F)
        )

        # ------------------------------------------------------------------
        # Комбинаторный read-mux (те же тайминги, что у регистров)
        # ------------------------------------------------------------------
        self.comb += [
            Case(self.address_read, {
                0x0500: self.data_read.eq(self.config[0:8]),
                0x0501: self.data_read.eq(self.config[8:16]),
                0x0502: self.data_read.eq(self.control),
                0x0503: self.data_read.eq(self.status),
                0x0504: self.data_read.eq(self.eeprom_address[0:8]),
                0x0505: self.data_read.eq(self.eeprom_address[8:16]),
                0x0506: self.data_read.eq(0),
                0x0507: self.data_read.eq(0),
                0x0508: self.data_read.eq(self.data_buffer[0][0:8]),
                0x0509: self.data_read.eq(self.data_buffer[0][8:16]),
                0x050A: self.data_read.eq(self.data_buffer[1][0:8]),
                0x050B: self.data_read.eq(self.data_buffer[1][8:16]),
                0x050C: self.data_read.eq(self.data_buffer[2][0:8]),
                0x050D: self.data_read.eq(self.data_buffer[2][8:16]),
                0x050E: self.data_read.eq(self.data_buffer[3][0:8]),
                0x050F: self.data_read.eq(self.data_buffer[3][8:16]),
                "default": self.data_read.eq(0),
            })
        ]

        # ------------------------------------------------------------------
        # Запись регистров
        # ------------------------------------------------------------------
        self.sync += [
            If(self.write_en,
                Case(self.address_write, {
                    0x0500: self.config[0:8].eq(self.data_write),
                    0x0501: self.config[8:16].eq(self.data_write),

                    0x0502: [
                        self.control.eq(self.data_write),
                        # Старт операции — по Busy=1, если ещё не заняты
                        If(self.data_write[3] & ~self.busy,
                            self.busy.eq(1),
                            self.op_read.eq(~self.data_write[0]),
                            self.counter.eq(0),
                            self.status[3].eq(1),
                        )
                    ],

                    0x0504: self.eeprom_address[0:8].eq(self.data_write),
                    0x0505: self.eeprom_address[8:16].eq(self.data_write),

                    0x0508: self.data_buffer[0][0:8].eq(self.data_write),
                    0x0509: self.data_buffer[0][8:16].eq(self.data_write),
                    0x050A: self.data_buffer[1][0:8].eq(self.data_write),
                    0x050B: self.data_buffer[1][8:16].eq(self.data_write),
                    0x050C: self.data_buffer[2][0:8].eq(self.data_write),
                    0x050D: self.data_buffer[2][8:16].eq(self.data_write),
                    0x050E: self.data_buffer[3][0:8].eq(self.data_write),
                    0x050F: self.data_buffer[3][8:16].eq(self.data_write),
                })
            )
        ]

        # ------------------------------------------------------------------
        # EEPROM read FSM
        # ------------------------------------------------------------------
        # Адрес памяти — комбинаторно от counter. Латентность 1 такт.
        #   cyc 0: adr = base+0, counter -> 1
        #   cyc 1: dat = EEPROM[base+0], buf[0]=dat, adr=base+1, counter -> 2
        #   cyc 2: dat = EEPROM[base+1], buf[1]=dat, adr=base+2, counter -> 3
        #   cyc 3: dat = EEPROM[base+2], buf[2]=dat, adr=base+3, counter -> 4
        #   cyc 4: dat = EEPROM[base+3], buf[3]=dat, busy=0, status[3]=0
        # ------------------------------------------------------------------
        self.comb += self.mem_port.adr.eq(self.eeprom_address + self.counter)

        self.sync += [
            If(self.busy,
                If(self.op_read,
                    If(self.counter == 0,
                        self.counter.eq(1)
                    ).Elif(self.counter == 1,
                        self.data_buffer[0].eq(self.mem_port.dat_r),
                        self.counter.eq(2)
                    ).Elif(self.counter == 2,
                        self.data_buffer[1].eq(self.mem_port.dat_r),
                        self.counter.eq(3)
                    ).Elif(self.counter == 3,
                        self.data_buffer[2].eq(self.mem_port.dat_r),
                        self.counter.eq(4)
                    ).Else(
                        self.data_buffer[3].eq(self.mem_port.dat_r),
                        self.busy.eq(0),
                        self.status[3].eq(0),
                    )
                ).Else(
                    # Write: read-only emulation, просто снимаем Busy
                    self.busy.eq(0),
                    self.status[3].eq(0),
                )
            )
        ]
