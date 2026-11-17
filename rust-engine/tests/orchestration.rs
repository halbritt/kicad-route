use kicad_route_engine::identity::MAX_REQUEST_BYTES;
use serde_json::{json, Value};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    thread,
    time::{Duration, Instant},
};

static TEST_SEQ: AtomicU64 = AtomicU64::new(0);

struct Workspace {
    dir: PathBuf,
}

impl Workspace {
    fn new(name: &str) -> Self {
        let id = TEST_SEQ.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!(
            "kicad-orch-int-{}-{}-{id}",
            std::process::id(),
            name
        ));
        fs::create_dir_all(&dir).unwrap();
        Self { dir }
    }

    fn path(&self, rel: &str) -> PathBuf {
        self.dir.join(rel)
    }

    fn write(&self, rel: &str, bytes: &[u8]) -> PathBuf {
        let p = self.path(rel);
        if let Some(parent) = p.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(&p, bytes).unwrap();
        p
    }
}

impl Drop for Workspace {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

fn engine_bin() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_kicad-route-engine"))
}

fn write_stub_bridge(ws: &Workspace, name: &str, body: &str) -> PathBuf {
    let p = ws.write(name, body.as_bytes());
    let mut perms = fs::metadata(&p).unwrap().permissions();
    perms.set_mode(0o755);
    fs::set_permissions(&p, perms).unwrap();
    p
}

fn standard_stub_script(extra_export: &str, extra_apply: &str) -> String {
    format!(
        r#"#!/usr/bin/env python3
import hashlib, json, os, pathlib, sys
cmd = sys.argv[1]
if cmd == "export":
    board = pathlib.Path(sys.argv[2])
    out = pathlib.Path(sys.argv[3])
    max_exp = int(sys.argv[sys.argv.index("--max-expansions") + 1])
    limits = None
    if "--limits" in sys.argv:
        limits = json.loads(pathlib.Path(sys.argv[sys.argv.index("--limits") + 1]).read_text())
{extra_export}
    source_id = hashlib.sha256(board.read_bytes()).hexdigest()
    req = {{
        "version": 1,
        "source_id": source_id,
        "layer": "F.Cu",
        "bounds": {{"min": [0, 0], "max": [60, 60]}},
        "rules": {{"width_nm": 2, "clearance_nm": 1, "grid_nm": 10, "edge_clearance_nm": 0}},
        "nets": [{{"id": 1, "name": "A"}}],
        "pads": [
            {{"id": "10000000-0000-4000-8000-000000000001", "net": 1, "at": [10, 10], "shape": {{"kind": "circle", "radius_nm": 1}}}},
            {{"id": "10000000-0000-4000-8000-000000000002", "net": 1, "at": [50, 10], "shape": {{"kind": "circle", "radius_nm": 1}}}}
        ],
        "tracks": [],
        "max_expansions": max_exp
    }}
    if limits is not None:
        req["limits"] = limits
    out.write_text(json.dumps(req, separators=(",", ":")))
elif cmd == "apply":
    board = pathlib.Path(sys.argv[2])
    plan_p = pathlib.Path(sys.argv[3])
    out = pathlib.Path(sys.argv[4])
    req_bytes = pathlib.Path(sys.argv[sys.argv.index("--request") + 1]).read_bytes()
    rep_p = pathlib.Path(sys.argv[sys.argv.index("--validation-report") + 1])
    plan = json.loads(plan_p.read_text())
{extra_apply}
    cand = board.read_bytes() + b"\n; routed"
    out.write_bytes(cand)
    report = {{
        "schema": "kicad-route/native-apply-report/1",
        "version": 1,
        "source_id": hashlib.sha256(board.read_bytes()).hexdigest(),
        "request_sha256": hashlib.sha256(req_bytes).hexdigest(),
        "candidate_sha256": hashlib.sha256(cand).hexdigest(),
        "candidate_bytes": len(cand),
        "before_unconnected_count": 1,
        "after_unconnected_count": 0,
        "before_violation_count": 0,
        "after_violation_count": 0,
        "introduced_violation_count": 0,
        "added_track_count": len(plan["added_tracks"]),
        "kicad_version": "stub"
    }}
    rep_p.write_text(json.dumps(report))
"#
    )
}

