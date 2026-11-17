use crate::{
    identity::MAX_REQUEST_BYTES,
    model::{Input, Limits, Status, RESULT_VERSION},
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::hash_map::RandomState,
    ffi::OsString,
    fs::{self, DirBuilder, File, OpenOptions},
    hash::{BuildHasher, Hasher},
    io::{ErrorKind, Read, Write},
    panic::{catch_unwind, AssertUnwindSafe},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicI32, AtomicU64, AtomicUsize, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[cfg(unix)]
use std::os::unix::{
    fs::{DirBuilderExt, OpenOptionsExt},
    process::{CommandExt, ExitStatusExt},
};

pub const OPERATION_SCHEMA: &str = "kicad-route/native-operation/1";
pub const OPERATION_VERSION: u32 = 1;
pub const APPLY_REPORT_SCHEMA: &str = "kicad-route/native-apply-report/1";
pub const APPLY_REPORT_VERSION: u32 = 1;
pub const MAX_CANDIDATE_BYTES: usize = 32 * 1024 * 1024;
pub const DEFAULT_MAX_EXPANSIONS: u64 = 100_000;
pub const DEFAULT_TIMEOUT_MS: u64 = 120_000;
pub const MIN_TIMEOUT_MS: u64 = 1;
pub const MAX_TIMEOUT_MS: u64 = 3_600_000;
pub const DEFAULT_MAX_CHILD_OUTPUT_BYTES: usize = 1_048_576;
pub const MIN_CHILD_OUTPUT_BYTES: usize = 1024;
pub const MAX_CHILD_OUTPUT_BYTES: usize = 16 * 1024 * 1024;
const STDERR_TAIL_BYTES: usize = 4096;
const CHILD_KILL_GRACE_MS: u64 = 200;

static OP_SEQ: AtomicU64 = AtomicU64::new(1);

#[cfg(unix)]
mod unix_sys {
    use std::ffi::c_int;
    pub const SIGINT: c_int = 2;
    pub const SIGTERM: c_int = 15;
    pub const SIGKILL: c_int = 9;
    pub const SIG_IGN: usize = 1;

    extern "C" {
        pub fn kill(pid: c_int, sig: c_int) -> c_int;
        pub fn signal(sig: c_int, handler: usize) -> usize;
    }
}

#[cfg(unix)]
static CANCEL_SIGNAL: AtomicI32 = AtomicI32::new(0);
#[cfg(unix)]
static SIGNAL_STATE: Mutex<SignalInstallState> = Mutex::new(SignalInstallState {
    ref_count: 0,
    prev_sigint: 0,
    prev_sigterm: 0,
});

#[cfg(unix)]
struct SignalInstallState {
    ref_count: usize,
    prev_sigint: usize,
    prev_sigterm: usize,
}

#[cfg(unix)]
extern "C" fn handle_cancel_signal(sig: std::ffi::c_int) {
    CANCEL_SIGNAL.store(sig, Ordering::SeqCst);
}

#[cfg(unix)]
struct SignalGuard;

#[cfg(unix)]
impl SignalGuard {
    fn acquire() -> Self {
        let mut state = SIGNAL_STATE
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if state.ref_count == 0 {
            CANCEL_SIGNAL.store(0, Ordering::SeqCst);
            let h = handle_cancel_signal as *const () as usize;
            // SAFETY: installing a signal-safe atomic store handler and restoring
            // the previous handler on drop; SIG_IGN is preserved if inherited.
            unsafe {
                let prev_int = unix_sys::signal(unix_sys::SIGINT, h);
                if prev_int == unix_sys::SIG_IGN {
                    let _ = unix_sys::signal(unix_sys::SIGINT, unix_sys::SIG_IGN);
                }
                let prev_term = unix_sys::signal(unix_sys::SIGTERM, h);
                if prev_term == unix_sys::SIG_IGN {
                    let _ = unix_sys::signal(unix_sys::SIGTERM, unix_sys::SIG_IGN);
                }
                state.prev_sigint = prev_int;
                state.prev_sigterm = prev_term;
            }
        }
        state.ref_count += 1;
        Self
    }
}

#[cfg(unix)]
impl Drop for SignalGuard {
    fn drop(&mut self) {
        let mut state = SIGNAL_STATE
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if state.ref_count > 0 {
            state.ref_count -= 1;
            if state.ref_count == 0 {
                CANCEL_SIGNAL.store(0, Ordering::SeqCst);
                // SAFETY: restoring previously saved signal dispositions.
                unsafe {
                    let _ = unix_sys::signal(unix_sys::SIGINT, state.prev_sigint);
                    let _ = unix_sys::signal(unix_sys::SIGTERM, state.prev_sigterm);
                }
            }
        }
    }
}

#[cfg(not(unix))]
struct SignalGuard;
#[cfg(not(unix))]
impl SignalGuard {
    fn acquire() -> Self {
        Self
    }
}

fn cancellation_signal() -> Option<i32> {
    #[cfg(unix)]
    {
        let sig = CANCEL_SIGNAL.load(Ordering::SeqCst);
        (sig != 0).then_some(sig)
    }
    #[cfg(not(unix))]
    {
        None
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OperationStatus {
    Complete,
    Invalid,
    Unsupported,
    NativeValidationFailed,
    Unroutable,
    BudgetExhausted,
    WorkExhausted,
    AdmissionExceeded,
    JournalExhausted,
    NestingExhausted,
    OperationTimeout,
    Cancelled,
    ChildOutputExceeded,
    InternalError,
    ChildProcessError,
    UsageRefused,
    PublicationRefused,
    DestinationExists,
    AmbiguousTermination,
}

impl OperationStatus {
    pub fn exit_code(self) -> u8 {
        match self {
            Self::Complete => 0,
            Self::Invalid | Self::Unsupported | Self::NativeValidationFailed => 2,
            Self::Unroutable => 3,
            Self::BudgetExhausted => 4,
            Self::WorkExhausted => 5,
            Self::AdmissionExceeded
            | Self::JournalExhausted
            | Self::NestingExhausted
            | Self::OperationTimeout
            | Self::Cancelled
            | Self::ChildOutputExceeded => 6,
            Self::InternalError | Self::ChildProcessError => 7,
            Self::UsageRefused
            | Self::PublicationRefused
            | Self::DestinationExists
            | Self::AmbiguousTermination => 8,
        }
    }
}

#[derive(Clone, Debug)]
pub struct OperationConfig {
    pub source: PathBuf,
    pub output: PathBuf,
    pub rules: PathBuf,
    pub limits: Option<PathBuf>,
    pub max_expansions: u64,
    pub operation_result: Option<PathBuf>,
    pub save_request: Option<PathBuf>,
    pub native_bridge: Option<PathBuf>,
    pub python: PathBuf,
    pub timeout_ms: u64,
    pub max_child_output_bytes: usize,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct BinaryIdentity {
    pub name: String,
    pub crate_version: String,
    pub binary_sha256: Option<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct SidecarCheck {
    pub checked_original_path: bool,
    pub checked_canonical_path: bool,
    pub kicad_pro_present: bool,
    pub kicad_dru_present: bool,
    pub passed: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SourceRecord {
    pub original_path: String,
    pub canonical_path: Option<String>,
    pub source_sha256: Option<String>,
    pub source_bytes: Option<u64>,
    pub sidecar_check: SidecarCheck,
    pub snapshot_sha256: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RequestRecord {
    pub rules_path: String,
    pub rules_sha256: Option<String>,
    pub limits_path: Option<String>,
    pub limits_sha256: Option<String>,
    pub max_expansions: u64,
    pub request_sha256: Option<String>,
    pub request_bytes: Option<u64>,
    pub saved_request_path: Option<String>,
    pub request_json: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct BudgetsRecord {
    pub work_limits: Option<Limits>,
    pub max_expansions: u64,
    pub operation_timeout_ms: u64,
    pub child_output_limit_bytes: usize,
    pub wall_elapsed_ms: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ChildProcessRecord {
    pub program: String,
    pub argv: Vec<String>,
    pub pid: Option<u32>,
    pub exit_code: Option<i32>,
    pub signal: Option<i32>,
    pub timed_out: bool,
    pub cancelled: bool,
    pub stdout_bytes: u64,
    pub stderr_bytes: u64,
    pub stdout_truncated: bool,
    pub stderr_truncated: bool,
    pub stderr_tail: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PhaseRecord {
    pub phase: String,
    pub status: String,
    pub elapsed_ms: u64,
    pub detail: Option<String>,
    pub child: Option<ChildProcessRecord>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RoutingSummary {
    pub result_version: u32,
    pub status: String,
    pub expanded_states: u64,
    pub work_used: u64,
    pub added_track_count: usize,
    pub removed_track_count: usize,
    pub request_sha256: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct StagingRecord {
    pub dir: Option<String>,
    pub cleaned_up: bool,
    pub cleanup_error: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PublicationRecord {
    pub destination_path: String,
    pub commit_point_reached: bool,
    pub published: bool,
    pub output_sha256: Option<String>,
    pub output_bytes: Option<u64>,
    pub ambiguous_termination: bool,
    pub reconciliation_note: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct OperationResult {
    pub schema: String,
    pub version: u32,
    pub operation_id: String,
    pub binary: BinaryIdentity,
    pub status: OperationStatus,
    pub exit_code: u8,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    pub source: SourceRecord,
    pub request: RequestRecord,
    pub budgets: BudgetsRecord,
    pub phases: Vec<PhaseRecord>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub routing: Option<RoutingSummary>,
    pub staging: StagingRecord,
    pub publication: PublicationRecord,
}

#[derive(Debug, Deserialize)]
struct ApplyValidationReport {
    schema: String,
    version: u32,
    source_id: String,
    request_sha256: String,
    candidate_sha256: String,
    candidate_bytes: u64,
    after_unconnected_count: u64,
    introduced_violation_count: u64,
    added_track_count: usize,
}

fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

fn generate_operation_id() -> String {
    let seq = OP_SEQ.fetch_add(1, Ordering::Relaxed);
    let pid = std::process::id();
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let mut entropy = [0u8; 16];
    let got_urandom = File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut entropy))
        .is_ok();
    if !got_urandom {
        let s1 = RandomState::new().build_hasher().finish().to_le_bytes();
        let s2 = RandomState::new().build_hasher().finish().to_le_bytes();
        entropy[..8].copy_from_slice(&s1);
        entropy[8..].copy_from_slice(&s2);
    }
    let mut hasher = Sha256::new();
    hasher.update(b"kicad-route-op-id");
    hasher.update(entropy);
    hasher.update(pid.to_le_bytes());
    hasher.update(seq.to_le_bytes());
    hasher.update(nanos.to_le_bytes());
    let digest: String = hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    format!("{}-{seq}", &digest[..32])
}

fn compute_binary_identity() -> BinaryIdentity {
    let binary_sha256 = std::env::current_exe().ok().and_then(|exe| {
        let mut file = File::open(exe).ok()?;
        let mut hasher = Sha256::new();
        let mut buf = [0u8; 65536];
        loop {
            let n = file.read(&mut buf).ok()?;
            if n == 0 {
                break;
            }
            hasher.update(&buf[..n]);
        }
        Some(
            hasher
                .finalize()
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect(),
        )
    });
    BinaryIdentity {
        name: "kicad-route-engine".to_owned(),
        crate_version: env!("CARGO_PKG_VERSION").to_owned(),
        binary_sha256,
    }
}

struct OwnedTempDir {
    path: Option<PathBuf>,
}

impl OwnedTempDir {
    fn create_in(parent: &Path, prefix: &str, op_id: &str) -> std::io::Result<Self> {
        for attempt in 0..64u32 {
            let name = if attempt == 0 {
                format!("{prefix}{op_id}")
            } else {
                format!("{prefix}{op_id}-{attempt}")
            };
            let candidate = parent.join(name);
            let mut builder = DirBuilder::new();
            #[cfg(unix)]
            builder.mode(0o700);
            match builder.create(&candidate) {
                Ok(()) => {
                    return Ok(Self {
                        path: Some(candidate),
                    })
                }
                Err(e) if e.kind() == ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(e),
            }
        }
        Err(std::io::Error::new(
            ErrorKind::AlreadyExists,
            "exhausted unique staging directory attempts",
        ))
    }

    fn path(&self) -> Option<&Path> {
        self.path.as_deref()
    }

    fn cleanup(&mut self) -> Result<(), String> {
        if let Some(dir) = self.path.take() {
            match fs::remove_dir_all(&dir) {
                Ok(()) => Ok(()),
                Err(e) if e.kind() == ErrorKind::NotFound => Ok(()),
                Err(e) => Err(format!("failed to clean up {}: {e}", dir.display())),
            }
        } else {
            Ok(())
        }
    }
}

impl Drop for OwnedTempDir {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}

fn write_file_create_new(path: &Path, bytes: &[u8], mode: u32) -> std::io::Result<()> {
    let mut opts = OpenOptions::new();
    opts.write(true).create_new(true);
    #[cfg(unix)]
    opts.mode(mode);
    #[cfg(not(unix))]
    let _ = mode;
    let mut file = opts.open(path)?;
    let write_res = (|| {
        file.write_all(bytes)?;
        file.sync_all()
    })();
    if write_res.is_err() {
        let _ = fs::remove_file(path);
    }
    write_res
}

fn parent_dir_or_dot(path: &Path) -> &Path {
    match path.parent() {
        Some(p) if !p.as_os_str().is_empty() => p,
        _ => Path::new("."),
    }
}

fn atomic_hard_link_publish(
    destination: &Path,
    bytes: &[u8],
    op_id: &str,
    mode: u32,
) -> std::io::Result<()> {
    let parent = parent_dir_or_dot(destination);
    let mut pub_dir = OwnedTempDir::create_in(parent, ".kicad-route-pub-", op_id)?;
    let name = destination
        .file_name()
        .ok_or_else(|| std::io::Error::other("destination needs a file name"))?;
    let candidate = pub_dir
        .path()
        .ok_or_else(|| std::io::Error::other("missing publication staging dir"))?
        .join(name);
    write_file_create_new(&candidate, bytes, mode)?;
    fs::hard_link(&candidate, destination)?;
    if let Ok(dir) = File::open(parent) {
        let _ = dir.sync_all();
    }
    let _ = pub_dir.cleanup();
    Ok(())
}

fn normalized_parent_filename(path: &Path) -> Option<(PathBuf, OsString)> {
    let name = path.file_name()?.to_os_string();
    let parent = parent_dir_or_dot(path);
    let canon_parent = fs::canonicalize(parent).ok()?;
    Some((canon_parent, name))
}

fn paths_may_alias(a: &Path, b: &Path) -> bool {
    if a == b {
        return true;
    }
    if let (Ok(ca), Ok(cb)) = (fs::canonicalize(a), fs::canonicalize(b)) {
        if ca == cb {
            return true;
        }
    }
    if let (Some(ka), Some(kb)) = (normalized_parent_filename(a), normalized_parent_filename(b)) {
        if ka == kb {
            return true;
        }
    }
    false
}

fn resolve_native_bridge(explicit: Option<&Path>) -> Result<PathBuf, String> {
    if let Some(p) = explicit {
        let meta = fs::metadata(p)
            .map_err(|e| format!("cannot inspect --native-bridge {}: {e}", p.display()))?;
        if !meta.is_file() {
            return Err(format!(
                "--native-bridge is not a regular file: {}",
                p.display()
            ));
        }
        return Ok(p.to_path_buf());
    }
    if let Ok(exe) = std::env::current_exe() {
        for anc in exe.ancestors().skip(1) {
            let candidate = anc.join("tools/rust_engine_kicad.py");
            if fs::metadata(&candidate).is_ok_and(|m| m.is_file()) {
                return Ok(candidate);
            }
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        for anc in cwd.ancestors() {
            let candidate = anc.join("tools/rust_engine_kicad.py");
            if fs::metadata(&candidate).is_ok_and(|m| m.is_file()) {
                return Ok(candidate);
            }
        }
    }
    let manifest_fallback =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../tools/rust_engine_kicad.py");
    if fs::metadata(&manifest_fallback).is_ok_and(|m| m.is_file()) {
        return Ok(manifest_fallback);
    }
    Err("cannot locate tools/rust_engine_kicad.py; pass --native-bridge".to_owned())
}

fn check_sidecar_at(
    path: &Path,
    check: &mut SidecarCheck,
) -> Result<(), (OperationStatus, String)> {
    for (ext, flag) in [
        ("kicad_pro", &mut check.kicad_pro_present),
        ("kicad_dru", &mut check.kicad_dru_present),
    ] {
        let sidecar = path.with_extension(ext);
        match fs::symlink_metadata(&sidecar) {
            Ok(_) => {
                *flag = true;
            }
            Err(e) if e.kind() == ErrorKind::NotFound => {}
            Err(e) => {
                return Err((
                    OperationStatus::Invalid,
                    format!("cannot inspect sidecar {}: {e}", sidecar.display()),
                ));
            }
        }
    }
    Ok(())
}

fn check_source_sidecars(
    original: &Path,
    canonical: Option<&Path>,
) -> (SidecarCheck, Result<(), (OperationStatus, String)>) {
    let mut check = SidecarCheck {
        checked_original_path: true,
        checked_canonical_path: false,
        kicad_pro_present: false,
        kicad_dru_present: false,
        passed: false,
    };
    if let Err(err) = check_sidecar_at(original, &mut check) {
        return (check, Err(err));
    }
    if let Some(canon) = canonical {
        check.checked_canonical_path = true;
        if canon != original {
            if let Err(err) = check_sidecar_at(canon, &mut check) {
                return (check, Err(err));
            }
        }
    }
    if check.kicad_pro_present || check.kicad_dru_present {
        let which = match (check.kicad_pro_present, check.kicad_dru_present) {
            (true, true) => ".kicad_pro and .kicad_dru",
            (true, false) => ".kicad_pro",
            (false, true) => ".kicad_dru",
            _ => unreachable!(),
        };
        return (
            check,
            Err((
                OperationStatus::Unsupported,
                format!("project/custom-rule import is not implemented: {which}"),
            )),
        );
    }
    check.passed = true;
    (check, Ok(()))
}

enum BoundedReadError {
    NotRegular(String),
    Io(String),
    TooLarge(String),
}

impl BoundedReadError {
    fn into_status(self) -> (OperationStatus, String) {
        match self {
            Self::NotRegular(m) | Self::Io(m) => (OperationStatus::Invalid, m),
            Self::TooLarge(m) => (OperationStatus::AdmissionExceeded, m),
        }
    }
}

fn read_bounded_regular(
    path: &Path,
    max_bytes: usize,
    label: &str,
) -> Result<Vec<u8>, BoundedReadError> {
    let pre_meta = fs::metadata(path).map_err(|e| {
        BoundedReadError::Io(format!("cannot stat {label} {}: {e}", path.display()))
    })?;
    if !pre_meta.is_file() {
        return Err(BoundedReadError::NotRegular(format!(
            "{label} is not a regular file: {}",
            path.display()
        )));
    }
    let file = File::open(path).map_err(|e| {
        BoundedReadError::Io(format!("cannot open {label} {}: {e}", path.display()))
    })?;
    let open_meta = file.metadata().map_err(|e| {
        BoundedReadError::Io(format!("cannot inspect {label} {}: {e}", path.display()))
    })?;
    if !open_meta.is_file() {
        return Err(BoundedReadError::NotRegular(format!(
            "{label} is not a regular file: {}",
            path.display()
        )));
    }
    let mut bytes = Vec::new();
    file.take((max_bytes as u64).saturating_add(1))
        .read_to_end(&mut bytes)
        .map_err(|e| {
            BoundedReadError::Io(format!("cannot read {label} {}: {e}", path.display()))
        })?;
    if bytes.len() > max_bytes {
        return Err(BoundedReadError::TooLarge(format!(
            "{label} exceeds {max_bytes} bytes: {}",
            path.display()
        )));
    }
    Ok(bytes)
}

fn verify_regular_non_symlink(path: &Path, label: &str) -> Result<(), String> {
    let meta = fs::symlink_metadata(path)
        .map_err(|e| format!("missing {label} {}: {e}", path.display()))?;
    if !meta.file_type().is_file() {
        return Err(format!(
            "{label} is not a regular non-symlink file: {}",
            path.display()
        ));
    }
    Ok(())
}

struct PipeCapture {
    bytes: Vec<u8>,
    total_bytes: u64,
    truncated: bool,
}

fn spawn_pipe_reader<R: Read + Send + 'static>(
    mut reader: R,
    limit: usize,
    overflow: Arc<AtomicBool>,
    done: Arc<AtomicUsize>,
) -> thread::JoinHandle<PipeCapture> {
    thread::spawn(move || {
        let mut capture = PipeCapture {
            bytes: Vec::new(),
            total_bytes: 0,
            truncated: false,
        };
        let mut buf = [0u8; 8192];
        loop {
            match reader.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    capture.total_bytes = capture.total_bytes.saturating_add(n as u64);
                    if capture.total_bytes > limit as u64 {
                        let remaining = limit.saturating_sub(capture.bytes.len());
                        if remaining > 0 {
                            capture.bytes.extend_from_slice(&buf[..remaining]);
                        }
                        capture.truncated = true;
                        overflow.store(true, Ordering::Release);
                        break;
                    }
                    capture.bytes.extend_from_slice(&buf[..n]);
                }
                Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                Err(_) => break,
            }
        }
        done.fetch_add(1, Ordering::Release);
        capture
    })
}

struct ChildGroupGuard {
    child: Option<Child>,
    pgid: i32,
}

impl ChildGroupGuard {
    fn kill_and_reap(&mut self) {
        if let Some(mut child) = self.child.take() {
            if self.pgid > 0 {
                #[cfg(unix)]
                unsafe {
                    let _ = unix_sys::kill(-self.pgid, unix_sys::SIGKILL);
                }
            }
            let _ = child.wait();
        }
    }
}

impl Drop for ChildGroupGuard {
    fn drop(&mut self) {
        self.kill_and_reap();
    }
}

fn send_pgid_signal(pgid: i32, sig: i32) {
    if pgid > 0 {
        #[cfg(unix)]
        unsafe {
            let _ = unix_sys::kill(-pgid, sig);
        }
        #[cfg(not(unix))]
        let _ = sig;
    }
}

fn format_stderr_tail(bytes: &[u8]) -> Option<String> {
    if bytes.is_empty() {
        return None;
    }
    let slice = if bytes.len() > STDERR_TAIL_BYTES {
        &bytes[bytes.len() - STDERR_TAIL_BYTES..]
    } else {
        bytes
    };
    Some(String::from_utf8_lossy(slice).into_owned())
}

struct ChildRunOutcome {
    record: ChildProcessRecord,
    spawn_error: Option<String>,
    output_exceeded: bool,
}

fn run_bounded_child(
    program: &Path,
    args: &[OsString],
    deadline: Instant,
    max_output_bytes: usize,
) -> ChildRunOutcome {
    let argv: Vec<String> = args
        .iter()
        .map(|a| a.to_string_lossy().into_owned())
        .collect();
    let mut cmd = Command::new(program);
    cmd.args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(unix)]
    cmd.process_group(0);

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            return ChildRunOutcome {
                record: ChildProcessRecord {
                    program: program.display().to_string(),
                    argv,
                    pid: None,
                    exit_code: None,
                    signal: None,
                    timed_out: false,
                    cancelled: false,
                    stdout_bytes: 0,
                    stderr_bytes: 0,
                    stdout_truncated: false,
                    stderr_truncated: false,
                    stderr_tail: None,
                },
                spawn_error: Some(format!("failed to spawn {}: {e}", program.display())),
                output_exceeded: false,
            };
        }
    };

    let pid = child.id();
    let pgid = i32::try_from(pid).unwrap_or(0);
    let stdout = child.stdout.take().expect("piped stdout");
    let stderr = child.stderr.take().expect("piped stderr");

    let overflow = Arc::new(AtomicBool::new(false));
    let readers_done = Arc::new(AtomicUsize::new(0));
    let out_handle = spawn_pipe_reader(
        stdout,
        max_output_bytes,
        Arc::clone(&overflow),
        Arc::clone(&readers_done),
    );
    let err_handle = spawn_pipe_reader(
        stderr,
        max_output_bytes,
        Arc::clone(&overflow),
        Arc::clone(&readers_done),
    );

    let mut guard = ChildGroupGuard {
        child: Some(child),
        pgid,
    };

    let mut timed_out = false;
    let mut cancelled = false;
    let mut output_exceeded = false;
    let mut exit_status = None;

    loop {
        if let Some(ch) = guard.child.as_mut() {
            match ch.try_wait() {
                Ok(Some(status)) => {
                    exit_status = Some(status);
                    guard.child = None;
                    break;
                }
                Ok(None) => {}
                Err(_) => break,
            }
        }
        if overflow.load(Ordering::Acquire) {
            output_exceeded = true;
            break;
        }
        if cancellation_signal().is_some() {
            cancelled = true;
            break;
        }
        if Instant::now() >= deadline {
            timed_out = true;
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }

    if output_exceeded || cancelled || timed_out {
        #[cfg(unix)]
        send_pgid_signal(pgid, unix_sys::SIGTERM);
        let grace_end = Instant::now() + Duration::from_millis(CHILD_KILL_GRACE_MS);
        let mut reaped = false;
        while Instant::now() < grace_end {
            if let Some(ch) = guard.child.as_mut() {
                if let Ok(Some(status)) = ch.try_wait() {
                    exit_status = Some(status);
                    guard.child = None;
                    reaped = true;
                    break;
                }
            }
            thread::sleep(Duration::from_millis(10));
        }
        if !reaped {
            #[cfg(unix)]
            send_pgid_signal(pgid, unix_sys::SIGKILL);
            if let Some(mut ch) = guard.child.take() {
                exit_status = ch.wait().ok();
            }
        } else if readers_done.load(Ordering::Acquire) < 2 {
            // A grandchild may have survived and kept pipes open after leader exit.
            #[cfg(unix)]
            send_pgid_signal(pgid, unix_sys::SIGKILL);
        }
    } else {
        let pipe_wait = Instant::now() + Duration::from_millis(50);
        while readers_done.load(Ordering::Acquire) < 2 && Instant::now() < pipe_wait {
            thread::sleep(Duration::from_millis(2));
        }
        if readers_done.load(Ordering::Acquire) < 2 {
            #[cfg(unix)]
            send_pgid_signal(pgid, unix_sys::SIGKILL);
        }
    }

    let out_cap = out_handle.join().unwrap_or(PipeCapture {
        bytes: Vec::new(),
        total_bytes: 0,
        truncated: false,
    });
    let err_cap = err_handle.join().unwrap_or(PipeCapture {
        bytes: Vec::new(),
        total_bytes: 0,
        truncated: false,
    });

    let exit_code = exit_status.and_then(|s| s.code());
    #[cfg(unix)]
    let signal = exit_status.and_then(|s| s.signal());
    #[cfg(not(unix))]
    let signal = None;

    let truncated_any = out_cap.truncated || err_cap.truncated;
    if truncated_any {
        output_exceeded = true;
    }

    ChildRunOutcome {
        record: ChildProcessRecord {
            program: program.display().to_string(),
            argv,
            pid: Some(pid),
            exit_code,
            signal,
            timed_out,
            cancelled,
            stdout_bytes: out_cap.total_bytes,
            stderr_bytes: err_cap.total_bytes,
            stdout_truncated: out_cap.truncated,
            stderr_truncated: err_cap.truncated,
            stderr_tail: format_stderr_tail(&err_cap.bytes),
        },
        spawn_error: None,
        output_exceeded,
    }
}

#[derive(Deserialize)]
struct PythonStderrError {
    status: String,
    reason: String,
}

fn parse_child_failure(outcome: &ChildRunOutcome, phase_name: &str) -> (OperationStatus, String) {
    if let Some(err) = &outcome.spawn_error {
        return (OperationStatus::ChildProcessError, err.clone());
    }
    if outcome.record.cancelled {
        return (
            OperationStatus::Cancelled,
            format!("{phase_name} cancelled by signal"),
        );
    }
    if outcome.record.timed_out {
        return (
            OperationStatus::OperationTimeout,
            format!("{phase_name} exceeded operation wall-clock timeout"),
        );
    }
    if outcome.output_exceeded {
        return (
            OperationStatus::ChildOutputExceeded,
            format!("{phase_name} child output exceeded byte cap"),
        );
    }
    if let Some(sig) = outcome.record.signal {
        return (
            OperationStatus::ChildProcessError,
            format!("{phase_name} child terminated by signal {sig}"),
        );
    }
    if let Some(tail) = &outcome.record.stderr_tail {
        for line in tail.lines().rev() {
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            if let Ok(parsed) = serde_json::from_str::<PythonStderrError>(trimmed) {
                let status = match parsed.status.as_str() {
                    "unsupported" => OperationStatus::Unsupported,
                    "invalid" => OperationStatus::Invalid,
                    "native_validation_failed" => OperationStatus::NativeValidationFailed,
                    "output_exists" => OperationStatus::DestinationExists,
                    _ => OperationStatus::ChildProcessError,
                };
                return (status, parsed.reason);
            }
            break;
        }
    }
    (
        OperationStatus::ChildProcessError,
        format!(
            "{phase_name} exited with code {:?}{}",
            outcome.record.exit_code,
            outcome
                .record
                .stderr_tail
                .as_deref()
                .map(|t| format!(": {}", t.trim()))
                .unwrap_or_default()
        ),
    )
}

fn map_routing_status(status: Status) -> OperationStatus {
    match status {
        Status::Complete => OperationStatus::Complete,
        Status::Unsupported => OperationStatus::Unsupported,
        Status::Invalid => OperationStatus::Invalid,
        Status::Unroutable => OperationStatus::Unroutable,
        Status::BudgetExhausted => OperationStatus::BudgetExhausted,
        Status::WorkExhausted => OperationStatus::WorkExhausted,
        Status::AdmissionExceeded => OperationStatus::AdmissionExceeded,
        Status::JournalExhausted => OperationStatus::JournalExhausted,
        Status::NestingExhausted => OperationStatus::NestingExhausted,
        Status::InternalError => OperationStatus::InternalError,
    }
}

#[derive(Default)]
struct CommitHooks<'a> {
    before_commit: Option<Box<dyn FnOnce() + 'a>>,
    after_commit: Option<Box<dyn FnOnce() + 'a>>,
}

fn finish_result(
    result: &mut OperationResult,
    status: OperationStatus,
    reason: Option<String>,
    start: Instant,
) {
    result.status = status;
    result.exit_code = status.exit_code();
    result.reason = reason;
    result.budgets.wall_elapsed_ms = start.elapsed().as_millis() as u64;
}

pub fn run_native(config: &OperationConfig) -> OperationResult {
    run_native_with_hooks(config, None)
}

fn run_native_with_hooks(
    config: &OperationConfig,
    hooks: Option<CommitHooks<'_>>,
) -> OperationResult {
    let start = Instant::now();
    let op_id = generate_operation_id();
    let _signal_guard = SignalGuard::acquire();

    let mut result = OperationResult {
        schema: OPERATION_SCHEMA.to_owned(),
        version: OPERATION_VERSION,
        operation_id: op_id.clone(),
        binary: compute_binary_identity(),
        status: OperationStatus::InternalError,
        exit_code: OperationStatus::InternalError.exit_code(),
        reason: None,
        source: SourceRecord {
            original_path: config.source.display().to_string(),
            canonical_path: None,
            source_sha256: None,
            source_bytes: None,
            sidecar_check: SidecarCheck::default(),
            snapshot_sha256: None,
        },
        request: RequestRecord {
            rules_path: config.rules.display().to_string(),
            rules_sha256: None,
            limits_path: config.limits.as_ref().map(|p| p.display().to_string()),
            limits_sha256: None,
            max_expansions: config.max_expansions,
            request_sha256: None,
            request_bytes: None,
            saved_request_path: None,
            request_json: None,
        },
        budgets: BudgetsRecord {
            work_limits: None,
            max_expansions: config.max_expansions,
            operation_timeout_ms: config.timeout_ms,
            child_output_limit_bytes: config.max_child_output_bytes,
            wall_elapsed_ms: 0,
        },
        phases: Vec::new(),
        routing: None,
        staging: StagingRecord {
            dir: None,
            cleaned_up: true,
            cleanup_error: None,
        },
        publication: PublicationRecord {
            destination_path: config.output.display().to_string(),
            commit_point_reached: false,
            published: false,
            output_sha256: None,
            output_bytes: None,
            ambiguous_termination: false,
            reconciliation_note: None,
        },
    };

    let outcome = catch_unwind(AssertUnwindSafe(|| {
        execute_phases(config, &op_id, start, &mut result, hooks)
    }));

    match outcome {
        Ok(Ok(())) => {
            finish_result(&mut result, OperationStatus::Complete, None, start);
        }
        Ok(Err((status, reason))) => {
            finish_result(&mut result, status, Some(reason), start);
        }
        Err(_) => {
            if let Some(dir) = result.staging.dir.as_deref() {
                let p = Path::new(dir);
                if p.exists() {
                    if let Err(e) = fs::remove_dir_all(p) {
                        result.staging.cleaned_up = false;
                        result.staging.cleanup_error = Some(e.to_string());
                    } else {
                        result.staging.cleaned_up = true;
                    }
                } else {
                    result.staging.cleaned_up = true;
                }
            }
            if result.publication.commit_point_reached {
                result.publication.ambiguous_termination = true;
                result.publication.reconciliation_note = Some(
                    "panic occurred at or after publication commit point; destination preserved and must be reconciled"
                        .to_owned(),
                );
                finish_result(
                    &mut result,
                    OperationStatus::AmbiguousTermination,
                    Some("internal panic at or after publication commit point".to_owned()),
                    start,
                );
            } else {
                finish_result(
                    &mut result,
                    OperationStatus::InternalError,
                    Some(
                        "internal panic before publication commit point; staging cleaned"
                            .to_owned(),
                    ),
                    start,
                );
            }
        }
    }

    result
}

struct AdmittedInputs {
    bridge_path: PathBuf,
    source_bytes: Vec<u8>,
    source_sha256: String,
    rules_bytes: Vec<u8>,
    limits_bytes: Option<Vec<u8>>,
}

fn check_output_preflight(path: &Path, label: &str) -> Result<(), (OperationStatus, String)> {
    if path.as_os_str().is_empty() || path.file_name().is_none() {
        return Err((
            OperationStatus::UsageRefused,
            format!("{label} path must include a file name: {}", path.display()),
        ));
    }
    let parent = parent_dir_or_dot(path);
    match fs::metadata(parent) {
        Ok(m) if m.is_dir() => {}
        Ok(_) => {
            return Err((
                OperationStatus::PublicationRefused,
                format!("{label} parent is not a directory: {}", parent.display()),
            ));
        }
        Err(e) => {
            return Err((
                OperationStatus::PublicationRefused,
                format!(
                    "{label} parent directory unavailable ({}): {e}",
                    parent.display()
                ),
            ));
        }
    }
    match fs::symlink_metadata(path) {
        Ok(_) => Err((
            OperationStatus::DestinationExists,
            format!("{label} already exists: {}", path.display()),
        )),
        Err(e) if e.kind() == ErrorKind::NotFound => Ok(()),
        Err(e) => Err((
            OperationStatus::PublicationRefused,
            format!("cannot inspect {label} {}: {e}", path.display()),
        )),
    }
}

fn phase_pre_snapshot_admission(
    config: &OperationConfig,
    result: &mut OperationResult,
) -> Result<AdmittedInputs, (OperationStatus, String)> {
    let t0 = Instant::now();
    let res = (|| -> Result<AdmittedInputs, (OperationStatus, String)> {
        if config.max_expansions == 0 || config.max_expansions > crate::model::MAX_EXPANSIONS {
            return Err((
                OperationStatus::UsageRefused,
                format!(
                    "max_expansions must be in 1..={}",
                    crate::model::MAX_EXPANSIONS
                ),
            ));
        }
        if config.timeout_ms < MIN_TIMEOUT_MS || config.timeout_ms > MAX_TIMEOUT_MS {
            return Err((
                OperationStatus::UsageRefused,
                format!("timeout-ms must be in {MIN_TIMEOUT_MS}..={MAX_TIMEOUT_MS}"),
            ));
        }
        if config.max_child_output_bytes < MIN_CHILD_OUTPUT_BYTES
            || config.max_child_output_bytes > MAX_CHILD_OUTPUT_BYTES
        {
            return Err((
                OperationStatus::UsageRefused,
                format!(
                    "max-child-output-bytes must be in {MIN_CHILD_OUTPUT_BYTES}..={MAX_CHILD_OUTPUT_BYTES}"
                ),
            ));
        }
        let bridge_path = resolve_native_bridge(config.native_bridge.as_deref())
            .map_err(|m| (OperationStatus::Invalid, m))?;

        let mut inputs: Vec<(&Path, &str)> = vec![
            (&config.source, "source"),
            (&config.rules, "rules"),
            (&bridge_path, "native-bridge"),
            (&config.python, "python"),
        ];
        if let Some(lim) = &config.limits {
            inputs.push((lim, "limits"));
        }
        let mut outputs: Vec<(&Path, &str)> = vec![(&config.output, "output")];
        if let Some(op_res) = &config.operation_result {
            outputs.push((op_res, "operation-result"));
        }
        if let Some(save_req) = &config.save_request {
            outputs.push((save_req, "save-request"));
        }
        for (out_p, out_lbl) in &outputs {
            for (in_p, in_lbl) in &inputs {
                if paths_may_alias(out_p, in_p) {
                    return Err((
                        OperationStatus::UsageRefused,
                        format!("{out_lbl} must be distinct from {in_lbl}"),
                    ));
                }
            }
        }
        for i in 0..outputs.len() {
            for j in (i + 1)..outputs.len() {
                if paths_may_alias(outputs[i].0, outputs[j].0) {
                    return Err((
                        OperationStatus::UsageRefused,
                        format!("{} must be distinct from {}", outputs[i].1, outputs[j].1),
                    ));
                }
            }
        }
        for (out_p, out_lbl) in &outputs {
            check_output_preflight(out_p, out_lbl)?;
        }

        let canon_source = fs::canonicalize(&config.source).map_err(|e| {
            (
                OperationStatus::Invalid,
                format!("cannot resolve source {}: {e}", config.source.display()),
            )
        })?;
        result.source.canonical_path = Some(canon_source.display().to_string());

        let (sidecar_check, sidecar_res) =
            check_source_sidecars(&config.source, Some(&canon_source));
        result.source.sidecar_check = sidecar_check;
        sidecar_res?;

        let source_bytes = read_bounded_regular(&config.source, MAX_REQUEST_BYTES, "source board")
            .map_err(BoundedReadError::into_status)?;
        let (recheck, recheck_res) = check_source_sidecars(&config.source, Some(&canon_source));
        result.source.sidecar_check = recheck;
        recheck_res?;

        let source_sha256 = sha256_hex(&source_bytes);
        result.source.source_sha256 = Some(source_sha256.clone());
        result.source.source_bytes = Some(source_bytes.len() as u64);

        let rules_bytes = read_bounded_regular(&config.rules, MAX_REQUEST_BYTES, "rules")
            .map_err(BoundedReadError::into_status)?;
        result.request.rules_sha256 = Some(sha256_hex(&rules_bytes));

        let limits_bytes = match &config.limits {
            Some(lp) => {
                let lb = read_bounded_regular(lp, MAX_REQUEST_BYTES, "limits")
                    .map_err(BoundedReadError::into_status)?;
                result.request.limits_sha256 = Some(sha256_hex(&lb));
                result.budgets.work_limits = serde_json::from_slice::<Limits>(&lb).ok();
                Some(lb)
            }
            None => None,
        };

        Ok(AdmittedInputs {
            bridge_path,
            source_bytes,
            source_sha256,
            rules_bytes,
            limits_bytes,
        })
    })();

    result.phases.push(PhaseRecord {
        phase: "pre_snapshot_admission".to_owned(),
        status: if res.is_ok() { "ok" } else { "failed" }.to_owned(),
        elapsed_ms: t0.elapsed().as_millis() as u64,
        detail: res.as_ref().err().map(|(_, m)| m.clone()),
        child: None,
    });
    res
}

fn check_cancel_or_deadline(deadline: Instant) -> Result<(), (OperationStatus, String)> {
    if let Some(sig) = cancellation_signal() {
        return Err((
            OperationStatus::Cancelled,
            format!("cancelled by signal {sig}"),
        ));
    }
    if Instant::now() >= deadline {
        return Err((
            OperationStatus::OperationTimeout,
            "operation wall-clock timeout exceeded".to_owned(),
        ));
    }
    Ok(())
}

fn execute_phases(
    config: &OperationConfig,
    op_id: &str,
    start: Instant,
    result: &mut OperationResult,
    mut hooks: Option<CommitHooks<'_>>,
) -> Result<(), (OperationStatus, String)> {
    let deadline = start + Duration::from_millis(config.timeout_ms);
    let admitted = phase_pre_snapshot_admission(config, result)?;

    // Phase 2: snapshot_stage
    let t_stage = Instant::now();
    let mut staging_guard =
        OwnedTempDir::create_in(&std::env::temp_dir(), "kicad-route-op-", op_id).map_err(|e| {
            result.phases.push(PhaseRecord {
                phase: "snapshot_stage".to_owned(),
                status: "failed".to_owned(),
                elapsed_ms: t_stage.elapsed().as_millis() as u64,
                detail: Some(format!("cannot create staging directory: {e}")),
                child: None,
            });
            (
                OperationStatus::InternalError,
                format!("cannot create staging directory: {e}"),
            )
        })?;
    let staging_dir = staging_guard
        .path()
        .expect("freshly created staging dir")
        .to_path_buf();
    result.staging.dir = Some(staging_dir.display().to_string());
    result.staging.cleaned_up = false;

    let staged_source = staging_dir.join("source.kicad_pcb");
    let staged_rules = staging_dir.join("rules.json");
    let staged_limits = admitted
        .limits_bytes
        .as_ref()
        .map(|_| staging_dir.join("limits.json"));
    let stage_write_res = (|| -> std::io::Result<()> {
        write_file_create_new(&staged_source, &admitted.source_bytes, 0o600)?;
        write_file_create_new(&staged_rules, &admitted.rules_bytes, 0o600)?;
        if let (Some(lp), Some(lb)) = (&staged_limits, &admitted.limits_bytes) {
            write_file_create_new(lp, lb, 0o600)?;
        }
        Ok(())
    })();
    if let Err(e) = stage_write_res {
        let msg = format!("cannot write staging snapshot: {e}");
        result.phases.push(PhaseRecord {
            phase: "snapshot_stage".to_owned(),
            status: "failed".to_owned(),
            elapsed_ms: t_stage.elapsed().as_millis() as u64,
            detail: Some(msg.clone()),
            child: None,
        });
        cleanup_staging(&mut staging_guard, result);
        return Err((OperationStatus::InternalError, msg));
    }
    result.source.snapshot_sha256 = Some(admitted.source_sha256.clone());
    result.phases.push(PhaseRecord {
        phase: "snapshot_stage".to_owned(),
        status: "ok".to_owned(),
        elapsed_ms: t_stage.elapsed().as_millis() as u64,
        detail: None,
        child: None,
    });

    let inner = execute_staged_phases(
        config,
        op_id,
        deadline,
        &admitted,
        &staging_dir,
        &staged_source,
        &staged_rules,
        staged_limits.as_deref(),
        result,
        &mut hooks,
    );
    cleanup_staging(&mut staging_guard, result);
    inner
}

fn cleanup_staging(staging_guard: &mut OwnedTempDir, result: &mut OperationResult) {
    let t0 = Instant::now();
    match staging_guard.cleanup() {
        Ok(()) => {
            result.staging.cleaned_up = true;
            result.staging.cleanup_error = None;
            result.phases.push(PhaseRecord {
                phase: "cleanup".to_owned(),
                status: "ok".to_owned(),
                elapsed_ms: t0.elapsed().as_millis() as u64,
                detail: None,
                child: None,
            });
        }
        Err(e) => {
            result.staging.cleaned_up = false;
            result.staging.cleanup_error = Some(e.clone());
            result.phases.push(PhaseRecord {
                phase: "cleanup".to_owned(),
                status: "failed".to_owned(),
                elapsed_ms: t0.elapsed().as_millis() as u64,
                detail: Some(e),
                child: None,
            });
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn execute_staged_phases(
    config: &OperationConfig,
    op_id: &str,
    deadline: Instant,
    admitted: &AdmittedInputs,
    staging_dir: &Path,
    staged_source: &Path,
    staged_rules: &Path,
    staged_limits: Option<&Path>,
    result: &mut OperationResult,
    hooks: &mut Option<CommitHooks<'_>>,
) -> Result<(), (OperationStatus, String)> {
    // Phase 3: native_export
    check_cancel_or_deadline(deadline)?;
    let staged_request = staging_dir.join("request.json");
    let mut export_args: Vec<OsString> = vec![
        "-B".into(),
        admitted.bridge_path.as_os_str().to_os_string(),
        "export".into(),
        staged_source.as_os_str().to_os_string(),
        staged_request.as_os_str().to_os_string(),
        "--rules".into(),
        staged_rules.as_os_str().to_os_string(),
        "--max-expansions".into(),
        config.max_expansions.to_string().into(),
        "--original-source".into(),
        config.source.as_os_str().to_os_string(),
    ];
    if let Some(lp) = staged_limits {
        export_args.push("--limits".into());
        export_args.push(lp.as_os_str().to_os_string());
    }
    let t_exp = Instant::now();
    let exp_outcome = run_bounded_child(
        &config.python,
        &export_args,
        deadline,
        config.max_child_output_bytes,
    );
    let exp_ok = exp_outcome.spawn_error.is_none()
        && !exp_outcome.record.timed_out
        && !exp_outcome.record.cancelled
        && !exp_outcome.output_exceeded
        && exp_outcome.record.signal.is_none()
        && exp_outcome.record.exit_code == Some(0);
    if !exp_ok {
        let (st, msg) = parse_child_failure(&exp_outcome, "native_export");
        result.phases.push(PhaseRecord {
            phase: "native_export".to_owned(),
            status: "failed".to_owned(),
            elapsed_ms: t_exp.elapsed().as_millis() as u64,
            detail: Some(msg.clone()),
            child: Some(exp_outcome.record),
        });
        return Err((st, msg));
    }
    if let Err(msg) = verify_regular_non_symlink(&staged_request, "exported request.json") {
        let m = format!("native_export did not produce a valid request artifact: {msg}");
        result.phases.push(PhaseRecord {
            phase: "native_export".to_owned(),
            status: "failed".to_owned(),
            elapsed_ms: t_exp.elapsed().as_millis() as u64,
            detail: Some(m.clone()),
            child: Some(exp_outcome.record),
        });
        return Err((OperationStatus::ChildProcessError, m));
    }
    let request_bytes =
        match read_bounded_regular(&staged_request, MAX_REQUEST_BYTES, "exported request.json") {
            Ok(b) => b,
            Err(err) => {
                let (st, msg) = err.into_status();
                result.phases.push(PhaseRecord {
                    phase: "native_export".to_owned(),
                    status: "failed".to_owned(),
                    elapsed_ms: t_exp.elapsed().as_millis() as u64,
                    detail: Some(msg.clone()),
                    child: Some(exp_outcome.record),
                });
                return Err((st, msg));
            }
        };
    let req_sha = sha256_hex(&request_bytes);
    result.request.request_sha256 = Some(req_sha.clone());
    result.request.request_bytes = Some(request_bytes.len() as u64);
    result.request.request_json = String::from_utf8(request_bytes.clone()).ok();

    if let Ok(parsed_input) = serde_json::from_slice::<Input>(&request_bytes) {
        if parsed_input.source_id != admitted.source_sha256 {
            let msg = "exported request source_id does not match staged source snapshot".to_owned();
            result.phases.push(PhaseRecord {
                phase: "native_export".to_owned(),
                status: "failed".to_owned(),
                elapsed_ms: t_exp.elapsed().as_millis() as u64,
                detail: Some(msg.clone()),
                child: Some(exp_outcome.record),
            });
            return Err((OperationStatus::ChildProcessError, msg));
        }
    }
    result.phases.push(PhaseRecord {
        phase: "native_export".to_owned(),
        status: "ok".to_owned(),
        elapsed_ms: t_exp.elapsed().as_millis() as u64,
        detail: None,
        child: Some(exp_outcome.record),
    });

    // Phase 4: rust_route
    check_cancel_or_deadline(deadline)?;
    let t_route = Instant::now();
    let route_output = match catch_unwind(AssertUnwindSafe(|| crate::run_json(&request_bytes))) {
        Ok(out) => out,
        Err(_) => {
            let msg = "routing executor panicked; edits discarded".to_owned();
            result.phases.push(PhaseRecord {
                phase: "rust_route".to_owned(),
                status: "failed".to_owned(),
                elapsed_ms: t_route.elapsed().as_millis() as u64,
                detail: Some(msg.clone()),
                child: None,
            });
            return Err((OperationStatus::InternalError, msg));
        }
    };
    result.routing = Some(RoutingSummary {
        result_version: route_output.version,
        status: serde_json::to_value(route_output.status)
            .ok()
            .and_then(|v| v.as_str().map(str::to_owned))
            .unwrap_or_else(|| "unknown".to_owned()),
        expanded_states: route_output.expanded_states,
        work_used: route_output.work_used,
        added_track_count: route_output.added_tracks.len(),
        removed_track_count: route_output.removed_track_ids.len(),
        request_sha256: route_output.request_sha256.clone(),
    });
    if route_output.version != RESULT_VERSION
        || route_output.request_sha256.as_deref() != Some(req_sha.as_str())
    {
        let msg = "routing output version or request_sha256 mismatch".to_owned();
        result.phases.push(PhaseRecord {
            phase: "rust_route".to_owned(),
            status: "failed".to_owned(),
            elapsed_ms: t_route.elapsed().as_millis() as u64,
            detail: Some(msg.clone()),
            child: None,
        });
        return Err((OperationStatus::InternalError, msg));
    }
    if route_output.status != Status::Complete {
        let st = map_routing_status(route_output.status);
        let msg = route_output
            .reason
            .clone()
            .unwrap_or_else(|| "routing failed".to_owned());
        result.phases.push(PhaseRecord {
            phase: "rust_route".to_owned(),
            status: "failed".to_owned(),
            elapsed_ms: t_route.elapsed().as_millis() as u64,
            detail: Some(msg.clone()),
            child: None,
        });
        return Err((st, msg));
    }
    if route_output.source_id != admitted.source_sha256 {
        let msg = "routing output source_id does not match staged snapshot".to_owned();
        result.phases.push(PhaseRecord {
            phase: "rust_route".to_owned(),
            status: "failed".to_owned(),
            elapsed_ms: t_route.elapsed().as_millis() as u64,
            detail: Some(msg.clone()),
            child: None,
        });
        return Err((OperationStatus::InternalError, msg));
    }
    if let Err(err) = check_cancel_or_deadline(deadline) {
        result.phases.push(PhaseRecord {
            phase: "rust_route".to_owned(),
            status: "failed".to_owned(),
            elapsed_ms: t_route.elapsed().as_millis() as u64,
            detail: Some(err.1.clone()),
            child: None,
        });
        return Err(err);
    }

    let staged_plan = staging_dir.join("plan.json");
    let plan_bytes = serde_json::to_vec(&route_output).map_err(|e| {
        (
            OperationStatus::InternalError,
            format!("cannot serialize routing output: {e}"),
        )
    })?;
    write_file_create_new(&staged_plan, &plan_bytes, 0o600).map_err(|e| {
        (
            OperationStatus::InternalError,
            format!("cannot write staging plan.json: {e}"),
        )
    })?;
    result.phases.push(PhaseRecord {
        phase: "rust_route".to_owned(),
        status: "ok".to_owned(),
        elapsed_ms: t_route.elapsed().as_millis() as u64,
        detail: None,
        child: None,
    });

    // Phase 5: native_apply_validate
    check_cancel_or_deadline(deadline)?;
    let remaining_ms = deadline
        .saturating_duration_since(Instant::now())
        .as_millis();
    if remaining_ms == 0 {
        return Err((
            OperationStatus::OperationTimeout,
            "operation wall-clock timeout exceeded before apply".to_owned(),
        ));
    }
    let drc_timeout_secs = ((remaining_ms as f64) / 1000.0).max(0.05);
    let staged_candidate = staging_dir.join("candidate.kicad_pcb");
    let staged_report = staging_dir.join("apply-report.json");
    let apply_args: Vec<OsString> = vec![
        "-B".into(),
        admitted.bridge_path.as_os_str().to_os_string(),
        "apply".into(),
        staged_source.as_os_str().to_os_string(),
        staged_plan.as_os_str().to_os_string(),
        staged_candidate.as_os_str().to_os_string(),
        "--request".into(),
        staged_request.as_os_str().to_os_string(),
        "--original-source".into(),
        config.source.as_os_str().to_os_string(),
        "--drc-timeout".into(),
        format!("{drc_timeout_secs:.3}").into(),
        "--validation-report".into(),
        staged_report.as_os_str().to_os_string(),
    ];
    let t_apply = Instant::now();
    let apply_outcome = run_bounded_child(
        &config.python,
        &apply_args,
        deadline,
        config.max_child_output_bytes,
    );
    let apply_ok = apply_outcome.spawn_error.is_none()
        && !apply_outcome.record.timed_out
        && !apply_outcome.record.cancelled
        && !apply_outcome.output_exceeded
        && apply_outcome.record.signal.is_none()
        && apply_outcome.record.exit_code == Some(0);
    if !apply_ok {
        let (st, msg) = parse_child_failure(&apply_outcome, "native_apply_validate");
        result.phases.push(PhaseRecord {
            phase: "native_apply_validate".to_owned(),
            status: "failed".to_owned(),
            elapsed_ms: t_apply.elapsed().as_millis() as u64,
            detail: Some(msg.clone()),
            child: Some(apply_outcome.record),
        });
        return Err((st, msg));
    }
    if let Err(msg) = verify_regular_non_symlink(&staged_candidate, "candidate board") {
        let m = format!("native_apply_validate did not produce candidate board: {msg}");
        result.phases.push(PhaseRecord {
            phase: "native_apply_validate".to_owned(),
            status: "failed".to_owned(),
            elapsed_ms: t_apply.elapsed().as_millis() as u64,
            detail: Some(m.clone()),
            child: Some(apply_outcome.record),
        });
        return Err((OperationStatus::ChildProcessError, m));
    }
    let candidate_bytes =
        match read_bounded_regular(&staged_candidate, MAX_CANDIDATE_BYTES, "candidate board") {
            Ok(b) => b,
            Err(err) => {
                let (st, msg) = err.into_status();
                result.phases.push(PhaseRecord {
                    phase: "native_apply_validate".to_owned(),
                    status: "failed".to_owned(),
                    elapsed_ms: t_apply.elapsed().as_millis() as u64,
                    detail: Some(msg.clone()),
                    child: Some(apply_outcome.record),
                });
                return Err((st, msg));
            }
        };
    let candidate_sha256 = sha256_hex(&candidate_bytes);
    let candidate_bytes_len = candidate_bytes.len() as u64;

    if let Err(msg) = verify_regular_non_symlink(&staged_report, "apply-report.json") {
        let m = format!("native_apply_validate did not produce apply-report.json: {msg}");
        result.phases.push(PhaseRecord {
            phase: "native_apply_validate".to_owned(),
            status: "failed".to_owned(),
            elapsed_ms: t_apply.elapsed().as_millis() as u64,
            detail: Some(m.clone()),
            child: Some(apply_outcome.record),
        });
        return Err((OperationStatus::ChildProcessError, m));
    }
    let report_bytes =
        match read_bounded_regular(&staged_report, MAX_REQUEST_BYTES, "apply-report.json") {
            Ok(b) => b,
            Err(err) => {
                let (st, msg) = err.into_status();
                result.phases.push(PhaseRecord {
                    phase: "native_apply_validate".to_owned(),
                    status: "failed".to_owned(),
                    elapsed_ms: t_apply.elapsed().as_millis() as u64,
                    detail: Some(msg.clone()),
                    child: Some(apply_outcome.record),
                });
                return Err((st, msg));
            }
        };
    let report: ApplyValidationReport = match serde_json::from_slice(&report_bytes) {
        Ok(r) => r,
        Err(e) => {
            let msg = format!("malformed apply-report.json: {e}");
            result.phases.push(PhaseRecord {
                phase: "native_apply_validate".to_owned(),
                status: "failed".to_owned(),
                elapsed_ms: t_apply.elapsed().as_millis() as u64,
                detail: Some(msg.clone()),
                child: Some(apply_outcome.record),
            });
            return Err((OperationStatus::ChildProcessError, msg));
        }
    };
    if report.schema != APPLY_REPORT_SCHEMA
        || report.version != APPLY_REPORT_VERSION
        || report.source_id != admitted.source_sha256
        || report.request_sha256 != req_sha
        || report.candidate_sha256 != candidate_sha256
        || report.candidate_bytes != candidate_bytes_len
        || report.after_unconnected_count != 0
        || report.introduced_violation_count != 0
        || report.added_track_count != route_output.added_tracks.len()
    {
        let msg = "apply-report.json failed consistency or native validation checks".to_owned();
        result.phases.push(PhaseRecord {
            phase: "native_apply_validate".to_owned(),
            status: "failed".to_owned(),
            elapsed_ms: t_apply.elapsed().as_millis() as u64,
            detail: Some(msg.clone()),
            child: Some(apply_outcome.record),
        });
        return Err((OperationStatus::NativeValidationFailed, msg));
    }
    result.phases.push(PhaseRecord {
        phase: "native_apply_validate".to_owned(),
        status: "ok".to_owned(),
        elapsed_ms: t_apply.elapsed().as_millis() as u64,
        detail: None,
        child: Some(apply_outcome.record),
    });

    // Phase 6: final_publish
    if let Err(err) = check_cancel_or_deadline(deadline) {
        result.phases.push(PhaseRecord {
            phase: "final_publish".to_owned(),
            status: "failed".to_owned(),
            elapsed_ms: 0,
            detail: Some(err.1.clone()),
            child: None,
        });
        return Err(err);
    }
    let t_pub = Instant::now();
    let dest_parent = parent_dir_or_dot(&config.output);
    let dest_name = config.output.file_name().ok_or_else(|| {
        (
            OperationStatus::UsageRefused,
            "output needs a filename".to_owned(),
        )
    })?;
    let mut pub_dir =
        OwnedTempDir::create_in(dest_parent, ".kicad-route-pub-", op_id).map_err(|e| {
            let msg = format!("cannot create publication staging dir: {e}");
            result.phases.push(PhaseRecord {
                phase: "final_publish".to_owned(),
                status: "failed".to_owned(),
                elapsed_ms: t_pub.elapsed().as_millis() as u64,
                detail: Some(msg.clone()),
                child: None,
            });
            (OperationStatus::PublicationRefused, msg)
        })?;
    let pub_candidate = pub_dir
        .path()
        .expect("fresh publication dir")
        .join(dest_name);
    write_file_create_new(&pub_candidate, &candidate_bytes, 0o644).map_err(|e| {
        let msg = format!("cannot stage publication candidate: {e}");
        result.phases.push(PhaseRecord {
            phase: "final_publish".to_owned(),
            status: "failed".to_owned(),
            elapsed_ms: t_pub.elapsed().as_millis() as u64,
            detail: Some(msg.clone()),
            child: None,
        });
        (OperationStatus::PublicationRefused, msg)
    })?;

    if let Some(h) = hooks.as_mut() {
        if let Some(before) = h.before_commit.take() {
            before();
        }
    }
    if let Err(err) = check_cancel_or_deadline(deadline) {
        result.phases.push(PhaseRecord {
            phase: "final_publish".to_owned(),
            status: "failed".to_owned(),
            elapsed_ms: t_pub.elapsed().as_millis() as u64,
            detail: Some(err.1.clone()),
            child: None,
        });
        return Err(err);
    }

    result.publication.commit_point_reached = true;
    match fs::hard_link(&pub_candidate, &config.output) {
        Ok(()) => {
            result.publication.published = true;
            result.publication.output_sha256 = Some(candidate_sha256);
            result.publication.output_bytes = Some(candidate_bytes_len);
            if let Ok(dir) = File::open(dest_parent) {
                let _ = dir.sync_all();
            }
            let _ = pub_dir.cleanup();
        }
        Err(e) if e.kind() == ErrorKind::AlreadyExists => {
            result.publication.published = false;
            let note = "destination appeared concurrently before hard_link; incumbent preserved"
                .to_owned();
            result.publication.reconciliation_note = Some(note.clone());
            result.phases.push(PhaseRecord {
                phase: "final_publish".to_owned(),
                status: "failed".to_owned(),
                elapsed_ms: t_pub.elapsed().as_millis() as u64,
                detail: Some(note.clone()),
                child: None,
            });
            return Err((OperationStatus::DestinationExists, note));
        }
        Err(e) => {
            result.publication.published = false;
            let msg = format!(
                "cannot link published board {}: {e}",
                config.output.display()
            );
            result.phases.push(PhaseRecord {
                phase: "final_publish".to_owned(),
                status: "failed".to_owned(),
                elapsed_ms: t_pub.elapsed().as_millis() as u64,
                detail: Some(msg.clone()),
                child: None,
            });
            return Err((OperationStatus::PublicationRefused, msg));
        }
    }

    if let Some(h) = hooks.as_mut() {
        if let Some(after) = h.after_commit.take() {
            after();
        }
    }
    if let Some(sig) = cancellation_signal() {
        result.publication.ambiguous_termination = true;
        let note = format!(
            "signal {sig} received after destination publication; destination preserved and not unlinked"
        );
        result.publication.reconciliation_note = Some(note.clone());
        result.phases.push(PhaseRecord {
            phase: "final_publish".to_owned(),
            status: "failed".to_owned(),
            elapsed_ms: t_pub.elapsed().as_millis() as u64,
            detail: Some(note.clone()),
            child: None,
        });
        return Err((OperationStatus::AmbiguousTermination, note));
    }

    if let Some(save_req) = &config.save_request {
        if let Err(e) = atomic_hard_link_publish(save_req, &request_bytes, op_id, 0o644) {
            result.publication.ambiguous_termination = true;
            let note = format!(
                "destination board was published, but saving --save-request {} failed: {e}",
                save_req.display()
            );
            result.publication.reconciliation_note = Some(note.clone());
            result.phases.push(PhaseRecord {
                phase: "final_publish".to_owned(),
                status: "failed".to_owned(),
                elapsed_ms: t_pub.elapsed().as_millis() as u64,
                detail: Some(note.clone()),
                child: None,
            });
            return Err((OperationStatus::AmbiguousTermination, note));
        }
        result.request.saved_request_path = Some(save_req.display().to_string());
    }

    result.phases.push(PhaseRecord {
        phase: "final_publish".to_owned(),
        status: "ok".to_owned(),
        elapsed_ms: t_pub.elapsed().as_millis() as u64,
        detail: None,
        child: None,
    });
    Ok(())
}

const USAGE_MSG: &str = "usage: kicad-route-engine route-native <SOURCE.kicad_pcb> <OUTPUT.kicad_pcb> --rules <RULES.json> [--limits <LIMITS.json>] [--max-expansions <N>] [--operation-result <OP_RESULT.json>] [--save-request <REQUEST.json>] [--native-bridge <RUST_ENGINE_KICAD.py>] [--python <PYTHON_BIN>] [--timeout-ms <MS>] [--max-child-output-bytes <BYTES>]";

fn take_flag_value(
    args: &[OsString],
    idx: &mut usize,
    inline: Option<&str>,
    flag: &str,
) -> Result<OsString, String> {
    if let Some(val) = inline {
        if val.is_empty() {
            return Err(format!("{flag} requires a non-empty value"));
        }
        return Ok(OsString::from(val));
    }
    *idx += 1;
    let val = args
        .get(*idx)
        .ok_or_else(|| format!("{flag} requires a value"))?;
    if val.is_empty() {
        return Err(format!("{flag} requires a non-empty value"));
    }
    Ok(val.clone())
}

fn parse_u64_flag(val: &OsString, flag: &str, min: u64, max: u64) -> Result<u64, String> {
    let s = val
        .to_str()
        .ok_or_else(|| format!("{flag} must be valid UTF-8 integer"))?;
    let parsed: u64 = s
        .parse()
        .map_err(|_| format!("{flag} must be an integer in {min}..={max}"))?;
    if !(min..=max).contains(&parsed) {
        return Err(format!("{flag} must be in {min}..={max}"));
    }
    Ok(parsed)
}

pub fn parse_cli_args(args: &[OsString]) -> Result<OperationConfig, String> {
    let mut positional: Vec<PathBuf> = Vec::new();
    let mut rules: Option<PathBuf> = None;
    let mut limits: Option<PathBuf> = None;
    let mut max_expansions: Option<u64> = None;
    let mut operation_result: Option<PathBuf> = None;
    let mut save_request: Option<PathBuf> = None;
    let mut native_bridge: Option<PathBuf> = None;
    let mut python: Option<PathBuf> = None;
    let mut timeout_ms: Option<u64> = None;
    let mut max_child_output_bytes: Option<usize> = None;
    let mut after_double_dash = false;

    let mut i = 0;
    while i < args.len() {
        let arg = &args[i];
        if !after_double_dash && arg == "--" {
            after_double_dash = true;
            i += 1;
            continue;
        }
        if !after_double_dash {
            if let Some(s) = arg.to_str() {
                if s.starts_with('-') {
                    let (flag, inline) = match s.split_once('=') {
                        Some((f, v)) => (f, Some(v)),
                        None => (s, None),
                    };
                    match flag {
                        "--rules" => {
                            if rules.is_some() {
                                return Err("duplicate --rules flag".to_owned());
                            }
                            rules =
                                Some(PathBuf::from(take_flag_value(args, &mut i, inline, flag)?));
                        }
                        "--limits" => {
                            if limits.is_some() {
                                return Err("duplicate --limits flag".to_owned());
                            }
                            limits =
                                Some(PathBuf::from(take_flag_value(args, &mut i, inline, flag)?));
                        }
                        "--max-expansions" => {
                            if max_expansions.is_some() {
                                return Err("duplicate --max-expansions flag".to_owned());
                            }
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            max_expansions =
                                Some(parse_u64_flag(&val, flag, 1, crate::model::MAX_EXPANSIONS)?);
                        }
                        "--operation-result" => {
                            if operation_result.is_some() {
                                return Err("duplicate --operation-result flag".to_owned());
                            }
                            operation_result =
                                Some(PathBuf::from(take_flag_value(args, &mut i, inline, flag)?));
                        }
                        "--save-request" => {
                            if save_request.is_some() {
                                return Err("duplicate --save-request flag".to_owned());
                            }
                            save_request =
                                Some(PathBuf::from(take_flag_value(args, &mut i, inline, flag)?));
                        }
                        "--native-bridge" => {
                            if native_bridge.is_some() {
                                return Err("duplicate --native-bridge flag".to_owned());
                            }
                            native_bridge =
                                Some(PathBuf::from(take_flag_value(args, &mut i, inline, flag)?));
                        }
                        "--python" => {
                            if python.is_some() {
                                return Err("duplicate --python flag".to_owned());
                            }
                            python =
                                Some(PathBuf::from(take_flag_value(args, &mut i, inline, flag)?));
                        }
                        "--timeout-ms" => {
                            if timeout_ms.is_some() {
                                return Err("duplicate --timeout-ms flag".to_owned());
                            }
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            timeout_ms =
                                Some(parse_u64_flag(&val, flag, MIN_TIMEOUT_MS, MAX_TIMEOUT_MS)?);
                        }
                        "--max-child-output-bytes" => {
                            if max_child_output_bytes.is_some() {
                                return Err("duplicate --max-child-output-bytes flag".to_owned());
                            }
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            let parsed = parse_u64_flag(
                                &val,
                                flag,
                                MIN_CHILD_OUTPUT_BYTES as u64,
                                MAX_CHILD_OUTPUT_BYTES as u64,
                            )?;
                            max_child_output_bytes = Some(parsed as usize);
                        }
                        _ => return Err(format!("unknown flag: {flag}")),
                    }
                    i += 1;
                    continue;
                }
            }
        }
        if arg.is_empty() {
            return Err("positional path must be non-empty".to_owned());
        }
        positional.push(PathBuf::from(arg));
        i += 1;
    }

    if positional.len() != 2 {
        return Err(format!(
            "expected <SOURCE.kicad_pcb> <OUTPUT.kicad_pcb>, got {} positional arguments",
            positional.len()
        ));
    }
    let rules = rules.ok_or_else(|| "missing required --rules <RULES.json>".to_owned())?;
    let mut pos_iter = positional.into_iter();
    let source = pos_iter.next().unwrap();
    let output = pos_iter.next().unwrap();
    Ok(OperationConfig {
        source,
        output,
        rules,
        limits,
        max_expansions: max_expansions.unwrap_or(DEFAULT_MAX_EXPANSIONS),
        operation_result,
        save_request,
        native_bridge,
        python: python.unwrap_or_else(|| PathBuf::from("/usr/bin/python3")),
        timeout_ms: timeout_ms.unwrap_or(DEFAULT_TIMEOUT_MS),
        max_child_output_bytes: max_child_output_bytes.unwrap_or(DEFAULT_MAX_CHILD_OUTPUT_BYTES),
    })
}

fn can_write_operation_result(config: &OperationConfig, op_result_path: &Path) -> bool {
    if check_output_preflight(op_result_path, "operation-result").is_err() {
        return false;
    }
    let mut others: Vec<&Path> = vec![
        &config.source,
        &config.rules,
        &config.output,
        &config.python,
    ];
    if let Some(l) = &config.limits {
        others.push(l);
    }
    if let Some(b) = &config.native_bridge {
        others.push(b);
    }
    if let Some(sr) = &config.save_request {
        others.push(sr);
    }
    !others.iter().any(|p| paths_may_alias(op_result_path, p))
}

fn usage_error_result(reason: String) -> OperationResult {
    let op_id = generate_operation_id();
    OperationResult {
        schema: OPERATION_SCHEMA.to_owned(),
        version: OPERATION_VERSION,
        operation_id: op_id,
        binary: compute_binary_identity(),
        status: OperationStatus::UsageRefused,
        exit_code: OperationStatus::UsageRefused.exit_code(),
        reason: Some(reason),
        source: SourceRecord {
            original_path: String::new(),
            canonical_path: None,
            source_sha256: None,
            source_bytes: None,
            sidecar_check: SidecarCheck::default(),
            snapshot_sha256: None,
        },
        request: RequestRecord {
            rules_path: String::new(),
            rules_sha256: None,
            limits_path: None,
            limits_sha256: None,
            max_expansions: DEFAULT_MAX_EXPANSIONS,
            request_sha256: None,
            request_bytes: None,
            saved_request_path: None,
            request_json: None,
        },
        budgets: BudgetsRecord {
            work_limits: None,
            max_expansions: DEFAULT_MAX_EXPANSIONS,
            operation_timeout_ms: DEFAULT_TIMEOUT_MS,
            child_output_limit_bytes: DEFAULT_MAX_CHILD_OUTPUT_BYTES,
            wall_elapsed_ms: 0,
        },
        phases: Vec::new(),
        routing: None,
        staging: StagingRecord {
            dir: None,
            cleaned_up: true,
            cleanup_error: None,
        },
        publication: PublicationRecord {
            destination_path: String::new(),
            commit_point_reached: false,
            published: false,
            output_sha256: None,
            output_bytes: None,
            ambiguous_termination: false,
            reconciliation_note: None,
        },
    }
}

pub fn run_cli(args: &[OsString]) -> u8 {
    let config = match parse_cli_args(args) {
        Ok(c) => c,
        Err(msg) => {
            let result = usage_error_result(msg.clone());
            if let Ok(json_text) = serde_json::to_string_pretty(&result) {
                println!("{json_text}");
            }
            eprintln!("{msg}\n{USAGE_MSG}");
            return result.exit_code;
        }
    };

    let mut result = run_native(&config);
    if let Some(op_result_path) = &config.operation_result {
        if can_write_operation_result(&config, op_result_path) {
            match serde_json::to_vec_pretty(&result) {
                Ok(mut bytes) => {
                    bytes.push(b'\n');
                    if let Err(e) = atomic_hard_link_publish(
                        op_result_path,
                        &bytes,
                        &result.operation_id,
                        0o644,
                    ) {
                        if result.publication.published {
                            result.publication.ambiguous_termination = true;
                            result.status = OperationStatus::AmbiguousTermination;
                            result.exit_code = 8;
                            let note = format!(
                                "destination board was published, but writing --operation-result failed: {e}"
                            );
                            result.publication.reconciliation_note = Some(note.clone());
                            result.reason = Some(note);
                        } else {
                            result.status = OperationStatus::PublicationRefused;
                            result.exit_code = 8;
                            result.reason = Some(format!(
                                "cannot write --operation-result {}: {e}",
                                op_result_path.display()
                            ));
                        }
                    }
                }
                Err(e) => {
                    if result.publication.published {
                        result.publication.ambiguous_termination = true;
                        result.status = OperationStatus::AmbiguousTermination;
                        result.exit_code = 8;
                        let note = format!(
                            "destination board was published, but serializing --operation-result failed: {e}"
                        );
                        result.publication.reconciliation_note = Some(note.clone());
                        result.reason = Some(note);
                    } else {
                        result.status = OperationStatus::InternalError;
                        result.exit_code = 7;
                        result.reason = Some(format!("cannot serialize operation result: {e}"));
                    }
                }
            }
        }
    }

    match serde_json::to_string_pretty(&result) {
        Ok(json_text) => println!("{json_text}"),
        Err(e) => {
            eprintln!("cannot serialize operation result: {e}");
            return if result.publication.commit_point_reached {
                8
            } else {
                7
            };
        }
    }
    if result.exit_code != 0 {
        if let Some(reason) = &result.reason {
            eprintln!("route-native failed (exit {}): {reason}", result.exit_code);
        }
    }
    result.exit_code
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    fn make_stub_bridge(dir: &Path) -> PathBuf {
        let bridge = dir.join("stub_bridge.py");
        let script = r#"#!/usr/bin/env python3
import hashlib, json, os, pathlib, sys
cmd = sys.argv[1]
if cmd == "export":
    board, out = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
    source_id = hashlib.sha256(board.read_bytes()).hexdigest()
    req = {
        "version": 1,
        "source_id": source_id,
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
    }
    out.write_text(json.dumps(req, separators=(",", ":")))
elif cmd == "apply":
    board, plan_p, out = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]), pathlib.Path(sys.argv[4])
    req_idx = sys.argv.index("--request") + 1
    rep_idx = sys.argv.index("--validation-report") + 1
    req_bytes = pathlib.Path(sys.argv[req_idx]).read_bytes()
    plan = json.loads(plan_p.read_text())
    cand = board.read_bytes() + b"\n; routed"
    out.write_bytes(cand)
    report = {
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
    }
    pathlib.Path(sys.argv[rep_idx]).write_text(json.dumps(report))
"#;
        fs::write(&bridge, script).unwrap();
        bridge
    }

    fn test_dir(tag: &str) -> PathBuf {
        static SEQ: AtomicUsize = AtomicUsize::new(0);
        let id = SEQ.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!(
            "kicad-orch-unit-{}-{}-{id}",
            std::process::id(),
            tag
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn base_config(dir: &Path, bridge: &Path) -> OperationConfig {
        let source = dir.join("board.kicad_pcb");
        let rules = dir.join("rules.json");
        let output = dir.join("out.kicad_pcb");
        fs::write(&source, b"(kicad_pcb)").unwrap();
        fs::write(&rules, b"{}").unwrap();
        OperationConfig {
            source,
            output,
            rules,
            limits: None,
            max_expansions: 1000,
            operation_result: None,
            save_request: None,
            native_bridge: Some(bridge.to_path_buf()),
            python: PathBuf::from("/usr/bin/python3"),
            timeout_ms: 15_000,
            max_child_output_bytes: DEFAULT_MAX_CHILD_OUTPUT_BYTES,
        }
    }

    static UNIT_SIGNAL_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn operation_ids_are_unique_nonces() {
        let a = generate_operation_id();
        let b = generate_operation_id();
        assert_ne!(a, b);
        assert!(a.len() > 32);
    }

    #[test]
    fn pre_commit_panic_cleans_staging_and_leaves_no_destination() {
        let _guard = UNIT_SIGNAL_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = test_dir("pre-panic");
        let bridge = make_stub_bridge(&dir);
        let cfg = base_config(&dir, &bridge);
        let hooks = CommitHooks {
            before_commit: Some(Box::new(|| panic!("injected pre-commit panic"))),
            after_commit: None,
        };
        let res = run_native_with_hooks(&cfg, Some(hooks));
        assert_eq!(res.status, OperationStatus::InternalError);
        assert_eq!(res.exit_code, 7);
        assert!(!res.publication.commit_point_reached);
        assert!(!res.publication.published);
        assert!(!res.publication.ambiguous_termination);
        assert!(!cfg.output.exists());
        assert!(res.staging.cleaned_up);
        assert!(!Path::new(res.staging.dir.as_deref().unwrap()).exists());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn post_commit_panic_preserves_destination_and_reports_ambiguous() {
        let _guard = UNIT_SIGNAL_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = test_dir("post-panic");
        let bridge = make_stub_bridge(&dir);
        let cfg = base_config(&dir, &bridge);
        let hooks = CommitHooks {
            before_commit: None,
            after_commit: Some(Box::new(|| panic!("injected post-commit panic"))),
        };
        let res = run_native_with_hooks(&cfg, Some(hooks));
        assert_eq!(res.status, OperationStatus::AmbiguousTermination);
        assert_eq!(res.exit_code, 8);
        assert!(res.publication.commit_point_reached);
        assert!(res.publication.published);
        assert!(res.publication.ambiguous_termination);
        assert!(cfg.output.exists());
        assert!(res.staging.cleaned_up);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn post_commit_signal_preserves_destination_and_reports_ambiguous() {
        let _guard = UNIT_SIGNAL_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = test_dir("post-sig");
        let bridge = make_stub_bridge(&dir);
        let cfg = base_config(&dir, &bridge);
        let hooks = CommitHooks {
            before_commit: None,
            #[cfg(unix)]
            after_commit: Some(Box::new(|| {
                CANCEL_SIGNAL.store(unix_sys::SIGTERM, Ordering::SeqCst);
            })),
            #[cfg(not(unix))]
            after_commit: None,
        };
        let res = run_native_with_hooks(&cfg, Some(hooks));
        #[cfg(unix)]
        {
            CANCEL_SIGNAL.store(0, Ordering::SeqCst);
            assert_eq!(res.status, OperationStatus::AmbiguousTermination);
            assert_eq!(res.exit_code, 8);
            assert!(res.publication.commit_point_reached);
            assert!(res.publication.published);
            assert!(res.publication.ambiguous_termination);
            assert!(cfg.output.exists());
            assert!(res.staging.cleaned_up);
        }
        let _ = fs::remove_dir_all(&dir);
    }
}
