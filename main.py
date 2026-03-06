#!/usr/bin/env python3
"""
FEDar — Fed tracker and analyst terminal platform.

Terminal-style interface for policy bands, signals, sessions, and feed data.
State can be stored locally (JSON) and optionally synced with Jer0me contract.
Single-file app: CLI, state, and mock RPC helpers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import struct
import sys
import textwrap
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

APP_NAME = "FEDar"
APP_VERSION = "1.4.2"
DEFAULT_STATE_FILE = "fedar_state.json"
DEFAULT_CONFIG_FILE = "fedar_config.json"
NAMESPACE = "Jer0me.fed.v2"
MAX_BANDS = 64
MAX_FEEDS = 16
BPS_DENOMINATOR = 10_000
SESSION_DURATION_BLOCKS = 150
EPOCH_BLOCKS = 2016
VOTE_HOLD, VOTE_UP, VOTE_DOWN = 0, 1, 2
BATCH_SIGNALS_MAX = 32

# -----------------------------------------------------------------------------
# Data models
# -----------------------------------------------------------------------------


@dataclass
class RateBand:
    band_id: int
    band_tag: str
    lower_bps: int
    upper_bps: int
    policy_epoch: int
    registered_at_block: int
    active: bool


