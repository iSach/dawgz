//! Small visual building blocks: progress bars, pills, spinners, heatmaps.

use crate::model::{Cat, Counts};
use crate::theme::Theme;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Span;

pub const SPINNER: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const EIGHTHS: [&str; 8] = [" ", "▏", "▎", "▍", "▌", "▋", "▊", "▉"];

pub fn spinner(tick: u64) -> &'static str {
    SPINNER[(tick as usize) % SPINNER.len()]
}

/// State glyph, animated for running states.
pub fn glyph(cat: Cat, tick: u64) -> &'static str {
    if cat == Cat::Running {
        spinner(tick)
    } else {
        cat.glyph()
    }
}

/// A thin progress bar (`━━━━╸───`) with a gradient fill.
pub fn thin_bar(
    theme: &Theme,
    fraction: f64,
    width: usize,
    color: Option<Color>,
) -> Vec<Span<'static>> {
    let fraction = fraction.clamp(0.0, 1.0);
    let halves = (fraction * 2.0 * width as f64).round() as usize;
    let full = halves / 2;
    let half = halves % 2 == 1;
    let mut spans = Vec::with_capacity(width);

    for k in 0..full {
        let c = color.unwrap_or_else(|| theme.gradient(k as f64 / width.max(1) as f64));
        spans.push(Span::styled("━", Style::default().fg(c)));
    }
    let mut used = full;
    if half && used < width {
        let c = color.unwrap_or_else(|| theme.gradient(used as f64 / width.max(1) as f64));
        spans.push(Span::styled("╸", Style::default().fg(c)));
        used += 1;
    }
    if used < width {
        spans.push(Span::styled(
            "━".repeat(width - used),
            Style::default().fg(theme.surface),
        ));
    }
    spans
}

/// A thick progress bar with eighth-cell precision and a gradient fill.
pub fn block_bar(theme: &Theme, fraction: f64, width: usize) -> Vec<Span<'static>> {
    let fraction = fraction.clamp(0.0, 1.0);
    let eighths = (fraction * 8.0 * width as f64).round() as usize;
    let full = eighths / 8;
    let rest = eighths % 8;
    let mut spans = Vec::with_capacity(width);

    for k in 0..full.min(width) {
        let c = theme.gradient(k as f64 / width.max(1) as f64);
        spans.push(Span::styled("█", Style::default().fg(c).bg(theme.surface)));
    }
    let mut used = full.min(width);
    if rest > 0 && used < width {
        let c = theme.gradient(used as f64 / width.max(1) as f64);
        spans.push(Span::styled(
            EIGHTHS[rest],
            Style::default().fg(c).bg(theme.surface),
        ));
        used += 1;
    }
    if used < width {
        spans.push(Span::styled(
            " ".repeat(width - used),
            Style::default().bg(theme.surface),
        ));
    }
    spans
}

/// Allocates `width` cells to segments proportionally, keeping at least one cell for
/// every non-empty segment (such that a single failure among thousands stays visible).
pub fn allocate(fractions: &[f64], width: usize) -> Vec<usize> {
    let total: f64 = fractions.iter().sum::<f64>().min(1.0);
    let target = ((total * width as f64).round() as usize).min(width);
    let mut cells: Vec<usize> = fractions
        .iter()
        .map(|f| (f * width as f64).floor() as usize)
        .collect();

    for (c, f) in cells.iter_mut().zip(fractions) {
        if *f > 0.0 && *c == 0 {
            *c = 1;
        }
    }

    // Too many cells: shrink the largest segments
    while cells.iter().sum::<usize>()
        > target
            .max(cells.iter().filter(|&&c| c > 0).count())
            .min(width)
    {
        let (k, _) = cells.iter().enumerate().max_by_key(|(_, &c)| c).unwrap();
        if cells[k] <= 1 {
            break;
        }
        cells[k] -= 1;
    }

    // Too few: grow by largest remainder
    let mut order: Vec<usize> = (0..fractions.len()).collect();
    order.sort_by(|&a, &b| {
        let ra = fractions[a] * width as f64 - cells[a] as f64;
        let rb = fractions[b] * width as f64 - cells[b] as f64;
        rb.total_cmp(&ra)
    });
    let mut k = 0;
    while cells.iter().sum::<usize>() < target && k < order.len() * 4 {
        let j = order[k % order.len()];
        if fractions[j] > 0.0 {
            cells[j] += 1;
        }
        k += 1;
    }

    cells
}

