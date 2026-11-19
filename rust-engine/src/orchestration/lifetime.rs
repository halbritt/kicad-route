//! Linux ownership of a standalone route-native operation's children and staging.
//! A pipe's final writer disappears even if its process is killed without unwinding.
use super::{unix_sys, OwnedTempDir};
use std::os::unix::process::CommandExt;
use std::{
    ffi::{c_int, OsString},
    fs,
    io::{self, BufRead, Read, Write},
    os::fd::AsRawFd,
    os::unix::{fs::MetadataExt, net::UnixStream},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::OnceLock,
};

static EXECUTABLE: OnceLock<PathBuf> = OnceLock::new();
const DIRECTORY_HELPER: &str = "--internal-native-stage-owner";
pub(super) fn enabled() -> bool {
    EXECUTABLE.get().is_some()
}
pub(super) fn initialize(args: &[OsString]) -> Option<u8> {
    if args.get(1).is_some_and(|s| s == DIRECTORY_HELPER) {
        return Some(match run_directory_helper(&args[2..]) {
            Ok(()) => 0,
            Err(e) => {
                eprintln!("native staging owner: {e}");
                1
            }
        });
    }
    if args.get(1).is_some_and(|s| s == "route-native") {
        match std::env::current_exe() {
            Ok(path) => {
                let _ = EXECUTABLE.set(path);
            }
            Err(e) => {
                eprintln!("locating native staging owner: {e}");
                return Some(8);
            }
        }
    }
    None
}

pub(super) fn create_directory(parent: &Path, prefix: &str, id: &str) -> io::Result<OwnedTempDir> {
    let mut child = Command::new(EXECUTABLE.get().expect("enabled for standalone native CLI"))
        .process_group(0)
        .arg(DIRECTORY_HELPER)
        .arg(parent)
        .arg(prefix)
        .arg(id)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()?;
    let prepared = (|| {
        let stdout = child.stdout.take().expect("piped staging response");
        let mut line = String::new();
        io::BufReader::new(stdout).take(512).read_line(&mut line)?;
        let mut fields = line.trim_end().split(' ');
        let name = fields.next().unwrap_or("");
        let identity = fields.next().and_then(|dev| {
            Some((
                dev.parse::<u64>().ok()?,
                fields.next()?.parse::<u64>().ok()?,
            ))
        });
        let base = format!("{prefix}{id}");
        if name != base
            && !name
                .strip_prefix(&base)
                .and_then(|s| s.strip_prefix('-'))
                .is_some_and(|s| s.parse::<u32>().is_ok_and(|n| (1..64).contains(&n)))
        {
            return Err(io::Error::other(
                "native staging owner failed to create directory",
            ));
        }
        let path = parent.join(name);
        let inode = fs::File::open(&path)?;
        let metadata = inode.metadata()?;
        if identity != Some((metadata.dev(), metadata.ino())) {
            return Err(io::Error::other(
                "native staging directory changed before use",
            ));
        }
        Ok((path, inode))
    })();
    match prepared {
        Ok((path, inode)) => Ok(OwnedTempDir {
            path: Some(path),
            inode: Some(inode),
            keeper: Some(child),
        }),
        Err(e) => {
            child.stdin.take();
            let _ = child.wait();
            Err(e)
        }
    }
}

fn run_directory_helper(args: &[OsString]) -> io::Result<()> {
    let [parent, prefix, id] = args else {
        return Err(io::Error::other("invalid staging owner arguments"));
    };
    let prefix = prefix.to_str().unwrap_or("");
    let id = id.to_str().unwrap_or("");
    if !matches!(prefix, "kicad-route-op-" | ".kicad-route-pub-")
        || id.is_empty()
        || id.len() > 160
        || !id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
    {
        return Err(io::Error::other("invalid native staging name"));
    }
    let mut staging = OwnedTempDir::create_local(Path::new(parent), prefix, id)?;
    let basename = staging
        .path()
        .and_then(|p| p.file_name())
        .and_then(|s| s.to_str())
        .unwrap();
    // The private directory already belongs to this helper before the caller can use it.
    let metadata = staging
        .inode
        .as_ref()
        .expect("new Linux staging inode")
        .metadata()?;
    writeln!(
        io::stdout().lock(),
        "{basename} {} {}",
        metadata.dev(),
        metadata.ino()
    )?;
    io::stdout().flush()?;
    let mut byte = [0; 1];
    loop {
        match io::stdin().read(&mut byte) {
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            _ => break,
        }
    }
    staging.cleanup().map_err(io::Error::other)
}

