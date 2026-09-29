//! Graph layouts: fan-out groups, `git log --graph` lanes and a layered DAG.

use crate::model::JobMeta;
use std::collections::HashMap;

/// Groups consecutive sibling jobs (same name, dependencies and dependents), such that
/// fan-outs like `[task(i) for i in range(50)]` appear as a single node.
pub fn groups(jobs: &[JobMeta], minimum: usize) -> Vec<Vec<usize>> {
    let mut children: HashMap<usize, Vec<usize>> = HashMap::new();
    for job in jobs {
        for (dep, _) in &job.deps {
            children.entry(*dep).or_default().push(job.index);
        }
    }

    let key = |job: &JobMeta| {
        let mut deps = job.deps.clone();
        deps.sort();
        let mut kids = children.get(&job.index).cloned().unwrap_or_default();
        kids.sort();
        (job.name.clone(), job.is_array(), deps, kids)
    };

    let mut out: Vec<Vec<usize>> = Vec::new();
    let mut last_key = None;

    for (k, job) in jobs.iter().enumerate() {
        let current = key(job);
        if !job.is_array() && last_key.as_ref() == Some(&current) {
            out.last_mut().unwrap().push(k);
        } else {
            out.push(vec![k]);
        }
        last_key = Some(current);
    }

    let mut result = Vec::new();
    for group in out {
        if group.len() >= minimum {
            result.push(group);
        } else {
            result.extend(group.into_iter().map(|k| vec![k]));
        }
    }
    result
}

/// Node-level parents of groups.
pub fn group_parents(jobs: &[JobMeta], groups: &[Vec<usize>]) -> Vec<Vec<usize>> {
    let mut node_of = HashMap::new();
    for (g, group) in groups.iter().enumerate() {
        for &k in group {
            node_of.insert(jobs[k].index, g);
        }
    }
    groups
        .iter()
        .map(|group| {
            let mut parents: Vec<usize> = group
                .iter()
                .flat_map(|&k| {
                    jobs[k]
                        .deps
                        .iter()
                        .filter_map(|(d, _)| node_of.get(d).copied())
                })
                .collect();
            parents.sort();
            parents.dedup();
            parents
        })
        .collect()
}

/// Lays out a DAG as `git log --graph` style lanes, one row per node (topologically
/// ordered). Each row is a list of 2-character cells; the node is marked with `@`.
pub fn lanes(parents: &[Vec<usize>]) -> Vec<Vec<[char; 2]>> {
    let n = parents.len();
    let mut children: Vec<Vec<usize>> = vec![Vec::new(); n];
    for (v, ps) in parents.iter().enumerate() {
        for &p in ps {
            if p < n {
                children[p].push(v);
            }
        }
    }

    let mut active: Vec<Option<usize>> = Vec::new();
    let mut rows = Vec::with_capacity(n);

    for v in 0..n {
        let cols: Vec<usize> = (0..active.len())
            .filter(|&c| active[c] == Some(v))
            .collect();
        let col = match cols.first() {
            Some(&c) => c,
            None => match active.iter().position(|t| t.is_none()) {
                Some(c) => c,
                None => {
                    active.push(None);
                    active.len() - 1
                }
            },
        };

        let merges: Vec<usize> = cols.iter().skip(1).copied().collect();
        let free: Vec<usize> = (0..active.len())
            .filter(|&c| active[c].is_none() && c != col)
            .collect();
        let kids = &children[v];
        let mut splits = Vec::new();

        for _ in kids.iter().skip(1) {
            match free.iter().find(|&&c| c > col && !splits.contains(&c)) {
                Some(&c) => splits.push(c),
                None => {
                    active.push(None);
                    splits.push(active.len() - 1);
                }
            }
        }

        let right = merges
            .iter()
            .chain(splits.iter())
            .copied()
            .max()
            .unwrap_or(col)
            .max(col);
        let mut cells = Vec::with_capacity(active.len());

        for c in 0..active.len() {
            let glyph = if c == col {
                '@'
            } else if merges.contains(&c) {
                '╯'
            } else if splits.contains(&c) {
                '╮'
            } else if active[c].is_some() {
                if col < c && c < right {
                    '┼'
                } else {
                    '│'
                }
            } else if col < c && c < right {
                '─'
            } else {
                ' '
            };
            let connector = if col <= c && c < right { '─' } else { ' ' };
            cells.push([glyph, connector]);
        }

        for &c in &merges {
            active[c] = None;
        }
        active[col] = kids.first().copied();
        for (&c, &k) in splits.iter().zip(kids.iter().skip(1)) {
            active[c] = Some(k);
        }
        while active.last() == Some(&None) {
            active.pop();
        }

        rows.push(cells);
    }

    rows
}

