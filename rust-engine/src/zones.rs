//! Zone resolution and deterministic thermal-spoke protection corridors.
use crate::{
    geometry,
    model::{
        DrillShape, GraphicShape, Input, OutlinePolygon, Pad, PadZoneConnection, Point, Shape,
        Zone, ZoneConnection, MAX_NM,
    },
    rules::{DruItemKind, EffectiveRules},
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ResolvedPadZoneConnection {
    None,
    Thermal,
    Solid,
}

pub(crate) fn resolve_pad_zone_connection(pad: &Pad, zone: &Zone) -> ResolvedPadZoneConnection {
    let is_pth = pad.drill.is_some();
    let resolve_zone_conn = |zc: ZoneConnection| match zc {
        ZoneConnection::None => ResolvedPadZoneConnection::None,
        ZoneConnection::Thermal => ResolvedPadZoneConnection::Thermal,
        ZoneConnection::Solid => ResolvedPadZoneConnection::Solid,
        ZoneConnection::ThtThermal => {
            if is_pth {
                ResolvedPadZoneConnection::Thermal
            } else {
                ResolvedPadZoneConnection::Solid
            }
        }
    };
    match pad.zone_connection {
        PadZoneConnection::Inherited => resolve_zone_conn(zone.connection),
        PadZoneConnection::None => ResolvedPadZoneConnection::None,
        PadZoneConnection::Thermal => ResolvedPadZoneConnection::Thermal,
        PadZoneConnection::Solid => ResolvedPadZoneConnection::Solid,
        PadZoneConnection::ThtThermal => {
            if is_pth {
                ResolvedPadZoneConnection::Thermal
            } else {
                ResolvedPadZoneConnection::Solid
            }
        }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct SpokeCorridor {
    pub layer_idx: usize,
    pub net: u32,
    pub local_clearance_nm: i64,
    pub verts: [Point; 4],
}

pub(crate) fn zone_contains_point(zone: &Zone, pt: Point) -> bool {
    if geometry::point_in_polygon_with_holes(pt, &zone.outer, &zone.holes) {
        return true;
    }
    zone.extra_polygons
        .iter()
        .any(|p| geometry::point_in_polygon_with_holes(pt, &p.outer, &p.holes))
}

fn zone_fill_polys_on_layer<'a>(zone: &'a Zone, layer_name: &str) -> &'a [OutlinePolygon] {
    zone.layer_fills
        .iter()
        .find(|lf| lf.layer == layer_name)
        .map_or(&[], |lf| lf.polygons.as_slice())
}

fn pad_min_side_nm(pad: &Pad) -> i64 {
    match &pad.shape {
        Shape::Union { .. } => unreachable!("compound pad thermal spokes are unsupported"),
        Shape::Circle { radius_nm } => radius_nm.saturating_mul(2),
        Shape::Rect { half_size_nm } | Shape::RoundRect { half_size_nm, .. } => {
            half_size_nm[0].min(half_size_nm[1]).saturating_mul(2)
        }
        Shape::BoxExact { half_size_2x, .. } => half_size_2x[0].min(half_size_2x[1]),
        Shape::Capsule { width_nm, .. } => *width_nm,
        Shape::ConvexPolygon {
            vertices,
            radius_nm,
        } => {
            if vertices.len() < 3 {
                let (lo, hi) = pad.copper_bounds();
                return (hi[0] - lo[0]).min(hi[1] - lo[1]);
            }
            let n = vertices.len();
            let mut min_w = f64::INFINITY;
            for i in 0..n {
                let dx = (vertices[(i + 1) % n][0] - vertices[i][0]) as f64;
                let dy = (vertices[(i + 1) % n][1] - vertices[i][1]) as f64;
                let len = (dx * dx + dy * dy).sqrt();
                if len < 1e-9 {
                    continue;
                }
                let (nx, ny) = (-dy / len, dx / len);
                let mut lo = f64::INFINITY;
                let mut hi = f64::NEG_INFINITY;
                for v in vertices {
                    let p = v[0] as f64 * nx + v[1] as f64 * ny;
                    lo = lo.min(p);
                    hi = hi.max(p);
                }
                min_w = min_w.min(hi - lo + 2.0 * (*radius_nm as f64));
            }
            if min_w.is_finite() {
                min_w.floor().max(0.0) as i64
            } else {
                let (lo, hi) = pad.copper_bounds();
                (hi[0] - lo[0]).min(hi[1] - lo[1])
            }
        }
    }
}

fn dir_unit_kicad(ang_mdeg: i64) -> (f64, f64) {
    let norm = ang_mdeg.rem_euclid(360_000);
    match norm {
        0 => (1.0, 0.0),
        90_000 => (0.0, -1.0),
        180_000 => (-1.0, 0.0),
        270_000 => (0.0, 1.0),
        _ => {
            let rad = (norm as f64 / 1000.0).to_radians();
            (rad.cos(), -rad.sin())
        }
    }
}

fn ray_exit_poly(pts: &[(f64, f64)], cx: f64, cy: f64, dx: f64, dy: f64) -> f64 {
    let mut best = 0.0f64;
    let n = pts.len();
    for i in 0..n {
        let (x1, y1) = pts[i];
        let (x2, y2) = pts[(i + 1) % n];
        let ex = x2 - x1;
        let ey = y2 - y1;
        let den = dx * ey - dy * ex;
        if den.abs() < 1e-12 {
            continue;
        }
        let t = ((x1 - cx) * ey - (y1 - cy) * ex) / den;
        let u = ((x1 - cx) * dy - (y1 - cy) * dx) / den;
        if t > 0.0 && (-1e-9..=1.0 + 1e-9).contains(&u) {
            best = best.max(t);
        }
    }
    best
}

fn box_reach_and_exit(hx: f64, hy: f64, r: f64, dx: f64, dy: f64) -> (f64, f64) {
    let chx = (hx - r).max(0.0);
    let chy = (hy - r).max(0.0);
    let reach = chx * dx.abs() + chy * dy.abs() + r;
    if r <= 0.0 {
        let tx = if dx.abs() > 1e-12 {
            hx / dx.abs()
        } else {
            f64::INFINITY
        };
        let ty = if dy.abs() > 1e-12 {
            hy / dy.abs()
        } else {
            f64::INFINITY
        };
        return (reach, tx.min(ty));
    }
    let mut pts = Vec::with_capacity(32);
    for (sx, sy, a0) in [
        (chx, chy, 0.0f64),
        (-chx, chy, std::f64::consts::FRAC_PI_2),
        (-chx, -chy, std::f64::consts::PI),
        (chx, -chy, 3.0 * std::f64::consts::FRAC_PI_2),
    ] {
        for s in 0..=8 {
            let a = a0 + (s as f64) * (std::f64::consts::FRAC_PI_2 / 8.0);
            pts.push((sx + r * a.cos(), sy + r * a.sin()));
        }
    }
    let exit = ray_exit_poly(&pts, 0.0, 0.0, dx, dy);
    (reach, if exit > 0.0 { exit } else { reach })
}

fn pad_reach_and_exit(pad: &Pad, cx: f64, cy: f64, dx: f64, dy: f64) -> (f64, f64) {
    match &pad.shape {
        Shape::Union { .. } => unreachable!("compound pad thermal spokes are unsupported"),
        Shape::Circle { radius_nm } => (*radius_nm as f64, *radius_nm as f64),
        Shape::Rect { half_size_nm } => {
            box_reach_and_exit(half_size_nm[0] as f64, half_size_nm[1] as f64, 0.0, dx, dy)
        }
        Shape::RoundRect {
            half_size_nm,
            radius_nm,
        } => box_reach_and_exit(
            half_size_nm[0] as f64,
            half_size_nm[1] as f64,
            *radius_nm as f64,
            dx,
            dy,
        ),
        Shape::BoxExact {
            half_size_2x,
            radius_2x,
            ..
        } => box_reach_and_exit(
            half_size_2x[0] as f64 / 2.0,
            half_size_2x[1] as f64 / 2.0,
            *radius_2x as f64 / 2.0,
            dx,
            dy,
        ),
        Shape::Capsule {
            start_nm,
            end_nm,
            width_nm,
        } => {
            let r = *width_nm as f64 / 2.0;
            let p1 = (start_nm[0] as f64 - cx) * dx + (start_nm[1] as f64 - cy) * dy;
            let p2 = (end_nm[0] as f64 - cx) * dx + (end_nm[1] as f64 - cy) * dy;
            let reach = p1.max(p2) + r;
            let sx = end_nm[0] as f64 - start_nm[0] as f64;
            let sy = end_nm[1] as f64 - start_nm[1] as f64;
            let slen = (sx * sx + sy * sy).sqrt();
            if slen < 1e-9 {
                return (r, r);
            }
            let ux = sx / slen;
            let uy = sy / slen;
            let nx = -uy;
            let ny = ux;
            let base_ang = uy.atan2(ux);
            let mut pts = Vec::with_capacity(20);
            for s in 0..=8 {
                let a = base_ang - std::f64::consts::FRAC_PI_2
                    + (s as f64) * (std::f64::consts::PI / 8.0);
                pts.push((
                    end_nm[0] as f64 + r * a.cos(),
                    end_nm[1] as f64 + r * a.sin(),
                ));
            }
            for s in 0..=8 {
                let a = base_ang
                    + std::f64::consts::FRAC_PI_2
                    + (s as f64) * (std::f64::consts::PI / 8.0);
                pts.push((
                    start_nm[0] as f64 + r * a.cos(),
                    start_nm[1] as f64 + r * a.sin(),
                ));
            }
            let _ = (nx, ny);
            let exit = ray_exit_poly(&pts, cx, cy, dx, dy);
            (reach, if exit > 0.0 { exit } else { reach })
        }
        Shape::ConvexPolygon {
            vertices,
            radius_nm,
        } => {
            let r = *radius_nm as f64;
            let reach = vertices
                .iter()
                .map(|v| (v[0] as f64 - cx) * dx + (v[1] as f64 - cy) * dy)
                .fold(0.0f64, f64::max)
                + r;
            let pts: Vec<(f64, f64)> = vertices
                .iter()
                .map(|v| (v[0] as f64, v[1] as f64))
                .collect();
            let exit = ray_exit_poly(&pts, cx, cy, dx, dy) + r;
            (reach, if exit > 0.0 { exit.min(reach) } else { reach })
        }
    }
}

fn pad_near_convex_poly(pad: &Pad, clearance: i64, verts: &[Point; 4]) -> bool {
    if geometry::point_in_convex_poly(pad.copper_center(), verts) {
        return true;
    }
    let n = verts.len();
    for i in 0..n {
        if geometry::track_near_pad(verts[i], verts[(i + 1) % n], 0, clearance, pad, false) {
            return true;
        }
    }
    false
}

fn corridor_conflicts_with_existing_copper(
    input: &Input,
    layer_idx: usize,
    layer_name: &str,
    layers: &[String],
    rules: &EffectiveRules,
    zone: &Zone,
    verts: &[Point; 4],
) -> bool {
    let z_clr = (zone.local_clearance_nm > 0).then_some(zone.local_clearance_nm);
    for pad in &input.pads {
        if pad.net == zone.net {
            continue;
        }
        let on_layer = if pad.layers.is_empty() {
            layer_name == input.layer
        } else {
            pad.layers.iter().any(|l| l == layer_name)
        };
        if !on_layer {
            continue;
        }
        let clr = rules
            .pair_clearance(
                pad.net,
                DruItemKind::Pad,
                zone.net,
                DruItemKind::Zone,
                layer_name,
                z_clr,
            )
            .max(pad.local_clearance_nm.unwrap_or(0));
        if pad_near_convex_poly(pad, clr, verts) {
            return true;
        }
    }
    for trk in &input.tracks {
        if trk.net == zone.net || trk.layer != layer_name {
            continue;
        }
        let clr = rules.pair_clearance(
            trk.net,
            DruItemKind::Track,
            zone.net,
            DruItemKind::Zone,
            layer_name,
            z_clr,
        );
        if geometry::track_near_convex_poly(trk.start, trk.end, trk.width_nm, clr, verts, 0, false)
        {
            return true;
        }
    }
    for via in &input.vias {
        if via.net == zone.net {
            continue;
        }
        let Some(s_idx) = layers.iter().position(|l| l == &via.start_layer) else {
            continue;
        };
        let Some(e_idx) = layers.iter().position(|l| l == &via.end_layer) else {
            continue;
        };
        if !(s_idx..=e_idx).contains(&layer_idx) {
            continue;
        }
        let clr = rules.pair_clearance_on_layer(
            (via.net, DruItemKind::Via, &layers[s_idx..=e_idx]),
            (zone.net, DruItemKind::Zone, &[]),
            layer_name,
            z_clr,
        );
        if geometry::track_near_convex_poly(via.at, via.at, via.diameter_nm, clr, verts, 0, false) {
            return true;
        }
    }
    for hole in &input.holes {
        if hole.net == zone.net && zone.net != 0 {
            continue;
        }
        let clr = rules.pair_hole_clearance(
            zone.net,
            DruItemKind::Zone,
            std::slice::from_ref(&layer_name.to_owned()),
            hole.net,
            DruItemKind::Hole,
            layers,
        );
        let hit = match hole.shape {
            DrillShape::Circle { diameter_nm } => geometry::track_near_convex_poly(
                hole.at,
                hole.at,
                diameter_nm,
                clr,
                verts,
                0,
                false,
            ),
            DrillShape::Capsule {
                start_nm,
                end_nm,
                width_nm,
            } => geometry::track_near_convex_poly(start_nm, end_nm, width_nm, clr, verts, 0, false),
        };
        if hit {
            return true;
        }
    }
    for g in &input.graphics {
        if g.net == zone.net || g.layer != layer_name {
            continue;
        }
        let clr = rules.pair_clearance(
            g.net,
            DruItemKind::Graphic,
            zone.net,
            DruItemKind::Zone,
            layer_name,
            z_clr,
        );
        let hit = match &g.shape {
            GraphicShape::Segment {
                start,
                end,
                width_nm,
            } => geometry::track_near_convex_poly(*start, *end, *width_nm, clr, verts, 0, false),
            GraphicShape::Arc {
                start,
                mid,
                end,
                width_nm,
            } => {
                let (chords, sag) = geometry::arc_to_conservative_chords(*start, *mid, *end);
                chords.windows(2).any(|w| {
                    geometry::track_near_convex_poly(
                        w[0],
                        w[1],
                        *width_nm,
                        clr + sag,
                        verts,
                        0,
                        false,
                    )
                })
            }
            GraphicShape::Circle {
                center,
                radius_nm,
                width_nm,
                filled,
            } => {
                if *filled {
                    geometry::track_near_convex_poly(
                        *center,
                        *center,
                        2 * radius_nm + width_nm,
                        clr,
                        verts,
                        0,
                        false,
                    )
                } else {
                    let n = verts.len();
                    (0..n).any(|i| {
                        geometry::track_near_circle(
                            verts[i],
                            verts[(i + 1) % n],
                            0,
                            clr,
                            *center,
                            *radius_nm,
                            *width_nm,
                            false,
                            false,
                        )
                    })
                }
            }
            GraphicShape::Polygon {
                outer,
                holes,
                width_nm,
                filled,
            } => {
                let n = verts.len();
                let gap2 = i128::from(*width_nm) + 2 * i128::from(clr);
                let ring_hit = |ring: &[Point]| {
                    let m = ring.len();
                    (0..n).any(|i| {
                        (0..m).any(|j| {
                            geometry::segments_near(
                                verts[i],
                                verts[(i + 1) % n],
                                ring[j],
                                ring[(j + 1) % m],
                                gap2,
                                false,
                            )
                        })
                    })
                };
                ring_hit(outer)
                    || holes.iter().any(|h| ring_hit(h))
                    || (*filled
                        && (verts
                            .iter()
                            .any(|&p| geometry::point_in_polygon_with_holes(p, outer, holes))
                            || outer
                                .iter()
                                .any(|&p| geometry::point_in_convex_poly(p, verts))))
            }
        };
        if hit {
            return true;
        }
    }
    false
}

pub(crate) fn plan_spoke_corridors(
    input: &Input,
    layers: &[String],
    rules: &EffectiveRules,
) -> Vec<SpokeCorridor> {
    if input.zones.is_empty() {
        return Vec::new();
    }
    let min_spokes = rules.minimums.min_resolved_spokes.unwrap_or(2) as usize;
    if min_spokes == 0 {
        return Vec::new();
    }
    let max_error_nm = rules.minimums.max_error_nm.unwrap_or(5_000);

    let mut zone_order: Vec<usize> = (0..input.zones.len()).collect();
    zone_order.sort_by_key(|&i| (std::cmp::Reverse(input.zones[i].priority), i));

    let mut out = Vec::new();
    for (l_idx, l_name) in layers.iter().enumerate() {
        for pad in &input.pads {
            if pad.net == 0 {
                continue;
            }
            let pad_on_layer = if pad.layers.is_empty() {
                l_name == &input.layer
            } else {
                pad.layers.iter().any(|l| l == l_name)
            };
            if !pad_on_layer {
                continue;
            }
            let pad_center = pad.copper_center();
            let Some(&z_idx) = zone_order.iter().find(|&&zi| {
                let z = &input.zones[zi];
                z.net == pad.net
                    && z.refillable
                    && z.layers.iter().any(|zl| zl == l_name)
                    && zone_contains_point(z, pad_center)
            }) else {
                continue;
            };
            let zone = &input.zones[z_idx];
            if resolve_pad_zone_connection(pad, zone) != ResolvedPadZoneConnection::Thermal {
                continue;
            }
            let gap = pad.thermal_gap_nm.unwrap_or(zone.thermal_gap_nm);
            let spoke_w_raw = pad
                .thermal_spoke_width_nm
                .unwrap_or(zone.thermal_spoke_width_nm);
            let spoke_w = spoke_w_raw.min(pad_min_side_nm(pad));
            if spoke_w < zone.min_thickness_nm || spoke_w <= 0 {
                continue;
            }
            let default_spoke_angle_mdeg = match pad.shape {
                Shape::Circle { .. } => 45_000,
                Shape::BoxExact {
                    half_size_2x,
                    radius_2x,
                    ..
                } if half_size_2x[0] == half_size_2x[1] && half_size_2x[0] == radius_2x => 45_000,
                _ => 90_000,
            };
            let spoke_angle_mdeg = pad
                .thermal_spoke_angle_mdeg
                .unwrap_or(default_spoke_angle_mdeg);
            let fill_polys = zone_fill_polys_on_layer(zone, l_name);
            let cx = pad_center[0] as f64;
            let cy = pad_center[1] as f64;
            let half_w = spoke_w as f64 / 2.0;

            struct Candidate {
                k: usize,
                score: usize,
                verts: [Point; 4],
            }
            let mut eligible: Vec<Candidate> = Vec::new();
            for k in 0..4 {
                let ang_mdeg = pad.orientation_mdeg + spoke_angle_mdeg + (k as i64) * 90_000;
                let (dx, dy) = dir_unit_kicad(ang_mdeg);
                let (nx, ny) = (-dy, dx);
                let (reach, edge) = pad_reach_and_exit(pad, cx, cy, dx, dy);
                let start = edge.max(0.0);
                let end = reach + (gap + zone.min_thickness_nm + max_error_nm) as f64;
                if end <= start + 1.0 {
                    continue;
                }
                let test_dist =
                    reach + gap as f64 + max_error_nm as f64 + zone.min_thickness_nm as f64 / 2.0;
                let test_pt = [
                    (cx + dx * test_dist).round() as i64,
                    (cy + dy * test_dist).round() as i64,
                ];
                let live = if zone.filled {
                    fill_polys
                        .iter()
                        .any(|p| geometry::point_in_polygon_with_holes(test_pt, &p.outer, &p.holes))
                } else {
                    zone_contains_point(zone, test_pt)
                        && !input.rule_areas.iter().any(|ra| {
                            ra.no_zone_fills
                                && ra.layers.iter().any(|l| l == l_name)
                                && (geometry::point_in_polygon_with_holes(
                                    test_pt, &ra.outer, &ra.holes,
                                ) || ra.extra_polygons.iter().any(|ep| {
                                    geometry::point_in_polygon_with_holes(
                                        test_pt, &ep.outer, &ep.holes,
                                    )
                                }))
                        })
                };
                if !live {
                    continue;
                }
                let mk_pt = |s: f64, h: f64| -> Point {
                    [
                        (cx + dx * s + nx * h)
                            .round()
                            .clamp(-MAX_NM as f64, MAX_NM as f64) as i64,
                        (cy + dy * s + ny * h)
                            .round()
                            .clamp(-MAX_NM as f64, MAX_NM as f64) as i64,
                    ]
                };
                let verts = [
                    mk_pt(start, -half_w),
                    mk_pt(end, -half_w),
                    mk_pt(end, half_w),
                    mk_pt(start, half_w),
                ];
                if !geometry::is_ccw_convex_polygon(&verts) {
                    continue;
                }
                if corridor_conflicts_with_existing_copper(
                    input, l_idx, l_name, layers, rules, zone, &verts,
                ) {
                    continue;
                }
                let ext = (end - reach).max(1.0);
                let mut score = 0usize;
                for si in 0..8 {
                    let s = reach
                        + gap as f64
                        + (2.0 * ext - gap as f64).max(1.0) * (si as f64 + 0.5) / 8.0;
                    for sj in [-1.0f64, 0.0, 1.0] {
                        let sample = mk_pt(s, half_w * sj * 0.5);
                        let in_pour = if zone.filled {
                            fill_polys.iter().any(|p| {
                                geometry::point_in_polygon_with_holes(sample, &p.outer, &p.holes)
                            })
                        } else {
                            zone_contains_point(zone, sample)
                        };
                        if in_pour {
                            score += 1;
                        }
                    }
                }
                eligible.push(Candidate { k, score, verts });
            }
            eligible.sort_by(|a, b| b.score.cmp(&a.score).then_with(|| a.k.cmp(&b.k)));
            for cand in eligible.into_iter().take(min_spokes) {
                out.push(SpokeCorridor {
                    layer_idx: l_idx,
                    net: zone.net,
                    local_clearance_nm: zone.local_clearance_nm,
                    verts: cand.verts,
                });
            }
        }
    }
    out
}
