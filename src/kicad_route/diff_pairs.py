"""Opt-in pair detection, sequential KRT passes, and native candidate evidence."""
import math
import shutil
import time

from . import common, length_groups, netnames, stall

NM = 1_000_000


def matching_settings(args):
    """Validate opt-in search controls independently of native design rules."""
    enabled = getattr(args, "diff_pair_intra_match", False)
    tolerance = getattr(args, "length_match_tolerance", None)
    amplitude = getattr(args, "meander_amplitude", None)
    if not enabled:
        if tolerance is not None or amplitude is not None:
            raise ValueError("matching controls require --diff-pair-intra-match")
        return None
    if getattr(args, "diff_pairs", None) != "auto":
        raise ValueError("--diff-pair-intra-match requires --diff-pairs auto")
    result = {"tolerance_mm": 0.1 if tolerance is None else tolerance,
              "amplitude_mm": 1.0 if amplitude is None else amplitude}
    if any(not math.isfinite(value) or value < 0.000001 for value in result.values()):
        raise ValueError("matching tolerance and amplitude must be finite and at least 0.000001 mm")
    return result


def probe(board, output, api):
    rec = common.run_cmd([api.pcbnew_python(), api.KICAD_SCRIPTS / "pairs.py", board, output],
                         board.parent, output.with_suffix(".log"), timeout=600)
    return (common.load_json(output) if rec["exit_code"] == 0 else None), common.public(rec)


def detect(nets, scope):
    """Canonical KiCad suffixes; retain sheet paths and suffix conventions."""
    pairs = {}
    for name in sorted(scope):
        if name.endswith(("_P", "_N")):
            p, n = name[:-2] + "_P", name[:-2] + "_N"
        elif name.endswith(("+", "-")):
            p, n = name[:-1] + "+", name[:-1] + "-"
        elif name.endswith(("P", "N")) and name[:-1] + ("N" if name[-1] == "P" else "P") in nets:
            p, n = name[:-1] + "P", name[:-1] + "N"
        else:
            continue
        if p not in nets or n not in nets:
            raise ValueError(f"unpaired differential net {name!r}: expected {n if p == name else p!r}")
        if p not in scope or n not in scope:
            raise ValueError(f"differential pair {p!r}, {n!r} is split by the net selection")
        pos, neg = nets[p], nets[n]
        if pos["netclass"] != neg["netclass"]:
            raise ValueError(f"differential pair {p!r}, {n!r} has mismatched effective net classes")
        for key in ("width", "gap"):
            if (pos[key] is None or neg[key] != pos[key] or not math.isfinite(pos[key]) or pos[key] <= 0):
                raise ValueError(f"differential pair {p!r}, {n!r} needs matching positive native diff-pair {key}")
        if pos["gap"] < max(pos["clearance"] or 0, neg["clearance"] or 0):
            raise ValueError(f"differential pair {p!r}, {n!r}: native gap is below class clearance")
        if min(pos["pad_count"], neg["pad_count"]) < 2:
            raise ValueError(f"differential pair {p!r}, {n!r} needs at least two pads per member")
        pairs[p] = {"p": p, "n": n, "netclass": pos["netclass"],
                    "width_mm": pos["width"] / NM, "gap_mm": pos["gap"] / NM}
    return [pairs[p] for p in sorted(pairs)]


def open_nets(drc, known):
    known = set(known)
    found = set()
    for item in drc["unconnected_items"]:
        names = {n for i in item.get("items", [])
                 for n in netnames.nets_in_description(i.get("description", ""), known)}
        # Unidentified connections cannot establish that a pair is connected.
        found.update((names & known) or known)
    return found


def grade(pairs, outcomes, nets, drc, matching=None):
    """Missing native evidence stays unknown; backend claims never establish connectivity."""
    opened = open_nets(drc, nets) if nets is not None and drc is not None else None
    records = []
    for pair in pairs:
        p, n = pair["p"], pair["n"]
        outcome = outcomes.get(p, {})
        lengths = [round(nets[name]["length"] / NM, 6) if nets is not None and name in nets else None
                   for name in (p, n)]
        gaps = None
        if drc is not None:
            gaps = 0
            for violation in drc["violations"]:
                if violation["type"] != "diff_pair_gap_out_of_range":
                    continue
                names = {name for item in violation.get("items", [])
                         for name in netnames.nets_in_description(item.get("description", ""), nets or {})}
                if nets is not None:
                    names &= set(nets)
                if not names or {p, n} & names:
                    gaps += 1
        coupled = outcome.get("status") in ("COUPLED", "ALREADY_CONNECTED")
        routed = bool(coupled and opened is not None and not {p, n} & opened and
                      all(length is not None for length in lengths) and gaps == 0)
        match = None
        if matching is not None:
            skew = abs(nets[p]["length"] - nets[n]["length"]) / NM if None not in lengths else None
            matched = skew is not None and skew <= matching["tolerance_mm"]
            match = {"tolerance_mm": matching["tolerance_mm"],
                     "status": "PASS" if matched else "FAIL" if skew is not None else "NOT_RUN",
                     "measurement": "saved native copper length"}
        records.append({**pair, "routed": routed,
                        "length_p": lengths[0], "length_n": lengths[1],
                        "skew": round(abs(lengths[0] - lengths[1]), 6) if None not in lengths else None,
                        "gap_violations": gaps,
                        "status": "ROUTED" if routed else "INCOMPLETE" if opened is not None else "NOT_RUN",
                        "backend_status": outcome.get("status", "NOT_RUN"),
                        "backend_report": outcome.get("report"),
                        **({"matching": match} if match is not None else {})})
    return records


