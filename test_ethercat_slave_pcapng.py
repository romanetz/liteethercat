#!/usr/bin/env python3
"""
Fixed TxPDO/RxPDO Tests with proper frame boundary detection
"""

import os
import sys
import gzip
import struct
import unittest
from migen import *
from migen.sim import run_simulation, passive

from litex.soc.interconnect import stream
from liteeth.common import eth_phy_description

sys.path.insert(0, os.path.dirname(__file__))
from ethercat_top import *

from ethercat_pu import state_map
STATE_MAP_REV = {v: k for k, v in state_map.items()}


# =============================================================================
# PCAP Reader/Writer (same as before)
# =============================================================================

class PCAPReader:
    @staticmethod
    def read_packets(filename):
        packets = []

        if filename.endswith('.gz'):
            with gzip.open(filename, 'rb') as f:
                data = f.read()
        else:
            with open(filename, 'rb') as f:
                data = f.read()

        if len(data) < 24:
            raise ValueError("File too short for PCAP header")

        magic = struct.unpack("<I", data[0:4])[0]

        if magic == 0xa1b2c3d4:
            endian = "<"
            offset = 24
        elif magic == 0xd4c3b2a1:
            endian = ">"
            offset = 24
        elif magic == 0x0a0d0d0a:
            return PCAPReader._read_pcapng(data)
        else:
            raise ValueError(f"Unknown magic: {magic:#010x}")

        while offset < len(data):
            if offset + 16 > len(data):
                break

            ts_sec, ts_usec, incl_len, orig_len = struct.unpack(
                endian + "IIII", data[offset:offset+16])
            offset += 16

            if offset + incl_len > len(data):
                break

            packet_data = list(data[offset:offset+incl_len])
            packets.append({
                'timestamp': ts_sec + ts_usec / 1e6,
                'data': packet_data,
                'orig_length': orig_len,
                'captured_length': incl_len
            })
            offset += incl_len

        return packets

    @staticmethod
    def _read_pcapng(data):
        packets = []
        offset = 0

        while offset < len(data):
            if offset + 8 > len(data):
                break

            block_type = struct.unpack("<I", data[offset:offset+4])[0]
            block_len = struct.unpack("<I", data[offset+4:offset+8])[0]

            if block_type == 0x00000006:
                if offset + 28 + block_len > len(data):
                    offset += block_len
                    continue

                captured_len = struct.unpack("<I", data[offset+20:offset+24])[0]
                orig_len = struct.unpack("<I", data[offset+24:offset+28])[0]

                if offset + 28 + captured_len > len(data):
                    offset += block_len
                    continue

                packet_data = list(data[offset+28:offset+28+captured_len])
                packets.append({
                    'timestamp': 0,
                    'data': packet_data,
                    'orig_length': orig_len,
                    'captured_length': captured_len
                })

            offset += block_len

        return packets


class PCAPWriter:
    def __init__(self, filename):
        self.filename = filename
        self.packets = []

    def add_packet(self, data, timestamp=0):
        self.packets.append({'timestamp': timestamp, 'data': list(data)})

    def write(self):
        with open(self.filename, 'wb') as f:
            f.write(struct.pack("<I", 0xa1b2c3d4))
            f.write(struct.pack("<H", 2))
            f.write(struct.pack("<H", 4))
            f.write(struct.pack("<I", 0))
            f.write(struct.pack("<I", 0))
            f.write(struct.pack("<I", 65535))
            f.write(struct.pack("<I", 1))

            for pkt in self.packets:
                ts = pkt['timestamp']
                ts_sec = int(ts)
                ts_usec = int((ts - ts_sec) * 1e6)
                pkt_data = bytes(pkt['data'])

                f.write(struct.pack("<I", ts_sec))
                f.write(struct.pack("<I", ts_usec))
                f.write(struct.pack("<I", len(pkt_data)))
                f.write(struct.pack("<I", len(pkt_data)))
                f.write(pkt_data)


# =============================================================================
# EtherCAT Analyzer (same as before)
# =============================================================================

