#!/usr/bin/env python3
from migen import *
from ethercat_sm import *
# =============================================================================
# Тесты (без изменений, кроме ожиданий)
# =============================================================================
def _test_3buffer_rxpdo(dut):
    """
    Тест 1: 3-buffer RxPDO (сеть пишет, процессор читает).

    Ротация указателей по кадрам:
      сброс:    ecat=0, proc=1, shadow=2
      кадр 1 →  пишется в buf0 (0x1400), затем ecat=2, shadow=0
      кадр 2 →  пишется в buf2 (0x3000), затем ecat=0, shadow=2
      процессор получает shadow=2 (0x3000) — данные кадра 2, самые свежие.
    """
    print("\n=== ТЕСТ 1: 3-BUFFER RxPDO ===")
    BASE = 0x0810

    def write_reg(offset, value):
        yield from sm_write_ecat_reg(dut, BASE, offset, value)

    def read_reg(offset):
        return (yield from sm_read_ecat_reg(dut, BASE, offset))

    yield from write_reg(0x00, 0x00)
    yield from write_reg(0x01, 0x14)
    yield from write_reg(0x02, 0x40)
    yield from write_reg(0x03, 0x00)
    yield from write_reg(0x04, 0x04)   # 3-Buffer + Write (RxPDO)
    yield from write_reg(0x06, 0x01)
    yield
    print("[Config] SM2: база=0x1400, длина=64, режим=3-Buffer RxPDO")

    # --- Кадр 1: ecat=0 → Buffer 0 ---
    yield dut.ecat_addr.eq(0x1405)
    yield dut.ecat_data_phase.eq(1)
    yield dut.ecat_write_en.eq(1)
    yield
    addr = yield dut.ecat_bram_addr
    print(f"[Кадр 1] Сеть 0x1405 → BRAM {hex(addr)} (ожидалось 0x1405)")
    assert addr == 0x1405, f"Кадр 1 должен идти в Buffer 0, получен {hex(addr)}"

    yield dut.ecat_last_data_byte.eq(1)
    yield
    yield dut.ecat_last_data_byte.eq(0)
    yield dut.ecat_write_en.eq(0)
    yield dut.ecat_data_phase.eq(0)
    yield

    # --- Кадр 2: ecat=2 → Shadow Buffer 2 ---
    yield dut.ecat_addr.eq(0x1405)
    yield dut.ecat_data_phase.eq(1)
    yield dut.ecat_write_en.eq(1)
    yield
    addr = yield dut.ecat_bram_addr
    print(f"[Кадр 2] Сеть 0x1405 → BRAM {hex(addr)} (ожидалось 0x3005)")
    assert addr == 0x3005, f"Ротация не сработала, получен {hex(addr)}"

    yield dut.ecat_last_data_byte.eq(1)
    yield
    yield dut.ecat_last_data_byte.eq(0)
    yield dut.ecat_write_en.eq(0)
    yield dut.ecat_data_phase.eq(0)
    yield

    # --- Процессор читает первый байт: комбинаторное предсказание отдаёт shadow=2 ---
    yield dut.proc_addr.eq(0x1400)
    yield dut.proc_read_en.eq(1)
    yield
    proc_addr = yield dut.proc_bram_addr
    print(f"[Процессор] Чтение 0x1400 → BRAM {hex(proc_addr)} (ожидалось 0x3000)")
    assert proc_addr == 0x3000, \
        f"Процессор должен получить свежий буфер (кадр 2), получен {hex(proc_addr)}"

    # --- Второй байт: указатель уже обновлён синхронно, буфер тот же ---
    yield dut.proc_addr.eq(0x1401)
    yield
    proc_addr = yield dut.proc_bram_addr
    print(f"[Процессор] Чтение 0x1401 → BRAM {hex(proc_addr)} (ожидалось 0x3001)")
    assert proc_addr == 0x3001, \
        f"Буфер не должен смениться посреди чтения, получен {hex(proc_addr)}"

    yield dut.proc_read_en.eq(0)
    yield

    print("=== ТЕСТ 1 ПРОЙДЕН ===\n")



