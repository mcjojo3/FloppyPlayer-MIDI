// config.h - RP2040 storage-server tuning.
#pragma once

// Diagnostics on a UART (GPIO12): USB carries the binary link to the Pi.
#define DBG_SERIAL Serial1
#define DBG_SERIAL_TX_PIN 12
#define DBG_SERIAL_RX_PIN 13
#define DBG_SERIAL_BAUD 115200

// Recaptures of a marginal cylinder before accepting it (~2.5s each).
#define MAX_CYLINDER_REFETCH_STREAK 3

// Cylinders cached in RAM (LRU).
#define NUM_CACHE_SLOTS 3

// No INDEX pulse within this means no disk is spinning.
#define INDEX_WAIT_TIMEOUT_MS 1000

// Spindle stops after this long without a read - the heads rest on the disk.
#define FLOPPY_MOTOR_IDLE_MS 15000

// Entries per directory listing (one LIST response).
#define MAX_DIR_ENTRIES 54
