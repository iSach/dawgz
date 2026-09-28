//! Application state, refresh logic and key handling.

use crate::graph::{self, Dag};
use crate::logs;
use crate::model::{Cat, JobMeta};
use crate::sacct;
use crate::store::{self, now, Workflow};
use crate::theme::Theme;
use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseEvent, MouseEventKind};
use ratatui::layout::Rect;
use ratatui::style::Color;
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::mpsc::{channel, Receiver, Sender};
use std::time::{Duration, Instant, SystemTime};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Focus {
    Workflows,
    Main,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Tab {
    Jobs,
    Graph,
    Logs,
    Info,
}

impl Tab {
    pub const ALL: [Tab; 4] = [Tab::Jobs, Tab::Graph, Tab::Logs, Tab::Info];

    pub fn title(self) -> &'static str {
        match self {
            Tab::Jobs => "Jobs",
            Tab::Graph => "Graph",
            Tab::Logs => "Logs",
            Tab::Info => "Info",
        }
    }
}

/// A row of the jobs table.
#[derive(Clone, Debug, PartialEq)]
pub enum Row {
    /// A node of the graph: a job (index into jobs) or a group of sibling jobs.
    Node { group: usize, jobs: Vec<usize> },
    /// A job of an expanded group.
    Member { group: usize, job: usize },
    /// An element of an expanded array.
    Element { group: usize, job: usize, i: usize },
}

impl Row {
    pub fn group(&self) -> usize {
        match self {
            Row::Node { group, .. } | Row::Member { group, .. } | Row::Element { group, .. } => {
                *group
            }
        }
    }
}

pub enum Popup {
    Help,
    Confirm { message: String, action: Action },
}

#[derive(Clone, Debug)]
pub enum Action {
    Cancel {
        workflow: usize,
        job: Option<usize>,
        i: Option<usize>,
    },
}

pub struct Toast {
    pub text: String,
    pub color: Color,
    pub until: Instant,
}

pub struct Search {
    pub active: bool,
    pub query: String,
}

pub struct LogView {
    pub key: Option<(String, usize, Option<usize>)>,
    pub path: Option<PathBuf>,
    pub lines: Vec<String>,
    pub size: u64,
    pub truncated: bool,
    pub mtime: Option<SystemTime>,
    pub scroll: usize,
    pub follow: bool,
    pub wrap: bool,
}

#[derive(Default, Clone, Copy)]
pub struct Areas {
    pub sidebar: Rect,
    pub cards: Rect,
    pub card_height: u16,
    pub card_offset: usize,
    pub table: Rect,
    pub table_offset: usize,
    pub tabs: Rect,
    pub main: Rect,
}

struct Request {
    uids: Vec<String>,
    jobids: Vec<String>,
}

struct Response {
    uids: Vec<String>,
    when: f64,
    result: Result<HashMap<String, serde_json::Value>, String>,
}

pub struct App {
    pub theme: Theme,
    pub dirs: Vec<PathBuf>,
    pub home_dirs: Vec<PathBuf>,
    pub all_dirs: bool,
    pub workflows: Vec<Workflow>,
    pub visible: Vec<usize>,
    pub selected: usize,
    pub focus: Focus,
    pub tab: Tab,
    pub groups: Vec<Vec<usize>>,
    pub parents: Vec<Vec<usize>>,
    pub gutter: Vec<Vec<[char; 2]>>,
    pub dag: Dag,
    pub graph_sel: usize,
    pub graph_scroll: (u16, u16),
    pub rows: Vec<Row>,
    pub row_sel: usize,
    pub expanded: HashSet<(String, usize)>,
    pub search: Search,
    pub job_query: String,
    pub active_only: bool,
    pub state_filter: Option<Cat>,
    pub log: LogView,
    pub info_scroll: usize,
    pub toasts: Vec<Toast>,
    pub popup: Option<Popup>,
    pub message: Option<(String, Instant)>,
    pub tick: u64,
    pub ttl: f64,
    pub offline: bool,
    pub quit: bool,
    pub areas: Areas,
    pub sacct_inflight: bool,
    pub sacct_error: Option<String>,
    pub sacct_last: f64,
    pub sacct_calls: usize,
    last_forced: f64,
    known: HashMap<(String, usize), Cat>,
    finished: HashSet<String>,
    registry_mtimes: HashMap<PathBuf, Option<SystemTime>>,
    last_reload: Instant,
    last_background: Instant,
    last_registry: Instant,
    tx: Option<Sender<Request>>,
    rx: Option<Receiver<Response>>,
    pub limit: usize,
}

