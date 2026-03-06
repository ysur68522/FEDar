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


@dataclass
class PolicySignal:
    signal_id: int
    signal_hash: str
    epoch: int
    relayer: str
    at_block: int


@dataclass
class AnalystVote:
    direction: int
    band_id: int
    at_block: int


@dataclass
class TerminalSession:
    session_id: int
    analyst: str
    opened_at_block: int
    expiry_block: int
    closed: bool
    votes: Dict[str, AnalystVote] = field(default_factory=dict)


@dataclass
class FeedSlot:
    feed_index: int
    value: int
    timestamp: int
    updated_at_block: int


@dataclass
class BandHistoryEntry:
    band_id: int
    lower_bps: int
    upper_bps: int
    active: bool
    at_block: int


# -----------------------------------------------------------------------------
# State container
# -----------------------------------------------------------------------------


@dataclass
class FEDarState:
    bands: Dict[int, RateBand] = field(default_factory=dict)
    signals: Dict[int, PolicySignal] = field(default_factory=dict)
    sessions: Dict[int, TerminalSession] = field(default_factory=dict)
    feeds: Dict[int, FeedSlot] = field(default_factory=dict)
    band_history: List[BandHistoryEntry] = field(default_factory=list)
    band_cap: int = MAX_BANDS
    current_epoch: int = 1
    signal_counter: int = 0
    session_counter: int = 0
    band_counter: int = 0
    history_counter: int = 0
    epoch_start_blocks: Dict[int, int] = field(default_factory=dict)
    analyst_whitelist: Dict[str, bool] = field(default_factory=dict)
    current_block: int = 0
    stale_window_blocks: int = 50
    fee_bps: int = 25

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bands": {str(k): asdict(v) for k, v in self.bands.items()},
            "signals": {str(k): asdict(v) for k, v in self.signals.items()},
            "sessions": {str(k): {**asdict(v), "votes": {a: asdict(vv) for a, vv in v.votes.items()}} for k, v in self.sessions.items()},
            "feeds": {str(k): asdict(v) for k, v in self.feeds.items()},
            "band_history": [asdict(e) for e in self.band_history],
            "band_cap": self.band_cap,
            "current_epoch": self.current_epoch,
            "signal_counter": self.signal_counter,
            "session_counter": self.session_counter,
            "band_counter": self.band_counter,
            "history_counter": self.history_counter,
            "epoch_start_blocks": self.epoch_start_blocks,
            "analyst_whitelist": self.analyst_whitelist,
            "current_block": self.current_block,
            "stale_window_blocks": self.stale_window_blocks,
            "fee_bps": self.fee_bps,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> FEDarState:
        state = cls()
        state.band_cap = d.get("band_cap", MAX_BANDS)
        state.current_epoch = d.get("current_epoch", 1)
        state.signal_counter = d.get("signal_counter", 0)
        state.session_counter = d.get("session_counter", 0)
        state.band_counter = d.get("band_counter", 0)
        state.history_counter = d.get("history_counter", 0)
        state.epoch_start_blocks = d.get("epoch_start_blocks", {})
        state.analyst_whitelist = d.get("analyst_whitelist", {})
        state.current_block = d.get("current_block", 0)
        state.stale_window_blocks = d.get("stale_window_blocks", 50)
        state.fee_bps = d.get("fee_bps", 25)
        for k, v in d.get("bands", {}).items():
            state.bands[int(k)] = RateBand(**v)
        for k, v in d.get("signals", {}).items():
            state.signals[int(k)] = PolicySignal(**v)
        for k, v in d.get("sessions", {}).items():
            votes = {a: AnalystVote(**vv) for a, vv in v.get("votes", {}).items()}
            state.sessions[int(k)] = TerminalSession(
                session_id=v["session_id"],
                analyst=v["analyst"],
                opened_at_block=v["opened_at_block"],
                expiry_block=v["expiry_block"],
                closed=v["closed"],
                votes=votes,
            )
        for k, v in d.get("feeds", {}).items():
            state.feeds[int(k)] = FeedSlot(**v)
        for e in d.get("band_history", []):
            state.band_history.append(BandHistoryEntry(**e))
        return state


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def bytes32_hex(s: str) -> str:
    h = hashlib.sha256(s.encode()).hexdigest()
    return "0x" + h[:64] if len(h) >= 64 else "0x" + h.ljust(64, "0")


