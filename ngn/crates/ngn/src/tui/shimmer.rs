use std::time::Duration;

use ratatui::style::Color;

fn smootherstep(x: f64) -> f64 {
    x.powi(3) * (x * (x * 6. - 15.) + 10.)
}

pub(super) struct Sweep {
    head: f64,
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
}
