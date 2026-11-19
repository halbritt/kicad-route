//! Deterministic forward-work accounting. Rollback never consumes this budget.
use crate::model::{Failure, Limits, Status};
use std::{cell::Cell, time::Instant};

const CONTROL_POLL_UNITS: u64 = 8192;

/// Operation-wide wall clock and cancellation, independent of the work limit.
#[derive(Clone, Copy, Debug)]
pub(crate) struct RouteControl {
    pub deadline: Instant,
    pub cancelled: fn() -> bool,
}

#[derive(Debug)]
pub(crate) struct WorkBudget {
    pub limits: Limits,
    used: Cell<u64>,
    active: Cell<bool>,
    pending_setup_work: Cell<u64>,
    control: Option<RouteControl>,
    next_poll: Cell<u64>,
}
impl WorkBudget {
    pub fn new(limits: Limits) -> Self {
        Self::with_control(limits, None)
    }
    pub fn with_control(limits: Limits, control: Option<RouteControl>) -> Self {
        Self {
            limits,
            used: Cell::new(0),
            active: Cell::new(false),
            pending_setup_work: Cell::new(0),
            control,
            next_poll: Cell::new(if control.is_some() { 0 } else { u64::MAX }),
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
    pub fn charge(&self, units: u64) -> Result<(), Failure> {
        if units > self.remaining() {
            return Err(self.failure(Status::WorkExhausted, "total engine work limit exhausted"));
        }
        let next = self.used() + units;
        if next >= self.next_poll.get() {
            self.poll_control(next)?;
        }
        self.used.set(next);
        Ok(())
    }
    #[inline(never)]
    fn poll_control(&self, next: u64) -> Result<(), Failure> {
        if let Some(control) = self.control {
            if (control.cancelled)() {
                return Err(self.failure(Status::Cancelled, "routing was cancelled"));
            }
            if Instant::now() >= control.deadline {
                return Err(self.failure(
                    Status::OperationTimeout,
                    "routing wall-clock deadline exceeded",
                ));
            }
            self.next_poll.set(next + CONTROL_POLL_UNITS);
        }
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
            if self.control.is_some() {
                self.next_poll.set(0);
            }
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn a_running_solve_polls_deadlines_and_cancellation_without_spending_work_on_stop() {
        thread_local! { static CANCEL: Cell<bool> = const { Cell::new(false) }; }
        let control = RouteControl {
            deadline: Instant::now() + Duration::from_secs(60),
            cancelled: || CANCEL.with(Cell::get),
        };
        let budget = WorkBudget::with_control(Limits::default(), Some(control));
        budget.charge(1).unwrap();
        CANCEL.with(|flag| flag.set(true));
        budget.charge(CONTROL_POLL_UNITS - 1).unwrap();
        let error = budget.charge(1).unwrap_err();
        assert_eq!(error.status, Status::Cancelled);
        assert_eq!(error.work_used, CONTROL_POLL_UNITS);
        assert_eq!(budget.used(), error.work_used);
        CANCEL.with(|flag| flag.set(false));

        let expired = WorkBudget::with_control(
            Limits::default(),
            Some(RouteControl {
                deadline: Instant::now(),
                ..control
            }),
        );
        assert_eq!(
            expired.charge(1).unwrap_err().status,
            Status::OperationTimeout
        );
        assert_eq!(expired.used(), 0);
    }
}
