//! Batched Slurm accounting queries, mirroring `dawgz/sacct.py`.

use serde_json::{json, Value};
use std::collections::HashMap;
use std::process::Command;

const FIELDS: &[&str] = &[
    "JobID",
    "State",
    "Reason",
    "Start",
    "End",
    "Elapsed",
    "ExitCode",
    "NodeList",
    "Timelimit",
];
const MINIMAL: &[&str] = &["JobID", "State"];
const CHUNK: usize = 500;

/// Queries the states of jobs with as few `sacct` calls as possible.
pub fn query(jobids: &[String]) -> Result<HashMap<String, Value>, String> {
    // Array tasks ("123_4") are fetched with their array ("123")
    let mut bases: Vec<String> = Vec::new();
    for id in jobids {
        let base = id.split('_').next().unwrap_or(id).to_string();
        if !bases.contains(&base) {
            bases.push(base);
        }
    }

    let mut entries = HashMap::new();
    for chunk in bases.chunks(CHUNK) {
        entries.extend(query_chunk(chunk)?);
    }
    Ok(entries)
}

fn query_chunk(ids: &[String]) -> Result<HashMap<String, Value>, String> {
    let mut error = String::new();
    for fields in [FIELDS, MINIMAL] {
        let out = Command::new("sacct")
            .args([
                "-X",
                "-n",
                "-P",
                "-j",
                &ids.join(","),
                "-o",
                &fields.join(","),
            ])
            .output()
            .map_err(|e| format!("sacct: {e}"))?;
        if out.status.success() {
            return Ok(parse(&String::from_utf8_lossy(&out.stdout), fields));
        }
        error = String::from_utf8_lossy(&out.stderr).trim().to_string();
    }
    Err(if error.is_empty() {
        "sacct failed".into()
    } else {
        error
    })
}

pub fn parse(text: &str, fields: &[&str]) -> HashMap<String, Value> {
    let mut entries = HashMap::new();

    for line in text.lines() {
        let values: Vec<&str> = line.split('|').collect();
        if values.len() < fields.len() {
            continue;
        }
        let row: HashMap<&str, &str> = fields.iter().copied().zip(values).collect();
        let id = row["JobID"].trim();

        if id.contains('.') || id.contains('+') {
            continue; // job steps, heterogeneous jobs
        }

        let entry = convert(&row);

        match id.split_once('_') {
            None => {
                entries.insert(id.to_string(), entry);
            }
            Some((base, task)) if task.starts_with('[') => {
                for i in expand(task.trim_start_matches('[').trim_end_matches(']')) {
                    entries.insert(format!("{base}_{i}"), entry.clone());
                }
                entries.entry(base.to_string()).or_insert(entry);
            }
            Some((base, task)) => {
                entries.insert(format!("{base}_{task}"), entry);
            }
        }
    }

    entries
}

pub fn expand(ranges: &str) -> Vec<usize> {
    let mut out = Vec::new();
    let spec = ranges.split('%').next().unwrap_or("");
    for part in spec.split(',').filter(|p| !p.is_empty()) {
        let (part, step) = match part.split_once(':') {
            Some((p, s)) => (p, s.parse().unwrap_or(1).max(1)),
            None => (part, 1),
        };
        match part.split_once('-') {
            Some((a, b)) => {
                if let (Ok(a), Ok(b)) = (a.parse::<usize>(), b.parse::<usize>()) {
                    out.extend((a..=b).step_by(step));
                }
            }
            None => {
                if let Ok(i) = part.parse() {
                    out.push(i);
                }
            }
        }
    }
    out
}

fn convert(row: &HashMap<&str, &str>) -> Value {
    let state = row
        .get("State")
        .and_then(|s| s.split_whitespace().next())
        .unwrap_or("UNKNOWN")
        .trim_end_matches('+');
    let mut entry = json!({"state": state});

    if let Some(r) = row.get("Reason").filter(|r| !r.is_empty() && **r != "None") {
        entry["reason"] = json!(r);
    }
    if let Some(t) = row.get("Start").and_then(|t| timestamp(t)) {
        entry["start"] = json!(t);
    }
    if let Some(t) = row.get("End").and_then(|t| timestamp(t)) {
        entry["end"] = json!(t);
    }
    if let Some(d) = row.get("Elapsed").and_then(|t| duration(t)) {
        entry["elapsed"] = json!(d);
    }
    if let Some(e) = row.get("ExitCode").filter(|e| !e.is_empty()) {
        entry["exit"] = json!(e);
    }
    if let Some(n) = row
        .get("NodeList")
        .filter(|n| !n.is_empty() && !n.starts_with("None"))
    {
        entry["node"] = json!(n);
    }
    if let Some(d) = row.get("Timelimit").and_then(|t| duration(t)) {
        entry["limit"] = json!(d);
    }

    entry
}