fn parse_stdout_json(out: &[u8]) -> Value {
    serde_json::from_slice(out).expect("stdout must be valid native-operation JSON")
}

#[test]
fn unique_staging_permissions_and_residue_isolation() {
    let ws = Workspace::new("staging-iso");
    let source = ws.write("board.kicad_pcb", b"(kicad_pcb)");
    let rules = ws.write("rules.json", b"{}");
    let out_dir = ws.path("out");
    fs::create_dir_all(&out_dir).unwrap();
    let foreign_file = out_dir.join(".unrelated-foreign.tmp");
    fs::write(&foreign_file, b"keep me").unwrap();
    let foreign_tmp_dir = ws.path("tmp");
    fs::create_dir_all(&foreign_tmp_dir).unwrap();
    let foreign_pid_dir = foreign_tmp_dir.join(format!("kicad-route-op-{}", std::process::id()));
    fs::create_dir_all(&foreign_pid_dir).unwrap();
    let foreign_pid_marker = foreign_pid_dir.join("marker.txt");
    fs::write(&foreign_pid_marker, b"foreign pid residue").unwrap();

    let mode_file = ws.path("staging_mode.txt");
    let check_mode = format!(
        "    st = os.stat(str(board.parent)).st_mode & 0o777\n    pathlib.Path({:?}).write_text(str(st))\n",
        mode_file
    );
    let bridge = write_stub_bridge(&ws, "bridge.py", &standard_stub_script(&check_mode, ""));
    let output = out_dir.join("routed.kicad_pcb");
    let op_result_file = out_dir.join("op_result.json");
    let save_req_file = out_dir.join("saved_req.json");

    let run = Command::new(engine_bin())
        .env("TMPDIR", &foreign_tmp_dir)
        .arg("route-native")
        .arg(&source)
        .arg(&output)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&bridge)
        .arg("--operation-result")
        .arg(&op_result_file)
        .arg("--save-request")
        .arg(&save_req_file)
        .output()
        .unwrap();
    assert_eq!(
        run.status.code(),
        Some(0),
        "stderr: {}",
        String::from_utf8_lossy(&run.stderr)
    );
    assert_eq!(fs::read_to_string(&mode_file).unwrap(), "448"); // 0o700 == 448
    let op = parse_stdout_json(&run.stdout);
    assert_eq!(op["schema"], "kicad-route/native-operation/1");
    assert_eq!(op["status"], "complete");
    assert_eq!(op["exit_code"], 0);
    assert!(op["binary"]["binary_sha256"].as_str().is_some());
    assert!(op["staging"]["cleaned_up"].as_bool().unwrap());
    assert!(!Path::new(op["staging"]["dir"].as_str().unwrap()).exists());
    assert_eq!(op["publication"]["commit_point_reached"], true);
    assert_eq!(op["publication"]["published"], true);
    assert_eq!(op["publication"]["ambiguous_termination"], false);
    assert!(op_result_file.exists() && save_req_file.exists() && output.exists());
    assert_eq!(fs::read(&foreign_file).unwrap(), b"keep me");
    assert_eq!(
        fs::read(&foreign_pid_marker).unwrap(),
        b"foreign pid residue"
    );
}

#[test]
fn bounded_source_read_exact_16_mib_vs_16_mib_plus_1_before_python() {
    let ws = Workspace::new("bounded-read");
    let rules = ws.write("rules.json", b"{}");
    let poison_marker = ws.path("poison_called");
    let poison_bridge = write_stub_bridge(
        &ws,
        "poison.py",
        &format!(
            "import pathlib, sys\npathlib.Path({:?}).write_text('called')\nsys.exit(99)\n",
            poison_marker
        ),
    );
    let too_big = ws.write("big.kicad_pcb", &vec![b' '; MAX_REQUEST_BYTES + 1]);
    let out_big = ws.path("big_out.kicad_pcb");
    let res_big = Command::new(engine_bin())
        .arg("route-native")
        .arg(&too_big)
        .arg(&out_big)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&poison_bridge)
        .output()
        .unwrap();
    assert_eq!(res_big.status.code(), Some(6));
    assert!(
        !poison_marker.exists(),
        "Python must not be spawned on oversized source"
    );
    assert!(!out_big.exists());
    let op_big = parse_stdout_json(&res_big.stdout);
    assert_eq!(op_big["status"], "admission_exceeded");
    assert!(op_big["source"]["source_sha256"].is_null());

    // Exact 16 MiB is admitted and passes through to bridge.
    let exact_bytes = vec![b' '; MAX_REQUEST_BYTES];
    let exact_src = ws.write("exact.kicad_pcb", &exact_bytes);
    let ok_bridge = write_stub_bridge(&ws, "ok.py", &standard_stub_script("", ""));
    let out_exact = ws.path("exact_out.kicad_pcb");
    let res_exact = Command::new(engine_bin())
        .arg("route-native")
        .arg(&exact_src)
        .arg(&out_exact)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&ok_bridge)
        .output()
        .unwrap();
    assert_eq!(res_exact.status.code(), Some(0));
    assert!(out_exact.exists());
}