def _test_3buffer_txpdo(dut):
    """
    Тест 2: 3-buffer TxPDO (процессор пишет, сеть читает).

    РАСШИРЕНО: многобайтовая запись с завершением по proc_last_data_byte.
    """
    print("\n=== ТЕСТ 2: 3-BUFFER TxPDO ===")
    BASE = 0x0818

    def write_reg(offset, value):
        yield from sm_write_ecat_reg(dut, BASE, offset, value)

    def read_reg(offset):
        return (yield from sm_read_ecat_reg(dut, BASE, offset))

    yield from write_reg(0x00, 0x00)
    yield from write_reg(0x01, 0x16)
    yield from write_reg(0x02, 0x20)
    yield from write_reg(0x03, 0x00)
    yield from write_reg(0x04, 0x00)   # 3-Buffer, direction=Read (TxPDO)
    yield from write_reg(0x06, 0x01)
    yield
    print("[Config] SM3: база=0x1600, длина=32, режим=3-Buffer TxPDO")

    # --- Процессор пишет 4 байта: только последний взводит last_data_byte ---
    for i, offset in enumerate([0x00, 0x04, 0x08, 0x0C]):
        yield dut.proc_addr.eq(0x1600 + offset)
        yield dut.proc_write_en.eq(1)
        yield dut.proc_data_valid.eq(1)
        if i == 3:
            yield dut.proc_last_data_byte.eq(1)
        yield
        proc_addr = yield dut.proc_bram_addr
        expected = SHADOW_BUF1_BASE + offset
        print(f"[Процессор] Байт {i+1}/4 по 0x{0x1600+offset:04X} → BRAM {hex(proc_addr)}")
        assert proc_addr == expected, \
            f"Процессор должен оставаться в buf1 во время записи, получен {hex(proc_addr)}"

    yield dut.proc_write_en.eq(0)
    yield dut.proc_data_valid.eq(0)
    yield dut.proc_last_data_byte.eq(0)
    yield

    # Проверяем, что статус взвёлся только после последнего байта
    status = yield dut.status
    print(f"[TxPDO] После записи 4 байт: status={bin(status)}")
    assert status & 0x02, "Status[1] (TxPDO ready) должен быть взведён"

    # --- Сеть читает первый байт: предсказание отдаёт shadow=1 ---
    yield dut.ecat_addr.eq(0x1600)
    yield dut.ecat_data_phase.eq(1)
    yield dut.ecat_read_en.eq(1)
    yield
    ecat_addr = yield dut.ecat_bram_addr
    print(f"[Сеть] Чтение 0x1600 → BRAM {hex(ecat_addr)} (ожидалось 0x2000)")
    assert ecat_addr == SHADOW_BUF1_BASE, \
        f"Сеть должна захватить Shadow Buffer 1, получен {hex(ecat_addr)}"

    # Второй байт
    yield dut.ecat_addr.eq(0x1608)
    yield
    ecat_addr = yield dut.ecat_bram_addr
    print(f"[Сеть] Чтение 0x1608 → BRAM {hex(ecat_addr)} (ожидалось 0x2008)")
    assert ecat_addr == 0x2008

    yield dut.ecat_last_data_byte.eq(1)
    yield
    yield dut.ecat_last_data_byte.eq(0)
    yield dut.ecat_read_en.eq(0)
    yield dut.ecat_data_phase.eq(0)
    yield
    yield
    status = yield dut.status
    print(f"[TxPDO] После чтения сетью: status={bin(status)}")
    assert status & 0x02 == 0, "Status[1] должен сброситься после чтения"

    print("=== ТЕСТ 2 ПРОЙДЕН ===\n")




