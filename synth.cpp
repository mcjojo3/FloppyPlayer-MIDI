// synth.cpp - see synth.h.
#include "synth.h"
#include <math.h>
#include <Arduino.h>
#include <pico/platform.h>

const int MAX_VOICES = 24;

// Coarse GM program -> waveform grouping (not a real per-instrument model,
// and not aiming for clean/anti-aliased either - deliberately leaning into
// an NES-APU-style chiptune character: triangle for bass, pulse waves at
// different duty cycles for lead/brass, saw for plucked/percussive attacks,
// sine kept for anything meant to stay mellow).
enum Waveform : uint8_t { WAVE_SINE, WAVE_TRIANGLE, WAVE_SAW, WAVE_PULSE50, WAVE_PULSE25, WAVE_PULSE12 };

static Waveform waveformForProgram(uint8_t program) {
  if (program <= 7) return WAVE_SAW;                        // piano
  if (program <= 15) return WAVE_PULSE12;                   // chromatic percussion (bells, mallets) - thin/nasal
  if (program >= 16 && program <= 23) return WAVE_PULSE50;  // organ
  if (program >= 24 && program <= 31) return WAVE_SAW;      // guitar
  if (program >= 32 && program <= 39) return WAVE_TRIANGLE; // bass - classic chiptune bass channel
  if (program >= 56 && program <= 63) return WAVE_PULSE25;  // brass
  if (program >= 80 && program <= 87) return WAVE_PULSE25;  // synth lead
  return WAVE_SINE; // strings, ensemble, reed, pipe, pad, etc. - stays mellow
}

struct Voice {
  bool active;
  bool releasing;
  uint8_t channel;
  uint8_t note;
  Waveform waveform;
  uint32_t phaseStep; // fixed 32-bit phase increment per sample, from noteStepTable
  float velocity;
  uint32_t phase;     // 32-bit phase accumulator - wraps naturally via unsigned overflow
  uint32_t startTimeUs;
  uint32_t releaseTimeUs;
  float releaseStartLevel; // level_at() at the instant releasing began - constant for the whole release tail
};

static Voice voices[MAX_VOICES];

static const float ATTACK = 0.01f;
static const float DECAY = 0.1f;
static const float SUSTAIN = 0.7f;
static const float RELEASE = 0.15f;
// Reciprocals precomputed once so per-sample envelope math multiplies
// instead of divides - division is the slowest basic float op without a hardware FPU.
static const float ATTACK_INV = 1.0f / ATTACK;
static const float DECAY_INV = 1.0f / DECAY;
static const float RELEASE_INV = 1.0f / RELEASE;
static const float US_TO_SEC = 1.0f / 1000000.0f; // same reasoning - avoids a per-sample divide

// RMS-matching gains vs. the sine table (peak 1.0, RMS ~0.707): saw/triangle
// are ~0.577 of peak, bipolar pulse is peak==RMS, so each needs a different
// scale-up to sound equally loud despite their different shapes.
static const float SAWTRI_GAIN = 1.2247f; // sqrt(3/2)
static const float PULSE_GAIN = 0.7071f;  // 1/sqrt(2)

#define SINE_TABLE_BITS 8
#define SINE_TABLE_SIZE (1 << SINE_TABLE_BITS)
static float sineTable[SINE_TABLE_SIZE];
static uint32_t noteStepTable[128];
static float invSqrtTable[MAX_VOICES + 1];

void synth_init(uint32_t sampleRate) {
  const float PI_F = 3.14159265358979f;
  for (int i = 0; i < SINE_TABLE_SIZE; i++) {
    sineTable[i] = sinf(2.0f * PI_F * i / SINE_TABLE_SIZE);
  }
  for (int note = 0; note < 128; note++) {
    float freq = 440.0f * powf(2.0f, (note - 69) / 12.0f);
    // 32-bit phase accumulator wraps at 2^32 = one full cycle; step per
    // sample = freq * (2^32 / sampleRate).
    noteStepTable[note] = (uint32_t)((double)freq * 4294967296.0 / sampleRate);
  }
  invSqrtTable[0] = 0.0f; // unused (0 active voices means silence, never multiplied)
  for (int n = 1; n <= MAX_VOICES; n++) {
    invSqrtTable[n] = 1.0f / sqrtf((float)n);
  }
}

static float __not_in_flash_func(level_at)(const Voice &v, uint32_t tUs) {
  float elapsed = (float)(tUs - v.startTimeUs) * US_TO_SEC;
  if (elapsed < ATTACK) return elapsed * ATTACK_INV;
  elapsed -= ATTACK;
  if (elapsed < DECAY) return 1.0f - (1.0f - SUSTAIN) * (elapsed * DECAY_INV);
  return SUSTAIN;
}

