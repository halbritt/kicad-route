//! Deterministic forward-work accounting. Rollback never consumes this budget.
use crate::model::{Failure, Limits, Status};
use std::cell::Cell;

#[derive(Debug)]
pub(crate) struct WorkBudget {
    pub limits: Limits,
    used: Cell<u64>,
    active: Cell<bool>,
}
impl WorkBudget {
    pub fn new(limits: Limits) -> Self {
        Self {
            limits,
            used: Cell::new(0),
            active: Cell::new(false),
        }
    }
    pub fn used(&self) -> u64 {
        self.used.get()
    }
    pub fn failure(&self, status: Status, reason: &str) -> Failure {
        let mut e = Failure::new(status, reason);
        e.work_used = self.used();
        e
    }
    #[inline(always)]
    pub fn charge(&self, units: u64) -> Result<(), Failure> {
        if units > self.limits.max_work - self.used() {
            return Err(self.failure(Status::WorkExhausted, "total engine work limit exhausted"));
        }
        self.used.set(self.used() + units);
        Ok(())
    }
    // Nested entry must never reset consumed work, including after handled errors.
    pub fn start(&self) -> bool {
        let outer = !self.active.replace(true);
        if outer {
            self.used.set(0);
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
