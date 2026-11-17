//! Per-call active time. A human approval decision is not tool execution time.

use std::time::{Duration, Instant};

pub(crate) struct ToolClock {
    started: Instant,
    waited: Duration,
    paused: Option<Instant>,
}

impl ToolClock {
    pub(crate) fn new(now: Instant) -> Self {
        Self {
            started: now,
            waited: Duration::ZERO,
            paused: None,
        }
    }

    pub(crate) fn elapsed(&self, now: Instant) -> u64 {
        self.paused
            .unwrap_or(now)
            .saturating_duration_since(self.started)
            .saturating_sub(self.waited)
            .as_millis()
            .min(u64::MAX as u128) as u64
    }

    pub(crate) fn pause(&mut self, now: Instant) -> u64 {
        self.paused.get_or_insert(now);
        self.elapsed(now)
    }

    pub(crate) fn resume(&mut self, now: Instant) -> u64 {
        if let Some(started) = self.paused.take() {
            self.waited = self
                .waited
                .saturating_add(now.saturating_duration_since(started));
        }
        self.elapsed(now)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repeated_decisions_pause_one_owning_clock_without_counting_wait() {
        let start = Instant::now();
        let mut clock = ToolClock::new(start);
        assert_eq!(clock.pause(start + Duration::from_millis(12)), 12);
        assert_eq!(clock.elapsed(start + Duration::from_secs(300)), 12);
        assert_eq!(clock.pause(start + Duration::from_secs(301)), 12);
        assert_eq!(clock.resume(start + Duration::from_secs(302)), 12);
        assert_eq!(clock.elapsed(start + Duration::from_secs(303)), 1012);
        assert_eq!(clock.pause(start + Duration::from_secs(304)), 2012);
        assert_eq!(clock.resume(start + Duration::from_secs(604)), 2012);
        assert_eq!(clock.resume(start + Duration::from_secs(605)), 3012);
    }
}
