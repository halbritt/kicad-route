mod refill;

use crate::{
    identity::MAX_REQUEST_BYTES,
    model::{Input, Limits, Status, RESULT_VERSION},
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{hash_map::RandomState, HashSet},
    ffi::OsString,
    fs::{self, DirBuilder, File, OpenOptions},
    hash::{BuildHasher, Hasher},
    io::{ErrorKind, Read, Write},
    panic::{catch_unwind, AssertUnwindSafe},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicI32, AtomicU64, AtomicUsize, Ordering},
        Arc, Mutex, OnceLock,
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[cfg(unix)]
use std::os::unix::{
    fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
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
const MAX_SYMLINK_HOPS: usize = 40;

static OP_SEQ: AtomicU64 = AtomicU64::new(1);

#[cfg(unix)]
mod unix_sys {
    use std::ffi::c_int;
    pub const SIGINT: c_int = 2;
    pub const SIGTERM: c_int = 15;
    pub const SIGKILL: c_int = 9;
    pub const SIG_IGN: usize = 1;
    pub const SIG_ERR: usize = usize::MAX;

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
    prev_sigint: None,
    prev_sigterm: None,
});

#[cfg(unix)]
struct SignalInstallState {
    ref_count: usize,
    prev_sigint: Option<usize>,
    prev_sigterm: Option<usize>,
}

#[cfg(unix)]
extern "C" fn handle_cancel_signal(sig: std::ffi::c_int) {
    CANCEL_SIGNAL.store(sig, Ordering::SeqCst);
}

#[cfg(all(unix, target_os = "linux"))]
fn is_signal_ignored_proc(sig: std::ffi::c_int) -> Option<bool> {
    if !(1..=64).contains(&sig) {
        return None;
    }
    let status = fs::read_to_string("/proc/self/status").ok()?;
    let hex = status
        .lines()
        .find_map(|line| line.strip_prefix("SigIgn:"))?
        .trim();
    let mask = u64::from_str_radix(hex, 16).ok()?;
    Some((mask & (1u64 << (sig - 1))) != 0)
}

#[cfg(all(unix, not(target_os = "linux")))]
fn is_signal_ignored_proc(_sig: std::ffi::c_int) -> Option<bool> {
    None
}

#[cfg(unix)]
unsafe fn install_cancel_handler(sig: std::ffi::c_int, handler: usize) -> Option<usize> {
    if is_signal_ignored_proc(sig) == Some(true) {
        return None;
    }
    let prev = unix_sys::signal(sig, handler);
    if prev == unix_sys::SIG_ERR {
        return None;
    }
    if prev == unix_sys::SIG_IGN {
        let _ = unix_sys::signal(sig, unix_sys::SIG_IGN);
        let _ = CANCEL_SIGNAL.compare_exchange(sig, 0, Ordering::SeqCst, Ordering::SeqCst);
        return None;
    }
    Some(prev)
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
            // SAFETY: installing a signal-safe atomic store handler; SIG_IGN and
            // SIG_ERR are never queued for restoration, and prior handlers are
            // restored on final Drop.
            unsafe {
                state.prev_sigint = install_cancel_handler(unix_sys::SIGINT, h);
                state.prev_sigterm = install_cancel_handler(unix_sys::SIGTERM, h);
            }
        }
        state.ref_count = state
            .ref_count
            .checked_add(1)
            .expect("SignalGuard ref_count overflow");
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
                // SAFETY: restoring previously saved signal dispositions (excluding SIG_IGN/SIG_ERR).
                unsafe {
                    if let Some(prev) = state.prev_sigint.take() {
                        let _ = unix_sys::signal(unix_sys::SIGINT, prev);
                    }
                    if let Some(prev) = state.prev_sigterm.take() {
                        let _ = unix_sys::signal(unix_sys::SIGTERM, prev);
                    }
                }
                CANCEL_SIGNAL.store(0, Ordering::SeqCst);
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
    pub rules: Option<PathBuf>,
    pub project: Option<PathBuf>,
    pub limits: Option<PathBuf>,
    pub max_expansions: u64,
    pub operation_result: Option<PathBuf>,
    pub save_request: Option<PathBuf>,
    pub native_bridge: Option<PathBuf>,
    pub python: PathBuf,
    pub timeout_ms: u64,
    pub max_child_output_bytes: usize,
    pub pour_area_loss: Option<f64>,
    pub pour_min_spokes: Option<u32>,
    pub routing_options: crate::model::RoutingOptions,
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
    pub rules_path: Option<String>,
    pub rules_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project_path: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dru_path: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dru_sha256: Option<String>,
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
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub bus_groups: Vec<Vec<u32>>,
    pub result_version: u32,
    pub status: String,
    pub expanded_states: u64,
    pub work_used: u64,
    pub added_track_count: usize,
    #[serde(default)]
    pub added_via_count: usize,
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
    #[serde(skip)]
    operation_result_admitted: bool,
}

