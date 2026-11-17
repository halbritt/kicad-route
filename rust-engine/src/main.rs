mod cli;

fn main() -> std::process::ExitCode {
    let args: Vec<_> = std::env::args_os().collect();
    std::process::ExitCode::from(cli::run(&args, kicad_route_engine::run_json))
}
