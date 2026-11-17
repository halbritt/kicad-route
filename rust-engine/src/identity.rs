//! Request consistency, not authentication or proof of the producing process.
use crate::model::Input;
use sha2::{Digest, Sha256};
use std::io::{self, Write};

pub const MAX_REQUEST_BYTES: usize = 16 * 1024 * 1024;

/// SHA-256 of exact request bytes, without normalization or a domain prefix.
/// Oversized input has no identity: the CLI may have read only a bounded prefix.
pub fn request_sha256(bytes: &[u8]) -> Option<String> {
    (bytes.len() <= MAX_REQUEST_BYTES).then(|| hex(&Sha256::digest(bytes)))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

struct HashWriter(Sha256);
impl Write for HashWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0.update(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// SHA-256 of compact serde_json serialization of the entire typed Input.
/// All defaults are explicit; field order follows the Serialize implementation.
/// This streams without a second document allocation and has no wire-size cap.
/// For wire requests, retain original bytes and use request_sha256 instead.
pub fn input_request_sha256(input: &Input) -> String {
    let mut writer = HashWriter(Sha256::new());
    // Input contains only concrete strings, integers, enums, structs and arrays;
    // serialization has no custom implementation, non-string map keys or I/O.
    serde_json::to_writer(&mut writer, input)
        .expect("Input serialization to an infallible hash writer");
    hex(&writer.0.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn standard_sha256_known_answers() {
        assert_eq!(
            request_sha256(b"").unwrap(),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            request_sha256(b"abc").unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }
}
