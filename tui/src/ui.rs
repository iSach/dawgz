//! Rendering.

use crate::app::{App, Focus, Popup, Row, Tab};
use crate::graph::{line_glyph, NODE_H};
use crate::logs;
use crate::model::{Cat, Counts, Entry, JobMeta};
use crate::store::{now, Summary, Workflow};
use crate::theme::Theme;
use crate::widgets::{
    self, age, block_bar, compact, counts, duration, glyph, pill, state_bar, thin_bar,
};
use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Layout, Margin, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{
    Block, BorderType, Borders, Clear, Paragraph, Scrollbar, ScrollbarOrientation, ScrollbarState,
    Wrap,
};
use ratatui::Frame;
use unicode_width::UnicodeWidthStr;

pub fn draw(f: &mut Frame, app: &mut App) {
    let area = f.area();
    let t = app.theme.clone();
    f.buffer_mut().set_style(area, t.base());

    let [header, body, footer] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(3),
        Constraint::Length(1),
    ])
    .areas(area);

    let sidebar_width = (area.width as f32 * 0.27).clamp(30.0, 46.0) as u16;
    let [sidebar, main] = if area.width < 90 {
        // Narrow terminals: hide the sidebar when the main pane is focused
        if app.focus == Focus::Workflows {
            [body, Rect::default()]
        } else {
            [Rect::default(), body]
        }
    } else {
        Layout::horizontal([Constraint::Length(sidebar_width), Constraint::Min(40)]).areas(body)
    };

    draw_header(f, app, header, &t);
    if sidebar.width > 0 {
        draw_sidebar(f, app, sidebar, &t);
    }
    if main.width > 0 {
        if app.queue.open {
            draw_queue(f, app, main, &t);
        } else {
            draw_main(f, app, main, &t);
        }
    }
    draw_footer(f, app, footer, &t);
    draw_toasts(f, app, area, &t);
    draw_popup(f, app, area, &t);

    app.areas.sidebar = sidebar;
    app.areas.main = main;
}

fn spans_width(spans: &[Span]) -> usize {
    spans.iter().map(|s| s.content.width()).sum()
}

fn truncate(text: &str, width: usize) -> String {
    if text.width() <= width {
        return text.to_string();
    }
    let mut out = String::new();
    let mut w = 0;
    for c in text.chars() {
        let cw = unicode_width::UnicodeWidthChar::width(c).unwrap_or(0);
        if w + cw + 1 > width {
            break;
        }
        out.push(c);
        w += cw;
    }
    out.push('…');
    out
}

fn clip(spans: Vec<Span<'static>>, width: usize) -> Vec<Span<'static>> {
    let mut out = Vec::new();
    let mut used = 0;
    for s in spans {
        let w = s.content.width();
        if used + w > width {
            if width > used {
                out.push(Span::styled(truncate(&s.content, width - used), s.style));
            }
            break;
        }
        used += w;
        out.push(s);
    }
    out
}

fn line_lr(left: Vec<Span<'static>>, right: Vec<Span<'static>>, width: usize) -> Line<'static> {
    // The right part never takes more than half of the line if the left part needs it
    let room = width.saturating_sub(spans_width(&left) + 1).max(width / 2);
    let right = clip(right, room);
    let left = clip(left, width.saturating_sub(spans_width(&right) + 1));
    let used = spans_width(&left) + spans_width(&right);
    let mut spans = left;
    if used < width {
        spans.push(Span::raw(" ".repeat(width - used)));
    }
    spans.extend(right);
    Line::from(spans)
}

fn panel<'a>(t: &Theme, title: Vec<Span<'a>>, focused: bool) -> Block<'a> {
    Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(if focused { t.accent } else { t.surface2 }))
        .title(Line::from(title))
        .style(Style::default().bg(t.bg))
}

// Header

fn draw_header(f: &mut Frame, app: &App, area: Rect, t: &Theme) {
    let mut totals = Counts::default();
    let mut active = 0;
    for &k in &app.visible {
        let w = &app.workflows[k];
        totals.merge(&w.totals);
        active += w.active() as usize;
    }

    let mut left = pill(" ◆ dawgz ", t.bg, t.accent, t.panel);
    let host = crate::store::hostname();
    let user = std::env::var("USER").unwrap_or_default();
    left.push(Span::styled(
        format!(" {user}@{host} "),
        Style::default().fg(t.subtext),
    ));
    let scope = if app.all_dirs {
        format!("{} dirs", app.dirs.len())
    } else {
        app.dirs
            .first()
            .map(|d| short_path(&d.display().to_string(), 40))
            .unwrap_or_default()
    };
    left.push(Span::styled(
        format!("· {scope} "),
        Style::default().fg(t.overlay),
    ));

    let mut right = Vec::new();
    right.push(Span::styled(
        format!("{} workflows", app.visible.len()),
        Style::default().fg(t.subtext),
    ));
    if active > 0 {
        right.push(Span::styled(
            format!(" ({active} active)"),
            Style::default().fg(t.sapphire),
        ));
    }
    right.push(Span::raw("  "));
    for cat in [
        Cat::Running,
        Cat::Pending,
        Cat::Done,
        Cat::Failed,
        Cat::Cancelled,
    ] {
        let n = totals.get(cat);
        if n == 0 {
            continue;
        }
        right.push(Span::styled(
            format!("{} {} ", glyph(cat, app.tick), compact(n as f64)),
            Style::default().fg(t.cat(cat)),
        ));
    }

    let slurm = if app.offline {
        Span::styled(" slurm off ", Style::default().fg(t.overlay))
    } else if app.sacct_inflight {
        Span::styled(
            format!(" {} slurm ", widgets::spinner(app.tick)),
            Style::default().fg(t.yellow),
        )
    } else if app.sacct_error.is_some() {
        Span::styled(" ⚠ slurm ", Style::default().fg(t.red))
    } else if app.sacct_last > 0.0 {
        Span::styled(
            format!(" ⟳ {} ", age(app.sacct_last, now())),
            Style::default().fg(t.subtext),
        )
    } else {
        Span::styled(" ⟳ – ", Style::default().fg(t.overlay))
    };
    right.push(slurm);
    right.extend(pill(
        &format!(" {} ", widgets::clock(now(), false)),
        t.text,
        t.surface,
        t.panel,
    ));

    let line = line_lr(left, right, area.width as usize);
    f.render_widget(
        Paragraph::new(line).style(Style::default().bg(t.panel)),
        area,
    );
}

fn short_path(path: &str, width: usize) -> String {
    let home = std::env::var("HOME").unwrap_or_default();
    let path = if !home.is_empty() && path.starts_with(&home) {
        format!("~{}", &path[home.len()..])
    } else {
        path.to_string()
    };
    if path.width() <= width {
        return path;
    }
    let tail: String = path
        .chars()
        .rev()
        .take(width - 1)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    format!("…{tail}")
}

// Sidebar

fn draw_sidebar(f: &mut Frame, app: &mut App, area: Rect, t: &Theme) {
    let focused = app.focus == Focus::Workflows;
    let mut title = vec![
        Span::styled(
            " Workflows ",
            Style::default().fg(t.text).add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            format!("{} ", app.visible.len()),
            Style::default().fg(t.overlay),
        ),
    ];
    if app.active_only {
        title.extend(pill("active", t.bg, t.sapphire, t.bg));
        title.push(Span::raw(" "));
    }
    if !app.search.query.is_empty() && (focused || app.search.active) {
        title.extend(pill(
            &format!("/{}", app.search.query),
            t.bg,
            t.yellow,
            t.bg,
        ));
        title.push(Span::raw(" "));
    }

    let block = panel(t, title, focused);
    let inner = block.inner(area);
    f.render_widget(block, area);

    let card_h = 4u16;

    // Activity panel below the cards when there is room
    let needed = app.visible.len() as u16 * card_h;
    let inner = if inner.height >= needed + 9 && inner.height >= 16 {
        let [cards, activity] =
            Layout::vertical([Constraint::Min(card_h), Constraint::Length(8)]).areas(inner);
        draw_activity(f, app, activity, t);
        cards
    } else {
        inner
    };
    let n_fit = (inner.height / card_h).max(1) as usize;
    let offset = app.selected.saturating_sub(n_fit - 1);
    let now = now();
    let width = inner.width as usize;

    app.areas.cards = inner;
    app.areas.card_height = card_h;
    app.areas.card_offset = offset;

    if app.visible.is_empty() {
        let text = if app.workflows.is_empty() {
            "no workflow yet"
        } else {
            "no match"
        };
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(text, t.dim()))).centered(),
            inner.inner(Margin {
                horizontal: 0,
                vertical: 1,
            }),
        );
        return;
    }

    for (slot, &k) in app.visible.iter().enumerate().skip(offset).take(n_fit) {
        let w = &app.workflows[k];
        let y = inner.y + ((slot - offset) as u16) * card_h;
        let card = Rect {
            x: inner.x,
            y,
            width: inner.width,
            height: card_h.min(inner.y + inner.height - y),
        };
        let selected = slot == app.selected;
        let bg = if selected { t.surface } else { t.bg };

        let state = w.totals.state();
        let cat = Cat::of(state);
        let mark = if selected {
            if focused {
                "▌"
            } else {
                "▏"
            }
        } else {
            " "
        };
        let mark_style = Style::default().fg(t.accent).bg(bg);

        // Line 1: state, name and age
        let q = if focused {
            app.search.query.as_str()
        } else {
            ""
        };
        let positions = widgets::fuzzy(q, w.name()).unwrap_or_default();
        let name = truncate(w.name(), width.saturating_sub(10));
        let mut left = vec![
            Span::styled(mark, mark_style),
            Span::styled(
                format!("{} ", glyph(cat, app.tick)),
                Style::default().fg(t.cat(cat)),
            ),
        ];
        left.extend(widgets::highlighted(
            &name,
            &positions,
            Style::default().fg(t.text).add_modifier(Modifier::BOLD),
            Style::default()
                .fg(t.yellow)
                .add_modifier(Modifier::BOLD | Modifier::UNDERLINED),
        ));
        let right = vec![Span::styled(
            format!("{} ", age(w.meta.timestamp, now)),
            t.dim(),
        )];
        let l1 = line_lr(left, right, width);

        // Line 2: progress
        let pct = format!(" {:>3.0}% ", 100.0 * w.fraction);
        let bar_w = width.saturating_sub(3 + pct.len());
        let mut l2 = vec![Span::styled(mark, mark_style), Span::raw(" ")];
        l2.extend(state_bar(t, &w.totals, w.fraction, bar_w, false));
        l2.push(Span::styled(
            pct,
            Style::default().fg(if w.active() { t.text } else { t.subtext }),
        ));

        // Line 3: backend, jobs and counts
        let backend_color = if w.is_slurm() { t.mauve } else { t.teal };
        let left = vec![
            Span::styled(mark, mark_style),
            Span::raw(" "),
            Span::styled(w.meta.backend.clone(), Style::default().fg(backend_color)),
            Span::styled(format!(" · {} jobs", w.jobs().len()), t.dim()),
        ];
        let mut right = counts(t, &w.totals, app.tick);
        right.push(Span::raw(" "));
        let l3 = line_lr(left, right, width);

        let lines = vec![l1, Line::from(l2), l3];
        f.render_widget(
            Paragraph::new(lines).style(Style::default().bg(bg)),
            Rect {
                height: 3.min(card.height),
                ..card
            },
        );
    }

    if app.visible.len() > n_fit {
        let mut state = ScrollbarState::new(app.visible.len()).position(app.selected);
        f.render_stateful_widget(
            Scrollbar::new(ScrollbarOrientation::VerticalRight)
                .begin_symbol(None)
                .end_symbol(None)
                .thumb_style(Style::default().fg(t.surface2))
                .track_style(Style::default().fg(t.bg)),
            area.inner(Margin {
                horizontal: 0,
                vertical: 1,
            }),
            &mut state,
        );
    }
}

