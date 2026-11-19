//! Process boundary only. Routing-library panics still unwind to their caller.
use kicad_route_engine::{
    identity::MAX_REQUEST_BYTES,
    model::{Failure, Output, Status},
};
use std::{
    ffi::OsString,
    fs,
    io::{ErrorKind, Read, Write},
    panic::{catch_unwind, AssertUnwindSafe},
    path::Path,
    sync::atomic::{AtomicU64, Ordering},
    time::{SystemTime, UNIX_EPOCH},
};

static NEXT_TEMP_SEQ: AtomicU64 = AtomicU64::new(0);

// Best effort identity recovery is only for an edit-free panic result. It does
// not validate input or confer permission to apply any result.
fn recover_source_id(bytes: &[u8]) -> String {
    if bytes.len() > MAX_REQUEST_BYTES {
        return String::new();
    }
    serde_json::from_slice::<serde_json::Value>(bytes)
        .ok()
        .and_then(|v| {
            v.get("source_id")
                .and_then(|s| s.as_str())
                .filter(|s| {
                    s.len() == 64
                        && s.bytes()
                            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                })
                .map(str::to_owned)
        })
        .unwrap_or_default()
}

fn write_output(path: &Path, output: &Output) -> std::io::Result<()> {
    let name = path
        .file_name()
        .ok_or_else(|| std::io::Error::other("output path needs a filename"))?
        .to_string_lossy();
    let pid = std::process::id();
    let (temporary, mut f) = {
        let mut opened = None;
        for _ in 0..64 {
            let seq = NEXT_TEMP_SEQ.fetch_add(1, Ordering::Relaxed);
            let nanos = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_or(0, |d| d.as_nanos());
            let candidate = path.with_file_name(format!(".{name}.{pid}.{nanos}.{seq}.tmp"));
            match fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&candidate)
            {
                Ok(file) => {
                    opened = Some((candidate, file));
                    break;
                }
                Err(e) if e.kind() == ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(e),
            }
        }
        opened.ok_or_else(|| {
            std::io::Error::new(
                ErrorKind::AlreadyExists,
                "could not allocate unique temporary output file",
            )
        })?
    };
    let result = (|| {
        serde_json::to_writer_pretty(&mut f, output)?;
        f.write_all(b"\n")?;
        f.sync_all()?;
        fs::rename(&temporary, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}
pub(crate) fn run_main(args: &[OsString]) -> u8 {
    if args
        .get(1)
        .is_some_and(|arg| arg == "--help" || arg == "-h")
    {
        println!(
            "usage: kicad-route-engine <INPUT.json> <OUTPUT.json>\n       kicad-route-engine route-native <SOURCE.kicad_pcb> <OUTPUT.kicad_pcb> [--rules <RULES.json>] [--project <PROJECT.kicad_pro>] [--limits <LIMITS.json>] [--diff-pairs <auto|P,N>] [--pair-layers <LAYER...>] [--no-short-pair-deferral] [--diff-pair-intra-match] [--length-match-tolerance <MM>] [--meander-amplitude <MM>] [--length-match-group <SPEC...>] [--length-match-group-json <JSON>] [--group-match-tolerance <MM>] [--group-delay-tolerance <PS>] [--group-meander-amplitude <MM>] [--group-meander-spacing <WIDTHS>] [--pair-impedance <OHM>] [--impedance-tolerance <PCT>] [--impedance-reference <NET>] [--impedance-uncoupled <MM>] [--protect <none|existing|human|locked>] [--nets <NET...>]"
        );
        return 0;
    }
    if args.get(1).is_some_and(|arg| arg == "route-native") {
        return kicad_route_engine::orchestration::run_cli(&args[2..]);
    }
    run(args, kicad_route_engine::run_json)
}

pub(crate) fn run(args: &[OsString], executor: impl FnOnce(&[u8]) -> Output) -> u8 {
    let same_file = args.len() == 3
        && (args[1] == args[2]
            || fs::canonicalize(&args[1])
                .ok()
                .zip(fs::canonicalize(&args[2]).ok())
                .is_some_and(|(a, b)| a == b));
    if args.len() != 3 || same_file {
        eprintln!("usage: kicad-route-engine INPUT.json OUTPUT.json (distinct paths); no fresh result established");
        return 8;
    }
    let bytes: std::io::Result<Vec<u8>> = (|| {
        let mut bytes = Vec::new();
        fs::File::open(&args[1])?
            .take((MAX_REQUEST_BYTES + 1) as u64)
            .read_to_end(&mut bytes)?;
        Ok(bytes)
    })();
    let output = match bytes {
        Ok(bytes) => match catch_unwind(AssertUnwindSafe(|| executor(&bytes))) {
            Ok(output) => output,
            Err(_) => {
                let failure = Failure::new(
                    Status::InternalError,
                    "routing executor panicked; edits discarded; expansion/work counters unavailable (reported as 0)",
                );
                let mut output = Output::failure(recover_source_id(&bytes), failure);
                output.request_sha256 = kicad_route_engine::request_sha256(&bytes);
                output
            }
        },
        Err(e) => Output::failure(
            String::new(),
            Failure::new(Status::Invalid, format!("cannot read input: {e}")),
        ),
    };
    if let Err(e) = write_output(Path::new(&args[2]), &output) {
        eprintln!("cannot publish output: {e}; no fresh result established; destination may still contain an older result");
        return 8;
    }
    match output.status {
        Status::Complete => 0,
        Status::BudgetExhausted => 4,
        Status::WorkExhausted => 5,
        Status::AdmissionExceeded | Status::JournalExhausted | Status::NestingExhausted => 6,
        Status::Unroutable => 3,
        Status::InternalError => 7,
        Status::Invalid | Status::Unsupported => 2,
    }
}

#[cfg(test)]
#[path = "cli_tests.rs"]
mod tests;
