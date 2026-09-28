//! Reading (and caching) workflow records, mirroring `dawgz/store.py`.

use crate::model::{is_terminal, Cache, Cat, Counts, Entry, JobMeta, Meta};
use std::collections::HashMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn hostname() -> String {
    let mut buf = [0u8; 256];
    // SAFETY: the buffer is valid for its length.
    let ok = unsafe { libc::gethostname(buf.as_mut_ptr() as *mut libc::c_char, buf.len()) } == 0;
    if !ok {
        return String::new();
    }
    let end = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
    String::from_utf8_lossy(&buf[..end]).into_owned()
}

pub fn pid_alive(pid: i64) -> bool {
    // SAFETY: signal 0 only checks for the existence of the process.
    let r = unsafe { libc::kill(pid as libc::pid_t, 0) };
    r == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

fn mtime(path: &Path) -> Option<SystemTime> {
    fs::metadata(path).and_then(|m| m.modified()).ok()
}

fn read<T: serde::de::DeserializeOwned>(path: &Path) -> Option<T> {
    let data = fs::read(path).ok()?;
    serde_json::from_slice(&data).ok()
}

/// Atomically writes a JSON value.
pub fn write_json(path: &Path, value: &serde_json::Value) -> std::io::Result<()> {
    let tmp = path.with_extension(format!("json.{}.tmp", std::process::id()));
    {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(serde_json::to_string(value)?.as_bytes())?;
    }
    fs::rename(tmp, path)
}

/// Advisory lock on a file, released on drop.
pub struct Lock(Option<fs::File>);

impl Lock {
    pub fn acquire(path: &Path) -> Lock {
        use std::os::unix::io::AsRawFd;
        match fs::OpenOptions::new().create(true).append(true).open(path) {
            Ok(f) => {
                // SAFETY: valid file descriptor.
                unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX) };
                Lock(Some(f))
            }
            Err(_) => Lock(None),
        }
    }
}

impl Drop for Lock {
    fn drop(&mut self) {
        use std::os::unix::io::AsRawFd;
        if let Some(f) = &self.0 {
            // SAFETY: valid file descriptor.
            unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_UN) };
        }
    }
}

// Registry

#[derive(Clone, Debug, Default)]
#[allow(dead_code)]
pub struct Row {
    pub name: String,
    pub uid: String,
    pub date: String,
    pub backend: String,
    pub jobs: String,
}

fn parse_csv_line(line: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut field = String::new();
    let mut quoted = false;
    let mut chars = line.chars().peekable();

    while let Some(c) = chars.next() {
        match c {
            '"' if quoted && chars.peek() == Some(&'"') => {
                field.push('"');
                chars.next();
            }
            '"' => quoted = !quoted,
            ',' if !quoted => out.push(std::mem::take(&mut field)),
            _ => field.push(c),
        }
    }
    out.push(field);
    out
}

pub fn registry(dir: &Path) -> Vec<Row> {
    let Ok(text) = fs::read_to_string(dir.join("workflows.csv")) else {
        return Vec::new();
    };

    text.lines()
        .map(|l| l.trim_end_matches('\r'))
        .filter(|l| !l.is_empty())
        .map(parse_csv_line)
        .filter(|f| f.len() >= 2)
        .map(|f| Row {
            name: f[0].clone(),
            uid: f[1].clone(),
            date: f.get(2).cloned().unwrap_or_default(),
            backend: f.get(3).cloned().unwrap_or_default(),
            jobs: f.get(4).cloned().unwrap_or_default(),
        })
        .collect()
}

pub fn known_dirs() -> Vec<PathBuf> {
    let base = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/state")));
    let Some(base) = base else { return Vec::new() };
    fs::read_to_string(base.join("dawgz").join("dirs"))
        .map(|t| {
            t.lines()
                .filter(|l| !l.is_empty())
                .map(PathBuf::from)
                .collect()
        })
        .unwrap_or_default()
}

// Workflows

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Readiness {
    Ready,
    Wait,
    Never,
}

