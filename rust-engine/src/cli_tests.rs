use super::*;
use kicad_route_engine::{model::Input, run_json, Engine};
use serde_json::{json, Value};
use std::{
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};

static NEXT: AtomicU64 = AtomicU64::new(0);
struct Files(PathBuf);
impl Files {
    fn new() -> Self {
        let dir = std::env::temp_dir().join(format!(
            "kicad-cli-boundary-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&dir).unwrap();
        Self(dir)
    }
    fn args(&self) -> Vec<OsString> {
        vec![
            "engine".into(),
            self.0.join("in.json").into_os_string(),
            self.0.join("out.json").into_os_string(),
        ]
    }
    fn seed(&self) -> Vec<u8> {
        let bytes = serde_json::to_vec(&json!({
            "version":1,"source_id":"a".repeat(64),"layer":"F.Cu",
            "bounds":{"min":[0,0],"max":[60,60]},
            "rules":{"width_nm":2,"clearance_nm":1,"grid_nm":10,"edge_clearance_nm":0},
            "nets":[{"id":1,"name":"A"}],
            "pads":[
                {"id":"10000000-0000-4000-8000-000000000001","net":1,"at":[10,10],"shape":{"kind":"circle","radius_nm":1}},
                {"id":"10000000-0000-4000-8000-000000000002","net":1,"at":[50,10],"shape":{"kind":"circle","radius_nm":1}}
            ],"tracks":[],"max_expansions":1000
        })).unwrap();
        fs::write(self.0.join("in.json"), &bytes).unwrap();
        assert_eq!(run(&self.args(), run_json), 0);
        assert!(!self.output()["added_tracks"].as_array().unwrap().is_empty());
        bytes
    }
    fn output(&self) -> Value {
        serde_json::from_slice(&fs::read(self.0.join("out.json")).unwrap()).unwrap()
    }
}
impl Drop for Files {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn copper(e: &Engine) -> Vec<u8> {
    assert!(e.copper().index_consistent());
    serde_json::to_vec(&e.copper().tracks().collect::<Vec<_>>()).unwrap()
}
fn staged_panic(e: &mut Engine) -> Output {
    let _: Result<(), kicad_route_engine::TransactionError<Failure>> = e.transaction(|tx| {
        tx.route(1, 1000)?;
        assert!(tx.copper().generated().count() > 0);
        // A typed payload demonstrates that the library preserves more than a message.
        std::panic::panic_any(0x1484_u64);
    });
    unreachable!()
}
#[test]
fn unwinding_executor_replaces_old_complete_and_rolls_back_library_state() {
    let files = Files::new();
    let bytes = files.seed();
    let input: Input = serde_json::from_slice(&bytes).unwrap();
    let mut e = Engine::new(input.clone()).unwrap();
    let before = copper(&e);
    assert_eq!(run(&files.args(), |_| staged_panic(&mut e)), 7);
    assert_eq!(copper(&e), before);
    let result = files.output();
    assert_eq!(result["version"], 2);
    assert_eq!(result["status"], "internal_error");
    assert_eq!(result["source_id"], "a".repeat(64));
    assert_eq!(result["added_tracks"], json!([]));
    assert_eq!(result["removed_track_ids"], json!([]));
    assert_eq!(result["expanded_states"], 0);
    assert_eq!(result["work_used"], 0);
    assert!(result["reason"]
        .as_str()
        .unwrap()
        .contains("counters unavailable"));
    assert!(e.work_used() > 0); // sentinel is not a measurement of actual work
    e.route(1, 1000).unwrap();
    let mut control = Engine::new(input).unwrap();
    control.route(1, 1000).unwrap();
    assert_eq!(copper(&e), copper(&control)); // includes next allocated ID
}
#[test]
fn library_preserves_original_panic_payload_and_allocator() {
    let files = Files::new();
    let bytes = files.seed();
    let mut e = Engine::new(serde_json::from_slice(&bytes).unwrap()).unwrap();
    let before = copper(&e);
    let payload = catch_unwind(AssertUnwindSafe(|| staged_panic(&mut e))).unwrap_err();
    assert_eq!(*payload.downcast::<u64>().unwrap(), 0x1484);
    assert_eq!(copper(&e), before);
    e.route(1, 1000).unwrap();
    assert_eq!(
        serde_json::to_value(e.copper().generated().collect::<Vec<_>>()).unwrap(),
        files.output()["added_tracks"]
    );
}
#[test]
fn publication_failure_keeps_old_bytes_and_foreign_temporary_but_returns_nonzero() {
    let files = Files::new();
    files.seed();
    let old = fs::read(files.0.join("out.json")).unwrap();
    let temporary = files
        .0
        .join(format!(".out.json.{}.tmp", std::process::id()));
    fs::write(&temporary, b"another publication owns this file").unwrap();
    assert_eq!(run(&files.args(), |_| panic!("executor failed")), 2);
    assert_eq!(fs::read(files.0.join("out.json")).unwrap(), old);
    assert_eq!(
        fs::read(&temporary).unwrap(),
        b"another publication owns this file"
    );
}
#[test]
fn missing_input_replaces_old_complete_with_read_failure() {
    let files = Files::new();
    files.seed();
    fs::remove_file(files.0.join("in.json")).unwrap();
    assert_eq!(run(&files.args(), |_| panic!("must not execute")), 2);
    let result = files.output();
    assert_eq!(result["version"], 2);
    assert_eq!(result["status"], "invalid");
    assert_eq!(result["added_tracks"], json!([]));
    assert!(result["reason"]
        .as_str()
        .unwrap()
        .contains("cannot read input"));
}
#[test]
fn same_input_output_is_refused_before_executor_or_write() {
    let files = Files::new();
    let bytes = files.seed();
    let mut args = files.args();
    args[2] = args[1].clone();
    assert_eq!(run(&args, |_| panic!("must not execute")), 2);
    assert_eq!(fs::read(&args[1]).unwrap(), bytes);
    #[cfg(unix)]
    {
        let alias = files.0.join("alias.json");
        std::os::unix::fs::symlink(&args[1], &alias).unwrap();
        args[2] = alias.into_os_string();
        assert_eq!(run(&args, |_| panic!("must not execute")), 2);
        assert_eq!(fs::read(&args[1]).unwrap(), bytes);
    }
}
#[test]
fn panic_without_valid_source_still_publishes_edit_free_failure() {
    let files = Files::new();
    files.seed();
    for bytes in [b"{broken".as_slice(), br#"{"source_id":"invalid"}"#] {
        fs::write(files.0.join("in.json"), bytes).unwrap();
        assert_eq!(run(&files.args(), |_| panic!("injected")), 7);
        let result = files.output();
        assert_eq!(result["source_id"], "");
        assert_eq!(result["status"], "internal_error");
        assert_eq!(result["added_tracks"], json!([]));
        assert_eq!(result["removed_track_ids"], json!([]));
    }
}