def apply_routing(routing, records):
    failed = {name for pair in records if not pair["routed"] for name in (pair["p"], pair["n"])}
    if failed and routing["status"] != "NOT_RUN":
        routing["status"] = "INCOMPLETE"
        routing["unrouted_nets"] = sorted(set(routing.get("unrouted_nets", [])) | failed)


def route(work, board, scope, facts, settings, api):
    """One isolated invocation per pair, then an exact scope excluding EVERY pair member."""
    started = time.monotonic()
    commands, restores, outcomes = [], [], {}
    proc = {"status": "PASS", "reasons": [], "commands": commands, "exit_code": 0,
            "output_board": board, "pair_routing": outcomes, "unsupported_reported": []}

    def fail(reason):
        proc.update(status="FAIL", reasons=[reason], output_board=None)
        return proc

    def lock(target, directory, only=None):
        record = directory / "locks.json"
        argv = [api.pcbnew_python(), api.KICAD_SCRIPTS / "locks.py", "lock", target, record]
        if only is not None:
            argv += ["--only", only]
        rec = common.run_cmd(argv, directory, directory / "lock.log", timeout=600)
        commands.append(common.public(rec))
        if rec["exit_code"] != 0:
            return False
        restores.append(record)
        return True

    def lock_pairs(target, directory, pairs):
        keys_path = directory / "keys.json"
        rec = common.run_cmd([api.pcbnew_python(), api.KICAD_SCRIPTS / "locks.py", "keys", target, keys_path],
                             directory, directory / "keys.log", timeout=600)
        commands.append(common.public(rec))
        if rec["exit_code"] != 0:
            return False
        keys = common.load_json(keys_path)["keys"]
        members = {name for pair in pairs for name in (pair['p'], pair['n'])}
        common.write_json(keys_path, [key for key in keys if key[1] in members])
        return lock(target, directory, keys_path)

    # Protect the design's copper before the first pair pass, using the same designation as route.py.
    initial = work / "diff-input"
    initial.mkdir()
    if settings["protect"] in ("existing", "human"):
        only = work.parent / settings["protect_only"] if settings.get("protect_only") else None
        if not lock(board, initial, only):
            return fail("could not lock existing copper before differential routing")
    current = board
    paired = {name for pair in settings['pairs'] for name in (pair['p'], pair['n'])}
    matching_groups = settings.get('length_match')
    coupled_groups = ([group for group in matching_groups['groups'] if set(group) <= paired]
                      if matching_groups else [])
    # Centerlines exist only inside one backend invocation. Keep the selected
    # pair group together until tuning finishes; ordinary routing stays sequential.
    batches = [settings['pairs']] if coupled_groups else [[pair] for pair in settings['pairs']]
    for index, batch in enumerate(batches):
        names = [name for pair in batch for name in (pair['p'], pair['n'])]
        directory = work / f"diff-{index:03d}"
        directory.mkdir()
        existing = [pair for pair in batch if pair.get('initially_connected')]
        pending = [pair for pair in batch if not pair.get('initially_connected')]
        if existing:
            for pair in existing:
                outcomes[pair['p']] = {"status": "ALREADY_CONNECTED"}
            protected = directory / 'existing'
            protected.mkdir()
            if not lock_pairs(current, protected, existing):
                return fail("could not protect pre-existing differential copper")
        if not pending:
            continue
        remaining = settings["budget_s"] - (time.monotonic() - started)
        if remaining <= 0:
            for pair in pending:
                outcomes[pair['p']] = {"status": "BUDGET_EXHAUSTED"}
            continue
        api.copy_project(current.parent, directory / "input")
        source = directory / "input" / board.name
        output = directory / board.name
        argv = [api.route_python(), api.KRT_DIR / "py_router" / "route_diff.py", source,
                "--output", output, "--no-short-pair-deferral", "--no-fix-drc-settings",
                "--keep-input-copper", "--no-stub-layer-swap", "--no-gnd-vias"]
        geometries = {(pair['width_mm'], pair['gap_mm']) for pair in batch}
        if len(geometries) == 1:
            width, gap = next(iter(geometries))
            argv += ['--track-width', str(width), '--diff-pair-gap', str(gap)]
        # Otherwise the backend resolves each pair's authored native class.
        matching = settings.get("intra_pair_match")
        if settings.get("pair_layers") is not None:
            argv += ["--layers", *settings["pair_layers"]]
        if matching is not None:
            argv += ["--diff-pair-intra-match", "--length-match-tolerance", str(matching["tolerance_mm"]),
                     "--meander-amplitude", str(matching["amplitude_mm"])]
        if coupled_groups:
            argv += length_groups.backend_args({**matching_groups, 'groups': coupled_groups})
        if settings.get("max_iterations"):
            argv += ["--max-iterations", str(settings["max_iterations"])]
        if settings.get("grid_step_mm") is not None:
            argv += ["--grid-step", str(settings["grid_step_mm"])]
        if settings.get("default_clearance_mm"):
            default = max((net["clearance"] or 0 for net in facts["nets"].values()
                           if net["netclass"] == "Default"), default=0) / NM
            argv += ["--clearance", str(max(default, settings["default_clearance_mm"]))]
        argv += ["--nets", *netnames.krt_patterns(names, facts["nets"])]
        env = {**api.KRT_ENV, **api.KRT_PROTECT_EXISTING_ENV, **settings["backend_env"]}
        env.update(api.krt_artwork_environment(directory, source, facts))
        log = directory / "route.log"
        rec = api.run_budgeted(argv, directory, log, remaining, env,
                               monitor=stall.Monitor("krt", log, settings.get("stall_s")))
        commands.append(rec)
        proc["exit_code"] = rec.get("exit_code")
        text = log.read_text(errors="replace") if log.exists() else ""
        proc["unsupported_reported"].extend(api.krt_unsupported_rules(text))
        report = api.tagged_json(text, "JSON_SUMMARY")
        if rec.get("error") or rec.get("killed") or rec.get("exit_code") != 0 or not output.exists():
            for pair in pending:
                outcomes[pair['p']] = {"status": "PROCESS_FAILED"}
            return fail(f"KRT differential router failed for {names} (see {common.rel(log)})")
        if not isinstance(report, dict) or not isinstance(report.get("pair_reports"), list):
            for pair in pending:
                outcomes[pair['p']] = {"status": "PROCESS_FAILED"}
            return fail(f"KRT differential router wrote no usable pair report for {names}")
        accepted = True
        for pair in pending:
            p, n = pair['p'], pair['n']
            member = next((r for r in report["pair_reports"] if isinstance(r, dict) and
                           (r.get("p_net"), r.get("n_net")) == (p, n)), None)
            if member is None:
                for affected in pending:
                    outcomes[affected['p']] = {"status": "PROCESS_FAILED", "report": report}
                return fail(f"KRT differential router omitted the pair report for {p}, {n}")
            accepted = accepted and (member.get("outcome") == "coupled" and
                    member.get("escape") != "hybrid" and
                    member.get("coupled_terminals") is not False and
                    not member.get("member_audit_mismatch") and not member.get("no_coupled_trunk") and
                    not member.get("incomplete_members") and
                    not (report.get("single_ended_fallback") or {}).get("routed"))
        for pair in pending:
            outcomes[pair['p']] = {"status": "COUPLED" if accepted else "REJECTED", "report": report}
        if not accepted:
            continue  # Discard internal single-ended fallback and incomplete output, including its side effects.
        # Carry ORIGINAL project rules to the next pass, never KRT's sibling writebacks.
        api.copy_project(current.parent, directory / "accepted")
        current = directory / "accepted" / board.name
        shutil.copyfile(output, current)
        if not lock_pairs(current, directory, batch):
            return fail("could not protect differential copper before the next routing pass")

    paired = {name for pair in settings["pairs"] for name in (pair["p"], pair["n"])}
    single = sorted(set(scope) - paired)
    remaining = settings["budget_s"] - (time.monotonic() - started)
    if single and remaining > 0:
        directory = work / "single"
        directory.mkdir()
        tail_settings = {**settings, "diff_pairs": None, "mode": "selected", "budget_s": remaining}
        if matching_groups:
            groups = [group for group in matching_groups['groups'] if set(group) <= set(single)]
            if groups:
                tail_settings['length_match'] = {**matching_groups, 'groups': groups}
            else:
                tail_settings.pop('length_match', None)
        # Earlier pair passes already applied --protect human's original designation.
        tail_settings.pop("protect_only", None)
        if settings["protect"] == "human":
            tail_settings["protect"] = "locked"
        tail = api.krt_route(directory, current, single, facts, tail_settings)
        commands.extend(tail.pop("commands", []))
        proc["unsupported_reported"].extend(tail.pop("unsupported_reported", []))
        proc.update({k: v for k, v in tail.items() if k != "commands"})
        if proc["status"] != "PASS":
            return proc
        current = proc["output_board"]
    # Always return a board, even when every pair failed normally: native grading decides exit 2/3.
    output_dir = work / "diff-out"
    api.copy_project(board.parent, output_dir)
    output = output_dir / board.name
    shutil.copyfile(current, output)
    for index, record in enumerate(reversed(restores)):
        rec = common.run_cmd([api.pcbnew_python(), api.KICAD_SCRIPTS / "locks.py", "restore", output, record],
                             work, work / f"diff-restore-{index}.log", timeout=600)
        commands.append(common.public(rec))
        if rec["exit_code"] != 0:
            return fail("could not restore differential routing lock flags")
    proc["output_board"] = output
    return proc
