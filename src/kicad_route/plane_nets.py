"""The nets that own a plane get engine passes of their own, before and after the others
(run --plane-nets).

A net that owns a copper zone on an inner layer is connected through that plane: each
surface-mount pad of it needs a via beside it, and beside a fine-pitch part the room for that
via is room the pad's neighbours want as well. The built-in engine takes its nets in an order
of its own and reaches the pads of the plane nets after the signals. On a four-layer board
with a 100-pin and a 144-pin QFP at 0.5 mm, 50 of the 56 connections a 300 s run left open
were pads of the 3.3 V and ground planes, boxed in by signal tracks, and one hour of routing
still left 17 of them. A designer places those vias before any signal is routed, and looks at
the planes again when the signals are in.

`--plane-nets first` does both with the engine itself, inside the run's budget:

    first pass   the plane nets of the scope alone, on the board as it came
    the run's    the whole scope, on that board
    last pass    the nets that own a zone, on the routed board, when the budget ended the
                 run's own pass before the engine's closing repair of planes and pours
                 (signal tracks may have cut a pour off from a pad, and nothing has looked
                 since), in the time the stop request leaves before the budget ends

The last pass does not depend on the option: it is what the budget took away, given back. It
covers every net of the scope that owns a zone, on any layer. On a two-layer board a ground
pad of a through-hole part sat on a piece of pour that signal tracks had ringed off, four
spokes into an island, and the run had been stopped in its net rescue with the repair that
joins such pieces still ahead.

On the same four-layer board, with 300 processor seconds: 14 connections open, none of them a
plane pad, where one pass leaves 56.

Only inner layers count. On a two-layer board a pour fills what the tracks leave, and its
nets routed first take the room of thermal spokes: a board that came out clean in one pass
had a starved thermal with its two pour nets first, another seven where it had one.

The extra passes are a preparation and a repair. When one fails, or writes no board, the run
goes on with what it has and says so. A run that routes buses or matches lengths keeps its
one pass: those options name nets the extra passes would not have in their scope.
"""
MODES = ("together", "first")
# One pass is the default. The passes of their own cost what they take from the signals. On the
# three four-layer boards of the router lab whose scope holds plane nets, at 300 processor
# seconds, `first` left 14, 6 and 123 connections open where one pass leaves 56, 1 and 83; at
# 600 seconds 12, 4 and 76 where one pass leaves 23, 1 and 78. (The 123 is the budget stopping
# the taps three seconds earlier, not the order.) The first board is the one whose open
# connections were plane pads, 50 of 56. A result says when a board is of that kind
# (routing.plane_nets_open), which is when to ask for `first`.
DEFAULT = "together"
# The first pass may use a third of the budget and is the engine's whole procedure on the
# plane nets, its closing plane repair included: on an empty board that left no plane pad open
# where the engine's main pass alone (its --quick) left seven that the signals then boxed in.
# A third, not half: the pass is asked to stop a quarter before its end, and what it does not
# use goes to the run's own pass, whose time decides the signals of a board the budget cuts
# short (with half, 136 open connections where a third leaves 83).
FIRST_SHARE = 1 / 3
FIRST_PASS_ARGS = ()
# The last pass takes nothing from the run's own pass. A run is asked to stop a quarter of its
# budget (at most 120 s) before the end and is out some 10 s later (median of 19 stopped
# passes; 58 s the longest), so that time is there. Given three tenths of the budget instead,
# the signals of a board the budget cuts short paid for it: 136 open connections where the
# run has 83 without the extra passes. The last pass is asked to stop a tenth before its own
# end; if it is stopped or killed the run keeps the board it had.
LAST_MIN_S = 10
LAST_RESERVE_SHARE = 0.1


def owners(facts):
    """The nets that own a copper zone on an inner layer of the board."""
    layers = [layer["name"] if isinstance(layer, dict) else layer for layer in facts.get("copper_layers") or []]
    inner = set(layers[1:-1])
    return {zone["net"] for zone in facts.get("zones") or [] if zone.get("net") and inner & set(zone.get("layers") or [])}


def nets(scope, facts, settings):
    """The plane nets that get passes of their own, sorted; empty when the run has one pass."""
    if settings.get("plane_nets", DEFAULT) != "first":
        return []
    if settings.get("bus") is not None or settings.get("length_match") is not None:
        return []
    scope = set(scope)
    chosen = sorted(scope & owners(facts))
    return chosen if chosen and len(chosen) < len(scope) else []


def zone_owners(facts):
    """The nets that own a copper zone on any layer of the board."""
    return {zone["net"] for zone in facts.get("zones") or [] if zone.get("net")}


def repair_nets(scope, facts, settings):
    """The zone-owning nets of the scope, sorted: the nets of the last pass. Empty when a run
    has no such pass (buses and matched lengths, as for the first pass)."""
    if settings.get("bus") is not None or settings.get("length_match") is not None:
        return []
    return sorted(set(scope) & zone_owners(facts))


def first_budget(budget):
    """Seconds of `budget` the first pass may use (at least one)."""
    return max(1, int(budget * FIRST_SHARE))


def main_budget(budget, first_used):
    """Seconds for the run's own pass: all the first pass left (at least one)."""
    return max(1, int(budget - (first_used or 0)))


def last_budget(budget, first_used, main_used):
    """Seconds left for the last pass, or None when that is too little to start one."""
    left = int(budget - (first_used or 0) - (main_used or 0))
    return left if left >= LAST_MIN_S else None


def last_reserve(budget):
    """How long before the end of its budget the last pass is asked to stop."""
    return budget * LAST_RESERVE_SHARE
