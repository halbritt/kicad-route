mod cli;

fn main() -> std::process::ExitCode {
    let args: Vec<_> = std::env::args_os().collect();
    std::process::ExitCode::from(cli::run_main(&args))
}