class EtherCATAnalyzer:
    MASTER_MAC = bytes([0x00, 0x14, 0x4f, 0x23, 0x98, 0xcf])

    @staticmethod
    def strip_padding(packet):
        if len(packet) < 14:
            return packet

        if not (len(packet) >= 14 and packet[12] == 0x88 and packet[13] == 0xA4):
            return packet

        if len(packet) < 16:
            return packet

        ecat_len = packet[14] | (packet[15] << 8)
        ecat_len &= 0x07FF
        actual_len = 14 + 2 + ecat_len

        if actual_len < len(packet):
            return packet[:actual_len]

        return packet

    @staticmethod
    def is_ethercat(packet):
        if len(packet) < 14:
            return False
        return packet[12] == 0x88 and packet[13] == 0xA4

    @staticmethod
    def is_from_master(packet):
        if len(packet) < 12:
            return False
        return bytes(packet[6:12]) == EtherCATAnalyzer.MASTER_MAC

    @staticmethod
    def parse_datagrams(packet):
        if not EtherCATAnalyzer.is_ethercat(packet):
            return []

        packet = EtherCATAnalyzer.strip_padding(packet)
        datagrams = []
        offset = 14

        if offset + 2 > len(packet):
            return []

        ecat_len = packet[offset] | (packet[offset+1] << 8)
        ecat_len &= 0x07FF
        offset += 2
        ecat_end = offset + ecat_len

        while offset + 12 <= ecat_end:
            cmd = packet[offset]
            idx = packet[offset + 1]

            address = (packet[offset + 2] |
                      (packet[offset + 3] << 8) |
                      (packet[offset + 4] << 16) |
                      (packet[offset + 5] << 24))

            length = packet[offset + 6] | ((packet[offset + 7] & 0x07) << 8)

            data_offset = offset + 10
            if data_offset + length + 2 > len(packet):
                break

            data = packet[data_offset:data_offset + length]
            wkc = packet[data_offset + length] | (packet[data_offset + length + 1] << 8)

            datagrams.append({
                'cmd': cmd,
                'idx': idx,
                'address': address,
                'length': length,
                'data': list(data),
                'wkc': wkc,
                'offset': offset
            })

            offset += 12 + length

        return datagrams

    @staticmethod
    def describe_command(cmd):
        commands = {
            0x01: "APRD", 0x02: "APWR", 0x03: "APRW",
            0x04: "FPRD", 0x05: "FPWR", 0x06: "FPRW",
            0x07: "BRD",  0x08: "BWR",  0x09: "BRW",
            0x0A: "LRD",  0x0B: "LWR",  0x0C: "LRW",
            0x0D: "ARMW", 0x0E: "FRMW"
        }
        return commands.get(cmd, f"UNK({cmd:#04x})")

# =============================================================================
# WKC-анализ кадров с несколькими датаграммами
# =============================================================================

def wkc_expectation(dg, fp_address=0):
    cmd = dg['cmd']
    adp = dg['address'] & 0xFFFF           # позиция/станция — МЛАДШЕЕ слово
    ado = (dg['address'] >> 16) & 0xFFFF   # смещение — старшее

    if cmd == 0x00:
        return False, "NOP"
    if cmd in (0x07, 0x08, 0x09):
        return True, f"broadcast reg={ado:#06x}, обязан"
    if cmd in (0x01, 0x02, 0x03):
        return (adp == 0x0000), f"auto-inc ADP={adp:#06x} (наш=0x0000)"
    if cmd in (0x04, 0x05, 0x06):
        return (adp == fp_address), f"station {adp:#06x} vs fp {fp_address:#06x}"
    if cmd in (0x0A, 0x0B, 0x0C):
        return None, f"logical @{dg['address']:#010x} (FMMU)"
    return None, f"cmd {cmd:#04x} не поддержана"

def analyze_frame_wkc(orig_packet, resp_packet, fp_address=0, verbose=False):
    """
    Парное сравнение WKC всех датаграмм кадра.
    Возвращает dict со статистикой; печатает таблицу при verbose или расхождениях.
    """
    orig_dgs = EtherCATAnalyzer.parse_datagrams(orig_packet)
    resp_dgs = EtherCATAnalyzer.parse_datagrams(resp_packet)

    stats = {'n_dg': len(orig_dgs), 'n_fail': 0, 'fails': []}

    lines = []
    if len(orig_dgs) != len(resp_dgs):
        stats['n_fail'] += 1
        stats['fails'].append(f"структура: {len(orig_dgs)} датаграмм -> "
                              f"{len(resp_dgs)}")
        verbose = True

    for i, (o, r) in enumerate(zip(orig_dgs, resp_dgs)):
        name  = EtherCATAnalyzer.describe_command(o['cmd'])
        delta = r['wkc'] - o['wkc']
        must, tag = wkc_expectation(o, fp_address)

        if must is None:
            ok, mark = True, "info"
        elif must:
            ok, mark = (delta > 0), "OK" if delta > 0 else "FAIL"
        else:
            ok, mark = (delta == 0), "OK" if delta == 0 else "FAIL"

        if not ok:
            stats['n_fail'] += 1
            stats['fails'].append(f"[{i}] {name} {tag}: WKC {o['wkc']}->{r['wkc']}")
            verbose = True

        lines.append(f"  [{i}] {name:<4} addr={o['address']:#011x} len={o['length']:<4}"
                     f" wkc {o['wkc']:>3} -> {r['wkc']:<3} (Δ{delta:+d})  "
                     f"{mark:>4}  {tag}")

    # компактная строка всегда: все wkc запроса и ответа
    orig_list = [d['wkc'] for d in orig_dgs]
    resp_list = [d['wkc'] for d in resp_dgs]
    print(f"  WKC in={orig_list} out={resp_list}")

    if verbose:
        print("\n".join(lines))

    return stats




