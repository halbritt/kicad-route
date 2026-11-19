pub mod identity;
pub use identity::{input_request_sha256, request_sha256};
mod budget;
mod components;
pub mod diff_pair;
pub mod geometry;
pub mod model;
pub mod negotiate;
pub mod orchestration;
pub mod rules;
pub mod search;
mod spatial;
pub mod state;
mod zones;
use components::{span_mask, Copper};
use model::*;
use rules::{DruItemKind, EffectiveRules};
use spatial::{
    ArtworkGeom, CopperAabb, DrillGeom, EdgeGeom, KeepoutGeom, MaskGeom, PadCore,
    SpokeCorridorGeom, StaticSpatialIndex, SweptAabb, ZoneFillGeom,
};
use state::CopperStore;
use std::ops::ControlFlow;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum StaticMaskOccupancy {
    Empty,
    Single(u32),
    Multi,
}

pub struct Engine {
    input: Input,
    layer_names: Vec<String>,
    effective_rules: EffectiveRules,
    max_pad_local_clearance_nm: i64,
    max_zone_local_clearance_nm: i64,
    copper: CopperStore,
    pad_spatial: Vec<StaticSpatialIndex<PadCore>>,
    hole_spatial: StaticSpatialIndex<DrillGeom>,
    outline_spatial: StaticSpatialIndex<EdgeGeom>,
    artwork_spatial: Vec<StaticSpatialIndex<ArtworkGeom>>,
    artwork_graphic_idx: Vec<Vec<usize>>,
    keepout_spatial: Vec<StaticSpatialIndex<KeepoutGeom>>,
    zone_fill_spatial: Vec<StaticSpatialIndex<ZoneFillGeom>>,
    spoke_corridor_spatial: Vec<StaticSpatialIndex<SpokeCorridorGeom>>,
    spoke_corridors: Vec<zones::SpokeCorridor>,
    mask_spatial: [StaticSpatialIndex<MaskGeom>; 2],
    mask_aperture_idx: [Vec<usize>; 2],
    mask_static_nets: Vec<StaticMaskOccupancy>,
    mask_aabbs: Vec<CopperAabb>,
    mask_group_id: Vec<usize>,
    mask_groups: Vec<Vec<usize>>,
    max_mask_expansion: i64,
    work: budget::WorkBudget,
    last_negotiation_stats: Option<negotiate::NegotiationStats>,
    last_block_generated: std::cell::Cell<bool>,
    prefer_higher_g: bool,
    delay_profile_layers: std::collections::BTreeMap<u32, std::collections::BTreeSet<String>>,
}
/// Engine scope admission errors are distinct from an arbitrary caller error.
#[derive(Debug, PartialEq, Eq)]
pub enum TransactionError<E> {
    Limit(Failure),
    Operation(E),
}
impl From<TransactionError<Failure>> for Failure {
    fn from(e: TransactionError<Failure>) -> Self {
        match e {
            TransactionError::Limit(e) | TransactionError::Operation(e) => e,
        }
    }
}
/// A borrowed view inside `Engine::transaction`. It cannot be constructed,
/// committed, rolled back or retained outside its closure by the caller.
/// Operations have their own nested scope, so a handled routing error does
/// not discard earlier work in the enclosing transaction.
pub struct Transaction<'a> {
    engine: &'a mut Engine,
}
impl Transaction<'_> {
    pub fn work_used(&self) -> u64 {
        self.engine.work_used()
    }
    pub fn copper(&self) -> &CopperStore {
        self.engine.copper()
    }
    pub fn route_all(&mut self) -> Result<u64, Failure> {
        self.engine.route_all()
    }
    pub fn route(&mut self, net: u32, max_expansions: u64) -> Result<u64, Failure> {
        self.engine.route(net, max_expansions)
    }
    pub fn reroute(&mut self, net: u32, max_expansions: u64) -> Result<u64, Failure> {
        self.engine.reroute(net, max_expansions)
    }
    pub fn transaction<T, E>(
        &mut self,
        operation: impl FnOnce(&mut Transaction<'_>) -> Result<T, E>,
    ) -> Result<T, TransactionError<E>> {
        self.engine.transaction(operation)
    }
}

// This guard is never handed to user code. Even forgetting the borrowed
// Transaction view cannot bypass rollback or move a frame to another engine.
struct Scope<'a> {
    engine: &'a mut Engine,
    committed: bool,
    outer: bool,
    prev_prefer_higher_g: bool,
}
impl Drop for Scope<'_> {
    fn drop(&mut self) {
        self.engine.prefer_higher_g = self.prev_prefer_higher_g;
        if !self.committed {
            self.engine.copper.rollback();
        }
        self.engine.work.end(self.outer);
    }
}

fn try_i32(v: i64) -> Result<i32, Failure> {
    i32::try_from(v).map_err(|_| {
        Failure::new(
            Status::Unsupported,
            "coordinate or width exceeds i32 bounds",
        )
    })
}

fn try_i32_pt(p: Point) -> Result<[i32; 2], Failure> {
    Ok([try_i32(p[0])?, try_i32(p[1])?])
}

fn i32_pt(p: Point) -> [i32; 2] {
    [
        i32::try_from(p[0]).expect("validated coordinate fits i32"),
        i32::try_from(p[1]).expect("validated coordinate fits i32"),
    ]
}

fn ring_aabb(ring: &[Point]) -> CopperAabb {
    let mut lo = [i64::MAX, i64::MAX];
    let mut hi = [i64::MIN, i64::MIN];
    for p in ring {
        for k in 0..2 {
            lo[k] = lo[k].min(p[k]);
            hi[k] = hi[k].max(p[k]);
        }
    }
    CopperAabb::from_bounds((lo, hi))
}

fn seg_aabb(a: Point, b: Point, radius: i64) -> CopperAabb {
    CopperAabb::from_bounds((
        [a[0].min(b[0]) - radius, a[1].min(b[1]) - radius],
        [a[0].max(b[0]) + radius, a[1].max(b[1]) + radius],
    ))
}

fn compute_static_mask_occupancy(
    input: &Input,
    layer_names: &[String],
    m: &MaskAperture,
    m_aabb: CopperAabb,
) -> StaticMaskOccupancy {
    let cu_layer = if m.layer == "F.Mask" {
        "F.Cu"
    } else if m.layer == "B.Mask" {
        "B.Cu"
    } else {
        return StaticMaskOccupancy::Empty;
    };
    let mut occ = match m.net {
        Some(0) => StaticMaskOccupancy::Multi,
        Some(n) => StaticMaskOccupancy::Single(n),
        None => StaticMaskOccupancy::Empty,
    };
    if occ == StaticMaskOccupancy::Multi {
        return occ;
    }
    let Some(l_idx) = layer_names.iter().position(|l| l == cu_layer) else {
        return occ;
    };
    let record_net = |occ: &mut StaticMaskOccupancy, net: u32| {
        if net == 0 {
            *occ = StaticMaskOccupancy::Multi;
        } else {
            *occ = match *occ {
                StaticMaskOccupancy::Empty => StaticMaskOccupancy::Single(net),
                StaticMaskOccupancy::Single(k) if k == net => StaticMaskOccupancy::Single(k),
                _ => StaticMaskOccupancy::Multi,
            };
        }
    };
    let pad_bridge_clr = input
        .rules
        .solder_mask_expansion_nm
        .max(0)
        .saturating_add(input.rules.solder_mask_min_width_nm.max(0));
    let mut pad_mask = m.clone();
    pad_mask.expansion_nm = m.expansion_nm.max(0).saturating_add(pad_bridge_clr);
    for pad in &input.pads {
        let on_layer = if pad.layers.is_empty() {
            cu_layer == input.layer
        } else {
            pad.layers.iter().any(|l| l == cu_layer)
        };
        let (pmin, pmax) = pad.copper_bounds();
        let pad_aabb = CopperAabb::from_bounds((
            [
                pmin[0].saturating_sub(pad_bridge_clr),
                pmin[1].saturating_sub(pad_bridge_clr),
            ],
            [
                pmax[0].saturating_add(pad_bridge_clr),
                pmax[1].saturating_add(pad_bridge_clr),
            ],
        ));
        if on_layer && pad_aabb.overlaps(m_aabb) && geometry::mask_overlaps_pad(&pad_mask, pad) {
            record_net(&mut occ, pad.net);
            if occ == StaticMaskOccupancy::Multi {
                return occ;
            }
        }
    }
    for g in &input.graphics {
        if g.layer == cu_layer
            && CopperAabb::from_bounds(g.copper_bounds()).overlaps(m_aabb)
            && geometry::mask_overlaps_graphic(m, g)
        {
            record_net(&mut occ, g.net);
            if occ == StaticMaskOccupancy::Multi {
                return occ;
            }
        }
    }
    for t in &input.tracks {
        if t.layer == cu_layer
            && CopperAabb::from_bounds(t.copper_bounds()).overlaps(m_aabb)
            && geometry::track_near_mask(t.start, t.end, t.width_nm, 0, m, false)
        {
            record_net(&mut occ, t.net);
            if occ == StaticMaskOccupancy::Multi {
                return occ;
            }
        }
    }
    for v in &input.vias {
        let s_idx = layer_names.iter().position(|l| l == &v.start_layer);
        let e_idx = layer_names.iter().position(|l| l == &v.end_layer);
        let spans = match (s_idx, e_idx) {
            (Some(s), Some(e)) => l_idx >= s.min(e) && l_idx <= s.max(e),
            _ => false,
        };
        let r = (v.diameter_nm + 1) / 2;
        if spans
            && CopperAabb::from_bounds(([v.at[0] - r, v.at[1] - r], [v.at[0] + r, v.at[1] + r]))
                .overlaps(m_aabb)
            && geometry::track_near_mask(v.at, v.at, v.diameter_nm, 0, m, false)
        {
            record_net(&mut occ, v.net);
            if occ == StaticMaskOccupancy::Multi {
                return occ;
            }
        }
    }
    for z in &input.zones {
        if z.refillable || !z.filled {
            continue;
        }
        for lf in &z.layer_fills {
            if lf.layer != cu_layer {
                continue;
            }
            for poly in &lf.polygons {
                if ring_aabb(&poly.outer).overlaps(m_aabb)
                    && geometry::mask_overlaps_filled_polygon(m, &poly.outer, &poly.holes)
                {
                    record_net(&mut occ, z.net);
                    if occ == StaticMaskOccupancy::Multi {
                        return occ;
                    }
                }
            }
        }
    }
    occ
}

fn merge_mask_occ(a: StaticMaskOccupancy, b: StaticMaskOccupancy) -> StaticMaskOccupancy {
    match (a, b) {
        (StaticMaskOccupancy::Empty, x) | (x, StaticMaskOccupancy::Empty) => x,
        (StaticMaskOccupancy::Single(x), StaticMaskOccupancy::Single(y)) if x == y => {
            StaticMaskOccupancy::Single(x)
        }
        _ => StaticMaskOccupancy::Multi,
    }
}

fn group_mask_components(
    apertures: &[MaskAperture],
    aabbs: &[CopperAabb],
    occ: &mut [StaticMaskOccupancy],
    web_nm: i64,
) -> (Vec<usize>, Vec<Vec<usize>>) {
    let n = apertures.len();
    if n == 0 {
        return (Vec::new(), Vec::new());
    }
    let mut parent: Vec<usize> = (0..n).collect();
    fn find(parent: &mut [usize], mut x: usize) -> usize {
        while parent[x] != x {
            parent[x] = parent[parent[x]];
            x = parent[x];
        }
        x
    }
    let union = |parent: &mut [usize], a: usize, b: usize| {
        let ra = find(parent, a);
        let rb = find(parent, b);
        if ra != rb {
            parent[rb] = ra;
        }
    };
    let web = web_nm.max(0);
    let mut by_group: std::collections::BTreeMap<(&str, &str), usize> =
        std::collections::BTreeMap::new();
    for (i, ap) in apertures.iter().enumerate() {
        if let Some(gid) = ap.group_id.as_deref() {
            if let Some(&first) = by_group.get(&(ap.layer.as_str(), gid)) {
                union(&mut parent, first, i);
            } else {
                by_group.insert((ap.layer.as_str(), gid), i);
            }
        }
    }
    for layer in ["F.Mask", "B.Mask"] {
        let mut idxs: Vec<usize> = (0..n).filter(|&i| apertures[i].layer == layer).collect();
        idxs.sort_by_key(|&i| (aabbs[i].min[0], aabbs[i].min[1], i));
        for pos in 0..idxs.len() {
            let i = idxs[pos];
            let max_x = aabbs[i].max[0].saturating_add(web);
            for &j in &idxs[(pos + 1)..] {
                if aabbs[j].min[0] > max_x {
                    break;
                }
                if aabbs[j].min[1] <= aabbs[i].max[1].saturating_add(web)
                    && aabbs[i].min[1] <= aabbs[j].max[1].saturating_add(web)
                    && find(&mut parent, i) != find(&mut parent, j)
                    && geometry::mask_apertures_overlap(&apertures[i], &apertures[j], web)
                {
                    union(&mut parent, i, j);
                }
            }
        }
    }
    let mut root_to_gid = vec![usize::MAX; n];
    let mut group_id = vec![0usize; n];
    let mut groups: Vec<Vec<usize>> = Vec::new();
    let mut group_occ: Vec<StaticMaskOccupancy> = Vec::new();
    for i in 0..n {
        let r = find(&mut parent, i);
        let gid = if root_to_gid[r] == usize::MAX {
            let g = groups.len();
            root_to_gid[r] = g;
            groups.push(Vec::new());
            group_occ.push(StaticMaskOccupancy::Empty);
            g
        } else {
            root_to_gid[r]
        };
        group_id[i] = gid;
        groups[gid].push(i);
        group_occ[gid] = merge_mask_occ(group_occ[gid], occ[i]);
    }
    for i in 0..n {
        occ[i] = group_occ[group_id[i]];
    }
    (group_id, groups)
}

