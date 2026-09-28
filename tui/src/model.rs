//! Records written by dawgz (see `docs/format.md`).

use serde::Deserialize;
use std::collections::HashMap;

#[derive(Deserialize, Clone, Debug, Default)]
#[serde(default)]
pub struct Meta {
    pub format: u32,
    pub uid: String,
    pub name: String,
    pub backend: String,
    pub date: String,
    pub timestamp: f64,
    pub cwd: String,
    pub argv: Vec<String>,
    pub host: String,
    pub user: String,
    pub pid: Option<i64>,
    pub pid_start: Option<u64>,
    pub snapshot: Option<serde_json::Value>,
    pub jobs: Vec<JobMeta>,
    pub sources: Vec<String>,
}

#[derive(Deserialize, Clone, Debug, Default)]
#[serde(default)]
pub struct Pruned {
    pub satisfied: usize,
    pub unsatisfied: usize,
}

#[derive(Deserialize, Clone, Debug, Default)]
#[serde(default)]
pub struct JobMeta {
    pub index: usize,
    pub tag: String,
    pub name: String,
    pub input: String,
    pub array: Option<usize>,
    pub throttle: Option<usize>,
    pub deps: Vec<(usize, String)>,
    pub wait: String,
    pub pruned: Pruned,
    pub jobid: Option<String>,
    pub settings: serde_json::Map<String, serde_json::Value>,
    pub source: Option<usize>,
    pub inputs: Option<Vec<String>>,
    pub script: Option<String>,
    pub stdout: Option<String>,
}

impl JobMeta {
    pub fn is_array(&self) -> bool {
        self.array.is_some()
    }

    pub fn label(&self) -> String {
        match self.array {
            Some(n) => format!("{}[0-{}]", self.name, n.saturating_sub(1)),
            None => self.name.clone(),
        }
    }
}

#[derive(Deserialize, Clone, Debug, Default, PartialEq)]
#[serde(default)]
pub struct Bar {
    pub desc: String,
    pub n: f64,
    pub total: Option<f64>,
    pub unit: Option<String>,
    pub rate: Option<f64>,
    pub eta: Option<f64>,
    pub postfix: Option<String>,
    pub t: Option<f64>,
}

impl Bar {
    pub fn fraction(&self) -> Option<f64> {
        match self.total {
            Some(total) if total > 0.0 => Some((self.n / total).clamp(0.0, 1.0)),
            _ => None,
        }
    }
}

/// A state entry, either cached by the scheduler (`state.json`) or reported by the job
/// itself (`*.run.json`).
#[derive(Deserialize, Clone, Debug, Default)]
#[serde(default)]
pub struct Entry {
    pub state: Option<String>,
    pub reason: Option<String>,
    pub start: Option<f64>,
    pub end: Option<f64>,
    pub elapsed: Option<f64>,
    pub exit: Option<String>,
    pub node: Option<String>,
    pub host: Option<String>,
    pub limit: Option<f64>,
    pub error: Option<String>,
    pub trace: Option<String>,
    pub status: Option<String>,
    pub jobid: Option<String>,
    pub updated: Option<f64>,
    pub progress: Vec<Bar>,
    pub tasks: HashMap<String, Entry>,
}

impl Entry {
    pub fn state(&self) -> &str {
        self.state.as_deref().unwrap_or("PENDING")
    }

    /// The most relevant progress bar: the first unfinished bar with a total, or the
    /// most recent one.
    pub fn main_bar(&self) -> Option<&Bar> {
        self.progress
            .iter()
            .find(|b| matches!(b.total, Some(t) if t > 0.0 && b.n < t))
            .or_else(|| {
                self.progress
                    .iter()
                    .max_by(|a, b| a.t.unwrap_or(0.0).total_cmp(&b.t.unwrap_or(0.0)))
            })
    }

    pub fn fraction(&self) -> Option<f64> {
        self.main_bar().and_then(Bar::fraction)
    }

    pub fn node(&self) -> Option<&str> {
        self.node.as_deref().or(self.host.as_deref())
    }
}

