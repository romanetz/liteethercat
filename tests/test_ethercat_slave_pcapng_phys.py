#!/usr/bin/env python3
"""
Multi-domain EtherCAT test with PHY simulation and CRC32 handling.
"""

import os
import sys
import gzip
import struct
import unittest
import zlib
from migen import *
from migen.sim import run_simulation, passive

from litex.soc.interconnect import stream
from liteeth.common import eth_phy_description
from liteeth.mac.crc import LiteEthMACCRC32Inserter, LiteEthMACCRC32Checker

sys.path.insert(0, os.path.dirname(__file__))
from ethercat_top import *
from ethercat_dl_router import EtherCATLoopbackRouter
from ethercat_pu import state_map

STATE_MAP_REV = {v: k for k, v in state_map.items()}


# =============================================================================
# Helpers (unchanged)
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


def wkc_expectation(dg, fp_address=0):
    cmd = dg['cmd']
    adp = dg['address'] & 0xFFFF
    ado = (dg['address'] >> 16) & 0xFFFF

    if cmd == 0x00:
        return False, "NOP"
    if cmd in (0x07, 0x08, 0x09):
        return True, f"broadcast reg={ado:#06x}"
    if cmd in (0x01, 0x02, 0x03):
        return (adp == 0x0000), f"auto-inc ADP={adp:#06x}"
    if cmd in (0x04, 0x05, 0x06):
        return (adp == fp_address), f"station {adp:#06x} vs fp {fp_address:#06x}"
    if cmd in (0x0A, 0x0B, 0x0C):
        return None, f"logical @{dg['address']:#010x}"
    return None, f"cmd {cmd:#04x}"


def analyze_frame_wkc(orig_packet, resp_packet, fp_address=0, verbose=False):
    orig_dgs = EtherCATAnalyzer.parse_datagrams(orig_packet)
    resp_dgs = EtherCATAnalyzer.parse_datagrams(resp_packet)

    stats = {'n_dg': len(orig_dgs), 'n_fail': 0, 'fails': []}

    lines = []
    if len(orig_dgs) != len(resp_dgs):
        stats['n_fail'] += 1
        stats['fails'].append(f"структура: {len(orig_dgs)} -> {len(resp_dgs)}")
        verbose = True

    for i, (o, r) in enumerate(zip(orig_dgs, resp_dgs)):
        name = EtherCATAnalyzer.describe_command(o['cmd'])
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

    orig_list = [d['wkc'] for d in orig_dgs]
    resp_list = [d['wkc'] for d in resp_dgs]
    print(f"  WKC in={orig_list} out={resp_list}")

    if verbose:
        print("\n".join(lines))

    return stats


# =============================================================================
# Multi-Domain DUT with PHY simulation
# =============================================================================

class PHYStub(Module):
    """
    Упрощённая заглушка PHY без реального RGMII.
    Предоставляет stream endpoints в указанных clock domains.
    """
    def __init__(self, rx_cd="eth_rx", tx_cd="eth_tx"):
        self.source = stream.Endpoint(eth_phy_description(8))  # RX stream (в rx_cd)
        self.sink = stream.Endpoint(eth_phy_description(8))    # TX stream (в tx_cd)

        # Для совместимости с кодом, который обращается к cd
        self.cd_rx = ClockDomain(rx_cd)
        self.cd_tx = ClockDomain(tx_cd)


class DUT_MultiDomain(Module):
    """
    DUT с тремя clock domains и роутером, имитирующим реальную топологию.
    """
    def __init__(self):
        # Три домена
        self.clock_domains.cd_sys = ClockDomain()
        self.clock_domains.cd_ethphy0_eth_rx = ClockDomain()
        self.clock_domains.cd_ethphy0_eth_tx = ClockDomain()
        self.clock_domains.cd_ethphy1_eth_rx = ClockDomain()
        self.clock_domains.cd_ethphy1_eth_tx = ClockDomain()

        self.sim_done = False

        # PHY заглушки
        self.submodules.phy0 = ClockDomainsRenamer({
            "eth_rx": "ethphy0_eth_rx",
            "eth_tx": "ethphy0_eth_tx"
        })(PHYStub())

        self.submodules.phy1 = ClockDomainsRenamer({
            "eth_rx": "ethphy1_eth_rx",
            "eth_tx": "ethphy1_eth_tx"
        })(PHYStub())

        # ESC core (в sys domain)
        self.submodules.esc = EtherCATSlave()

        # Router с AsyncFIFO
        self.submodules.router = EtherCATLoopbackRouter(
            self.phy0,
            self.phy1,
            self.esc
        )

        # Всегда активные линки для теста
        self.comb += [
            self.router.link0.eq(1),
            self.router.link1.eq(1),
        ]

        # Счётчики кадров
        self.frames_sent = Signal(32)
        self.frames_received = Signal(32)

        self.sync.ethphy0_eth_rx += [
            If(self.phy0.source.valid & self.phy0.source.ready & self.phy0.source.last_be,
                self.frames_sent.eq(self.frames_sent + 1)
            )
        ]

        self.sync.ethphy1_eth_tx += [
            If(self.phy1.sink.valid & self.phy1.sink.ready & self.phy1.sink.last_be,
                self.frames_received.eq(self.frames_received + 1)
            )
        ]