impl App {
    pub fn new(theme: Theme, dirs: Vec<PathBuf>, all_dirs: bool, ttl: f64, offline: bool) -> App {
        let mut app = App {
            theme,
            home_dirs: dirs.clone(),
            dirs,
            all_dirs,
            workflows: Vec::new(),
            visible: Vec::new(),
            selected: 0,
            focus: Focus::Workflows,
            tab: Tab::Jobs,
            groups: Vec::new(),
            parents: Vec::new(),
            gutter: Vec::new(),
            dag: Dag::default(),
            graph_sel: 0,
            graph_scroll: (0, 0),
            rows: Vec::new(),
            row_sel: 0,
            expanded: HashSet::new(),
            search: Search {
                active: false,
                query: String::new(),
            },
            job_query: String::new(),
            active_only: false,
            state_filter: None,
            log: LogView {
                key: None,
                path: None,
                lines: Vec::new(),
                size: 0,
                truncated: false,
                mtime: None,
                scroll: 0,
                follow: true,
                wrap: false,
            },
            info_scroll: 0,
            toasts: Vec::new(),
            popup: None,
            message: None,
            tick: 0,
            ttl,
            offline,
            quit: false,
            areas: Areas::default(),
            sacct_inflight: false,
            sacct_error: None,
            sacct_last: 0.0,
            sacct_calls: 0,
            last_forced: 0.0,
            known: HashMap::new(),
            finished: HashSet::new(),
            registry_mtimes: HashMap::new(),
            last_reload: Instant::now(),
            last_background: Instant::now(),
            last_registry: Instant::now(),
            tx: None,
            rx: None,
            limit: 200,
        };

        if all_dirs {
            app.dirs = app.all_known_dirs();
        }

        app.load();

        // Without workflows here, show those of all known directories
        if app.workflows.is_empty() && !app.all_dirs {
            let known = store::known_dirs();
            if !known.is_empty() {
                app.all_dirs = true;
                app.dirs = app.all_known_dirs();
                app.load();
            }
        }

        app.rebuild();
        app
    }

    fn all_known_dirs(&self) -> Vec<PathBuf> {
        let mut dirs = self.home_dirs.clone();
        for d in store::known_dirs() {
            if !dirs.contains(&d) {
                dirs.push(d);
            }
        }
        dirs
    }

    /// Starts the background thread querying Slurm.
    pub fn spawn_worker(&mut self) {
        let (tx, rx_req) = channel::<Request>();
        let (tx_res, rx) = channel::<Response>();
        std::thread::spawn(move || {
            while let Ok(req) = rx_req.recv() {
                let when = now();
                let result = sacct::query(&req.jobids);
                if tx_res
                    .send(Response {
                        uids: req.uids,
                        when,
                        result,
                    })
                    .is_err()
                {
                    break;
                }
            }
        });
        self.tx = Some(tx);
        self.rx = Some(rx);
    }

    // Loading

    fn load(&mut self) {
        let selected_uid = self.current().map(|w| w.uid().to_string());
        let mut loaded: HashMap<String, Workflow> = self
            .workflows
            .drain(..)
            .map(|w| (format!("{}/{}", w.dir.display(), w.uid()), w))
            .collect();

        for dir in self.dirs.clone() {
            let csv = dir.join("workflows.csv");
            self.registry_mtimes.insert(
                dir.clone(),
                std::fs::metadata(&csv).and_then(|m| m.modified()).ok(),
            );
            let rows = store::registry(&dir);
            let skip = rows.len().saturating_sub(self.limit);
            for row in rows.into_iter().skip(skip) {
                let key = format!("{}/{}", dir.display(), row.uid);
                if let Some(w) = loaded.remove(&key) {
                    self.workflows.push(w);
                } else if let Some(w) = Workflow::open(&dir, row) {
                    self.workflows.push(w);
                }
            }
        }

        // Newest first (registry order breaks ties between workflows of the same second)
        let order: HashMap<String, usize> = self
            .workflows
            .iter()
            .enumerate()
            .map(|(k, w)| (format!("{}/{}", w.dir.display(), w.uid()), k))
            .collect();
        self.workflows.sort_by(|a, b| {
            b.meta
                .timestamp
                .floor()
                .total_cmp(&a.meta.timestamp.floor())
                .then_with(|| {
                    let ka = order[&format!("{}/{}", a.dir.display(), a.uid())];
                    let kb = order[&format!("{}/{}", b.dir.display(), b.uid())];
                    kb.cmp(&ka)
                })
        });

        for w in &self.workflows {
            if !w.active() {
                self.finished.insert(w.uid().to_string());
            }
            for (k, s) in w.summaries.iter().enumerate() {
                self.known.insert((w.uid().to_string(), k), s.cat());
            }
        }

        self.filter();

        if let Some(uid) = selected_uid {
            if let Some(pos) = self
                .visible
                .iter()
                .position(|&k| self.workflows[k].uid() == uid)
            {
                self.selected = pos;
            }
        }
    }

