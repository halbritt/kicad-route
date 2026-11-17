pub mod geometry;
pub mod model;
pub mod search;
pub mod state;
use model::*;
use state::CopperStore;
use std::collections::BTreeSet;

pub struct Engine {
    input: Input,
    copper: CopperStore,
}
/// A borrowed view inside `Engine::transaction`. It cannot be constructed,
/// committed, rolled back or retained outside its closure by the caller.
/// Operations have their own nested scope, so a handled routing error does
/// not discard earlier work in the enclosing transaction.
pub struct Transaction<'a> {
    engine: &'a mut Engine,
}
impl Transaction<'_> {
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
    ) -> Result<T, E> {
        self.engine.transaction(operation)
    }
}

// This guard is never handed to user code. Even forgetting the borrowed
// Transaction view cannot bypass rollback or move a frame to another engine.
struct Scope<'a> {
    engine: &'a mut Engine,
    committed: bool,
}
impl Drop for Scope<'_> {
    fn drop(&mut self) {
        if !self.committed {
            self.engine.copper.rollback();
        }
    }
}

impl Engine {
    pub fn new(mut input: Input) -> Result<Self, Failure> {
        input.validate()?;
        let reserved = input
            .pads
            .iter()
            .map(|p| p.id.clone())
            .chain(input.tracks.iter().map(|t| t.id.clone()))
            .collect();
        let copper = CopperStore::new(
            std::mem::take(&mut input.tracks),
            reserved,
            &input.source_id,
        );
        Ok(Self { input, copper })
    }
    /// Group routing operations atomically. `Ok` commits this scope; `Err`
    /// or a panic unwinding through it rolls it back. Inner commits remain
    /// part of the parent's undo history. Panics are not caught or converted.
    /// Abort, process termination and allocation failure are not recovered.
    ///
    /// The borrowed view cannot escape (there are no reusable checkpoint tokens):
    /// ```compile_fail
    /// use kicad_route_engine::Engine;
    /// fn escape(engine: &mut Engine) {
    ///     let mut saved = None;
    ///     let _: Result<(), ()> = engine.transaction(|tx| {
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
    ///     let _: Result<(), Failure> = engine.transaction(|parent| {
    ///         parent.transaction(|child| {
    ///             parent.route(1, 100)?;
    ///             child.route(2, 100)?;
    ///             Ok(())
    ///         })
    ///     });
    /// }
    /// ```
    ///
    /// Views from independent engines cannot be swapped across their scopes:
    /// ```compile_fail
    /// use kicad_route_engine::Engine;
    /// fn swap_scopes(left: &mut Engine, right: &mut Engine) {
    ///     let _: Result<(), ()> = left.transaction(|a| {
    ///         right.transaction(|b| {
    ///             std::mem::swap(a, b);
    ///             Ok(())
    ///         })
    ///     });
    /// }
    /// ```
    pub fn transaction<T, E>(
        &mut self,
        operation: impl FnOnce(&mut Transaction<'_>) -> Result<T, E>,
    ) -> Result<T, E> {
        self.copper.begin();
        let mut scope = Scope {
            engine: self,
            committed: false,
        };
        let result = operation(&mut Transaction {
            engine: scope.engine,
        });
        if result.is_ok() {
            scope.engine.copper.commit();
            scope.committed = true;
        }
        result
    }
    pub fn copper(&self) -> &CopperStore {
        &self.copper
    }
    pub fn legal_edge(&self, net: u32, a: Point, b: Point) -> bool {
        let r = self.input.rules;
        let bounds = self.input.bounds;
        let margin = i128::from(r.width_nm) + 2 * i128::from(r.edge_clearance_nm);
        for p in [a, b] {
            for (i, value) in p.iter().enumerate() {
                if 2 * i128::from(*value) < 2 * i128::from(bounds.min[i]) + margin
                    || 2 * i128::from(*value) > 2 * i128::from(bounds.max[i]) - margin
                {
                    return false;
                }
            }
        }
        if self.input.pads.iter().any(|p| {
            (p.net == 0 || p.net != net)
                && geometry::track_near_pad(a, b, r.width_nm, r.clearance_nm, p, false)
        }) {
            return false;
        }
        !self.copper.tracks().any(|t| {
            (t.net == 0 || t.net != net)
                && geometry::segments_near(
                    a,
                    b,
                    t.start,
                    t.end,
                    i128::from(r.width_nm)
                        + i128::from(t.width_nm)
                        + 2 * i128::from(r.clearance_nm),
                    false,
                )
        })
    }
    fn terminals(&self, net: u32) -> Vec<&Pad> {
        self.input.pads.iter().filter(|p| p.net == net).collect()
    }
    fn connected(&self, a: &Pad, b: &Pad) -> bool {
        if a.net == 0 || a.net != b.net {
            return false;
        }
        if geometry::pads_touch(a, b) {
            return true;
        }
        let tracks: Vec<_> = self.copper.tracks().filter(|t| t.net == a.net).collect();
        let mut reached = BTreeSet::new();
        let mut todo = Vec::new();
        for (i, t) in tracks.iter().enumerate() {
            if geometry::track_connects_pad(t, a) {
                reached.insert(i);
                todo.push(i);
            }
        }
        while let Some(i) = todo.pop() {
            let t = tracks[i];
            if geometry::track_connects_pad(t, b) {
                return true;
            }
            for (j, u) in tracks.iter().enumerate() {
                if !reached.contains(&j) && geometry::tracks_connect(t, u) {
                    reached.insert(j);
                    todo.push(j);
                }
            }
        }
        false
    }
    fn route_net(&mut self, net: u32, limit: u64, expanded: &mut u64) -> Result<(), Failure> {
        let terminals = self.terminals(net);
        if terminals.len() != 2 || self.connected(terminals[0], terminals[1]) {
            return Ok(());
        }
        let path = search::search(self, net, terminals[0].at, terminals[1].at, limit, expanded)?;
        // Coalesce only collinear steps: the swept geometry is unchanged.
        let mut points: Vec<Point> = Vec::new();
        for p in path.points {
            if points.len() >= 2 {
                let a = points[points.len() - 2];
                let b = points[points.len() - 1];
                if (a[0] == b[0] && b[0] == p[0]) || (a[1] == b[1] && b[1] == p[1]) {
                    points.pop();
                }
            }
            points.push(p);
        }
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
        if proposal
            .iter()
            .any(|t| !self.legal_edge(net, t.start, t.end))
        {
            return Err(Failure::new(
                Status::Unroutable,
                "proposal failed continuous clearance validation",
            ));
        }
        for t in proposal {
            self.copper.insert(t);
        }
        Ok(())
    }
    pub fn route_all(&mut self) -> Result<u64, Failure> {
        self.transaction(|tx| {
            let engine = &mut *tx.engine;
            let mut expanded = 0;
            let mut nets: Vec<_> = engine.input.nets.iter()
                .filter(|n| n.id != 0).map(|n| n.id).collect();
            nets.sort();
            for net in nets {
                if let Err(mut e) = engine.route_net(net, engine.input.max_expansions, &mut expanded) {
                    e.expanded_states = expanded;
                    if e.status == Status::Unroutable {
                        e.reason = format!("net {net}: {}; failure for fixed net order/current copper, not proof of global infeasibility", e.reason);
                    }
                    return Err(e);
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
        if net == 0
            || max_expansions == 0
            || max_expansions > MAX_EXPANSIONS
            || !self.input.nets.iter().any(|n| n.id == net)
        {
            return Err(Failure::new(
                Status::Invalid,
                "routing needs a known nonzero net and budget 1..=1000000",
            ));
        }
        self.transaction(|tx| {
            if replace {
                tx.engine.copper.remove_net(net);
            }
            let mut expanded = 0;
            tx.engine
                .route_net(net, max_expansions, &mut expanded)
                .map_err(|mut e| {
                    e.expanded_states = expanded;
                    e
                })?;
            Ok(expanded)
        })
    }
}
impl Output {
    pub fn failure(source_id: String, e: Failure) -> Self {
        Self {
            version: 1,
            source_id,
            status: e.status,
            added_tracks: Vec::new(),
            removed_track_ids: Vec::new(),
            expanded_states: e.expanded_states,
            reason: Some(e.reason),
        }
    }
}
pub fn run(input: Input) -> Output {
    let source = input.source_id.clone();
    let mut engine = match Engine::new(input) {
        Ok(e) => e,
        Err(e) => return Output::failure(source, e),
    };
    match engine.route_all() {
        Ok(expanded) => Output {
            version: 1,
            source_id: source,
            status: Status::Complete,
            added_tracks: engine.copper.generated().cloned().collect(),
            removed_track_ids: Vec::new(),
            expanded_states: expanded,
            reason: None,
        },
        Err(e) => Output::failure(source, e),
    }
}
pub fn run_json(bytes: &[u8]) -> Output {
    // Deserialize directly into the contract: going through Value first would
    // erase duplicate keys, including a second `tracks` field hiding copper.
    match serde_json::from_slice::<Input>(bytes) {
        Ok(input) => run(input),
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
    }
}
