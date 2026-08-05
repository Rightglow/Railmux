"""Local, pane-scoped history state for the fast SSH display.

The state machine is transport-agnostic: callers supply decoded snapshots and
send the returned protocol frames without letting history pacing flow-control
the remote agent process.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace

from railmux.config import (
    SSH_HISTORY_DEFAULT_LINES,
    SSH_HISTORY_MAX_LINES,
    SSH_HISTORY_MIN_LINES,
)
from railmux.fast_display_input import (
    SgrMouseEvent,
    SelectionSource,
    page_key_direction,
)
from railmux.fast_display_protocol import (
    HistoryBatch,
    HistorySnapshot,
    encode_history_prefetch,
    encode_history_request,
)

_SGR_STYLE_RE = re.compile(rb"\x1b\[[0-9;]*m")
_HISTORY_SCROLL_BASE_LINES = 1
_HISTORY_PREFETCH_LINES = 300
_HISTORY_INITIAL_LINES = 2000
_HISTORY_PAGE_LINES = 2000
_HISTORY_LOAD_AHEAD_LINES = 120
_HISTORY_PREFETCH_TIMEOUT = 6.0
_HISTORY_DEEP_TIMEOUT = 10.0
_HISTORY_CONTENT_PANES = 8


@dataclass(frozen=True)
class HistoryAction:
    protocol_frame: bytes = b""
    forwarded_input: bytes = b""
    render_history: bool = False
    restore_live: bool = False
    refresh_routes: bool = False
    info_message: str | None = None
    claude_history_prompt: bytes = b""


@dataclass(frozen=True)
class HistoryMetrics:
    prefetch_requests: int = 0
    deep_requests: int = 0
    timeouts: int = 0
    anchor_rejects: int = 0


@dataclass
class PeriodicPrefetchGate:
    """Skip periodic captures until a newer screen update can stale the cache."""

    screen_generation: int = 0
    pending_request_id: int | None = None
    pending_generation: int | None = None
    accepted_generation: int | None = None

    def screen_updated(self) -> None:
        self.screen_generation += 1

    def should_request(self) -> bool:
        return self.accepted_generation != self.screen_generation

    def sent(self, request_id: int | None) -> None:
        if request_id is None:
            return
        self.pending_request_id = request_id
        self.pending_generation = self.screen_generation

    def accepted(self, request_id: int, expected_request_id: int | None) -> None:
        if request_id == expected_request_id and request_id == self.pending_request_id:
            self.accepted_generation = self.pending_generation
            self.pending_request_id = None
            self.pending_generation = None

    def reset(self) -> None:
        self.screen_generation = 0
        self.pending_request_id = None
        self.pending_generation = None
        self.accepted_generation = None


@dataclass
class _HistoryViewport:
    """One immutable pane snapshot plus its local offset from the bottom."""

    snapshot: HistorySnapshot
    offset: int
    loaded_limit: int
    exhausted: bool = False
    top_notified: bool = False


@dataclass(frozen=True)
class _PendingHistory:
    epoch: int
    pane_id: str
    target_lines: int
    requested_at: float
    # A first wheel-up waits for one cumulative snapshot before freezing the
    # viewport. This avoids exposing a short hot suffix whose later deep page
    # may not share a stable anchor while the provider is actively repainting.
    initial_offset: int | None = None


class LocalHistoryView:
    """Keep bounded history content separate from visible pointer routes."""

    def __init__(
        self,
        history_limit: int = SSH_HISTORY_DEFAULT_LINES,
    ) -> None:
        if not SSH_HISTORY_MIN_LINES <= history_limit <= SSH_HISTORY_MAX_LINES:
            raise ValueError("invalid local history limit")
        self.history_limit = history_limit
        self.viewports: dict[str, _HistoryViewport] = {}
        self._deep_pending: dict[int, _PendingHistory] = {}
        self.prefetch_pending_id: int | None = None
        self.prefetch_pending_epoch: int | None = None
        self.prefetch_started = 0.0
        self.visible_routes: tuple[HistorySnapshot, ...] = ()
        self._routes_ready = False
        self.content_cache: dict[str, HistorySnapshot] = {}
        self._unverified_after_reconnect: set[str] = set()
        self.route_epoch = 1
        self._local_pointer_capture = False
        self._forwarded_pointer_capture = False
        self._suppress_forwarded_drag = False
        self._next_request_id = 1
        self._prefetch_requests = 0
        self._deep_requests = 0
        self._timeouts = 0
        self._anchor_rejects = 0

    @property
    def active(self) -> bool:
        return bool(self.viewports)

    @property
    def pending(self) -> bool:
        return bool(self._deep_pending)

    @property
    def metrics(self) -> HistoryMetrics:
        return HistoryMetrics(
            prefetch_requests=self._prefetch_requests,
            deep_requests=self._deep_requests,
            timeouts=self._timeouts,
            anchor_rejects=self._anchor_rejects,
        )

    def _allocate_request_id(self) -> int:
        request_id = self._next_request_id
        self._next_request_id = (request_id + 1) & 0xFFFFFFFF
        if self._next_request_id == 0:
            self._next_request_id = 1
        return request_id

    def begin_prefetch(self, now: float, *, force: bool = False) -> bytes:
        if (
            not force
            and self.prefetch_pending_id is not None
            and now - self.prefetch_started < _HISTORY_PREFETCH_TIMEOUT
        ):
            return b""
        if (
            self.prefetch_pending_id is not None
            and now - self.prefetch_started >= _HISTORY_PREFETCH_TIMEOUT
        ):
            self._timeouts += 1
        request_id = self._allocate_request_id()
        self.prefetch_pending_id = request_id
        self.prefetch_pending_epoch = self.route_epoch
        self.prefetch_started = now
        self._prefetch_requests += 1
        return encode_history_prefetch(request_id, _HISTORY_PREFETCH_LINES)

    def accept_prefetch(self, batch: HistoryBatch) -> HistoryAction:
        if (
            batch.request_id != self.prefetch_pending_id
            or self.prefetch_pending_epoch != self.route_epoch
        ):
            return HistoryAction()
        self.prefetch_pending_id = None
        self.prefetch_pending_epoch = None
        self.prefetch_started = 0.0
        # Replacement is atomic: hidden/removed panes immediately stop being
        # pointer targets, while their bounded text may remain reusable.
        self.visible_routes = batch.snapshots
        self._routes_ready = True
        for snapshot in batch.snapshots:
            if snapshot.pane_id is not None:
                self._remember_content(snapshot)
        routes = {
            route.pane_id: route
            for route in self.visible_routes
            if route.pane_id is not None
        }
        restore_live = False
        for pane_id, viewport in tuple(self.viewports.items()):
            route = routes.get(pane_id)
            if (
                route is None
                or not self._same_geometry(viewport.snapshot, route)
                or not self._history_source_matches(viewport.snapshot, route)
            ):
                self.cancel_pane(pane_id)
                restore_live = True
        return HistoryAction(restore_live=restore_live)

    @staticmethod
    def _history_source_matches(
        left: HistorySnapshot,
        right: HistorySnapshot,
    ) -> bool:
        """Do not combine different providers or terminal generations."""
        return (
            left.transcript_backed == right.transcript_backed
            and left.history_choice_required == right.history_choice_required
            and left.generation == right.generation
        )

    @staticmethod
    def _timeline_delta(
        previous: tuple[bytes, ...],
        incoming: tuple[bytes, ...],
    ) -> int | None:
        """Locate incoming[0] in previous's coordinate space."""
        previous_positions: dict[bytes, list[int]] = {}
        incoming_positions: dict[bytes, list[int]] = {}
        for index, line in enumerate(previous):
            key = _SGR_STYLE_RE.sub(b"", line)
            if key.strip():
                previous_positions.setdefault(key, []).append(index)
        for index, line in enumerate(incoming):
            key = _SGR_STYLE_RE.sub(b"", line)
            if key in previous_positions:
                incoming_positions.setdefault(key, []).append(index)
        votes: dict[int, int] = {}
        for key, positions in incoming_positions.items():
            old_positions = previous_positions[key]
            if len(old_positions) == 1 and len(positions) == 1:
                delta = old_positions[0] - positions[0]
                votes[delta] = votes.get(delta, 0) + 1
        if not votes:
            return None
        best_votes = max(votes.values())
        best = [delta for delta, count in votes.items() if count == best_votes]
        if best_votes < 2 or len(best) != 1:
            return None
        return best[0]

    @staticmethod
    def _line_is_blank(line: bytes) -> bool:
        return not _SGR_STYLE_RE.sub(b"", line).strip()

    def _merge_content(
        self,
        previous: HistorySnapshot,
        incoming: HistorySnapshot,
    ) -> HistorySnapshot:
        """Retain already-fetched history when a later hot capture shrinks."""
        if not self._same_geometry(
            previous, incoming
        ) or not self._history_source_matches(previous, incoming):
            return incoming
        delta = self._timeline_delta(previous.lines, incoming.lines)
        if delta is None:
            # Never splice two captures whose timelines cannot be aligned.
            # Retaining old history and replacing only the live viewport
            # looked conservative, but it silently omitted every row produced
            # between those two pieces. Keep the newest internally contiguous
            # capture instead. An already-frozen viewport owns its immutable
            # snapshot and therefore remains undisturbed by cache replacement.
            lines = incoming.lines
        else:
            start = min(0, delta)
            incoming_end = delta + len(incoming.lines)
            end = max(len(previous.lines), incoming_end)
            if 0 <= incoming_end < len(previous.lines) and all(
                self._line_is_blank(line) for line in previous.lines[incoming_end:]
            ):
                # A temporary full-screen view such as Codex /btw can append
                # an almost-empty live viewport. Once a newer capture anchors
                # before that suffix, the old blank tail is no longer a valid
                # future point on the session timeline.
                end = incoming_end
            merged = [b""] * (end - start)
            old_start = -start
            old_count = min(len(previous.lines), end)
            merged[old_start : old_start + old_count] = previous.lines[:old_count]
            new_start = delta - start
            # Prefer the fresher capture for the overlapping live viewport.
            merged[new_start : new_start + len(incoming.lines)] = incoming.lines
            lines = tuple(merged)
        if len(lines) > self.history_limit:
            lines = lines[-self.history_limit :]
        return replace(incoming, lines=tuple(lines))

    def _remember_content(
        self,
        snapshot: HistorySnapshot,
    ) -> HistorySnapshot:
        assert snapshot.pane_id is not None
        previous = self.content_cache.get(snapshot.pane_id)
        if previous is None:
            stored = snapshot
        elif snapshot.pane_id in self._unverified_after_reconnect:
            # A reconnect can reach a restarted tmux server whose pane IDs
            # happen to match the old server. Retain cached history only when
            # the first fresh capture has a trustworthy timeline anchor.
            anchored = (
                self._same_geometry(previous, snapshot)
                and self._history_source_matches(previous, snapshot)
                and self._timeline_delta(previous.lines, snapshot.lines) is not None
            )
            stored = self._merge_content(previous, snapshot) if anchored else snapshot
            self._unverified_after_reconnect.discard(snapshot.pane_id)
        else:
            stored = self._merge_content(previous, snapshot)
        # Reinsert an existing pane to keep insertion order as recency order.
        self.content_cache.pop(snapshot.pane_id, None)
        self.content_cache[snapshot.pane_id] = stored
        while len(self.content_cache) > _HISTORY_CONTENT_PANES:
            del self.content_cache[next(iter(self.content_cache))]
        return stored

    def _replace_content(self, snapshot: HistorySnapshot) -> HistorySnapshot:
        """Install one verified contiguous capture without timeline merging."""
        assert snapshot.pane_id is not None
        self.content_cache.pop(snapshot.pane_id, None)
        self.content_cache[snapshot.pane_id] = snapshot
        while len(self.content_cache) > _HISTORY_CONTENT_PANES:
            del self.content_cache[next(iter(self.content_cache))]
        return snapshot

    def invalidate_routes(self) -> bool:
        """Drop pointer authority without discarding bounded pane content."""
        was_active = self.cancel()
        self.route_epoch = (self.route_epoch + 1) & 0xFFFFFFFF
        if self.route_epoch == 0:
            self.route_epoch = 1
        self.visible_routes = ()
        self._routes_ready = False
        self.prefetch_pending_id = None
        self.prefetch_pending_epoch = None
        self.prefetch_started = 0.0
        return was_active

    def mark_reconnected(self) -> bool:
        """Preserve bounded text but distrust it until fresh routes re-anchor."""
        was_active = self.invalidate_routes()
        self._unverified_after_reconnect = set(self.content_cache)
        return was_active

    def clear_cache(self) -> None:
        self.invalidate_routes()
        self.content_cache.clear()
        self._unverified_after_reconnect.clear()

    def _route_at(self, event: SgrMouseEvent) -> HistorySnapshot | None:
        return self._route_at_position(event.x - 1, event.y - 1)

    @staticmethod
    def _contains_position(
        snapshot: HistorySnapshot,
        x: int,
        y: int,
    ) -> bool:
        return (
            snapshot.x <= x < snapshot.x + snapshot.width
            and snapshot.y <= y < snapshot.y + snapshot.height
        )

    def _route_at_position(self, x: int, y: int) -> HistorySnapshot | None:
        return next(
            (
                route
                for route in self.visible_routes
                if self._contains_position(route, x, y)
            ),
            None,
        )

    def _near_agent_route(self, event: SgrMouseEvent) -> bool:
        """Recognize the one-cell tmux border around known agent panes."""
        x, y = event.x - 1, event.y - 1
        return any(
            (
                snapshot.x - 1 <= x <= snapshot.x + snapshot.width
                and snapshot.y - 1 <= y <= snapshot.y + snapshot.height
            )
            for snapshot in self.visible_routes
        )

    def pane_id_at_position(self, x: int, y: int) -> str | None:
        route = self._route_at_position(x, y)
        return None if route is None else route.pane_id

    def pane_is_frozen(self, pane_id: str | None) -> bool:
        """Whether one routed pane currently owns a local history viewport."""
        return pane_id is not None and pane_id in self.viewports

    def selection_source(
        self,
        event: SgrMouseEvent,
        live_rows: tuple[bytes, ...],
        *,
        focused_pane_id: str | None = None,
    ) -> SelectionSource | None:
        """Freeze the visible pane rows under one prospective local drag."""
        route = self._route_at(event)
        if route is None or route.pane_id is None:
            return None
        semantic_open = route.pane_id == focused_pane_id
        viewport = self.viewports.get(route.pane_id)
        if viewport is not None:
            return SelectionSource(
                route,
                self._visible_lines(viewport),
                0,
                semantic_open,
            )
        start = route.y
        return SelectionSource(
            route,
            live_rows[start : start + route.height],
            route.x,
            semantic_open,
        )

    @staticmethod
    def _same_geometry(left: HistorySnapshot, right: HistorySnapshot) -> bool:
        return (
            left.pane_id == right.pane_id
            and left.x == right.x
            and left.y == right.y
            and left.width == right.width
            and left.height == right.height
        )

    def _start_history(
        self,
        route: HistorySnapshot,
        event: SgrMouseEvent,
        now: float | None,
        *,
        initial_offset: int = _HISTORY_SCROLL_BASE_LINES,
    ) -> HistoryAction:
        assert route.pane_id is not None
        cached = self.content_cache.get(route.pane_id, route)
        if not self._same_geometry(cached, route):
            cached = route
        loaded_limit = min(len(cached.lines), self.history_limit)
        target_lines = min(self.history_limit, _HISTORY_INITIAL_LINES)
        if (
            cached.more_available
            and loaded_limit < min(target_lines, _HISTORY_PREFETCH_LINES)
        ):
            self.cancel_pane(route.pane_id)
            request_id = self._allocate_request_id()
            self._deep_pending[request_id] = _PendingHistory(
                self.route_epoch,
                route.pane_id,
                target_lines,
                time.monotonic() if now is None else now,
                max(1, initial_offset),
            )
            self._deep_requests += 1
            # A byte-budgeted or older peer may return less than the normal
            # coherent routing capture. Keep the live pane intact until one
            # cumulative response arrives rather than expose a short suffix.
            return HistoryAction(
                protocol_frame=encode_history_request(
                    request_id, event.x, event.y, target_lines
                )
            )
        maximum = max(0, len(cached.lines) - cached.height)
        if maximum == 0:
            if cached.transcript_backed:
                # Claude may not have written its first transcript record yet.
                # This is a normal transient state, not a Railmux status event:
                # keep the live frame intact and swallow the wheel tick instead
                # of painting a local full-width pseudo status bar.
                return HistoryAction()
            return HistoryAction(
                forwarded_input=event.raw if cached.mouse_forwardable else b""
            )
        self.cancel_pane(route.pane_id)
        viewport = _HistoryViewport(
            cached,
            min(maximum, max(1, initial_offset)),
            loaded_limit,
            exhausted=(
                not cached.more_available and loaded_limit >= _HISTORY_INITIAL_LINES
            ),
        )
        self.viewports[route.pane_id] = viewport
        # A complete 300-row routing capture is already one coherent history
        # source. Paint it synchronously, then fetch a cumulative page only as
        # the viewport approaches its top. This removes an SSH round trip from
        # the first wheel tick without reintroducing a hot/deep splice.
        return HistoryAction(
            protocol_frame=self._extend_history(
                viewport,
                now=now,
                request_position=(event.x, event.y),
            ),
            render_history=True,
        )

    def _extend_history(
        self,
        viewport: _HistoryViewport,
        *,
        now: float | None = None,
        request_position: tuple[int, int] | None = None,
    ) -> bytes:
        """Request the next cumulative page when a viewport nears its top."""
        snapshot = viewport.snapshot
        assert snapshot.pane_id is not None
        requested_at = time.monotonic() if now is None else now
        expired = sum(
            pending.pane_id == snapshot.pane_id
            and requested_at - pending.requested_at >= _HISTORY_DEEP_TIMEOUT
            for pending in self._deep_pending.values()
        )
        self._timeouts += expired
        self._deep_pending = {
            request_id: pending
            for request_id, pending in self._deep_pending.items()
            if (
                pending.pane_id != snapshot.pane_id
                or requested_at - pending.requested_at < _HISTORY_DEEP_TIMEOUT
            )
        }
        if (
            viewport.exhausted
            or viewport.loaded_limit >= self.history_limit
            or any(
                pending.pane_id == snapshot.pane_id
                for pending in self._deep_pending.values()
            )
        ):
            return b""
        maximum = max(0, len(snapshot.lines) - snapshot.height)
        if maximum - viewport.offset > _HISTORY_LOAD_AHEAD_LINES:
            return b""
        target_lines = min(
            self.history_limit,
            _HISTORY_INITIAL_LINES
            if viewport.loaded_limit < _HISTORY_INITIAL_LINES
            else viewport.loaded_limit + _HISTORY_PAGE_LINES,
        )
        request_id = self._allocate_request_id()
        self._deep_pending[request_id] = _PendingHistory(
            self.route_epoch,
            snapshot.pane_id,
            target_lines,
            requested_at,
        )
        self._deep_requests += 1
        request_x, request_y = request_position or (
            snapshot.x + 1,
            snapshot.y + 1,
        )
        return encode_history_request(
            request_id,
            request_x,
            request_y,
            target_lines,
        )

    def _scroll_route(
        self,
        route: HistorySnapshot,
        event: SgrMouseEvent,
        direction: int,
        distance: int,
        *,
        now: float | None = None,
    ) -> HistoryAction:
        assert route.pane_id is not None
        if direction > 0 and route.history_choice_required:
            return HistoryAction(claude_history_prompt=event.raw)
        viewport = self.viewports.get(route.pane_id)
        initial_request = next(
            (
                (request_id, pending)
                for request_id, pending in self._deep_pending.items()
                if pending.pane_id == route.pane_id
                and pending.initial_offset is not None
            ),
            None,
        )
        if initial_request is not None:
            request_id, pending = initial_request
            requested_at = time.monotonic() if now is None else now
            if requested_at - pending.requested_at >= _HISTORY_DEEP_TIMEOUT:
                self._deep_pending.pop(request_id, None)
                self._timeouts += 1
                if direction < 0:
                    return HistoryAction()
                assert pending.initial_offset is not None
                distance = min(
                    self.history_limit,
                    pending.initial_offset + max(1, distance),
                )
                initial_request = None
        if viewport is None and initial_request is not None:
            if direction < 0:
                # The live pane never left the screen, so reversing direction
                # simply cancels the pending entry. A late response is ignored.
                self.cancel_pane(route.pane_id)
                return HistoryAction()
            request_id, pending = initial_request
            assert pending.initial_offset is not None
            self._deep_pending[request_id] = replace(
                pending,
                initial_offset=min(
                    self.history_limit,
                    pending.initial_offset + max(1, distance),
                ),
            )
            return HistoryAction()
        if viewport is not None:
            maximum = max(0, len(viewport.snapshot.lines) - viewport.snapshot.height)
            viewport.offset = max(
                0,
                min(
                    maximum,
                    viewport.offset + direction * max(1, distance),
                ),
            )
            if direction < 0:
                viewport.top_notified = False
            if viewport.offset == 0:
                self.cancel_pane(route.pane_id)
                return HistoryAction(restore_live=True)
            protocol_frame = (
                self._extend_history(viewport, now=now) if direction > 0 else b""
            )
            info_message = None
            at_loaded_top = viewport.offset == maximum
            cannot_extend = (
                viewport.exhausted or viewport.loaded_limit >= self.history_limit
            )
            if (
                direction > 0
                and at_loaded_top
                and cannot_extend
                and not viewport.top_notified
            ):
                viewport.top_notified = True
                info_message = (
                    f"History top · {self.history_limit:,}-line local limit"
                    if viewport.loaded_limit >= self.history_limit
                    else (
                        "History top · complete read-only session transcript loaded"
                        if viewport.snapshot.transcript_backed
                        else "History top · complete session history loaded"
                    )
                    if viewport.exhausted
                    else None
                )
            return HistoryAction(
                protocol_frame=protocol_frame,
                render_history=True,
                info_message=info_message,
            )
        # Once a pointer is known to be over an agent pane, the local history
        # layer owns vertical wheel input. The sole fallback is a pane that
        # explicitly enabled mouse reporting: if tmux has no local history,
        # a generic application (for example less) must receive the wheel.
        # A transcript-backed Claude pane remains locally owned even while its
        # first transcript snapshot is unavailable.
        # Non-mouse-aware panes stay isolated from tmux copy-mode.
        if direction < 0:
            if route.transcript_backed:
                return HistoryAction()
            return HistoryAction(
                forwarded_input=event.raw if route.mouse_forwardable else b""
            )
        return self._start_history(
            route,
            event,
            now,
            initial_offset=distance,
        )

    def wheel(
        self,
        event: SgrMouseEvent,
        *,
        now: float | None = None,
    ) -> HistoryAction:
        direction = event.wheel_direction
        if direction == 0:
            return HistoryAction(forwarded_input=event.raw)
        route = self._route_at(event)
        if route is None:
            # Stock tmux WheelUpPane enters copy-mode. Until the current
            # prefetch establishes exact pane geometry, or on the one-cell
            # border around a known agent, dropping a wheel tick is safer than
            # leaking it to tmux and freezing both dual-agent panes through
            # selection isolation. A valid empty route set still forwards
            # modal/sidebar scrolling normally.
            if not self._routes_ready:
                return HistoryAction(refresh_routes=True)
            if self._near_agent_route(event):
                return HistoryAction()
            return HistoryAction(forwarded_input=event.raw)
        return self._scroll_route(
            route,
            event,
            direction,
            _HISTORY_SCROLL_BASE_LINES,
            now=now,
        )

    def page(
        self,
        data: bytes,
        x: int,
        y: int,
        *,
        now: float | None = None,
    ) -> HistoryAction:
        """Page locally when the keyboard cursor is inside a known agent pane."""
        direction = page_key_direction(data)
        route = self._route_at_position(x, y)
        if direction == 0 or route is None:
            return HistoryAction(forwarded_input=data)
        event = SgrMouseEvent(
            data,
            64 if direction > 0 else 65,
            x + 1,
            y + 1,
            True,
        )
        return self._scroll_route(
            route,
            event,
            direction,
            max(1, route.height - 1),
            now=now,
        )

    def pointer_event(
        self,
        event: SgrMouseEvent,
        focused_pane_id: str | None = None,
        status_row: int | None = None,
        now: float | None = None,
    ) -> HistoryAction:
        if status_row is not None and event.y == status_row:
            # The tmux status line is navigation chrome, never agent history.
            # Forward it even if a prior local selection capture missed its
            # release or a stale pane route briefly overlaps the bottom row.
            # A press can switch compact pages, so invalidate route geometry
            # immediately; the next prefetch repopulates the new visible pane.
            changes_page = (
                event.pressed and not event.button & 32 and not event.button & 64
            )
            restore_live = self.invalidate_routes() if changes_page else False
            return HistoryAction(
                forwarded_input=event.raw,
                restore_live=restore_live,
                refresh_routes=changes_page,
            )
        if self._forwarded_pointer_capture:
            if not event.pressed:
                self._forwarded_pointer_capture = False
                self._suppress_forwarded_drag = False
            elif self._suppress_forwarded_drag and event.wheel_direction:
                # Keep agent wheel input local even while a click capture is
                # active. If the pointer has moved to the sidebar, wheel()
                # finds no agent route and preserves normal forwarding.
                return self.wheel(event, now=now)
            elif self._suppress_forwarded_drag and event.button & 32:
                # A press that began over an agent is forwarded so tmux can
                # focus that pane. Do not forward its motion reports: tmux's
                # stock MouseDrag1Pane binding would otherwise enter copy-mode
                # implicitly. Explicit Ctrl-B [ remains opaque keyboard input.
                return HistoryAction()
            return HistoryAction(forwarded_input=event.raw)
        if self._local_pointer_capture:
            if not event.pressed:
                self._local_pointer_capture = False
            return HistoryAction()
        if event.wheel_direction:
            return self.wheel(event, now=now)
        frozen = next(
            (
                viewport.snapshot
                for viewport in self.viewports.values()
                if self._contains_position(viewport.snapshot, event.x - 1, event.y - 1)
            ),
            None,
        )
        if frozen is not None:
            if event.pressed and not event.button & 32:
                if frozen.pane_id != focused_pane_id:
                    self._forwarded_pointer_capture = True
                    self._suppress_forwarded_drag = True
                    return HistoryAction(
                        forwarded_input=event.raw,
                        refresh_routes=True,
                    )
                self._local_pointer_capture = True
            return HistoryAction()
        if event.pressed and not event.button & 32:
            if self._route_at(event) is not None:
                self._forwarded_pointer_capture = True
                self._suppress_forwarded_drag = True
                return HistoryAction(
                    forwarded_input=event.raw,
                    refresh_routes=True,
                )
            restore_live = self.invalidate_routes()
            self._forwarded_pointer_capture = True
            self._suppress_forwarded_drag = False
            return HistoryAction(
                forwarded_input=event.raw,
                restore_live=restore_live,
                refresh_routes=True,
            )
        return HistoryAction()

    def accept(self, snapshot: HistorySnapshot) -> HistoryAction:
        pending = self._deep_pending.pop(snapshot.request_id, None)
        if pending is None:
            return HistoryAction()
        if pending.epoch != self.route_epoch or snapshot.pane_id != pending.pane_id:
            return HistoryAction()
        route = next(
            (
                route
                for route in self.visible_routes
                if route.pane_id == snapshot.pane_id
            ),
            None,
        )
        if (
            route is None
            or not self._same_geometry(route, snapshot)
            or not self._history_source_matches(route, snapshot)
        ):
            return HistoryAction()
        viewport = self.viewports.get(pending.pane_id)
        if viewport is None:
            if pending.initial_offset is None:
                self._remember_content(snapshot)
                return HistoryAction()
            maximum = max(0, len(snapshot.lines) - snapshot.height)
            if maximum == 0:
                self._remember_content(snapshot)
                return HistoryAction()
            stored = self._replace_content(snapshot)
            maximum = max(0, len(stored.lines) - stored.height)
            viewport = _HistoryViewport(
                stored,
                min(maximum, pending.initial_offset),
                min(self.history_limit, max(pending.target_lines, len(stored.lines))),
                exhausted=not snapshot.more_available,
            )
            self.viewports[pending.pane_id] = viewport
            return HistoryAction(
                protocol_frame=self._extend_history(viewport),
                render_history=True,
            )
        maximum = max(0, len(snapshot.lines) - snapshot.height)
        if maximum == 0:
            self._remember_content(snapshot)
            self.cancel_pane(pending.pane_id)
            return HistoryAction(restore_live=True)
        anchor = self._visible_lines(viewport)
        aligned_offset = self._aligned_offset(snapshot, anchor)
        if aligned_offset is None:
            # The live pane moved while the deep capture was in flight and no
            # unique exact visible anchor survived. Keep the immutable hot
            # snapshot instead of jumping to newer or unrelated text.
            self._anchor_rejects += 1
            return HistoryAction()
        # The response is one cumulative capture and its exact visible anchor
        # was just proved above. Replace the cached timeline as a whole instead
        # of merging it with the older hot generation; this keeps every row's
        # text and background styling from one server render.
        stored = self._replace_content(snapshot)
        stored_offset = self._aligned_offset(stored, anchor)
        if stored_offset is None:
            # The raw response was valid, so this can only be an ambiguous
            # cache merge. Preserve the user's frozen viewport and retry later.
            self._anchor_rejects += 1
            return HistoryAction()
        viewport.snapshot = stored
        viewport.offset = stored_offset
        viewport.loaded_limit = min(
            self.history_limit,
            max(pending.target_lines, len(stored.lines)),
        )
        viewport.exhausted = not snapshot.more_available
        return HistoryAction(
            protocol_frame=self._extend_history(viewport),
            render_history=True,
        )

    @staticmethod
    def _visible_lines(viewport: _HistoryViewport) -> tuple[bytes, ...]:
        snapshot = viewport.snapshot
        end = len(snapshot.lines) - viewport.offset
        start = max(0, end - snapshot.height)
        lines = snapshot.lines[start:end]
        if len(lines) < snapshot.height:
            lines = (b"",) * (snapshot.height - len(lines)) + lines
        return lines

    @staticmethod
    def _aligned_offset(
        snapshot: HistorySnapshot,
        anchor: tuple[bytes, ...],
    ) -> int | None:
        if not anchor or len(anchor) > len(snapshot.lines):
            return None
        # Prefer an exact whole-viewport match. This is both cheapest and
        # preserves the strongest ambiguity check when the live pane did not
        # change while the deeper capture was in flight.
        matched_offset: int | None = None
        for start in range(len(snapshot.lines) - len(anchor), -1, -1):
            if snapshot.lines[start : start + len(anchor)] == anchor:
                if matched_offset is not None:
                    return None
                matched_offset = len(snapshot.lines) - (start + len(anchor))
        if matched_offset is not None:
            return matched_offset

        # Agent status rows (for example a Codex spinner) can change between
        # the hot snapshot and a deep response. Requiring the
        # entire visible viewport to remain byte-identical then strands the
        # user at the hot-cache boundary. Align on a majority of stable,
        # non-blank lines instead, but only when each evidence line occurs
        # exactly once on both sides and all winning lines agree on one
        # position. Repeated output and unrelated captures remain rejected.
        anchor_positions: dict[bytes, list[int]] = {}
        snapshot_positions: dict[bytes, list[int]] = {}
        for index, line in enumerate(anchor):
            if _SGR_STYLE_RE.sub(b"", line).strip():
                anchor_positions.setdefault(line, []).append(index)
        for index, line in enumerate(snapshot.lines):
            if line in anchor_positions:
                snapshot_positions.setdefault(line, []).append(index)

        unique_anchor_lines = {
            line: positions[0]
            for line, positions in anchor_positions.items()
            if len(positions) == 1
        }
        votes: dict[int, int] = {}
        latest_start = len(snapshot.lines) - len(anchor)
        for line, anchor_index in unique_anchor_lines.items():
            positions = snapshot_positions.get(line, ())
            if len(positions) != 1:
                continue
            start = positions[0] - anchor_index
            if 0 <= start <= latest_start:
                votes[start] = votes.get(start, 0) + 1

        if not votes:
            return None
        required_votes = max(2, (len(unique_anchor_lines) + 1) // 2)
        best_votes = max(votes.values())
        best_starts = [start for start, count in votes.items() if count == best_votes]
        if best_votes < required_votes or len(best_starts) != 1:
            return None
        start = best_starts[0]
        return len(snapshot.lines) - (start + len(anchor))

    def overlays(
        self,
    ) -> tuple[tuple[HistorySnapshot, tuple[bytes, ...]], ...]:
        return tuple(
            (viewport.snapshot, self._visible_lines(viewport))
            for viewport in self.viewports.values()
        )

    def cancel_pane(self, pane_id: str) -> bool:
        was_active = self.viewports.pop(pane_id, None) is not None
        self._deep_pending = {
            request_id: pending
            for request_id, pending in self._deep_pending.items()
            if pending.pane_id != pane_id
        }
        return was_active

    def cancel_for_input(self, x: int, y: int) -> bool:
        """Restore only the input pane, or all panes if routing is unknown."""
        route = self._route_at_position(x, y)
        if route is None or route.pane_id is None:
            return self.cancel()
        return self.cancel_pane(route.pane_id)

    def cancel(self) -> bool:
        was_active = self.active
        self.viewports.clear()
        self._deep_pending.clear()
        self._local_pointer_capture = False
        self._forwarded_pointer_capture = False
        self._suppress_forwarded_drag = False
        return was_active


def claim_batched_forwarded_wheel(
    event: SgrMouseEvent,
    handled_directions: set[int],
) -> bool:
    """Admit at most one forwarded wheel tick per direction in one read.

    Remote sidebar/modal owners need one bounded event rather than a stale
    backlog. Locally owned agent history must not use this helper: it retains
    the complete reported distance and coalesces only its terminal paint.
    """
    direction = event.wheel_direction
    if direction == 0:
        return True
    if direction in handled_directions:
        return False
    handled_directions.add(direction)
    return True


def input_may_change_routes(
    data: bytes,
    *,
    routes_visible: bool,
    cursor_in_agent: bool = True,
) -> bool:
    """Recognize bounded Railmux layout/modal keys without taxing agent typing."""
    if data in (b"\x1b[I", b"\x1b[O"):
        return False
    if b"\x02" in data:
        return True
    if b"\x1b[19~" in data or b"\x1b[20~" in data or data == b"?":
        return True
    if routes_visible and not cursor_in_agent:
        return True
    return not routes_visible and data in (b"\x1b", b"\r", b"\n")