#[test]
fn non_regular_source_refused_without_hanging_or_spawning_python() {
    let ws = Workspace::new("non-reg");
    let rules = ws.write("rules.json", b"{}");
    let poison_marker = ws.path("poison_called");
    let poison_bridge = write_stub_bridge(
        &ws,
        "poison.py",
        &format!(
            "import pathlib, sys\npathlib.Path({:?}).write_text('called')\nsys.exit(99)\n",
            poison_marker
        ),
    );
    let dir_src = ws.path("dir.kicad_pcb");
    fs::create_dir_all(&dir_src).unwrap();
    let broken_src = ws.path("broken.kicad_pcb");
    std::os::unix::fs::symlink(ws.path("missing_target.kicad_pcb"), &broken_src).unwrap();
    let fifo_src = ws.path("fifo.kicad_pcb");
    let mkfifo_status = Command::new("mkfifo").arg(&fifo_src).status().unwrap();
    assert!(mkfifo_status.success());

    for (idx, src) in [&dir_src, &broken_src, &fifo_src].iter().enumerate() {
        let out = ws.path(&format!("out-{idx}.kicad_pcb"));
        let res = Command::new(engine_bin())
            .arg("route-native")
            .arg(src)
            .arg(&out)
            .arg("--rules")
            .arg(&rules)
            .arg("--native-bridge")
            .arg(&poison_bridge)
            .arg("--timeout-ms")
            .arg("2000")
            .output()
            .unwrap();
        assert_eq!(res.status.code(), Some(2));
        assert!(!poison_marker.exists());
        assert!(!out.exists());
        let op = parse_stdout_json(&res.stdout);
        assert_eq!(op["status"], "invalid");
    }
}

#[test]
fn sidecar_refusal_on_original_canonical_and_dangling_symlink_before_child_launch() {
    let ws = Workspace::new("sidecars");
    let rules = ws.write("rules.json", b"{}");
    let poison_marker = ws.path("poison_called");
    let poison_bridge = write_stub_bridge(
        &ws,
        "poison.py",
        &format!(
            "import pathlib, sys\npathlib.Path({:?}).write_text('called')\nsys.exit(99)\n",
            poison_marker
        ),
    );

    // Case 1: dangling .kicad_pro symlink beside source
    let src1 = ws.write("c1/board.kicad_pcb", b"(kicad_pcb)");
    std::os::unix::fs::symlink(ws.path("nonexistent.pro"), src1.with_extension("kicad_pro"))
        .unwrap();
    let out1 = ws.path("out1.kicad_pcb");
    let r1 = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&src1)
        .arg(&out1)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&poison_bridge)
        .output()
        .unwrap();
    assert_eq!(r1.status.code(), Some(2));
    assert!(!poison_marker.exists());
    let op1 = parse_stdout_json(&r1.stdout);
    assert_eq!(op1["status"], "unsupported");
    assert_eq!(op1["source"]["sidecar_check"]["kicad_pro_present"], true);
    assert_eq!(op1["source"]["sidecar_check"]["passed"], false);

    // Case 2: symlinked source pointing into project dir containing .kicad_dru
    let real_src = ws.write("proj/real.kicad_pcb", b"(kicad_pcb)");
    ws.write("proj/real.kicad_dru", b"(version 1)");
    let link_src = ws.path("clean/alias.kicad_pcb");
    fs::create_dir_all(link_src.parent().unwrap()).unwrap();
    std::os::unix::fs::symlink(&real_src, &link_src).unwrap();
    let out2 = ws.path("out2.kicad_pcb");
    let r2 = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&link_src)
        .arg(&out2)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&poison_bridge)
        .output()
        .unwrap();
    assert_eq!(r2.status.code(), Some(2));
    assert!(!poison_marker.exists());
    let op2 = parse_stdout_json(&r2.stdout);
    assert_eq!(op2["status"], "unsupported");
    assert_eq!(
        op2["source"]["sidecar_check"]["checked_canonical_path"],
        true
    );
    assert_eq!(op2["source"]["sidecar_check"]["kicad_dru_present"], true);
}

