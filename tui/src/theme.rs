//! Color themes. The default is inspired by Catppuccin Mocha.

use crate::model::Cat;
use ratatui::style::{Color, Modifier, Style};

#[derive(Clone, Debug)]
pub struct Theme {
    pub bg: Color,
    pub panel: Color,
    pub surface: Color,
    pub surface2: Color,
    pub overlay: Color,
    pub subtext: Color,
    pub text: Color,
    pub accent: Color,
    pub accent2: Color,
    pub blue: Color,
    pub sapphire: Color,
    pub teal: Color,
    pub green: Color,
    pub yellow: Color,
    pub peach: Color,
    pub red: Color,
    pub mauve: Color,
    pub pink: Color,
    pub truecolor: bool,
}

impl Theme {
    pub fn mocha() -> Theme {
        Theme {
            bg: Color::Rgb(30, 30, 46),
            panel: Color::Rgb(24, 24, 37),
            surface: Color::Rgb(49, 50, 68),
            surface2: Color::Rgb(69, 71, 90),
            overlay: Color::Rgb(108, 112, 134),
            subtext: Color::Rgb(166, 173, 200),
            text: Color::Rgb(205, 214, 244),
            accent: Color::Rgb(203, 166, 247),
            accent2: Color::Rgb(180, 190, 254),
            blue: Color::Rgb(137, 180, 250),
            sapphire: Color::Rgb(116, 199, 236),
            teal: Color::Rgb(148, 226, 213),
            green: Color::Rgb(166, 227, 161),
            yellow: Color::Rgb(249, 226, 175),
            peach: Color::Rgb(250, 179, 135),
            red: Color::Rgb(243, 139, 168),
            mauve: Color::Rgb(203, 166, 247),
            pink: Color::Rgb(245, 194, 231),
            truecolor: true,
        }
    }

    pub fn latte() -> Theme {
        Theme {
            bg: Color::Rgb(239, 241, 245),
            panel: Color::Rgb(230, 233, 239),
            surface: Color::Rgb(204, 208, 218),
            surface2: Color::Rgb(188, 192, 204),
            overlay: Color::Rgb(140, 143, 161),
            subtext: Color::Rgb(92, 95, 119),
            text: Color::Rgb(76, 79, 105),
            accent: Color::Rgb(136, 57, 239),
            accent2: Color::Rgb(114, 135, 253),
            blue: Color::Rgb(30, 102, 245),
            sapphire: Color::Rgb(32, 159, 181),
            teal: Color::Rgb(23, 146, 153),
            green: Color::Rgb(64, 160, 43),
            yellow: Color::Rgb(223, 142, 29),
            peach: Color::Rgb(254, 100, 11),
            red: Color::Rgb(210, 15, 57),
            mauve: Color::Rgb(136, 57, 239),
            pink: Color::Rgb(234, 118, 203),
            truecolor: true,
        }
    }

    /// Uses the 16 colors of the terminal palette.
    pub fn terminal() -> Theme {
        Theme {
            bg: Color::Reset,
            panel: Color::Reset,
            surface: Color::DarkGray,
            surface2: Color::DarkGray,
            overlay: Color::DarkGray,
            subtext: Color::Gray,
            text: Color::Reset,
            accent: Color::Magenta,
            accent2: Color::LightBlue,
            blue: Color::Blue,
            sapphire: Color::Cyan,
            teal: Color::Cyan,
            green: Color::Green,
            yellow: Color::Yellow,
            peach: Color::LightRed,
            red: Color::Red,
            mauve: Color::Magenta,
            pink: Color::LightMagenta,
            truecolor: false,
        }
    }

    pub fn named(name: &str) -> Theme {
        match name {
            "latte" | "light" => Theme::latte(),
            "terminal" | "none" | "16" => Theme::terminal(),
            _ => {
                let colorterm = std::env::var("COLORTERM").unwrap_or_default();
                let term = std::env::var("TERM").unwrap_or_default();
                if colorterm.contains("truecolor")
                    || colorterm.contains("24bit")
                    || term.contains("direct")
                    || name == "mocha"
                {
                    Theme::mocha()
                } else if term.contains("256") {
                    Theme::mocha().indexed()
                } else {
                    Theme::terminal()
                }
            }
        }
    }

    /// Approximates the colors with the 256-color palette.
    pub fn indexed(self) -> Theme {
        fn convert(c: Color) -> Color {
            match c {
                Color::Rgb(r, g, b) => {
                    let q = |x: u8| ((x as f64 / 255.0) * 5.0).round() as u8;
                    Color::Indexed(16 + 36 * q(r) + 6 * q(g) + q(b))
                }
                other => other,
            }
        }
        Theme {
            bg: Color::Reset,
            panel: Color::Reset,
            surface: convert(self.surface),
            surface2: convert(self.surface2),
            overlay: convert(self.overlay),
            subtext: convert(self.subtext),
            text: Color::Reset,
            accent: convert(self.accent),
            accent2: convert(self.accent2),
            blue: convert(self.blue),
            sapphire: convert(self.sapphire),
            teal: convert(self.teal),
            green: convert(self.green),
            yellow: convert(self.yellow),
            peach: convert(self.peach),
            red: convert(self.red),
            mauve: convert(self.mauve),
            pink: convert(self.pink),
            truecolor: false,
        }
    }

    pub fn cat(&self, cat: Cat) -> Color {
        match cat {
            Cat::Done => self.green,
            Cat::Running => self.sapphire,
            Cat::Pending => self.overlay,
            Cat::Failed => self.red,
            Cat::Cancelled => self.peach,
            Cat::Unknown => self.mauve,
        }
    }

    pub fn state(&self, state: &str) -> Color {
        match state {
            "TIMEOUT" | "OUT_OF_MEMORY" | "NODE_FAIL" => self.pink,
            _ => self.cat(Cat::of(state)),
        }
    }

    pub fn base(&self) -> Style {
        Style::default().fg(self.text).bg(self.bg)
    }

    pub fn dim(&self) -> Style {
        Style::default().fg(self.overlay)
    }

    pub fn sub(&self) -> Style {
        Style::default().fg(self.subtext)
    }

    pub fn bold(&self) -> Style {
        Style::default().fg(self.text).add_modifier(Modifier::BOLD)
    }

    /// Linear interpolation between two colors (truecolor only).
    pub fn lerp(&self, a: Color, b: Color, t: f64) -> Color {
        match (a, b) {
            (Color::Rgb(r1, g1, b1), Color::Rgb(r2, g2, b2)) if self.truecolor => {
                let t = t.clamp(0.0, 1.0);
                let m = |x: u8, y: u8| (x as f64 + (y as f64 - x as f64) * t).round() as u8;
                Color::Rgb(m(r1, r2), m(g1, g2), m(b1, b2))
            }
            _ => {
                if t < 0.5 {
                    a
                } else {
                    b
                }
            }
        }
    }

    /// Gradient used by progress bars, from blue to teal to green.
    pub fn gradient(&self, t: f64) -> Color {
        if t < 0.5 {
            self.lerp(self.blue, self.teal, t * 2.0)
        } else {
            self.lerp(self.teal, self.green, (t - 0.5) * 2.0)
        }
    }
}