    pub fn filter(&mut self) {
        let query = if self.focus == Focus::Workflows {
            self.search.query.clone()
        } else {
            String::new()
        };
        self.visible = (0..self.workflows.len())
            .filter(|&k| {
                let w = &self.workflows[k];
                (!self.active_only || w.active())
                    && (query.is_empty()
                        || widgets_fuzzy(&query, w.name())
                        || widgets_fuzzy(&query, w.uid()))
            })
            .collect();
        self.selected = self.selected.min(self.visible.len().saturating_sub(1));
    }

    pub fn current(&self) -> Option<&Workflow> {
        self.visible.get(self.selected).map(|&k| &self.workflows[k])
    }

    /// Rebuilds the job rows and graph of the selected workflow.
    pub fn rebuild(&mut self) {
        let Some(w) = self.current() else {
            self.groups.clear();
            self.rows.clear();
            self.gutter.clear();
            self.dag = Dag::default();
            return;
        };

        let jobs = w.jobs();
        let groups = graph::groups(jobs, 3);
        let parents = graph::group_parents(jobs, &groups);
        let uid = w.uid().to_string();
        let query = self.job_query.to_lowercase();
        let mut rows = Vec::new();

        for (g, members) in groups.iter().enumerate() {
            let matches = |k: usize| {
                let job = &jobs[k];
                (query.is_empty()
                    || widgets_fuzzy(&query, &job.name)
                    || job.input.to_lowercase().contains(&query))
                    && self.state_filter.is_none_or(|f| {
                        let s = &w.summaries[job.index];
                        s.counts.get(f) > 0
                    })
            };
            if !members.iter().any(|&k| matches(k)) {
                continue;
            }
            rows.push(Row::Node {
                group: g,
                jobs: members.clone(),
            });
            if self.expanded.contains(&(uid.clone(), g)) {
                if members.len() > 1 {
                    for &k in members {
                        if matches(k) {
                            rows.push(Row::Member { group: g, job: k });
                        }
                    }
                } else if let Some(n) = jobs[members[0]].array {
                    for i in 0..n {
                        rows.push(Row::Element {
                            group: g,
                            job: members[0],
                            i,
                        });
                    }
                }
            }
        }

        self.gutter = graph::lanes(&parents);
        let node_w = if self.dag.node_w == 0 {
            graph::NODE_W
        } else {
            self.dag.node_w
        };
        self.dag = Dag::layout_with(&parents, node_w);
        self.groups = groups;
        self.parents = parents;
        self.rows = rows;
        self.row_sel = self.row_sel.min(self.rows.len().saturating_sub(1));
        self.graph_sel = self.graph_sel.min(self.groups.len().saturating_sub(1));
    }

    /// The job (and element) targeted by the selection.
    pub fn target(&self) -> Option<(usize, Option<usize>)> {
        let w = self.current()?;
        match self.rows.get(self.row_sel)? {
            Row::Node { jobs, .. } => {
                if jobs.len() == 1 {
                    let job = &w.jobs()[jobs[0]];
                    if job.is_array() {
                        Some((jobs[0], Some(interesting(w, job))))
                    } else {
                        Some((jobs[0], None))
                    }
                } else {
                    // The most interesting member of a group
                    let pick = jobs
                        .iter()
                        .copied()
                        .min_by_key(|&k| (rank(w.summaries[w.jobs()[k].index].cat()), k))
                        .unwrap_or(jobs[0]);
                    Some((pick, None))
                }
            }
            Row::Member { job, .. } => Some((*job, None)),
            Row::Element { job, i, .. } => Some((*job, Some(*i))),
        }
    }

    // Refresh