def random_address() -> str:
    return "0x" + "".join(random.choices("0123456789abcdef", k=40))


def advance_block(state: FEDarState, delta: int = 1) -> None:
    state.current_block += delta


def resolve_band_for_bps(state: FEDarState, bps: int) -> Tuple[Optional[int], bool]:
    for bid, b in state.bands.items():
        if b.active and bps >= b.lower_bps and bps <= b.upper_bps:
            return (bid, True)
    return (None, False)


def is_session_open(state: FEDarState, session_id: int) -> bool:
    s = state.sessions.get(session_id)
    if not s or s.closed:
        return False
    return state.current_block <= s.expiry_block


def is_feed_stale(state: FEDarState, feed_index: int) -> bool:
    f = state.feeds.get(feed_index)
    if not f:
        return True
    return state.current_block > f.updated_at_block + state.stale_window_blocks


# -----------------------------------------------------------------------------
# Commands: bands
# -----------------------------------------------------------------------------


def cmd_band_register(state: FEDarState, tag: str, lower_bps: int, upper_bps: int) -> str:
    if lower_bps >= upper_bps:
        return "Error: lower_bps must be < upper_bps"
    if state.band_counter >= state.band_cap:
        return "Error: band cap exceeded"
    state.band_counter += 1
    bid = state.band_counter
    state.bands[bid] = RateBand(
        band_id=bid,
        band_tag=tag,
        lower_bps=lower_bps,
        upper_bps=upper_bps,
        policy_epoch=state.current_epoch,
        registered_at_block=state.current_block,
        active=True,
    )
    state.history_counter += 1
    state.band_history.append(
        BandHistoryEntry(band_id=bid, lower_bps=lower_bps, upper_bps=upper_bps, active=True, at_block=state.current_block)
    )
    return f"Band {bid} registered: {tag} [{lower_bps}-{upper_bps}] bps"


def cmd_band_list(state: FEDarState, active_only: bool = False) -> str:
    lines = ["id | tag | lower_bps | upper_bps | epoch | active"]
    for bid in sorted(state.bands.keys()):
        b = state.bands[bid]
        if active_only and not b.active:
            continue
        lines.append(f"{b.band_id} | {b.band_tag} | {b.lower_bps} | {b.upper_bps} | {b.policy_epoch} | {b.active}")
    return "\n".join(lines) if len(lines) > 1 else "No bands"


def cmd_band_resolve(state: FEDarState, bps: int) -> str:
    band_id, found = resolve_band_for_bps(state, bps)
    if found:
        return f"Bps {bps} -> band_id {band_id}"
    return f"No active band for bps {bps}"


# -----------------------------------------------------------------------------
# Commands: signals
# -----------------------------------------------------------------------------


def cmd_signal_push(state: FEDarState, payload: str) -> str:
    state.signal_counter += 1
    sig_hash = bytes32_hex(payload)
    state.signals[state.signal_counter] = PolicySignal(
        signal_id=state.signal_counter,
        signal_hash=sig_hash,
        epoch=state.current_epoch,
        relayer=random_address(),
        at_block=state.current_block,
    )
    return f"Signal {state.signal_counter} pushed (epoch {state.current_epoch})"


def cmd_signal_list(state: FEDarState, epoch: Optional[int] = None, limit: int = 20) -> str:
    lines = ["id | signal_hash | epoch | at_block"]
    count = 0
    for sid in reversed(sorted(state.signals.keys())):
        if count >= limit:
            break
        s = state.signals[sid]
        if epoch is not None and s.epoch != epoch:
            continue
        lines.append(f"{s.signal_id} | {s.signal_hash[:18]}... | {s.epoch} | {s.at_block}")
        count += 1
    return "\n".join(lines) if len(lines) > 1 else "No signals"


# -----------------------------------------------------------------------------
# Commands: sessions & votes
# -----------------------------------------------------------------------------


