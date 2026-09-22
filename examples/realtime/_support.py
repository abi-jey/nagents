"""Shared example device and console support, without a Live protocol dependency."""

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "_voice"))

from audio_adapters import SoundDeviceAudioInput as SoundDeviceAudioInput
from audio_adapters import SoundDeviceAudioOutput as SoundDeviceAudioOutput
from event_printer import print_event as print_event
from event_printer import start_timer as start_timer
