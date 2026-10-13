#!/usr/bin/env python3
"""
Minimal LRD/LWR test with full debugging
"""

import sys
import unittest
import struct
from migen import *
from migen.sim import run_simulation, passive

from litex.soc.interconnect import stream
from liteeth.common import eth_phy_description

sys.path.insert(0, ".")
from ethercat_top import *

# test_lwr_lrd_minimal.py — добавить в начало параметр:
ASYNC = "--async" in sys.argv


# =============================================================================
# Test DUT
# =============================================================================

class DUT(Module):
    def __init__(self):
        self.clock_domains.cd_sys = ClockDomain()
        self.submodules.slave = EtherCATSlave(async_read=False)
        self.sim_done = False

        # Packet queue
        self.tx_packets = []
        self.rx_packets = []
        self.current_rx = []


# =============================================================================
# Generators
# =============================================================================

@passive
def source_gen(dut):
    """Send packets from queue."""
    startup = 10

    while not dut.sim_done:
        if startup > 0:
            startup -= 1
            yield dut.slave.sink.valid.eq(0)
            yield
            continue

        if dut.tx_packets:
            packet = dut.tx_packets.pop(0)
            print(f"[SOURCE] Sending {len(packet)} bytes")

            for i, byte in enumerate(packet):
                yield dut.slave.sink.valid.eq(1)
                yield dut.slave.sink.data.eq(byte)
                yield dut.slave.sink.last_be.eq(1 if i == len(packet)-1 else 0)
                yield dut.slave.sink.error.eq(0)

                # Wait for ready
                ready = yield dut.slave.sink.ready
                while not ready:
                    yield
                    ready = yield dut.slave.sink.ready

                yield  # Clock edge

            # Clear
            yield dut.slave.sink.valid.eq(0)
            yield dut.slave.sink.last_be.eq(0)

            # Inter-frame gap
            for _ in range(20):
                if dut.sim_done:
                    return
                yield

            print(f"[SOURCE] Packet sent")
        else:
            yield dut.slave.sink.valid.eq(0)
            yield


@passive
def sink_gen(dut):
    """Capture packets."""

    while not dut.sim_done:
        yield dut.slave.source.ready.eq(1)
        yield

        valid = yield dut.slave.source.valid
        ready = yield dut.slave.source.ready

        if valid and ready:
            byte = yield dut.slave.source.data
            last = yield dut.slave.source.last_be

            dut.current_rx.append(byte)

            if last:
                dut.rx_packets.append(list(dut.current_rx))
                print(f"[SINK] Captured {len(dut.current_rx)} bytes")
                dut.current_rx = []


# =============================================================================
# Helper functions
# =============================================================================

def build_ethercat_frame(cmd, addr, data):
    """Build valid EtherCAT frame."""
    frame = bytearray(60)

    # Ethernet header
    frame[0:6] = [0xff] * 6        # DST MAC (broadcast)
    frame[6:12] = [0x00, 0x14, 0x4f, 0x23, 0x98, 0xcf]  # SRC MAC
    frame[12:14] = [0x88, 0xa4]    # EtherType = EtherCAT

    # EtherCAT header (bytes 14-15)
    data_len = len(data)
    datagram_len = 10 + data_len + 2  # header + data + WKC
    ec_header = (datagram_len & 0x7FF) | (0x1 << 12)  # Length + Type=1
    frame[14] = ec_header & 0xFF
    frame[15] = (ec_header >> 8) & 0xFF

    # Datagram header (bytes 16-25)
    frame[16] = cmd                # CMD
    frame[17] = 0x00               # IDX
    frame[18:22] = struct.pack('<I', addr)  # ADDR (little-endian)

    len_field = (data_len & 0x7FF)  # LEN (bits 0-10)
    frame[22] = len_field & 0xFF
    frame[23] = (len_field >> 8) & 0xFF

    frame[24:26] = [0x00, 0x00]    # IRQ

    # Data (bytes 26:26+data_len)
    frame[26:26+data_len] = data

    # WKC (bytes 26+data_len:28+data_len)
    frame[26+data_len:28+data_len] = [0x00, 0x00]

    return bytes(frame)


def build_lwr_frame(addr, data):
    """Build LWR (Logical Write) frame."""
    return build_ethercat_frame(0x0B, addr, data)


def build_lrd_frame(addr, length):
    """Build LRD (Logical Read) frame with zero data."""
    return build_ethercat_frame(0x0A, addr, [0x00] * length)



