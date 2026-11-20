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