void synth_note_on(uint8_t channel, uint8_t note, uint8_t velocity, uint8_t program, uint32_t nowUs) {
  int slot = -1;
  // Retriggering a note already sounding on this channel replaces that
  // voice rather than consuming a fresh one - otherwise a repeated note
  // (e.g. a bass line) burns a new voice slot every time for no musical reason.
  for (int i = 0; i < MAX_VOICES; i++) {
    if (voices[i].active && voices[i].channel == channel && voices[i].note == note) {
      slot = i;
      break;
    }
  }
  if (slot == -1) {
    for (int i = 0; i < MAX_VOICES; i++) {
      if (!voices[i].active) {
        slot = i;
        break;
      }
    }
  }
  if (slot == -1) {
    // Prefer stealing a voice already fading out - less audible than cutting a sustained note.
    uint32_t oldest = 0xFFFFFFFF;
    for (int i = 0; i < MAX_VOICES; i++) {
      if (voices[i].releasing && voices[i].releaseTimeUs < oldest) {
        oldest = voices[i].releaseTimeUs;
        slot = i;
      }
    }
  }
  if (slot == -1) {
    // No releasing voice available - steal the oldest sustaining one instead.
    uint32_t oldest = 0xFFFFFFFF;
    for (int i = 0; i < MAX_VOICES; i++) {
      if (voices[i].startTimeUs < oldest) {
        oldest = voices[i].startTimeUs;
        slot = i;
      }
    }
  }
  // active is set LAST, only after every other field is already valid.
  voices[slot].releasing = false;
  voices[slot].channel = channel;
  voices[slot].note = note;
  voices[slot].waveform = waveformForProgram(program);
  voices[slot].phaseStep = noteStepTable[note & 0x7F];
  voices[slot].velocity = velocity / 127.0f;
  voices[slot].phase = 0;
  voices[slot].startTimeUs = nowUs;
  voices[slot].active = true;
}

void synth_note_off(uint8_t channel, uint8_t note, uint32_t nowUs) {
  for (int i = 0; i < MAX_VOICES; i++) {
    if (voices[i].active && !voices[i].releasing &&
        voices[i].channel == channel && voices[i].note == note) {
      voices[i].releasing = true;
      voices[i].releaseTimeUs = nowUs;
      voices[i].releaseStartLevel = level_at(voices[i], nowUs);
    }
  }
}

void synth_all_notes_off(uint32_t nowUs) {
  for (int i = 0; i < MAX_VOICES; i++) {
    if (voices[i].active && !voices[i].releasing) {
      voices[i].releasing = true;
      voices[i].releaseTimeUs = nowUs;
      voices[i].releaseStartLevel = level_at(voices[i], nowUs);
    }
  }
}

static float __not_in_flash_func(envelope)(Voice &v, uint32_t nowUs) {
  if (v.releasing) {
    float relElapsed = (float)(nowUs - v.releaseTimeUs) * US_TO_SEC;
    if (relElapsed >= RELEASE) {
      v.active = false;
      return 0.0f;
    }
    return v.releaseStartLevel * (1.0f - relElapsed * RELEASE_INV);
  }
  return level_at(v, nowUs);
}

float __not_in_flash_func(synth_sample)(uint32_t nowUs) {
  float sum = 0.0f;
  int activeCount = 0;
  for (int i = 0; i < MAX_VOICES; i++) {
    if (!voices[i].active) continue;
    float env = envelope(voices[i], nowUs);
    if (!voices[i].active) continue; // envelope() may have just deactivated it

    float osc;
    switch (voices[i].waveform) {
      case WAVE_SAW:
        // Scaled so RMS loudness matches the sine table, not just peak amplitude.
        osc = SAWTRI_GAIN * (float)(int32_t)voices[i].phase / 2147483648.0f;
        break;
      case WAVE_TRIANGLE: {
        float ramp = (float)(int32_t)voices[i].phase / 2147483648.0f;
        osc = SAWTRI_GAIN * (2.0f * fabsf(ramp) - 1.0f); // fold the ramp into a triangle
        break;
      }
      // Bipolar pulse (swings +-PULSE_GAIN regardless of duty cycle) - RMS
      // equals peak amplitude here, so this one constant matches all three.
      case WAVE_PULSE50:
        osc = (voices[i].phase < 0x80000000u) ? PULSE_GAIN : -PULSE_GAIN;
        break;
      case WAVE_PULSE25:
        osc = (voices[i].phase < 0x40000000u) ? PULSE_GAIN : -PULSE_GAIN;
        break;
      case WAVE_PULSE12:
        osc = (voices[i].phase < 0x20000000u) ? PULSE_GAIN : -PULSE_GAIN;
        break;
      default: {
        uint32_t tableIndex = voices[i].phase >> (32 - SINE_TABLE_BITS);
        osc = sineTable[tableIndex];
        break;
      }
    }
    float s = osc * env * voices[i].velocity;
    voices[i].phase += voices[i].phaseStep; // wraps naturally, no branch needed

    sum += s;
    activeCount++;
  }
  if (activeCount >= 1) {
    sum *= invSqrtTable[activeCount];
  }
  return sum;
}