# =============================================================================
# Generators
# =============================================================================

class PacketQueue:
    """Thread-safe packet queue для передачи между generators."""
    def __init__(self):
        self.queue = []

    def put(self, packet):
        self.queue.append(list(packet))

    def get(self):
        if self.queue:
            return self.queue.pop(0)
        return None

    def empty(self):
        return len(self.queue) == 0


def add_ethernet_fcs(frame):
    """Добавляет FCS (CRC32) к Ethernet-кадру."""
    crc = zlib.crc32(bytes(frame)) & 0xFFFFFFFF
    return frame + [
        crc & 0xFF,
        (crc >> 8) & 0xFF,
        (crc >> 16) & 0xFF,
        (crc >> 24) & 0xFF
    ]


@passive
def master_tx_gen(dut, tx_queue):
    """Отправляет кадры в PHY0 source (RX domain)."""
    for _ in range(20):
        yield

    while not dut.sim_done:
        packet = tx_queue.get()

        if packet:
            # Добавляем FCS
            packet_with_fcs = add_ethernet_fcs(packet)

            print(f"[TX] Sending {len(packet_with_fcs)} bytes (with FCS)")

            for i, byte in enumerate(packet_with_fcs):
                yield dut.phy0.source.valid.eq(1)
                yield dut.phy0.source.data.eq(byte)
                yield dut.phy0.source.last_be.eq(1 if i == len(packet_with_fcs)-1 else 0)
                yield dut.phy0.source.error.eq(0)

                while True:
                    yield
                    ready = yield dut.phy0.source.ready
                    if ready:
                        break

            yield dut.phy0.source.valid.eq(0)
            yield dut.phy0.source.last_be.eq(0)

            # Inter-frame gap
            for _ in range(20):
                if dut.sim_done:
                    return
                yield
        else:
            yield dut.phy0.source.valid.eq(0)
            yield


@passive
def slave_rx_gen(dut, rx_queue):
    """Принимает кадры из PHY1 sink (TX domain)."""
    frame = []

    while not dut.sim_done:
        yield dut.phy1.sink.ready.eq(1)
        yield

        valid = yield dut.phy1.sink.valid
        ready = yield dut.phy1.sink.ready

        if valid and ready:
            byte = yield dut.phy1.sink.data
            last = yield dut.phy1.sink.last_be

            frame.append(byte)

            if last:
                # Проверяем и удаляем FCS
                if len(frame) >= 4:
                    payload = frame[:-4]
                    fcs_received = int.from_bytes(bytes(frame[-4:]), 'little')
                    fcs_calculated = zlib.crc32(bytes(payload)) & 0xFFFFFFFF

                    if fcs_received == fcs_calculated:
                        print(f"[RX] Received {len(payload)} bytes (FCS OK)")
                        rx_queue.put(payload)
                    else:
                        print(f"[RX] FCS ERROR: got {fcs_received:#010x}, expected {fcs_calculated:#010x}")
                else:
                    print(f"[RX] Frame too short: {len(frame)} bytes")

                frame = []


@passive
def state_monitor(dut, log_file):
    """Логирует состояние FSM в sys domain."""
    with open(log_file, 'w') as f:
        f.write("cycle,state,cmd,addr,cmd_match,sink_valid,sink_data,source_valid,tx_data\n")

        cycle = 0
        while not dut.sim_done and cycle < 100000:
            yield

            state = yield dut.esc.pu.dbg_state
            cmd = yield dut.esc.pu.dbg_cmd
            addr = yield dut.esc.pu.dbg_addr
            cmd_match = yield dut.esc.pu.dbg_cmd_match
            sink_valid = yield dut.esc.sink.valid
            sink_data = yield dut.esc.sink.data
            source_valid = yield dut.esc.source.valid
            tx_data = yield dut.esc.source.data

            state_name = STATE_MAP_REV.get(state, f"UNK_{state}")

            f.write(f"{cycle},{state_name},0x{cmd:02x},"
                   f"0x{addr:08x},{cmd_match},{sink_valid},0x{sink_data:02x},"
                   f"{source_valid},0x{tx_data:02x}\n")

            cycle += 1