#[test]
fn source_mutation_mid_run_pins_single_t0_snapshot() {
    let ws = Workspace::new("snapshot-pin");
    let original_bytes = b"(kicad_pcb (version t0))";
    let mutated_bytes = b"(kicad_pcb (version t1_mutated))";
    let source = ws.write("board.kicad_pcb", original_bytes);
    let rules = ws.write("rules.json", b"{}");
    let extra_export = format!(
        "    pathlib.Path({:?}).write_bytes({:?}.encode('utf-8'))\n",
        source,
        String::from_utf8_lossy(mutated_bytes).as_ref()
    );
    let bridge = write_stub_bridge(&ws, "bridge.py", &standard_stub_script(&extra_export, ""));
    let output = ws.path("out.kicad_pcb");

    let run = Command::new(engine_bin())
        .arg("route-native")
        .arg(&source)
        .arg(&output)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&bridge)
        .output()
        .unwrap();
    assert_eq!(run.status.code(), Some(0));
    assert_eq!(fs::read(&source).unwrap(), mutated_bytes);
    assert_eq!(
        fs::read(&output).unwrap(),
        b"(kicad_pcb (version t0))\n; routed"
    );
}

#[test]
fn destination_exists_dangling_symlink_same_file_and_concurrent_hard_link_race() {
    let ws = Workspace::new("dest-race");
    let source = ws.write("board.kicad_pcb", b"(kicad_pcb)");
    let rules = ws.write("rules.json", b"{}");
    let bridge = write_stub_bridge(&ws, "bridge.py", &standard_stub_script("", ""));

    // Existing file destination refused before Python
    let existing = ws.write("existing.kicad_pcb", b"incumbent");
    let r_ex = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&existing)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&bridge)
        .output()
        .unwrap();
    assert_eq!(r_ex.status.code(), Some(8));
    assert_eq!(fs::read(&existing).unwrap(), b"incumbent");
    assert_eq!(
        parse_stdout_json(&r_ex.stdout)["status"],
        "destination_exists"
    );

    // Dangling symlink destination refused
    let dangling = ws.path("dangling.kicad_pcb");
    std::os::unix::fs::symlink(ws.path("absent"), &dangling).unwrap();
    let r_dang = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&dangling)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&bridge)
        .output()
        .unwrap();
    assert_eq!(r_dang.status.code(), Some(8));
    assert!(fs::symlink_metadata(&dangling).is_ok());

    // Same-file alias refused
    let alias = ws.path("alias.kicad_pcb");
    std::os::unix::fs::symlink(&source, &alias).unwrap();
    let r_alias = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&alias)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&bridge)
        .output()
        .unwrap();
    assert_eq!(r_alias.status.code(), Some(8));
    assert_eq!(
        parse_stdout_json(&r_alias.stdout)["status"],
        "usage_refused"
    );

    // Concurrent writer race during apply before hard_link
    let race_out = ws.path("raced.kicad_pcb");
    let race_hook = format!(
        "    pathlib.Path({:?}).write_bytes(b'concurrent winner')\n",
        race_out
    );
    let race_bridge =
        write_stub_bridge(&ws, "race_bridge.py", &standard_stub_script("", &race_hook));
    let r_race = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&race_out)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&race_bridge)
        .output()
        .unwrap();
    assert_eq!(r_race.status.code(), Some(8));
    assert_eq!(fs::read(&race_out).unwrap(), b"concurrent winner");
    let op_race = parse_stdout_json(&r_race.stdout);
    assert_eq!(op_race["status"], "destination_exists");
    assert_eq!(op_race["publication"]["published"], false);
    assert!(op_race["staging"]["cleaned_up"].as_bool().unwrap());
    assert!(!fs::read_dir(&ws.dir).unwrap().any(|e| e
        .unwrap()
        .file_name()
        .to_string_lossy()
        .starts_with(".kicad-route-pub-")));
}