# =============================================================================
# PHY Models - FIXED
# =============================================================================

class MasterPHY:
    def __init__(self):
        self.tx_queue = []

    def send_packet(self, data, delay_cycles=0):
        self.tx_queue.append({'data': list(data), 'delay': delay_cycles})

    def get_next(self):
        if self.tx_queue and self.tx_queue[0]['delay'] == 0:
            return self.tx_queue.pop(0)['data']
        elif self.tx_queue:
            self.tx_queue[0]['delay'] -= 1
        return None


class SlavePHY:
    def __init__(self):
        self.rx_buffer = []
        self.rx_done = False
        self.packets = []

    def add_byte(self, byte):
        self.rx_buffer.append(byte)

    def mark_complete(self):
        if self.rx_buffer:
            self.packets.append(list(self.rx_buffer))
            self.rx_buffer = []
            self.rx_done = True

    def clear(self):
        self.rx_done = False

    def get_packets(self):
        return list(self.packets)


# =============================================================================
# DUT
# =============================================================================

class DUT_PDO(Module):
    def __init__(self):
        self.clock_domains.cd_sys = ClockDomain()
        self.sim_done = False

        self.submodules.slave = EtherCATSlave()

        self.master_source = stream.Endpoint(eth_phy_description(8))
        self.slave_sink = stream.Endpoint(eth_phy_description(8))

        self.comb += [
            self.slave.sink.valid.eq(self.master_source.valid),
            self.slave.sink.data.eq(self.master_source.data),
            self.slave.sink.last_be.eq(self.master_source.last_be),
            self.slave.sink.error.eq(self.master_source.error),
            self.master_source.ready.eq(self.slave.sink.ready),

            self.slave_sink.valid.eq(self.slave.source.valid),
            self.slave_sink.data.eq(self.slave.source.data),
            self.slave_sink.last_be.eq(self.slave.source.last_be),
            self.slave_sink.error.eq(self.slave.source.error),
            self.slave.source.ready.eq(self.slave_sink.ready),
        ]

        self.master = MasterPHY()
        self.slave_rx = SlavePHY()

        self.frames_sent = Signal(32)
        self.frames_received = Signal(32)

        self.sync += [
            If(self.master_source.valid & self.master_source.ready & self.master_source.last_be,
                self.frames_sent.eq(self.frames_sent + 1)
            ),
            If(self.slave_sink.valid & self.slave_sink.ready & self.slave_sink.last_be,
                self.frames_received.eq(self.frames_received + 1)
            )
        ]


# =============================================================================
# Generators - FIXED with proper frame boundary handling
# =============================================================================

@passive
def master_tx_generator(dut):
    """Fixed: Send complete frames with proper last_be signaling."""
    startup = 20

    while not dut.sim_done:
        if startup > 0:
            startup -= 1
            yield dut.master_source.valid.eq(0)
            yield dut.master_source.last_be.eq(0)
            yield
            continue

        packet = dut.master.get_next()

        if packet:
            # Send packet byte-by-byte
            for i, byte in enumerate(packet):
                yield dut.master_source.valid.eq(1)
                yield dut.master_source.data.eq(byte)
                yield dut.master_source.last_be.eq(1 if i == len(packet)-1 else 0)
                yield dut.master_source.error.eq(0)

                # Wait for ready
                while True:
                    yield
                    ready = yield dut.master_source.ready
                    if ready:
                        break

            # Clear signals after frame
            yield dut.master_source.valid.eq(0)
            yield dut.master_source.last_be.eq(0)

            # Inter-frame gap (critical!)
            for _ in range(20):
                if dut.sim_done:
                    return
                yield
        else:
            yield dut.master_source.valid.eq(0)
            yield dut.master_source.last_be.eq(0)
            yield


@passive
def slave_rx_generator(dut):
    """Fixed: Capture complete frames based on last_be."""

    while not dut.sim_done:
        yield dut.slave_sink.ready.eq(1)
        yield

        valid = yield dut.slave_sink.valid
        ready = yield dut.slave_sink.ready

        if valid and ready:
            byte = yield dut.slave_sink.data
            last = yield dut.slave_sink.last_be

            dut.slave_rx.add_byte(byte)

            if last:
                dut.slave_rx.mark_complete()
                print(f"[DEBUG] Captured frame: {len(dut.slave_rx.packets[-1])} bytes")


