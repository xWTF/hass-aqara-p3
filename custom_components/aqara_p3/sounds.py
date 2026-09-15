# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Aqara P3 contributors

"""Factory 4.0.4 sound durations, measured from PCM WAV frame counts.

Names/indices come from mha_master's audio table. Only metadata is bundled;
the P3 plays its own files in /data/musics/music-scene/.
"""

SOUND_DURATIONS = {
    "DingDong": 2.87346875,
    "Knock": 1.33675,
    "Funny": 5.95590625,
    "Ring": 5.198375,
    "MiMix": 7.5050625,
    "Enthusiastic": 8.9705625,
    "GuitarClassic": 7.92521875,
    "IceWorldPiano": 11.25265625,
    "LeisureTime": 9.55571875,
    "Childhood": 8.72425,
    "MorningStreamlet": 8.440125,
    "MusixBox": 10.001375,
    "MusicBox": 10.001375,
    "Orange": 9.76534375,
    "Thinker": 6.5410625,
    "PoliceCar_1": 6.5794375,
    "PoliceCar_2": 13.09109375,
    "Accident": 4.77621875,
    "CountDown": 5.87621875,
    "Ghost": 6.9746875,
    "Gun": 7.39265625,
    "Battle": 7.5144375,
    "AirRaid": 10.64125,
    "Dog": 4.19809375,
}

# Allow the factory audio worker to start/drain before sending another play.
REPEAT_GAP = 0.3