#[derive(Clone, Debug, Default)]
pub struct Summary {
    pub state: String,
    pub counts: Counts,
    pub fraction: f64,
    pub total: usize,
}

impl Summary {
    pub fn cat(&self) -> Cat {
        Cat::of(&self.state)
    }
}

pub struct Workflow {
    pub dir: PathBuf,
    pub path: PathBuf,
    pub row: Row,
    pub meta: Meta,
    pub cache: Cache,
    pub runs: HashMap<String, Entry>,
    run_mtimes: HashMap<String, SystemTime>,
    dir_mtime: Option<SystemTime>,
    cache_mtime: Option<SystemTime>,
    pub inferred: HashMap<usize, Readiness>,
    pub summaries: Vec<Summary>,
    pub totals: Counts,
    pub fraction: f64,
    pub loaded: f64,
}

impl Workflow {
    pub fn open(dir: &Path, row: Row) -> Option<Workflow> {
        let path = dir.join(&row.uid);
        let meta: Meta = read(&path.join("workflow.json"))?;
        let mut w = Workflow {
            dir: dir.to_path_buf(),
            path,
            row,
            meta,
            cache: Cache::default(),
            runs: HashMap::new(),
            run_mtimes: HashMap::new(),
            dir_mtime: None,
            cache_mtime: None,
            inferred: HashMap::new(),
            summaries: Vec::new(),
            totals: Counts::default(),
            fraction: 0.0,
            loaded: 0.0,
        };
        w.reload(true);
        Some(w)
    }

    pub fn uid(&self) -> &str {
        if self.meta.uid.is_empty() {
            &self.row.uid
        } else {
            &self.meta.uid
        }
    }

    pub fn name(&self) -> &str {
        if self.meta.name.is_empty() {
            &self.row.name
        } else {
            &self.meta.name
        }
    }

    pub fn jobs(&self) -> &[JobMeta] {
        &self.meta.jobs
    }

    pub fn is_slurm(&self) -> bool {
        self.meta.backend == "slurm"
    }

    pub fn active(&self) -> bool {
        self.totals.active()
    }

    /// Reloads the files that changed since the last call. Returns whether anything changed.
    pub fn reload(&mut self, force: bool) -> bool {
        let mut changed = false;

        let cache_mtime = mtime(&self.path.join("state.json"));
        if force || cache_mtime != self.cache_mtime {
            self.cache = read(&self.path.join("state.json")).unwrap_or_default();
            self.cache_mtime = cache_mtime;
            changed = true;
        }

        // Atomic writes (renames) update the directory mtime
        let dir_mtime = mtime(&self.path);
        if force || dir_mtime != self.dir_mtime {
            self.dir_mtime = dir_mtime;
            if let Ok(entries) = fs::read_dir(&self.path) {
                for entry in entries.flatten() {
                    let name = entry.file_name();
                    let name = name.to_string_lossy();
                    let Some(key) = name.strip_suffix(".run.json") else {
                        continue;
                    };
                    let modified = entry.metadata().and_then(|m| m.modified()).ok();
                    if modified.is_some() && self.run_mtimes.get(key) == modified.as_ref() {
                        continue;
                    }
                    if let Some(run) = read::<Entry>(&entry.path()) {
                        self.runs.insert(key.to_string(), run);
                        if let Some(m) = modified {
                            self.run_mtimes.insert(key.to_string(), m);
                        }
                        changed = true;
                    }
                }
            }
        }

        if changed {
            self.check_alive();
            self.infer();
            self.summarize();
        }

        self.loaded = now();
        changed
    }

    fn check_alive(&mut self) {
        if !matches!(self.meta.backend.as_str(), "local" | "async" | "dummy") || self.cache.finished
        {
            return;
        }
        let Some(pid) = self.meta.pid else { return };
        if self.meta.host != hostname() || pid_alive(pid) {
            return;
        }
        fn mark(e: &mut Entry) {
            if !is_terminal(e.state()) {
                e.state = Some("CANCELLED".into());
                e.reason = Some("scheduler exited".into());
            }
        }
        for entry in self.cache.jobs.values_mut() {
            mark(entry);
            for task in entry.tasks.values_mut() {
                mark(task);
            }
        }
        self.cache.finished = true;
    }

