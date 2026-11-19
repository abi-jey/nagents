use std::time::Duration;

use ratatui::style::Color;

fn smootherstep(x: f64) -> f64 {
    x.powi(3) * (x * (x * 6. - 15.) + 10.)
}

#[derive(Clone, Copy)]
pub(super) struct Sweep {
    head: f64,
}

pub(super) const COMPLETION_DURATION: Duration = Duration::from_millis(1250);
pub(super) const COMPLETE: Color = Color::Rgb(143, 174, 152);

pub(super) struct Wipe {
    progress: f64,
    head: f64,
}

impl Wipe {
    pub(super) fn new(count: usize, age: Duration) -> Self {
        let progress = (age.as_secs_f64() / COMPLETION_DURATION.as_secs_f64()).min(1.);
        Self {
            progress,
            head: -5. + smootherstep(progress) * (count as f64 + 10.),
        }
    }

    pub(super) fn color(&self, index: usize, source: Color) -> Color {
        if self.progress >= 1. {
            return COMPLETE;
        }
        // widgets.py:_paint_completion, default/unknown surface.
        let (r, g, b) = match source {
            Color::Rgb(r, g, b) => (r, g, b),
            _ => (136, 136, 136),
        };
        let distance = self.head - index as f64;
        let settled = smootherstep(((distance + 3.) / 6.).clamp(0., 1.));
        let gleam = smootherstep((1. - distance.abs() / 5.).max(0.))
            * (1. - smootherstep(((self.progress - 0.85) / 0.15).clamp(0., 1.)))
            * 0.8;
        let [r, g, b] = [(r, 143., 182.), (g, 174., 216.), (b, 152., 187.)].map(
            |(source, final_ink, gleam_ink)| {
                let source = f64::from(source);
                let ink = source + (final_ink - source) * settled;
                (ink + (gleam_ink - ink) * gleam).round_ties_even() as u8
            },
        );
        Color::Rgb(r, g, b)
    }
}

impl Sweep {
    pub(super) fn new(count: usize, age: Duration) -> Self {
        let phase = (age.as_secs_f64() % 1.8) / 1.8;
        Self {
            head: -4. + smootherstep(phase) * (count as f64 + 12.),
        }
    }

    pub(super) fn color(&self, index: usize) -> Color {
        // widgets.py:634–658, default/unknown surface. The native terminal
        // does not know the composer's theme foreground.
        let distance = index as f64 - self.head;
        let amount = smootherstep((1. - distance.abs() / 5.).max(0.));
        let blend = ((distance + 5.) / 10.).clamp(0., 1.);
        let [r, g, b] = [(129., 181.), (199., 165.), (190., 223.)].map(|(a, b)| {
            let tint = a + (b - a) * blend;
            (136. + (tint - 136.) * amount).round_ties_even() as u8
        });
        Color::Rgb(r, g, b)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_legacy_window_palette_and_period() {
        let mid = Sweep::new(40, Duration::from_millis(900)); // head = 22
        let next = Sweep::new(40, Duration::from_millis(2700));
        assert_eq!(mid.color(17), Color::Rgb(136, 136, 136));
        assert_eq!(mid.color(20), Color::Rgb(142, 172, 180));
        assert_eq!(mid.color(22), Color::Rgb(155, 182, 206));
        assert_eq!(mid.color(24), Color::Rgb(156, 163, 189));
        assert_eq!(mid.color(27), Color::Rgb(136, 136, 136));
        for index in 0..40 {
            assert_eq!(mid.color(index), next.color(index));
        }
        // Quarter phase is eased (head ≈ 1.38), not a linear head at 9.
        let quarter = Sweep::new(40, Duration::from_millis(450));
        assert_ne!(quarter.color(1), Color::Rgb(136, 136, 136));
        assert_eq!(quarter.color(9), Color::Rgb(136, 136, 136));
    }

    #[test]
    fn completion_matches_python_smootherstep_gleam_and_settled_palette() {
        let source = Color::Rgb(129, 199, 190);
        let gray = Color::Rgb(136, 136, 136);
        let initial = Wipe::new(40, Duration::ZERO);
        assert_eq!(initial.color(0, source), source);
        assert_eq!(initial.color(39, Color::Reset), gray);
        let quarter = Wipe::new(40, Duration::from_micros(312_500));
        assert_eq!(quarter.color(0, gray), Color::Rgb(174, 204, 179));
        assert_eq!(quarter.color(20, gray), gray);
        let middle = Wipe::new(40, Duration::from_millis(625));
        assert_eq!(middle.color(0, gray), COMPLETE);
        assert_eq!(middle.color(20, gray), Color::Rgb(174, 204, 178));
        assert_eq!(middle.color(39, gray), gray);
        for age in [COMPLETION_DURATION, COMPLETION_DURATION * 2] {
            let end = Wipe::new(40, age);
            assert!((0..40).all(|index| end.color(index, source) == COMPLETE));
        }
    }
}
