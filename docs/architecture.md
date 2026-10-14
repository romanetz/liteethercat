
## 5. Phase 1 Breakdown

### Task 1.1 — Remove CoE from ESI
**Why:** IgH expects CoE, which is not implemented → error flag.
**Files:** `colorlight4fmmu.xml`, `siitool/examples/colorlight4fmmu.bin`
**Steps:**
- [ ] Remove `<Mailbox>` block from ESI
- [ ] Verify no CoE attributes in `<General>`
- [ ] Regenerate `.bin` via `siitool`
- [ ] Rebuild bitstream
- [ ] `ethercat slaves -v` → `Flag: -` (no error)
**Acceptance:** `dmesg` clean, error flag cleared.

### Task 1.2 — AL FSM: PREOP → SAFEOP → OP
**Why:** process data requires SAFEOP/OP.
**Files:** `ethercat_al.py`
**Steps:**
- [ ] Add `sm3_activated` check for SAFEOP
- [ ] Add `sm2_activated` check for OP
- [ ] Handle `AL Control = 0x04` → SAFEOP
- [ ] Handle `AL Control = 0x08` → OP
- [ ] On error, set `AL Status Code`
**Acceptance:** `ethercat slaves -v` → `State: SAFEOP`, then `OP`.

### Task 1.3 — Quadrature Decoder
**Why:** encoders must count position.
**Files:** new `encoder_bank.py`
**Steps:**
- [ ] Define interface (A, B, Idx per encoder)
- [ ] FSM to detect A/B edges
- [ ] Determine direction (A leads B or vice versa)
- [ ] 32-bit up/down counter
- [ ] Handle Idx (reset or latch)
- [ ] Testbench: feed quadrature, verify count
**Acceptance:** testbench passes, counter counts correctly.

### Task 1.4 — Encoder → DPRAM
**Why:** data must land in SM3.
**Files:** `ethercat_soc.py`, `ethercat_sm.py`
**Steps:**
- [ ] Connect `encoder_bank` to `ESCMemory`
- [ ] Write 4×32 bit at `0x1104..0x1123` on each sys_clk
- [ ] Verify with LiteScope: `mem.data_read` = encoder value
- [ ] Verify with `ethercat reg_read 0x1104` (should return counter bytes)
**Acceptance:** rotate encoder → `ethercat reg_read` shows change.

### Task 1.5 — FMMU inputs configuration
**Why:** IgH must know where SM3 data goes.
**Files:** `colorlight4fmmu.xml`, `ethercat_fmmu.py`
**Steps:**
- [ ] Verify FMMU inputs in ESI matches SM3
- [ ] Ensure on SAFEOP transition IgH writes FMMU config
- [ ] Verify with LiteScope: `fmmu.channels[1].physical_start_addr = 0x1104`
**Acceptance:** FMMU configured, `ethercat slaves` → SAFEOP.

### Task 1.6 — LinuxCNC HAL configuration
**Why:** data must reach HAL pins.
**Files:** new `linuxcnc/ethercat-conf.xml`, `linuxcnc/esc.hal`
**Steps:**
- [ ] Install `linuxcnc-ethercat`
- [ ] Write `ethercat-conf.xml` (slave → alias mapping)
- [ ] Write `.hal` with lcec load
- [ ] Run `linuxcnc`, open HAL Scope
- [ ] Rotate encoder, watch pin change
**Acceptance:** HAL pin changes when encoder rotates.

### Task 1.7 — Video and publication
**Steps:**
- [ ] Record 30-60 sec video
- [ ] Show `ethercat slaves -v` (OP)
- [ ] Show rotating encoder
- [ ] Show HAL Scope
- [ ] Upload to YouTube
- [ ] Update README with link
- [ ] Post on LinuxCNC forum
- [ ] Message Alexey
**Acceptance:** video link in README, post published.

## 6. Critical Risks

| Risk | Probability | Mitigation |
|------|-------------|-----------|
| BRAM exhausted (DP16KD) | high | Reduce LiteScope to depth=512 |
| IgH won't reach SAFEOP without CoE | medium | Check ESI, ensure `<RxPdo Fixed>` |
| Encoders unstable | low | Debounce + testbench |
| HAL doesn't find pins | medium | Check `lcec_conf` logs |

## 7. Out of Scope for Phase 1

- CoE / mailbox / SDO
- VexRiscv
- Distributed Clocks
- Ring redundancy with two slaves
- ETG conformance
- Third-party masters (TwinCAT, Omron)

These belong to Phase 2 and 3.