    pub fn on_tick(&mut self) {
        self.tick = self.tick.wrapping_add(1);
        self.toasts.retain(|t| t.until > Instant::now());

        if let Some((_, until)) = &self.message {
            if *until < Instant::now() {
                self.message = None;
            }
        }

        // Slurm responses
        let mut responses = Vec::new();
        if let Some(rx) = &self.rx {
            while let Ok(r) = rx.try_recv() {
                responses.push(r);
            }
        }
        for r in responses {
            self.sacct_inflight = false;
            match r.result {
                Ok(entries) => {
                    self.sacct_error = None;
                    self.sacct_last = r.when;
                    for w in self
                        .workflows
                        .iter_mut()
                        .filter(|w| r.uids.contains(&w.uid().to_string()))
                    {
                        w.update(&entries, r.when);
                    }
                    self.after_reload();
                }
                Err(e) => {
                    self.sacct_error = Some(e);
                    self.sacct_last = r.when;
                }
            }
        }

        // Files: selected workflow every second, others every 5 seconds
        if self.last_reload.elapsed() >= Duration::from_millis(1000) {
            self.last_reload = Instant::now();
            let background = self.last_background.elapsed() >= Duration::from_secs(5);
            if background {
                self.last_background = Instant::now();
            }
            let selected = self.visible.get(self.selected).copied();
            let mut changed = false;
            for (k, w) in self.workflows.iter_mut().enumerate() {
                if Some(k) == selected || (background && (w.active() || w.loaded == 0.0)) {
                    changed |= w.reload(false);
                }
            }
            if changed {
                self.after_reload();
            }
            self.reload_log(false);
        }

        // New workflows
        if self.last_registry.elapsed() >= Duration::from_secs(3) {
            self.last_registry = Instant::now();
            let changed = self.dirs.iter().any(|d| {
                let m = std::fs::metadata(d.join("workflows.csv"))
                    .and_then(|m| m.modified())
                    .ok();
                self.registry_mtimes.get(d) != Some(&m)
            });
            if changed {
                self.load();
                self.rebuild();
            }
        }

        self.maybe_refresh(false);
    }

    fn after_reload(&mut self) {
        let mut toasts = Vec::new();

        for w in &self.workflows {
            let uid = w.uid().to_string();
            for (k, s) in w.summaries.iter().enumerate() {
                let cat = s.cat();
                let old = self.known.insert((uid.clone(), k), cat);
                if old.is_some() && old != Some(cat) && cat == Cat::Failed {
                    toasts.push((
                        format!("✘ {} · {} failed", w.name(), w.jobs()[k].label()),
                        self.theme.red,
                    ));
                }
            }
            if !w.active() && w.totals.total() > 0 && self.finished.insert(uid) {
                let (text, color) = if w.totals.failed + w.totals.cancelled > 0 {
                    (
                        format!(
                            "⚠ {} finished with {} failure(s)",
                            w.name(),
                            w.totals.failed + w.totals.cancelled
                        ),
                        self.theme.peach,
                    )
                } else {
                    (format!("✔ {} finished", w.name()), self.theme.green)
                };
                toasts.push((text, color));
            }
        }

        for (text, color) in toasts.into_iter().take(3) {
            self.toast(text, color);
        }

        self.filter();
        self.rebuild();
    }

    pub fn toast(&mut self, text: String, color: Color) {
        self.toasts.push(Toast {
            text,
            color,
            until: Instant::now() + Duration::from_secs(6),
        });
        if self.toasts.len() > 4 {
            self.toasts.remove(0);
        }
    }

    pub fn flash(&mut self, text: impl Into<String>) {
        self.message = Some((text.into(), Instant::now() + Duration::from_secs(4)));
    }

    /// Queries Slurm synchronously (used by snapshots).
    pub fn refresh_now(&mut self) {
        let mut uids = Vec::new();
        let mut jobids = Vec::new();
        for &k in self.visible.iter().take(50) {
            let w = &self.workflows[k];
            let stale = w.stale(self.ttl);
            if !stale.is_empty() {
                uids.push(w.uid().to_string());
                jobids.extend(stale);
            }
        }
        if jobids.is_empty() {
            return;
        }
        let when = now();
        match sacct::query(&jobids) {
            Ok(entries) => {
                for w in self
                    .workflows
                    .iter_mut()
                    .filter(|w| uids.contains(&w.uid().to_string()))
                {
                    w.update(&entries, when);
                }
                self.sacct_last = when;
                self.sacct_calls += 1;
                self.filter();
                self.rebuild();
            }
            Err(e) => self.sacct_error = Some(e),
        }
    }

    /// Queries Slurm (in the background) for the stale jobs of visible workflows.
    pub fn maybe_refresh(&mut self, force: bool) {
        if self.offline || self.sacct_inflight || self.tx.is_none() {
            return;
        }

        // Never more than once per 5 seconds, even when forced
        let t = now();
        if force && t - self.last_forced < 5.0 {
            self.flash("slurm was queried less than 5 seconds ago");
            return;
        }

        let ttl = if force { 0.0 } else { self.ttl };
        let mut uids = Vec::new();
        let mut jobids = Vec::new();

        for &k in self.visible.iter().take(50) {
            let w = &self.workflows[k];
            let stale = w.stale(ttl);
            if !stale.is_empty() {
                uids.push(w.uid().to_string());
                jobids.extend(stale);
            }
        }

        if jobids.is_empty() {
            if force {
                self.flash("nothing to refresh");
            }
            return;
        }

        if force {
            self.last_forced = t;
        }

        if let Some(tx) = &self.tx {
            if tx.send(Request { uids, jobids }).is_ok() {
                self.sacct_inflight = true;
                self.sacct_calls += 1;
            }
        }
    }

