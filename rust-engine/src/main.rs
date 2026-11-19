mod cli;

fn main() -> std::process::ExitCode {
    let args: Vec<_> = std::env::args_os().collect();
    #[cfg(target_os = "linux")]
    if let Some(status) = kicad_route_engine::orchestration::initialize_standalone_lifetimes(&args)
    {
        return std::process::ExitCode::from(status);
    }
    std::process::ExitCode::from(cli::run_main(&args))
}