def _test_mailbox_rx(dut):
    """Тест 3: RxMailbox — сеть пишет SDO, процессор квитирует."""
    print("\n=== ТЕСТ 3: MAILBOX RX (Сеть → Процессор) ===")
    BASE = 0x0800
    def write_reg(offset, value):
        yield from sm_write_ecat_reg(dut, BASE, offset, value)

    def read_reg(offset):
        return (yield from sm_read_ecat_reg(dut, BASE, offset))

    yield from write_reg(0x00, 0x00)
    yield from write_reg(0x01, 0x10)
    yield from write_reg(0x02, 0x80)
    yield from write_reg(0x03, 0x00)
    yield from write_reg(0x04, 0x06)   # Mailbox + Write
    yield from write_reg(0x06, 0x01)
    yield
    print("[Config] SM0: Mailbox RxSDO, база=0x1000, длина=128")

    yield dut.ecat_addr.eq(0x1010)
    yield dut.ecat_data_phase.eq(1)
    yield dut.ecat_write_en.eq(1)
    yield
    addr = yield dut.ecat_bram_addr
    print(f"[Mailbox] Сеть пишет 0x1010 → BRAM {hex(addr)}")
    assert addr == 0x1010, "В Mailbox режиме адрес идёт напрямую в Buffer 0"

    yield dut.ecat_last_data_byte.eq(1)
    yield
    yield dut.ecat_last_data_byte.eq(0)
    yield dut.ecat_write_en.eq(0)
    yield dut.ecat_data_phase.eq(0)
    yield

    status = yield dut.status
    print(f"[Mailbox] SDO записан, status={bin(status)}")
    assert status & 0x01, "Status[0] (Write Event) должен быть взведён"

    # Процессор квитирует через pdi_control[0]
    yield from sm_write_pdi_reg(dut, BASE, 0x07, 0x01)
    yield
    yield
    status = yield dut.status
    irq = yield dut.irq
    print(f"[Mailbox] Процессор квитировал, status={bin(status)}, irq={irq}")
    assert status & 0x01 == 0, "Status[0] должен сброситься после квитирования"

    print("=== ТЕСТ 3 ПРОЙДЕН ===\n")


def _test_mailbox_tx(dut):
    """
    Тест 4: TxMailbox — процессор пишет SDO ответ, сеть читает.

    РАСШИРЕНО: многобайтовая запись SDO-ответа с завершением.
    """
    print("\n=== ТЕСТ 4: MAILBOX TX (Процессор → Сеть) ===")
    BASE = 0x0808
    def write_reg(offset, value):
        yield from sm_write_ecat_reg(dut, BASE, offset, value)

    def read_reg(offset):
        return (yield from sm_read_ecat_reg(dut, BASE, offset))

    yield from write_reg(0x00, 0x80)
    yield from write_reg(0x01, 0x10)
    yield from write_reg(0x02, 0x80)
    yield from write_reg(0x03, 0x00)
    yield from write_reg(0x04, 0x02)   # Mailbox, direction=Read
    yield from write_reg(0x06, 0x01)
    yield
    print("[Config] SM1: Mailbox TxSDO, база=0x1080, длина=128")

    # Процессор пишет SDO-ответ из 8 байт
    for i in range(8):
        yield dut.proc_addr.eq(0x1080 + i)
        yield dut.proc_write_en.eq(1)
        yield dut.proc_data_valid.eq(1)
        if i == 7:
            yield dut.proc_last_data_byte.eq(1)
        yield
        proc_addr = yield dut.proc_bram_addr
        print(f"[Mailbox] Процессор байт {i+1}/8 → BRAM {hex(proc_addr)}")
        assert proc_addr == 0x1080 + i

    yield dut.proc_write_en.eq(0)
    yield dut.proc_data_valid.eq(0)
    yield dut.proc_last_data_byte.eq(0)
    yield
    yield
    status = yield dut.status
    print(f"[Mailbox] SDO ответ готов (8 байт), status={bin(status)}")
    assert status & 0x08, "Status[3] (Read Event) должен взвестись только после последнего байта"

    # Сеть читает буфер целиком
    yield dut.ecat_addr.eq(0x1080)
    yield dut.ecat_data_phase.eq(1)
    yield dut.ecat_read_en.eq(1)
    yield
    for i in range(7):
        yield dut.ecat_addr.eq(0x1080 + i + 1)
        yield
    yield dut.ecat_last_data_byte.eq(1)
    yield
    yield dut.ecat_last_data_byte.eq(0)
    yield dut.ecat_read_en.eq(0)
    yield dut.ecat_data_phase.eq(0)
    yield
    yield
    status = yield dut.status
    print(f"[Mailbox] Сеть прочитала 8 байт, status={bin(status)}")
    assert status & 0x08 == 0, "Status[3] должен сброситься после чтения сетью"

    print("=== ТЕСТ 4 ПРОЙДЕН ===\n")