#[test]
fn forged_stdout_missing_artifacts_and_mismatched_hashes_are_refused() {
    let ws = Workspace::new("forged-controls");
    let source = ws.write("board.kicad_pcb", b"(kicad_pcb)");
    let rules = ws.write("rules.json", b"{}");

    // 1) apply prints fake complete JSON on stdout and exits 2
    let fake_stdout = write_stub_bridge(
        &ws,
        "fake_stdout.py",
        &standard_stub_script(
            "",
            "    print('{\"status\":\"complete\"}')\n    sys.exit(2)\n",
        ),
    );
    let out1 = ws.path("out1.kicad_pcb");
    let r1 = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&out1)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&fake_stdout)
        .output()
        .unwrap();
    assert_eq!(r1.status.code(), Some(7));
    assert!(!out1.exists());

    // 2) apply exits 0 without writing candidate.kicad_pcb
    let no_cand = write_stub_bridge(
        &ws,
        "no_cand.py",
        &standard_stub_script(
            "",
            "    print('{\"status\":\"complete\"}')\n    sys.exit(0)\n",
        ),
    );
    let out2 = ws.path("out2.kicad_pcb");
    let r2 = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&out2)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&no_cand)
        .output()
        .unwrap();
    assert_eq!(r2.status.code(), Some(7));
    assert!(!out2.exists());

    // 3) apply exits 0 with apply-report claiming unconnected items > 0
    let bad_report = write_stub_bridge(
        &ws,
        "bad_report.py",
        &standard_stub_script(
            "",
            r#"    cand = board.read_bytes() + b"\n; routed"
    out.write_bytes(cand)
    rep_p.write_text(json.dumps({
        "schema": "kicad-route/native-apply-report/1",
        "version": 1,
        "source_id": hashlib.sha256(board.read_bytes()).hexdigest(),
        "request_sha256": hashlib.sha256(req_bytes).hexdigest(),
        "candidate_sha256": hashlib.sha256(cand).hexdigest(),
        "candidate_bytes": len(cand),
        "after_unconnected_count": 1,
        "introduced_violation_count": 0,
        "added_track_count": len(plan["added_tracks"])
    }))
    sys.exit(0)
"#,
        ),
    );
    let out3 = ws.path("out3.kicad_pcb");
    let r3 = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&out3)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&bad_report)
        .output()
        .unwrap();
    assert_eq!(r3.status.code(), Some(2));
    assert_eq!(
        parse_stdout_json(&r3.stdout)["status"],
        "native_validation_failed"
    );
    assert!(!out3.exists());

    // 4) export emits wrong source_id
    let wrong_src = write_stub_bridge(
        &ws,
        "wrong_src.py",
        &standard_stub_script("", "").replace(
            "source_id = hashlib.sha256(board.read_bytes()).hexdigest()",
            "source_id = '0' * 64",
        ),
    );
    let out4 = ws.path("out4.kicad_pcb");
    let r4 = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&out4)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&wrong_src)
        .output()
        .unwrap();
    assert_eq!(r4.status.code(), Some(7));
    assert!(!out4.exists());
}

