#pragma once

// WLAN + Venus MQTT client. Non-blocking; call net_loop() from loop().
void net_begin();
void net_loop();
// Pause page: hold (true) or release (false) this display's charger; see model.h.
void net_set_pause(bool on);