def _test_buffer_error(dut):
    """Тест 5: buffer_error при некорректной конфигурации."""
    print("\n=== ТЕСТ 5: ПРОВЕРКА buffer_error ===")
    BASE = 0x0810
    def write_reg(offset, value):
        yield from sm_write_ecat_reg(dut, BASE, offset, value)

    def read_reg(offset):
        return (yield from sm_read_ecat_reg(dut, BASE, offset))

    # 1) База внутри регистрового окна
    yield from write_reg(0x00, 0x00)
    yield from write_reg(0x01, 0x08)   # 0x0800
    yield from write_reg(0x02, 0x40)
    yield from write_reg(0x03, 0x00)
    yield from write_reg(0x04, 0x04)
    yield from write_reg(0x06, 0x01)
    yield
    yield
    error = yield dut.buffer_error
    print(f"[Config] база 0x0800 < 0x1000 → buffer_error={error}")
    assert error == 1

    # 2) Длина больше 4KB
    yield from write_reg(0x01, 0x10)   # база 0x1000
    yield from write_reg(0x02, 0x01)
    yield from write_reg(0x03, 0x10)   # длина 0x1001
    yield
    yield
    error = yield dut.buffer_error
    print(f"[Config] длина 0x1001 > 4KB → buffer_error={error}")
    assert error == 1

    # 3) Выход за верхнюю границу Buffer 0
    yield from write_reg(0x01, 0x1F)   # база 0x1F00
    yield from write_reg(0x02, 0x00)
    yield from write_reg(0x03, 0x02)   # длина 0x200
    yield
    yield
    error = yield dut.buffer_error
    print(f"[Config] 0x1F00 + 0x200 = 0x2100 > 0x2000 → buffer_error={error}")
    assert error == 1

    # 4) Корректная конфигурация — ошибки нет
    yield from write_reg(0x00, 0x00)
    yield from write_reg(0x01, 0x14)
    yield from write_reg(0x02, 0x40)
    yield from write_reg(0x03, 0x00)
    yield
    yield
    error = yield dut.buffer_error
    print(f"[Config] 0x1400 + 0x40 → buffer_error={error}")
    assert error == 0, "Валидная конфигурация не должна давать ошибку"

    print("=== ТЕСТ 5 ПРОЙДЕН ===\n")


def _test_read_config(dut):
    """Тест 6: обратное чтение конфигурационных регистров."""
    print("\n=== ТЕСТ 6: ЧТЕНИЕ КОНФИГУРАЦИИ ===")
    BASE = 0x0800

    def write_reg(offset, value):
        yield from sm_write_ecat_reg(dut, BASE, offset, value)

    def read_reg(offset):
        return (yield from sm_read_ecat_reg(dut, BASE, offset))

    def read_pdi_reg(offset):
        return (yield from sm_read_pdi_reg(dut, BASE, offset))

    yield from write_reg(0x00, 0xAB)
    yield from write_reg(0x01, 0x12)   # 13-битное поле: сохранится 0x12 & 0x1F = 0x12
    yield from write_reg(0x02, 0xCD)
    yield from write_reg(0x03, 0x34)
    yield from write_reg(0x04, 0x06)
    yield from write_reg(0x06, 0x01)
    yield from sm_write_pdi_reg(dut, BASE, 0x07, 0x09)

    yield

    # Проверка через ECAT-порт (мастер читает конфигурацию)
    checks_ecat = [
        (0x00, 0xAB, "Physical Start Addr LSB"),
        (0x01, 0x12, "Physical Start Addr MSB (5 бит)"),
        (0x02, 0xCD, "Length LSB"),
        (0x03, 0x34, "Length MSB"),
        (0x04, 0x06, "Control"),
        (0x06, 0x01, "Activate"),
        (0x07, 0x09, "PDI Control (ECAT может читать)"),
    ]
    for offset, expected, name in checks_ecat:
        value = yield from read_reg(offset)
        print(f"[ECAT Read] 0x{BASE+offset:04X} {name}: {hex(value)} (ожидалось {hex(expected)})")
        assert value == expected, f"Несовпадение при чтении {name} через ECAT"

    # Проверка через PDI-порт (процессор читает свой регистр)
    value = yield from read_pdi_reg(0x07)
    print(f"[PDI Read] 0x{BASE+0x07:04X} PDI Control: {hex(value)} (ожидалось 0x09)")
    assert value == 0x09, "PDI должен читать свой регистр"

    # Адрес вне окна канала не должен драйвить данные
    yield dut.ecat_reg_addr.eq(0x0900)
    yield
    value = yield dut.ecat_reg_read_data
    print(f"[Read] 0x0900 вне окна: {hex(value)} (ожидалось 0x00)")
    assert value == 0, "Канал не должен отвечать на адреса вне своего окна"

    print("=== ТЕСТ 6 ПРОЙДЕН ===\n")



