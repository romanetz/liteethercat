from migen import *
from ethercat_fmmu import *
# =============================================================================
# Тест канала: трансляция, права, фазы, границы DPRAM
# =============================================================================
def _fmmu_translation_test(dut):
    """Проверяем комбинаторную трансляцию и права доступа за 0 тактов."""

    BASE = 0x0600

    def write_reg(offset, value):
        yield dut.reg_write_addr.eq(BASE + offset)
        yield dut.reg_write_data.eq(value)
        yield dut.reg_write_en.eq(1)
        yield

    print("\n=== ТЕСТ FMMU: КОМБИНАТОРНАЯ ТРАНСЛЯЦИЯ ===")

    # Logical Start Address = 0x11000000
    yield from write_reg(0x00, 0x00)
    yield from write_reg(0x01, 0x00)
    yield from write_reg(0x02, 0x00)
    yield from write_reg(0x03, 0x11)
    # Length = 0x0040 (64 байта)
    yield from write_reg(0x04, 0x40)
    yield from write_reg(0x05, 0x00)
    # Start/End bit — байтовое отображение
    yield from write_reg(0x06, 0x00)
    yield from write_reg(0x07, 0x07)
    # Physical Start Address = 0x1000
    yield from write_reg(0x08, 0x00)
    yield from write_reg(0x09, 0x10)
    # Type = 0x02 (только запись разрешена)
    yield from write_reg(0x0B, 0x02)
    # Activate = 1
    yield from write_reg(0x0C, 0x01)
    yield dut.reg_write_en.eq(0)
    yield
    print("[Конфиг] Окно 0x11000000+0x40 -> DPRAM 0x1000, Type=WriteOnly, Active")

    # --- Тест 1: LWR внутрь окна, тот же такт ---
    yield dut.logical_addr_in.eq(0x11000005)
    yield dut.data_phase.eq(1)
    yield dut.cmd_write.eq(1)
    yield dut.cmd_read.eq(0)
    yield

    hit   = yield dut.hit
    would = yield dut.would_hit
    ok    = yield dut.access_ok
    phys  = yield dut.physical_addr_out
    perm  = yield dut.perm_error
    print(f"[LWR 0x11000005] hit={hit}, would_hit={would}, phys={hex(phys)}, "
          f"perm_error={perm}, access_ok={ok}")
    assert hit == 1, "Промах внутри окна FMMU!"
    assert phys == 0x1005, f"Неверная трансляция: {hex(phys)} вместо 0x1005"
    assert perm == 0, "Ложное нарушение прав при разрешённой записи!"
    assert ok == 1, "access_ok должен быть взведён при корректном доступе!"

    # --- Тест 2: LRD в Write-Only окно -> perm_error, access_ok снят ---
    yield dut.cmd_write.eq(0)
    yield dut.cmd_read.eq(1)
    yield
    perm = yield dut.perm_error
    ok   = yield dut.access_ok
    print(f"[LRD в Write-Only] perm_error={perm}, access_ok={ok}")
    assert perm == 1, "Нарушение прав доступа не задетектировано!"
    assert ok == 0, "access_ok взведён при нарушении прав — WKC соврёт мастеру!"

    # --- Тест 3: LRW -> запись разрешена, чтение нет -> perm_error ---
    yield dut.cmd_write.eq(1)
    yield dut.cmd_read.eq(1)
    yield
    perm = yield dut.perm_error
    ok   = yield dut.access_ok
    print(f"[LRW в Write-Only] perm_error={perm}, access_ok={ok}")
    assert perm == 1, "LRW должен ловить запрет чтения!"
    assert ok == 0, "access_ok взведён при частично запрещённом LRW!"

    # --- Тест 4: адрес вне окна ---
    yield dut.logical_addr_in.eq(0x11000100)
    yield dut.cmd_read.eq(0)
    yield dut.cmd_write.eq(1)
    yield
    hit   = yield dut.hit
    would = yield dut.would_hit
    print(f"[LWR 0x11000100 вне окна] hit={hit}, would_hit={would}")
    assert hit == 0, "Ложное попадание вне окна FMMU!"
    assert would == 0, "would_hit взведён вне окна!"

    # --- Тест 5: вне фазы данных hit запрещён, would_hit остаётся ---
    yield dut.logical_addr_in.eq(0x11000005)
    yield dut.data_phase.eq(0)
    yield
    hit   = yield dut.hit
    would = yield dut.would_hit
    ok    = yield dut.access_ok
    print(f"[data_phase=0] hit={hit}, would_hit={would}, access_ok={ok}")
    assert hit == 0, "hit взведён вне фазы данных!"
    assert would == 1, "would_hit должен работать вне фазы данных — FSM нужен ранний детект!"
    assert ok == 0, "access_ok не должен взводиться вне фазы данных!"

    # --- Тест 6: верхняя граница DPRAM без range_error ---
    yield from write_reg(0x08, 0x00)
    yield from write_reg(0x09, 0x1F)      # physical_start = 0x1F00
    yield from write_reg(0x04, 0xFF)      # length = 0x00FF
    yield dut.reg_write_en.eq(0)
    yield dut.logical_addr_in.eq(0x110000F0)   # offset 0xF0 -> phys 0x1FF0
    yield dut.data_phase.eq(1)
    yield dut.cmd_write.eq(1)
    yield dut.cmd_read.eq(0)
    yield
    hit  = yield dut.hit
    phys = yield dut.physical_addr_out
    rng  = yield dut.range_error
    ok   = yield dut.access_ok
    print(f"[LWR offset 0xF0] hit={hit}, phys={hex(phys)}, range_error={rng}, access_ok={ok}")
    assert hit == 1, "Промах у верхней границы DPRAM!"
    assert phys == 0x1FF0, f"Неверная трансляция: {hex(phys)} вместо 0x1FF0"
    assert rng == 0, "Ложный range_error внутри DPRAM!"
    assert ok == 1, "access_ok снят при корректном доступе у границы!"

    # --- Тест 6b: физическое окно ниже DPRAM -> range_error ---
    yield from write_reg(0x08, 0x00)
    yield from write_reg(0x09, 0x05)      # physical_start = 0x0500, вне DPRAM
    yield dut.reg_write_en.eq(0)
    yield dut.logical_addr_in.eq(0x11000010)
    yield
    hit  = yield dut.hit
    phys = yield dut.physical_addr_out
    rng  = yield dut.range_error
    ok   = yield dut.access_ok
    print(f"[phys_start=0x0500] hit={hit}, phys={hex(phys)}, range_error={rng}, access_ok={ok}")
    assert hit == 1, "hit должен взводиться — окно логически валидно"
    assert rng == 1, "range_error не сработал при выходе ниже 0x1000!"
    assert ok == 0, "access_ok взведён при выходе за DPRAM — запись уйдёт в чужие регистры!"

    # Возвращаем корректный конфиг для следующего теста
    yield from write_reg(0x08, 0x00)
    yield from write_reg(0x09, 0x10)
    yield dut.reg_write_en.eq(0)
    yield


    # --- Тест 7: битовое окно -> канал молчит ---
    yield from write_reg(0x07, 0x03)      # end_bit = 3, не байтовое отображение
    yield dut.reg_write_en.eq(0)
    yield
    hit   = yield dut.hit
    would = yield dut.would_hit
    unsup = yield dut.bit_mapping_unsupported
    print(f"[end_bit=3] hit={hit}, would_hit={would}, bit_unsupported={unsup}")
    assert unsup == 1, "Битовое окно не задетектировано!"
    assert hit == 0, "Канал даёт hit при неподдерживаемой конфигурации!"
    assert would == 0, "would_hit взведён при неподдерживаемой конфигурации!"

    print("=== ТЕСТ КАНАЛА FMMU ПРОЙДЕН ===\n")