fn draw_activity(f: &mut Frame, app: &App, area: Rect, t: &Theme) {
    let now = now();
    let bins = (area.width as usize).saturating_sub(2).clamp(10, 60);
    let window = 3600.0;
    let mut done = vec![0u64; bins];
    let (mut n_done, mut n_failed) = (0, 0);

    for &k in &app.visible {
        let w = &app.workflows[k];
        for job in w.jobs() {
            for e in w.elements(job) {
                let cat = Cat::of(e.state());
                let Some(end) = e.end.filter(|_| cat.terminal()) else {
                    continue;
                };
                let ago = now - end;
                if !(0.0..window).contains(&ago) {
                    continue;
                }
                let b = bins - 1 - ((ago / window) * bins as f64).floor() as usize;
                done[b.min(bins - 1)] += 1;
                if cat == Cat::Failed {
                    n_failed += 1;
                } else {
                    n_done += 1;
                }
            }
        }
    }

    let block = Block::default()
        .borders(Borders::TOP)
        .border_style(Style::default().fg(t.surface2))
        .title(Line::from(vec![
            Span::styled("─ ", Style::default().fg(t.surface2)),
            Span::styled(
                "Activity ",
                Style::default().fg(t.text).add_modifier(Modifier::BOLD),
            ),
            Span::styled("last hour ", t.dim()),
        ]));
    let inner = block.inner(area);
    f.render_widget(block, area);

    let [spark, axis, stats, slurm] = Layout::vertical([
        Constraint::Length(3),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .areas(inner.inner(Margin {
        horizontal: 1,
        vertical: 0,
    }));

    // Sparkline with a vertical gradient
    let max = done.iter().copied().max().unwrap_or(0).max(1) as f64;
    let levels = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"];
    let x0 = spark.x + spark.width.saturating_sub(bins as u16);
    for (b, &n) in done.iter().enumerate() {
        let h = n as f64 / max * spark.height as f64 * 8.0;
        for row in 0..spark.height {
            let filled = h - (spark.height - 1 - row) as f64 * 8.0;
            let symbol = if filled >= 8.0 {
                "█"
            } else if filled > 0.0 {
                levels[(filled as usize).min(7)]
            } else if row == spark.height - 1 {
                "▁"
            } else {
                " "
            };
            let color = if n == 0 {
                t.surface
            } else {
                t.gradient(1.0 - row as f64 / spark.height as f64 * 0.8)
            };
            if x0 + (b as u16) < spark.x + spark.width {
                f.buffer_mut()[(x0 + b as u16, spark.y + row)]
                    .set_symbol(symbol)
                    .set_style(Style::default().fg(color));
            }
        }
    }
    f.render_widget(
        Paragraph::new(line_lr(
            vec![Span::styled("-60m", t.dim())],
            vec![Span::styled("now", t.dim())],
            axis.width as usize,
        )),
        axis,
    );

    let mut spans = vec![Span::styled(
        format!("✔ {n_done} done"),
        Style::default().fg(t.green),
    )];
    if n_failed > 0 {
        spans.push(Span::styled(
            format!("  ✘ {n_failed} failed"),
            Style::default().fg(t.red),
        ));
    }
    f.render_widget(Paragraph::new(Line::from(spans)), stats);

    let text = if app.offline {
        "slurm: offline, files only".to_string()
    } else if app.sacct_last > 0.0 {
        let next = (app.ttl - (now - app.sacct_last)).max(0.0);
        format!(
            "slurm ⟳ {} ago · next ≥{} · {}×",
            age(app.sacct_last, now),
            duration(next),
            app.sacct_calls
        )
    } else {
        format!("slurm: every ≥{} when needed", duration(app.ttl))
    };
    f.render_widget(
        Paragraph::new(Span::styled(truncate(&text, slurm.width as usize), t.dim())),
        slurm,
    );
}

// Main pane

fn draw_main(f: &mut Frame, app: &mut App, area: Rect, t: &Theme) {
    let focused = app.focus == Focus::Main;
    let block = panel(t, vec![], focused);
    let inner = block.inner(area);
    f.render_widget(block, area);

    let [tabs, head, gauge, _, content] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Min(3),
    ])
    .areas(inner);

    // Tabs
    let mut spans = Vec::new();
    for (k, tab) in Tab::ALL.iter().enumerate() {
        let label = format!(" {} {} ", k + 1, tab.title());
        if *tab == app.tab {
            spans.extend(pill(
                &label,
                t.bg,
                if focused { t.accent } else { t.surface2 },
                t.bg,
            ));
        } else {
            spans.push(Span::styled(
                format!(" {label} "),
                Style::default().fg(t.subtext),
            ));
        }
    }
    app.areas.tabs = tabs;

    let Some(w) = app.current() else {
        f.render_widget(Paragraph::new(Line::from(spans)), tabs);
        empty_state(f, content, t);
        return;
    };

    let right = vec![Span::styled(format!("{} ", w.uid()), t.dim())];
    f.render_widget(
        Paragraph::new(line_lr(spans, right, tabs.width as usize)),
        tabs,
    );

    // Workflow header
    let now = now();
    let mut left = vec![Span::styled(
        format!(" {} ", w.name()),
        Style::default().fg(t.text).add_modifier(Modifier::BOLD),
    )];
    let backend_color = if w.is_slurm() { t.mauve } else { t.teal };
    left.extend(pill(&w.meta.backend, t.bg, backend_color, t.bg));
    left.push(Span::styled(
        format!(
            "  created {} ago · {} jobs",
            age(w.meta.timestamp, now),
            w.jobs().len()
        ),
        Style::default().fg(t.subtext),
    ));
    if w.totals.total() != w.jobs().len() {
        left.push(Span::styled(
            format!(" · {} tasks", w.totals.total()),
            Style::default().fg(t.subtext),
        ));
    }
    if !w.meta.cwd.is_empty() {
        left.push(Span::styled(
            format!(" · {}", short_path(&w.meta.cwd, 40)),
            t.dim(),
        ));
    }
    let mut right = Vec::new();
    if w.is_slurm() && w.cache.updated > 0.0 {
        right.push(Span::styled(
            format!("slurm states {} ago ", age(w.cache.updated, now)),
            t.dim(),
        ));
    }
    f.render_widget(
        Paragraph::new(line_lr(left, right, head.width as usize)),
        head,
    );

    // Gauge
    let mut pct = format!(" {:>3.0}% ", 100.0 * w.fraction);
    if w.active() && w.fraction > 0.02 && w.fraction < 1.0 {
        let start = w
            .cache
            .jobs
            .values()
            .flat_map(|e| std::iter::once(e.start).chain(e.tasks.values().map(|t| t.start)))
            .chain(w.runs.values().map(|r| r.start))
            .flatten()
            .fold(f64::INFINITY, f64::min);
        if start.is_finite() {
            let spent = now - start;
            pct.push_str(&format!(
                "eta ~{} ",
                duration(spent * (1.0 - w.fraction) / w.fraction)
            ));
        }
    }
    let cnt = counts(t, &w.totals, app.tick);
    let bar_w = (gauge.width as usize).saturating_sub(pct.width() + spans_width(&cnt) + 3);
    let mut spans = vec![Span::raw(" ")];
    if w.totals.failed + w.totals.cancelled > 0 {
        spans.extend(state_bar(t, &w.totals, w.fraction, bar_w, true));
    } else {
        spans.extend(block_bar(t, w.fraction, bar_w));
    }
    spans.push(Span::styled(
        pct,
        Style::default().fg(t.text).add_modifier(Modifier::BOLD),
    ));
    spans.push(Span::raw(" "));
    spans.extend(cnt);
    f.render_widget(Paragraph::new(Line::from(spans)), gauge);

    match app.tab {
        Tab::Jobs => draw_jobs(f, app, content, t),
        Tab::Graph => draw_graph(f, app, content, t),
        Tab::Logs => draw_logs(f, app, content, t),
        Tab::Info => draw_info(f, app, content, t),
    }
}

fn empty_state(f: &mut Frame, area: Rect, t: &Theme) {
    let lines = vec![
        Line::from(""),
        Line::from(Span::styled("No workflow to show", t.bold())),
        Line::from(""),
        Line::from(Span::styled("Schedule one from Python:", t.sub())),
        Line::from(Span::styled(
            "dawgz.schedule(job, backend=\"slurm\")",
            Style::default().fg(t.green),
        )),
        Line::from(""),
        Line::from(Span::styled(
            "press D to show all known directories",
            t.dim(),
        )),
    ];
    f.render_widget(Paragraph::new(lines).centered(), area);
}

// Jobs tab

struct RowView {
    index: String,
    name: String,
    depth: u8,
    summary: Summary,
    entry: Option<Entry>,
    job: Option<JobMeta>,
    jobid: String,
    elapsed: Option<f64>,
    is_group: bool,
    expandable: bool,
    expanded: bool,
}

fn elapsed(e: &Entry, now: f64) -> Option<f64> {
    let terminal = Cat::of(e.state()).terminal();
    if terminal {
        if let Some(x) = e.elapsed.filter(|x| *x > 0.0) {
            return Some(x);
        }
    }
    let start = e.start?;
    let end = if terminal { e.end.unwrap_or(now) } else { now };
    Some((end - start).max(0.0))
}

