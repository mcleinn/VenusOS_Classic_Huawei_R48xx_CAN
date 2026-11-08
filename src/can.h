#pragma once

// Passive CAN listener on the Huawei charger bus (TJA1051T/3 on the P1 header pins).
// The display is a second node that only listens: TWAI listen-only mode, never a master, no acks.
// It decodes both directions: the control board's set commands and the charger's status/acks.
void can_begin();
void can_loop();
