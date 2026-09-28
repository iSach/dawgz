#![allow(clippy::needless_range_loop, clippy::type_complexity)]
//! dawgz-tui: interactive terminal interface for dawgz workflows.

mod app;
mod graph;
mod logs;
mod model;
mod sacct;
mod store;
mod theme;
mod ui;
mod widgets;

use app::{App, Focus, Tab};
use ratatui::backend::TestBackend;
use ratatui::buffer::Buffer;
use ratatui::crossterm::event::{
    self, DisableMouseCapture, EnableMouseCapture, Event, KeyEventKind,
};
use ratatui::crossterm::execute;
use ratatui::style::{Color, Modifier};
use ratatui::Terminal;
use std::path::PathBuf;
use std::time::{Duration, Instant};

const HELP: &str = "\
dawgz-tui — interactive terminal interface for dawgz workflows

USAGE:
    dawgz-tui [OPTIONS]

OPTIONS:
    --dir <DIR>          dawgz directory (default: $DAWGZ_DIR or .dawgz), repeatable
    --all                show the workflows of all known dawgz directories
    --interval <SECS>    minimum time between two Slurm queries (default: $DAWGZ_SACCT_TTL or 30)
    --offline            never query Slurm, only read files
    --theme <NAME>       mocha (default), latte or terminal
    --snapshot <WxH>     render a single frame to stdout (ANSI) and exit
    --tab <TAB>          initial tab: jobs, graph, logs or info
    --select <N>         initially selected workflow
    --row <N>            initially selected job row (focuses the main pane)
    --expand             expand the selected row
    -V, --version        print version
    -h, --help           print help

The TUI reads the records written by dawgz (workflow.json, state.json, *.run.json and
logs). Slurm is queried in the background with a single batched `sacct` call, at most
once per interval, and only for jobs whose state cannot be known from files.
";

struct Args {
    dirs: Vec<PathBuf>,
    all: bool,
    interval: f64,
    offline: bool,
    theme: String,
    snapshot: Option<(u16, u16)>,
    tab: Option<Tab>,
    select: Option<usize>,
    row: Option<usize>,
    expand: bool,
}

fn parse_args() -> Result<Args, String> {
    let mut args = Args {
        dirs: Vec::new(),
        all: false,
        interval: std::env::var("DAWGZ_SACCT_TTL")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(30.0),
        offline: false,
        theme: std::env::var("DAWGZ_THEME").unwrap_or_else(|_| "auto".into()),
        snapshot: None,
        tab: None,
        select: None,
        row: None,
        expand: false,
    };

    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        let mut value = |name: &str| it.next().ok_or(format!("missing value for {name}"));
        match arg.as_str() {
            "--dir" => args.dirs.push(PathBuf::from(value("--dir")?)),
            "--all" => args.all = true,
            "--interval" => {
                args.interval = value("--interval")?
                    .parse()
                    .map_err(|_| "invalid interval")?
            }
            "--offline" => args.offline = true,
            "--theme" => args.theme = value("--theme")?,
            "--snapshot" => {
                let v = value("--snapshot")?;
                let (w, h) = v.split_once('x').ok_or("expected WxH")?;
                args.snapshot = Some((
                    w.parse().map_err(|_| "invalid width")?,
                    h.parse().map_err(|_| "invalid height")?,
                ));
            }
            "--tab" => {
                args.tab = Some(match value("--tab")?.as_str() {
                    "jobs" => Tab::Jobs,
                    "graph" => Tab::Graph,
                    "logs" => Tab::Logs,
                    "info" => Tab::Info,
                    other => return Err(format!("unknown tab '{other}'")),
                })
            }
            "--select" => {
                args.select = Some(value("--select")?.parse().map_err(|_| "invalid index")?)
            }
            "--row" => args.row = Some(value("--row")?.parse().map_err(|_| "invalid row")?),
            "--expand" => args.expand = true,
            "-V" | "--version" => {
                println!("dawgz-tui {}", env!("CARGO_PKG_VERSION"));
                std::process::exit(0);
            }
            "-h" | "--help" => {
                print!("{HELP}");
                std::process::exit(0);
            }
            other => return Err(format!("unknown argument '{other}' (see --help)")),
        }
    }

    // Slurm refreshes are shared by all monitors, never allow hammering it
    args.interval = args.interval.max(5.0);

    if args.dirs.is_empty() {
        let dir = std::env::var("DAWGZ_DIR").unwrap_or_else(|_| ".dawgz".into());
        args.dirs.push(PathBuf::from(dir));
    }

    args.dirs = args
        .dirs
        .into_iter()
        .map(|d| {
            let d = if let Some(rest) = d.to_str().and_then(|s| s.strip_prefix("~/")) {
                PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(rest)
            } else {
                d
            };
            std::fs::canonicalize(&d).unwrap_or(d)
        })
        .collect();

    Ok(args)
}