fn row_view(app: &App, w: &Workflow, row: &Row, now: f64) -> RowView {
    let jobs = w.jobs();
    match row {
        Row::Node {
            group,
            jobs: members,
        } => {
            let job = &jobs[members[0]];
            let expanded = app.expanded.contains(&(w.uid().to_string(), *group));
            if members.len() == 1 {
                let summary = w.summary(job.index).clone();
                let elements = w.elements(job);
                let times: Vec<f64> = elements.iter().filter_map(|e| elapsed(e, now)).collect();
                RowView {
                    index: job.index.to_string(),
                    name: job.label(),
                    depth: 0,
                    entry: (!job.is_array()).then(|| elements[0].clone()),
                    job: Some(job.clone()),
                    jobid: job.jobid.clone().unwrap_or_default(),
                    elapsed: times.into_iter().reduce(f64::max),
                    is_group: job.is_array(),
                    expandable: job.is_array(),
                    expanded,
                    summary,
                }
            } else {
                let mut counts = Counts::default();
                let mut fraction = 0.0;
                let mut total = 0;
                let mut times = Vec::new();
                for &k in members {
                    let s = w.summary(jobs[k].index);
                    counts.merge(&s.counts);
                    fraction += s.fraction * s.total as f64;
                    total += s.total;
                    for e in w.elements(&jobs[k]) {
                        if let Some(x) = elapsed(&e, now) {
                            times.push(x);
                        }
                    }
                }
                let last = &jobs[*members.last().unwrap()];
                let ids: Vec<&String> = members
                    .iter()
                    .filter_map(|&k| jobs[k].jobid.as_ref())
                    .collect();
                let jobid = match ids.first() {
                    Some(first) => {
                        let mut bases: Vec<u64> = ids
                            .iter()
                            .filter_map(|i| i.split('_').next().and_then(|b| b.parse().ok()))
                            .collect();
                        bases.sort();
                        bases.dedup();
                        if bases.len() == 1 && ids.len() > 1 && first.contains('_') {
                            format!("{}_[…]", bases[0])
                        } else if bases.len() > 1 && bases.len() < ids.len() {
                            format!("{}…{}", bases[0], bases[bases.len() - 1])
                        } else if ids.len() > 1 {
                            format!("{first} +{}", ids.len() - 1)
                        } else {
                            first.to_string()
                        }
                    }
                    None => String::new(),
                };
                RowView {
                    index: format!("{}-{}", job.index, last.index),
                    name: format!("{} ×{}", job.name, members.len()),
                    depth: 0,
                    summary: Summary {
                        state: counts.state().to_string(),
                        counts,
                        fraction: fraction / total.max(1) as f64,
                        total,
                    },
                    entry: None,
                    job: None,
                    jobid,
                    elapsed: times.into_iter().reduce(f64::max),
                    is_group: true,
                    expandable: true,
                    expanded,
                }
            }
        }
        Row::Member { job, .. } => {
            let job = &jobs[*job];
            let e = w.entry(job, None);
            RowView {
                index: job.index.to_string(),
                name: job.input.lines().next().unwrap_or(&job.name).to_string(),
                depth: 1,
                summary: w.summary(job.index).clone(),
                elapsed: elapsed(&e, now),
                entry: Some(e),
                job: Some(job.clone()),
                jobid: job.jobid.clone().unwrap_or_default(),
                is_group: false,
                expandable: false,
                expanded: false,
            }
        }
        Row::Element { job, i, .. } => {
            let job = &jobs[*job];
            let e = w.entry(job, Some(*i));
            let mut counts = Counts::default();
            counts.add(Cat::of(e.state()));
            let name = job
                .inputs
                .as_ref()
                .and_then(|v| v.get(*i))
                .map(|s| s.lines().next().unwrap_or("").to_string())
                .unwrap_or_else(|| format!("{}[{i}]", job.name));
            RowView {
                index: format!("{i}"),
                name,
                depth: 1,
                summary: Summary {
                    state: e.state().to_string(),
                    counts,
                    fraction: 0.0,
                    total: 1,
                },
                elapsed: elapsed(&e, now),
                entry: Some(e),
                job: Some(job.clone()),
                jobid: job
                    .jobid
                    .as_ref()
                    .map(|j| format!("{j}_{i}"))
                    .unwrap_or_default(),
                is_group: false,
                expandable: false,
                expanded: false,
            }
        }
    }
}

fn progress_spans(
    app: &App,
    w: &Workflow,
    view: &RowView,
    width: usize,
    t: &Theme,
) -> Vec<Span<'static>> {
    let bar_w = 16.min(width.saturating_sub(6));

    if view.is_group || view.summary.total > 1 {
        let mut spans = state_bar(t, &view.summary.counts, view.summary.fraction, bar_w, false);
        spans.push(Span::styled(
            format!(" {:>3.0}% ", 100.0 * view.summary.fraction),
            Style::default().fg(t.subtext),
        ));
        spans.extend(counts(t, &view.summary.counts, app.tick));
        return spans;
    }

    let Some(e) = &view.entry else { return vec![] };
    let cat = Cat::of(e.state());

    match cat {
        Cat::Running => {
            if let Some(bar) = e.main_bar() {
                let mut spans = Vec::new();
                match bar.fraction() {
                    Some(frac) => {
                        spans.extend(thin_bar(t, frac, bar_w, None));
                        spans.push(Span::styled(
                            format!(" {:>3.0}% ", 100.0 * frac),
                            Style::default().fg(t.sapphire),
                        ));
                    }
                    None => spans.push(Span::styled(
                        format!("{} ", widgets::spinner(app.tick)),
                        Style::default().fg(t.sapphire),
                    )),
                }
                spans.push(Span::styled(bar_text(bar), t.dim()));
                spans
            } else if let Some(s) = &e.status {
                vec![Span::styled(
                    format!("› {s}"),
                    Style::default().fg(t.subtext),
                )]
            } else {
                vec![Span::styled("running…", t.dim())]
            }
        }
        Cat::Pending => {
            let reason = e.reason.clone().unwrap_or_default();
            let text = match reason.as_str() {
                "Dependency" => {
                    let job = view.job.as_ref();
                    let waiting: Vec<String> = job
                        .map(|j| {
                            j.deps
                                .iter()
                                .filter(|(d, _)| {
                                    w.summaries
                                        .get(*d)
                                        .is_some_and(|s| s.counts.finished() < s.total)
                                })
                                .map(|(d, _)| format!("#{d}"))
                                .collect()
                        })
                        .unwrap_or_default();
                    if waiting.is_empty() {
                        "waiting for dependencies".to_string()
                    } else if waiting.len() > 4 {
                        format!(
                            "waiting for {}… (+{})",
                            waiting[..3].join(", "),
                            waiting.len() - 3
                        )
                    } else {
                        format!("waiting for {}", waiting.join(", "))
                    }
                }
                "DependencyNeverSatisfied" => "dependency never satisfied".to_string(),
                "JobArrayTaskLimit" => "throttled".to_string(),
                "" => "queued".to_string(),
                other => other.to_lowercase(),
            };
            vec![Span::styled(text, t.dim())]
        }
        Cat::Failed | Cat::Cancelled => {
            let mut text = e.error.clone().or(e.reason.clone()).unwrap_or_default();
            if text == "DependencyNeverSatisfied" {
                text = "dependency never satisfied".into();
            }
            let text = text.lines().last().unwrap_or("").to_string();
            let state = e.state();
            let text = if !matches!(state, "FAILED" | "CANCELLED") {
                format!("{} {text}", state.to_lowercase().replace('_', " "))
            } else {
                text
            };
            vec![Span::styled(
                text,
                Style::default().fg(if cat == Cat::Failed { t.red } else { t.peach }),
            )]
        }
        Cat::Done => {
            if let Some(bar) = e.main_bar() {
                vec![Span::styled(bar_text(bar), t.dim())]
            } else {
                vec![]
            }
        }
        Cat::Unknown => vec![],
    }
}

fn bar_text(bar: &crate::model::Bar) -> String {
    let mut parts = Vec::new();
    if !bar.desc.is_empty() && bar.desc != "progress" {
        parts.push(bar.desc.clone());
    }
    let unit = bar.unit.clone().unwrap_or_else(|| "it".into());
    match bar.total {
        Some(total) => parts.push(format!("{}/{}", compact(bar.n), compact(total))),
        None => parts.push(format!("{} {unit}", compact(bar.n))),
    }
    if let Some(rate) = bar.rate.filter(|r| *r > 0.0) {
        parts.push(if rate >= 1.0 {
            format!("{} {unit}/s", sig3(rate))
        } else {
            format!("{} s/{unit}", sig3(1.0 / rate))
        });
    }
    if let Some(eta) = bar.total.filter(|t| bar.n < *t).and(bar.eta) {
        parts.push(format!("eta {}", duration(eta)));
    }
    parts.join(" · ")
}

/// Formats a number with 3 significant digits.
fn sig3(x: f64) -> String {
    if x >= 100.0 {
        format!("{x:.0}")
    } else if x >= 10.0 {
        format!("{x:.1}")
    } else {
        format!("{x:.2}")
    }
}