    pub fn reload_log(&mut self, force: bool) {
        let key = self.current().and_then(|w| {
            let (k, i) = self.target()?;
            Some((w.uid().to_string(), k, i))
        });

        let path = self.current().and_then(|w| {
            let (k, i) = self.target()?;
            Some(w.log_path(&w.jobs()[k], i))
        });

        if key != self.log.key {
            self.log.key = key;
            self.log.scroll = 0;
            self.log.follow = true;
            self.log.mtime = None;
        }

        let Some(path) = path else {
            self.log.lines.clear();
            self.log.path = None;
            return;
        };

        let mtime = std::fs::metadata(&path).and_then(|m| m.modified()).ok();
        if !force && mtime == self.log.mtime && self.log.path.as_ref() == Some(&path) {
            return;
        }

        self.log.mtime = mtime;
        self.log.path = Some(path.clone());

        match logs::read(&path) {
            Some(log) => {
                self.log.lines = log.lines;
                self.log.size = log.size;
                self.log.truncated = log.truncated;
            }
            None => {
                // Errors recorded instead of logs (e.g. submission errors)
                let text = self.current().and_then(|w| {
                    let (k, i) = self.target()?;
                    let e = w.entry(&w.jobs()[k], i);
                    e.trace.or(e.error)
                });
                self.log.lines = text
                    .map(|t| t.lines().map(String::from).collect())
                    .unwrap_or_default();
                self.log.size = 0;
                self.log.truncated = false;
            }
        }
    }

    // Input

    pub fn on_key(&mut self, key: KeyEvent) {
        if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
            self.quit = true;
            return;
        }

        if let Some(popup) = self.popup.take() {
            match popup {
                Popup::Help => {}
                Popup::Confirm { action, message } => match key.code {
                    KeyCode::Char('y') | KeyCode::Char('Y') | KeyCode::Enter => {
                        self.execute(action)
                    }
                    KeyCode::Char('n') | KeyCode::Esc | KeyCode::Char('q') => {}
                    _ => self.popup = Some(Popup::Confirm { message, action }),
                },
            }
            return;
        }

        if self.search.active {
            match key.code {
                KeyCode::Esc => {
                    self.search.active = false;
                    self.search.query.clear();
                    self.apply_search();
                }
                KeyCode::Enter => self.search.active = false,
                KeyCode::Backspace => {
                    self.search.query.pop();
                    self.apply_search();
                }
                KeyCode::Char(c) => {
                    self.search.query.push(c);
                    self.apply_search();
                }
                KeyCode::Down => self.move_selection(1),
                KeyCode::Up => self.move_selection(-1),
                _ => {}
            }
            return;
        }

