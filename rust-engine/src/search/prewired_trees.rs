//! Pick the next edge of a geometric spanning tree between already routed
//! pad groups. A distant first group need not launch the next connection.
use super::{Copper, Engine, Failure};

pub(super) fn origin(engine: &Engine, groups: &[Vec<Copper<'_>>]) -> Result<usize, Failure> {
    if groups.len() < 3 {
        return Ok(0);
    }
    engine.work.charge(groups.len() as u64)?;
    // Two pads and their interconnect are the minimum size of a wired tree.
    if groups.iter().any(|g| g.len() < 3) {
        return Ok(0);
    }
    let mut contacts = Vec::new();
    for (i, members) in groups.iter().enumerate() {
        engine.work.charge(members.len() as u64)?;
        let first = contacts.len();
        let mut wired = false;
        for &item in members {
            match item {
                Copper::Pad(p) => {
                    engine.work.search_storage(contacts.len() + 1)?;
                    contacts.push((i, p.copper_center()));
                }
                Copper::Track(_) => wired = true,
                Copper::Via(_) => {}
                _ => return Ok(0),
            }
        }
        if !wired || contacts.len() - first < 2 {
            return Ok(0);
        }
    }
    let count = contacts.len() as u64;
    if count.saturating_mul(count.saturating_sub(1)) / 2 > engine.work.remaining() {
        return Ok(0);
    }
    let mut best = None;
    for (n, &(i, a)) in contacts.iter().enumerate() {
        engine.work.charge(n as u64)?;
        for &(j, b) in &contacts[..n] {
            if i == j {
                continue;
            }
            let dx = u128::from(a[0].abs_diff(b[0]));
            let dy = u128::from(a[1].abs_diff(b[1]));
            let pair = (dx * dx + dy * dy, j.min(i), j.max(i));
            if best.is_none_or(|old| pair < old) {
                best = Some(pair);
            }
        }
    }
    Ok(match best {
        Some((_, first, second)) if first > 0 => {
            if groups[first].len() <= groups[second].len() {
                first
            } else {
                second
            }
        }
        _ => 0,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Input;

    #[test]
    fn nearest_already_connected_trees_keep_their_choice_across_order_and_reflection() {
        let template: Input = serde_json::from_str(include_str!(
            "../../tests/fixtures/multiterminal-native-two-nets.json"
        ))
        .unwrap();
        for mirror in [false, true] {
            let mut input = template.clone();
            let pad = input.pads[0].clone();
            let track = input.tracks[0].clone();
            input.pads.clear();
            input.tracks.clear();
            for (group, stops) in [[2, 4], [23, 25], [29, 31]].into_iter().enumerate() {
                let xy = |x| {
                    [
                        if mirror {
                            (38 - x) * 1_000_000_i64
                        } else {
                            x * 1_000_000_i64
                        },
                        8_000_000,
                    ]
                };
                for (j, x) in stops.iter().enumerate() {
                    let mut p = pad.clone();
                    p.id = format!("23000000-0000-4000-8000-{:012x}", 1 + group * 2 + j);
                    p.at = xy(*x);
                    p.net = 1;
                    input.pads.push(p);
                }
                let mut t = track.clone();
                t.id = format!("24000000-0000-4000-8000-{group:012x}");
                t.net = 1;
                t.start = xy(stops[0]);
                t.end = xy(stops[1]);
                input.tracks.push(t);
            }
            let e = Engine::new(input).unwrap();
            let mut groups = e.components(1).unwrap();
            groups.sort_by_key(|g| {
                g.iter()
                    .find_map(|c| match c {
                        Copper::Track(t) => Some(&t.id),
                        _ => None,
                    })
                    .unwrap()
            });
            assert_eq!(groups.len(), 3);
            assert_eq!(origin(&e, &groups).unwrap(), 1);
            groups.swap(0, 2);
            assert_eq!(origin(&e, &groups).unwrap(), 0);
            groups.swap(0, 2);
            groups[2].retain(|c| matches!(c, Copper::Pad(_)));
            assert_eq!(origin(&e, &groups).unwrap(), 0);
        }
    }
}
