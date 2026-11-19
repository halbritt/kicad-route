//! Effective `.kicad_pro`, netclass, and `.kicad_dru` rule parsing and evaluation.
//! Zero external regex dependencies: uses a bounded linear-time Thompson NFA
//! alongside KiCad wildcard matching for `netclass_patterns`, and a fail-closed
//! recursive-descent S-expression / condition evaluator for `.kicad_dru`.

use crate::model::{Failure, Input, Net, Rules, Status, MAX_NM};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

const MAX_NFA_STATES: usize = 1024;
const MAX_REGEX_LEN: usize = 1024;
const MAX_DRU_EXPR_DEPTH: usize = 64;

fn mm_to_nm(val: f64, label: &str) -> Result<i64, Failure> {
    if !val.is_finite() || val < 0.0 || val > 200.0 {
        return Err(Failure::new(
            Status::Invalid,
            format!("{label} must be finite, nonnegative, and <= 200 mm"),
        ));
    }
    let nm = (val * 1_000_000.0).round();
    if !nm.is_finite() || nm < 0.0 || nm > (2 * MAX_NM) as f64 {
        return Err(Failure::new(
            Status::Invalid,
            format!("{label} exceeds supported nanometre bounds"),
        ));
    }
    Ok(nm as i64)
}

fn json_mm_field(
    obj: &serde_json::Map<String, serde_json::Value>,
    key: &str,
    default_nm: i64,
) -> Result<i64, Failure> {
    match obj.get(key) {
        None | Some(serde_json::Value::Null) => Ok(default_nm),
        Some(v) => {
            let f = v.as_f64().ok_or_else(|| {
                Failure::new(
                    Status::Invalid,
                    format!(".kicad_pro {key} must be a number"),
                )
            })?;
            mm_to_nm(f, key)
        }
    }
}

fn json_bool_field(
    obj: &serde_json::Map<String, serde_json::Value>,
    key: &str,
    default_val: bool,
) -> Result<bool, Failure> {
    match obj.get(key) {
        None | Some(serde_json::Value::Null) => Ok(default_val),
        Some(v) => v.as_bool().ok_or_else(|| {
            Failure::new(
                Status::Invalid,
                format!(".kicad_pro {key} must be a boolean"),
            )
        }),
    }
}

fn json_opt_bool_field(
    obj: &serde_json::Map<String, serde_json::Value>,
    key: &str,
) -> Result<Option<bool>, Failure> {
    match obj.get(key) {
        None | Some(serde_json::Value::Null) => Ok(None),
        Some(v) => v.as_bool().map(Some).ok_or_else(|| {
            Failure::new(
                Status::Invalid,
                format!(".kicad_pro {key} must be a boolean"),
            )
        }),
    }
}