    /// Merged state entry of a job or array element.
    pub fn entry(&self, job: &JobMeta, i: Option<usize>) -> Entry {
        let empty = Entry::default();
        let parent = self
            .cache
            .jobs
            .get(&job.index.to_string())
            .unwrap_or(&empty);

        let cached = match i {
            None => parent.clone(),
            Some(i) => match parent.tasks.get(&i.to_string()) {
                Some(t) => t.clone(),
                None if parent.state.as_deref().map(is_terminal).unwrap_or(false) => Entry {
                    state: parent.state.clone(),
                    reason: parent.reason.clone(),
                    ..Entry::default()
                },
                None => Entry::default(),
            },
        };

        let key = match i {
            None => job.tag.clone(),
            Some(i) => format!("{}_{}", job.tag, i),
        };
        let run = self.runs.get(&key);

        let mut merged = cached.clone();
        let terminal = cached.state.as_deref().map(is_terminal).unwrap_or(false);

        if let Some(run) = run {
            let rs = run.state();
            if !terminal && (is_terminal(rs) || rs == "RUNNING") {
                // Cached times are older than the job's own report
                merged.elapsed = None;
                merged.reason = None;
                merged.state = run.state.clone();
                merged.start = run.start.or(merged.start);
                if run.end.is_some() {
                    merged.end = run.end;
                }
                merged.host = run.host.clone().or(merged.host);
                if run.error.is_some() {
                    merged.error = run.error.clone();
                }
                merged.trace = run.trace.clone();
                merged.jobid = run.jobid.clone().or(merged.jobid);
            }
            if !run.progress.is_empty() {
                merged.progress = run.progress.clone();
            }
            if run.status.is_some() {
                merged.status = run.status.clone();
            }
        }

        if merged.state.is_none() {
            merged.state = Some("PENDING".into());
        }

        if merged.state() == "PENDING" {
            match self.inferred.get(&job.index) {
                Some(Readiness::Never) => {
                    if kills_invalid(job) {
                        merged.state = Some("CANCELLED".into());
                    }
                    merged.reason = Some("DependencyNeverSatisfied".into());
                }
                Some(Readiness::Wait) => merged.reason = Some("Dependency".into()),
                _ => {}
            }
        }

        merged
    }

    pub fn elements(&self, job: &JobMeta) -> Vec<Entry> {
        match job.array {
            Some(n) => (0..n).map(|i| self.entry(job, Some(i))).collect(),
            None => vec![self.entry(job, None)],
        }
    }

    fn compute_summary(&self, job: &JobMeta) -> Summary {
        let elements = self.elements(job);
        let mut counts = Counts::default();
        let mut fraction = 0.0;

        for e in &elements {
            let cat = Cat::of(e.state());
            counts.add(cat);
            if cat.terminal() {
                fraction += 1.0;
            } else if cat == Cat::Running {
                fraction += e.fraction().unwrap_or(0.0);
            }
        }

        let state = if elements.len() == 1 {
            elements[0].state().to_string()
        } else {
            counts.state().to_string()
        };

        Summary {
            state,
            counts,
            fraction: fraction / elements.len().max(1) as f64,
            total: elements.len(),
        }
    }

    fn infer(&mut self) {
        self.inferred.clear();

        if !self.is_slurm() {
            return;
        }

        let mut outcomes: HashMap<usize, Option<&'static str>> = HashMap::new();
        let n = self.meta.jobs.len();

        for _ in 0..=n {
            let mut changed = false;
            for k in 0..n {
                let job = &self.meta.jobs[k];
                let status = readiness(job, &outcomes);
                if self.inferred.get(&job.index) != Some(&status) {
                    self.inferred.insert(job.index, status);
                    changed = true;
                }
                let job = &self.meta.jobs[k];
                let summary = self.compute_summary(job);
                let outcome = if summary.counts.finished() == summary.total {
                    match summary.cat() {
                        Cat::Done => Some("success"),
                        Cat::Failed => Some("failure"),
                        Cat::Cancelled => Some("cancelled"),
                        _ => None,
                    }
                } else {
                    None
                };
                if outcomes.get(&job.index).copied().flatten() != outcome {
                    outcomes.insert(job.index, outcome);
                    changed = true;
                }
            }
            if !changed {
                break;
            }
        }
    }