def cmd_session_open(state: FEDarState, analyst: str) -> str:
    state.analyst_whitelist[analyst] = True
    state.session_counter += 1
    sid = state.session_counter
    expiry = state.current_block + SESSION_DURATION_BLOCKS
    state.sessions[sid] = TerminalSession(
        session_id=sid,
        analyst=analyst,
        opened_at_block=state.current_block,
        expiry_block=expiry,
        closed=False,
        votes={},
    )
    return f"Session {sid} opened for {analyst}, expires at block {expiry}"


def cmd_session_close(state: FEDarState, session_id: int) -> str:
    s = state.sessions.get(session_id)
    if not s:
        return "Session not found"
    if s.closed:
        return "Session already closed"
    s.closed = True
    return f"Session {session_id} closed"


def cmd_vote_cast(state: FEDarState, session_id: int, analyst: str, direction: int, band_id: int) -> str:
    s = state.sessions.get(session_id)
    if not s:
        return "Session not found"
    if s.closed:
        return "Session closed"
    if state.current_block > s.expiry_block:
        return "Session expired"
    if s.analyst != analyst:
        return "Not session analyst"
    if direction not in (VOTE_HOLD, VOTE_UP, VOTE_DOWN):
        return "Invalid direction (0=hold, 1=up, 2=down)"
    if band_id not in state.bands or not state.bands[band_id].active:
        return "Band not found or inactive"
    s.votes[analyst] = AnalystVote(direction=direction, band_id=band_id, at_block=state.current_block)
    return f"Vote cast: session={session_id} direction={direction} band_id={band_id}"


def cmd_session_list(state: FEDarState, open_only: bool = False) -> str:
    lines = ["id | analyst | opened | expiry | closed"]
    for sid in sorted(state.sessions.keys()):
        s = state.sessions[sid]
        if open_only and (s.closed or state.current_block > s.expiry_block):
            continue
        lines.append(f"{s.session_id} | {s.analyst[:12]}... | {s.opened_at_block} | {s.expiry_block} | {s.closed}")
    return "\n".join(lines) if len(lines) > 1 else "No sessions"


# -----------------------------------------------------------------------------
# Commands: feeds
# -----------------------------------------------------------------------------


def cmd_feed_update(state: FEDarState, feed_index: int, value: int) -> str:
    if feed_index < 0 or feed_index >= MAX_FEEDS:
        return f"Feed index must be 0..{MAX_FEEDS - 1}"
    state.feeds[feed_index] = FeedSlot(
        feed_index=feed_index,
        value=value,
        timestamp=int(datetime.now().timestamp()),
        updated_at_block=state.current_block,
    )
    return f"Feed {feed_index} = {value}"


def cmd_feed_list(state: FEDarState) -> str:
    lines = ["index | value | updated_block | stale"]
    for idx in sorted(state.feeds.keys()):
        f = state.feeds[idx]
        stale = is_feed_stale(state, idx)
        lines.append(f"{f.feed_index} | {f.value} | {f.updated_at_block} | {stale}")
    return "\n".join(lines) if len(lines) > 1 else "No feeds"


# -----------------------------------------------------------------------------
# Commands: epoch & block
# -----------------------------------------------------------------------------


def cmd_epoch_advance(state: FEDarState) -> str:
    prev = state.current_epoch
    state.current_epoch += 1
    state.epoch_start_blocks[state.current_epoch] = state.current_block
    return f"Epoch advanced: {prev} -> {state.current_epoch}"


def cmd_block_advance(state: FEDarState, delta: int = 1) -> str:
    advance_block(state, delta)
    return f"Block: {state.current_block}"


def cmd_block_set(state: FEDarState, block_num: int) -> str:
    state.current_block = block_num
    return f"Block set to {state.current_block}"


# -----------------------------------------------------------------------------
# Commands: config & state
# -----------------------------------------------------------------------------


def cmd_config_show(state: FEDarState) -> str:
    return (
        f"band_cap={state.band_cap} current_epoch={state.current_epoch} "
        f"current_block={state.current_block} stale_window={state.stale_window_blocks} fee_bps={state.fee_bps}"
    )