/// A bar whose segments are colored by state; running elements count by their progress.
pub fn state_bar(
    theme: &Theme,
    counts: &Counts,
    fraction: f64,
    width: usize,
    thick: bool,
) -> Vec<Span<'static>> {
    let total = counts.total().max(1) as f64;
    let finished = counts.finished() as f64 / total;
    let running = (fraction - finished).max(0.0);
    let segments = [
        (counts.done as f64 / total, theme.green),
        (running, theme.sapphire),
        (counts.failed as f64 / total, theme.red),
        (counts.cancelled as f64 / total, theme.peach),
    ];
    let cells = allocate(&segments.iter().map(|s| s.0).collect::<Vec<_>>(), width);

    let symbol = if thick { "█" } else { "━" };
    let track = if thick { " " } else { "━" };
    let mut spans = Vec::new();
    let mut used = 0usize;

    for ((_, color), n) in segments.iter().zip(cells) {
        if n == 0 || used >= width {
            continue;
        }
        let n = n.min(width - used);
        let style = if thick {
            Style::default().fg(*color).bg(theme.surface)
        } else {
            Style::default().fg(*color)
        };
        spans.push(Span::styled(symbol.repeat(n), style));
        used += n;
    }
    if used < width {
        let style = if thick {
            Style::default().bg(theme.surface)
        } else {
            Style::default().fg(theme.surface)
        };
        spans.push(Span::styled(track.repeat(width - used), style));
    }
    spans
}

/// A "pill" label: `▐ text ▌` with a colored background.
pub fn pill(text: &str, fg: Color, bg: Color, outer: Color) -> Vec<Span<'static>> {
    vec![
        Span::styled("▐", Style::default().fg(bg).bg(outer)),
        Span::styled(
            text.to_string(),
            Style::default().fg(fg).bg(bg).add_modifier(Modifier::BOLD),
        ),
        Span::styled("▌", Style::default().fg(bg).bg(outer)),
    ]
}

/// Compact colored state counts: `✔27 ●4 ✘1 ◌12`.
pub fn counts(theme: &Theme, counts: &Counts, tick: u64) -> Vec<Span<'static>> {
    let mut spans = Vec::new();
    for cat in [
        Cat::Done,
        Cat::Running,
        Cat::Failed,
        Cat::Cancelled,
        Cat::Pending,
    ] {
        let n = counts.get(cat);
        if n == 0 {
            continue;
        }
        if !spans.is_empty() {
            spans.push(Span::raw(" "));
        }
        spans.push(Span::styled(
            format!("{}{}", glyph(cat, tick), compact(n as f64)),
            Style::default().fg(theme.cat(cat)),
        ));
    }
    spans
}

/// Draws array elements as a grid of colored cells.
pub fn heatmap(
    buf: &mut Buffer,
    area: Rect,
    theme: &Theme,
    states: &[Cat],
    selected: Option<usize>,
    tick: u64,
) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    // Bigger cells for small arrays
    let cell = heatmap_cell(states.len(), area.width as usize);
    let per_row = (area.width as usize / cell).max(1);
    for (k, &cat) in states.iter().enumerate() {
        let (row, col) = (k / per_row, k % per_row);
        if row >= area.height as usize {
            break;
        }
        let mut color = theme.cat(cat);
        if cat == Cat::Running && (tick / 4 + k as u64) % 2 == 0 {
            color = theme.lerp(theme.sapphire, theme.blue, 0.6);
        }
        let mut style = Style::default().fg(color);
        if Some(k) == selected {
            style = style.bg(theme.surface2).add_modifier(Modifier::BOLD);
        }
        let x = area.x + (col * cell) as u16;
        let y = area.y + row as u16;
        if cell == 1 {
            buf[(x, y)]
                .set_symbol(if Some(k) == selected { "▣" } else { "■" })
                .set_style(style);
        } else {
            for dx in 0..cell - 1 {
                buf[(x + dx as u16, y)].set_symbol("▆").set_style(style);
            }
        }
    }
}

/// Width of heatmap cells (including a 1-cell gap when wider than 1).
pub fn heatmap_cell(n: usize, width: usize) -> usize {
    if n == 0 {
        return 1;
    }
    (width / n).clamp(1, 4)
}

/// Rows needed by a heatmap.
pub fn heatmap_rows(n: usize, width: usize) -> usize {
    let per_row = (width / heatmap_cell(n, width)).max(1);
    n.div_ceil(per_row)
}

pub fn compact(n: f64) -> String {
    if n >= 1e6 {
        format!("{:.1}M", n / 1e6)
    } else if n >= 1e4 {
        format!("{:.0}k", n / 1e3)
    } else if n.fract() != 0.0 {
        format!("{n:.1}")
    } else {
        format!("{}", n as i64)
    }
}