fn draw_jobs(f: &mut Frame, app: &mut App, area: Rect, t: &Theme) {
    let Some(w) = app.current() else { return };
    let now = now();

    // The table takes what it needs (up to 60%), then the detail pane, then the timeline
    let wanted = app.rows.len() as u16 + 2;
    let max_table = (area.height * 3 / 5).max(5);
    let table_h = wanted
        .clamp(4, max_table.max(4))
        .min(area.height.saturating_sub(6).max(4));
    let rest = area.height.saturating_sub(table_h);
    let timeline_h = if rest >= 22 {
        (app.groups.len() as u16 + 3).min(rest - 14).min(rest / 2)
    } else {
        0
    };
    let [table, timeline, detail] = Layout::vertical([
        Constraint::Length(table_h),
        Constraint::Length(timeline_h),
        Constraint::Min(4),
    ])
    .areas(area);
    if timeline_h >= 4 {
        draw_timeline(f, app, timeline, t);
    }

    let width = table.width as usize;
    let gutter_w = app.gutter.iter().map(|r| r.len()).max().unwrap_or(0) * 2;
    let show_gutter = gutter_w > 0 && gutter_w <= 16 && app.parents.iter().any(|p| !p.is_empty());
    let gutter_w = if show_gutter { gutter_w } else { 0 };

    // Columns
    let index_w = app
        .rows
        .iter()
        .map(|r| match r {
            Row::Node { jobs, .. } if jobs.len() > 1 => {
                format!("{}-{}", jobs[0], jobs[jobs.len() - 1]).len()
            }
            _ => 3,
        })
        .max()
        .unwrap_or(3)
        .max(3);
    let time_w = 8;
    let jobid_w = if width > 100 { 12 } else { 0 };
    let state_w = 10;
    let name_w = (width.saturating_sub(index_w + gutter_w + state_w + time_w + jobid_w + 10) * 2
        / 5)
    .clamp(10, 36);
    let progress_w = width.saturating_sub(
        1 + index_w + 1 + gutter_w + 2 + name_w + 1 + state_w + 1 + time_w + 1 + jobid_w + 1,
    );

    let header = Line::from(vec![
        Span::raw(" "),
        Span::styled(format!("{:>index_w$} ", "#"), t.dim()),
        Span::raw(" ".repeat(gutter_w)),
        Span::styled(format!("  {:<name_w$} ", "JOB"), t.dim()),
        Span::styled(format!("{:<state_w$} ", "STATE"), t.dim()),
        Span::styled(format!("{:<progress_w$} ", "PROGRESS"), t.dim()),
        Span::styled(format!("{:>time_w$} ", "TIME"), t.dim()),
        Span::styled(
            if jobid_w > 0 {
                format!("{:>jobid_w$}", "JOBID")
            } else {
                String::new()
            },
            t.dim(),
        ),
    ]);

    let visible_rows = table.height.saturating_sub(1) as usize;
    let offset = app.row_sel.saturating_sub(visible_rows.saturating_sub(1));
    let mut lines = vec![header];
    let focused = app.focus == Focus::Main;

    for (k, row) in app.rows.iter().enumerate().skip(offset).take(visible_rows) {
        let view = row_view(app, w, row, now);
        let selected = k == app.row_sel;
        let bg = if selected { t.surface } else { t.bg };
        let cat = view.summary.cat();
        let mut spans = Vec::new();

        spans.push(Span::styled(
            if selected && focused { "▌" } else { " " },
            Style::default().fg(t.accent),
        ));
        spans.push(Span::styled(format!("{:>index_w$} ", view.index), t.dim()));

        // Graph gutter
        if show_gutter {
            let g = row.group();
            let cells = app.gutter.get(g).cloned().unwrap_or_default();
            let is_node = matches!(row, Row::Node { .. });
            let mut text = Vec::new();
            for c in 0..gutter_w / 2 {
                let cell = cells.get(c).copied().unwrap_or([' ', ' ']);
                let (a, b) = if is_node {
                    (cell[0], cell[1])
                } else {
                    // Continuation rows below a node
                    let a = match cell[0] {
                        '@' => {
                            if app.parents.iter().any(|p| p.contains(&g)) {
                                '│'
                            } else {
                                ' '
                            }
                        }
                        '╮' | '┼' | '│' => '│',
                        _ => ' ',
                    };
                    (a, ' ')
                };
                if a == '@' {
                    text.push(Span::styled(
                        glyph(cat, app.tick).to_string(),
                        Style::default().fg(t.state(&view.summary.state)),
                    ));
                } else {
                    text.push(Span::styled(a.to_string(), Style::default().fg(t.surface2)));
                }
                text.push(Span::styled(b.to_string(), Style::default().fg(t.surface2)));
            }
            spans.extend(text);
        }

        // Name
        let marker = if view.expandable {
            if view.expanded {
                "▾ "
            } else {
                "▸ "
            }
        } else if view.depth > 0 || show_gutter {
            "  "
        } else {
            ""
        };
        let (icon, icon_style) = if show_gutter && view.depth == 0 {
            (String::new(), Style::default())
        } else {
            (
                format!("{} ", glyph(cat, app.tick)),
                Style::default().fg(t.state(&view.summary.state)),
            )
        };
        let prefix = if view.depth > 0 { "  " } else { "" };
        let name_style = if view.depth > 0 {
            Style::default().fg(t.subtext)
        } else if cat == Cat::Running {
            Style::default().fg(t.text).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(t.text)
        };
        let q = if focused { app.job_query.as_str() } else { "" };
        let avail = name_w.saturating_sub(prefix.width() + marker.width() + icon.width());
        let name = truncate(&view.name, avail);
        let positions = widgets::fuzzy(q, &name).unwrap_or_default();
        let mut name_spans = vec![
            Span::raw(format!(" {prefix}")),
            Span::styled(marker.to_string(), Style::default().fg(t.overlay)),
            Span::styled(icon.clone(), icon_style),
        ];
        name_spans.extend(widgets::highlighted(
            &name,
            &positions,
            name_style,
            name_style.fg(t.yellow).add_modifier(Modifier::UNDERLINED),
        ));
        let used = spans_width(&name_spans);
        spans.extend(name_spans);
        spans.push(Span::raw(" ".repeat((name_w + 2).saturating_sub(used) + 1)));

        // State
        let state = view.summary.state.to_lowercase().replace('_', " ");
        spans.push(Span::styled(
            format!("{:<state_w$} ", truncate(&state, state_w)),
            Style::default().fg(t.state(&view.summary.state)),
        ));

        // Progress
        let mut prog = progress_spans(app, w, &view, progress_w, t);
        let pw = spans_width(&prog);
        if pw > progress_w {
            let mut acc = Vec::new();
            let mut used = 0;
            for s in prog {
                let sw = s.content.width();
                if used + sw > progress_w {
                    acc.push(Span::styled(
                        truncate(&s.content, progress_w - used),
                        s.style,
                    ));
                    break;
                }
                used += sw;
                acc.push(s);
            }
            prog = acc;
        }
        let pw = spans_width(&prog);
        spans.extend(prog);
        spans.push(Span::raw(" ".repeat(progress_w.saturating_sub(pw) + 1)));

        spans.push(Span::styled(
            format!(
                "{:>time_w$} ",
                view.elapsed.map(duration).unwrap_or_default()
            ),
            Style::default().fg(t.subtext),
        ));
        if jobid_w > 0 {
            spans.push(Span::styled(
                format!("{:>jobid_w$}", truncate(&view.jobid, jobid_w)),
                t.dim(),
            ));
        }

        lines.push(Line::from(spans).style(Style::default().bg(bg)));
    }

    if app.rows.is_empty() {
        lines.push(Line::from(Span::styled(
            "  no job matches the filters",
            t.dim(),
        )));
    }

    f.render_widget(Paragraph::new(lines), table);
    app.areas.table = table;
    app.areas.table_offset = offset;

    if app.rows.len() > visible_rows {
        let mut state = ScrollbarState::new(app.rows.len()).position(app.row_sel);
        f.render_stateful_widget(
            Scrollbar::new(ScrollbarOrientation::VerticalRight)
                .begin_symbol(None)
                .end_symbol(None)
                .thumb_style(Style::default().fg(t.surface2))
                .track_style(Style::default().fg(t.bg)),
            Rect {
                y: table.y + 1,
                height: table.height.saturating_sub(1),
                ..table
            },
            &mut state,
        );
    }

    draw_detail(f, app, detail, t);
}

fn draw_timeline(f: &mut Frame, app: &App, area: Rect, t: &Theme) {
    let Some(w) = app.current() else { return };
    let now = now();
    let jobs = w.jobs();

    // Time range
    let mut t0 = f64::INFINITY;
    let mut t1: f64 = 0.0;
    let mut spans_of: Vec<Vec<(f64, f64, Cat)>> = Vec::with_capacity(app.groups.len());
    for members in &app.groups {
        let mut v = Vec::new();
        for &k in members {
            for e in w.elements(&jobs[k]) {
                let Some(start) = e.start else { continue };
                let cat = Cat::of(e.state());
                let end = if cat.terminal() {
                    e.end.unwrap_or(start + e.elapsed.unwrap_or(0.0))
                } else {
                    now
                };
                t0 = t0.min(start);
                t1 = t1.max(end);
                v.push((start, end, cat));
            }
        }
        spans_of.push(v);
    }
    if !t0.is_finite() {
        t0 = w.meta.timestamp;
    }
    t0 = t0.min(w.meta.timestamp.max(t0 - 1.0));
    if w.active() {
        t1 = t1.max(now);
    }
    let range = (t1 - t0).max(1.0);

    let title = vec![
        Span::styled("─ ", Style::default().fg(t.surface2)),
        Span::styled(
            "Timeline ",
            Style::default().fg(t.text).add_modifier(Modifier::BOLD),
        ),
        Span::styled(format!("{} ", duration(range)), t.dim()),
    ];
    let block = Block::default()
        .borders(Borders::TOP)
        .border_style(Style::default().fg(t.surface2))
        .title(Line::from(title));
    let inner = block.inner(area);
    f.render_widget(block, area);

    let label_w = 20usize.min(inner.width as usize / 3);
    let bar_w = (inner.width as usize).saturating_sub(label_w + 2).max(1);
    let rows = (inner.height as usize).saturating_sub(1);
    let selected_group = app.rows.get(app.row_sel).map(|r| r.group());

    let mut lines = Vec::new();
    let skip = selected_group
        .unwrap_or(0)
        .saturating_sub(rows.saturating_sub(1));
    for (g, members) in app.groups.iter().enumerate().skip(skip).take(rows) {
        let job = &jobs[members[0]];
        let name = if members.len() > 1 {
            format!("{} ×{}", job.name, members.len())
        } else {
            job.label()
        };
        let mut cells: Vec<u8> = vec![0; bar_w];
        for &(a, b, cat) in &spans_of[g] {
            let c0 = (((a - t0) / range) * bar_w as f64).floor() as usize;
            let c1 = ((((b - t0) / range) * bar_w as f64).ceil() as usize).max(c0 + 1);
            let p = match cat {
                Cat::Running => 4,
                Cat::Failed => 3,
                Cat::Cancelled => 2,
                Cat::Done => 1,
                _ => 0,
            };
            for c in cells.iter_mut().take(c1.min(bar_w)).skip(c0.min(bar_w)) {
                *c = (*c).max(p);
            }
        }
        let first = cells.iter().position(|&c| c > 0);
        let selected = Some(g) == selected_group;
        let label_style = if selected {
            Style::default().fg(t.text).add_modifier(Modifier::BOLD)
        } else {
            t.sub()
        };
        let mut spans = vec![
            Span::styled(
                if selected { "▸" } else { " " },
                Style::default().fg(t.accent),
            ),
            Span::styled(
                format!("{:<label_w$} ", truncate(&name, label_w)),
                label_style,
            ),
        ];
        for (c, &p) in cells.iter().enumerate() {
            let (symbol, color) = match p {
                4 => ("━", t.sapphire),
                3 => ("━", t.red),
                2 => ("━", t.peach),
                1 => ("━", t.green),
                _ if first.is_some_and(|f| c < f) => ("┈", t.surface2),
                _ if first.is_none() => ("┈", t.surface),
                _ => (" ", t.bg),
            };
            spans.push(Span::styled(symbol, Style::default().fg(color)));
        }
        lines.push(Line::from(spans));
    }

    // Axis
    let mut axis = vec![' '; bar_w];
    let mut labels: Vec<(usize, String)> = Vec::new();
    for k in 0..=4 {
        let c = ((bar_w - 1) as f64 * k as f64 / 4.0).round() as usize;
        labels.push((c, duration(range * k as f64 / 4.0)));
    }
    for (c, text) in &labels {
        let start = if *c + text.len() > bar_w {
            bar_w.saturating_sub(text.len())
        } else {
            *c
        };
        for (i, ch) in text.chars().enumerate() {
            if start + i < bar_w {
                axis[start + i] = ch;
            }
        }
    }
    let mut spans = vec![Span::raw(" ".repeat(label_w + 2))];
    spans.push(Span::styled(axis.into_iter().collect::<String>(), t.dim()));
    lines.push(Line::from(spans));

    f.render_widget(Paragraph::new(lines), inner);
}

