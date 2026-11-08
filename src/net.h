#pragma once

// WLAN + Venus MQTT client. Non-blocking; call net_loop() from loop().
void net_begin();
void net_loop();
