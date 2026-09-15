# Protocol test vectors

The JSON files contain only protocol data and audio timing metadata:

- `buttons.json`, `calibration.json`, `modes.json`, `timers.json`: Daikin E-Max 7
  state bytes and IR pulse lengths. `index` is a sample counter, not a device ID.
- `sound_frames.json`: frame counts and sample rates for the factory sound
  catalog, used to verify playback durations without distributing audio files.

Device addresses, credentials, capture timestamps and device identifiers are
excluded. Tests use loopback addresses and invented locally administered MACs.
