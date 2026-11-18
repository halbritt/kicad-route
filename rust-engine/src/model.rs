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

fn is_zero_i64(v: &i64) -> bool {
    *v == 0
}

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
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub holes: Vec<Hole>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub board_outline: Option<BoardOutline>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub graphics: Vec<CopperGraphic>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub mask_apertures: Vec<MaskAperture>,
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
    #[serde(default, skip_serializing_if = "is_zero_i64")]
    pub hole_clearance_nm: i64,
    #[serde(default, skip_serializing_if = "is_zero_i64")]
    pub same_net_hole_clearance_nm: i64,
    #[serde(default, skip_serializing_if = "is_zero_i64")]
    pub solder_mask_expansion_nm: i64,
    #[serde(default, skip_serializing_if = "is_zero_i64")]
    pub solder_mask_min_width_nm: i64,
    #[serde(default, skip_serializing_if = "is_zero_i64")]
    pub solder_mask_to_copper_clearance_nm: i64,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Net {
    pub id: u32,
    pub name: String,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum DrillShape {
    Circle {
        diameter_nm: i64,
    },
    Capsule {
        start_nm: Point,
        end_nm: Point,
        width_nm: i64,
    },
}
impl DrillShape {
    pub fn bounds(&self, at: Point) -> (Point, Point) {
        match *self {
            DrillShape::Circle { diameter_nm } => {
                let r = (diameter_nm + 1) / 2;
                ([at[0] - r, at[1] - r], [at[0] + r, at[1] + r])
            }
            DrillShape::Capsule {
                start_nm,
                end_nm,
                width_nm,
            } => {
                let r = (width_nm + 1) / 2;
                (
                    [
                        start_nm[0].min(end_nm[0]) - r,
                        start_nm[1].min(end_nm[1]) - r,
                    ],
                    [
                        start_nm[0].max(end_nm[0]) + r,
                        start_nm[1].max(end_nm[1]) + r,
                    ],
                )
            }
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Hole {
    pub id: String,
    pub net: u32,
    pub at: Point,
    pub shape: DrillShape,
}
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OutlinePolygon {
    pub outer: Vec<Point>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub holes: Vec<Vec<Point>>,
}
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BoardOutline {
    pub polygons: Vec<OutlinePolygon>,
}
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum GraphicShape {
    Segment {
        start: Point,
        end: Point,
        width_nm: i64,
    },
    Arc {
        start: Point,
        mid: Point,
        end: Point,
        width_nm: i64,
    },
    Circle {
        center: Point,
        radius_nm: i64,
        width_nm: i64,
        #[serde(default)]
        filled: bool,
    },
    Polygon {
        outer: Vec<Point>,
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        holes: Vec<Vec<Point>>,
        width_nm: i64,
        #[serde(default)]
        filled: bool,
    },
}
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CopperGraphic {
    pub id: String,
    pub net: u32,
    pub layer: String,
    pub shape: GraphicShape,
}
impl CopperGraphic {
    pub fn copper_bounds(&self) -> (Point, Point) {
        match &self.shape {
            GraphicShape::Segment {
                start,
                end,
                width_nm,
            } => {
                let r = (width_nm + 1) / 2;
                (
                    [start[0].min(end[0]) - r, start[1].min(end[1]) - r],
                    [start[0].max(end[0]) + r, start[1].max(end[1]) + r],
                )
            }
            GraphicShape::Arc {
                start,
                mid,
                end,
                width_nm,
            } => {
                let (chords, sag) = crate::geometry::arc_to_conservative_chords(*start, *mid, *end);
                let r = (width_nm + 1) / 2 + sag;
                let mut lo = [i64::MAX, i64::MAX];
                let mut hi = [i64::MIN, i64::MIN];
                for p in chords {
                    for k in 0..2 {
                        lo[k] = lo[k].min(p[k]);
                        hi[k] = hi[k].max(p[k]);
                    }
                }
                ([lo[0] - r, lo[1] - r], [hi[0] + r, hi[1] + r])
            }
            GraphicShape::Circle {
                center,
                radius_nm,
                width_nm,
                ..
            } => {
                let r = radius_nm + (width_nm + 1) / 2;
                (
                    [center[0] - r, center[1] - r],
                    [center[0] + r, center[1] + r],
                )
            }
            GraphicShape::Polygon {
                outer,
                holes,
                width_nm,
                ..
            } => {
                let r = (width_nm + 1) / 2;
                let mut lo = [i64::MAX, i64::MAX];
                let mut hi = [i64::MIN, i64::MIN];
                for p in outer.iter().chain(holes.iter().flatten()) {
                    for k in 0..2 {
                        lo[k] = lo[k].min(p[k]);
                        hi[k] = hi[k].max(p[k]);
                    }
                }
                ([lo[0] - r, lo[1] - r], [hi[0] + r, hi[1] + r])
            }
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MaskAperture {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub net: Option<u32>,
    pub layer: String,
    pub shape: GraphicShape,
    #[serde(default, skip_serializing_if = "is_zero_i64")]
    pub expansion_nm: i64,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Pad {
    pub id: String,
    pub net: u32,
    pub at: Point,
    pub shape: Shape,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub drill: Option<DrillShape>,
}
impl Pad {
    pub fn copper_center(&self) -> Point {
        match &self.shape {
            Shape::Circle { .. } | Shape::Rect { .. } | Shape::RoundRect { .. } => self.at,
            Shape::BoxExact { center_2x, .. } => {
                [center_2x[0].div_euclid(2), center_2x[1].div_euclid(2)]
            }
            Shape::Capsule {
                start_nm, end_nm, ..
            } => [
                ((i128::from(start_nm[0]) + i128::from(end_nm[0])) / 2) as i64,
                ((i128::from(start_nm[1]) + i128::from(end_nm[1])) / 2) as i64,
            ],
            Shape::ConvexPolygon { vertices, .. } => {
                let n = i128::from(vertices.len() as i64);
                let sx: i128 = vertices.iter().map(|v| i128::from(v[0])).sum();
                let sy: i128 = vertices.iter().map(|v| i128::from(v[1])).sum();
                [(sx / n) as i64, (sy / n) as i64]
            }
        }
    }

    /// Outward copper envelope for an admitted pad. This is candidate-selection
    /// geometry only; exact clearance and electrical contact remain separate.
    pub fn copper_bounds(&self) -> (Point, Point) {
        match &self.shape {
            Shape::Circle { radius_nm } => {
                let half = [*radius_nm; 2];
                (
                    std::array::from_fn(|k| self.at[k] - half[k]),
                    std::array::from_fn(|k| self.at[k] + half[k]),
                )
            }
            Shape::Rect { half_size_nm } | Shape::RoundRect { half_size_nm, .. } => (
                std::array::from_fn(|k| self.at[k] - half_size_nm[k]),
                std::array::from_fn(|k| self.at[k] + half_size_nm[k]),
            ),
            Shape::BoxExact {
                center_2x,
                half_size_2x,
                ..
            } => (
                std::array::from_fn(|k| (center_2x[k] - half_size_2x[k]).div_euclid(2)),
                std::array::from_fn(|k| (center_2x[k] + half_size_2x[k] + 1).div_euclid(2)),
            ),
            Shape::Capsule {
                start_nm,
                end_nm,
                width_nm,
            } => {
                let r = (width_nm + 1) / 2;
                (
                    [
                        start_nm[0].min(end_nm[0]) - r,
                        start_nm[1].min(end_nm[1]) - r,
                    ],
                    [
                        start_nm[0].max(end_nm[0]) + r,
                        start_nm[1].max(end_nm[1]) + r,
                    ],
                )
            }
            Shape::ConvexPolygon {
                vertices,
                radius_nm,
            } => {
                let mut lo = [i64::MAX, i64::MAX];
                let mut hi = [i64::MIN, i64::MIN];
                for v in vertices {
                    for k in 0..2 {
                        lo[k] = lo[k].min(v[k]);
                        hi[k] = hi[k].max(v[k]);
                    }
                }
                (
                    [lo[0] - radius_nm, lo[1] - radius_nm],
                    [hi[0] + radius_nm, hi[1] + radius_nm],
                )
            }
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Shape {
    Circle {
        radius_nm: i64,
    },
    Rect {
        half_size_nm: Point,
    },
    /// A closed axis-aligned box inset by radius_nm, swept by that radius.
    /// Radius zero is a rectangle; radius=min(half_size) is a capsule.
    RoundRect {
        half_size_nm: Point,
        radius_nm: i64,
    },
    /// Exact 2x-nanometre axis-aligned shape for odd-nm dimensions and/or offset copper.
    BoxExact {
        center_2x: Point,
        half_size_2x: Point,
        radius_2x: i64,
    },
    /// Exact swept segment in board coordinates (rotated ovals, odd ovals).
    Capsule {
        start_nm: Point,
        end_nm: Point,
        width_nm: i64,
    },
    /// Strictly CCW convex polygon swept by `radius_nm`.
    ConvexPolygon {
        vertices: Vec<Point>,
        #[serde(default, skip_serializing_if = "is_zero_i64")]
        radius_nm: i64,
    },
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
            .saturating_add(self.holes.len())
            .saturating_add(self.graphics.len())
            .saturating_add(self.mask_apertures.len())
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
        let bounded_pt = |p: Point| bounded(p[0]) && bounded(p[1]);
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
            || ![
                r.clearance_nm,
                r.edge_clearance_nm,
                r.hole_clearance_nm,
                r.same_net_hole_clearance_nm,
                r.solder_mask_expansion_nm,
                r.solder_mask_min_width_nm,
                r.solder_mask_to_copper_clearance_nm,
            ]
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
        let validate_drill = |drill: &DrillShape| -> Result<(), Failure> {
            match *drill {
                DrillShape::Circle { diameter_nm } => {
                    if !(1..=2 * MAX_NM).contains(&diameter_nm) {
                        return Err(invalid("drill diameter must be 1..=200000000 nm"));
                    }
                }
                DrillShape::Capsule {
                    start_nm,
                    end_nm,
                    width_nm,
                } => {
                    if !bounded_pt(start_nm)
                        || !bounded_pt(end_nm)
                        || !(1..=2 * MAX_NM).contains(&width_nm)
                    {
                        return Err(invalid("drill capsule coordinates or width are invalid"));
                    }
                }
            }
            Ok(())
        };
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
            match &p.shape {
                Shape::Circle { radius_nm } => {
                    if !(1..=MAX_NM).contains(radius_nm) {
                        return Err(invalid("pad sizes must be positive and <=100000000 nm"));
                    }
                }
                Shape::Rect { half_size_nm } => {
                    if !half_size_nm.iter().all(|v| (1..=MAX_NM).contains(v)) {
                        return Err(invalid("pad sizes must be positive and <=100000000 nm"));
                    }
                }
                Shape::RoundRect {
                    half_size_nm,
                    radius_nm,
                } => {
                    if !half_size_nm.iter().all(|v| (1..=MAX_NM).contains(v)) {
                        return Err(invalid("pad sizes must be positive and <=100000000 nm"));
                    }
                    if *radius_nm < 0 || *radius_nm > half_size_nm[0].min(half_size_nm[1]) {
                        return Err(invalid(
                            "rounded pad radius must be nonnegative and at most either half size",
                        ));
                    }
                }
                Shape::BoxExact {
                    center_2x,
                    half_size_2x,
                    radius_2x,
                } => {
                    if !center_2x
                        .iter()
                        .all(|v| (-2 * MAX_NM..=2 * MAX_NM).contains(v))
                        || !half_size_2x.iter().all(|v| (1..=2 * MAX_NM).contains(v))
                    {
                        return Err(invalid(
                            "exact box center/size must be positive and within bounds",
                        ));
                    }
                    if *radius_2x < 0 || *radius_2x > half_size_2x[0].min(half_size_2x[1]) {
                        return Err(invalid(
                            "rounded pad radius must be nonnegative and at most either half size",
                        ));
                    }
                }
                Shape::Capsule {
                    start_nm,
                    end_nm,
                    width_nm,
                } => {
                    if !bounded_pt(*start_nm)
                        || !bounded_pt(*end_nm)
                        || !(1..=2 * MAX_NM).contains(width_nm)
                    {
                        return Err(invalid("capsule pad coordinates or width are invalid"));
                    }
                }
                Shape::ConvexPolygon {
                    vertices,
                    radius_nm,
                } => {
                    if !(3..=64).contains(&vertices.len())
                        || !vertices.iter().all(|v| bounded_pt(*v))
                        || !(0..=MAX_NM).contains(radius_nm)
                    {
                        return Err(invalid("convex polygon pad vertices or radius are invalid"));
                    }
                    if !crate::geometry::is_ccw_convex_polygon(vertices) {
                        return Err(invalid(
                            "convex polygon pad must be strictly CCW convex with nonzero area",
                        ));
                    }
                }
            }
            if let Some(drill) = &p.drill {
                validate_drill(drill)?;
            }
        }
        for h in &self.holes {
            if !canonical_uuid(&h.id)
                || !ids.insert(&h.id)
                || (h.net != 0 && !nets.contains(&h.net))
                || !inside(h.at)
            {
                return Err(invalid(
                    "hole identity, net membership or position is invalid",
                ));
            }
            validate_drill(&h.shape)?;
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
        if let Some(outline) = &self.board_outline {
            if outline.polygons.is_empty() {
                return Err(invalid("board_outline must contain at least one polygon"));
            }
            let mut total_verts: usize = 0;
            let mut all_rings: Vec<&[Point]> = Vec::new();
            for poly in &outline.polygons {
                total_verts = total_verts.saturating_add(poly.outer.len());
                if poly.outer.len() < 3
                    || !poly.outer.iter().all(|p| inside(*p))
                    || !crate::geometry::is_valid_simple_ring(&poly.outer)
                {
                    return Err(invalid(
                        "board_outline outer ring must be simple, bounded and nonzero area",
                    ));
                }
                all_rings.push(&poly.outer);
                for hole in &poly.holes {
                    total_verts = total_verts.saturating_add(hole.len());
                    if hole.len() < 3
                        || !hole.iter().all(|p| inside(*p))
                        || !crate::geometry::is_valid_simple_ring(hole)
                    {
                        return Err(invalid(
                            "board_outline hole ring must be simple, bounded and nonzero area",
                        ));
                    }
                    all_rings.push(hole);
                }
            }
            if total_verts > 65_536 {
                return Err(Failure::new(
                    Status::AdmissionExceeded,
                    "board_outline vertex limit exceeded",
                ));
            }
            for i in 0..all_rings.len() {
                for j in (i + 1)..all_rings.len() {
                    if crate::geometry::rings_cross(all_rings[i], all_rings[j]) {
                        return Err(invalid("board_outline rings must not cross"));
                    }
                }
            }
            for poly in &outline.polygons {
                for hole in &poly.holes {
                    if !crate::geometry::point_in_simple_polygon(hole[0], &poly.outer) {
                        return Err(invalid("board_outline hole must lie inside outer ring"));
                    }
                }
            }
        }
        let validate_graphic_shape = |shape: &GraphicShape| -> Result<(), Failure> {
            match shape {
                GraphicShape::Segment {
                    start,
                    end,
                    width_nm,
                } => {
                    if !bounded_pt(*start)
                        || !bounded_pt(*end)
                        || start == end
                        || !(0..=2 * MAX_NM).contains(width_nm)
                    {
                        return Err(invalid("graphic segment endpoints or width are invalid"));
                    }
                }
                GraphicShape::Arc {
                    start,
                    mid,
                    end,
                    width_nm,
                } => {
                    if !bounded_pt(*start)
                        || !bounded_pt(*mid)
                        || !bounded_pt(*end)
                        || !crate::geometry::is_valid_arc(*start, *mid, *end)
                        || !(0..=2 * MAX_NM).contains(width_nm)
                    {
                        return Err(invalid("graphic arc points or width are invalid"));
                    }
                    if crate::geometry::is_ultra_large_major_arc(*start, *mid, *end) {
                        return Err(unsupported("graphic arc exceeds supported extent"));
                    }
                    let (chords, sag) =
                        crate::geometry::arc_to_conservative_chords(*start, *mid, *end);
                    let fits_2x = |v: i64| (-2 * MAX_NM..=2 * MAX_NM).contains(&v);
                    if !(0..=MAX_NM).contains(&sag)
                        || width_nm.checked_add(2 * sag).is_none_or(|w| {
                            !(0..=4 * MAX_NM).contains(&w) || i32::try_from(w).is_err()
                        })
                        || chords.iter().any(|p| {
                            !fits_2x(p[0])
                                || !fits_2x(p[1])
                                || i32::try_from(p[0]).is_err()
                                || i32::try_from(p[1]).is_err()
                        })
                    {
                        return Err(unsupported("graphic arc exceeds supported extent"));
                    }
                }
                GraphicShape::Circle {
                    center,
                    radius_nm,
                    width_nm,
                    ..
                } => {
                    if !bounded_pt(*center)
                        || !(1..=MAX_NM).contains(radius_nm)
                        || !(0..=2 * MAX_NM).contains(width_nm)
                    {
                        return Err(invalid("graphic circle parameters are invalid"));
                    }
                }
                GraphicShape::Polygon {
                    outer,
                    holes,
                    width_nm,
                    ..
                } => {
                    if outer.len() < 3
                        || !outer.iter().all(|p| bounded_pt(*p))
                        || !crate::geometry::is_valid_simple_ring(outer)
                        || !(0..=2 * MAX_NM).contains(width_nm)
                    {
                        return Err(invalid("graphic polygon outer ring or width is invalid"));
                    }
                    let mut rings: Vec<&[Point]> = vec![outer];
                    for hole in holes {
                        if hole.len() < 3
                            || !hole.iter().all(|p| bounded_pt(*p))
                            || !crate::geometry::is_valid_simple_ring(hole)
                        {
                            return Err(invalid("graphic polygon hole ring is invalid"));
                        }
                        rings.push(hole);
                    }
                    let total_verts: usize = rings.iter().map(|r| r.len()).sum();
                    if total_verts > 65_536 {
                        return Err(Failure::new(
                            Status::AdmissionExceeded,
                            "graphic polygon vertex limit exceeded",
                        ));
                    }
                    for i in 0..rings.len() {
                        for j in (i + 1)..rings.len() {
                            if crate::geometry::rings_cross(rings[i], rings[j]) {
                                return Err(invalid("graphic polygon rings must not cross"));
                            }
                        }
                    }
                    for hole in holes {
                        if !crate::geometry::point_in_simple_polygon(hole[0], outer) {
                            return Err(invalid("graphic polygon hole must lie inside outer ring"));
                        }
                    }
                }
            }
            Ok(())
        };
        for g in &self.graphics {
            if !canonical_uuid(&g.id)
                || !ids.insert(&g.id)
                || (g.net != 0 && !nets.contains(&g.net))
            {
                return Err(invalid("graphic identity or net membership is invalid"));
            }
            if g.layer != "F.Cu" && g.layer != "B.Cu" {
                return Err(unsupported("copper graphics only support F.Cu and B.Cu"));
            }
            validate_graphic_shape(&g.shape)?;
        }
        for m in &self.mask_apertures {
            if !canonical_uuid(&m.id)
                || !ids.insert(&m.id)
                || m.net.is_some_and(|n| n != 0 && !nets.contains(&n))
                || !(0..=MAX_NM).contains(&m.expansion_nm)
            {
                return Err(invalid(
                    "mask aperture identity, net membership or expansion is invalid",
                ));
            }
            if m.layer != "F.Mask" && m.layer != "B.Mask" {
                return Err(unsupported("mask apertures only support F.Mask and B.Mask"));
            }
            validate_graphic_shape(&m.shape)?;
        }
        Ok(())
    }
}