fn kv(t: &Theme, key: &str, value: Vec<Span<'static>>) -> Line<'static> {
    let mut spans = vec![Span::styled(
        format!(" {key:<9}"),
        Style::default().fg(t.overlay),
    )];
    spans.extend(value);
    Line::from(spans)
}

fn draw_detail(f: &mut Frame, app: &App, area: Rect, t: &Theme) {
    let Some(w) = app.current() else { return };
    let Some(row) = app.rows.get(app.row_sel) else {
        return;
    };
    let now = now();
    let view = row_view(app, w, row, now);
    let cat = view.summary.cat();

    let mut title = vec![Span::styled("─ ", Style::default().fg(t.surface2))];
    title.extend(pill(
        &format!(
            " {} {} ",
            glyph(cat, app.tick),
            view.summary.state.to_lowercase().replace('_', " ")
        ),
        t.bg,
        t.state(&view.summary.state),
        t.bg,
    ));
    title.push(Span::styled(
        format!(" {} ", view.name),
        Style::default().fg(t.text).add_modifier(Modifier::BOLD),
    ));
    if !view.jobid.is_empty() {
        title.push(Span::styled(format!("· {} ", view.jobid), t.dim()));
    }

    let block = Block::default()
        .borders(Borders::TOP)
        .border_style(Style::default().fg(t.surface2))
        .title(Line::from(title));
    let inner = block.inner(area);
    f.render_widget(block, area);

    // Arrays and groups: heatmap and the most interesting element
    let multi = match row {
        Row::Node { jobs, .. } => jobs.len() > 1 || w.jobs()[jobs[0]].is_array(),
        _ => false,
    };

    if multi {
        let Row::Node { jobs, .. } = row else { return };
        let mut cats = Vec::new();
        for &k in jobs {
            for e in w.elements(&w.jobs()[k]) {
                cats.push(Cat::of(e.state()));
            }
        }
        let grid_w = inner.width.saturating_sub(2).max(1);
        let rows_needed = widgets::heatmap_rows(cats.len(), grid_w as usize) as u16;
        let grid_h = rows_needed.min(inner.height.saturating_sub(4).max(1));
        let [legend, grid, _, rest] = Layout::vertical([
            Constraint::Length(1),
            Constraint::Length(grid_h),
            Constraint::Length(1),
            Constraint::Min(0),
        ])
        .areas(inner);

        let c = &view.summary.counts;
        let mut spans = vec![Span::styled(format!(" {} elements  ", cats.len()), t.sub())];
        for cat in [
            Cat::Done,
            Cat::Running,
            Cat::Failed,
            Cat::Cancelled,
            Cat::Pending,
        ] {
            let n = c.get(cat);
            if n > 0 {
                spans.push(Span::styled("■ ", Style::default().fg(t.cat(cat))));
                spans.push(Span::styled(
                    format!("{} {}  ", n, cat.name()),
                    Style::default().fg(t.subtext),
                ));
            }
        }
        f.render_widget(Paragraph::new(Line::from(spans)), legend);
        widgets::heatmap(
            f.buffer_mut(),
            Rect {
                x: grid.x + 1,
                width: grid_w,
                ..grid
            },
            t,
            &cats,
            None,
            app.tick,
        );

        // Most interesting element
        if let Some((k, i)) = app.target() {
            let job = &w.jobs()[k];
            let e = w.entry(job, i);
            let label = match i {
                Some(i) => format!("{}[{i}]", job.name),
                None => job.input.lines().next().unwrap_or(&job.name).to_string(),
            };
            draw_entry(f, app, w, job, i, &e, &label, rest, t);
        }
        return;
    }

    let (k, i) = app.target().unwrap_or((0, None));
    let job = &w.jobs()[k];
    let e = view.entry.clone().unwrap_or_else(|| w.entry(job, i));
    draw_entry(f, app, w, job, i, &e, "", inner, t);
}

#[allow(clippy::too_many_arguments)]
fn draw_entry(
    f: &mut Frame,
    app: &App,
    w: &Workflow,
    job: &JobMeta,
    i: Option<usize>,
    e: &Entry,
    label: &str,
    area: Rect,
    t: &Theme,
) {
    if area.height == 0 {
        return;
    }
    let now = now();
    let info_w = 40.min(area.width / 2);
    let [info, right] =
        Layout::horizontal([Constraint::Length(info_w), Constraint::Min(10)]).areas(area);

    let mut lines = Vec::new();
    if !label.is_empty() {
        let cat = Cat::of(e.state());
        lines.push(Line::from(vec![
            Span::styled(
                format!(" {} ", glyph(cat, app.tick)),
                Style::default().fg(t.state(e.state())),
            ),
            Span::styled(truncate(label, info_w as usize - 4), t.bold()),
        ]));
    }
    if let Some(node) = e.node() {
        lines.push(kv(t, "node", vec![Span::styled(node.to_string(), t.sub())]));
    }
    if let Some(x) = elapsed(e, now) {
        let mut v = vec![Span::styled(duration(x), Style::default().fg(t.text))];
        if let Some(limit) = e.limit.filter(|l| *l > 0.0) {
            v.push(Span::styled(format!(" / {} ", duration(limit)), t.dim()));
            let frac = x / limit;
            let color = if frac > 0.9 {
                t.red
            } else if frac > 0.75 {
                t.yellow
            } else {
                t.teal
            };
            v.extend(thin_bar(t, frac, 8, Some(color)));
        }
        lines.push(kv(t, "time", v));
    }
    if let Some(start) = e.start {
        lines.push(kv(
            t,
            "started",
            vec![Span::styled(widgets::clock(start, true), t.sub())],
        ));
    }
    if let Some(exit) = e
        .exit
        .as_ref()
        .filter(|x| *x != "0:0" && Cat::of(e.state()).terminal())
    {
        lines.push(kv(
            t,
            "exit",
            vec![Span::styled(exit.clone(), Style::default().fg(t.red))],
        ));
    }
    if let Some(reason) = e.reason.as_ref().filter(|r| {
        !r.is_empty() && Cat::of(e.state()) != Cat::Done && Cat::of(e.state()) != Cat::Running
    }) {
        lines.push(kv(
            t,
            "reason",
            vec![Span::styled(reason.clone(), Style::default().fg(t.yellow))],
        ));
    }
    if !job.deps.is_empty() && lines.len() < info.height as usize {
        let mut v = Vec::new();
        if job.wait == "any" {
            v.push(Span::styled("any of ", t.dim()));
        }
        for (d, status) in job.deps.iter().take(6) {
            let s = &w.summaries[*d];
            v.push(Span::styled(
                format!("{}#{d} ", s.cat().glyph()),
                Style::default().fg(t.cat(s.cat())),
            ));
            if status != "success" {
                v.push(Span::styled(format!("({status}) "), t.dim()));
            }
        }
        if job.deps.len() > 6 {
            v.push(Span::styled(format!("+{}", job.deps.len() - 6), t.dim()));
        }
        lines.push(kv(t, "after", v));
    }
    if lines.len() < info.height as usize {
        let input = match (i, &job.inputs) {
            (Some(i), Some(inputs)) => inputs.get(i).cloned().unwrap_or_default(),
            _ => job.input.clone(),
        };
        lines.push(kv(
            t,
            "input",
            vec![Span::styled(
                truncate(input.lines().next().unwrap_or(""), info_w as usize - 11),
                t.sub(),
            )],
        ));
    }
    f.render_widget(Paragraph::new(lines), info);

    // Progress bars, status and logs
    let mut lines: Vec<Line> = Vec::new();
    let bar_w = (right.width as usize).saturating_sub(48).clamp(10, 40);
    for bar in e
        .progress
        .iter()
        .rev()
        .take(4)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
    {
        let mut spans = vec![Span::styled(
            format!("{:<14}", truncate(&bar.desc, 13)),
            Style::default().fg(t.accent2),
        )];
        match bar.fraction() {
            Some(frac) => {
                spans.extend(block_bar(t, frac, bar_w));
                spans.push(Span::styled(
                    format!(" {:>3.0}% ", 100.0 * frac),
                    Style::default().fg(t.text).add_modifier(Modifier::BOLD),
                ));
            }
            None => spans.push(Span::styled(
                format!("{} ", widgets::spinner(app.tick)),
                Style::default().fg(t.sapphire),
            )),
        }
        let mut text = bar_text(bar);
        if let Some(pos) = text
            .find(" · ")
            .filter(|_| !bar.desc.is_empty() && bar.desc != "progress")
        {
            text = text[pos + 3..].to_string();
        }
        spans.push(Span::styled(text, t.dim()));
        if let Some(postfix) = &bar.postfix {
            spans.push(Span::styled(
                format!("  {postfix}"),
                Style::default().fg(t.yellow),
            ));
        }
        lines.push(Line::from(spans));
    }
    if let Some(status) = &e.status {
        lines.push(Line::from(vec![
            Span::styled("› ", Style::default().fg(t.accent)),
            Span::styled(status.clone(), t.sub()),
        ]));
    }
    if Cat::of(e.state()) == Cat::Pending && !job.deps.is_empty() {
        let mut c = Counts::default();
        let mut frac = 0.0;
        let mut total = 0;
        let mut finished = 0;
        for (d, _) in &job.deps {
            let s = &w.summaries[*d];
            c.merge(&s.counts);
            frac += s.fraction * s.total as f64;
            total += s.total;
            finished += (s.counts.finished() == s.total) as usize;
        }
        let mut spans = vec![Span::styled(
            format!("{:<14}", "dependencies"),
            Style::default().fg(t.accent2),
        )];
        spans.extend(state_bar(t, &c, frac / total.max(1) as f64, bar_w, true));
        spans.push(Span::styled(
            format!(" {finished}/{} finished ", job.deps.len()),
            Style::default().fg(t.text),
        ));
        spans.extend(counts(t, &c, app.tick));
        lines.push(Line::from(spans));
    }

    let used = lines.len() as u16;
    let [top, bottom] =
        Layout::vertical([Constraint::Length(used), Constraint::Min(0)]).areas(right);
    f.render_widget(Paragraph::new(lines), top);

    if bottom.height >= 2 {
        let path = w.log_path(job, i);
        let log = logs::read(&path);
        let (title, lines): (String, Vec<String>) = match &log {
            Some(l) => (
                format!(" logs · {} ", widgets::size(l.size)),
                l.lines.clone(),
            ),
            None => {
                let text = e.trace.clone().or(e.error.clone()).unwrap_or_default();
                (
                    " logs ".to_string(),
                    text.lines().map(String::from).collect(),
                )
            }
        };
        let block = Block::default()
            .borders(Borders::LEFT)
            .border_style(Style::default().fg(t.surface))
            .title(Span::styled(title, t.dim()));
        let inner = block.inner(Rect {
            y: bottom.y,
            height: bottom.height,
            ..bottom
        });
        f.render_widget(block, bottom);
        let n = inner.height as usize;
        let start = lines.len().saturating_sub(n);
        let rendered: Vec<Line> = lines[start..]
            .iter()
            .map(|l| log_line(l, t, inner.width as usize))
            .collect();
        if rendered.is_empty() {
            f.render_widget(
                Paragraph::new(Span::styled(" (no output yet)", t.dim())),
                inner,
            );
        } else {
            f.render_widget(Paragraph::new(rendered), inner);
        }
    }
}

fn log_line(line: &str, t: &Theme, width: usize) -> Line<'static> {
    let plain = logs::strip(line).to_lowercase();
    let base =
        if plain.contains("traceback") || plain.contains("error") || plain.contains("exception") {
            Style::default().fg(t.red)
        } else if plain.contains("warn") {
            Style::default().fg(t.yellow)
        } else if plain.starts_with("  file ") {
            Style::default().fg(t.subtext)
        } else {
            Style::default().fg(t.text)
        };
    let mut l = logs::styled(line, base);
    // Truncate long lines
    let mut used = 0;
    let mut spans = Vec::new();
    for s in l.spans.drain(..) {
        let w = s.content.width();
        if used + w > width {
            spans.push(Span::styled(truncate(&s.content, width - used), s.style));
            break;
        }
        used += w;
        spans.push(s);
    }
    Line::from(spans)
}

// Graph tab