        match key.code {
            KeyCode::Char('q') => self.quit = true,
            KeyCode::Char('?') => self.popup = Some(Popup::Help),
            KeyCode::Esc => {
                if !self.search.query.is_empty() || !self.job_query.is_empty() {
                    self.search.query.clear();
                    self.job_query.clear();
                    self.filter();
                    self.rebuild();
                } else if self.focus == Focus::Main {
                    self.focus = Focus::Workflows;
                }
            }
            KeyCode::Char('/') => {
                self.search.active = true;
                self.search.query = if self.focus == Focus::Main {
                    self.job_query.clone()
                } else {
                    self.search.query.clone()
                };
            }
            KeyCode::Char('1') => self.set_tab(Tab::Jobs),
            KeyCode::Char('2') => self.set_tab(Tab::Graph),
            KeyCode::Char('3') => self.set_tab(Tab::Logs),
            KeyCode::Char('4') => self.set_tab(Tab::Info),
            KeyCode::Tab => {
                let k = Tab::ALL.iter().position(|&t| t == self.tab).unwrap_or(0);
                self.set_tab(Tab::ALL[(k + 1) % Tab::ALL.len()]);
            }
            KeyCode::BackTab => {
                let k = Tab::ALL.iter().position(|&t| t == self.tab).unwrap_or(0);
                self.set_tab(Tab::ALL[(k + Tab::ALL.len() - 1) % Tab::ALL.len()]);
            }
            KeyCode::Char('a') => {
                self.active_only = !self.active_only;
                self.filter();
                self.rebuild();
                self.flash(if self.active_only {
                    "showing active workflows only"
                } else {
                    "showing all workflows"
                });
            }
            KeyCode::Char('D') => {
                self.all_dirs = !self.all_dirs;
                self.dirs = if self.all_dirs {
                    self.all_known_dirs()
                } else {
                    self.home_dirs.clone()
                };
                self.load();
                self.rebuild();
                self.flash(if self.all_dirs {
                    "workflows of all known directories"
                } else {
                    "workflows of the current directory"
                });
            }
            KeyCode::Char('s') => {
                self.state_filter = match self.state_filter {
                    None => Some(Cat::Running),
                    Some(Cat::Running) => Some(Cat::Failed),
                    Some(Cat::Failed) => Some(Cat::Pending),
                    Some(Cat::Pending) => Some(Cat::Done),
                    _ => None,
                };
                self.rebuild();
                self.flash(match self.state_filter {
                    None => "all jobs".to_string(),
                    Some(c) => format!("{} jobs only", c.name()),
                });
            }
            KeyCode::Char('r') => self.maybe_refresh(true),
            KeyCode::Char('c') => self.ask_cancel(),
            KeyCode::Char('y') => self.copy_jobid(),
            KeyCode::Char('f') if self.tab == Tab::Logs => {
                self.log.follow = !self.log.follow;
                if self.log.follow {
                    self.log.scroll = 0;
                }
            }
            KeyCode::Char('w') if self.tab == Tab::Logs => self.log.wrap = !self.log.wrap,
            KeyCode::Char('e') | KeyCode::Char(' ') => {
                self.toggle_expand();
            }
            KeyCode::Char('g') | KeyCode::Home => self.jump(true),
            KeyCode::Char('G') | KeyCode::End => self.jump(false),
            KeyCode::PageDown => self.page(1),
            KeyCode::PageUp => self.page(-1),
            KeyCode::Down | KeyCode::Char('j') => self.arrow(0, 1),
            KeyCode::Up | KeyCode::Char('k') => self.arrow(0, -1),
            KeyCode::Left | KeyCode::Char('h') => self.arrow(-1, 0),
            KeyCode::Right | KeyCode::Char('l') => self.arrow(1, 0),
            KeyCode::Enter => self.enter(),
            _ => {}
        }
    }

    fn apply_search(&mut self) {
        if self.focus == Focus::Main {
            self.job_query = self.search.query.clone();
            self.row_sel = 0;
            self.rebuild();
        } else {
            self.selected = 0;
            self.filter();
            self.rebuild();
        }
    }

    fn set_tab(&mut self, tab: Tab) {
        self.tab = tab;
        if self.focus == Focus::Workflows && tab != Tab::Jobs {
            self.focus = Focus::Main;
        }
        if tab == Tab::Logs {
            self.reload_log(true);
        }
        if tab == Tab::Graph {
            // Select the node of the current row
            if let Some(row) = self.rows.get(self.row_sel) {
                self.graph_sel = row.group();
            }
        }
    }

    fn arrow(&mut self, dx: i32, dy: i32) {
        match (self.focus, self.tab) {
            (Focus::Workflows, _) => {
                if dx > 0 {
                    self.focus = Focus::Main;
                } else if dy != 0 {
                    self.move_selection(dy);
                }
            }
            (Focus::Main, Tab::Graph) => {
                if let Some(v) = (self.graph_sel < self.dag.nodes.len())
                    .then(|| self.dag.neighbor(self.graph_sel, dx, dy))
                    .flatten()
                {
                    self.graph_sel = v;
                } else if dx < 0 {
                    self.focus = Focus::Workflows;
                }
            }
            (Focus::Main, Tab::Logs) => {
                if dy != 0 {
                    self.scroll_log(-dy);
                } else if dx < 0 {
                    self.focus = Focus::Workflows;
                }
            }
            (Focus::Main, Tab::Info) => {
                if dy != 0 {
                    self.info_scroll = (self.info_scroll as i64 + dy as i64).max(0) as usize;
                } else if dx < 0 {
                    self.focus = Focus::Workflows;
                }
            }
            (Focus::Main, Tab::Jobs) => {
                if dy != 0 {
                    self.move_row(dy);
                } else if dx > 0 {
                    self.expand(true);
                } else if dx < 0 && !self.expand(false) {
                    self.focus = Focus::Workflows;
                }
            }
        }
    }

    fn move_selection(&mut self, dy: i32) {
        if self.focus == Focus::Main && self.tab == Tab::Jobs {
            self.move_row(dy);
            return;
        }
        if self.visible.is_empty() {
            return;
        }
        let n = self.visible.len() as i64;
        self.selected = (self.selected as i64 + dy as i64).clamp(0, n - 1) as usize;
        self.row_sel = 0;
        self.graph_sel = 0;
        self.info_scroll = 0;
        self.rebuild();
        self.reload_log(true);
    }

    fn move_row(&mut self, dy: i32) {
        if self.rows.is_empty() {
            return;
        }
        let n = self.rows.len() as i64;
        self.row_sel = (self.row_sel as i64 + dy as i64).clamp(0, n - 1) as usize;
        self.reload_log(true);
    }

    fn page(&mut self, d: i32) {
        match (self.focus, self.tab) {
            (Focus::Main, Tab::Logs) => self.scroll_log(-d * 20),
            (Focus::Main, Tab::Info) => {
                self.info_scroll = (self.info_scroll as i64 + d as i64 * 20).max(0) as usize
            }
            _ => self.move_selection(d * 10),
        }
    }

    fn jump(&mut self, top: bool) {
        match (self.focus, self.tab) {
            (Focus::Main, Tab::Logs) => {
                if top {
                    self.log.scroll = self.log.lines.len();
                    self.log.follow = false;
                } else {
                    self.log.scroll = 0;
                    self.log.follow = true;
                }
            }
            (Focus::Main, Tab::Jobs) => {
                self.row_sel = if top {
                    0
                } else {
                    self.rows.len().saturating_sub(1)
                };
                self.reload_log(true);
            }
            _ => {
                self.selected = if top {
                    0
                } else {
                    self.visible.len().saturating_sub(1)
                };
                self.rebuild();
            }
        }
    }

    pub fn scroll_log(&mut self, d: i32) {
        // `scroll` counts lines from the bottom
        let max = self.log.lines.len();
        self.log.scroll = (self.log.scroll as i64 + d as i64).clamp(0, max as i64) as usize;
        self.log.follow = self.log.scroll == 0;
    }

    fn enter(&mut self) {
        match (self.focus, self.tab) {
            (Focus::Workflows, _) => self.focus = Focus::Main,
            (Focus::Main, Tab::Graph) => {
                let g = self.graph_sel;
                if let Some(k) = self
                    .rows
                    .iter()
                    .position(|r| matches!(r, Row::Node { group, .. } if *group == g))
                {
                    self.row_sel = k;
                }
                self.tab = Tab::Jobs;
            }
            (Focus::Main, Tab::Jobs) if !self.toggle_expand() => self.set_tab(Tab::Logs),
            _ => {}
        }
    }

    /// Expands or collapses the selected group or array. Returns whether it could.
    fn expand(&mut self, open: bool) -> bool {
        let Some(w) = self.current() else {
            return false;
        };
        let uid = w.uid().to_string();
        let Some(row) = self.rows.get(self.row_sel).cloned() else {
            return false;
        };
        let g = row.group();
        let expandable = self
            .groups
            .get(g)
            .is_some_and(|m| m.len() > 1 || w.jobs()[m[0]].is_array());
        if !expandable {
            return false;
        }
        let key = (uid, g);
        let is_open = self.expanded.contains(&key);
        if open == is_open {
            if !open && !matches!(row, Row::Node { .. }) {
                // Collapse from a child row
                self.expanded.remove(&key);
                self.rebuild();
                if let Some(k) = self
                    .rows
                    .iter()
                    .position(|r| matches!(r, Row::Node { group, .. } if *group == g))
                {
                    self.row_sel = k;
                }
                return true;
            }
            return false;
        }
        if open {
            self.expanded.insert(key);
        } else {
            self.expanded.remove(&key);
        }
        self.rebuild();
        if !open {
            if let Some(k) = self
                .rows
                .iter()
                .position(|r| matches!(r, Row::Node { group, .. } if *group == g))
            {
                self.row_sel = k;
            }
        }
        true
    }

    fn toggle_expand(&mut self) -> bool {
        let Some(w) = self.current() else {
            return false;
        };
        let uid = w.uid().to_string();
        let Some(row) = self.rows.get(self.row_sel) else {
            return false;
        };
        let open = self.expanded.contains(&(uid, row.group()));
        self.expand(!open)
    }

    fn ask_cancel(&mut self) {
        let Some(&wk) = self.visible.get(self.selected) else {
            return;
        };
        let w = &self.workflows[wk];

        let (job, i, what) = if self.focus == Focus::Workflows {
            (None, None, format!("workflow {} ({})", w.name(), w.uid()))
        } else {
            match self.rows.get(self.row_sel) {
                Some(Row::Element { job, i, .. }) => (
                    Some(w.jobs()[*job].index),
                    Some(*i),
                    format!("{}[{i}]", w.jobs()[*job].name),
                ),
                Some(Row::Member { job, .. }) => {
                    (Some(w.jobs()[*job].index), None, w.jobs()[*job].label())
                }
                Some(Row::Node { jobs, .. }) if jobs.len() == 1 => (
                    Some(w.jobs()[jobs[0]].index),
                    None,
                    w.jobs()[jobs[0]].label(),
                ),
                _ => (None, None, format!("workflow {} ({})", w.name(), w.uid())),
            }
        };

        self.popup = Some(Popup::Confirm {
            message: format!("Cancel {what}?"),
            action: Action::Cancel {
                workflow: wk,
                job,
                i,
            },
        });
    }

    fn execute(&mut self, action: Action) {
        match action {
            Action::Cancel { workflow, job, i } => {
                if let Some(w) = self.workflows.get_mut(workflow) {
                    let message = w.cancel(job, i);
                    self.flash(message);
                }
                self.after_reload();
            }
        }
    }

    fn copy_jobid(&mut self) {
        let Some(w) = self.current() else { return };
        let Some((k, i)) = self.target() else { return };
        let job = &w.jobs()[k];
        let Some(jobid) = job.jobid.clone() else {
            self.flash("no Slurm job ID");
            return;
        };
        let text = match (i, job.is_array()) {
            (Some(i), true) => format!("{jobid}_{i}"),
            _ => jobid,
        };
        // OSC 52 clipboard escape, supported by most modern terminals
        let encoded = base64(text.as_bytes());
        print!("\x1b]52;c;{encoded}\x07");
        self.flash(format!("copied {text}"));
    }

    pub fn on_mouse(&mut self, event: MouseEvent) {
        let (x, y) = (event.column, event.row);
        let inside = |r: Rect| x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height;
        match event.kind {
            MouseEventKind::ScrollDown | MouseEventKind::ScrollUp => {
                let d = if matches!(event.kind, MouseEventKind::ScrollDown) {
                    1
                } else {
                    -1
                };
                if inside(self.areas.sidebar) {
                    self.focus = Focus::Workflows;
                    self.move_selection(d);
                } else if self.tab == Tab::Logs {
                    self.scroll_log(-d * 3);
                } else if self.tab == Tab::Info {
                    self.info_scroll = (self.info_scroll as i64 + d as i64 * 3).max(0) as usize;
                } else {
                    self.focus = Focus::Main;
                    self.move_row(d);
                }
            }
            MouseEventKind::Down(_) => {
                if inside(self.areas.cards) && self.areas.card_height > 0 {
                    self.focus = Focus::Workflows;
                    let k = self.areas.card_offset
                        + ((y - self.areas.cards.y) / self.areas.card_height) as usize;
                    if k < self.visible.len() {
                        self.selected = k;
                        self.row_sel = 0;
                        self.rebuild();
                        self.reload_log(true);
                    }
                } else if inside(self.areas.tabs) {
                    let rel = (x - self.areas.tabs.x) as usize;
                    let mut pos = 0;
                    for tab in Tab::ALL {
                        let w = tab.title().len() + 6;
                        if rel < pos + w {
                            self.set_tab(tab);
                            break;
                        }
                        pos += w;
                    }
                } else if inside(self.areas.table) && y > self.areas.table.y {
                    self.focus = Focus::Main;
                    let k = self.areas.table_offset + (y - self.areas.table.y - 1) as usize;
                    if k < self.rows.len() {
                        if k == self.row_sel {
                            self.toggle_expand();
                        } else {
                            self.row_sel = k;
                            self.reload_log(true);
                        }
                    }
                } else if inside(self.areas.main) {
                    self.focus = Focus::Main;
                }
            }
            _ => {}
        }
    }
}