# =============================================================================
# Тест менеджера: приоритет, мультихит, агрегация
# =============================================================================
def _fmmu_manager_test(dut):
    """Приоритет каналов при перекрытии окон и агрегация признаков."""

    def write_reg(base, offset, value):
        yield dut.reg_write_addr.eq(base + offset)
        yield dut.reg_write_data.eq(value)
        yield dut.reg_write_en.eq(1)
        yield

    def config_channel(base, log_start, length, phys_start, op_type):
        for i in range(4):
            yield from write_reg(base, 0x00 + i, (log_start >> (i * 8)) & 0xFF)
        yield from write_reg(base, 0x04, length & 0xFF)
        yield from write_reg(base, 0x05, (length >> 8) & 0xFF)
        yield from write_reg(base, 0x06, 0x00)
        yield from write_reg(base, 0x07, 0x07)
        yield from write_reg(base, 0x08, phys_start & 0xFF)
        yield from write_reg(base, 0x09, (phys_start >> 8) & 0x1F)
        yield from write_reg(base, 0x0B, op_type)
        yield from write_reg(base, 0x0C, 0x01)

    print("\n=== ТЕСТ FMMU: МЕНЕДЖЕР (приоритет, агрегация) ===")

    CH0 = 0x0600
    CH1 = 0x0610

    # Канал 0: 0x11000000+0x40 -> 0x1000, read+write
    yield from config_channel(CH0, 0x11000000, 0x0040, 0x1000, 0x03)
    # Канал 1: 0x12000000+0x40 -> 0x1800, только чтение
    yield from config_channel(CH1, 0x12000000, 0x0040, 0x1800, 0x01)
    yield dut.reg_write_en.eq(0)
    yield
    print("[Конфиг] CH0: 0x11000000->0x1000 RW | CH1: 0x12000000->0x1800 RO")

    # --- Доступ в окно канала 1 ---
    yield dut.logical_addr_in.eq(0x12000010)
    yield dut.data_phase.eq(1)
    yield dut.cmd_read.eq(1)
    yield dut.cmd_write.eq(0)
    yield
    hit   = yield dut.any_hit
    phys  = yield dut.final_phys_addr
    which = yield dut.hit_channel
    multi = yield dut.multi_hit_error
    ok    = yield dut.access_ok
    print(f"[LRD 0x12000010] hit={hit}, phys={hex(phys)}, канал={which}, "
          f"multi={multi}, access_ok={ok}")
    assert hit == 1, "Промах в окне канала 1!"
    assert phys == 0x1810, f"Неверная трансляция: {hex(phys)} вместо 0x1810"
    assert which == 1, "Сработал не тот канал!"
    assert multi == 0, "Ложный multi_hit при неперекрывающихся окнах!"
    assert ok == 1, "access_ok снят при разрешённом чтении!"

    # --- Запись в read-only окно канала 1 ---
    yield dut.cmd_read.eq(0)
    yield dut.cmd_write.eq(1)
    yield
    perm = yield dut.any_perm_error
    ok   = yield dut.access_ok
    print(f"[LWR в RO-окно CH1] any_perm_error={perm}, access_ok={ok}")
    assert perm == 1, "Запись в read-only окно не задетектирована!"
    assert ok == 0, "access_ok взведён при запрещённой записи!"

    # --- Перекрытие: канал 1 переводим на адрес канала 0 ---
    yield from config_channel(CH1, 0x11000000, 0x0040, 0x1800, 0x01)
    yield dut.reg_write_en.eq(0)
    yield dut.logical_addr_in.eq(0x11000010)
    yield dut.cmd_read.eq(1)
    yield dut.cmd_write.eq(0)
    yield
    phys  = yield dut.final_phys_addr
    which = yield dut.hit_channel
    multi = yield dut.multi_hit_error
    ok    = yield dut.access_ok
    print(f"[Перекрытие, LRD 0x11000010] phys={hex(phys)}, канал={which}, "
          f"multi={multi}, access_ok={ok}")
    assert multi == 1, "Перекрытие окон не задетектировано!"
    assert which == 0, "Приоритет нарушен: должен выиграть канал 0!"
    assert phys == 0x1010, f"Адрес взят у проигравшего канала: {hex(phys)}"
    assert ok == 1, "Победитель арбитража разрешает чтение — access_ok должен стоять!"

    # --- Вне фазы данных: any_would_hit держится, any_hit снят ---
    yield dut.data_phase.eq(0)
    yield
    hit   = yield dut.any_hit
    would = yield dut.any_would_hit
    print(f"[data_phase=0] any_hit={hit}, any_would_hit={would}")
    assert hit == 0, "any_hit взведён вне фазы данных!"
    assert would == 1, "any_would_hit должен работать вне фазы данных!"

    # --- Адрес мимо всех окон ---
    yield dut.logical_addr_in.eq(0x99000000)
    yield dut.data_phase.eq(1)
    yield
    hit   = yield dut.any_hit
    would = yield dut.any_would_hit
    ok    = yield dut.access_ok
    print(f"[LRD 0x99000000 мимо всех окон] any_hit={hit}, would={would}, ok={ok}")
    assert hit == 0, "Ложное попадание вне всех окон!"
    assert would == 0, "Ложный would_hit вне всех окон!"
    assert ok == 0, "access_ok вне окон — WKC насчитает несуществующий доступ!"

    print("=== ТЕСТ МЕНЕДЖЕРА FMMU ПРОЙДЕН ===\n")