fn draw_graph(f: &mut Frame, app: &mut App, area: Rect, t: &Theme) {
    if app.current().is_none() || app.dag.nodes.is_empty() {
        return;
    }

    let detail_h = if area.height >= 24 { 9 } else { 0 };
    let [canvas, legend, detail] = Layout::vertical([
        Constraint::Min(3),
        Constraint::Length(1),
        Constraint::Length(detail_h),
    ])
    .areas(area);

    if detail_h > 0 {
        draw_node_detail(f, app, detail, t);
    }

    // Fit the nodes to the width of the pane if possible
    let depth = crate::graph::Dag::depth(&app.parents).max(1) as u16;
    let fit = (canvas
        .width
        .saturating_sub((depth - 1) * crate::graph::GAP + 1)
        / depth)
        .clamp(18, 30);
    if app.dag.node_w != fit {
        app.dag = crate::graph::Dag::layout_with(&app.parents, fit);
    }

    let Some(w) = app.current() else { return };
    let dag = &app.dag;
    let node_w = dag.node_w;

    // Center small graphs
    let (ox, oy) = (
        canvas.width.saturating_sub(dag.width) / 2,
        canvas.height.saturating_sub(dag.height) / 3,
    );
    let canvas = Rect {
        x: canvas.x + ox,
        y: canvas.y + oy,
        width: canvas.width - ox,
        height: canvas.height - oy,
    };
    let sel = app.graph_sel.min(dag.nodes.len() - 1);
    let node = &dag.nodes[sel];

    // Scroll to keep the selection visible
    let (mut sx, mut sy) = app.graph_scroll;
    if node.x < sx {
        sx = node.x.saturating_sub(2);
    }
    if node.x + node_w + 2 > sx + canvas.width {
        sx = (node.x + node_w + 2).saturating_sub(canvas.width);
    }
    if node.y < sy {
        sy = node.y.saturating_sub(1);
    }
    if node.y + NODE_H + 1 > sy + canvas.height {
        sy = (node.y + NODE_H + 1).saturating_sub(canvas.height);
    }
    let (sx, sy) = if dag.width <= canvas.width {
        (0, sy)
    } else {
        (sx, sy)
    };

    let buf = f.buffer_mut();
    let place = |x: u16, y: u16| -> Option<(u16, u16)> {
        if x < sx || y < sy {
            return None;
        }
        let (px, py) = (canvas.x + x - sx, canvas.y + y - sy);
        (px < canvas.x + canvas.width && py < canvas.y + canvas.height).then_some((px, py))
    };

    // Edges
    for (&(x, y), (bits, edges)) in &dag.lines {
        let Some((px, py)) = place(x, y) else {
            continue;
        };
        let hot = edges.iter().any(|&(a, b)| a == sel || b == sel);
        let color = if hot { t.accent } else { t.surface2 };
        buf[(px, py)]
            .set_char(line_glyph(*bits))
            .set_style(Style::default().fg(color));
    }
    for &(x, y, (a, b)) in &dag.arrows {
        let Some((px, py)) = place(x, y) else {
            continue;
        };
        let hot = a == sel || b == sel;
        buf[(px, py)]
            .set_char('▶')
            .set_style(Style::default().fg(if hot { t.accent } else { t.surface2 }));
    }

    // Nodes
    let jobs = w.jobs();
    for (g, members) in app.groups.iter().enumerate() {
        let p = &dag.nodes[g];
        let job = &jobs[members[0]];
        let (summary, name) = if members.len() == 1 {
            (w.summary(job.index).clone(), job.label())
        } else {
            let mut c = Counts::default();
            let mut frac = 0.0;
            let mut total = 0;
            for &k in members {
                let s = w.summary(jobs[k].index);
                c.merge(&s.counts);
                frac += s.fraction * s.total as f64;
                total += s.total;
            }
            (
                Summary {
                    state: c.state().into(),
                    counts: c,
                    fraction: frac / total.max(1) as f64,
                    total,
                },
                format!("{} ×{}", job.name, members.len()),
            )
        };
        let cat = summary.cat();
        let selected = g == sel;
        let border = if selected {
            t.accent
        } else if cat == Cat::Pending {
            t.surface2
        } else {
            t.state(&summary.state)
        };
        let bg = if selected { t.surface } else { t.bg };
        let (tl, tr, bl, br, h, v) = if selected {
            ('┏', '┓', '┗', '┛', '━', '┃')
        } else {
            ('╭', '╮', '╰', '╯', '─', '│')
        };

        for dy in 0..NODE_H {
            for dx in 0..node_w {
                let Some((px, py)) = place(p.x + dx, p.y + dy) else {
                    continue;
                };
                let ch = match (dx, dy) {
                    (0, 0) => tl,
                    (x, 0) if x == node_w - 1 => tr,
                    (0, y) if y == NODE_H - 1 => bl,
                    (x, y) if x == node_w - 1 && y == NODE_H - 1 => br,
                    (_, 0) => h,
                    (_, y) if y == NODE_H - 1 => h,
                    (0, _) => v,
                    (x, _) if x == node_w - 1 => v,
                    _ => ' ',
                };
                buf[(px, py)]
                    .set_char(ch)
                    .set_style(Style::default().fg(border).bg(bg));
            }
        }

        // Index on the top border
        let label = format!(
            " #{} ",
            if members.len() > 1 {
                format!("{}-{}", job.index, jobs[*members.last().unwrap()].index)
            } else {
                job.index.to_string()
            }
        );
        let lx = p.x + node_w - 1 - label.width() as u16;
        put(
            buf,
            &place,
            lx,
            p.y,
            &label,
            Style::default().fg(t.overlay).bg(bg),
        );

        // Name
        let inner_w = (node_w - 4) as usize;
        let mut spans = vec![Span::styled(
            format!("{} ", glyph(cat, app.tick)),
            Style::default().fg(t.state(&summary.state)).bg(bg),
        )];
        spans.push(Span::styled(
            truncate(&name, inner_w - 2),
            Style::default()
                .fg(t.text)
                .bg(bg)
                .add_modifier(Modifier::BOLD),
        ));
        put_spans(buf, &place, p.x + 2, p.y + 1, &spans);

        // Progress
        let pct = format!(" {:>3.0}%", 100.0 * summary.fraction);
        let bar = state_bar(
            t,
            &summary.counts,
            summary.fraction,
            inner_w - pct.len(),
            false,
        );
        let mut spans: Vec<Span> = bar
            .into_iter()
            .map(|s| {
                let st = s.style.bg(bg);
                s.style(st)
            })
            .collect();
        spans.push(Span::styled(pct, Style::default().fg(t.subtext).bg(bg)));
        put_spans(buf, &place, p.x + 2, p.y + 2, &spans);
    }

    app.graph_scroll = (sx, sy);

    let hint = Line::from(vec![
        Span::styled(" ←→↑↓ ", Style::default().fg(t.text).bg(t.surface)),
        Span::styled(" navigate  ", t.dim()),
        Span::styled(" ⏎ ", Style::default().fg(t.text).bg(t.surface)),
        Span::styled(" show in jobs  ", t.dim()),
        Span::styled(
            format!("{} nodes · {} layers", dag.nodes.len(), dag.layers.len()),
            t.dim(),
        ),
    ]);
    f.render_widget(Paragraph::new(hint), legend);
}

fn draw_node_detail(f: &mut Frame, app: &App, area: Rect, t: &Theme) {
    let Some(w) = app.current() else { return };
    let Some(members) = app.groups.get(app.graph_sel) else {
        return;
    };
    let jobs = w.jobs();
    let job = &jobs[members[0]];

    // The most interesting job (or element) of the node
    let (k, i) = if members.len() > 1 {
        let k = members
            .iter()
            .copied()
            .min_by_key(|&k| {
                let c = w.summaries[jobs[k].index].cat();
                (
                    match c {
                        Cat::Running => 0,
                        Cat::Failed => 1,
                        Cat::Cancelled => 2,
                        Cat::Pending => 3,
                        _ => 4,
                    },
                    k,
                )
            })
            .unwrap_or(members[0]);
        (k, None)
    } else if job.is_array() {
        (members[0], Some(crate::app::interesting(w, job)))
    } else {
        (members[0], None)
    };

    let target = &jobs[k];
    let e = w.entry(target, i);
    let label = match i {
        Some(i) => format!("{}[{i}]", target.name),
        None => target
            .input
            .lines()
            .next()
            .unwrap_or(&target.name)
            .to_string(),
    };

    let block = Block::default()
        .borders(Borders::TOP)
        .border_style(Style::default().fg(t.surface2))
        .title(Line::from(vec![
            Span::styled("─ ", Style::default().fg(t.surface2)),
            Span::styled("Selected ", t.bold()),
        ]));
    let inner = block.inner(area);
    f.render_widget(block, area);
    draw_entry(f, app, w, target, i, &e, &label, inner, t);
}

fn put(
    buf: &mut Buffer,
    place: &dyn Fn(u16, u16) -> Option<(u16, u16)>,
    x: u16,
    y: u16,
    text: &str,
    style: Style,
) {
    let mut dx = 0;
    for c in text.chars() {
        if let Some((px, py)) = place(x + dx, y) {
            buf[(px, py)].set_char(c).set_style(style);
        }
        dx += unicode_width::UnicodeWidthChar::width(c).unwrap_or(1) as u16;
    }
}

fn put_spans(
    buf: &mut Buffer,
    place: &dyn Fn(u16, u16) -> Option<(u16, u16)>,
    x: u16,
    y: u16,
    spans: &[Span],
) {
    let mut dx = 0;
    for s in spans {
        put(buf, place, x + dx, y, &s.content, s.style);
        dx += s.content.width() as u16;
    }
}

// Logs tab

