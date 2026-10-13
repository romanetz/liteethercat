
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
