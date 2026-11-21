//! Construction bound for whole-net absolute length maxima. KiCad merges tracks,
//! clips the centerline at pads/vias, and can shorten a via to its used span.
use crate::budget::WorkBudget;
use crate::model::{Failure, GraphicShape, Input, Point, Status};
use crate::rules::{DruConstraint, DruSeverity, EffectiveRules};
use crate::Engine;

fn active(rules: &EffectiveRules) -> bool {
    rules.dru_rules.iter().any(|r| {
        r.severity != DruSeverity::Ignore && matches!(r.constraint, DruConstraint::Length { .. })
    })
}

pub(super) fn admit(
    input: &Input,
    rules: &EffectiveRules,
    work: &WorkBudget,
) -> Result<(), Failure> {
    if !active(rules) {
        return Ok(());
    }
    let selected = crate::net_scope::active_nets(input, work)?;
    for g in &input.graphics {
        work.charge(1)?;
        if matches!(g.shape, GraphicShape::Arc { .. })
            && g.net != 0
            && selected.as_ref().is_none_or(|nets| nets.contains(&g.net))
        {
            if let Some((name, _)) = rules.whole_net_length_limit(g.net) {
                return Err(Failure::new(
                    Status::Unsupported,
                    format!(
                    "unsupported length rule {name:?} on net {:?}: routed arcs cannot be measured",
                    rules.net(g.net).net_name),
                ));
            }
        }
    }
    Ok(())
}

#[derive(Clone, Copy)]
struct Terminal {
    center: Point,
    bounds: (Point, Point),
}

impl Terminal {
    fn contains(&self, p: Point) -> bool {
        (0..2).all(|i| self.bounds.0[i] <= p[i] && p[i] <= self.bounds.1[i])
    }
}

fn distance_ceil(a: Point, b: Point) -> i64 {
    let dx = i128::from(a[0]) - i128::from(b[0]);
    let dy = i128::from(a[1]) - i128::from(b[1]);
    let sq = dx * dx + dy * dy;
    let root = sq.isqrt();
    (root + i128::from(root * root < sq)) as i64
}

fn upper_nm(engine: &Engine, net: u32) -> Result<i64, Failure> {
    let w = &engine.work;
    let mut total = 0i64;
    let mut terminals = Vec::new();
    let settings = engine.input.board_design_settings.as_ref();
    for pad in &engine.input.pads {
        w.charge(1)?;
        if pad.net == net {
            if let Some(v) = settings.and_then(|b| b.pad_to_die_length_nm.get(&pad.id)) {
                total = total.saturating_add(*v);
            }
            terminals.push(Terminal {
                center: pad.at,
                bounds: pad.copper_bounds(),
            });
        }
    }
    // Full stack thickness also bounds a blind/buried or partially used barrel.
    w.charge(engine.input.stackup.len() as u64)?;
    let stack: i64 = engine
        .input
        .stackup
        .iter()
        .map(|s| s.thickness_nm.max(0))
        .fold(0, i64::saturating_add);
    let height = stack.max(engine.effective_rules.board_thickness_nm.unwrap_or(0));
    let mut has_vias = false;
    for via in engine.copper.vias() {
        w.charge(1)?;
        if via.net != net {
            continue;
        }
        has_vias = true;
        if engine.effective_rules.use_height_for_length_calcs {
            if height <= 0 {
                return Err(Failure::new(
                    Status::Unsupported,
                    "absolute length with vias needs board or stackup thickness",
                ));
            }
            total = total.saturating_add(height);
        }
        let r = (via.diameter_nm + 1) / 2;
        terminals.push(Terminal {
            center: via.at,
            bounds: (
                [via.at[0] - r, via.at[1] - r],
                [via.at[0] + r, via.at[1] + r],
            ),
        });
    }
    let mut ends = Vec::new();
    for track in engine.copper.tracks() {
        w.charge(1)?;
        if track.net == net {
            total = total.saturating_add(distance_ceil(track.start, track.end));
            ends.extend([track.start, track.end]);
        }
    }
    if ends.is_empty() {
        return Ok(total);
    }
    // A merged line ends at an original segment endpoint. A pad/via clip
    // replaces that endpoint with its center and shortcuts the interior.
    // Bound any length increase by the endpoint displacement (triangle
    // inequality) and two rounded intersection distances. If another clip
    // moved the endpoint already, it can only be at another terminal center.
    let axial = !has_vias
        && (0..2).any(|i| {
            ends.iter()
                .chain(terminals.iter().map(|t| &t.center))
                .all(|p| p[i] == ends[0][i])
        });
    // KiCad accepts via intersections within squared distance 3 of an integer
    // nearest point on the segment. Rounding that projection costs at most
    // sqrt(2) nm. Each of the two replacement lengths can thus gain < 4 nm.
    // On a pad-only axial line the intersection is on that same integer axis.
    let rounding = if axial { 0 } else { 8 };
    for (index, terminal) in terminals.iter().enumerate() {
        w.charge((ends.len() + terminals.len()) as u64)?;
        for &end in &ends {
            if terminal.contains(end) {
                total = total.saturating_add(distance_ceil(end, terminal.center) + rounding);
            }
        }
        let shift = terminals
            .iter()
            .enumerate()
            .filter(|(other_index, other)| *other_index != index && terminal.contains(other.center))
            .map(|(_, t)| distance_ceil(t.center, terminal.center) + rounding)
            .max()
            .unwrap_or(0);
        total = total.saturating_add(shift.saturating_mul(ends.len() as i64));
    }
    Ok(total)
}

pub(super) fn check(engine: &Engine, expanded: u64) -> Result<(), Failure> {
    if !active(&engine.effective_rules) {
        return Ok(());
    }
    for net in &engine.input.nets {
        engine
            .work
            .charge(1 + engine.effective_rules.dru_rules.len() as u64)?;
        if net.id == 0 || !engine.copper.has_generated_for_net(net.id) {
            continue;
        }
        if let Some((name, max)) = engine.effective_rules.whole_net_length_limit(net.id) {
            let upper = upper_nm(engine, net.id)?;
            if upper > max {
                let mut failure = Failure::new(Status::Unroutable, format!(
                    "length rule {name:?}: cannot certify net {:?} within {max} nm (constructed upper bound {upper} nm)", net.name));
                failure.expanded_states = expanded;
                return Err(failure);
            }
        }
    }
    Ok(())
}