def cmd_stats(state: FEDarState) -> str:
    open_sessions = sum(1 for s in state.sessions.values() if not s.closed and state.current_block <= s.expiry_block)
    active_bands = sum(1 for b in state.bands.values() if b.active)
    return (
        f"Bands: {len(state.bands)} (active: {active_bands}) | "
        f"Signals: {len(state.signals)} | Sessions: {len(state.sessions)} (open: {open_sessions}) | "
        f"Feeds: {len(state.feeds)} | Epoch: {state.current_epoch} | Block: {state.current_block}"
    )


def cmd_analyst_whitelist(state: FEDarState, analyst: str, allowed: bool) -> str:
    state.analyst_whitelist[analyst] = allowed
    return f"Analyst {analyst} whitelist={allowed}"


# -----------------------------------------------------------------------------
# Commands: band history & feed aggregation
# -----------------------------------------------------------------------------


def cmd_band_history(state: FEDarState, from_idx: Optional[int] = None, to_idx: Optional[int] = None) -> str:
    entries = state.band_history
    if not entries:
        return "No band history"
    if from_idx is not None and to_idx is not None:
        entries = entries[from_idx : to_idx + 1]
    lines = ["entry | band_id | lower_bps | upper_bps | active | at_block"]
    for i, e in enumerate(entries):
        lines.append(f"{i} | {e.band_id} | {e.lower_bps} | {e.upper_bps} | {e.active} | {e.at_block}")
    return "\n".join(lines)


def cmd_feed_sum(state: FEDarState, from_idx: int = 0, to_idx: int = MAX_FEEDS - 1) -> str:
    if from_idx > to_idx or to_idx >= MAX_FEEDS:
        return f"Invalid range; use 0..{MAX_FEEDS - 1}"
    total = 0
    for i in range(from_idx, to_idx + 1):
        if i in state.feeds:
            total += state.feeds[i].value
    return f"Feed sum [{from_idx}-{to_idx}] = {total}"


def cmd_feed_mean(state: FEDarState, from_idx: int = 0, to_idx: int = MAX_FEEDS - 1) -> str:
    if from_idx > to_idx or to_idx >= MAX_FEEDS:
        return f"Invalid range; use 0..{MAX_FEEDS - 1}"
    vals = [state.feeds[i].value for i in range(from_idx, to_idx + 1) if i in state.feeds]
    if not vals:
        return "No feed data in range"
    return f"Feed mean [{from_idx}-{to_idx}] = {sum(vals) / len(vals):.2f}"


def cmd_config_snapshot(state: FEDarState) -> str:
    return (
        f"band_cap={state.band_cap} band_count={len(state.bands)} current_epoch={state.current_epoch} "
        f"stale_window={state.stale_window_blocks} fee_bps={state.fee_bps} current_block={state.current_block}"
    )


def cmd_epoch_stats(state: FEDarState, epoch: int) -> str:
    start = state.epoch_start_blocks.get(epoch, 0)
    count = sum(1 for s in state.signals.values() if s.epoch == epoch)
    return f"Epoch {epoch}: start_block={start} signal_count={count}"


def cmd_band_by_tag(state: FEDarState, tag: str) -> str:
    for bid, b in state.bands.items():
        if b.band_tag == tag:
            return f"Band id={bid} tag={tag} lower={b.lower_bps} upper={b.upper_bps} active={b.active}"
    return f"No band with tag '{tag}'"


def cmd_session_votes(state: FEDarState, session_id: int) -> str:
    s = state.sessions.get(session_id)
    if not s:
        return "Session not found"
    if not s.votes:
        return f"Session {session_id}: no votes"
    lines = [f"Session {session_id} votes:"]
    for analyst, v in s.votes.items():
        lines.append(f"  {analyst[:16]}... dir={v.direction} band_id={v.band_id}")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Jer0me contract ABI (stub for simulation / future RPC)
# -----------------------------------------------------------------------------