def analyze_frame_detailed(frame):
    """Detailed EtherCAT frame analysis with all fields."""
    if len(frame) < 30:
        return f"FRAME_LEN={len(frame)} (too short)"

    print("\n" + "="*70)
    print("ETHERCAT FRAME DETAILED ANALYSIS")
    print("="*70)

    # Ethernet header
    dst_mac = ':'.join(f'{b:02x}' for b in frame[0:6])
    src_mac = ':'.join(f'{b:02x}' for b in frame[6:12])
    ethertype = (frame[12] << 8) | frame[13]

    print(f"[Ethernet Header]")
    print(f"  DST MAC:    {dst_mac}")
    print(f"  SRC MAC:    {src_mac}")
    print(f"  EtherType:  0x{ethertype:04x}")

    # EtherCAT header
    ec_header = (frame[14] << 8) | frame[15]
    ec_length = ec_header & 0x7FF
    ec_reserved = (ec_header >> 11) & 0x1
    ec_type = (ec_header >> 12) & 0xF

    print(f"\n[EtherCAT Header] (bytes 14-15)")
    print(f"  Length:     {ec_length} (0x{ec_length:03x})")
    print(f"  Reserved:   {ec_reserved}")
    print(f"  Type:       {ec_type} (0x{ec_type:x})")

    # Datagram header
    cmd = frame[16]
    idx = frame[17]
    addr = frame[18] | (frame[19] << 8) | (frame[20] << 16) | (frame[21] << 24)
    length_word = frame[22] | (frame[23] << 8)
    data_len = length_word & 0x7FF
    circulating = (length_word >> 14) & 0x1
    multiple = (length_word >> 13) & 0x1
    reserved_len = (length_word >> 11) & 0x3

    irq = frame[24] | (frame[25] << 8)

    cmd_names = {
        0x01: "APRD", 0x02: "APWR", 0x03: "APRW",
        0x04: "FPRD", 0x05: "FPWR", 0x06: "FPRW",
        0x07: "BRD",  0x08: "BWR",  0x09: "BRW",
        0x0A: "LRD",  0x0B: "LWR",  0x0C: "LRW",
        0x0D: "ARMW", 0x0E: "FRMW"
    }

    cmd_name = cmd_names.get(cmd, f"UNKNOWN")

    print(f"\n[Datagram Header] (bytes 16-25)")
    print(f"  CMD:        0x{cmd:02x} ({cmd_name})")
    print(f"  IDX:        0x{idx:02x}")
    print(f"  ADDR:       0x{addr:08x}")
    print(f"  LEN:        {data_len} (0x{data_len:03x})")
    print(f"    C-bit:    {circulating} (circulating)")
    print(f"    M-bit:    {multiple} (multiple)")
    print(f"    Reserved: {reserved_len}")
    print(f"  IRQ:        0x{irq:04x}")

    # Data
    data_start = 26
    data_end = data_start + data_len

    if data_end <= len(frame):
        data = frame[data_start:data_end]
        print(f"\n[Data] (bytes {data_start}-{data_end-1}, {len(data)} bytes)")

        # Print in rows of 16 bytes
        for i in range(0, len(data), 16):
            chunk = data[i:i+16]
            hex_str = ' '.join(f'{b:02x}' for b in chunk)
            ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
            print(f"  {data_start+i:04d}: {hex_str:<48s} {ascii_str}")

        # Also decimal for test data
        if len(data) <= 16:
            print(f"  Decimal: {list(data)}")

    # WKC
    wkc_pos = data_end
    if wkc_pos + 1 < len(frame):
        wkc = frame[wkc_pos] | (frame[wkc_pos + 1] << 8)
        print(f"\n[Working Counter] (bytes {wkc_pos}-{wkc_pos+1})")
        print(f"  WKC:        {wkc} (0x{wkc:04x})")
        print(f"  Bytes:      [0x{frame[wkc_pos]:02x}, 0x{frame[wkc_pos+1]:02x}]")

    # FCS (if present)
    fcs_pos = wkc_pos + 2
    if fcs_pos + 3 < len(frame):
        fcs = frame[fcs_pos] | (frame[fcs_pos+1] << 8) | (frame[fcs_pos+2] << 16) | (frame[fcs_pos+3] << 24)
        print(f"\n[Frame Check Sequence] (bytes {fcs_pos}-{fcs_pos+3})")
        print(f"  FCS:        0x{fcs:08x}")

    print("="*70 + "\n")

    return f"CMD={cmd_name} IDX={idx:#04x} ADDR={addr:#010x} LEN={data_len} WKC={wkc}"