# =============================================================================
# Test Cases
# =============================================================================

class TestEtherCATPDO(unittest.TestCase):

    def test_logical_read_write(self):
        """Test LRD/LWR commands."""
        dut = DUT_PDO()

        def build_fpwr_frame(station, offset, data):
            # Новая раскладка: [ADP (станция/позиция) 16b | ADO (смещение) 16b]
            addr32 = ((offset & 0xFFFF) << 16) | (station & 0xFFFF)
            frame = [0xFF]*6 + [0x00, 0x14, 0x4f, 0x23, 0x98, 0xcf] + [0x88, 0xA4]
            ecat_len = 10 + len(data) + 2
            frame += [ecat_len & 0xFF, (ecat_len >> 8) & 0xFF]
            frame += [EC_CMD_FPWR, 0x00]
            frame += [addr32 & 0xFF, (addr32 >> 8) & 0xFF,
                    (addr32 >> 16) & 0xFF, (addr32 >> 24) & 0xFF]
            frame += [len(data) & 0xFF, (len(data) >> 8) & 0x07]
            frame += [0x00, 0x00] + list(data) + [0x00, 0x00]
            while len(frame) < 60:
                frame.append(0x00)
            return frame

        def build_lwr_frame(log_addr, data):
            # Логический адрес — 32 бита как есть, без сдвигов!
            frame = [0xFF]*6 + [0x00, 0x14, 0x4f, 0x23, 0x98, 0xcf] + [0x88, 0xA4]
            ecat_len = 10 + len(data) + 2
            frame += [ecat_len & 0xFF, (ecat_len >> 8) & 0xFF]
            frame += [EC_CMD_LWR, 0x00]
            frame += [log_addr & 0xFF, (log_addr >> 8) & 0xFF,
                    (log_addr >> 16) & 0xFF, (log_addr >> 24) & 0xFF]  # 0x1100 → [0x00, 0x11, 0x00, 0x00]
            frame += [len(data) & 0xFF, (len(data) >> 8) & 0x07]
            frame += [0x00, 0x00] + list(data) + [0x00, 0x00]
            while len(frame) < 60:
                frame.append(0x00)
            return frame

        def build_lrd_frame(log_addr, length):
            frame = [0xFF]*6 + [0x00, 0x14, 0x4f, 0x23, 0x98, 0xcf] + [0x88, 0xA4]
            ecat_len = 10 + length + 2
            frame += [ecat_len & 0xFF, (ecat_len >> 8) & 0xFF]
            frame += [EC_CMD_LRD, 0x00]
            frame += [log_addr & 0xFF, (log_addr >> 8) & 0xFF,
                    (log_addr >> 16) & 0xFF, (log_addr >> 24) & 0xFF]
            frame += [length & 0xFF, (length >> 8) & 0x07]
            frame += [0x00, 0x00]
            frame += [0x00] * length
            frame += [0x00, 0x00]
            while len(frame) < 60:
                frame.append(0x00)
            return frame



        fmmu_cfg = [0x00, 0x11, 0x00, 0x00,  0x00, 0x01,  0x00, 0x07,
                    0x00, 0x10,              0x00,        0x03, 0x01,
                    0x00, 0x00, 0x00]

        def test_sequence():
            for _ in range(30):
                yield

            # --- FPWR ---
            print("TEST1 [DEBUG FPWR] Configure FMMU")
            dut.master.send_packet(build_fpwr_frame(0x0000, 0x0600, fmmu_cfg))

            timeout = 0
            while not dut.slave_rx.rx_done and timeout < 2000:
                timeout += 1
                fsm_state = yield dut.slave.pu.dbg_state
                dbg_cmd = yield dut.slave.pu.dbg_cmd
                dbg_addr = yield dut.slave.pu.dbg_addr
                dbg_match = yield dut.slave.pu.dbg_cmd_match
                mem_logical_early_would_hit = yield dut.slave.pu.mem_logical_early_would_hit
                dbg_fp = yield dut.slave.fp_address
                sink_data = yield dut.slave.pu.sink.data
                sink_valid = yield dut.slave.pu.sink.valid
                sink_ready = yield dut.slave.pu.sink.ready
                print(f"[TEST 1 DEBUG FPWR] input_data={hex(sink_data)}, fsm_state={STATE_MAP_REV[fsm_state]}, cmd={hex(dbg_cmd)}, addr={hex(dbg_addr)}, "
                    f"addr_pos={hex(dbg_addr >> 16)}, fp_address={hex(dbg_fp)}, early_would_hit={mem_logical_early_would_hit},match={dbg_match}")
                #print(f"[DEBUG SINK] valid={sink_valid}, ready={sink_ready}")
                yield

            self.assertTrue(dut.slave_rx.rx_done, "FPWR timeout")
            cfg_resp = EtherCATAnalyzer.parse_datagrams(dut.slave_rx.packets[-1])
            cfg_wkc = cfg_resp[0]['wkc'] if cfg_resp else None

            # DEBUG после rx_done
            for _ in range(10):
                yield

            print(f"[CFG] FMMU WKC={cfg_wkc}")

            self.assertEqual(cfg_wkc, 1, "FPWR WKC != 1")
            dut.slave_rx.clear()

            for _ in range(50):
                yield



            # После FPWR WKC=1, до LWR:
            #print("\n[FMMU CONFIG DUMP]")
            #for i in range(16):
                # Читаем регистры FMMU канала 0 (base=0x0600)
            #    yield dut.slave.mem.fmmu.channels[0].reg_read_addr.eq(0x0600 + i)
            #    yield
            #    val = yield dut.slave.mem.fmmu.channels[0].reg_read_data
            #    print(f"  FMMU[0x{0x0600+i:04x}] = 0x{val:02x}")

            for i in range(4):
                ch = dut.slave.mem.fmmu.channels[i]
                log_start = yield ch.logical_start_addr
                length    = yield ch.length
                phys_start= yield ch.physical_start_addr
                op_type   = yield ch.operation_type
                activate  = yield ch.activate
                start_bit = yield ch.logical_start_bit
                end_bit   = yield ch.logical_end_bit
                print(f"\n[FMMU CH{i}] log_start=0x{log_start:08x} length=0x{length:04x} "
                    f"phys=0x{phys_start:04x} type=0x{op_type:02x} active={activate} "
                    f"start_bit={start_bit} end_bit={end_bit}")

            # Проверим would_hit для адреса 0x1100
            #yield dut.slave.mem.fmmu.logical_addr_in.eq(0x1100)
            #yield
            would_hit = yield dut.slave.mem.fmmu.any_would_hit
            print(f"[FMMU] would_hit для addr=0x1100: {would_hit}")


            # --- LWR (ТОЛЬКО ТЕПЕРЬ кладём в очередь!) ---
            print("\n[TEST] LWR to logical 0x1100")
            test_data = [0xAA, 0xBB, 0xCC, 0xDD]
            frame = build_lwr_frame(0x1100, test_data)
            dut.master.send_packet(frame)   # ← ПОСЛЕ дренажа FPWR

            timeout = 0
            while not dut.slave_rx.rx_done and timeout < 2000:
                fsm_state = yield dut.slave.pu.dbg_state
                dbg_cmd = yield dut.slave.pu.dbg_cmd
                dbg_addr = yield dut.slave.pu.dbg_addr
                dbg_match = yield dut.slave.pu.dbg_cmd_match
                mem_logical_early_would_hit = yield dut.slave.pu.mem_logical_early_would_hit
                dbg_fp = yield dut.slave.fp_address
                sink_data = yield dut.slave.pu.sink.data
                sink_valid = yield dut.slave.pu.sink.valid
                sink_ready = yield dut.slave.pu.sink.ready
                mem_rd_data = yield dut.slave.mem.rd_port.dat_r
                mem_addr = yield dut.slave.mem.rd_port.adr
                print(f"[TEST 1 DEBUG LWR] input_data={hex(sink_data)}, fsm_state={STATE_MAP_REV[fsm_state]}, cmd={hex(dbg_cmd)}, addr={hex(dbg_addr)}, "
                    f"addr_pos={hex(dbg_addr >> 16)}, fp={hex(dbg_fp)}, early_would_hit={mem_logical_early_would_hit},match={dbg_match},mem_addr={hex(mem_addr)}, mem_rd_data={hex(mem_rd_data)}")
                #print(f"[DEBUG SINK] valid={sink_valid}, ready={sink_ready}")
                timeout += 1
                yield

            self.assertTrue(dut.slave_rx.rx_done, "LWR response timeout")
            response = dut.slave_rx.packets[-1]
            print(f"[RX] LWR response: {len(response)} bytes")
            # После получения ответа на LWR, до clear:
            fmmu_would = yield dut.slave.mem.fmmu.any_would_hit
            fmmu_hit   = yield dut.slave.mem.fmmu.any_hit
            fmmu_ok    = yield dut.slave.mem.fmmu.access_ok
            fmmu_addr  = yield dut.slave.mem.fmmu.final_phys_addr
            fmmu_perm  = yield dut.slave.mem.fmmu.any_perm_error
            fmmu_range = yield dut.slave.mem.fmmu.range_error   # если есть
            cmd_match  = yield dut.slave.pu.dbg_cmd_match
            print(f"[FMMU] would_hit={fmmu_would} hit={fmmu_hit} access_ok={fmmu_ok} "
                f"phys={hex(fmmu_addr)} perm={fmmu_perm} range={fmmu_range if fmmu_range is not None else 'N/A'}")
            print(f"[PU] cmd_match={cmd_match} dg_access_ok={yield dut.slave.pu.dg_access_ok}")
            resp_dg = EtherCATAnalyzer.parse_datagrams(response)
            if resp_dg:
                print(f"[PASS] LWR completed, WKC={resp_dg[0]['wkc']}")
                self.assertEqual(resp_dg[0]['wkc'], 1)
            else:
                print("[FAIL] Could not parse LWR response")
                self.fail("LWR response parsing failed")




            dut.slave_rx.clear()

            # Wait between frames
            for _ in range(50):
                yield

            # Предполагая, что в ESCMemory физическая DPRAM хранится как self.mem (Migen Memory)
            data0 = yield dut.slave.mem.mem[0x1000]
            data1 = yield dut.slave.mem.mem[0x1001]
            data2 = yield dut.slave.mem.mem[0x1002]
            data3 = yield dut.slave.mem.mem[0x1003]
            print(f"[MEM DUMP] 0x1000..0x1003 = {data0:#04x} {data1:#04x} {data2:#04x} {data3:#04x}")


            # --- FPWR ---
            print("TEST2 [DEBUG FPWR] Configure FMMU")
            dut.master.send_packet(build_fpwr_frame(0x0000, 0x0600, fmmu_cfg))

            timeout = 0
            while not dut.slave_rx.rx_done and timeout < 2000:
                fsm_state = yield dut.slave.pu.dbg_state
                dbg_cmd = yield dut.slave.pu.dbg_cmd
                dbg_addr = yield dut.slave.pu.dbg_addr
                dbg_match = yield dut.slave.pu.dbg_cmd_match
                dbg_fp = yield dut.slave.fp_address
                sink_data = yield dut.slave.pu.sink.data
                sink_valid = yield dut.slave.pu.sink.valid
                sink_ready = yield dut.slave.pu.sink.ready
                mem_data = yield dut.slave.mem.logical_data_read
                print(f"[TEST 2 DEBUG FPWR] input_data={hex(sink_data)}, fsm_state={STATE_MAP_REV[fsm_state]}, cmd={hex(dbg_cmd)}, addr={hex(dbg_addr)}, "
                    f"addr_pos={hex(dbg_addr >> 16)}, fp={hex(dbg_fp)}, match={dbg_match}, mem_data={hex(mem_data)}")
                #print(f"[DEBUG SINK] valid={sink_valid}, ready={sink_ready}")
                timeout += 1
                yield

            self.assertTrue(dut.slave_rx.rx_done, "FPWR timeout")
            cfg_resp = EtherCATAnalyzer.parse_datagrams(dut.slave_rx.packets[-1])
            cfg_wkc = cfg_resp[0]['wkc'] if cfg_resp else None

            # DEBUG после rx_done
            for _ in range(10):
                yield

            fsm_state = yield dut.slave.pu.dbg_state
            dbg_cmd = yield dut.slave.pu.dbg_cmd
            dbg_addr = yield dut.slave.pu.dbg_addr
            dbg_match = yield dut.slave.pu.dbg_cmd_match
            dbg_fp = yield dut.slave.fp_address
            sink_valid = yield dut.slave.pu.sink.valid
            sink_ready = yield dut.slave.pu.sink.ready
            print(f"[DEBUG FPWR] fsm_state={fsm_state}, cmd={hex(dbg_cmd)}, addr={hex(dbg_addr)}, "
                  f"addr_pos={hex(dbg_addr >> 16)}, fp={hex(dbg_fp)}, match={dbg_match}")
            #print(f"[DEBUG SINK] valid={sink_valid}, ready={sink_ready}")

            print(f"[CFG] FMMU WKC={cfg_wkc}")

            self.assertEqual(cfg_wkc, 1, "FPWR WKC != 1")
            dut.slave_rx.clear()

            for _ in range(50):
                yield

            # После FPWR WKC=1, до LWR:
            print("\n[FMMU CONFIG DUMP]")
            for i in range(16):
                # Читаем регистры FMMU канала 0 (base=0x0600)
                yield dut.slave.mem.fmmu.channels[0].reg_read_addr.eq(0x0600 + i)
                yield
                val = yield dut.slave.mem.fmmu.channels[0].reg_read_data
                print(f"  FMMU[0x{0x0600+i:04x}] = 0x{val:02x}")

            ch = dut.slave.mem.fmmu.channels[0]
            log_start = yield ch.logical_start_addr
            length    = yield ch.length
            phys_start= yield ch.physical_start_addr
            op_type   = yield ch.operation_type
            activate  = yield ch.activate
            start_bit = yield ch.logical_start_bit
            end_bit   = yield ch.logical_end_bit
            print(f"\n[FMMU CH0] log_start=0x{log_start:08x} length=0x{length:04x} "
                f"phys=0x{phys_start:04x} type=0x{op_type:02x} active={activate} "
                f"start_bit={start_bit} end_bit={end_bit}")

            # Проверим would_hit для адреса 0x1100
            #yield dut.slave.mem.fmmu.logical_addr_in.eq(0x1100)
            #yield
            #would_hit = yield dut.slave.mem.fmmu.any_would_hit
            #print(f"[FMMU] would_hit для addr=0x1100: {would_hit}")


            print("\n[TEST] LRD from logical 0x1100")
            frame = build_lrd_frame(0x1100, 4)
            print(f"[TX] LRD frame: {len(frame)} bytes")
            dut.master.send_packet(frame)

            timeout = 0
            while not dut.slave_rx.rx_done and timeout < 2000:
                fsm_state = yield dut.slave.pu.dbg_state
                dbg_cmd = yield dut.slave.pu.dbg_cmd
                dbg_addr = yield dut.slave.pu.dbg_addr
                dbg_match = yield dut.slave.pu.dbg_cmd_match
                dbg_fp = yield dut.slave.fp_address
                sink_data = yield dut.slave.pu.sink.data
                sink_valid = yield dut.slave.pu.sink.valid
                sink_ready = yield dut.slave.pu.sink.ready
                mem_data = yield dut.slave.mem.logical_data_read
                mem_rd_data = yield dut.slave.mem.rd_port.dat_r
                mem_addr = yield dut.slave.mem.rd_port.adr
                print(f"[TEST 2 DEBUG LRD] input_data={hex(sink_data)}, fsm_state={STATE_MAP_REV[fsm_state]}, cmd={hex(dbg_cmd)}, addr={hex(dbg_addr)}, "
                    f"addr_pos={hex(dbg_addr >> 16)}, fp={hex(dbg_fp)}, match={dbg_match},mem_addr={hex(mem_addr)}, mem_rd_data={hex(mem_rd_data)}")

                #print(f"[DEBUG SINK] valid={sink_valid}, ready={sink_ready}")
                timeout += 1
                yield

            self.assertTrue(dut.slave_rx.rx_done, "LRD response timeout")
            response = dut.slave_rx.packets[-1]
            print(f"[RX] LRD response: {len(response)} bytes")
            # После получения ответа на LWR, до clear:
            fmmu_would = yield dut.slave.mem.fmmu.any_would_hit
            fmmu_hit   = yield dut.slave.mem.fmmu.any_hit
            fmmu_ok    = yield dut.slave.mem.fmmu.access_ok
            fmmu_addr  = yield dut.slave.mem.fmmu.final_phys_addr
            fmmu_perm  = yield dut.slave.mem.fmmu.any_perm_error
            fmmu_range = yield dut.slave.mem.fmmu.range_error   # если есть
            cmd_match  = yield dut.slave.pu.dbg_cmd_match
            print(f"[FMMU] would_hit={fmmu_would} hit={fmmu_hit} access_ok={fmmu_ok} "
                f"phys={hex(fmmu_addr)} perm={fmmu_perm} range={fmmu_range if fmmu_range is not None else 'N/A'}")
            print(f"[PU] cmd_match={cmd_match} dg_access_ok={yield dut.slave.pu.dg_access_ok}")
            resp_dg = EtherCATAnalyzer.parse_datagrams(response)
            if resp_dg:
                print(f"[PASS] LRD completed, data={resp_dg[0]['data']}, WKC={resp_dg[0]['wkc']}")
                self.assertEqual(resp_dg[0]['wkc'], 1)
                self.assertEqual(resp_dg[0]['data'], test_data)
            else:
                print("[FAIL] Could not parse LRD response")
                self.fail("LRD response parsing failed")

            print("\n[PASS] Logical read/write test completed")

            dut.sim_done = True
            yield

        generators = {
            "sys": [
                test_sequence(),
                master_tx_generator(dut),
                slave_rx_generator(dut),
            ]
        }

        run_simulation(dut, generators, clocks={"sys": 10},
                      vcd_name="lrd_lwr_test.vcd")

    def test_pcap_replay(self):
        """Replay real EtherCAT capture."""

        pcap_file = os.path.join(os.path.dirname(__file__), "ethercat.cap.gz")
        if not os.path.exists(pcap_file):
            self.skipTest(f"Capture file not found: {pcap_file}")

        print(f"\n[INFO] Loading capture: {pcap_file}")
        packets = PCAPReader.read_packets(pcap_file)
        print(f"[INFO] Loaded {len(packets)} packets")

        master_packets = []
        for pkt in packets:
            if EtherCATAnalyzer.is_from_master(pkt['data']):
                if EtherCATAnalyzer.is_ethercat(pkt['data']):
                    master_packets.append(pkt)

        print(f"[INFO] Found {len(master_packets)} master EtherCAT frames")

        # Analyze first 10
        print("\n[INFO] First 10 datagrams:")
        for i, pkt in enumerate(master_packets[:10]):
            datagrams = EtherCATAnalyzer.parse_datagrams(pkt['data'])
            print(f"Packet {i}: {len(pkt['data'])} bytes, {len(datagrams)} datagrams")
            for dg in datagrams:
                cmd_name = EtherCATAnalyzer.describe_command(dg['cmd'])
                print(f"  {cmd_name} addr={dg['address']:#010x} len={dg['length']} wkc={dg['wkc']}")

        dut = DUT_PDO()
        pcap_out = PCAPWriter("slave_response.pcap")

        def test_sequence():
            for _ in range(30):
                yield

            test_count = min(10, len(master_packets))  # Test first 20 packets
            print(f"\n[TEST] Replaying {test_count} packets...")

            processed = 0
            total_dg = 0
            failed = 0

            for i, pkt in enumerate(master_packets[:test_count]):
                orig_data = EtherCATAnalyzer.strip_padding(pkt['data'])

                print(f"\n[TX] Packet {i}: {len(orig_data)} bytes")
                dut.master.send_packet(orig_data, delay_cycles=50)  # Increased delay

                # Wait for response with timeout
                timeout = 0
                max_timeout = 4000

                while not dut.slave_rx.rx_done and timeout < max_timeout:
                    timeout += 1
                    yield

                if dut.slave_rx.rx_done:
                    response = dut.slave_rx.packets[-1]
                    print(f"[RX] Response {i}: {len(response)} bytes (timeout={timeout})")

                    pcap_out.add_packet(response, timestamp=i * 0.001)

                    response_stripped = EtherCATAnalyzer.strip_padding(response)

                    if len(response_stripped) == len(orig_data):
                        st = analyze_frame_wkc(orig_data, response,
                                            fp_address=(yield dut.slave.fp_address),
                                            verbose=False)   # True — таблица по каждому кадру

                        total_dg += st['n_dg']
                        if st['n_fail'] == 0:
                            processed += 1
                        else:
                            failed += 1
                            for f in st['fails']:
                                print(f"  [WKC-FAIL] {f}")

                    else:
                        print(f"  [WARN] Length mismatch: {len(response_stripped)} != {len(orig_data)}")
                        failed += 1

                    dut.slave_rx.clear()
                else:
                    print(f"  [FAIL] Timeout after {timeout} cycles")
                    failed += 1

            pcap_out.write()
            print(f"\n[INFO] Wrote responses to: slave_response.pcap")

            sent = yield dut.frames_sent
            recv = yield dut.frames_received
            ecat_cnt = yield dut.slave.ethercat_count

            print(f"\n[STATS] Sent: {sent}, Received: {recv}, EtherCAT: {ecat_cnt}")
            print(f"[STATS] Processed: {processed}/{test_count}, Failed: {failed}")
            print(f"\n[STATS] Frames: {processed}/{test_count} ok, {failed} failed")
            print(f"[STATS] Datagrams checked: {total_dg}")

            ecat_cnt = yield dut.slave.ethercat_count
            self.assertGreater(ecat_cnt, 0,
                "Ни один кадр не прошёл FSM — проверяйте ethertype/маршрутизацию")
            self.assertEqual(failed, 0,
                f"{failed} кадров с нарушением WKC-обязательств (см. [WKC-FAIL] выше)")



            self.assertGreater(processed, 0, "No packets processed successfully")
            print(f"\n[PASS] Successfully processed {processed} packets")

            dut.sim_done = True
            yield

        generators = {
            "sys": [
                test_sequence(),
                master_tx_generator(dut),
                slave_rx_generator(dut),
            ]
        }

        run_simulation(dut, generators, clocks={"sys": 10},
                      vcd_name="pdo_test_results.vcd")


if __name__ == "__main__":
    unittest.main(verbosity=2)
