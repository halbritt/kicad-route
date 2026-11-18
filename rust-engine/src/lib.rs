pub mod identity;
pub use identity::{input_request_sha256, request_sha256};
mod budget;
mod components;
pub mod geometry;
pub mod model;
pub mod orchestration;
pub mod search;
mod spatial;
pub mod state;
use components::Copper;
use model::*;
use spatial::{
    ArtworkGeom, CopperAabb, DrillGeom, EdgeGeom, MaskGeom, PadCore, StaticSpatialIndex, SweptAabb,
};
use state::CopperStore;
use std::ops::ControlFlow;

pub struct Engine {
    input: Input,
    copper: CopperStore,
    pad_spatial: StaticSpatialIndex<PadCore>,
    hole_spatial: StaticSpatialIndex<DrillGeom>,
    outline_spatial: StaticSpatialIndex<EdgeGeom>,
    artwork_spatial: StaticSpatialIndex<ArtworkGeom>,
    mask_spatial: StaticSpatialIndex<MaskGeom>,
    max_mask_expansion: i64,
    work: budget::WorkBudget,
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
}
impl Drop for Scope<'_> {
    fn drop(&mut self) {
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

impl Engine {
    pub fn new(mut input: Input) -> Result<Self, Failure> {
        input.validate()?;
        let base_items = input.pads.len()
            + input.tracks.len()
            + input.nets.len()
            + input.holes.len()
            + input.graphics.len()
            + input.mask_apertures.len();
        let work = budget::WorkBudget::new(input.limits);
        let reserved = input
            .pads
            .iter()
            .map(|p| p.id.clone())
            .chain(input.tracks.iter().map(|t| t.id.clone()))
            .chain(input.holes.iter().map(|h| h.id.clone()))
            .chain(input.graphics.iter().map(|g| g.id.clone()))
            .chain(input.mask_apertures.iter().map(|m| m.id.clone()))
            .collect();
        let pad_spatial = StaticSpatialIndex::new(input.pads.iter().enumerate().map(|(i, pad)| {
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
                Shape::ConvexPolygon { .. } => PadCore::ConvexPoly,
            };
            (
                u32::try_from(i).expect("bounded input pad count"),
                pad.net,
                CopperAabb::from_bounds(pad.copper_bounds()),
                core,
            )
        }));
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

        let mut artwork_items = Vec::new();
        let mut art_ord = 0u32;
        for (g_idx, g) in input.graphics.iter().enumerate() {
            if g.layer != input.layer {
                continue;
            }
            match &g.shape {
                GraphicShape::Segment {
                    start,
                    end,
                    width_nm,
                } => {
                    artwork_items.push((
                        art_ord,
                        g.net,
                        seg_aabb(*start, *end, (*width_nm + 1) / 2),
                        ArtworkGeom::Segment {
                            a: i32_pt(*start),
                            b: i32_pt(*end),
                            width: *width_nm as i32,
                        },
                    ));
                    art_ord += 1;
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
                        artwork_items.push((
                            art_ord,
                            g.net,
                            seg_aabb(w[0], w[1], rad),
                            ArtworkGeom::Segment {
                                a: try_i32_pt(w[0])?,
                                b: try_i32_pt(w[1])?,
                                width: eff_w_i32,
                            },
                        ));
                        art_ord += 1;
                    }
                }
                GraphicShape::Circle {
                    center,
                    radius_nm,
                    width_nm,
                    filled,
                } => {
                    let r = radius_nm + (width_nm + 1) / 2;
                    artwork_items.push((
                        art_ord,
                        g.net,
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
                    ));
                    art_ord += 1;
                }
                GraphicShape::Polygon {
                    outer,
                    holes,
                    width_nm,
                    filled,
                } => {
                    if *filled {
                        artwork_items.push((
                            art_ord,
                            g.net,
                            ring_aabb(outer),
                            ArtworkGeom::FilledPolygon {
                                graphic_idx: g_idx as u32,
                            },
                        ));
                        art_ord += 1;
                    }
                    let rad = (*width_nm + 1) / 2;
                    for ring in std::iter::once(outer).chain(holes.iter()) {
                        let n = ring.len();
                        for i in 0..n {
                            let a = ring[i];
                            let b = ring[(i + 1) % n];
                            artwork_items.push((
                                art_ord,
                                g.net,
                                seg_aabb(a, b, rad),
                                ArtworkGeom::Segment {
                                    a: i32_pt(a),
                                    b: i32_pt(b),
                                    width: *width_nm as i32,
                                },
                            ));
                            art_ord += 1;
                        }
                    }
                }
            }
        }
        let artwork_spatial = StaticSpatialIndex::new(artwork_items);

        let mut mask_items = Vec::new();
        let mut mask_ord = 0u32;
        let mut max_mask_expansion = 0i64;
        for (m_idx, m) in input.mask_apertures.iter().enumerate() {
            if m.layer != "F.Mask" {
                continue;
            }
            max_mask_expansion = max_mask_expansion.max(m.expansion_nm);
            let m_net = m.net.unwrap_or(0);
            let exp = m.expansion_nm as i32;
            match &m.shape {
                GraphicShape::Segment {
                    start,
                    end,
                    width_nm,
                } => {
                    mask_items.push((
                        mask_ord,
                        m_net,
                        seg_aabb(*start, *end, (*width_nm + 1) / 2),
                        MaskGeom::Segment {
                            a: i32_pt(*start),
                            b: i32_pt(*end),
                            width: *width_nm as i32,
                            expansion: exp,
                        },
                    ));
                    mask_ord += 1;
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
                        mask_items.push((
                            mask_ord,
                            m_net,
                            seg_aabb(w[0], w[1], rad),
                            MaskGeom::Segment {
                                a: try_i32_pt(w[0])?,
                                b: try_i32_pt(w[1])?,
                                width: eff_w_i32,
                                expansion: exp,
                            },
                        ));
                        mask_ord += 1;
                    }
                }
                GraphicShape::Circle {
                    center,
                    radius_nm,
                    width_nm,
                    filled,
                } => {
                    let r = radius_nm + (width_nm + 1) / 2;
                    mask_items.push((
                        mask_ord,
                        m_net,
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
                    ));
                    mask_ord += 1;
                }
                GraphicShape::Polygon {
                    outer,
                    holes,
                    width_nm,
                    filled,
                } => {
                    if *filled {
                        mask_items.push((
                            mask_ord,
                            m_net,
                            ring_aabb(outer),
                            MaskGeom::FilledPolygon {
                                mask_idx: m_idx as u32,
                                expansion: exp,
                            },
                        ));
                        mask_ord += 1;
                    }
                    let rad = (*width_nm + 1) / 2;
                    for ring in std::iter::once(outer).chain(holes.iter()) {
                        let n = ring.len();
                        for i in 0..n {
                            let a = ring[i];
                            let b = ring[(i + 1) % n];
                            mask_items.push((
                                mask_ord,
                                m_net,
                                seg_aabb(a, b, rad),
                                MaskGeom::Segment {
                                    a: i32_pt(a),
                                    b: i32_pt(b),
                                    width: *width_nm as i32,
                                    expansion: exp,
                                },
                            ));
                            mask_ord += 1;
                        }
                    }
                }
            }
        }
        let mask_spatial = StaticSpatialIndex::new(mask_items);

        let copper = CopperStore::new(
            std::mem::take(&mut input.tracks),
            reserved,
            &input.source_id,
            base_items,
        );
        Ok(Self {
            input,
            copper,
            pad_spatial,
            hole_spatial,
            outline_spatial,
            artwork_spatial,
            mask_spatial,
            max_mask_expansion,
            work,
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
        let mut scope = Scope {
            engine: self,
            committed: false,
            outer,
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
    /// A standalone query gets its own finite work scope; inside a transaction
    /// it consumes that transaction's remaining work. Geometry is not mutated.
    pub fn legal_edge(&self, net: u32, a: Point, b: Point) -> Result<bool, Failure> {
        let _scope = self.work.scope();
        self.work.charge(1)?;
        let r = self.input.rules;
        let bounds = self.input.bounds;
        let margin = i128::from(r.width_nm) + 2 * i128::from(r.edge_clearance_nm);
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
            let edge_swept = SweptAabb::swept(a, b, r.width_nm, r.edge_clearance_nm);
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
        let pad_clr = if r.solder_mask_to_copper_clearance_nm > 0 {
            r.clearance_nm
                .max(r.solder_mask_expansion_nm + r.solder_mask_to_copper_clearance_nm)
        } else {
            r.clearance_nm
        };
        let pad_swept = SweptAabb::swept(a, b, r.width_nm, pad_clr);
        let pad_flow = self.pad_spatial.visit_matching(
            pad_swept,
            Some(net),
            &self.work,
            |ordinal, core| {
                self.work.charge(1)?;
                let hit = match *core {
                    PadCore::Box { lo, hi, radius } => geometry::track_near_core(
                        a,
                        b,
                        r.width_nm,
                        pad_clr,
                        [i64::from(lo[0]), i64::from(lo[1])],
                        [i64::from(hi[0]), i64::from(hi[1])],
                        i64::from(radius),
                        false,
                    ),
                    PadCore::Box2x { lo2, hi2, radius2 } => geometry::track_near_core_2x(
                        a,
                        b,
                        r.width_nm,
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
                        i128::from(r.width_nm) + i128::from(width) + 2 * i128::from(pad_clr),
                        false,
                    ),
                    PadCore::ConvexPoly => geometry::track_near_pad(
                        a,
                        b,
                        r.width_nm,
                        pad_clr,
                        &self.input.pads[ordinal as usize],
                        false,
                    ),
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
        let swept = SweptAabb::swept(a, b, r.width_nm, r.clearance_nm);
        let track_flow =
            self.copper
                .visit_candidates_geoms_filtered(swept, Some(net), &self.work, |t| {
                    if geometry::segments_near(
                        a,
                        b,
                        t.start,
                        t.end,
                        i128::from(r.width_nm)
                            + i128::from(t.width_nm)
                            + 2 * i128::from(r.clearance_nm),
                        false,
                    ) {
                        return Ok(ControlFlow::Break(()));
                    }
                    Ok(ControlFlow::Continue(()))
                })?;
        if track_flow.is_break() {
            return Ok(false);
        }
        let hole_net_filter = if r.same_net_hole_clearance_nm > 0 {
            None
        } else {
            Some(net)
        };
        let max_hole_clr = if r.same_net_hole_clearance_nm > 0 {
            r.hole_clearance_nm.max(r.same_net_hole_clearance_nm)
        } else {
            r.hole_clearance_nm
        };
        let hole_swept = SweptAabb::swept(a, b, r.width_nm, max_hole_clr);
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
                        let seg = Track {
                            id: String::new(),
                            net,
                            start: a,
                            end: b,
                            width_nm: r.width_nm,
                            locked: false,
                        };
                        if geometry::track_connects_pad(&seg, pad) {
                            return Ok(ControlFlow::Continue(()));
                        }
                    }
                    r.same_net_hole_clearance_nm
                } else {
                    r.hole_clearance_nm
                };
                let hit = match *drill {
                    DrillGeom::Circle { center, diameter } => geometry::point_near_segment(
                        [i64::from(center[0]), i64::from(center[1])],
                        a,
                        b,
                        i128::from(r.width_nm) + i128::from(diameter) + 2 * i128::from(clr),
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
                        i128::from(r.width_nm) + i128::from(width) + 2 * i128::from(clr),
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
        let art_flow =
            self.artwork_spatial
                .visit_matching(swept, Some(net), &self.work, |_, geom| {
                    self.work.charge(1)?;
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
                            i128::from(r.width_nm)
                                + i128::from(width)
                                + 2 * i128::from(r.clearance_nm),
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
                            r.width_nm,
                            r.clearance_nm,
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
                })?;
        if art_flow.is_break() {
            return Ok(false);
        }
        if r.solder_mask_to_copper_clearance_nm > 0 {
            let mask_swept = SweptAabb::swept(
                a,
                b,
                r.width_nm,
                r.solder_mask_to_copper_clearance_nm + self.max_mask_expansion,
            );
            let mask_flow = self.mask_spatial.visit_matching(
                mask_swept,
                Some(net),
                &self.work,
                |_, geom| {
                    self.work.charge(1)?;
                    let hit = match *geom {
                        MaskGeom::Segment {
                            a: sa,
                            b: sb,
                            width,
                            expansion,
                        } => {
                            let clr = r.solder_mask_to_copper_clearance_nm + i64::from(expansion);
                            geometry::segments_near(
                                a,
                                b,
                                [i64::from(sa[0]), i64::from(sa[1])],
                                [i64::from(sb[0]), i64::from(sb[1])],
                                i128::from(r.width_nm) + i128::from(width) + 2 * i128::from(clr),
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
                            let clr = r.solder_mask_to_copper_clearance_nm + i64::from(expansion);
                            geometry::track_near_circle(
                                a,
                                b,
                                r.width_nm,
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
                                &self.input.mask_apertures[mask_idx as usize].shape
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
            if mask_flow.is_break() {
                return Ok(false);
            }
        }
        Ok(true)
    }
    fn components(&self, net: u32) -> Result<Vec<Vec<Copper<'_>>>, Failure> {
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
        for g in &self.input.graphics {
            self.work.charge(1)?;
            if g.layer == self.input.layer && g.net == net {
                items.push(Copper::Graphic(g));
            }
        }
        components::partition(items, &self.work)
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
    fn insert_path(&mut self, net: u32, path: search::Path) -> Result<(), Failure> {
        // Coalesce only collinear steps: the swept geometry is unchanged.
        let mut points: Vec<Point> = Vec::new();
        for p in path.points {
            self.work.charge(1)?;
            if points.len() >= 2 {
                let a = points[points.len() - 2];
                let b = points[points.len() - 1];
                if (a[0] == b[0] && b[0] == p[0]) || (a[1] == b[1] && b[1] == p[1]) {
                    points.pop();
                }
            }
            points.push(p);
        }
        self.work.charge(points.len() as u64)?;
        let proposal: Vec<_> = points
            .windows(2)
            .map(|p| Track {
                id: String::new(),
                net,
                start: p[0],
                end: p[1],
                width_nm: self.input.rules.width_nm,
                locked: false,
            })
            .collect();
        let mut normalized = Vec::new();
        for t in proposal {
            if self.input.rules.same_net_hole_clearance_nm > 0
                && (t.start == t.end || !self.legal_edge(net, t.start, t.end)?)
            {
                return Err(Failure::new(
                    Status::Unroutable,
                    "proposal failed continuous clearance validation",
                ));
            }
            for piece in self.subtract_collinear_overlaps(&t, &normalized)? {
                if piece.start == piece.end
                    || (self.input.rules.same_net_hole_clearance_nm == 0
                        && !self.legal_edge(net, piece.start, piece.end)?)
                {
                    return Err(Failure::new(
                        Status::Unroutable,
                        "proposal failed continuous clearance validation",
                    ));
                }
                normalized.push(piece);
            }
        }
        for t in normalized {
            self.copper.insert(t, &self.work)?;
        }
        Ok(())
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
        self.operation(|tx| {
            let engine = &mut *tx.engine;
            let mut expanded = 0;
            engine.work.charge(engine.input.nets.len() as u64)?;
            let mut nets: Vec<_> = engine
                .input
                .nets
                .iter()
                .filter(|n| n.id != 0)
                .map(|n| n.id)
                .collect();
            engine.work.charge(nets.len() as u64)?;
            nets.sort();
            for net in nets {
                engine.work.charge(1).map_err(|mut e| {
                    e.expanded_states = expanded;
                    e
                })?;
                if let Err(mut e) =
                    engine.route_net(net, engine.input.max_expansions, &mut expanded)
                {
                    e.expanded_states = expanded;
                    return Err(scoped_routing_failure(e, net));
                }
            }
            Ok(expanded)
        })
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
fn scoped_routing_failure(mut e: Failure, net: u32) -> Failure {
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
    let mut result = run_unidentified(input);
    result.request_sha256 = Some(identity);
    result
}
fn run_unidentified(input: Input) -> Output {
    let source = input.source_id.clone();
    let mut engine = match Engine::new(input) {
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
            removed_track_ids: Vec::new(),
            expanded_states: expanded,
            work_used: engine.work_used(),
            reason: None,
        },
        Err(e) => Output::failure(source, e),
    }
}
pub fn run_json(bytes: &[u8]) -> Output {
    if bytes.len() > identity::MAX_REQUEST_BYTES {
        return Output::failure(
            String::new(),
            Failure::new(Status::AdmissionExceeded, "JSON input exceeds 16 MiB"),
        );
    }
    // Deserialize directly into the contract: going through Value first would
    // erase duplicate keys, including a second `tracks` field hiding copper.
    let mut result = match serde_json::from_slice::<Input>(bytes) {
        Ok(input) => run_unidentified(input),
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
