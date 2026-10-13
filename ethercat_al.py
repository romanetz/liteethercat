#!/usr/bin/env python3
"""
Минимальная AL State Machine для ESC.

Поддерживает:
  INIT    (0x01) — всегда
  PREOP   (0x02) — если SM0 и SM1 активированы
  SAFEOP  (0x04) — отказ (AL Status Code 0x0011)
  OP      (0x08) — отказ (AL Status Code 0x0011)

Не реализует:
  - Bootstrap
  - Проверку DC-конфигурации перед SAFEOP
  - Проверку входных SM перед OP
  - Error handling с конкретными кодами (кроме «invalid state change»)
"""

from migen import *


class ALStateMachine(Module):
    AL_STATE_INIT   = 0x01
    AL_STATE_PREOP  = 0x02
    AL_STATE_SAFEOP = 0x04
    AL_STATE_OP     = 0x08

    # AL Status Code (ETG.1000.6 Table 21)
    AL_STATUS_NO_ERROR          = 0x0000
    AL_STATUS_INVALID_STATE     = 0x0011  # Invalid requested state change

    def __init__(self):
        # ------------------------------------------------------------------
        # Интерфейс к регистрам ESC
        # ------------------------------------------------------------------
        # AL Control — то, что написал мастер (0x0120-0x0121)
        self.al_control         = Signal(16)
        # AL Status — что мы возвращаем (0x0130-0x0131)
        self.al_status          = Signal(16, reset=self.AL_STATE_INIT)
        # AL Status Code — код ошибки (0x0134-0x0135)
        self.al_status_code     = Signal(16, reset=self.AL_STATUS_NO_ERROR)

        # ------------------------------------------------------------------
        # Строб: мастер записал новый AL Control
        # ------------------------------------------------------------------
        self.al_control_write   = Signal()
        self.al_control_value   = Signal(16)

        # ------------------------------------------------------------------
        # Признаки готовности SM (подаются снаружи)
        # ------------------------------------------------------------------
        # SM0 (MBoxOut) и SM1 (MBoxIn) должны быть активированы для PREOP.
        # Для чистого INIT эти сигналы не важны.
        self.sm0_activated      = Signal()
        self.sm1_activated      = Signal()

        # ------------------------------------------------------------------
        # Диагностика
        # ------------------------------------------------------------------
        self.state_changes_ok   = Signal(32)
        self.state_changes_err  = Signal(32)

        # ------------------------------------------------------------------
        # FSM
        # ------------------------------------------------------------------
        self.submodules.fsm = fsm = FSM(reset_state="WAIT_CONTROL")

        fsm.act("WAIT_CONTROL",
            If(self.al_control_write,
                NextValue(self.state_changes_ok, self.state_changes_ok),
                NextState("DECODE")
            )
        )

        fsm.act("DECODE",
            # Смотрим, чего хочет мастер (младшие 4 бита AL Control)
            Case(self.al_control_value[0:4], {
                self.AL_STATE_INIT: [
                    NextValue(self.al_status, self.AL_STATE_INIT),
                    NextValue(self.al_status_code, self.AL_STATUS_NO_ERROR),
                    NextValue(self.state_changes_ok, self.state_changes_ok + 1),
                    NextState("WAIT_CONTROL"),
                ],
                self.AL_STATE_PREOP: [
                    # Разрешаем PREOP только если SM0/SM1 сконфигурированы
                    If(self.sm0_activated & self.sm1_activated,
                        NextValue(self.al_status, self.AL_STATE_PREOP),
                        NextValue(self.al_status_code, self.AL_STATUS_NO_ERROR),
                        NextValue(self.state_changes_ok,
                                  self.state_changes_ok + 1),
                    ).Else(
                        NextValue(self.al_status_code,
                                  self.AL_STATUS_INVALID_STATE),
                        NextValue(self.state_changes_err,
                                  self.state_changes_err + 1),
                    ),
                    NextState("WAIT_CONTROL"),
                ],
                self.AL_STATE_SAFEOP: [
                    NextValue(self.al_status_code, self.AL_STATUS_INVALID_STATE),
                    NextValue(self.state_changes_err,
                              self.state_changes_err + 1),
                    NextState("WAIT_CONTROL"),
                ],
                self.AL_STATE_OP: [
                    NextValue(self.al_status_code, self.AL_STATUS_INVALID_STATE),
                    NextValue(self.state_changes_err,
                              self.state_changes_err + 1),
                    NextState("WAIT_CONTROL"),
                ],
                "default": [
                    NextValue(self.al_status_code, self.AL_STATUS_INVALID_STATE),
                    NextValue(self.state_changes_err,
                              self.state_changes_err + 1),
                    NextState("WAIT_CONTROL"),
                ],
            })
        )