fn still_owned(path: &Path, dir: &fs::File) -> io::Result<bool> {
    let id = dir.metadata()?;
    match fs::symlink_metadata(path) {
        Ok(actual) => {
            Ok(actual.file_type().is_dir() && (actual.dev(), actual.ino()) == (id.dev(), id.ino()))
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(e),
    }
}

pub(super) fn remove_owned(path: &Path, dir: &fs::File) -> io::Result<()> {
    if !still_owned(path, dir)? {
        return Ok(());
    }
    // Keep recursive deletion anchored to the open directory even across renames.
    // Only the last, non-recursive rmdir uses the public pathname.
    let anchor = PathBuf::from(format!("/proc/self/fd/{}", dir.as_raw_fd()));
    for entry in fs::read_dir(&anchor)? {
        let entry = entry?;
        let result = if entry.file_type()?.is_dir() {
            fs::remove_dir_all(entry.path())
        } else {
            fs::remove_file(entry.path())
        };
        match result {
            Err(e) if e.kind() == io::ErrorKind::NotFound => (),
            otherwise => otherwise?,
        }
    }
    if still_owned(path, dir)? {
        match fs::remove_dir(path) {
            Err(e) if e.kind() == io::ErrorKind::NotFound => (),
            otherwise => otherwise?,
        }
    }
    Ok(())
}

// A syscall works with older libc releases. On older kernels fall back to close.
unsafe fn try_close_range(first: u32, last: u32) -> bool {
    libc::syscall(libc::SYS_close_range, first, last, 0u32) == 0
}

pub(super) struct NativeGroup {
    pub(super) id: c_int,
    writer: Option<UnixStream>,
}
impl NativeGroup {
    pub(super) fn new(stage_lease: Option<&std::process::ChildStdin>) -> io::Result<Self> {
        let (reader, writer) = UnixStream::pair()?;
        let fd = reader.as_raw_fd();
        let stage_fd = stage_lease.map(AsRawFd::as_raw_fd).unwrap_or(fd);
        let low = fd.min(stage_fd) as u32;
        let high = fd.max(stage_fd) as u32;
        // Linux _SC_OPEN_MAX. On old kernels without close_range the child
        // must still drop every unrelated pipe inherited from its parent.
        let max_fd = unsafe { libc::sysconf(libc::_SC_OPEN_MAX) };
        if max_fd < 0 {
            return Err(io::Error::last_os_error());
        }
        let pid = unsafe { libc::fork() };
        if pid < 0 {
            return Err(io::Error::last_os_error());
        }
        if pid == 0 {
            // Only async-signal-safe system calls after fork. This new process
            // creates/joins its own group before ever sending a group signal.
            unsafe {
                if libc::setpgid(0, 0) != 0 {
                    libc::_exit(1);
                }
                // Keep only the parent-death socket and this invocation's stage
                // writer. EOF for staging follows the group's termination.
                let closed_low = low == 0 || try_close_range(0, low - 1);
                let closed_between = high - low <= 1 || try_close_range(low + 1, high - 1);
                let closed_high = try_close_range(high + 1, u32::MAX);
                if !(closed_low && closed_between && closed_high) {
                    for n in 0..max_fd as c_int {
                        if n != fd && n != stage_fd {
                            libc::close(n);
                        }
                    }
                }
                unix_sys::signal(unix_sys::SIGINT, unix_sys::SIG_IGN);
                unix_sys::signal(unix_sys::SIGTERM, unix_sys::SIG_IGN);
                let mut byte = 0u8;
                while libc::read(fd, (&mut byte as *mut u8).cast(), 1) < 0 {}
                unix_sys::kill(0, unix_sys::SIGKILL);
                libc::_exit(1);
            }
        }
        // Establish the group synchronously before assigning a native child.
        // Its inherited CLOEXEC writer pins the leader across fork/setpgid/exec
        // if the router is killed while spawning that child.
        let group = Self {
            id: pid,
            writer: Some(writer),
        };
        if unsafe { libc::setpgid(pid, pid) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(group)
    }
}

impl Drop for NativeGroup {
    fn drop(&mut self) {
        drop(self.writer.take());
        // Our unreaped direct child pins the numeric group ID. The monitor
        // signals only its own group on EOF, even if a native leader exited.
        let mut status = 0;
        while unsafe { libc::waitpid(self.id, &mut status, 0) } < 0
            && io::Error::last_os_error().kind() == io::ErrorKind::Interrupted
        {}
    }
}