fn setup(app: &mut App, args: &Args) {
    if let Some(k) = args.select {
        app.selected = k.min(app.visible.len().saturating_sub(1));
        app.rebuild();
    }
    if let Some(r) = args.row {
        app.focus = Focus::Main;
        app.row_sel = r.min(app.rows.len().saturating_sub(1));
    }
    if args.expand {
        app.focus = Focus::Main;
        app.on_key(event::KeyEvent::from(event::KeyCode::Right));
    }
    if let Some(tab) = args.tab {
        app.focus = Focus::Main;
        app.tab = tab;
        if tab == Tab::Graph {
            if let Some(row) = app.rows.get(app.row_sel) {
                app.graph_sel = row.group();
            }
        }
    }
    app.reload_log(true);
}

fn main() {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("error: {e}");
            std::process::exit(2);
        }
    };

    let theme = theme::Theme::named(&args.theme);

    if let Some((w, h)) = args.snapshot {
        // No background worker: Slurm is queried at most once, synchronously
        let mut app = App::new(
            theme,
            args.dirs.clone(),
            args.all,
            args.interval,
            args.offline,
        );
        if !args.offline {
            app.refresh_now();
        }
        setup(&mut app, &args);
        let mut terminal = Terminal::new(TestBackend::new(w, h)).expect("backend");
        terminal.draw(|f| ui::draw(f, &mut app)).expect("draw");
        print!("{}", to_ansi(terminal.backend().buffer()));
        return;
    }

    let mut app = App::new(
        theme,
        args.dirs.clone(),
        args.all,
        args.interval,
        args.offline,
    );
    setup(&mut app, &args);
    app.spawn_worker();
    app.maybe_refresh(false);

    let mut terminal = ratatui::init();
    let _ = execute!(std::io::stdout(), EnableMouseCapture);
    let result = run(&mut terminal, &mut app);
    let _ = execute!(std::io::stdout(), DisableMouseCapture);
    ratatui::restore();

    if let Err(e) = result {
        eprintln!("error: {e}");
        std::process::exit(1);
    }
}

fn run(terminal: &mut ratatui::DefaultTerminal, app: &mut App) -> std::io::Result<()> {
    let frame = Duration::from_millis(100);
    let mut last = Instant::now();

    while !app.quit {
        terminal.draw(|f| ui::draw(f, app))?;

        let timeout = frame.saturating_sub(last.elapsed());
        if event::poll(timeout)? {
            match event::read()? {
                Event::Key(key) if key.kind == KeyEventKind::Press => app.on_key(key),
                Event::Mouse(mouse) => app.on_mouse(mouse),
                _ => {}
            }
        }

        if last.elapsed() >= frame {
            last = Instant::now();
            app.on_tick();
        }
    }

    Ok(())
}

