use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};
use ratatui::{
    buffer::Buffer,
    layout::{Position, Rect},
    style::{Color, Modifier, Style},
};

struct Viewport {
    area: Rect,
    thumb: Rect,
    last_top: usize,
}

impl Viewport {
    fn new(area: Rect, top: usize, last_top: usize) -> Option<Self> {
        if area.is_empty() || area.height < 2 || last_top == 0 {
            return None;
        }
        let height = usize::from(area.height);
        let thumb_height = (height * height / (last_top + height)).max(1);
        let offset = scale(top, last_top, height - thumb_height) as u16;
        Some(Self {
            area,
            thumb: Rect::new(area.x, area.y + offset, 1, thumb_height as u16),
            last_top,
        })
    }

    fn scroll_at(&self, row: u16, grab: u16) -> Option<usize> {
        let travel = self.area.height - self.thumb.height;
        let grab = grab.min(self.thumb.height - 1);
        let offset = row.saturating_sub(self.area.y).saturating_sub(grab);
        let top = scale(
            usize::from(offset.min(travel)),
            usize::from(travel),
            self.last_top,
        );
        // The bottom of the track restores live output rather than pinning it.
        (top < self.last_top).then_some(top)
    }
}

fn scale(value: usize, from: usize, to: usize) -> usize {
    // Round to the nearest row in either direction, without overflowing on large histories.
    ((value as u128 * to as u128 + from as u128 / 2) / from as u128) as usize
}

struct Drag {
    grab: u16,
    row: u16,
}

#[derive(Default)]
pub(super) struct Scrollbar {
    viewport: Option<Viewport>,
    drag: Option<Drag>,
}

impl Scrollbar {
    pub(super) fn update(&mut self, area: Rect, (top, last_top): (usize, usize)) {
        let next = Viewport::new(area, top, last_top);
        if self.viewport.as_ref().map(|view| view.area) != next.as_ref().map(|view| view.area) {
            self.cancel_drag();
        }
        self.viewport = next;
    }

    pub(super) fn cancel_drag(&mut self) {
        self.drag = None;
    }

    pub(super) fn contains(&self, position: Position) -> bool {
        self.viewport
            .as_ref()
            .is_some_and(|view| view.area.contains(position))
    }

    pub(super) fn paint(&self, buffer: &mut Buffer, color: Color) {
        let Some(view) = &self.viewport else { return };
        let thumb = Style::new().fg(color);
        let track = thumb.add_modifier(Modifier::DIM);
        for y in view.area.top()..view.area.bottom() {
            let cell = &mut buffer[(view.area.x, y)];
            if (view.thumb.top()..view.thumb.bottom()).contains(&y) {
                cell.set_symbol("█").set_style(thumb);
            } else {
                cell.set_symbol("│").set_style(track);
            }
        }
    }

    /// Claim a scrollbar gesture before text selection or tool disclosure.
    pub(super) fn mouse(&mut self, mouse: MouseEvent, scroll: &mut Option<usize>) -> bool {
        let drag = self.drag.take();
        let Some(view) = &self.viewport else {
            return false;
        };
        if !mouse.modifiers.is_empty() {
            return false;
        }
        let position = Position::new(mouse.column, mouse.row);
        match mouse.kind {
            MouseEventKind::Down(MouseButton::Left) if view.area.contains(position) => {
                let grab = if view.thumb.contains(position) {
                    mouse.row - view.thumb.y
                } else {
                    let grab = view.thumb.height / 2;
                    *scroll = view.scroll_at(mouse.row, grab);
                    grab
                };
                self.drag = Some(Drag {
                    grab,
                    row: mouse.row,
                });
            }
            MouseEventKind::Drag(MouseButton::Left) | MouseEventKind::Up(MouseButton::Left) => {
                let Some(mut drag) = drag else { return false };
                if mouse.row != drag.row {
                    *scroll = view.scroll_at(mouse.row, drag.grab);
                    drag.row = mouse.row;
                }
                if matches!(mouse.kind, MouseEventKind::Drag(_)) {
                    self.drag = Some(drag);
                }
            }
            _ => return false,
        }
        true
    }
}
