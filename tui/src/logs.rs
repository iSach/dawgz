//! Bounded log reading with terminal semantics (carriage returns, ANSI colors).

use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

const MAX_BYTES: u64 = 4 << 20;

#[derive(Clone)]
pub struct Log {
    pub lines: Vec<String>,
    pub size: u64,
    pub truncated: bool,
}

/// Reads the end of a log (at most 4 MiB), rendered as a terminal would.
pub fn read(path: &Path) -> Option<Log> {
    read_tail(path, MAX_BYTES)
}

/// Reads at most the last `max_bytes` of a log.
pub fn read_tail(path: &Path, max_bytes: u64) -> Option<Log> {
    let mut f = File::open(path).ok()?;
    let size = f.metadata().ok()?.len();
    let start = size.saturating_sub(max_bytes);
    f.seek(SeekFrom::Start(start)).ok()?;
    let mut data = Vec::with_capacity((size - start) as usize);
    f.read_to_end(&mut data).ok()?;

    let text = String::from_utf8_lossy(&data);
    let mut lines = cook(&text);

    if start > 0 && !lines.is_empty() {
        lines.remove(0); // partial line
    }
    while lines.last().is_some_and(|l| l.is_empty()) {
        lines.pop();
    }

    Some(Log {
        lines,
        size,
        truncated: start > 0,
    })
}

/// Collapses carriage-return redraws, keeping what a terminal would display.
pub fn cook(text: &str) -> Vec<String> {
    text.split('\n')
        .map(|line| {
            if !line.contains('\r') {
                return line.to_string();
            }
            let mut out: Vec<char> = Vec::new();
            for segment in line.split('\r') {
                for (k, c) in segment.chars().enumerate() {
                    if k < out.len() {
                        out[k] = c;
                    } else {
                        out.push(c);
                    }
                }
            }
            out.into_iter().collect()
        })
        .collect()
}

fn ansi_color(code: u8) -> Color {
    match code {
        0 => Color::Black,
        1 => Color::Red,
        2 => Color::Green,
        3 => Color::Yellow,
        4 => Color::Blue,
        5 => Color::Magenta,
        6 => Color::Cyan,
        _ => Color::Gray,
    }
}

fn bright_color(code: u8) -> Color {
    match code {
        0 => Color::DarkGray,
        1 => Color::LightRed,
        2 => Color::LightGreen,
        3 => Color::LightYellow,
        4 => Color::LightBlue,
        5 => Color::LightMagenta,
        6 => Color::LightCyan,
        _ => Color::White,
    }
}

/// Converts a line with ANSI escape sequences into styled spans.
pub fn styled(line: &str, base: Style) -> Line<'static> {
    let mut spans = Vec::new();
    let mut style = base;
    let mut text = String::new();
    let mut chars = line.chars().peekable();

    while let Some(c) = chars.next() {
        if c == '\x1b' && chars.peek() == Some(&'[') {
            chars.next();
            let mut params = String::new();
            let mut end = ' ';
            for c in chars.by_ref() {
                if c.is_ascii_alphabetic() {
                    end = c;
                    break;
                }
                params.push(c);
            }
            if end != 'm' {
                continue; // cursor movements, erase, ...
            }
            if !text.is_empty() {
                spans.push(Span::styled(std::mem::take(&mut text), style));
            }
            let codes: Vec<u16> = params.split(';').map(|p| p.parse().unwrap_or(0)).collect();
            let mut k = 0;
            while k < codes.len() {
                match codes[k] {
                    0 => style = base,
                    1 => style = style.add_modifier(Modifier::BOLD),
                    2 => style = style.add_modifier(Modifier::DIM),
                    3 => style = style.add_modifier(Modifier::ITALIC),
                    4 => style = style.add_modifier(Modifier::UNDERLINED),
                    22 => style = style.remove_modifier(Modifier::BOLD | Modifier::DIM),
                    23 => style = style.remove_modifier(Modifier::ITALIC),
                    24 => style = style.remove_modifier(Modifier::UNDERLINED),
                    c @ 30..=37 => style = style.fg(ansi_color((c - 30) as u8)),
                    39 => style = style.fg(base.fg.unwrap_or(Color::Reset)),
                    c @ 90..=97 => style = style.fg(bright_color((c - 90) as u8)),
                    38 if codes.get(k + 1) == Some(&5) => {
                        if let Some(&n) = codes.get(k + 2) {
                            style = style.fg(Color::Indexed(n as u8));
                        }
                        k += 2;
                    }
                    38 if codes.get(k + 1) == Some(&2) => {
                        if let (Some(&r), Some(&g), Some(&b)) =
                            (codes.get(k + 2), codes.get(k + 3), codes.get(k + 4))
                        {
                            style = style.fg(Color::Rgb(r as u8, g as u8, b as u8));
                        }
                        k += 4;
                    }
                    _ => {}
                }
                k += 1;
            }
        } else if c == '\t' {
            text.push_str("    ");
        } else if !c.is_control() {
            text.push(c);
        }
    }

    if !text.is_empty() {
        spans.push(Span::styled(text, style));
    }

    Line::from(spans)
}

pub fn strip(line: &str) -> String {
    styled(line, Style::default())
        .spans
        .iter()
        .map(|s| s.content.as_ref())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cooks_carriage_returns() {
        assert_eq!(cook("abcd\refg"), vec!["efgd"]);
        assert_eq!(cook("10%\r50%\r100%\nnext"), vec!["100%", "next"]);
    }

    #[test]
    fn parses_ansi() {
        let line = styled("\x1b[31merror\x1b[0m ok\x1b[2K", Style::default());
        assert_eq!(line.spans.len(), 2);
        assert_eq!(line.spans[0].content, "error");
        assert_eq!(line.spans[0].style.fg, Some(Color::Red));
        assert_eq!(strip("\x1b[1;32mhi\x1b[0m"), "hi");
    }
}