// Layered DAG (left to right)

pub const NODE_W: u16 = 26;
pub const NODE_H: u16 = 4;
pub const GAP: u16 = 8;

#[derive(Clone, Debug)]
pub struct Placed {
    pub x: u32,
    pub y: u32,
    pub layer: usize,
}

pub const UP: u8 = 1;
pub const DOWN: u8 = 2;
pub const LEFT: u8 = 4;
pub const RIGHT: u8 = 8;

#[derive(Clone, Debug, Default)]
pub struct Dag {
    /// Positions of the (real) nodes.
    pub nodes: Vec<Placed>,
    /// Line cells: (x, y) -> (direction bits, edges passing through the cell).
    pub lines: HashMap<(u32, u32), (u8, Vec<(usize, usize)>)>,
    /// Arrow heads, in front of children.
    pub arrows: Vec<(u32, u32, (usize, usize))>,
    pub width: u32,
    pub height: u32,
    pub layers: Vec<Vec<usize>>,
    pub node_w: u16,
}

impl Dag {
    /// Number of layers of the layout of a DAG.
    pub fn depth(parents: &[Vec<usize>]) -> usize {
        let mut layer = vec![0usize; parents.len()];
        for v in 0..parents.len() {
            for &p in &parents[v] {
                if p < v {
                    layer[v] = layer[v].max(layer[p] + 1);
                }
            }
        }
        layer.into_iter().max().map(|d| d + 1).unwrap_or(0)
    }