fn json_nonneg_rounded_i64_field(
    obj: &serde_json::Map<String, serde_json::Value>,
    key: &str,
    required: bool,
    default_val: i64,
) -> Result<i64, Failure> {
    match obj.get(key) {
        None => {
            if required {
                Err(Failure::new(
                    Status::Invalid,
                    format!(".kicad_pro {key} is required"),
                ))
            } else {
                Ok(default_val)
            }
        }
        Some(v) => {
            if let Some(i) = v.as_i64() {
                if i < 0 {
                    return Err(Failure::new(
                        Status::Invalid,
                        format!(".kicad_pro {key} must be nonnegative"),
                    ));
                }
                return Ok(i);
            }
            if let Some(u) = v.as_u64() {
                return i64::try_from(u).map_err(|_| {
                    Failure::new(
                        Status::Invalid,
                        format!(".kicad_pro {key} exceeds i64 bounds"),
                    )
                });
            }
            if let Some(f) = v.as_f64() {
                if !f.is_finite() || f < 0.0 {
                    return Err(Failure::new(
                        Status::Invalid,
                        format!(".kicad_pro {key} must be finite and nonnegative"),
                    ));
                }
                let r = f.round();
                if !r.is_finite() || r < 0.0 || r >= (i64::MAX as f64) {
                    return Err(Failure::new(
                        Status::Invalid,
                        format!(".kicad_pro {key} exceeds i64 bounds"),
                    ));
                }
                return Ok(r as i64);
            }
            Err(Failure::new(
                Status::Invalid,
                format!(".kicad_pro {key} must be a number"),
            ))
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct BoardDesignSettings {
    pub min_track_width_nm: i64,
    pub min_clearance_nm: i64,
    pub min_edge_clearance_nm: i64,
    pub min_via_diameter_nm: i64,
    pub min_via_annular_width_nm: i64,
    pub min_through_hole_diameter_nm: i64,
    pub min_hole_clearance_nm: i64,
    pub min_hole_to_hole_nm: i64,
    pub min_microvia_diameter_nm: i64,
    pub min_microvia_drill_nm: i64,
    pub solder_mask_expansion_nm: i64,
    pub solder_mask_min_width_nm: i64,
    pub solder_mask_to_copper_clearance_nm: i64,
    pub default_clearance_nm: i64,
    pub default_track_width_nm: i64,
    pub default_via_diameter_nm: i64,
    pub default_via_drill_nm: i64,
    pub default_microvia_diameter_nm: i64,
    pub default_microvia_drill_nm: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min_resolved_spokes: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_error_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub netclasses: Vec<NetClassDef>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub netclass_assignments: BTreeMap<String, Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub use_height_for_length_calcs: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub board_thickness_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outer_solder_mask_thickness_nm: Option<i64>,
    #[serde(default)]
    pub cap_vias: bool,
    #[serde(default)]
    pub fill_vias: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct BoardMinimums {
    pub min_track_width_nm: i64,
    pub min_clearance_nm: i64,
    pub min_edge_clearance_nm: i64,
    pub min_via_diameter_nm: i64,
    pub min_via_annular_width_nm: i64,
    pub min_through_hole_diameter_nm: i64,
    pub min_hole_clearance_nm: i64,
    pub min_hole_to_hole_nm: i64,
    pub min_microvia_diameter_nm: i64,
    pub min_microvia_drill_nm: i64,
    pub solder_mask_expansion_nm: i64,
    pub solder_mask_min_width_nm: i64,
    pub solder_mask_to_copper_clearance_nm: i64,
    pub allow_blind_buried_vias: bool,
    pub allow_microvias: bool,
    pub hole_to_hole_active: bool,
    pub hole_clearance_active: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min_resolved_spokes: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_error_nm: Option<i64>,
}

impl Default for BoardMinimums {
    fn default() -> Self {
        Self {
            min_track_width_nm: 0,
            min_clearance_nm: 0,
            min_edge_clearance_nm: 0,
            min_via_diameter_nm: 0,
            min_via_annular_width_nm: 0,
            min_through_hole_diameter_nm: 0,
            min_hole_clearance_nm: 0,
            min_hole_to_hole_nm: 0,
            min_microvia_diameter_nm: 0,
            min_microvia_drill_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
            allow_blind_buried_vias: false,
            allow_microvias: false,
            hole_to_hole_active: true,
            hole_clearance_active: true,
            min_resolved_spokes: None,
            max_error_nm: None,
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(default)]
pub struct NetClassDef {
    pub name: String,
    pub priority: i64,
    pub clearance_nm: i64,
    pub track_width_nm: i64,
    pub via_diameter_nm: i64,
    pub via_drill_nm: i64,
    pub microvia_diameter_nm: i64,
    pub microvia_drill_nm: i64,
    pub diff_pair_width_nm: i64,
    pub diff_pair_gap_nm: i64,
    pub diff_pair_via_gap_nm: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tuning_profile: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
pub struct TuningProfileLayerEntry {
    pub signal_layer: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub top_reference_layer: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub bottom_reference_layer: Option<String>,
    pub width_nm: i64,
    pub diff_pair_gap_nm: i64,
    pub delay_as_per_mm: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
pub struct TuningViaOverride {
    #[serde(default)]
    pub via_diameter_nm: i64,
    #[serde(default)]
    pub start_layer: String,
    #[serde(default)]
    pub end_layer: String,
    #[serde(default)]
    pub signal_layer_from: String,
    #[serde(default)]
    pub signal_layer_to: String,
    #[serde(default)]
    pub via_layer_from: String,
    #[serde(default)]
    pub via_layer_to: String,
    pub delay_as: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
pub struct TuningProfile {
    pub profile_name: String,
    pub profile_type: i64,
    pub target_impedance_ohm: i64,
    pub enable_time_domain_tuning: bool,
    pub layer_entries: Vec<TuningProfileLayerEntry>,
    pub via_prop_delay_as_per_mm: i64,
    pub via_overrides: Vec<TuningViaOverride>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NetClassPattern {
    pub netclass: String,
    pub pattern: String,
    matcher: PatternMatcher,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProjectRules {
    pub version: u32,
    pub minimums: BoardMinimums,
    pub classes: Vec<NetClassDef>,
    pub assignments: BTreeMap<String, Vec<String>>,
    pub patterns: Vec<NetClassPattern>,
    pub tuning_profiles: Vec<TuningProfile>,
    pub use_height_for_length_calcs: Option<bool>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum CharMatcher {
    Any,
    Literal(char),
    Class {
        negated: bool,
        ranges: Vec<(char, char)>,
        shorthands: Vec<u8>,
    },
}

impl CharMatcher {
    fn matches(&self, c: char) -> bool {
        match self {
            Self::Any => true,
            Self::Literal(ch) => c == *ch,
            Self::Class {
                negated,
                ranges,
                shorthands,
            } => {
                let mut hit = ranges.iter().any(|&(lo, hi)| c >= lo && c <= hi);
                if !hit {
                    for &s in shorthands {
                        let m = match s {
                            b'd' => c.is_ascii_digit(),
                            b'D' => !c.is_ascii_digit(),
                            b'w' => c.is_ascii_alphanumeric() || c == '_',
                            b'W' => !(c.is_ascii_alphanumeric() || c == '_'),
                            b's' => c.is_whitespace(),
                            b'S' => !c.is_whitespace(),
                            _ => false,
                        };
                        if m {
                            hit = true;
                            break;
                        }
                    }
                }
                if *negated {
                    !hit
                } else {
                    hit
                }
            }
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum NfaNode {
    Match,
    AnchorStart(usize),
    AnchorEnd(usize),
    Step { matcher: CharMatcher, next: usize },
    Split(usize, usize),
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Nfa {
    nodes: Vec<NfaNode>,
    start: usize,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct PatternMatcher {
    regex: Option<Nfa>,
    wildcard: Option<String>,
}

impl PatternMatcher {
    fn compile(pattern: &str) -> Result<Self, Failure> {
        if pattern.len() > MAX_REGEX_LEN {
            return Err(Failure::new(
                Status::Unsupported,
                "netclass pattern exceeds maximum length",
            ));
        }
        let has_regex_meta = pattern.chars().any(|c| {
            matches!(
                c,
                '^' | '$' | '\\' | '+' | '[' | ']' | '(' | ')' | '{' | '}' | '|'
            )
        }) || pattern.contains(".*")
            || pattern.contains(".?");
        let wildcard = if !has_regex_meta && (pattern.contains('*') || pattern.contains('?')) {
            Some(pattern.to_owned())
        } else {
            None
        };
        let regex = if wildcard.is_some() {
            None
        } else {
            Some(compile_nfa(pattern)?)
        };
        Ok(Self { regex, wildcard })
    }

    fn matches(&self, text: &str) -> bool {
        if let Some(nfa) = &self.regex {
            if nfa.full_match(text) {
                return true;
            }
        }
        if let Some(glob) = &self.wildcard {
            if wildcard_full_match(glob, text) {
                return true;
            }
        }
        false
    }
}

fn wildcard_full_match(pattern: &str, text: &str) -> bool {
    let p: Vec<char> = pattern.chars().collect();
    let t: Vec<char> = text.chars().collect();
    let (mut pi, mut ti) = (0usize, 0usize);
    let mut star_pi: Option<usize> = None;
    let mut match_ti = 0usize;
    while ti < t.len() {
        if pi < p.len() && (p[pi] == '?' || p[pi] == t[ti]) {
            pi += 1;
            ti += 1;
        } else if pi < p.len() && p[pi] == '*' {
            star_pi = Some(pi);
            match_ti = ti;
            pi += 1;
        } else if let Some(sp) = star_pi {
            pi = sp + 1;
            match_ti += 1;
            ti = match_ti;
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == '*' {
        pi += 1;
    }
    pi == p.len()
}

#[derive(Clone, Debug)]
enum RegexAst {
    Empty,
    AnchorStart,
    AnchorEnd,
    Char(CharMatcher),
    Seq(Vec<RegexAst>),
    Alt(Vec<RegexAst>),
    Repeat {
        inner: Box<RegexAst>,
        min: usize,
        max: Option<usize>,
    },
}

struct RegexParser<'a> {
    chars: &'a [char],
    pos: usize,
}

impl<'a> RegexParser<'a> {
    fn peek(&self) -> Option<char> {
        self.chars.get(self.pos).copied()
    }

    fn next(&mut self) -> Option<char> {
        let ch = self.peek()?;
        self.pos += 1;
        Some(ch)
    }

    fn parse_alt(&mut self, depth: usize) -> Result<RegexAst, Failure> {
        if depth > 32 {
            return Err(Failure::new(Status::Unsupported, "regex nesting too deep"));
        }
        let mut branches = vec![self.parse_seq(depth)?];
        while self.peek() == Some('|') {
            self.next();
            branches.push(self.parse_seq(depth)?);
        }
        if branches.len() == 1 {
            Ok(branches.pop().unwrap())
        } else {
            Ok(RegexAst::Alt(branches))
        }
    }

    fn parse_seq(&mut self, depth: usize) -> Result<RegexAst, Failure> {
        let mut items = Vec::new();
        while let Some(ch) = self.peek() {
            if ch == ')' || ch == '|' {
                break;
            }
            items.push(self.parse_quantified(depth)?);
        }
        if items.is_empty() {
            Ok(RegexAst::Empty)
        } else if items.len() == 1 {
            Ok(items.pop().unwrap())
        } else {
            Ok(RegexAst::Seq(items))
        }
    }

    fn parse_quantified(&mut self, depth: usize) -> Result<RegexAst, Failure> {
        let mut atom = self.parse_atom(depth)?;
        loop {
            match self.peek() {
                Some('*') => {
                    self.next();
                    if self.peek() == Some('?') {
                        self.next();
                    }
                    atom = RegexAst::Repeat {
                        inner: Box::new(atom),
                        min: 0,
                        max: None,
                    };
                }
                Some('+') => {
                    self.next();
                    if self.peek() == Some('?') {
                        self.next();
                    }
                    atom = RegexAst::Repeat {
                        inner: Box::new(atom),
                        min: 1,
                        max: None,
                    };
                }
                Some('?') => {
                    self.next();
                    if self.peek() == Some('?') {
                        self.next();
                    }
                    atom = RegexAst::Repeat {
                        inner: Box::new(atom),
                        min: 0,
                        max: Some(1),
                    };
                }
                Some('{') => {
                    let (min, max) = self.parse_brace_quantifier()?;
                    if self.peek() == Some('?') {
                        self.next();
                    }
                    atom = RegexAst::Repeat {
                        inner: Box::new(atom),
                        min,
                        max,
                    };
                }
                _ => break,
            }
        }
        Ok(atom)
    }

    fn parse_brace_quantifier(&mut self) -> Result<(usize, Option<usize>), Failure> {
        self.next(); // '{'
        let mut num1 = String::new();
        while let Some(c) = self.peek() {
            if c.is_ascii_digit() {
                num1.push(c);
                self.next();
            } else {
                break;
            }
        }
        if num1.is_empty() {
            return Err(Failure::new(
                Status::Invalid,
                "invalid regex quantifier '{'",
            ));
        }
        let min: usize = num1
            .parse()
            .map_err(|_| Failure::new(Status::Unsupported, "regex quantifier too large"))?;
        let max = match self.peek() {
            Some('}') => {
                self.next();
                Some(min)
            }
            Some(',') => {
                self.next();
                if self.peek() == Some('}') {
                    self.next();
                    None
                } else {
                    let mut num2 = String::new();
                    while let Some(c) = self.peek() {
                        if c.is_ascii_digit() {
                            num2.push(c);
                            self.next();
                        } else {
                            break;
                        }
                    }
                    if self.next() != Some('}') || num2.is_empty() {
                        return Err(Failure::new(Status::Invalid, "invalid regex quantifier"));
                    }
                    let mx: usize = num2.parse().map_err(|_| {
                        Failure::new(Status::Unsupported, "regex quantifier too large")
                    })?;
                    if mx < min {
                        return Err(Failure::new(Status::Invalid, "regex quantifier max < min"));
                    }
                    Some(mx)
                }
            }
            _ => {
                return Err(Failure::new(
                    Status::Invalid,
                    "unclosed regex quantifier '{'",
                ))
            }
        };
        if min > 64 || max.is_some_and(|m| m > 64) {
            return Err(Failure::new(
                Status::Unsupported,
                "regex quantifier bound > 64",
            ));
        }
        Ok((min, max))
    }

    fn parse_atom(&mut self, depth: usize) -> Result<RegexAst, Failure> {
        let Some(ch) = self.next() else {
            return Err(Failure::new(Status::Invalid, "unexpected end of regex"));
        };
        match ch {
            '*' | '+' | '?' | '{' | ')' | ']' | '}' => Err(Failure::new(
                Status::Invalid,
                format!("unexpected regex token '{ch}'"),
            )),
            '^' => Ok(RegexAst::AnchorStart),
            '$' => Ok(RegexAst::AnchorEnd),
            '.' => Ok(RegexAst::Char(CharMatcher::Any)),
            '[' => self.parse_char_class(),
            '(' => {
                if self.peek() == Some('?') {
                    self.next();
                    if self.next() != Some(':') {
                        return Err(Failure::new(
                            Status::Unsupported,
                            "unsupported regex group construct",
                        ));
                    }
                }
                let inner = self.parse_alt(depth + 1)?;
                if self.next() != Some(')') {
                    return Err(Failure::new(Status::Invalid, "unclosed regex group '('"));
                }
                Ok(inner)
            }
            '\\' => self.parse_escape(),
            lit => Ok(RegexAst::Char(CharMatcher::Literal(lit))),
        }
    }

    fn parse_escape(&mut self) -> Result<RegexAst, Failure> {
        let Some(ch) = self.next() else {
            return Err(Failure::new(Status::Invalid, "trailing regex backslash"));
        };
        match ch {
            'd' | 'D' | 'w' | 'W' | 's' | 'S' => Ok(RegexAst::Char(CharMatcher::Class {
                negated: false,
                ranges: Vec::new(),
                shorthands: vec![ch as u8],
            })),
            'n' => Ok(RegexAst::Char(CharMatcher::Literal('\n'))),
            'r' => Ok(RegexAst::Char(CharMatcher::Literal('\r'))),
            't' => Ok(RegexAst::Char(CharMatcher::Literal('\t'))),
            '1'..='9' => Err(Failure::new(
                Status::Unsupported,
                "regex backreferences are unsupported",
            )),
            c if c.is_ascii_punctuation() || c == ' ' => {
                Ok(RegexAst::Char(CharMatcher::Literal(c)))
            }
            other => Err(Failure::new(
                Status::Unsupported,
                format!("unsupported regex escape \\{other}"),
            )),
        }
    }

    fn parse_char_class(&mut self) -> Result<RegexAst, Failure> {
        let negated = if self.peek() == Some('^') {
            self.next();
            true
        } else {
            false
        };
        let mut ranges = Vec::new();
        let mut shorthands = Vec::new();
        let mut first = true;
        loop {
            let Some(ch) = self.next() else {
                return Err(Failure::new(Status::Invalid, "unclosed regex class '['"));
            };
            if ch == ']' && !first {
                break;
            }
            first = false;
            if ch == '\\' {
                let Some(esc) = self.next() else {
                    return Err(Failure::new(Status::Invalid, "unclosed regex class '['"));
                };
                match esc {
                    'd' | 'D' | 'w' | 'W' | 's' | 'S' => shorthands.push(esc as u8),
                    'n' => ranges.push(('\n', '\n')),
                    'r' => ranges.push(('\r', '\r')),
                    't' => ranges.push(('\t', '\t')),
                    c => ranges.push((c, c)),
                }
                continue;
            }
            if self.peek() == Some('-')
                && self
                    .chars
                    .get(self.pos + 1)
                    .copied()
                    .is_some_and(|n| n != ']')
            {
                self.next(); // '-'
                let end_ch = self
                    .next()
                    .ok_or_else(|| Failure::new(Status::Invalid, "unclosed regex class range"))?;
                let hi = if end_ch == '\\' {
                    self.next().ok_or_else(|| {
                        Failure::new(Status::Invalid, "unclosed regex class range")
                    })?
                } else {
                    end_ch
                };
                if ch > hi {
                    return Err(Failure::new(
                        Status::Invalid,
                        "invalid character class range",
                    ));
                }
                ranges.push((ch, hi));
            } else {
                ranges.push((ch, ch));
            }
        }
        Ok(RegexAst::Char(CharMatcher::Class {
            negated,
            ranges,
            shorthands,
        }))
    }
}

fn alloc_nfa_node(nodes: &mut Vec<NfaNode>, node: NfaNode) -> Result<usize, Failure> {
    if nodes.len() >= MAX_NFA_STATES {
        return Err(Failure::new(
            Status::Unsupported,
            "regex pattern exceeds NFA state budget",
        ));
    }
    let idx = nodes.len();
    nodes.push(node);
    Ok(idx)
}

fn compile_ast(ast: &RegexAst, next: usize, nodes: &mut Vec<NfaNode>) -> Result<usize, Failure> {
    match ast {
        RegexAst::Empty => Ok(next),
        RegexAst::AnchorStart => alloc_nfa_node(nodes, NfaNode::AnchorStart(next)),
        RegexAst::AnchorEnd => alloc_nfa_node(nodes, NfaNode::AnchorEnd(next)),
        RegexAst::Char(m) => alloc_nfa_node(
            nodes,
            NfaNode::Step {
                matcher: m.clone(),
                next,
            },
        ),
        RegexAst::Seq(items) => {
            let mut cur = next;
            for item in items.iter().rev() {
                cur = compile_ast(item, cur, nodes)?;
            }
            Ok(cur)
        }
        RegexAst::Alt(branches) => {
            let mut it = branches.iter().rev();
            let last = it
                .next()
                .ok_or_else(|| Failure::new(Status::Invalid, "empty alternation"))?;
            let mut cur = compile_ast(last, next, nodes)?;
            for b in it {
                let left = compile_ast(b, next, nodes)?;
                cur = alloc_nfa_node(nodes, NfaNode::Split(left, cur))?;
            }
            Ok(cur)
        }
        RegexAst::Repeat { inner, min, max } => {
            let mut cur = match max {
                None => {
                    let s = alloc_nfa_node(nodes, NfaNode::Split(0, next))?;
                    let body = compile_ast(inner, s, nodes)?;
                    nodes[s] = NfaNode::Split(body, next);
                    s
                }
                Some(mx) => {
                    let mut c = next;
                    for _ in *min..*mx {
                        let opt_body = compile_ast(inner, c, nodes)?;
                        c = alloc_nfa_node(nodes, NfaNode::Split(opt_body, c))?;
                    }
                    c
                }
            };
            for _ in 0..*min {
                cur = compile_ast(inner, cur, nodes)?;
            }
            Ok(cur)
        }
    }
}

fn compile_nfa(pattern: &str) -> Result<Nfa, Failure> {
    let chars: Vec<char> = pattern.chars().collect();
    let mut parser = RegexParser {
        chars: &chars,
        pos: 0,
    };
    let ast = parser.parse_alt(0)?;
    if parser.pos != chars.len() {
        return Err(Failure::new(
            Status::Invalid,
            "unexpected trailing token in regex",
        ));
    }
    let mut nodes = vec![NfaNode::Match];
    let start = compile_ast(&ast, 0, &mut nodes)?;
    Ok(Nfa { nodes, start })
}

impl Nfa {
    fn epsilon_closure(&self, seeds: &[usize], at_start: bool, at_end: bool) -> Vec<usize> {
        let mut seen = vec![false; self.nodes.len()];
        let mut stack: Vec<usize> = Vec::with_capacity(seeds.len());
        let mut out = Vec::new();
        for &s in seeds {
            if !seen[s] {
                seen[s] = true;
                stack.push(s);
            }
        }
        while let Some(idx) = stack.pop() {
            match &self.nodes[idx] {
                NfaNode::AnchorStart(n) => {
                    if at_start && !seen[*n] {
                        seen[*n] = true;
                        stack.push(*n);
                    }
                }
                NfaNode::AnchorEnd(n) => {
                    if at_end && !seen[*n] {
                        seen[*n] = true;
                        stack.push(*n);
                    }
                }
                NfaNode::Split(a, b) => {
                    if !seen[*a] {
                        seen[*a] = true;
                        stack.push(*a);
                    }
                    if !seen[*b] {
                        seen[*b] = true;
                        stack.push(*b);
                    }
                }
                NfaNode::Match | NfaNode::Step { .. } => {
                    out.push(idx);
                }
            }
        }
        out
    }

    fn full_match(&self, text: &str) -> bool {
        let chars: Vec<char> = text.chars().collect();
        let mut current = self.epsilon_closure(&[self.start], true, chars.is_empty());
        for (i, &ch) in chars.iter().enumerate() {
            let mut next_seeds = Vec::new();
            for &idx in &current {
                if let NfaNode::Step { matcher, next } = &self.nodes[idx] {
                    if matcher.matches(ch) {
                        next_seeds.push(*next);
                    }
                }
            }
            if next_seeds.is_empty() {
                return false;
            }
            current = self.epsilon_closure(&next_seeds, false, i + 1 == chars.len());
        }
        current.contains(&0)
    }
}

pub fn parse_kicad_pro(bytes: &[u8]) -> Result<ProjectRules, Failure> {
    let text = std::str::from_utf8(bytes).map_err(|e| {
        Failure::new(
            Status::Invalid,
            format!(".kicad_pro is not valid UTF-8: {e}"),
        )
    })?;
    let root: serde_json::Value = serde_json::from_str(text)
        .map_err(|e| Failure::new(Status::Invalid, format!("malformed .kicad_pro JSON: {e}")))?;
    let obj = root
        .as_object()
        .ok_or_else(|| Failure::new(Status::Invalid, ".kicad_pro root must be a JSON object"))?;
    let meta = obj
        .get("meta")
        .and_then(|v| v.as_object())
        .ok_or_else(|| Failure::new(Status::Unsupported, ".kicad_pro missing meta object"))?;
    let version_u64 = meta
        .get("version")
        .and_then(|v| v.as_u64())
        .ok_or_else(|| Failure::new(Status::Unsupported, ".kicad_pro missing meta.version"))?;
    if !(1..=3).contains(&version_u64) {
        return Err(Failure::new(
            Status::Unsupported,
            format!("unsupported .kicad_pro meta.version {version_u64}"),
        ));
    }

    let mut minimums = BoardMinimums::default();
    let mut use_height_for_length_calcs: Option<bool> = None;
    if let Some(board_val) = obj.get("board") {
        let board_obj = board_val
            .as_object()
            .ok_or_else(|| Failure::new(Status::Invalid, ".kicad_pro board must be an object"))?;
        if let Some(ds_val) = board_obj.get("design_settings") {
            let ds = ds_val.as_object().ok_or_else(|| {
                Failure::new(
                    Status::Invalid,
                    ".kicad_pro board.design_settings must be an object",
                )
            })?;
            if let Some(rules_val) = ds.get("rules") {
                let r = rules_val.as_object().ok_or_else(|| {
                    Failure::new(
                        Status::Invalid,
                        ".kicad_pro design_settings.rules must be an object",
                    )
                })?;
                if let Some(v) = json_opt_bool_field(r, "use_height_for_length_calcs")? {
                    use_height_for_length_calcs = Some(v);
                }
                minimums.min_track_width_nm = json_mm_field(r, "min_track_width", 0)?;
                minimums.min_clearance_nm = json_mm_field(r, "min_clearance", 0)?;
                minimums.min_edge_clearance_nm = json_mm_field(r, "min_copper_edge_clearance", 0)?;
                minimums.min_via_diameter_nm = json_mm_field(r, "min_via_diameter", 0)?;
                minimums.min_via_annular_width_nm = json_mm_field(r, "min_via_annular_width", 0)?;
                minimums.min_through_hole_diameter_nm =
                    json_mm_field(r, "min_through_hole_diameter", 0)?;
                minimums.min_hole_clearance_nm = json_mm_field(r, "min_hole_clearance", 0)?;
                minimums.min_hole_to_hole_nm = json_mm_field(r, "min_hole_to_hole", 0)?;
                minimums.min_microvia_diameter_nm = json_mm_field(r, "min_microvia_diameter", 0)?;
                minimums.min_microvia_drill_nm = json_mm_field(r, "min_microvia_drill", 0)?;
                minimums.solder_mask_expansion_nm = json_mm_field(r, "solder_mask_expansion", 0)?;
                minimums.solder_mask_min_width_nm = json_mm_field(r, "solder_mask_min_width", 0)?;
                minimums.solder_mask_to_copper_clearance_nm =
                    json_mm_field(r, "solder_mask_to_copper_clearance", 0)?;
                minimums.allow_blind_buried_vias =
                    json_bool_field(r, "allow_blind_buried_vias", false)?;
                minimums.allow_microvias = json_bool_field(r, "allow_microvias", false)?;
                if let Some(v) = r.get("min_resolved_spokes") {
                    if !v.is_null() {
                        let n = v.as_u64().ok_or_else(|| {
                            Failure::new(
                                Status::Invalid,
                                ".kicad_pro min_resolved_spokes must be a nonnegative integer",
                            )
                        })?;
                        if n > 16 {
                            return Err(Failure::new(
                                Status::Invalid,
                                ".kicad_pro min_resolved_spokes exceeds supported range",
                            ));
                        }
                        minimums.min_resolved_spokes = Some(n as u32);
                    }
                }
            }
            if let Some(sev_val) = ds.get("rule_severities") {
                let sev = sev_val.as_object().ok_or_else(|| {
                    Failure::new(
                        Status::Invalid,
                        ".kicad_pro rule_severities must be an object",
                    )
                })?;
                if sev.get("hole_to_hole").and_then(|v| v.as_str()) == Some("ignore") {
                    minimums.hole_to_hole_active = false;
                }
                if sev.get("hole_clearance").and_then(|v| v.as_str()) == Some("ignore") {
                    minimums.hole_clearance_active = false;
                }
            }
        }
    }

    let mut classes = Vec::new();
    let mut assignments = BTreeMap::new();
    let mut patterns = Vec::new();
    if let Some(ns_val) = obj.get("net_settings") {
        let ns = ns_val.as_object().ok_or_else(|| {
            Failure::new(Status::Invalid, ".kicad_pro net_settings must be an object")
        })?;
        if let Some(cls_arr) = ns.get("classes").and_then(|v| v.as_array()) {
            for item in cls_arr {
                let c = item.as_object().ok_or_else(|| {
                    Failure::new(
                        Status::Invalid,
                        ".kicad_pro netclass entry must be an object",
                    )
                })?;
                let name = c
                    .get("name")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| {
                        Failure::new(Status::Invalid, ".kicad_pro netclass missing name")
                    })?
                    .to_owned();
                let priority = c.get("priority").and_then(|v| v.as_i64()).unwrap_or(0);
                let tuning_profile = match c.get("tuning_profile") {
                    None | Some(serde_json::Value::Null) => None,
                    Some(v) => {
                        let s = v.as_str().ok_or_else(|| {
                            Failure::new(
                                Status::Invalid,
                                ".kicad_pro netclass tuning_profile must be a string",
                            )
                        })?;
                        (!s.is_empty()).then(|| s.to_owned())
                    }
                };
                classes.push(NetClassDef {
                    name,
                    priority,
                    clearance_nm: json_mm_field(c, "clearance", 0)?,
                    track_width_nm: json_mm_field(c, "track_width", 0)?,
                    via_diameter_nm: json_mm_field(c, "via_diameter", 0)?,
                    via_drill_nm: json_mm_field(c, "via_drill", 0)?,
                    microvia_diameter_nm: json_mm_field(c, "microvia_diameter", 0)?,
                    microvia_drill_nm: json_mm_field(c, "microvia_drill", 0)?,
                    diff_pair_width_nm: json_mm_field(c, "diff_pair_width", 0)?,
                    diff_pair_gap_nm: json_mm_field(c, "diff_pair_gap", 0)?,
                    diff_pair_via_gap_nm: json_mm_field(c, "diff_pair_via_gap", 0)?,
                    tuning_profile,
                });
            }
        }
        if let Some(assign_val) = ns.get("netclass_assignments") {
            if !assign_val.is_null() {
                if let Some(arr) = assign_val.as_array() {
                    if !arr.is_empty() {
                        return Err(Failure::new(
                            Status::Invalid,
                            ".kicad_pro netclass_assignments must be an object",
                        ));
                    }
                } else {
                    let map = assign_val.as_object().ok_or_else(|| {
                        Failure::new(
                            Status::Invalid,
                            ".kicad_pro netclass_assignments must be an object",
                        )
                    })?;
                    for (net_name, val) in map {
                        match val {
                            serde_json::Value::String(s) => {
                                assignments.insert(net_name.clone(), vec![s.clone()]);
                            }
                            serde_json::Value::Array(arr) => {
                                let mut list = Vec::with_capacity(arr.len());
                                for elem in arr {
                                    let s = elem.as_str().ok_or_else(|| {
                                        Failure::new(
                                            Status::Invalid,
                                            ".kicad_pro netclass_assignments list entries must be strings",
                                        )
                                    })?;
                                    list.push(s.to_owned());
                                }
                                assignments.insert(net_name.clone(), list);
                            }
                            _ => {
                                return Err(Failure::new(
                                    Status::Invalid,
                                    ".kicad_pro netclass_assignments values must be string or list",
                                ));
                            }
                        }
                    }
                }
            }
        }
        if let Some(pat_val) = ns.get("netclass_patterns") {
            if !pat_val.is_null() {
                let arr = pat_val.as_array().ok_or_else(|| {
                    Failure::new(
                        Status::Invalid,
                        ".kicad_pro netclass_patterns must be an array",
                    )
                })?;
                for item in arr {
                    let p = item.as_object().ok_or_else(|| {
                        Failure::new(
                            Status::Invalid,
                            ".kicad_pro pattern entry must be an object",
                        )
                    })?;
                    let netclass = p
                        .get("netclass")
                        .and_then(|v| v.as_str())
                        .ok_or_else(|| {
                            Failure::new(Status::Invalid, ".kicad_pro pattern missing netclass")
                        })?
                        .to_owned();
                    let pattern = p
                        .get("pattern")
                        .and_then(|v| v.as_str())
                        .ok_or_else(|| {
                            Failure::new(Status::Invalid, ".kicad_pro pattern missing pattern")
                        })?
                        .to_owned();
                    let matcher = PatternMatcher::compile(&pattern)?;
                    patterns.push(NetClassPattern {
                        netclass,
                        pattern,
                        matcher,
                    });
                }
            }
        }
    }

    let mut tuning_profiles = Vec::new();
    if let Some(tp_val) = obj.get("tuning_profiles") {
        if !tp_val.is_null() {
            let tp_obj = tp_val.as_object().ok_or_else(|| {
                Failure::new(
                    Status::Invalid,
                    ".kicad_pro tuning_profiles must be an object",
                )
            })?;
            if let Some(arr_val) = tp_obj.get("tuning_profiles_impedance_geometric") {
                if !arr_val.is_null() {
                    let arr = arr_val.as_array().ok_or_else(|| {
                        Failure::new(
                            Status::Invalid,
                            ".kicad_pro tuning_profiles_impedance_geometric must be an array",
                        )
                    })?;
                    for item in arr {
                        let p = item.as_object().ok_or_else(|| {
                            Failure::new(
                                Status::Invalid,
                                ".kicad_pro tuning profile entry must be an object",
                            )
                        })?;
                        let profile_name = p
                            .get("profile_name")
                            .and_then(|v| v.as_str())
                            .ok_or_else(|| {
                                Failure::new(
                                    Status::Invalid,
                                    ".kicad_pro tuning profile missing profile_name",
                                )
                            })?
                            .to_owned();
                        let profile_type = json_nonneg_rounded_i64_field(p, "type", false, 0)?;
                        let target_impedance_ohm =
                            json_nonneg_rounded_i64_field(p, "target_impedance", false, 0)?;
                        let enable_time_domain_tuning =
                            json_bool_field(p, "enable_time_domain_tuning", false)?;
                        let via_prop_delay_as_per_mm =
                            json_nonneg_rounded_i64_field(p, "via_prop_delay", false, 0)?;
                        let mut layer_entries = Vec::new();
                        if let Some(le_arr) = p.get("layer_entries").and_then(|v| v.as_array()) {
                            for le_val in le_arr {
                                let le = le_val.as_object().ok_or_else(|| {
                                    Failure::new(
                                        Status::Invalid,
                                        ".kicad_pro tuning profile layer_entry must be an object",
                                    )
                                })?;
                                let signal_layer = le
                                    .get("signal_layer")
                                    .and_then(|v| v.as_str())
                                    .ok_or_else(|| {
                                        Failure::new(
                                            Status::Invalid,
                                            ".kicad_pro layer_entry missing signal_layer",
                                        )
                                    })?
                                    .to_owned();
                                let clean_ref = |k: &str| {
                                    le.get(k)
                                        .and_then(|v| v.as_str())
                                        .filter(|s| !s.is_empty() && *s != "UNDEFINED")
                                        .map(str::to_owned)
                                };
                                let width_nm =
                                    json_nonneg_rounded_i64_field(le, "width", false, 0)?;
                                let diff_pair_gap_nm =
                                    json_nonneg_rounded_i64_field(le, "diff_pair_gap", false, 0)?;
                                let delay_as_per_mm =
                                    json_nonneg_rounded_i64_field(le, "delay", false, 0)?;
                                layer_entries.push(TuningProfileLayerEntry {
                                    signal_layer,
                                    top_reference_layer: clean_ref("top_reference_layer"),
                                    bottom_reference_layer: clean_ref("bottom_reference_layer"),
                                    width_nm,
                                    diff_pair_gap_nm,
                                    delay_as_per_mm,
                                });
                            }
                        }
                        let mut via_overrides = Vec::new();
                        if let Some(vo_arr) = p.get("via_overrides").and_then(|v| v.as_array()) {
                            for vo_val in vo_arr {
                                let vo = vo_val.as_object().ok_or_else(|| {
                                    Failure::new(
                                        Status::Invalid,
                                        ".kicad_pro via_override entry must be an object",
                                    )
                                })?;
                                let via_diameter_nm =
                                    json_nonneg_rounded_i64_field(vo, "via_diameter", false, 0)?;
                                let signal_layer_from = vo
                                    .get("signal_layer_from")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("")
                                    .to_owned();
                                let signal_layer_to = vo
                                    .get("signal_layer_to")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("")
                                    .to_owned();
                                let via_layer_from = vo
                                    .get("via_layer_from")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("")
                                    .to_owned();
                                let via_layer_to = vo
                                    .get("via_layer_to")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("")
                                    .to_owned();
                                let start_layer = vo
                                    .get("start_layer")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("")
                                    .to_owned();
                                let end_layer = vo
                                    .get("end_layer")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("")
                                    .to_owned();
                                let delay_as = json_nonneg_rounded_i64_field(vo, "delay", true, 0)?;
                                via_overrides.push(TuningViaOverride {
                                    via_diameter_nm,
                                    start_layer,
                                    end_layer,
                                    signal_layer_from,
                                    signal_layer_to,
                                    via_layer_from,
                                    via_layer_to,
                                    delay_as,
                                });
                            }
                        }
                        tuning_profiles.push(TuningProfile {
                            profile_name,
                            profile_type,
                            target_impedance_ohm,
                            enable_time_domain_tuning,
                            layer_entries,
                            via_prop_delay_as_per_mm,
                            via_overrides,
                        });
                    }
                }
            }
        }
    }

    Ok(ProjectRules {
        version: version_u64 as u32,
        minimums,
        classes,
        assignments,
        patterns,
        tuning_profiles,
        use_height_for_length_calcs,
    })
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum Sexpr {
    Atom(String),
    Str(String),
    List(Vec<Sexpr>),
}

fn parse_sexprs(input: &str) -> Result<Vec<Sexpr>, Failure> {
    let bytes = input.as_bytes();
    let mut i = 0usize;
    let mut stack: Vec<Vec<Sexpr>> = Vec::new();
    let mut top: Vec<Sexpr> = Vec::new();

    while i < bytes.len() {
        let b = bytes[i];
        if b.is_ascii_whitespace() {
            i += 1;
            continue;
        }
        if b == b'#' {
            while i < bytes.len() && bytes[i] != b'\n' {
                i += 1;
            }
            continue;
        }
        if b == b'(' {
            if stack.len() >= MAX_DRU_EXPR_DEPTH {
                return Err(Failure::new(
                    Status::Unsupported,
                    ".kicad_dru S-expression exceeds nesting depth",
                ));
            }
            stack.push(Vec::new());
            i += 1;
            continue;
        }
        if b == b')' {
            let list = stack
                .pop()
                .ok_or_else(|| Failure::new(Status::Invalid, "unexpected ')' in .kicad_dru"))?;
            let node = Sexpr::List(list);
            if let Some(parent) = stack.last_mut() {
                parent.push(node);
            } else {
                top.push(node);
            }
            i += 1;
            continue;
        }
        if b == b'"' {
            i += 1;
            let mut s = String::new();
            let mut closed = false;
            while i < bytes.len() {
                let ch = input[i..].chars().next().unwrap();
                if ch == '"' {
                    i += 1;
                    closed = true;
                    break;
                }
                if ch == '\\' {
                    i += 1;
                    if i >= bytes.len() {
                        return Err(Failure::new(
                            Status::Invalid,
                            "unterminated escape in .kicad_dru string",
                        ));
                    }
                    let esc = input[i..].chars().next().unwrap();
                    i += esc.len_utf8();
                    match esc {
                        'n' => s.push('\n'),
                        'r' => s.push('\r'),
                        't' => s.push('\t'),
                        '\\' => s.push('\\'),
                        '"' => s.push('"'),
                        other => s.push(other),
                    }
                } else {
                    s.push(ch);
                    i += ch.len_utf8();
                }
            }
            if !closed {
                return Err(Failure::new(
                    Status::Invalid,
                    "unterminated string in .kicad_dru",
                ));
            }
            let node = Sexpr::Str(s);
            if let Some(parent) = stack.last_mut() {
                parent.push(node);
            } else {
                top.push(node);
            }
            continue;
        }
        let start = i;
        while i < bytes.len() {
            let ch = bytes[i];
            if ch.is_ascii_whitespace() || matches!(ch, b'(' | b')' | b'"' | b'#') {
                break;
            }
            i += 1;
        }
        let atom = input[start..i].to_owned();
        let node = Sexpr::Atom(atom);
        if let Some(parent) = stack.last_mut() {
            parent.push(node);
        } else {
            top.push(node);
        }
    }
    if !stack.is_empty() {
        return Err(Failure::new(Status::Invalid, "unclosed '(' in .kicad_dru"));
    }
    Ok(top)
}

fn parse_dru_dim_nm(tok: &str) -> Result<i64, Failure> {
    let s = tok.trim();
    let (num_str, scale_nm) = if let Some(v) = s.strip_suffix("mm") {
        (v, 1_000_000.0)
    } else if let Some(v) = s.strip_suffix("um") {
        (v, 1_000.0)
    } else if let Some(v) = s.strip_suffix("mil") {
        (v, 25_400.0)
    } else if let Some(v) = s.strip_suffix("in") {
        (v, 25_400_000.0)
    } else if let Some(v) = s.strip_suffix("nm") {
        (v, 1.0)
    } else {
        return Err(Failure::new(
            Status::Unsupported,
            format!("unsupported .kicad_dru dimension unit: {tok}"),
        ));
    };
    let val: f64 = num_str.trim().parse().map_err(|_| {
        Failure::new(
            Status::Invalid,
            format!("invalid .kicad_dru dimension: {tok}"),
        )
    })?;
    if !val.is_finite() || val < 0.0 {
        return Err(Failure::new(
            Status::Invalid,
            format!(".kicad_dru dimension must be finite and nonnegative: {tok}"),
        ));
    }
    let nm = (val * scale_nm).round();
    if !nm.is_finite() || nm < 0.0 || nm > (2 * MAX_NM) as f64 {
        return Err(Failure::new(
            Status::Invalid,
            format!(".kicad_dru dimension exceeds bounds: {tok}"),
        ));
    }
    Ok(nm as i64)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum DruLengthOrDelay {
    LengthNm(i64),
    DelayAs(i64),
}

fn parse_dru_length_or_delay(tok: &str) -> Result<DruLengthOrDelay, Failure> {
    let s = tok.trim();
    let time_scale = if let Some(v) = s.strip_suffix("ps") {
        Some((v, 1_000_000.0))
    } else if let Some(v) = s.strip_suffix("ns") {
        Some((v, 1_000_000_000.0))
    } else if let Some(v) = s.strip_suffix("us") {
        Some((v, 1_000_000_000_000.0))
    } else if let Some(v) = s.strip_suffix("fs") {
        Some((v, 1_000.0))
    } else if let Some(v) = s.strip_suffix("as") {
        Some((v, 1.0))
    } else {
        None
    };
    if let Some((num_str, scale_as)) = time_scale {
        let val: f64 = num_str.trim().parse().map_err(|_| {
            Failure::new(
                Status::Invalid,
                format!("invalid .kicad_dru time dimension: {tok}"),
            )
        })?;
        if !val.is_finite() || val < 0.0 {
            return Err(Failure::new(
                Status::Invalid,
                format!(".kicad_dru time dimension must be finite and nonnegative: {tok}"),
            ));
        }
        let as_val = (val * scale_as).round();
        if !as_val.is_finite() || as_val < 0.0 || as_val > (i64::MAX / 2) as f64 {
            return Err(Failure::new(
                Status::Invalid,
                format!(".kicad_dru time dimension exceeds bounds: {tok}"),
            ));
        }
        return Ok(DruLengthOrDelay::DelayAs(as_val as i64));
    }
    Ok(DruLengthOrDelay::LengthNm(parse_dru_dim_nm(tok)?))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DruSeverity {
    Error,
    Warning,
    Ignore,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
pub enum Side {
    A,
    B,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
pub enum DruProp {
    NetClass,
    NetName,
    Net,
    Type,
    Layer,
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
pub enum DruValueExpr {
    Prop(Side, DruProp),
    StrLit(String),
    IntLit(i64),
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
pub enum DruExpr {
    BoolLit(bool),
    Not(Box<DruExpr>),
    And(Box<DruExpr>, Box<DruExpr>),
    Or(Box<DruExpr>, Box<DruExpr>),
    Eq(DruValueExpr, DruValueExpr),
    Ne(DruValueExpr, DruValueExpr),
    SameNet,
    ExistsOnLayer { side: Side, layer: String },
    HasNetClass { side: Side, class_name: String },
    InDiffPair { side: Side, pattern: String },
    FromTo { side: Side, a: String, b: String },
}

impl DruExpr {
    fn uses_b_or_ab(&self) -> bool {
        match self {
            Self::BoolLit(_) => false,
            Self::Not(x) => x.uses_b_or_ab(),
            Self::And(a, b) | Self::Or(a, b) => a.uses_b_or_ab() || b.uses_b_or_ab(),
            Self::Eq(l, r) | Self::Ne(l, r) => {
                matches!(l, DruValueExpr::Prop(Side::B, _))
                    || matches!(r, DruValueExpr::Prop(Side::B, _))
            }
            Self::SameNet => true,
            Self::ExistsOnLayer { side, .. }
            | Self::HasNetClass { side, .. }
            | Self::InDiffPair { side, .. }
            | Self::FromTo { side, .. } => *side == Side::B,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
pub enum DruConstraint {
    Clearance {
        min_nm: i64,
    },
    TrackWidth {
        min_nm: i64,
        max_nm: Option<i64>,
    },
    ViaDiameter {
        min_nm: i64,
        max_nm: Option<i64>,
    },
    ViaDrill {
        min_nm: i64,
        max_nm: Option<i64>,
    },
    HoleClearance {
        min_nm: i64,
    },
    HoleToHole {
        min_nm: i64,
    },
    EdgeClearance {
        min_nm: i64,
    },
    AnnularWidth {
        min_nm: i64,
    },
    ViaCount {
        max: u32,
    },
    Disallow {
        track: bool,
        via: bool,
    },
    DiffPairGap {
        min_nm: Option<i64>,
        max_nm: Option<i64>,
    },
    DiffPairUncoupled {
        min_nm: Option<i64>,
        max_nm: i64,
    },
    Length {
        min_nm: Option<i64>,
        max_nm: Option<i64>,
        min_as: Option<i64>,
        max_as: Option<i64>,
    },
    Skew {
        min_nm: Option<i64>,
        max_nm: Option<i64>,
        min_as: Option<i64>,
        max_as: Option<i64>,
        within_diff_pairs: bool,
    },
}

impl DruConstraint {
    fn is_binary(&self) -> bool {
        matches!(
            self,
            Self::Clearance { .. } | Self::HoleClearance { .. } | Self::HoleToHole { .. }
        )
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
pub struct DruRule {
    pub name: String,
    pub layer: Option<String>,
    pub condition: Option<DruExpr>,
    pub constraint: DruConstraint,
    pub severity: DruSeverity,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum ExprTok {
    LParen,
    RParen,
    Comma,
    AndAnd,
    OrOr,
    Not,
    EqEq,
    NotEq,
    Str(String),
    Ident(String),
    Int(i64),
}

fn tokenize_dru_expr(s: &str) -> Result<Vec<ExprTok>, Failure> {
    let chars: Vec<char> = s.chars().collect();
    let mut i = 0usize;
    let mut toks = Vec::new();
    while i < chars.len() {
        let ch = chars[i];
        if ch.is_whitespace() {
            i += 1;
            continue;
        }
        match ch {
            '(' => {
                toks.push(ExprTok::LParen);
                i += 1;
            }
            ')' => {
                toks.push(ExprTok::RParen);
                i += 1;
            }
            ',' => {
                toks.push(ExprTok::Comma);
                i += 1;
            }
            '&' if chars.get(i + 1) == Some(&'&') => {
                toks.push(ExprTok::AndAnd);
                i += 2;
            }
            '|' if chars.get(i + 1) == Some(&'|') => {
                toks.push(ExprTok::OrOr);
                i += 2;
            }
            '=' if chars.get(i + 1) == Some(&'=') => {
                toks.push(ExprTok::EqEq);
                i += 2;
            }
            '!' if chars.get(i + 1) == Some(&'=') => {
                toks.push(ExprTok::NotEq);
                i += 2;
            }
            '!' => {
                toks.push(ExprTok::Not);
                i += 1;
            }
            '\'' | '"' => {
                let quote = ch;
                i += 1;
                let mut out = String::new();
                let mut closed = false;
                while i < chars.len() {
                    let c = chars[i];
                    if c == quote {
                        i += 1;
                        closed = true;
                        break;
                    }
                    if c == '\\' {
                        i += 1;
                        let esc = *chars.get(i).ok_or_else(|| {
                            Failure::new(
                                Status::Invalid,
                                "unterminated escape in .kicad_dru condition",
                            )
                        })?;
                        out.push(esc);
                        i += 1;
                    } else {
                        out.push(c);
                        i += 1;
                    }
                }
                if !closed {
                    return Err(Failure::new(
                        Status::Invalid,
                        "unterminated string in .kicad_dru condition",
                    ));
                }
                toks.push(ExprTok::Str(out));
            }
            c if c.is_ascii_digit() => {
                let start = i;
                while i < chars.len() && chars[i].is_ascii_digit() {
                    i += 1;
                }
                if i < chars.len() && (chars[i].is_ascii_alphabetic() || chars[i] == '.') {
                    return Err(Failure::new(
                        Status::Unsupported,
                        "unsupported numeric/unit literal in .kicad_dru condition",
                    ));
                }
                let num_str: String = chars[start..i].iter().collect();
                let v: i64 = num_str.parse().map_err(|_| {
                    Failure::new(Status::Invalid, "invalid integer in .kicad_dru condition")
                })?;
                toks.push(ExprTok::Int(v));
            }
            c if c.is_ascii_alphabetic() || c == '_' => {
                let start = i;
                while i < chars.len()
                    && (chars[i].is_ascii_alphanumeric() || chars[i] == '_' || chars[i] == '.')
                {
                    i += 1;
                }
                let ident: String = chars[start..i].iter().collect();
                toks.push(ExprTok::Ident(ident));
            }
            other => {
                return Err(Failure::new(
                    Status::Unsupported,
                    format!("unsupported operator/token '{other}' in .kicad_dru condition"),
                ));
            }
        }
    }
    Ok(toks)
}

struct ExprParser<'a> {
    toks: &'a [ExprTok],
    pos: usize,
}

impl<'a> ExprParser<'a> {
    fn peek(&self) -> Option<&ExprTok> {
        self.toks.get(self.pos)
    }

    fn next(&mut self) -> Option<&ExprTok> {
        let t = self.toks.get(self.pos)?;
        self.pos += 1;
        Some(t)
    }

    fn parse_or(&mut self, depth: usize) -> Result<DruExpr, Failure> {
        if depth > MAX_DRU_EXPR_DEPTH {
            return Err(Failure::new(
                Status::Unsupported,
                ".kicad_dru condition exceeds nesting depth",
            ));
        }
        let mut lhs = self.parse_and(depth)?;
        while self.peek() == Some(&ExprTok::OrOr) {
            self.next();
            let rhs = self.parse_and(depth)?;
            lhs = DruExpr::Or(Box::new(lhs), Box::new(rhs));
        }
        Ok(lhs)
    }

    fn parse_and(&mut self, depth: usize) -> Result<DruExpr, Failure> {
        let mut lhs = self.parse_unary(depth)?;
        while self.peek() == Some(&ExprTok::AndAnd) {
            self.next();
            let rhs = self.parse_unary(depth)?;
            lhs = DruExpr::And(Box::new(lhs), Box::new(rhs));
        }
        Ok(lhs)
    }

    fn parse_unary(&mut self, depth: usize) -> Result<DruExpr, Failure> {
        if self.peek() == Some(&ExprTok::Not) {
            self.next();
            let inner = self.parse_unary(depth + 1)?;
            return Ok(DruExpr::Not(Box::new(inner)));
        }
        self.parse_primary(depth)
    }

    fn parse_primary(&mut self, depth: usize) -> Result<DruExpr, Failure> {
        if self.peek() == Some(&ExprTok::LParen) {
            self.next();
            let inner = self.parse_or(depth + 1)?;
            if self.next() != Some(&ExprTok::RParen) {
                return Err(Failure::new(
                    Status::Invalid,
                    "unclosed '(' in .kicad_dru condition",
                ));
            }
            return Ok(inner);
        }
        let lhs_val = self.parse_value_or_bool()?;
        match lhs_val {
            Ok(val) => {
                let op = self.next().ok_or_else(|| {
                    Failure::new(
                        Status::Unsupported,
                        "expected comparison operator in .kicad_dru condition",
                    )
                })?;
                let is_eq = match op {
                    ExprTok::EqEq => true,
                    ExprTok::NotEq => false,
                    _ => {
                        return Err(Failure::new(
                            Status::Unsupported,
                            "unsupported comparison operator in .kicad_dru condition",
                        ));
                    }
                };
                let rhs = match self.parse_value_or_bool()? {
                    Ok(v) => v,
                    Err(_) => {
                        return Err(Failure::new(
                            Status::Unsupported,
                            "cannot compare boolean predicate in .kicad_dru condition",
                        ));
                    }
                };
                Ok(if is_eq {
                    DruExpr::Eq(val, rhs)
                } else {
                    DruExpr::Ne(val, rhs)
                })
            }
            Err(bool_expr) => Ok(bool_expr),
        }
    }

    fn parse_value_or_bool(&mut self) -> Result<Result<DruValueExpr, DruExpr>, Failure> {
        let tok = self
            .next()
            .ok_or_else(|| {
                Failure::new(
                    Status::Invalid,
                    "unexpected end of .kicad_dru condition expression",
                )
            })?
            .clone();
        match tok {
            ExprTok::Str(s) => Ok(Ok(DruValueExpr::StrLit(s))),
            ExprTok::Int(v) => Ok(Ok(DruValueExpr::IntLit(v))),
            ExprTok::Ident(ident) => {
                if self.peek() == Some(&ExprTok::LParen) {
                    self.next(); // '('
                    let mut fn_args: Vec<String> = Vec::new();
                    if self.peek() != Some(&ExprTok::RParen) {
                        loop {
                            match self.peek() {
                                Some(ExprTok::Str(s)) => {
                                    fn_args.push(s.clone());
                                    self.next();
                                }
                                _ => {
                                    return Err(Failure::new(
                                        Status::Unsupported,
                                        format!(
                                            "unsupported function call argument in .kicad_dru: {ident}"
                                        ),
                                    ));
                                }
                            }
                            if self.peek() == Some(&ExprTok::Comma) {
                                self.next();
                                continue;
                            }
                            break;
                        }
                    }
                    if self.next() != Some(&ExprTok::RParen) {
                        return Err(Failure::new(
                            Status::Unsupported,
                            format!("unsupported function call syntax in .kicad_dru: {ident}"),
                        ));
                    }
                    return match ident.as_str() {
                        "A.existsOnLayer" if fn_args.len() == 1 => {
                            Ok(Err(DruExpr::ExistsOnLayer {
                                side: Side::A,
                                layer: fn_args.pop().unwrap(),
                            }))
                        }
                        "B.existsOnLayer" if fn_args.len() == 1 => {
                            Ok(Err(DruExpr::ExistsOnLayer {
                                side: Side::B,
                                layer: fn_args.pop().unwrap(),
                            }))
                        }
                        "A.hasNetClass" if fn_args.len() == 1 => Ok(Err(DruExpr::HasNetClass {
                            side: Side::A,
                            class_name: fn_args.pop().unwrap(),
                        })),
                        "B.hasNetClass" if fn_args.len() == 1 => Ok(Err(DruExpr::HasNetClass {
                            side: Side::B,
                            class_name: fn_args.pop().unwrap(),
                        })),
                        "A.inDiffPair" if fn_args.len() == 1 => Ok(Err(DruExpr::InDiffPair {
                            side: Side::A,
                            pattern: fn_args.pop().unwrap(),
                        })),
                        "B.inDiffPair" if fn_args.len() == 1 => Ok(Err(DruExpr::InDiffPair {
                            side: Side::B,
                            pattern: fn_args.pop().unwrap(),
                        })),
                        "A.fromTo" if fn_args.len() == 2 => {
                            let b = fn_args.pop().unwrap();
                            let a = fn_args.pop().unwrap();
                            Ok(Err(DruExpr::FromTo {
                                side: Side::A,
                                a,
                                b,
                            }))
                        }
                        "B.fromTo" if fn_args.len() == 2 => {
                            let b = fn_args.pop().unwrap();
                            let a = fn_args.pop().unwrap();
                            Ok(Err(DruExpr::FromTo {
                                side: Side::B,
                                a,
                                b,
                            }))
                        }
                        _ => Err(Failure::new(
                            Status::Unsupported,
                            format!("unsupported .kicad_dru condition function: {ident}"),
                        )),
                    };
                }
                match ident.as_str() {
                    "true" => Ok(Err(DruExpr::BoolLit(true))),
                    "false" => Ok(Err(DruExpr::BoolLit(false))),
                    "AB.sameNet" => Ok(Err(DruExpr::SameNet)),
                    "A.NetClass" => Ok(Ok(DruValueExpr::Prop(Side::A, DruProp::NetClass))),
                    "B.NetClass" => Ok(Ok(DruValueExpr::Prop(Side::B, DruProp::NetClass))),
                    "A.NetName" => Ok(Ok(DruValueExpr::Prop(Side::A, DruProp::NetName))),
                    "B.NetName" => Ok(Ok(DruValueExpr::Prop(Side::B, DruProp::NetName))),
                    "A.Net" => Ok(Ok(DruValueExpr::Prop(Side::A, DruProp::Net))),
                    "B.Net" => Ok(Ok(DruValueExpr::Prop(Side::B, DruProp::Net))),
                    "A.Type" => Ok(Ok(DruValueExpr::Prop(Side::A, DruProp::Type))),
                    "B.Type" => Ok(Ok(DruValueExpr::Prop(Side::B, DruProp::Type))),
                    "A.Layer" => Ok(Ok(DruValueExpr::Prop(Side::A, DruProp::Layer))),
                    "B.Layer" => Ok(Ok(DruValueExpr::Prop(Side::B, DruProp::Layer))),
                    _ => Err(Failure::new(
                        Status::Unsupported,
                        format!("unsupported .kicad_dru condition identifier: {ident}"),
                    )),
                }
            }
            _ => Err(Failure::new(
                Status::Invalid,
                "unexpected token in .kicad_dru condition",
            )),
        }
    }
}

fn parse_dru_condition(s: &str) -> Result<DruExpr, Failure> {
    let toks = tokenize_dru_expr(s)?;
    if toks.is_empty() {
        return Err(Failure::new(
            Status::Invalid,
            "empty .kicad_dru condition expression",
        ));
    }
    let mut parser = ExprParser {
        toks: &toks,
        pos: 0,
    };
    let expr = parser.parse_or(0)?;
    if parser.pos != toks.len() {
        return Err(Failure::new(
            Status::Unsupported,
            "unexpected trailing tokens in .kicad_dru condition",
        ));
    }
    Ok(expr)
}

fn sexpr_str(s: &Sexpr) -> Option<&str> {
    match s {
        Sexpr::Atom(a) | Sexpr::Str(a) => Some(a.as_str()),
        Sexpr::List(_) => None,
    }
}

fn parse_min_max_dims(
    args: &[Sexpr],
    allow_max: bool,
    kind: &str,
) -> Result<(Option<i64>, Option<i64>), Failure> {
    let mut min_nm = None;
    let mut max_nm = None;
    for item in args {
        let Sexpr::List(pair) = item else {
            return Err(Failure::new(
                Status::Unsupported,
                format!("unsupported .kicad_dru {kind} argument"),
            ));
        };
        if pair.len() != 2 {
            return Err(Failure::new(
                Status::Unsupported,
                format!("unsupported .kicad_dru {kind} subclause"),
            ));
        }
        let key = sexpr_str(&pair[0]).ok_or_else(|| {
            Failure::new(Status::Invalid, "expected keyword in .kicad_dru constraint")
        })?;
        let val_tok = sexpr_str(&pair[1]).ok_or_else(|| {
            Failure::new(
                Status::Invalid,
                "expected dimension in .kicad_dru constraint",
            )
        })?;
        match key {
            "min" => {
                if min_nm.is_some() {
                    return Err(Failure::new(
                        Status::Invalid,
                        "duplicate min in .kicad_dru constraint",
                    ));
                }
                min_nm = Some(parse_dru_dim_nm(val_tok)?);
            }
            "max" if allow_max => {
                if max_nm.is_some() {
                    return Err(Failure::new(
                        Status::Invalid,
                        "duplicate max in .kicad_dru constraint",
                    ));
                }
                max_nm = Some(parse_dru_dim_nm(val_tok)?);
            }
            _ => {
                return Err(Failure::new(
                    Status::Unsupported,
                    format!("unsupported .kicad_dru {kind} bound '{key}'"),
                ));
            }
        }
    }
    if min_nm.is_none() && (!allow_max || max_nm.is_none()) {
        return Err(Failure::new(
            Status::Unsupported,
            format!(".kicad_dru {kind} constraint missing required bound"),
        ));
    }
    if let (Some(lo), Some(hi)) = (min_nm, max_nm) {
        if lo > hi {
            return Err(Failure::new(
                Status::Invalid,
                format!(".kicad_dru {kind} min exceeds max"),
            ));
        }
    }
    Ok((min_nm, max_nm))
}

#[allow(clippy::type_complexity)]
fn parse_length_or_delay_bounds(
    args: &[Sexpr],
    allow_within_diff_pairs: bool,
    kind: &str,
) -> Result<(Option<i64>, Option<i64>, Option<i64>, Option<i64>, bool), Failure> {
    let mut min_nm: Option<i64> = None;
    let mut max_nm: Option<i64> = None;
    let mut min_as: Option<i64> = None;
    let mut max_as: Option<i64> = None;
    let mut within_diff_pairs = false;

    for item in args {
        if allow_within_diff_pairs {
            if let Sexpr::Atom(a) = item {
                if a == "within_diff_pairs" {
                    within_diff_pairs = true;
                    continue;
                }
            }
            if let Sexpr::List(list) = item {
                if list.len() == 1 && sexpr_str(&list[0]) == Some("within_diff_pairs") {
                    within_diff_pairs = true;
                    continue;
                }
            }
        }
        let Sexpr::List(pair) = item else {
            return Err(Failure::new(
                Status::Unsupported,
                format!("unsupported .kicad_dru {kind} argument"),
            ));
        };
        if pair.len() != 2 {
            return Err(Failure::new(
                Status::Unsupported,
                format!("unsupported .kicad_dru {kind} subclause"),
            ));
        }
        let key = sexpr_str(&pair[0]).ok_or_else(|| {
            Failure::new(Status::Invalid, "expected keyword in .kicad_dru constraint")
        })?;
        let val_tok = sexpr_str(&pair[1]).ok_or_else(|| {
            Failure::new(
                Status::Invalid,
                "expected dimension in .kicad_dru constraint",
            )
        })?;
        let parsed = parse_dru_length_or_delay(val_tok)?;
        match key {
            "min" => {
                if min_nm.is_some() || min_as.is_some() {
                    return Err(Failure::new(
                        Status::Invalid,
                        "duplicate min in .kicad_dru constraint",
                    ));
                }
                match parsed {
                    DruLengthOrDelay::LengthNm(v) => min_nm = Some(v),
                    DruLengthOrDelay::DelayAs(v) => min_as = Some(v),
                }
            }
            "max" => {
                if max_nm.is_some() || max_as.is_some() {
                    return Err(Failure::new(
                        Status::Invalid,
                        "duplicate max in .kicad_dru constraint",
                    ));
                }
                match parsed {
                    DruLengthOrDelay::LengthNm(v) => max_nm = Some(v),
                    DruLengthOrDelay::DelayAs(v) => max_as = Some(v),
                }
            }
            _ => {
                return Err(Failure::new(
                    Status::Unsupported,
                    format!("unsupported .kicad_dru {kind} bound '{key}'"),
                ));
            }
        }
    }
    if min_nm.is_none() && max_nm.is_none() && min_as.is_none() && max_as.is_none() {
        return Err(Failure::new(
            Status::Unsupported,
            format!(".kicad_dru {kind} constraint missing required bound"),
        ));
    }
    if (min_nm.is_some() || max_nm.is_some()) && (min_as.is_some() || max_as.is_some()) {
        return Err(Failure::new(
            Status::Invalid,
            format!(".kicad_dru {kind} cannot mix length and time bounds"),
        ));
    }
    if let (Some(lo), Some(hi)) = (min_nm, max_nm) {
        if lo > hi {
            return Err(Failure::new(
                Status::Invalid,
                format!(".kicad_dru {kind} min exceeds max"),
            ));
        }
    }
    if let (Some(lo), Some(hi)) = (min_as, max_as) {
        if lo > hi {
            return Err(Failure::new(
                Status::Invalid,
                format!(".kicad_dru {kind} min exceeds max"),
            ));
        }
    }
    Ok((min_nm, max_nm, min_as, max_as, within_diff_pairs))
}

fn parse_dru_constraint(items: &[Sexpr]) -> Result<DruConstraint, Failure> {
    if items.len() < 2 {
        return Err(Failure::new(
            Status::Invalid,
            "malformed .kicad_dru constraint",
        ));
    }
    let kind = sexpr_str(&items[1])
        .ok_or_else(|| Failure::new(Status::Invalid, "expected constraint kind in .kicad_dru"))?;
    let args = &items[2..];
    match kind {
        "clearance" => {
            let (min_nm, _) = parse_min_max_dims(args, false, kind)?;
            Ok(DruConstraint::Clearance {
                min_nm: min_nm.unwrap(),
            })
        }
        "hole_clearance" => {
            let (min_nm, _) = parse_min_max_dims(args, false, kind)?;
            Ok(DruConstraint::HoleClearance {
                min_nm: min_nm.unwrap(),
            })
        }
        "hole_to_hole" => {
            let (min_nm, _) = parse_min_max_dims(args, false, kind)?;
            Ok(DruConstraint::HoleToHole {
                min_nm: min_nm.unwrap(),
            })
        }
        "edge_clearance" => {
            let (min_nm, _) = parse_min_max_dims(args, false, kind)?;
            Ok(DruConstraint::EdgeClearance {
                min_nm: min_nm.unwrap(),
            })
        }
        "annular_width" => {
            let (min_nm, _) = parse_min_max_dims(args, false, kind)?;
            Ok(DruConstraint::AnnularWidth {
                min_nm: min_nm.unwrap(),
            })
        }
        "track_width" => {
            let (min_nm, max_nm) = parse_min_max_dims(args, true, kind)?;
            Ok(DruConstraint::TrackWidth {
                min_nm: min_nm.unwrap_or(0),
                max_nm,
            })
        }
        "via_diameter" => {
            let (min_nm, max_nm) = parse_min_max_dims(args, true, kind)?;
            Ok(DruConstraint::ViaDiameter {
                min_nm: min_nm.unwrap_or(0),
                max_nm,
            })
        }
        "via_drill" => {
            let (min_nm, max_nm) = parse_min_max_dims(args, true, kind)?;
            Ok(DruConstraint::ViaDrill {
                min_nm: min_nm.unwrap_or(0),
                max_nm,
            })
        }
        "via_count" => {
            if args.len() != 1 {
                return Err(Failure::new(
                    Status::Unsupported,
                    "unsupported via_count arguments",
                ));
            }
            let Sexpr::List(pair) = &args[0] else {
                return Err(Failure::new(
                    Status::Unsupported,
                    "unsupported via_count argument",
                ));
            };
            if pair.len() != 2 || sexpr_str(&pair[0]) != Some("max") {
                return Err(Failure::new(
                    Status::Unsupported,
                    "via_count only supports (max N)",
                ));
            }
            let n_str = sexpr_str(&pair[1])
                .ok_or_else(|| Failure::new(Status::Invalid, "via_count max must be an integer"))?;
            let max: u32 = n_str.parse().map_err(|_| {
                Failure::new(
                    Status::Invalid,
                    "via_count max must be a nonnegative integer",
                )
            })?;
            Ok(DruConstraint::ViaCount { max })
        }
        "disallow" => {
            if args.is_empty() {
                return Err(Failure::new(
                    Status::Invalid,
                    "empty disallow constraint in .kicad_dru",
                ));
            }
            let mut track = false;
            let mut via = false;
            for a in args {
                match sexpr_str(a) {
                    Some("track") => track = true,
                    Some("via") => via = true,
                    _ => {
                        return Err(Failure::new(
                            Status::Unsupported,
                            "unsupported disallow target in .kicad_dru (only track and via are supported)",
                        ));
                    }
                }
            }
            Ok(DruConstraint::Disallow { track, via })
        }
        "diff_pair_gap" => {
            let (min_nm, max_nm) = parse_min_max_dims(args, true, kind)?;
            Ok(DruConstraint::DiffPairGap { min_nm, max_nm })
        }
        "diff_pair_uncoupled" => {
            let (min_nm, max_nm) = parse_min_max_dims(args, true, kind)?;
            let max_nm = max_nm.ok_or_else(|| {
                Failure::new(
                    Status::Unsupported,
                    ".kicad_dru diff_pair_uncoupled constraint missing max bound",
                )
            })?;
            Ok(DruConstraint::DiffPairUncoupled { min_nm, max_nm })
        }
        "length" => {
            let (min_nm, max_nm, min_as, max_as, _) =
                parse_length_or_delay_bounds(args, false, kind)?;
            Ok(DruConstraint::Length {
                min_nm,
                max_nm,
                min_as,
                max_as,
            })
        }
        "skew" => {
            let (min_nm, max_nm, min_as, max_as, within_diff_pairs) =
                parse_length_or_delay_bounds(args, true, kind)?;
            Ok(DruConstraint::Skew {
                min_nm,
                max_nm,
                min_as,
                max_as,
                within_diff_pairs,
            })
        }
        other => Err(Failure::new(
            Status::Unsupported,
            format!("unsupported .kicad_dru constraint '{other}'"),
        )),
    }
}

pub fn parse_kicad_dru(bytes: &[u8]) -> Result<Vec<DruRule>, Failure> {
    let text = std::str::from_utf8(bytes).map_err(|e| {
        Failure::new(
            Status::Invalid,
            format!(".kicad_dru is not valid UTF-8: {e}"),
        )
    })?;
    let forms = parse_sexprs(text)?;
    if forms.is_empty() {
        return Err(Failure::new(
            Status::Invalid,
            ".kicad_dru is empty (missing (version 1))",
        ));
    }
    let Sexpr::List(ver) = &forms[0] else {
        return Err(Failure::new(
            Status::Invalid,
            "expected (version 1) header in .kicad_dru",
        ));
    };
    if ver.len() != 2 || sexpr_str(&ver[0]) != Some("version") || sexpr_str(&ver[1]) != Some("1") {
        return Err(Failure::new(
            Status::Unsupported,
            ".kicad_dru requires (version 1) header",
        ));
    }

    let mut rules = Vec::new();
    for form in &forms[1..] {
        let Sexpr::List(items) = form else {
            return Err(Failure::new(
                Status::Invalid,
                "expected (rule ...) form in .kicad_dru",
            ));
        };
        if items.len() < 3 || sexpr_str(&items[0]) != Some("rule") {
            return Err(Failure::new(
                Status::Unsupported,
                "unsupported top-level form in .kicad_dru",
            ));
        }
        let name = sexpr_str(&items[1])
            .ok_or_else(|| Failure::new(Status::Invalid, ".kicad_dru rule missing name"))?
            .to_owned();
        let mut layer: Option<String> = None;
        let mut condition: Option<DruExpr> = None;
        let mut constraint: Option<DruConstraint> = None;
        let mut severity: Option<DruSeverity> = None;

        for clause in &items[2..] {
            let Sexpr::List(cl) = clause else {
                return Err(Failure::new(
                    Status::Invalid,
                    "malformed clause in .kicad_dru rule",
                ));
            };
            if cl.is_empty() {
                return Err(Failure::new(
                    Status::Invalid,
                    "empty clause in .kicad_dru rule",
                ));
            }
            match sexpr_str(&cl[0]) {
                Some("layer") => {
                    if layer.is_some() || cl.len() != 2 {
                        return Err(Failure::new(
                            Status::Unsupported,
                            "invalid or duplicate layer clause in .kicad_dru",
                        ));
                    }
                    let l = sexpr_str(&cl[1]).ok_or_else(|| {
                        Failure::new(Status::Invalid, "expected layer string in .kicad_dru")
                    })?;
                    layer = Some(l.to_owned());
                }
                Some("condition") => {
                    if condition.is_some() || cl.len() != 2 {
                        return Err(Failure::new(
                            Status::Unsupported,
                            "invalid or duplicate condition clause in .kicad_dru",
                        ));
                    }
                    let cond_str = sexpr_str(&cl[1]).ok_or_else(|| {
                        Failure::new(Status::Invalid, "expected condition string in .kicad_dru")
                    })?;
                    condition = Some(parse_dru_condition(cond_str)?);
                }
                Some("severity") => {
                    if severity.is_some() || cl.len() != 2 {
                        return Err(Failure::new(
                            Status::Unsupported,
                            "invalid or duplicate severity clause in .kicad_dru",
                        ));
                    }
                    let sev = match sexpr_str(&cl[1]) {
                        Some("error") => DruSeverity::Error,
                        Some("warning") => DruSeverity::Warning,
                        Some("ignore") => DruSeverity::Ignore,
                        _ => {
                            return Err(Failure::new(
                                Status::Unsupported,
                                "unsupported severity in .kicad_dru",
                            ));
                        }
                    };
                    severity = Some(sev);
                }
                Some("constraint") => {
                    if constraint.is_some() {
                        return Err(Failure::new(
                            Status::Unsupported,
                            "multiple constraints in single .kicad_dru rule",
                        ));
                    }
                    constraint = Some(parse_dru_constraint(cl)?);
                }
                Some(other) => {
                    return Err(Failure::new(
                        Status::Unsupported,
                        format!("unsupported clause '{other}' in .kicad_dru rule"),
                    ));
                }
                None => {
                    return Err(Failure::new(
                        Status::Invalid,
                        "malformed clause head in .kicad_dru",
                    ));
                }
            }
        }
        let constraint = constraint.ok_or_else(|| {
            Failure::new(
                Status::Invalid,
                format!(".kicad_dru rule '{name}' missing constraint"),
            )
        })?;
        if !constraint.is_binary() && condition.as_ref().is_some_and(DruExpr::uses_b_or_ab) {
            return Err(Failure::new(
                Status::Unsupported,
                format!(".kicad_dru unary rule '{name}' cannot reference B or AB"),
            ));
        }
        rules.push(DruRule {
            name,
            layer,
            condition,
            constraint,
            severity: severity.unwrap_or(DruSeverity::Error),
        });
    }
    Ok(rules)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DruItemKind {
    Track,
    Via,
    Pad,
    Hole,
    Graphic,
    Zone,
}

impl DruItemKind {
    fn as_kicad_str(self) -> &'static str {
        match self {
            Self::Track => "Track",
            Self::Via => "Via",
            Self::Pad => "Pad",
            Self::Hole => "Hole",
            Self::Graphic => "Graphic",
            Self::Zone => "Zone",
        }
    }
}

pub struct DruItem<'a> {
    pub net_id: u32,
    pub net_name: &'a str,
    pub net_class: &'a str,
    pub class_members: &'a [String],
    pub kind: DruItemKind,
    pub layers: &'a [String],
}

fn layer_matches_spec(layer: &str, spec: &str) -> bool {
    match spec {
        "*" => true,
        "outer" => layer == "F.Cu" || layer == "B.Cu",
        "inner" => layer != "F.Cu" && layer != "B.Cu",
        exact => layer == exact,
    }
}

fn eval_value_eq(
    val_a: &DruValueExpr,
    val_b: &DruValueExpr,
    a: &DruItem<'_>,
    b: Option<&DruItem<'_>>,
) -> bool {
    let pick = |side: Side| -> Option<&DruItem<'_>> {
        match side {
            Side::A => Some(a),
            Side::B => b,
        }
    };
    match (val_a, val_b) {
        (DruValueExpr::StrLit(x), DruValueExpr::StrLit(y)) => x == y,
        (DruValueExpr::IntLit(x), DruValueExpr::IntLit(y)) => x == y,
        (DruValueExpr::Prop(s, DruProp::Layer), DruValueExpr::StrLit(l))
        | (DruValueExpr::StrLit(l), DruValueExpr::Prop(s, DruProp::Layer)) => {
            pick(*s).is_some_and(|it| it.layers.iter().any(|il| layer_matches_spec(il, l)))
        }
        (DruValueExpr::Prop(s1, DruProp::Layer), DruValueExpr::Prop(s2, DruProp::Layer)) => {
            match (pick(*s1), pick(*s2)) {
                (Some(i1), Some(i2)) => i1.layers.iter().any(|l| i2.layers.contains(l)),
                _ => false,
            }
        }
        (DruValueExpr::Prop(s, DruProp::Net), DruValueExpr::IntLit(n))
        | (DruValueExpr::IntLit(n), DruValueExpr::Prop(s, DruProp::Net)) => {
            pick(*s).is_some_and(|it| i64::from(it.net_id) == *n)
        }
        (DruValueExpr::Prop(s1, DruProp::Net), DruValueExpr::Prop(s2, DruProp::Net)) => {
            match (pick(*s1), pick(*s2)) {
                (Some(i1), Some(i2)) => i1.net_id == i2.net_id,
                _ => false,
            }
        }
        (DruValueExpr::Prop(s, p), DruValueExpr::StrLit(lit))
        | (DruValueExpr::StrLit(lit), DruValueExpr::Prop(s, p)) => {
            let Some(it) = pick(*s) else { return false };
            match p {
                DruProp::NetClass => it.net_class == lit,
                DruProp::NetName => it.net_name == lit,
                DruProp::Type => it.kind.as_kicad_str() == lit,
                DruProp::Net => it.net_name == lit,
                DruProp::Layer => unreachable!(),
            }
        }
        (DruValueExpr::Prop(s1, p1), DruValueExpr::Prop(s2, p2)) if p1 == p2 => {
            match (pick(*s1), pick(*s2)) {
                (Some(i1), Some(i2)) => match p1 {
                    DruProp::NetClass => i1.net_class == i2.net_class,
                    DruProp::NetName => i1.net_name == i2.net_name,
                    DruProp::Type => i1.kind == i2.kind,
                    DruProp::Net | DruProp::Layer => unreachable!(),
                },
                _ => false,
            }
        }
        _ => false,
    }
}

fn eval_dru_expr(expr: &DruExpr, a: &DruItem<'_>, b: Option<&DruItem<'_>>) -> bool {
    eval_dru_expr_for_context(expr, a, b, None, None)
}

pub fn eval_dru_expr_for_context(
    expr: &DruExpr,
    a: &DruItem<'_>,
    b: Option<&DruItem<'_>>,
    in_diff_pair_stem: Option<&str>,
    fromto_tokens: Option<&[String]>,
) -> bool {
    match expr {
        DruExpr::BoolLit(v) => *v,
        DruExpr::Not(x) => !eval_dru_expr_for_context(x, a, b, in_diff_pair_stem, fromto_tokens),
        DruExpr::And(x, y) => {
            eval_dru_expr_for_context(x, a, b, in_diff_pair_stem, fromto_tokens)
                && eval_dru_expr_for_context(y, a, b, in_diff_pair_stem, fromto_tokens)
        }
        DruExpr::Or(x, y) => {
            eval_dru_expr_for_context(x, a, b, in_diff_pair_stem, fromto_tokens)
                || eval_dru_expr_for_context(y, a, b, in_diff_pair_stem, fromto_tokens)
        }
        DruExpr::Eq(l, r) => eval_value_eq(l, r, a, b),
        DruExpr::Ne(l, r) => !eval_value_eq(l, r, a, b),
        DruExpr::SameNet => b.is_some_and(|ib| a.net_id == ib.net_id),
        DruExpr::ExistsOnLayer { side, layer } => {
            let it = match side {
                Side::A => Some(a),
                Side::B => b,
            };
            it.is_some_and(|item| item.layers.iter().any(|l| layer_matches_spec(l, layer)))
        }
        DruExpr::HasNetClass { side, class_name } => {
            let it = match side {
                Side::A => Some(a),
                Side::B => b,
            };
            it.is_some_and(|item| {
                item.net_class == class_name || item.class_members.iter().any(|c| c == class_name)
            })
        }
        DruExpr::InDiffPair { side, pattern } => {
            if *side != Side::A {
                return false;
            }
            let Some(stem) = in_diff_pair_stem else {
                return false;
            };
            wildcard_full_match(pattern, stem)
                || stem
                    .strip_suffix('_')
                    .is_some_and(|s| wildcard_full_match(pattern, s))
        }
        DruExpr::FromTo {
            side,
            a: ft_a,
            b: ft_b,
        } => {
            if *side != Side::A {
                return false;
            }
            let Some(tokens) = fromto_tokens else {
                return false;
            };
            ft_a != ft_b && tokens.iter().any(|t| t == ft_a) && tokens.iter().any(|t| t == ft_b)
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NetResolvedRules {
    pub net_id: u32,
    pub net_name: String,
    pub class_name: String,
    pub class_members: Vec<String>,
    pub width_nm: i64,
    pub clearance_nm: i64,
    pub via_diameter_nm: i64,
    pub via_drill_nm: i64,
    pub microvia_diameter_nm: i64,
    pub microvia_drill_nm: i64,
    pub edge_clearance_nm: i64,
    pub hole_clearance_nm: i64,
    pub hole_to_hole_nm: i64,
    pub annular_width_nm: i64,
    pub max_via_count: Option<u32>,
    #[serde(default)]
    pub diff_pair_width_nm: i64,
    #[serde(default)]
    pub diff_pair_gap_nm: i64,
    #[serde(default)]
    pub diff_pair_via_gap_nm: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tuning_profile: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EffectiveRules {
    pub minimums: BoardMinimums,
    pub net_rules: BTreeMap<u32, NetResolvedRules>,
    pub dru_rules: Vec<DruRule>,
    pub allow_blind_buried_vias: bool,
    pub allow_microvias: bool,
    pub via_cost: u64,
    pub max_pair_clearance_nm: i64,
    pub max_hole_clearance_nm: i64,
    pub max_hole_to_hole_nm: i64,
    pub max_edge_clearance_nm: i64,
    pub max_track_width_nm: i64,
    pub max_via_diameter_nm: i64,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tuning_profiles: Vec<TuningProfile>,
    #[serde(default)]
    pub use_height_for_length_calcs: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub board_thickness_nm: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outer_solder_mask_thickness_nm: Option<i64>,
}

impl EffectiveRules {
    pub fn from_uniform_rules(rules: Rules, nets: &[Net]) -> Self {
        let mut net_rules = BTreeMap::new();
        let make_net = |id: u32, name: String| NetResolvedRules {
            net_id: id,
            net_name: name,
            class_name: "Default".to_owned(),
            class_members: vec!["Default".to_owned()],
            width_nm: rules.width_nm,
            clearance_nm: rules.clearance_nm,
            via_diameter_nm: (rules.width_nm * 2).max(600_000),
            via_drill_nm: rules.width_nm.max(300_000),
            microvia_diameter_nm: 300_000,
            microvia_drill_nm: 150_000,
            edge_clearance_nm: rules.edge_clearance_nm,
            hole_clearance_nm: rules.hole_clearance_nm,
            hole_to_hole_nm: 0,
            annular_width_nm: 0,
            max_via_count: None,
            diff_pair_width_nm: 0,
            diff_pair_gap_nm: 0,
            diff_pair_via_gap_nm: 0,
            tuning_profile: None,
        };
        net_rules.insert(0, make_net(0, String::new()));
        for n in nets {
            net_rules.insert(n.id, make_net(n.id, n.name.clone()));
        }
        Self {
            minimums: BoardMinimums::default(),
            net_rules,
            dru_rules: Vec::new(),
            allow_blind_buried_vias: false,
            allow_microvias: false,
            via_cost: 1,
            max_pair_clearance_nm: rules.clearance_nm,
            max_hole_clearance_nm: rules
                .hole_clearance_nm
                .max(rules.same_net_hole_clearance_nm),
            max_hole_to_hole_nm: 0,
            max_edge_clearance_nm: rules.edge_clearance_nm,
            max_track_width_nm: rules.width_nm,
            max_via_diameter_nm: (rules.width_nm * 2).max(600_000),
            tuning_profiles: Vec::new(),
            use_height_for_length_calcs: false,
            board_thickness_nm: None,
            outer_solder_mask_thickness_nm: None,
        }
    }

    pub fn validate(&self) -> Result<(), Failure> {
        if !self.net_rules.contains_key(&0) {
            return Err(Failure::new(
                Status::Invalid,
                "effective_rules must contain net 0 fallback",
            ));
        }
        if self
            .board_thickness_nm
            .is_some_and(|bt| !(1..=2 * MAX_NM).contains(&bt))
        {
            return Err(Failure::new(
                Status::Invalid,
                "effective_rules board_thickness_nm must be in 1..=200000000 nm",
            ));
        }
        if self
            .outer_solder_mask_thickness_nm
            .is_some_and(|m| !(0..=2 * MAX_NM).contains(&m))
        {
            return Err(Failure::new(
                Status::Invalid,
                "effective_rules outer_solder_mask_thickness_nm must be in 0..=200000000 nm",
            ));
        }
        if let (Some(bt), Some(mask)) =
            (self.board_thickness_nm, self.outer_solder_mask_thickness_nm)
        {
            if mask > bt {
                return Err(Failure::new(
                    Status::Invalid,
                    "effective_rules outer_solder_mask_thickness_nm exceeds board_thickness_nm",
                ));
            }
        }
        let m = &self.minimums;
        if [
            m.min_track_width_nm,
            m.min_clearance_nm,
            m.min_edge_clearance_nm,
            m.min_via_diameter_nm,
            m.min_via_annular_width_nm,
            m.min_through_hole_diameter_nm,
            m.min_hole_clearance_nm,
            m.min_hole_to_hole_nm,
            m.min_microvia_diameter_nm,
            m.min_microvia_drill_nm,
            m.solder_mask_expansion_nm,
            m.solder_mask_min_width_nm,
            m.solder_mask_to_copper_clearance_nm,
            self.max_pair_clearance_nm,
            self.max_hole_clearance_nm,
            self.max_hole_to_hole_nm,
            self.max_edge_clearance_nm,
            self.max_track_width_nm,
            self.max_via_diameter_nm,
        ]
        .iter()
        .any(|&v| !(0..=2 * MAX_NM).contains(&v))
        {
            return Err(Failure::new(
                Status::Invalid,
                "effective_rules minimums and maximums must be in 0..=200000000 nm",
            ));
        }
        let mut req_pair_clearance_nm = m.min_clearance_nm;
        let mut req_hole_clearance_nm = if m.hole_clearance_active {
            m.min_hole_clearance_nm
        } else {
            0
        };
        let mut req_hole_to_hole_nm = if m.hole_to_hole_active {
            m.min_hole_to_hole_nm
        } else {
            0
        };
        let mut req_edge_clearance_nm = m.min_edge_clearance_nm;
        let mut req_track_width_nm = m.min_track_width_nm;
        let mut req_via_diameter_nm = m.min_via_diameter_nm;
        let mut max_via_drill_nm = m.min_through_hole_diameter_nm;
        let mut max_via_annular_nm = m.min_via_annular_width_nm;
        if self.allow_microvias {
            req_via_diameter_nm = req_via_diameter_nm.max(m.min_microvia_diameter_nm);
            max_via_drill_nm = max_via_drill_nm.max(m.min_microvia_drill_nm);
        }

        for (&id, nr) in &self.net_rules {
            if nr.net_id != id
                || !(1..=MAX_NM).contains(&nr.width_nm)
                || !(1..=2 * MAX_NM).contains(&nr.via_drill_nm)
                || !(nr.via_drill_nm + 1..=2 * MAX_NM).contains(&nr.via_diameter_nm)
            {
                return Err(Failure::new(
                    Status::Invalid,
                    "effective_rules net entry has invalid id, track width, or via dimensions",
                ));
            }
            if self.allow_microvias
                && (!(1..=2 * MAX_NM).contains(&nr.microvia_drill_nm)
                    || !(nr.microvia_drill_nm + 1..=2 * MAX_NM).contains(&nr.microvia_diameter_nm))
            {
                return Err(Failure::new(
                    Status::Invalid,
                    "effective_rules net entry has invalid microvia dimensions",
                ));
            }
            if [
                nr.clearance_nm,
                nr.microvia_diameter_nm,
                nr.microvia_drill_nm,
                nr.edge_clearance_nm,
                nr.hole_clearance_nm,
                nr.hole_to_hole_nm,
                nr.annular_width_nm,
            ]
            .iter()
            .any(|&v| !(0..=2 * MAX_NM).contains(&v))
            {
                return Err(Failure::new(
                    Status::Invalid,
                    "effective_rules net entry values must be in 0..=200000000 nm",
                ));
            }
            req_pair_clearance_nm = req_pair_clearance_nm.max(nr.clearance_nm);
            req_hole_clearance_nm = req_hole_clearance_nm.max(nr.hole_clearance_nm);
            req_hole_to_hole_nm = req_hole_to_hole_nm.max(nr.hole_to_hole_nm);
            req_edge_clearance_nm = req_edge_clearance_nm.max(nr.edge_clearance_nm);
            req_track_width_nm = req_track_width_nm.max(nr.width_nm);
            req_via_diameter_nm = req_via_diameter_nm.max(nr.via_diameter_nm);
            max_via_drill_nm = max_via_drill_nm.max(nr.via_drill_nm);
            max_via_annular_nm = max_via_annular_nm.max(nr.annular_width_nm);
            if self.allow_microvias {
                req_via_diameter_nm = req_via_diameter_nm.max(nr.microvia_diameter_nm);
                max_via_drill_nm = max_via_drill_nm.max(nr.microvia_drill_nm);
            }
        }
        for rule in &self.dru_rules {
            let valid_dim = |v: i64| (0..=2 * MAX_NM).contains(&v);
            match rule.constraint {
                DruConstraint::Clearance { min_nm }
                | DruConstraint::HoleClearance { min_nm }
                | DruConstraint::HoleToHole { min_nm }
                | DruConstraint::EdgeClearance { min_nm }
                | DruConstraint::AnnularWidth { min_nm } => {
                    if !valid_dim(min_nm) {
                        return Err(Failure::new(
                            Status::Invalid,
                            "effective_rules dru_rules constraint dimension out of bounds",
                        ));
                    }
                }
                DruConstraint::TrackWidth { min_nm, max_nm }
                | DruConstraint::ViaDiameter { min_nm, max_nm }
                | DruConstraint::ViaDrill { min_nm, max_nm } => {
                    if !valid_dim(min_nm) || max_nm.is_some_and(|mx| !valid_dim(mx) || min_nm > mx)
                    {
                        return Err(Failure::new(
                            Status::Invalid,
                            "effective_rules dru_rules min/max constraint bounds are invalid",
                        ));
                    }
                }
                DruConstraint::ViaCount { .. } | DruConstraint::Disallow { .. } => {}
                DruConstraint::DiffPairGap { min_nm, max_nm } => {
                    if min_nm.is_some_and(|v| !valid_dim(v))
                        || max_nm.is_some_and(|v| !valid_dim(v))
                        || min_nm.zip(max_nm).is_some_and(|(lo, hi)| lo > hi)
                    {
                        return Err(Failure::new(
                            Status::Invalid,
                            "effective_rules dru_rules diff_pair_gap bounds are invalid",
                        ));
                    }
                }
                DruConstraint::DiffPairUncoupled { min_nm, max_nm } => {
                    if min_nm.is_some_and(|v| !valid_dim(v))
                        || !valid_dim(max_nm)
                        || min_nm.is_some_and(|lo| lo > max_nm)
                    {
                        return Err(Failure::new(
                            Status::Invalid,
                            "effective_rules dru_rules diff_pair_uncoupled bounds are invalid",
                        ));
                    }
                }
                DruConstraint::Length {
                    min_nm,
                    max_nm,
                    min_as,
                    max_as,
                }
                | DruConstraint::Skew {
                    min_nm,
                    max_nm,
                    min_as,
                    max_as,
                    ..
                } => {
                    if min_nm.is_some_and(|v| !valid_dim(v))
                        || max_nm.is_some_and(|v| !valid_dim(v))
                        || min_nm.zip(max_nm).is_some_and(|(lo, hi)| lo > hi)
                        || min_as.is_some_and(|v| v < 0)
                        || max_as.is_some_and(|v| v < 0)
                        || min_as.zip(max_as).is_some_and(|(lo, hi)| lo > hi)
                    {
                        return Err(Failure::new(
                            Status::Invalid,
                            "effective_rules dru_rules length/skew bounds are invalid",
                        ));
                    }
                }
            }
            if rule.severity == DruSeverity::Ignore {
                continue;
            }
            match rule.constraint {
                DruConstraint::Clearance { min_nm } => {
                    req_pair_clearance_nm = req_pair_clearance_nm.max(min_nm);
                }
                DruConstraint::HoleClearance { min_nm } => {
                    req_hole_clearance_nm = req_hole_clearance_nm.max(min_nm);
                }
                DruConstraint::HoleToHole { min_nm } => {
                    req_hole_to_hole_nm = req_hole_to_hole_nm.max(min_nm);
                }
                DruConstraint::EdgeClearance { min_nm } => {
                    req_edge_clearance_nm = req_edge_clearance_nm.max(min_nm);
                }
                DruConstraint::TrackWidth { min_nm, .. } => {
                    req_track_width_nm = req_track_width_nm.max(min_nm);
                }
                DruConstraint::ViaDiameter { min_nm, .. } => {
                    req_via_diameter_nm = req_via_diameter_nm.max(min_nm);
                }
                DruConstraint::ViaDrill { min_nm, .. } => {
                    max_via_drill_nm = max_via_drill_nm.max(min_nm);
                }
                DruConstraint::AnnularWidth { min_nm } => {
                    max_via_annular_nm = max_via_annular_nm.max(min_nm);
                }
                DruConstraint::ViaCount { .. }
                | DruConstraint::Disallow { .. }
                | DruConstraint::DiffPairGap { .. }
                | DruConstraint::DiffPairUncoupled { .. }
                | DruConstraint::Length { .. }
                | DruConstraint::Skew { .. } => {}
            }
        }
        let req_via_diameter_i128 = i128::from(req_via_diameter_nm)
            .max(i128::from(max_via_drill_nm) + 2 * i128::from(max_via_annular_nm));
        if self.max_pair_clearance_nm < req_pair_clearance_nm {
            return Err(Failure::new(
                Status::Invalid,
                "effective_rules max_pair_clearance_nm understates minimums/net_rules/dru_rules",
            ));
        }
        if self.max_hole_clearance_nm < req_hole_clearance_nm {
            return Err(Failure::new(
                Status::Invalid,
                "effective_rules max_hole_clearance_nm understates minimums/net_rules/dru_rules",
            ));
        }
        if self.max_hole_to_hole_nm < req_hole_to_hole_nm {
            return Err(Failure::new(
                Status::Invalid,
                "effective_rules max_hole_to_hole_nm understates minimums/net_rules/dru_rules",
            ));
        }
        if self.max_edge_clearance_nm < req_edge_clearance_nm {
            return Err(Failure::new(
                Status::Invalid,
                "effective_rules max_edge_clearance_nm understates minimums/net_rules/dru_rules",
            ));
        }
        if self.max_track_width_nm < req_track_width_nm {
            return Err(Failure::new(
                Status::Invalid,
                "effective_rules max_track_width_nm understates minimums/net_rules/dru_rules",
            ));
        }
        if i128::from(self.max_via_diameter_nm) < req_via_diameter_i128 {
            return Err(Failure::new(
                Status::Invalid,
                "effective_rules max_via_diameter_nm understates minimums/net_rules/dru_rules",
            ));
        }
        Ok(())
    }

    pub fn net(&self, net_id: u32) -> &NetResolvedRules {
        self.net_rules
            .get(&net_id)
            .or_else(|| self.net_rules.get(&0))
            .expect("net 0 fallback exists")
    }

    pub fn track_width_for(&self, net_id: u32, layer: &str) -> Result<i64, Failure> {
        let nr = self.net(net_id);
        let mut width = nr.width_nm;
        let mut max_cap: Option<i64> = None;
        let layer_owned = layer.to_owned();
        let item = DruItem {
            net_id: nr.net_id,
            net_name: &nr.net_name,
            net_class: &nr.class_name,
            class_members: &nr.class_members,
            kind: DruItemKind::Track,
            layers: std::slice::from_ref(&layer_owned),
        };
        for rule in &self.dru_rules {
            if rule.severity == DruSeverity::Ignore {
                continue;
            }
            if let Some(l) = &rule.layer {
                if !layer_matches_spec(layer, l) {
                    continue;
                }
            }
            if let Some(cond) = &rule.condition {
                if !eval_dru_expr(cond, &item, None) {
                    continue;
                }
            }
            if let DruConstraint::TrackWidth { min_nm, max_nm } = rule.constraint {
                width = width.max(min_nm);
                if let Some(mx) = max_nm {
                    max_cap = Some(max_cap.map_or(mx, |c| c.min(mx)));
                }
            }
        }
        if let Some(mx) = max_cap {
            if width > mx {
                return Err(Failure::new(
                    Status::Unroutable,
                    format!(
                        "net {} track_width {width} nm exceeds .kicad_dru max {mx} nm",
                        nr.net_name
                    ),
                ));
            }
        }
        if !(1..=MAX_NM).contains(&width) {
            return Err(Failure::new(
                Status::Unroutable,
                format!(
                    "net {} track_width {width} nm is outside supported track width bounds",
                    nr.net_name
                ),
            ));
        }
        Ok(width)
    }

    fn make_item<'a>(
        &'a self,
        net_id: u32,
        kind: DruItemKind,
        layers: &'a [String],
    ) -> DruItem<'a> {
        let nr = self.net(net_id);
        DruItem {
            net_id: nr.net_id,
            net_name: &nr.net_name,
            net_class: &nr.class_name,
            class_members: &nr.class_members,
            kind,
            layers,
        }
    }

    pub fn pair_clearance(
        &self,
        net_a: u32,
        kind_a: DruItemKind,
        net_b: u32,
        kind_b: DruItemKind,
        layer: &str,
        local_clearance_nm: Option<i64>,
    ) -> i64 {
        let na = self.net(net_a);
        let nb = self.net(net_b);
        let mut clr = na
            .clearance_nm
            .max(nb.clearance_nm)
            .max(self.minimums.min_clearance_nm);
        if let Some(lc) = local_clearance_nm {
            clr = clr.max(lc);
        }
        if !self.dru_rules.is_empty() {
            let layers = [layer.to_owned()];
            let ia = self.make_item(net_a, kind_a, &layers);
            let ib = self.make_item(net_b, kind_b, &layers);
            for rule in &self.dru_rules {
                if rule.severity == DruSeverity::Ignore {
                    continue;
                }
                let DruConstraint::Clearance { min_nm } = rule.constraint else {
                    continue;
                };
                if let Some(l) = &rule.layer {
                    if !layer_matches_spec(layer, l) {
                        continue;
                    }
                }
                let matched = match &rule.condition {
                    None => true,
                    Some(cond) => {
                        eval_dru_expr(cond, &ia, Some(&ib)) || eval_dru_expr(cond, &ib, Some(&ia))
                    }
                };
                if matched {
                    clr = clr.max(min_nm);
                }
            }
        }
        clr
    }

    pub fn pair_hole_clearance(
        &self,
        net_a: u32,
        kind_a: DruItemKind,
        layers_a: &[String],
        net_b: u32,
        kind_b: DruItemKind,
        layers_b: &[String],
    ) -> i64 {
        let na = self.net(net_a);
        let nb = self.net(net_b);
        let mut clr = na.hole_clearance_nm.max(nb.hole_clearance_nm);
        if !self.dru_rules.is_empty() {
            let ia = self.make_item(net_a, kind_a, layers_a);
            let ib = self.make_item(net_b, kind_b, layers_b);
            for rule in &self.dru_rules {
                if rule.severity == DruSeverity::Ignore {
                    continue;
                }
                let DruConstraint::HoleClearance { min_nm } = rule.constraint else {
                    continue;
                };
                if let Some(l) = &rule.layer {
                    let overlap = layers_a
                        .iter()
                        .any(|la| layers_b.contains(la) && layer_matches_spec(la, l));
                    if !overlap {
                        continue;
                    }
                }
                let matched = match &rule.condition {
                    None => true,
                    Some(cond) => {
                        eval_dru_expr(cond, &ia, Some(&ib)) || eval_dru_expr(cond, &ib, Some(&ia))
                    }
                };
                if matched {
                    clr = clr.max(min_nm);
                }
            }
        }
        clr
    }

    pub fn pair_hole_to_hole(
        &self,
        net_a: u32,
        kind_a: DruItemKind,
        layers_a: &[String],
        net_b: u32,
        kind_b: DruItemKind,
        layers_b: &[String],
    ) -> i64 {
        let na = self.net(net_a);
        let nb = self.net(net_b);
        let mut clr = na.hole_to_hole_nm.max(nb.hole_to_hole_nm);
        if !self.dru_rules.is_empty() {
            let ia = self.make_item(net_a, kind_a, layers_a);
            let ib = self.make_item(net_b, kind_b, layers_b);
            for rule in &self.dru_rules {
                if rule.severity == DruSeverity::Ignore {
                    continue;
                }
                let DruConstraint::HoleToHole { min_nm } = rule.constraint else {
                    continue;
                };
                if let Some(l) = &rule.layer {
                    let overlap = layers_a
                        .iter()
                        .any(|la| layers_b.contains(la) && layer_matches_spec(la, l));
                    if !overlap {
                        continue;
                    }
                }
                let matched = match &rule.condition {
                    None => true,
                    Some(cond) => {
                        eval_dru_expr(cond, &ia, Some(&ib)) || eval_dru_expr(cond, &ib, Some(&ia))
                    }
                };
                if matched {
                    clr = clr.max(min_nm);
                }
            }
        }
        clr
    }
}

impl EffectiveRules {
    pub fn edge_clearance_for(&self, net_id: u32, kind: DruItemKind, layers: &[String]) -> i64 {
        let nr = self.net(net_id);
        let mut clr = nr.edge_clearance_nm;
        if !self.dru_rules.is_empty() {
            let item = self.make_item(net_id, kind, layers);
            for rule in &self.dru_rules {
                if rule.severity == DruSeverity::Ignore {
                    continue;
                }
                let DruConstraint::EdgeClearance { min_nm } = rule.constraint else {
                    continue;
                };
                if let Some(l) = &rule.layer {
                    if !layers.iter().any(|il| layer_matches_spec(il, l)) {
                        continue;
                    }
                }
                if rule
                    .condition
                    .as_ref()
                    .is_none_or(|c| eval_dru_expr(c, &item, None))
                {
                    clr = clr.max(min_nm);
                }
            }
        }
        clr
    }

    pub fn via_dims_for(
        &self,
        net_id: u32,
        is_micro: bool,
        span_layers: &[String],
    ) -> Result<(i64, i64), Failure> {
        let nr = self.net(net_id);
        let (mut diam, mut drill) = if is_micro {
            (nr.microvia_diameter_nm, nr.microvia_drill_nm)
        } else {
            (nr.via_diameter_nm, nr.via_drill_nm)
        };
        let mut annular = nr
            .annular_width_nm
            .max(self.minimums.min_via_annular_width_nm);
        let mut max_diam: Option<i64> = None;
        let mut max_drill: Option<i64> = None;

        if !self.dru_rules.is_empty() {
            let item = self.make_item(net_id, DruItemKind::Via, span_layers);
            for rule in &self.dru_rules {
                if rule.severity == DruSeverity::Ignore {
                    continue;
                }
                if let Some(l) = &rule.layer {
                    if !span_layers.iter().any(|il| layer_matches_spec(il, l)) {
                        continue;
                    }
                }
                if !rule
                    .condition
                    .as_ref()
                    .is_none_or(|c| eval_dru_expr(c, &item, None))
                {
                    continue;
                }
                match rule.constraint {
                    DruConstraint::ViaDiameter { min_nm, max_nm } => {
                        diam = diam.max(min_nm);
                        if let Some(mx) = max_nm {
                            max_diam = Some(max_diam.map_or(mx, |c| c.min(mx)));
                        }
                    }
                    DruConstraint::ViaDrill { min_nm, max_nm } => {
                        drill = drill.max(min_nm);
                        if let Some(mx) = max_nm {
                            max_drill = Some(max_drill.map_or(mx, |c| c.min(mx)));
                        }
                    }
                    DruConstraint::AnnularWidth { min_nm } => {
                        annular = annular.max(min_nm);
                    }
                    _ => {}
                }
            }
        }
        let diam_i128 = i128::from(diam).max(i128::from(drill) + 2 * i128::from(annular));
        if !(1..=2 * MAX_NM).contains(&drill)
            || diam_i128 <= i128::from(drill)
            || diam_i128 > i128::from(2 * MAX_NM)
        {
            return Err(Failure::new(
                Status::Unroutable,
                format!(
                    "net {} via dimensions ({diam_i128} nm / {drill} nm) violate valid via invariants",
                    nr.net_name
                ),
            ));
        }
        let diam = diam_i128 as i64;
        if let Some(mx) = max_drill {
            if drill > mx {
                return Err(Failure::new(
                    Status::Unroutable,
                    format!(
                        "net {} via drill {drill} nm exceeds .kicad_dru max {mx} nm",
                        nr.net_name
                    ),
                ));
            }
        }
        if let Some(mx) = max_diam {
            if diam > mx {
                return Err(Failure::new(
                    Status::Unroutable,
                    format!(
                        "net {} via diameter {diam} nm exceeds .kicad_dru max {mx} nm",
                        nr.net_name
                    ),
                ));
            }
        }
        Ok((diam, drill))
    }

    pub fn disallow_track_on(&self, net_id: u32, layer: &str) -> bool {
        if self.dru_rules.is_empty() {
            return false;
        }
        let layers = [layer.to_owned()];
        let item = self.make_item(net_id, DruItemKind::Track, &layers);
        for rule in &self.dru_rules {
            if rule.severity == DruSeverity::Ignore {
                continue;
            }
            let DruConstraint::Disallow { track: true, .. } = rule.constraint else {
                continue;
            };
            if let Some(l) = &rule.layer {
                if !layer_matches_spec(layer, l) {
                    continue;
                }
            }
            if rule
                .condition
                .as_ref()
                .is_none_or(|c| eval_dru_expr(c, &item, None))
            {
                return true;
            }
        }
        false
    }

    pub fn disallow_via_on(&self, net_id: u32, span_layers: &[String]) -> bool {
        if self.dru_rules.is_empty() {
            return false;
        }
        let item = self.make_item(net_id, DruItemKind::Via, span_layers);
        for rule in &self.dru_rules {
            if rule.severity == DruSeverity::Ignore {
                continue;
            }
            let DruConstraint::Disallow { via: true, .. } = rule.constraint else {
                continue;
            };
            if let Some(l) = &rule.layer {
                if !span_layers.iter().any(|il| layer_matches_spec(il, l)) {
                    continue;
                }
            }
            if rule
                .condition
                .as_ref()
                .is_none_or(|c| eval_dru_expr(c, &item, None))
            {
                return true;
            }
        }
        false
    }
}

fn merge_minimums(bds: Option<&BoardDesignSettings>, pro: Option<&ProjectRules>) -> BoardMinimums {
    let mut m = pro.map_or_else(BoardMinimums::default, |p| p.minimums.clone());
    if let Some(b) = bds {
        m.min_track_width_nm = m.min_track_width_nm.max(b.min_track_width_nm);
        m.min_clearance_nm = m.min_clearance_nm.max(b.min_clearance_nm);
        m.min_edge_clearance_nm = m.min_edge_clearance_nm.max(b.min_edge_clearance_nm);
        m.min_via_diameter_nm = m.min_via_diameter_nm.max(b.min_via_diameter_nm);
        m.min_via_annular_width_nm = m.min_via_annular_width_nm.max(b.min_via_annular_width_nm);
        m.min_through_hole_diameter_nm = m
            .min_through_hole_diameter_nm
            .max(b.min_through_hole_diameter_nm);
        m.min_hole_clearance_nm = m.min_hole_clearance_nm.max(b.min_hole_clearance_nm);
        m.min_hole_to_hole_nm = m.min_hole_to_hole_nm.max(b.min_hole_to_hole_nm);
        m.min_microvia_diameter_nm = m.min_microvia_diameter_nm.max(b.min_microvia_diameter_nm);
        m.min_microvia_drill_nm = m.min_microvia_drill_nm.max(b.min_microvia_drill_nm);
        m.solder_mask_expansion_nm = m.solder_mask_expansion_nm.max(b.solder_mask_expansion_nm);
        m.solder_mask_min_width_nm = m.solder_mask_min_width_nm.max(b.solder_mask_min_width_nm);
        m.solder_mask_to_copper_clearance_nm = m
            .solder_mask_to_copper_clearance_nm
            .max(b.solder_mask_to_copper_clearance_nm);
        m.min_resolved_spokes = m.min_resolved_spokes.or(b.min_resolved_spokes);
        m.max_error_nm = b.max_error_nm;
    }
    m
}

fn resolve_net_classes<'a>(
    net_name: &str,
    pro: &'a ProjectRules,
) -> (String, Vec<String>, Option<&'a NetClassDef>) {
    if net_name.is_empty() {
        let def = pro.classes.iter().find(|c| c.name == "Default");
        return ("Default".to_owned(), vec!["Default".to_owned()], def);
    }
    let mut matched_names: BTreeSet<String> = BTreeSet::new();
    if let Some(assigned) = pro.assignments.get(net_name) {
        for c in assigned {
            matched_names.insert(c.clone());
        }
    }
    for pat in &pro.patterns {
        if pat.matcher.matches(net_name) {
            matched_names.insert(pat.netclass.clone());
        }
    }
    matched_names.remove("Default");
    let mut members: Vec<String> = matched_names.into_iter().collect();
    if members.is_empty() {
        members.push("Default".to_owned());
    }
    let mut matched_defs: Vec<&NetClassDef> = pro
        .classes
        .iter()
        .filter(|c| members.iter().any(|m| m == &c.name))
        .collect();
    matched_defs.sort_by(|a, b| {
        b.priority
            .cmp(&a.priority)
            .then_with(|| a.name.cmp(&b.name))
    });
    let class_name = if members.len() == 1 {
        members[0].clone()
    } else {
        matched_defs
            .iter()
            .map(|d| d.name.as_str())
            .collect::<Vec<_>>()
            .join(",")
    };
    let primary = matched_defs
        .first()
        .copied()
        .or_else(|| pro.classes.iter().find(|c| c.name == "Default"));
    (class_name, members, primary)
}

pub fn resolve_effective_rules(input: &mut Input) -> Result<EffectiveRules, Failure> {
    if input.project_rules.is_none()
        && input.custom_rules.is_none()
        && input.board_design_settings.is_none()
    {
        if let Some(eff) = &input.effective_rules {
            eff.validate()?;
            if input.rules.is_omitted() {
                let nr = eff.net(0);
                input.rules = Rules {
                    width_nm: nr.width_nm.max(1),
                    clearance_nm: nr.clearance_nm.max(0),
                    grid_nm: nr.width_nm.clamp(100_000, 500_000),
                    edge_clearance_nm: nr.edge_clearance_nm.max(0),
                    hole_clearance_nm: nr.hole_clearance_nm.max(0),
                    same_net_hole_clearance_nm: 0,
                    solder_mask_expansion_nm: eff.minimums.solder_mask_expansion_nm.max(0),
                    solder_mask_min_width_nm: eff.minimums.solder_mask_min_width_nm.max(0),
                    solder_mask_to_copper_clearance_nm: eff
                        .minimums
                        .solder_mask_to_copper_clearance_nm
                        .max(0),
                };
            }
            return Ok(eff.clone());
        }
        if !input.rules.is_omitted() {
            return Ok(EffectiveRules::from_uniform_rules(input.rules, &input.nets));
        }
        return Err(Failure::new(
            Status::Invalid,
            "missing routing rules and project/board design settings",
        ));
    }

    let pro = match &input.project_rules {
        Some(text) => Some(parse_kicad_pro(text.as_bytes())?),
        None => input.board_design_settings.as_ref().and_then(|bds| {
            if bds.netclasses.is_empty() && bds.netclass_assignments.is_empty() {
                None
            } else {
                Some(ProjectRules {
                    version: 3,
                    minimums: BoardMinimums::default(),
                    classes: bds.netclasses.clone(),
                    assignments: bds.netclass_assignments.clone(),
                    patterns: Vec::new(),
                    tuning_profiles: Vec::new(),
                    use_height_for_length_calcs: bds.use_height_for_length_calcs,
                })
            }
        }),
    };
    let dru_rules = match &input.custom_rules {
        Some(text) => parse_kicad_dru(text.as_bytes())?,
        None => Vec::new(),
    };
    if let Some(bds) = &input.board_design_settings {
        let fields = [
            bds.min_track_width_nm,
            bds.min_clearance_nm,
            bds.min_edge_clearance_nm,
            bds.min_via_diameter_nm,
            bds.min_via_annular_width_nm,
            bds.min_through_hole_diameter_nm,
            bds.min_hole_clearance_nm,
            bds.min_hole_to_hole_nm,
            bds.min_microvia_diameter_nm,
            bds.min_microvia_drill_nm,
            bds.solder_mask_expansion_nm,
            bds.solder_mask_min_width_nm,
            bds.solder_mask_to_copper_clearance_nm,
            bds.default_clearance_nm,
            bds.default_track_width_nm,
            bds.default_via_diameter_nm,
            bds.default_via_drill_nm,
            bds.default_microvia_diameter_nm,
            bds.default_microvia_drill_nm,
        ];
        if fields.iter().any(|&v| v < 0) {
            return Err(Failure::new(
                Status::Invalid,
                "board_design_settings values must be nonnegative",
            ));
        }
        if bds.min_resolved_spokes.is_some_and(|n| n > 16) {
            return Err(Failure::new(
                Status::Invalid,
                "board_design_settings min_resolved_spokes exceeds supported range",
            ));
        }
        if bds.max_error_nm.is_some_and(|e| !(0..=MAX_NM).contains(&e)) {
            return Err(Failure::new(
                Status::Invalid,
                "board_design_settings max_error_nm must be 0..=100000000 nm",
            ));
        }
        if bds
            .board_thickness_nm
            .is_some_and(|bt| !(1..=2 * MAX_NM).contains(&bt))
        {
            return Err(Failure::new(
                Status::Invalid,
                "board_design_settings board_thickness_nm must be in 1..=200000000 nm",
            ));
        }
        if bds
            .outer_solder_mask_thickness_nm
            .is_some_and(|m| !(0..=2 * MAX_NM).contains(&m))
        {
            return Err(Failure::new(
                Status::Invalid,
                "board_design_settings outer_solder_mask_thickness_nm must be in 0..=200000000 nm",
            ));
        }
        if let (Some(bt), Some(mask)) = (bds.board_thickness_nm, bds.outer_solder_mask_thickness_nm)
        {
            if mask > bt {
                return Err(Failure::new(
                    Status::Invalid,
                    "board_design_settings outer_solder_mask_thickness_nm exceeds board_thickness_nm",
                ));
            }
        }
    }
    let minimums = merge_minimums(input.board_design_settings.as_ref(), pro.as_ref());

    let explicit_rules = if input.rules.is_omitted() {
        None
    } else {
        Some(input.rules)
    };
    if let Some(explicit) = explicit_rules {
        if explicit.width_nm < minimums.min_track_width_nm {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "width_nm={} is below native/project floor {}",
                    explicit.width_nm, minimums.min_track_width_nm
                ),
            ));
        }
        if explicit.clearance_nm < minimums.min_clearance_nm {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "clearance_nm={} is below native/project floor {}",
                    explicit.clearance_nm, minimums.min_clearance_nm
                ),
            ));
        }
        if explicit.edge_clearance_nm < minimums.min_edge_clearance_nm {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "edge_clearance_nm={} is below native/project floor {}",
                    explicit.edge_clearance_nm, minimums.min_edge_clearance_nm
                ),
            ));
        }
    }

    let explicit_rules = input.rules;
    let bds_default = input.board_design_settings.as_ref();
    let default_cls = pro
        .as_ref()
        .and_then(|p| p.classes.iter().find(|c| c.name == "Default"));
    let default_width = default_cls
        .map_or_else(
            || bds_default.map_or(0, |b| b.default_track_width_nm),
            |c| c.track_width_nm,
        )
        .max(minimums.min_track_width_nm);
    let default_width = if default_width > 0 {
        default_width
    } else {
        200_000
    };
    let default_clearance = default_cls
        .map_or_else(
            || bds_default.map_or(0, |b| b.default_clearance_nm),
            |c| c.clearance_nm,
        )
        .max(minimums.min_clearance_nm);

    let base_rules = if !explicit_rules.is_omitted() {
        let mut r = explicit_rules;
        if minimums.hole_clearance_active {
            r.hole_clearance_nm = r.hole_clearance_nm.max(minimums.min_hole_clearance_nm);
        }
        r.solder_mask_expansion_nm = r
            .solder_mask_expansion_nm
            .max(minimums.solder_mask_expansion_nm);
        r.solder_mask_min_width_nm = r
            .solder_mask_min_width_nm
            .max(minimums.solder_mask_min_width_nm);
        r.solder_mask_to_copper_clearance_nm = r
            .solder_mask_to_copper_clearance_nm
            .max(minimums.solder_mask_to_copper_clearance_nm);
        r
    } else {
        Rules {
            width_nm: default_width,
            clearance_nm: default_clearance,
            grid_nm: default_width.clamp(100_000, 500_000),
            edge_clearance_nm: minimums.min_edge_clearance_nm,
            hole_clearance_nm: if minimums.hole_clearance_active {
                minimums.min_hole_clearance_nm
            } else {
                0
            },
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: minimums.solder_mask_expansion_nm,
            solder_mask_min_width_nm: minimums.solder_mask_min_width_nm,
            solder_mask_to_copper_clearance_nm: minimums.solder_mask_to_copper_clearance_nm,
        }
    };
    input.rules = base_rules;

    let mut net_rules = BTreeMap::new();
    let mut all_nets: Vec<Net> = input.nets.clone();
    if !all_nets.iter().any(|n| n.id == 0) {
        all_nets.push(Net {
            id: 0,
            name: String::new(),
        });
    }

    let layers_ref: Vec<String> = if input.layers.is_empty() {
        vec![input.layer.clone()]
    } else {
        input.layers.clone()
    };

    for net in all_nets {
        let (class_name, class_members, cls_opt) = match &pro {
            Some(p) => resolve_net_classes(&net.name, p),
            None => ("Default".to_owned(), vec!["Default".to_owned()], None),
        };
        let cls_w = cls_opt.map_or(0, |c| c.track_width_nm);
        let cls_c = cls_opt.map_or(0, |c| c.clearance_nm);
        let cls_vd = cls_opt.map_or(0, |c| c.via_diameter_nm);
        let cls_vdr = cls_opt.map_or(0, |c| c.via_drill_nm);
        let cls_mvd = cls_opt.map_or(0, |c| c.microvia_diameter_nm);
        let cls_mvdr = cls_opt.map_or(0, |c| c.microvia_drill_nm);
        let diff_pair_width_nm = cls_opt.map_or(0, |c| c.diff_pair_width_nm);
        let diff_pair_gap_nm = cls_opt.map_or(0, |c| c.diff_pair_gap_nm);
        let diff_pair_via_gap_nm = cls_opt.map_or(0, |c| c.diff_pair_via_gap_nm);
        let tuning_profile = cls_opt.and_then(|c| c.tuning_profile.clone());

        let mut width_nm = (if cls_w > 0 {
            cls_w
        } else {
            base_rules.width_nm
        })
        .max(minimums.min_track_width_nm);
        let mut clearance_nm = (if cls_c > 0 {
            cls_c
        } else {
            base_rules.clearance_nm
        })
        .max(minimums.min_clearance_nm);
        if !explicit_rules.is_omitted() {
            width_nm = width_nm.max(explicit_rules.width_nm);
            clearance_nm = clearance_nm.max(explicit_rules.clearance_nm);
        }

        let fallback_vd = if pro.is_none() {
            bds_default.map_or(0, |b| b.default_via_diameter_nm)
        } else {
            0
        };
        let fallback_vdr = if pro.is_none() {
            bds_default.map_or(0, |b| b.default_via_drill_nm)
        } else {
            0
        };
        let fallback_mvd = if pro.is_none() {
            bds_default.map_or(0, |b| b.default_microvia_diameter_nm)
        } else {
            0
        };
        let fallback_mvdr = if pro.is_none() {
            bds_default.map_or(0, |b| b.default_microvia_drill_nm)
        } else {
            0
        };

        let mut via_drill_nm = if cls_vdr > 0 {
            cls_vdr
        } else if fallback_vdr > 0 {
            fallback_vdr
        } else {
            300_000
        }
        .max(minimums.min_through_hole_diameter_nm)
        .max(1);
        let mut annular_width_nm = minimums.min_via_annular_width_nm;
        let mut via_diameter_nm = if cls_vd > 0 {
            cls_vd
        } else if fallback_vd > 0 {
            fallback_vd
        } else {
            600_000
        }
        .max(minimums.min_via_diameter_nm)
        .max(via_drill_nm + 2 * annular_width_nm);
        let microvia_drill_nm = if cls_mvdr > 0 {
            cls_mvdr
        } else if fallback_mvdr > 0 {
            fallback_mvdr
        } else {
            150_000
        }
        .max(minimums.min_microvia_drill_nm)
        .max(1);
        let microvia_diameter_nm = if cls_mvd > 0 {
            cls_mvd
        } else if fallback_mvd > 0 {
            fallback_mvd
        } else {
            300_000
        }
        .max(minimums.min_microvia_diameter_nm)
        .max(microvia_drill_nm + 2 * annular_width_nm);

        let mut edge_clearance_nm = base_rules
            .edge_clearance_nm
            .max(minimums.min_edge_clearance_nm);
        let mut hole_clearance_nm = base_rules.hole_clearance_nm;
        let mut hole_to_hole_nm = if minimums.hole_to_hole_active {
            minimums.min_hole_to_hole_nm
        } else {
            0
        };
        let mut max_via_count: Option<u32> = None;

        let item_track = DruItem {
            net_id: net.id,
            net_name: &net.name,
            net_class: &class_name,
            class_members: &class_members,
            kind: DruItemKind::Track,
            layers: &layers_ref,
        };
        let item_via = DruItem {
            net_id: net.id,
            net_name: &net.name,
            net_class: &class_name,
            class_members: &class_members,
            kind: DruItemKind::Via,
            layers: &layers_ref,
        };
        for rule in &dru_rules {
            if rule.severity == DruSeverity::Ignore {
                continue;
            }
            let matches_for = |it: &DruItem<'_>| -> bool {
                if let Some(l) = &rule.layer {
                    if !it.layers.iter().any(|il| layer_matches_spec(il, l)) {
                        return false;
                    }
                }
                rule.condition
                    .as_ref()
                    .is_none_or(|c| eval_dru_expr(c, it, None))
            };
            match rule.constraint {
                DruConstraint::TrackWidth { min_nm, .. } if rule.layer.is_none() => {
                    if matches_for(&item_track) {
                        width_nm = width_nm.max(min_nm);
                    }
                }
                DruConstraint::ViaDiameter { min_nm, .. } if rule.layer.is_none() => {
                    if matches_for(&item_via) {
                        via_diameter_nm = via_diameter_nm.max(min_nm);
                    }
                }
                DruConstraint::ViaDrill { min_nm, .. } if rule.layer.is_none() => {
                    if matches_for(&item_via) {
                        via_drill_nm = via_drill_nm.max(min_nm);
                    }
                }
                DruConstraint::AnnularWidth { min_nm } if rule.layer.is_none() => {
                    if matches_for(&item_via) {
                        annular_width_nm = annular_width_nm.max(min_nm);
                    }
                }
                DruConstraint::EdgeClearance { min_nm } if rule.layer.is_none() => {
                    if matches_for(&item_track) {
                        edge_clearance_nm = edge_clearance_nm.max(min_nm);
                    }
                }
                DruConstraint::ViaCount { max } => {
                    if matches_for(&item_via) {
                        max_via_count = Some(max_via_count.map_or(max, |m| m.min(max)));
                    }
                }
                _ => {}
            }
        }
        via_diameter_nm = via_diameter_nm.max(via_drill_nm + 2 * annular_width_nm);
        let _ = (&mut hole_clearance_nm, &mut hole_to_hole_nm);

        net_rules.insert(
            net.id,
            NetResolvedRules {
                net_id: net.id,
                net_name: net.name,
                class_name,
                class_members,
                width_nm,
                clearance_nm,
                via_diameter_nm,
                via_drill_nm,
                microvia_diameter_nm,
                microvia_drill_nm,
                edge_clearance_nm,
                hole_clearance_nm,
                hole_to_hole_nm,
                annular_width_nm,
                max_via_count,
                diff_pair_width_nm,
                diff_pair_gap_nm,
                diff_pair_via_gap_nm,
                tuning_profile,
            },
        );
    }

    let mut max_pair_clearance_nm = base_rules.clearance_nm.max(minimums.min_clearance_nm);
    let mut max_hole_clearance_nm = base_rules
        .hole_clearance_nm
        .max(base_rules.same_net_hole_clearance_nm);
    let mut max_hole_to_hole_nm = if minimums.hole_to_hole_active {
        minimums.min_hole_to_hole_nm
    } else {
        0
    };
    let mut max_edge_clearance_nm = base_rules.edge_clearance_nm;
    let mut max_track_width_nm = base_rules.width_nm;
    let mut max_via_diameter_nm = minimums.min_via_diameter_nm;
    let mut max_via_drill_nm = minimums.min_through_hole_diameter_nm;
    let mut max_via_annular_nm = minimums.min_via_annular_width_nm;

    for nr in net_rules.values() {
        max_pair_clearance_nm = max_pair_clearance_nm.max(nr.clearance_nm);
        max_hole_clearance_nm = max_hole_clearance_nm.max(nr.hole_clearance_nm);
        max_hole_to_hole_nm = max_hole_to_hole_nm.max(nr.hole_to_hole_nm);
        max_edge_clearance_nm = max_edge_clearance_nm.max(nr.edge_clearance_nm);
        max_track_width_nm = max_track_width_nm.max(nr.width_nm);
        max_via_diameter_nm = max_via_diameter_nm.max(nr.via_diameter_nm);
        max_via_drill_nm = max_via_drill_nm.max(nr.via_drill_nm);
        max_via_annular_nm = max_via_annular_nm.max(nr.annular_width_nm);
        if minimums.allow_microvias {
            max_via_diameter_nm = max_via_diameter_nm.max(nr.microvia_diameter_nm);
            max_via_drill_nm = max_via_drill_nm.max(nr.microvia_drill_nm);
        }
    }
    for rule in &dru_rules {
        if rule.severity == DruSeverity::Ignore {
            continue;
        }
        match rule.constraint {
            DruConstraint::Clearance { min_nm } => {
                max_pair_clearance_nm = max_pair_clearance_nm.max(min_nm);
            }
            DruConstraint::HoleClearance { min_nm } => {
                max_hole_clearance_nm = max_hole_clearance_nm.max(min_nm);
            }
            DruConstraint::HoleToHole { min_nm } => {
                max_hole_to_hole_nm = max_hole_to_hole_nm.max(min_nm);
            }
            DruConstraint::EdgeClearance { min_nm } => {
                max_edge_clearance_nm = max_edge_clearance_nm.max(min_nm);
            }
            DruConstraint::TrackWidth { min_nm, .. } => {
                max_track_width_nm = max_track_width_nm.max(min_nm);
            }
            DruConstraint::ViaDiameter { min_nm, .. } => {
                max_via_diameter_nm = max_via_diameter_nm.max(min_nm);
            }
            DruConstraint::ViaDrill { min_nm, .. } => {
                max_via_drill_nm = max_via_drill_nm.max(min_nm);
            }
            DruConstraint::AnnularWidth { min_nm } => {
                max_via_annular_nm = max_via_annular_nm.max(min_nm);
            }
            _ => {}
        }
    }
    max_via_diameter_nm = max_via_diameter_nm.max(max_via_drill_nm + 2 * max_via_annular_nm);

    let use_height_for_length_calcs = pro
        .as_ref()
        .and_then(|p| p.use_height_for_length_calcs)
        .or_else(|| {
            input
                .board_design_settings
                .as_ref()
                .and_then(|b| b.use_height_for_length_calcs)
        })
        .unwrap_or(false);
    let board_thickness_nm = input
        .board_design_settings
        .as_ref()
        .and_then(|b| b.board_thickness_nm);
    let outer_solder_mask_thickness_nm = input
        .board_design_settings
        .as_ref()
        .and_then(|b| b.outer_solder_mask_thickness_nm);
    let tuning_profiles = pro
        .as_ref()
        .map(|p| p.tuning_profiles.clone())
        .unwrap_or_default();

    Ok(EffectiveRules {
        allow_blind_buried_vias: minimums.allow_blind_buried_vias,
        allow_microvias: minimums.allow_microvias,
        minimums,
        net_rules,
        dru_rules,
        via_cost: 40,
        max_pair_clearance_nm,
        max_hole_clearance_nm,
        max_hole_to_hole_nm,
        max_edge_clearance_nm,
        max_track_width_nm,
        max_via_diameter_nm,
        tuning_profiles,
        use_height_for_length_calcs,
        board_thickness_nm,
        outer_solder_mask_thickness_nm,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn regex_nfa_and_wildcard_matching_semantics() {
        let p_in = PatternMatcher::compile(r"^in\d+$").unwrap();
        assert!(p_in.matches("in1"));
        assert!(p_in.matches("in42"));
        assert!(!p_in.matches("plain_in"));
        assert!(!p_in.matches("x_in1"));

        let p_out = PatternMatcher::compile(r".*out\d+$").unwrap();
        assert!(p_out.matches("clk_out2"));
        assert!(!p_out.matches("clk_out"));

        let p_pwr = PatternMatcher::compile(r"^PWR.*").unwrap();
        assert!(p_pwr.matches("PWR_3V3"));
        assert!(!p_pwr.matches("X_PWR"));

        let p_alt = PatternMatcher::compile(r"^(3V3|VBUS|GND)$").unwrap();
        assert!(p_alt.matches("3V3"));
        assert!(p_alt.matches("GND"));
        assert!(!p_alt.matches("5V"));

        let p_glob = PatternMatcher::compile("*PWR*").unwrap();
        assert!(p_glob.matches("SYS_PWR_3V3"));
        assert!(!p_glob.matches("SIGNAL"));

        // Wildcard mode must not evaluate regex quantifier semantics (* = 0+, ? = 0/1)
        let p_pwr_glob = PatternMatcher::compile("PWR*").unwrap();
        assert!(p_pwr_glob.matches("PWR"));
        assert!(p_pwr_glob.matches("PWR_3V3"));
        assert!(!p_pwr_glob.matches("PW"));

        let p_usb_glob = PatternMatcher::compile("USB?").unwrap();
        assert!(p_usb_glob.matches("USB1"));
        assert!(p_usb_glob.matches("USBA"));
        assert!(!p_usb_glob.matches("USB"));
        assert!(!p_usb_glob.matches("US"));

        let p_dot_glob = PatternMatcher::compile("3.3V*").unwrap();
        assert!(p_dot_glob.matches("3.3V_SYS"));
        assert!(!p_dot_glob.matches("3a3"));

        let p_dotstar_re = PatternMatcher::compile("HV_.*").unwrap();
        assert!(p_dotstar_re.matches("HV_BUS"));
        assert!(!p_dotstar_re.matches("LV_BUS"));

        // ReDoS adversarial pattern remains linear-time
        let redos = PatternMatcher::compile(r"(a+)+b").unwrap();
        assert!(!redos.matches(&"a".repeat(50)));

        // Unsupported/malformed patterns fail closed
        assert_eq!(
            PatternMatcher::compile(r"(?=a)b").unwrap_err().status,
            Status::Unsupported
        );
        assert_eq!(
            PatternMatcher::compile(r"[unclosed").unwrap_err().status,
            Status::Invalid
        );
        assert_eq!(
            PatternMatcher::compile(r"(a)\1").unwrap_err().status,
            Status::Unsupported
        );
    }

    #[test]
    fn kicad_pro_parses_versions_minimums_and_assignments() {
        let bad_ver = br#"{"meta": {"version": 999}}"#;
        assert_eq!(
            parse_kicad_pro(bad_ver).unwrap_err().status,
            Status::Unsupported
        );

        let pro = br#"{
            "meta": {"version": 3},
            "board": {
                "design_settings": {
                    "rules": {
                        "min_track_width": 0.3,
                        "min_via_diameter": 1.5,
                        "min_via_annular_width": 0.15,
                        "min_hole_to_hole": 0.5,
                        "allow_blind_buried_vias": true,
                        "allow_microvias": false
                    },
                    "rule_severities": {"hole_to_hole": "error"}
                }
            },
            "net_settings": {
                "classes": [
                    {"name": "Default", "priority": 0, "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.6, "via_drill": 0.3},
                    {"name": "FastIn", "priority": 1, "clearance": 0.25, "track_width": 0.35, "via_diameter": 0.8, "via_drill": 0.4}
                ],
                "netclass_assignments": {"VCC": "FastIn", "BUS": ["FastIn"]},
                "netclass_patterns": [{"netclass": "FastIn", "pattern": "^in\\d+$"}]
            }
        }"#;
        let parsed = parse_kicad_pro(pro).unwrap();
        assert_eq!(parsed.version, 3);
        assert_eq!(parsed.minimums.min_track_width_nm, 300_000);
        assert_eq!(parsed.minimums.min_via_diameter_nm, 1_500_000);
        assert!(parsed.minimums.allow_blind_buried_vias);
        assert!(!parsed.minimums.allow_microvias);
        assert_eq!(resolve_net_classes("in1", &parsed).0, "FastIn");
        assert_eq!(resolve_net_classes("plain_in", &parsed).0, "Default");
        assert_eq!(resolve_net_classes("VCC", &parsed).0, "FastIn");
    }

    #[test]
    fn kicad_dru_parses_supported_and_fails_closed_on_unsupported() {
        let dru = br#"
        (version 1)
        # comment
        (rule "pwr_clr"
          (condition "A.NetClass == 'Power' && B.NetClass != 'Power'")
          (constraint clearance (min 0.4mm)))
        (rule "via_h2h"
          (layer "In1.Cu")
          (condition "A.Type == 'Via' && B.Type == 'Via'")
          (constraint hole_to_hole (min 3.0mm)))
        "#;
        let rules = parse_kicad_dru(dru).unwrap();
        assert_eq!(rules.len(), 2);

        for (bad, expected) in [
            ("(version 2)", Status::Unsupported),
            ("(version 1)\n(rule \"x\" (constraint clearance", Status::Invalid),
            ("(version 1)\n(rule \"x\" (constraint courtyard_clearance (min 0.2mm)))", Status::Unsupported),
            ("(version 1)\n(rule \"x\" (constraint disallow bogus))", Status::Unsupported),
            ("(version 1)\n(rule \"x\" (condition \"A.onLayer('F.Cu')\") (constraint clearance (min 0.2mm)))", Status::Unsupported),
            ("(version 1)\n(rule \"x\" (condition \"B.NetClass == 'Power'\") (constraint track_width (min 0.3mm)))", Status::Unsupported),
        ] {
            let err = parse_kicad_dru(bad.as_bytes()).unwrap_err();
            assert_eq!(err.status, expected, "input: {bad}");
        }
    }

    #[test]
    fn caller_effective_rules_missing_net0_or_invalid_bounds_returns_invalid() {
        let mut inp: Input = serde_json::from_value(serde_json::json!({
            "version": 1,
            "source_id": "a".repeat(64),
            "layer": "F.Cu",
            "bounds": {"min": [0, 0], "max": [1_000_000, 1_000_000]},
            "nets": [{"id": 0, "name": ""}, {"id": 1, "name": "N"}],
            "pads": [],
            "tracks": [],
            "max_expansions": 100
        }))
        .unwrap();
        let base_rules = Rules {
            width_nm: 200_000,
            clearance_nm: 200_000,
            grid_nm: 100_000,
            edge_clearance_nm: 200_000,
            hole_clearance_nm: 0,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        };
        let mut eff = EffectiveRules::from_uniform_rules(base_rules, &inp.nets);
        eff.net_rules.remove(&0);
        inp.effective_rules = Some(eff);
        let err = resolve_effective_rules(&mut inp).unwrap_err();
        assert_eq!(err.status, Status::Invalid);

        let mut eff2 = EffectiveRules::from_uniform_rules(base_rules, &inp.nets);
        eff2.net_rules.get_mut(&0).unwrap().width_nm = 0;
        inp.effective_rules = Some(eff2);
        let err2 = resolve_effective_rules(&mut inp).unwrap_err();
        assert_eq!(err2.status, Status::Invalid);

        let mut eff3 = EffectiveRules::from_uniform_rules(base_rules, &inp.nets);
        eff3.max_pair_clearance_nm = -1;
        inp.effective_rules = Some(eff3);
        let err3 = resolve_effective_rules(&mut inp).unwrap_err();
        assert_eq!(err3.status, Status::Invalid);
    }

    #[test]
    fn effective_rules_max_via_diameter_includes_layer_scoped_annular_and_drill() {
        let mut inp: Input = serde_json::from_value(serde_json::json!({
            "version": 1,
            "source_id": "b".repeat(64),
            "layer": "F.Cu",
            "layers": ["F.Cu", "B.Cu"],
            "bounds": {"min": [0, 0], "max": [10_000_000, 10_000_000]},
            "rules": {
                "width_nm": 100_000,
                "clearance_nm": 100_000,
                "grid_nm": 100_000,
                "edge_clearance_nm": 100_000,
                "hole_clearance_nm": 0,
                "same_net_hole_clearance_nm": 0,
                "solder_mask_expansion_nm": 0,
                "solder_mask_min_width_nm": 0,
                "solder_mask_to_copper_clearance_nm": 0
            },
            "nets": [{"id": 0, "name": ""}, {"id": 1, "name": "N"}],
            "pads": [],
            "tracks": [],
            "max_expansions": 100
        }))
        .unwrap();
        inp.project_rules = Some(
            serde_json::json!({
                "meta": {"version": 3},
                "board": {"design_settings": {"rules": {"allow_microvias": false}}},
                "net_settings": {"classes": [{
                    "name": "Default",
                    "track_width": 0.1,
                    "clearance": 0.1,
                    "via_diameter": 0.2,
                    "via_drill": 0.2
                }]}
            })
            .to_string(),
        );
        inp.custom_rules = Some(
            "(version 1)\n(rule \"ann\" (layer \"F.Cu\") (constraint annular_width (min 1.0mm)))"
                .into(),
        );
        let eff = resolve_effective_rules(&mut inp).unwrap();
        assert!(
            eff.max_via_diameter_nm >= 2_200_000,
            "got {}",
            eff.max_via_diameter_nm
        );

        inp.custom_rules = Some(
            "(version 1)\n(rule \"drl\" (layer \"B.Cu\") (constraint via_drill (min 0.9mm)))"
                .into(),
        );
        let eff2 = resolve_effective_rules(&mut inp).unwrap();
        assert!(
            eff2.max_via_diameter_nm >= 900_000,
            "got {}",
            eff2.max_via_diameter_nm
        );
    }

    #[test]
    fn effective_rules_validate_rejects_understated_max_bounds_and_invalid_dru_rules() {
        let base_inp = || -> Input {
            serde_json::from_value(serde_json::json!({
                "version": 1,
                "source_id": "c".repeat(64),
                "layer": "F.Cu",
                "layers": ["F.Cu", "B.Cu"],
                "bounds": {"min": [0, 0], "max": [10_000_000, 10_000_000]},
                "nets": [{"id": 0, "name": ""}, {"id": 1, "name": "SIG"}],
                "pads": [],
                "tracks": [],
                "max_expansions": 100
            }))
            .unwrap()
        };
        let base_rules = Rules {
            width_nm: 200_000,
            clearance_nm: 200_000,
            grid_nm: 100_000,
            edge_clearance_nm: 200_000,
            hole_clearance_nm: 100_000,
            same_net_hole_clearance_nm: 0,
            solder_mask_expansion_nm: 0,
            solder_mask_min_width_nm: 0,
            solder_mask_to_copper_clearance_nm: 0,
        };
        let valid_eff = EffectiveRules::from_uniform_rules(base_rules, &base_inp().nets);
        valid_eff.validate().unwrap();

        // Challenger 2 probes: understated max_pair_clearance_nm, max_hole_to_hole_nm, max_hole_clearance_nm
        let mut e1 = valid_eff.clone();
        e1.net_rules.get_mut(&1).unwrap().clearance_nm = 400_000;
        e1.max_pair_clearance_nm = 0;
        assert_eq!(e1.validate().unwrap_err().status, Status::Invalid);

        let mut e2 = valid_eff.clone();
        e2.minimums.min_hole_to_hole_nm = 1_500_000;
        e2.net_rules.get_mut(&1).unwrap().hole_to_hole_nm = 1_500_000;
        e2.max_hole_to_hole_nm = 0;
        assert_eq!(e2.validate().unwrap_err().status, Status::Invalid);

        let mut e3 = valid_eff.clone();
        e3.minimums.min_hole_clearance_nm = 500_000;
        e3.net_rules.get_mut(&1).unwrap().hole_clearance_nm = 500_000;
        e3.max_hole_clearance_nm = 0;
        assert_eq!(e3.validate().unwrap_err().status, Status::Invalid);

        // max_edge_clearance_nm, max_track_width_nm, max_via_diameter_nm
        let mut e4 = valid_eff.clone();
        e4.net_rules.get_mut(&1).unwrap().edge_clearance_nm = 300_000;
        assert_eq!(e4.validate().unwrap_err().status, Status::Invalid);

        let mut e5 = valid_eff.clone();
        e5.net_rules.get_mut(&1).unwrap().width_nm = 350_000;
        assert_eq!(e5.validate().unwrap_err().status, Status::Invalid);

        let mut e6 = valid_eff.clone();
        e6.net_rules.get_mut(&1).unwrap().via_diameter_nm = 900_000;
        assert_eq!(e6.validate().unwrap_err().status, Status::Invalid);

        // Layer-scoped DRU annular_width inflating required via diameter
        let mut e6_dru = valid_eff.clone();
        e6_dru.dru_rules.push(DruRule {
            name: "ann".into(),
            layer: Some("F.Cu".into()),
            condition: None,
            constraint: DruConstraint::AnnularWidth { min_nm: 500_000 },
            severity: DruSeverity::Error,
        });
        assert_eq!(e6_dru.validate().unwrap_err().status, Status::Invalid);

        // Invalid dru_rules values (negative or min > max)
        let mut e_neg_dru = valid_eff.clone();
        e_neg_dru.dru_rules.push(DruRule {
            name: "neg".into(),
            layer: None,
            condition: None,
            constraint: DruConstraint::Clearance { min_nm: -1 },
            severity: DruSeverity::Ignore,
        });
        assert_eq!(e_neg_dru.validate().unwrap_err().status, Status::Invalid);

        let mut e_bad_range = valid_eff.clone();
        e_bad_range.dru_rules.push(DruRule {
            name: "range".into(),
            layer: None,
            condition: None,
            constraint: DruConstraint::TrackWidth {
                min_nm: 400_000,
                max_nm: Some(200_000),
            },
            severity: DruSeverity::Error,
        });
        e_bad_range.max_track_width_nm = 400_000;
        assert_eq!(e_bad_range.validate().unwrap_err().status, Status::Invalid);

        // Positive controls:
        // 1) DruSeverity::Ignore does not require max_pair_clearance_nm bump
        let mut e_ign = valid_eff.clone();
        e_ign.dru_rules.push(DruRule {
            name: "ign".into(),
            layer: None,
            condition: None,
            constraint: DruConstraint::Clearance { min_nm: 5_000_000 },
            severity: DruSeverity::Ignore,
        });
        e_ign.validate().unwrap();

        // 2) allow_microvias=false with larger microvia_diameter_nm still passes validate()
        let mut e_no_micro = valid_eff.clone();
        e_no_micro.allow_microvias = false;
        e_no_micro.max_via_diameter_nm = 200_000;
        for nr in e_no_micro.net_rules.values_mut() {
            nr.via_diameter_nm = 200_000;
            nr.via_drill_nm = 100_000;
            nr.microvia_diameter_nm = 1_000_000;
            nr.microvia_drill_nm = 500_000;
        }
        e_no_micro.validate().unwrap();

        // 3) hole_to_hole_active=false does not require min_hole_to_hole_nm bump
        let mut e_h2h_off = valid_eff.clone();
        e_h2h_off.minimums.hole_to_hole_active = false;
        e_h2h_off.minimums.min_hole_to_hole_nm = 2_000_000;
        e_h2h_off.max_hole_to_hole_nm = 0;
        e_h2h_off.validate().unwrap();

        // Task 3: via_drill_nm == 0, via_drill_nm >= via_diameter_nm, allow_microvias=true invalid microvia dims,
        // width_nm > MAX_NM, and > 2*MAX_NM upper bounds.
        let mut e_zero_drill = valid_eff.clone();
        e_zero_drill.net_rules.get_mut(&1).unwrap().via_drill_nm = 0;
        assert_eq!(e_zero_drill.validate().unwrap_err().status, Status::Invalid);

        let mut e_drill_eq_diam = valid_eff.clone();
        e_drill_eq_diam.net_rules.get_mut(&1).unwrap().via_drill_nm = 600_000;
        e_drill_eq_diam
            .net_rules
            .get_mut(&1)
            .unwrap()
            .via_diameter_nm = 600_000;
        assert_eq!(
            e_drill_eq_diam.validate().unwrap_err().status,
            Status::Invalid
        );

        let mut e_zero_micro = valid_eff.clone();
        e_zero_micro.allow_microvias = true;
        e_zero_micro
            .net_rules
            .get_mut(&1)
            .unwrap()
            .microvia_diameter_nm = 0;
        e_zero_micro
            .net_rules
            .get_mut(&1)
            .unwrap()
            .microvia_drill_nm = 0;
        assert_eq!(e_zero_micro.validate().unwrap_err().status, Status::Invalid);

        let mut e_micro_eq = valid_eff.clone();
        e_micro_eq.allow_microvias = true;
        e_micro_eq
            .net_rules
            .get_mut(&1)
            .unwrap()
            .microvia_diameter_nm = 300_000;
        e_micro_eq.net_rules.get_mut(&1).unwrap().microvia_drill_nm = 300_000;
        assert_eq!(e_micro_eq.validate().unwrap_err().status, Status::Invalid);

        let mut e_wide_track = valid_eff.clone();
        e_wide_track.net_rules.get_mut(&1).unwrap().width_nm = MAX_NM + 1;
        e_wide_track.max_track_width_nm = MAX_NM + 1;
        assert_eq!(e_wide_track.validate().unwrap_err().status, Status::Invalid);

        let mut e_huge_max = valid_eff.clone();
        e_huge_max.max_pair_clearance_nm = 2 * MAX_NM + 1;
        assert_eq!(e_huge_max.validate().unwrap_err().status, Status::Invalid);

        let mut e_via_drill_dru = valid_eff;
        e_via_drill_dru.dru_rules.push(DruRule {
            name: "big_drill".into(),
            layer: None,
            condition: None,
            constraint: DruConstraint::ViaDrill {
                min_nm: 600_000,
                max_nm: None,
            },
            severity: DruSeverity::Error,
        });
        assert_eq!(
            e_via_drill_dru
                .via_dims_for(1, false, &["F.Cu".into(), "B.Cu".into()])
                .unwrap_err()
                .status,
            Status::Unroutable
        );
    }

    #[test]
    fn use_height_precedence_and_board_thickness_validation() {
        let mut inp: Input = serde_json::from_value(serde_json::json!({
            "version": 1,
            "source_id": "d".repeat(64),
            "layer": "F.Cu",
            "layers": ["F.Cu", "B.Cu"],
            "bounds": {"min": [0, 0], "max": [10_000_000, 10_000_000]},
            "nets": [{"id": 0, "name": ""}, {"id": 1, "name": "SIG"}],
            "pads": [],
            "tracks": [],
            "board_design_settings": {
                "use_height_for_length_calcs": true,
                "board_thickness_nm": 1_600_000,
                "outer_solder_mask_thickness_nm": 20_000
            },
            "max_expansions": 100
        }))
        .unwrap();
        // Omitted in .kicad_pro -> falls back to board_design_settings (true)
        inp.project_rules = Some(
            serde_json::json!({
                "meta": {"version": 3},
                "board": {"design_settings": {"rules": {}}}
            })
            .to_string(),
        );
        let eff_omit = resolve_effective_rules(&mut inp).unwrap();
        assert!(eff_omit.use_height_for_length_calcs);
        assert_eq!(eff_omit.board_thickness_nm, Some(1_600_000));
        assert_eq!(eff_omit.outer_solder_mask_thickness_nm, Some(20_000));

        // Explicit false in .kicad_pro design_settings.rules overrides board_design_settings true
        inp.project_rules = Some(
            serde_json::json!({
                "meta": {"version": 3},
                "board": {"design_settings": {"rules": {"use_height_for_length_calcs": false}}}
            })
            .to_string(),
        );
        let eff_false = resolve_effective_rules(&mut inp).unwrap();
        assert!(!eff_false.use_height_for_length_calcs);

        // use_height_for_length_calcs outside .rules is ignored by KiCad 10.0.8
        inp.project_rules = Some(
            serde_json::json!({
                "meta": {"version": 3},
                "board": {
                    "design_settings": {
                        "use_height_for_length_calcs": false,
                        "rules": {}
                    }
                }
            })
            .to_string(),
        );
        let eff_ds_ignored = resolve_effective_rules(&mut inp).unwrap();
        assert!(eff_ds_ignored.use_height_for_length_calcs);

        // Invalid board_thickness_nm (0) and mask > board_thickness fail closed
        inp.board_design_settings
            .as_mut()
            .unwrap()
            .board_thickness_nm = Some(0);
        assert_eq!(
            resolve_effective_rules(&mut inp).unwrap_err().status,
            Status::Invalid
        );
        inp.board_design_settings
            .as_mut()
            .unwrap()
            .board_thickness_nm = Some(10_000);
        inp.board_design_settings
            .as_mut()
            .unwrap()
            .outer_solder_mask_thickness_nm = Some(20_000);
        assert_eq!(
            resolve_effective_rules(&mut inp).unwrap_err().status,
            Status::Invalid
        );
    }

    #[test]
    fn via_overrides_strict_signal_layer_parsing() {
        let pro = serde_json::json!({
            "meta": {"version": 3},
            "tuning_profiles": {
                "tuning_profiles_impedance_geometric": [{
                    "profile_name": "P1",
                    "type": 0,
                    "target_impedance": 50,
                    "enable_time_domain_tuning": true,
                    "via_prop_delay": 7_500_000,
                    "layer_entries": [{"signal_layer": "F.Cu", "width": 250_000, "delay": 6_000_000}],
                    "via_overrides": [{
                        "start_layer": "F.Cu",
                        "end_layer": "In2.Cu",
                        "via_layer_from": "F.Cu",
                        "via_layer_to": "B.Cu",
                        "via_diameter": 500_000,
                        "delay": 90_000_000
                    }]
                }]
            }
        })
        .to_string();
        let parsed = parse_kicad_pro(pro.as_bytes()).unwrap();
        let ov = &parsed.tuning_profiles[0].via_overrides[0];
        assert_eq!(ov.signal_layer_from, "");
        assert_eq!(ov.signal_layer_to, "");
        assert_eq!(ov.via_layer_from, "F.Cu");
        assert_eq!(ov.via_layer_to, "B.Cu");
        assert_eq!(ov.via_diameter_nm, 500_000);
        assert_eq!(ov.delay_as, 90_000_000);
    }

    #[test]
    fn tuning_profiles_float_and_malformed_numeric_fields() {
        let mk_pro =
            |via_prop_delay: serde_json::Value, le: serde_json::Value, vo: serde_json::Value| {
                serde_json::json!({
                    "meta": {"version": 3},
                    "tuning_profiles": {
                        "tuning_profiles_impedance_geometric": [{
                            "profile_name": "P1",
                            "type": 0.0,
                            "target_impedance": 50.0,
                            "enable_time_domain_tuning": true,
                            "via_prop_delay": via_prop_delay,
                            "layer_entries": [le],
                            "via_overrides": [vo]
                        }]
                    }
                })
                .to_string()
            };
        let valid = mk_pro(
            serde_json::json!(9e7),
            serde_json::json!({
                "signal_layer": "F.Cu",
                "width": 250_000.0,
                "diff_pair_gap": 150_000.0,
                "delay": 6_000_000.6
            }),
            serde_json::json!({
                "signal_layer_from": "F.Cu",
                "signal_layer_to": "In2.Cu",
                "via_layer_from": "F.Cu",
                "via_layer_to": "B.Cu",
                "via_diameter": 600_000.0,
                "delay": 90_000_000.6
            }),
        );
        let parsed = parse_kicad_pro(valid.as_bytes()).unwrap();
        let prof = &parsed.tuning_profiles[0];
        assert_eq!(prof.target_impedance_ohm, 50);
        assert_eq!(prof.via_prop_delay_as_per_mm, 90_000_000);
        assert_eq!(prof.layer_entries[0].width_nm, 250_000);
        assert_eq!(prof.layer_entries[0].diff_pair_gap_nm, 150_000);
        assert_eq!(prof.layer_entries[0].delay_as_per_mm, 6_000_001);
        assert_eq!(prof.via_overrides[0].via_diameter_nm, 600_000);
        assert_eq!(prof.via_overrides[0].delay_as, 90_000_001);

        let ok_le =
            serde_json::json!({"signal_layer": "F.Cu", "width": 250_000, "delay": 6_000_000});
        let ok_vo = serde_json::json!({
            "signal_layer_from": "F.Cu",
            "signal_layer_to": "B.Cu",
            "via_layer_from": "F.Cu",
            "via_layer_to": "B.Cu",
            "delay": 90_000_000
        });
        for bad_delay in [
            serde_json::json!(-1.0),
            serde_json::json!(-0.4),
            serde_json::json!("90000000"),
            serde_json::Value::Null,
            serde_json::json!(true),
        ] {
            let bad_vo = serde_json::json!({
                "signal_layer_from": "F.Cu",
                "signal_layer_to": "B.Cu",
                "via_layer_from": "F.Cu",
                "via_layer_to": "B.Cu",
                "delay": bad_delay
            });
            let s = mk_pro(serde_json::json!(7_500_000), ok_le.clone(), bad_vo);
            assert_eq!(
                parse_kicad_pro(s.as_bytes()).unwrap_err().status,
                Status::Invalid
            );
        }
        // Omitted via_overrides[].delay must fail closed Invalid
        let missing_delay_vo = serde_json::json!({
            "signal_layer_from": "F.Cu",
            "signal_layer_to": "B.Cu",
            "via_layer_from": "F.Cu",
            "via_layer_to": "B.Cu"
        });
        let s_missing = mk_pro(
            serde_json::json!(7_500_000),
            ok_le.clone(),
            missing_delay_vo,
        );
        assert_eq!(
            parse_kicad_pro(s_missing.as_bytes()).unwrap_err().status,
            Status::Invalid
        );
        // Negative float on via_prop_delay and layer_entries[].delay must also fail Invalid
        let s_bad_vp = mk_pro(serde_json::json!(-0.4), ok_le, ok_vo.clone());
        assert_eq!(
            parse_kicad_pro(s_bad_vp.as_bytes()).unwrap_err().status,
            Status::Invalid
        );
        let bad_le = serde_json::json!({"signal_layer": "F.Cu", "width": 250_000, "delay": -0.4});
        let s_bad_le = mk_pro(serde_json::json!(7_500_000), bad_le, ok_vo);
        assert_eq!(
            parse_kicad_pro(s_bad_le.as_bytes()).unwrap_err().status,
            Status::Invalid
        );
    }
}
