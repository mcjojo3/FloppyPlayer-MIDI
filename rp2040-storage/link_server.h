// link_server.h - the RP2040 side of the link to the Pi. See link_protocol.h.
#pragma once

// Starts the USB link and tries a first mount of both backends. Call from
// setup(), after floppy_init().
void link_server_init();

// Waits up to ~1s for one request and answers it. Call every loop().
void link_server_poll();