if __name__ == "__main__":
    from migen.sim import run_simulation

    EC_FMMU_START_ADDR = 0x0600
    EC_FMMU_SIZE       = 0x0010
    EC_FMMU_COUNT      = 0x0008

    info = EtherCATFMMUManager(
        start_addr=EC_FMMU_START_ADDR,
        size=EC_FMMU_SIZE,
        count=EC_FMMU_COUNT,
    )
    print(f"Сгенерировано {info.count} каналов FMMU.")
    print(f"Диапазон регистров: {hex(info.start_addr)} - "
          f"{hex(info.start_addr + info.size * info.count - 1)}")

    print("\nПроверка валидатора (count=1)...")
    try:
        EtherCATFMMUManager(count=1)
        print("ОШИБКА: валидатор не сработал!")
    except ValueError as e:
        print(f"Валидатор перехватил ошибку: {e}")

    # Для симуляции создаются ОТДЕЛЬНЫЕ экземпляры: Migen не позволяет
    # элаборировать один и тот же модуль дважды (get_fragment_called).
    ch_dut = EtherCATFMMUChannel(base_register_address=0x0600)
    run_simulation(ch_dut, _fmmu_translation_test(ch_dut), vcd_name="fmmu_channel.vcd")

    mgr_dut = EtherCATFMMUManager(start_addr=0x0600, size=0x0010, count=4)
    run_simulation(mgr_dut, _fmmu_manager_test(mgr_dut), vcd_name="fmmu_manager.vcd")