#[test]
fn missing_python_binary_and_killed_child_sigkill_clean_staging() {
    let ws = Workspace::new("missing-kill");
    let source = ws.write("board.kicad_pcb", b"(kicad_pcb)");
    let rules = ws.write("rules.json", b"{}");
    let bridge = write_stub_bridge(&ws, "bridge.py", &standard_stub_script("", ""));

    let out1 = ws.path("out1.kicad_pcb");
    let r1 = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&out1)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&bridge)
        .arg("--python")
        .arg(ws.path("nonexistent-python"))
        .output()
        .unwrap();
    assert_eq!(r1.status.code(), Some(7));
    let op1 = parse_stdout_json(&r1.stdout);
    assert_eq!(op1["status"], "child_process_error");
    assert!(op1["staging"]["cleaned_up"].as_bool().unwrap());
    assert!(!out1.exists());

    let sigkill_bridge = write_stub_bridge(
        &ws,
        "sigkill.py",
        &standard_stub_script(
            "    import signal\n    os.kill(os.getpid(), signal.SIGKILL)\n",
            "",
        ),
    );
    let out2 = ws.path("out2.kicad_pcb");
    let r2 = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&out2)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&sigkill_bridge)
        .output()
        .unwrap();
    assert_eq!(r2.status.code(), Some(7));
    let op2 = parse_stdout_json(&r2.stdout);
    assert_eq!(op2["status"], "child_process_error");
    assert_eq!(op2["phases"][2]["child"]["signal"], 9);
    assert!(op2["staging"]["cleaned_up"].as_bool().unwrap());
    assert!(!out2.exists());
}

#[test]
fn wall_clock_timeout_kills_stubborn_child_and_grandchild_pgid() {
    let ws = Workspace::new("timeout-pgid");
    let source = ws.write("board.kicad_pcb", b"(kicad_pcb)");
    let rules = ws.write("rules.json", b"{}");
    let pids_file = ws.path("pids.json");
    let stubborn_script = format!(
        r#"import json, os, pathlib, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([
    sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
])
pathlib.Path({:?}).write_text(json.dumps({{"leader": os.getpid(), "grandchild": child.pid}}))
time.sleep(60)
"#,
        pids_file
    );
    let bridge = write_stub_bridge(&ws, "stubborn.py", &stubborn_script);
    let out = ws.path("out.kicad_pcb");
    let t0 = Instant::now();
    let res = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&out)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&bridge)
        .arg("--timeout-ms")
        .arg("500")
        .output()
        .unwrap();
    let elapsed = t0.elapsed();
    assert_eq!(res.status.code(), Some(6));
    assert!(elapsed < Duration::from_secs(5), "took {elapsed:?}");
    let op = parse_stdout_json(&res.stdout);
    assert_eq!(op["status"], "operation_timeout");
    assert_eq!(op["phases"][2]["child"]["timed_out"], true);
    assert!(op["staging"]["cleaned_up"].as_bool().unwrap());
    assert!(!out.exists());

    let pids: Value = serde_json::from_slice(&fs::read(&pids_file).unwrap()).unwrap();
    let leader = pids["leader"].as_i64().unwrap();
    let grandchild = pids["grandchild"].as_i64().unwrap();
    // Wait briefly for OS /proc cleanup after SIGKILL
    thread::sleep(Duration::from_millis(50));
    assert!(
        !Path::new(&format!("/proc/{leader}")).exists(),
        "leader {leader} survived"
    );
    assert!(
        !Path::new(&format!("/proc/{grandchild}")).exists(),
        "grandchild {grandchild} survived"
    );
}

#[test]
fn bounded_stdout_and_stderr_overflow_kill_child_promptly() {
    let ws = Workspace::new("output-overflow");
    let source = ws.write("board.kicad_pcb", b"(kicad_pcb)");
    let rules = ws.write("rules.json", b"{}");

    for stream in ["stdout", "stderr"] {
        let flood = format!(
            r#"import sys, time
target = sys.{stream}.buffer
chunk = b"X" * 65536
for _ in range(1024):
    try:
        target.write(chunk)
        target.flush()
    except BrokenPipeError:
        break
time.sleep(60)
"#
        );
        let bridge = write_stub_bridge(&ws, &format!("flood_{stream}.py"), &flood);
        let out = ws.path(&format!("out_{stream}.kicad_pcb"));
        let t0 = Instant::now();
        let res = Command::new(engine_bin())
            .args(["route-native"])
            .arg(&source)
            .arg(&out)
            .arg("--rules")
            .arg(&rules)
            .arg("--native-bridge")
            .arg(&bridge)
            .arg("--max-child-output-bytes")
            .arg("4096")
            .arg("--timeout-ms")
            .arg("30000")
            .output()
            .unwrap();
        let elapsed = t0.elapsed();
        assert_eq!(res.status.code(), Some(6));
        assert!(elapsed < Duration::from_secs(5), "took {elapsed:?}");
        let op = parse_stdout_json(&res.stdout);
        assert_eq!(op["status"], "child_output_exceeded");
        let field = format!("{stream}_truncated");
        assert_eq!(op["phases"][2]["child"][&field], true);
        assert!(op["staging"]["cleaned_up"].as_bool().unwrap());
        assert!(!out.exists());
    }
}