impl Engine {
    pub fn new(input: Input) -> Result<Self, Failure> {
        Self::new_with_control(input, None)
    }
    fn new_with_control(
        mut input: Input,
        control: Option<budget::RouteControl>,
    ) -> Result<Self, Failure> {
        input.limits.validate()?;
        if input.version != INPUT_VERSION {
            return Err(Failure::new(
                Status::Unsupported,
                "only input version 1 is supported",
            ));
        }
        let effective_rules = rules::resolve_effective_rules(&mut input)?;
        let work = budget::WorkBudget::with_control(input.limits, control);
        input.validate_with_work(&work)?;
        work.carry_setup_into_first_scope();
        let layer_names = input.active_layers();
        let layer_count = layer_names.len();
        let base_items = input.item_count();
        let reserved = input
            .pads
            .iter()
            .map(|p| p.id.clone())
            .chain(input.tracks.iter().map(|t| t.id.clone()))
            .chain(input.vias.iter().map(|v| v.id.clone()))
            .chain(input.rule_areas.iter().map(|ra| ra.id.clone()))
            .chain(input.zones.iter().map(|z| z.id.clone()))
            .chain(input.holes.iter().map(|h| h.id.clone()))
            .chain(input.graphics.iter().map(|g| g.id.clone()))
            .chain(input.mask_apertures.iter().map(|m| m.id.clone()))
            .collect();
        let max_pad_local_clearance_nm = input
            .pads
            .iter()
            .filter_map(|p| p.local_clearance_nm)
            .max()
            .unwrap_or(0);
        let max_zone_local_clearance_nm = input
            .zones
            .iter()
            .map(|z| z.local_clearance_nm)
            .max()
            .unwrap_or(0);
        let mut per_layer_pads: Vec<Vec<(u32, u32, CopperAabb, PadCore)>> =
            vec![Vec::new(); layer_count];
        for (i, pad) in input.pads.iter().enumerate() {
            let core = match &pad.shape {
                Shape::Circle { radius_nm } => PadCore::Box {
                    lo: i32_pt(pad.at),
                    hi: i32_pt(pad.at),
                    radius: *radius_nm as i32,
                },
                Shape::Rect { half_size_nm: h } => PadCore::Box {
                    lo: i32_pt([pad.at[0] - h[0], pad.at[1] - h[1]]),
                    hi: i32_pt([pad.at[0] + h[0], pad.at[1] + h[1]]),
                    radius: 0,
                },
                Shape::RoundRect {
                    half_size_nm: h,
                    radius_nm: r,
                } => PadCore::Box {
                    lo: i32_pt([pad.at[0] - (h[0] - r), pad.at[1] - (h[1] - r)]),
                    hi: i32_pt([pad.at[0] + (h[0] - r), pad.at[1] + (h[1] - r)]),
                    radius: *r as i32,
                },
                Shape::BoxExact {
                    center_2x,
                    half_size_2x,
                    radius_2x,
                } => {
                    let ch = [half_size_2x[0] - radius_2x, half_size_2x[1] - radius_2x];
                    PadCore::Box2x {
                        lo2: i32_pt([center_2x[0] - ch[0], center_2x[1] - ch[1]]),
                        hi2: i32_pt([center_2x[0] + ch[0], center_2x[1] + ch[1]]),
                        radius2: *radius_2x as i32,
                    }
                }
                Shape::Capsule {
                    start_nm,
                    end_nm,
                    width_nm,
                } => PadCore::Capsule {
                    a: i32_pt(*start_nm),
                    b: i32_pt(*end_nm),
                    width: *width_nm as i32,
                },
                Shape::ConvexPolygon { .. } | Shape::Union { .. } => PadCore::General,
            };
            let entry = (
                u32::try_from(i).expect("bounded input pad count"),
                pad.net,
                CopperAabb::from_bounds(pad.copper_bounds()),
                core,
            );
            if pad.layers.is_empty() {
                let l_idx = layer_names
                    .iter()
                    .position(|l| l == &input.layer)
                    .unwrap_or(0);
                per_layer_pads[l_idx].push(entry);
            } else {
                for pl in &pad.layers {
                    if let Some(l_idx) = layer_names.iter().position(|l| l == pl) {
                        per_layer_pads[l_idx].push(entry);
                    }
                }
            }
        }
        let pad_spatial = per_layer_pads
            .into_iter()
            .map(StaticSpatialIndex::new)
            .collect();
        let drill_to_geom = |at: Point, drill: &DrillShape| -> DrillGeom {
            match *drill {
                DrillShape::Circle { diameter_nm } => DrillGeom::Circle {
                    center: i32_pt(at),
                    diameter: diameter_nm as i32,
                },
                DrillShape::Capsule {
                    start_nm,
                    end_nm,
                    width_nm,
                } => DrillGeom::Capsule {
                    a: i32_pt(start_nm),
                    b: i32_pt(end_nm),
                    width: width_nm as i32,
                },
            }
        };
        let hole_items = input
            .holes
            .iter()
            .enumerate()
            .map(|(i, hole)| {
                (
                    u32::try_from(i).expect("bounded hole count"),
                    hole.net,
                    CopperAabb::from_bounds(hole.shape.bounds(hole.at)),
                    drill_to_geom(hole.at, &hole.shape),
                )
            })
            .chain(input.pads.iter().enumerate().filter_map(|(i, pad)| {
                pad.drill.as_ref().map(|drill| {
                    (
                        u32::try_from(input.holes.len() + i).expect("bounded hole+pad count"),
                        pad.net,
                        CopperAabb::from_bounds(drill.bounds(pad.at)),
                        drill_to_geom(pad.at, drill),
                    )
                })
            }));
        let hole_spatial = StaticSpatialIndex::new(hole_items);

        let mut outline_items = Vec::new();
        if let Some(outline) = &input.board_outline {
            let mut ord = 0u32;
            for poly in &outline.polygons {
                for ring in std::iter::once(&poly.outer).chain(poly.holes.iter()) {
                    let n = ring.len();
                    for i in 0..n {
                        let a = ring[i];
                        let b = ring[(i + 1) % n];
                        outline_items.push((
                            ord,
                            0u32,
                            seg_aabb(a, b, 0),
                            EdgeGeom {
                                a: i32_pt(a),
                                b: i32_pt(b),
                            },
                        ));
                        ord += 1;
                    }
                }
            }
        }
        let outline_spatial = StaticSpatialIndex::new(outline_items);

        let mut per_layer_art: Vec<Vec<(u32, u32, CopperAabb, ArtworkGeom)>> =
            vec![Vec::new(); layer_count];
        let mut artwork_graphic_idx: Vec<Vec<usize>> = vec![Vec::new(); layer_count];
        for (g_idx, g) in input.graphics.iter().enumerate() {
            let Some(l_idx) = layer_names.iter().position(|l| l == &g.layer) else {
                continue;
            };
            let push_art = |per_layer: &mut Vec<Vec<_>>,
                            g_map: &mut Vec<Vec<usize>>,
                            aabb: CopperAabb,
                            geom: ArtworkGeom| {
                let ord = u32::try_from(g_map[l_idx].len()).expect("bounded artwork count");
                g_map[l_idx].push(g_idx);
                per_layer[l_idx].push((ord, g.net, aabb, geom));
            };
            match &g.shape {
                GraphicShape::Segment {
                    start,
                    end,
                    width_nm,
                } => {
                    push_art(
                        &mut per_layer_art,
                        &mut artwork_graphic_idx,
                        seg_aabb(*start, *end, (*width_nm + 1) / 2),
                        ArtworkGeom::Segment {
                            a: i32_pt(*start),
                            b: i32_pt(*end),
                            width: *width_nm as i32,
                        },
                    );
                }
                GraphicShape::Arc {
                    start,
                    mid,
                    end,
                    width_nm,
                } => {
                    let (chords, sag) = geometry::arc_to_conservative_chords(*start, *mid, *end);
                    let eff_w = width_nm + 2 * sag;
                    let eff_w_i32 = try_i32(eff_w)?;
                    let rad = (eff_w + 1) / 2;
                    for w in chords.windows(2) {
                        push_art(
                            &mut per_layer_art,
                            &mut artwork_graphic_idx,
                            seg_aabb(w[0], w[1], rad),
                            ArtworkGeom::Segment {
                                a: try_i32_pt(w[0])?,
                                b: try_i32_pt(w[1])?,
                                width: eff_w_i32,
                            },
                        );
                    }
                }
                GraphicShape::Circle {
                    center,
                    radius_nm,
                    width_nm,
                    filled,
                } => {
                    let r = radius_nm + (width_nm + 1) / 2;
                    push_art(
                        &mut per_layer_art,
                        &mut artwork_graphic_idx,
                        CopperAabb::from_bounds((
                            [center[0] - r, center[1] - r],
                            [center[0] + r, center[1] + r],
                        )),
                        ArtworkGeom::Circle {
                            center: i32_pt(*center),
                            radius: *radius_nm as i32,
                            width: *width_nm as i32,
                            filled: *filled,
                        },
                    );
                }
                GraphicShape::Polygon {
                    outer,
                    holes,
                    width_nm,
                    filled,
                } => {
                    if *filled {
                        push_art(
                            &mut per_layer_art,
                            &mut artwork_graphic_idx,
                            ring_aabb(outer),
                            ArtworkGeom::FilledPolygon {
                                graphic_idx: g_idx as u32,
                            },
                        );
                    }
                    let rad = (*width_nm + 1) / 2;
                    for ring in std::iter::once(outer).chain(holes.iter()) {
                        let n = ring.len();
                        for i in 0..n {
                            let a = ring[i];
                            let b = ring[(i + 1) % n];
                            push_art(
                                &mut per_layer_art,
                                &mut artwork_graphic_idx,
                                seg_aabb(a, b, rad),
                                ArtworkGeom::Segment {
                                    a: i32_pt(a),
                                    b: i32_pt(b),
                                    width: *width_nm as i32,
                                },
                            );
                        }
                    }
                }
            }
        }
        let artwork_spatial = per_layer_art
            .into_iter()
            .map(StaticSpatialIndex::new)
            .collect();

        let mut per_layer_keepouts: Vec<Vec<(u32, u32, CopperAabb, KeepoutGeom)>> =
            vec![Vec::new(); layer_count];
        for (ra_idx, ra) in input.rule_areas.iter().enumerate() {
            if !ra.no_tracks && !ra.no_vias && !ra.no_zone_fills {
                continue;
            }
            let polys = std::iter::once((0u32, &ra.outer)).chain(
                ra.extra_polygons
                    .iter()
                    .enumerate()
                    .map(|(ei, ep)| (u32::try_from(ei + 1).expect("bounded poly idx"), &ep.outer)),
            );
            for (poly_idx, outer) in polys {
                let aabb = ring_aabb(outer);
                let geom = KeepoutGeom {
                    rule_area_idx: u32::try_from(ra_idx).expect("bounded rule area count"),
                    poly_idx,
                    no_tracks: ra.no_tracks,
                    no_vias: ra.no_vias,
                    no_zone_fills: ra.no_zone_fills,
                };
                for lname in &ra.layers {
                    if let Some(l_idx) = layer_names.iter().position(|l| l == lname) {
                        let ord = u32::try_from(per_layer_keepouts[l_idx].len())
                            .expect("bounded keepout count");
                        per_layer_keepouts[l_idx].push((ord, 0u32, aabb, geom));
                    }
                }
            }
        }
        let keepout_spatial = per_layer_keepouts
            .into_iter()
            .map(StaticSpatialIndex::new)
            .collect();

        let mut per_layer_zone_fills: Vec<Vec<(u32, u32, CopperAabb, ZoneFillGeom)>> =
            vec![Vec::new(); layer_count];
        for (z_idx, z) in input.zones.iter().enumerate() {
            if z.refillable || !z.filled {
                continue;
            }
            for lf in &z.layer_fills {
                let Some(l_idx) = layer_names.iter().position(|l| l == &lf.layer) else {
                    continue;
                };
                for (p_idx, poly) in lf.polygons.iter().enumerate() {
                    let ord = u32::try_from(per_layer_zone_fills[l_idx].len())
                        .expect("bounded zone fill count");
                    per_layer_zone_fills[l_idx].push((
                        ord,
                        z.net,
                        ring_aabb(&poly.outer),
                        ZoneFillGeom {
                            zone_idx: u32::try_from(z_idx).expect("bounded zone count"),
                            poly_idx: u32::try_from(p_idx).expect("bounded zone fill poly count"),
                            refillable: false,
                        },
                    ));
                }
            }
        }
        let zone_fill_spatial = per_layer_zone_fills
            .into_iter()
            .map(StaticSpatialIndex::new)
            .collect();

        let spoke_corridors = zones::plan_spoke_corridors(&input, &layer_names, &effective_rules);
        let mut per_layer_corridors: Vec<Vec<(u32, u32, CopperAabb, SpokeCorridorGeom)>> =
            vec![Vec::new(); layer_count];
        for (c_idx, sc) in spoke_corridors.iter().enumerate() {
            let ord = u32::try_from(per_layer_corridors[sc.layer_idx].len())
                .expect("bounded spoke corridor count");
            per_layer_corridors[sc.layer_idx].push((
                ord,
                sc.net,
                ring_aabb(&sc.verts),
                SpokeCorridorGeom {
                    corridor_idx: u32::try_from(c_idx).expect("bounded spoke corridor index"),
                },
            ));
        }
        let spoke_corridor_spatial = per_layer_corridors
            .into_iter()
            .map(StaticSpatialIndex::new)
            .collect();

        let mut mask_items: [Vec<(u32, u32, CopperAabb, MaskGeom)>; 2] = [Vec::new(), Vec::new()];
        let mut mask_aperture_idx: [Vec<usize>; 2] = [Vec::new(), Vec::new()];
        let mask_aabbs: Vec<CopperAabb> = input
            .mask_apertures
            .iter()
            .map(|m| CopperAabb::from_bounds(m.copper_bounds()))
            .collect();
        let mut mask_static_nets: Vec<StaticMaskOccupancy> = input
            .mask_apertures
            .iter()
            .zip(mask_aabbs.iter())
            .map(|(m, &m_aabb)| compute_static_mask_occupancy(&input, &layer_names, m, m_aabb))
            .collect();
        let (mask_group_id, mask_groups) = group_mask_components(
            &input.mask_apertures,
            &mask_aabbs,
            &mut mask_static_nets,
            input.rules.solder_mask_min_width_nm,
        );
        let mut max_mask_expansion = 0i64;
        for (m_idx, m) in input.mask_apertures.iter().enumerate() {
            let occ = mask_static_nets[m_idx];
            let m_slot = if m.layer == "F.Mask" {
                0
            } else if m.layer == "B.Mask" {
                1
            } else {
                continue;
            };
            max_mask_expansion = max_mask_expansion.max(m.expansion_nm);
            let m_net = match occ {
                StaticMaskOccupancy::Single(k) => k,
                StaticMaskOccupancy::Empty | StaticMaskOccupancy::Multi => 0,
            };
            let exp = m.expansion_nm as i32;
            let mut push_mask = |aabb: CopperAabb, geom: MaskGeom| {
                let ord =
                    u32::try_from(mask_aperture_idx[m_slot].len()).expect("bounded mask count");
                mask_aperture_idx[m_slot].push(m_idx);
                mask_items[m_slot].push((ord, m_net, aabb, geom));
            };
            match &m.shape {
                GraphicShape::Segment {
                    start,
                    end,
                    width_nm,
                } => {
                    push_mask(
                        seg_aabb(*start, *end, (*width_nm + 1) / 2),
                        MaskGeom::Segment {
                            a: i32_pt(*start),
                            b: i32_pt(*end),
                            width: *width_nm as i32,
                            expansion: exp,
                        },
                    );
                }
                GraphicShape::Arc {
                    start,
                    mid,
                    end,
                    width_nm,
                } => {
                    let (chords, sag) = geometry::arc_to_conservative_chords(*start, *mid, *end);
                    let eff_w = width_nm + 2 * sag;
                    let eff_w_i32 = try_i32(eff_w)?;
                    let rad = (eff_w + 1) / 2;
                    for w in chords.windows(2) {
                        push_mask(
                            seg_aabb(w[0], w[1], rad),
                            MaskGeom::Segment {
                                a: try_i32_pt(w[0])?,
                                b: try_i32_pt(w[1])?,
                                width: eff_w_i32,
                                expansion: exp,
                            },
                        );
                    }
                }
                GraphicShape::Circle {
                    center,
                    radius_nm,
                    width_nm,
                    filled,
                } => {
                    let r = radius_nm + (width_nm + 1) / 2;
                    push_mask(
                        CopperAabb::from_bounds((
                            [center[0] - r, center[1] - r],
                            [center[0] + r, center[1] + r],
                        )),
                        MaskGeom::Circle {
                            center: i32_pt(*center),
                            radius: *radius_nm as i32,
                            width: *width_nm as i32,
                            expansion: exp,
                            filled: *filled,
                        },
                    );
                }
                GraphicShape::Polygon {
                    outer,
                    holes,
                    width_nm,
                    filled,
                } => {
                    if *filled {
                        push_mask(
                            ring_aabb(outer),
                            MaskGeom::FilledPolygon {
                                mask_idx: m_idx as u32,
                                expansion: exp,
                            },
                        );
                    }
                    let rad = (*width_nm + 1) / 2;
                    for ring in std::iter::once(outer).chain(holes.iter()) {
                        let n = ring.len();
                        for i in 0..n {
                            let a = ring[i];
                            let b = ring[(i + 1) % n];
                            push_mask(
                                seg_aabb(a, b, rad),
                                MaskGeom::Segment {
                                    a: i32_pt(a),
                                    b: i32_pt(b),
                                    width: *width_nm as i32,
                                    expansion: exp,
                                },
                            );
                        }
                    }
                }
            }
        }
        let [mask_f, mask_b] = mask_items;
        let mask_spatial = [
            StaticSpatialIndex::new(mask_f),
            StaticSpatialIndex::new(mask_b),
        ];

        let copper = CopperStore::new_multilayer(
            std::mem::take(&mut input.tracks),
            std::mem::take(&mut input.vias),
            layer_names.clone(),
            reserved,
            &input.source_id,
            base_items,
        );
        Ok(Self {
            input,
            layer_names,
            effective_rules,
            max_pad_local_clearance_nm,
            max_zone_local_clearance_nm,
            copper,
            pad_spatial,
            hole_spatial,
            outline_spatial,
            artwork_spatial,
            artwork_graphic_idx,
            keepout_spatial,
            zone_fill_spatial,
            spoke_corridor_spatial,
            spoke_corridors,
            mask_spatial,
            mask_aperture_idx,
            mask_static_nets,
            mask_aabbs,
            mask_group_id,
            mask_groups,
            max_mask_expansion,
            work,
            last_negotiation_stats: None,
            last_block_generated: std::cell::Cell::new(false),
            prefer_higher_g: false,
            delay_profile_layers: std::collections::BTreeMap::new(),
        })
    }
    /// Group routing operations atomically. `Ok` commits this scope; `Err`
    /// or a panic unwinding through it rolls it back. Inner commits remain
    /// part of the parent's undo history. Panics are not caught or converted.
    /// Abort, process termination and allocation failure are not recovered.
    ///
    /// The borrowed view cannot escape (there are no reusable checkpoint tokens):
    /// ```compile_fail
    /// use kicad_route_engine::{Engine, TransactionError};
    /// fn escape(engine: &mut Engine) {
    ///     let mut saved = None;
    ///     let _: Result<(), TransactionError<()>> = engine.transaction(|tx| {
    ///         saved = Some(tx);
    ///         Ok(())
    ///     });
    ///     saved.unwrap().route_all().unwrap();
    /// }
    /// ```
    ///
    /// An ancestor cannot be used out of order while its child is borrowed:
    /// ```compile_fail
    /// use kicad_route_engine::{Engine, model::Failure};
    /// fn out_of_order(engine: &mut Engine) {
    ///     let _ = engine.transaction(|parent| {
    ///         parent.transaction(|child| {
    ///             parent.route(1, 100)?;
    ///             child.route(2, 100)?;
    ///             Ok::<_, Failure>(())
    ///         })
    ///     });
    /// }
    /// ```
    ///
    /// Views from independent engines cannot be swapped across their scopes:
    /// ```compile_fail
    /// use kicad_route_engine::{Engine, TransactionError};
    /// fn swap_scopes(left: &mut Engine, right: &mut Engine) {
    ///     let _ = left.transaction(|a| {
    ///         right.transaction(|b| {
    ///             std::mem::swap(a, b);
    ///             Ok::<_, ()>(())
    ///         })
    ///     });
    /// }
    /// ```
    pub fn transaction<T, E>(
        &mut self,
        operation: impl FnOnce(&mut Transaction<'_>) -> Result<T, E>,
    ) -> Result<T, TransactionError<E>> {
        let outer = self.work.start();
        if let Err(e) = self.copper.begin(&self.work) {
            self.work.end(outer);
            return Err(TransactionError::Limit(e));
        }
        let prev_prefer_higher_g = self.prefer_higher_g;
        let mut scope = Scope {
            engine: self,
            committed: false,
            outer,
            prev_prefer_higher_g,
        };
        let result = operation(&mut Transaction {
            engine: scope.engine,
        });
        if result.is_ok() {
            scope.engine.copper.commit();
            scope.committed = true;
        }
        result.map_err(TransactionError::Operation)
    }
    pub fn work_used(&self) -> u64 {
        self.work.used()
    }
    pub fn limits(&self) -> Limits {
        self.work.limits
    }
    pub fn copper(&self) -> &CopperStore {
        &self.copper
    }
    pub fn last_negotiation_stats(&self) -> Option<&negotiate::NegotiationStats> {
        self.last_negotiation_stats.as_ref()
    }
    pub(crate) fn set_last_negotiation_stats(
        &mut self,
        stats: Option<negotiate::NegotiationStats>,
    ) {
        self.last_negotiation_stats = stats;
    }
    pub(crate) fn charge_work(&self, units: u64) -> Result<(), Failure> {
        self.work.charge(units)
    }
    pub(crate) fn input(&self) -> &Input {
        &self.input
    }
    pub(crate) fn effective_rules(&self) -> &EffectiveRules {
        &self.effective_rules
    }
    pub(crate) fn work(&self) -> &budget::WorkBudget {
        &self.work
    }
    pub(crate) fn remove_generated_net(&mut self, net: u32) -> Result<(), Failure> {
        self.copper.remove_net(net, &self.work)
    }
    pub(crate) fn clear_last_block_generated(&self) {
        self.last_block_generated.set(false);
    }
    pub(crate) fn set_last_block_generated(&self, v: bool) {
        self.last_block_generated.set(v);
    }
    pub(crate) fn last_block_generated(&self) -> bool {
        self.last_block_generated.get()
    }
    pub(crate) fn prefer_higher_g(&self) -> bool {
        self.prefer_higher_g
    }
    pub(crate) fn set_prefer_higher_g(&mut self, v: bool) -> bool {
        std::mem::replace(&mut self.prefer_higher_g, v)
    }
    /// A standalone query gets its own finite work scope; inside a transaction
    /// it consumes that transaction's remaining work. Geometry is not mutated.
    pub fn legal_edge(&self, net: u32, a: Point, b: Point) -> Result<bool, Failure> {
        self.legal_edge_on(net, 0, a, b)
    }

    pub fn legal_edge_on_layer(
        &self,
        net: u32,
        layer: &str,
        a: Point,
        b: Point,
    ) -> Result<bool, Failure> {
        let Some(idx) = self.layer_index(layer) else {
            return Ok(false);
        };
        self.legal_edge_on(net, idx, a, b)
    }

    pub(crate) fn layer_count(&self) -> usize {
        self.layer_names.len()
    }

    pub(crate) fn layer_name(&self, idx: usize) -> &str {
        &self.layer_names[idx]
    }

    pub(crate) fn layer_index(&self, name: &str) -> Option<usize> {
        self.layer_names.iter().position(|l| l == name)
    }

    pub(crate) fn all_layers_slice(&self) -> &[String] {
        &self.layer_names
    }

    pub(crate) fn pad_layers_owned(&self, pad: &Pad) -> Vec<String> {
        if pad.layers.is_empty() {
            vec![self.input.layer.clone()]
        } else {
            pad.layers.clone()
        }
    }

    pub(crate) fn legal_edge_on(
        &self,
        net: u32,
        layer_idx: usize,
        a: Point,
        b: Point,
    ) -> Result<bool, Failure> {
        self.last_block_generated.set(false);
        let _scope = self.work.scope();
        self.work.charge(1)?;
        let layer_name = &self.layer_names[layer_idx];
        if self.effective_rules.disallow_track_on(net, layer_name)
            || self
                .delay_profile_layers
                .get(&net)
                .is_some_and(|allowed| !allowed.contains(layer_name))
        {
            return Ok(false);
        }
        let r = self.input.rules;
        let width_nm = if net == 0 {
            r.width_nm
        } else {
            match self.effective_rules.track_width_for(net, layer_name) {
                Ok(w) => w,
                Err(e) if e.status == Status::Unroutable => return Ok(false),
                Err(e) => return Err(e),
            }
        };
        let layer_slice = std::slice::from_ref(layer_name);
        let edge_clr =
            self.effective_rules
                .edge_clearance_for(net, DruItemKind::Track, layer_slice);
        let bounds = self.input.bounds;
        let margin = i128::from(width_nm) + 2 * i128::from(edge_clr);
        for p in [a, b] {
            for (i, value) in p.iter().enumerate() {
                if 2 * i128::from(*value) < 2 * i128::from(bounds.min[i]) + margin
                    || 2 * i128::from(*value) > 2 * i128::from(bounds.max[i]) - margin
                {
                    return Ok(false);
                }
            }
        }
        if let Some(outline) = &self.input.board_outline {
            self.work.charge(1)?;
            if !geometry::point_in_board_outline(a, outline)
                || !geometry::point_in_board_outline(b, outline)
            {
                return Ok(false);
            }
            let edge_swept = SweptAabb::swept(a, b, width_nm, edge_clr);
            let edge_flow =
                self.outline_spatial
                    .visit_matching(edge_swept, None, &self.work, |_, edge| {
                        self.work.charge(1)?;
                        if geometry::segments_near(
                            a,
                            b,
                            [i64::from(edge.a[0]), i64::from(edge.a[1])],
                            [i64::from(edge.b[0]), i64::from(edge.b[1])],
                            margin,
                            false,
                        ) {
                            return Ok(ControlFlow::Break(()));
                        }
                        Ok(ControlFlow::Continue(()))
                    })?;
            if edge_flow.is_break() {
                return Ok(false);
            }
        }
        let ko_swept = SweptAabb::swept(a, b, width_nm, 0);
        let ko_flow = self.keepout_spatial[layer_idx].visit_matching(
            ko_swept,
            None,
            &self.work,
            |_, kg| {
                self.work.charge(1)?;
                if kg.no_tracks {
                    let ra = &self.input.rule_areas[kg.rule_area_idx as usize];
                    let (outer, holes) = if kg.poly_idx == 0 {
                        (&ra.outer, &ra.holes)
                    } else {
                        let ep = &ra.extra_polygons[kg.poly_idx as usize - 1];
                        (&ep.outer, &ep.holes)
                    };
                    if geometry::track_near_rule_area_poly(a, b, width_nm, outer, holes) {
                        return Ok(ControlFlow::Break(()));
                    }
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
        if ko_flow.is_break() {
            return Ok(false);
        }

        let max_zone_query_clr = self
            .effective_rules
            .max_pair_clearance_nm
            .max(self.max_zone_local_clearance_nm);
        let zone_swept = SweptAabb::swept(a, b, width_nm, max_zone_query_clr);
        let zone_flow = self.zone_fill_spatial[layer_idx].visit_matching(
            zone_swept,
            Some(net),
            &self.work,
            |_, zg| {
                self.work.charge(1)?;
                if zg.refillable {
                    return Ok(ControlFlow::Continue(()));
                }
                let z = &self.input.zones[zg.zone_idx as usize];
                let Some(lf) = z.layer_fills.iter().find(|lf| lf.layer == *layer_name) else {
                    return Ok(ControlFlow::Continue(()));
                };
                let poly = &lf.polygons[zg.poly_idx as usize];
                let clr = self.effective_rules.pair_clearance(
                    net,
                    DruItemKind::Track,
                    z.net,
                    DruItemKind::Zone,
                    layer_name,
                    (z.local_clearance_nm > 0).then_some(z.local_clearance_nm),
                );
                if geometry::track_near_filled_polygon(
                    a,
                    b,
                    width_nm,
                    clr,
                    &poly.outer,
                    &poly.holes,
                    false,
                ) {
                    return Ok(ControlFlow::Break(()));
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
        if zone_flow.is_break() {
            return Ok(false);
        }

        let spoke_flow = self.spoke_corridor_spatial[layer_idx].visit_matching(
            zone_swept,
            Some(net),
            &self.work,
            |_, sg| {
                self.work.charge(1)?;
                let sc = &self.spoke_corridors[sg.corridor_idx as usize];
                let clr = self.effective_rules.pair_clearance(
                    net,
                    DruItemKind::Track,
                    sc.net,
                    DruItemKind::Zone,
                    layer_name,
                    (sc.local_clearance_nm > 0).then_some(sc.local_clearance_nm),
                );
                if geometry::track_near_convex_poly(a, b, width_nm, clr, &sc.verts, 0, false) {
                    return Ok(ControlFlow::Break(()));
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
        if spoke_flow.is_break() {
            return Ok(false);
        }

        let mask_pad_clr = if layer_name == "F.Cu" || layer_name == "B.Cu" {
            r.solder_mask_expansion_nm.max(0) + r.solder_mask_to_copper_clearance_nm.max(0)
        } else {
            0
        };
        let max_pad_query_clr = self
            .effective_rules
            .max_pair_clearance_nm
            .max(self.max_pad_local_clearance_nm)
            .max(mask_pad_clr);
        let pad_swept = SweptAabb::swept(a, b, width_nm, max_pad_query_clr);
        let pad_flow = self.pad_spatial[layer_idx].visit_matching(
            pad_swept,
            Some(net),
            &self.work,
            |ordinal, core| {
                let pad = &self.input.pads[ordinal as usize];
                self.work.charge(pad.shape.parts().len() as u64)?;
                let pad_clr = self
                    .effective_rules
                    .pair_clearance(
                        net,
                        DruItemKind::Track,
                        pad.net,
                        DruItemKind::Pad,
                        layer_name,
                        pad.local_clearance_nm,
                    )
                    .max(mask_pad_clr);
                let hit = match *core {
                    PadCore::Box { lo, hi, radius } => geometry::track_near_core(
                        a,
                        b,
                        width_nm,
                        pad_clr,
                        [i64::from(lo[0]), i64::from(lo[1])],
                        [i64::from(hi[0]), i64::from(hi[1])],
                        i64::from(radius),
                        false,
                    ),
                    PadCore::Box2x { lo2, hi2, radius2 } => geometry::track_near_core_2x(
                        a,
                        b,
                        width_nm,
                        pad_clr,
                        [i64::from(lo2[0]), i64::from(lo2[1])],
                        [i64::from(hi2[0]), i64::from(hi2[1])],
                        i64::from(radius2),
                        false,
                    ),
                    PadCore::Capsule {
                        a: sa,
                        b: sb,
                        width,
                    } => geometry::segments_near(
                        a,
                        b,
                        [i64::from(sa[0]), i64::from(sa[1])],
                        [i64::from(sb[0]), i64::from(sb[1])],
                        i128::from(width_nm) + i128::from(width) + 2 * i128::from(pad_clr),
                        false,
                    ),
                    PadCore::General => {
                        geometry::track_near_pad(a, b, width_nm, pad_clr, pad, false)
                    }
                };
                if hit {
                    return Ok(ControlFlow::Break(()));
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
        if pad_flow.is_break() {
            return Ok(false);
        }
        let swept = SweptAabb::swept(a, b, width_nm, self.effective_rules.max_pair_clearance_nm);
        let track_flow = self.copper.visit_layer_candidates_with_net_filtered(
            layer_idx,
            swept,
            Some(net),
            &self.work,
            |other_net, t| {
                let clr = self.effective_rules.pair_clearance(
                    net,
                    DruItemKind::Track,
                    other_net,
                    DruItemKind::Track,
                    layer_name,
                    None,
                );
                if geometry::segments_near(
                    a,
                    b,
                    t.start,
                    t.end,
                    i128::from(width_nm) + i128::from(t.width_nm) + 2 * i128::from(clr),
                    false,
                ) {
                    return Ok(ControlFlow::Break(()));
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
        if track_flow.is_break() {
            self.last_block_generated
                .set(self.copper.last_hit_generated());
            return Ok(false);
        }

        let layer_bit = 1u32 << layer_idx;
        let max_via_query_clr = self
            .effective_rules
            .max_pair_clearance_nm
            .max(self.effective_rules.max_hole_clearance_nm)
            .max(r.same_net_hole_clearance_nm);
        let via_net_filter = if r.same_net_hole_clearance_nm > 0 {
            None
        } else {
            Some(net)
        };
        let via_swept = SweptAabb::swept(a, b, width_nm, max_via_query_clr);
        let via_flow = self.copper.visit_via_candidates_filtered(
            via_swept,
            via_net_filter,
            &self.work,
            |via, v_mask| {
                if (v_mask & layer_bit) == 0 {
                    return Ok(ControlFlow::Continue(()));
                }
                if via.net == net && net != 0 {
                    if r.same_net_hole_clearance_nm == 0 {
                        return Ok(ControlFlow::Continue(()));
                    }
                    let seg = Track {
                        id: String::new(),
                        net,
                        start: a,
                        end: b,
                        width_nm,
                        locked: false,
                        layer: layer_name.clone(),
                    };
                    if geometry::track_connects_via(&seg, via) {
                        return Ok(ControlFlow::Continue(()));
                    }
                    if geometry::point_near_segment(
                        via.at,
                        a,
                        b,
                        i128::from(width_nm)
                            + i128::from(via.drill_nm)
                            + 2 * i128::from(r.same_net_hole_clearance_nm),
                        false,
                    ) {
                        return Ok(ControlFlow::Break(()));
                    }
                    return Ok(ControlFlow::Continue(()));
                }
                let s_idx = self.layer_index(&via.start_layer).unwrap_or(0);
                let e_idx = self.layer_index(&via.end_layer).unwrap_or(s_idx);
                let via_layers = &self.layer_names[s_idx.min(e_idx)..=s_idx.max(e_idx)];
                let copper_clr = self.effective_rules.pair_clearance_on_layer(
                    (net, DruItemKind::Track, layer_slice),
                    (via.net, DruItemKind::Via, via_layers),
                    layer_name,
                    None,
                );
                if geometry::point_near_segment(
                    via.at,
                    a,
                    b,
                    i128::from(width_nm) + i128::from(via.diameter_nm) + 2 * i128::from(copper_clr),
                    false,
                ) {
                    return Ok(ControlFlow::Break(()));
                }
                let hole_clr = self.effective_rules.pair_hole_clearance(
                    net,
                    DruItemKind::Track,
                    layer_slice,
                    via.net,
                    DruItemKind::Via,
                    via_layers,
                );
                if geometry::point_near_segment(
                    via.at,
                    a,
                    b,
                    i128::from(width_nm) + i128::from(via.drill_nm) + 2 * i128::from(hole_clr),
                    false,
                ) {
                    return Ok(ControlFlow::Break(()));
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
        if via_flow.is_break() {
            self.last_block_generated
                .set(self.copper.last_hit_generated());
            return Ok(false);
        }

        let hole_net_filter = if r.same_net_hole_clearance_nm > 0 {
            None
        } else {
            Some(net)
        };
        let max_hole_clr = self
            .effective_rules
            .max_hole_clearance_nm
            .max(r.same_net_hole_clearance_nm);
        let hole_swept = SweptAabb::swept(a, b, width_nm, max_hole_clr);
        let holes_len = self.input.holes.len();
        let hole_flow = self.hole_spatial.visit_matching(
            hole_swept,
            hole_net_filter,
            &self.work,
            |ordinal, drill| {
                self.work.charge(1)?;
                let idx = ordinal as usize;
                let (entry_net, maybe_pad) = if idx < holes_len {
                    (self.input.holes[idx].net, None)
                } else {
                    let p = &self.input.pads[idx - holes_len];
                    (p.net, Some(p))
                };
                let clr = if entry_net == net && net != 0 {
                    if r.same_net_hole_clearance_nm == 0 {
                        return Ok(ControlFlow::Continue(()));
                    }
                    if let Some(pad) = maybe_pad {
                        if pad.layers.is_empty() || pad.layers.iter().any(|l| l == layer_name) {
                            let seg = Track {
                                id: String::new(),
                                net,
                                start: a,
                                end: b,
                                width_nm,
                                locked: false,
                                layer: layer_name.clone(),
                            };
                            if geometry::track_connects_pad(&seg, pad) {
                                return Ok(ControlFlow::Continue(()));
                            }
                        }
                    }
                    r.same_net_hole_clearance_nm
                } else if let Some(pad) = maybe_pad {
                    let p_layers = self.pad_layers_owned(pad);
                    self.effective_rules.pair_hole_clearance(
                        net,
                        DruItemKind::Track,
                        layer_slice,
                        entry_net,
                        DruItemKind::Pad,
                        &p_layers,
                    )
                } else {
                    self.effective_rules.pair_hole_clearance(
                        net,
                        DruItemKind::Track,
                        layer_slice,
                        entry_net,
                        DruItemKind::Hole,
                        &self.layer_names,
                    )
                };
                let hit = match *drill {
                    DrillGeom::Circle { center, diameter } => geometry::point_near_segment(
                        [i64::from(center[0]), i64::from(center[1])],
                        a,
                        b,
                        i128::from(width_nm) + i128::from(diameter) + 2 * i128::from(clr),
                        false,
                    ),
                    DrillGeom::Capsule {
                        a: da,
                        b: db,
                        width,
                    } => geometry::segments_near(
                        a,
                        b,
                        [i64::from(da[0]), i64::from(da[1])],
                        [i64::from(db[0]), i64::from(db[1])],
                        i128::from(width_nm) + i128::from(width) + 2 * i128::from(clr),
                        false,
                    ),
                };
                if hit {
                    return Ok(ControlFlow::Break(()));
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
        if hole_flow.is_break() {
            return Ok(false);
        }
        if !self.edge_clears_artwork(net, layer_idx, a, b, width_nm)? {
            return Ok(false);
        }
        self.edge_clears_mask(net, layer_idx, a, b, width_nm)
    }

    pub(crate) fn edge_clears_artwork(
        &self,
        net: u32,
        layer_idx: usize,
        a: Point,
        b: Point,
        width_nm: i64,
    ) -> Result<bool, Failure> {
        let layer_name = &self.layer_names[layer_idx];
        let swept = SweptAabb::swept(a, b, width_nm, self.effective_rules.max_pair_clearance_nm);
        let art_flow = self.artwork_spatial[layer_idx].visit_matching(
            swept,
            Some(net),
            &self.work,
            |ordinal, geom| {
                self.work.charge(1)?;
                let g_idx = self.artwork_graphic_idx[layer_idx][ordinal as usize];
                let g_net = self.input.graphics[g_idx].net;
                let clr = self.effective_rules.pair_clearance(
                    net,
                    DruItemKind::Track,
                    g_net,
                    DruItemKind::Graphic,
                    layer_name,
                    None,
                );
                let hit = match *geom {
                    ArtworkGeom::Segment {
                        a: sa,
                        b: sb,
                        width,
                    } => geometry::segments_near(
                        a,
                        b,
                        [i64::from(sa[0]), i64::from(sa[1])],
                        [i64::from(sb[0]), i64::from(sb[1])],
                        i128::from(width_nm) + i128::from(width) + 2 * i128::from(clr),
                        false,
                    ),
                    ArtworkGeom::Circle {
                        center,
                        radius,
                        width,
                        filled,
                    } => geometry::track_near_circle(
                        a,
                        b,
                        width_nm,
                        clr,
                        [i64::from(center[0]), i64::from(center[1])],
                        i64::from(radius),
                        i64::from(width),
                        filled,
                        false,
                    ),
                    ArtworkGeom::FilledPolygon { graphic_idx } => {
                        if let GraphicShape::Polygon { outer, holes, .. } =
                            &self.input.graphics[graphic_idx as usize].shape
                        {
                            geometry::point_in_polygon_with_holes(a, outer, holes)
                                || geometry::point_in_polygon_with_holes(b, outer, holes)
                        } else {
                            false
                        }
                    }
                };
                if hit {
                    return Ok(ControlFlow::Break(()));
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
        Ok(!art_flow.is_break())
    }

    fn geom_hits_mask(
        mask_apertures: &[MaskAperture],
        a: Point,
        b: Point,
        width_nm: i64,
        to_cu: i64,
        geom: &MaskGeom,
    ) -> bool {
        match *geom {
            MaskGeom::Segment {
                a: sa,
                b: sb,
                width,
                expansion,
            } => {
                let clr = to_cu + i64::from(expansion);
                geometry::segments_near(
                    a,
                    b,
                    [i64::from(sa[0]), i64::from(sa[1])],
                    [i64::from(sb[0]), i64::from(sb[1])],
                    i128::from(width_nm) + i128::from(width) + 2 * i128::from(clr),
                    false,
                )
            }
            MaskGeom::Circle {
                center,
                radius,
                width,
                expansion,
                filled,
            } => {
                let clr = to_cu + i64::from(expansion);
                geometry::track_near_circle(
                    a,
                    b,
                    width_nm,
                    clr,
                    [i64::from(center[0]), i64::from(center[1])],
                    i64::from(radius),
                    i64::from(width),
                    filled,
                    false,
                )
            }
            MaskGeom::FilledPolygon { mask_idx, .. } => {
                if let GraphicShape::Polygon { outer, holes, .. } =
                    &mask_apertures[mask_idx as usize].shape
                {
                    geometry::point_in_polygon_with_holes(a, outer, holes)
                        || geometry::point_in_polygon_with_holes(b, outer, holes)
                } else {
                    false
                }
            }
        }
    }

    fn generated_other_net_touches_mask(
        &self,
        net: u32,
        layer_idx: usize,
        m_idx: usize,
    ) -> Result<bool, Failure> {
        let net_filter = (net != 0).then_some(net);
        let layer_bit = 1u32 << layer_idx;
        for &member_idx in &self.mask_groups[self.mask_group_id[m_idx]] {
            let m = &self.input.mask_apertures[member_idx];
            let aabb = self.mask_aabbs[member_idx];
            let query = SweptAabb {
                min: [i128::from(aabb.min[0]), i128::from(aabb.min[1])],
                max: [i128::from(aabb.max[0]), i128::from(aabb.max[1])],
            };
            let trk_flow = self.copper.visit_generated_layer_candidates(
                layer_idx,
                query,
                net_filter,
                &self.work,
                |other_net, t| {
                    if other_net == net && net != 0 {
                        return Ok(ControlFlow::Continue(()));
                    }
                    if geometry::track_near_mask(t.start, t.end, t.width_nm, 0, m, false) {
                        return Ok(ControlFlow::Break(()));
                    }
                    Ok(ControlFlow::Continue(()))
                },
            )?;
            if trk_flow.is_break() {
                return Ok(true);
            }
            let via_flow = self.copper.visit_generated_via_candidates(
                query,
                net_filter,
                &self.work,
                |via, v_mask| {
                    if (v_mask & layer_bit) == 0 || (via.net == net && net != 0) {
                        return Ok(ControlFlow::Continue(()));
                    }
                    if geometry::track_near_mask(via.at, via.at, via.diameter_nm, 0, m, false) {
                        return Ok(ControlFlow::Break(()));
                    }
                    Ok(ControlFlow::Continue(()))
                },
            )?;
            if via_flow.is_break() {
                return Ok(true);
            }
        }
        Ok(false)
    }

    pub(crate) fn generated_nets_touching_mask_group(
        &self,
        net_filter: Option<u32>,
        layer_idx: usize,
        group_id: usize,
        out: &mut Vec<u32>,
    ) -> Result<(), Failure> {
        out.clear();
        let Some(members) = self.mask_groups.get(group_id) else {
            return Ok(());
        };
        let layer_bit = 1u32 << layer_idx;
        for &member_idx in members {
            let m = &self.input.mask_apertures[member_idx];
            let aabb = self.mask_aabbs[member_idx];
            let query = SweptAabb {
                min: [i128::from(aabb.min[0]), i128::from(aabb.min[1])],
                max: [i128::from(aabb.max[0]), i128::from(aabb.max[1])],
            };
            let _ = self.copper.visit_generated_layer_candidates(
                layer_idx,
                query,
                net_filter,
                &self.work,
                |other_net, t| {
                    if other_net != 0
                        && geometry::track_near_mask(t.start, t.end, t.width_nm, 0, m, false)
                    {
                        out.push(other_net);
                    }
                    Ok(ControlFlow::Continue(()))
                },
            )?;
            let _ = self.copper.visit_generated_via_candidates(
                query,
                net_filter,
                &self.work,
                |via, v_mask| {
                    if via.net != 0
                        && (v_mask & layer_bit) != 0
                        && geometry::track_near_mask(via.at, via.at, via.diameter_nm, 0, m, false)
                    {
                        out.push(via.net);
                    }
                    Ok(ControlFlow::Continue(()))
                },
            )?;
        }
        out.sort_unstable();
        out.dedup();
        Ok(())
    }

    pub(crate) fn edge_clears_mask(
        &self,
        net: u32,
        layer_idx: usize,
        a: Point,
        b: Point,
        width_nm: i64,
    ) -> Result<bool, Failure> {
        let layer_name = &self.layer_names[layer_idx];
        let ms = if layer_name == "F.Cu" {
            0usize
        } else if layer_name == "B.Cu" {
            1usize
        } else {
            return Ok(true);
        };
        if self.mask_aperture_idx[ms].is_empty() {
            return Ok(true);
        }
        let to_cu = self.input.rules.solder_mask_to_copper_clearance_nm.max(0);
        let mask_swept = SweptAabb::swept(a, b, width_nm, to_cu + self.max_mask_expansion);
        let mask_flow = self.mask_spatial[ms].visit_matching(
            mask_swept,
            Some(net),
            &self.work,
            |ordinal, geom| {
                self.work.charge(1)?;
                if !Self::geom_hits_mask(&self.input.mask_apertures, a, b, width_nm, to_cu, geom) {
                    return Ok(ControlFlow::Continue(()));
                }
                if to_cu > 0 {
                    return Ok(ControlFlow::Break(()));
                }
                let m_idx = self.mask_aperture_idx[ms][ordinal as usize];
                let allowed_static = match self.mask_static_nets[m_idx] {
                    StaticMaskOccupancy::Empty => true,
                    StaticMaskOccupancy::Single(n) => net != 0 && n == net,
                    StaticMaskOccupancy::Multi => false,
                };
                if !allowed_static {
                    return Ok(ControlFlow::Break(()));
                }
                if self.generated_other_net_touches_mask(net, layer_idx, m_idx)? {
                    self.last_block_generated.set(true);
                    return Ok(ControlFlow::Break(()));
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
        Ok(!mask_flow.is_break())
    }

    pub(crate) fn via_clears_mask_layer(
        &self,
        net: u32,
        l_idx: usize,
        at: Point,
        diam: i64,
    ) -> Result<bool, Failure> {
        self.edge_clears_mask(net, l_idx, at, at, diam)
    }

    pub(crate) fn mask_apertures_hit(
        &self,
        layer_idx: usize,
        a: Point,
        b: Point,
        width_nm: i64,
        out: &mut Vec<usize>,
    ) -> Result<(), Failure> {
        out.clear();
        if self.input.mask_apertures.is_empty() {
            return Ok(());
        }
        let layer_name = &self.layer_names[layer_idx];
        let ms = if layer_name == "F.Cu" {
            0usize
        } else if layer_name == "B.Cu" {
            1usize
        } else {
            return Ok(());
        };
        if self.mask_aperture_idx[ms].is_empty() {
            return Ok(());
        }
        let mask_swept = SweptAabb::swept(a, b, width_nm, self.max_mask_expansion);
        let _ = self.mask_spatial[ms].visit_matching(
            mask_swept,
            None,
            &self.work,
            |ordinal, geom| {
                self.work.charge(1)?;
                if Self::geom_hits_mask(&self.input.mask_apertures, a, b, width_nm, 0, geom) {
                    let m_idx = self.mask_aperture_idx[ms][ordinal as usize];
                    out.push(self.mask_group_id[m_idx]);
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
        Ok(())
    }

    pub(crate) fn edge_clears_same_net_vias(
        &self,
        net: u32,
        layer_idx: usize,
        a: Point,
        b: Point,
        prior_vias: &[search::PlacedViaKey],
    ) -> Result<bool, Failure> {
        let r = self.input.rules;
        if r.same_net_hole_clearance_nm <= 0 || net == 0 || prior_vias.is_empty() {
            return Ok(true);
        }
        let layer_name = &self.layer_names[layer_idx];
        let width_nm = match self.effective_rules.track_width_for(net, layer_name) {
            Ok(w) => w,
            Err(e) if e.status == Status::Unroutable => return Ok(false),
            Err(e) => return Err(e),
        };
        let seg = Track {
            id: String::new(),
            net,
            start: a,
            end: b,
            width_nm,
            locked: false,
            layer: layer_name.clone(),
        };
        for pv in prior_vias {
            self.work.charge(1)?;
            if layer_idx < pv.start_layer as usize || layer_idx > pv.end_layer as usize {
                continue;
            }
            let kind = match pv.kind {
                0 => ViaKind::Through,
                1 => ViaKind::BlindBuried,
                _ => ViaKind::Micro,
            };
            let pv_layers = &self.layer_names[pv.start_layer as usize..=pv.end_layer as usize];
            let (diam, drill) =
                self.effective_rules
                    .via_dims_for(net, kind == ViaKind::Micro, pv_layers)?;
            let synth = Via {
                id: String::new(),
                net,
                at: pv.at,
                diameter_nm: diam,
                drill_nm: drill,
                start_layer: self.layer_names[pv.start_layer as usize].clone(),
                end_layer: self.layer_names[pv.end_layer as usize].clone(),
                kind,
                locked: false,
            };
            if geometry::track_connects_via(&seg, &synth) {
                continue;
            }
            if geometry::point_near_segment(
                pv.at,
                a,
                b,
                i128::from(width_nm)
                    + i128::from(drill)
                    + 2 * i128::from(r.same_net_hole_clearance_nm),
                false,
            ) {
                return Ok(false);
            }
        }
        Ok(true)
    }

    pub(crate) fn legal_via(
        &self,
        net: u32,
        at: Point,
        start_idx: usize,
        end_idx: usize,
        kind: ViaKind,
        prior_vias: &[search::PlacedViaKey],
    ) -> Result<Option<(i64, i64)>, Failure> {
        self.last_block_generated.set(false);
        let _scope = self.work.scope();
        self.work.charge(1)?;
        let l_count = self.layer_names.len();
        if l_count < 2 || start_idx >= end_idx || end_idx >= l_count {
            return Ok(None);
        }
        match kind {
            ViaKind::Through => {
                if start_idx != 0 || end_idx + 1 != l_count {
                    return Ok(None);
                }
            }
            ViaKind::BlindBuried => {
                if !self.effective_rules.allow_blind_buried_vias
                    || (start_idx == 0 && end_idx + 1 == l_count)
                {
                    return Ok(None);
                }
            }
            ViaKind::Micro => {
                if !self.effective_rules.allow_microvias
                    || end_idx != start_idx + 1
                    || (start_idx != 0 && end_idx + 1 != l_count)
                {
                    return Ok(None);
                }
            }
        }
        if let Some(max_vc) = self.effective_rules.net(net).max_via_count {
            if self.copper.via_count_for_net(net) + prior_vias.len() + 1 > max_vc as usize {
                return Ok(None);
            }
        }
        let span_layers = &self.layer_names[start_idx..=end_idx];
        if self.effective_rules.disallow_via_on(net, span_layers) {
            return Ok(None);
        }
        let (diam, drill) =
            match self
                .effective_rules
                .via_dims_for(net, kind == ViaKind::Micro, span_layers)
            {
                Ok(dims) => dims,
                Err(e) if e.status == Status::Unroutable => return Ok(None),
                Err(e) => return Err(e),
            };
        let edge_clr = self
            .effective_rules
            .edge_clearance_for(net, DruItemKind::Via, span_layers);
        let bounds = self.input.bounds;
        let margin = i128::from(diam) + 2 * i128::from(edge_clr);
        for (i, value) in at.iter().enumerate() {
            if 2 * i128::from(*value) < 2 * i128::from(bounds.min[i]) + margin
                || 2 * i128::from(*value) > 2 * i128::from(bounds.max[i]) - margin
            {
                return Ok(None);
            }
        }
        if let Some(outline) = &self.input.board_outline {
            self.work.charge(1)?;
            if !geometry::point_in_board_outline(at, outline) {
                return Ok(None);
            }
            let edge_swept = SweptAabb::swept(at, at, diam, edge_clr);
            let edge_flow =
                self.outline_spatial
                    .visit_matching(edge_swept, None, &self.work, |_, edge| {
                        self.work.charge(1)?;
                        if geometry::segments_near(
                            at,
                            at,
                            [i64::from(edge.a[0]), i64::from(edge.a[1])],
                            [i64::from(edge.b[0]), i64::from(edge.b[1])],
                            margin,
                            false,
                        ) {
                            return Ok(ControlFlow::Break(()));
                        }
                        Ok(ControlFlow::Continue(()))
                    })?;
            if edge_flow.is_break() {
                return Ok(None);
            }
        }
        let my_mask = span_mask(start_idx, end_idx);
        let my_drill = DrillShape::Circle { diameter_nm: drill };
        let r = self.input.rules;
        for pv in prior_vias {
            self.work.charge(1)?;
            let pv_mask = span_mask(pv.start_layer as usize, pv.end_layer as usize);
            if (my_mask & pv_mask) == 0 {
                continue;
            }
            let pv_layers = &self.layer_names[pv.start_layer as usize..=pv.end_layer as usize];
            let (pv_diam_nm, pv_drill_nm) =
                self.effective_rules
                    .via_dims_for(net, pv.kind == 2, pv_layers)?;
            let h2h = self.effective_rules.pair_hole_to_hole(
                net,
                DruItemKind::Via,
                span_layers,
                net,
                DruItemKind::Via,
                pv_layers,
            );
            if geometry::holes_near(
                at,
                &my_drill,
                pv.at,
                &DrillShape::Circle {
                    diameter_nm: pv_drill_nm,
                },
                h2h,
                false,
            ) {
                return Ok(None);
            }
            if r.same_net_hole_clearance_nm > 0 && net != 0 {
                let pv_kind = match pv.kind {
                    0 => ViaKind::Through,
                    1 => ViaKind::BlindBuried,
                    _ => ViaKind::Micro,
                };
                let synth = Via {
                    id: String::new(),
                    net,
                    at,
                    diameter_nm: diam,
                    drill_nm: drill,
                    start_layer: self.layer_names[start_idx].clone(),
                    end_layer: self.layer_names[end_idx].clone(),
                    kind,
                    locked: false,
                };
                let pv_via = Via {
                    id: String::new(),
                    net,
                    at: pv.at,
                    diameter_nm: pv_diam_nm,
                    drill_nm: pv_drill_nm,
                    start_layer: self.layer_names[pv.start_layer as usize].clone(),
                    end_layer: self.layer_names[pv.end_layer as usize].clone(),
                    kind: pv_kind,
                    locked: false,
                };
                if !geometry::vias_connect(&synth, &pv_via) {
                    let via_copper = DrillShape::Circle { diameter_nm: diam };
                    let pv_copper = DrillShape::Circle {
                        diameter_nm: pv_diam_nm,
                    };
                    let pv_drill = DrillShape::Circle {
                        diameter_nm: pv_drill_nm,
                    };
                    if geometry::holes_near(
                        at,
                        &via_copper,
                        pv.at,
                        &pv_drill,
                        r.same_net_hole_clearance_nm,
                        false,
                    ) || geometry::holes_near(
                        at,
                        &my_drill,
                        pv.at,
                        &pv_copper,
                        r.same_net_hole_clearance_nm,
                        false,
                    ) {
                        return Ok(None);
                    }
                }
            }
        }
        let mask_pad_clr =
            r.solder_mask_expansion_nm.max(0) + r.solder_mask_to_copper_clearance_nm.max(0);
        let any_outer_layer = (start_idx..=end_idx)
            .any(|idx| matches!(self.layer_names[idx].as_str(), "F.Cu" | "B.Cu"));
        let max_copper_query_clr = self
            .effective_rules
            .max_pair_clearance_nm
            .max(self.max_pad_local_clearance_nm)
            .max(self.max_zone_local_clearance_nm)
            .max(self.effective_rules.max_hole_clearance_nm)
            .max(if any_outer_layer { mask_pad_clr } else { 0 });

        for l_idx in start_idx..=end_idx {
            let layer_name = &self.layer_names[l_idx];
            let layer_slice = std::slice::from_ref(layer_name);
            let layer_mask_pad_clr = if layer_name == "F.Cu" || layer_name == "B.Cu" {
                mask_pad_clr
            } else {
                0
            };
            let ko_swept = SweptAabb::swept(at, at, diam, 0);
            let ko_flow = self.keepout_spatial[l_idx].visit_matching(
                ko_swept,
                None,
                &self.work,
                |_, kg| {
                    self.work.charge(1)?;
                    if kg.no_vias {
                        let ra = &self.input.rule_areas[kg.rule_area_idx as usize];
                        let (outer, holes) = if kg.poly_idx == 0 {
                            (&ra.outer, &ra.holes)
                        } else {
                            let ep = &ra.extra_polygons[kg.poly_idx as usize - 1];
                            (&ep.outer, &ep.holes)
                        };
                        if geometry::via_near_rule_area_poly(at, diam, outer, holes) {
                            return Ok(ControlFlow::Break(()));
                        }
                    }
                    Ok(ControlFlow::Continue(()))
                },
            )?;
            if ko_flow.is_break() {
                return Ok(None);
            }
            let zone_swept = SweptAabb::swept(
                at,
                at,
                diam,
                self.effective_rules
                    .max_pair_clearance_nm
                    .max(self.max_zone_local_clearance_nm),
            );
            let zone_flow = self.zone_fill_spatial[l_idx].visit_matching(
                zone_swept,
                Some(net),
                &self.work,
                |_, zg| {
                    self.work.charge(1)?;
                    if zg.refillable {
                        return Ok(ControlFlow::Continue(()));
                    }
                    let z = &self.input.zones[zg.zone_idx as usize];
                    let Some(lf) = z.layer_fills.iter().find(|lf| lf.layer == *layer_name) else {
                        return Ok(ControlFlow::Continue(()));
                    };
                    let poly = &lf.polygons[zg.poly_idx as usize];
                    let clr = self.effective_rules.pair_clearance_on_layer(
                        (net, DruItemKind::Via, span_layers),
                        (z.net, DruItemKind::Zone, &[]),
                        layer_name,
                        (z.local_clearance_nm > 0).then_some(z.local_clearance_nm),
                    );
                    if geometry::track_near_filled_polygon(
                        at,
                        at,
                        diam,
                        clr,
                        &poly.outer,
                        &poly.holes,
                        false,
                    ) {
                        return Ok(ControlFlow::Break(()));
                    }
                    Ok(ControlFlow::Continue(()))
                },
            )?;
            if zone_flow.is_break() {
                return Ok(None);
            }
            let spoke_flow = self.spoke_corridor_spatial[l_idx].visit_matching(
                zone_swept,
                Some(net),
                &self.work,
                |_, sg| {
                    self.work.charge(1)?;
                    let sc = &self.spoke_corridors[sg.corridor_idx as usize];
                    let clr = self.effective_rules.pair_clearance_on_layer(
                        (net, DruItemKind::Via, span_layers),
                        (sc.net, DruItemKind::Zone, &[]),
                        layer_name,
                        (sc.local_clearance_nm > 0).then_some(sc.local_clearance_nm),
                    );
                    if geometry::track_near_convex_poly(at, at, diam, clr, &sc.verts, 0, false) {
                        return Ok(ControlFlow::Break(()));
                    }
                    Ok(ControlFlow::Continue(()))
                },
            )?;
            if spoke_flow.is_break() {
                return Ok(None);
            }
            let disallow_smd_via_in_pad = self
                .input
                .board_design_settings
                .as_ref()
                .is_some_and(|bds| !(bds.cap_vias && bds.fill_vias));
            let pad_net_filter =
                if (r.same_net_hole_clearance_nm > 0 || disallow_smd_via_in_pad) && net != 0 {
                    None
                } else {
                    Some(net)
                };
            let pad_swept = SweptAabb::swept(
                at,
                at,
                diam.max(drill),
                max_copper_query_clr.max(r.same_net_hole_clearance_nm),
            );
            let pad_flow = self.pad_spatial[l_idx].visit_matching(
                pad_swept,
                pad_net_filter,
                &self.work,
                |ordinal, core| {
                    self.work.charge(1)?;
                    let pad = &self.input.pads[ordinal as usize];
                    let check_core = |w: i64, clr: i64| -> Result<bool, Failure> {
                        Ok(match *core {
                            PadCore::Box { lo, hi, radius } => geometry::track_near_core(
                                at,
                                at,
                                w,
                                clr,
                                [i64::from(lo[0]), i64::from(lo[1])],
                                [i64::from(hi[0]), i64::from(hi[1])],
                                i64::from(radius),
                                false,
                            ),
                            PadCore::Box2x { lo2, hi2, radius2 } => geometry::track_near_core_2x(
                                at,
                                at,
                                w,
                                clr,
                                [i64::from(lo2[0]), i64::from(lo2[1])],
                                [i64::from(hi2[0]), i64::from(hi2[1])],
                                i64::from(radius2),
                                false,
                            ),
                            PadCore::Capsule {
                                a: sa,
                                b: sb,
                                width,
                            } => geometry::segments_near(
                                at,
                                at,
                                [i64::from(sa[0]), i64::from(sa[1])],
                                [i64::from(sb[0]), i64::from(sb[1])],
                                i128::from(w) + i128::from(width) + 2 * i128::from(clr),
                                false,
                            ),
                            PadCore::General => {
                                self.work.charge((pad.shape.parts().len() - 1) as u64)?;
                                geometry::track_near_pad(at, at, w, clr, pad, false)
                            }
                        })
                    };
                    if pad.net == net && net != 0 {
                        if pad.drill.is_none() && disallow_smd_via_in_pad {
                            if check_core(drill, 0)? {
                                return Ok(ControlFlow::Break(()));
                            }
                            let synth = Via {
                                id: String::new(),
                                net,
                                at,
                                diameter_nm: diam,
                                drill_nm: drill,
                                start_layer: self.layer_names[start_idx].clone(),
                                end_layer: self.layer_names[end_idx].clone(),
                                kind,
                                locked: false,
                            };
                            if Copper::Pad(pad).touches_via(
                                &synth,
                                &self.layer_names,
                                &self.work,
                            )? {
                                return Ok(ControlFlow::Break(()));
                            }
                        }
                        if r.same_net_hole_clearance_nm == 0 {
                            return Ok(ControlFlow::Continue(()));
                        }
                        let synth = Via {
                            id: String::new(),
                            net,
                            at,
                            diameter_nm: diam,
                            drill_nm: drill,
                            start_layer: self.layer_names[start_idx].clone(),
                            end_layer: self.layer_names[end_idx].clone(),
                            kind,
                            locked: false,
                        };
                        if !Copper::Pad(pad).touches_via(&synth, &self.layer_names, &self.work)?
                            && check_core(drill, r.same_net_hole_clearance_nm)?
                        {
                            return Ok(ControlFlow::Break(()));
                        }
                        return Ok(ControlFlow::Continue(()));
                    }
                    let pad_clr = self
                        .effective_rules
                        .pair_clearance_on_layer(
                            (net, DruItemKind::Via, span_layers),
                            (pad.net, DruItemKind::Pad, &[]),
                            layer_name,
                            pad.local_clearance_nm,
                        )
                        .max(layer_mask_pad_clr);
                    let p_layers = self.pad_layers_owned(pad);
                    let hole_clr = self.effective_rules.pair_hole_clearance(
                        net,
                        DruItemKind::Via,
                        span_layers,
                        pad.net,
                        DruItemKind::Pad,
                        &p_layers,
                    );
                    if check_core(diam, pad_clr)? || (hole_clr > 0 && check_core(drill, hole_clr)?)
                    {
                        return Ok(ControlFlow::Break(()));
                    }
                    Ok(ControlFlow::Continue(()))
                },
            )?;
            if pad_flow.is_break() {
                return Ok(None);
            }
            let trk_net_filter = if r.same_net_hole_clearance_nm > 0 {
                None
            } else {
                Some(net)
            };
            let trk_swept = SweptAabb::swept(
                at,
                at,
                diam.max(drill),
                max_copper_query_clr.max(r.same_net_hole_clearance_nm),
            );
            let trk_flow = self.copper.visit_layer_candidates_with_net_filtered(
                l_idx,
                trk_swept,
                trk_net_filter,
                &self.work,
                |other_net, t| {
                    if other_net == net && net != 0 {
                        if r.same_net_hole_clearance_nm == 0 {
                            return Ok(ControlFlow::Continue(()));
                        }
                        let synth = Via {
                            id: String::new(),
                            net,
                            at,
                            diameter_nm: diam,
                            drill_nm: drill,
                            start_layer: self.layer_names[start_idx].clone(),
                            end_layer: self.layer_names[end_idx].clone(),
                            kind,
                            locked: false,
                        };
                        let seg = Track {
                            id: String::new(),
                            net: other_net,
                            start: t.start,
                            end: t.end,
                            width_nm: t.width_nm,
                            locked: false,
                            layer: layer_name.clone(),
                        };
                        if geometry::track_connects_via(&seg, &synth) {
                            return Ok(ControlFlow::Continue(()));
                        }
                        if geometry::point_near_segment(
                            at,
                            t.start,
                            t.end,
                            i128::from(drill)
                                + i128::from(t.width_nm)
                                + 2 * i128::from(r.same_net_hole_clearance_nm),
                            false,
                        ) {
                            return Ok(ControlFlow::Break(()));
                        }
                        return Ok(ControlFlow::Continue(()));
                    }
                    let clr = self.effective_rules.pair_clearance_on_layer(
                        (net, DruItemKind::Via, span_layers),
                        (other_net, DruItemKind::Track, layer_slice),
                        layer_name,
                        None,
                    );
                    if geometry::point_near_segment(
                        at,
                        t.start,
                        t.end,
                        i128::from(diam) + i128::from(t.width_nm) + 2 * i128::from(clr),
                        false,
                    ) {
                        return Ok(ControlFlow::Break(()));
                    }
                    let hole_clr = self.effective_rules.pair_hole_clearance(
                        net,
                        DruItemKind::Via,
                        span_layers,
                        other_net,
                        DruItemKind::Track,
                        layer_slice,
                    );
                    if hole_clr > 0
                        && geometry::point_near_segment(
                            at,
                            t.start,
                            t.end,
                            i128::from(drill) + i128::from(t.width_nm) + 2 * i128::from(hole_clr),
                            false,
                        )
                    {
                        return Ok(ControlFlow::Break(()));
                    }
                    Ok(ControlFlow::Continue(()))
                },
            )?;
            if trk_flow.is_break() {
                self.last_block_generated
                    .set(self.copper.last_hit_generated());
                return Ok(None);
            }
            let art_flow = self.artwork_spatial[l_idx].visit_matching(
                trk_swept,
                Some(net),
                &self.work,
                |ordinal, geom| {
                    self.work.charge(1)?;
                    let g_idx = self.artwork_graphic_idx[l_idx][ordinal as usize];
                    let g_net = self.input.graphics[g_idx].net;
                    let clr = self.effective_rules.pair_clearance_on_layer(
                        (net, DruItemKind::Via, span_layers),
                        (g_net, DruItemKind::Graphic, layer_slice),
                        layer_name,
                        None,
                    );
                    let hit = match *geom {
                        ArtworkGeom::Segment {
                            a: sa,
                            b: sb,
                            width,
                        } => geometry::segments_near(
                            at,
                            at,
                            [i64::from(sa[0]), i64::from(sa[1])],
                            [i64::from(sb[0]), i64::from(sb[1])],
                            i128::from(diam) + i128::from(width) + 2 * i128::from(clr),
                            false,
                        ),
                        ArtworkGeom::Circle {
                            center,
                            radius,
                            width,
                            filled,
                        } => geometry::track_near_circle(
                            at,
                            at,
                            diam,
                            clr,
                            [i64::from(center[0]), i64::from(center[1])],
                            i64::from(radius),
                            i64::from(width),
                            filled,
                            false,
                        ),
                        ArtworkGeom::FilledPolygon { graphic_idx } => {
                            if let GraphicShape::Polygon { outer, holes, .. } =
                                &self.input.graphics[graphic_idx as usize].shape
                            {
                                geometry::point_in_polygon_with_holes(at, outer, holes)
                            } else {
                                false
                            }
                        }
                    };
                    if hit {
                        return Ok(ControlFlow::Break(()));
                    }
                    Ok(ControlFlow::Continue(()))
                },
            )?;
            if art_flow.is_break() {
                return Ok(None);
            }
            if !self.via_clears_mask_layer(net, l_idx, at, diam)? {
                return Ok(None);
            }
        }

        let max_hole_clr = self
            .effective_rules
            .max_hole_clearance_nm
            .max(self.effective_rules.max_hole_to_hole_nm)
            .max(r.same_net_hole_clearance_nm);
        let hole_swept = SweptAabb::swept(at, at, diam, max_hole_clr);
        let holes_len = self.input.holes.len();
        let hole_flow = self.hole_spatial.visit_matching(
            hole_swept,
            None,
            &self.work,
            |ordinal, other_drill| {
                self.work.charge(1)?;
                let idx = ordinal as usize;
                let (entry_net, entry_at, entry_shape, maybe_pad) = if idx < holes_len {
                    let h = &self.input.holes[idx];
                    (h.net, h.at, &h.shape, None)
                } else {
                    let p = &self.input.pads[idx - holes_len];
                    (p.net, p.at, p.drill.as_ref().unwrap(), Some(p))
                };
                let (hole_kind, hole_layers) = match maybe_pad {
                    Some(p) => (DruItemKind::Pad, self.pad_layers_owned(p)),
                    None => (DruItemKind::Hole, self.layer_names.clone()),
                };
                // Physical drills (both non-plated/plated Hole and PTH Pad.drill) traverse all layers.
                let h2h = self.effective_rules.pair_hole_to_hole(
                    net,
                    DruItemKind::Via,
                    span_layers,
                    entry_net,
                    hole_kind,
                    &self.layer_names,
                );
                if geometry::holes_near(at, &my_drill, entry_at, entry_shape, h2h, false) {
                    return Ok(ControlFlow::Break(()));
                }
                if entry_net != net || entry_net == 0 {
                    let hole_clr = self.effective_rules.pair_hole_clearance(
                        net,
                        DruItemKind::Via,
                        span_layers,
                        entry_net,
                        hole_kind,
                        &hole_layers,
                    );
                    let via_copper = DrillShape::Circle { diameter_nm: diam };
                    if geometry::holes_near(at, &via_copper, entry_at, entry_shape, hole_clr, false)
                    {
                        return Ok(ControlFlow::Break(()));
                    }
                } else if r.same_net_hole_clearance_nm > 0 {
                    let connects = match maybe_pad {
                        Some(pad) => {
                            let synth = Via {
                                id: String::new(),
                                net,
                                at,
                                diameter_nm: diam,
                                drill_nm: drill,
                                start_layer: self.layer_names[start_idx].clone(),
                                end_layer: self.layer_names[end_idx].clone(),
                                kind,
                                locked: false,
                            };
                            Copper::Pad(pad).touches_via(&synth, &self.layer_names, &self.work)?
                        }
                        None => false,
                    };
                    if !connects {
                        let via_copper = DrillShape::Circle { diameter_nm: diam };
                        if geometry::holes_near(
                            at,
                            &via_copper,
                            entry_at,
                            entry_shape,
                            r.same_net_hole_clearance_nm,
                            false,
                        ) {
                            return Ok(ControlFlow::Break(()));
                        }
                    }
                }
                let _ = other_drill;
                Ok(ControlFlow::Continue(()))
            },
        )?;
        if hole_flow.is_break() {
            return Ok(None);
        }

        let via_swept = SweptAabb::swept(at, at, diam, max_copper_query_clr.max(max_hole_clr));
        let via_flow = self.copper.visit_via_candidates_filtered(
            via_swept,
            None,
            &self.work,
            |ev, ev_mask| {
                if (my_mask & ev_mask) == 0 {
                    return Ok(ControlFlow::Continue(()));
                }
                let ev_s = self.layer_index(&ev.start_layer).unwrap_or(0);
                let ev_e = self.layer_index(&ev.end_layer).unwrap_or(ev_s);
                let ev_layers = &self.layer_names[ev_s..=ev_e];
                let h2h = self.effective_rules.pair_hole_to_hole(
                    net,
                    DruItemKind::Via,
                    span_layers,
                    ev.net,
                    DruItemKind::Via,
                    ev_layers,
                );
                if geometry::holes_near(
                    at,
                    &my_drill,
                    ev.at,
                    &DrillShape::Circle {
                        diameter_nm: ev.drill_nm,
                    },
                    h2h,
                    false,
                ) {
                    return Ok(ControlFlow::Break(()));
                }
                if ev.net != net || ev.net == 0 {
                    for l_idx in start_idx..=end_idx {
                        if (ev_mask & (1u32 << l_idx)) != 0 {
                            let clr = self.effective_rules.pair_clearance_on_layer(
                                (net, DruItemKind::Via, span_layers),
                                (ev.net, DruItemKind::Via, ev_layers),
                                &self.layer_names[l_idx],
                                None,
                            );
                            if geometry::point_near_segment(
                                at,
                                ev.at,
                                ev.at,
                                i128::from(diam) + i128::from(ev.diameter_nm) + 2 * i128::from(clr),
                                false,
                            ) {
                                return Ok(ControlFlow::Break(()));
                            }
                        }
                    }
                    let hole_clr = self.effective_rules.pair_hole_clearance(
                        net,
                        DruItemKind::Via,
                        span_layers,
                        ev.net,
                        DruItemKind::Via,
                        ev_layers,
                    );
                    if geometry::point_near_segment(
                        at,
                        ev.at,
                        ev.at,
                        i128::from(diam) + i128::from(ev.drill_nm) + 2 * i128::from(hole_clr),
                        false,
                    ) || geometry::point_near_segment(
                        at,
                        ev.at,
                        ev.at,
                        i128::from(drill) + i128::from(ev.diameter_nm) + 2 * i128::from(hole_clr),
                        false,
                    ) {
                        return Ok(ControlFlow::Break(()));
                    }
                } else if r.same_net_hole_clearance_nm > 0 {
                    let synth = Via {
                        id: String::new(),
                        net,
                        at,
                        diameter_nm: diam,
                        drill_nm: drill,
                        start_layer: self.layer_names[start_idx].clone(),
                        end_layer: self.layer_names[end_idx].clone(),
                        kind,
                        locked: false,
                    };
                    if !Copper::Via(ev).touches_via(&synth, &self.layer_names, &self.work)?
                        && (geometry::point_near_segment(
                            at,
                            ev.at,
                            ev.at,
                            i128::from(diam)
                                + i128::from(ev.drill_nm)
                                + 2 * i128::from(r.same_net_hole_clearance_nm),
                            false,
                        ) || geometry::point_near_segment(
                            at,
                            ev.at,
                            ev.at,
                            i128::from(drill)
                                + i128::from(ev.diameter_nm)
                                + 2 * i128::from(r.same_net_hole_clearance_nm),
                            false,
                        ))
                    {
                        return Ok(ControlFlow::Break(()));
                    }
                }
                Ok(ControlFlow::Continue(()))
            },
        )?;
        if via_flow.is_break() {
            self.last_block_generated
                .set(self.copper.last_hit_generated());
            return Ok(None);
        }
        Ok(Some((diam, drill)))
    }

    pub(crate) fn components(&self, net: u32) -> Result<Vec<Vec<Copper<'_>>>, Failure> {
        let mut items = Vec::new();
        for p in &self.input.pads {
            self.work.charge(1)?;
            if p.net == net {
                items.push(Copper::Pad(p));
            }
        }
        for t in self.copper.tracks() {
            self.work.charge(1)?;
            if t.net == net {
                items.push(Copper::Track(t));
            }
        }
        for v in self.copper.vias() {
            self.work.charge(1)?;
            if v.net == net {
                items.push(Copper::Via(v));
            }
        }
        for g in &self.input.graphics {
            self.work.charge(1)?;
            if g.net == net && self.layer_index(&g.layer).is_some() {
                items.push(Copper::Graphic(g));
            }
        }
        let mut includes_refillable_fill = false;
        for z in &self.input.zones {
            self.work.charge(1)?;
            if z.net != net || !z.filled {
                continue;
            }
            for lf in &z.layer_fills {
                let Some(l_idx) = self.layer_index(&lf.layer) else {
                    continue;
                };
                for poly in &lf.polygons {
                    self.work.charge(1)?;
                    includes_refillable_fill |= z.refillable;
                    items.push(Copper::ZoneFill {
                        layer_idx: l_idx,
                        zone: z,
                        poly,
                    });
                }
            }
        }
        let mut groups = components::partition(items, &self.layer_names, &self.work)?;
        if includes_refillable_fill && groups.len() > 1 {
            // Source fills supply a few seed vertices. Target fills can accept
            // a legal connection anywhere on their copper, including long edges.
            let mut smallest = (i64::MAX, usize::MAX, usize::MAX);
            for (index, group) in groups.iter().enumerate() {
                self.work.charge(group.len() as u64)?;
                let mut max_fill_area = 0;
                for item in group {
                    if let Copper::ZoneFill { zone, poly, .. } = item {
                        if !zone.refillable {
                            continue;
                        }
                        self.work.charge(poly.outer.len() as u64)?;
                        let (lo, hi) = item.copper_bounds();
                        max_fill_area = max_fill_area.max((hi[0] - lo[0]) * (hi[1] - lo[1]));
                    }
                }
                smallest = smallest.min((max_fill_area, group.len(), index));
            }
            groups[..=smallest.2].rotate_right(1);
        }
        Ok(groups)
    }
    fn route_net(&mut self, net: u32, limit: u64, expanded: &mut u64) -> Result<(), Failure> {
        loop {
            let groups = self.components(net)?;
            let before = groups.len();
            if before <= 1 {
                return Ok(());
            }
            let path =
                search::join_components(self, net, &groups[0], &groups[1..], limit, expanded)?;
            self.insert_path(net, path)?;
            if self.components(net)?.len() >= before {
                return Err(Failure::new(
                    Status::Unroutable,
                    "proposed route did not reduce conservative copper components",
                ));
            }
        }
    }
    pub(crate) fn route_net_negotiated(
        &mut self,
        net: u32,
        limit: u64,
        expanded: &mut u64,
        history: &negotiate::CongestionHistory,
    ) -> Result<(), negotiate::UnitRouteError> {
        let mut budget = negotiate::TrialBudget::fixed(limit);
        self.route_net_negotiated_with_budget(net, &mut budget, expanded, history)
    }

    pub(crate) fn route_net_negotiated_with_budget(
        &mut self,
        net: u32,
        budget: &mut negotiate::TrialBudget<'_>,
        expanded: &mut u64,
        history: &negotiate::CongestionHistory,
    ) -> Result<(), negotiate::UnitRouteError> {
        let had_generated_before = self.copper.has_generated_for_net(net);
        let mut joins_done = 0usize;
        loop {
            let groups = self
                .components(net)
                .map_err(negotiate::UnitRouteError::Fatal)?;
            let before = groups.len();
            if before <= 1 {
                return Ok(());
            }
            let mut frontier = negotiate::BlockedFrontier::new();
            match search::join_components_negotiated_with_budget(
                self,
                net,
                &groups[0],
                &groups[1..],
                budget,
                expanded,
                history,
                &mut frontier,
            ) {
                Ok(path) => match self.insert_path(net, path) {
                    Ok(()) => {
                        joins_done += 1;
                        let after = self
                            .components(net)
                            .map_err(negotiate::UnitRouteError::Fatal)?
                            .len();
                        if after >= before {
                            if joins_done > 0 && !had_generated_before {
                                self.copper
                                    .remove_net(net, &self.work)
                                    .map_err(negotiate::UnitRouteError::Fatal)?;
                            }
                            return Err(negotiate::UnitRouteError::Blocked(frontier));
                        }
                    }
                    Err(e) if e.status == Status::Unroutable => {
                        if joins_done > 0 && !had_generated_before {
                            self.copper
                                .remove_net(net, &self.work)
                                .map_err(negotiate::UnitRouteError::Fatal)?;
                        }
                        return Err(negotiate::UnitRouteError::Blocked(frontier));
                    }
                    Err(e) => return Err(negotiate::UnitRouteError::Fatal(e)),
                },
                Err(e) if e.status == Status::Unroutable => {
                    if joins_done > 0 && !had_generated_before {
                        self.copper
                            .remove_net(net, &self.work)
                            .map_err(negotiate::UnitRouteError::Fatal)?;
                    }
                    return Err(negotiate::UnitRouteError::Blocked(frontier));
                }
                Err(e)
                    if e.status == Status::BudgetExhausted
                        && (budget.aborted
                            || (budget.current_limit < budget.batch_limit
                                && !frontier.is_empty())) =>
                {
                    if joins_done > 0 && !had_generated_before {
                        self.copper
                            .remove_net(net, &self.work)
                            .map_err(negotiate::UnitRouteError::Fatal)?;
                    }
                    return Err(negotiate::UnitRouteError::Blocked(frontier));
                }
                Err(e) => return Err(negotiate::UnitRouteError::Fatal(e)),
            }
        }
    }
    fn insert_path(&mut self, net: u32, path: search::Path) -> Result<(), Failure> {
        let mut raw_tracks: Vec<Track> = Vec::new();
        let mut pending_vias: Vec<(search::PlacedViaKey, Via)> = Vec::new();
        let mut prior_vias: Vec<search::PlacedViaKey> = Vec::new();
        let mut can_coalesce_last = false;

        for edge in path.edges {
            self.work.charge(1)?;
            match edge {
                search::PathEdge::Track { start, end, layer } => {
                    let l_name = &self.layer_names[layer as usize];
                    let width_nm = self.effective_rules.track_width_for(net, l_name)?;
                    if can_coalesce_last {
                        if let Some(last) = raw_tracks.last_mut() {
                            if last.layer == *l_name
                                && last.end == start
                                && ((last.start[0] == last.end[0]
                                    && last.end[0] == end[0]
                                    && last.end[1] != last.start[1]
                                    && (last.end[1] - last.start[1]).signum()
                                        == (end[1] - start[1]).signum())
                                    || (last.start[1] == last.end[1]
                                        && last.end[1] == end[1]
                                        && last.end[0] != last.start[0]
                                        && (last.end[0] - last.start[0]).signum()
                                            == (end[0] - start[0]).signum()))
                            {
                                last.end = end;
                                continue;
                            }
                        }
                    }
                    raw_tracks.push(Track {
                        id: String::new(),
                        net,
                        start,
                        end,
                        width_nm,
                        locked: false,
                        layer: l_name.clone(),
                    });
                    can_coalesce_last = true;
                }
                search::PathEdge::Via(pv) => {
                    can_coalesce_last = false;
                    let kind = match pv.kind {
                        0 => ViaKind::Through,
                        1 => ViaKind::BlindBuried,
                        _ => ViaKind::Micro,
                    };
                    let Some((diam, drill)) = self.legal_via(
                        net,
                        pv.at,
                        pv.start_layer as usize,
                        pv.end_layer as usize,
                        kind,
                        &prior_vias,
                    )?
                    else {
                        return Err(Failure::new(
                            Status::Unroutable,
                            "proposed via failed continuous clearance validation",
                        ));
                    };
                    prior_vias.push(pv);
                    pending_vias.push((
                        pv,
                        Via {
                            id: String::new(),
                            net,
                            at: pv.at,
                            diameter_nm: diam,
                            drill_nm: drill,
                            start_layer: self.layer_names[pv.start_layer as usize].clone(),
                            end_layer: self.layer_names[pv.end_layer as usize].clone(),
                            kind,
                            locked: false,
                        },
                    ));
                }
            }
        }
        let _ = pending_vias.len();
        self.work.charge(raw_tracks.len() as u64)?;
        let mut normalized = Vec::new();
        for t in raw_tracks {
            let l_idx = self.layer_index(&t.layer).expect("validated layer");
            if self.input.rules.same_net_hole_clearance_nm > 0 {
                if t.start == t.end
                    || !self.legal_edge_on(net, l_idx, t.start, t.end)?
                    || !self.edge_clears_same_net_vias(net, l_idx, t.start, t.end, &prior_vias)?
                {
                    return Err(Failure::new(
                        Status::Unroutable,
                        "proposal failed continuous clearance validation",
                    ));
                }
                for piece in self.subtract_collinear_overlaps_legal(
                    net,
                    l_idx,
                    &t,
                    &normalized,
                    &prior_vias,
                )? {
                    if piece.start == piece.end {
                        return Err(Failure::new(
                            Status::Unroutable,
                            "proposal failed continuous clearance validation",
                        ));
                    }
                    normalized.push(piece);
                }
            } else {
                for piece in self.subtract_collinear_overlaps(&t, &normalized)? {
                    if piece.start == piece.end
                        || !self.legal_edge_on(net, l_idx, piece.start, piece.end)?
                    {
                        return Err(Failure::new(
                            Status::Unroutable,
                            "proposal failed continuous clearance validation",
                        ));
                    }
                    normalized.push(piece);
                }
            }
        }
        for t in normalized {
            self.copper.insert(t, &self.work)?;
        }
        for (_, v) in pending_vias {
            self.copper.insert_via(v, &self.work)?;
        }
        Ok(())
    }
    fn subtract_collinear_overlaps_legal(
        &self,
        net: u32,
        layer_idx: usize,
        seg: &Track,
        accepted: &[Track],
        prior_vias: &[search::PlacedViaKey],
    ) -> Result<Vec<Track>, Failure> {
        let pieces = self.subtract_collinear_overlaps(seg, accepted)?;
        if self.input.rules.same_net_hole_clearance_nm == 0 {
            return Ok(pieces);
        }
        let mut all_legal = true;
        for piece in &pieces {
            if piece.start == piece.end
                || !self.legal_edge_on(net, layer_idx, piece.start, piece.end)?
                || !self.edge_clears_same_net_vias(
                    net,
                    layer_idx,
                    piece.start,
                    piece.end,
                    prior_vias,
                )?
            {
                all_legal = false;
                break;
            }
        }
        if all_legal {
            return Ok(pieces);
        }
        let axis = usize::from(seg.start[0] == seg.end[0]);
        let ortho = seg.start[1 - axis];
        let s_lo = seg.start[axis].min(seg.end[axis]);
        let s_hi = seg.start[axis].max(seg.end[axis]);
        let mut overlaps: Vec<(i64, i64)> = Vec::new();
        for existing in self.copper.tracks().chain(accepted.iter()) {
            self.work.charge(1)?;
            if existing.net == seg.net
                && existing.layer == seg.layer
                && existing.width_nm >= seg.width_nm
                && existing.start[1 - axis] == ortho
                && existing.end[1 - axis] == ortho
            {
                let e_lo = existing.start[axis].min(existing.end[axis]);
                let e_hi = existing.start[axis].max(existing.end[axis]);
                let c_lo = e_lo.max(s_lo);
                let c_hi = e_hi.min(s_hi);
                if c_lo < c_hi {
                    overlaps.push((c_lo, c_hi));
                }
            }
        }
        overlaps.sort_unstable();
        let mut merged: Vec<(i64, i64)> = Vec::new();
        for (c_lo, c_hi) in overlaps {
            self.work.charge(1)?;
            if let Some(last) = merged.last_mut() {
                if c_lo <= last.1 {
                    last.1 = last.1.max(c_hi);
                    continue;
                }
            }
            merged.push((c_lo, c_hi));
        }
        let point = |coord: i64| {
            let mut p = [0, 0];
            p[axis] = coord;
            p[1 - axis] = ortho;
            p
        };
        let mut keep = vec![true; merged.len()];
        let mut spans: Vec<(i64, i64)>;
        loop {
            let mut cursor = s_lo;
            spans = Vec::new();
            for (idx, &(c_lo, c_hi)) in merged.iter().enumerate() {
                if !keep[idx] {
                    continue;
                }
                if cursor < c_lo {
                    spans.push((cursor, c_lo));
                }
                if c_hi > cursor {
                    cursor = c_hi;
                }
            }
            if cursor < s_hi {
                spans.push((cursor, s_hi));
            }
            let mut dropped = false;
            for &(a, b) in &spans {
                self.work.charge(1)?;
                if !self.legal_edge_on(net, layer_idx, point(a), point(b))?
                    || !self.edge_clears_same_net_vias(
                        net,
                        layer_idx,
                        point(a),
                        point(b),
                        prior_vias,
                    )?
                {
                    if a > s_lo {
                        if let Some(idx) = merged
                            .iter()
                            .enumerate()
                            .position(|(idx, &(_, hi))| keep[idx] && hi == a)
                        {
                            keep[idx] = false;
                            dropped = true;
                            break;
                        }
                    }
                    if b < s_hi {
                        if let Some(idx) = merged
                            .iter()
                            .enumerate()
                            .position(|(idx, &(lo, _))| keep[idx] && lo == b)
                        {
                            keep[idx] = false;
                            dropped = true;
                            break;
                        }
                    }
                    return Ok(vec![seg.clone()]);
                }
            }
            if !dropped {
                break;
            }
        }
        if seg.end[axis] < seg.start[axis] {
            self.work.charge(spans.len() as u64)?;
            spans.reverse();
            for span in &mut spans {
                *span = (span.1, span.0);
            }
        }
        self.work.charge(spans.len() as u64)?;
        Ok(spans
            .into_iter()
            .map(|(a, b)| Track {
                id: String::new(),
                net: seg.net,
                start: point(a),
                end: point(b),
                width_nm: seg.width_nm,
                locked: false,
                layer: seg.layer.clone(),
            })
            .collect())
    }
    fn subtract_collinear_overlaps(
        &self,
        seg: &Track,
        accepted: &[Track],
    ) -> Result<Vec<Track>, Failure> {
        let axis = if seg.start[1] == seg.end[1] {
            0
        } else if seg.start[0] == seg.end[0] {
            1
        } else {
            return Ok(vec![seg.clone()]);
        };
        let ortho = seg.start[1 - axis];
        let s_lo = seg.start[axis].min(seg.end[axis]);
        let s_hi = seg.start[axis].max(seg.end[axis]);
        let mut overlaps: Vec<(i64, i64)> = Vec::new();
        for existing in self.copper.tracks().chain(accepted.iter()) {
            self.work.charge(1)?;
            if existing.net == seg.net
                && existing.layer == seg.layer
                && existing.width_nm >= seg.width_nm
                && existing.start[1 - axis] == ortho
                && existing.end[1 - axis] == ortho
            {
                let e_lo = existing.start[axis].min(existing.end[axis]);
                let e_hi = existing.start[axis].max(existing.end[axis]);
                let c_lo = e_lo.max(s_lo);
                let c_hi = e_hi.min(s_hi);
                if c_lo < c_hi {
                    overlaps.push((c_lo, c_hi));
                }
            }
        }
        if overlaps.is_empty() {
            return Ok(vec![seg.clone()]);
        }
        self.work.charge(overlaps.len() as u64)?;
        overlaps.sort_unstable();
        let mut cursor = s_lo;
        let mut spans: Vec<(i64, i64)> = Vec::new();
        for (c_lo, c_hi) in overlaps {
            self.work.charge(1)?;
            if cursor < c_lo {
                spans.push((cursor, c_lo));
            }
            if c_hi > cursor {
                cursor = c_hi;
            }
        }
        if cursor < s_hi {
            spans.push((cursor, s_hi));
        }
        if seg.end[axis] < seg.start[axis] {
            self.work.charge(spans.len() as u64)?;
            spans.reverse();
            for span in &mut spans {
                *span = (span.1, span.0);
            }
        }
        let point = |coord: i64| {
            let mut p = [0, 0];
            p[axis] = coord;
            p[1 - axis] = ortho;
            p
        };
        self.work.charge(spans.len() as u64)?;
        Ok(spans
            .into_iter()
            .map(|(a, b)| Track {
                id: String::new(),
                net: seg.net,
                start: point(a),
                end: point(b),
                width_nm: seg.width_nm,
                locked: false,
                layer: seg.layer.clone(),
            })
            .collect())
    }
    fn operation<T>(
        &mut self,
        operation: impl FnOnce(&mut Transaction<'_>) -> Result<T, Failure>,
    ) -> Result<T, Failure> {
        self.transaction(operation).map_err(|e| {
            let mut e = Failure::from(e);
            e.work_used = self.work.used();
            e
        })
    }
    pub fn route_all(&mut self) -> Result<u64, Failure> {
        self.last_negotiation_stats = None;
        self.delay_profile_layers.clear();
        let res = self.operation(|tx| {
            let diff_state = diff_pair::plan_diff_pairs(tx.engine)?;
            tx.engine.delay_profile_layers =
                diff_pair::delay_profile_layers(tx.engine, &diff_state);
            let mut expanded = 0u64;
            if diff_state.plan.pairs.is_empty() {
                let mut pinned_nets = diff_state.routed_pair_nets.clone();
                pinned_nets.extend(diff_state.protected_nets.iter().copied());
                let mut adapter = negotiate::SingleEndedAdapter::new(
                    pinned_nets,
                    diff_state.active_single_ended_nets.clone(),
                );
                let _stats = negotiate::solve_with_adapter(tx, &mut adapter, &mut expanded)?;
            } else {
                let mut adapter = diff_pair::CompositeRouteAdapter::new(diff_state.clone());
                let _stats = negotiate::solve_with_adapter(tx, &mut adapter, &mut expanded)?;
            }
            diff_pair::apply_length_matching_in_tx(tx.engine, &diff_state).map_err(|mut e| {
                if e.expanded_states == 0 {
                    e.expanded_states = expanded;
                }
                e
            })?;
            for n in &tx.engine.input.nets {
                if n.id == 0 {
                    continue;
                }
                let is_protected = diff_state.protected_nets.contains(&n.id);
                let is_pair_net = diff_state.routed_pair_nets.contains(&n.id);
                if !is_protected && !is_pair_net {
                    continue;
                }
                if !diff_state
                    .plan
                    .active_nets
                    .as_ref()
                    .is_none_or(|s| s.contains(&n.id))
                {
                    continue;
                }
                if tx.engine.components(n.id)?.len() > 1 {
                    return Err(Failure::new(
                        Status::Unroutable,
                        if is_protected {
                            format!("net {} ({:?}) is incomplete under --protect", n.id, n.name)
                        } else {
                            format!(
                                "differential pair net {} ({:?}) has disconnected copper",
                                n.id, n.name
                            )
                        },
                    ));
                }
            }
            Ok(expanded)
        });
        self.delay_profile_layers.clear();
        res
    }
    /// Route one known nonzero net, preserving already-connected copper.
    /// The expansion limit applies to this operation, not its enclosing scope.
    pub fn route(&mut self, net: u32, max_expansions: u64) -> Result<u64, Failure> {
        self.route_selected(net, max_expansions, false)
    }
    /// Remove only generated copper for a net, then route again. Failure
    /// restores removed objects, index and ID allocator, even inside a parent.
    pub fn reroute(&mut self, net: u32, max_expansions: u64) -> Result<u64, Failure> {
        self.route_selected(net, max_expansions, true)
    }
    fn route_selected(
        &mut self,
        net: u32,
        max_expansions: u64,
        replace: bool,
    ) -> Result<u64, Failure> {
        self.operation(|tx| {
            let engine = &mut *tx.engine;
            engine.work.charge(engine.input.nets.len() as u64)?;
            if net == 0
                || max_expansions == 0
                || max_expansions > MAX_EXPANSIONS
                || !engine.input.nets.iter().any(|n| n.id == net)
            {
                return Err(Failure::new(
                    Status::Invalid,
                    "routing needs a known nonzero net and budget 1..=1000000",
                ));
            }
            if replace {
                engine.copper.remove_net(net, &engine.work)?;
            }
            let mut expanded = 0;
            engine
                .route_net(net, max_expansions, &mut expanded)
                .map_err(|mut e| {
                    e.expanded_states = expanded;
                    scoped_routing_failure(e, net)
                })?;
            Ok(expanded)
        })
    }
}
pub(crate) fn scoped_routing_failure(mut e: Failure, net: u32) -> Failure {
    if e.status == Status::Unroutable {
        e.reason = format!("net {net}: {}; failure for fixed net order/current copper, fixed grid and pad-center/track-endpoint seeds; not proof of global infeasibility", e.reason);
    }
    e
}
impl Output {
    pub fn failure(source_id: String, e: Failure) -> Self {
        Self {
            version: RESULT_VERSION,
            request_sha256: None,
            source_id,
            status: e.status,
            added_tracks: Vec::new(),
            added_vias: Vec::new(),
            removed_track_ids: Vec::new(),
            expanded_states: e.expanded_states,
            work_used: e.work_used,
            reason: Some(e.reason),
        }
    }
}
/// Route a typed request, identified by its full compact serde_json serialization.
/// Original JSON formatting and omitted defaults cannot be recovered from Input.
pub fn run(input: Input) -> Output {
    let identity = input_request_sha256(&input);
    let mut result = run_unidentified(input, None);
    result.request_sha256 = Some(identity);
    result
}
fn run_unidentified(input: Input, control: Option<budget::RouteControl>) -> Output {
    let source = input.source_id.clone();
    let mut engine = match Engine::new_with_control(input, control) {
        Ok(e) => e,
        Err(e) => return Output::failure(source, e),
    };
    match engine.route_all() {
        Ok(expanded) => Output {
            version: RESULT_VERSION,
            request_sha256: None,
            source_id: source,
            status: Status::Complete,
            added_tracks: engine.copper.generated().cloned().collect(),
            added_vias: engine.copper.generated_vias().cloned().collect(),
            removed_track_ids: Vec::new(),
            expanded_states: expanded,
            work_used: engine.work_used(),
            reason: None,
        },
        Err(e) => Output::failure(source, e),
    }
}
pub fn run_json(bytes: &[u8]) -> Output {
    run_json_with_control(bytes, None)
}
fn run_json_with_control(bytes: &[u8], control: Option<budget::RouteControl>) -> Output {
    if bytes.len() > identity::MAX_REQUEST_BYTES {
        return Output::failure(
            String::new(),
            Failure::new(Status::AdmissionExceeded, "JSON input exceeds 16 MiB"),
        );
    }
    // Deserialize directly into the contract: going through Value first would
    // erase duplicate keys, including a second `tracks` field hiding copper.
    let mut result = match serde_json::from_slice::<Input>(bytes) {
        Ok(input) => run_unidentified(input, control),
        Err(error) => {
            let source = serde_json::from_slice::<serde_json::Value>(bytes)
                .ok()
                .and_then(|v| {
                    v.get("source_id")
                        .and_then(|s| s.as_str())
                        .map(str::to_owned)
                })
                .unwrap_or_default();
            let message = error.to_string();
            let status = if message.contains("unknown field") || message.contains("unknown variant")
            {
                Status::Unsupported
            } else {
                Status::Invalid
            };
            Output::failure(source, Failure::new(status, message))
        }
    };
    result.request_sha256 = request_sha256(bytes);
    result
}