/// Serializes a buffer with ANSI escape sequences (truecolor).
pub fn to_ansi(buf: &Buffer) -> String {
    fn color(c: Color, fg: bool) -> String {
        let base = if fg { 30 } else { 40 };
        match c {
            Color::Reset => format!("{}", if fg { 39 } else { 49 }),
            Color::Rgb(r, g, b) => format!("{};2;{r};{g};{b}", if fg { 38 } else { 48 }),
            Color::Indexed(i) => format!("{};5;{i}", if fg { 38 } else { 48 }),
            Color::Black => format!("{base}"),
            Color::Red => format!("{}", base + 1),
            Color::Green => format!("{}", base + 2),
            Color::Yellow => format!("{}", base + 3),
            Color::Blue => format!("{}", base + 4),
            Color::Magenta => format!("{}", base + 5),
            Color::Cyan => format!("{}", base + 6),
            Color::Gray => format!("{}", base + 7),
            Color::DarkGray => format!("{}", base + 60),
            Color::LightRed => format!("{}", base + 61),
            Color::LightGreen => format!("{}", base + 62),
            Color::LightYellow => format!("{}", base + 63),
            Color::LightBlue => format!("{}", base + 64),
            Color::LightMagenta => format!("{}", base + 65),
            Color::LightCyan => format!("{}", base + 66),
            Color::White => format!("{}", base + 67),
        }
    }

    let mut out = String::new();
    let area = buf.area;
    for y in 0..area.height {
        let mut last: Option<(Color, Color, Modifier)> = None;
        let mut skip = 0;
        for x in 0..area.width {
            let cell = &buf[(x, y)];
            if skip > 0 {
                skip -= 1;
                continue;
            }
            let style = (cell.fg, cell.bg, cell.modifier);
            if last != Some(style) {
                out.push_str("\x1b[0");
                if cell.modifier.contains(Modifier::BOLD) {
                    out.push_str(";1");
                }
                if cell.modifier.contains(Modifier::DIM) {
                    out.push_str(";2");
                }
                if cell.modifier.contains(Modifier::ITALIC) {
                    out.push_str(";3");
                }
                if cell.modifier.contains(Modifier::UNDERLINED) {
                    out.push_str(";4");
                }
                out.push(';');
                out.push_str(&color(cell.fg, true));
                out.push(';');
                out.push_str(&color(cell.bg, false));
                out.push('m');
                last = Some(style);
            }
            let symbol = cell.symbol();
            out.push_str(symbol);
            skip = unicode_width::UnicodeWidthStr::width(symbol).saturating_sub(1);
        }
        out.push_str("\x1b[0m\n");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;

    fn tempdir(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("dawgz-tui-test-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// A workflow as written by dawgz: a chain, a fan-out of 5 jobs and an array.
    fn fixture(name: &str) -> PathBuf {
        let dir = tempdir(name);
        let uid = "brave_otter_12345678";
        let wf = dir.join(uid);
        fs::create_dir_all(&wf).unwrap();
        fs::write(
            dir.join("workflows.csv"),
            format!("demo.py,{uid},2026-01-01 10:00:00,slurm,8,0\n"),
        )
        .unwrap();

        let mut jobs = vec![
            json!({"index": 0, "tag": "0000_prep", "name": "prep", "input": "prep()", "deps": [], "wait": "all", "jobid": "100", "source": 0}),
        ];
        for i in 1..=5 {
            jobs.push(json!({"index": i, "tag": format!("{i:04}_task"), "name": "task", "input": format!("task({i})"), "deps": [[0, "success"]], "wait": "all", "jobid": format!("101_{}", i - 1), "script": "0001_task.pack.sh", "source": 0}));
        }
        jobs.push(json!({"index": 6, "tag": "0006_train", "name": "train", "input": "train[0-3]", "array": 4, "deps": [[1, "success"], [2, "success"], [3, "success"], [4, "success"], [5, "success"]], "wait": "all", "jobid": "102", "source": 0}));
        jobs.push(json!({"index": 7, "tag": "0007_merge", "name": "merge", "input": "merge()", "deps": [[6, "success"]], "wait": "all", "jobid": "103", "source": 0}));

        let meta = json!({"format": 1, "uid": uid, "name": "demo.py", "backend": "slurm", "date": "2026-01-01T10:00:00", "timestamp": 1.0e9, "cwd": "/tmp", "argv": ["demo.py"], "host": "h", "user": "u", "pid": 1, "jobs": jobs, "sources": ["def task(i):\n    return i  # comment"]});
        fs::write(wf.join("workflow.json"), meta.to_string()).unwrap();

        let state = json!({"format": 1, "updated": store::now(), "source": "sacct", "jobs": {
            "0": {"state": "COMPLETED", "start": 1.0e9, "end": 1.0e9 + 60.0},
            "3": {"state": "FAILED", "start": 1.0e9, "end": 1.0e9 + 10.0, "exit": "1:0"},
            "6": {"state": "PENDING", "tasks": {}},
        }});
        fs::write(wf.join("state.json"), state.to_string()).unwrap();

        // Jobs report their own status
        for i in [1, 2] {
            fs::write(
                wf.join(format!("{i:04}_task.run.json")),
                json!({"state": "COMPLETED", "start": 1.0e9, "end": 1.0e9 + 5.0}).to_string(),
            )
            .unwrap();
        }
        fs::write(
            wf.join("0004_task.run.json"),
            json!({"state": "RUNNING", "start": 1.0e9, "progress": [{"desc": "epoch", "n": 3, "total": 10, "rate": 1.5}]}).to_string(),
        )
        .unwrap();
        fs::write(
            wf.join("0004_task.log"),
            "starting\r10%\r50%\nline two\n\x1b[31mred\x1b[0m\n",
        )
        .unwrap();
        dir
    }

    #[test]
    fn merges_states() {
        let dir = fixture("merge");
        let rows = store::registry(&dir);
        let w = store::Workflow::open(&dir, rows[0].clone()).unwrap();

        assert_eq!(w.summary(0).state, "COMPLETED");
        assert_eq!(w.summary(1).state, "COMPLETED"); // from run file
        assert_eq!(w.summary(3).state, "FAILED");
        assert_eq!(w.summary(4).state, "RUNNING");
        assert!((w.summary(4).fraction - 0.3).abs() < 1e-9);
        assert_eq!(w.summary(5).state, "PENDING");

        // A failed dependency cancels the array and its dependents
        assert_eq!(w.summary(6).state, "CANCELLED");
        assert_eq!(w.summary(7).state, "CANCELLED");
        assert_eq!(w.totals.total(), 11);

        // Only jobs that may have started are refreshed
        let stale = w.stale(0.0);
        assert!(stale.contains(&"101_3".to_string()));
        assert!(stale.contains(&"101_4".to_string()));
        assert!(!stale.contains(&"102".to_string()));
        assert!(!stale.contains(&"100".to_string()));
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn updates_cache() {
        let dir = fixture("update");
        let rows = store::registry(&dir);
        let mut w = store::Workflow::open(&dir, rows[0].clone()).unwrap();
        let entries = sacct::parse(
            "101_4|COMPLETED|None|Unknown|Unknown|00:00:10|0:0|n1|01:00:00\n",
            &[
                "JobID",
                "State",
                "Reason",
                "Start",
                "End",
                "Elapsed",
                "ExitCode",
                "NodeList",
                "Timelimit",
            ],
        );
        w.update(&entries, store::now());
        assert_eq!(w.summary(5).state, "COMPLETED");
        assert_eq!(w.summary(0).state, "COMPLETED"); // preserved
        let _ = fs::remove_dir_all(dir);
    }

    fn render(app: &mut App, w: u16, h: u16) -> String {
        let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
        terminal.draw(|f| ui::draw(f, app)).unwrap();
        let buf = terminal.backend().buffer();
        let mut text = String::new();
        for y in 0..h {
            for x in 0..w {
                text.push_str(buf[(x, y)].symbol());
            }
            text.push('\n');
        }
        text
    }

    #[test]
    fn renders_every_tab_and_size() {
        let dir = fixture("render");
        let mut app = App::new(theme::Theme::mocha(), vec![dir.clone()], false, 30.0, true);
        assert_eq!(app.workflows.len(), 1);

        for (w, h) in [(60, 15), (80, 24), (120, 40), (200, 60)] {
            for tab in Tab::ALL {
                app.tab = tab;
                for focus in [Focus::Workflows, Focus::Main] {
                    app.focus = focus;
                    let text = render(&mut app, w, h);
                    assert!(text.contains("dawgz"), "{w}x{h} {tab:?}");
                }
            }
        }

        app.tab = Tab::Jobs;
        app.focus = Focus::Main;
        let text = render(&mut app, 160, 48);
        assert!(text.contains("demo.py"));
        assert!(text.contains("task ×5"));
        assert!(text.contains("train[0-3]"));
        assert!(text.contains("Timeline"));
        assert!(text.contains("Activity"));

        // Expand the fan-out
        app.row_sel = 1;
        app.on_key(event::KeyEvent::from(event::KeyCode::Right));
        let text = render(&mut app, 160, 48);
        assert!(text.contains("task(4)"));

        // Logs of the running task, with carriage returns collapsed
        app.row_sel = app
            .rows
            .iter()
            .position(|r| matches!(r, app::Row::Member { job: 4, .. }))
            .unwrap();
        app.tab = Tab::Logs;
        app.reload_log(true);
        let text = render(&mut app, 120, 30);
        assert!(text.contains("50%"));
        assert!(!text.contains("10%"));
        assert!(text.contains("line two"));

        // Graph and info
        app.tab = Tab::Graph;
        let text = render(&mut app, 160, 40);
        assert!(text.contains("merge"));
        app.tab = Tab::Info;
        let text = render(&mut app, 160, 40);
        assert!(text.contains("def task"));

        // Search
        app.tab = Tab::Jobs;
        app.focus = Focus::Main;
        app.on_key(event::KeyEvent::from(event::KeyCode::Char('/')));
        for c in "mrg".chars() {
            app.on_key(event::KeyEvent::from(event::KeyCode::Char(c)));
        }
        assert_eq!(app.rows.len(), 1);
        app.on_key(event::KeyEvent::from(event::KeyCode::Esc));
        assert!(app.rows.len() > 1);

        // Help popup
        app.on_key(event::KeyEvent::from(event::KeyCode::Char('?')));
        let text = render(&mut app, 120, 40);
        assert!(text.contains("dawgz help"));

        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn ansi_snapshot() {
        let dir = fixture("ansi");
        let mut app = App::new(theme::Theme::mocha(), vec![dir.clone()], false, 30.0, true);
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
        terminal.draw(|f| ui::draw(f, &mut app)).unwrap();
        let ansi = to_ansi(terminal.backend().buffer());
        assert!(ansi.contains("\x1b["));
        assert_eq!(ansi.lines().count(), 30);
        let _ = fs::remove_dir_all(dir);
    }
}