def analyze_frame(frame):
    """Quick summary for inline display."""
    if len(frame) < 30:
        return f"FRAME_LEN={len(frame)} (too short)"

    cmd = frame[16]
    idx = frame[17]
    addr = frame[18] | (frame[19] << 8) | (frame[20] << 16) | (frame[21] << 24)
    length = frame[22] | (frame[23] << 8)
    data_len = length & 0x7FF

    wkc_pos = 26 + data_len
    if wkc_pos + 1 < len(frame):
        wkc = frame[wkc_pos] | (frame[wkc_pos + 1] << 8)
    else:
        wkc = 0xFFFF

    cmd_names = {
        0x01: "APRD", 0x02: "APWR", 0x03: "APRW",
        0x04: "FPRD", 0x05: "FPWR", 0x06: "FPRW",
        0x07: "BRD",  0x08: "BWR",  0x09: "BRW",
        0x0A: "LRD",  0x0B: "LWR",  0x0C: "LRW",
        0x0D: "ARMW", 0x0E: "FRMW"
    }

    cmd_name = cmd_names.get(cmd, f"UNK({cmd:#04x})")

    return f"CMD={cmd_name} IDX={idx:#04x} ADDR={addr:#010x} LEN={data_len} WKC={wkc} FRAME_LEN={len(frame)}"



# =============================================================================
# Test Case
# =============================================================================