fn draw_logs(f: &mut Frame, app: &mut App, area: Rect, t: &Theme) {
    let Some(w) = app.current() else { return };
    let Some((k, i)) = app.target() else { return };
    let job = &w.jobs()[k];

    let [head, body] = Layout::vertical([Constraint::Length(1), Constraint::Min(1)]).areas(area);

    let label = match i {
        Some(i) if job.is_array() => format!("{}[{i}]", job.name),
        _ => job.label(),
    };
    let mut left = vec![Span::styled(format!(" {label} "), t.bold())];
    if let Some(path) = &app.log.path {
        let name = path
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_default();
        left.push(Span::styled(
            format!("{name} · {} ", widgets::size(app.log.size)),
            t.dim(),
        ));
    }
    if app.log.truncated {
        left.push(Span::styled("(last 4 MiB) ", Style::default().fg(t.yellow)));
    }
    let mut right = Vec::new();
    if app.log.wrap {
        right.extend(pill("WRAP", t.bg, t.blue, t.bg));
        right.push(Span::raw(" "));
    }
    if app.log.follow {
        right.extend(pill("FOLLOW", t.bg, t.green, t.bg));
    } else {
        right.extend(pill(&format!("↑{}", app.log.scroll), t.bg, t.yellow, t.bg));
    }
    right.push(Span::raw(" "));
    f.render_widget(
        Paragraph::new(line_lr(left, right, head.width as usize)),
        head,
    );

    // Banner for jobs that did not complete
    let e = w.entry(job, i);
    let cat = Cat::of(e.state());
    let body = if matches!(cat, Cat::Failed | Cat::Cancelled) && body.height > 3 {
        let [banner, rest] =
            Layout::vertical([Constraint::Length(2), Constraint::Min(1)]).areas(body);
        let mut spans = pill(
            &format!(
                " {} {} ",
                cat.glyph(),
                e.state().to_lowercase().replace('_', " ")
            ),
            t.bg,
            t.state(e.state()),
            t.bg,
        );
        let mut text = e.error.clone().or(e.reason.clone()).unwrap_or_default();
        if text == "DependencyNeverSatisfied" {
            text = "a dependency can never be satisfied".into();
        } else if e.state() == "TIMEOUT" {
            text = format!(
                "reached the time limit{}",
                e.limit
                    .map(|l| format!(" of {}", duration(l)))
                    .unwrap_or_default()
            );
        } else if e.state() == "OUT_OF_MEMORY" {
            text = "ran out of memory".into();
        }
        spans.push(Span::styled(
            format!(" {}", text.lines().last().unwrap_or("")),
            Style::default().fg(t.text),
        ));
        if let Some(exit) = e.exit.as_ref().filter(|x| *x != "0:0") {
            spans.push(Span::styled(format!("  exit {exit}"), t.dim()));
        }
        f.render_widget(Paragraph::new(Line::from(spans)), banner);
        rest
    } else {
        body
    };

    let n = app.log.lines.len();
    let gutter = n.to_string().len().max(3);
    let text_w = (body.width as usize).saturating_sub(gutter + 3);
    let height = body.height as usize;

    if n == 0 {
        f.render_widget(
            Paragraph::new(Span::styled("  (no output yet)", t.dim())),
            body,
        );
        return;
    }

    let end = n.saturating_sub(app.log.scroll);
    let start = end.saturating_sub(height);
    let mut lines = Vec::new();
    for (k, line) in app.log.lines[start..end].iter().enumerate() {
        let mut spans = vec![
            Span::styled(
                format!("{:>gutter$} ", start + k + 1),
                Style::default().fg(t.surface2),
            ),
            Span::styled("│ ", Style::default().fg(t.surface)),
        ];
        let styled = if app.log.wrap {
            logs::styled(line, Style::default().fg(t.text))
        } else {
            log_line(line, t, text_w)
        };
        spans.extend(styled.spans);
        lines.push(Line::from(spans));
    }

    let mut paragraph = Paragraph::new(lines);
    if app.log.wrap {
        paragraph = paragraph.wrap(Wrap { trim: false });
    }
    f.render_widget(paragraph, body);

    let mut state = ScrollbarState::new(n.saturating_sub(height).max(1)).position(start);
    f.render_stateful_widget(
        Scrollbar::new(ScrollbarOrientation::VerticalRight)
            .begin_symbol(None)
            .end_symbol(None)
            .thumb_style(Style::default().fg(t.surface2))
            .track_style(Style::default().fg(t.bg)),
        body,
        &mut state,
    );
}

// Info tab

fn highlight_python(t: &Theme, code: &str) -> Vec<Line<'static>> {
    const KEYWORDS: &[&str] = &[
        "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class",
        "continue", "def", "del", "elif", "else", "except", "finally", "for", "from", "global",
        "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return",
        "try", "while", "with", "yield",
    ];
    code.lines()
        .map(|line| {
            let mut spans = Vec::new();
            let chars: Vec<char> = line.chars().collect();
            let mut k = 0;
            let mut prev_def = false;
            while k < chars.len() {
                let c = chars[k];
                if c == '#' {
                    spans.push(Span::styled(
                        chars[k..].iter().collect::<String>(),
                        Style::default()
                            .fg(t.overlay)
                            .add_modifier(Modifier::ITALIC),
                    ));
                    break;
                } else if c == '"' || c == '\'' {
                    let quote = c;
                    let mut j = k + 1;
                    while j < chars.len() && chars[j] != quote {
                        if chars[j] == '\\' {
                            j += 1;
                        }
                        j += 1;
                    }
                    let j = (j + 1).min(chars.len());
                    spans.push(Span::styled(
                        chars[k..j].iter().collect::<String>(),
                        Style::default().fg(t.green),
                    ));
                    k = j;
                } else if c == '@' {
                    let mut j = k + 1;
                    while j < chars.len()
                        && (chars[j].is_alphanumeric() || chars[j] == '_' || chars[j] == '.')
                    {
                        j += 1;
                    }
                    spans.push(Span::styled(
                        chars[k..j].iter().collect::<String>(),
                        Style::default().fg(t.yellow),
                    ));
                    k = j;
                } else if c.is_alphabetic() || c == '_' {
                    let mut j = k;
                    while j < chars.len() && (chars[j].is_alphanumeric() || chars[j] == '_') {
                        j += 1;
                    }
                    let word: String = chars[k..j].iter().collect();
                    let style = if KEYWORDS.contains(&word.as_str()) {
                        Style::default().fg(t.mauve)
                    } else if prev_def {
                        Style::default().fg(t.blue).add_modifier(Modifier::BOLD)
                    } else if j < chars.len() && chars[j] == '(' {
                        Style::default().fg(t.blue)
                    } else {
                        Style::default().fg(t.text)
                    };
                    prev_def = word == "def" || word == "class";
                    spans.push(Span::styled(word, style));
                    k = j;
                } else if c.is_ascii_digit() {
                    let mut j = k;
                    while j < chars.len()
                        && (chars[j].is_ascii_alphanumeric() || chars[j] == '.' || chars[j] == '_')
                    {
                        j += 1;
                    }
                    spans.push(Span::styled(
                        chars[k..j].iter().collect::<String>(),
                        Style::default().fg(t.peach),
                    ));
                    k = j;
                } else {
                    spans.push(Span::styled(c.to_string(), Style::default().fg(t.subtext)));
                    k += 1;
                }
            }
            Line::from(spans)
        })
        .collect()
}

fn highlight_shell(t: &Theme, script: &str) -> Vec<Line<'static>> {
    script
        .lines()
        .map(|line| {
            if let Some(rest) = line.strip_prefix("#SBATCH ") {
                let (key, value) = rest.split_once('=').unwrap_or((rest, ""));
                let mut spans = vec![
                    Span::styled("#SBATCH ", Style::default().fg(t.overlay)),
                    Span::styled(key.to_string(), Style::default().fg(t.sapphire)),
                ];
                if !value.is_empty() {
                    spans.push(Span::styled("=", Style::default().fg(t.overlay)));
                    spans.push(Span::styled(
                        value.to_string(),
                        Style::default().fg(t.yellow),
                    ));
                }
                Line::from(spans)
            } else if line.starts_with('#') {
                Line::from(Span::styled(
                    line.to_string(),
                    Style::default().fg(t.overlay),
                ))
            } else {
                Line::from(Span::styled(line.to_string(), Style::default().fg(t.text)))
            }
        })
        .collect()
}

fn section(t: &Theme, title: &str) -> Line<'static> {
    Line::from(vec![
        Span::styled("▍", Style::default().fg(t.accent)),
        Span::styled(
            format!("{title} "),
            Style::default().fg(t.accent).add_modifier(Modifier::BOLD),
        ),
    ])
}

fn draw_info(f: &mut Frame, app: &mut App, area: Rect, t: &Theme) {
    let Some(w) = app.current() else { return };
    let Some((k, i)) = app.target() else { return };
    let job = &w.jobs()[k];
    let mut lines = Vec::new();

    lines.push(section(t, &format!("Job #{} {}", job.index, job.label())));
    let input = match (i, &job.inputs) {
        (Some(i), Some(inputs)) => inputs.get(i).cloned().unwrap_or_default(),
        _ => job.input.clone(),
    };
    for l in input.lines() {
        lines.push(Line::from(Span::styled(
            format!("  {l}"),
            Style::default().fg(t.text),
        )));
    }
    if let Some(jobid) = &job.jobid {
        lines.push(kv(t, "job id", vec![Span::styled(jobid.clone(), t.sub())]));
    }
    if !job.settings.is_empty() {
        let settings: Vec<String> = job
            .settings
            .iter()
            .map(|(k, v)| format!("{k}={}", v.to_string().trim_matches('"')))
            .collect();
        lines.push(kv(
            t,
            "settings",
            vec![Span::styled(
                settings.join("  "),
                Style::default().fg(t.yellow),
            )],
        ));
    }
    lines.push(Line::from(""));

    let script = w.script_path(job);
    if let Ok(text) = std::fs::read_to_string(&script) {
        lines.push(section(t, "Submission script"));
        lines.extend(highlight_shell(t, &text).into_iter().map(|mut l| {
            l.spans.insert(0, Span::raw("  "));
            l
        }));
        lines.push(Line::from(""));
    }

    if let Some(source) = job
        .source
        .and_then(|s| w.meta.sources.get(s))
        .filter(|s| !s.is_empty())
    {
        lines.push(section(t, "Source"));
        lines.extend(highlight_python(t, source).into_iter().map(|mut l| {
            l.spans.insert(0, Span::raw("  "));
            l
        }));
        lines.push(Line::from(""));
    }

    lines.push(section(t, "Workflow"));
    let m = &w.meta;
    lines.push(kv(t, "id", vec![Span::styled(m.uid.clone(), t.sub())]));
    lines.push(kv(
        t,
        "created",
        vec![Span::styled(m.date.replace('T', " "), t.sub())],
    ));
    lines.push(kv(
        t,
        "host",
        vec![Span::styled(format!("{} ({})", m.host, m.user), t.sub())],
    ));
    lines.push(kv(t, "cwd", vec![Span::styled(m.cwd.clone(), t.sub())]));
    lines.push(kv(
        t,
        "command",
        vec![Span::styled(m.argv.join(" "), t.sub())],
    ));
    lines.push(kv(
        t,
        "records",
        vec![Span::styled(w.path.display().to_string(), t.dim())],
    ));

    let max = lines.len().saturating_sub(area.height as usize);
    app.info_scroll = app.info_scroll.min(max);
    f.render_widget(
        Paragraph::new(lines).scroll((app.info_scroll as u16, 0)),
        area,
    );
}

// Footer, toasts and popups

fn key(t: &Theme, k: &str, label: &str) -> Vec<Span<'static>> {
    vec![
        Span::styled(
            format!(" {k} "),
            Style::default()
                .fg(t.text)
                .bg(t.surface)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled(format!(" {label}  "), Style::default().fg(t.subtext)),
    ]
}

fn draw_footer(f: &mut Frame, app: &App, area: Rect, t: &Theme) {
    let mut spans = Vec::new();

    if app.search.active {
        spans.extend(pill(" / ", t.bg, t.yellow, t.panel));
        spans.push(Span::styled(
            format!(" {}", app.search.query),
            Style::default().fg(t.text),
        ));
        spans.push(Span::styled("█", Style::default().fg(t.yellow)));
        spans.push(Span::styled(
            if app.focus == Focus::Main {
                "  filter jobs · ⏎ keep · esc clear"
            } else {
                "  filter workflows · ⏎ keep · esc clear"
            },
            t.dim(),
        ));
    } else {
        let hints: Vec<(&str, &str)> = match (app.focus, app.tab) {
            (Focus::Workflows, _) => vec![
                ("↑↓", "select"),
                ("→", "open"),
                ("/", "search"),
                ("a", "active"),
                ("D", "all dirs"),
                ("Q", "queue"),
                ("c", "cancel"),
            ],
            (Focus::Main, Tab::Jobs) => vec![
                ("↑↓", "select"),
                ("→←", "expand"),
                ("⏎", "logs"),
                ("/", "filter"),
                ("s", "state"),
                ("c", "cancel"),
                ("y", "copy id"),
            ],
            (Focus::Main, Tab::Graph) => vec![("←→↑↓", "navigate"), ("⏎", "show")],
            (Focus::Main, Tab::Logs) => vec![
                ("↑↓", "scroll"),
                ("f", "follow"),
                ("w", "wrap"),
                ("g/G", "top/end"),
            ],
            (Focus::Main, Tab::Info) => vec![("↑↓", "scroll")],
        };
        for (k, label) in hints {
            spans.extend(key(t, k, label));
        }
        spans.extend(key(t, "tab", "tabs"));
        spans.extend(key(t, "r", "refresh"));
        spans.extend(key(t, "?", "help"));
        spans.extend(key(t, "q", "quit"));
    }

    let right = match &app.message {
        Some((m, _)) => vec![Span::styled(format!("{m} "), Style::default().fg(t.yellow))],
        None => match &app.sacct_error {
            Some(e) => vec![Span::styled(
                format!("slurm: {} ", truncate(e, 50)),
                Style::default().fg(t.red),
            )],
            None => vec![],
        },
    };

    // Drop hints that do not fit
    let width = area.width as usize;
    while spans_width(&spans) + spans_width(&right) > width && spans.len() > 2 {
        spans.truncate(spans.len() - 2);
    }

    f.render_widget(
        Paragraph::new(line_lr(spans, right, width)).style(Style::default().bg(t.panel)),
        area,
    );
}