JER0ME_ABI = [
    {"inputs": [], "name": "getBandCount", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "uint256", "name": "bandId_", "type": "uint256"}], "name": "getBand", "outputs": [{"internalType": "bytes32", "name": "bandTag", "type": "bytes32"}, {"internalType": "uint256", "name": "lowerBps", "type": "uint256"}, {"internalType": "uint256", "name": "upperBps", "type": "uint256"}, {"internalType": "uint256", "name": "policyEpoch", "type": "uint256"}, {"internalType": "uint256", "name": "registeredAtBlock", "type": "uint256"}, {"internalType": "bool", "name": "active", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "uint256", "name": "bps_", "type": "uint256"}], "name": "resolveBandForBps", "outputs": [{"internalType": "uint256", "name": "bandId", "type": "uint256"}, {"internalType": "bool", "name": "found", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "currentEpoch", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "uint256", "name": "feedIndex_", "type": "uint256"}], "name": "getFeed", "outputs": [{"internalType": "int256", "name": "value", "type": "int256"}, {"internalType": "uint256", "name": "timestamp", "type": "uint256"}, {"internalType": "uint256", "name": "updatedAtBlock", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "uint256", "name": "sessionId_", "type": "uint256"}], "name": "getSession", "outputs": [{"internalType": "address", "name": "analyst", "type": "address"}, {"internalType": "uint256", "name": "openedAtBlock", "type": "uint256"}, {"internalType": "uint256", "name": "expiryBlock", "type": "uint256"}, {"internalType": "bool", "name": "closed", "type": "bool"}], "stateMutability": "view", "type": "function"},
]


def simulate_contract_get_band_count(state: FEDarState) -> int:
    return len(state.bands)


def simulate_contract_resolve_band_for_bps(state: FEDarState, bps: int) -> Tuple[int, bool]:
    band_id, found = resolve_band_for_bps(state, bps)
    return (band_id or 0, found)


def simulate_contract_get_feed(state: FEDarState, feed_index: int) -> Tuple[int, int, int]:
    f = state.feeds.get(feed_index)
    if not f:
        return (0, 0, 0)
    return (f.value, f.timestamp, f.updated_at_block)


def simulate_contract_get_session(state: FEDarState, session_id: int) -> Tuple[str, int, int, bool]:
    s = state.sessions.get(session_id)
    if not s:
        return ("0x0000000000000000000000000000000000000000", 0, 0, True)
    return (s.analyst, s.opened_at_block, s.expiry_block, s.closed)


# -----------------------------------------------------------------------------
# Export / Import
# -----------------------------------------------------------------------------


def cmd_export(state: FEDarState, path: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, indent=2)
    return f"State exported to {path}"


def cmd_seed_demo(state: FEDarState) -> str:
    """Populate state with sample bands, signals, feeds, and one session for demo."""
    if state.band_counter > 0:
        return "State already has data; use fresh state for seed"
    for tag, low, high in [("TIGHT", 0, 25), ("NEUTRAL", 25, 75), ("LOOSE", 75, 100)]:
        cmd_band_register(state, tag, low * 100, high * 100)
    for _ in range(5):
        cmd_signal_push(state, str(random.randint(0, 2**32)))
    for i in range(4):
        cmd_feed_update(state, i, random.randint(-100, 100))
    adv = random_address()
    state.analyst_whitelist[adv] = True
    cmd_session_open(state, adv)
    cmd_epoch_advance(state)
    return "Seeded: 3 bands, 5 signals, 4 feeds, 1 session, epoch advanced"


