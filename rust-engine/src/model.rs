use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Limits {
    pub max_work: u64,
    pub max_items: usize,
    pub max_search_entries: usize,
    pub max_journal_entries: usize,
    pub max_depth: usize,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            max_work: 100_000_000,
            max_items: 100_000,
            max_search_entries: 1_000_000,
            max_journal_entries: 1_000_000,
            max_depth: 64,
        }
    }
}
impl Limits {
    pub fn validate(self) -> Result<(), Failure> {
        if self.max_work > 1_000_000_000
            || self.max_items > 1_000_000
            || self.max_search_entries > 4_000_000
            || self.max_journal_entries > 1_000_000
            || self.max_depth > 256
        {
            return Err(Failure::new(
                Status::Invalid,
                "limits exceed supported ceilings",
            ));
        }
        Ok(())
    }
}

// Input and result documents evolve independently. Never infer result shape
// from the version of the input that produced it.
pub const INPUT_VERSION: u32 = 1;
pub const RESULT_VERSION: u32 = 3;

pub type Point = [i64; 2];
// +/-100 mm coordinates (up to a 200 mm span). These bounds also keep every
// squared cross-product clearance comparison safely within signed i128.
pub const MAX_NM: i64 = 100_000_000;
pub const MAX_EXPANSIONS: u64 = 1_000_000;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Input {
    pub version: u32,
    pub source_id: String,
    pub layer: String,
    pub bounds: Bounds,
    pub rules: Rules,
    pub nets: Vec<Net>,
    pub pads: Vec<Pad>,
    pub tracks: Vec<Track>,
    pub max_expansions: u64,
    #[serde(default)]
    pub limits: Limits,
}
#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Bounds {
    pub min: Point,
    pub max: Point,
}
#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Rules {
    pub width_nm: i64,
    pub clearance_nm: i64,
    pub grid_nm: i64,
    pub edge_clearance_nm: i64,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Net {
    pub id: u32,
    pub name: String,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Pad {
    pub id: String,
    pub net: u32,
    pub at: Point,
    pub shape: Shape,
}
impl Pad {
    /// Outward copper envelope for an admitted pad. This is candidate-selection
    /// geometry only; exact clearance and electrical contact remain separate.
    pub fn copper_bounds(&self) -> (Point, Point) {
        let half = match self.shape {
            Shape::Circle { radius_nm } => [radius_nm; 2],
            Shape::Rect { half_size_nm } => half_size_nm,
        };
        (
            std::array::from_fn(|k| self.at[k] - half[k]),
            std::array::from_fn(|k| self.at[k] + half[k]),
        )
    }
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Shape {
    Circle { radius_nm: i64 },
    Rect { half_size_nm: Point },
}
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Track {
    pub id: String,
    pub net: u32,
    pub start: Point,
    pub end: Point,
    pub width_nm: i64,
    pub locked: bool,
}
impl Track {
    /// Outward copper envelope for an admitted track, including odd-nm widths
    /// and diagonal endpoint caps. It is not a final collision predicate.
    pub fn copper_bounds(&self) -> (Point, Point) {
        let radius = (self.width_nm + 1) / 2;
        (
            std::array::from_fn(|k| self.start[k].min(self.end[k]) - radius),
            std::array::from_fn(|k| self.start[k].max(self.end[k]) + radius),
        )
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Complete,
    Unsupported,
    Invalid,
    Unroutable,
    BudgetExhausted,
    WorkExhausted,
    AdmissionExceeded,
    JournalExhausted,
    NestingExhausted,
    InternalError,
}
#[derive(Debug, Serialize)]
pub struct Output {
    pub version: u32,
    pub request_sha256: Option<String>,
    pub source_id: String,
    pub status: Status,
    pub added_tracks: Vec<Track>,
    pub removed_track_ids: Vec<String>,
    pub expanded_states: u64,
    pub work_used: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Failure {
    pub status: Status,
    pub reason: String,
    pub expanded_states: u64,
    pub work_used: u64,
}
impl Failure {
    pub fn new(status: Status, reason: impl Into<String>) -> Self {
        Self {
            status,
            reason: reason.into(),
            expanded_states: 0,
            work_used: 0,
        }
    }
}
pub fn canonical_uuid(id: &str) -> bool {
    id.len() == 36
        && id.bytes().enumerate().all(|(i, b)| {
            if [8, 13, 18, 23].contains(&i) {
                b == b'-'
            } else {
                b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
            }
        })
        && id.bytes().any(|b| b != b'0' && b != b'-')
}
impl Input {
    pub fn validate(&self) -> Result<(), Failure> {
        self.limits.validate()?;
        if self
            .nets
            .len()
            .saturating_add(self.pads.len())
            .saturating_add(self.tracks.len())
            > self.limits.max_items
        {
            return Err(Failure::new(
                Status::AdmissionExceeded,
                "input item limit exceeded",
            ));
        }
        let invalid = |s: &str| Failure::new(Status::Invalid, s);
        let unsupported = |s: &str| Failure::new(Status::Unsupported, s);
        if self.version != INPUT_VERSION {
            return Err(unsupported("only input version 1 is supported"));
        }
        if self.layer != "F.Cu" {
            return Err(unsupported("only F.Cu routing is supported"));
        }
        if self.source_id.len() != 64
            || !self
                .source_id
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(invalid("source_id must be 64 lowercase SHA256 hex digits"));
        }
        let bounded = |v: i64| (-MAX_NM..=MAX_NM).contains(&v);
        if !self
            .bounds
            .min
            .iter()
            .chain(self.bounds.max.iter())
            .all(|v| bounded(*v))
            || (0..2).any(|i| self.bounds.min[i] >= self.bounds.max[i])
        {
            return Err(invalid(
                "bounds must have positive spans within +/-100000000 nm",
            ));
        }
        let r = self.rules;
        if ![r.width_nm, r.grid_nm]
            .iter()
            .all(|v| (1..=MAX_NM).contains(v))
            || ![r.clearance_nm, r.edge_clearance_nm]
                .iter()
                .all(|v| (0..=MAX_NM).contains(v))
        {
            return Err(invalid(
                "width/grid must be positive; clearances nonnegative; all <=100000000 nm",
            ));
        }
        if self.max_expansions == 0 || self.max_expansions > MAX_EXPANSIONS {
            return Err(invalid("max_expansions must be 1..=1000000"));
        }
        let mut nets = BTreeSet::new();
        let mut names = BTreeSet::new();
        for n in &self.nets {
            if n.name.len() > 1024 {
                return Err(invalid("net name exceeds 1024 UTF-8 bytes"));
            }
            if (n.id != 0 && n.name.is_empty())
                || (n.id == 0 && !n.name.is_empty())
                || !nets.insert(n.id)
                || !names.insert(&n.name)
            {
                return Err(invalid(
                    "net IDs/names must be unique; only unconnected net 0 has an empty name",
                ));
            }
        }
        let mut ids = BTreeSet::new();
        let inside =
            |p: Point| (0..2).all(|i| p[i] >= self.bounds.min[i] && p[i] <= self.bounds.max[i]);
        for p in &self.pads {
            if !canonical_uuid(&p.id)
                || !ids.insert(&p.id)
                || !nets.contains(&p.net)
                || !inside(p.at)
            {
                return Err(invalid(
                    "pad identity, net membership or position is invalid",
                ));
            }
            if (0..2).any(|i| (p.at[i] - self.bounds.min[i]) % r.grid_nm != 0) {
                return Err(unsupported(
                    "pad centers must align to grid relative to bounds.min",
                ));
            }
            let sizes = match p.shape {
                Shape::Circle { radius_nm } => [radius_nm; 2],
                Shape::Rect { half_size_nm } => half_size_nm,
            };
            if !sizes.iter().all(|v| (1..=MAX_NM).contains(v)) {
                return Err(invalid("pad sizes must be positive and <=100000000 nm"));
            }
        }
        for t in &self.tracks {
            if !canonical_uuid(&t.id)
                || !ids.insert(&t.id)
                || !nets.contains(&t.net)
                || !inside(t.start)
                || !inside(t.end)
                || t.start == t.end
                || !(1..=MAX_NM).contains(&t.width_nm)
            {
                return Err(invalid(
                    "track identity, net membership, endpoints or width is invalid",
                ));
            }
        }
        Ok(())
    }
}
