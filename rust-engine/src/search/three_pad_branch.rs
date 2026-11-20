//! A three-terminal junction on the surface can use one straight arm per pad.
//! Return all three verified arms in the same copper proposal.
use super::{allowed_track_layers, Copper, Engine, Failure, PathEdge, Point, TargetBounds};
use crate::negotiate::{BlockedFrontier, TrialBudget};

fn median(a: [i64; 3]) -> i64 {
    a[0].clamp(a[1].min(a[2]), a[1].max(a[2]))
}

// Store axial + diagonal * sqrt(2) in nanometres. Rounding each arm to
// integer nanometres can reverse the comparison of two near-equal trees.
#[derive(Clone, Copy, Default)]
struct Length {
    axial: u64,
    diagonal: u64,
}

impl Length {
    fn between(a: Point, b: Point) -> Self {
        let dx = a[0].abs_diff(b[0]);
        let dy = a[1].abs_diff(b[1]);
        Self {
            axial: dx.abs_diff(dy),
            diagonal: dx.min(dy),
        }
    }

    fn plus(self, other: Self) -> Self {
        Self {
            axial: self.axial + other.axial,
            diagonal: self.diagonal + other.diagonal,
        }
    }

    fn compare(self, other: Self) -> std::cmp::Ordering {
        let a = i128::from(self.axial) - i128::from(other.axial);
        let d = i128::from(self.diagonal) - i128::from(other.diagonal);
        if a == 0 || d == 0 || a.signum() == d.signum() {
            return (a + d).cmp(&0);
        }
        // The two terms have opposite signs. Squaring their magnitudes is
        // exact throughout the validated board coordinate range.
        let magnitudes = (a * a).cmp(&(2 * d * d));
        if a > 0 {
            magnitudes
        } else {
            magnitudes.reverse()
        }
    }
}

pub(super) fn between(
    engine: &Engine,
    net: u32,
    groups: &[Vec<Copper<'_>>],
    budget: &mut TrialBudget<'_>,
    expanded: &mut u64,
    frontier: Option<&BlockedFrontier>,
) -> Result<Option<Vec<PathEdge>>, Failure> {
    if groups.len() != 3 || engine.surface_search_share.is_none() {
        return Ok(None);
    }
    let allowed = allowed_track_layers(engine, net)?;
    let mut pins = [[0; 2]; 3];
    let mut face = None;
    for (i, g) in groups.iter().enumerate() {
        engine.work.charge(1)?;
        let [c @ Copper::Pad(_)] = g.as_slice() else {
            return Ok(None);
        };
        let bounds = TargetBounds::new(*c, engine.all_layers_slice(), &engine.work)?;
        let Some((point, l)) = bounds.surface_contact(&allowed, &engine.work)? else {
            return Ok(None);
        };
        if face.is_some_and(|other| other != l) {
            return Ok(None);
        }
        face = Some(l);
        pins[i] = point;
    }
    let Some(layer) = face else { return Ok(None) };
    let cartesian = [median(pins.map(|p| p[0])), median(pins.map(|p| p[1]))];
    let u = median(pins.map(|p| p[0] + p[1]));
    let v = median(pins.map(|p| p[0] - p[1]));
    let diagonal = ((u + v) % 2 == 0).then_some([(u + v) / 2, (u - v) / 2]);
    let mut spans = [
        Length::between(pins[0], pins[1]),
        Length::between(pins[1], pins[2]),
        Length::between(pins[2], pins[0]),
    ];
    spans.sort_unstable_by(|a, b| a.compare(*b));
    let pairwise = spans[0].plus(spans[1]);
    let mut best = None;
    'candidate: for joint in [Some(cartesian), diagonal].into_iter().flatten() {
        engine.work.charge(1)?;
        let mut total = Length::default();
        for p in &pins {
            let x = p[0].abs_diff(joint[0]);
            let y = p[1].abs_diff(joint[1]);
            if (x == 0 && y == 0) || (x != 0 && y != 0 && x != y) {
                continue 'candidate;
            }
            total = total.plus(Length::between(*p, joint));
        }
        if !total.compare(pairwise).is_lt()
            || best
                .as_ref()
                .is_some_and(|(len, _)| !total.compare(*len).is_lt())
        {
            continue;
        }
        budget.check_before_expand(engine, *expanded, frontier)?;
        *expanded += 1;
        for p in pins {
            if !engine.legal_edge_on(net, layer, p, joint)? {
                continue 'candidate;
            }
        }
        best = Some((total, joint));
    }
    Ok(best.map(|(_, joint)| {
        pins.into_iter()
            .map(|end| PathEdge::Track {
                start: joint,
                end,
                layer: layer as u8,
            })
            .collect()
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cmp::Ordering;

    #[test]
    fn only_a_strict_saving_in_true_octilinear_length_counts() {
        // 239^2 - 2 * 169^2 = -1. Rounding to the nearest nanometre
        // calls these equal. Switching the operands must reverse the order.
        let axial = Length::between([0, 0], [239, 0]);
        let diagonal = Length::between([0, 0], [169, 169]);
        assert_eq!(axial.compare(diagonal), Ordering::Less);
        assert_eq!(diagonal.compare(axial), Ordering::Greater);
        let common = Length::between([-10_000, 3_233], [28_721, -9_122]);
        assert_eq!(
            common.plus(axial).compare(common.plus(diagonal)),
            Ordering::Less
        );
        assert_eq!(common.compare(common), Ordering::Equal);
    }
}