#[derive(Deserialize, Clone, Debug, Default)]
#[serde(default)]
pub struct Cache {
    pub format: u32,
    pub updated: f64,
    pub source: String,
    pub finished: bool,
    pub jobs: HashMap<String, Entry>,
}

/// State categories.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Cat {
    Running,
    Failed,
    Pending,
    Cancelled,
    Done,
    Unknown,
}

impl Cat {
    pub const ALL: [Cat; 6] = [
        Cat::Done,
        Cat::Running,
        Cat::Failed,
        Cat::Cancelled,
        Cat::Pending,
        Cat::Unknown,
    ];

    pub fn of(state: &str) -> Cat {
        match state {
            "COMPLETED" => Cat::Done,
            "RUNNING" | "COMPLETING" | "CONFIGURING" | "STAGE_OUT" | "SIGNALING" | "RESIZING" => {
                Cat::Running
            }
            "PENDING" | "REQUEUED" | "REQUEUE_FED" | "REQUEUE_HOLD" | "RESV_DEL_HOLD"
            | "SUSPENDED" | "STOPPED" | "SUBMITTING" => Cat::Pending,
            "FAILED" | "TIMEOUT" | "OUT_OF_MEMORY" | "NODE_FAIL" | "BOOT_FAIL" | "DEADLINE"
            | "SPECIAL_EXIT" => Cat::Failed,
            "CANCELLED" | "PREEMPTED" | "REVOKED" => Cat::Cancelled,
            _ => Cat::Unknown,
        }
    }

    pub fn terminal(self) -> bool {
        matches!(self, Cat::Done | Cat::Failed | Cat::Cancelled)
    }

    pub fn glyph(self) -> &'static str {
        match self {
            Cat::Done => "✔",
            Cat::Running => "●",
            Cat::Pending => "◌",
            Cat::Failed => "✘",
            Cat::Cancelled => "⊘",
            Cat::Unknown => "?",
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Cat::Done => "done",
            Cat::Running => "running",
            Cat::Pending => "pending",
            Cat::Failed => "failed",
            Cat::Cancelled => "cancelled",
            Cat::Unknown => "unknown",
        }
    }
}

pub fn is_terminal(state: &str) -> bool {
    Cat::of(state).terminal()
}

/// Counts of element states.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Counts {
    pub done: usize,
    pub running: usize,
    pub pending: usize,
    pub failed: usize,
    pub cancelled: usize,
    pub unknown: usize,
}

impl Counts {
    pub fn add(&mut self, cat: Cat) {
        *self.get_mut(cat) += 1;
    }

    pub fn get(&self, cat: Cat) -> usize {
        match cat {
            Cat::Done => self.done,
            Cat::Running => self.running,
            Cat::Pending => self.pending,
            Cat::Failed => self.failed,
            Cat::Cancelled => self.cancelled,
            Cat::Unknown => self.unknown,
        }
    }

    fn get_mut(&mut self, cat: Cat) -> &mut usize {
        match cat {
            Cat::Done => &mut self.done,
            Cat::Running => &mut self.running,
            Cat::Pending => &mut self.pending,
            Cat::Failed => &mut self.failed,
            Cat::Cancelled => &mut self.cancelled,
            Cat::Unknown => &mut self.unknown,
        }
    }

    pub fn merge(&mut self, other: &Counts) {
        for cat in Cat::ALL {
            *self.get_mut(cat) += other.get(cat);
        }
    }

    pub fn total(&self) -> usize {
        Cat::ALL.iter().map(|&c| self.get(c)).sum()
    }

    pub fn finished(&self) -> usize {
        self.done + self.failed + self.cancelled
    }

    pub fn active(&self) -> bool {
        self.running + self.pending > 0
    }

    /// The aggregated state of a group of elements.
    pub fn state(&self) -> &'static str {
        if self.failed > 0 {
            if self.running + self.pending == 0 {
                "FAILED"
            } else {
                "RUNNING"
            }
        } else if self.running > 0 {
            "RUNNING"
        } else if self.pending > 0 {
            "PENDING"
        } else if self.cancelled > 0 {
            "CANCELLED"
        } else if self.done > 0 {
            "COMPLETED"
        } else {
            "UNKNOWN"
        }
    }
}