    fn summarize(&mut self) {
        self.summaries = self
            .meta
            .jobs
            .iter()
            .map(|j| self.compute_summary(j))
            .collect();
        let mut totals = Counts::default();
        let mut done = 0.0;
        for s in &self.summaries {
            totals.merge(&s.counts);
            done += s.fraction * s.total as f64;
        }
        self.fraction = done / totals.total().max(1) as f64;
        self.totals = totals;
    }

    pub fn summary(&self, index: usize) -> &Summary {
        &self.summaries[index]
    }

    /// Slurm job IDs whose state should be refreshed (see `dawgz.store.Workflow.stale`).
    pub fn stale(&self, ttl: f64) -> Vec<String> {
        if !self.is_slurm() || now() - self.cache.updated < ttl {
            return Vec::new();
        }
        self.meta
            .jobs
            .iter()
            .filter(|j| j.jobid.is_some())
            .filter(|j| {
                !matches!(
                    self.inferred.get(&j.index),
                    Some(Readiness::Wait) | Some(Readiness::Never)
                )
            })
            .filter(|j| {
                let s = &self.summaries[j.index];
                s.counts.finished() < s.total
            })
            .filter_map(|j| j.jobid.clone())
            .collect()
    }

    /// Merges fresh Slurm entries (keyed by job ID) into `state.json`.
    pub fn update(&mut self, entries: &HashMap<String, serde_json::Value>, when: f64) {
        let _lock = Lock::acquire(&self.path.join("state.lock"));
        let file = self.path.join("state.json");
        let mut cache: serde_json::Value =
            read(&file).unwrap_or_else(|| serde_json::json!({"format": 1, "jobs": {}}));

        if !cache.get("jobs").map(|j| j.is_object()).unwrap_or(false) {
            cache["jobs"] = serde_json::json!({});
        }

        for job in &self.meta.jobs {
            let Some(jobid) = &job.jobid else { continue };
            let key = job.index.to_string();
            let jobs = cache["jobs"].as_object_mut().unwrap();
            let old = jobs.remove(&key).unwrap_or_else(|| serde_json::json!({}));
            let mut new = old.clone();

            if let Some(n) = job.array {
                let mut tasks = old
                    .get("tasks")
                    .cloned()
                    .unwrap_or_else(|| serde_json::json!({}));
                for i in 0..n {
                    if let Some(e) = entries.get(&format!("{jobid}_{i}")) {
                        tasks[i.to_string()] = e.clone();
                    }
                }
                if let Some(parent) = entries.get(jobid) {
                    merge(&mut new, parent);
                }
                new["tasks"] = tasks;
            } else if let Some(e) = entries.get(jobid) {
                merge(&mut new, e);
            }

            jobs.insert(key, new);
        }

        cache["updated"] = serde_json::json!(when);
        cache["source"] = serde_json::json!("sacct");
        let _ = write_json(&file, &cache);
        drop(_lock);

        self.reload(true);
    }