fn widgets_fuzzy(query: &str, text: &str) -> bool {
    crate::widgets::fuzzy(query, text).is_some()
}

fn rank(cat: Cat) -> u8 {
    match cat {
        Cat::Running => 0,
        Cat::Failed => 1,
        Cat::Cancelled => 2,
        Cat::Pending => 3,
        Cat::Done => 4,
        Cat::Unknown => 5,
    }
}

/// The most interesting element of an array: running, then failed, then the last done.
pub fn interesting(w: &Workflow, job: &JobMeta) -> usize {
    let elements = w.elements(job);
    elements
        .iter()
        .enumerate()
        .min_by_key(|(k, e)| {
            (
                rank(Cat::of(e.state())),
                if Cat::of(e.state()) == Cat::Done {
                    usize::MAX - k
                } else {
                    *k
                },
            )
        })
        .map(|(k, _)| k)
        .unwrap_or(0)
}

fn base64(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in data.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = (b[0] as u32) << 16 | (b[1] as u32) << 8 | b[2] as u32;
        for k in 0..4 {
            if k <= chunk.len() {
                out.push(T[(n >> (18 - 6 * k) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_base64() {
        assert_eq!(base64(b"1234_5"), "MTIzNF81");
        assert_eq!(base64(b"12"), "MTI=");
    }
}