def _test_irq_3buffer(dut):
    """Тест 7: IRQ в 3-buffer режиме с квитированием через pdi_control."""
    print("\n=== ТЕСТ 7: IRQ В РЕЖИМЕ 3-BUFFER ===")
    BASE = 0x0810
    def write_reg(offset, value):
        yield from sm_write_ecat_reg(dut, BASE, offset, value)

    def read_reg(offset):
        return (yield from sm_read_ecat_reg(dut, BASE, offset))

    # Явная деактивация сбрасывает указатели и статус
    yield from write_reg(0x06, 0x00)
    yield
    yield

    yield from write_reg(0x00, 0x00)
    yield from write_reg(0x01, 0x14)
    yield from write_reg(0x02, 0x40)
    yield from write_reg(0x03, 0x00)
    yield from write_reg(0x04, 0x04)   # 3-Buffer RxPDO
    yield from sm_write_pdi_reg(dut, BASE, 0x07, 0x01)
    yield from write_reg(0x06, 0x01)
    yield
    print("[Config] SM2: 3-Buffer RxPDO, IRQ разрешён через pdi_control[0]")

    irq = yield dut.irq
    assert irq == 0, "IRQ не должен быть взведён до записи данных"

    yield dut.ecat_addr.eq(0x1400)
    yield dut.ecat_data_phase.eq(1)
    yield dut.ecat_write_en.eq(1)
    yield
    yield dut.ecat_last_data_byte.eq(1)
    yield
    yield dut.ecat_last_data_byte.eq(0)
    yield dut.ecat_write_en.eq(0)
    yield dut.ecat_data_phase.eq(0)
    yield
    yield
    irq = yield dut.irq
    print(f"[IRQ] После записи кадра: irq={irq}")
    assert irq == 1, "IRQ должен взвестись при появлении данных в shadow"

    yield dut.proc_addr.eq(0x1400)
    yield dut.proc_read_en.eq(1)
    yield
    yield
    yield dut.proc_read_en.eq(0)
    yield
    yield
    irq = yield dut.irq
    print(f"[IRQ] После захвата буфера процессором: irq={irq}")
    assert irq == 0, "IRQ должен сброситься после захвата shadow процессором"

    print("=== ТЕСТ 7 ПРОЙДЕН ===\n")

def _test_manager_irq_aggregation(dut):
    """Тест 8: агрегация IRQ и мультиплексор чтения на уровне менеджера."""
    print("\n=== ТЕСТ 8: МЕНЕДЖЕР (агрегация IRQ, мультиплексор чтения) ===")

    def write_reg(addr, value):
        yield from sm_write_ecat_reg(dut, 0x0000, addr, value)

    def write_pdi_reg(addr, value):
        yield from sm_write_pdi_reg(dut, 0x0000, addr, value)

    def read_reg(addr):
        return (yield from sm_read_ecat_reg(dut, 0x0000, addr))

    # SM2 (база регистров 0x0810): 3-Buffer RxPDO с IRQ
    yield from write_reg(0x0810, 0x00)
    yield from write_reg(0x0811, 0x14)
    yield from write_reg(0x0812, 0x40)
    yield from write_reg(0x0813, 0x00)
    yield from write_reg(0x0814, 0x04)
    yield from write_pdi_reg(0x0817, 0x01)  # PDI control через PDI-порт
    yield from write_reg(0x0816, 0x01)

    # SM3 (база 0x0818): другой диапазон
    yield from write_reg(0x0818, 0x00)
    yield from write_reg(0x0819, 0x16)
    yield from write_reg(0x081A, 0x20)
    yield from write_reg(0x081B, 0x00)
    yield from write_reg(0x081C, 0x04)
    yield from write_reg(0x081E, 0x01)
    yield

    irq = yield dut.irq_out
    assert irq == 0, "irq_out должен быть 0 до появления данных"

    # Кадр в окно SM2
    yield dut.ecat_addr.eq(0x1400)
    yield dut.ecat_data_phase.eq(1)
    yield dut.ecat_write_en.eq(1)
    yield
    yield dut.ecat_last_data_byte.eq(1)
    yield
    yield dut.ecat_last_data_byte.eq(0)
    yield dut.ecat_write_en.eq(0)
    yield dut.ecat_data_phase.eq(0)
    yield
    yield
    irq = yield dut.irq_out
    print(f"[Менеджер] После кадра в окно SM2: irq_out={irq}")
    assert irq == 1, "irq_out должен агрегировать IRQ канала SM2"

    # Мультиплексор чтения
    value = yield from read_reg(0x0819)
    print(f"[Менеджер] Чтение 0x0819 (SM3 addr MSB): {hex(value)} (ожидалось 0x16)")
    assert value == 0x16, "Мультиплексор чтения выбрал не тот канал"

    value = yield from read_reg(0x0812)
    print(f"[Менеджер] Чтение 0x0812 (SM2 length LSB): {hex(value)} (ожидалось 0x40)")
    assert value == 0x40

    print("=== ТЕСТ 8 ПРОЙДЕН ===\n")

