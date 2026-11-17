mod budget;
mod components;
pub mod geometry;
pub mod model;
pub mod search;
pub mod state;
use components::Copper;
use model::*;
use state::CopperStore;

pub struct Engine {
    input: Input,
    copper: CopperStore,
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

impl Engine {
    pub fn new(mut input: Input) -> Result<Self, Failure> {
        input.validate()?;
        let base_items = input.pads.len() + input.tracks.len() + input.nets.len();
        let work = budget::WorkBudget::new(input.limits);
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
            base_items,
        );
        Ok(Self {
            input,
            copper,
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
        for p in &self.input.pads {
            self.work.charge(1)?;
            if p.net == 0 || p.net != net {
                self.work.charge(1)?;
                if geometry::track_near_pad(a, b, r.width_nm, r.clearance_nm, p, false) {
                    return Ok(false);
                }
            }
        }
        for t in self.copper.tracks() {
            self.work.charge(1)?;
            if t.net == 0 || t.net != net {
                self.work.charge(1)?;
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
                    return Ok(false);
                }
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
        components::partition(items, &self.work)
    }
    fn route_net(&mut self, net: u32, limit: u64, expanded: &mut u64) -> Result<(), Failure> {
        loop {
            let groups = self.components(net)?;
            let before = groups.len();
            if before <= 1 {
                return Ok(());
            }
            let path = match (&groups[0][..], &groups[1][..]) {
                // Preserve the original two-terminal cost and expansion contract.
                ([Copper::Pad(a)], [Copper::Pad(b)]) if before == 2 => {
                    search::search(self, net, a.at, b.at, limit, expanded)?
                }
                _ => search::join_components(self, net, &groups[0], &groups[1..], limit, expanded)?,
            };
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
        for t in proposal {
            if t.start == t.end || !self.legal_edge(net, t.start, t.end)? {
                return Err(Failure::new(
                    Status::Unroutable,
                    "proposal failed continuous clearance validation",
                ));
            }
            self.copper.insert(t, &self.work)?;
        }
        Ok(())
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
pub fn run(input: Input) -> Output {
    let source = input.source_id.clone();
    let mut engine = match Engine::new(input) {
        Ok(e) => e,
        Err(e) => return Output::failure(source, e),
    };
    match engine.route_all() {
        Ok(expanded) => Output {
            version: RESULT_VERSION,
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
    if bytes.len() > 16 * 1024 * 1024 {
        return Output::failure(
            String::new(),
            Failure::new(Status::AdmissionExceeded, "JSON input exceeds 16 MiB"),
        );
    }
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
