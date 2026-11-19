use super::{map_routing_status, OperationStatus};
use crate::model::{CopperGraphic, Input, Output, Status, ZoneLayerFill};
use serde::Deserialize;
use std::collections::BTreeMap;

#[derive(Deserialize)]
pub(super) struct RefilledCopper {
    zones: BTreeMap<String, Vec<ZoneLayerFill>>,
    teardrops: Vec<CopperGraphic>,
    introduced_violations: usize,
}

/// Rejoin components on the measured fill. Previous edits become fixed copper in
/// the second search. Only a complete combined plan can go back to native apply.
pub(super) fn repair(
    mut input: Input,
    mut copper: RefilledCopper,
    first: &mut Output,
    control: crate::budget::RouteControl,
) -> Result<(), (OperationStatus, String)> {
    if copper.introduced_violations > 0 {
        return Err((
            OperationStatus::NativeValidationFailed,
            format!(
                "native refill reports {} new DRC violations",
                copper.introduced_violations
            ),
        ));
    }
    for zone in &mut input.zones {
        zone.layer_fills = copper.zones.remove(&zone.id).ok_or_else(|| {
            (
                OperationStatus::ChildProcessError,
                format!("missing native fill for zone {}", zone.id),
            )
        })?;
    }
    if !copper.zones.is_empty() {
        return Err((
            OperationStatus::ChildProcessError,
            "unrecognized native zone in refill".into(),
        ));
    }
    input.graphics.extend(copper.teardrops);
    input.tracks.extend_from_slice(&first.added_tracks);
    input.vias.extend_from_slice(&first.added_vias);
    input.max_expansions = input.max_expansions.saturating_sub(first.expanded_states);
    input.limits.max_work = input.limits.max_work.saturating_sub(first.work_used);
    let remainder = if input.max_expansions == 0 {
        Err((
            Status::BudgetExhausted,
            "no expansions remain after zone refill",
        ))
    } else if input.limits.max_work == 0 {
        Err((Status::WorkExhausted, "no work remains after zone refill"))
    } else {
        Ok(())
    };
    let extra = match remainder {
        Ok(()) => std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            crate::run_unidentified(input, Some(control))
        }))
        .map_err(|_| {
            (
                OperationStatus::InternalError,
                "refill routing panicked".into(),
            )
        })?,
        Err((status, reason)) => Output::failure(
            first.source_id.clone(),
            crate::model::Failure::new(status, reason),
        ),
    };
    first.expanded_states += extra.expanded_states;
    first.work_used += extra.work_used;
    first.status = extra.status;
    if extra.status != Status::Complete {
        first.added_tracks.clear();
        first.added_vias.clear();
        first.removed_track_ids.clear();
        return Err((
            map_routing_status(extra.status),
            extra
                .reason
                .unwrap_or_else(|| "zone refill repair failed".into()),
        ));
    }
    if !extra.removed_track_ids.is_empty() {
        return Err((
            OperationStatus::InternalError,
            "zone repair attempted to remove fixed copper".into(),
        ));
    }
    first.added_tracks.extend(extra.added_tracks);
    first.added_vias.extend(extra.added_vias);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repair_uses_only_unspent_expansions_and_work() {
        let fixture = include_bytes!("../../tests/fixtures/multiterminal-native-two-nets.json");
        for (expansions, work, expected) in [
            (100, 25, Status::BudgetExhausted),
            (50, 100, Status::WorkExhausted),
        ] {
            let mut input: Input = serde_json::from_slice(fixture).unwrap();
            input.max_expansions = 100;
            input.limits.max_work = 100;
            let mut first = Output {
                diff_pairs: Vec::new(),
                bus_groups: Vec::new(),
                version: crate::model::RESULT_VERSION,
                source_id: input.source_id.clone(),
                request_sha256: None,
                status: Status::Complete,
                added_tracks: vec![],
                added_vias: vec![],
                removed_track_ids: vec![],
                expanded_states: expansions,
                work_used: work,
                reason: None,
            };
            let fill = RefilledCopper {
                zones: BTreeMap::new(),
                teardrops: vec![],
                introduced_violations: 0,
            };
            let (status, _) = repair(
                input,
                fill,
                &mut first,
                super::super::route_control(std::time::Instant::now()),
            )
            .unwrap_err();
            assert_eq!(status, map_routing_status(expected));
            assert_eq!(first.status, expected);
            assert_eq!((first.expanded_states, first.work_used), (expansions, work));
        }
    }
}