def sm_write_ecat_reg(dut, base, offset, value):
    """Запись со стороны сети (мастер): владеет 0x00-0x04, 0x06."""
    yield dut.ecat_reg_addr.eq(base + offset)
    yield dut.ecat_reg_write_data.eq(value)
    yield dut.ecat_reg_write_en.eq(1)
    yield
    yield dut.ecat_reg_write_en.eq(0)
    yield


def sm_write_pdi_reg(dut, base, offset, value):
    """Запись со стороны процессора: владеет только 0x07."""
    yield dut.pdi_reg_addr.eq(base + offset)
    yield dut.pdi_reg_write_data.eq(value)
    yield dut.pdi_reg_write_en.eq(1)
    yield
    yield dut.pdi_reg_write_en.eq(0)
    yield


def sm_read_ecat_reg(dut, base, offset):
    yield dut.ecat_reg_addr.eq(base + offset)
    yield
    return (yield dut.ecat_reg_read_data)


def sm_read_pdi_reg(dut, base, offset):
    yield dut.pdi_reg_addr.eq(base + offset)
    yield
    return (yield dut.pdi_reg_read_data)

def write_reg(offset, value):
    yield from sm_write_ecat_reg(dut, BASE, offset, value)

def read_reg(offset):
    return (yield from sm_read_ecat_reg(dut, BASE, offset))

# =============================================================================
# Запуск
# =============================================================================
if __name__ == "__main__":
    from migen.sim import run_simulation

    print("\n" + "=" * 70)
    print("НАБОР ТЕСТОВ SyncManager")
    print("=" * 70)

    # Каждый тест на своём канале: base_register_address должен совпадать
    # с адресами, по которым тест пишет конфигурацию.
    cases = [
        (0x0810, _test_3buffer_rxpdo, "syncmanager_rxpdo.vcd"),
        (0x0818, _test_3buffer_txpdo, "syncmanager_txpdo.vcd"),
        (0x0800, _test_mailbox_rx,    "syncmanager_mbx_rx.vcd"),
        (0x0808, _test_mailbox_tx,    "syncmanager_mbx_tx.vcd"),
        (0x0810, _test_buffer_error,  "syncmanager_buferr.vcd"),
        (0x0800, _test_read_config,   "syncmanager_regs.vcd"),
        (0x0810, _test_irq_3buffer,   "syncmanager_irq.vcd")
    ]

    for base, test_fn, vcd in cases:
        dut = EtherCATSyncManagerChannel(base_register_address=base)
        run_simulation(dut, test_fn(dut), vcd_name=vcd)

    dut = EtherCATSyncManagerManager(start_addr=0x0800, size=0x0008, count=4)
    run_simulation(dut, _test_manager_irq_aggregation(dut),
                   vcd_name="syncmanager_manager.vcd")

    print("\n" + "=" * 70)
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ")
    print("=" * 70)
    print("""
Покрыто:
  1. 3-buffer RxPDO   — ротация с подменой базы на 0x3000, свежий буфер процессору
  2. 3-buffer TxPDO   — процессор пишет в 0x2000, сеть комбинаторно захватывает shadow
  3. Mailbox RX       — прямая адресация Buffer 0, Write Event, квитирование
  4. Mailbox TX       — Read Event, сброс после чтения сетью
  5. buffer_error     — база вне DPRAM, длина > 4KB, выход за 0x2000, валидный случай
  6. Регистры         — обратное чтение всех полей, молчание вне окна канала
  7. IRQ 3-buffer     — взвод по данным в shadow, сброс по захвату процессором
  8. Менеджер         — агрегация irq_out, мультиплексор reg_read_data по каналам
""")
