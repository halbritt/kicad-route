use kicad_route_engine::{
    model::{Failure, Output, Status},
    run_json,
};
use std::{
    env, fs,
    io::{Read, Write},
    path::Path,
    process::ExitCode,
};
fn write_output(path: &Path, output: &Output) -> std::io::Result<()> {
    let name = path
        .file_name()
        .ok_or_else(|| std::io::Error::other("output path needs a filename"))?
        .to_string_lossy();
    let temporary = path.with_file_name(format!(".{name}.{}.tmp", std::process::id()));
    let mut f = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
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
fn main() -> ExitCode {
    let args: Vec<_> = env::args_os().collect();
    let same_file = args.len() == 3
        && (args[1] == args[2]
            || fs::canonicalize(&args[1])
                .ok()
                .zip(fs::canonicalize(&args[2]).ok())
                .is_some_and(|(a, b)| a == b));
    if args.len() != 3 || same_file {
        eprintln!("usage: kicad-route-engine INPUT.json OUTPUT.json (distinct paths)");
        return ExitCode::from(2);
    }
    let bytes: std::io::Result<Vec<u8>> = (|| {
        let mut bytes = Vec::new();
        fs::File::open(&args[1])?
            .take(16 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)?;
        Ok(bytes)
    })();
    let output = match bytes {
        Ok(bytes) => run_json(&bytes),
        Err(e) => Output::failure(
            String::new(),
            Failure::new(Status::Invalid, format!("cannot read input: {e}")),
        ),
    };
    if let Err(e) = write_output(Path::new(&args[2]), &output) {
        eprintln!("cannot publish output: {e}");
        return ExitCode::from(2);
    }
    match output.status {
        Status::Complete => ExitCode::SUCCESS,
        Status::BudgetExhausted => ExitCode::from(4),
        Status::WorkExhausted => ExitCode::from(5),
        Status::AdmissionExceeded | Status::JournalExhausted | Status::NestingExhausted => {
            ExitCode::from(6)
        }
        Status::Unroutable => ExitCode::from(3),
        _ => ExitCode::from(2),
    }
}