#[test]
fn malformed_non_utf8_child_stderr_does_not_panic() {
    let ws = Workspace::new("non-utf8");
    let source = ws.write("board.kicad_pcb", b"(kicad_pcb)");
    let rules = ws.write("rules.json", b"{}");
    let bridge = write_stub_bridge(
        &ws,
        "non_utf8.py",
        r#"import sys
sys.stderr.buffer.write(b'{"status":\xff\xfe\x00broken')
sys.stderr.buffer.flush()
sys.exit(2)
"#,
    );
    let out = ws.path("out.kicad_pcb");
    let res = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&out)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&bridge)
        .output()
        .unwrap();
    assert_eq!(res.status.code(), Some(7));
    let op = parse_stdout_json(&res.stdout);
    assert_eq!(op["status"], "child_process_error");
    assert!(op["phases"][2]["child"]["stderr_tail"].as_str().is_some());
    assert!(op["staging"]["cleaned_up"].as_bool().unwrap());
    assert!(!out.exists());
}

#[test]
fn sigterm_and_sigint_cancel_child_pgid_and_clean_staging() {
    let ws = Workspace::new("parent-signals");
    let source = ws.write("board.kicad_pcb", b"(kicad_pcb)");
    let rules = ws.write("rules.json", b"{}");

    for (idx, sig) in ["15", "2"].iter().enumerate() {
        let ready = ws.path(&format!("ready_{idx}.json"));
        let bridge_script = format!(
            r#"import json, os, pathlib, signal, subprocess, sys, time
grand = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path({:?}).write_text(json.dumps({{"leader": os.getpid(), "grandchild": grand.pid}}))
time.sleep(60)
"#,
            ready
        );
        let bridge = write_stub_bridge(&ws, &format!("slow_{idx}.py"), &bridge_script);
        let out = ws.path(&format!("out_{idx}.kicad_pcb"));
        let child = Command::new(engine_bin())
            .args(["route-native"])
            .arg(&source)
            .arg(&out)
            .arg("--rules")
            .arg(&rules)
            .arg("--native-bridge")
            .arg(&bridge)
            .arg("--timeout-ms")
            .arg("30000")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();

        let wait_start = Instant::now();
        while !ready.exists() && wait_start.elapsed() < Duration::from_secs(5) {
            thread::sleep(Duration::from_millis(20));
        }
        assert!(ready.exists(), "stub child never became ready");
        let pids: Value = serde_json::from_slice(&fs::read(&ready).unwrap()).unwrap();
        let leader = pids["leader"].as_i64().unwrap();
        let grandchild = pids["grandchild"].as_i64().unwrap();

        let kill_st = Command::new("kill")
            .arg(format!("-{sig}"))
            .arg(child.id().to_string())
            .status()
            .unwrap();
        assert!(kill_st.success());

        let finished = child.wait_with_output().unwrap();
        assert_eq!(finished.status.code(), Some(6));
        let op = parse_stdout_json(&finished.stdout);
        assert_eq!(op["status"], "cancelled");
        assert!(op["staging"]["cleaned_up"].as_bool().unwrap());
        assert!(!Path::new(op["staging"]["dir"].as_str().unwrap()).exists());
        assert!(!out.exists());

        thread::sleep(Duration::from_millis(50));
        assert!(!Path::new(&format!("/proc/{leader}")).exists());
        assert!(!Path::new(&format!("/proc/{grandchild}")).exists());
    }
}