    pub fn layout_with(parents: &[Vec<usize>], node_w16: u16) -> Dag {
        let node_w = node_w16 as u32;
        let (node_h, gap) = (NODE_H as u32, GAP as u32);
        let n = parents.len();
        if n == 0 {
            return Dag::default();
        }

        // Longest-path layering
        let mut layer = vec![0usize; n];
        for v in 0..n {
            for &p in &parents[v] {
                if p < v {
                    layer[v] = layer[v].max(layer[p] + 1);
                }
            }
        }
        let depth = layer.iter().copied().max().unwrap_or(0) + 1;

        // Virtual nodes: real nodes, then dummies for long edges
        struct V {
            real: Option<usize>,
            layer: usize,
            preds: Vec<usize>,
            edge: Option<(usize, usize)>,
        }
        let mut vs: Vec<V> = (0..n)
            .map(|v| V {
                real: Some(v),
                layer: layer[v],
                preds: Vec::new(),
                edge: None,
            })
            .collect();

        for v in 0..n {
            for &p in &parents[v] {
                if p >= v {
                    continue;
                }
                let mut prev = p;
                for l in layer[p] + 1..layer[v] {
                    vs.push(V {
                        real: None,
                        layer: l,
                        preds: vec![prev],
                        edge: Some((p, v)),
                    });
                    prev = vs.len() - 1;
                }
                vs[v].preds.push(prev);
            }
        }

        let mut layers: Vec<Vec<usize>> = vec![Vec::new(); depth];
        for (k, v) in vs.iter().enumerate() {
            layers[v.layer].push(k);
        }

        let mut succs: Vec<Vec<usize>> = vec![Vec::new(); vs.len()];
        for (k, v) in vs.iter().enumerate() {
            for &p in &v.preds {
                succs[p].push(k);
            }
        }

        // Barycenter ordering
        let mut pos = vec![0.0f64; vs.len()];
        let refresh = |layers: &Vec<Vec<usize>>, pos: &mut Vec<f64>| {
            for l in layers {
                for (i, &k) in l.iter().enumerate() {
                    pos[k] = i as f64;
                }
            }
        };
        refresh(&layers, &mut pos);

        for sweep in 0..6 {
            let down = sweep % 2 == 0;
            let order: Vec<usize> = if down {
                (1..depth).collect()
            } else {
                (0..depth.saturating_sub(1)).rev().collect()
            };
            for l in order {
                let mut keyed: Vec<(f64, usize)> = layers[l]
                    .iter()
                    .map(|&k| {
                        let neigh = if down { &vs[k].preds } else { &succs[k] };
                        let key = if neigh.is_empty() {
                            pos[k]
                        } else {
                            neigh.iter().map(|&q| pos[q]).sum::<f64>() / neigh.len() as f64
                        };
                        (key, k)
                    })
                    .collect();
                keyed.sort_by(|a, b| a.0.total_cmp(&b.0));
                layers[l] = keyed.into_iter().map(|(_, k)| k).collect();
                for (i, &k) in layers[l].iter().enumerate() {
                    pos[k] = i as f64;
                }
            }
        }

        // Coordinates
        let height_of = |k: usize| if vs[k].real.is_some() { node_h } else { 1 };
        let center = |k: usize, y: u32| if vs[k].real.is_some() { y + 1 } else { y };
        let mut ys = vec![0u32; vs.len()];

        for l in 0..depth {
            let mut bottom: i32 = -1;
            for &k in &layers[l] {
                let preds = &vs[k].preds;
                let desired = if preds.is_empty() {
                    0
                } else {
                    let c: f64 = preds.iter().map(|&p| center(p, ys[p]) as f64).sum::<f64>()
                        / preds.len() as f64;
                    let offset = if vs[k].real.is_some() { 1.0 } else { 0.0 };
                    (c - offset).round().max(0.0) as i32
                };
                let y = desired.max(bottom + 1) as u32;
                ys[k] = y;
                bottom = (y + height_of(k)) as i32;
            }
        }

        let x_of = |l: usize| l as u32 * (node_w + gap);
        let mut dag = Dag {
            nodes: vec![
                Placed {
                    x: 0,
                    y: 0,
                    layer: 0
                };
                n
            ],
            layers: vec![Vec::new(); depth],
            node_w: node_w16,
            ..Dag::default()
        };

        for (k, v) in vs.iter().enumerate() {
            if let Some(r) = v.real {
                dag.nodes[r] = Placed {
                    x: x_of(v.layer),
                    y: ys[k],
                    layer: v.layer,
                };
            }
        }
        for l in 0..depth {
            dag.layers[l] = layers[l].iter().filter_map(|&k| vs[k].real).collect();
        }

        // Edges between adjacent layers, with one bus column per parent
        for l in 0..depth.saturating_sub(1) {
            let parents_in_layer: Vec<usize> = layers[l]
                .iter()
                .copied()
                .filter(|&k| !succs[k].is_empty())
                .collect();
            let slots = (gap - 3).max(1) as usize;

            for (rank, &p) in parents_in_layer.iter().enumerate() {
                let bus = x_of(l) + node_w + 1 + (rank % slots) as u32;
                let py = center(p, ys[p]);
                let px = if vs[p].real.is_some() {
                    x_of(l) + node_w
                } else {
                    x_of(l)
                };

                for &c in &succs[p] {
                    let edge = match (vs[p].real, vs[c].real) {
                        (Some(a), Some(b)) => (a, b),
                        _ => vs[c].edge.or(vs[p].edge).unwrap_or((0, 0)),
                    };
                    let cy = center(c, ys[c]);
                    let cx = x_of(l + 1);
                    dag.hline(px, bus, py, edge);
                    dag.vline(bus, py, cy, edge);
                    if vs[c].real.is_some() {
                        dag.hline(bus, cx - 1, cy, edge);
                        dag.arrows.push((cx - 1, cy, edge));
                    } else {
                        dag.hline(bus, cx + node_w, cy, edge);
                    }
                }
            }
        }

        dag.width = x_of(depth - 1) + node_w + 1;
        dag.height = vs
            .iter()
            .enumerate()
            .map(|(k, _)| ys[k] + height_of(k))
            .max()
            .unwrap_or(0)
            + 1;

        dag
    }

    fn add(&mut self, x: u32, y: u32, bits: u8, edge: (usize, usize)) {
        let cell = self.lines.entry((x, y)).or_insert((0, Vec::new()));
        cell.0 |= bits;
        if !cell.1.contains(&edge) {
            cell.1.push(edge);
        }
    }

    fn hline(&mut self, a: u32, b: u32, y: u32, edge: (usize, usize)) {
        let (a, b) = (a.min(b), a.max(b));
        for x in a..=b {
            let mut bits = 0;
            if x > a {
                bits |= LEFT;
            }
            if x < b {
                bits |= RIGHT;
            }
            self.add(x, y, bits, edge);
        }
    }

    fn vline(&mut self, x: u32, a: u32, b: u32, edge: (usize, usize)) {
        if a == b {
            return;
        }
        let (a, b) = (a.min(b), a.max(b));
        for y in a..=b {
            let mut bits = 0;
            if y > a {
                bits |= UP;
            }
            if y < b {
                bits |= DOWN;
            }
            self.add(x, y, bits, edge);
        }
    }