def cmd_import(state: FEDarState, path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    imported = FEDarState.from_dict(data)
    state.bands = imported.bands
    state.signals = imported.signals
    state.sessions = imported.sessions
    state.feeds = imported.feeds
    state.band_history = imported.band_history
    state.band_cap = imported.band_cap
    state.current_epoch = imported.current_epoch
    state.signal_counter = imported.signal_counter
    state.session_counter = imported.session_counter
    state.band_counter = imported.band_counter
    state.history_counter = imported.history_counter
    state.epoch_start_blocks = imported.epoch_start_blocks
    state.analyst_whitelist = imported.analyst_whitelist
    state.current_block = imported.current_block
    state.stale_window_blocks = imported.stale_window_blocks
    state.fee_bps = imported.fee_bps
    return f"State imported from {path}"


def cmd_export_bands_csv(state: FEDarState, path: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("band_id,band_tag,lower_bps,upper_bps,policy_epoch,active\n")
        for b in sorted(state.bands.values(), key=lambda x: x.band_id):
            f.write(f"{b.band_id},{b.band_tag},{b.lower_bps},{b.upper_bps},{b.policy_epoch},{b.active}\n")
    return f"Bands CSV exported to {path}"


def cmd_export_signals_csv(state: FEDarState, path: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("signal_id,signal_hash,epoch,at_block\n")
        for s in sorted(state.signals.values(), key=lambda x: x.signal_id):
            f.write(f"{s.signal_id},{s.signal_hash},{s.epoch},{s.at_block}\n")
    return f"Signals CSV exported to {path}"


# -----------------------------------------------------------------------------
# Persistence
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# Validation and formatting helpers
# -----------------------------------------------------------------------------


def validate_bps(value: int) -> bool:
    return 0 <= value <= BPS_DENOMINATOR


def validate_band_bounds(lower: int, upper: int) -> Tuple[bool, str]:
    if lower >= upper:
        return False, "lower_bps must be < upper_bps"
    if not validate_bps(lower) or not validate_bps(upper):
        return False, "bps must be in [0, 10000]"
    return True, ""


def format_band_row(b: RateBand) -> str:
    return f"{b.band_id:4} | {b.band_tag:12} | {b.lower_bps:5} | {b.upper_bps:5} | {b.policy_epoch:3} | {b.active}"


def format_signal_row(s: PolicySignal) -> str:
    return f"{s.signal_id:4} | {s.signal_hash[:20]}... | {s.epoch:3} | {s.at_block}"


def format_feed_row(f: FeedSlot, stale: bool) -> str:
    return f"{f.feed_index:2} | {f.value:6} | {f.updated_at_block:6} | {stale}"


def format_session_row(s: TerminalSession) -> str:
    return f"{s.session_id:4} | {s.analyst[:14]:14} | {s.opened_at_block:6} | {s.expiry_block:6} | {s.closed}"


def compute_fee(state: FEDarState, amount: int) -> int:
    return (amount * state.fee_bps) // BPS_DENOMINATOR


def compute_net_after_fee(state: FEDarState, amount: int) -> int:
    return amount - compute_fee(state, amount)


def count_active_bands(state: FEDarState) -> int:
    return sum(1 for b in state.bands.values() if b.active)


def count_signals_in_epoch(state: FEDarState, epoch: int) -> int:
    return sum(1 for s in state.signals.values() if s.epoch == epoch)


def count_open_sessions(state: FEDarState) -> int:
    return sum(1 for s in state.sessions.values() if not s.closed and state.current_block <= s.expiry_block)


def get_last_signal_for_epoch(state: FEDarState, epoch: int) -> Optional[PolicySignal]:
    candidates = [s for s in state.signals.values() if s.epoch == epoch]
    if not candidates:
        return None
    return max(candidates, key=lambda x: x.at_block)


def blocks_until_session_expiry(state: FEDarState, session_id: int) -> int:
    s = state.sessions.get(session_id)
    if not s or s.closed:
        return 0
    if state.current_block >= s.expiry_block:
        return 0
    return s.expiry_block - state.current_block


def blocks_since_epoch_start(state: FEDarState, epoch: int) -> int:
    start = state.epoch_start_blocks.get(epoch, 0)
    if start == 0:
        return 0
    return state.current_block - start


def is_epoch_current(state: FEDarState, epoch: int) -> bool:
    return epoch == state.current_epoch


def has_active_band_at_bps(state: FEDarState, bps: int) -> bool:
    _, found = resolve_band_for_bps(state, bps)
    return found


def batch_resolve_bands_for_bps(state: FEDarState, bps_list: List[int]) -> List[Optional[int]]:
    result = []
    for bps in bps_list:
        bid, _ = resolve_band_for_bps(state, bps)
        result.append(bid)
    return result


def load_state(path: Path) -> FEDarState:
    if not path.exists():
        return FEDarState()
    with open(path, "r", encoding="utf-8") as f:
        return FEDarState.from_dict(json.load(f))


def save_state(state: FEDarState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, indent=2)


# -----------------------------------------------------------------------------
# CLI parser and REPL
# -----------------------------------------------------------------------------


def run_cmd(state: FEDarState, args: List[str]) -> str:
    if not args:
        return ""
    cmd = args[0].lower()
    rest = args[1:]

    if cmd == "band" and len(rest) >= 4 and rest[0] == "register":
        return cmd_band_register(state, rest[1], int(rest[2]), int(rest[3]))
    if cmd == "band" and len(rest) >= 1 and rest[0] == "list":
        return cmd_band_list(state, active_only="--active" in rest)
    if cmd == "band" and len(rest) >= 1 and rest[0] == "resolve":
        return cmd_band_resolve(state, int(rest[1])) if len(rest) > 1 else "Usage: band resolve <bps>"

    if cmd == "signal" and len(rest) >= 1 and rest[0] == "push":
        payload = rest[1] if len(rest) > 1 else str(random.randint(0, 2**32))
        return cmd_signal_push(state, payload)
    if cmd == "signal" and len(rest) >= 1 and rest[0] == "list":
        epoch = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else None
        return cmd_signal_list(state, epoch=epoch)

    if cmd == "session" and len(rest) >= 2 and rest[0] == "open":
        return cmd_session_open(state, rest[1])
    if cmd == "session" and len(rest) >= 2 and rest[0] == "close":
        return cmd_session_close(state, int(rest[1]))
    if cmd == "session" and len(rest) >= 1 and rest[0] == "list":
        return cmd_session_list(state, open_only="--open" in rest)
    if cmd == "vote" and len(rest) >= 4:
        return cmd_vote_cast(state, int(rest[0]), rest[1], int(rest[2]), int(rest[3]))

    if cmd == "feed" and len(rest) >= 2 and rest[0] == "update":
        return cmd_feed_update(state, int(rest[1]), int(rest[2]) if len(rest) > 2 else 0)
    if cmd == "feed" and len(rest) >= 1 and rest[0] == "list":
        return cmd_feed_list(state)

    if cmd == "epoch" and len(rest) >= 1 and rest[0] == "advance":
        return cmd_epoch_advance(state)
    if cmd == "block" and len(rest) >= 1:
        if rest[0] == "advance":
            delta = int(rest[1]) if len(rest) > 1 else 1
            return cmd_block_advance(state, delta)
        if rest[0] == "set":
            return cmd_block_set(state, int(rest[1])) if len(rest) > 1 else "Usage: block set <n>"

    if cmd == "config":
        return cmd_config_show(state)
    if cmd == "config" and len(rest) >= 1 and rest[0] == "snapshot":
        return cmd_config_snapshot(state)
    if cmd == "stats":
        return cmd_stats(state)
    if cmd == "analyst" and len(rest) >= 2:
        allowed = rest[1].lower() in ("1", "true", "yes")
        return cmd_analyst_whitelist(state, rest[0], allowed)

    if cmd == "band" and len(rest) >= 1 and rest[0] == "history":
        from_i = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else None
        to_i = int(rest[2]) if len(rest) > 2 and rest[2].isdigit() else None
        return cmd_band_history(state, from_i, to_i)
    if cmd == "band" and len(rest) >= 2 and rest[0] == "tag":
        return cmd_band_by_tag(state, rest[1])

    if cmd == "feed" and len(rest) >= 2 and rest[0] == "sum":
        from_i = int(rest[1]) if len(rest) > 1 else 0
        to_i = int(rest[2]) if len(rest) > 2 else MAX_FEEDS - 1
        return cmd_feed_sum(state, from_i, to_i)
    if cmd == "feed" and len(rest) >= 2 and rest[0] == "mean":
        from_i = int(rest[1]) if len(rest) > 1 else 0
        to_i = int(rest[2]) if len(rest) > 2 else MAX_FEEDS - 1
        return cmd_feed_mean(state, from_i, to_i)

    if cmd == "epoch" and len(rest) >= 2 and rest[0] == "stats":
        return cmd_epoch_stats(state, int(rest[1]))
    if cmd == "session" and len(rest) >= 2 and rest[0] == "votes":
        return cmd_session_votes(state, int(rest[1]))

    if cmd == "export" and len(rest) >= 2:
