//! Resolve declared layer names and track costs once against the physical stack.
use crate::{
    budget::WorkBudget,
    components::span_mask,
    model::{Failure, Input, Status},
};
use std::collections::BTreeMap;

pub(crate) struct ResolvedLayerPolicy {
    tracks: u32,
    by_net: BTreeMap<u32, u32>,
    costs: Vec<u64>,
    unit: u64,
}

impl ResolvedLayerPolicy {
    pub(crate) fn resolve(
        input: &Input,
        layers: &[String],
        work: &WorkBudget,
    ) -> Result<Self, Failure> {
        let mut result = Self {
            tracks: u32::MAX,
            by_net: BTreeMap::new(),
            costs: vec![],
            unit: 1,
        };
        let Some(policy) = input
            .routing_options
            .as_ref()
            .and_then(|o| o.layer_policy.as_ref())
        else {
            return Ok(result);
        };
        if policy.per_net_signal_layers.len() > input.limits.max_items {
            return Err(Failure::new(
                Status::AdmissionExceeded,
                "layer policy net count exceeds the input item limit",
            ));
        }
        work.charge(
            policy.signal_layers.len() as u64
                + policy
                    .per_net_signal_layers
                    .values()
                    .map(|ls| 1 + ls.len() as u64)
                    .sum::<u64>(),
        )?;
        policy.validate()?;
        let mask = |names: &[String]| -> Result<u32, Failure> {
            let mut m = 0;
            for name in names {
                work.charge(layers.len() as u64)?;
                let i = layers.iter().position(|l| l == name).ok_or_else(|| {
                    Failure::new(
                        Status::Invalid,
                        format!("layer policy names missing board layer {name}"),
                    )
                })?;
                m |= 1u32 << i;
            }
            Ok(m)
        };
        result.tracks = mask(&policy.signal_layers)?;
        if !policy.per_net_signal_layers.is_empty() {
            work.charge(input.nets.len() as u64)?;
            let by_name: BTreeMap<_, _> =
                input.nets.iter().map(|n| (n.name.as_str(), n.id)).collect();
            for (name, allowed) in &policy.per_net_signal_layers {
                let net = by_name.get(name.as_str()).ok_or_else(|| {
                    Failure::new(
                        Status::Invalid,
                        format!("layer policy names missing board net {name}"),
                    )
                })?;
                result.by_net.insert(*net, mask(allowed)?);
            }
        }
        if !policy.layer_costs.is_empty() && policy.layer_costs.iter().any(|&c| c != 1.0) {
            // Keep integer policies exact. Fractional costs use one million
            // ticks per ordinary grid step, including via and history costs.
            if policy.layer_costs.iter().any(|c| c.fract() != 0.0) {
                result.unit = 1_000_000;
            }
            result.costs = vec![result.unit; layers.len()];
            for (name, cost) in policy.signal_layers.iter().zip(&policy.layer_costs) {
                let i = layers
                    .iter()
                    .position(|l| l == name)
                    .expect("checked layer");
                result.costs[i] = (*cost * result.unit as f64).round() as u64;
            }
        }
        Ok(result)
    }
    pub(crate) fn track_allowed(&self, net: u32, layer: usize) -> bool {
        self.by_net.get(&net).copied().unwrap_or(self.tracks) & (1u32 << layer) != 0
    }
    pub(crate) fn via_allowed(&self, net: u32, start: usize, end: usize) -> bool {
        self.by_net
            .get(&net)
            .is_none_or(|mask| span_mask(start, end) & !mask == 0)
    }
    pub(crate) fn net_mask(&self, net: u32) -> Option<u32> {
        self.by_net.get(&net).copied()
    }
    pub(crate) fn weight(&self, layer: usize) -> u64 {
        self.costs.get(layer).copied().unwrap_or(self.unit)
    }
    pub(crate) fn minimum_step(&self, net: u32) -> u64 {
        self.costs
            .iter()
            .enumerate()
            .filter(|(l, _)| self.track_allowed(net, *l))
            .map(|(_, &cost)| cost)
            .min()
            .unwrap_or(self.unit)
    }
    pub(crate) fn scale(&self, raw: u64) -> u64 {
        raw.saturating_mul(self.unit)
    }
    pub(crate) fn via_step(&self, via_cost: u64, congestion: u64) -> Result<u64, Failure> {
        via_cost
            .checked_add(congestion)
            .and_then(|cost| cost.checked_mul(self.unit))
            .ok_or_else(|| {
                Failure::new(
                    Status::Invalid,
                    "via cost exceeds the supported 64-bit range",
                )
            })
    }
    pub(crate) fn unscale(&self, weighted: u64) -> u64 {
        weighted.div_ceil(self.unit)
    }
    pub(crate) fn step(&self, layer: usize, congestion: u64) -> u64 {
        self.weight(layer).saturating_add(self.scale(congestion))
    }
    pub(crate) fn length_cost(&self, layer: usize, nm: u64) -> u64 {
        ((u128::from(nm) * u128::from(self.weight(layer)) + u128::from(self.unit) / 2)
            / u128::from(self.unit)) as u64
    }
}