/// Parses local ISO timestamps (`2026-01-01T10:00:00`).
pub fn timestamp(text: &str) -> Option<f64> {
    let (date, time) = text.split_once('T')?;
    let d: Vec<i32> = date
        .split('-')
        .map(|x| x.parse().ok())
        .collect::<Option<_>>()?;
    let t: Vec<i32> = time
        .split(':')
        .map(|x| x.parse().ok())
        .collect::<Option<_>>()?;
    if d.len() != 3 || t.len() != 3 {
        return None;
    }
    // SAFETY: `tm` is fully initialized before use.
    unsafe {
        let mut tm: libc::tm = std::mem::zeroed();
        tm.tm_year = d[0] - 1900;
        tm.tm_mon = d[1] - 1;
        tm.tm_mday = d[2];
        tm.tm_hour = t[0];
        tm.tm_min = t[1];
        tm.tm_sec = t[2];
        tm.tm_isdst = -1;
        let ts = libc::mktime(&mut tm);
        (ts != -1).then_some(ts as f64)
    }
}

/// Parses Slurm durations (`1-02:03:04`, `02:03:04`, `03:04`).
pub fn duration(text: &str) -> Option<f64> {
    let (days, rest) = match text.split_once('-') {
        Some((d, r)) => (d.parse::<f64>().ok()?, r),
        None => (0.0, text),
    };
    let mut seconds = 0.0;
    for part in rest.split(':') {
        seconds = 60.0 * seconds + part.parse::<f64>().ok()?;
    }
    Some(seconds + 86400.0 * days)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_sacct_output() {
        let text = "100|COMPLETED|None|2026-01-01T10:00:00|2026-01-01T10:01:00|00:01:00|0:0|node1|01:00:00\n\
                    101_[3-5,8%2]|PENDING|JobArrayTaskLimit|Unknown|Unknown|00:00:00|0:0|None assigned|UNLIMITED\n\
                    101_0|RUNNING|None|2026-01-01T10:00:00|Unknown|1-00:00:01|0:0|node2|1-00:00:00\n\
                    102|CANCELLED by 1234|None|Unknown|Unknown|00:00:00|0:0|None assigned|10:00\n\
                    102.batch|CANCELLED|None|Unknown|Unknown|00:00:00|0:0|None assigned|\n";
        let e = parse(text, FIELDS);
        assert_eq!(e["100"]["state"], "COMPLETED");
        assert_eq!(e["100"]["elapsed"], 60.0);
        assert_eq!(e["100"]["limit"], 3600.0);
        assert_eq!(e["101_4"]["reason"], "JobArrayTaskLimit");
        assert!(e.contains_key("101_8"));
        assert_eq!(e["101_0"]["elapsed"], 86401.0);
        assert_eq!(e["102"]["state"], "CANCELLED");
        assert!(!e.contains_key("102.batch"));
        assert!(e["102"].get("node").is_none());
    }

    #[test]
    fn expands_ranges() {
        assert_eq!(expand("0-3"), vec![0, 1, 2, 3]);
        assert_eq!(expand("0-9:3%2"), vec![0, 3, 6, 9]);
        assert_eq!(expand("1,4-5"), vec![1, 4, 5]);
    }
}

/// A job of the Slurm queue (`squeue`).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct QueueJob {
    pub id: String,
    pub partition: String,
    pub name: String,
    pub state: String,
    pub time: String,
    pub limit: String,
    pub nodes: String,
    pub reason: String,
}

/// Lists the jobs of the current user with a single `squeue` call.
pub fn queue() -> Result<Vec<QueueJob>, String> {
    let user = std::env::var("USER").unwrap_or_default();
    let mut cmd = Command::new("squeue");
    if !user.is_empty() {
        cmd.args(["-u", &user]);
    }
    let out = cmd
        .args(["-h", "-o", "%i|%P|%j|%T|%M|%l|%D|%R"])
        .output()
        .map_err(|e| format!("squeue: {e}"))?;
    if !out.status.success() {
        return Err(String::from_utf8_lossy(&out.stderr).trim().to_string());
    }
    Ok(parse_queue(&String::from_utf8_lossy(&out.stdout)))
}

pub fn parse_queue(text: &str) -> Vec<QueueJob> {
    text.lines()
        .filter_map(|line| {
            let f: Vec<&str> = line.split('|').collect();
            (f.len() >= 8).then(|| QueueJob {
                id: f[0].trim().to_string(),
                partition: f[1].trim().to_string(),
                name: f[2].trim().to_string(),
                state: f[3].trim().to_string(),
                time: f[4].trim().to_string(),
                limit: f[5].trim().to_string(),
                nodes: f[6].trim().to_string(),
                reason: f[7..].join("|").trim().to_string(),
            })
        })
        .collect()
}

#[cfg(test)]
mod queue_tests {
    use super::*;

    #[test]
    fn parses_squeue() {
        let jobs = parse_queue("12_[3-9]|gpu|0001_train|PENDING|0:00|1:00:00|1|(Priority)\n13|cpu|job|RUNNING|1:02|UNLIMITED|2|node[1-2]\nbad line\n");
        assert_eq!(jobs.len(), 2);
        assert_eq!(jobs[0].id, "12_[3-9]");
        assert_eq!(jobs[1].reason, "node[1-2]");
    }
}
