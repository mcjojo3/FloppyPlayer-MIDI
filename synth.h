// synth.h - simple polyphonic sine + linear ADSR synth (chiptune-style
// rendition, not real GM instrument sounds). Oscillator/envelope math uses
// precomputed lookup tables, not live sinf()/sqrtf()/powf() - this chip's
// Cortex-M0+ cores have no hardware FPU, and those calls are too slow for
// the audio hot path.
#pragma once
#include <stdint.h>

// Must be called once at startup (before any note_on) with the actual audio
// sample rate, to build the note-frequency step table correctly.
void synth_init(uint32_t sampleRate);

void synth_note_on(uint8_t channel, uint8_t note, uint8_t velocity, uint32_t nowUs);
void synth_note_off(uint8_t channel, uint8_t note, uint32_t nowUs);
// Starts every active voice's normal release fade (same curve as
// synth_note_off()) instead of an instant cutoff - avoids an audible pop.
void synth_all_notes_off(uint32_t nowUs);

// Renders one sample from all currently active voices, roughly in [-1, 1]
// (headroom-scaled by a precomputed 1/sqrt(active voice count) table -
// caller still applies master volume and final clipping).
float synth_sample(uint32_t nowUs);