class TestLRDLWR(unittest.TestCase):

    def test_simple_lwr(self):
        """Test single LWR command."""
        dut = DUT()

        def test_sequence():
            print("\n" + "="*70)
            print("TEST: Simple LWR")
            print("="*70)

            for _ in range(20):
                yield

            test_data = [0xAA, 0xBB, 0xCC, 0xDD]
            frame = build_lwr_frame(0x1100, test_data)

            print(f"\n[TX] {analyze_frame_detailed(frame)}")
            print(f"[TX] Hex: {' '.join(f'{b:02x}' for b in frame[:40])}...")

            # CRITICAL: Calculate expected WKC position manually
            # Frame structure:
            # 0-5:   Dst MAC
            # 6-11:  Src MAC
            # 12-13: EtherType (0x88A4)
            # 14-15: EtherCAT header (length field)
            # 16:    CMD
            # 17:    IDX
            # 18-21: Address (4 bytes)
            # 22-23: Length field
            # 24-25: Reserved
            # 26-29: Data (4 bytes for this test)
            # 30-31: WKC ← EXPECTED POSITION

            expected_wkc_pos = 30
            print(f"[DEBUG] Expected WKC position: byte {expected_wkc_pos}")
            print(f"[DEBUG] WKC bytes in TX frame: [{frame[30]:#04x}, {frame[31]:#04x}]")

            dut.tx_packets.append(frame)

            # Wait a bit for processing
            for _ in range(100):
                yield

            # Read debug signals from FPGA
            #byte_count = yield dut.slave.pu.debug_byte_count
            dg_len = yield dut.slave.pu.dbg_dg_len
            dg_wkc_pos = yield dut.slave.pu.dbg_dg_wkc_pos
            cmd_match = yield dut.slave.pu.dbg_cmd_match

            print(f"\n[FPGA DEBUG]")
            #print(f"  byte_count    = {byte_count}")
            print(f"  dg_len        = {dg_len}")
            print(f"  dg_wkc_pos    = {dg_wkc_pos} (expected {expected_wkc_pos})")
            print(f"  cmd_match     = {cmd_match}")

            # ===================================================================
            # ВСТАВЬТЕ СЮДА: Monitor DURING transmission
            # ===================================================================
            print(f"\n[MONITOR] Watching byte_count during transmission:")

            for cycle in range(200):
                yield

                sink_valid = yield dut.slave.sink.valid
                sink_ready = yield dut.slave.sink.ready
                sink_data = yield dut.slave.sink.data
                source_valid = yield dut.slave.source.valid
                source_data = yield dut.slave.source.data

                #byte_count = yield dut.slave.pu.debug_byte_count
                dg_wkc_pos = yield dut.slave.pu.dbg_dg_wkc_pos
                cmd_match = yield dut.slave.pu.debug_cmd_match

                if sink_valid and sink_ready:
                    match_marker = " ← WKC!" if (byte_count == dg_wkc_pos or byte_count == dg_wkc_pos + 1) and cmd_match else ""
                    print(f"  Cycle {cycle:3d}: sink={sink_data:#04x}, source={source_data:#04x}, wkc_pos={dg_wkc_pos}, match={cmd_match}{match_marker}")

                if len(dut.rx_packets) > 0:
                    print(f"\n[MONITOR] Frame complete at cycle {cycle}")
                    break
            # ===================================================================
            # КОНЕЦ ВСТАВКИ
            # ===================================================================

            # Wait for response
            timeout = 0
            while len(dut.rx_packets) == 0 and timeout < 3000:
                timeout += 1
                yield

            if len(dut.rx_packets) > 0:
                response = dut.rx_packets[0]
                print(f"\n[RX] {analyze_frame_detailed(response)}")
                print(f"[RX] Hex: {' '.join(f'{b:02x}' for b in response[:40])}...")
                print(f"[RX] WKC bytes: [{response[30]:#04x}, {response[31]:#04x}]")

                # Check if WKC position matches
                if dg_wkc_pos != expected_wkc_pos:
                    print(f"\n[ERROR] WKC position mismatch!")
                    print(f"  FPGA calculated: {dg_wkc_pos}")
                    print(f"  Expected:        {expected_wkc_pos}")
                    print(f"  Difference:      {dg_wkc_pos - expected_wkc_pos}")

                # Analyze what actually changed
                print(f"\n[DIFF] Bytes that changed:")
                for i in range(min(len(frame), len(response))):
                    if frame[i] != response[i]:
                        print(f"  Byte {i}: {frame[i]:#04x} -> {response[i]:#04x}")

                self.assertEqual(len(response), len(frame))

                wkc_pos = 30
                rx_wkc = response[wkc_pos] | (response[wkc_pos + 1] << 8)
                tx_wkc = frame[wkc_pos] | (frame[wkc_pos + 1] << 8)

                print(f"\n[WKC] TX={tx_wkc} RX={rx_wkc} (expected {tx_wkc + 1})")
                self.assertEqual(rx_wkc, tx_wkc + 1, "WKC not incremented")

                print("\n[PASS] LWR test successful")
            else:
                self.fail("No response received")

            dut.sim_done = True
            yield



            generators = {
                "sys": [
                    test_sequence(),
                    source_gen(dut),
                    sink_gen(dut),
                ]
            }

            run_simulation(dut, generators, clocks={"sys": 10},
                        vcd_name="lwr_test.vcd")


    def test_lwr_then_lrd(self):
        """Test LWR followed by LRD."""
        dut = DUT()

        def test_sequence():
            print("\n" + "="*70)
            print("TEST: LWR then LRD")
            print("="*70)

            for _ in range(20):
                yield

            # ============================================================
            # Phase 1: LWR
            # ============================================================
            test_data = [0x11, 0x22, 0x33, 0x44]
            frame_wr = build_lwr_frame(0x1100, test_data)

            print(f"[TX] {analyze_frame_detailed(frame_wr)}")


            print(f"\n[TX] {analyze_frame_detailed(frame_wr)}")
            print(f"[TX] Hex: {' '.join(f'{b:02x}' for b in frame_wr[:40])}...")

            # CRITICAL: Calculate expected WKC position manually
            # Frame structure:
            # 0-5:   Dst MAC
            # 6-11:  Src MAC
            # 12-13: EtherType (0x88A4)
            # 14-15: EtherCAT header (length field)
            # 16:    CMD
            # 17:    IDX
            # 18-21: Address (4 bytes)
            # 22-23: Length field
            # 24-25: Reserved
            # 26-29: Data (4 bytes for this test)
            # 30-31: WKC ← EXPECTED POSITION

            expected_wkc_pos = 30
            print(f"[DEBUG] Expected WKC position: byte {expected_wkc_pos}")
            print(f"[DEBUG] WKC bytes in TX frame: [{frame_wr[30]:#04x}, {frame_wr[31]:#04x}]")

            dut.tx_packets.append(frame_wr)


            # Wait a bit for processing
            for _ in range(100):
                yield

            # Read debug signals from FPGA
            #byte_count = yield dut.slave.pu.debug_byte_count
            dg_len = yield dut.slave.pu.dbg_dg_len
            #dg_wkc_pos = yield dut.slave.pu.debg_dg_wkc_pos
            cmd_match = yield dut.slave.pu.dbg_cmd_match

            print(f"\n[FPGA DEBUG]")
            #print(f"  byte_count    = {byte_count}")
            print(f"  dg_len        = {dg_len}")
            #print(f"  dg_wkc_pos    = {dg_wkc_pos} (expected {expected_wkc_pos})")
            print(f"  cmd_match     = {cmd_match}")

            ch = dut.slave.mem.fmmu.channels[0]   # поправь имя, если поле называется иначе
            print("\n[FMMU CH0]")
            print(f"  log_start  = 0x{(yield ch.logical_start_addr):08X}")
            print(f"  length     = {(yield ch.length)}")
            print(f"  phys_start = 0x{(yield ch.physical_start_addr):04X}")
            print(f"  type       = 0x{(yield ch.operation_type):02X}")
            print(f"  enable     = {(yield ch.activate)}")
            print(f"\n[PHASE 1] LWR")

            timeout = 0
            while len(dut.rx_packets) == 0 and timeout < 3000:
                timeout += 1
                yield

            if len(dut.rx_packets) > 0:
                response = dut.rx_packets[0]
                print(f"[RX] {analyze_frame_detailed(response)}")
                print(f"[TX] Hex: {' '.join(f'{b:02x}' for b in response[:40])}...")

                wkc_pos = 26 + 4
                rx_wkc = (response[wkc_pos]) | (response[wkc_pos + 1]  <<8)
                print(f"[WKC] {rx_wkc} (timeout={timeout})")

                self.assertEqual(rx_wkc, 1, "LWR WKC should be 1")
            else:
                self.fail("LWR: No response")

            dut.rx_packets.clear()

            # Wait between frames
            for _ in range(50):
                yield

            # ============================================================
            # Phase 2: LRD
            # ============================================================
            frame_rd = build_lrd_frame(0x1100, 4)

            print(f"\n[PHASE 2] LRD")
            print(f"[TX] {analyze_frame_detailed(frame_rd)}")

            dut.tx_packets.append(frame_rd)

            timeout = 0
            while len(dut.rx_packets) == 0 and timeout < 3000:
                timeout += 1
                yield
            ch = dut.slave.mem.fmmu.channels[0]   # поправь имя, если поле называется иначе
            print("\n[FMMU CH0]")
            print(f"  log_start  = 0x{(yield ch.logical_start_addr):08X}")
            print(f"  length     = {(yield ch.length)}")
            print(f"  phys_start = 0x{(yield ch.physical_start_addr):04X}")
            print(f"  type       = 0x{(yield ch.operation_type):02X}")
            print(f"  enable     = {(yield ch.activate)}")
            print(f"\n[PHASE 1] LWR")
            if len(dut.rx_packets) > 0:
                response = dut.rx_packets[0]
                print(f"[RX] {analyze_frame_detailed(response)}")

                # Check data
                rx_data = response[26:30]
                print(f"[DATA] TX={test_data} RX={list(rx_data)}")

                self.assertEqual(list(rx_data), test_data,
                               "Data mismatch in LRD response")

                # Check WKC
                wkc_pos = 26 + 4
                rx_wkc = response[wkc_pos] | (response[wkc_pos + 1] << 8)
                print(f"[WKC] {rx_wkc}")

                self.assertEqual(rx_wkc, 1, "LRD WKC should be 1")

                print("\n[PASS] LWR+LRD test successful")
            else:
                self.fail("LRD: No response")

            dut.sim_done = True
            yield

        generators = {
            "sys": [
                test_sequence(),
                source_gen(dut),
                sink_gen(dut),
            ]
        }

        run_simulation(dut, generators, clocks={"sys": 10},
                      vcd_name="lwr_lrd_test.vcd")
    def test_mem_latency():
        dut = ESCMemory()
        def gen():
            yield dut.address_write.eq(0x1100)
            yield dut.data_write.eq(0xA5)
            yield dut.write_en.eq(1)
            yield
            yield dut.write_en.eq(0)
            yield; yield; yield
            yield dut.address_read.eq(0x1100)
            for i in range(5):
                yield
                print(f"cycle {i}: data_read = 0x{(yield dut.data_read):02x}")
        run_simulation(dut, gen(), vcd_name="memlat.vcd")


if __name__ == "__main__":
    unittest.main(verbosity=2)