    /// Cancels the workflow, a job or an array element. Returns a message.
    pub fn cancel(&mut self, index: Option<usize>, i: Option<usize>) -> String {
        if self.is_slurm() {
            let mut ids = Vec::new();
            for job in &self.meta.jobs {
                if index.is_some_and(|k| k != job.index) {
                    continue;
                }
                let Some(jobid) = &job.jobid else { continue };
                match (i, job.array) {
                    (Some(i), Some(n)) => {
                        if !is_terminal(self.entry(job, Some(i % n)).state()) {
                            ids.push(format!("{jobid}_{}", i % n));
                        }
                    }
                    _ => {
                        let s = &self.summaries[job.index];
                        if s.counts.finished() < s.total {
                            ids.push(jobid.clone());
                        }
                    }
                }
            }
            if ids.is_empty() {
                return "nothing to cancel".into();
            }
            let out = std::process::Command::new("scancel")
                .arg("-v")
                .args(&ids)
                .output();
            {
                let _lock = Lock::acquire(&self.path.join("state.lock"));
                let file = self.path.join("state.json");
                let mut cache: serde_json::Value =
                    read(&file).unwrap_or_else(|| serde_json::json!({"format": 1, "jobs": {}}));
                cache["updated"] = serde_json::json!(0);
                let _ = write_json(&file, &cache);
            }
            self.reload(true);
            match out {
                Ok(o) => {
                    let text = String::from_utf8_lossy(if o.stderr.is_empty() {
                        &o.stdout
                    } else {
                        &o.stderr
                    })
                    .trim()
                    .to_string();
                    if text.is_empty() {
                        format!("cancelled {} job(s)", ids.len())
                    } else {
                        text.lines().last().unwrap_or_default().to_string()
                    }
                }
                Err(e) => format!("scancel failed: {e}"),
            }
        } else {
            if index.is_some() {
                return format!(
                    "cancelling single jobs is not supported by the '{}' backend",
                    self.meta.backend
                );
            }
            let Some(pid) = self.meta.pid else {
                return "nothing to cancel".into();
            };
            if self.cache.finished || !pid_alive(pid) {
                return "nothing to cancel".into();
            }
            if self.meta.host != hostname() {
                return format!("the workflow runs on '{}'", self.meta.host);
            }
            // SAFETY: sending a signal to a known process.
            unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
            format!("sent SIGTERM to scheduler process {pid}")
        }
    }

    pub fn log_path(&self, job: &JobMeta, i: Option<usize>) -> PathBuf {
        let path = match i {
            Some(i) => self.path.join(format!("{}_{}.log", job.tag, i)),
            None => self.path.join(format!("{}.log", job.tag)),
        };
        // Packed jobs that failed before the dawgz runtime started
        match &job.stdout {
            Some(out) if !path.exists() && self.path.join(out).exists() => self.path.join(out),
            _ => path,
        }
    }

    pub fn script_path(&self, job: &JobMeta) -> PathBuf {
        match &job.script {
            Some(s) => self.path.join(s),
            None => self.path.join(format!("{}.sh", job.tag)),
        }
    }
}

fn merge(dst: &mut serde_json::Value, src: &serde_json::Value) {
    if let (Some(d), Some(s)) = (dst.as_object_mut(), src.as_object()) {
        for (k, v) in s {
            d.insert(k.clone(), v.clone());
        }
    }
}

pub fn readiness(job: &JobMeta, outcomes: &HashMap<usize, Option<&'static str>>) -> Readiness {
    let results: Vec<Option<bool>> = job
        .deps
        .iter()
        .map(|(dep, status)| match outcomes.get(dep).copied().flatten() {
            None => None,
            Some("cancelled") => Some(false),
            Some(outcome) => Some(status == "any" || status == outcome),
        })
        .collect();

    if job.wait != "any" {
        if job.pruned.unsatisfied > 0 || results.contains(&Some(false)) {
            return Readiness::Never;
        } else if results.iter().all(|r| *r == Some(true)) {
            return Readiness::Ready;
        }
    } else if job.pruned.satisfied > 0
        || results.contains(&Some(true))
        || (results.is_empty() && job.pruned.unsatisfied == 0)
    {
        return Readiness::Ready;
    } else if !results.contains(&None) {
        return Readiness::Never;
    }

    Readiness::Wait
}

fn kills_invalid(job: &JobMeta) -> bool {
    let value = job
        .settings
        .get("kill_on_invalid_dep")
        .or_else(|| job.settings.get("kill-on-invalid-dep"));
    !matches!(
        value,
        Some(serde_json::Value::Bool(false)) | Some(serde_json::Value::Null)
    ) && value.and_then(|v| v.as_str()) != Some("no")
}