#[derive(Debug, Deserialize)]
struct ApplyValidationReport {
    schema: String,
    version: u32,
    source_id: String,
    request_sha256: String,
    candidate_sha256: String,
    candidate_bytes: u64,
    #[serde(default)]
    before_unconnected_count: Option<u64>,
    after_unconnected_count: u64,
    introduced_violation_count: u64,
    added_track_count: usize,
    #[serde(default)]
    added_via_count: usize,
    #[serde(default)]
    has_copper_zones: bool,
    #[serde(default)]
    pour_integrity_checked: bool,
    #[serde(default)]
    pour_integrity_passed: bool,
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
    static BINARY_IDENTITY: OnceLock<BinaryIdentity> = OnceLock::new();
    BINARY_IDENTITY
        .get_or_init(|| {
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
        })
        .clone()
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

fn is_canonical_board_alias(path: &Path, canonical: Option<&Path>) -> bool {
    let Some(canon) = canonical else {
        return false;
    };
    if path == canon {
        return true;
    }
    if path.file_name() != canon.file_name() {
        return false;
    }
    let parent = parent_dir_or_dot(path);
    fs::canonicalize(parent).ok().as_deref() == canon.parent()
}

fn check_sidecar_at(
    path: &Path,
    canonical: Option<&Path>,
    check: &mut SidecarCheck,
) -> Result<(), (OperationStatus, String)> {
    if path.file_name().is_none() {
        return Ok(());
    }
    let is_canon = is_canonical_board_alias(path, canonical);
    for (ext, flag) in [
        ("kicad_pro", &mut check.kicad_pro_present),
        ("kicad_dru", &mut check.kicad_dru_present),
    ] {
        let sidecar = path.with_extension(ext);
        match fs::symlink_metadata(&sidecar) {
            Ok(_) => {
                *flag = true;
                if !is_canon {
                    return Err((
                        OperationStatus::Unsupported,
                        format!(
                            "ambiguous symlink-hop .{ext} sidecar shadows canonical board: {}",
                            sidecar.display()
                        ),
                    ));
                }
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
    let mut seen: HashSet<PathBuf> = HashSet::new();
    let mut cur = original.to_path_buf();

    for hop in 0..=MAX_SYMLINK_HOPS {
        if seen.insert(cur.clone()) {
            if let Err(err) = check_sidecar_at(&cur, canonical, &mut check) {
                return (check, Err(err));
            }
        }
        let parent = parent_dir_or_dot(&cur);
        let canon_parent = fs::canonicalize(parent).ok();
        if let (Some(name), Some(cp)) = (cur.file_name(), canon_parent.as_ref()) {
            let norm_cur = cp.join(name);
            if seen.insert(norm_cur.clone()) {
                if let Err(err) = check_sidecar_at(&norm_cur, canonical, &mut check) {
                    return (check, Err(err));
                }
            }
        }
        match fs::symlink_metadata(&cur) {
            Ok(meta) if meta.file_type().is_symlink() => {
                if hop >= MAX_SYMLINK_HOPS {
                    return (
                        check,
                        Err((
                            OperationStatus::Invalid,
                            format!(
                                "too many symbolic link levels resolving source {}",
                                original.display()
                            ),
                        )),
                    );
                }
                let target = match fs::read_link(&cur) {
                    Ok(t) => t,
                    Err(e) => {
                        return (
                            check,
                            Err((
                                OperationStatus::Invalid,
                                format!("cannot read symlink {}: {e}", cur.display()),
                            )),
                        );
                    }
                };
                cur = if target.is_absolute() {
                    target
                } else {
                    canon_parent
                        .unwrap_or_else(|| parent.to_path_buf())
                        .join(target)
                };
            }
            Ok(_) => break,
            Err(e) => {
                return (
                    check,
                    Err((
                        OperationStatus::Invalid,
                        format!("cannot inspect source {}: {e}", cur.display()),
                    )),
                );
            }
        }
    }

    if let Some(canon) = canonical {
        check.checked_canonical_path = true;
        if seen.insert(canon.to_path_buf()) {
            if let Err(err) = check_sidecar_at(canon, canonical, &mut check) {
                return (check, Err(err));
            }
        }
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

fn read_bounded_regular_with_meta(
    path: &Path,
    max_bytes: usize,
    label: &str,
) -> Result<(Vec<u8>, fs::Metadata), BoundedReadError> {
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
    Ok((bytes, open_meta))
}

fn read_bounded_regular(
    path: &Path,
    max_bytes: usize,
    label: &str,
) -> Result<Vec<u8>, BoundedReadError> {
    read_bounded_regular_with_meta(path, max_bytes, label).map(|(bytes, _)| bytes)
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
        Status::OperationTimeout => OperationStatus::OperationTimeout,
        Status::Cancelled => OperationStatus::Cancelled,
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
    let binary = compute_binary_identity();
    let op_id = generate_operation_id();
    let _signal_guard = SignalGuard::acquire();
    let start = Instant::now();

    let mut result = OperationResult {
        schema: OPERATION_SCHEMA.to_owned(),
        version: OPERATION_VERSION,
        operation_id: op_id.clone(),
        binary,
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
            rules_path: config.rules.as_ref().map(|p| p.display().to_string()),
            rules_sha256: None,
            project_path: config.project.as_ref().map(|p| p.display().to_string()),
            project_sha256: None,
            dru_path: None,
            dru_sha256: None,
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
        operation_result_admitted: false,
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

#[derive(Debug)]
struct AdmittedSource {
    source_bytes: Vec<u8>,
    source_sha256: String,
    project_bytes: Option<Vec<u8>>,
    project_sha256: Option<String>,
    project_canonical: Option<PathBuf>,
    dru_bytes: Option<Vec<u8>>,
    dru_sha256: Option<String>,
}

struct AdmittedInputs {
    bridge_path: PathBuf,
    source_bytes: Vec<u8>,
    source_sha256: String,
    rules_bytes: Option<Vec<u8>>,
    project_bytes: Option<Vec<u8>>,
    dru_bytes: Option<Vec<u8>>,
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

fn check_operation_result_preflight(config: &OperationConfig, bridge_path: Option<&Path>) -> bool {
    let Some(op_result_path) = &config.operation_result else {
        return false;
    };
    if check_output_preflight(op_result_path, "operation-result").is_err() {
        return false;
    }
    let mut others: Vec<&Path> = vec![&config.source, &config.output, &config.python];
    if let Some(r) = &config.rules {
        others.push(r);
    }
    if let Some(p) = &config.project {
        others.push(p);
    }
    if let Some(l) = &config.limits {
        others.push(l);
    }
    if let Some(b) = bridge_path.or(config.native_bridge.as_deref()) {
        others.push(b);
    }
    if let Some(sr) = &config.save_request {
        others.push(sr);
    }
    !others.iter().any(|p| paths_may_alias(op_result_path, p))
}

fn admit_optional_sidecar(
    path: &Path,
    present: bool,
    label: &str,
) -> Result<Option<(Vec<u8>, fs::Metadata, PathBuf)>, (OperationStatus, String)> {
    if !present {
        return Ok(None);
    }
    let (bytes, meta) = read_bounded_regular_with_meta(path, MAX_REQUEST_BYTES, label)
        .map_err(BoundedReadError::into_status)?;
    let canon = fs::canonicalize(path).map_err(|e| {
        (
            OperationStatus::Invalid,
            format!("cannot resolve {label} {}: {e}", path.display()),
        )
    })?;
    Ok(Some((bytes, meta, canon)))
}

fn verify_sidecar_unchanged(
    path: &Path,
    present_after: bool,
    pre: &Option<(Vec<u8>, fs::Metadata, PathBuf)>,
    label: &str,
) -> Result<(), (OperationStatus, String)> {
    match (pre, present_after) {
        (None, false) => Ok(()),
        (None, true) => Err((
            OperationStatus::Invalid,
            format!("{label} appeared during read: {}", path.display()),
        )),
        (Some(_), false) => Err((
            OperationStatus::Invalid,
            format!("{label} disappeared during read: {}", path.display()),
        )),
        (Some((_, pre_meta, pre_canon)), true) => {
            let post_canon = fs::canonicalize(path).map_err(|e| {
                (
                    OperationStatus::Invalid,
                    format!("cannot re-resolve {label} {}: {e}", path.display()),
                )
            })?;
            if &post_canon != pre_canon {
                return Err((
                    OperationStatus::Invalid,
                    format!(
                        "{label} symlink/path changed during read: {} -> {}",
                        pre_canon.display(),
                        post_canon.display()
                    ),
                ));
            }
            let post_meta = fs::metadata(&post_canon).map_err(|e| {
                (
                    OperationStatus::Invalid,
                    format!(
                        "cannot inspect resolved {label} {}: {e}",
                        post_canon.display()
                    ),
                )
            })?;
            #[cfg(unix)]
            if (pre_meta.dev(), pre_meta.ino()) != (post_meta.dev(), post_meta.ino()) {
                return Err((
                    OperationStatus::Invalid,
                    format!(
                        "{label} changed during read (device/inode mismatch): {}",
                        path.display()
                    ),
                ));
            }
            #[cfg(not(unix))]
            let _ = (pre_meta, post_meta);
            Ok(())
        }
    }
}

fn admit_source_board_with_hook(
    source: &Path,
    result: &mut OperationResult,
    after_read_hook: Option<&mut dyn FnMut()>,
) -> Result<AdmittedSource, (OperationStatus, String)> {
    let canon_source = fs::canonicalize(source).map_err(|e| {
        (
            OperationStatus::Invalid,
            format!("cannot resolve source {}: {e}", source.display()),
        )
    })?;
    result.source.canonical_path = Some(canon_source.display().to_string());

    let (sidecar_check, sidecar_res) = check_source_sidecars(source, Some(&canon_source));
    result.source.sidecar_check = sidecar_check.clone();
    sidecar_res?;

    let (source_bytes, open_meta) =
        read_bounded_regular_with_meta(source, MAX_REQUEST_BYTES, "source board")
            .map_err(BoundedReadError::into_status)?;
    let pro_path = canon_source.with_extension("kicad_pro");
    let dru_path = canon_source.with_extension("kicad_dru");
    let pre_pro = match admit_optional_sidecar(
        &pro_path,
        sidecar_check.kicad_pro_present,
        "project sidecar (.kicad_pro)",
    ) {
        Ok(v) => v,
        Err(e) => {
            result.source.sidecar_check.passed = false;
            return Err(e);
        }
    };
    let pre_dru = match admit_optional_sidecar(
        &dru_path,
        sidecar_check.kicad_dru_present,
        "custom rules sidecar (.kicad_dru)",
    ) {
        Ok(v) => v,
        Err(e) => {
            result.source.sidecar_check.passed = false;
            return Err(e);
        }
    };

    if let Some(hook) = after_read_hook {
        hook();
    }

    let canon_after = fs::canonicalize(source).map_err(|e| {
        (
            OperationStatus::Invalid,
            format!("cannot re-resolve source {}: {e}", source.display()),
        )
    })?;
    let (recheck, recheck_res) = check_source_sidecars(source, Some(&canon_after));
    result.source.sidecar_check = recheck.clone();
    recheck_res?;

    if canon_after != canon_source {
        return Err((
            OperationStatus::Invalid,
            format!(
                "source board symlink/path changed during read: {} -> {}",
                canon_source.display(),
                canon_after.display()
            ),
        ));
    }
    let canon_meta = fs::metadata(&canon_after).map_err(|e| {
        (
            OperationStatus::Invalid,
            format!(
                "cannot inspect resolved source {}: {e}",
                canon_after.display()
            ),
        )
    })?;
    #[cfg(unix)]
    if (open_meta.dev(), open_meta.ino()) != (canon_meta.dev(), canon_meta.ino()) {
        return Err((
            OperationStatus::Invalid,
            format!(
                "source board changed during read (device/inode mismatch): {}",
                source.display()
            ),
        ));
    }
    #[cfg(not(unix))]
    let _ = (open_meta, canon_meta);

    if let Err(e) = verify_sidecar_unchanged(
        &canon_after.with_extension("kicad_pro"),
        recheck.kicad_pro_present,
        &pre_pro,
        "project sidecar (.kicad_pro)",
    ) {
        result.source.sidecar_check.passed = false;
        return Err(e);
    }
    if let Err(e) = verify_sidecar_unchanged(
        &canon_after.with_extension("kicad_dru"),
        recheck.kicad_dru_present,
        &pre_dru,
        "custom rules sidecar (.kicad_dru)",
    ) {
        result.source.sidecar_check.passed = false;
        return Err(e);
    }

    let source_sha256 = sha256_hex(&source_bytes);
    result.source.source_sha256 = Some(source_sha256.clone());
    result.source.source_bytes = Some(source_bytes.len() as u64);
    let (project_bytes, project_sha256, project_canonical) = match pre_pro {
        Some((b, _, c)) => {
            let sha = sha256_hex(&b);
            (Some(b), Some(sha), Some(c))
        }
        None => (None, None, None),
    };
    let (dru_bytes, dru_sha256) = match pre_dru {
        Some((b, _, _)) => {
            let sha = sha256_hex(&b);
            (Some(b), Some(sha))
        }
        None => (None, None),
    };
    Ok(AdmittedSource {
        source_bytes,
        source_sha256,
        project_bytes,
        project_sha256,
        project_canonical,
        dru_bytes,
        dru_sha256,
    })
}

fn phase_pre_snapshot_admission(
    config: &OperationConfig,
    result: &mut OperationResult,
) -> Result<AdmittedInputs, (OperationStatus, String)> {
    let t0 = Instant::now();
    result.operation_result_admitted = check_operation_result_preflight(config, None);
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
        result.operation_result_admitted =
            check_operation_result_preflight(config, Some(&bridge_path));

        let mut inputs: Vec<(&Path, &str)> = vec![
            (&config.source, "source"),
            (&bridge_path, "native-bridge"),
            (&config.python, "python"),
        ];
        if let Some(rp) = &config.rules {
            inputs.push((rp, "rules"));
        }
        if let Some(pp) = &config.project {
            inputs.push((pp, "project"));
        }
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

        let admitted_source = admit_source_board_with_hook(&config.source, result, None)?;

        if let Some(proj) = &config.project {
            let ext_ok = proj.extension().and_then(|e| e.to_str()) == Some("kicad_pro");
            let canon_src_stem = result
                .source
                .canonical_path
                .as_deref()
                .and_then(|s| Path::new(s).file_stem());
            if !ext_ok
                || proj.file_stem() != config.source.file_stem()
                || canon_src_stem.is_none()
                || proj.file_stem() != canon_src_stem
            {
                return Err((
                    OperationStatus::Invalid,
                    format!(
                        "--project {} must be a .kicad_pro file matching source board stem",
                        proj.display()
                    ),
                ));
            }
            let _ = read_bounded_regular(proj, MAX_REQUEST_BYTES, "project rules (--project)")
                .map_err(BoundedReadError::into_status)?;
            let canon_proj = fs::canonicalize(proj).map_err(|e| {
                (
                    OperationStatus::Invalid,
                    format!("cannot resolve --project {}: {e}", proj.display()),
                )
            })?;
            if canon_proj.file_stem() != canon_src_stem {
                return Err((
                    OperationStatus::Invalid,
                    format!(
                        "--project {} canonical target must match canonical source board stem",
                        proj.display()
                    ),
                ));
            }
            let canon_src_dir = result
                .source
                .canonical_path
                .as_deref()
                .and_then(|s| Path::new(s).parent());
            let proj_parent_canon = proj
                .parent()
                .filter(|p| !p.as_os_str().is_empty())
                .unwrap_or_else(|| Path::new("."))
                .canonicalize()
                .ok();
            let same_dir = proj_parent_canon.as_deref() == canon_src_dir;
            let matches_sidecar = admitted_source.project_canonical.as_ref() == Some(&canon_proj);
            if !same_dir && !matches_sidecar {
                return Err((
                    OperationStatus::Invalid,
                    format!(
                        "--project {} does not match source board directory",
                        proj.display()
                    ),
                ));
            }
            result.request.project_path = Some(proj.display().to_string());
        }

        if let Some(pro_bytes) = &admitted_source.project_bytes {
            crate::rules::parse_kicad_pro(pro_bytes)
                .map_err(|f| (map_routing_status(f.status), f.reason))?;
            if result.request.project_path.is_none() {
                if let Some(canon_src) = &result.source.canonical_path {
                    result.request.project_path = Some(
                        Path::new(canon_src)
                            .with_extension("kicad_pro")
                            .display()
                            .to_string(),
                    );
                }
            }
            result.request.project_sha256 = admitted_source.project_sha256.clone();
        }
        if let Some(dru_bytes) = &admitted_source.dru_bytes {
            crate::rules::parse_kicad_dru(dru_bytes)
                .map_err(|f| (map_routing_status(f.status), f.reason))?;
            if let Some(canon_src) = &result.source.canonical_path {
                result.request.dru_path = Some(
                    Path::new(canon_src)
                        .with_extension("kicad_dru")
                        .display()
                        .to_string(),
                );
            }
            result.request.dru_sha256 = admitted_source.dru_sha256.clone();
        }

        let rules_bytes = match &config.rules {
            Some(rp) => {
                let rb = read_bounded_regular(rp, MAX_REQUEST_BYTES, "rules")
                    .map_err(BoundedReadError::into_status)?;
                result.request.rules_sha256 = Some(sha256_hex(&rb));
                Some(rb)
            }
            None => None,
        };

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
            source_bytes: admitted_source.source_bytes,
            source_sha256: admitted_source.source_sha256,
            rules_bytes,
            project_bytes: admitted_source.project_bytes,
            dru_bytes: admitted_source.dru_bytes,
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

fn route_control(deadline: Instant) -> crate::budget::RouteControl {
    crate::budget::RouteControl {
        deadline,
        cancelled: || cancellation_signal().is_some(),
    }
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
    let staged_rules = admitted
        .rules_bytes
        .as_ref()
        .map(|_| staging_dir.join("rules.json"));
    let staged_pro = admitted
        .project_bytes
        .as_ref()
        .map(|_| staging_dir.join("source.kicad_pro"));
    let staged_dru = admitted
        .dru_bytes
        .as_ref()
        .map(|_| staging_dir.join("source.kicad_dru"));
    let staged_limits = admitted
        .limits_bytes
        .as_ref()
        .map(|_| staging_dir.join("limits.json"));
    let stage_write_res = (|| -> std::io::Result<()> {
        write_file_create_new(&staged_source, &admitted.source_bytes, 0o600)?;
        if let (Some(rp), Some(rb)) = (&staged_rules, &admitted.rules_bytes) {
            write_file_create_new(rp, rb, 0o600)?;
        }
        if let (Some(pp), Some(pb)) = (&staged_pro, &admitted.project_bytes) {
            write_file_create_new(pp, pb, 0o600)?;
        }
        if let (Some(dp), Some(db)) = (&staged_dru, &admitted.dru_bytes) {
            write_file_create_new(dp, db, 0o600)?;
        }
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
        staged_rules.as_deref(),
        staged_pro.as_deref(),
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

fn push_failed_phase(
    result: &mut OperationResult,
    phase: &str,
    elapsed: Duration,
    detail: String,
    child: Option<ChildProcessRecord>,
) {
    result.phases.push(PhaseRecord {
        phase: phase.to_owned(),
        status: "failed".to_owned(),
        elapsed_ms: elapsed.as_millis() as u64,
        detail: Some(detail),
        child,
    });
}

fn summarize_routing(route_output: &crate::model::Output) -> RoutingSummary {
    RoutingSummary {
        bus_groups: route_output.bus_groups.clone(),
        result_version: route_output.version,
        status: serde_json::to_value(route_output.status)
            .ok()
            .and_then(|v| v.as_str().map(str::to_owned))
            .unwrap_or_else(|| "unknown".to_owned()),
        expanded_states: route_output.expanded_states,
        work_used: route_output.work_used,
        added_track_count: route_output.added_tracks.len(),
        added_via_count: route_output.added_vias.len(),
        removed_track_count: route_output.removed_track_ids.len(),
        request_sha256: route_output.request_sha256.clone(),
    }
}

fn phase_rust_route_with_hook(
    request_bytes: &[u8],
    req_sha: &str,
    source_sha256: &str,
    staging_dir: &Path,
    deadline: Instant,
    result: &mut OperationResult,
    after_route_hook: Option<&mut dyn FnMut()>,
) -> Result<(crate::model::Output, PathBuf), (OperationStatus, String)> {
    if let Err(err) = check_cancel_or_deadline(deadline) {
        push_failed_phase(result, "rust_route", Duration::ZERO, err.1.clone(), None);
        return Err(err);
    }
    let t_route = Instant::now();
    let route_output = match catch_unwind(AssertUnwindSafe(|| {
        crate::run_json_with_control(request_bytes, Some(route_control(deadline)))
    })) {
        Ok(out) => out,
        Err(_) => {
            let msg = "routing executor panicked; edits discarded".to_owned();
            push_failed_phase(result, "rust_route", t_route.elapsed(), msg.clone(), None);
            return Err((OperationStatus::InternalError, msg));
        }
    };
    result.routing = Some(summarize_routing(&route_output));
    if let Some(hook) = after_route_hook {
        hook();
    }
    if let Err(err) = check_cancel_or_deadline(deadline) {
        push_failed_phase(result, "rust_route", t_route.elapsed(), err.1.clone(), None);
        return Err(err);
    }
    if route_output.version != RESULT_VERSION
        || route_output.request_sha256.as_deref() != Some(req_sha)
    {
        let msg = "routing output version or request_sha256 mismatch".to_owned();
        push_failed_phase(result, "rust_route", t_route.elapsed(), msg.clone(), None);
        return Err((OperationStatus::InternalError, msg));
    }
    if route_output.status != Status::Complete {
        let st = map_routing_status(route_output.status);
        let msg = route_output
            .reason
            .clone()
            .unwrap_or_else(|| "routing failed".to_owned());
        push_failed_phase(result, "rust_route", t_route.elapsed(), msg.clone(), None);
        return Err((st, msg));
    }
    if route_output.source_id != source_sha256 {
        let msg = "routing output source_id does not match staged snapshot".to_owned();
        push_failed_phase(result, "rust_route", t_route.elapsed(), msg.clone(), None);
        return Err((OperationStatus::InternalError, msg));
    }

    let staged_plan = staging_dir.join("plan.json");
    let plan_bytes = match serde_json::to_vec(&route_output) {
        Ok(b) => b,
        Err(e) => {
            let msg = format!("cannot serialize routing output: {e}");
            push_failed_phase(result, "rust_route", t_route.elapsed(), msg.clone(), None);
            return Err((OperationStatus::InternalError, msg));
        }
    };
    if let Err(e) = write_file_create_new(&staged_plan, &plan_bytes, 0o600) {
        let msg = format!("cannot write staging plan.json: {e}");
        push_failed_phase(result, "rust_route", t_route.elapsed(), msg.clone(), None);
        return Err((OperationStatus::InternalError, msg));
    }
    result.phases.push(PhaseRecord {
        phase: "rust_route".to_owned(),
        status: "ok".to_owned(),
        elapsed_ms: t_route.elapsed().as_millis() as u64,
        detail: None,
        child: None,
    });
    Ok((route_output, staged_plan))
}

#[allow(clippy::too_many_arguments)]
fn execute_staged_phases(
    config: &OperationConfig,
    op_id: &str,
    deadline: Instant,
    admitted: &AdmittedInputs,
    staging_dir: &Path,
    staged_source: &Path,
    staged_rules: Option<&Path>,
    staged_project: Option<&Path>,
    staged_limits: Option<&Path>,
    result: &mut OperationResult,
    hooks: &mut Option<CommitHooks<'_>>,
) -> Result<(), (OperationStatus, String)> {
    // Phase 3: native_export
    if let Err(err) = check_cancel_or_deadline(deadline) {
        push_failed_phase(result, "native_export", Duration::ZERO, err.1.clone(), None);
        return Err(err);
    }
    let staged_request = staging_dir.join("request.json");
    let mut export_args: Vec<OsString> = vec![
        "-B".into(),
        admitted.bridge_path.as_os_str().to_os_string(),
        "export".into(),
        staged_source.as_os_str().to_os_string(),
        staged_request.as_os_str().to_os_string(),
        "--max-expansions".into(),
        config.max_expansions.to_string().into(),
        "--original-source".into(),
        config.source.as_os_str().to_os_string(),
    ];
    if let Some(rp) = staged_rules {
        export_args.push("--rules".into());
        export_args.push(rp.as_os_str().to_os_string());
    }
    if let Some(pp) = staged_project {
        if config.project.is_some() {
            export_args.push("--project".into());
            export_args.push(pp.as_os_str().to_os_string());
        }
    }
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
    let mut staged_request = staged_request;
    let mut request_bytes =
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

    let mut input_has_zones = false;
    let has_custom_routing_options =
        config.routing_options != crate::model::RoutingOptions::default();
    match serde_json::from_slice::<Input>(&request_bytes) {
        Ok(mut parsed_input) => {
            if parsed_input.source_id != admitted.source_sha256 {
                let msg =
                    "exported request source_id does not match staged source snapshot".to_owned();
                result.phases.push(PhaseRecord {
                    phase: "native_export".to_owned(),
                    status: "failed".to_owned(),
                    elapsed_ms: t_exp.elapsed().as_millis() as u64,
                    detail: Some(msg.clone()),
                    child: Some(exp_outcome.record),
                });
                return Err((OperationStatus::ChildProcessError, msg));
            }
            input_has_zones = !parsed_input.zones.is_empty();
            if has_custom_routing_options {
                parsed_input.routing_options = Some(config.routing_options.clone());
                let updated_bytes = serde_json::to_vec(&parsed_input).map_err(|e| {
                    let msg = format!("failed to serialize request with routing_options: {e}");
                    result.phases.push(PhaseRecord {
                        phase: "native_export".to_owned(),
                        status: "failed".to_owned(),
                        elapsed_ms: t_exp.elapsed().as_millis() as u64,
                        detail: Some(msg.clone()),
                        child: Some(exp_outcome.record.clone()),
                    });
                    (OperationStatus::ChildProcessError, msg)
                })?;
                let opts_request = staging_dir.join("request-opts.json");
                fs::write(&opts_request, &updated_bytes).map_err(|e| {
                    let msg = format!("failed to write staged request-opts.json: {e}");
                    result.phases.push(PhaseRecord {
                        phase: "native_export".to_owned(),
                        status: "failed".to_owned(),
                        elapsed_ms: t_exp.elapsed().as_millis() as u64,
                        detail: Some(msg.clone()),
                        child: Some(exp_outcome.record.clone()),
                    });
                    (OperationStatus::ChildProcessError, msg)
                })?;
                staged_request = opts_request;
                request_bytes = updated_bytes;
            }
        }
        Err(e) if has_custom_routing_options => {
            let msg = format!("failed to parse exported request.json for routing_options: {e}");
            result.phases.push(PhaseRecord {
                phase: "native_export".to_owned(),
                status: "failed".to_owned(),
                elapsed_ms: t_exp.elapsed().as_millis() as u64,
                detail: Some(msg.clone()),
                child: Some(exp_outcome.record),
            });
            return Err((OperationStatus::ChildProcessError, msg));
        }
        Err(_) => {}
    }
    let req_sha = sha256_hex(&request_bytes);
    result.request.request_sha256 = Some(req_sha.clone());
    result.request.request_bytes = Some(request_bytes.len() as u64);
    result.request.request_json = String::from_utf8(request_bytes.clone()).ok();
    result.phases.push(PhaseRecord {
        phase: "native_export".to_owned(),
        status: "ok".to_owned(),
        elapsed_ms: t_exp.elapsed().as_millis() as u64,
        detail: None,
        child: Some(exp_outcome.record),
    });

    // Phase 4: rust_route
    let (mut route_output, staged_plan) = phase_rust_route_with_hook(
        &request_bytes,
        &req_sha,
        &admitted.source_sha256,
        staging_dir,
        deadline,
        result,
        None,
    )?;

    // Phase 5: native_apply_validate
    if let Err(err) = check_cancel_or_deadline(deadline) {
        push_failed_phase(
            result,
            "native_apply_validate",
            Duration::ZERO,
            err.1.clone(),
            None,
        );
        return Err(err);
    }
    let remaining_ms = deadline
        .saturating_duration_since(Instant::now())
        .as_millis();
    if remaining_ms == 0 {
        let msg = "operation wall-clock timeout exceeded before apply".to_owned();
        push_failed_phase(
            result,
            "native_apply_validate",
            Duration::ZERO,
            msg.clone(),
            None,
        );
        return Err((OperationStatus::OperationTimeout, msg));
    }
    let drc_timeout_secs = ((remaining_ms as f64) / 1000.0).max(0.05);
    let staged_candidate = staging_dir.join("candidate.kicad_pcb");
    let staged_report = staging_dir.join("apply-report.json");
    let mut apply_options: Vec<OsString> = vec![
        "--request".into(),
        staged_request.as_os_str().to_os_string(),
        "--original-source".into(),
        config.source.as_os_str().to_os_string(),
        "--drc-timeout".into(),
        format!("{drc_timeout_secs:.3}").into(),
        "--validation-report".into(),
        staged_report.as_os_str().to_os_string(),
    ];
    if let Some(pct) = config.pour_area_loss {
        apply_options.push("--pour-area-loss".into());
        apply_options.push(format!("{pct}").into());
    }
    if let Some(min_spokes) = config.pour_min_spokes {
        apply_options.push("--pour-min-spokes".into());
        apply_options.push(min_spokes.to_string().into());
    }
    let mut current_plan = staged_plan;
    let refill_file = staging_dir.join("refilled-copper.json");
    // A lattice or bus preference changes the search, not which nets may be repaired.
    let mut request_refill = input_has_zones
        && config.routing_options
            == crate::model::RoutingOptions {
                grid_step_nm: config.routing_options.grid_step_nm,
                bus: config.routing_options.bus,
                ..Default::default()
            };
    let (apply_outcome, t_apply) = loop {
        let t_apply = Instant::now();
        let mut apply_args: Vec<OsString> = vec![
            "-B".into(),
            admitted.bridge_path.as_os_str().to_owned(),
            "apply".into(),
            staged_source.as_os_str().to_owned(),
            current_plan.as_os_str().to_owned(),
            staged_candidate.as_os_str().to_owned(),
        ];
        apply_args.extend_from_slice(&apply_options);
        if request_refill {
            apply_args.extend(["--refill-state".into(), refill_file.as_os_str().to_owned()]);
        }
        let outcome = run_bounded_child(
            &config.python,
            &apply_args,
            deadline,
            config.max_child_output_bytes,
        );
        if outcome.spawn_error.is_none()
            && !outcome.record.timed_out
            && !outcome.record.cancelled
            && !outcome.output_exceeded
            && outcome.record.signal.is_none()
            && outcome.record.exit_code == Some(0)
        {
            break (outcome, t_apply);
        }
        let (status, reason) = parse_child_failure(&outcome, "native_apply_validate");
        push_failed_phase(
            result,
            "native_apply_validate",
            t_apply.elapsed(),
            reason.clone(),
            Some(outcome.record),
        );
        // Only the first native apply is asked for readback. On its second
        // invocation the original source and native acceptance checks still apply.
        if !request_refill
            || status != OperationStatus::NativeValidationFailed
            || !refill_file.is_file()
        {
            return Err((status, reason));
        }
        check_cancel_or_deadline(deadline)?;
        let t_repair = Instant::now();
        let attempt = (|| {
            let bytes = read_bounded_regular(&refill_file, MAX_REQUEST_BYTES, "native refill data")
                .map_err(|e| e.into_status())?;
            let measured = serde_json::from_slice(&bytes).map_err(|e| {
                (
                    OperationStatus::ChildProcessError,
                    format!("invalid native refill data: {e}"),
                )
            })?;
            let input = serde_json::from_slice(&request_bytes).map_err(|e| {
                (
                    OperationStatus::InternalError,
                    format!("cannot read original routing request: {e}"),
                )
            })?;
            refill::repair(input, measured, &mut route_output, route_control(deadline))?;
            check_cancel_or_deadline(deadline)?;
            let repair_plan = staging_dir.join("refill-plan.json");
            let plan_bytes = serde_json::to_vec(&route_output).map_err(|e| {
                (
                    OperationStatus::InternalError,
                    format!("cannot encode repaired plan: {e}"),
                )
            })?;
            write_file_create_new(&repair_plan, &plan_bytes, 0o600).map_err(|e| {
                (
                    OperationStatus::InternalError,
                    format!("cannot stage repaired plan: {e}"),
                )
            })?;
            Ok(repair_plan)
        })();
        result.routing = Some(summarize_routing(&route_output));
        match attempt {
            Ok(plan) => {
                current_plan = plan;
                request_refill = false;
                result.phases.push(PhaseRecord {
                    phase: "rust_refill_repair".into(),
                    status: "ok".into(),
                    elapsed_ms: t_repair.elapsed().as_millis() as u64,
                    detail: None,
                    child: None,
                });
            }
            Err((status, reason)) => {
                push_failed_phase(
                    result,
                    "rust_refill_repair",
                    t_repair.elapsed(),
                    reason.clone(),
                    None,
                );
                return Err((status, reason));
            }
        }
    };
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
    let partial_net_filter = !config.routing_options.net_filters.is_empty();
    let unconnected_bad = if partial_net_filter {
        report
            .before_unconnected_count
            .is_none_or(|b| report.after_unconnected_count > b)
    } else {
        report.after_unconnected_count != 0
    };
    if report.schema != APPLY_REPORT_SCHEMA
        || report.version != APPLY_REPORT_VERSION
        || report.source_id != admitted.source_sha256
        || report.request_sha256 != req_sha
        || report.candidate_sha256 != candidate_sha256
        || report.candidate_bytes != candidate_bytes_len
        || unconnected_bad
        || report.introduced_violation_count != 0
        || report.added_track_count != route_output.added_tracks.len()
        || report.added_via_count != route_output.added_vias.len()
        || ((input_has_zones || report.has_copper_zones)
            && (!report.pour_integrity_checked || !report.pour_integrity_passed))
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
    let dest_name = match config.output.file_name() {
        Some(name) => name,
        None => {
            let msg = "output needs a filename".to_owned();
            push_failed_phase(result, "final_publish", t_pub.elapsed(), msg.clone(), None);
            return Err((OperationStatus::UsageRefused, msg));
        }
    };
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

    match fs::hard_link(&pub_candidate, &config.output) {
        Ok(()) => {
            result.publication.commit_point_reached = true;
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

const USAGE_MSG: &str = "usage: kicad-route-engine route-native <SOURCE.kicad_pcb> <OUTPUT.kicad_pcb> [--rules <RULES.json>] [--project <PROJECT.kicad_pro>] [--limits <LIMITS.json>] [--max-expansions <N>] [--grid-step <MM>] [--operation-result <OP_RESULT.json>] [--save-request <REQUEST.json>] [--native-bridge <RUST_ENGINE_KICAD.py>] [--python <PYTHON_BIN>] [--timeout-ms <MS>] [--max-child-output-bytes <BYTES>] [--pour-area-loss <PCT>] [--pour-min-spokes <N>] [--diff-pairs <auto|P,N>] [--pair-layers <LAYER...>] [--no-short-pair-deferral] [--diff-pair-intra-match] [--length-match-tolerance <MM>] [--meander-amplitude <MM>] [--length-match-group <SPEC...>] [--length-match-group-json <JSON>] [--group-match-tolerance <MM>] [--group-delay-tolerance <PS>] [--group-meander-amplitude <MM>] [--group-meander-spacing <WIDTHS>] [--pair-impedance <OHM>] [--impedance-tolerance <PCT>] [--impedance-reference <NET>] [--impedance-uncoupled <MM>] [--ac-couple-chain-json <JSON>] [--ac-match-tolerance <MM>] [--ac-meander-amplitude <MM>] [--bus] [--bus-detection-radius <MM>] [--bus-attraction-radius <MM>] [--bus-attraction-bonus <N>] [--bus-min-nets <N>] [--protect <none|existing|human|locked>] [--nets <NET...>]";

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

fn parse_f64_flag(val: &OsString, flag: &str, strictly_positive: bool) -> Result<f64, String> {
    let s = val
        .to_str()
        .ok_or_else(|| format!("{flag} must be a valid UTF-8 number"))?;
    let parsed: f64 = s
        .parse()
        .map_err(|_| format!("{flag} must be a finite number"))?;
    let valid = if strictly_positive {
        parsed.is_finite() && parsed > 0.0
    } else {
        parsed.is_finite() && parsed >= 0.0
    };
    if !valid {
        let cmp = if strictly_positive { "> 0" } else { ">= 0" };
        return Err(format!("{flag} must be finite and {cmp}"));
    }
    Ok(parsed)
}

fn f64_scaled_to_i64(val: f64, scale: f64, flag: &str) -> Result<i64, String> {
    let scaled = (val * scale).round();
    if !scaled.is_finite() || scaled >= -(i64::MIN as f64) || scaled < (i64::MIN as f64) {
        return Err(format!("{flag} value out of representable range"));
    }
    Ok(scaled as i64)
}

fn take_flag_str(
    args: &[OsString],
    idx: &mut usize,
    inline: Option<&str>,
    flag: &str,
) -> Result<String, String> {
    let raw = take_flag_value(args, idx, inline, flag)?;
    raw.into_string()
        .map_err(|_| format!("{flag} value must be valid UTF-8"))
}

fn take_variadic_strs(
    args: &[OsString],
    idx: &mut usize,
    inline: Option<&str>,
    flag: &str,
) -> Result<Vec<String>, String> {
    if let Some(val) = inline {
        if val.is_empty() {
            return Err(format!("{flag} requires at least one non-empty value"));
        }
        let items: Vec<String> = val
            .split(',')
            .filter(|part| !part.is_empty())
            .map(|part| part.to_owned())
            .collect();
        if items.is_empty() {
            return Err(format!("{flag} requires at least one non-empty value"));
        }
        return Ok(items);
    }
    let mut items = Vec::new();
    while let Some(next) = args.get(*idx + 1) {
        let Some(s) = next.to_str() else {
            return Err(format!("{flag} values must be valid UTF-8"));
        };
        if s == "--" || s.starts_with('-') {
            break;
        }
        if s.is_empty() {
            return Err(format!("{flag} requires non-empty values"));
        }
        items.push(s.to_owned());
        *idx += 1;
    }
    if items.is_empty() {
        return Err(format!("{flag} requires at least one value"));
    }
    Ok(items)
}

pub fn parse_cli_args(args: &[OsString]) -> Result<OperationConfig, String> {
    let mut positional: Vec<PathBuf> = Vec::new();
    let mut rules: Option<PathBuf> = None;
    let mut project: Option<PathBuf> = None;
    let mut limits: Option<PathBuf> = None;
    let mut max_expansions: Option<u64> = None;
    let mut operation_result: Option<PathBuf> = None;
    let mut save_request: Option<PathBuf> = None;
    let mut native_bridge: Option<PathBuf> = None;
    let mut python: Option<PathBuf> = None;
    let mut timeout_ms: Option<u64> = None;
    let mut max_child_output_bytes: Option<usize> = None;
    let mut pour_area_loss: Option<f64> = None;
    let mut pour_min_spokes: Option<u32> = None;
    let mut routing_options = crate::model::RoutingOptions::default();
    let mut bus = crate::model::BusOptions::default();
    let mut bus_flags = HashSet::new();
    let mut saw_pair_layers = false;
    let mut saw_length_match_tolerance = false;
    let mut saw_meander_amplitude = false;
    let mut saw_group_match_tolerance = false;
    let mut saw_group_delay_tolerance = false;
    let mut saw_group_meander_amplitude = false;
    let mut saw_group_meander_spacing = false;
    let mut saw_pair_impedance = false;
    let mut saw_impedance_tolerance = false;
    let mut saw_impedance_reference = false;
    let mut saw_impedance_uncoupled = false;
    let mut saw_ac_match_tolerance = false;
    let mut saw_ac_meander_amplitude = false;
    let mut saw_protect = false;
    let mut last_variadic_target: Option<&'static str> = None;
    let mut after_double_dash = false;
    let flush_variadic_positionals =
        |last_variadic_target: &mut Option<&'static str>,
         positional: &mut Vec<PathBuf>,
         routing_options: &mut crate::model::RoutingOptions| {
            let Some(target) = last_variadic_target.take() else {
                return;
            };
            if positional.len() >= 2 {
                return;
            }
            let target_vec: Option<&mut Vec<String>> = match target {
                "pair_layers" => Some(&mut routing_options.pair_layers),
                "nets" => Some(&mut routing_options.net_filters),
                "group" => routing_options
                    .length_match_groups
                    .last_mut()
                    .map(|g| &mut g.members),
                _ => None,
            };
            if let Some(items) = target_vec {
                let needed = 2 - positional.len();
                let max_take = needed.min(items.len().saturating_sub(1));
                let mut k = 0usize;
                while k < max_take
                    && items[items.len() - 1 - k]
                        .to_ascii_lowercase()
                        .ends_with(".kicad_pcb")
                {
                    k += 1;
                }
                if k > 0 {
                    let start = items.len() - k;
                    let stolen: Vec<String> = items.drain(start..).collect();
                    for s in stolen {
                        positional.push(PathBuf::from(s));
                    }
                }
            }
        };

    let mut i = 0;
    while i < args.len() {
        let arg = &args[i];
        if !after_double_dash && arg == "--" {
            flush_variadic_positionals(
                &mut last_variadic_target,
                &mut positional,
                &mut routing_options,
            );
            after_double_dash = true;
            i += 1;
            continue;
        }
        if !after_double_dash {
            if let Some(s) = arg.to_str() {
                if s.starts_with('-') {
                    flush_variadic_positionals(
                        &mut last_variadic_target,
                        &mut positional,
                        &mut routing_options,
                    );
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
                        "--project" => {
                            if project.is_some() {
                                return Err("duplicate --project flag".to_owned());
                            }
                            project =
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
                        "--pour-area-loss" => {
                            if pour_area_loss.is_some() {
                                return Err("duplicate --pour-area-loss flag".to_owned());
                            }
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            let s = val.to_str().ok_or_else(|| {
                                "--pour-area-loss must be a valid UTF-8 number".to_owned()
                            })?;
                            let parsed: f64 = s.parse().map_err(|_| {
                                "--pour-area-loss must be a finite percentage in 0..=100".to_owned()
                            })?;
                            if !parsed.is_finite() || !(0.0..=100.0).contains(&parsed) {
                                return Err(
                                    "--pour-area-loss must be a finite percentage in 0..=100"
                                        .to_owned(),
                                );
                            }
                            pour_area_loss = Some(parsed);
                        }
                        "--pour-min-spokes" => {
                            if pour_min_spokes.is_some() {
                                return Err("duplicate --pour-min-spokes flag".to_owned());
                            }
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            let parsed = parse_u64_flag(&val, flag, 0, 16)?;
                            pour_min_spokes = Some(parsed as u32);
                            last_variadic_target = None;
                        }
                        "--diff-pairs" => {
                            let val = take_flag_str(args, &mut i, inline, flag)?;
                            if val == "auto" {
                                if routing_options.diff_pairs_mode.is_some() {
                                    return Err("duplicate --diff-pairs auto flag".to_owned());
                                }
                                if !routing_options.explicit_pairs.is_empty() {
                                    return Err(
                                        "cannot combine --diff-pairs auto with explicit --diff-pairs P,N"
                                            .to_owned(),
                                    );
                                }
                                routing_options.diff_pairs_mode =
                                    Some(crate::model::DiffPairsMode::Auto);
                            } else {
                                if routing_options.diff_pairs_mode.is_some() {
                                    return Err(
                                        "cannot combine --diff-pairs auto with explicit --diff-pairs P,N"
                                            .to_owned(),
                                    );
                                }
                                let parts: Vec<&str> = val.split(',').collect();
                                if parts.len() != 2 || parts[0].is_empty() || parts[1].is_empty() {
                                    return Err(
                                        "--diff-pairs requires 'auto' or '<POS_NET>,<NEG_NET>'"
                                            .to_owned(),
                                    );
                                }
                                routing_options
                                    .explicit_pairs
                                    .push([parts[0].to_owned(), parts[1].to_owned()]);
                            }
                            last_variadic_target = None;
                        }
                        "--pair-layers" => {
                            if saw_pair_layers {
                                return Err("duplicate --pair-layers flag".to_owned());
                            }
                            saw_pair_layers = true;
                            routing_options.pair_layers =
                                take_variadic_strs(args, &mut i, inline, flag)?;
                            last_variadic_target = if inline.is_none() {
                                Some("pair_layers")
                            } else {
                                None
                            };
                        }
                        "--no-short-pair-deferral" => {
                            if inline.is_some() {
                                return Err("--no-short-pair-deferral takes no value".to_owned());
                            }
                            routing_options.no_short_pair_deferral = true;
                            last_variadic_target = None;
                        }
                        "--diff-pair-intra-match" => {
                            if inline.is_some() {
                                return Err("--diff-pair-intra-match takes no value".to_owned());
                            }
                            routing_options.intra_match = true;
                            last_variadic_target = None;
                        }
                        "--length-match-tolerance" => {
                            if saw_length_match_tolerance {
                                return Err("duplicate --length-match-tolerance flag".to_owned());
                            }
                            saw_length_match_tolerance = true;
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            let mm = parse_f64_flag(&val, flag, false)?;
                            let nm = f64_scaled_to_i64(mm, 1_000_000.0, flag)?;
                            if mm > 0.0 && nm == 0 {
                                return Err(format!("{flag} non-zero values must be >= 1 nm"));
                            }
                            routing_options.length_match_tolerance_nm = Some(nm);
                            last_variadic_target = None;
                        }
                        "--meander-amplitude" => {
                            if saw_meander_amplitude {
                                return Err("duplicate --meander-amplitude flag".to_owned());
                            }
                            saw_meander_amplitude = true;
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            let mm = parse_f64_flag(&val, flag, true)?;
                            let nm = f64_scaled_to_i64(mm, 1_000_000.0, flag)?;
                            if nm <= 0 {
                                return Err(format!("{flag} must be >= 1 nm"));
                            }
                            routing_options.meander_amplitude_nm = Some(nm);
                            last_variadic_target = None;
                        }
                        "--length-match-group" => {
                            let members = take_variadic_strs(args, &mut i, inline, flag)?;
                            routing_options.length_match_groups.push(
                                crate::model::LengthMatchGroupSpec {
                                    members,
                                    ..Default::default()
                                },
                            );
                            last_variadic_target = if inline.is_none() {
                                Some("group")
                            } else {
                                None
                            };
                        }
                        "--length-match-group-json" => {
                            let val = take_flag_str(args, &mut i, inline, flag)?;
                            let trimmed = val.trim();
                            if trimmed.starts_with('[') {
                                let members: Vec<String> =
                                    serde_json::from_str(trimmed).map_err(|e| {
                                        format!("invalid --length-match-group-json array: {e}")
                                    })?;
                                if members.len() < 2 || members.iter().any(|m| m.is_empty()) {
                                    return Err(
                                        "--length-match-group-json array requires at least two non-empty members"
                                            .to_owned(),
                                    );
                                }
                                routing_options.length_match_groups.push(
                                    crate::model::LengthMatchGroupSpec {
                                        members,
                                        ..Default::default()
                                    },
                                );
                            } else if trimmed.starts_with('{') {
                                let spec: crate::model::LengthMatchGroupSpec =
                                    serde_json::from_str(trimmed).map_err(|e| {
                                        format!("invalid --length-match-group-json object: {e}")
                                    })?;
                                if spec.members.is_empty() && spec.from_to.is_empty() {
                                    return Err("--length-match-group-json spec must not be empty"
                                        .to_owned());
                                }
                                if spec.members.iter().any(|m| m.is_empty()) {
                                    return Err(
                                        "--length-match-group-json members must be non-empty strings"
                                            .to_owned(),
                                    );
                                }
                                if !spec.members.is_empty() && spec.members.len() < 2 {
                                    return Err(
                                        "--length-match-group-json members requires at least two entries"
                                            .to_owned(),
                                    );
                                }
                                if spec
                                    .from_to
                                    .iter()
                                    .any(|pair| pair[0].is_empty() || pair[1].is_empty())
                                {
                                    return Err(
                                        "--length-match-group-json from_to entries must be non-empty strings"
                                            .to_owned(),
                                    );
                                }
                                if spec.members.is_empty() && spec.from_to.len() < 2 {
                                    return Err(
                                        "--length-match-group-json from_to requires at least two entries when members is omitted"
                                            .to_owned(),
                                    );
                                }
                                if spec.tolerance_nm.is_some_and(|v| v < 0) {
                                    return Err(
                                        "--length-match-group-json tolerance_nm must be >= 0"
                                            .to_owned(),
                                    );
                                }
                                if spec.delay_tolerance_as.is_some_and(|v| v < 0) {
                                    return Err(
                                        "--length-match-group-json delay_tolerance_as must be >= 0"
                                            .to_owned(),
                                    );
                                }
                                if spec.tolerance_nm.is_some() && spec.delay_tolerance_as.is_some()
                                {
                                    return Err(
                                        "--length-match-group-json tolerance_nm and delay_tolerance_as are mutually exclusive"
                                            .to_owned(),
                                    );
                                }
                                if spec.meander_amplitude_nm.is_some_and(|v| v <= 0) {
                                    return Err(
                                        "--length-match-group-json meander_amplitude_nm must be > 0"
                                            .to_owned(),
                                    );
                                }
                                if spec
                                    .meander_spacing_widths
                                    .is_some_and(|v| !v.is_finite() || v < 1.0)
                                {
                                    return Err(
                                        "--length-match-group-json meander_spacing_widths must be finite and >= 1.0"
                                            .to_owned(),
                                    );
                                }
                                routing_options.length_match_groups.push(spec);
                            } else {
                                return Err(
                                    "--length-match-group-json must be a JSON array or object"
                                        .to_owned(),
                                );
                            }
                            last_variadic_target = None;
                        }
                        "--group-match-tolerance" => {
                            if saw_group_match_tolerance {
                                return Err("duplicate --group-match-tolerance flag".to_owned());
                            }
                            saw_group_match_tolerance = true;
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            let mm = parse_f64_flag(&val, flag, false)?;
                            let nm = f64_scaled_to_i64(mm, 1_000_000.0, flag)?;
                            if mm > 0.0 && nm == 0 {
                                return Err(format!("{flag} non-zero values must be >= 1 nm"));
                            }
                            routing_options.group_match_tolerance_nm = Some(nm);
                            last_variadic_target = None;
                        }
                        "--group-delay-tolerance" => {
                            if saw_group_delay_tolerance {
                                return Err("duplicate --group-delay-tolerance flag".to_owned());
                            }
                            saw_group_delay_tolerance = true;
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            let ps = parse_f64_flag(&val, flag, false)?;
                            let as_val = f64_scaled_to_i64(ps, 1_000_000.0, flag)?;
                            if ps > 0.0 && as_val == 0 {
                                return Err(format!("{flag} non-zero values must be >= 1 as"));
                            }
                            routing_options.group_delay_tolerance_as = Some(as_val);
                            last_variadic_target = None;
                        }
                        "--group-meander-amplitude" => {
                            if saw_group_meander_amplitude {
                                return Err("duplicate --group-meander-amplitude flag".to_owned());
                            }
                            saw_group_meander_amplitude = true;
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            let mm = parse_f64_flag(&val, flag, true)?;
                            let nm = f64_scaled_to_i64(mm, 1_000_000.0, flag)?;
                            if nm <= 0 {
                                return Err(format!("{flag} must be >= 1 nm"));
                            }
                            routing_options.group_meander_amplitude_nm = Some(nm);
                            last_variadic_target = None;
                        }
                        "--group-meander-spacing" => {
                            if saw_group_meander_spacing {
                                return Err("duplicate --group-meander-spacing flag".to_owned());
                            }
                            saw_group_meander_spacing = true;
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            let widths = parse_f64_flag(&val, flag, true)?;
                            if widths < 1.0 {
                                return Err("--group-meander-spacing must be >= 1.0".to_owned());
                            }
                            routing_options.group_meander_spacing_widths = Some(widths);
                            last_variadic_target = None;
                        }
                        "--pair-impedance" => {
                            if saw_pair_impedance {
                                return Err("duplicate --pair-impedance flag".to_owned());
                            }
                            saw_pair_impedance = true;
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            routing_options.pair_impedance_ohm =
                                Some(parse_f64_flag(&val, flag, true)?);
                            last_variadic_target = None;
                        }
                        "--impedance-tolerance" => {
                            if saw_impedance_tolerance {
                                return Err("duplicate --impedance-tolerance flag".to_owned());
                            }
                            saw_impedance_tolerance = true;
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            routing_options.impedance_tolerance_pct =
                                Some(parse_f64_flag(&val, flag, true)?);
                            last_variadic_target = None;
                        }
                        "--impedance-reference" => {
                            if saw_impedance_reference {
                                return Err("duplicate --impedance-reference flag".to_owned());
                            }
                            saw_impedance_reference = true;
                            routing_options.impedance_reference =
                                Some(take_flag_str(args, &mut i, inline, flag)?);
                            last_variadic_target = None;
                        }
                        "--impedance-uncoupled" => {
                            if saw_impedance_uncoupled {
                                return Err("duplicate --impedance-uncoupled flag".to_owned());
                            }
                            saw_impedance_uncoupled = true;
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            let mm = parse_f64_flag(&val, flag, false)?;
                            let nm = f64_scaled_to_i64(mm, 1_000_000.0, flag)?;
                            if mm > 0.0 && nm == 0 {
                                return Err(format!("{flag} non-zero values must be >= 1 nm"));
                            }
                            routing_options.impedance_uncoupled_nm = Some(nm);
                            last_variadic_target = None;
                        }
                        "--ac-couple-chain-json" => {
                            let val = take_flag_str(args, &mut i, inline, flag)?;
                            let refs: Vec<String> =
                                serde_json::from_str(val.trim()).map_err(|e| {
                                    format!("invalid --ac-couple-chain-json array: {e}")
                                })?;
                            if refs.len() < 2 || refs.iter().any(|r| r.is_empty()) {
                                return Err(
                                    "--ac-couple-chain-json requires at least two non-empty bridge references"
                                        .to_owned(),
                                );
                            }
                            routing_options.ac_couple_chains.push(refs);
                            last_variadic_target = None;
                        }
                        "--ac-match-tolerance" => {
                            if saw_ac_match_tolerance {
                                return Err("duplicate --ac-match-tolerance flag".to_owned());
                            }
                            saw_ac_match_tolerance = true;
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            let mm = parse_f64_flag(&val, flag, true)?;
                            let nm = f64_scaled_to_i64(mm, 1_000_000.0, flag)?;
                            if nm <= 0 {
                                return Err(format!("{flag} must be >= 1 nm"));
                            }
                            routing_options.ac_match_tolerance_nm = Some(nm);
                            last_variadic_target = None;
                        }
                        "--ac-meander-amplitude" => {
                            if saw_ac_meander_amplitude {
                                return Err("duplicate --ac-meander-amplitude flag".to_owned());
                            }
                            saw_ac_meander_amplitude = true;
                            let val = take_flag_value(args, &mut i, inline, flag)?;
                            let mm = parse_f64_flag(&val, flag, true)?;
                            let nm = f64_scaled_to_i64(mm, 1_000_000.0, flag)?;
                            if nm <= 0 {
                                return Err(format!("{flag} must be >= 1 nm"));
                            }
                            routing_options.ac_meander_amplitude_nm = Some(nm);
                            last_variadic_target = None;
                        }
                        "--grid-step" => {
                            if routing_options.grid_step_nm.is_some() {
                                return Err("duplicate --grid-step flag".into());
                            }
                            let value = take_flag_value(args, &mut i, inline, flag)?;
                            let mm = parse_f64_flag(&value, flag, true)?;
                            if !(0.000001..=crate::model::MAX_NM as f64 / 1_000_000.0).contains(&mm)
                            {
                                return Err(
                                    "--grid-step must be in the supported range 0.000001..=100 mm"
                                        .into(),
                                );
                            }
                            routing_options.grid_step_nm =
                                Some(f64_scaled_to_i64(mm, 1_000_000.0, flag)?);
                        }
                        "--bus"
                        | "--bus-detection-radius"
                        | "--bus-attraction-radius"
                        | "--bus-attraction-bonus"
                        | "--bus-min-nets" => {
                            if !bus_flags.insert(flag) {
                                return Err(format!("duplicate {flag} flag"));
                            }
                            if flag == "--bus" {
                                if inline.is_some() {
                                    return Err("--bus takes no value".to_owned());
                                }
                            } else {
                                let value = take_flag_value(args, &mut i, inline, flag)?;
                                match flag {
                                    "--bus-attraction-bonus" => {
                                        bus.attraction_bonus =
                                            parse_u64_flag(&value, flag, 0, u32::MAX.into())?
                                                as u32;
                                    }
                                    "--bus-min-nets" => {
                                        bus.min_nets =
                                            parse_u64_flag(&value, flag, 2, usize::MAX as u64)?
                                                as usize;
                                    }
                                    _ => {
                                        let mm = parse_f64_flag(&value, flag, true)?;
                                        if mm < 0.000001 {
                                            return Err(format!("{flag} must be at least 1 nm"));
                                        }
                                        let nm = f64_scaled_to_i64(mm, 1_000_000.0, flag)?;
                                        if flag == "--bus-detection-radius" {
                                            bus.detection_radius_nm = nm;
                                        } else {
                                            bus.attraction_radius_nm = nm;
                                        }
                                    }
                                }
                            }
                        }
                        "--protect" => {
                            if saw_protect {
                                return Err("duplicate --protect flag".to_owned());
                            }
                            saw_protect = true;
                            let val = take_flag_str(args, &mut i, inline, flag)?;
                            let mode = match val.as_str() {
                                "none" => crate::model::ProtectMode::None,
                                "existing" => crate::model::ProtectMode::Existing,
                                "human" => crate::model::ProtectMode::Human,
                                "locked" => crate::model::ProtectMode::Locked,
                                _ => {
                                    return Err(
                                        "--protect must be one of: none, existing, human, locked"
                                            .to_owned(),
                                    );
                                }
                            };
                            routing_options.protect_mode = Some(mode);
                            last_variadic_target = None;
                        }
                        "--nets" => {
                            let items = take_variadic_strs(args, &mut i, inline, flag)?;
                            routing_options.net_filters.extend(items);
                            last_variadic_target =
                                if inline.is_none() { Some("nets") } else { None };
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

    flush_variadic_positionals(
        &mut last_variadic_target,
        &mut positional,
        &mut routing_options,
    );

    if bus_flags.contains("--bus") {
        routing_options.bus = Some(bus);
    } else if !bus_flags.is_empty() {
        return Err("bus controls require --bus".into());
    }
    if positional.len() != 2 {
        return Err(format!(
            "expected <SOURCE.kicad_pcb> <OUTPUT.kicad_pcb>, got {} positional arguments",
            positional.len()
        ));
    }
    let mut pos_iter = positional.into_iter();
    let source = pos_iter.next().unwrap();
    let output = pos_iter.next().unwrap();
    Ok(OperationConfig {
        source,
        output,
        rules,
        project,
        limits,
        max_expansions: max_expansions.unwrap_or(DEFAULT_MAX_EXPANSIONS),
        operation_result,
        save_request,
        native_bridge,
        python: python.unwrap_or_else(|| PathBuf::from("/usr/bin/python3")),
        timeout_ms: timeout_ms.unwrap_or(DEFAULT_TIMEOUT_MS),
        max_child_output_bytes: max_child_output_bytes.unwrap_or(DEFAULT_MAX_CHILD_OUTPUT_BYTES),
        pour_area_loss,
        pour_min_spokes,
        routing_options,
    })
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
            rules_path: None,
            rules_sha256: None,
            project_path: None,
            project_sha256: None,
            dru_path: None,
            dru_sha256: None,
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
        operation_result_admitted: false,
    }
}

fn emit_stderr_failure(result: &OperationResult) {
    let summary = serde_json::json!({
        "status": result.status,
        "exit_code": result.exit_code,
        "reason": result.reason,
    });
    if let Ok(line) = serde_json::to_string(&summary) {
        eprintln!("{line}");
    }
}

pub fn run_cli(args: &[OsString]) -> u8 {
    for arg in args {
        if arg == "--" {
            break;
        }
        if arg == "--help" || arg == "-h" {
            println!("{USAGE_MSG}");
            return 0;
        }
    }
    let config = match parse_cli_args(args) {
        Ok(c) => c,
        Err(msg) => {
            let result = usage_error_result(msg.clone());
            if let Ok(json_text) = serde_json::to_string_pretty(&result) {
                println!("{json_text}");
            }
            eprintln!("{msg}\n{USAGE_MSG}");
            emit_stderr_failure(&result);
            return result.exit_code;
        }
    };

    let mut result = run_native(&config);
    if let Some(op_result_path) = &config.operation_result {
        if result.operation_result_admitted {
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
                            result.exit_code = OperationStatus::AmbiguousTermination.exit_code();
                            let note = format!(
                                "destination board was published, but writing --operation-result {} failed: {e}",
                                op_result_path.display()
                            );
                            let combined = match result.publication.reconciliation_note.take() {
                                Some(prev) => format!("{prev}; {note}"),
                                None => note,
                            };
                            result.publication.reconciliation_note = Some(combined.clone());
                            result.reason = Some(combined);
                        } else {
                            result.status = if e.kind() == ErrorKind::AlreadyExists {
                                OperationStatus::DestinationExists
                            } else {
                                OperationStatus::PublicationRefused
                            };
                            result.exit_code = result.status.exit_code();
                            let write_err = format!(
                                "cannot write --operation-result {}: {e}",
                                op_result_path.display()
                            );
                            result.reason = Some(match result.reason.take() {
                                Some(prev) => format!("{write_err} (prior error: {prev})"),
                                None => write_err,
                            });
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
            return if result.publication.commit_point_reached || result.exit_code == 8 {
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
        emit_stderr_failure(&result);
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
            rules: Some(rules),
            project: None,
            limits: None,
            max_expansions: 1000,
            operation_result: None,
            save_request: None,
            native_bridge: Some(bridge.to_path_buf()),
            python: PathBuf::from("/usr/bin/python3"),
            timeout_ms: 15_000,
            max_child_output_bytes: DEFAULT_MAX_CHILD_OUTPUT_BYTES,
            pour_area_loss: None,
            pour_min_spokes: None,
            routing_options: crate::model::RoutingOptions::default(),
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

    #[cfg(unix)]
    #[test]
    fn signal_guard_ref_count_and_sig_ign_preservation() {
        let _guard = UNIT_SIGNAL_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let prev = unsafe { unix_sys::signal(unix_sys::SIGTERM, unix_sys::SIG_IGN) };
        CANCEL_SIGNAL.store(0, Ordering::SeqCst);

        let g1 = SignalGuard::acquire();
        {
            let st = SIGNAL_STATE.lock().unwrap_or_else(|p| p.into_inner());
            assert_eq!(st.ref_count, 1);
            assert!(st.prev_sigterm.is_none());
        }
        let g2 = SignalGuard::acquire();
        {
            let st = SIGNAL_STATE.lock().unwrap_or_else(|p| p.into_inner());
            assert_eq!(st.ref_count, 2);
        }
        unsafe {
            let _ = unix_sys::kill(std::process::id() as std::ffi::c_int, unix_sys::SIGTERM);
        }
        assert_eq!(CANCEL_SIGNAL.load(Ordering::SeqCst), 0);

        drop(g2);
        {
            let st = SIGNAL_STATE.lock().unwrap_or_else(|p| p.into_inner());
            assert_eq!(st.ref_count, 1);
        }
        drop(g1);
        {
            let st = SIGNAL_STATE.lock().unwrap_or_else(|p| p.into_inner());
            assert_eq!(st.ref_count, 0);
        }

        let after = unsafe { unix_sys::signal(unix_sys::SIGTERM, prev) };
        assert_eq!(after, unix_sys::SIG_IGN);
        CANCEL_SIGNAL.store(0, Ordering::SeqCst);
    }

    #[test]
    fn rust_route_cancel_beats_work_exhausted_and_emits_failed_phase() {
        let _guard = UNIT_SIGNAL_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = test_dir("cancel-vs-work");
        let bridge = make_stub_bridge(&dir);
        let cfg = base_config(&dir, &bridge);
        let mut res = usage_error_result("init".to_owned());
        let source_bytes = b"(kicad_pcb)";
        let source_sha = sha256_hex(source_bytes);
        let req = serde_json::json!({
            "version": 1,
            "source_id": source_sha,
            "layer": "F.Cu",
            "bounds": {"min": [0, 0], "max": [60, 60]},
            "rules": {"width_nm": 2, "clearance_nm": 1, "grid_nm": 10, "edge_clearance_nm": 0},
            "nets": [{"id": 1, "name": "A"}],
            "pads": [
                {"id": "10000000-0000-4000-8000-000000000001", "net": 1, "at": [10, 10], "shape": {"kind": "circle", "radius_nm": 1}},
                {"id": "10000000-0000-4000-8000-000000000002", "net": 1, "at": [50, 10], "shape": {"kind": "circle", "radius_nm": 1}}
            ],
            "tracks": [],
            "max_expansions": 1000,
            "limits": {"max_work": 0}
        });
        let req_bytes = serde_json::to_vec(&req).unwrap();
        let req_sha = sha256_hex(&req_bytes);
        let deadline = Instant::now() + Duration::from_secs(10);

        #[cfg(unix)]
        {
            CANCEL_SIGNAL.store(0, Ordering::SeqCst);
            let mut hook = || {
                CANCEL_SIGNAL.store(unix_sys::SIGTERM, Ordering::SeqCst);
            };
            let err = phase_rust_route_with_hook(
                &req_bytes,
                &req_sha,
                &source_sha,
                &dir,
                deadline,
                &mut res,
                Some(&mut hook),
            )
            .unwrap_err();
            CANCEL_SIGNAL.store(0, Ordering::SeqCst);
            assert_eq!(err.0, OperationStatus::Cancelled);
            assert_eq!(res.routing.as_ref().unwrap().status, "work_exhausted");
            assert_eq!(res.phases.last().unwrap().phase, "rust_route");
            assert_eq!(res.phases.last().unwrap().status, "failed");
        }
        let _ = cfg;
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn compute_binary_identity_cached_and_native_export_deadline_emits_failed_phase() {
        let _guard = UNIT_SIGNAL_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let id_a = compute_binary_identity();
        let id_b = compute_binary_identity();
        assert_eq!(id_a.name, id_b.name);
        assert_eq!(id_a.binary_sha256, id_b.binary_sha256);

        let dir = test_dir("expired-export");
        let bridge = make_stub_bridge(&dir);
        let cfg = base_config(&dir, &bridge);
        let admitted = AdmittedInputs {
            bridge_path: bridge,
            source_bytes: b"(kicad_pcb)".to_vec(),
            source_sha256: sha256_hex(b"(kicad_pcb)"),
            rules_bytes: Some(b"{}".to_vec()),
            project_bytes: None,
            dru_bytes: None,
            limits_bytes: None,
        };
        let mut res = usage_error_result("init".to_owned());
        let expired = Instant::now() - Duration::from_millis(1);
        let mut hooks = None;
        let err = execute_staged_phases(
            &cfg,
            "op-expired",
            expired,
            &admitted,
            &dir,
            &cfg.source,
            cfg.rules.as_deref(),
            None,
            None,
            &mut res,
            &mut hooks,
        )
        .unwrap_err();
        assert_eq!(err.0, OperationStatus::OperationTimeout);
        assert_eq!(res.phases.len(), 1);
        assert_eq!(res.phases[0].phase, "native_export");
        assert_eq!(res.phases[0].status, "failed");
        let _ = fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn admit_source_rejects_mid_read_symlink_retarget_and_inode_swap() {
        let dir = test_dir("toctou-source");
        let clean_a = dir.join("clean_a.kicad_pcb");
        let clean_b = dir.join("clean_b.kicad_pcb");
        fs::write(&clean_a, b"(kicad_pcb a)").unwrap();
        fs::write(&clean_b, b"(kicad_pcb b)").unwrap();

        // Case A: symlink retarget between read and post-read canonicalization
        let link = dir.join("link.kicad_pcb");
        std::os::unix::fs::symlink(&clean_a, &link).unwrap();
        let mut res_a = usage_error_result("toctou-a".to_owned());
        let mut retarget_hook = || {
            let tmp = dir.join(".link.tmp");
            std::os::unix::fs::symlink(&clean_b, &tmp).unwrap();
            fs::rename(&tmp, &link).unwrap();
        };
        let err_a =
            admit_source_board_with_hook(&link, &mut res_a, Some(&mut retarget_hook)).unwrap_err();
        assert_eq!(err_a.0, OperationStatus::Invalid);
        assert!(err_a.1.contains("changed during read"), "got: {}", err_a.1);

        // Case B: same-path inode replacement via atomic rename
        let target = dir.join("target.kicad_pcb");
        let replacement = dir.join("replacement.kicad_pcb");
        fs::write(&target, b"(kicad_pcb v1)").unwrap();
        fs::write(&replacement, b"(kicad_pcb v2)").unwrap();
        let mut res_b = usage_error_result("toctou-b".to_owned());
        let mut swap_hook = || {
            fs::rename(&replacement, &target).unwrap();
        };
        let err_b =
            admit_source_board_with_hook(&target, &mut res_b, Some(&mut swap_hook)).unwrap_err();
        assert_eq!(err_b.0, OperationStatus::Invalid);
        assert!(
            err_b.1.contains("device/inode mismatch"),
            "got: {}",
            err_b.1
        );

        // Case C: sidecar created after read
        let plain = dir.join("plain.kicad_pcb");
        fs::write(&plain, b"(kicad_pcb plain)").unwrap();
        let mut res_c = usage_error_result("toctou-c".to_owned());
        let mut sidecar_hook = || {
            fs::write(plain.with_extension("kicad_pro"), b"{}").unwrap();
        };
        let err_c =
            admit_source_board_with_hook(&plain, &mut res_c, Some(&mut sidecar_hook)).unwrap_err();
        assert_eq!(err_c.0, OperationStatus::Invalid);
        assert!(res_c.source.sidecar_check.kicad_pro_present);
        assert!(err_c.1.contains("appeared during read"), "got: {}", err_c.1);

        let _ = fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn check_source_sidecars_rejects_symlink_loop_and_excessive_hops() {
        let dir = test_dir("symlink-hops");
        let real = dir.join("real.kicad_pcb");
        fs::write(&real, b"(kicad_pcb)").unwrap();

        let mut prev = real.clone();
        for i in 0..=MAX_SYMLINK_HOPS {
            let link = dir.join(format!("hop_{i}.kicad_pcb"));
            std::os::unix::fs::symlink(&prev, &link).unwrap();
            prev = link;
        }
        let (check, res) = check_source_sidecars(&prev, None);
        assert!(!check.passed);
        let err = res.unwrap_err();
        assert_eq!(err.0, OperationStatus::Invalid);
        assert!(
            err.1.contains("too many symbolic link levels"),
            "got: {}",
            err.1
        );
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn cli_parses_all_m4_diff_pair_and_length_group_flags() {
        let args: Vec<OsString> = vec![
            "src.kicad_pcb",
            "out.kicad_pcb",
            "--diff-pairs",
            "auto",
            "--pair-layers",
            "F.Cu",
            "In2.Cu",
            "B.Cu",
            "--no-short-pair-deferral",
            "--diff-pair-intra-match",
            "--length-match-tolerance",
            "0.05",
            "--meander-amplitude",
            "1.5",
            "--length-match-group",
            "DQ0",
            "DQ1",
            "--length-match-group-json",
            "[\"-DQ2\",\"DQ3\"]",
            "--group-match-tolerance",
            "0.1",
            "--group-delay-tolerance",
            "1.25",
            "--group-meander-amplitude",
            "3.0",
            "--group-meander-spacing",
            "2.5",
            "--pair-impedance",
            "100",
            "--impedance-tolerance",
            "10",
            "--impedance-reference",
            "GND",
            "--impedance-uncoupled",
            "3.0",
            "--protect",
            "existing",
            "--nets",
            "A_P",
            "A_N",
            "!CLK*",
        ]
        .into_iter()
        .map(OsString::from)
        .collect();
        let cfg = parse_cli_args(&args).expect("all 18 M4 flags should parse");
        let ro = &cfg.routing_options;
        assert_eq!(ro.diff_pairs_mode, Some(crate::model::DiffPairsMode::Auto));
        assert_eq!(ro.pair_layers, vec!["F.Cu", "In2.Cu", "B.Cu"]);
        assert!(ro.no_short_pair_deferral);
        assert!(ro.intra_match);
        assert_eq!(ro.length_match_tolerance_nm, Some(50_000));
        assert_eq!(ro.meander_amplitude_nm, Some(1_500_000));
        assert_eq!(ro.length_match_groups.len(), 2);
        assert_eq!(ro.length_match_groups[0].members, vec!["DQ0", "DQ1"]);
        assert_eq!(ro.length_match_groups[1].members, vec!["-DQ2", "DQ3"]);
        assert_eq!(ro.group_match_tolerance_nm, Some(100_000));
        assert_eq!(ro.group_delay_tolerance_as, Some(1_250_000));
        assert_eq!(ro.group_meander_amplitude_nm, Some(3_000_000));
        assert_eq!(ro.group_meander_spacing_widths, Some(2.5));
        assert_eq!(ro.pair_impedance_ohm, Some(100.0));
        assert_eq!(ro.impedance_tolerance_pct, Some(10.0));
        assert_eq!(ro.impedance_reference.as_deref(), Some("GND"));
        assert_eq!(ro.impedance_uncoupled_nm, Some(3_000_000));
        assert_eq!(ro.protect_mode, Some(crate::model::ProtectMode::Existing));
        assert_eq!(ro.net_filters, vec!["A_P", "A_N", "!CLK*"]);
    }

    #[test]
    fn cli_usage_exposes_supported_routing_options() {
        for flag in [
            "--diff-pairs",
            "--pair-layers",
            "--no-short-pair-deferral",
            "--diff-pair-intra-match",
            "--length-match-tolerance",
            "--meander-amplitude",
            "--length-match-group",
            "--length-match-group-json",
            "--group-match-tolerance",
            "--group-delay-tolerance",
            "--group-meander-amplitude",
            "--group-meander-spacing",
            "--pair-impedance",
            "--impedance-tolerance",
            "--impedance-reference",
            "--impedance-uncoupled",
            "--ac-couple-chain-json",
            "--ac-match-tolerance",
            "--ac-meander-amplitude",
            "--bus",
            "--bus-detection-radius",
            "--bus-attraction-radius",
            "--bus-attraction-bonus",
            "--bus-min-nets",
            "--protect",
            "--nets",
        ] {
            assert!(USAGE_MSG.contains(flag), "missing CLI flag {flag}");
        }
    }

    #[test]
    fn explicit_grid_selects_only_the_search_pitch() {
        let args = |pitch: &str| {
            vec![
                "source.kicad_pcb".into(),
                "out.kicad_pcb".into(),
                "--grid-step".into(),
                OsString::from(pitch),
            ]
        };
        for invalid in ["NaN", "0", "-1", "0.0000009", "101"] {
            assert!(parse_cli_args(&args(invalid)).is_err());
        }
        let config = parse_cli_args(&args("0.025")).unwrap();
        let fixture = include_bytes!("../tests/fixtures/multiterminal-native-two-nets.json");
        let mut input: Input = serde_json::from_slice(fixture).unwrap();
        let original = crate::Engine::new(input.clone()).unwrap();
        input.routing_options = Some(config.routing_options);
        let selected = crate::Engine::new(input.clone()).unwrap();
        assert_eq!(selected.input().rules.grid_nm, 25_000);
        assert_ne!(
            original.input().rules.grid_nm,
            selected.input().rules.grid_nm
        );
        assert_eq!(
            serde_json::to_value(original.effective_rules()).unwrap(),
            serde_json::to_value(selected.effective_rules()).unwrap()
        );
        input.rules.grid_nm = 0;
        assert!(crate::Engine::new(input).is_err());
    }

    #[test]
    fn bus_cli_checks_units_ranges_and_opt_in_before_routing() {
        let parse = |flags: &[&str]| {
            let mut args = vec![
                OsString::from("src.kicad_pcb"),
                OsString::from("out.kicad_pcb"),
            ];
            args.extend(flags.iter().map(OsString::from));
            parse_cli_args(&args).map(|cfg| cfg.routing_options.bus)
        };
        assert_eq!(parse(&[]).unwrap(), None);
        assert_eq!(
            parse(&["--bus"]).unwrap(),
            Some(crate::model::BusOptions::default())
        );
        let config = parse(&[
            "--bus-detection-radius",
            "0.25",
            "--bus",
            "--bus-attraction-radius=0.000001",
            "--bus-attraction-bonus",
            "0",
            "--bus-min-nets=3",
        ])
        .unwrap()
        .unwrap();
        assert_eq!(
            (
                config.detection_radius_nm,
                config.attraction_radius_nm,
                config.attraction_bonus,
                config.min_nets
            ),
            (250_000, 1, 0, 3)
        );
        for flags in [
            vec!["--bus-min-nets=2"],
            vec!["--bus", "--bus"],
            vec!["--bus=1"],
            vec!["--bus", "--bus-attraction-radius=NaN"],
            vec!["--bus", "--bus-detection-radius=0.0000009"],
            vec!["--bus", "--bus-detection-radius=9223372036854.775808"],
            vec!["--bus", "--bus-min-nets=1"],
            vec!["--bus", "--bus-attraction-bonus=-1"],
            vec![
                "--bus",
                "--bus-attraction-radius=1",
                "--bus-attraction-radius=2",
            ],
        ] {
            assert!(parse(&flags).is_err(), "accepted {flags:?}");
        }
        let fixture = include_bytes!("../tests/fixtures/multiterminal-native-two-nets.json");
        let mut input: Input = serde_json::from_slice(fixture).unwrap();
        input.routing_options = Some(crate::model::RoutingOptions {
            bus: Some(crate::model::BusOptions {
                detection_radius_nm: 0,
                ..Default::default()
            }),
            ..Default::default()
        });
        assert_eq!(input.validate().unwrap_err().status, Status::Invalid);
    }

    #[test]
    fn cli_parses_ac_couple_chain_flags_and_rejects_malformed() {
        let args: Vec<OsString> = vec![
            "src.kicad_pcb",
            "out.kicad_pcb",
            "--diff-pairs",
            "auto",
            "--ac-couple-chain-json",
            "[\"C1\",\"C2\"]",
            "--ac-couple-chain-json",
            "[\"C3\",\"C4\"]",
            "--ac-match-tolerance",
            "0.1",
            "--ac-meander-amplitude",
            "3",
        ]
        .into_iter()
        .map(OsString::from)
        .collect();
        let cfg = parse_cli_args(&args).expect("AC chain flags should parse");
        assert_eq!(
            cfg.routing_options.ac_couple_chains,
            vec![
                vec!["C1".to_owned(), "C2".to_owned()],
                vec!["C3".to_owned(), "C4".to_owned()]
            ]
        );
        assert_eq!(cfg.routing_options.ac_match_tolerance_nm, Some(100_000));
        assert_eq!(cfg.routing_options.ac_meander_amplitude_nm, Some(3_000_000));

        for bad in [
            vec![
                "src.kicad_pcb",
                "out.kicad_pcb",
                "--ac-couple-chain-json",
                "not json",
            ],
            vec![
                "src.kicad_pcb",
                "out.kicad_pcb",
                "--ac-couple-chain-json",
                "{}",
            ],
            vec![
                "src.kicad_pcb",
                "out.kicad_pcb",
                "--ac-match-tolerance",
                "0",
            ],
            vec![
                "src.kicad_pcb",
                "out.kicad_pcb",
                "--ac-meander-amplitude",
                "-1",
            ],
        ] {
            let os: Vec<OsString> = bad.into_iter().map(OsString::from).collect();
            assert!(parse_cli_args(&os).is_err());
        }
    }
}