fn draw_toasts(f: &mut Frame, app: &App, area: Rect, t: &Theme) {
    let mut y = area.y + 2;
    for toast in &app.toasts {
        let w = (toast.text.width() as u16 + 4).min(area.width.saturating_sub(4));
        let rect = Rect {
            x: area.x + area.width.saturating_sub(w + 2),
            y,
            width: w,
            height: 3,
        };
        if rect.y + rect.height > area.y + area.height {
            break;
        }
        f.render_widget(Clear, rect);
        let block = Block::default()
            .borders(Borders::ALL)
            .border_type(BorderType::Rounded)
            .border_style(Style::default().fg(toast.color))
            .style(Style::default().bg(t.panel));
        f.render_widget(
            Paragraph::new(Span::styled(
                format!(" {}", truncate(&toast.text, w as usize - 3)),
                Style::default().fg(t.text),
            ))
            .block(block),
            rect,
        );
        y += 3;
    }
}

fn centered(area: Rect, w: u16, h: u16) -> Rect {
    let w = w.min(area.width.saturating_sub(2));
    let h = h.min(area.height.saturating_sub(2));
    Rect {
        x: area.x + (area.width - w) / 2,
        y: area.y + (area.height - h) / 2,
        width: w,
        height: h,
    }
}

fn draw_popup(f: &mut Frame, app: &App, area: Rect, t: &Theme) {
    match &app.popup {
        None => {}
        Some(Popup::Confirm { message, .. }) => {
            let rect = centered(area, (message.width() as u16 + 8).max(36), 7);
            f.render_widget(Clear, rect);
            let block = Block::default()
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .border_style(Style::default().fg(t.red))
                .title(Span::styled(
                    " confirm ",
                    Style::default().fg(t.red).add_modifier(Modifier::BOLD),
                ))
                .style(Style::default().bg(t.panel));
            let mut keys = vec![Span::raw("  ")];
            keys.extend(key(t, "y", "yes"));
            keys.extend(key(t, "n", "no"));
            let lines = vec![
                Line::from(""),
                Line::from(Span::styled(format!("  {message}"), t.bold())),
                Line::from(""),
                Line::from(keys),
            ];
            f.render_widget(Paragraph::new(lines).block(block), rect);
        }
        Some(Popup::Help) => {
            let rows = [
                ("Navigation", ""),
                ("↑↓ j k", "move selection"),
                ("← → h l", "focus sidebar / main, collapse / expand"),
                ("⏎", "open (expand groups, show logs)"),
                ("1-4, tab", "switch tabs: jobs, graph, logs, info"),
                ("g G", "top / bottom (logs: follow)"),
                ("PgUp PgDn", "page"),
                ("", ""),
                ("Filters", ""),
                ("/", "search workflows (sidebar) or jobs (main)"),
                ("a", "active workflows only"),
                ("s", "cycle job state filter"),
                ("D", "workflows of all known directories"),
                ("Q", "Slurm queue of all your jobs (squeue)"),
                ("", ""),
                ("Actions", ""),
                ("r", "query Slurm now (at most every 5 s)"),
                ("c", "cancel workflow, job or element"),
                ("y", "copy Slurm job ID"),
                ("f w", "logs: follow, wrap"),
                ("q ctrl-c", "quit"),
            ];
            let rect = centered(area, 64, rows.len() as u16 + 4);
            f.render_widget(Clear, rect);
            let block = Block::default()
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .border_style(Style::default().fg(t.accent))
                .title(Line::from(pill(" ◆ dawgz help ", t.bg, t.accent, t.panel)))
                .title_bottom(
                    Line::from(Span::styled(
                        format!(
                            " slurm refresh every {:.0}s · {} queries this session ",
                            app.ttl, app.sacct_calls
                        ),
                        t.dim(),
                    ))
                    .right_aligned(),
                )
                .style(Style::default().bg(t.panel));
            let lines: Vec<Line> = std::iter::once(Line::from(""))
                .chain(rows.iter().map(|(k, v)| {
                    if v.is_empty() && !k.is_empty() {
                        Line::from(Span::styled(
                            format!("  {k}"),
                            Style::default().fg(t.accent).add_modifier(Modifier::BOLD),
                        ))
                    } else {
                        Line::from(vec![
                            Span::styled(format!("  {k:<12}"), Style::default().fg(t.yellow)),
                            Span::styled(v.to_string(), Style::default().fg(t.text)),
                        ])
                    }
                }))
                .collect();
            f.render_widget(Paragraph::new(lines).block(block), rect);
        }
    }
}

fn draw_queue(f: &mut Frame, app: &mut App, area: Rect, t: &Theme) {
    let q = &app.queue;
    let now = now();
    let mut title = vec![Span::raw(" ")];
    title.extend(pill(" Slurm queue ", t.bg, t.mauve, t.bg));
    title.push(Span::styled(
        format!(" {} ", std::env::var("USER").unwrap_or_default()),
        t.sub(),
    ));
    let block = panel(t, title, true);
    let inner = block.inner(area);
    f.render_widget(block, area);

    let [head, _, table, foot] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Min(3),
        Constraint::Length(1),
    ])
    .areas(inner);

    // Summary
    let mut by_state: Vec<(String, usize)> = Vec::new();
    for job in &q.jobs {
        match by_state.iter_mut().find(|(s, _)| *s == job.state) {
            Some((_, n)) => *n += 1,
            None => by_state.push((job.state.clone(), 1)),
        }
    }
    let mut left = vec![Span::styled(format!(" {} jobs  ", q.jobs.len()), t.bold())];
    for (state, n) in &by_state {
        let cat = Cat::of(state);
        left.push(Span::styled(
            format!("{} {n} {}  ", glyph(cat, app.tick), state.to_lowercase()),
            Style::default().fg(t.state(state)),
        ));
    }
    let right = if q.inflight {
        vec![Span::styled(
            format!("{} squeue ", widgets::spinner(app.tick)),
            Style::default().fg(t.yellow),
        )]
    } else if let Some(e) = &q.error {
        vec![Span::styled(
            format!("⚠ {} ", truncate(e, 40)),
            Style::default().fg(t.red),
        )]
    } else if q.last > 0.0 {
        vec![Span::styled(
            format!(
                "⟳ {} ago · auto every {} ",
                age(q.last, now),
                duration(crate::app::QUEUE_INTERVAL)
            ),
            t.dim(),
        )]
    } else {
        vec![]
    };
    f.render_widget(
        Paragraph::new(line_lr(left, right, head.width as usize)),
        head,
    );

    // Table
    let width = table.width as usize;
    let name_w = width
        .saturating_sub(4 + 15 + 9 + 11 + 10 + 10 + 4 + 16)
        .clamp(16, 40);
    let header = format!(
        "    {:<14} {:<8} {:<name_w$} {:<10} {:>9} {:>9} {:>3} {}",
        "JOBID", "PART.", "NAME", "STATE", "TIME", "LIMIT", "N", "REASON / NODES"
    );
    let rest = width.saturating_sub(4 + 15 + 9 + name_w + 1 + 10 + 10 + 10 + 4);
    let mut lines = vec![Line::from(Span::styled(truncate(&header, width), t.dim()))];
    let visible = table.height.saturating_sub(1) as usize;
    let offset = q.selected.saturating_sub(visible.saturating_sub(1));

    for (k, job) in q.jobs.iter().enumerate().skip(offset).take(visible) {
        let selected = k == q.selected;
        let bg = if selected { t.surface } else { t.bg };
        let cat = Cat::of(&job.state);
        let bold = if cat == Cat::Running {
            Modifier::BOLD
        } else {
            Modifier::empty()
        };
        let mut spans = vec![
            Span::styled(
                if selected { "▌" } else { " " },
                Style::default().fg(t.accent),
            ),
            Span::styled(
                format!(" {} ", glyph(cat, app.tick)),
                Style::default().fg(t.state(&job.state)),
            ),
            Span::styled(
                format!("{:<14} ", truncate(&job.id, 14)),
                Style::default().fg(t.subtext),
            ),
            Span::styled(format!("{:<8} ", truncate(&job.partition, 8)), t.dim()),
        ];

        // Jobs of dawgz workflows are shown as "workflow › job"
        let name = match app.owner(&job.id) {
            Some((wf, label)) => vec![
                Span::styled("◆ ", Style::default().fg(t.accent)),
                Span::styled(format!("{wf} › "), Style::default().fg(t.accent2)),
                Span::styled(label, Style::default().fg(t.text).add_modifier(bold)),
            ],
            None => vec![Span::styled(
                job.name.clone(),
                Style::default().fg(t.text).add_modifier(bold),
            )],
        };
        let name = clip(name, name_w);
        let used = spans_width(&name);
        spans.extend(name);
        spans.push(Span::raw(" ".repeat(name_w.saturating_sub(used) + 1)));
        spans.extend([
            Span::styled(
                format!("{:<10}", truncate(&job.state.to_lowercase(), 10)),
                Style::default().fg(t.state(&job.state)),
            ),
            Span::styled(format!("{:>9} ", job.time), Style::default().fg(t.text)),
            Span::styled(format!("{:>9} ", job.limit), t.dim()),
            Span::styled(format!("{:>3} ", job.nodes), t.dim()),
            Span::styled(truncate(&job.reason, rest.max(8)), t.dim()),
        ]);
        lines.push(Line::from(spans).style(Style::default().bg(bg)));
    }
    if q.jobs.is_empty() {
        let text = if q.inflight || q.last == 0.0 {
            "loading…"
        } else {
            "no job in the queue"
        };
        lines.push(Line::from(Span::styled(format!("  {text}"), t.dim())));
    }
    f.render_widget(Paragraph::new(lines), table);

    let mut hints = Vec::new();
    for (k, label) in [
        ("↑↓", "select"),
        ("c", "cancel"),
        ("r", "refresh"),
        ("Q/esc", "back"),
    ] {
        hints.extend(key(t, k, label));
    }
    hints.push(Span::styled(
        format!("{} squeue calls this session", q.calls),
        t.dim(),
    ));
    f.render_widget(Paragraph::new(Line::from(hints)), foot);
}
