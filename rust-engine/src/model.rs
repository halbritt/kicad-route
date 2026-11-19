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
pub(crate) const MAX_PAD_PRIMITIVES: usize = 16;
// +/-100 mm coordinates (up to a 200 mm span). These bounds also keep every
// squared cross-product clearance comparison safely within signed i128.
pub const MAX_NM: i64 = 100_000_000;
pub const MAX_EXPANSIONS: u64 = 1_000_000;

fn is_zero_i64(v: &i64) -> bool {
    *v == 0
}

fn is_false(v: &bool) -> bool {
    !*v
}

fn default_true() -> bool {
    true
}

fn is_true(v: &bool) -> bool {
    *v
}

pub(crate) fn default_front_layer() -> String {
    "F.Cu".to_owned()
}

pub(crate) fn is_front_layer(layer: &str) -> bool {
    layer == "F.Cu"
}

fn deserialize_rules_or_omitted<'de, D>(deserializer: D) -> Result<Rules, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let opt = Option::<Rules>::deserialize(deserializer)?;
    Ok(opt.unwrap_or_else(Rules::omitted_sentinel))
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Input {
    pub version: u32,
    pub source_id: String,
    pub layer: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub layers: Vec<String>,
    pub bounds: Bounds,
    #[serde(
        default = "Rules::omitted_sentinel",
        deserialize_with = "deserialize_rules_or_omitted"
    )]
    pub rules: Rules,
    pub nets: Vec<Net>,
    pub pads: Vec<Pad>,
    pub tracks: Vec<Track>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub vias: Vec<Via>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub rule_areas: Vec<RuleArea>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub board_design_settings: Option<crate::rules::BoardDesignSettings>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project_rules: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub custom_rules: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub effective_rules: Option<crate::rules::EffectiveRules>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub zones: Vec<Zone>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub components: Vec<ComponentInfo>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub stackup: Vec<StackupLayer>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub routing_options: Option<RoutingOptions>,
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
impl Rules {
    pub(crate) fn omitted_sentinel() -> Self {
        Self {
            width_nm: i64::MIN,
            clearance_nm: 0,
            grid_nm: i64::MIN,
            edge_clearance_nm: 0,
            hole_clearance_nm: 0,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        }
    }
    pub(crate) fn is_omitted(&self) -> bool {
        self.width_nm == i64::MIN && self.grid_nm == i64::MIN
    }
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
impl GraphicShape {
    pub fn copper_bounds(&self) -> (Point, Point) {
        match self {
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
impl CopperGraphic {
    pub fn copper_bounds(&self) -> (Point, Point) {
        self.shape.copper_bounds()
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MaskAperture {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub net: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub group_id: Option<String>,
    pub layer: String,
    pub shape: GraphicShape,
    #[serde(default, skip_serializing_if = "is_zero_i64")]
    pub expansion_nm: i64,
}
impl MaskAperture {
    pub fn copper_bounds(&self) -> (Point, Point) {
        let (lo, hi) = self.shape.copper_bounds();
        let exp = self.expansion_nm.max(0);
        ([lo[0] - exp, lo[1] - exp], [hi[0] + exp, hi[1] + exp])
    }
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
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub layers: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub local_clearance_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "PadZoneConnection::is_inherited")]
    pub zone_connection: PadZoneConnection,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thermal_gap_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thermal_spoke_width_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thermal_spoke_angle_mdeg: Option<i64>,
    #[serde(default, skip_serializing_if = "is_zero_i64")]
    pub orientation_mdeg: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub footprint_ref: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pad_number: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fromto_id: Option<String>,
}
#[derive(Clone, Copy)]
pub(crate) struct PadGeometry<'a> {
    pub at: Point,
    pub shape: &'a Shape,
}

impl Pad {
    pub(crate) fn geometry(&self) -> PadGeometry<'_> {
        PadGeometry {
            at: self.at,
            shape: &self.shape,
        }
    }

    pub fn copper_center(&self) -> Point {
        self.geometry().copper_center()
    }

    pub fn copper_bounds(&self) -> (Point, Point) {
        self.geometry().copper_bounds()
    }
}

impl<'a> PadGeometry<'a> {
    pub(crate) fn parts(self) -> impl Iterator<Item = Self> + 'a {
        self.shape
            .parts()
            .iter()
            .map(move |shape| Self { at: self.at, shape })
    }