pub fn duration(seconds: f64) -> String {
    if !seconds.is_finite() || seconds < 0.0 {
        return String::new();
    }
    let s = seconds as u64;
    let (d, h, m, s) = (s / 86400, s % 86400 / 3600, s % 3600 / 60, s % 60);
    if d > 0 {
        format!("{d}d{h:02}h")
    } else if h > 0 {
        format!("{h}:{m:02}:{s:02}")
    } else {
        format!("{m}:{s:02}")
    }
}

pub fn age(timestamp: f64, now: f64) -> String {
    if timestamp <= 0.0 {
        return String::new();
    }
    let d = (now - timestamp).max(0.0);
    if d < 60.0 {
        format!("{}s", d as u64)
    } else if d < 3600.0 {
        format!("{}m", (d / 60.0) as u64)
    } else if d < 86400.0 {
        format!("{}h", (d / 3600.0) as u64)
    } else {
        format!("{}d", (d / 86400.0) as u64)
    }
}

pub fn size(n: u64) -> String {
    let mut x = n as f64;
    for unit in ["B", "K", "M", "G", "T"] {
        if x < 1024.0 || unit == "T" {
            return if unit == "B" {
                format!("{x:.0}{unit}")
            } else {
                format!("{x:.1}{unit}")
            };
        }
        x /= 1024.0;
    }
    format!("{x:.1}P")
}

/// Local wall-clock time `HH:MM:SS` (or `YYYY-MM-DD HH:MM` if `date`).
pub fn clock(timestamp: f64, date: bool) -> String {
    // SAFETY: localtime_r writes into the provided struct.
    unsafe {
        let t = timestamp as libc::time_t;
        let mut tm: libc::tm = std::mem::zeroed();
        libc::localtime_r(&t, &mut tm);
        if date {
            format!(
                "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
                tm.tm_year + 1900,
                tm.tm_mon + 1,
                tm.tm_mday,
                tm.tm_hour,
                tm.tm_min,
                tm.tm_sec
            )
        } else {
            format!("{:02}:{:02}:{:02}", tm.tm_hour, tm.tm_min, tm.tm_sec)
        }
    }
}

/// Subsequence fuzzy match; returns matched char positions.
pub fn fuzzy(query: &str, text: &str) -> Option<Vec<usize>> {
    if query.is_empty() {
        return Some(Vec::new());
    }
    let q: Vec<char> = query.to_lowercase().chars().collect();
    let mut positions = Vec::new();
    let mut k = 0;
    for (i, c) in text.to_lowercase().chars().enumerate() {
        if k < q.len() && c == q[k] {
            positions.push(i);
            k += 1;
        }
    }
    (k == q.len()).then_some(positions)
}

/// Spans of `text` where fuzzy-matched characters are highlighted.
pub fn highlighted(text: &str, positions: &[usize], style: Style, hl: Style) -> Vec<Span<'static>> {
    if positions.is_empty() {
        return vec![Span::styled(text.to_string(), style)];
    }
    text.chars()
        .enumerate()
        .map(|(i, c)| {
            Span::styled(
                c.to_string(),
                if positions.contains(&i) { hl } else { style },
            )
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn width(spans: &[Span]) -> usize {
        spans.iter().map(|s| s.content.chars().count()).sum()
    }

    #[test]
    fn bars_have_exact_width() {
        let theme = Theme::mocha();
        for w in [1, 5, 17, 40] {
            for f in [0.0, 0.01, 0.33, 0.5, 0.99, 1.0] {
                assert_eq!(width(&thin_bar(&theme, f, w, None)), w);
                assert_eq!(width(&block_bar(&theme, f, w)), w);
                let c = Counts {
                    done: 3,
                    running: 2,
                    failed: 1,
                    pending: 4,
                    ..Counts::default()
                };
                assert_eq!(width(&state_bar(&theme, &c, f.max(0.4), w, false)), w);
            }
        }
    }

    #[test]
    fn keeps_rare_segments_visible() {
        let cells = allocate(&[39.0 / 40.0, 0.0, 1.0 / 40.0, 0.0], 12);
        assert_eq!(cells.iter().sum::<usize>(), 12);
        assert_eq!(cells[2], 1);
        let cells = allocate(&[0.5, 0.0, 0.0, 0.0], 10);
        assert_eq!(cells, vec![5, 0, 0, 0]);
    }

    #[test]
    fn fuzzy_matches() {
        assert_eq!(fuzzy("tr", "train"), Some(vec![0, 1]));
        assert_eq!(fuzzy("tn", "train"), Some(vec![0, 4]));
        assert_eq!(fuzzy("xz", "train"), None);
    }

    #[test]
    fn formats() {
        assert_eq!(duration(61.0), "1:01");
        assert_eq!(duration(3661.0), "1:01:01");
        assert_eq!(compact(12345.0), "12k");
        assert_eq!(size(2048), "2.0K");
    }
}