    /// Nearest node in an adjacent layer (for keyboard navigation).
    pub fn neighbor(&self, node: usize, dx: i32, dy: i32) -> Option<usize> {
        let here = &self.nodes[node];
        if dy != 0 {
            let layer = &self.layers[here.layer];
            let k = layer.iter().position(|&v| v == node)? as i32 + dy;
            return (0..layer.len() as i32)
                .contains(&k)
                .then(|| layer[k as usize]);
        }
        let l = here.layer as i32 + dx;
        if l < 0 || l as usize >= self.layers.len() {
            return None;
        }
        self.layers[l as usize]
            .iter()
            .copied()
            .min_by_key(|&v| (self.nodes[v].y as i32 - here.y as i32).abs())
    }
}

pub fn line_glyph(bits: u8) -> char {
    match bits {
        b if b == LEFT | RIGHT || b == LEFT || b == RIGHT => '─',
        b if b == UP | DOWN || b == UP || b == DOWN => '│',
        b if b == DOWN | RIGHT => '╭',
        b if b == DOWN | LEFT => '╮',
        b if b == UP | RIGHT => '╰',
        b if b == UP | LEFT => '╯',
        b if b == UP | DOWN | RIGHT => '├',
        b if b == UP | DOWN | LEFT => '┤',
        b if b == LEFT | RIGHT | DOWN => '┬',
        b if b == LEFT | RIGHT | UP => '┴',
        _ => '┼',
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(rows: &[Vec<[char; 2]>]) -> Vec<String> {
        rows.iter()
            .map(|r| {
                r.iter()
                    .flat_map(|c| c.iter())
                    .collect::<String>()
                    .trim_end()
                    .to_string()
            })
            .collect()
    }

    #[test]
    fn diamond_lanes() {
        let parents = vec![vec![], vec![0], vec![0], vec![1, 2]];
        assert_eq!(text(&lanes(&parents)), vec!["@─╮", "@ │", "│ @", "@─╯"]);
    }

    #[test]
    fn chain_lanes() {
        let parents = vec![vec![], vec![0], vec![1]];
        assert_eq!(text(&lanes(&parents)), vec!["@", "@", "@"]);
    }

    #[test]
    fn dag_layout_is_layered() {
        // 0 -> 1 -> 3, 0 -> 2 -> 3, 0 -> 3
        let parents = vec![vec![], vec![0], vec![0], vec![0, 1, 2]];
        let dag = Dag::layout_with(&parents, NODE_W);
        assert_eq!(dag.nodes[0].layer, 0);
        assert_eq!(dag.nodes[1].layer, 1);
        assert_eq!(dag.nodes[3].layer, 2);
        assert_ne!(dag.nodes[1].y, dag.nodes[2].y);
        // Nodes never overlap within a layer
        for layer in &dag.layers {
            for w in layer.windows(2) {
                let (a, b) = (&dag.nodes[w[0]], &dag.nodes[w[1]]);
                let h = NODE_H as u32;
                assert!(a.y + h <= b.y || b.y + h <= a.y);
            }
        }
        assert!(!dag.lines.is_empty());
        assert_eq!(dag.neighbor(0, 1, 0).map(|v| dag.nodes[v].layer), Some(1));
    }

    #[test]
    fn long_chains_are_fast() {
        let n = 20_000;
        let parents: Vec<Vec<usize>> = (0..n)
            .map(|v| if v == 0 { vec![] } else { vec![v - 1] })
            .collect();
        let start = std::time::Instant::now();
        let dag = Dag::layout_with(&parents, NODE_W);
        assert!(start.elapsed().as_secs_f64() < 2.0);
        assert_eq!(dag.nodes[n - 1].layer, n - 1);
        assert!(dag.width > u16::MAX as u32); // no overflow
    }

    #[test]
    fn groups_fan_outs() {
        let mk = |index, name: &str, deps: Vec<usize>| JobMeta {
            index,
            name: name.into(),
            deps: deps
                .into_iter()
                .map(|d| (d, "success".to_string()))
                .collect(),
            ..JobMeta::default()
        };
        let mut jobs = vec![mk(0, "prep", vec![])];
        for i in 1..=5 {
            jobs.push(mk(i, "task", vec![0]));
        }
        jobs.push(mk(6, "merge", (1..=5).collect()));
        let g = groups(&jobs, 3);
        assert_eq!(g.iter().map(|g| g.len()).collect::<Vec<_>>(), vec![1, 5, 1]);
        assert_eq!(group_parents(&jobs, &g), vec![vec![], vec![0], vec![1]]);
    }
}