# =============================================================================
# Test Cases
# =============================================================================

class TestEtherCATMultiDomain(unittest.TestCase):

    def test_pcap_replay_multidomain(self):
        """Replay pcap через multi-domain архитектуру."""

        pcap_file = os.path.join(os.path.dirname(__file__), "ethercat.cap.gz")
        if not os.path.exists(pcap_file):
            self.skipTest(f"Capture file not found: {pcap_file}")

        print(f"\n[INFO] Loading capture: {pcap_file}")
        packets = PCAPReader.read_packets(pcap_file)

        master_packets = []
        for pkt in packets:
            if EtherCATAnalyzer.is_from_master(pkt['data']):
                if EtherCATAnalyzer.is_ethercat(pkt['data']):
                    master_packets.append(pkt)

        print(f"[INFO] Found {len(master_packets)} master EtherCAT frames")

        dut = DUT_MultiDomain()
        tx_queue = PacketQueue()
        rx_queue = PacketQueue()
        pcap_out = PCAPWriter("slave_response_multidomain.pcap")

        def test_sequence():
            for _ in range(50):
                yield

            test_count = min(20, len(master_packets))
            print(f"\n[TEST] Replaying {test_count} packets...")

            processed = 0
            failed = 0
            total_dg = 0

            for i, pkt in enumerate(master_packets[:test_count]):
                orig_data = EtherCATAnalyzer.strip_padding(pkt['data'])

                print(f"\n[TX] Packet {i}: {len(orig_data)} bytes")
                tx_queue.put(orig_data)

                # Ждём ответа
                timeout = 0
                max_timeout = 5000

                while rx_queue.empty() and timeout < max_timeout and not dut.sim_done:
                    timeout += 1
                    yield

                if not rx_queue.empty():
                    response = rx_queue.get()
                    print(f"[RX] Response {i}: {len(response)} bytes (timeout={timeout})")

                    pcap_out.add_packet(response, timestamp=i * 0.001)

                    response_stripped = EtherCATAnalyzer.strip_padding(response)

                    if len(response_stripped) == len(orig_data):
                        st = analyze_frame_wkc(orig_data, response,
                                            fp_address=(yield dut.esc.fp_address),
                                            verbose=False)

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
                else:
                    print(f"  [FAIL] Timeout after {timeout} cycles")
                    failed += 1

                # Inter-frame delay
                for _ in range(100):
                    yield

            pcap_out.write()
            print(f"\n[INFO] Wrote responses to: slave_response_multidomain.pcap")

            sent = yield dut.frames_sent
            recv = yield dut.frames_received
            ecat_cnt = yield dut.esc.ethercat_count

            print(f"\n[STATS] Frames: {processed}/{test_count} ok, {failed} failed")
            print(f"[STATS] Sent: {sent}, Received: {recv}, EtherCAT: {ecat_cnt}")
            print(f"[STATS] Datagrams checked: {total_dg}")

            self.assertGreater(ecat_cnt, 0, "Ни один кадр не прошёл FSM")
            self.assertEqual(failed, 0, f"{failed} кадров с нарушением WKC")

            dut.sim_done = True
            yield

        generators = {
            "sys": [
                test_sequence(),
                state_monitor(dut, "fsm_trace_multidomain.csv")
            ],
            "ethphy0_eth_rx": [
                master_tx_gen(dut, tx_queue)
            ],
            "ethphy1_eth_tx": [
                slave_rx_gen(dut, rx_queue)
            ]
        }

        run_simulation(dut, generators,
                      clocks={
                          "sys": 20,                  # 50 MHz
                          "ethphy0_eth_rx": 40,       # 25 MHz
                          "ethphy0_eth_tx": 40,
                          "ethphy1_eth_rx": 40,
                          "ethphy1_eth_tx": 40
                      },
                      vcd_name="multidomain_test.vcd")


if __name__ == "__main__":
    unittest.main(verbosity=2)