    pub fn copper_center(&self) -> Point {
        match self.shape {
            Shape::Union { .. }
            | Shape::Circle { .. }
            | Shape::Rect { .. }
            | Shape::RoundRect { .. } => self.at,
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
        match self.shape {
            Shape::Union { .. } => self
                .parts()
                .map(|p| p.copper_bounds())
                .reduce(|(lo, hi), (p, q)| {
                    (
                        [lo[0].min(p[0]), lo[1].min(p[1])],
                        [hi[0].max(q[0]), hi[1].max(q[1])],
                    )
                })
                .expect("admitted pad union has at least two parts"),
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
    /// Connected union of 2–16 analytic primitives owned by one SMD terminal.
    Union {
        shapes: Vec<Shape>,
    },
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
impl Shape {
    pub(crate) fn parts(&self) -> &[Shape] {
        match self {
            Self::Union { shapes } => shapes,
            single => std::slice::from_ref(single),
        }
    }
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
    #[serde(
        default = "default_front_layer",
        skip_serializing_if = "is_front_layer"
    )]
    pub layer: String,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ViaKind {
    Through,
    BlindBuried,
    Micro,
}
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Via {
    pub id: String,
    pub net: u32,
    pub at: Point,
    pub diameter_nm: i64,
    pub drill_nm: i64,
    pub start_layer: String,
    pub end_layer: String,
    pub kind: ViaKind,
    pub locked: bool,
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ZoneConnection {
    None,
    #[default]
    Thermal,
    Solid,
    ThtThermal,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PadZoneConnection {
    #[default]
    Inherited,
    None,
    Thermal,
    Solid,
    ThtThermal,
}

impl PadZoneConnection {
    pub fn is_inherited(&self) -> bool {
        matches!(self, Self::Inherited)
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ZoneIslandRemoval {
    #[default]
    Always,
    Never,
    AreaLimit,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ZoneFillMode {
    #[default]
    Polygon,
    Hatch,
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ZoneLayerFill {
    pub layer: String,
    pub polygons: Vec<OutlinePolygon>,
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Zone {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    pub net: u32,
    pub layers: Vec<String>,
    pub outer: Vec<Point>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub holes: Vec<Vec<Point>>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub extra_polygons: Vec<OutlinePolygon>,
    #[serde(default, skip_serializing_if = "is_zero_i64")]
    pub priority: i64,
    #[serde(default, skip_serializing_if = "is_zero_i64")]
    pub local_clearance_nm: i64,
    #[serde(default, skip_serializing_if = "is_zero_i64")]
    pub min_thickness_nm: i64,
    #[serde(default)]
    pub connection: ZoneConnection,
    #[serde(default, skip_serializing_if = "is_zero_i64")]
    pub thermal_gap_nm: i64,
    #[serde(default, skip_serializing_if = "is_zero_i64")]
    pub thermal_spoke_width_nm: i64,
    #[serde(default)]
    pub island_removal: ZoneIslandRemoval,
    #[serde(default, skip_serializing_if = "is_zero_i64")]
    pub island_area_min_nm2: i64,
    #[serde(default)]
    pub fill_mode: ZoneFillMode,
    #[serde(default, skip_serializing_if = "is_false")]
    pub filled: bool,
    #[serde(default = "default_true", skip_serializing_if = "is_true")]
    pub refillable: bool,
    #[serde(default, skip_serializing_if = "is_false")]
    pub locked: bool,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub layer_fills: Vec<ZoneLayerFill>,
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ComponentInfo {
    pub ref_des: String,
    #[serde(default, skip_serializing_if = "is_false")]
    pub dnp: bool,
    #[serde(default, skip_serializing_if = "is_false")]
    pub exclude_from_bom: bool,
    #[serde(default, skip_serializing_if = "is_false")]
    pub exclude_from_pos: bool,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub pad_numbers: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub net_ids: Vec<u32>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StackupKind {
    Copper,
    Core,
    Prepreg,
    SolderMask,
    Other,
}

#[derive(Clone, Debug, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct StackupLayer {
    pub name: String,
    pub kind: StackupKind,
    pub thickness_nm: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub epsilon_r: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub material: Option<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DiffPairsMode {
    Auto,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProtectMode {
    None,
    Existing,
    Human,
    Locked,
}

#[derive(Clone, Debug, Default, PartialEq, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct LengthMatchGroupSpec {
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub members: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub from_to: Vec<[String; 2]>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tolerance_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delay_tolerance_as: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub meander_amplitude_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub meander_spacing_widths: Option<f64>,
}

/// Geometric preferences for single-ended nets. Distances are in nanometres.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct BusOptions {
    pub detection_radius_nm: i64,
    pub attraction_radius_nm: i64,
    pub attraction_bonus: u32,
    pub min_nets: usize,
}

impl Default for BusOptions {
    fn default() -> Self {
        Self {
            detection_radius_nm: 5_000_000,
            attraction_radius_nm: 5_000_000,
            attraction_bonus: 5000,
            min_nets: 2,
        }
    }
}

impl BusOptions {
    pub fn validate(&self) -> Result<(), Failure> {
        if self.detection_radius_nm < 1 || self.attraction_radius_nm < 1 {
            return Err(Failure::new(
                Status::Invalid,
                "bus radii must be at least 1 nm",
            ));
        }
        if self.min_nets < 2 {
            return Err(Failure::new(
                Status::Invalid,
                "bus minimum group size must be at least two nets",
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Default, PartialEq, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct RoutingOptions {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub grid_step_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bus: Option<BusOptions>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub diff_pairs_mode: Option<DiffPairsMode>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub explicit_pairs: Vec<[String; 2]>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub pair_layers: Vec<String>,
    #[serde(default, skip_serializing_if = "is_false")]
    pub no_short_pair_deferral: bool,
    #[serde(default, skip_serializing_if = "is_false")]
    pub intra_match: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub length_match_tolerance_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub meander_amplitude_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub length_match_groups: Vec<LengthMatchGroupSpec>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub group_match_tolerance_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub group_delay_tolerance_as: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub group_meander_amplitude_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub group_meander_spacing_widths: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pair_impedance_ohm: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub impedance_tolerance_pct: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub impedance_reference: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub impedance_uncoupled_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub ac_couple_chains: Vec<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ac_match_tolerance_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ac_meander_amplitude_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub net_filters: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub protect_mode: Option<ProtectMode>,
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuleArea {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    pub layers: Vec<String>,
    pub outer: Vec<Point>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub holes: Vec<Vec<Point>>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub extra_polygons: Vec<OutlinePolygon>,
    pub no_tracks: bool,
    pub no_vias: bool,
    #[serde(default, skip_serializing_if = "is_false")]
    pub no_pads: bool,
    #[serde(default, skip_serializing_if = "is_false")]
    pub no_footprints: bool,
    #[serde(default, skip_serializing_if = "is_false")]
    pub no_zone_fills: bool,
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
    OperationTimeout,
    Cancelled,
    AdmissionExceeded,
    JournalExhausted,
    NestingExhausted,
    InternalError,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PairDisposition {
    Coupled,
    Existing,
    Unverified,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RoutedPair {
    pub pos_net: u32,
    pub neg_net: u32,
    pub pos_name: String,
    pub neg_name: String,
    pub class_name: String,
    pub width_nm: i64,
    pub gap_nm: i64,
    pub disposition: PairDisposition,
}

#[derive(Debug, Serialize)]
pub struct Output {
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub diff_pairs: Vec<RoutedPair>,
    /// Groups of single-ended net IDs, in physical lane order, used by this solve.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub bus_groups: Vec<Vec<u32>>,
    pub version: u32,
    pub request_sha256: Option<String>,
    pub source_id: String,
    pub status: Status,
    pub added_tracks: Vec<Track>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub added_vias: Vec<Via>,
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
    pub fn active_layers(&self) -> Vec<String> {
        if self.layers.is_empty() {
            vec![self.layer.clone()]
        } else {
            self.layers.clone()
        }
    }

    pub(crate) fn item_count(&self) -> usize {
        let pad_primitives = self.pads.iter().fold(0usize, |total, p| {
            total.saturating_add(p.shape.parts().len())
        });
        self.nets
            .len()
            .saturating_add(pad_primitives)
            .saturating_add(self.tracks.len())
            .saturating_add(self.vias.len())
            .saturating_add(self.rule_areas.len())
            .saturating_add(self.zones.len())
            .saturating_add(self.holes.len())
            .saturating_add(self.graphics.len())
            .saturating_add(self.mask_apertures.len())
    }

    pub fn validate(&self) -> Result<(), Failure> {
        self.validate_with_work(&crate::budget::WorkBudget::new(self.limits))
    }

    pub(crate) fn validate_with_work(
        &self,
        compound_work: &crate::budget::WorkBudget,
    ) -> Result<(), Failure> {
        self.limits.validate()?;
        if let Some(options) = &self.routing_options {
            if options
                .grid_step_nm
                .is_some_and(|grid| !(1..=MAX_NM).contains(&grid))
            {
                return Err(Failure::new(
                    Status::Invalid,
                    "grid_step_nm is outside the supported routing grid range",
                ));
            }
            if let Some(bus) = options.bus {
                bus.validate()?;
            }
        }
        if self.item_count() > self.limits.max_items {
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
        let active_layers: Vec<String> = if self.layers.is_empty() {
            if self.layer != "F.Cu" {
                return Err(unsupported(
                    "only F.Cu routing is supported without stackup layers",
                ));
            }
            vec![self.layer.clone()]
        } else {
            if self.layers.len() > 32 {
                return Err(unsupported(
                    "board copper stackup supports at most 32 layers",
                ));
            }
            if self.layers[0] != "F.Cu" {
                return Err(unsupported("copper stackup must start at F.Cu"));
            }
            if self.layers.len() >= 2 && self.layers.last().map(String::as_str) != Some("B.Cu") {
                return Err(unsupported("multi-layer copper stackup must end at B.Cu"));
            }
            let mut seen_layers = BTreeSet::new();
            for (idx, lname) in self.layers.iter().enumerate() {
                if !seen_layers.insert(lname.as_str()) {
                    return Err(invalid("duplicate layer in copper stackup"));
                }
                if idx > 0 && idx + 1 < self.layers.len() {
                    let valid_inner = lname
                        .strip_prefix("In")
                        .and_then(|s| s.strip_suffix(".Cu"))
                        .and_then(|n| n.parse::<u32>().ok())
                        .is_some_and(|n| (1..=30).contains(&n));
                    if !valid_inner {
                        return Err(unsupported("unsupported inner copper layer name"));
                    }
                }
            }
            if !self.layers.contains(&self.layer) {
                return Err(unsupported("input.layer must be in active copper stackup"));
            }
            self.layers.clone()
        };
        let layer_idx_of =
            |name: &str| -> Option<usize> { active_layers.iter().position(|l| l == name) };
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
            if let Some(lc) = p.local_clearance_nm {
                if !(0..=MAX_NM).contains(&lc) {
                    return Err(invalid("pad local_clearance_nm must be 0..=100000000 nm"));
                }
            }
            if let Some(tg) = p.thermal_gap_nm {
                if !(0..=MAX_NM).contains(&tg) {
                    return Err(invalid("pad thermal_gap_nm must be 0..=100000000 nm"));
                }
            }
            if let Some(ts) = p.thermal_spoke_width_nm {
                if !(0..=MAX_NM).contains(&ts) {
                    return Err(invalid(
                        "pad thermal_spoke_width_nm must be 0..=100000000 nm",
                    ));
                }
            }
            if let Some(sa) = p.thermal_spoke_angle_mdeg {
                if !(-3_600_000..=3_600_000).contains(&sa) {
                    return Err(invalid("pad thermal_spoke_angle_mdeg is out of bounds"));
                }
            }
            if !(-3_600_000..=3_600_000).contains(&p.orientation_mdeg) {
                return Err(invalid("pad orientation_mdeg is out of bounds"));
            }
            for pl in &p.layers {
                if layer_idx_of(pl).is_none() {
                    return Err(unsupported("pad layer is not in active board stackup"));
                }
            }
            if let Shape::Union { shapes } = &p.shape {
                if !(2..=MAX_PAD_PRIMITIVES).contains(&shapes.len()) {
                    return Err(invalid("pad union must contain 2 to 16 primitives"));
                }
                if p.drill.is_some() || p.layers.len() > 1 {
                    return Err(unsupported(
                        "compound copper is supported on single-layer undrilled pads",
                    ));
                }
            }
            for shape in p.shape.parts() {
                match shape {
                    Shape::Union { .. } => {
                        return Err(invalid("nested pad unions are unsupported"))
                    }
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
                            return Err(invalid(
                                "convex polygon pad vertices or radius are invalid",
                            ));
                        }
                        if !crate::geometry::is_ccw_convex_polygon(vertices) {
                            return Err(invalid(
                                "convex polygon pad must be strictly CCW convex with nonzero area",
                            ));
                        }
                    }
                }
            }
            if matches!(p.shape, Shape::Union { .. }) {
                compound_work.charge(p.shape.parts().len() as u64)?;
                if !crate::geometry::point_in_pad(p.at, p) {
                    return Err(unsupported(
                        "compound pad anchor must lie inside a copper primitive",
                    ));
                }
                if !crate::geometry::union_is_connected(p, compound_work)? {
                    return Err(unsupported("compound pad primitives must connect through at least 1 nm of positive overlap"));
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
            if layer_idx_of(&t.layer).is_none() {
                return Err(unsupported("track layer is not in active board stackup"));
            }
        }
        for v in &self.vias {
            if !canonical_uuid(&v.id)
                || !ids.insert(&v.id)
                || (v.net != 0 && !nets.contains(&v.net))
                || !inside(v.at)
                || !(1..=2 * MAX_NM).contains(&v.drill_nm)
                || !(v.drill_nm + 1..=2 * MAX_NM).contains(&v.diameter_nm)
            {
                return Err(invalid(
                    "via identity, net membership, position, or dimensions are invalid",
                ));
            }
            let Some(s_idx) = layer_idx_of(&v.start_layer) else {
                return Err(unsupported(
                    "via start_layer is not in active board stackup",
                ));
            };
            let Some(e_idx) = layer_idx_of(&v.end_layer) else {
                return Err(unsupported("via end_layer is not in active board stackup"));
            };
            if s_idx >= e_idx {
                return Err(invalid("via start_layer must precede end_layer in stackup"));
            }
            match v.kind {
                ViaKind::Through => {
                    if s_idx != 0 || e_idx + 1 != active_layers.len() {
                        return Err(invalid("through via must span first to last copper layer"));
                    }
                }
                ViaKind::BlindBuried => {
                    if s_idx == 0 && e_idx + 1 == active_layers.len() {
                        return Err(invalid("blind/buried via cannot span full through stackup"));
                    }
                }
                ViaKind::Micro => {
                    if e_idx != s_idx + 1 || (s_idx != 0 && e_idx + 1 != active_layers.len()) {
                        return Err(unsupported(
                            "microvia must span adjacent outer-to-inner layer",
                        ));
                    }
                }
            }
        }
        let validate_simple_poly = |outer: &[Point],
                                    holes: &[Vec<Point>],
                                    label: &str,
                                    vert_budget: &mut usize|
         -> Result<(), Failure> {
            if outer.len() < 3
                || !outer.iter().all(|p| bounded_pt(*p))
                || !crate::geometry::is_valid_simple_ring(outer)
            {
                return Err(Failure::new(
                    Status::Invalid,
                    format!("{label} outer polygon is invalid"),
                ));
            }
            *vert_budget = vert_budget.saturating_add(outer.len());
            let mut rings: Vec<&[Point]> = vec![outer];
            for hole in holes {
                if hole.len() < 3
                    || !hole.iter().all(|p| bounded_pt(*p))
                    || !crate::geometry::is_valid_simple_ring(hole)
                {
                    return Err(Failure::new(
                        Status::Invalid,
                        format!("{label} hole polygon is invalid"),
                    ));
                }
                *vert_budget = vert_budget.saturating_add(hole.len());
                rings.push(hole);
            }
            if *vert_budget > 65_536 {
                return Err(Failure::new(
                    Status::AdmissionExceeded,
                    format!("{label} vertex limit exceeded"),
                ));
            }
            for i in 0..rings.len() {
                for j in (i + 1)..rings.len() {
                    if crate::geometry::rings_cross(rings[i], rings[j]) {
                        return Err(Failure::new(
                            Status::Invalid,
                            format!("{label} rings must not cross"),
                        ));
                    }
                }
            }
            for hole in holes {
                if !crate::geometry::point_in_simple_polygon(hole[0], outer) {
                    return Err(Failure::new(
                        Status::Invalid,
                        format!("{label} hole must lie inside outer ring"),
                    ));
                }
            }
            Ok(())
        };
        for ra in &self.rule_areas {
            if !canonical_uuid(&ra.id) || !ids.insert(&ra.id) || ra.layers.is_empty() {
                return Err(invalid("rule_area identity or layers are invalid"));
            }
            if let Some(name) = &ra.name {
                if name.len() > 1024 {
                    return Err(invalid("rule_area name exceeds 1024 UTF-8 bytes"));
                }
            }
            let mut seen_ra_layers = BTreeSet::new();
            for l in &ra.layers {
                if !seen_ra_layers.insert(l.as_str()) {
                    return Err(invalid("duplicate layer in rule_area"));
                }
                if layer_idx_of(l).is_none() {
                    return Err(unsupported(
                        "rule_area layer is not in active board stackup",
                    ));
                }
            }
            let mut verts = 0usize;
            validate_simple_poly(&ra.outer, &ra.holes, "rule_area", &mut verts)?;
            for extra in &ra.extra_polygons {
                validate_simple_poly(&extra.outer, &extra.holes, "rule_area", &mut verts)?;
            }
        }
        for z in &self.zones {
            if !canonical_uuid(&z.id)
                || !ids.insert(&z.id)
                || z.layers.is_empty()
                || (z.net != 0 && !nets.contains(&z.net))
            {
                return Err(invalid(
                    "zone identity, net membership or layers are invalid",
                ));
            }
            if let Some(name) = &z.name {
                if name.len() > 1024 {
                    return Err(invalid("zone name exceeds 1024 UTF-8 bytes"));
                }
            }
            if z.fill_mode == ZoneFillMode::Hatch {
                return Err(unsupported("hatched copper zones are not supported"));
            }
            if ![
                z.local_clearance_nm,
                z.min_thickness_nm,
                z.thermal_gap_nm,
                z.thermal_spoke_width_nm,
            ]
            .iter()
            .all(|v| (0..=MAX_NM).contains(v))
                || z.island_area_min_nm2 < 0
            {
                return Err(invalid(
                    "zone clearance, thermal, or island bounds are invalid",
                ));
            }
            let mut seen_z_layers = BTreeSet::new();
            for l in &z.layers {
                if !seen_z_layers.insert(l.as_str()) {
                    return Err(invalid("duplicate layer in zone"));
                }
                if layer_idx_of(l).is_none() {
                    return Err(unsupported("zone layer is not in active board stackup"));
                }
            }
            let mut verts = 0usize;
            validate_simple_poly(&z.outer, &z.holes, "zone", &mut verts)?;
            for extra in &z.extra_polygons {
                validate_simple_poly(&extra.outer, &extra.holes, "zone", &mut verts)?;
            }
            if !z.filled && !z.layer_fills.is_empty() {
                return Err(invalid("unfilled zone cannot carry layer_fills"));
            }
            let mut seen_fill_layers = BTreeSet::new();
            let mut fill_verts = 0usize;
            for lf in &z.layer_fills {
                if !seen_fill_layers.insert(lf.layer.as_str()) {
                    return Err(invalid("duplicate layer in zone layer_fills"));
                }
                if layer_idx_of(&lf.layer).is_none() {
                    return Err(unsupported(
                        "zone layer_fill layer is not in active board stackup",
                    ));
                }
                if !seen_z_layers.contains(lf.layer.as_str()) {
                    return Err(invalid("zone layer_fill layer is not in zone layers"));
                }
                for poly in &lf.polygons {
                    if !poly.outer.iter().all(|p| bounded_pt(*p))
                        || !crate::geometry::is_valid_closed_ring(&poly.outer)
                    {
                        return Err(invalid("zone layer_fill outer polygon is invalid"));
                    }
                    fill_verts = fill_verts.saturating_add(poly.outer.len());
                    for hole in &poly.holes {
                        if !hole.iter().all(|p| bounded_pt(*p))
                            || !crate::geometry::is_valid_closed_ring(hole)
                            || !crate::geometry::point_in_simple_polygon(hole[0], &poly.outer)
                        {
                            return Err(invalid("zone layer_fill hole polygon is invalid"));
                        }
                        fill_verts = fill_verts.saturating_add(hole.len());
                    }
                    if fill_verts > 1_000_000 {
                        return Err(Failure::new(
                            Status::AdmissionExceeded,
                            "zone layer_fills vertex limit exceeded",
                        ));
                    }
                }
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
            if layer_idx_of(&g.layer).is_none() {
                return Err(unsupported(
                    "copper graphic layer is not in active board stackup",
                ));
            }
            validate_graphic_shape(&g.shape)?;
        }
        for m in &self.mask_apertures {
            if !canonical_uuid(&m.id)
                || !ids.insert(&m.id)
                || m.group_id.as_deref().is_some_and(|g| !canonical_uuid(g))
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
        for sl in &self.stackup {
            if sl.name.is_empty()
                || sl.name.len() > 1024
                || !(0..=2 * MAX_NM).contains(&sl.thickness_nm)
            {
                return Err(invalid("stackup layer name or thickness is invalid"));
            }
            if let Some(er) = sl.epsilon_r {
                if !er.is_finite() || er <= 0.0 {
                    return Err(invalid("stackup epsilon_r must be finite and positive"));
                }
            }
        }
        if let Some(ro) = &self.routing_options {
            if ro.length_match_tolerance_nm.is_some_and(|v| v < 0)
                || ro.meander_amplitude_nm.is_some_and(|v| v <= 0)
                || ro.group_match_tolerance_nm.is_some_and(|v| v < 0)
                || ro.group_delay_tolerance_as.is_some_and(|v| v < 0)
                || ro.group_meander_amplitude_nm.is_some_and(|v| v <= 0)
                || ro.impedance_uncoupled_nm.is_some_and(|v| v < 0)
                || ro.ac_match_tolerance_nm.is_some_and(|v| v <= 0)
                || ro.ac_meander_amplitude_nm.is_some_and(|v| v <= 0)
                || ro
                    .group_meander_spacing_widths
                    .is_some_and(|v| !v.is_finite() || v < 1.0)
                || ro
                    .pair_impedance_ohm
                    .is_some_and(|v| !v.is_finite() || v <= 0.0)
                || ro
                    .impedance_tolerance_pct
                    .is_some_and(|v| !v.is_finite() || v <= 0.0)
            {
                return Err(invalid(
                    "routing_options numeric tolerance, amplitude, spacing, or impedance out of range",
                ));
            }
        }
        for p in self
            .pads
            .iter()
            .filter(|p| matches!(p.shape, Shape::Union { .. }) && p.net != 0)
        {
            let layer = p.layers.first().unwrap_or(&self.layer);
            for z in &self.zones {
                compound_work.charge(1)?;
                if z.net != p.net
                    || !z.layers.contains(layer)
                    || crate::zones::resolve_pad_zone_connection(p, z)
                        != crate::zones::ResolvedPadZoneConnection::Thermal
                {
                    continue;
                }
                let overlaps = |outer: &[Point], holes: &[Vec<Point>]| -> Result<bool, Failure> {
                    let vertices = outer.len() + holes.iter().map(Vec::len).sum::<usize>();
                    compound_work.charge(vertices as u64 * p.shape.parts().len() as u64)?;
                    Ok(
                        crate::geometry::point_in_polygon_with_holes(p.at, outer, holes)
                            || crate::geometry::zone_fill_connects_pad(outer, holes, p),
                    )
                };
                if overlaps(&z.outer, &z.holes)? {
                    return Err(unsupported(
                        "thermal zone connection on a compound pad is unsupported",
                    ));
                }
                for extra in &z.extra_polygons {
                    if overlaps(&extra.outer, &extra.holes)? {
                        return Err(unsupported(
                            "thermal zone connection on a compound pad is unsupported",
                        ));
                    }
                }
            }
        }
        Ok(())
    }
}
