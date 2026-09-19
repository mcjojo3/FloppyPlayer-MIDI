// RP2040 storage server: serves the floppy (PIO flux capture) and SD card to
// pi-player/ over USB. Wiring is in README.md.
#include "config.h"
#include "floppy_fat12.h"
#include "link_server.h"

void setup() {
  DBG_SERIAL.setTX(DBG_SERIAL_TX_PIN);
  DBG_SERIAL.setRX(DBG_SERIAL_RX_PIN);
  DBG_SERIAL.begin(DBG_SERIAL_BAUD);
  delay(500);
  DBG_SERIAL.println("RP2040 storage server starting...");

  if (!floppy_init()) {
    DBG_SERIAL.println("floppy_init() failed.");
  }

  link_server_init();
  DBG_SERIAL.println("Link server ready.");
}

void loop() {
  link_server_poll();
  floppy_idle();
}
