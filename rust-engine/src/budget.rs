//! Deterministic forward-work accounting. Rollback never consumes this budget.
use crate::model::{Failure, Limits, Status};
use std::cell::Cell;

#[derive(Debug)]
pub(crate) struct WorkBudget {
    pub limits: Limits,
    used: Cell<u64>,
    active: Cell<bool>,
    pending_setup_work: Cell<u64>,
}
impl WorkBudget {
    pub fn new(limits: Limits) -> Self {
        Self {
            limits,
            used: Cell::new(0),
            active: Cell::new(false),
            pending_setup_work: Cell::new(0),
        }
    }
    pub fn used(&self) -> u64 {
        self.used.get()
    }
    #[cold]
    #[inline(never)]
    pub fn failure(&self, status: Status, reason: &str) -> Failure {
        let mut e = Failure::new(status, reason);
        e.work_used = self.used();
        e
    }
    #[inline(always)]
    pub fn remaining(&self) -> u64 {
        self.limits.max_work - self.used()
    }
    #[inline(always)]
    pub fn advance(&self, units: u64) {
        self.used.set(self.used() + units);
    }
    #[inline(always)]
    pub fn charge(&self, units: u64) -> Result<(), Failure> {
        if units > self.remaining() {
            return Err(self.failure(Status::WorkExhausted, "total engine work limit exhausted"));
        }
        self.advance(units);
        Ok(())
    }
    pub fn carry_setup_into_first_scope(&self) {
        self.pending_setup_work.set(self.used());
    }
    // Nested entry must never reset consumed work, including after handled errors.
    pub fn start(&self) -> bool {
        let outer = !self.active.replace(true);
        if outer {
            self.used.set(self.pending_setup_work.replace(0));
        }
        outer
    }
    pub fn end(&self, outer: bool) {
        if outer {
            self.active.set(false);
        }
    }
    pub fn scope(&self) -> WorkScope<'_> {
        WorkScope {
            budget: self,
            outer: self.start(),
        }
    }
    pub fn search_storage(&self, entries: usize) -> Result<(), Failure> {
        if entries > self.limits.max_search_entries {
            return Err(self.failure(
                Status::AdmissionExceeded,
                "search storage entry limit exceeded",
            ));
        }
        Ok(())
    }
}
pub(crate) struct WorkScope<'a> {
    budget: &'a WorkBudget,
    outer: bool,
}
impl Drop for WorkScope<'_> {
    fn drop(&mut self) {
        self.budget.end(self.outer);
    }
}