#[test]
fn work_budget_and_wall_clock_separation_and_legacy_cli_compat() {
    let ws = Workspace::new("budget-sep");
    let source = ws.write("board.kicad_pcb", b"(kicad_pcb)");
    let rules = ws.write("rules.json", b"{}");
    let limits_zero = ws.write("limits_zero.json", b"{\"max_work\": 0}");
    let bridge = write_stub_bridge(&ws, "bridge.py", &standard_stub_script("", ""));

    // 1) --max-expansions 1 -> exit 4 budget_exhausted
    let out_exp = ws.path("out_exp.kicad_pcb");
    let r_exp = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&out_exp)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&bridge)
        .arg("--max-expansions")
        .arg("1")
        .output()
        .unwrap();
    assert_eq!(r_exp.status.code(), Some(4));
    assert_eq!(
        parse_stdout_json(&r_exp.stdout)["status"],
        "budget_exhausted"
    );
    assert!(!out_exp.exists());

    // 2) --limits max_work 0 -> exit 5 work_exhausted
    let out_work = ws.path("out_work.kicad_pcb");
    let r_work = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&out_work)
        .arg("--rules")
        .arg(&rules)
        .arg("--limits")
        .arg(&limits_zero)
        .arg("--native-bridge")
        .arg(&bridge)
        .output()
        .unwrap();
    assert_eq!(r_work.status.code(), Some(5));
    assert_eq!(
        parse_stdout_json(&r_work.stdout)["status"],
        "work_exhausted"
    );
    assert!(!out_work.exists());

    // 3) Different wall-clock timeouts produce identical deterministic routing summary
    let out_a = ws.path("out_a.kicad_pcb");
    let out_b = ws.path("out_b.kicad_pcb");
    let r_a = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&out_a)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&bridge)
        .arg("--timeout-ms")
        .arg("15000")
        .output()
        .unwrap();
    let r_b = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&out_b)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&bridge)
        .arg("--timeout-ms")
        .arg("60000")
        .output()
        .unwrap();
    assert_eq!(r_a.status.code(), Some(0));
    assert_eq!(r_b.status.code(), Some(0));
    let op_a = parse_stdout_json(&r_a.stdout);
    let op_b = parse_stdout_json(&r_b.stdout);
    assert_ne!(op_a["operation_id"], op_b["operation_id"]);
    assert_eq!(op_a["routing"], op_b["routing"]);
    assert_eq!(
        op_a["publication"]["output_sha256"],
        op_b["publication"]["output_sha256"]
    );

    // 4) Pre-existing --operation-result refused without overwriting
    let existing_op = ws.write("existing_op.json", b"preserve-op");
    let out_c = ws.path("out_c.kicad_pcb");
    let r_c = Command::new(engine_bin())
        .args(["route-native"])
        .arg(&source)
        .arg(&out_c)
        .arg("--rules")
        .arg(&rules)
        .arg("--native-bridge")
        .arg(&bridge)
        .arg("--operation-result")
        .arg(&existing_op)
        .output()
        .unwrap();
    assert_eq!(r_c.status.code(), Some(8));
    assert_eq!(fs::read(&existing_op).unwrap(), b"preserve-op");
    assert!(!out_c.exists());

    // 5) Legacy 2-arg INPUT.json OUTPUT.json still overwrites existing OUTPUT.json
    let req = json!({
        "version": 1,
        "source_id": "a".repeat(64),
        "layer": "F.Cu",
        "bounds": {"min": [0, 0], "max": [60, 60]},
        "rules": {"width_nm": 2, "clearance_nm": 1, "grid_nm": 10, "edge_clearance_nm": 0},
        "nets": [{"id": 1, "name": "A"}],
        "pads": [
            {"id": "10000000-0000-4000-8000-000000000001", "net": 1, "at": [10, 10], "shape": {"kind": "circle", "radius_nm": 1}},
            {"id": "10000000-0000-4000-8000-000000000002", "net": 1, "at": [50, 10], "shape": {"kind": "circle", "radius_nm": 1}}
        ],
        "tracks": [],
        "max_expansions": 1000
    });
    let leg_in = ws.write("leg_in.json", serde_json::to_vec(&req).unwrap().as_slice());
    let leg_out = ws.write("leg_out.json", b"old-result");
    let leg_run = Command::new(engine_bin())
        .arg(&leg_in)
        .arg(&leg_out)
        .output()
        .unwrap();
    assert_eq!(leg_run.status.code(), Some(0));
    let leg_val: Value = serde_json::from_slice(&fs::read(&leg_out).unwrap()).unwrap();
    assert_eq!(leg_val["version"], 3);
    assert_eq!(leg_val["status"], "complete");
}
