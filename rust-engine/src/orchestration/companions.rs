//! Keep a published native PCB with the project and custom rules used to grade it.
use super::{
    check_cancel_or_deadline, parent_dir_or_dot, read_bounded_regular, write_file_create_new,
    OperationStatus, MAX_REQUEST_BYTES,
};
#[cfg(target_os = "linux")]
use std::os::fd::AsRawFd;
#[cfg(unix)]
use std::os::unix::fs::MetadataExt;
use std::{
    ffi::OsString,
    fs::{self, File},
    io::{self, ErrorKind},
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};

#[derive(Clone, Copy, Default)]
pub(super) struct Present {
    pub project: bool,
    pub rules: bool,
}

pub(super) fn inspect(
    output: &Path,
    project: Option<&[u8]>,
    rules: Option<&[u8]>,
    required: Present,
) -> Result<Present, String> {
    Ok(Present {
        project: inspect_file(
            &output.with_extension("kicad_pro"),
            project,
            required.project,
        )?,
        rules: inspect_file(&output.with_extension("kicad_dru"), rules, required.rules)?,
    })
}

fn inspect_file(path: &Path, expected: Option<&[u8]>, required: bool) -> Result<bool, String> {
    match fs::symlink_metadata(path) {
        Err(e) if e.kind() == ErrorKind::NotFound && !required => return Ok(false),
        Err(e) => {
            return Err(format!(
                "cannot inspect destination companion {}: {e}",
                path.display()
            ))
        }
        Ok(_) => (),
    }
    let expected = expected.ok_or_else(|| {
        format!(
            "destination companion {} has no corresponding source file",
            path.display()
        )
    })?;
    let actual = read_bounded_regular(path, MAX_REQUEST_BYTES, "destination companion")
        .map_err(|e| e.into_status().1)?;
    if actual != expected {
        return Err(format!(
            "destination companion {} differs from the admitted source file",
            path.display()
        ));
    }
    Ok(true)
}

pub(super) struct Publication {
    pub name: OsString,
    pub directory: File,
}

impl Publication {
    pub fn lock(output: &Path, deadline: Instant) -> Result<Self, (OperationStatus, String)> {
        let parent = parent_dir_or_dot(output);
        let map_err = |e: io::Error| {
            (
                OperationStatus::PublicationRefused,
                format!(
                    "cannot lock board publication directory {}: {e}",
                    parent.display()
                ),
            )
        };
        let dir = File::open(parent).map_err(map_err)?;
        loop {
            match dir.try_lock() {
                Ok(()) => break,
                Err(fs::TryLockError::WouldBlock) => {
                    check_cancel_or_deadline(deadline)?;
                    thread::sleep(Duration::from_millis(5));
                }
                Err(fs::TryLockError::Error(e)) => return Err(map_err(e)),
            }
        }
        let name = output.file_name().ok_or_else(|| {
            (
                OperationStatus::UsageRefused,
                "output needs a filename".to_owned(),
            )
        })?;
        Ok(Self {
            name: name.to_owned(),
            directory: dir,
        })
    }

    pub fn rollback_unlinked_board(&self, stage_dir: &Path) -> io::Result<()> {
        #[cfg(target_os = "linux")]
        let parent = PathBuf::from(format!("/proc/self/fd/{}", self.directory.as_raw_fd()));
        #[cfg(not(target_os = "linux"))]
        let parent = parent_dir_or_dot(stage_dir).to_owned();
        let staged = stage_dir.join(&self.name);
        let dest = parent.join(&self.name);
        #[cfg(unix)]
        {
            if same_regular_file(&staged, &dest)? {
                return Ok(()); // Linked board commits the entire set.
            }
            for ext in ["kicad_pro", "kicad_dru"] {
                let path = dest.with_extension(ext);
                if same_regular_file(&staged.with_extension(ext), &path)? {
                    fs::remove_file(path)?;
                }
            }
        }
        Ok(())
    }
}

// Each newly installed file has a private hard link until the board commit
// is resolved. Identical incumbents never acquire such a link.
pub(super) fn install(
    staged_board: &Path,
    output: &Path,
    project: Option<&[u8]>,
    rules: Option<&[u8]>,
    existing: Present,
) -> io::Result<()> {
    for (ext, contents, present) in [
        ("kicad_pro", project, existing.project),
        ("kicad_dru", rules, existing.rules),
    ] {
        if let Some(bytes) = contents.filter(|_| !present) {
            let staged = staged_board.with_extension(ext);
            write_file_create_new(&staged, bytes, 0o644)?;
            let target = output.with_extension(ext);
            match fs::hard_link(&staged, &target) {
                Ok(()) => (),
                // A concurrent external copy may be identical. The caller
                // checks both companions again before it links the board.
                Err(e) if e.kind() == ErrorKind::AlreadyExists => (),
                Err(e) => {
                    return Err(io::Error::new(
                        e.kind(),
                        format!("cannot publish companion {}: {e}", target.display()),
                    ))
                }
            }
        }
    }
    Ok(())
}

#[cfg(unix)]
fn same_regular_file(a: &Path, b: &Path) -> io::Result<bool> {
    fn regular(path: &Path) -> io::Result<Option<fs::Metadata>> {
        match fs::symlink_metadata(path) {
            Ok(m) => Ok(m.is_file().then_some(m)),
            Err(e) if e.kind() == ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e),
        }
    }
    let Some(a) = regular(a)? else {
        return Ok(false);
    };
    Ok(regular(b)?.is_some_and(|b| (a.dev(), a.ino()) == (b.dev(), b.ino())))
}
