use crate::geometry;
use crate::model::{
    DiffPairsMode, Failure, Input, Point, ProtectMode, RoutingOptions, StackupKind, Status, Track,
    Via, ViaKind,
};
use crate::rules::{
    eval_dru_expr_for_context, DruConstraint, DruItem, DruItemKind, DruSeverity, EffectiveRules,
    TuningProfile,
};
use crate::search::PlacedViaKey;
use crate::spatial::{DrillGeom, PadCore, SweptAabb};
use crate::{Engine, TransactionError};
use std::collections::{BTreeMap, BTreeSet, BinaryHeap};
use std::ops::ControlFlow;

pub use crate::negotiate::RouteUnitId;
use crate::negotiate::{
    BlockedFrontier, CongestionHistory, RouteUnitAdapter, TrialBudget, UnitRouteError, UnitSpec,
};

#[derive(Clone, Debug)]
pub struct ImpedancePlan {
    pub target_ohm: f64,
    pub tolerance_pct: f64,
    pub reference_net: u32,
    pub reference_layers: Vec<String>,
    pub signal_layer: String,
    pub solved_width_nm: i64,
    pub solved_ohm: f64,
}

#[derive(Clone, Debug)]
pub struct ResolvedDiffPair {
    pub index: usize,
    pub stem: String,
    pub pos_net: u32,
    pub neg_net: u32,
    pub pos_name: String,
    pub neg_name: String,
    pub class_name: String,
    pub width_nm: i64,
    pub class_width_nm: i64,
    pub gap_nm: i64,
    pub gap_min_nm: i64,
    pub gap_max_nm: i64,
    pub via_gap_nm: i64,
    pub clearance_nm: i64,
    pub uncoupled_max_nm: i64,
    pub has_dru_gap_max: bool,
    pub dru_skew_max_nm: Option<i64>,
    pub impedance: Option<ImpedancePlan>,
}

#[derive(Clone, Debug)]
pub enum GroupTargetMode {
    LengthNm { tolerance_nm: i64 },
    DelayAs { tolerance_as: i64 },
}

#[derive(Clone, Debug)]
pub struct ResolvedLengthGroup {
    pub members: Vec<u32>,
    pub member_names: Vec<String>,
    pub contains_diff_pairs: bool,
    pub mode: GroupTargetMode,
    pub amplitude_nm: i64,
    pub spacing_widths: f64,
}

#[derive(Clone, Debug)]
pub struct ResolvedAcChain {
    pub bridges: Vec<String>,
    pub p_nets: Vec<u32>,
    pub n_nets: Vec<u32>,
    pub pair_indices: Vec<usize>,
}

#[derive(Clone, Debug)]
pub struct DiffPairPlan {
    pub pairs: Vec<ResolvedDiffPair>,
    pub length_groups: Vec<ResolvedLengthGroup>,
    pub ac_chains: Vec<ResolvedAcChain>,
    pub ac_match_tolerance_nm: i64,
    pub ac_meander_amplitude_nm: i64,
    pub pair_layers: Vec<String>,
    pub active_nets: Option<BTreeSet<u32>>,
    pub protected_nets: BTreeSet<u32>,
    pub protect_mode: ProtectMode,
    pub no_short_pair_deferral: bool,
    pub intra_match: bool,
    pub intra_tolerance_nm: i64,
    pub intra_amplitude_nm: i64,
}

#[derive(Clone, Debug)]
pub struct DiffPairRunState {
    pub routed_pair_nets: BTreeSet<u32>,
    pub active_single_ended_nets: Option<BTreeSet<u32>>,
    pub protected_nets: BTreeSet<u32>,
    pub(crate) plan: DiffPairPlan,
}

pub fn unit_members(plan: &DiffPairPlan, unit: RouteUnitId) -> Vec<u32> {
    match unit {
        RouteUnitId::DiffPair(idx) => plan
            .pairs
            .get(idx)
            .map(|p| vec![p.pos_net, p.neg_net])
            .unwrap_or_default(),
        RouteUnitId::SingleNet(net) => vec![net],
    }
}

fn unwrap_tx<T>(res: Result<T, TransactionError<Failure>>) -> Result<T, Failure> {
    match res {
        Ok(v) => Ok(v),
        Err(TransactionError::Operation(f)) | Err(TransactionError::Limit(f)) => Err(f),
    }
}

fn glob_match(pattern: &str, text: &str) -> bool {
    let p: Vec<char> = pattern.chars().collect();
    let t: Vec<char> = text.chars().collect();
    let mut dp = vec![vec![false; t.len() + 1]; p.len() + 1];
    dp[0][0] = true;
    for i in 1..=p.len() {
        if p[i - 1] == '*' {
            dp[i][0] = dp[i - 1][0];
        }
    }
    for i in 1..=p.len() {
        for j in 1..=t.len() {
            if p[i - 1] == '*' {
                dp[i][j] = dp[i - 1][j] || dp[i][j - 1];
            } else if p[i - 1] == '?' || p[i - 1] == t[j - 1] {
                dp[i][j] = dp[i - 1][j - 1];
            }
        }
    }
    dp[p.len()][t.len()]
}

fn net_matches_filter_token(pattern: &str, net_name: &str) -> bool {
    if glob_match(pattern, net_name) {
        return true;
    }
    if !pattern.contains('/') {
        let leaf = net_name.rsplit('/').next().unwrap_or(net_name);
        if glob_match(pattern, leaf) {
            return true;
        }
    }
    false
}

fn compute_active_nets(
    input: &Input,
    opts: &RoutingOptions,
) -> Result<Option<BTreeSet<u32>>, Failure> {
    if opts.net_filters.is_empty() {
        return Ok(None);
    }
    let mut pos = Vec::new();
    let mut neg = Vec::new();
    for raw in &opts.net_filters {
        if let Some(pat) = raw.strip_prefix('!') {
            if pat.is_empty() {
                return Err(Failure::new(
                    Status::Invalid,
                    "--nets exclusion pattern cannot be empty",
                ));
            }
            neg.push(pat);
        } else {
            if raw.is_empty() {
                return Err(Failure::new(
                    Status::Invalid,
                    "--nets filter pattern cannot be empty",
                ));
            }
            pos.push(raw.as_str());
        }
    }
    let mut out = BTreeSet::new();
    for n in &input.nets {
        if n.id == 0 {
            continue;
        }
        let pos_ok = pos.is_empty() || pos.iter().any(|p| net_matches_filter_token(p, &n.name));
        let neg_hit = neg.iter().any(|p| net_matches_filter_token(p, &n.name));
        if pos_ok && !neg_hit {
            out.insert(n.id);
        }
    }
    if out.is_empty() {
        return Err(Failure::new(
            Status::Invalid,
            "--nets filter matched zero nets",
        ));
    }
    Ok(Some(out))
}

fn compute_protected_nets(tracks: &[Track], vias: &[Via], mode: ProtectMode) -> BTreeSet<u32> {
    let mut out = BTreeSet::new();
    match mode {
        ProtectMode::None => {}
        ProtectMode::Existing => {
            for t in tracks {
                if t.net != 0 {
                    out.insert(t.net);
                }
            }
            for v in vias {
                if v.net != 0 {
                    out.insert(v.net);
                }
            }
        }
        ProtectMode::Human | ProtectMode::Locked => {
            for t in tracks {
                if t.net != 0 && t.locked {
                    out.insert(t.net);
                }
            }
            for v in vias {
                if v.net != 0 && v.locked {
                    out.insert(v.net);
                }
            }
        }
    }
    out
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PairConv {
    UnderscorePN,
    PlusMinus,
    BarePN,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PairRole {
    Pos,
    Neg,
}

fn classify_pair_name(name: &str) -> Option<(String, PairConv, PairRole)> {
    let (prefix, leaf) = match name.rfind('/') {
        Some(idx) => (&name[..=idx], &name[idx + 1..]),
        None => ("", name),
    };
    if leaf.starts_with("Net-(") || name.starts_with("Net-(") {
        return None;
    }
    if let Some(stem) = leaf.strip_suffix("_P") {
        if !stem.is_empty() {
            return Some((
                format!("{prefix}{stem}"),
                PairConv::UnderscorePN,
                PairRole::Pos,
            ));
        }
    }
    if let Some(stem) = leaf.strip_suffix("_N") {
        if !stem.is_empty() {
            return Some((
                format!("{prefix}{stem}"),
                PairConv::UnderscorePN,
                PairRole::Neg,
            ));
        }
    }
    if let Some(stem) = leaf.strip_suffix('+') {
        if !stem.is_empty() {
            return Some((
                format!("{prefix}{stem}"),
                PairConv::PlusMinus,
                PairRole::Pos,
            ));
        }
    }
    if let Some(stem) = leaf.strip_suffix('-') {
        if !stem.is_empty() {
            return Some((
                format!("{prefix}{stem}"),
                PairConv::PlusMinus,
                PairRole::Neg,
            ));
        }
    }
    if let Some(stem) = leaf.strip_suffix('P') {
        if !stem.is_empty() {
            return Some((format!("{prefix}{stem}"), PairConv::BarePN, PairRole::Pos));
        }
    }
    if let Some(stem) = leaf.strip_suffix('N') {
        if !stem.is_empty() {
            return Some((format!("{prefix}{stem}"), PairConv::BarePN, PairRole::Neg));
        }
    }
    None
}

/// KiCad `DRC_ENGINE::MatchDpSuffix`: scans from the end skipping ASCII digits and `'_'`,
/// then matches `'+'`/`'-'`/`'P'`/`'N'` and returns `(complement_net_name, dru_base_stem)`.
fn match_dp_suffix(name: &str) -> Option<(String, String)> {
    if name.starts_with("Net-(") {
        return None;
    }
    let bytes = name.as_bytes();
    let mut i = bytes.len();
    while i > 0 {
        let b = bytes[i - 1];
        if b.is_ascii_digit() || b == b'_' {
            i -= 1;
            continue;
        }
        let comp = match b {
            b'+' => '-',
            b'-' => '+',
            b'P' => 'N',
            b'N' => 'P',
            _ => return None,
        };
        let base = &name[..i - 1];
        if base.is_empty() {
            return None;
        }
        let tail = &name[i..];
        return Some((format!("{base}{comp}{tail}"), base.to_owned()));
    }
    None
}

// ---------------------------------------------------------------------------
// Closed-form Differential Impedance Equations (ported from impedance.py)
// ---------------------------------------------------------------------------

fn microstrip_z0_air(u: f64) -> f64 {
    if u <= 0.0 {
        return 0.0;
    }
    if u <= 1.0 {
        60.0 * (8.0 / u + 0.25 * u).ln()
    } else {
        (120.0 * std::f64::consts::PI) / (u + 1.393 + 0.667 * (u + 1.444).ln())
    }
}

pub fn microstrip_z0(w_mm: f64, h_mm: f64, t_mm: f64, er: f64) -> f64 {
    let mut u = w_mm / h_mm;
    let mut ur = u;
    if t_mm > 0.0 {
        let th = (6.517 * u).sqrt().tanh();
        let du1 = (t_mm / std::f64::consts::PI)
            * (1.0 + (4.0 * std::f64::consts::E * h_mm / t_mm) * th * th).ln();
        let du_r = 0.5 * (1.0 + 1.0 / (er - 1.0).max(0.0).sqrt().cosh()) * du1;
        u = (w_mm + du1) / h_mm;
        ur = (w_mm + du_r) / h_mm;
    }
    let mut er_eff = 0.5 * (er + 1.0) + 0.5 * (er - 1.0) / (1.0 + 12.0 / ur).sqrt();
    if t_mm > 0.0 {
        let z_air1 = microstrip_z0_air(u);
        let z_air_r = microstrip_z0_air(ur);
        if z_air_r > 0.0 {
            let ratio = z_air1 / z_air_r;
            er_eff *= ratio * ratio;
        }
    }
    if ur <= 1.0 {
        (60.0 / er_eff.sqrt()) * (8.0 / ur + 0.25 * ur).ln()
    } else {
        (120.0 * std::f64::consts::PI / er_eff.sqrt()) / (ur + 1.393 + 0.667 * (ur + 1.444).ln())
    }
}

pub fn differential_microstrip_z0(w_mm: f64, s_mm: f64, h_mm: f64, t_mm: f64, er: f64) -> f64 {
    let z0 = microstrip_z0(w_mm, h_mm, t_mm, er);
    let k = 0.48 * (-0.96 * s_mm / h_mm).exp();
    2.0 * z0 * (1.0 - k)
}

pub fn stripline_z0(w_mm: f64, h_mm: f64, t_mm: f64, er: f64) -> f64 {
    let b = 2.0 * h_mm + t_mm;
    let m = 6.0 * h_mm / (3.0 * h_mm + t_mm);
    let term1 = (t_mm / (2.0 * h_mm + t_mm)).powi(2);
    let term2 = (t_mm / (m * std::f64::consts::PI * w_mm + 1.1 * t_mm)).powi(2);
    let w_eff = w_mm + (t_mm / std::f64::consts::PI) * (1.0 - 0.5 * (term1 + term2).ln());
    let cf = 1.0 / (1.0 + (w_eff / b / 0.35).powf(2.5));
    (60.0 / er.sqrt()) * (1.9 * b / (0.8 * w_eff + t_mm) * (1.0 + cf * 0.4)).ln()
}

pub fn differential_stripline_z0(w_mm: f64, s_mm: f64, h_mm: f64, t_mm: f64, er: f64) -> f64 {
    let z0 = stripline_z0(w_mm, h_mm, t_mm, er);
    let k = 0.347 * (-2.9 * s_mm / (2.0 * h_mm)).exp();
    2.0 * z0 * (1.0 - k)
}

fn solve_diff_width_for_z0(
    is_stripline: bool,
    target_zdiff: f64,
    s_mm: f64,
    h_mm: f64,
    t_mm: f64,
    er: f64,
) -> Result<(f64, f64), Failure> {
    let eval = |w: f64| -> f64 {
        if is_stripline {
            differential_stripline_z0(w, s_mm, h_mm, t_mm, er)
        } else {
            differential_microstrip_z0(w, s_mm, h_mm, t_mm, er)
        }
    };
    let mut lo = 0.01_f64;
    let mut hi = 20.0_f64;
    let z_lo = eval(lo);
    let z_hi = eval(hi);
    if !(z_lo.is_finite() && z_hi.is_finite()) || target_zdiff > z_lo || target_zdiff < z_hi {
        return Err(Failure::new(
            Status::Invalid,
            format!(
                "target differential impedance {target_zdiff:.2} ohm is outside solvable range [{z_hi:.2}, {z_lo:.2}]"
            ),
        ));
    }
    for _ in 0..80 {
        let mid = 0.5 * (lo + hi);
        if eval(mid) > target_zdiff {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    let w = 0.5 * (lo + hi);
    Ok((w, eval(w)))
}

fn resolve_impedance_plan(
    input: &Input,
    opts: &RoutingOptions,
    signal_layer: &str,
    class_width_nm: i64,
    nominal_gap_nm: i64,
    max_width_nm: Option<i64>,
) -> Result<ImpedancePlan, Failure> {
    let target_ohm = opts
        .pair_impedance_ohm
        .ok_or_else(|| Failure::new(Status::Invalid, "missing --pair-impedance value"))?;
    let tolerance_pct = opts
        .impedance_tolerance_pct
        .ok_or_else(|| Failure::new(Status::Invalid, "missing --impedance-tolerance value"))?;
    let ref_name = opts
        .impedance_reference
        .as_deref()
        .ok_or_else(|| Failure::new(Status::Invalid, "missing --impedance-reference net name"))?;
    let uncoupled_imp = opts
        .impedance_uncoupled_nm
        .ok_or_else(|| Failure::new(Status::Invalid, "missing --impedance-uncoupled value"))?;
    if uncoupled_imp <= 0 {
        return Err(Failure::new(
            Status::Invalid,
            "--impedance-uncoupled must be > 0",
        ));
    }

    let ref_net = input
        .nets
        .iter()
        .find(|n| n.id != 0 && n.name == ref_name)
        .map(|n| n.id)
        .ok_or_else(|| {
            Failure::new(
                Status::Invalid,
                format!("impedance reference net {ref_name:?} not found on board"),
            )
        })?;

    let stack_cd: Vec<&crate::model::StackupLayer> = input
        .stackup
        .iter()
        .filter(|s| {
            matches!(
                s.kind,
                StackupKind::Copper | StackupKind::Core | StackupKind::Prepreg
            )
        })
        .collect();
    if stack_cd.is_empty() {
        return Err(Failure::new(
            Status::Invalid,
            "board stackup is required for impedance control",
        ));
    }
    let sig_pos = stack_cd
        .iter()
        .position(|s| s.kind == StackupKind::Copper && s.name == signal_layer)
        .ok_or_else(|| {
            Failure::new(
                Status::Invalid,
                format!("signal layer {signal_layer:?} missing from board stackup"),
            )
        })?;
    let sig_t_nm = stack_cd[sig_pos].thickness_nm;
    if sig_t_nm <= 0 {
        return Err(Failure::new(
            Status::Invalid,
            format!("signal layer {signal_layer:?} missing positive copper thickness in stackup"),
        ));
    }

    let is_di = |k: StackupKind| matches!(k, StackupKind::Core | StackupKind::Prepreg);

    let (is_stripline, h_nm, er, ref_layers) = if sig_pos == 0 || sig_pos + 1 == stack_cd.len() {
        let (di_pos, ref_pos) = if sig_pos == 0 {
            (1usize, 2usize)
        } else {
            if sig_pos < 2 {
                return Err(Failure::new(
                    Status::Invalid,
                    "stackup lacks dielectric and reference plane adjacent to outer signal layer",
                ));
            }
            (sig_pos - 1, sig_pos - 2)
        };
        let di = stack_cd.get(di_pos).ok_or_else(|| {
            Failure::new(
                Status::Invalid,
                "missing dielectric adjacent to outer signal layer",
            )
        })?;
        let ref_cu = stack_cd.get(ref_pos).ok_or_else(|| {
            Failure::new(
                Status::Invalid,
                "missing reference copper layer adjacent to dielectric",
            )
        })?;
        if !is_di(di.kind) || ref_cu.kind != StackupKind::Copper {
            return Err(Failure::new(
                Status::Invalid,
                "invalid stackup ordering around microstrip signal layer",
            ));
        }
        let h_nm = di.thickness_nm;
        if h_nm <= 0 {
            return Err(Failure::new(
                Status::Invalid,
                "dielectric thickness missing or non-positive",
            ));
        }
        let er = di
            .epsilon_r
            .filter(|&v| v.is_finite() && v > 0.0)
            .ok_or_else(|| {
                Failure::new(
                    Status::Invalid,
                    "dielectric epsilon_r missing or non-positive",
                )
            })?;
        (false, h_nm, er, vec![ref_cu.name.clone()])
    } else {
        if sig_pos < 2 || sig_pos + 2 >= stack_cd.len() {
            return Err(Failure::new(
                Status::Invalid,
                "stripline signal layer lacks two bounding dielectric and reference copper layers",
            ));
        }
        let di_top = stack_cd[sig_pos - 1];
        let di_bot = stack_cd[sig_pos + 1];
        let ref_top = stack_cd[sig_pos - 2];
        let ref_bot = stack_cd[sig_pos + 2];
        if !is_di(di_top.kind)
            || !is_di(di_bot.kind)
            || ref_top.kind != StackupKind::Copper
            || ref_bot.kind != StackupKind::Copper
        {
            return Err(Failure::new(
                Status::Invalid,
                "invalid stackup ordering around stripline signal layer",
            ));
        }
        let h_top = di_top.thickness_nm;
        if h_top <= 0 {
            return Err(Failure::new(
                Status::Invalid,
                "top stripline dielectric thickness missing",
            ));
        }
        let h_bot = di_bot.thickness_nm;
        if h_bot <= 0 {
            return Err(Failure::new(
                Status::Invalid,
                "bottom stripline dielectric thickness missing",
            ));
        }
        if (h_top - h_bot).abs() > 1 {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "asymmetric stripline dielectrics ({h_top} nm vs {h_bot} nm > 1 nm) are unsupported"
                ),
            ));
        }
        let er_top = di_top
            .epsilon_r
            .filter(|&v| v.is_finite() && v > 0.0)
            .ok_or_else(|| {
                Failure::new(
                    Status::Invalid,
                    "top stripline dielectric epsilon_r missing",
                )
            })?;
        let er_bot = di_bot
            .epsilon_r
            .filter(|&v| v.is_finite() && v > 0.0)
            .ok_or_else(|| {
                Failure::new(
                    Status::Invalid,
                    "bottom stripline dielectric epsilon_r missing",
                )
            })?;
        if (er_top - er_bot).abs() > 1e-6 {
            return Err(Failure::new(
                Status::Invalid,
                format!("mismatched stripline dielectric epsilon_r ({er_top} vs {er_bot})"),
            ));
        }
        (
            true,
            (h_top + h_bot) / 2,
            er_top,
            vec![ref_top.name.clone(), ref_bot.name.clone()],
        )
    };

    for rl in &ref_layers {
        let has_fill = input.zones.iter().any(|z| {
            z.net == ref_net
                && z.layer_fills
                    .iter()
                    .any(|lf| lf.layer == *rl && !lf.polygons.is_empty())
        });
        if !has_fill {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "impedance reference net {ref_name:?} has no filled zone on reference layer {rl:?}"
                ),
            ));
        }
    }

    let s_mm = nominal_gap_nm as f64 / 1_000_000.0;
    let h_mm = h_nm as f64 / 1_000_000.0;
    let t_mm = sig_t_nm as f64 / 1_000_000.0;
    let (solved_w_mm, solved_ohm_raw) =
        solve_diff_width_for_z0(is_stripline, target_ohm, s_mm, h_mm, t_mm, er)?;
    let mut solved_width_nm = (solved_w_mm * 1_000_000.0).round() as i64;
    let mut solved_ohm = solved_ohm_raw;
    if solved_width_nm < class_width_nm {
        let floor_mm = class_width_nm as f64 / 1_000_000.0;
        let floor_z = if is_stripline {
            differential_stripline_z0(floor_mm, s_mm, h_mm, t_mm, er)
        } else {
            differential_microstrip_z0(floor_mm, s_mm, h_mm, t_mm, er)
        };
        let err_pct = ((floor_z - target_ohm).abs() / target_ohm) * 100.0;
        if err_pct <= tolerance_pct {
            solved_width_nm = class_width_nm;
            solved_ohm = floor_z;
        } else {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "solved width {solved_width_nm} nm is below native pair width floor {class_width_nm} nm ({floor_z:.2} ohm exceeds {tolerance_pct}% tolerance)"
                ),
            ));
        }
    }
    if let Some(max_w) = max_width_nm {
        if solved_width_nm > max_w {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "solved impedance width {solved_width_nm} nm exceeds DRC track_width max {max_w} nm"
                ),
            ));
        }
    }
    let err_pct = ((solved_ohm - target_ohm).abs() / target_ohm) * 100.0;
    if err_pct > tolerance_pct {
        return Err(Failure::new(
            Status::Invalid,
            format!("solved impedance {solved_ohm:.2} ohm outside +/-{tolerance_pct}%"),
        ));
    }

    Ok(ImpedancePlan {
        target_ohm,
        tolerance_pct,
        reference_net: ref_net,
        reference_layers: ref_layers,
        signal_layer: signal_layer.to_owned(),
        solved_width_nm,
        solved_ohm,
    })
}

fn is_valid_terminal_token(tok: &str) -> bool {
    !tok.is_empty()
        && tok
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'.')
}

fn validate_delay_member_profile(
    input: &Input,
    copper_tracks: &[Track],
    rules: &EffectiveRules,
    net_id: u32,
    net_name: &str,
    routable_layers: &[String],
) -> Result<(), Failure> {
    if !rules.use_height_for_length_calcs {
        return Err(Failure::new(
            Status::Invalid,
            "use_height_for_length_calcs must be enabled in board design settings for delay matching",
        ));
    }
    if input.stackup.is_empty()
        || !input
            .stackup
            .iter()
            .any(|sl| matches!(sl.kind, StackupKind::Core | StackupKind::Prepreg))
    {
        return Err(Failure::new(
            Status::Invalid,
            "complete board stackup with dielectric layers is required for delay matching",
        ));
    }
    for sl in &input.stackup {
        if matches!(
            sl.kind,
            StackupKind::Copper | StackupKind::Core | StackupKind::Prepreg
        ) && sl.thickness_nm <= 0
        {
            return Err(Failure::new(
                Status::Invalid,
                format!("stackup layer {:?} missing positive thickness", sl.name),
            ));
        }
        if matches!(sl.kind, StackupKind::Core | StackupKind::Prepreg)
            && sl.epsilon_r.is_none_or(|er| !er.is_finite() || er <= 0.0)
        {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "dielectric stackup layer {:?} missing positive epsilon_r",
                    sl.name
                ),
            ));
        }
    }
    let nr = rules.net(net_id);
    let prof_name = nr.tuning_profile.as_deref().ok_or_else(|| {
        Failure::new(
            Status::Invalid,
            format!("net {net_name:?} has no assigned tuning_profile for delay matching"),
        )
    })?;
    let prof = rules
        .tuning_profiles
        .iter()
        .find(|p| p.profile_name == prof_name)
        .ok_or_else(|| {
            Failure::new(
                Status::Invalid,
                format!("tuning_profile {prof_name:?} referenced by net {net_name:?} not found"),
            )
        })?;
    if prof.profile_type != 0 {
        return Err(Failure::new(
            Status::Invalid,
            format!(
                "tuning_profile {prof_name:?} must be a single-ended profile (type 0, got {})",
                prof.profile_type
            ),
        ));
    }
    if !prof.enable_time_domain_tuning {
        return Err(Failure::new(
            Status::Invalid,
            format!("tuning_profile {prof_name:?} has enable_time_domain_tuning disabled"),
        ));
    }
    if prof.layer_entries.is_empty()
        || prof
            .layer_entries
            .iter()
            .any(|le| le.delay_as_per_mm <= 0 || le.width_nm <= 0)
    {
        return Err(Failure::new(
            Status::Invalid,
            format!(
                "tuning_profile {prof_name:?} must declare positive width and delay on all layer entries"
            ),
        ));
    }
    if prof.via_prop_delay_as_per_mm <= 0 {
        return Err(Failure::new(
            Status::Invalid,
            format!("tuning_profile {prof_name:?} missing positive via delay"),
        ));
    }
    if prof.via_overrides.iter().any(|ov| ov.delay_as <= 0) {
        return Err(Failure::new(
            Status::Invalid,
            format!("tuning_profile {prof_name:?} via_overrides must have positive delay"),
        ));
    }
    for t in copper_tracks {
        if t.net == net_id {
            let entry = prof
                .layer_entries
                .iter()
                .find(|l| l.signal_layer == t.layer)
                .ok_or_else(|| {
                    Failure::new(
                        Status::Invalid,
                        format!(
                            "existing track on net {net_name:?} layer {:?} missing from tuning_profile {prof_name:?}",
                            t.layer
                        ),
                    )
                })?;
            if entry.delay_as_per_mm <= 0 {
                return Err(Failure::new(
                    Status::Invalid,
                    format!(
                        "tuning_profile {prof_name:?} layer {:?} has non-positive delay",
                        t.layer
                    ),
                ));
            }
            if entry.width_nm > 0 && t.width_nm != entry.width_nm {
                return Err(Failure::new(
                    Status::Invalid,
                    format!(
                        "existing track width {} nm on net {net_name:?} layer {:?} mismatches tuning_profile width {} nm",
                        t.width_nm, t.layer, entry.width_nm
                    ),
                ));
            }
        }
    }
    let has_routable = routable_layers.iter().any(|ln| {
        prof.layer_entries
            .iter()
            .any(|le| le.signal_layer == *ln && le.delay_as_per_mm > 0 && le.width_nm > 0)
    });
    if !has_routable {
        return Err(Failure::new(
            Status::Invalid,
            format!(
                "tuning_profile {prof_name:?} has no valid layer entry on enabled copper layers"
            ),
        ));
    }
    Ok(())
}

fn layer_matches_spec_local(layer: &str, spec: &str) -> bool {
    match spec {
        "*" => true,
        "outer" => layer == "F.Cu" || layer == "B.Cu",
        "inner" => layer != "F.Cu" && layer != "B.Cu",
        exact => layer == exact,
    }
}

fn net_pad_tokens(input: &Input, net_id: u32) -> Vec<String> {
    input
        .pads
        .iter()
        .filter(|p| p.net == net_id)
        .filter_map(|p| {
            let r = p.footprint_ref.as_deref()?;
            let n = p.pad_number.as_deref()?;
            Some(format!("{r}-{n}"))
        })
        .collect()
}

#[allow(clippy::too_many_arguments)]
fn resolve_pair_dru_bounds(
    input: &Input,
    rules: &EffectiveRules,
    display_stem: &str,
    dru_stem: &str,
    pos_net: u32,
    neg_net: u32,
    pos_name: &str,
    neg_name: &str,
    layers: &[String],
    pair_stem_by_net: &BTreeMap<u32, String>,
) -> Result<
    (
        Option<i64>,
        Option<i64>,
        Option<i64>,
        Option<i64>,
        Option<i64>,
    ),
    Failure,
> {
    let mut gap_min: Option<i64> = None;
    let mut gap_max: Option<i64> = None;
    let mut uncoupled_max: Option<i64> = None;
    let mut max_track_width: Option<i64> = None;
    let mut skew_max: Option<i64> = None;
    let board_layers: &[String] = if input.layers.is_empty() {
        std::slice::from_ref(&input.layer)
    } else {
        &input.layers
    };
    let pos_rules = rules.net(pos_net);
    let neg_rules = rules.net(neg_net);

    for (net_id, net_name) in [(pos_net, pos_name), (neg_net, neg_name)] {
        let nr_self = rules.net(net_id);
        let ft_owned = net_pad_tokens(input, net_id);
        let ft_ref = (!ft_owned.is_empty()).then_some(ft_owned.as_slice());
        let item = DruItem {
            net_id,
            net_name,
            net_class: &nr_self.class_name,
            class_members: &nr_self.class_members,
            kind: DruItemKind::Track,
            layers,
        };
        for r in &rules.dru_rules {
            if r.severity == DruSeverity::Ignore {
                continue;
            }
            if let Some(rl) = r.layer.as_deref() {
                if !layers.iter().any(|l| layer_matches_spec_local(l, rl)) {
                    continue;
                }
            }
            if let Some(cond) = &r.condition {
                if !eval_dru_expr_for_context(cond, &item, None, Some(dru_stem), ft_ref) {
                    continue;
                }
            }
            match r.constraint {
                DruConstraint::DiffPairGap { min_nm, max_nm } => {
                    if let Some(lo) = min_nm {
                        gap_min = Some(gap_min.map_or(lo, |cur| cur.max(lo)));
                    }
                    if let Some(hi) = max_nm {
                        gap_max = Some(gap_max.map_or(hi, |cur| cur.min(hi)));
                    }
                }
                DruConstraint::DiffPairUncoupled { max_nm, .. } => {
                    uncoupled_max = Some(uncoupled_max.map_or(max_nm, |cur| cur.min(max_nm)));
                }
                DruConstraint::TrackWidth {
                    max_nm: Some(mx), ..
                } => {
                    max_track_width = Some(max_track_width.map_or(mx, |cur| cur.min(mx)));
                }
                DruConstraint::Skew {
                    max_nm: Some(mx),
                    within_diff_pairs,
                    ..
                } => {
                    if within_diff_pairs {
                        skew_max = Some(skew_max.map_or(mx, |cur| cur.min(mx)));
                    } else if let Some(cond) = &r.condition {
                        let pos_ft = net_pad_tokens(input, pos_net);
                        let neg_ft = net_pad_tokens(input, neg_net);
                        let pos_item = DruItem {
                            net_id: pos_net,
                            net_name: pos_name,
                            net_class: &pos_rules.class_name,
                            class_members: &pos_rules.class_members,
                            kind: DruItemKind::Track,
                            layers,
                        };
                        let neg_item = DruItem {
                            net_id: neg_net,
                            net_name: neg_name,
                            net_class: &neg_rules.class_name,
                            class_members: &neg_rules.class_members,
                            kind: DruItemKind::Track,
                            layers,
                        };
                        let both_match = eval_dru_expr_for_context(
                            cond,
                            &pos_item,
                            None,
                            Some(dru_stem),
                            (!pos_ft.is_empty()).then_some(pos_ft.as_slice()),
                        ) && eval_dru_expr_for_context(
                            cond,
                            &neg_item,
                            None,
                            Some(dru_stem),
                            (!neg_ft.is_empty()).then_some(neg_ft.as_slice()),
                        );
                        let any_other = input.nets.iter().any(|on| {
                            if on.id == 0 || on.id == pos_net || on.id == neg_net {
                                return false;
                            }
                            let nr = rules.net(on.id);
                            let oft = net_pad_tokens(input, on.id);
                            let other_stem = pair_stem_by_net.get(&on.id).map(String::as_str);
                            let oitem = DruItem {
                                net_id: on.id,
                                net_name: &on.name,
                                net_class: &nr.class_name,
                                class_members: &nr.class_members,
                                kind: DruItemKind::Track,
                                layers: board_layers,
                            };
                            eval_dru_expr_for_context(
                                cond,
                                &oitem,
                                None,
                                other_stem,
                                (!oft.is_empty()).then_some(oft.as_slice()),
                            )
                        });
                        if both_match && !any_other {
                            skew_max = Some(skew_max.map_or(mx, |cur| cur.min(mx)));
                        }
                    }
                }
                _ => {}
            }
        }
    }
    if let (Some(lo), Some(hi)) = (gap_min, gap_max) {
        if lo > hi {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "conflicting DRC diff_pair_gap bounds ({lo} > {hi}) for pair {display_stem:?}"
                ),
            ));
        }
    }
    Ok((gap_min, gap_max, uncoupled_max, max_track_width, skew_max))
}

pub fn discover_and_validate(
    input: &Input,
    rules: &EffectiveRules,
) -> Result<DiffPairPlan, Failure> {
    discover_and_validate_with_copper(input, &input.tracks, &input.vias, rules)
}

fn discover_and_validate_with_copper(
    input: &Input,
    copper_tracks: &[Track],
    copper_vias: &[Via],
    rules: &EffectiveRules,
) -> Result<DiffPairPlan, Failure> {
    let empty_opts = RoutingOptions::default();
    let opts = input.routing_options.as_ref().unwrap_or(&empty_opts);
    let active_nets = compute_active_nets(input, opts)?;
    let protect_mode = opts.protect_mode.unwrap_or(ProtectMode::None);
    let protected_nets = compute_protected_nets(copper_tracks, copper_vias, protect_mode);

    let pair_mode_active = opts.diff_pairs_mode.is_some() || !opts.explicit_pairs.is_empty();

    if !opts.pair_layers.is_empty() && !pair_mode_active {
        return Err(Failure::new(
            Status::Invalid,
            "--pair-layers requires --diff-pairs",
        ));
    }
    if (opts.intra_match
        || opts.length_match_tolerance_nm.is_some()
        || opts.meander_amplitude_nm.is_some())
        && !pair_mode_active
    {
        return Err(Failure::new(
            Status::Invalid,
            "intra-pair tuning flags require --diff-pairs",
        ));
    }
    let imp_count = usize::from(opts.pair_impedance_ohm.is_some())
        + usize::from(opts.impedance_tolerance_pct.is_some())
        + usize::from(opts.impedance_reference.is_some())
        + usize::from(opts.impedance_uncoupled_nm.is_some());
    if imp_count > 0 && (!pair_mode_active || imp_count != 4) {
        return Err(Failure::new(
            Status::Invalid,
            "--pair-impedance, --impedance-tolerance, --impedance-reference, and --impedance-uncoupled must all be provided together with --diff-pairs",
        ));
    }

    if opts.group_match_tolerance_nm.is_some() && opts.group_delay_tolerance_as.is_some() {
        return Err(Failure::new(
            Status::Invalid,
            "--group-match-tolerance and --group-delay-tolerance cannot be combined",
        ));
    }
    if opts.length_match_groups.is_empty()
        && (opts.group_match_tolerance_nm.is_some()
            || opts.group_delay_tolerance_as.is_some()
            || opts.group_meander_amplitude_nm.is_some()
            || opts.group_meander_spacing_widths.is_some())
    {
        return Err(Failure::new(
            Status::Invalid,
            "group length-matching flags require at least one --length-match-group",
        ));
    }
    if opts.ac_couple_chains.is_empty()
        && (opts.ac_match_tolerance_nm.is_some() || opts.ac_meander_amplitude_nm.is_some())
    {
        return Err(Failure::new(
            Status::Invalid,
            "AC matching controls require --ac-couple-chain-json",
        ));
    }
    if !opts.ac_couple_chains.is_empty() && !pair_mode_active {
        return Err(Failure::new(
            Status::Invalid,
            "--ac-couple-chain-json requires --diff-pairs",
        ));
    }

    let pair_layers: Vec<String> = if !opts.pair_layers.is_empty() {
        let mut seen = BTreeSet::new();
        for l in &opts.pair_layers {
            if !seen.insert(l.clone()) {
                return Err(Failure::new(
                    Status::Invalid,
                    format!("duplicate layer {l:?} in --pair-layers"),
                ));
            }
            if !input.layers.iter().any(|avail| avail == l) {
                return Err(Failure::new(
                    Status::Invalid,
                    format!("layer {l:?} in --pair-layers is unknown, disabled, or non-copper"),
                ));
            }
        }
        opts.pair_layers.clone()
    } else {
        let mut defaults = Vec::new();
        for candidate in ["F.Cu", "B.Cu"] {
            if input.layers.iter().any(|l| l == candidate) {
                defaults.push(candidate.to_owned());
            }
        }
        if defaults.is_empty() {
            defaults = input.layers.clone();
        }
        defaults
    };

    if imp_count == 4 && pair_layers.len() != 1 {
        return Err(Failure::new(
            Status::Invalid,
            format!(
                "impedance control requires exactly 1 explicit --pair-layers signal layer, got {}",
                pair_layers.len()
            ),
        ));
    }

    let mut net_by_name: BTreeMap<&str, u32> = BTreeMap::new();
    let mut name_by_id: BTreeMap<u32, &str> = BTreeMap::new();
    for n in &input.nets {
        if n.id != 0 {
            net_by_name.insert(n.name.as_str(), n.id);
            name_by_id.insert(n.id, n.name.as_str());
        }
    }
    let mut pad_counts: BTreeMap<u32, usize> = BTreeMap::new();
    for p in &input.pads {
        if p.net != 0 {
            *pad_counts.entry(p.net).or_insert(0) += 1;
        }
    }

    let in_scope =
        |net_id: u32| -> bool { active_nets.as_ref().is_none_or(|s| s.contains(&net_id)) };

    let mut raw_pairs: Vec<(String, u32, u32)> = Vec::new();
    let mut seen_pair_nets: BTreeSet<u32> = BTreeSet::new();
    if opts.diff_pairs_mode == Some(DiffPairsMode::Auto) {
        let mut groups: BTreeMap<(String, u8), (Option<u32>, Option<u32>)> = BTreeMap::new();
        for n in &input.nets {
            if n.id == 0 {
                continue;
            }
            if let Some((stem, conv, role)) = classify_pair_name(&n.name) {
                let conv_key = match conv {
                    PairConv::UnderscorePN => 0u8,
                    PairConv::PlusMinus => 1u8,
                    PairConv::BarePN => 2u8,
                };
                let entry = groups.entry((stem, conv_key)).or_insert((None, None));
                match role {
                    PairRole::Pos => entry.0 = Some(n.id),
                    PairRole::Neg => entry.1 = Some(n.id),
                }
            }
        }
        for ((stem, conv_key), (pos_opt, neg_opt)) in groups {
            match (pos_opt, neg_opt) {
                (Some(p), Some(n)) => {
                    let p_sel = in_scope(p);
                    let n_sel = in_scope(n);
                    if p_sel != n_sel {
                        return Err(Failure::new(
                            Status::Invalid,
                            format!(
                                "--nets selection splits detected differential pair ({:?}, {:?})",
                                name_by_id[&p], name_by_id[&n]
                            ),
                        ));
                    }
                    if p_sel {
                        if !seen_pair_nets.insert(p) || !seen_pair_nets.insert(n) {
                            return Err(Failure::new(
                                Status::Invalid,
                                format!("net in pair {stem:?} belongs to multiple pairs"),
                            ));
                        }
                        raw_pairs.push((stem, p, n));
                    }
                }
                (Some(only), None) | (None, Some(only)) => {
                    // Bare P/N without partner stays ordinary (e.g. VIN); _P/_N and +/- refuse!
                    if conv_key != 2 && in_scope(only) {
                        return Err(Failure::new(
                            Status::Invalid,
                            format!(
                                "unpaired differential net {:?} in selected routing scope",
                                name_by_id[&only]
                            ),
                        ));
                    }
                }
                (None, None) => {}
            }
        }
    } else if !opts.explicit_pairs.is_empty() {
        for pair in &opts.explicit_pairs {
            let pos_name = pair[0].as_str();
            let neg_name = pair[1].as_str();
            let pos_id = *net_by_name.get(pos_name).ok_or_else(|| {
                Failure::new(
                    Status::Invalid,
                    format!("explicit pair net {pos_name:?} not found"),
                )
            })?;
            let neg_id = *net_by_name.get(neg_name).ok_or_else(|| {
                Failure::new(
                    Status::Invalid,
                    format!("explicit pair net {neg_name:?} not found"),
                )
            })?;
            if pos_id == neg_id {
                return Err(Failure::new(
                    Status::Invalid,
                    format!("explicit pair {pos_name:?} cannot pair with itself"),
                ));
            }
            if !in_scope(pos_id) || !in_scope(neg_id) {
                return Err(Failure::new(
                    Status::Invalid,
                    format!("explicit pair ({pos_name:?}, {neg_name:?}) outside --nets scope"),
                ));
            }
            if !seen_pair_nets.insert(pos_id) || !seen_pair_nets.insert(neg_id) {
                return Err(Failure::new(
                    Status::Invalid,
                    format!("duplicate net in explicit pair ({pos_name:?}, {neg_name:?})"),
                ));
            }
            let stem = classify_pair_name(pos_name)
                .map(|(s, _, _)| s)
                .unwrap_or_else(|| pos_name.to_owned());
            raw_pairs.push((stem, pos_id, neg_id));
        }
    }

    // Sort deterministically by positive net name per docs/diff-pairs.md
    raw_pairs.sort_by(|a, b| name_by_id[&a.1].cmp(name_by_id[&b.1]));

    let mut pair_stem_by_net: BTreeMap<u32, String> = BTreeMap::new();
    for (stem, p, n) in &raw_pairs {
        let dru_stem = match_dp_suffix(name_by_id[p])
            .filter(|(comp, _)| comp == name_by_id[n])
            .or_else(|| match_dp_suffix(name_by_id[n]).filter(|(comp, _)| comp == name_by_id[p]))
            .map(|(_, base)| base)
            .unwrap_or_else(|| stem.clone());
        pair_stem_by_net.insert(*p, dru_stem.clone());
        pair_stem_by_net.insert(*n, dru_stem);
    }
    {
        for n in &input.nets {
            if n.id == 0 || pair_stem_by_net.contains_key(&n.id) {
                continue;
            }
            if let Some((comp, base)) = match_dp_suffix(&n.name) {
                if let Some(&comp_id) = net_by_name.get(comp.as_str()) {
                    if comp_id != n.id && !pair_stem_by_net.contains_key(&comp_id) {
                        pair_stem_by_net.insert(n.id, base.clone());
                        pair_stem_by_net.insert(comp_id, base);
                    }
                }
            }
        }
    }

    let mut pairs = Vec::with_capacity(raw_pairs.len());
    for (idx, (stem, pos_net, neg_net)) in raw_pairs.into_iter().enumerate() {
        let pos_name = name_by_id[&pos_net].to_owned();
        let neg_name = name_by_id[&neg_net].to_owned();
        let p_pads = pad_counts.get(&pos_net).copied().unwrap_or(0);
        let n_pads = pad_counts.get(&neg_net).copied().unwrap_or(0);
        if p_pads < 2 || n_pads < 2 {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "differential pair ({pos_name:?}, {neg_name:?}) requires >= 2 pads per leg (got {p_pads}, {n_pads})"
                ),
            ));
        }
        let pr = rules.net(pos_net);
        let nr = rules.net(neg_net);
        if pr.class_name != nr.class_name {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "differential pair ({pos_name:?}, {neg_name:?}) has mismatched netclasses ({:?} vs {:?})",
                    pr.class_name, nr.class_name
                ),
            ));
        }
        if pr.diff_pair_width_nm <= 0 || pr.diff_pair_width_nm != nr.diff_pair_width_nm {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "differential pair ({pos_name:?}, {neg_name:?}) lacks matching positive diff_pair_width"
                ),
            ));
        }
        if pr.diff_pair_gap_nm <= 0 || pr.diff_pair_gap_nm != nr.diff_pair_gap_nm {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "differential pair ({pos_name:?}, {neg_name:?}) lacks matching positive diff_pair_gap"
                ),
            ));
        }
        let bds_default_clr = input
            .board_design_settings
            .as_ref()
            .map_or(0, |b| b.default_clearance_nm);
        let min_clr = pr
            .clearance_nm
            .max(nr.clearance_nm)
            .max(input.rules.clearance_nm)
            .max(bds_default_clr);
        if pr.diff_pair_gap_nm < min_clr {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "differential pair ({pos_name:?}, {neg_name:?}) gap {} nm < effective clearance {} nm",
                    pr.diff_pair_gap_nm, min_clr
                ),
            ));
        }
        let dru_stem = pair_stem_by_net
            .get(&pos_net)
            .map(String::as_str)
            .unwrap_or(&stem);
        let (dru_gap_min, dru_gap_max, dru_uncoupled, dru_max_width, dru_skew_max_nm) =
            resolve_pair_dru_bounds(
                input,
                rules,
                &stem,
                dru_stem,
                pos_net,
                neg_net,
                &pos_name,
                &neg_name,
                &pair_layers,
                &pair_stem_by_net,
            )?;
        let gap_nm = pr.diff_pair_gap_nm;
        let gap_min_nm = dru_gap_min.unwrap_or(gap_nm).max(min_clr);
        let gap_max_nm = dru_gap_max.unwrap_or(gap_nm);
        if gap_min_nm > gap_max_nm {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "differential pair ({pos_name:?}, {neg_name:?}) effective gap_min {gap_min_nm} > gap_max {gap_max_nm}"
                ),
            ));
        }
        if gap_nm < gap_min_nm || gap_nm > gap_max_nm {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "differential pair ({pos_name:?}, {neg_name:?}) gap {gap_nm} nm outside DRC diff_pair_gap range [{gap_min_nm}, {gap_max_nm}] nm"
                ),
            ));
        }
        let uncoupled_max_nm = match (dru_uncoupled, opts.impedance_uncoupled_nm) {
            (Some(a), Some(b)) => a.min(b),
            (Some(a), None) => a,
            (None, Some(b)) => b,
            (None, None) => 4_000_000,
        };
        let impedance = if imp_count == 4 {
            Some(resolve_impedance_plan(
                input,
                opts,
                &pair_layers[0],
                pr.diff_pair_width_nm,
                gap_nm,
                dru_max_width,
            )?)
        } else {
            None
        };
        let width_nm = impedance
            .as_ref()
            .map_or(pr.diff_pair_width_nm, |imp| imp.solved_width_nm);
        let via_gap_nm = if pr.diff_pair_via_gap_nm > 0 {
            pr.diff_pair_via_gap_nm
        } else {
            gap_nm
        };
        pairs.push(ResolvedDiffPair {
            index: idx,
            stem,
            pos_net,
            neg_net,
            pos_name,
            neg_name,
            class_name: pr.class_name.clone(),
            width_nm,
            class_width_nm: pr.diff_pair_width_nm,
            gap_nm,
            gap_min_nm,
            gap_max_nm,
            via_gap_nm,
            clearance_nm: min_clr,
            uncoupled_max_nm,
            has_dru_gap_max: dru_gap_max.is_some(),
            dru_skew_max_nm,
            impedance,
        });
    }

    let mut pair_partner_by_net: BTreeMap<u32, u32> = BTreeMap::new();
    let mut pair_index_by_net: BTreeMap<u32, usize> = BTreeMap::new();
    for (p_idx, p) in pairs.iter().enumerate() {
        pair_partner_by_net.insert(p.pos_net, p.neg_net);
        pair_partner_by_net.insert(p.neg_net, p.pos_net);
        pair_index_by_net.insert(p.pos_net, p_idx);
        pair_index_by_net.insert(p.neg_net, p_idx);
    }

    let default_group_amp = opts.group_meander_amplitude_nm.unwrap_or(1_000_000);
    let default_group_spacing = opts.group_meander_spacing_widths.unwrap_or(2.0);
    if default_group_amp <= 0 || default_group_spacing < 1.0 {
        return Err(Failure::new(
            Status::Invalid,
            "group meander amplitude must be > 0 and spacing must be >= 1.0 widths",
        ));
    }

    let resolve_from_to_net = |tok: &str| -> Result<(u32, usize), Failure> {
        let (fp_ref, pad_num) = tok
            .rsplit_once('-')
            .or_else(|| tok.rsplit_once('.'))
            .ok_or_else(|| {
                Failure::new(
                    Status::Invalid,
                    format!(
                        "from_to terminal {tok:?} must use '<REF>-<PAD>' or '<REF>.<PAD>' format"
                    ),
                )
            })?;
        let mut found: Option<(u32, usize)> = None;
        for (idx, p) in input.pads.iter().enumerate() {
            if p.footprint_ref.as_deref() == Some(fp_ref)
                && p.pad_number.as_deref() == Some(pad_num)
            {
                if found.is_some() {
                    return Err(Failure::new(
                        Status::Invalid,
                        format!("ambiguous from_to terminal {tok:?}"),
                    ));
                }
                found = Some((p.net, idx));
            }
        }
        let (net_id, idx) = found.ok_or_else(|| {
            Failure::new(
                Status::Invalid,
                format!("from_to terminal {tok:?} not found on board"),
            )
        })?;
        if net_id == 0 {
            return Err(Failure::new(
                Status::Invalid,
                format!("from_to terminal {tok:?} is unconnected (net 0)"),
            ));
        }
        if !name_by_id.contains_key(&net_id) {
            return Err(Failure::new(
                Status::Invalid,
                format!("from_to terminal {tok:?} references undeclared net id {net_id}"),
            ));
        }
        Ok((net_id, idx))
    };

    let mut length_groups = Vec::with_capacity(opts.length_match_groups.len());
    for g in &opts.length_match_groups {
        let mut from_to_nets: Vec<String> = Vec::new();
        if !g.from_to.is_empty() {
            let mut ft_seen_nets = BTreeSet::new();
            for pair_tok in &g.from_to {
                let (na, ia) = resolve_from_to_net(&pair_tok[0])?;
                let (nb, ib) = resolve_from_to_net(&pair_tok[1])?;
                if ia == ib {
                    return Err(Failure::new(
                        Status::Invalid,
                        format!("from_to entry {:?} references the same pad twice", pair_tok),
                    ));
                }
                let na_name = name_by_id.get(&na).copied().unwrap_or("");
                let nb_name = name_by_id.get(&nb).copied().unwrap_or("");
                if na != nb {
                    return Err(Failure::new(
                        Status::Invalid,
                        format!(
                            "from_to entry {:?} connects different nets ({:?} vs {:?})",
                            pair_tok, na_name, nb_name
                        ),
                    ));
                }
                if !ft_seen_nets.insert(na) {
                    return Err(Failure::new(
                        Status::Invalid,
                        format!("duplicate net {:?} in from_to group", na_name),
                    ));
                }
                from_to_nets.push(na_name.to_owned());
            }
        }
        let effective_member_names: Vec<String> = if !g.members.is_empty() {
            if !from_to_nets.is_empty() {
                let a: BTreeSet<&str> = g.members.iter().map(String::as_str).collect();
                let b: BTreeSet<&str> = from_to_nets.iter().map(String::as_str).collect();
                if a != b {
                    return Err(Failure::new(
                        Status::Invalid,
                        "length-match group members and from_to nets do not match",
                    ));
                }
            }
            g.members.clone()
        } else {
            from_to_nets
        };
        if effective_member_names.len() < 2 {
            return Err(Failure::new(
                Status::Invalid,
                "length-match group requires at least 2 members",
            ));
        }
        let mut seen = BTreeSet::new();
        let mut member_ids = Vec::with_capacity(effective_member_names.len());
        let mut member_names = Vec::with_capacity(effective_member_names.len());
        let mut any_pair_member = false;
        let mut any_se_member = false;
        for m in &effective_member_names {
            let nid = *net_by_name.get(m.as_str()).ok_or_else(|| {
                Failure::new(
                    Status::Invalid,
                    format!("length-match group member {m:?} not found on board"),
                )
            })?;
            if !seen.insert(nid) {
                return Err(Failure::new(
                    Status::Invalid,
                    format!("duplicate member {m:?} in length-match group"),
                ));
            }
            if !in_scope(nid) {
                return Err(Failure::new(
                    Status::Invalid,
                    format!("length-match group member {m:?} outside --nets scope"),
                ));
            }
            let n_pads = pad_counts.get(&nid).copied().unwrap_or(0);
            if n_pads != 2 {
                return Err(Failure::new(
                    Status::Invalid,
                    format!(
                        "length-match group member {m:?} must have exactly 2 pads (got {n_pads})"
                    ),
                ));
            }
            if input.zones.iter().any(|z| z.net == nid)
                || input.graphics.iter().any(|gr| gr.net == nid)
            {
                return Err(Failure::new(
                    Status::Invalid,
                    format!("length-match group member {m:?} has zone or graphical copper"),
                ));
            }
            for pad in input.pads.iter().filter(|p| p.net == nid) {
                if let Some(r) = &pad.footprint_ref {
                    if !is_valid_terminal_token(r) {
                        return Err(Failure::new(
                            Status::Invalid,
                            format!("pad footprint_ref {r:?} on {m:?} is not a valid fromTo token"),
                        ));
                    }
                }
                if let Some(pn) = &pad.pad_number {
                    if !is_valid_terminal_token(pn) {
                        return Err(Failure::new(
                            Status::Invalid,
                            format!("pad_number {pn:?} on {m:?} is not a valid fromTo token"),
                        ));
                    }
                }
            }
            if pair_partner_by_net.contains_key(&nid) {
                any_pair_member = true;
            } else {
                any_se_member = true;
            }
            member_ids.push(nid);
            member_names.push(m.clone());
        }
        if any_pair_member && any_se_member {
            return Err(Failure::new(
                Status::Invalid,
                "coupled length-match group cannot mix differential pair members and single-ended nets",
            ));
        }
        for &nid in &member_ids {
            if let Some(&partner) = pair_partner_by_net.get(&nid) {
                if !seen.contains(&partner) {
                    return Err(Failure::new(
                        Status::Invalid,
                        format!(
                            "length-match group splits differential pair ({:?}, {:?})",
                            name_by_id[&nid], name_by_id[&partner]
                        ),
                    ));
                }
            }
        }
        if any_pair_member {
            let mut imp_geom: Option<Option<(String, i64, i64)>> = None;
            for &nid in &member_ids {
                let p = &pairs[pair_index_by_net[&nid]];
                let sig = p
                    .impedance
                    .as_ref()
                    .map(|i| (i.signal_layer.clone(), i.solved_width_nm, p.gap_nm));
                match &imp_geom {
                    None => imp_geom = Some(sig),
                    Some(prev) if *prev != sig => {
                        return Err(Failure::new(
                            Status::Invalid,
                            "coupled length-match group cannot mix differing solved impedance geometries",
                        ));
                    }
                    _ => {}
                }
            }
        }
        let intra_tol_opt = if opts.intra_match {
            Some(opts.length_match_tolerance_nm.unwrap_or(50_000))
        } else {
            opts.length_match_tolerance_nm
        };
        let intra_amp_opt = if opts.intra_match {
            Some(opts.meander_amplitude_nm.unwrap_or(1_500_000))
        } else {
            opts.meander_amplitude_nm
        };
        let explicit_group_amp = g.meander_amplitude_nm.or(opts.group_meander_amplitude_nm);
        let group_amp = explicit_group_amp.unwrap_or_else(|| {
            if any_pair_member {
                intra_amp_opt.unwrap_or(4_500_000)
            } else {
                default_group_amp
            }
        });
        let group_spacing = g.meander_spacing_widths.unwrap_or(default_group_spacing);
        if group_amp <= 0 || group_spacing < 1.0 {
            return Err(Failure::new(
                Status::Invalid,
                "group meander amplitude must be > 0 and spacing must be >= 1.0 widths",
            ));
        }
        let mode = if let Some(delay_as) = g.delay_tolerance_as.or(opts.group_delay_tolerance_as) {
            if any_pair_member {
                return Err(Failure::new(
                    Status::Invalid,
                    "delay-profile matching is not supported on coupled differential-pair groups",
                ));
            }
            if delay_as < 0 {
                return Err(Failure::new(
                    Status::Invalid,
                    "delay tolerance cannot be negative",
                ));
            }
            for (&nid, mname) in member_ids.iter().zip(member_names.iter()) {
                validate_delay_member_profile(
                    input,
                    copper_tracks,
                    rules,
                    nid,
                    mname,
                    &input.layers,
                )?;
            }
            GroupTargetMode::DelayAs {
                tolerance_as: delay_as,
            }
        } else {
            let explicit_group_tol = g.tolerance_nm.or(opts.group_match_tolerance_nm);
            let tol_nm = explicit_group_tol.unwrap_or_else(|| {
                if any_pair_member {
                    intra_tol_opt.unwrap_or(100_000)
                } else {
                    100_000
                }
            });
            if tol_nm < 0 {
                return Err(Failure::new(
                    Status::Invalid,
                    "length tolerance cannot be negative",
                ));
            }
            if any_pair_member {
                if let (Some(intra_tol), Some(grp_tol)) = (intra_tol_opt, explicit_group_tol) {
                    if intra_tol != grp_tol {
                        return Err(Failure::new(
                            Status::Invalid,
                            "coupled length-match groups and intra-pair tuning must declare identical controls",
                        ));
                    }
                }
                if let (Some(intra_amp), Some(grp_amp)) = (intra_amp_opt, explicit_group_amp) {
                    if intra_amp != grp_amp {
                        return Err(Failure::new(
                            Status::Invalid,
                            "coupled length-match groups and intra-pair tuning must declare identical controls",
                        ));
                    }
                }
            }
            GroupTargetMode::LengthNm {
                tolerance_nm: tol_nm,
            }
        };
        length_groups.push(ResolvedLengthGroup {
            members: member_ids,
            member_names,
            contains_diff_pairs: any_pair_member,
            mode,
            amplitude_nm: group_amp,
            spacing_widths: group_spacing,
        });
    }

    let ac_match_tolerance_nm = opts.ac_match_tolerance_nm.unwrap_or(100_000);
    let ac_meander_amplitude_nm = opts.ac_meander_amplitude_nm.unwrap_or(1_000_000);
    let ac_chains = if opts.ac_couple_chains.is_empty() {
        Vec::new()
    } else {
        if ac_match_tolerance_nm <= 0 || ac_meander_amplitude_nm <= 0 {
            return Err(Failure::new(
                Status::Invalid,
                "AC matching tolerance and meander amplitude must be > 0",
            ));
        }
        resolve_ac_chains(
            input,
            &opts.ac_couple_chains,
            &pairs,
            &pad_counts,
            &name_by_id,
        )?
    };

    for pair in &pairs {
        let p_pads = pad_counts.get(&pair.pos_net).copied().unwrap_or(0);
        let n_pads = pad_counts.get(&pair.neg_net).copied().unwrap_or(0);
        if p_pads != 2 || n_pads != 2 {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "differential pair ({:?}, {:?}) requires exactly 2 pads per leg (got {p_pads}, {n_pads})",
                    pair.pos_name, pair.neg_name
                ),
            ));
        }
    }

    Ok(DiffPairPlan {
        pairs,
        length_groups,
        ac_chains,
        ac_match_tolerance_nm,
        ac_meander_amplitude_nm,
        pair_layers,
        active_nets,
        protected_nets,
        protect_mode,
        no_short_pair_deferral: opts.no_short_pair_deferral,
        intra_match: opts.intra_match,
        intra_tolerance_nm: opts.length_match_tolerance_nm.unwrap_or(50_000),
        intra_amplitude_nm: opts.meander_amplitude_nm.unwrap_or(1_500_000),
    })
}

fn resolve_ac_chains(
    input: &Input,
    chains: &[Vec<String>],
    pairs: &[ResolvedDiffPair],
    pad_counts: &BTreeMap<u32, usize>,
    name_by_id: &BTreeMap<u32, &str>,
) -> Result<Vec<ResolvedAcChain>, Failure> {
    let mut by_ref: BTreeMap<&str, &crate::model::ComponentInfo> = BTreeMap::new();
    for comp in &input.components {
        if by_ref.insert(comp.ref_des.as_str(), comp).is_some() {
            return Err(Failure::new(
                Status::Invalid,
                format!(
                    "AC matching cannot identify duplicate footprint reference {:?}",
                    comp.ref_des
                ),
            ));
        }
    }
    let active_layers = input.active_layers();
    let comp_copper_pads = |ref_des: &str| -> Vec<&crate::model::Pad> {
        input
            .pads
            .iter()
            .filter(|p| {
                p.footprint_ref.as_deref() == Some(ref_des)
                    && if p.layers.is_empty() {
                        active_layers.iter().any(|l| l == &input.layer)
                    } else {
                        p.layers
                            .iter()
                            .any(|pl| active_layers.iter().any(|al| al == pl))
                    }
            })
            .collect()
    };
    let mut members: BTreeMap<u32, (usize, PairRole)> = BTreeMap::new();
    for (idx, p) in pairs.iter().enumerate() {
        members.insert(p.pos_net, (idx, PairRole::Pos));
        members.insert(p.neg_net, (idx, PairRole::Neg));
    }

    let mut seen_refs: BTreeSet<String> = BTreeSet::new();
    let mut used_pairs: BTreeSet<usize> = BTreeSet::new();
    let mut resolved = Vec::with_capacity(chains.len());

    for refs in chains {
        if refs.len() < 2 || refs.iter().any(|r| r.is_empty()) {
            return Err(Failure::new(
                Status::Invalid,
                "each AC chain needs at least two explicit series-part references",
            ));
        }
        let mut chain_ref_set: BTreeSet<&str> = BTreeSet::new();
        for r in refs {
            if !chain_ref_set.insert(r.as_str()) || !seen_refs.insert(r.clone()) {
                return Err(Failure::new(
                    Status::Invalid,
                    format!("AC series-part reference {r:?} must not repeat or belong to multiple chains"),
                ));
            }
        }
        let mut edges: BTreeMap<(usize, usize), (Option<String>, Option<String>)> = BTreeMap::new();
        for r in refs {
            let Some(comp) = by_ref.get(r.as_str()) else {
                return Err(Failure::new(
                    Status::Invalid,
                    format!("AC series part {r:?} is missing"),
                ));
            };
            if comp.dnp {
                return Err(Failure::new(
                    Status::Invalid,
                    format!("AC series part {r:?} is DNP"),
                ));
            }
            let cpads = comp_copper_pads(r.as_str());
            if cpads.len() != 2 {
                return Err(Failure::new(
                    Status::Invalid,
                    format!(
                        "AC series part {r:?} needs exactly two native copper pads (got {})",
                        cpads.len()
                    ),
                ));
            }
            let (Some(&(a, pa)), Some(&(b, pb))) =
                (members.get(&cpads[0].net), members.get(&cpads[1].net))
            else {
                return Err(Failure::new(
                    Status::Invalid,
                    format!("AC series part {r:?} has a net outside selected complete pairs"),
                ));
            };
            if a == b || pa != pb {
                return Err(Failure::new(
                    Status::Invalid,
                    format!("AC series part {r:?} bridges the same pair or opposite polarities"),
                ));
            }
            let key = if a < b { (a, b) } else { (b, a) };
            let entry = edges.entry(key).or_insert((None, None));
            let slot = match pa {
                PairRole::Pos => &mut entry.0,
                PairRole::Neg => &mut entry.1,
            };
            if slot.is_some() {
                return Err(Failure::new(
                    Status::Invalid,
                    format!("AC chain has duplicate parallel bridges at {r:?}"),
                ));
            }
            *slot = Some(r.clone());
        }
        if edges.values().any(|(p, n)| p.is_none() || n.is_none()) {
            return Err(Failure::new(
                Status::Invalid,
                "AC chain has asymmetric P/N series bridges",
            ));
        }
        let mut adjacency: BTreeMap<usize, BTreeSet<usize>> = BTreeMap::new();
        for &(a, b) in edges.keys() {
            adjacency.entry(a).or_default().insert(b);
            adjacency.entry(b).or_default().insert(a);
        }
        if adjacency.len() < 2
            || edges.len() != adjacency.len() - 1
            || adjacency.values().any(|nb| nb.len() > 2)
        {
            return Err(Failure::new(
                Status::Invalid,
                "AC bridges must form one nonbranching, acyclic chain",
            ));
        }
        let first = *adjacency.keys().next().unwrap();
        let mut reached: BTreeSet<usize> = BTreeSet::new();
        let mut pending = vec![first];
        while let Some(node) = pending.pop() {
            if reached.insert(node) {
                for &nb in &adjacency[&node] {
                    if !reached.contains(&nb) {
                        pending.push(nb);
                    }
                }
            }
        }
        if reached.len() != adjacency.len() || !reached.is_disjoint(&used_pairs) {
            return Err(Failure::new(
                Status::Invalid,
                "AC chain is disconnected or shares pair members with another chain",
            ));
        }
        used_pairs.extend(&reached);

        let pair_indices: Vec<usize> = reached.iter().copied().collect();
        let mut p_nets = Vec::with_capacity(pair_indices.len());
        let mut n_nets = Vec::with_capacity(pair_indices.len());
        let mut chain_nets = BTreeSet::new();
        for &p_idx in &pair_indices {
            let pair = &pairs[p_idx];
            chain_nets.insert(pair.pos_net);
            chain_nets.insert(pair.neg_net);
            p_nets.push(pair.pos_net);
            n_nets.push(pair.neg_net);
        }

        for comp in &input.components {
            if chain_ref_set.contains(comp.ref_des.as_str()) || comp.dnp {
                continue;
            }
            let cpads = comp_copper_pads(comp.ref_des.as_str());
            if cpads.len() != 2 {
                continue;
            }
            let (n0, n1) = (cpads[0].net, cpads[1].net);
            if !chain_nets.contains(&n0) && !chain_nets.contains(&n1) {
                continue;
            }
            let same_pair_terminal = n0 != 0
                && n1 != 0
                && members
                    .get(&n0)
                    .zip(members.get(&n1))
                    .is_some_and(|((pa, _), (pb, _))| pa == pb);
            if !same_pair_terminal {
                return Err(Failure::new(
                    Status::Invalid,
                    format!(
                        "AC chain has an unlisted two-pad continuation at {:?}",
                        comp.ref_des
                    ),
                ));
            }
        }

        for &p_idx in &pair_indices {
            let pair = &pairs[p_idx];
            for nid in [pair.pos_net, pair.neg_net] {
                let n_pads = pad_counts.get(&nid).copied().unwrap_or(0);
                if n_pads != 2 {
                    let net_name = name_by_id.get(&nid).copied().unwrap_or("");
                    return Err(Failure::new(
                        Status::Invalid,
                        format!(
                            "AC chain member net {net_name:?} must have exactly 2 pads (got {n_pads})"
                        ),
                    ));
                }
                if input.zones.iter().any(|z| z.net == nid)
                    || input.graphics.iter().any(|gr| gr.net == nid)
                {
                    let net_name = name_by_id.get(&nid).copied().unwrap_or("");
                    return Err(Failure::new(
                        Status::Invalid,
                        format!("AC chain member net {net_name:?} has zone or graphical copper"),
                    ));
                }
            }
        }

        let mut bridges = refs.clone();
        bridges.sort();
        resolved.push(ResolvedAcChain {
            bridges,
            p_nets,
            n_nets,
            pair_indices,
        });
    }

    Ok(resolved)
}

// ---------------------------------------------------------------------------
// Stackup Via Barrel Height & fromTo Useful Path Length / Delay Measurement
// ---------------------------------------------------------------------------

fn find_tuning_profile<'a>(rules: &'a EffectiveRules, net: u32) -> Option<&'a TuningProfile> {
    let name = rules.net(net).tuning_profile.as_deref()?;
    rules
        .tuning_profiles
        .iter()
        .find(|p| p.profile_name == name)
}

fn via_barrel_height_mm(
    input: &Input,
    rules: &EffectiveRules,
    layer_a: &str,
    layer_b: &str,
) -> Result<f64, Failure> {
    if layer_a == layer_b {
        return Ok(0.0);
    }
    if input.stackup.is_empty() {
        let bt = rules
            .board_thickness_nm
            .or_else(|| {
                input
                    .board_design_settings
                    .as_ref()
                    .and_then(|b| b.board_thickness_nm)
            })
            .ok_or_else(|| {
                Failure::new(
                    Status::Invalid,
                    "board stackup or board_thickness_nm is required when use_height_for_length_calcs is enabled and vias span layers",
                )
            })?;
        let mask_nm = rules
            .outer_solder_mask_thickness_nm
            .or_else(|| {
                input
                    .board_design_settings
                    .as_ref()
                    .and_then(|b| b.outer_solder_mask_thickness_nm)
            })
            .unwrap_or(0);
        let cu_total = (bt - mask_nm).max(0) as f64;
        let active = input.active_layers();
        let n = active.len();
        if n < 2 {
            return Ok(0.0);
        }
        let Some(idx_a) = active.iter().position(|l| l == layer_a) else {
            return Ok(0.0);
        };
        let Some(idx_b) = active.iter().position(|l| l == layer_b) else {
            return Ok(0.0);
        };
        let (lo, hi) = if idx_a < idx_b {
            (idx_a, idx_b)
        } else {
            (idx_b, idx_a)
        };
        if lo == 0 && hi == n - 1 {
            return Ok(cu_total / 1_000_000.0);
        }
        let t_cu = 35_000.0_f64;
        let cu_all = (n as f64) * t_cu;
        let mut cu_sum = 0.0_f64;
        for k in lo..=hi {
            let endpoint = k == lo || k == hi;
            let outer = k == 0 || k == n - 1;
            cu_sum += if endpoint && !outer { 0.5 * t_cu } else { t_cu };
        }
        if cu_total >= cu_all {
            let t_diel = (cu_total - cu_all) / ((n - 1) as f64);
            let total_nm = cu_sum + ((hi - lo) as f64) * t_diel;
            return Ok(total_nm / 1_000_000.0);
        }
        let scale = if cu_all > 0.0 { cu_total / cu_all } else { 0.0 };
        return Ok((cu_sum * scale) / 1_000_000.0);
    }
    let Some(idx_a) = input
        .stackup
        .iter()
        .position(|s| s.kind == StackupKind::Copper && s.name == layer_a)
    else {
        return Ok(0.0);
    };
    let Some(idx_b) = input
        .stackup
        .iter()
        .position(|s| s.kind == StackupKind::Copper && s.name == layer_b)
    else {
        return Ok(0.0);
    };
    let (lo, hi) = if idx_a < idx_b {
        (idx_a, idx_b)
    } else {
        (idx_b, idx_a)
    };
    let cu_first = input
        .stackup
        .iter()
        .position(|s| s.kind == StackupKind::Copper);
    let cu_last = input
        .stackup
        .iter()
        .rposition(|s| s.kind == StackupKind::Copper);

    let mut total_nm: f64 = 0.0;
    for idx in lo..=hi {
        let sl = &input.stackup[idx];
        let t = sl.thickness_nm as f64;
        if idx == lo || idx == hi {
            let is_outer = Some(idx) == cu_first || Some(idx) == cu_last;
            total_nm += if is_outer { t } else { 0.5 * t };
        } else {
            total_nm += t;
        }
    }
    Ok(total_nm / 1_000_000.0)
}

fn copper_layer_order_index(input: &Input, layer: &str) -> Option<usize> {
    if !input.stackup.is_empty() {
        if let Some(pos) = input
            .stackup
            .iter()
            .filter(|s| s.kind == StackupKind::Copper)
            .position(|s| s.name == layer)
        {
            return Some(pos);
        }
    }
    input.active_layers().iter().position(|l| l == layer)
}

fn canonical_layer_pair<'a>(input: &Input, a: &'a str, b: &'a str) -> (&'a str, &'a str) {
    match (
        copper_layer_order_index(input, a),
        copper_layer_order_index(input, b),
    ) {
        (Some(ia), Some(ib)) if ia > ib => (b, a),
        _ => (a, b),
    }
}

fn via_delay_between_layers_as(
    input: &Input,
    rules: &EffectiveRules,
    prof: &TuningProfile,
    via: &Via,
    layer_a: &str,
    layer_b: &str,
) -> Result<f64, Failure> {
    if layer_a == layer_b {
        return Ok(0.0);
    }
    let (sig_top, sig_bot) = canonical_layer_pair(input, layer_a, layer_b);
    let (via_top, via_bot) = canonical_layer_pair(input, &via.start_layer, &via.end_layer);
    for ov in prof.via_overrides.iter().rev() {
        if ov.signal_layer_from.is_empty()
            || ov.signal_layer_to.is_empty()
            || ov.via_layer_from.is_empty()
            || ov.via_layer_to.is_empty()
        {
            continue;
        }
        if ov.signal_layer_from == sig_top
            && ov.signal_layer_to == sig_bot
            && ov.via_layer_from == via_top
            && ov.via_layer_to == via_bot
        {
            return Ok(ov.delay_as as f64);
        }
    }
    let h_mm = via_barrel_height_mm(input, rules, layer_a, layer_b)?;
    Ok(h_mm * (prof.via_prop_delay_as_per_mm as f64))
}

#[derive(Clone, Copy, Debug)]
struct NetMeasure {
    length_nm: i64,
    delay_as: i64,
}

fn pad_supports_layer(engine: &Engine, pad: &crate::model::Pad, layer_name: &str) -> bool {
    if pad.layers.is_empty() {
        layer_name == engine.input.layer
    } else {
        pad.layers.iter().any(|l| l == layer_name)
    }
}

fn pad_layer_indices(engine: &Engine, pad: &crate::model::Pad) -> Vec<usize> {
    if pad.layers.is_empty() {
        engine
            .layer_index(&engine.input.layer)
            .into_iter()
            .collect()
    } else {
        pad.layers
            .iter()
            .filter_map(|ln| engine.layer_index(ln))
            .collect()
    }
}

fn measure_net_fromto(
    engine: &Engine,
    net: u32,
    require_delay: bool,
) -> Result<Option<NetMeasure>, Failure> {
    let pads: Vec<_> = engine.input.pads.iter().filter(|p| p.net == net).collect();
    if pads.len() < 2 {
        return Ok(None);
    }
    let prof = if require_delay {
        find_tuning_profile(&engine.effective_rules, net)
    } else {
        None
    };

    let tracks: Vec<&Track> = engine.copper.tracks().filter(|t| t.net == net).collect();
    let vias: Vec<&Via> = engine.copper.vias().filter(|v| v.net == net).collect();

    // Directed split-node item-traversal graph matching KiCad 10.0.8 LENGTH_DELAY_CALCULATION:
    // - Pad nodes: 0..pads.len() (s_node = 0, t_node = 1, plus any intermediate pads)
    // - Track i: directed edge in_i -> out_i with (len(t_i), delay(t_i))
    // - Same-layer touching tracks (segments_near(..., gap2, false)): out_i -> in_j & out_j -> in_i (0.0, 0.0)
    // - Vias & pads: connect to in_i / out_i unconditionally
    #[derive(Clone, Copy)]
    struct DirEdge {
        to: usize,
        len_nm: f64,
        delay_as: f64,
    }
    #[derive(Clone, Copy, PartialEq, Eq)]
    enum NodeKind {
        Pad,
        TrackIn(usize),
        TrackOut(usize),
        ViaIn(usize, usize),
        ViaOut(usize, usize),
    }
    let mut adj: Vec<Vec<DirEdge>> = vec![Vec::new(); pads.len()];
    let mut node_kind: Vec<NodeKind> = vec![NodeKind::Pad; pads.len()];
    let add_dir_edge =
        |u: usize, v: usize, len_nm: f64, delay_as: f64, adj: &mut Vec<Vec<DirEdge>>| {
            if u == v {
                return;
            }
            adj[u].push(DirEdge {
                to: v,
                len_nm,
                delay_as,
            });
        };

    let mut track_nodes: Vec<Option<(usize, usize, usize, f64, f64)>> =
        Vec::with_capacity(tracks.len());
    for (i, t) in tracks.iter().enumerate() {
        let Some(li) = engine.layer_index(&t.layer) else {
            track_nodes.push(None);
            continue;
        };
        let layer_delay_rate = if let Some(pr) = prof {
            pr.layer_entries
                .iter()
                .find(|le| le.signal_layer == t.layer)
                .map(|le| le.delay_as_per_mm as f64)
                .ok_or_else(|| {
                    Failure::new(
                        Status::Invalid,
                        format!("layer {:?} missing in tuning profile", t.layer),
                    )
                })?
        } else {
            0.0
        };
        let in_i = adj.len();
        adj.push(Vec::new());
        node_kind.push(NodeKind::TrackIn(i));
        let out_i = adj.len();
        adj.push(Vec::new());
        node_kind.push(NodeKind::TrackOut(i));
        let len_i = dist_f64(t.start, t.end);
        let del_i = (len_i / 1_000_000.0) * layer_delay_rate;
        add_dir_edge(in_i, out_i, len_i, del_i, &mut adj);
        track_nodes.push(Some((in_i, out_i, li, layer_delay_rate, len_i)));
    }

    // Same-layer track-to-track copper overlaps (including crossings, endpoints, near-T, parallel)
    for i in 0..tracks.len() {
        let Some((_, out_i, li_a, _, _)) = track_nodes[i] else {
            continue;
        };
        let ta = tracks[i];
        let a_min_x = i128::from(ta.start[0].min(ta.end[0]));
        let a_max_x = i128::from(ta.start[0].max(ta.end[0]));
        let a_min_y = i128::from(ta.start[1].min(ta.end[1]));
        let a_max_y = i128::from(ta.start[1].max(ta.end[1]));
        for j in (i + 1)..tracks.len() {
            let Some((in_j, out_j, li_b, _, _)) = track_nodes[j] else {
                continue;
            };
            if li_a != li_b {
                continue;
            }
            let tb = tracks[j];
            let gap2 = i128::from(ta.width_nm) + i128::from(tb.width_nm);
            let pad = (gap2 + 1) / 2;
            let b_min_x = i128::from(tb.start[0].min(tb.end[0]));
            let b_max_x = i128::from(tb.start[0].max(tb.end[0]));
            if a_max_x + pad < b_min_x || b_max_x + pad < a_min_x {
                continue;
            }
            let b_min_y = i128::from(tb.start[1].min(tb.end[1]));
            let b_max_y = i128::from(tb.start[1].max(tb.end[1]));
            if a_max_y + pad < b_min_y || b_max_y + pad < a_min_y {
                continue;
            }
            let touches = if gap2 > 0 {
                geometry::segments_near(ta.start, ta.end, tb.start, tb.end, gap2, false)
            } else {
                ta.start == tb.start || ta.start == tb.end || ta.end == tb.start || ta.end == tb.end
            };
            if touches {
                let in_i = track_nodes[i].expect("checked above").0;
                add_dir_edge(out_i, in_j, 0.0, 0.0, &mut adj);
                add_dir_edge(out_j, in_i, 0.0, 0.0, &mut adj);
            }
        }
    }

    // Via split nodes (via_in, via_out) per (via_idx, layer_idx), track-to-via, pad-to-via, stacked vias, and vertical barrels
    let mut via_nodes: Vec<BTreeMap<usize, (usize, usize)>> = vec![BTreeMap::new(); vias.len()];
    let mut neg_vias: Vec<Vec<(usize, usize, bool, f64)>> = vec![Vec::new(); tracks.len()];
    let mut track_pos_vias: Vec<Vec<usize>> = vec![Vec::new(); tracks.len()];
    let mut via_touch_count: Vec<Vec<usize>> = vec![vec![0; engine.layer_names.len()]; vias.len()];
    let mut via_any_pos_tracks: Vec<Vec<bool>> =
        vec![vec![false; engine.layer_names.len()]; vias.len()];
    let get_via_node = |via_idx: usize,
                        li: usize,
                        via_nodes: &mut [BTreeMap<usize, (usize, usize)>],
                        adj: &mut Vec<Vec<DirEdge>>,
                        node_kind: &mut Vec<NodeKind>|
     -> (usize, usize) {
        *via_nodes[via_idx].entry(li).or_insert_with(|| {
            let v_in = adj.len();
            adj.push(Vec::new());
            node_kind.push(NodeKind::ViaIn(via_idx, li));
            let v_out = adj.len();
            adj.push(Vec::new());
            node_kind.push(NodeKind::ViaOut(via_idx, li));
            (v_in, v_out)
        })
    };

    for (j, v) in vias.iter().enumerate() {
        let s = engine.layer_index(&v.start_layer).unwrap_or(0);
        let e = engine.layer_index(&v.end_layer).unwrap_or(s);
        let (v_lo, v_hi) = (s.min(e), s.max(e));
        let via_r = (v.diameter_nm as f64) * 0.5;
        let mut active_layers: Vec<usize> = Vec::new();

        for (i, t) in tracks.iter().enumerate() {
            let Some((in_i, out_i, li, layer_delay_rate, len_i)) = track_nodes[i] else {
                continue;
            };
            if li < v_lo || li > v_hi {
                continue;
            }
            let gap2 = i128::from(v.diameter_nm) + i128::from(t.width_nm);
            let touches = geometry::point_near_segment(v.at, t.start, t.end, 2, false)
                || geometry::point_near_segment(v.at, t.start, t.end, gap2, false);
            if !touches {
                continue;
            }
            let (v_in, v_out) = get_via_node(j, li, &mut via_nodes, &mut adj, &mut node_kind);
            active_layers.push(li);
            via_touch_count[j][li] += 1;

            let d_start = dist_f64(t.start, v.at);
            let d_end = dist_f64(t.end, v.at);
            let start_in = d_start < via_r;
            let end_in = d_end < via_r;
            let has_pos_via_cost = start_in ^ end_in;
            let mut ep_is_end = false;
            let delta = if has_pos_via_cost {
                track_pos_vias[i].push(j);
                via_any_pos_tracks[j][li] = true;
                let (ep, other, is_end) = if start_in {
                    (t.start, t.end, false)
                } else {
                    (t.end, t.start, true)
                };
                ep_is_end = is_end;
                let seg_dx = (ep[0] - other[0]) as f64;
                let seg_dy = (ep[1] - other[1]) as f64;
                let seg_len = seg_dx.hypot(seg_dy);
                if seg_len > 0.0 {
                    let ux = seg_dx / seg_len;
                    let uy = seg_dy / seg_len;
                    let vx = (v.at[0] - ep[0]) as f64;
                    let vy = (v.at[1] - ep[1]) as f64;
                    let signed_along = vx * ux + vy * uy;
                    let perp = (vx * uy - vy * ux).abs();
                    let radial = if perp < via_r {
                        (via_r - (via_r * via_r - perp * perp).sqrt()).max(0.0)
                    } else {
                        0.0
                    };
                    signed_along + radial
                } else {
                    0.0
                }
            } else {
                0.0
            };

            if delta >= 0.0 {
                let del = (delta / 1_000_000.0) * layer_delay_rate;
                add_dir_edge(out_i, v_in, delta, del, &mut adj);
                add_dir_edge(v_out, in_i, delta, del, &mut adj);
            } else {
                let eff_len = (len_i + delta).max(0.0);
                let eff_del = (eff_len / 1_000_000.0) * layer_delay_rate;
                add_dir_edge(in_i, v_in, eff_len, eff_del, &mut adj);
                add_dir_edge(v_out, out_i, eff_len, eff_del, &mut adj);
                neg_vias[i].push((v_in, v_out, ep_is_end, delta));
            }
        }

        for (k, p) in pads.iter().enumerate() {
            if geometry::via_connects_pad(v, p) {
                for li in pad_layer_indices(engine, p) {
                    if li >= v_lo && li <= v_hi {
                        let (v_in, v_out) =
                            get_via_node(j, li, &mut via_nodes, &mut adj, &mut node_kind);
                        if k != 1 {
                            add_dir_edge(k, v_in, 0.0, 0.0, &mut adj);
                        }
                        if k != 0 {
                            add_dir_edge(v_out, k, 0.0, 0.0, &mut adj);
                        }
                        active_layers.push(li);
                    }
                }
            }
        }

        for (jb, vb) in vias.iter().enumerate() {
            if jb != j && vb.at == v.at {
                let sb = engine.layer_index(&vb.start_layer).unwrap_or(0);
                let eb = engine.layer_index(&vb.end_layer).unwrap_or(sb);
                let (b_lo, b_hi) = (sb.min(eb), sb.max(eb));
                for li in v_lo.max(b_lo)..=v_hi.min(b_hi) {
                    let _ = get_via_node(j, li, &mut via_nodes, &mut adj, &mut node_kind);
                    active_layers.push(li);
                }
            }
        }

        active_layers.sort_unstable();
        active_layers.dedup();
        for &li in &active_layers {
            let (v_in, v_out) = via_nodes[j][&li];
            add_dir_edge(v_in, v_out, 0.0, 0.0, &mut adj);
        }
        for a_idx in 0..active_layers.len() {
            for b_idx in (a_idx + 1)..active_layers.len() {
                let la = active_layers[a_idx];
                let lb = active_layers[b_idx];
                let (u_in, u_out) = via_nodes[j][&la];
                let (w_in, w_out) = via_nodes[j][&lb];
                let h_nm = if engine.effective_rules.use_height_for_length_calcs {
                    via_barrel_height_mm(
                        &engine.input,
                        &engine.effective_rules,
                        engine.layer_name(la),
                        engine.layer_name(lb),
                    )? * 1_000_000.0
                } else {
                    0.0
                };
                let del = if engine.effective_rules.use_height_for_length_calcs {
                    if let Some(pr) = prof {
                        via_delay_between_layers_as(
                            &engine.input,
                            &engine.effective_rules,
                            pr,
                            v,
                            engine.layer_name(la),
                            engine.layer_name(lb),
                        )?
                    } else {
                        0.0
                    }
                } else {
                    0.0
                };
                add_dir_edge(u_in, w_out, h_nm, del, &mut adj);
                add_dir_edge(w_in, u_out, h_nm, del, &mut adj);
            }
        }
    }

    for j in 0..vias.len() {
        for jb in (j + 1)..vias.len() {
            if vias[j].at == vias[jb].at {
                for (&li, &(in_a, out_a)) in &via_nodes[j] {
                    if let Some(&(in_b, out_b)) = via_nodes[jb].get(&li) {
                        add_dir_edge(out_a, in_b, 0.0, 0.0, &mut adj);
                        add_dir_edge(out_b, in_a, 0.0, 0.0, &mut adj);
                    }
                }
            }
        }
    }

    for (i, nv) in neg_vias.iter().enumerate() {
        if nv.len() < 2 {
            continue;
        }
        let Some((_, _, _, layer_delay_rate, len_i)) = track_nodes[i] else {
            continue;
        };
        for a in 0..nv.len() {
            for b in (a + 1)..nv.len() {
                if nv[a].2 == nv[b].2 {
                    continue;
                }
                let eff_len = (len_i + nv[a].3 + nv[b].3).max(0.0);
                let eff_del = (eff_len / 1_000_000.0) * layer_delay_rate;
                add_dir_edge(nv[a].1, nv[b].0, eff_len, eff_del, &mut adj);
                add_dir_edge(nv[b].1, nv[a].0, eff_len, eff_del, &mut adj);
            }
        }
    }
    for tv in &mut track_pos_vias {
        tv.sort_unstable();
        tv.dedup();
    }

    // Unconditional pad-to-track edges for all net pads (handles PTH multi-layer + boundary + intermediate pads)
    for (k, p) in pads.iter().enumerate() {
        for (i, t) in tracks.iter().enumerate() {
            let Some((in_i, out_i, _, _, _)) = track_nodes[i] else {
                continue;
            };
            if pad_supports_layer(engine, p, &t.layer) && geometry::track_connects_pad(t, p) {
                add_dir_edge(k, in_i, 0.0, 0.0, &mut adj);
                add_dir_edge(out_i, k, 0.0, 0.0, &mut adj);
            }
        }
    }
    let s_node = 0usize;
    let t_node = 1usize;

    // Shortest useful path from s_node to t_node (non-negative weights guarantee stubs are never chosen)
    #[derive(Clone)]
    struct State {
        cost: f64,
        len_nm: f64,
        delay_as: f64,
        node: usize,
        exited_via: Option<usize>,
        blocked_vias: Vec<usize>,
    }
    impl PartialEq for State {
        fn eq(&self, other: &Self) -> bool {
            self.cost == other.cost
                && self.node == other.node
                && self.exited_via == other.exited_via
                && self.blocked_vias == other.blocked_vias
        }
    }
    impl Eq for State {}
    impl Ord for State {
        fn cmp(&self, other: &Self) -> std::cmp::Ordering {
            other
                .cost
                .partial_cmp(&self.cost)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| self.node.cmp(&other.node))
                .then_with(|| self.exited_via.cmp(&other.exited_via))
                .then_with(|| self.blocked_vias.cmp(&other.blocked_vias))
        }
    }
    impl PartialOrd for State {
        fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
            Some(self.cmp(other))
        }
    }

    let mut base_dist = vec![f64::INFINITY; adj.len()];
    let mut ext_dist: BTreeMap<(usize, Option<usize>, Vec<usize>), f64> = BTreeMap::new();
    let mut heap = BinaryHeap::new();
    base_dist[s_node] = 0.0;
    heap.push(State {
        cost: 0.0,
        len_nm: 0.0,
        delay_as: 0.0,
        node: s_node,
        exited_via: None,
        blocked_vias: Vec::new(),
    });
    while let Some(cur) = heap.pop() {
        if cur.node == t_node {
            return Ok(Some(NetMeasure {
                length_nm: cur.len_nm.round() as i64,
                delay_as: cur.delay_as.round() as i64,
            }));
        }
        let best = if cur.exited_via.is_none() && cur.blocked_vias.is_empty() {
            base_dist[cur.node]
        } else {
            ext_dist
                .get(&(cur.node, cur.exited_via, cur.blocked_vias.clone()))
                .copied()
                .unwrap_or(f64::INFINITY)
        };
        if cur.cost > best {
            continue;
        }
        let cur_kind = node_kind[cur.node];
        for &e in &adj[cur.node] {
            let next_kind = node_kind[e.to];
            if matches!(cur_kind, NodeKind::TrackOut(_) | NodeKind::TrackIn(_)) {
                if let NodeKind::ViaIn(vj, _) = next_kind {
                    if cur.exited_via == Some(vj) || cur.blocked_vias.binary_search(&vj).is_ok() {
                        continue;
                    }
                }
            }
            if let (NodeKind::TrackOut(_), NodeKind::TrackIn(ti)) = (cur_kind, next_kind) {
                if let Some(ev) = cur.exited_via {
                    if track_pos_vias[ti].binary_search(&ev).is_ok() {
                        continue;
                    }
                }
            }
            let (next_exited_via, next_blocked_vias) = match (cur_kind, next_kind) {
                (_, NodeKind::Pad | NodeKind::ViaIn(_, _) | NodeKind::ViaOut(_, _)) => {
                    (None, Vec::new())
                }
                (NodeKind::ViaOut(vj, vli), NodeKind::TrackIn(_)) => {
                    if (via_any_pos_tracks[vj][vli] && via_touch_count[vj][vli] >= 2)
                        || via_nodes[vj].len() >= 3
                    {
                        (Some(vj), Vec::new())
                    } else {
                        (None, Vec::new())
                    }
                }
                (NodeKind::ViaOut(_, _), NodeKind::TrackOut(_)) => (None, Vec::new()),
                (NodeKind::TrackOut(ti), NodeKind::TrackIn(_)) => {
                    let mut nb = cur.blocked_vias.clone();
                    if let Some((_, _, li_i, _, _)) = track_nodes[ti] {
                        for &vj in &track_pos_vias[ti] {
                            if via_touch_count[vj][li_i] >= 2 {
                                if let Err(pos) = nb.binary_search(&vj) {
                                    nb.insert(pos, vj);
                                }
                            }
                        }
                    }
                    (cur.exited_via, nb)
                }
                _ => (cur.exited_via, cur.blocked_vias.clone()),
            };
            let step_cost = if require_delay { e.delay_as } else { e.len_nm };
            let nc = cur.cost + step_cost;
            let improved = if next_exited_via.is_none() && next_blocked_vias.is_empty() {
                if nc < base_dist[e.to] {
                    base_dist[e.to] = nc;
                    true
                } else {
                    false
                }
            } else if nc < base_dist[e.to] {
                let key = (e.to, next_exited_via, next_blocked_vias.clone());
                let prev = ext_dist.get(&key).copied().unwrap_or(f64::INFINITY);
                if nc < prev {
                    ext_dist.insert(key, nc);
                    true
                } else {
                    false
                }
            } else {
                false
            };
            if improved {
                heap.push(State {
                    cost: nc,
                    len_nm: cur.len_nm + e.len_nm,
                    delay_as: cur.delay_as + e.delay_as,
                    node: e.to,
                    exited_via: next_exited_via,
                    blocked_vias: next_blocked_vias,
                });
            }
        }
    }
    Ok(None)
}
// ---------------------------------------------------------------------------
// Width-Override Segment Legality, Reference Zone Coverage & Mitered Offsets
// ---------------------------------------------------------------------------

fn legal_edge_with_width(
    engine: &Engine,
    net: u32,
    layer_idx: usize,
    a: Point,
    b: Point,
    width_nm: i64,
) -> Result<bool, Failure> {
    if a == b {
        return Ok(true);
    }
    let layer_name = engine.layer_name(layer_idx);
    let default_w = engine.effective_rules.track_width_for(net, layer_name)?;
    if width_nm <= default_w {
        return engine.legal_edge_on(net, layer_idx, a, b);
    }
    // Check default legality first (covers outline point-in-polygon, mask, artwork, etc.)
    if !engine.legal_edge_on(net, layer_idx, a, b)? {
        return Ok(false);
    }
    // Additional exact check at the wider width_nm (e.g. solved impedance width > class width)
    let r = engine.input.rules;
    let layer_slice = std::slice::from_ref(&engine.layer_names[layer_idx]);
    let edge_clr = engine
        .effective_rules
        .edge_clearance_for(net, DruItemKind::Track, layer_slice);
    let bounds = engine.input.bounds;
    let margin = i128::from(width_nm) + 2 * i128::from(edge_clr);
    for p in [a, b] {
        for (i, value) in p.iter().enumerate() {
            if 2 * i128::from(*value) < 2 * i128::from(bounds.min[i]) + margin
                || 2 * i128::from(*value) > 2 * i128::from(bounds.max[i]) - margin
            {
                return Ok(false);
            }
        }
    }
    if engine.input.board_outline.is_some() {
        let edge_swept = SweptAabb::swept(a, b, width_nm, edge_clr);
        let edge_flow =
            engine
                .outline_spatial
                .visit_matching(edge_swept, None, &engine.work, |_, edge| {
                    engine.work.charge(1)?;
                    if geometry::segments_near(
                        a,
                        b,
                        [i64::from(edge.a[0]), i64::from(edge.a[1])],
                        [i64::from(edge.b[0]), i64::from(edge.b[1])],
                        margin,
                        false,
                    ) {
                        return Ok(ControlFlow::Break(()));
                    }
                    Ok(ControlFlow::Continue(()))
                })?;
        if edge_flow.is_break() {
            return Ok(false);
        }
    }
    let ko_swept = SweptAabb::swept(a, b, width_nm, 0);
    let ko_flow = engine.keepout_spatial[layer_idx].visit_matching(
        ko_swept,
        None,
        &engine.work,
        |_, kg| {
            engine.work.charge(1)?;
            if kg.no_tracks {
                let ra = &engine.input.rule_areas[kg.rule_area_idx as usize];
                let (outer, holes) = if kg.poly_idx == 0 {
                    (&ra.outer, &ra.holes)
                } else {
                    let ep = &ra.extra_polygons[kg.poly_idx as usize - 1];
                    (&ep.outer, &ep.holes)
                };
                if geometry::track_near_rule_area_poly(a, b, width_nm, outer, holes) {
                    return Ok(ControlFlow::Break(()));
                }
            }
            Ok(ControlFlow::Continue(()))
        },
    )?;
    if ko_flow.is_break() {
        return Ok(false);
    }

    let max_zone_query_clr = engine
        .effective_rules
        .max_pair_clearance_nm
        .max(engine.max_zone_local_clearance_nm);
    let zone_swept = SweptAabb::swept(a, b, width_nm, max_zone_query_clr);
    let zone_flow = engine.zone_fill_spatial[layer_idx].visit_matching(
        zone_swept,
        Some(net),
        &engine.work,
        |_, zg| {
            engine.work.charge(1)?;
            if zg.refillable {
                return Ok(ControlFlow::Continue(()));
            }
            let z = &engine.input.zones[zg.zone_idx as usize];
            let Some(lf) = z.layer_fills.iter().find(|lf| lf.layer == *layer_name) else {
                return Ok(ControlFlow::Continue(()));
            };
            let poly = &lf.polygons[zg.poly_idx as usize];
            let clr = engine.effective_rules.pair_clearance(
                net,
                DruItemKind::Track,
                z.net,
                DruItemKind::Zone,
                layer_name,
                (z.local_clearance_nm > 0).then_some(z.local_clearance_nm),
            );
            if geometry::track_near_filled_polygon(
                a,
                b,
                width_nm,
                clr,
                &poly.outer,
                &poly.holes,
                false,
            ) {
                return Ok(ControlFlow::Break(()));
            }
            Ok(ControlFlow::Continue(()))
        },
    )?;
    if zone_flow.is_break() {
        return Ok(false);
    }
    let spoke_flow = engine.spoke_corridor_spatial[layer_idx].visit_matching(
        zone_swept,
        Some(net),
        &engine.work,
        |_, sg| {
            engine.work.charge(1)?;
            let sc = &engine.spoke_corridors[sg.corridor_idx as usize];
            let clr = engine.effective_rules.pair_clearance(
                net,
                DruItemKind::Track,
                sc.net,
                DruItemKind::Zone,
                layer_name,
                (sc.local_clearance_nm > 0).then_some(sc.local_clearance_nm),
            );
            if geometry::track_near_convex_poly(a, b, width_nm, clr, &sc.verts, 0, false) {
                return Ok(ControlFlow::Break(()));
            }
            Ok(ControlFlow::Continue(()))
        },
    )?;
    if spoke_flow.is_break() {
        return Ok(false);
    }

    let mask_pad_clr = if layer_name == "F.Cu" || layer_name == "B.Cu" {
        r.solder_mask_expansion_nm.max(0) + r.solder_mask_to_copper_clearance_nm.max(0)
    } else {
        0
    };
    let max_pad_query_clr = engine
        .effective_rules
        .max_pair_clearance_nm
        .max(engine.max_pad_local_clearance_nm)
        .max(mask_pad_clr);
    let pad_swept = SweptAabb::swept(a, b, width_nm, max_pad_query_clr);
    let pad_flow = engine.pad_spatial[layer_idx].visit_matching(
        pad_swept,
        Some(net),
        &engine.work,
        |ordinal, core| {
            engine.work.charge(1)?;
            let pad = &engine.input.pads[ordinal as usize];
            let pad_clr = engine
                .effective_rules
                .pair_clearance(
                    net,
                    DruItemKind::Track,
                    pad.net,
                    DruItemKind::Pad,
                    layer_name,
                    pad.local_clearance_nm,
                )
                .max(mask_pad_clr);
            let hit = match *core {
                PadCore::Box { lo, hi, radius } => geometry::track_near_core(
                    a,
                    b,
                    width_nm,
                    pad_clr,
                    [i64::from(lo[0]), i64::from(lo[1])],
                    [i64::from(hi[0]), i64::from(hi[1])],
                    i64::from(radius),
                    false,
                ),
                PadCore::Box2x { lo2, hi2, radius2 } => geometry::track_near_core_2x(
                    a,
                    b,
                    width_nm,
                    pad_clr,
                    [i64::from(lo2[0]), i64::from(lo2[1])],
                    [i64::from(hi2[0]), i64::from(hi2[1])],
                    i64::from(radius2),
                    false,
                ),
                PadCore::Capsule {
                    a: sa,
                    b: sb,
                    width,
                } => geometry::segments_near(
                    a,
                    b,
                    [i64::from(sa[0]), i64::from(sa[1])],
                    [i64::from(sb[0]), i64::from(sb[1])],
                    i128::from(width_nm) + i128::from(width) + 2 * i128::from(pad_clr),
                    false,
                ),
                PadCore::ConvexPoly => {
                    geometry::track_near_pad(a, b, width_nm, pad_clr, pad, false)
                }
            };
            if hit {
                return Ok(ControlFlow::Break(()));
            }
            Ok(ControlFlow::Continue(()))
        },
    )?;
    if pad_flow.is_break() {
        return Ok(false);
    }

    let swept = SweptAabb::swept(a, b, width_nm, engine.effective_rules.max_pair_clearance_nm);
    let track_flow = engine.copper.visit_layer_candidates_with_net_filtered(
        layer_idx,
        swept,
        Some(net),
        &engine.work,
        |other_net, t| {
            let clr = engine.effective_rules.pair_clearance(
                net,
                DruItemKind::Track,
                other_net,
                DruItemKind::Track,
                layer_name,
                None,
            );
            if geometry::segments_near(
                a,
                b,
                t.start,
                t.end,
                i128::from(width_nm) + i128::from(t.width_nm) + 2 * i128::from(clr),
                false,
            ) {
                return Ok(ControlFlow::Break(()));
            }
            Ok(ControlFlow::Continue(()))
        },
    )?;
    if track_flow.is_break() {
        return Ok(false);
    }

    let layer_bit = 1u32 << layer_idx;
    let max_via_query_clr = engine
        .effective_rules
        .max_pair_clearance_nm
        .max(engine.effective_rules.max_hole_clearance_nm)
        .max(r.same_net_hole_clearance_nm);
    let via_net_filter = if r.same_net_hole_clearance_nm > 0 {
        None
    } else {
        Some(net)
    };
    let via_swept = SweptAabb::swept(a, b, width_nm, max_via_query_clr);
    let via_flow = engine.copper.visit_via_candidates_filtered(
        via_swept,
        via_net_filter,
        &engine.work,
        |via, v_mask| {
            if (v_mask & layer_bit) == 0 {
                return Ok(ControlFlow::Continue(()));
            }
            if via.net == net && net != 0 {
                if r.same_net_hole_clearance_nm == 0 {
                    return Ok(ControlFlow::Continue(()));
                }
                let seg = Track {
                    id: String::new(),
                    net,
                    start: a,
                    end: b,
                    width_nm,
                    locked: false,
                    layer: layer_name.to_owned(),
                };
                if geometry::track_connects_via(&seg, via) {
                    return Ok(ControlFlow::Continue(()));
                }
                if geometry::point_near_segment(
                    via.at,
                    a,
                    b,
                    i128::from(width_nm)
                        + i128::from(via.drill_nm)
                        + 2 * i128::from(r.same_net_hole_clearance_nm),
                    false,
                ) {
                    return Ok(ControlFlow::Break(()));
                }
                return Ok(ControlFlow::Continue(()));
            }
            let copper_clr = engine.effective_rules.pair_clearance(
                net,
                DruItemKind::Track,
                via.net,
                DruItemKind::Via,
                layer_name,
                None,
            );
            if geometry::point_near_segment(
                via.at,
                a,
                b,
                i128::from(width_nm) + i128::from(via.diameter_nm) + 2 * i128::from(copper_clr),
                false,
            ) {
                return Ok(ControlFlow::Break(()));
            }
            Ok(ControlFlow::Continue(()))
        },
    )?;
    if via_flow.is_break() {
        return Ok(false);
    }

    let hole_net_filter = if r.same_net_hole_clearance_nm > 0 {
        None
    } else {
        Some(net)
    };
    let max_hole_clr = engine
        .effective_rules
        .max_hole_clearance_nm
        .max(r.same_net_hole_clearance_nm);
    let hole_swept = SweptAabb::swept(a, b, width_nm, max_hole_clr);
    let holes_len = engine.input.holes.len();
    let hole_flow = engine.hole_spatial.visit_matching(
        hole_swept,
        hole_net_filter,
        &engine.work,
        |ordinal, drill| {
            engine.work.charge(1)?;
            let idx = ordinal as usize;
            let (entry_net, maybe_pad) = if idx < holes_len {
                (engine.input.holes[idx].net, None)
            } else {
                let p = &engine.input.pads[idx - holes_len];
                (p.net, Some(p))
            };
            let clr = if entry_net == net && net != 0 {
                if r.same_net_hole_clearance_nm == 0 {
                    return Ok(ControlFlow::Continue(()));
                }
                if let Some(pad) = maybe_pad {
                    if pad_supports_layer(engine, pad, layer_name) {
                        let seg = Track {
                            id: String::new(),
                            net,
                            start: a,
                            end: b,
                            width_nm,
                            locked: false,
                            layer: layer_name.to_owned(),
                        };
                        if geometry::track_connects_pad(&seg, pad) {
                            return Ok(ControlFlow::Continue(()));
                        }
                    }
                }
                r.same_net_hole_clearance_nm
            } else if let Some(pad) = maybe_pad {
                let p_layers = engine.pad_layers_owned(pad);
                engine.effective_rules.pair_hole_clearance(
                    net,
                    DruItemKind::Track,
                    layer_slice,
                    entry_net,
                    DruItemKind::Pad,
                    &p_layers,
                )
            } else {
                engine.effective_rules.pair_hole_clearance(
                    net,
                    DruItemKind::Track,
                    layer_slice,
                    entry_net,
                    DruItemKind::Hole,
                    &engine.layer_names,
                )
            };
            let hit = match *drill {
                DrillGeom::Circle { center, diameter } => geometry::point_near_segment(
                    [i64::from(center[0]), i64::from(center[1])],
                    a,
                    b,
                    i128::from(width_nm) + i128::from(diameter) + 2 * i128::from(clr),
                    false,
                ),
                DrillGeom::Capsule {
                    a: da,
                    b: db,
                    width,
                } => geometry::segments_near(
                    a,
                    b,
                    [i64::from(da[0]), i64::from(da[1])],
                    [i64::from(db[0]), i64::from(db[1])],
                    i128::from(width_nm) + i128::from(width) + 2 * i128::from(clr),
                    false,
                ),
            };
            if hit {
                return Ok(ControlFlow::Break(()));
            }
            Ok(ControlFlow::Continue(()))
        },
    )?;
    if hole_flow.is_break() {
        return Ok(false);
    }
    if !engine.edge_clears_artwork(net, layer_idx, a, b, width_nm)? {
        return Ok(false);
    }
    engine.edge_clears_mask(net, layer_idx, a, b, width_nm)
}

#[allow(clippy::too_many_arguments)]
fn pair_mask_disjoint(
    engine: &Engine,
    layer_idx: usize,
    pa: Point,
    pb: Point,
    pw: i64,
    na: Point,
    nb: Point,
    nw: i64,
) -> Result<bool, Failure> {
    if engine.input.mask_apertures.is_empty()
        || engine.input.rules.solder_mask_to_copper_clearance_nm > 0
    {
        return Ok(true);
    }
    let mut p_hits = Vec::new();
    engine.mask_apertures_hit(layer_idx, pa, pb, pw, &mut p_hits)?;
    if p_hits.is_empty() {
        return Ok(true);
    }
    let mut n_hits = Vec::new();
    engine.mask_apertures_hit(layer_idx, na, nb, nw, &mut n_hits)?;
    Ok(!p_hits.iter().any(|m| n_hits.contains(m)))
}

fn point_in_poly_ray(pt: Point, outer: &[Point], holes: &[Vec<Point>]) -> bool {
    let inside = |ring: &[Point]| -> bool {
        if ring.len() < 3 {
            return false;
        }
        let mut odd = false;
        let x = pt[0] as f64;
        let y = pt[1] as f64;
        let mut j = ring.len() - 1;
        for i in 0..ring.len() {
            let xi = ring[i][0] as f64;
            let yi = ring[i][1] as f64;
            let xj = ring[j][0] as f64;
            let yj = ring[j][1] as f64;
            if ((yi > y) != (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi) {
                odd = !odd;
            }
            j = i;
        }
        odd
    };
    if !inside(outer) {
        return false;
    }
    !holes.iter().any(|h| inside(h))
}

fn point_over_layer(engine: &Engine, pt: Point, ref_net: u32, layer_name: &str) -> bool {
    engine.input.zones.iter().any(|z| {
        z.net == ref_net
            && z.layer_fills.iter().any(|lf| {
                lf.layer == layer_name
                    && lf
                        .polygons
                        .iter()
                        .any(|poly| point_in_poly_ray(pt, &poly.outer, &poly.holes))
            })
    })
}

fn point_over_reference_zones(engine: &Engine, pt: Point, imp: &ImpedancePlan) -> bool {
    imp.reference_layers
        .iter()
        .all(|rl| point_over_layer(engine, pt, imp.reference_net, rl))
}

fn ring_aabb_overlaps(ring: &[Point], qx0: i64, qx1: i64, qy0: i64, qy1: i64) -> bool {
    if ring.is_empty() {
        return false;
    }
    let mut rx0 = i64::MAX;
    let mut rx1 = i64::MIN;
    let mut ry0 = i64::MAX;
    let mut ry1 = i64::MIN;
    for p in ring {
        rx0 = rx0.min(p[0]);
        rx1 = rx1.max(p[0]);
        ry0 = ry0.min(p[1]);
        ry1 = ry1.max(p[1]);
    }
    rx1 >= qx0 && rx0 <= qx1 && ry1 >= qy0 && ry0 <= qy1
}

fn quad_over_reference_zones(engine: &Engine, quad: [Point; 4], imp: &ImpedancePlan) -> bool {
    let cx = (quad[0][0] + quad[1][0] + quad[2][0] + quad[3][0]) / 4;
    let cy = (quad[0][1] + quad[1][1] + quad[2][1] + quad[3][1]) / 4;
    for &pt in &quad {
        if !point_over_reference_zones(engine, pt, imp) {
            return false;
        }
    }
    if !point_over_reference_zones(engine, [cx, cy], imp) {
        return false;
    }

    let qx0 = quad[0][0].min(quad[1][0]).min(quad[2][0]).min(quad[3][0]);
    let qx1 = quad[0][0].max(quad[1][0]).max(quad[2][0]).max(quad[3][0]);
    let qy0 = quad[0][1].min(quad[1][1]).min(quad[2][1]).min(quad[3][1]);
    let qy1 = quad[0][1].max(quad[1][1]).max(quad[2][1]).max(quad[3][1]);

    // Precompute inward half-planes (nx, ny) for each quad edge
    let mut planes = [(0.0_f64, 0.0_f64, [0i64; 2]); 4];
    for i in 0..4 {
        let a = quad[i];
        let b = quad[(i + 1) % 4];
        let ex = (b[0] - a[0]) as f64;
        let ey = (b[1] - a[1]) as f64;
        let elen = ex.hypot(ey);
        if elen < 1.0 {
            return true;
        }
        let mut nx = -ey / elen;
        let mut ny = ex / elen;
        if ((cx - a[0]) as f64) * nx + ((cy - a[1]) as f64) * ny < 0.0 {
            nx = -nx;
            ny = -ny;
        }
        planes[i] = (nx, ny, a);
    }

    let in_quad = |pt: Point| -> bool {
        planes
            .iter()
            .all(|&(nx, ny, a)| ((pt[0] - a[0]) as f64) * nx + ((pt[1] - a[1]) as f64) * ny >= 0.0)
    };

    for rl in &imp.reference_layers {
        for z in &engine.input.zones {
            if z.net != imp.reference_net {
                continue;
            }
            for lf in &z.layer_fills {
                if lf.layer != *rl {
                    continue;
                }
                for poly in &lf.polygons {
                    for ring in std::iter::once(&poly.outer).chain(poly.holes.iter()) {
                        if ring.len() < 3 || !ring_aabb_overlaps(ring, qx0, qx1, qy0, qy1) {
                            continue;
                        }
                        for i in 0..ring.len() {
                            let e0 = ring[i];
                            let e1 = ring[(i + 1) % ring.len()];
                            if e0[0].max(e1[0]) < qx0
                                || e0[0].min(e1[0]) > qx1
                                || e0[1].max(e1[1]) < qy0
                                || e0[1].min(e1[1]) > qy1
                            {
                                continue;
                            }
                            let dx = (e1[0] - e0[0]) as f64;
                            let dy = (e1[1] - e0[1]) as f64;
                            let seg_len = dx.hypot(dy);
                            if seg_len < 1.0 {
                                continue;
                            }
                            let mut t_lo = 0.0_f64;
                            let mut t_hi = 1.0_f64;
                            let mut outside = false;
                            for &(nx, ny, a) in &planes {
                                let num =
                                    ((e0[0] - a[0]) as f64) * nx + ((e0[1] - a[1]) as f64) * ny;
                                let den = dx * nx + dy * ny;
                                if den.abs() < 1e-9 {
                                    if num < 0.0 {
                                        outside = true;
                                        break;
                                    }
                                } else if den > 0.0 {
                                    t_lo = t_lo.max(-num / den);
                                } else {
                                    t_hi = t_hi.min(-num / den);
                                }
                            }
                            if outside || t_hi <= t_lo || seg_len * (t_hi - t_lo) <= 1.0 {
                                continue;
                            }
                            let t_mid = 0.5 * (t_lo + t_hi);
                            let mx = e0[0] as f64 + t_mid * dx;
                            let my = e0[1] as f64 + t_mid * dy;
                            let n_perp = (-dy / seg_len, dx / seg_len);
                            let d_min = planes
                                .iter()
                                .map(|&(nx, ny, a)| {
                                    ((mx - a[0] as f64) * nx + (my - a[1] as f64) * ny).max(0.0)
                                })
                                .fold(f64::MAX, f64::min);
                            for base_eps in [5.0_f64, 100.0_f64] {
                                let eps = if d_min > 2.0 {
                                    base_eps.min(0.5 * d_min)
                                } else {
                                    base_eps
                                };
                                for sign in [1.0_f64, -1.0_f64] {
                                    let pt = [
                                        (mx + sign * eps * n_perp.0).round() as i64,
                                        (my + sign * eps * n_perp.1).round() as i64,
                                    ];
                                    if in_quad(pt)
                                        && !point_over_layer(engine, pt, imp.reference_net, rl)
                                    {
                                        return false;
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    true
}

fn segment_footprint_over_reference_zones(
    engine: &Engine,
    a: Point,
    b: Point,
    width_nm: i64,
    imp: &ImpedancePlan,
) -> bool {
    let Some((ux, uy)) = dir_unit_f64(a, b) else {
        return point_over_reference_zones(engine, a, imp);
    };
    let hw = 0.5 * (width_nm as f64);
    if hw < 1.0 {
        return point_over_reference_zones(engine, a, imp)
            && point_over_reference_zones(engine, b, imp)
            && point_over_reference_zones(engine, [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], imp);
    }
    let nx = -uy * hw;
    let ny = ux * hw;
    let quad = [
        [a[0] + nx.round() as i64, a[1] + ny.round() as i64],
        [b[0] + nx.round() as i64, b[1] + ny.round() as i64],
        [b[0] - nx.round() as i64, b[1] - ny.round() as i64],
        [a[0] - nx.round() as i64, a[1] - ny.round() as i64],
    ];
    quad_over_reference_zones(engine, quad, imp)
}

fn coupled_corridor_over_reference_zones(
    engine: &Engine,
    pa: Point,
    pb: Point,
    na: Point,
    nb: Point,
    width_nm: i64,
    imp: &ImpedancePlan,
) -> bool {
    if pa != pb && !segment_footprint_over_reference_zones(engine, pa, pb, width_nm, imp) {
        return false;
    }
    if na != nb && !segment_footprint_over_reference_zones(engine, na, nb, width_nm, imp) {
        return false;
    }
    let Some((ux, uy)) = dir_unit_f64(pa, pb) else {
        return true;
    };
    let Some((vx, vy)) = dir_unit_f64(na, nb) else {
        return true;
    };
    if (ux * vx + uy * vy).abs() < 0.999 {
        return true;
    }
    let len = dist_f64(pa, pb);
    let p1 = ux * ((na[0] - pa[0]) as f64) + uy * ((na[1] - pa[1]) as f64);
    let p2 = ux * ((nb[0] - pa[0]) as f64) + uy * ((nb[1] - pa[1]) as f64);
    let lo = p1.min(p2).max(0.0);
    let hi = p1.max(p2).min(len);
    if hi - lo <= 1.0 {
        return true;
    }
    // Left normal (-uy, ux): signed perpendicular offset of N relative to P
    let offset = (-uy) * ((na[0] - pa[0]) as f64) + ux * ((na[1] - pa[1]) as f64);
    let hw = 0.5 * (width_nm as f64);
    let low = (-hw).min(offset - hw);
    let high = hw.max(offset + hw);
    let pt_along_across = |along: f64, across: f64| -> Point {
        [
            pa[0] + (ux * along - uy * across).round() as i64,
            pa[1] + (uy * along + ux * across).round() as i64,
        ]
    };
    let corridor = [
        pt_along_across(lo, low),
        pt_along_across(hi, low),
        pt_along_across(hi, high),
        pt_along_across(lo, high),
    ];
    quad_over_reference_zones(engine, corridor, imp)
}

fn legal_pair_coupled_step(
    engine: &Engine,
    pair: &ResolvedDiffPair,
    layer_idx: usize,
    pa: Point,
    pb: Point,
    na: Point,
    nb: Point,
    width_nm: i64,
) -> Result<bool, Failure> {
    if !legal_edge_with_width(engine, pair.pos_net, layer_idx, pa, pb, width_nm)? {
        return Ok(false);
    }
    if !legal_edge_with_width(engine, pair.neg_net, layer_idx, na, nb, width_nm)? {
        return Ok(false);
    }
    // Enforce edge-to-edge gap >= gap_min_nm (segments_near threshold is w1 + w2 + 2 * clr)
    let min_center_dist = 2 * i128::from(width_nm) + 2 * i128::from(pair.gap_min_nm);
    if geometry::segments_near(pa, pb, na, nb, min_center_dist, false) {
        return Ok(false);
    }
    if !pair_mask_disjoint(engine, layer_idx, pa, pb, width_nm, na, nb, width_nm)? {
        return Ok(false);
    }
    if let Some(imp) = &pair.impedance {
        if engine.layer_name(layer_idx) != imp.signal_layer {
            return Ok(false);
        }
        if !coupled_corridor_over_reference_zones(engine, pa, pb, na, nb, width_nm, imp) {
            return Ok(false);
        }
    }
    Ok(true)
}
// ---------------------------------------------------------------------------
// Exact i128 Centerline Offset Polylines & Pad-Tail Cleanup
// ---------------------------------------------------------------------------

fn dist_f64(a: Point, b: Point) -> f64 {
    ((b[0] - a[0]) as f64).hypot((b[1] - a[1]) as f64)
}

fn dir_unit_f64(a: Point, b: Point) -> Option<(f64, f64)> {
    let dx = (b[0] - a[0]) as f64;
    let dy = (b[1] - a[1]) as f64;
    let len = dx.hypot(dy);
    if len < 1.0 {
        return None;
    }
    Some((dx / len, dy / len))
}

fn simplify_collinear_points(pts: Vec<Point>) -> Vec<Point> {
    let mut out: Vec<Point> = Vec::with_capacity(pts.len());
    for p in pts {
        if out.last().copied() == Some(p) {
            continue;
        }
        while out.len() >= 2 {
            let a = out[out.len() - 2];
            let b = out[out.len() - 1];
            let cross = i128::from(b[0] - a[0]) * i128::from(p[1] - b[1])
                - i128::from(b[1] - a[1]) * i128::from(p[0] - b[0]);
            let dot = i128::from(b[0] - a[0]) * i128::from(p[0] - b[0])
                + i128::from(b[1] - a[1]) * i128::from(p[1] - b[1]);
            if cross.abs() <= 4 && dot >= 0 {
                out.pop();
            } else {
                break;
            }
        }
        out.push(p);
    }
    out
}

fn round_outward(val: f64) -> i64 {
    if (val - val.round()).abs() <= 1e-6 {
        val.round() as i64
    } else if val >= 0.0 {
        val.ceil() as i64
    } else {
        val.floor() as i64
    }
}

/// Computes exact parallel left (+h) and right (-h) mitered offset polylines for a centerline.
/// Left normal of unit tangent (ux, uy) is (-uy, ux).
fn offset_centerline_mitered(
    centerline: &[Point],
    s_nm: i64,
    pos_on_left: bool,
) -> (Vec<Point>, Vec<Point>) {
    let pts = simplify_collinear_points(centerline.to_vec());
    if pts.len() < 2 {
        return (pts.clone(), pts);
    }
    let h_total = s_nm as f64;
    let h_left = 0.5 * h_total;
    let h_right = 0.5 * h_total;

    let seg_count = pts.len() - 1;
    let mut normals: Vec<(f64, f64)> = Vec::with_capacity(seg_count);
    for w in pts.windows(2) {
        if let Some((ux, uy)) = dir_unit_f64(w[0], w[1]) {
            normals.push((-uy, ux));
        }
    }
    if normals.is_empty() {
        return (vec![], vec![]);
    }

    let miter_vertex = |c: Point, n0: (f64, f64), n1: (f64, f64), signed_h: f64| -> Point {
        // Intersection of line (c + h*n0 + t*d0) and line (c + h*n1 + s*d1):
        // Solve p . n0 = c . n0 + h and p . n1 = c . n1 + h for offset delta = p - c:
        // delta . n0 = h, delta . n1 = h => det = n0.x * n1.y - n0.y * n1.x
        let det = n0.0 * n1.1 - n0.1 * n1.0;
        if det.abs() < 1e-6 {
            return [
                c[0] + round_outward(signed_h * n0.0),
                c[1] + round_outward(signed_h * n0.1),
            ];
        }
        let dx = (signed_h * (n1.1 - n0.1)) / det;
        let dy = (signed_h * (n0.0 - n1.0)) / det;
        // Clamp extreme miter spikes on sharp angles (fallback to normal average)
        if dx.hypot(dy) > 3.0 * signed_h.abs() {
            let nx = 0.5 * (n0.0 + n1.0);
            let ny = 0.5 * (n0.1 + n1.1);
            let nl = nx.hypot(ny).max(1e-9);
            return [
                c[0] + round_outward(signed_h * nx / nl),
                c[1] + round_outward(signed_h * ny / nl),
            ];
        }
        [c[0] + round_outward(dx), c[1] + round_outward(dy)]
    };

    let mut left_poly = Vec::with_capacity(pts.len());
    let mut right_poly = Vec::with_capacity(pts.len());
    // Start point
    let n_first = normals[0];
    left_poly.push([
        pts[0][0] + round_outward(h_left * n_first.0),
        pts[0][1] + round_outward(h_left * n_first.1),
    ]);
    right_poly.push([
        pts[0][0] - round_outward(h_right * n_first.0),
        pts[0][1] - round_outward(h_right * n_first.1),
    ]);
    // Interior vertices
    for i in 1..normals.len() {
        let n0 = normals[i - 1];
        let n1 = normals[i];
        let c = pts[i];
        left_poly.push(miter_vertex(c, n0, n1, h_left));
        right_poly.push(miter_vertex(c, n0, n1, -h_right));
    }
    // End point
    let n_last = *normals.last().unwrap();
    let p_last = *pts.last().unwrap();
    left_poly.push([
        p_last[0] + round_outward(h_left * n_last.0),
        p_last[1] + round_outward(h_left * n_last.1),
    ]);
    right_poly.push([
        p_last[0] - round_outward(h_right * n_last.0),
        p_last[1] - round_outward(h_right * n_last.1),
    ]);

    if pos_on_left {
        (
            simplify_collinear_points(left_poly),
            simplify_collinear_points(right_poly),
        )
    } else {
        (
            simplify_collinear_points(right_poly),
            simplify_collinear_points(left_poly),
        )
    }
}

/// Removes dangling degree-1 endpoints that terminate inside a terminal pad other than `pad.at`,
/// preventing `track_dangling` DRC violations when coupled segments clip across pad copper.
fn trim_dangling_pad_tails(tracks: &mut Vec<Track>, pads: &[&crate::model::Pad], engine: &Engine) {
    tracks.retain(|t| t.start != t.end);
    loop {
        let mut deg: BTreeMap<(Point, &str), usize> = BTreeMap::new();
        for t in tracks.iter() {
            *deg.entry((t.start, t.layer.as_str())).or_insert(0) += 1;
            *deg.entry((t.end, t.layer.as_str())).or_insert(0) += 1;
        }
        let mut changed = false;
        let mut next_tracks = Vec::with_capacity(tracks.len());
        for t in tracks.iter() {
            let ds = deg.get(&(t.start, t.layer.as_str())).copied().unwrap_or(0);
            let de = deg.get(&(t.end, t.layer.as_str())).copied().unwrap_or(0);
            let is_pad_center = |pt: Point| pads.iter().any(|p| p.at == pt);
            let in_pad_copper = |pt: Point, layer: &str| {
                pads.iter().any(|p| {
                    pad_supports_layer(engine, p, layer)
                        && geometry::track_near_pad(pt, pt, 0, 0, p, false)
                })
            };
            if (ds == 1 && !is_pad_center(t.start) && in_pad_copper(t.start, &t.layer))
                || (de == 1 && !is_pad_center(t.end) && in_pad_copper(t.end, &t.layer))
            {
                changed = true;
                continue;
            }
            next_tracks.push(t.clone());
        }
        *tracks = next_tracks;
        if !changed {
            break;
        }
    }
}

/// Computes a 1-segment or 2-segment octilinear (45-deg / ortho) path between `a` and `b`.
fn octilinear_elbow_paths(a: Point, b: Point) -> Vec<Vec<Point>> {
    if a == b {
        return vec![vec![a]];
    }
    let dx = b[0] - a[0];
    let dy = b[1] - a[1];
    if dx == 0 || dy == 0 || dx.abs() == dy.abs() {
        return vec![vec![a, b]];
    }
    let sx = dx.signum();
    let sy = dy.signum();
    let adx = dx.abs();
    let ady = dy.abs();
    let d_diag = adx.min(ady);
    // Path 1: diagonal first, then axis-aligned
    let elbow1 = [a[0] + sx * d_diag, a[1] + sy * d_diag];
    // Path 2: axis-aligned first, then diagonal
    let elbow2 = if adx > ady {
        [a[0] + sx * (adx - ady), a[1]]
    } else {
        [a[0], a[1] + sy * (ady - adx)]
    };
    vec![vec![a, elbow1, b], vec![a, elbow2, b]]
}

#[derive(Clone, Debug)]
struct EndFanout {
    gather: Point,
    gp: Point,
    gn: Point,
    dir: (f64, f64),
    layer_idx: usize,
    pos_on_left: bool,
    pos_segs: Vec<(Point, Point, i64)>,
    neg_segs: Vec<(Point, Point, i64)>,
    uncoupled_nm: i64,
    est_uncoupled_nm: i64,
    skew_nm: i64,
    short_seg_count: usize,
}

fn find_pad_pairs<'a>(
    input: &'a Input,
    pos_net: u32,
    neg_net: u32,
) -> Option<[(&'a crate::model::Pad, &'a crate::model::Pad); 2]> {
    let pos_pads: Vec<_> = input.pads.iter().filter(|p| p.net == pos_net).collect();
    let neg_pads: Vec<_> = input.pads.iter().filter(|p| p.net == neg_net).collect();
    if pos_pads.len() < 2 || neg_pads.len() < 2 {
        return None;
    }
    // Find the two closest distinct (pos_pad, neg_pad) terminal groupings.
    let mut best: Option<(f64, [(&crate::model::Pad, &crate::model::Pad); 2])> = None;
    for i in 0..pos_pads.len() {
        for j in (i + 1)..pos_pads.len() {
            for a in 0..neg_pads.len() {
                for b in 0..neg_pads.len() {
                    if a == b {
                        continue;
                    }
                    let d = dist_f64(pos_pads[i].at, neg_pads[a].at)
                        + dist_f64(pos_pads[j].at, neg_pads[b].at);
                    if best.as_ref().is_none_or(|(bd, _)| d < *bd) {
                        best = Some((d, [(pos_pads[i], neg_pads[a]), (pos_pads[j], neg_pads[b])]));
                    }
                }
            }
        }
    }
    best.map(|(_, pair)| pair)
}

fn fanout_path_exits_pad_cleanly(
    path: &[Point],
    pad: &crate::model::Pad,
    esc_width: i64,
    trunk_width: i64,
) -> bool {
    if path.len() < 2 || path.first() == path.last() {
        return true;
    }
    let exit_pt = *path.last().unwrap();
    if geometry::track_near_pad(
        exit_pt,
        exit_pt,
        trunk_width.max(esc_width),
        1_000,
        pad,
        true,
    ) {
        return false;
    }
    for &v in &path[1..path.len() - 1] {
        if geometry::track_near_pad(v, v, esc_width, 1_000, pad, true) {
            return false;
        }
    }
    for seg in path.windows(2).skip(1) {
        if geometry::track_near_pad(seg[0], seg[1], esc_width, 1_000, pad, true) {
            return false;
        }
    }
    true
}

fn enumerate_end_fanouts(
    engine: &Engine,
    pair: &ResolvedDiffPair,
    p_pad: &crate::model::Pad,
    n_pad: &crate::model::Pad,
    other_mid: Point,
    is_end: bool,
    allowed_layer_idxs: &[usize],
) -> Result<Vec<EndFanout>, Failure> {
    let s_nm = pair.width_nm + pair.gap_nm;
    let mid = [
        (p_pad.at[0] + n_pad.at[0]) / 2,
        (p_pad.at[1] + n_pad.at[1]) / 2,
    ];
    let inv_sqrt2 = std::f64::consts::FRAC_1_SQRT_2;
    let oct_dirs: [(f64, f64); 8] = [
        (1.0, 0.0),
        (inv_sqrt2, inv_sqrt2),
        (0.0, 1.0),
        (-inv_sqrt2, inv_sqrt2),
        (-1.0, 0.0),
        (-inv_sqrt2, -inv_sqrt2),
        (0.0, -1.0),
        (inv_sqrt2, -inv_sqrt2),
    ];
    // For end fanout, `dir` represents the forward path direction (from other_mid -> mid),
    // whereas the gather offset from `mid` moves backward (-dir) toward `other_mid`.
    let forward_ref = if is_end {
        dir_unit_f64(other_mid, mid).unwrap_or((1.0, 0.0))
    } else {
        dir_unit_f64(mid, other_mid).unwrap_or((1.0, 0.0))
    };
    let mut ranked_dirs: Vec<(f64, (f64, f64))> = oct_dirs
        .iter()
        .map(|&d| (d.0 * forward_ref.0 + d.1 * forward_ref.1, d))
        .collect();
    ranked_dirs.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));

    let pad_dist = dist_f64(p_pad.at, n_pad.at);
    let mut widths_to_try = vec![pair.width_nm];
    if pair.class_width_nm < pair.width_nm {
        widths_to_try.push(pair.class_width_nm);
    }

    let mut out = Vec::new();
    let max_reach = (pair.uncoupled_max_nm / 2).max(1_000_000).min(4_000_000);
    let offsets_nm: [i64; 12] = [
        0, 350_000, 500_000, 700_000, 1_000_000, 1_250_000, 1_500_000, 1_750_000, 2_000_000,
        2_500_000, 3_000_000, max_reach,
    ];

    let mut candidate_layers: Vec<usize> = allowed_layer_idxs
        .iter()
        .copied()
        .filter(|&li| {
            let lname = engine.layer_name(li);
            pad_supports_layer(engine, p_pad, lname) && pad_supports_layer(engine, n_pad, lname)
        })
        .collect();
    if candidate_layers.is_empty() {
        // SMD pads on a layer excluded from --pair-layers: escape on pad layer to coupled via
        for li in 0..engine.layer_count() {
            let lname = engine.layer_name(li);
            if pad_supports_layer(engine, p_pad, lname) && pad_supports_layer(engine, n_pad, lname)
            {
                candidate_layers.push(li);
            }
        }
    }

    for &li in &candidate_layers {
        let needs_via_escape = !allowed_layer_idxs.contains(&li);
        for &(_, dir) in &ranked_dirs {
            let left = (-dir.1, dir.0);
            let pn_vec = (
                (p_pad.at[0] - n_pad.at[0]) as f64,
                (p_pad.at[1] - n_pad.at[1]) as f64,
            );
            let pos_on_left = (pn_vec.0 * left.0 + pn_vec.1 * left.1) >= 0.0;
            let sign = if pos_on_left { 1.0 } else { -1.0 };
            let step_sign = if is_end { -1.0 } else { 1.0 };
            let esc_dir = (step_sign * dir.0, step_sign * dir.1);

            for &off in &offsets_nm {
                if off == 0 && (pad_dist - s_nm as f64).abs() > 2_000.0 {
                    continue;
                }
                if off > max_reach {
                    continue;
                }
                let g = [
                    mid[0] + (step_sign * (off as f64) * dir.0).round() as i64,
                    mid[1] + (step_sign * (off as f64) * dir.1).round() as i64,
                ];
                let h = 0.5 * (s_nm as f64);
                let gp = [
                    g[0] + (sign * h * left.0).round() as i64,
                    g[1] + (sign * h * left.1).round() as i64,
                ];
                let gn = [
                    g[0] - (sign * h * left.0).round() as i64,
                    g[1] - (sign * h * left.1).round() as i64,
                ];
                let p_fwd = (gp[0] - p_pad.at[0]) as f64 * esc_dir.0
                    + (gp[1] - p_pad.at[1]) as f64 * esc_dir.1;
                let n_fwd = (gn[0] - n_pad.at[0]) as f64 * esc_dir.0
                    + (gn[1] - n_pad.at[1]) as f64 * esc_dir.1;
                if p_fwd < -1.0 || n_fwd < -1.0 {
                    continue;
                }
                if needs_via_escape
                    && !allowed_layer_idxs.iter().any(|&to_li| {
                        let (from_l, to_l) = if is_end { (to_li, li) } else { (li, to_li) };
                        let Ok(Some((vp, vn, _, _))) =
                            coupled_via_pair_check(engine, pair, g, dir, pos_on_left, from_l, to_l)
                        else {
                            return false;
                        };
                        let p_ok = vp.at == p_pad.at || !geometry::via_connects_pad(&vp, p_pad);
                        let n_ok = vn.at == n_pad.at || !geometry::via_connects_pad(&vn, n_pad);
                        p_ok && n_ok
                    })
                {
                    continue;
                }
                if let Some(imp) = &pair.impedance {
                    if !point_over_reference_zones(engine, gp, imp)
                        || !point_over_reference_zones(engine, gn, imp)
                    {
                        continue;
                    }
                }

                let path_short_and_non_oct = |path: &[Point], ew: i64| -> (usize, usize) {
                    let short_thresh = (ew.max(pair.width_nm) + 5_000) as f64;
                    let mut short_segs = 0usize;
                    if path.len() >= 3 {
                        for w in path.windows(2) {
                            if w[0] != w[1] && dist_f64(w[0], w[1]) <= short_thresh {
                                short_segs += 1;
                            }
                        }
                        if dist_f64(path[0], path[path.len() - 1]) <= short_thresh {
                            short_segs += 1;
                        }
                    }
                    let non_oct = path
                        .windows(2)
                        .filter(|w| {
                            w[0] != w[1]
                                && w[0][0] != w[1][0]
                                && w[0][1] != w[1][1]
                                && (w[1][0] - w[0][0]).abs() != (w[1][1] - w[0][1]).abs()
                        })
                        .count();
                    (short_segs, non_oct)
                };

                let mut best_overall: Option<((usize, u8, i64, usize, i64), EndFanout)> = None;
                'esc: for &ew in &widths_to_try {
                    let mut p_paths = octilinear_elbow_paths(p_pad.at, gp);
                    if p_pad.at != gp
                        && !p_paths
                            .iter()
                            .any(|p| p.len() == 2 && p[0] == p_pad.at && p[1] == gp)
                    {
                        p_paths.push(vec![p_pad.at, gp]);
                    }
                    let mut n_paths = octilinear_elbow_paths(n_pad.at, gn);
                    if n_pad.at != gn
                        && !n_paths
                            .iter()
                            .any(|p| p.len() == 2 && p[0] == n_pad.at && p[1] == gn)
                    {
                        n_paths.push(vec![n_pad.at, gn]);
                    }
                    let mut best_ew: Option<((usize, i64, usize, i64), EndFanout)> = None;
                    for pp in &p_paths {
                        if !fanout_path_exits_pad_cleanly(pp, p_pad, ew, pair.width_nm) {
                            continue;
                        }
                        let p_legal = pp.windows(2).all(|seg| {
                            legal_edge_with_width(engine, pair.pos_net, li, seg[0], seg[1], ew)
                                .unwrap_or(false)
                        });
                        if !p_legal {
                            continue;
                        }
                        for np in &n_paths {
                            if !fanout_path_exits_pad_cleanly(np, n_pad, ew, pair.width_nm) {
                                continue;
                            }
                            let n_legal = np.windows(2).all(|seg| {
                                legal_edge_with_width(engine, pair.neg_net, li, seg[0], seg[1], ew)
                                    .unwrap_or(false)
                            });
                            if !n_legal {
                                continue;
                            }
                            let mut cross_ok = true;
                            for ps in pp.windows(2) {
                                if ps[0] == ps[1] {
                                    continue;
                                }
                                for ns in np.windows(2) {
                                    if ns[0] == ns[1] {
                                        continue;
                                    }
                                    if !fanout_trunk_pair_ok(
                                        pair, ps[0], ps[1], ew, ns[0], ns[1], ew,
                                    ) || !pair_mask_disjoint(
                                        engine, li, ps[0], ps[1], ew, ns[0], ns[1], ew,
                                    )? {
                                        cross_ok = false;
                                        break;
                                    }
                                }
                                if !cross_ok {
                                    break;
                                }
                            }
                            if !cross_ok {
                                continue;
                            }
                            let lp: f64 = pp.windows(2).map(|w| dist_f64(w[0], w[1])).sum();
                            let ln: f64 = np.windows(2).map(|w| dist_f64(w[0], w[1])).sum();
                            let skew_nm = (lp - ln).abs().round() as i64;
                            let pos_segs: Vec<_> = pp
                                .windows(2)
                                .filter(|w| w[0] != w[1])
                                .map(|w| (w[0], w[1], ew))
                                .collect();
                            let neg_segs: Vec<_> = np
                                .windows(2)
                                .filter(|w| w[0] != w[1])
                                .map(|w| (w[0], w[1], ew))
                                .collect();
                            let lname = engine.layer_name(li);
                            let fan_tracks: Vec<Track> = pos_segs
                                .iter()
                                .map(|&(a, b, w)| Track {
                                    id: String::new(),
                                    net: pair.pos_net,
                                    start: a,
                                    end: b,
                                    width_nm: w,
                                    locked: false,
                                    layer: lname.to_owned(),
                                })
                                .chain(neg_segs.iter().map(|&(a, b, w)| Track {
                                    id: String::new(),
                                    net: pair.neg_net,
                                    start: a,
                                    end: b,
                                    width_nm: w,
                                    locked: false,
                                    layer: lname.to_owned(),
                                }))
                                .collect();
                            let est_uncoupled_nm = measure_tracks_uncoupled_nm(pair, &fan_tracks);
                            let uncoupled_nm = lp.max(ln).round() as i64;
                            let (sp, op) = path_short_and_non_oct(pp, ew);
                            let (sn, on) = path_short_and_non_oct(np, ew);
                            let short_seg_count = sp + sn;
                            let non_oct_count = op + on;
                            let key = (short_seg_count, skew_nm, non_oct_count, uncoupled_nm);
                            let cand = EndFanout {
                                gather: g,
                                gp,
                                gn,
                                dir,
                                layer_idx: li,
                                pos_on_left,
                                pos_segs,
                                neg_segs,
                                uncoupled_nm,
                                est_uncoupled_nm,
                                skew_nm,
                                short_seg_count,
                            };
                            if best_ew.as_ref().is_none_or(|(bk, _)| key < *bk) {
                                best_ew = Some((key, cand));
                            }
                        }
                    }
                    if let Some((ew_key, fan)) = best_ew {
                        if ew == pair.width_nm && ew_key.0 == 0 {
                            out.push(fan);
                            best_overall = None;
                            break 'esc;
                        }
                        let okey = (
                            ew_key.0,
                            u8::from(ew < pair.width_nm),
                            ew_key.1,
                            ew_key.2,
                            ew_key.3,
                        );
                        if best_overall.as_ref().is_none_or(|(bk, _)| okey < *bk) {
                            best_overall = Some((okey, fan));
                        }
                    }
                }
                if let Some((_, fan)) = best_overall {
                    out.push(fan);
                }
            }
        }
    }
    out.sort_by_key(|f| (f.short_seg_count, f.uncoupled_nm, f.skew_nm));
    Ok(out)
}
// ---------------------------------------------------------------------------
// Coupled Centerline Path Search (Direct + Multi-Layer Octilinear A*)
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
struct CoupledSolution {
    tracks: Vec<Track>,
    vias: Vec<Via>,
}

fn coupled_via_pair_check(
    engine: &Engine,
    pair: &ResolvedDiffPair,
    at: Point,
    dir: (f64, f64),
    pos_on_left: bool,
    from_layer: usize,
    to_layer: usize,
) -> Result<Option<(Via, Via, Point, Point)>, Failure> {
    if from_layer == to_layer {
        return Ok(None);
    }
    let (s_idx, e_idx, kind) = if engine.effective_rules.allow_blind_buried_vias
        && !(from_layer.min(to_layer) == 0 && from_layer.max(to_layer) + 1 == engine.layer_count())
    {
        (
            from_layer.min(to_layer),
            from_layer.max(to_layer),
            ViaKind::BlindBuried,
        )
    } else {
        (
            0usize,
            engine.layer_count().saturating_sub(1),
            ViaKind::Through,
        )
    };
    let span_layers: Vec<String> = (s_idx..=e_idx)
        .map(|i| engine.layer_name(i).to_owned())
        .collect();
    let (v_diam_p, v_drill_p) =
        match engine
            .effective_rules
            .via_dims_for(pair.pos_net, false, &span_layers)
        {
            Ok(d) => d,
            Err(_) => return Ok(None),
        };
    let (v_diam_n, v_drill_n) =
        match engine
            .effective_rules
            .via_dims_for(pair.neg_net, false, &span_layers)
        {
            Ok(d) => d,
            Err(_) => return Ok(None),
        };
    let s_track = pair.width_nm + pair.gap_nm;
    let via_diam = v_diam_p.max(v_diam_n);
    let via_clr = engine.effective_rules.pair_clearance(
        pair.pos_net,
        DruItemKind::Via,
        pair.neg_net,
        DruItemKind::Via,
        engine.layer_name(from_layer),
        None,
    );
    let via_sep = (via_diam + pair.via_gap_nm.max(via_clr)).max(s_track);
    let half_via = 0.5 * (via_sep as f64);
    let left = (-dir.1, dir.0);
    let sign = if pos_on_left { 1.0 } else { -1.0 };
    let vp = [
        at[0] + (sign * half_via * left.0).round() as i64,
        at[1] + (sign * half_via * left.1).round() as i64,
    ];
    let vn = [
        at[0] - (sign * half_via * left.0).round() as i64,
        at[1] - (sign * half_via * left.1).round() as i64,
    ];

    let Some((d_p, dr_p)) = engine.legal_via(pair.pos_net, vp, s_idx, e_idx, kind, &[])? else {
        return Ok(None);
    };
    let key_p = PlacedViaKey {
        at: vp,
        start_layer: s_idx as u8,
        end_layer: e_idx as u8,
        kind: 0,
    };
    let Some((d_n, dr_n)) = engine.legal_via(pair.neg_net, vn, s_idx, e_idx, kind, &[key_p])?
    else {
        return Ok(None);
    };
    let via_p = Via {
        id: String::new(),
        net: pair.pos_net,
        at: vp,
        diameter_nm: d_p.max(v_diam_p),
        drill_nm: dr_p.max(v_drill_p),
        locked: false,
        start_layer: engine.layer_name(s_idx).to_owned(),
        end_layer: engine.layer_name(e_idx).to_owned(),
        kind,
    };
    let via_n = Via {
        id: String::new(),
        net: pair.neg_net,
        at: vn,
        diameter_nm: d_n.max(v_diam_n),
        drill_nm: dr_n.max(v_drill_n),
        locked: false,
        start_layer: engine.layer_name(s_idx).to_owned(),
        end_layer: engine.layer_name(e_idx).to_owned(),
        kind,
    };
    for l_idx in s_idx..=e_idx {
        if !pair_mask_disjoint(
            engine,
            l_idx,
            vp,
            vp,
            via_p.diameter_nm,
            vn,
            vn,
            via_n.diameter_nm,
        )? {
            return Ok(None);
        }
    }
    Ok(Some((via_p, via_n, vp, vn)))
}

fn coupled_gap_max_ok(pair: &ResolvedDiffPair, p_poly: &[Point], n_poly: &[Point]) -> bool {
    let max_center_dist2 = 2 * i128::from(pair.width_nm) + 2 * i128::from(pair.gap_max_nm) + 20;
    for w in p_poly.windows(2) {
        let mid = [(w[0][0] + w[1][0]) / 2, (w[0][1] + w[1][1]) / 2];
        if !n_poly
            .windows(2)
            .any(|nw| geometry::point_near_segment(mid, nw[0], nw[1], max_center_dist2, true))
        {
            return false;
        }
    }
    for w in n_poly.windows(2) {
        let mid = [(w[0][0] + w[1][0]) / 2, (w[0][1] + w[1][1]) / 2];
        if !p_poly
            .windows(2)
            .any(|pw| geometry::point_near_segment(mid, pw[0], pw[1], max_center_dist2, true))
        {
            return false;
        }
    }
    true
}

fn fanout_trunk_pair_ok(
    pair: &ResolvedDiffPair,
    fa: Point,
    fb: Point,
    fw: i64,
    na: Point,
    nb: Point,
    nw: i64,
) -> bool {
    let min_clr2 = i128::from(fw) + i128::from(nw) + 2 * i128::from(pair.clearance_nm);
    if geometry::segments_near(fa, fb, na, nb, min_clr2, false) {
        return false;
    }
    if let (Some((ufx, ufy)), Some((unx, uny))) = (dir_unit_f64(fa, fb), dir_unit_f64(na, nb)) {
        let abs_dot = (ufx * unx + ufy * uny).abs();
        if abs_dot >= 0.999 {
            let len = dist_f64(fa, fb);
            let p1 = ufx * ((na[0] - fa[0]) as f64) + ufy * ((na[1] - fa[1]) as f64);
            let p2 = ufx * ((nb[0] - fa[0]) as f64) + ufy * ((nb[1] - fa[1]) as f64);
            let lo = p1.min(p2).max(0.0);
            let hi = p1.max(p2).min(len);
            if hi > lo + 1_000.0 {
                let perp = (ufx * ((na[1] - fa[1]) as f64) - ufy * ((na[0] - fa[0]) as f64)).abs();
                let min_center = 0.5 * ((fw + nw) as f64) + (pair.gap_min_nm as f64) - 1.0;
                if perp < min_center {
                    return false;
                }
                if pair.has_dru_gap_max && abs_dot >= 0.99999 {
                    let max_center = 0.5 * ((fw + nw) as f64) + (pair.gap_max_nm as f64) + 1.0;
                    if perp > max_center {
                        return false;
                    }
                }
            }
        }
    }
    true
}

fn net_clear_non_root_pads(engine: &Engine, net: u32, tracks: &[Track], vias: &[Via]) -> bool {
    for pad in engine.input.pads.iter().filter(|p| p.net == net) {
        let mut root_touches = 0usize;
        for t in tracks {
            if t.net != net || !pad_supports_layer(engine, pad, &t.layer) {
                continue;
            }
            if t.start == pad.at || t.end == pad.at {
                root_touches += 1;
                if root_touches > 1 {
                    return false;
                }
            } else if geometry::track_near_pad(t.start, t.end, t.width_nm, 1_000, pad, true) {
                return false;
            }
        }
        for v in vias {
            if v.net != net || v.at == pad.at {
                continue;
            }
            let s_idx = engine.layer_index(&v.start_layer).unwrap_or(0);
            let e_idx = engine.layer_index(&v.end_layer).unwrap_or(s_idx);
            let shares_layer = (s_idx.min(e_idx)..=s_idx.max(e_idx))
                .any(|li| pad_supports_layer(engine, pad, engine.layer_name(li)));
            if shares_layer && geometry::via_connects_pad(v, pad) {
                return false;
            }
        }
    }
    true
}

fn validate_via_jog_pair(
    engine: &Engine,
    pair: &ResolvedDiffPair,
    layer_idx: usize,
    p_a: Point,
    p_b: Point,
    n_a: Point,
    n_b: Point,
) -> Result<Option<i64>, Failure> {
    if p_a != p_b
        && !legal_edge_with_width(engine, pair.pos_net, layer_idx, p_a, p_b, pair.width_nm)?
    {
        return Ok(None);
    }
    if n_a != n_b
        && !legal_edge_with_width(engine, pair.neg_net, layer_idx, n_a, n_b, pair.width_nm)?
    {
        return Ok(None);
    }
    if p_a != p_b || n_a != n_b {
        if let Some(imp) = &pair.impedance {
            if engine.layer_name(layer_idx) != imp.signal_layer
                || !coupled_corridor_over_reference_zones(
                    engine,
                    p_a,
                    p_b,
                    n_a,
                    n_b,
                    pair.width_nm,
                    imp,
                )
            {
                return Ok(None);
            }
        }
        let min_clr2 = 2 * i128::from(pair.width_nm) + 2 * i128::from(pair.clearance_nm);
        if geometry::segments_near(p_a, p_b, n_a, n_b, min_clr2, false) {
            return Ok(None);
        }
        if !pair_mask_disjoint(
            engine,
            layer_idx,
            p_a,
            p_b,
            pair.width_nm,
            n_a,
            n_b,
            pair.width_nm,
        )? {
            return Ok(None);
        }
    }
    let lp = if p_a != p_b { dist_f64(p_a, p_b) } else { 0.0 };
    let ln = if n_a != n_b { dist_f64(n_a, n_b) } else { 0.0 };
    Ok(Some(lp.max(ln).round() as i64))
}

fn validate_and_build_coupled_solution(
    engine: &Engine,
    pair: &ResolvedDiffPair,
    start_f: &EndFanout,
    end_f: &EndFanout,
    centerline_runs: &[(Vec<Point>, usize)],
) -> Result<Option<CoupledSolution>, Failure> {
    if start_f.pos_on_left != end_f.pos_on_left {
        return Ok(None);
    }
    // Reject >90-degree foldbacks within any centerline run
    for (run_pts, _) in centerline_runs {
        let mut prev_dir: Option<(f64, f64)> = None;
        for w in run_pts.windows(2) {
            let Some(d) = dir_unit_f64(w[0], w[1]) else {
                continue;
            };
            if let Some(pd) = prev_dir {
                if pd.0 * d.0 + pd.1 * d.1 < -1e-6 {
                    return Ok(None);
                }
            }
            prev_dir = Some(d);
        }
    }
    // Enforce aligned departure/arrival direction when crossing layers at fanout gather points
    if let Some((first_pts, first_li)) = centerline_runs.first() {
        if start_f.layer_idx != *first_li {
            let Some(first_dir) = first_pts.windows(2).find_map(|w| dir_unit_f64(w[0], w[1]))
            else {
                return Ok(None);
            };
            if first_dir.0 * start_f.dir.0 + first_dir.1 * start_f.dir.1 < 0.99 {
                return Ok(None);
            }
        }
    }
    if let Some((last_pts, last_li)) = centerline_runs.last() {
        if end_f.layer_idx != *last_li {
            let Some(last_dir) = last_pts
                .windows(2)
                .rev()
                .find_map(|w| dir_unit_f64(w[0], w[1]))
            else {
                return Ok(None);
            };
            if last_dir.0 * end_f.dir.0 + last_dir.1 * end_f.dir.1 < 0.99 {
                return Ok(None);
            }
        }
    }
    let pos_on_left = start_f.pos_on_left;
    let s_nm = pair.width_nm + pair.gap_nm;
    let mut tracks: Vec<Track> = Vec::new();
    let mut vias: Vec<Via> = Vec::new();
    let mut non_fanout_tracks: Vec<Track> = Vec::new();
    let mut start_run_jog_p: Option<(Point, Point)> = None;
    let mut start_run_jog_n: Option<(Point, Point)> = None;

    for &(a, b, w) in &start_f.pos_segs {
        if !legal_edge_with_width(engine, pair.pos_net, start_f.layer_idx, a, b, w)? {
            return Ok(None);
        }
        tracks.push(Track {
            id: String::new(),
            net: pair.pos_net,
            start: a,
            end: b,
            width_nm: w,
            locked: false,
            layer: engine.layer_name(start_f.layer_idx).to_owned(),
        });
    }
    for &(a, b, w) in &start_f.neg_segs {
        if !legal_edge_with_width(engine, pair.neg_net, start_f.layer_idx, a, b, w)? {
            return Ok(None);
        }
        tracks.push(Track {
            id: String::new(),
            net: pair.neg_net,
            start: a,
            end: b,
            width_nm: w,
            locked: false,
            layer: engine.layer_name(start_f.layer_idx).to_owned(),
        });
    }

    let first_run_layer = centerline_runs
        .first()
        .map_or(start_f.layer_idx, |(_, li)| *li);
    if start_f.layer_idx != first_run_layer {
        if centerline_runs.first().is_none_or(|(pts, _)| pts.len() < 2) {
            return Ok(None);
        }
        let Some((vp, vn, p_pt, n_pt)) = coupled_via_pair_check(
            engine,
            pair,
            start_f.gather,
            start_f.dir,
            pos_on_left,
            start_f.layer_idx,
            first_run_layer,
        )?
        else {
            return Ok(None);
        };
        let (p0, n0) = offset_centerline_mitered(
            &[start_f.gather, centerline_runs[0].0[1]],
            s_nm,
            pos_on_left,
        );
        if p0.is_empty() || n0.is_empty() {
            return Ok(None);
        }
        if validate_via_jog_pair(
            engine,
            pair,
            start_f.layer_idx,
            start_f.gp,
            p_pt,
            start_f.gn,
            n_pt,
        )?
        .is_none()
        {
            return Ok(None);
        }
        if validate_via_jog_pair(engine, pair, first_run_layer, p_pt, p0[0], n_pt, n0[0])?.is_none()
        {
            return Ok(None);
        }
        if start_f.gp != p_pt {
            let t = Track {
                id: String::new(),
                net: pair.pos_net,
                start: start_f.gp,
                end: p_pt,
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(start_f.layer_idx).to_owned(),
            };
            non_fanout_tracks.push(t.clone());
            tracks.push(t);
        }
        if start_f.gn != n_pt {
            let t = Track {
                id: String::new(),
                net: pair.neg_net,
                start: start_f.gn,
                end: n_pt,
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(start_f.layer_idx).to_owned(),
            };
            non_fanout_tracks.push(t.clone());
            tracks.push(t);
        }
        if p_pt != p0[0] {
            let t = Track {
                id: String::new(),
                net: pair.pos_net,
                start: p_pt,
                end: p0[0],
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(first_run_layer).to_owned(),
            };
            non_fanout_tracks.push(t.clone());
            tracks.push(t);
            start_run_jog_p = Some((p_pt, p0[0]));
        }
        if n_pt != n0[0] {
            let t = Track {
                id: String::new(),
                net: pair.neg_net,
                start: n_pt,
                end: n0[0],
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(first_run_layer).to_owned(),
            };
            non_fanout_tracks.push(t.clone());
            tracks.push(t);
            start_run_jog_n = Some((n_pt, n0[0]));
        }
        vias.push(vp);
        vias.push(vn);
    }

    let mut last_run_p_segs: Vec<(Point, Point)> = Vec::new();
    let mut last_run_n_segs: Vec<(Point, Point)> = Vec::new();

    for run_idx in 0..centerline_runs.len() {
        let (ref run_pts, li) = centerline_runs[run_idx];
        let (p_poly, n_poly) = offset_centerline_mitered(run_pts, s_nm, pos_on_left);
        if p_poly.len() < 2 || n_poly.len() < 2 {
            return Ok(None);
        }
        if run_idx == 0 && start_f.layer_idx == li {
            if (start_f.gp != p_poly[0] || start_f.gn != n_poly[0])
                && geometry::segments_near(
                    start_f.gp,
                    p_poly[0],
                    start_f.gn,
                    n_poly[0],
                    2 * i128::from(pair.width_nm) + 2 * i128::from(pair.gap_min_nm),
                    false,
                )
            {
                return Ok(None);
            }
            if start_f.gp != p_poly[0] || start_f.gn != n_poly[0] {
                if let Some(imp) = &pair.impedance {
                    if engine.layer_name(li) != imp.signal_layer
                        || !coupled_corridor_over_reference_zones(
                            engine,
                            start_f.gp,
                            p_poly[0],
                            start_f.gn,
                            n_poly[0],
                            pair.width_nm,
                            imp,
                        )
                    {
                        return Ok(None);
                    }
                }
            }
            if start_f.gp != p_poly[0] {
                if !legal_edge_with_width(
                    engine,
                    pair.pos_net,
                    li,
                    start_f.gp,
                    p_poly[0],
                    pair.width_nm,
                )? {
                    return Ok(None);
                }
                let t = Track {
                    id: String::new(),
                    net: pair.pos_net,
                    start: start_f.gp,
                    end: p_poly[0],
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(li).to_owned(),
                };
                non_fanout_tracks.push(t.clone());
                tracks.push(t);
            }
            if start_f.gn != n_poly[0] {
                if !legal_edge_with_width(
                    engine,
                    pair.neg_net,
                    li,
                    start_f.gn,
                    n_poly[0],
                    pair.width_nm,
                )? {
                    return Ok(None);
                }
                let t = Track {
                    id: String::new(),
                    net: pair.neg_net,
                    start: start_f.gn,
                    end: n_poly[0],
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(li).to_owned(),
                };
                non_fanout_tracks.push(t.clone());
                tracks.push(t);
            }
        }
        for w in p_poly.windows(2) {
            if !legal_edge_with_width(engine, pair.pos_net, li, w[0], w[1], pair.width_nm)? {
                return Ok(None);
            }
            let t = Track {
                id: String::new(),
                net: pair.pos_net,
                start: w[0],
                end: w[1],
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(li).to_owned(),
            };
            non_fanout_tracks.push(t.clone());
            tracks.push(t);
        }
        for w in n_poly.windows(2) {
            if !legal_edge_with_width(engine, pair.neg_net, li, w[0], w[1], pair.width_nm)? {
                return Ok(None);
            }
            let t = Track {
                id: String::new(),
                net: pair.neg_net,
                start: w[0],
                end: w[1],
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(li).to_owned(),
            };
            non_fanout_tracks.push(t.clone());
            tracks.push(t);
        }
        if let Some(imp) = &pair.impedance {
            if engine.layer_name(li) != imp.signal_layer {
                return Ok(None);
            }
            if p_poly.len() == n_poly.len() {
                for (pw, nw) in p_poly.windows(2).zip(n_poly.windows(2)) {
                    if !coupled_corridor_over_reference_zones(
                        engine,
                        pw[0],
                        pw[1],
                        nw[0],
                        nw[1],
                        pair.width_nm,
                        imp,
                    ) {
                        return Ok(None);
                    }
                }
            } else {
                for w in p_poly.windows(2) {
                    if !segment_footprint_over_reference_zones(
                        engine,
                        w[0],
                        w[1],
                        pair.width_nm,
                        imp,
                    ) {
                        return Ok(None);
                    }
                }
                for w in n_poly.windows(2) {
                    if !segment_footprint_over_reference_zones(
                        engine,
                        w[0],
                        w[1],
                        pair.width_nm,
                        imp,
                    ) {
                        return Ok(None);
                    }
                }
            }
        }
        let last_p = *p_poly.last().unwrap();
        let last_n = *n_poly.last().unwrap();
        // Check coupled separation between ALL P and N segments on this layer run (including miter joins)
        let min_center_dist = 2 * i128::from(pair.width_nm) + 2 * i128::from(pair.gap_min_nm);
        let mut run_p_segs: Vec<(Point, Point)> = p_poly.windows(2).map(|w| (w[0], w[1])).collect();
        let mut run_n_segs: Vec<(Point, Point)> = n_poly.windows(2).map(|w| (w[0], w[1])).collect();
        if run_idx == 0 && start_f.layer_idx == li {
            if start_f.gp != p_poly[0] {
                run_p_segs.push((start_f.gp, p_poly[0]));
            }
            if start_f.gn != n_poly[0] {
                run_n_segs.push((start_f.gn, n_poly[0]));
            }
        }
        if run_idx + 1 == centerline_runs.len() && end_f.layer_idx == li {
            if last_p != end_f.gp {
                run_p_segs.push((last_p, end_f.gp));
            }
            if last_n != end_f.gn {
                run_n_segs.push((last_n, end_f.gn));
            }
        }
        for &(pa, pb) in &run_p_segs {
            for &(na, nb) in &run_n_segs {
                if geometry::segments_near(pa, pb, na, nb, min_center_dist, false) {
                    return Ok(None);
                }
            }
        }
        let min_clr2 = 2 * i128::from(pair.width_nm) + 2 * i128::from(pair.clearance_nm);
        if run_idx == 0 {
            if let Some((ja, jb)) = start_run_jog_p {
                for &(na, nb) in &run_n_segs {
                    if geometry::segments_near(ja, jb, na, nb, min_clr2, false) {
                        return Ok(None);
                    }
                }
            }
            if let Some((ja, jb)) = start_run_jog_n {
                for &(pa, pb) in &run_p_segs {
                    if geometry::segments_near(pa, pb, ja, jb, min_clr2, false) {
                        return Ok(None);
                    }
                }
            }
        }
        if run_idx + 1 == centerline_runs.len() {
            last_run_p_segs = run_p_segs;
            last_run_n_segs = run_n_segs;
        }
        if !coupled_gap_max_ok(pair, &p_poly, &n_poly) {
            return Ok(None);
        }
        if run_idx + 1 == centerline_runs.len() && end_f.layer_idx == li {
            if (last_p != end_f.gp || last_n != end_f.gn)
                && geometry::segments_near(
                    last_p,
                    end_f.gp,
                    last_n,
                    end_f.gn,
                    2 * i128::from(pair.width_nm) + 2 * i128::from(pair.gap_min_nm),
                    false,
                )
            {
                return Ok(None);
            }
            if last_p != end_f.gp || last_n != end_f.gn {
                if let Some(imp) = &pair.impedance {
                    if engine.layer_name(li) != imp.signal_layer
                        || !coupled_corridor_over_reference_zones(
                            engine,
                            last_p,
                            end_f.gp,
                            last_n,
                            end_f.gn,
                            pair.width_nm,
                            imp,
                        )
                    {
                        return Ok(None);
                    }
                }
            }
            if last_p != end_f.gp {
                if !legal_edge_with_width(
                    engine,
                    pair.pos_net,
                    li,
                    last_p,
                    end_f.gp,
                    pair.width_nm,
                )? {
                    return Ok(None);
                }
                let t = Track {
                    id: String::new(),
                    net: pair.pos_net,
                    start: last_p,
                    end: end_f.gp,
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(li).to_owned(),
                };
                non_fanout_tracks.push(t.clone());
                tracks.push(t);
            }
            if last_n != end_f.gn {
                if !legal_edge_with_width(
                    engine,
                    pair.neg_net,
                    li,
                    last_n,
                    end_f.gn,
                    pair.width_nm,
                )? {
                    return Ok(None);
                }
                let t = Track {
                    id: String::new(),
                    net: pair.neg_net,
                    start: last_n,
                    end: end_f.gn,
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(li).to_owned(),
                };
                non_fanout_tracks.push(t.clone());
                tracks.push(t);
            }
        }
        if run_idx + 1 < centerline_runs.len() {
            let next_li = centerline_runs[run_idx + 1].1;
            let via_center = *run_pts.last().unwrap();
            if run_pts.len() < 2 {
                return Ok(None);
            }
            let dir = dir_unit_f64(run_pts[run_pts.len() - 2], via_center).unwrap_or((1.0, 0.0));
            let Some((vp, vn, p_pt, n_pt)) =
                coupled_via_pair_check(engine, pair, via_center, dir, pos_on_left, li, next_li)?
            else {
                return Ok(None);
            };
            let p_end = *p_poly.last().unwrap();
            let n_end = *n_poly.last().unwrap();
            let (next_p, next_n) =
                offset_centerline_mitered(&centerline_runs[run_idx + 1].0, s_nm, pos_on_left);
            if next_p.is_empty() || next_n.is_empty() {
                return Ok(None);
            }
            if validate_via_jog_pair(engine, pair, li, p_end, p_pt, n_end, n_pt)?.is_none() {
                return Ok(None);
            }
            if validate_via_jog_pair(engine, pair, next_li, p_pt, next_p[0], n_pt, next_n[0])?
                .is_none()
            {
                return Ok(None);
            }
            if p_end != p_pt {
                let t = Track {
                    id: String::new(),
                    net: pair.pos_net,
                    start: p_end,
                    end: p_pt,
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(li).to_owned(),
                };
                non_fanout_tracks.push(t.clone());
                tracks.push(t);
            }
            if n_end != n_pt {
                let t = Track {
                    id: String::new(),
                    net: pair.neg_net,
                    start: n_end,
                    end: n_pt,
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(li).to_owned(),
                };
                non_fanout_tracks.push(t.clone());
                tracks.push(t);
            }
            if next_p[0] != p_pt {
                let t = Track {
                    id: String::new(),
                    net: pair.pos_net,
                    start: p_pt,
                    end: next_p[0],
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(next_li).to_owned(),
                };
                non_fanout_tracks.push(t.clone());
                tracks.push(t);
            }
            if next_n[0] != n_pt {
                let t = Track {
                    id: String::new(),
                    net: pair.neg_net,
                    start: n_pt,
                    end: next_n[0],
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(next_li).to_owned(),
                };
                non_fanout_tracks.push(t.clone());
                tracks.push(t);
            }
            vias.push(vp);
            vias.push(vn);
        }
    }

    let last_run_layer = centerline_runs
        .last()
        .map_or(end_f.layer_idx, |(_, li)| *li);
    if end_f.layer_idx != last_run_layer {
        let Some(last_run) = centerline_runs.last() else {
            return Ok(None);
        };
        let last_pts = &last_run.0;
        if last_pts.len() < 2 {
            return Ok(None);
        }
        let Some((vp, vn, p_pt, n_pt)) = coupled_via_pair_check(
            engine,
            pair,
            end_f.gather,
            end_f.dir,
            pos_on_left,
            last_run_layer,
            end_f.layer_idx,
        )?
        else {
            return Ok(None);
        };
        let (p_end, n_end) = offset_centerline_mitered(
            &[last_pts[last_pts.len() - 2], end_f.gather],
            s_nm,
            pos_on_left,
        );
        if p_end.is_empty() || n_end.is_empty() {
            return Ok(None);
        }
        let p_last = *p_end.last().unwrap();
        let n_last = *n_end.last().unwrap();
        if validate_via_jog_pair(engine, pair, last_run_layer, p_last, p_pt, n_last, n_pt)?
            .is_none()
        {
            return Ok(None);
        }
        if validate_via_jog_pair(
            engine,
            pair,
            end_f.layer_idx,
            p_pt,
            end_f.gp,
            n_pt,
            end_f.gn,
        )?
        .is_none()
        {
            return Ok(None);
        }
        let min_clr2 = 2 * i128::from(pair.width_nm) + 2 * i128::from(pair.clearance_nm);
        if p_last != p_pt {
            for &(na, nb) in &last_run_n_segs {
                if geometry::segments_near(p_last, p_pt, na, nb, min_clr2, false) {
                    return Ok(None);
                }
            }
            let t = Track {
                id: String::new(),
                net: pair.pos_net,
                start: p_last,
                end: p_pt,
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(last_run_layer).to_owned(),
            };
            non_fanout_tracks.push(t.clone());
            tracks.push(t);
        }
        if n_last != n_pt {
            for &(pa, pb) in &last_run_p_segs {
                if geometry::segments_near(pa, pb, n_last, n_pt, min_clr2, false) {
                    return Ok(None);
                }
            }
            let t = Track {
                id: String::new(),
                net: pair.neg_net,
                start: n_last,
                end: n_pt,
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(last_run_layer).to_owned(),
            };
            non_fanout_tracks.push(t.clone());
            tracks.push(t);
        }
        if p_pt != end_f.gp {
            let t = Track {
                id: String::new(),
                net: pair.pos_net,
                start: p_pt,
                end: end_f.gp,
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(end_f.layer_idx).to_owned(),
            };
            non_fanout_tracks.push(t.clone());
            tracks.push(t);
        }
        if n_pt != end_f.gn {
            let t = Track {
                id: String::new(),
                net: pair.neg_net,
                start: n_pt,
                end: end_f.gn,
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(end_f.layer_idx).to_owned(),
            };
            non_fanout_tracks.push(t.clone());
            tracks.push(t);
        }
        vias.push(vp);
        vias.push(vn);
    }

    for &(a, b, w) in &end_f.pos_segs {
        if !legal_edge_with_width(engine, pair.pos_net, end_f.layer_idx, a, b, w)? {
            return Ok(None);
        }
        tracks.push(Track {
            id: String::new(),
            net: pair.pos_net,
            start: a,
            end: b,
            width_nm: w,
            locked: false,
            layer: engine.layer_name(end_f.layer_idx).to_owned(),
        });
    }
    for &(a, b, w) in &end_f.neg_segs {
        if !legal_edge_with_width(engine, pair.neg_net, end_f.layer_idx, a, b, w)? {
            return Ok(None);
        }
        tracks.push(Track {
            id: String::new(),
            net: pair.neg_net,
            start: a,
            end: b,
            width_nm: w,
            locked: false,
            layer: engine.layer_name(end_f.layer_idx).to_owned(),
        });
    }

    // Check fanouts vs opposite-net trunks/via-jogs/opposite-end fanout for clearance & parallel gap_min_nm
    let start_lname = engine.layer_name(start_f.layer_idx);
    let end_lname = engine.layer_name(end_f.layer_idx);
    for &(fa, fb, fw) in &start_f.pos_segs {
        for nt in &non_fanout_tracks {
            if nt.net == pair.neg_net
                && nt.layer == start_lname
                && !fanout_trunk_pair_ok(pair, fa, fb, fw, nt.start, nt.end, nt.width_nm)
            {
                return Ok(None);
            }
        }
        if start_f.layer_idx == end_f.layer_idx {
            for &(na, nb, nw) in &end_f.neg_segs {
                if !fanout_trunk_pair_ok(pair, fa, fb, fw, na, nb, nw) {
                    return Ok(None);
                }
            }
        }
    }
    for &(fa, fb, fw) in &start_f.neg_segs {
        for pt in &non_fanout_tracks {
            if pt.net == pair.pos_net
                && pt.layer == start_lname
                && !fanout_trunk_pair_ok(pair, fa, fb, fw, pt.start, pt.end, pt.width_nm)
            {
                return Ok(None);
            }
        }
        if start_f.layer_idx == end_f.layer_idx {
            for &(pa, pb, pw) in &end_f.pos_segs {
                if !fanout_trunk_pair_ok(pair, fa, fb, fw, pa, pb, pw) {
                    return Ok(None);
                }
            }
        }
    }
    for &(fa, fb, fw) in &end_f.pos_segs {
        for nt in &non_fanout_tracks {
            if nt.net == pair.neg_net
                && nt.layer == end_lname
                && !fanout_trunk_pair_ok(pair, fa, fb, fw, nt.start, nt.end, nt.width_nm)
            {
                return Ok(None);
            }
        }
    }
    for &(fa, fb, fw) in &end_f.neg_segs {
        for pt in &non_fanout_tracks {
            if pt.net == pair.pos_net
                && pt.layer == end_lname
                && !fanout_trunk_pair_ok(pair, fa, fb, fw, pt.start, pt.end, pt.width_nm)
            {
                return Ok(None);
            }
        }
    }
    // Verify every generated track in `tracks` clears every opposite-net via in `vias`
    for tr in &tracks {
        let Some(li) = engine.layer_index(&tr.layer) else {
            continue;
        };
        let layer_slice = std::slice::from_ref(&tr.layer);
        for v in &vias {
            if v.net == tr.net {
                continue;
            }
            let s_idx = engine.layer_index(&v.start_layer).unwrap_or(0);
            let e_idx = engine.layer_index(&v.end_layer).unwrap_or(s_idx);
            if li < s_idx.min(e_idx) || li > s_idx.max(e_idx) {
                continue;
            }
            let copper_clr = engine.effective_rules.pair_clearance(
                tr.net,
                DruItemKind::Track,
                v.net,
                DruItemKind::Via,
                &tr.layer,
                None,
            );
            if geometry::point_near_segment(
                v.at,
                tr.start,
                tr.end,
                i128::from(tr.width_nm) + i128::from(v.diameter_nm) + 2 * i128::from(copper_clr),
                false,
            ) {
                return Ok(None);
            }
            let hole_clr = engine.effective_rules.pair_hole_clearance(
                tr.net,
                DruItemKind::Track,
                layer_slice,
                v.net,
                DruItemKind::Via,
                &engine.layer_names[s_idx.min(e_idx)..=s_idx.max(e_idx)],
            );
            if geometry::point_near_segment(
                v.at,
                tr.start,
                tr.end,
                i128::from(tr.width_nm) + i128::from(v.drill_nm) + 2 * i128::from(hole_clr),
                false,
            ) {
                return Ok(None);
            }
        }
    }
    // Reject non-jog same-net via-to-track copper overlaps (allows tracks incident to v.at
    // or incident to an immediate perpendicular jog hub j_pt within R_via + w/2 of v.at).
    for v in &vias {
        let s_idx = engine.layer_index(&v.start_layer).unwrap_or(0);
        let e_idx = engine.layer_index(&v.end_layer).unwrap_or(s_idx);
        let mut jog_hubs: Vec<(usize, Point)> = Vec::new();
        for tj in &tracks {
            if tj.net != v.net || (tj.start != v.at && tj.end != v.at) {
                continue;
            }
            let Some(li) = engine.layer_index(&tj.layer) else {
                continue;
            };
            if li < s_idx.min(e_idx) || li > s_idx.max(e_idx) {
                continue;
            }
            let other = if tj.start == v.at { tj.end } else { tj.start };
            if dist_f64(other, v.at) * 2.0 < (v.diameter_nm + tj.width_nm) as f64 {
                jog_hubs.push((li, other));
            }
        }
        for tr in &tracks {
            if tr.net != v.net || tr.start == v.at || tr.end == v.at {
                continue;
            }
            let Some(li) = engine.layer_index(&tr.layer) else {
                continue;
            };
            if li < s_idx.min(e_idx) || li > s_idx.max(e_idx) {
                continue;
            }
            if jog_hubs
                .iter()
                .any(|&(jli, jpt)| jli == li && (tr.start == jpt || tr.end == jpt))
            {
                continue;
            }
            if geometry::point_near_segment(
                v.at,
                tr.start,
                tr.end,
                i128::from(v.diameter_nm) + i128::from(tr.width_nm),
                false,
            ) {
                return Ok(None);
            }
        }
    }
    // Reject non-incident same-net same-layer track-to-track copper overlaps (< width_nm stub bypasses)
    let is_short_via_jog = |tr: &Track| -> bool {
        let Some(li) = engine.layer_index(&tr.layer) else {
            return false;
        };
        vias.iter().any(|v| {
            if v.net != tr.net || (tr.start != v.at && tr.end != v.at) {
                return false;
            }
            let s_idx = engine.layer_index(&v.start_layer).unwrap_or(0);
            let e_idx = engine.layer_index(&v.end_layer).unwrap_or(s_idx);
            li >= s_idx.min(e_idx)
                && li <= s_idx.max(e_idx)
                && dist_f64(tr.start, tr.end) * 2.0 < (v.diameter_nm + tr.width_nm) as f64
        })
    };
    for i in 0..tracks.len() {
        let ta = &tracks[i];
        if ta.start == ta.end {
            continue;
        }
        if is_short_via_jog(ta) {
            continue;
        }
        for tb in &tracks[(i + 1)..] {
            if tb.net != ta.net || tb.layer != ta.layer || tb.start == tb.end {
                continue;
            }
            if ta.start == tb.start || ta.start == tb.end || ta.end == tb.start || ta.end == tb.end
            {
                continue;
            }
            if is_short_via_jog(tb) {
                continue;
            }
            let gap2 = i128::from(ta.width_nm) + i128::from(tb.width_nm);
            if geometry::segments_near(ta.start, ta.end, tb.start, tb.end, gap2, false) {
                return Ok(None);
            }
        }
    }
    let pos_pads: Vec<_> = engine
        .input
        .pads
        .iter()
        .filter(|p| p.net == pair.pos_net)
        .collect();
    let neg_pads: Vec<_> = engine
        .input
        .pads
        .iter()
        .filter(|p| p.net == pair.neg_net)
        .collect();
    let mut p_tracks: Vec<Track> = tracks
        .iter()
        .filter(|t| t.net == pair.pos_net)
        .cloned()
        .collect();
    let mut n_tracks: Vec<Track> = tracks
        .iter()
        .filter(|t| t.net == pair.neg_net)
        .cloned()
        .collect();
    trim_dangling_pad_tails(&mut p_tracks, &pos_pads, engine);
    trim_dangling_pad_tails(&mut n_tracks, &neg_pads, engine);
    if !net_clear_non_root_pads(engine, pair.pos_net, &p_tracks, &vias)
        || !net_clear_non_root_pads(engine, pair.neg_net, &n_tracks, &vias)
    {
        return Ok(None);
    }
    if !engine.input.mask_apertures.is_empty()
        && engine.input.rules.solder_mask_to_copper_clearance_nm == 0
    {
        let mut hits = Vec::new();
        let mut pos_hits = BTreeSet::new();
        for t in p_tracks
            .iter()
            .chain(engine.copper.tracks().filter(|t| t.net == pair.pos_net))
        {
            if let Some(li) = engine.layer_index(&t.layer) {
                engine.mask_apertures_hit(li, t.start, t.end, t.width_nm, &mut hits)?;
                pos_hits.extend(hits.iter().copied());
            }
        }
        for v in vias
            .iter()
            .chain(engine.copper.vias().filter(|v| v.net == pair.pos_net))
        {
            if v.net != pair.pos_net {
                continue;
            }
            let s_idx = engine.layer_index(&v.start_layer).unwrap_or(0);
            let e_idx = engine.layer_index(&v.end_layer).unwrap_or(s_idx);
            for li in s_idx.min(e_idx)..=s_idx.max(e_idx) {
                engine.mask_apertures_hit(li, v.at, v.at, v.diameter_nm, &mut hits)?;
                pos_hits.extend(hits.iter().copied());
            }
        }
        if !pos_hits.is_empty() {
            for t in n_tracks
                .iter()
                .chain(engine.copper.tracks().filter(|t| t.net == pair.neg_net))
            {
                if let Some(li) = engine.layer_index(&t.layer) {
                    engine.mask_apertures_hit(li, t.start, t.end, t.width_nm, &mut hits)?;
                    if hits.iter().any(|m| pos_hits.contains(m)) {
                        return Ok(None);
                    }
                }
            }
            for v in vias
                .iter()
                .chain(engine.copper.vias().filter(|v| v.net == pair.neg_net))
            {
                if v.net != pair.neg_net {
                    continue;
                }
                let s_idx = engine.layer_index(&v.start_layer).unwrap_or(0);
                let e_idx = engine.layer_index(&v.end_layer).unwrap_or(s_idx);
                for li in s_idx.min(e_idx)..=s_idx.max(e_idx) {
                    engine.mask_apertures_hit(li, v.at, v.at, v.diameter_nm, &mut hits)?;
                    if hits.iter().any(|m| pos_hits.contains(m)) {
                        return Ok(None);
                    }
                }
            }
        }
    }
    if pair.impedance.is_none() {
        coalesce_collinear_net_tracks(&mut p_tracks, &pos_pads, &vias);
        coalesce_collinear_net_tracks(&mut n_tracks, &neg_pads, &vias);
    }
    tracks = p_tracks;
    tracks.extend(n_tracks);
    let mut all_pair_tracks: Vec<Track> = engine
        .copper
        .tracks()
        .filter(|t| t.net == pair.pos_net || t.net == pair.neg_net)
        .cloned()
        .collect();
    all_pair_tracks.extend(tracks.iter().cloned());
    if measure_tracks_uncoupled_nm(pair, &all_pair_tracks) > pair.uncoupled_max_nm {
        return Ok(None);
    }
    Ok(Some(CoupledSolution { tracks, vias }))
}

fn coalesce_collinear_net_tracks(
    tracks: &mut Vec<Track>,
    pads: &[&crate::model::Pad],
    vias: &[Via],
) {
    loop {
        let mut merged = false;
        'outer: for i in 0..tracks.len() {
            for j in (i + 1)..tracks.len() {
                if tracks[i].layer != tracks[j].layer
                    || tracks[i].width_nm != tracks[j].width_nm
                    || tracks[i].locked != tracks[j].locked
                {
                    continue;
                }
                let a = &tracks[i];
                let b = &tracks[j];
                let pair_pts = if a.end == b.start {
                    Some((a.start, a.end, b.end))
                } else if a.end == b.end {
                    Some((a.start, a.end, b.start))
                } else if a.start == b.start {
                    Some((a.end, a.start, b.end))
                } else if a.start == b.end {
                    Some((a.end, a.start, b.start))
                } else {
                    None
                };
                let Some((p0, shared, p2)) = pair_pts else {
                    continue;
                };
                if p0 == p2
                    || pads.iter().any(|p| p.at == shared)
                    || vias.iter().any(|v| v.net == a.net && v.at == shared)
                {
                    continue;
                }
                let touches = tracks
                    .iter()
                    .filter(|t| t.layer == a.layer && (t.start == shared || t.end == shared))
                    .count();
                if touches != 2 {
                    continue;
                }
                let dx1 = i128::from(shared[0] - p0[0]);
                let dy1 = i128::from(shared[1] - p0[1]);
                let dx2 = i128::from(p2[0] - shared[0]);
                let dy2 = i128::from(p2[1] - shared[1]);
                if dx1 * dy2 == dy1 * dx2 && dx1 * dx2 + dy1 * dy2 > 0 {
                    tracks[i].start = p0;
                    tracks[i].end = p2;
                    tracks.remove(j);
                    merged = true;
                    break 'outer;
                }
            }
        }
        if !merged {
            break;
        }
    }
}
#[allow(clippy::too_many_arguments)]
fn astar_coupled_centerline(
    engine: &Engine,
    pair: &ResolvedDiffPair,
    sf: &EndFanout,
    ef: &EndFanout,
    allowed_layer_idxs: &[usize],
    step_nm: i64,
    step_penalty: &dyn Fn(u8, Point) -> u64,
    via_penalty: &dyn Fn(Point, u8, u8) -> u64,
    budget: &mut TrialBudget<'_>,
    mut expanded_out: Option<&mut u64>,
    mut frontier: Option<&mut BlockedFrontier>,
) -> Result<Option<CoupledSolution>, Failure> {
    if allowed_layer_idxs.is_empty() || sf.pos_on_left != ef.pos_on_left {
        return Ok(None);
    }
    let s_nm = pair.width_nm + pair.gap_nm;
    let half = 0.5 * (s_nm as f64);
    let sign = if sf.pos_on_left { 1.0 } else { -1.0 };

    const DIRS: [(i32, i32); 8] = [
        (1, 0),
        (1, 1),
        (0, 1),
        (-1, 1),
        (-1, 0),
        (-1, -1),
        (0, -1),
        (1, -1),
    ];
    let dir_vec = |d: usize| -> (f64, f64) {
        let (dx, dy) = DIRS[d];
        let l = (dx as f64).hypot(dy as f64);
        (dx as f64 / l, dy as f64 / l)
    };

    let via_base_cost = 600_000u64;
    let via_diam_est = engine
        .effective_rules
        .via_dims_for(pair.pos_net, false, &engine.layer_names)
        .map_or(600_000, |(d, _)| d)
        .max(
            engine
                .effective_rules
                .via_dims_for(pair.neg_net, false, &engine.layer_names)
                .map_or(600_000, |(d, _)| d),
        );
    let min_via_lead_nm = ((via_diam_est as f64) * 0.5
        + (pair.width_nm as f64) * 0.5
        + 0.5 * (s_nm as f64) * (std::f64::consts::SQRT_2 - 1.0)
        + 50_000.0)
        .max(600_000.0);

    let sf_in_allowed = allowed_layer_idxs.contains(&sf.layer_idx);
    let mut valid_start_layers: Vec<(usize, u64)> = Vec::new();
    if sf_in_allowed {
        valid_start_layers.push((sf.layer_idx, 0));
    }
    for &to_li in allowed_layer_idxs {
        if to_li == sf.layer_idx {
            continue;
        }
        if coupled_via_pair_check(
            engine,
            pair,
            sf.gather,
            sf.dir,
            sf.pos_on_left,
            sf.layer_idx,
            to_li,
        )?
        .is_some()
        {
            valid_start_layers.push((
                to_li,
                via_base_cost.saturating_add(via_penalty(
                    sf.gather,
                    sf.layer_idx as u8,
                    to_li as u8,
                )),
            ));
        }
    }
    if valid_start_layers.is_empty() {
        return Ok(None);
    }

    let ef_in_allowed = allowed_layer_idxs.contains(&ef.layer_idx);
    let mut valid_end_layers: Vec<usize> = Vec::new();
    if ef_in_allowed {
        valid_end_layers.push(ef.layer_idx);
    }
    for &to_li in allowed_layer_idxs {
        if to_li == ef.layer_idx {
            continue;
        }
        if coupled_via_pair_check(
            engine,
            pair,
            ef.gather,
            ef.dir,
            ef.pos_on_left,
            to_li,
            ef.layer_idx,
        )?
        .is_some()
        {
            valid_end_layers.push(to_li);
        }
    }
    if valid_end_layers.is_empty() {
        return Ok(None);
    }

    let origin = sf.gather;
    let grid_pt = |gx: i32, gy: i32| -> Point {
        [
            origin[0] + i64::from(gx) * step_nm,
            origin[1] + i64::from(gy) * step_nm,
        ]
    };
    let heuristic = |pt: Point, li: usize| -> u64 {
        let d = dist_f64(pt, ef.gather).round() as u64;
        let at_goal_layer = if ef_in_allowed {
            li == ef.layer_idx
        } else {
            valid_end_layers.contains(&li)
        };
        let v = if at_goal_layer { 0 } else { 400_000 };
        d.saturating_mul(5).saturating_div(4).saturating_add(v)
    };

    #[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
    struct Key {
        gx: i32,
        gy: i32,
        li: u8,
        dir: u8,
    }
    #[derive(Clone, Copy, PartialEq, Eq)]
    struct Node {
        f: u64,
        g: u64,
        key: Key,
    }
    impl Ord for Node {
        fn cmp(&self, other: &Self) -> std::cmp::Ordering {
            other
                .f
                .cmp(&self.f)
                .then_with(|| other.g.cmp(&self.g))
                .then_with(|| self.key.gx.cmp(&other.key.gx))
                .then_with(|| self.key.gy.cmp(&other.key.gy))
                .then_with(|| self.key.li.cmp(&other.key.li))
                .then_with(|| self.key.dir.cmp(&other.key.dir))
        }
    }
    impl PartialOrd for Node {
        fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
            Some(self.cmp(other))
        }
    }

    let mut best_g: BTreeMap<Key, u64> = BTreeMap::new();
    let mut parent: BTreeMap<Key, Key> = BTreeMap::new();
    let mut heap = BinaryHeap::new();
    for &(s_li, init_g) in &valid_start_layers {
        let sk = Key {
            gx: 0,
            gy: 0,
            li: s_li as u8,
            dir: 8,
        };
        best_g.insert(sk, init_g);
        heap.push(Node {
            f: init_g.saturating_add(heuristic(origin, s_li)),
            g: init_g,
            key: sk,
        });
    }

    let snap_radius = step_nm * 6;
    let diag_len = ((step_nm as f64) * std::f64::consts::SQRT_2).round() as u64;
    let mut expansions = 0usize;
    let max_expansions = (engine.input.max_expansions as usize).min(35_000);
    let mut snap_attempts = 0usize;

    let reconstruct_runs = |tail_key: Key,
                            tail_pts: &[Point],
                            tail_target_li: Option<usize>,
                            parent: &BTreeMap<Key, Key>|
     -> Vec<(Vec<Point>, usize)> {
        let mut chain = Vec::new();
        let mut cur = tail_key;
        chain.push(cur);
        while let Some(&prev) = parent.get(&cur) {
            chain.push(prev);
            cur = prev;
        }
        chain.reverse();
        let mut runs: Vec<(Vec<Point>, usize)> = Vec::new();
        for k in chain {
            let pt = grid_pt(k.gx, k.gy);
            let li = k.li as usize;
            match runs.last_mut() {
                Some((pts, prev_li)) if *prev_li == li => {
                    if pts.last().copied() != Some(pt) {
                        pts.push(pt);
                    }
                }
                _ => runs.push((vec![pt], li)),
            }
        }
        if let Some(target_li) = tail_target_li {
            if runs.last().is_some_and(|(_, l)| *l != target_li) {
                let pivot = *runs.last().unwrap().0.last().unwrap();
                let mut tail_vec = vec![pivot];
                for &tp in tail_pts {
                    if tail_vec.last().copied() != Some(tp) {
                        tail_vec.push(tp);
                    }
                }
                runs.push((tail_vec, target_li));
            } else if let Some((last_pts, _)) = runs.last_mut() {
                for &tp in tail_pts {
                    if last_pts.last().copied() != Some(tp) {
                        last_pts.push(tp);
                    }
                }
            }
        } else if let Some((last_pts, _)) = runs.last_mut() {
            for &tp in tail_pts {
                if last_pts.last().copied() != Some(tp) {
                    last_pts.push(tp);
                }
            }
        }
        for (pts, _) in &mut runs {
            *pts = simplify_collinear_points(std::mem::take(pts));
        }
        let single_run = runs.len() == 1;
        runs.retain(|(pts, _)| single_run || pts.len() >= 2);
        runs
    };

    while let Some(cur) = heap.pop() {
        let k = cur.key;
        if cur.g > best_g.get(&k).copied().unwrap_or(u64::MAX) {
            continue;
        }
        if let Some(ex) = expanded_out.as_deref_mut() {
            budget.check_before_expand(engine, *ex, frontier.as_deref())?;
            *ex = ex.checked_add(1).expect("bounded expansion count");
        }
        expansions += 1;
        if expansions > max_expansions {
            break;
        }
        engine.work.charge(1)?;
        let pt = grid_pt(k.gx, k.gy);
        let li = k.li as usize;

        if (pt[0] - ef.gather[0]).abs() <= snap_radius
            && (pt[1] - ef.gather[1]).abs() <= snap_radius
        {
            let can_finish_here = if ef_in_allowed {
                li == ef.layer_idx || valid_end_layers.contains(&li)
            } else {
                valid_end_layers.contains(&li)
            };
            if can_finish_here {
                let mut tails = Vec::new();
                if ef.layer_idx != li {
                    let d1 = (min_via_lead_nm.round() as i64).max(800_000);
                    let d2 = (min_via_lead_nm.round() as i64).max(600_000);
                    let mut leads = vec![d1];
                    if d2 != d1 {
                        leads.push(d2);
                    }
                    for d_lead in leads {
                        let p_out = [
                            ef.gather[0] - ((d_lead as f64) * ef.dir.0).round() as i64,
                            ef.gather[1] - ((d_lead as f64) * ef.dir.1).round() as i64,
                        ];
                        let fwd = ((p_out[0] - pt[0]) as f64) * ef.dir.0
                            + ((p_out[1] - pt[1]) as f64) * ef.dir.1;
                        if fwd < -1.0 {
                            continue;
                        }
                        for mut t in octilinear_elbow_paths(pt, p_out) {
                            t.push(ef.gather);
                            tails.push(t);
                        }
                    }
                } else {
                    tails = octilinear_elbow_paths(pt, ef.gather);
                    if dist_f64(pt, ef.gather) >= 700_000.0 {
                        let p_out = [
                            ef.gather[0] - (600_000.0 * ef.dir.0).round() as i64,
                            ef.gather[1] - (600_000.0 * ef.dir.1).round() as i64,
                        ];
                        let fwd = ((p_out[0] - pt[0]) as f64) * ef.dir.0
                            + ((p_out[1] - pt[1]) as f64) * ef.dir.1;
                        if fwd >= -1.0 {
                            for mut t in octilinear_elbow_paths(pt, p_out) {
                                t.push(ef.gather);
                                tails.push(t);
                            }
                        }
                    }
                }
                let tail_target_options: &[Option<usize>] = if ef_in_allowed && li != ef.layer_idx {
                    &[Some(ef.layer_idx), None]
                } else {
                    &[None]
                };
                for tail in &tails {
                    for &tail_target_li in tail_target_options {
                        let runs = reconstruct_runs(k, tail, tail_target_li, &parent);
                        if let Some(sol) =
                            validate_and_build_coupled_solution(engine, pair, sf, ef, &runs)?
                        {
                            return Ok(Some(sol));
                        }
                    }
                }
                snap_attempts += 1;
                if snap_attempts >= 128 {
                    break;
                }
            }
        }

        let sf_aligned_dir = if sf.layer_idx != li {
            let req_d = DIRS
                .iter()
                .enumerate()
                .max_by(|(_, a), (_, b)| {
                    let la = (a.0 as f64).hypot(a.1 as f64);
                    let lb = (b.0 as f64).hypot(b.1 as f64);
                    let da = (a.0 as f64 / la) * sf.dir.0 + (a.1 as f64 / la) * sf.dir.1;
                    let db = (b.0 as f64 / lb) * sf.dir.0 + (b.1 as f64 / lb) * sf.dir.1;
                    da.partial_cmp(&db).unwrap_or(std::cmp::Ordering::Equal)
                })
                .map(|(idx, _)| idx);
            req_d.filter(|&rd| {
                k.dir == 8
                    || (dist_f64(pt, origin) + 1_000.0 < min_via_lead_nm && k.dir == rd as u8)
            })
        } else {
            None
        };

        // 8 octilinear steps
        for (nd, &(dx, dy)) in DIRS.iter().enumerate() {
            if let Some(req_d) = sf_aligned_dir {
                if nd != req_d {
                    continue;
                }
            }
            if k.dir < 8 {
                let d_diff = ((nd as i32 - i32::from(k.dir)).rem_euclid(8))
                    .min(8 - (nd as i32 - i32::from(k.dir)).rem_euclid(8));
                if d_diff > 1 {
                    continue;
                }
            }
            let nx = k.gx + dx;
            let ny = k.gy + dy;
            let npt = grid_pt(nx, ny);
            let (ux, uy) = dir_vec(nd);
            let left = (-uy, ux);
            let pa = [
                pt[0] + round_outward(sign * half * left.0),
                pt[1] + round_outward(sign * half * left.1),
            ];
            let pb = [
                npt[0] + round_outward(sign * half * left.0),
                npt[1] + round_outward(sign * half * left.1),
            ];
            let na = [
                pt[0] - round_outward(sign * half * left.0),
                pt[1] - round_outward(sign * half * left.1),
            ];
            let nb = [
                npt[0] - round_outward(sign * half * left.0),
                npt[1] - round_outward(sign * half * left.1),
            ];
            if k.dir == 8
                && sf.layer_idx == li
                && (sf.gp != pa || sf.gn != na)
                && geometry::segments_near(
                    sf.gp,
                    pa,
                    sf.gn,
                    na,
                    2 * i128::from(pair.width_nm) + 2 * i128::from(pair.gap_min_nm),
                    false,
                )
            {
                continue;
            }
            if !legal_pair_coupled_step(engine, pair, li, pa, pb, na, nb, pair.width_nm)? {
                if let Some(f) = frontier.as_deref_mut() {
                    f.push_edge(li as u8, pa, pb);
                    f.push_edge(li as u8, na, nb);
                }
                continue;
            }
            let step_len = if dx != 0 && dy != 0 {
                diag_len
            } else {
                step_nm as u64
            };
            let turn_pen = if k.dir < 8 && nd as u8 != k.dir {
                let just_via = parent.get(&k).is_some_and(|pk| pk.li != k.li);
                if just_via {
                    300_000
                } else {
                    25_000
                }
            } else {
                0
            };
            let ext_pen = step_penalty(k.li, npt);
            let ng = cur
                .g
                .saturating_add(step_len)
                .saturating_add(turn_pen)
                .saturating_add(ext_pen);
            let nk = Key {
                gx: nx,
                gy: ny,
                li: k.li,
                dir: nd as u8,
            };
            if ng < best_g.get(&nk).copied().unwrap_or(u64::MAX) {
                best_g.insert(nk, ng);
                parent.insert(nk, k);
                heap.push(Node {
                    f: ng.saturating_add(heuristic(npt, li)),
                    g: ng,
                    key: nk,
                });
            }
        }

        // Layer transitions via coupled via pair
        if k.dir < 8
            && allowed_layer_idxs.len() > 1
            && !(sf.layer_idx != li && dist_f64(pt, origin) + 1_000.0 < min_via_lead_nm)
        {
            let cur_dir = dir_vec(k.dir as usize);
            // Discourage placing a via within 1 short step of a miter turn so KiCad fromTo
            // does not collapse the corner inside the via copper pad.
            let near_turn_pen = if step_nm < 800_000
                && parent
                    .get(&k)
                    .is_some_and(|pk| pk.li == k.li && pk.dir < 8 && pk.dir != k.dir)
            {
                300_000u64
            } else {
                0u64
            };
            for &next_li in allowed_layer_idxs {
                if next_li == li {
                    continue;
                }
                if coupled_via_pair_check(engine, pair, pt, cur_dir, sf.pos_on_left, li, next_li)?
                    .is_none()
                {
                    if let Some(f) = frontier.as_deref_mut() {
                        let lo_l = li.min(next_li) as u8;
                        let hi_l = li.max(next_li) as u8;
                        let left_v = (-cur_dir.1, cur_dir.0);
                        let half_v = 0.5 * (s_nm.max(via_diam_est) as f64);
                        let vp_est = [
                            pt[0] + (sign * half_v * left_v.0).round() as i64,
                            pt[1] + (sign * half_v * left_v.1).round() as i64,
                        ];
                        let vn_est = [
                            pt[0] - (sign * half_v * left_v.0).round() as i64,
                            pt[1] - (sign * half_v * left_v.1).round() as i64,
                        ];
                        f.push_via_site(lo_l, hi_l, pt);
                        f.push_via_site(lo_l, hi_l, vp_est);
                        f.push_via_site(lo_l, hi_l, vn_est);
                    }
                    continue;
                }
                let ext_via = via_penalty(pt, k.li, next_li as u8);
                let ng = cur
                    .g
                    .saturating_add(via_base_cost)
                    .saturating_add(near_turn_pen)
                    .saturating_add(ext_via);
                let nk = Key {
                    gx: k.gx,
                    gy: k.gy,
                    li: next_li as u8,
                    dir: k.dir,
                };
                if ng < best_g.get(&nk).copied().unwrap_or(u64::MAX) {
                    best_g.insert(nk, ng);
                    parent.insert(nk, k);
                    heap.push(Node {
                        f: ng.saturating_add(heuristic(pt, next_li)),
                        g: ng,
                        key: nk,
                    });
                }
            }
        }
    }
    Ok(None)
}

fn path_hits_step_penalty(
    layer: u8,
    path: &[Point],
    step_penalty: &dyn Fn(u8, Point) -> u64,
    grid_nm: i64,
) -> bool {
    let step = grid_nm.max(100_000);
    for w in path.windows(2) {
        let a = w[0];
        let b = w[1];
        let dx = b[0] - a[0];
        let dy = b[1] - a[1];
        let dist = dx.abs().max(dy.abs());
        let samples = (dist / step).clamp(1, 32);
        for s in 0..=samples {
            let pt = [a[0] + dx * s / samples, a[1] + dy * s / samples];
            if step_penalty(layer, pt) > 0 {
                return true;
            }
        }
    }
    false
}

#[allow(clippy::too_many_arguments)]
fn route_single_diff_pair_inner(
    engine: &mut Engine,
    pair: &ResolvedDiffPair,
    plan: &DiffPairPlan,
    step_penalty: &dyn Fn(u8, Point) -> u64,
    via_penalty: &dyn Fn(Point, u8, u8) -> u64,
    budget: &mut TrialBudget<'_>,
    mut expanded: Option<&mut u64>,
    mut frontier: Option<&mut BlockedFrontier>,
    use_commit_tx: bool,
) -> Result<(), UnitRouteError> {
    // Check if already connected & protected / pre-routed
    let p_conn = measure_net_fromto(engine, pair.pos_net, false)
        .map_err(UnitRouteError::Fatal)?
        .is_some()
        && engine
            .components(pair.pos_net)
            .map_err(UnitRouteError::Fatal)?
            .len()
            <= 1;
    let n_conn = measure_net_fromto(engine, pair.neg_net, false)
        .map_err(UnitRouteError::Fatal)?
        .is_some()
        && engine
            .components(pair.neg_net)
            .map_err(UnitRouteError::Fatal)?
            .len()
            <= 1;
    if p_conn && n_conn {
        return Ok(());
    }
    if plan.protected_nets.contains(&pair.pos_net) || plan.protected_nets.contains(&pair.neg_net) {
        return Err(UnitRouteError::Fatal(Failure::new(
            Status::Unroutable,
            format!(
                "differential pair ({:?}, {:?}) is incomplete under --protect",
                pair.pos_name, pair.neg_name
            ),
        )));
    }
    let has_input_copper = engine
        .copper
        .input()
        .iter()
        .any(|t| t.net == pair.pos_net || t.net == pair.neg_net)
        || engine
            .copper
            .input_vias()
            .iter()
            .any(|v| v.net == pair.pos_net || v.net == pair.neg_net);
    if has_input_copper {
        return Err(UnitRouteError::Fatal(Failure::new(
            Status::Unroutable,
            format!(
                "differential pair ({:?}, {:?}) has pre-existing input copper on an incomplete pair",
                pair.pos_name, pair.neg_name
            ),
        )));
    }

    let allowed_layer_idxs: Vec<usize> = plan
        .pair_layers
        .iter()
        .filter_map(|ln| engine.layer_index(ln))
        .collect();
    if allowed_layer_idxs.is_empty() {
        return Err(UnitRouteError::Fatal(Failure::new(
            Status::Invalid,
            "no allowed copper layers available for differential pair routing",
        )));
    }

    let Some([(p_start, n_start), (p_end, n_end)]) =
        find_pad_pairs(&engine.input, pair.pos_net, pair.neg_net)
    else {
        return Err(UnitRouteError::Fatal(Failure::new(
            Status::Unroutable,
            format!("unable to pair terminal pads for {}", pair.stem),
        )));
    };
    let (p_start_at, n_start_at, p_end_at, n_end_at) = (p_start.at, n_start.at, p_end.at, n_end.at);

    let start_mid = [
        (p_start.at[0] + n_start.at[0]) / 2,
        (p_start.at[1] + n_start.at[1]) / 2,
    ];
    let end_mid = [
        (p_end.at[0] + n_end.at[0]) / 2,
        (p_end.at[1] + n_end.at[1]) / 2,
    ];
    let start_fanouts = enumerate_end_fanouts(
        engine,
        pair,
        p_start,
        n_start,
        end_mid,
        false,
        &allowed_layer_idxs,
    )
    .map_err(UnitRouteError::Fatal)?;
    let end_fanouts = enumerate_end_fanouts(
        engine,
        pair,
        p_end,
        n_end,
        start_mid,
        true,
        &allowed_layer_idxs,
    )
    .map_err(UnitRouteError::Fatal)?;

    let mut pairs_ranked: Vec<(i64, usize, usize)> = Vec::new();
    for (si, sf) in start_fanouts.iter().enumerate() {
        for (ei, ef) in end_fanouts.iter().enumerate() {
            if sf.pos_on_left != ef.pos_on_left {
                continue;
            }
            if sf.est_uncoupled_nm + ef.est_uncoupled_nm > pair.uncoupled_max_nm {
                continue;
            }
            let trunk_dir = dir_unit_f64(sf.gather, ef.gather).unwrap_or(sf.dir);
            let s_align = sf.dir.0 * trunk_dir.0 + sf.dir.1 * trunk_dir.1;
            let e_align = ef.dir.0 * trunk_dir.0 + ef.dir.1 * trunk_dir.1;
            let align_pen =
                if s_align < 0.99 { 400_000 } else { 0 } + if e_align < 0.99 { 400_000 } else { 0 };
            let same_layer_bonus = if sf.layer_idx == ef.layer_idx {
                0
            } else {
                500_000
            };
            let short_pen = if sf.short_seg_count > 0 || ef.short_seg_count > 0 {
                10_000_000
            } else {
                0
            };
            let score = sf.uncoupled_nm
                + ef.uncoupled_nm
                + 2 * (sf.skew_nm + ef.skew_nm)
                + same_layer_bonus
                + align_pen
                + short_pen;
            pairs_ranked.push((score, si, ei));
        }
    }
    pairs_ranked.sort_by_key(|t| t.0);
    pairs_ranked.truncate(64);

    // 1. Fast direct octilinear single-layer centerline check across allowed layers
    for &(_, si, ei) in &pairs_ranked {
        let sf = &start_fanouts[si];
        let ef = &end_fanouts[ei];
        if sf.layer_idx == ef.layer_idx && allowed_layer_idxs.contains(&sf.layer_idx) {
            let mut candidates = octilinear_elbow_paths(sf.gather, ef.gather);
            if dist_f64(sf.gather, ef.gather) >= 2_500_000.0 {
                let p_in = [
                    sf.gather[0] + (600_000.0 * sf.dir.0).round() as i64,
                    sf.gather[1] + (600_000.0 * sf.dir.1).round() as i64,
                ];
                let p_out = [
                    ef.gather[0] - (600_000.0 * ef.dir.0).round() as i64,
                    ef.gather[1] - (600_000.0 * ef.dir.1).round() as i64,
                ];
                // Try aligned launch + approach (plus slight overshoot bend for orthogonal 90-deg pads)
                for mid_path in octilinear_elbow_paths(p_in, p_out) {
                    let mut p = Vec::with_capacity(mid_path.len() + 2);
                    p.push(sf.gather);
                    p.extend(mid_path);
                    p.push(ef.gather);
                    candidates.push(p);
                }
                let p_over = [
                    p_out[0] - (2_000_000.0 * ef.dir.0).round() as i64,
                    p_out[1] - (2_000_000.0 * ef.dir.1).round() as i64,
                ];
                for mid_path in octilinear_elbow_paths(p_in, p_over) {
                    let mut p = Vec::with_capacity(mid_path.len() + 3);
                    p.push(sf.gather);
                    p.extend(mid_path);
                    p.push(p_out);
                    p.push(ef.gather);
                    candidates.push(p);
                }
            }
            for path in candidates {
                if path_hits_step_penalty(
                    sf.layer_idx as u8,
                    &path,
                    step_penalty,
                    engine.input.rules.grid_nm,
                ) {
                    continue;
                }
                let runs = vec![(path, sf.layer_idx)];
                if let Some(sol) = validate_and_build_coupled_solution(engine, pair, sf, ef, &runs)
                    .map_err(UnitRouteError::Fatal)?
                {
                    if use_commit_tx {
                        return commit_coupled_solution(engine, pair, sol)
                            .map_err(UnitRouteError::Fatal);
                    }
                    if commit_coupled_solution_direct(engine, pair, sol)
                        .map_err(UnitRouteError::Fatal)?
                    {
                        return Ok(());
                    }
                }
            }
        }
    }

    // 2. Coupled multi-layer octilinear A* with step_penalty / via_penalty hooks
    let span = dist_f64(start_mid, end_mid);
    let mut step_candidates = if span > 150_000_000.0 {
        vec![1_000_000_i64, 500_000, 250_000]
    } else if span > 15_000_000.0 {
        vec![500_000_i64, 250_000, 100_000]
    } else {
        vec![250_000_i64, 100_000]
    };
    if span < 5_000_000.0 {
        step_candidates.push(100_000);
    }
    for &step_nm in &step_candidates {
        for &(_, si, ei) in pairs_ranked.iter().take(16) {
            let sf = &start_fanouts[si];
            let ef = &end_fanouts[ei];
            let res = astar_coupled_centerline(
                engine,
                pair,
                sf,
                ef,
                &allowed_layer_idxs,
                step_nm,
                step_penalty,
                via_penalty,
                budget,
                expanded.as_deref_mut(),
                frontier.as_deref_mut(),
            );
            match res {
                Ok(Some(sol)) => {
                    if use_commit_tx {
                        return commit_coupled_solution(engine, pair, sol)
                            .map_err(UnitRouteError::Fatal);
                    }
                    if commit_coupled_solution_direct(engine, pair, sol)
                        .map_err(UnitRouteError::Fatal)?
                    {
                        return Ok(());
                    }
                }
                Ok(None) => {}
                Err(e) if e.status == Status::BudgetExhausted => {
                    if let Some(f) = frontier {
                        if budget.aborted
                            || (budget.current_limit < budget.batch_limit && !f.is_empty())
                        {
                            return Err(UnitRouteError::Blocked(std::mem::take(f)));
                        }
                    }
                    return Err(UnitRouteError::Fatal(e));
                }
                Err(e) => return Err(UnitRouteError::Fatal(e)),
            }
        }
    }

    if let Some(f) = frontier {
        if f.is_empty() {
            for &(_, si, ei) in pairs_ranked.iter().take(4) {
                let sf = &start_fanouts[si];
                let ef = &end_fanouts[ei];
                f.push_edge(sf.layer_idx as u8, sf.gp, ef.gp);
                f.push_edge(sf.layer_idx as u8, sf.gn, ef.gn);
            }
            if f.is_empty() {
                for &li in &allowed_layer_idxs {
                    f.push_edge(li as u8, p_start_at, p_end_at);
                    f.push_edge(li as u8, n_start_at, n_end_at);
                }
            }
        }
        return Err(UnitRouteError::Blocked(std::mem::take(f)));
    }

    Err(UnitRouteError::Fatal(Failure::new(
        Status::Unroutable,
        format!(
            "coupled routing failed for differential pair ({:?}, {:?}); no single-ended fallback allowed",
            pair.pos_name, pair.neg_name
        ),
    )))
}

fn route_single_diff_pair(
    engine: &mut Engine,
    pair: &ResolvedDiffPair,
    plan: &DiffPairPlan,
    step_penalty: &dyn Fn(u8, Point) -> u64,
    via_penalty: &dyn Fn(Point, u8, u8) -> u64,
) -> Result<(), Failure> {
    let mut budget = TrialBudget::fixed(engine.input.max_expansions);
    match route_single_diff_pair_inner(
        engine,
        pair,
        plan,
        step_penalty,
        via_penalty,
        &mut budget,
        None,
        None,
        true,
    ) {
        Ok(()) => Ok(()),
        Err(UnitRouteError::Fatal(e)) => Err(e),
        Err(UnitRouteError::Blocked(_)) => Err(Failure::new(
            Status::Unroutable,
            format!(
                "coupled routing failed for differential pair ({:?}, {:?}); no single-ended fallback allowed",
                pair.pos_name, pair.neg_name
            ),
        )),
    }
}

fn commit_coupled_solution_direct(
    engine: &mut Engine,
    pair: &ResolvedDiffPair,
    sol: CoupledSolution,
) -> Result<bool, Failure> {
    let p_had_gen = engine.copper.has_generated_for_net(pair.pos_net);
    let n_had_gen = engine.copper.has_generated_for_net(pair.neg_net);
    if p_had_gen || n_had_gen {
        return match commit_coupled_solution(engine, pair, sol) {
            Ok(()) => Ok(true),
            Err(e) if e.status == Status::Unroutable => Ok(false),
            Err(e) => Err(e),
        };
    }
    for t in sol.tracks {
        if t.start != t.end {
            engine.copper.insert(t, &engine.work)?;
        }
    }
    for v in sol.vias {
        engine.copper.insert_via(v, &engine.work)?;
    }
    let p_ok = measure_net_fromto(engine, pair.pos_net, false)?.is_some()
        && engine.components(pair.pos_net)?.len() <= 1;
    let n_ok = measure_net_fromto(engine, pair.neg_net, false)?.is_some()
        && engine.components(pair.neg_net)?.len() <= 1;
    if !p_ok || !n_ok {
        engine.copper.remove_net(pair.pos_net, &engine.work)?;
        engine.copper.remove_net(pair.neg_net, &engine.work)?;
        return Ok(false);
    }
    Ok(true)
}

fn commit_coupled_solution(
    engine: &mut Engine,
    pair: &ResolvedDiffPair,
    sol: CoupledSolution,
) -> Result<(), Failure> {
    unwrap_tx(engine.transaction(|tx| {
        let eng = &mut *tx.engine;
        for t in sol.tracks {
            if t.start != t.end {
                eng.copper.insert(t, &eng.work)?;
            }
        }
        for v in sol.vias {
            eng.copper.insert_via(v, &eng.work)?;
        }
        let p_ok = measure_net_fromto(eng, pair.pos_net, false)?.is_some()
            && eng.components(pair.pos_net)?.len() <= 1;
        let n_ok = measure_net_fromto(eng, pair.neg_net, false)?.is_some()
            && eng.components(pair.neg_net)?.len() <= 1;
        if !p_ok || !n_ok {
            return Err(Failure::new(
                Status::Unroutable,
                format!(
                    "coupled solution failed connectivity verification for {}",
                    pair.stem
                ),
            ));
        }
        Ok(())
    }))
}

pub(crate) fn delay_profile_layers(
    engine: &Engine,
    state: &DiffPairRunState,
) -> BTreeMap<u32, BTreeSet<String>> {
    let mut out: BTreeMap<u32, BTreeSet<String>> = BTreeMap::new();
    for g in &state.plan.length_groups {
        if !matches!(g.mode, GroupTargetMode::DelayAs { .. }) {
            continue;
        }
        for &nid in &g.members {
            let Some(prof_name) = engine.effective_rules.net(nid).tuning_profile.as_deref() else {
                continue;
            };
            if let Some(prof) = engine
                .effective_rules
                .tuning_profiles
                .iter()
                .find(|p| p.profile_name == prof_name)
            {
                let allowed: BTreeSet<String> = prof
                    .layer_entries
                    .iter()
                    .map(|le| le.signal_layer.clone())
                    .collect();
                if !allowed.is_empty() {
                    out.insert(nid, allowed);
                }
            }
        }
    }
    out
}

pub(crate) fn plan_diff_pairs(engine: &Engine) -> Result<DiffPairRunState, Failure> {
    let plan = discover_and_validate_with_copper(
        &engine.input,
        engine.copper.input(),
        engine.copper.input_vias(),
        &engine.effective_rules,
    )?;
    let routed_pair_nets: BTreeSet<u32> = plan
        .pairs
        .iter()
        .flat_map(|p| [p.pos_net, p.neg_net])
        .collect();

    let active_single_ended_nets = if plan.active_nets.is_some()
        || !routed_pair_nets.is_empty()
        || !plan.protected_nets.is_empty()
    {
        let mut set = BTreeSet::new();
        for n in &engine.input.nets {
            if n.id == 0 {
                continue;
            }
            if let Some(active) = &plan.active_nets {
                if !active.contains(&n.id) {
                    continue;
                }
            }
            if !routed_pair_nets.contains(&n.id) && !plan.protected_nets.contains(&n.id) {
                set.insert(n.id);
            }
        }
        Some(set)
    } else {
        None
    };

    Ok(DiffPairRunState {
        routed_pair_nets,
        active_single_ended_nets,
        protected_nets: plan.protected_nets.clone(),
        plan,
    })
}

pub struct CompositeRouteAdapter {
    state: DiffPairRunState,
    pair_by_net: BTreeMap<u32, usize>,
}

impl CompositeRouteAdapter {
    pub fn new(state: DiffPairRunState) -> Self {
        let mut pair_by_net = BTreeMap::new();
        for (idx, pair) in state.plan.pairs.iter().enumerate() {
            pair_by_net.insert(pair.pos_net, idx);
            pair_by_net.insert(pair.neg_net, idx);
        }
        Self { state, pair_by_net }
    }

    fn route_unit_with_budget(
        &mut self,
        engine: &mut Engine,
        unit: RouteUnitId,
        history: &CongestionHistory,
        budget: &mut TrialBudget<'_>,
        expanded: &mut u64,
    ) -> Result<(), UnitRouteError> {
        match unit {
            RouteUnitId::SingleNet(net) => {
                engine.route_net_negotiated_with_budget(net, budget, expanded, history)
            }
            RouteUnitId::DiffPair(idx) => {
                let pair = self.state.plan.pairs.get(idx).ok_or_else(|| {
                    UnitRouteError::Fatal(Failure::new(
                        Status::Invalid,
                        format!("unknown diff pair unit index {idx}"),
                    ))
                })?;
                let bounds_min = engine.input.bounds.min;
                let grid_nm = engine.input.rules.grid_nm;
                let step_pen = |layer: u8, pt: Point| -> u64 {
                    history
                        .step_penalty(layer, pt, bounds_min, grid_nm)
                        .saturating_mul(250_000)
                };
                let via_pen = |pt: Point, s: u8, e: u8| -> u64 {
                    history
                        .via_penalty(pt, s, e, bounds_min, grid_nm)
                        .saturating_mul(250_000)
                };
                let mut frontier = BlockedFrontier::new();
                route_single_diff_pair_inner(
                    engine,
                    pair,
                    &self.state.plan,
                    &step_pen,
                    &via_pen,
                    budget,
                    Some(expanded),
                    Some(&mut frontier),
                    false,
                )
            }
        }
    }
}

impl RouteUnitAdapter for CompositeRouteAdapter {
    fn units(&self, engine: &Engine) -> Result<Vec<UnitSpec>, Failure> {
        engine.charge_work(engine.input().nets.len() as u64)?;
        let mut out: Vec<UnitSpec> = self
            .state
            .plan
            .pairs
            .iter()
            .enumerate()
            .map(|(idx, pair)| UnitSpec {
                id: RouteUnitId::DiffPair(idx),
                nets: vec![pair.pos_net, pair.neg_net],
                priority_tier: 0,
                rippable_by_single: false,
            })
            .collect();
        let mut se_nets: Vec<u32> = engine
            .input()
            .nets
            .iter()
            .filter(|n| {
                n.id != 0
                    && !self.state.routed_pair_nets.contains(&n.id)
                    && !self.state.protected_nets.contains(&n.id)
                    && self
                        .state
                        .active_single_ended_nets
                        .as_ref()
                        .is_none_or(|s| s.contains(&n.id))
            })
            .map(|n| n.id)
            .collect();
        engine.charge_work(se_nets.len() as u64)?;
        se_nets.sort_unstable();
        out.extend(se_nets.into_iter().map(|net| UnitSpec {
            id: RouteUnitId::SingleNet(net),
            nets: vec![net],
            priority_tier: 1,
            rippable_by_single: true,
        }));
        Ok(out)
    }

    fn unit_for_net(&self, net: u32) -> Option<RouteUnitId> {
        if let Some(&idx) = self.pair_by_net.get(&net) {
            return Some(RouteUnitId::DiffPair(idx));
        }
        if net == 0 || self.state.protected_nets.contains(&net) {
            return None;
        }
        if let Some(active) = &self.state.active_single_ended_nets {
            if !active.contains(&net) {
                return None;
            }
        }
        Some(RouteUnitId::SingleNet(net))
    }

    fn route_unit(
        &mut self,
        engine: &mut Engine,
        unit: RouteUnitId,
        history: &CongestionHistory,
        max_expansions: u64,
        expanded: &mut u64,
    ) -> Result<(), UnitRouteError> {
        let mut budget = TrialBudget::fixed(max_expansions);
        self.route_unit_with_budget(engine, unit, history, &mut budget, expanded)
    }

    fn route_unit_with_trial(
        &mut self,
        engine: &mut Engine,
        unit: RouteUnitId,
        history: &CongestionHistory,
        trial_limit: u64,
        batch_limit: u64,
        expanded: &mut u64,
        on_trial_limit: &mut dyn FnMut(&Engine, &BlockedFrontier) -> Result<bool, Failure>,
    ) -> Result<(), UnitRouteError> {
        let mut budget = TrialBudget::with_trial(trial_limit, batch_limit, on_trial_limit);
        self.route_unit_with_budget(engine, unit, history, &mut budget, expanded)
    }

    fn rip_unit(&mut self, engine: &mut Engine, unit: RouteUnitId) -> Result<(), Failure> {
        match unit {
            RouteUnitId::SingleNet(net) => engine.remove_generated_net(net),
            RouteUnitId::DiffPair(idx) => {
                let pair = self.state.plan.pairs.get(idx).ok_or_else(|| {
                    Failure::new(
                        Status::Invalid,
                        format!("unknown diff pair unit index {idx}"),
                    )
                })?;
                engine.remove_generated_net(pair.pos_net)?;
                engine.remove_generated_net(pair.neg_net)?;
                Ok(())
            }
        }
    }

    fn is_unit_complete(&self, engine: &Engine, unit: RouteUnitId) -> Result<bool, Failure> {
        match unit {
            RouteUnitId::SingleNet(net) => Ok(engine.components(net)?.len() <= 1),
            RouteUnitId::DiffPair(idx) => {
                let pair = self.state.plan.pairs.get(idx).ok_or_else(|| {
                    Failure::new(
                        Status::Invalid,
                        format!("unknown diff pair unit index {idx}"),
                    )
                })?;
                Ok(measure_net_fromto(engine, pair.pos_net, false)?.is_some()
                    && measure_net_fromto(engine, pair.neg_net, false)?.is_some()
                    && engine.components(pair.pos_net)?.len() <= 1
                    && engine.components(pair.neg_net)?.len() <= 1)
            }
        }
    }
}

pub fn prepare_and_route_pairs_in_tx(engine: &mut Engine) -> Result<DiffPairRunState, Failure> {
    let zero_step = |_: u8, _: Point| 0u64;
    let zero_via = |_: Point, _: u8, _: u8| 0u64;
    prepare_and_route_pairs_with_penalty_in_tx(engine, &zero_step, &zero_via)
}

pub fn prepare_and_route_pairs_with_penalty_in_tx(
    engine: &mut Engine,
    step_penalty: &dyn Fn(u8, Point) -> u64,
    via_penalty: &dyn Fn(Point, u8, u8) -> u64,
) -> Result<DiffPairRunState, Failure> {
    let state = plan_diff_pairs(engine)?;
    if !state.plan.pairs.is_empty() {
        unwrap_tx(engine.transaction(|tx| {
            let eng = &mut *tx.engine;
            for pair in &state.plan.pairs {
                route_single_diff_pair(eng, pair, &state.plan, step_penalty, via_penalty)?;
            }
            Ok(())
        }))?;
    }
    Ok(state)
}
// ---------------------------------------------------------------------------
// Length & Delay Matching (Intra-Pair, Coupled Tandem & Single-Ended Meanders)
// ---------------------------------------------------------------------------

#[derive(Default)]
struct MeanderAttempts {
    capacity_failure: Option<Failure>,
}

impl MeanderAttempts {
    fn accepted(
        &mut self,
        attempt: Result<(), TransactionError<Failure>>,
    ) -> Result<bool, Failure> {
        match attempt {
            Ok(()) => Ok(true),
            Err(TransactionError::Operation(f)) if f.status == Status::Unroutable => Ok(false),
            // Rollback frees the items and undo entries. A smaller candidate may fit.
            Err(TransactionError::Operation(f))
                if matches!(
                    f.status,
                    Status::JournalExhausted | Status::AdmissionExceeded
                ) =>
            {
                self.capacity_failure.get_or_insert(f);
                Ok(false)
            }
            Err(e) => Err(e.into()),
        }
    }

    fn failure(self, fallback: Failure) -> Failure {
        if fallback.status == Status::Unroutable {
            self.capacity_failure.unwrap_or(fallback)
        } else {
            fallback
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn generate_45_meander_wave(
    start: Point,
    ux: f64,
    uy: f64,
    nx: f64,
    ny: f64,
    t_center: f64,
    amp_nm: f64,
    spacing_nm: f64,
    n_waves: usize,
    alternate_sides: bool,
    width_nm: f64,
    inner_offset_margin_nm: f64,
    allow_ortho: bool,
    allow_chamfer: bool,
) -> (Vec<Point>, f64, f64) {
    let min_seg = (1.2 * width_nm).max(width_nm + 50_000.0);
    let c_try = (0.3 * spacing_nm).min(0.35 * amp_nm);
    let gap_try = (spacing_nm - 2.0 * c_try).max(0.5 * spacing_nm);
    let inner_miter_shrink = inner_offset_margin_nm * (2.0 - std::f64::consts::SQRT_2);
    let inner_top_shrink = 2.0 * inner_offset_margin_nm * (std::f64::consts::SQRT_2 - 1.0);
    let chamfer_ok = allow_chamfer
        && c_try - inner_miter_shrink >= min_seg
        && (spacing_nm - 2.0 * c_try - inner_top_shrink) >= min_seg
        && (amp_nm - 2.0 * c_try - inner_top_shrink) >= min_seg
        && (n_waves <= 1 || (gap_try - inner_top_shrink) >= min_seg);

    let pt_at = |t: f64, h: f64| -> Point {
        [
            start[0] + (t * ux + h * nx).round() as i64,
            start[1] + (t * uy + h * ny).round() as i64,
        ]
    };

    if chamfer_ok {
        let c = c_try;
        let wave_span = spacing_nm + 2.0 * c;
        let gap_between = gap_try;
        let pitch = wave_span + gap_between;
        let total_span =
            (n_waves as f64) * wave_span + (n_waves.saturating_sub(1) as f64) * gap_between;
        let t0 = t_center - 0.5 * total_span;
        let mut pts = Vec::with_capacity(n_waves * 8);
        for i in 0..n_waves {
            let s = if alternate_sides && (i % 2 == 1) {
                -1.0
            } else {
                1.0
            };
            let tb = t0 + (i as f64) * pitch;
            let t_up = tb + c;
            let t_dn = t_up + spacing_nm;
            let t_end = t_dn + c;
            pts.push(pt_at(tb, 0.0));
            pts.push(pt_at(t_up, s * c));
            pts.push(pt_at(t_up, s * (amp_nm - c)));
            pts.push(pt_at(t_up + c, s * amp_nm));
            pts.push(pt_at(t_dn - c, s * amp_nm));
            pts.push(pt_at(t_dn, s * (amp_nm - c)));
            pts.push(pt_at(t_dn, s * c));
            pts.push(pt_at(t_end, 0.0));
        }
        return (pts, t0, t0 + total_span);
    }

    let ortho_ok = allow_ortho
        && (amp_nm - 2.0 * inner_offset_margin_nm) >= min_seg
        && (spacing_nm - 2.0 * inner_offset_margin_nm) >= min_seg;
    if ortho_ok {
        let wave_span = spacing_nm;
        let gap_between = spacing_nm;
        let pitch = wave_span + gap_between;
        let total_span =
            (n_waves as f64) * wave_span + (n_waves.saturating_sub(1) as f64) * gap_between;
        let t0 = t_center - 0.5 * total_span;
        let mut pts = Vec::with_capacity(n_waves * 4);
        for i in 0..n_waves {
            let s = if alternate_sides && (i % 2 == 1) {
                -1.0
            } else {
                1.0
            };
            let tb = t0 + (i as f64) * pitch;
            let t_end = tb + spacing_nm;
            pts.push(pt_at(tb, 0.0));
            pts.push(pt_at(tb, s * amp_nm));
            pts.push(pt_at(t_end, s * amp_nm));
            pts.push(pt_at(t_end, 0.0));
        }
        return (pts, t0, t0 + total_span);
    }

    // Small 45-degree wave: flat-top 45-degree trapezoid when `!allow_ortho` (avoiding 90-degree
    // corners that overhang in KiCad diff_pair_uncoupled), or 45-degree triangle bump otherwise.
    // Never exceed the caller-requested `amp_nm` ceiling.
    if amp_nm <= 0.0 {
        return (Vec::new(), t_center, t_center);
    }
    let h_small = amp_nm;
    let top_len = if allow_ortho {
        0.0
    } else {
        min_seg + inner_top_shrink
    };
    let gap_between = if allow_ortho {
        (min_seg + inner_top_shrink).max(0.5 * spacing_nm)
    } else {
        min_seg + inner_top_shrink
    };
    let wave_span = 2.0 * h_small + top_len;
    let tri_span = (n_waves as f64) * wave_span + (n_waves.saturating_sub(1) as f64) * gap_between;
    let t_start = t_center - 0.5 * tri_span;
    let step_tri = wave_span + gap_between;
    let mut pts = Vec::with_capacity(n_waves * 4);
    for i in 0..n_waves {
        let s = if alternate_sides && (i % 2 == 1) {
            -1.0
        } else {
            1.0
        };
        let tb = t_start + (i as f64) * step_tri;
        pts.push(pt_at(tb, 0.0));
        pts.push(pt_at(tb + h_small, s * h_small));
        if top_len > 0.0 {
            pts.push(pt_at(tb + h_small + top_len, s * h_small));
        }
        pts.push(pt_at(tb + wave_span, 0.0));
    }
    (pts, t_start, t_start + tri_span)
}

fn measure_tracks_uncoupled_nm(pair: &ResolvedDiffPair, tracks: &[Track]) -> i64 {
    let p_tracks: Vec<&Track> = tracks.iter().filter(|t| t.net == pair.pos_net).collect();
    let n_tracks: Vec<&Track> = tracks.iter().filter(|t| t.net == pair.neg_net).collect();

    let leg_uncoupled = |own: &[&Track], other: &[&Track]| -> f64 {
        let mut total = 0.0_f64;
        for t in own {
            let Some((ux, uy)) = dir_unit_f64(t.start, t.end) else {
                continue;
            };
            let len = dist_f64(t.start, t.end);
            if len <= 1.0 {
                continue;
            }
            let mut intervals: Vec<(f64, f64)> = Vec::new();
            for o in other {
                if o.layer != t.layer {
                    continue;
                }
                let Some((ox, oy)) = dir_unit_f64(o.start, o.end) else {
                    continue;
                };
                if (ux * ox + uy * oy).abs() < 0.999 {
                    continue;
                }
                let vx = (o.start[0] - t.start[0]) as f64;
                let vy = (o.start[1] - t.start[1]) as f64;
                let perp = (ux * vy - uy * vx).abs();
                let half_w = 0.5 * (t.width_nm + o.width_nm) as f64;
                let min_c = (half_w + pair.gap_min_nm as f64 - 500.0).max(0.0);
                let max_c = half_w + pair.gap_max_nm as f64 + 500.0;
                if perp < min_c || perp > max_c {
                    continue;
                }
                let wx = (o.end[0] - t.start[0]) as f64;
                let wy = (o.end[1] - t.start[1]) as f64;
                let p1 = ux * vx + uy * vy;
                let p2 = ux * wx + uy * wy;
                let lo = p1.min(p2).max(0.0);
                let hi = p1.max(p2).min(len);
                if hi > lo {
                    intervals.push((lo, hi));
                }
            }
            intervals.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
            let mut coupled = 0.0_f64;
            let mut cur_end = 0.0_f64;
            for (lo, hi) in intervals {
                let start = lo.max(cur_end);
                if hi > start {
                    coupled += hi - start;
                    cur_end = hi;
                }
            }
            total += (len - coupled).max(0.0);
        }
        total
    };

    let u_p = leg_uncoupled(&p_tracks, &n_tracks);
    let u_n = leg_uncoupled(&n_tracks, &p_tracks);
    u_p.max(u_n).round() as i64
}

fn measure_pair_uncoupled_nm(engine: &Engine, pair: &ResolvedDiffPair) -> i64 {
    let pair_tracks: Vec<Track> = engine
        .copper
        .tracks()
        .filter(|t| t.net == pair.pos_net || t.net == pair.neg_net)
        .cloned()
        .collect();
    measure_tracks_uncoupled_nm(pair, &pair_tracks)
}

fn meander_clean_clears_pads(engine: &Engine, net: u32, cand: &Track, clean: &[Point]) -> bool {
    if clean.len() < 2 {
        return true;
    }
    for pad in engine.input.pads.iter().filter(|p| p.net == net) {
        if !pad_supports_layer(engine, pad, &cand.layer) {
            continue;
        }
        for &v in &clean[1..clean.len() - 1] {
            if geometry::track_near_pad(v, v, cand.width_nm, 1_000, pad, true) {
                return false;
            }
        }
        let start_in_pad =
            geometry::track_near_pad(cand.start, cand.start, cand.width_nm, 1_000, pad, true);
        let end_in_pad =
            geometry::track_near_pad(cand.end, cand.end, cand.width_nm, 1_000, pad, true);
        for (idx, w) in clean.windows(2).enumerate() {
            let skip_start = idx == 0 && start_in_pad;
            let skip_end = idx + 2 == clean.len() && end_in_pad;
            if skip_start || skip_end {
                continue;
            }
            if geometry::track_near_pad(w[0], w[1], cand.width_nm, 1_000, pad, true) {
                return false;
            }
        }
    }
    true
}

fn same_net_meander_clear(
    cand: &Track,
    clean: &[Point],
    other_same_net: &[Track],
    min_clr_nm: i64,
) -> bool {
    let clr2 = 2 * i128::from(cand.width_nm) + 2 * i128::from(min_clr_nm);
    let on_cand_line = |p: Point| -> bool {
        let vx = i128::from(cand.end[0] - cand.start[0]);
        let vy = i128::from(cand.end[1] - cand.start[1]);
        let wx = i128::from(p[0] - cand.start[0]);
        let wy = i128::from(p[1] - cand.start[1]);
        (vx * wy - vy * wx).abs() <= vx.abs().max(vy.abs()) * 2
    };
    for ot in other_same_net {
        if ot.layer != cand.layer {
            continue;
        }
        // Only check newly introduced off-line vertices and segments that leave `cand`'s line;
        // collinear sub-segments of `cand` touching `cand.start`/`cand.end` retain pre-existing proximity.
        for &pt in clean {
            if pt != cand.start
                && pt != cand.end
                && !on_cand_line(pt)
                && geometry::point_near_segment(pt, ot.start, ot.end, clr2, false)
            {
                return false;
            }
        }
        for w in clean.windows(2) {
            if !on_cand_line(w[0]) || !on_cand_line(w[1]) {
                if w[0] != cand.start && w[0] != cand.end && w[1] != cand.start && w[1] != cand.end
                {
                    if geometry::segments_near(w[0], w[1], ot.start, ot.end, clr2, false) {
                        return false;
                    }
                } else {
                    // V-peak leg attached to cand endpoint: check midpoint + 75% point toward apex
                    let off = if !on_cand_line(w[0]) { w[0] } else { w[1] };
                    let end = if !on_cand_line(w[0]) { w[1] } else { w[0] };
                    let mid = [(off[0] + end[0]) / 2, (off[1] + end[1]) / 2];
                    if geometry::point_near_segment(mid, ot.start, ot.end, clr2, false) {
                        return false;
                    }
                }
            }
        }
    }
    true
}

fn replace_generated_track_with_pts(
    engine: &mut Engine,
    net: u32,
    seg_match: &Track,
    replacement_pts: &[Point],
) -> Result<(), Failure> {
    let mut gen_tracks: Vec<Track> = engine
        .copper
        .generated()
        .filter(|t| t.net == net)
        .cloned()
        .collect();
    let gen_vias: Vec<Via> = engine
        .copper
        .generated_vias()
        .filter(|v| v.net == net)
        .cloned()
        .collect();

    let Some(pos) = gen_tracks.iter().position(|t| {
        t.layer == seg_match.layer
            && t.width_nm == seg_match.width_nm
            && ((t.start == seg_match.start && t.end == seg_match.end)
                || (t.start == seg_match.end && t.end == seg_match.start))
    }) else {
        return Err(Failure::new(
            Status::Unroutable,
            "target track segment missing for meander replacement",
        ));
    };
    let old = gen_tracks.remove(pos);
    let clean = simplify_collinear_points(replacement_pts.to_vec());
    for w in clean.windows(2) {
        if w[0] != w[1] {
            gen_tracks.push(Track {
                id: String::new(),
                net,
                start: w[0],
                end: w[1],
                width_nm: old.width_nm,
                locked: false,
                layer: old.layer.clone(),
            });
        }
    }
    engine.copper.remove_net(net, &engine.work)?;
    for t in gen_tracks {
        engine.copper.insert(t, &engine.work)?;
    }
    for v in gen_vias {
        engine.copper.insert_via(v, &engine.work)?;
    }
    Ok(())
}

fn is_pad_connected_uncoupled_escape(
    engine: &Engine,
    pair: &ResolvedDiffPair,
    net: u32,
    cand: &Track,
) -> bool {
    let other_net = if net == pair.pos_net {
        pair.neg_net
    } else {
        pair.pos_net
    };
    let own_tracks: Vec<&Track> = engine
        .copper
        .tracks()
        .filter(|t| t.net == net && t.layer == cand.layer)
        .collect();
    let other_tracks: Vec<&Track> = engine
        .copper
        .tracks()
        .filter(|t| t.net == other_net && t.layer == cand.layer)
        .collect();
    let has_coupled_overlap = |t: &Track| -> bool {
        let Some((ux, uy)) = dir_unit_f64(t.start, t.end) else {
            return false;
        };
        let len = dist_f64(t.start, t.end);
        for o in &other_tracks {
            let Some((ox, oy)) = dir_unit_f64(o.start, o.end) else {
                continue;
            };
            if (ux * ox + uy * oy).abs() < 0.999 {
                continue;
            }
            let vx = (o.start[0] - t.start[0]) as f64;
            let vy = (o.start[1] - t.start[1]) as f64;
            let perp = (ux * vy - uy * vx).abs();
            let half_w = 0.5 * (t.width_nm + o.width_nm) as f64;
            let min_c = (half_w + pair.gap_min_nm as f64 - 1_000.0).max(0.0);
            let max_c = half_w + pair.gap_max_nm as f64 + 1_000.0;
            if perp < min_c || perp > max_c {
                continue;
            }
            let wx = (o.end[0] - t.start[0]) as f64;
            let wy = (o.end[1] - t.start[1]) as f64;
            let p1 = ux * vx + uy * vy;
            let p2 = ux * wx + uy * wy;
            let lo = p1.min(p2).max(0.0);
            let hi = p1.max(p2).min(len);
            if hi > lo + 1.0 {
                return true;
            }
        }
        false
    };
    if has_coupled_overlap(cand) {
        return false;
    }
    let uncoupled_own: Vec<&Track> = own_tracks
        .into_iter()
        .filter(|t| !has_coupled_overlap(t))
        .collect();
    let Some(cand_idx) = uncoupled_own.iter().position(|t| {
        t.width_nm == cand.width_nm
            && ((t.start == cand.start && t.end == cand.end)
                || (t.start == cand.end && t.end == cand.start))
    }) else {
        return false;
    };
    let net_pads: Vec<_> = engine
        .input
        .pads
        .iter()
        .filter(|p| p.net == net && pad_supports_layer(engine, p, &cand.layer))
        .collect();
    let mut visited = vec![false; uncoupled_own.len()];
    let mut queue = Vec::new();
    for (i, t) in uncoupled_own.iter().enumerate() {
        if net_pads
            .iter()
            .any(|p| t.start == p.at || t.end == p.at || geometry::track_connects_pad(t, p))
        {
            visited[i] = true;
            queue.push(i);
        }
    }
    while let Some(cur) = queue.pop() {
        if cur == cand_idx {
            return true;
        }
        let ct = uncoupled_own[cur];
        for (j, nt) in uncoupled_own.iter().enumerate() {
            if !visited[j]
                && (ct.start == nt.start
                    || ct.start == nt.end
                    || ct.end == nt.start
                    || ct.end == nt.end)
            {
                visited[j] = true;
                queue.push(j);
            }
        }
    }
    visited[cand_idx]
}

fn apply_single_ended_meander(
    engine: &mut Engine,
    net: u32,
    target_val: i64,
    tol: i64,
    is_delay: bool,
    amp_max_nm: i64,
    spacing_widths: f64,
    near_terminal: bool,
    pair_ctx: Option<&ResolvedDiffPair>,
) -> Result<(), Failure> {
    if let Some(pair) = pair_ctx {
        if measure_pair_uncoupled_nm(engine, pair) > pair.uncoupled_max_nm {
            return Err(Failure::new(
                Status::Unroutable,
                "diff pair already exceeds diff_pair_uncoupled limit before intra-pair meander",
            ));
        }
    }
    let prof = if is_delay {
        find_tuning_profile(&engine.effective_rules, net).cloned()
    } else {
        None
    };

    let pad_pts: Vec<Point> = engine
        .input
        .pads
        .iter()
        .filter(|p| p.net == net)
        .map(|p| p.at)
        .collect();

    let mut candidates: Vec<Track> = engine
        .copper
        .generated()
        .filter(|t| t.net == net)
        .filter(|t| {
            !pair_ctx.is_some_and(|p| p.impedance.is_some())
                || pair_ctx.is_some_and(|p| is_pad_connected_uncoupled_escape(engine, p, net, t))
        })
        .cloned()
        .collect();
    candidates.sort_by(|a, b| {
        if near_terminal {
            let a_pad = pad_pts.contains(&a.start) || pad_pts.contains(&a.end);
            let b_pad = pad_pts.contains(&b.start) || pad_pts.contains(&b.end);
            b_pad.cmp(&a_pad).then_with(|| {
                dist_f64(b.start, b.end)
                    .partial_cmp(&dist_f64(a.start, a.end))
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
        } else {
            dist_f64(b.start, b.end)
                .partial_cmp(&dist_f64(a.start, a.end))
                .unwrap_or(std::cmp::Ordering::Equal)
        }
    });

    let mut attempts = MeanderAttempts::default();
    // Charge one work unit per wave per leg each time a prospective shape is built,
    // including shapes rejected before copper legality or a transaction is reached.
    for cand in candidates {
        let seg_len = dist_f64(cand.start, cand.end);
        let spacing_nm = ((cand.width_nm as f64) * spacing_widths)
            .round()
            .max((cand.width_nm as f64 * 1.2).max(cand.width_nm as f64 + 50_000.0));
        if seg_len < 2.2 * spacing_nm {
            continue;
        }
        let Some(li) = engine.layer_index(&cand.layer) else {
            continue;
        };
        let other_same_net: Vec<Track> = engine
            .copper
            .generated()
            .chain(engine.input.tracks.iter())
            .filter(|t| {
                t.net == net
                    && !(t.layer == cand.layer
                        && t.width_nm == cand.width_nm
                        && ((t.start == cand.start && t.end == cand.end)
                            || (t.start == cand.end && t.end == cand.start)))
            })
            .cloned()
            .collect();
        let delay_rate = if let Some(ref p) = prof {
            let Some(le) = p
                .layer_entries
                .iter()
                .find(|e| e.signal_layer == cand.layer)
            else {
                continue;
            };
            if le.delay_as_per_mm <= 0 {
                continue;
            }
            le.delay_as_per_mm as f64
        } else {
            0.0
        };

        let cur = measure_net_fromto(engine, net, is_delay)?.ok_or_else(|| {
            Failure::new(Status::Unroutable, "net disconnected before meandering")
        })?;
        let cur_val = if is_delay {
            cur.delay_as
        } else {
            cur.length_nm
        };
        if cur_val >= target_val - tol {
            return Ok(());
        }
        let needed_len_nm = if is_delay {
            ((target_val - cur_val) as f64 / delay_rate) * 1_000_000.0
        } else {
            (target_val - cur_val) as f64
        };

        let Some((ux, uy)) = dir_unit_f64(cand.start, cand.end) else {
            continue;
        };
        let max_waves =
            (((seg_len - 1.5 * spacing_nm) / (2.0 * spacing_nm)).floor() as usize).max(1);
        let max_amp = amp_max_nm as f64;
        if max_amp <= 10_000.0 {
            continue;
        }
        let eval_added = |n_waves: usize,
                          amp: f64,
                          nx: f64,
                          ny: f64,
                          alt: bool,
                          center_t: f64|
         -> Option<(Vec<Point>, f64)> {
            let (wave_pts, t0, t1) = generate_45_meander_wave(
                cand.start,
                ux,
                uy,
                nx,
                ny,
                center_t,
                amp,
                spacing_nm,
                n_waves,
                alt,
                cand.width_nm as f64,
                0.0,
                true,
                true,
            );
            if t0 < 50_000.0 || t1 > seg_len - 50_000.0 {
                return None;
            }
            let mut full = Vec::with_capacity(wave_pts.len() + 2);
            full.push(cand.start);
            full.extend(wave_pts);
            full.push(cand.end);
            let clean = simplify_collinear_points(full);
            let new_len: f64 = clean.windows(2).map(|w| dist_f64(w[0], w[1])).sum();
            Some((clean, new_len - seg_len))
        };

        let center_candidates: Vec<f64> = if near_terminal {
            let mut cs = Vec::new();
            if pad_pts.contains(&cand.start) {
                cs.push((1_200_000.0_f64).min(0.45 * seg_len));
            }
            if pad_pts.contains(&cand.end) {
                cs.push((seg_len - 1_200_000.0).max(0.55 * seg_len));
            }
            if cs.is_empty() {
                cs.push((1_200_000.0_f64).min(0.45 * seg_len));
                cs.push((seg_len - 1_200_000.0).max(0.55 * seg_len));
            }
            cs
        } else {
            vec![0.5 * seg_len]
        };

        for &center_t in &center_candidates {
            for n_waves in 1..=max_waves {
                for &(nx, ny, alt) in &[
                    (-uy, ux, true),
                    (uy, -ux, true),
                    (-uy, ux, false),
                    (uy, -ux, false),
                ] {
                    engine.work.charge(n_waves as u64)?;
                    let Some((_, max_add)) = eval_added(n_waves, max_amp, nx, ny, alt, center_t)
                    else {
                        continue;
                    };
                    if max_add + (tol as f64) < needed_len_nm {
                        continue;
                    }
                    let min_legal_h = (std::f64::consts::FRAC_1_SQRT_2 * cand.width_nm as f64
                        + 1_000.0)
                        .max(10_000.0);
                    let mut lo = min_legal_h.min(max_amp);
                    let mut hi = max_amp;
                    for _ in 0..32 {
                        let mid = 0.5 * (lo + hi);
                        engine.work.charge(n_waves as u64)?;
                        match eval_added(n_waves, mid, nx, ny, alt, center_t) {
                            Some((_, add)) if add < needed_len_nm => lo = mid,
                            _ => hi = mid,
                        }
                    }
                    let mid_amp = 0.5 * (lo + hi);
                    engine.work.charge(3 * n_waves as u64)?;
                    let best_clean = [hi, mid_amp, lo]
                        .into_iter()
                        .filter_map(|a| {
                            eval_added(n_waves, a, nx, ny, alt, center_t).map(|res| (a, res))
                        })
                        .min_by(|(_, (_, add_a)), (_, (_, add_b))| {
                            (add_a - needed_len_nm)
                                .abs()
                                .partial_cmp(&(add_b - needed_len_nm).abs())
                                .unwrap_or(std::cmp::Ordering::Equal)
                        })
                        .map(|(_, (pts, _))| pts);
                    let Some(clean) = best_clean else {
                        continue;
                    };
                    if !same_net_meander_clear(&cand, &clean, &other_same_net, 50_000) {
                        continue;
                    }
                    let attempt = engine.transaction(|tx| {
                        let eng = &mut *tx.engine;
                        replace_generated_track_with_pts(eng, net, &cand, &clean)?;
                        for w in clean.windows(2) {
                            if !legal_edge_with_width(eng, net, li, w[0], w[1], cand.width_nm)? {
                                return Err(Failure::new(Status::Unroutable, "meander illegal"));
                            }
                            if let Some(imp) = pair_ctx.and_then(|p| p.impedance.as_ref()) {
                                if cand.layer == imp.signal_layer
                                    && !segment_footprint_over_reference_zones(
                                        eng,
                                        w[0],
                                        w[1],
                                        cand.width_nm,
                                        imp,
                                    )
                                {
                                    return Err(Failure::new(
                                        Status::Unroutable,
                                        "meander left impedance reference zone",
                                    ));
                                }
                            }
                        }
                        if !meander_clean_clears_pads(eng, net, &cand, &clean) {
                            return Err(Failure::new(
                                Status::Unroutable,
                                "meander re-entered pad copper",
                            ));
                        }
                        if let Some(pair) = pair_ctx {
                            if measure_pair_uncoupled_nm(eng, pair) > pair.uncoupled_max_nm {
                                return Err(Failure::new(
                                    Status::Unroutable,
                                    "intra-pair meander exceeded diff_pair_uncoupled limit",
                                ));
                            }
                        }
                        let m = measure_net_fromto(eng, net, is_delay)?
                            .ok_or_else(|| Failure::new(Status::Unroutable, "disconnected"))?;
                        let new_val = if is_delay { m.delay_as } else { m.length_nm };
                        if (new_val - target_val).abs() > tol {
                            return Err(Failure::new(
                                Status::Unroutable,
                                "meander result outside tolerance",
                            ));
                        }
                        Ok(())
                    });
                    if attempts.accepted(attempt)? {
                        return Ok(());
                    }
                }
            }
        }

        // Shallow V-detour fallback for small deltas below the 45-degree wave minimum
        // while keeping `min_legal_h` so copper never self-bridges across the baseline.
        let min_legal_h =
            (std::f64::consts::FRAC_1_SQRT_2 * cand.width_nm as f64 + 1_000.0).max(10_000.0);
        if max_amp >= min_legal_h && needed_len_nm > 0.0 {
            let amp_levels = if (max_amp - min_legal_h).abs() < 1.0 {
                vec![min_legal_h]
            } else {
                vec![min_legal_h, 0.5 * (min_legal_h + max_amp), max_amp]
            };
            for &center_t in &center_candidates {
                let max_half_span = (center_t - 50_000.0).min(seg_len - 50_000.0 - center_t);
                for &amp_h in &amp_levels {
                    if max_half_span <= amp_h {
                        continue;
                    }
                    for &(nx, ny) in &[(-uy, ux), (uy, -ux)] {
                        let eval_v_detour = |half_span: f64| -> Option<(Vec<Point>, f64)> {
                            let t0 = center_t - half_span;
                            let t1 = center_t + half_span;
                            if t0 < 50_000.0 || t1 > seg_len - 50_000.0 {
                                return None;
                            }
                            let pt_at = |t: f64, h: f64| -> Point {
                                [
                                    cand.start[0] + (t * ux + h * nx).round() as i64,
                                    cand.start[1] + (t * uy + h * ny).round() as i64,
                                ]
                            };
                            let full = vec![
                                cand.start,
                                pt_at(t0, 0.0),
                                pt_at(center_t, amp_h),
                                pt_at(t1, 0.0),
                                cand.end,
                            ];
                            let clean = simplify_collinear_points(full);
                            let new_len: f64 = clean.windows(2).map(|w| dist_f64(w[0], w[1])).sum();
                            Some((clean, new_len - seg_len))
                        };
                        engine.work.charge(1)?;
                        let Some((_, add_max)) = eval_v_detour(amp_h) else {
                            continue;
                        };
                        engine.work.charge(1)?;
                        let Some((_, add_min)) = eval_v_detour(max_half_span) else {
                            continue;
                        };
                        if needed_len_nm + (tol as f64) < add_min
                            || needed_len_nm - (tol as f64) > add_max
                        {
                            continue;
                        }
                        let mut lo_d = amp_h;
                        let mut hi_d = max_half_span;
                        for _ in 0..32 {
                            let mid_d = 0.5 * (lo_d + hi_d);
                            engine.work.charge(1)?;
                            match eval_v_detour(mid_d) {
                                Some((_, add)) if add > needed_len_nm => lo_d = mid_d,
                                _ => hi_d = mid_d,
                            }
                        }
                        let mid_d = 0.5 * (lo_d + hi_d);
                        engine.work.charge(3)?;
                        let best_clean = [lo_d, mid_d, hi_d]
                            .into_iter()
                            .filter_map(eval_v_detour)
                            .min_by(|(_, add_a), (_, add_b)| {
                                (add_a - needed_len_nm)
                                    .abs()
                                    .partial_cmp(&(add_b - needed_len_nm).abs())
                                    .unwrap_or(std::cmp::Ordering::Equal)
                            })
                            .map(|(pts, _)| pts);
                        let Some(clean) = best_clean else {
                            continue;
                        };
                        if !same_net_meander_clear(&cand, &clean, &other_same_net, 50_000) {
                            continue;
                        }
                        let attempt = engine.transaction(|tx| {
                            let eng = &mut *tx.engine;
                            replace_generated_track_with_pts(eng, net, &cand, &clean)?;
                            for w in clean.windows(2) {
                                if !legal_edge_with_width(eng, net, li, w[0], w[1], cand.width_nm)?
                                {
                                    return Err(Failure::new(
                                        Status::Unroutable,
                                        "meander illegal",
                                    ));
                                }
                                if let Some(imp) = pair_ctx.and_then(|p| p.impedance.as_ref()) {
                                    if cand.layer == imp.signal_layer
                                        && !segment_footprint_over_reference_zones(
                                            eng,
                                            w[0],
                                            w[1],
                                            cand.width_nm,
                                            imp,
                                        )
                                    {
                                        return Err(Failure::new(
                                            Status::Unroutable,
                                            "meander left impedance reference zone",
                                        ));
                                    }
                                }
                            }
                            if !meander_clean_clears_pads(eng, net, &cand, &clean) {
                                return Err(Failure::new(
                                    Status::Unroutable,
                                    "meander re-entered pad copper",
                                ));
                            }
                            if let Some(pair) = pair_ctx {
                                if measure_pair_uncoupled_nm(eng, pair) > pair.uncoupled_max_nm {
                                    return Err(Failure::new(
                                        Status::Unroutable,
                                        "intra-pair meander exceeded diff_pair_uncoupled limit",
                                    ));
                                }
                            }
                            let m = measure_net_fromto(eng, net, is_delay)?
                                .ok_or_else(|| Failure::new(Status::Unroutable, "disconnected"))?;
                            let new_val = if is_delay { m.delay_as } else { m.length_nm };
                            if (new_val - target_val).abs() > tol {
                                return Err(Failure::new(
                                    Status::Unroutable,
                                    "meander result outside tolerance",
                                ));
                            }
                            Ok(())
                        });
                        if attempts.accepted(attempt)? {
                            return Ok(());
                        }
                    }
                }
            }
        }
    }

    Err(attempts.failure(Failure::new(
        Status::Unroutable,
        format!("unable to match net {net} to target {target_val} within tolerance {tol}"),
    )))
}
fn apply_coupled_pair_meander(
    engine: &mut Engine,
    pair: &ResolvedDiffPair,
    target_val: i64,
    tol: i64,
    is_delay: bool,
    amp_max_nm: i64,
) -> Result<(), Failure> {
    let mp = measure_net_fromto(engine, pair.pos_net, is_delay)?
        .ok_or_else(|| Failure::new(Status::Unroutable, "pos leg disconnected"))?;
    let mn = measure_net_fromto(engine, pair.neg_net, is_delay)?
        .ok_or_else(|| Failure::new(Status::Unroutable, "neg leg disconnected"))?;
    let cur_p = if is_delay { mp.delay_as } else { mp.length_nm };
    let cur_n = if is_delay { mn.delay_as } else { mn.length_nm };
    let cur_avg = (cur_p + cur_n) / 2;
    if cur_p >= target_val - tol && cur_n >= target_val - tol {
        return Ok(());
    }
    let existing_uncoupled = measure_pair_uncoupled_nm(engine, pair) as f64;
    if existing_uncoupled > pair.uncoupled_max_nm as f64 {
        return Err(Failure::new(
            Status::Unroutable,
            format!(
                "diff pair {} exceeds diff_pair_uncoupled limit before coupled meander",
                pair.stem
            ),
        ));
    }
    let s_nm = pair.width_nm + pair.gap_nm;
    let pos_tracks: Vec<Track> = engine
        .copper
        .generated()
        .filter(|t| t.net == pair.pos_net && t.width_nm == pair.width_nm)
        .cloned()
        .collect();
    let neg_tracks: Vec<Track> = engine
        .copper
        .generated()
        .filter(|t| t.net == pair.neg_net && t.width_nm == pair.width_nm)
        .cloned()
        .collect();

    // Find parallel coupled trunk pairs
    let mut trunk_pairs: Vec<(f64, Track, Track)> = Vec::new();
    for pt in &pos_tracks {
        let Some((upx, upy)) = dir_unit_f64(pt.start, pt.end) else {
            continue;
        };
        let lp = dist_f64(pt.start, pt.end);
        if lp < (3 * s_nm) as f64 {
            continue;
        }
        for nt in &neg_tracks {
            if nt.layer != pt.layer {
                continue;
            }
            let Some((unx, uny)) = dir_unit_f64(nt.start, nt.end) else {
                continue;
            };
            let dot = upx * unx + upy * uny;
            let (na, nb) = if dot > 0.999 {
                (nt.start, nt.end)
            } else if dot < -0.999 {
                (nt.end, nt.start)
            } else {
                continue;
            };
            // Perpendicular distance between parallel lines
            let vx = (na[0] - pt.start[0]) as f64;
            let vy = (na[1] - pt.start[1]) as f64;
            let perp = (vx * (-upy) + vy * upx).abs();
            if (perp - s_nm as f64).abs() <= 15_000.0
                && dist_f64(pt.start, na) <= (4 * s_nm) as f64
                && dist_f64(pt.end, nb) <= (4 * s_nm) as f64
            {
                trunk_pairs.push((lp.min(dist_f64(na, nb)), pt.clone(), nt.clone()));
            }
        }
    }
    trunk_pairs.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));

    let min_seg = (1.2 * pair.width_nm as f64).max(pair.width_nm as f64 + 50_000.0);
    let inner_offset = 0.5 * (s_nm as f64);
    let inner_miter_shrink = inner_offset * (2.0 - std::f64::consts::SQRT_2);
    let inner_top_shrink = 2.0 * inner_offset * (std::f64::consts::SQRT_2 - 1.0);
    let min_c_chamfer = min_seg + inner_miter_shrink;
    let min_top_chamfer = min_seg + inner_top_shrink;
    let min_spacing_for_chamfer = (min_c_chamfer / 0.3)
        .max(2.0 * min_c_chamfer + min_top_chamfer)
        .max(min_top_chamfer / 0.4);
    let spacing_nm = ((2.2 * s_nm as f64)
        .max(s_nm as f64 + 1.5 * pair.width_nm as f64 + pair.clearance_nm as f64)
        .max(min_spacing_for_chamfer))
    .ceil();
    let prof = if is_delay {
        find_tuning_profile(&engine.effective_rules, pair.pos_net).cloned()
    } else {
        None
    };

    let mut attempts = MeanderAttempts::default();
    for (_, pt_orig, nt_orig) in trunk_pairs {
        let Some(li) = engine.layer_index(&pt_orig.layer) else {
            continue;
        };
        let Some((ux, uy)) = dir_unit_f64(pt_orig.start, pt_orig.end) else {
            continue;
        };
        let (na, nb) = {
            let (unx, uny) = dir_unit_f64(nt_orig.start, nt_orig.end).unwrap();
            if ux * unx + uy * uny >= 0.0 {
                (nt_orig.start, nt_orig.end)
            } else {
                (nt_orig.end, nt_orig.start)
            }
        };
        let left = (-uy, ux);
        let pos_on_left = (((pt_orig.start[0] - na[0]) as f64) * left.0
            + ((pt_orig.start[1] - na[1]) as f64) * left.1)
            >= 0.0;
        let c_start = [
            (pt_orig.start[0] + na[0]) / 2,
            (pt_orig.start[1] + na[1]) / 2,
        ];
        let c_end = [(pt_orig.end[0] + nb[0]) / 2, (pt_orig.end[1] + nb[1]) / 2];
        let c_len = dist_f64(c_start, c_end);
        let min_tri_step =
            2.0 * (0.85 * pair.width_nm as f64) + min_top_chamfer.max(0.5 * spacing_nm);
        let max_waves = (((c_len - 2.0 * s_nm as f64)
            / min_tri_step.min(2.0 * spacing_nm).max(min_seg))
        .floor() as usize)
            .max(1);

        let delay_rate = if let Some(ref p) = prof {
            let Some(le) = p
                .layer_entries
                .iter()
                .find(|e| e.signal_layer == pt_orig.layer)
            else {
                continue;
            };
            le.delay_as_per_mm as f64
        } else {
            0.0
        };
        let needed_len_nm = if is_delay {
            ((target_val - cur_avg) as f64 / delay_rate) * 1_000_000.0
        } else {
            (target_val - cur_avg) as f64
        };
        let max_amp = amp_max_nm as f64;
        if max_amp <= 10_000.0 {
            continue;
        }

        let eval_coupled = |n_waves: usize,
                            amp: f64,
                            nx: f64,
                            ny: f64,
                            alt: bool,
                            allow_chamfer: bool|
         -> Option<(Vec<Point>, Vec<Point>, f64)> {
            let (wave_pts, t0, t1) = generate_45_meander_wave(
                c_start,
                ux,
                uy,
                nx,
                ny,
                0.5 * c_len,
                amp,
                spacing_nm,
                n_waves,
                alt,
                pair.width_nm as f64,
                0.5 * (s_nm as f64),
                false,
                allow_chamfer,
            );
            if t0 < s_nm as f64 || t1 > c_len - (s_nm as f64) {
                return None;
            }
            let mut full_c = Vec::with_capacity(wave_pts.len() + 2);
            full_c.push(c_start);
            full_c.extend(wave_pts);
            full_c.push(c_end);
            let (mut p_poly, mut n_poly) = offset_centerline_mitered(&full_c, s_nm, pos_on_left);
            if p_poly.len() < 2 || n_poly.len() < 2 {
                return None;
            }
            *p_poly.first_mut().unwrap() = pt_orig.start;
            *p_poly.last_mut().unwrap() = pt_orig.end;
            *n_poly.first_mut().unwrap() = na;
            *n_poly.last_mut().unwrap() = nb;
            let p_new: f64 = p_poly.windows(2).map(|w| dist_f64(w[0], w[1])).sum();
            let n_new: f64 = n_poly.windows(2).map(|w| dist_f64(w[0], w[1])).sum();
            let avg_add =
                0.5 * ((p_new - dist_f64(pt_orig.start, pt_orig.end)) + (n_new - dist_f64(na, nb)));
            Some((p_poly, n_poly, avg_add))
        };

        for n_waves in 1..=max_waves {
            for allow_chamfer in [true, false] {
                for &(nx, ny, alt) in &[
                    (-uy, ux, true),
                    (uy, -ux, true),
                    (-uy, ux, false),
                    (uy, -ux, false),
                ] {
                    engine.work.charge(2 * n_waves as u64)?;
                    let Some((_, _, max_add)) =
                        eval_coupled(n_waves, max_amp, nx, ny, alt, allow_chamfer)
                    else {
                        continue;
                    };
                    if max_add + (tol as f64) < needed_len_nm {
                        continue;
                    }
                    let min_legal_h = (std::f64::consts::FRAC_1_SQRT_2 * (pair.width_nm as f64)
                        + 0.5 * (s_nm as f64) * (std::f64::consts::SQRT_2 - 1.0)
                        + 1_000.0)
                        .max(10_000.0);
                    let mut lo = min_legal_h.min(max_amp);
                    let mut hi = max_amp;
                    for _ in 0..32 {
                        let mid = 0.5 * (lo + hi);
                        engine.work.charge(2 * n_waves as u64)?;
                        match eval_coupled(n_waves, mid, nx, ny, alt, allow_chamfer) {
                            Some((_, _, add)) if add < needed_len_nm => lo = mid,
                            _ => hi = mid,
                        }
                    }
                    let mid_amp = 0.5 * (lo + hi);
                    engine.work.charge(3 * 2 * n_waves as u64)?;
                    let best_coupled = [hi, mid_amp, lo]
                        .into_iter()
                        .filter_map(|a| eval_coupled(n_waves, a, nx, ny, alt, allow_chamfer))
                        .min_by(|(_, _, add_a), (_, _, add_b)| {
                            (add_a - needed_len_nm)
                                .abs()
                                .partial_cmp(&(add_b - needed_len_nm).abs())
                                .unwrap_or(std::cmp::Ordering::Equal)
                        });
                    let Some((p_poly, n_poly, _)) = best_coupled else {
                        continue;
                    };
                    let attempt = engine.transaction(|tx| {
                        let eng = &mut *tx.engine;
                        replace_generated_track_with_pts(eng, pair.pos_net, &pt_orig, &p_poly)?;
                        replace_generated_track_with_pts(eng, pair.neg_net, &nt_orig, &n_poly)?;
                        for w in p_poly.windows(2) {
                            if !legal_edge_with_width(
                                eng,
                                pair.pos_net,
                                li,
                                w[0],
                                w[1],
                                pair.width_nm,
                            )? {
                                return Err(Failure::new(
                                    Status::Unroutable,
                                    "coupled meander pos illegal",
                                ));
                            }
                        }
                        for w in n_poly.windows(2) {
                            if !legal_edge_with_width(
                                eng,
                                pair.neg_net,
                                li,
                                w[0],
                                w[1],
                                pair.width_nm,
                            )? {
                                return Err(Failure::new(
                                    Status::Unroutable,
                                    "coupled meander neg illegal",
                                ));
                            }
                        }
                        if let Some(imp) = &pair.impedance {
                            if pt_orig.layer != imp.signal_layer {
                                return Err(Failure::new(
                                    Status::Unroutable,
                                    "coupled meander outside ref layer",
                                ));
                            }
                            if p_poly.len() == n_poly.len() {
                                for (pw, nw) in p_poly.windows(2).zip(n_poly.windows(2)) {
                                    if !coupled_corridor_over_reference_zones(
                                        eng,
                                        pw[0],
                                        pw[1],
                                        nw[0],
                                        nw[1],
                                        pair.width_nm,
                                        imp,
                                    ) {
                                        return Err(Failure::new(
                                            Status::Unroutable,
                                            "coupled meander outside ref zone",
                                        ));
                                    }
                                }
                            } else {
                                for w in p_poly.windows(2) {
                                    if !segment_footprint_over_reference_zones(
                                        eng,
                                        w[0],
                                        w[1],
                                        pair.width_nm,
                                        imp,
                                    ) {
                                        return Err(Failure::new(
                                            Status::Unroutable,
                                            "coupled meander outside ref zone",
                                        ));
                                    }
                                }
                                for w in n_poly.windows(2) {
                                    if !segment_footprint_over_reference_zones(
                                        eng,
                                        w[0],
                                        w[1],
                                        pair.width_nm,
                                        imp,
                                    ) {
                                        return Err(Failure::new(
                                            Status::Unroutable,
                                            "coupled meander outside ref zone",
                                        ));
                                    }
                                }
                            }
                        }
                        if !meander_clean_clears_pads(eng, pair.pos_net, &pt_orig, &p_poly)
                            || !meander_clean_clears_pads(eng, pair.neg_net, &nt_orig, &n_poly)
                        {
                            return Err(Failure::new(
                                Status::Unroutable,
                                "coupled meander re-entered pad copper",
                            ));
                        }
                        // Verify coupled gap across all segment pairs (no -4nm slack, no equal-len gate)
                        let min_center_dist =
                            2 * i128::from(pair.width_nm) + 2 * i128::from(pair.gap_min_nm.max(0));
                        for pw in p_poly.windows(2) {
                            for nw in n_poly.windows(2) {
                                if geometry::segments_near(
                                    pw[0],
                                    pw[1],
                                    nw[0],
                                    nw[1],
                                    min_center_dist,
                                    false,
                                ) {
                                    return Err(Failure::new(
                                        Status::Unroutable,
                                        "coupled meander pinched gap below diff_pair_gap_min",
                                    ));
                                }
                            }
                        }
                        if !coupled_gap_max_ok(pair, &p_poly, &n_poly) {
                            return Err(Failure::new(
                                Status::Unroutable,
                                "coupled meander exceeded diff_pair_gap_max",
                            ));
                        }
                        if measure_pair_uncoupled_nm(eng, pair) > pair.uncoupled_max_nm {
                            return Err(Failure::new(
                                Status::Unroutable,
                                "coupled meander exceeded diff_pair_uncoupled limit",
                            ));
                        }
                        let mp2 = measure_net_fromto(eng, pair.pos_net, is_delay)?
                            .ok_or_else(|| Failure::new(Status::Unroutable, "pos disconnected"))?;
                        let mn2 = measure_net_fromto(eng, pair.neg_net, is_delay)?
                            .ok_or_else(|| Failure::new(Status::Unroutable, "neg disconnected"))?;
                        let vp = if is_delay {
                            mp2.delay_as
                        } else {
                            mp2.length_nm
                        };
                        let vn = if is_delay {
                            mn2.delay_as
                        } else {
                            mn2.length_nm
                        };
                        if (vp - target_val).abs() > tol
                            || (vn - target_val).abs() > tol
                            || (vp - vn).abs() > tol
                        {
                            return Err(Failure::new(
                                Status::Unroutable,
                                "coupled meander missed tolerance",
                            ));
                        }
                        Ok(())
                    });
                    if attempts.accepted(attempt)? {
                        return Ok(());
                    }
                }
            }
        }

        // Shallow coupled V-detour fallback for small length deltas below the 45-degree wave minimum.
        // Try symmetric alternating (+h, -h) V-detours first so outer/inner miter path differences cancel.
        let min_legal_h = (std::f64::consts::FRAC_1_SQRT_2 * (pair.width_nm as f64)
            + 0.5 * (s_nm as f64) * (std::f64::consts::SQRT_2 - 1.0)
            + 1_000.0)
            .max(10_000.0);
        let max_half_span_single = 0.5 * c_len - (s_nm as f64);
        let max_half_span_alt = 0.5 * (0.5 * c_len - 1.5 * (s_nm as f64));
        if max_amp >= min_legal_h && needed_len_nm > 0.0 && max_half_span_single > min_legal_h {
            let amp_levels = if (max_amp - min_legal_h).abs() < 1.0 {
                vec![min_legal_h]
            } else {
                vec![min_legal_h, 0.5 * (min_legal_h + max_amp), max_amp]
            };
            for alternating in [true, false] {
                let wave_work = if alternating { 4 } else { 2 };
                let max_half_span = if alternating {
                    max_half_span_alt
                } else {
                    max_half_span_single
                };
                for &amp_h in &amp_levels {
                    if max_half_span <= amp_h {
                        continue;
                    }
                    for &(nx, ny) in &[(-uy, ux), (uy, -ux)] {
                        let eval_coupled_v =
                            |half_span: f64| -> Option<(Vec<Point>, Vec<Point>, f64)> {
                                let pt_at = |t: f64, h: f64| -> Point {
                                    [
                                        c_start[0] + (t * ux + h * nx).round() as i64,
                                        c_start[1] + (t * uy + h * ny).round() as i64,
                                    ]
                                };
                                let full_c = if alternating {
                                    let gap_mid = s_nm as f64;
                                    let c1 = 0.5 * c_len - 0.5 * gap_mid - half_span;
                                    let c2 = 0.5 * c_len + 0.5 * gap_mid + half_span;
                                    let t0 = c1 - half_span;
                                    let t1 = c1 + half_span;
                                    let t2 = c2 - half_span;
                                    let t3 = c2 + half_span;
                                    if t0 < s_nm as f64 || t3 > c_len - (s_nm as f64) {
                                        return None;
                                    }
                                    vec![
                                        c_start,
                                        pt_at(t0, 0.0),
                                        pt_at(c1, amp_h),
                                        pt_at(t1, 0.0),
                                        pt_at(t2, 0.0),
                                        pt_at(c2, -amp_h),
                                        pt_at(t3, 0.0),
                                        c_end,
                                    ]
                                } else {
                                    let t0 = 0.5 * c_len - half_span;
                                    let t1 = 0.5 * c_len + half_span;
                                    if t0 < s_nm as f64 || t1 > c_len - (s_nm as f64) {
                                        return None;
                                    }
                                    vec![
                                        c_start,
                                        pt_at(t0, 0.0),
                                        pt_at(0.5 * c_len, amp_h),
                                        pt_at(t1, 0.0),
                                        c_end,
                                    ]
                                };
                                let (mut p_poly, mut n_poly) =
                                    offset_centerline_mitered(&full_c, s_nm, pos_on_left);
                                if p_poly.len() < 2 || n_poly.len() < 2 {
                                    return None;
                                }
                                *p_poly.first_mut().unwrap() = pt_orig.start;
                                *p_poly.last_mut().unwrap() = pt_orig.end;
                                *n_poly.first_mut().unwrap() = na;
                                *n_poly.last_mut().unwrap() = nb;
                                let p_new: f64 =
                                    p_poly.windows(2).map(|w| dist_f64(w[0], w[1])).sum();
                                let n_new: f64 =
                                    n_poly.windows(2).map(|w| dist_f64(w[0], w[1])).sum();
                                let avg_add = 0.5
                                    * ((p_new - dist_f64(pt_orig.start, pt_orig.end))
                                        + (n_new - dist_f64(na, nb)));
                                Some((p_poly, n_poly, avg_add))
                            };
                        engine.work.charge(wave_work)?;
                        let Some((_, _, add_max)) = eval_coupled_v(amp_h) else {
                            continue;
                        };
                        engine.work.charge(wave_work)?;
                        let Some((_, _, add_min)) = eval_coupled_v(max_half_span) else {
                            continue;
                        };
                        if needed_len_nm + (tol as f64) < add_min
                            || needed_len_nm - (tol as f64) > add_max
                        {
                            continue;
                        }
                        let mut lo_d = amp_h;
                        let mut hi_d = max_half_span;
                        for _ in 0..32 {
                            let mid_d = 0.5 * (lo_d + hi_d);
                            engine.work.charge(wave_work)?;
                            match eval_coupled_v(mid_d) {
                                Some((_, _, add)) if add > needed_len_nm => lo_d = mid_d,
                                _ => hi_d = mid_d,
                            }
                        }
                        let mid_d = 0.5 * (lo_d + hi_d);
                        engine.work.charge(3 * wave_work)?;
                        let best_coupled = [lo_d, mid_d, hi_d]
                            .into_iter()
                            .filter_map(eval_coupled_v)
                            .min_by(|(_, _, add_a), (_, _, add_b)| {
                                (add_a - needed_len_nm)
                                    .abs()
                                    .partial_cmp(&(add_b - needed_len_nm).abs())
                                    .unwrap_or(std::cmp::Ordering::Equal)
                            });
                        let Some((p_poly, n_poly, _)) = best_coupled else {
                            continue;
                        };
                        let attempt = engine.transaction(|tx| {
                            let eng = &mut *tx.engine;
                            replace_generated_track_with_pts(eng, pair.pos_net, &pt_orig, &p_poly)?;
                            replace_generated_track_with_pts(eng, pair.neg_net, &nt_orig, &n_poly)?;
                            for w in p_poly.windows(2) {
                                if !legal_edge_with_width(
                                    eng,
                                    pair.pos_net,
                                    li,
                                    w[0],
                                    w[1],
                                    pair.width_nm,
                                )? {
                                    return Err(Failure::new(
                                        Status::Unroutable,
                                        "coupled meander pos illegal",
                                    ));
                                }
                            }
                            for w in n_poly.windows(2) {
                                if !legal_edge_with_width(
                                    eng,
                                    pair.neg_net,
                                    li,
                                    w[0],
                                    w[1],
                                    pair.width_nm,
                                )? {
                                    return Err(Failure::new(
                                        Status::Unroutable,
                                        "coupled meander neg illegal",
                                    ));
                                }
                            }
                            if let Some(imp) = &pair.impedance {
                                if pt_orig.layer != imp.signal_layer {
                                    return Err(Failure::new(
                                        Status::Unroutable,
                                        "coupled meander outside ref layer",
                                    ));
                                }
                                if p_poly.len() == n_poly.len() {
                                    for (pw, nw) in p_poly.windows(2).zip(n_poly.windows(2)) {
                                        if !coupled_corridor_over_reference_zones(
                                            eng,
                                            pw[0],
                                            pw[1],
                                            nw[0],
                                            nw[1],
                                            pair.width_nm,
                                            imp,
                                        ) {
                                            return Err(Failure::new(
                                                Status::Unroutable,
                                                "coupled meander outside ref zone",
                                            ));
                                        }
                                    }
                                } else {
                                    for w in p_poly.windows(2) {
                                        if !segment_footprint_over_reference_zones(
                                            eng,
                                            w[0],
                                            w[1],
                                            pair.width_nm,
                                            imp,
                                        ) {
                                            return Err(Failure::new(
                                                Status::Unroutable,
                                                "coupled meander outside ref zone",
                                            ));
                                        }
                                    }
                                    for w in n_poly.windows(2) {
                                        if !segment_footprint_over_reference_zones(
                                            eng,
                                            w[0],
                                            w[1],
                                            pair.width_nm,
                                            imp,
                                        ) {
                                            return Err(Failure::new(
                                                Status::Unroutable,
                                                "coupled meander outside ref zone",
                                            ));
                                        }
                                    }
                                }
                            }
                            if !meander_clean_clears_pads(eng, pair.pos_net, &pt_orig, &p_poly)
                                || !meander_clean_clears_pads(eng, pair.neg_net, &nt_orig, &n_poly)
                            {
                                return Err(Failure::new(
                                    Status::Unroutable,
                                    "coupled meander re-entered pad copper",
                                ));
                            }
                            let min_center_dist = 2 * i128::from(pair.width_nm)
                                + 2 * i128::from(pair.gap_min_nm.max(0));
                            for pw in p_poly.windows(2) {
                                for nw in n_poly.windows(2) {
                                    if geometry::segments_near(
                                        pw[0],
                                        pw[1],
                                        nw[0],
                                        nw[1],
                                        min_center_dist,
                                        false,
                                    ) {
                                        return Err(Failure::new(
                                            Status::Unroutable,
                                            "coupled meander pinched gap below diff_pair_gap_min",
                                        ));
                                    }
                                }
                            }
                            if !coupled_gap_max_ok(pair, &p_poly, &n_poly) {
                                return Err(Failure::new(
                                    Status::Unroutable,
                                    "coupled meander exceeded diff_pair_gap_max",
                                ));
                            }
                            if measure_pair_uncoupled_nm(eng, pair) > pair.uncoupled_max_nm {
                                return Err(Failure::new(
                                    Status::Unroutable,
                                    "coupled meander exceeded diff_pair_uncoupled limit",
                                ));
                            }
                            let mp2 = measure_net_fromto(eng, pair.pos_net, is_delay)?.ok_or_else(
                                || Failure::new(Status::Unroutable, "pos disconnected"),
                            )?;
                            let mn2 = measure_net_fromto(eng, pair.neg_net, is_delay)?.ok_or_else(
                                || Failure::new(Status::Unroutable, "neg disconnected"),
                            )?;
                            let vp = if is_delay {
                                mp2.delay_as
                            } else {
                                mp2.length_nm
                            };
                            let vn = if is_delay {
                                mn2.delay_as
                            } else {
                                mn2.length_nm
                            };
                            if (vp - target_val).abs() > tol
                                || (vn - target_val).abs() > tol
                                || (vp - vn).abs() > tol
                            {
                                return Err(Failure::new(
                                    Status::Unroutable,
                                    "coupled meander missed tolerance",
                                ));
                            }
                            Ok(())
                        });
                        if attempts.accepted(attempt)? {
                            return Ok(());
                        }
                    }
                }
            }
        }
    }

    Err(attempts.failure(Failure::new(
        Status::Unroutable,
        format!(
            "unable to match coupled pair {} to target {target_val}",
            pair.stem
        ),
    )))
}

fn apply_intra_pair_match(
    engine: &mut Engine,
    pair: &ResolvedDiffPair,
    tol_nm: i64,
    amp_max_nm: i64,
) -> Result<(), Failure> {
    let mp = measure_net_fromto(engine, pair.pos_net, false)?
        .ok_or_else(|| Failure::new(Status::Unroutable, "pos disconnected"))?;
    let mn = measure_net_fromto(engine, pair.neg_net, false)?
        .ok_or_else(|| Failure::new(Status::Unroutable, "neg disconnected"))?;
    let diff = (mp.length_nm - mn.length_nm).abs();
    if diff <= tol_nm {
        return Ok(());
    }
    let (short_net, long_net, short_len, long_len) = if mp.length_nm < mn.length_nm {
        (pair.pos_net, pair.neg_net, mp.length_nm, mn.length_nm)
    } else {
        (pair.neg_net, pair.pos_net, mn.length_nm, mp.length_nm)
    };
    match_net_in_pair_to_target(
        engine, pair, short_net, long_net, short_len, long_len, tol_nm, amp_max_nm,
    )
}

#[allow(clippy::too_many_arguments)]
fn match_net_in_pair_to_target(
    engine: &mut Engine,
    pair: &ResolvedDiffPair,
    short_net: u32,
    long_net: u32,
    short_len: i64,
    target_len: i64,
    tol_nm: i64,
    amp_max_nm: i64,
) -> Result<(), Failure> {
    if (short_len - target_len).abs() <= tol_nm {
        return Ok(());
    }
    if measure_pair_uncoupled_nm(engine, pair) > pair.uncoupled_max_nm {
        return Err(Failure::new(
            Status::Unroutable,
            format!(
                "diff pair {} exceeds diff_pair_uncoupled limit before intra-pair match",
                pair.stem
            ),
        ));
    }
    let needed = (target_len - short_len) as f64;
    if needed <= 0.0 {
        return Err(Failure::new(
            Status::Unroutable,
            format!(
                "diff pair {} leg net {short_net} already exceeds target {target_len}",
                pair.stem
            ),
        ));
    }
    let pad_pts: Vec<Point> = engine
        .input
        .pads
        .iter()
        .filter(|p| p.net == short_net)
        .map(|p| p.at)
        .collect();
    let other_mids: Vec<Point> = engine
        .copper
        .tracks()
        .filter(|t| t.net == long_net)
        .map(|t| [(t.start[0] + t.end[0]) / 2, (t.start[1] + t.end[1]) / 2])
        .collect();
    let pad_segs: Vec<Track> = engine
        .copper
        .generated()
        .filter(|t| t.net == short_net && (pad_pts.contains(&t.start) || pad_pts.contains(&t.end)))
        .filter(|t| {
            pair.impedance.is_none()
                || is_pad_connected_uncoupled_escape(engine, pair, short_net, t)
        })
        .cloned()
        .collect();
    let max_h = amp_max_nm.min(pair.uncoupled_max_nm / 2) as f64;

    let mut attempts = MeanderAttempts::default();
    for cand in &pad_segs {
        let Some(li) = engine.layer_index(&cand.layer) else {
            continue;
        };
        let (p_at, p_join) = if pad_pts.contains(&cand.start) {
            (cand.start, cand.end)
        } else {
            (cand.end, cand.start)
        };
        let seg_len = dist_f64(p_at, p_join);
        let Some((ux, uy)) = dir_unit_f64(p_at, p_join) else {
            continue;
        };
        if max_h <= 10_000.0 || seg_len < ((3 * cand.width_nm).max(300_000)) as f64 {
            continue;
        }
        // Rank outward normal first (away from partner tracks)
        let mid = [(p_at[0] + p_join[0]) / 2, (p_at[1] + p_join[1]) / 2];
        let mut normals = [(-uy, ux), (uy, -ux)];
        if let Some(near_other) = other_mids.iter().min_by(|a, b| {
            dist_f64(mid, **a)
                .partial_cmp(&dist_f64(mid, **b))
                .unwrap_or(std::cmp::Ordering::Equal)
        }) {
            let to_other = (
                (near_other[0] - mid[0]) as f64,
                (near_other[1] - mid[1]) as f64,
            );
            if normals[0].0 * to_other.0 + normals[0].1 * to_other.1
                > normals[1].0 * to_other.0 + normals[1].1 * to_other.1
            {
                normals.swap(0, 1);
            }
        }

        let eval_pad_bump = |h: f64, nx: f64, ny: f64| -> (Vec<Point>, f64) {
            // Tilted V-peak directly from pad.at to p_join avoids creating parallel uncoupled segments
            // that KiCad 10 DRC flags as diff_pair_gap_out_of_range.
            let pts = vec![
                p_at,
                [
                    mid[0] + (h * nx).round() as i64,
                    mid[1] + (h * ny).round() as i64,
                ],
                p_join,
            ];
            let clean = simplify_collinear_points(pts);
            let new_len: f64 = clean.windows(2).map(|w| dist_f64(w[0], w[1])).sum();
            (clean, new_len - seg_len)
        };

        let other_same_net: Vec<Track> = engine
            .copper
            .generated()
            .chain(engine.input.tracks.iter())
            .filter(|t| {
                t.net == short_net
                    && !(t.layer == cand.layer
                        && t.width_nm == cand.width_nm
                        && ((t.start == cand.start && t.end == cand.end)
                            || (t.start == cand.end && t.end == cand.start)))
            })
            .cloned()
            .collect();

        for &(nx, ny) in &normals {
            engine.work.charge(1)?;
            let (_, max_add) = eval_pad_bump(max_h, nx, ny);
            if max_add + (tol_nm as f64) < needed {
                continue;
            }
            let mut lo = 10_000.0_f64.min(max_h);
            let mut hi = max_h;
            for _ in 0..32 {
                let mid_h = 0.5 * (lo + hi);
                engine.work.charge(1)?;
                if eval_pad_bump(mid_h, nx, ny).1 < needed {
                    lo = mid_h;
                } else {
                    hi = mid_h;
                }
            }
            engine.work.charge(1)?;
            let (clean, _) = eval_pad_bump(0.5 * (lo + hi), nx, ny);
            if !same_net_meander_clear(cand, &clean, &other_same_net, 50_000) {
                continue;
            }
            let attempt = engine.transaction(|tx| {
                let eng = &mut *tx.engine;
                replace_generated_track_with_pts(eng, short_net, cand, &clean)?;
                for w in clean.windows(2) {
                    if !legal_edge_with_width(eng, short_net, li, w[0], w[1], cand.width_nm)? {
                        return Err(Failure::new(Status::Unroutable, "pad detour illegal"));
                    }
                    if let Some(imp) = &pair.impedance {
                        if cand.layer == imp.signal_layer
                            && !segment_footprint_over_reference_zones(
                                eng,
                                w[0],
                                w[1],
                                cand.width_nm,
                                imp,
                            )
                        {
                            return Err(Failure::new(
                                Status::Unroutable,
                                "pad detour left impedance reference zone",
                            ));
                        }
                    }
                }
                if !meander_clean_clears_pads(eng, short_net, cand, &clean) {
                    return Err(Failure::new(
                        Status::Unroutable,
                        "pad detour re-entered pad copper",
                    ));
                }
                if measure_pair_uncoupled_nm(eng, pair) > pair.uncoupled_max_nm {
                    return Err(Failure::new(
                        Status::Unroutable,
                        "pad detour exceeded diff_pair_uncoupled limit",
                    ));
                }
                let m = measure_net_fromto(eng, short_net, false)?
                    .ok_or_else(|| Failure::new(Status::Unroutable, "disconnected"))?;
                if (m.length_nm - target_len).abs() > tol_nm {
                    return Err(Failure::new(Status::Unroutable, "pad detour missed tol"));
                }
                Ok(())
            });
            if attempts.accepted(attempt)? {
                return Ok(());
            }
        }
    }

    let spacing_widths = 2.5;
    apply_single_ended_meander(
        engine,
        short_net,
        target_len,
        tol_nm,
        false,
        amp_max_nm.min(pair.uncoupled_max_nm / 2),
        spacing_widths,
        true,
        Some(pair),
    )
    .map_err(|f| attempts.failure(f))
}

fn apply_ac_chain_match(
    engine: &mut Engine,
    plan: &DiffPairPlan,
    chain: &ResolvedAcChain,
) -> Result<(), Failure> {
    let tol_nm = plan.ac_match_tolerance_nm;
    let amp_nm = plan.ac_meander_amplitude_nm;
    let is_modifiable = |eng: &Engine, nid: u32| -> bool {
        !plan.protected_nets.contains(&nid) && !eng.copper.input().iter().any(|t| t.net == nid)
    };

    for &p_idx in &chain.pair_indices {
        let pair = &plan.pairs[p_idx];
        if let Some(d) = pair.dru_skew_max_nm {
            let mp = measure_net_fromto(engine, pair.pos_net, false)?
                .ok_or_else(|| Failure::new(Status::Unroutable, "AC chain pos disconnected"))?;
            let mn = measure_net_fromto(engine, pair.neg_net, false)?
                .ok_or_else(|| Failure::new(Status::Unroutable, "AC chain neg disconnected"))?;
            if (mp.length_nm - mn.length_nm).abs() > d {
                let (short_net, long_net, short_len, long_len) = if mp.length_nm < mn.length_nm {
                    (pair.pos_net, pair.neg_net, mp.length_nm, mn.length_nm)
                } else {
                    (pair.neg_net, pair.pos_net, mn.length_nm, mp.length_nm)
                };
                if !is_modifiable(engine, short_net) {
                    return Err(Failure::new(
                        Status::Unroutable,
                        format!(
                            "protected/existing net {short_net} in AC pair {} exceeds DRU skew",
                            pair.stem
                        ),
                    ));
                }
                let half_d = (d / 2).max(1);
                match_net_in_pair_to_target(
                    engine,
                    pair,
                    short_net,
                    long_net,
                    short_len,
                    long_len - (d / 2),
                    half_d,
                    amp_nm,
                )?;
            }
        }
    }

    let measure_chain = |eng: &Engine| -> Result<(i64, i64), Failure> {
        let mut sum_p = 0i64;
        let mut sum_n = 0i64;
        for &p_idx in &chain.pair_indices {
            let pair = &plan.pairs[p_idx];
            let mp = measure_net_fromto(eng, pair.pos_net, false)?
                .ok_or_else(|| Failure::new(Status::Unroutable, "AC chain pos disconnected"))?;
            let mn = measure_net_fromto(eng, pair.neg_net, false)?
                .ok_or_else(|| Failure::new(Status::Unroutable, "AC chain neg disconnected"))?;
            sum_p += mp.length_nm;
            sum_n += mn.length_nm;
        }
        Ok((sum_p, sum_n))
    };

    let mut attempts = MeanderAttempts::default();
    let (mut sum_p, mut sum_n) = measure_chain(engine)?;
    if (sum_p - sum_n).abs() > tol_nm {
        let p_is_short = sum_p < sum_n;
        let rem = (sum_p - sum_n).abs();
        let mut cands: Vec<(usize, u32, u32, i64, i64, i64)> = Vec::new();
        for &p_idx in &chain.pair_indices {
            let pair = &plan.pairs[p_idx];
            let (short_net, long_net) = if p_is_short {
                (pair.pos_net, pair.neg_net)
            } else {
                (pair.neg_net, pair.pos_net)
            };
            if !is_modifiable(engine, short_net) {
                continue;
            }
            let ms = measure_net_fromto(engine, short_net, false)?
                .ok_or_else(|| Failure::new(Status::Unroutable, "AC chain short disconnected"))?;
            let ml = measure_net_fromto(engine, long_net, false)?
                .ok_or_else(|| Failure::new(Status::Unroutable, "AC chain long disconnected"))?;
            let max_allowed = pair
                .dru_skew_max_nm
                .map_or(i64::MAX / 4, |d| ml.length_nm + d);
            cands.push((
                p_idx,
                short_net,
                long_net,
                ms.length_nm,
                ml.length_nm,
                max_allowed,
            ));
        }
        cands.sort_by(|a, b| (b.4 - b.3).cmp(&(a.4 - a.3)));

        let mut solved_single = false;
        for &(p_idx, short_net, long_net, len_s, _len_l, max_allowed) in &cands {
            let lo = (len_s + rem - tol_nm).max(len_s + 1);
            let hi = (len_s + rem + tol_nm).min(max_allowed);
            if lo > hi {
                continue;
            }
            let pair = &plan.pairs[p_idx];
            let target = (lo + hi) / 2;
            let eff_tol = (hi - lo) / 2;
            let attempt = engine.transaction(|tx| {
                match_net_in_pair_to_target(
                    tx.engine, pair, short_net, long_net, len_s, target, eff_tol, amp_nm,
                )
            });
            if attempts.accepted(attempt)? {
                solved_single = true;
                break;
            }
        }

        if !solved_single {
            cands.sort_by(|a, b| (b.5 - b.3).cmp(&(a.5 - a.3)));
            for _pass in 0..2 {
                (sum_p, sum_n) = measure_chain(engine)?;
                let cur_rem = (sum_p - sum_n).abs();
                if cur_rem <= tol_nm {
                    break;
                }
                let cur_p_short = sum_p < sum_n;
                for &(p_idx, _, _, _, _, _) in &cands {
                    (sum_p, sum_n) = measure_chain(engine)?;
                    let cur_rem = (sum_p - sum_n).abs();
                    if cur_rem <= tol_nm || (sum_p < sum_n) != cur_p_short {
                        break;
                    }
                    let pair = &plan.pairs[p_idx];
                    let (short_net, long_net) = if cur_p_short {
                        (pair.pos_net, pair.neg_net)
                    } else {
                        (pair.neg_net, pair.pos_net)
                    };
                    if !is_modifiable(engine, short_net) {
                        continue;
                    }
                    let len_s = measure_net_fromto(engine, short_net, false)?
                        .ok_or_else(|| {
                            Failure::new(Status::Unroutable, "AC chain short disconnected")
                        })?
                        .length_nm;
                    let len_l = measure_net_fromto(engine, long_net, false)?
                        .ok_or_else(|| {
                            Failure::new(Status::Unroutable, "AC chain long disconnected")
                        })?
                        .length_nm;
                    let max_allowed = pair.dru_skew_max_nm.map_or(i64::MAX / 4, |d| len_l + d);
                    let headroom = max_allowed - len_s;
                    if headroom <= 0 {
                        continue;
                    }
                    let add = cur_rem.min(headroom);
                    let lo = (len_s + add - tol_nm).max(len_s + 1);
                    let hi = (len_s + add + tol_nm).min(max_allowed);
                    if lo > hi {
                        continue;
                    }
                    let target = (lo + hi) / 2;
                    let eff_tol = ((hi - lo) / 2).max(1);
                    let attempt = engine.transaction(|tx| {
                        match_net_in_pair_to_target(
                            tx.engine, pair, short_net, long_net, len_s, target, eff_tol, amp_nm,
                        )
                    });
                    attempts.accepted(attempt)?;
                }
            }
        }
    }

    let (final_p, final_n) = measure_chain(engine)?;
    if (final_p - final_n).abs() > tol_nm {
        return Err(attempts.failure(Failure::new(
            Status::Unroutable,
            format!(
                "AC chain {:?} composed skew {} nm exceeds tolerance {tol_nm} nm",
                chain.bridges,
                (final_p - final_n).abs()
            ),
        )));
    }
    for &p_idx in &chain.pair_indices {
        let pair = &plan.pairs[p_idx];
        if let Some(d) = pair.dru_skew_max_nm {
            let mp = measure_net_fromto(engine, pair.pos_net, false)?
                .ok_or_else(|| Failure::new(Status::Unroutable, "AC chain pos disconnected"))?;
            let mn = measure_net_fromto(engine, pair.neg_net, false)?
                .ok_or_else(|| Failure::new(Status::Unroutable, "AC chain neg disconnected"))?;
            if (mp.length_nm - mn.length_nm).abs() > d {
                return Err(Failure::new(
                    Status::Unroutable,
                    format!(
                        "AC chain pair {} skew {} nm exceeds DRU skew limit {d} nm",
                        pair.stem,
                        (mp.length_nm - mn.length_nm).abs()
                    ),
                ));
            }
        }
    }
    Ok(())
}

pub fn apply_length_matching_in_tx(
    engine: &mut Engine,
    state: &DiffPairRunState,
) -> Result<(), Failure> {
    if state.plan.pairs.is_empty()
        && state.plan.length_groups.is_empty()
        && state.plan.ac_chains.is_empty()
    {
        return Ok(());
    }
    let plan = &state.plan;
    let mut pair_by_net: BTreeMap<u32, usize> = BTreeMap::new();
    for (idx, p) in plan.pairs.iter().enumerate() {
        pair_by_net.insert(p.pos_net, idx);
        pair_by_net.insert(p.neg_net, idx);
    }
    let mut ac_pair_indices: BTreeSet<usize> = BTreeSet::new();
    for ch in &plan.ac_chains {
        ac_pair_indices.extend(ch.pair_indices.iter().copied());
    }

    unwrap_tx(engine.transaction(|tx| {
        let eng = &mut *tx.engine;

        for pair in &plan.pairs {
            if ac_pair_indices.contains(&pair.index) {
                continue;
            }
            if !plan.protected_nets.contains(&pair.pos_net)
                && !plan.protected_nets.contains(&pair.neg_net)
            {
                let eff_tol = match (plan.intra_match, pair.dru_skew_max_nm) {
                    (true, Some(d)) => Some(plan.intra_tolerance_nm.min(d)),
                    (true, None) => Some(plan.intra_tolerance_nm),
                    (false, Some(d)) => Some(d),
                    (false, None) => None,
                };
                if let Some(tol_nm) = eff_tol {
                    apply_intra_pair_match(eng, pair, tol_nm, plan.intra_amplitude_nm)?;
                }
            }
        }

        for chain in &plan.ac_chains {
            apply_ac_chain_match(eng, plan, chain)?;
        }

        for group in &plan.length_groups {
            let (is_delay, tol) = match group.mode {
                GroupTargetMode::LengthNm { tolerance_nm } => (false, tolerance_nm),
                GroupTargetMode::DelayAs { tolerance_as } => (true, tolerance_as),
            };
            let mut vals: Vec<(u32, i64, bool)> = Vec::with_capacity(group.members.len());
            let mut max_val = i64::MIN;
            let mut min_val = i64::MAX;
            for &nid in &group.members {
                let m = measure_net_fromto(eng, nid, is_delay)?.ok_or_else(|| {
                    Failure::new(
                        Status::Unroutable,
                        format!("length-match group member net {nid} is disconnected"),
                    )
                })?;
                let v = if is_delay { m.delay_as } else { m.length_nm };
                let has_input = eng.copper.input().iter().any(|t| t.net == nid);
                let modifiable = !plan.protected_nets.contains(&nid) && !has_input;
                max_val = max_val.max(v);
                min_val = min_val.min(v);
                vals.push((nid, v, modifiable));
            }

            if max_val - min_val <= tol {
                continue;
            }

            let mut target = max_val;
            if group.contains_diff_pairs {
                // Option B: bring skewed pairs in a coupled group (including 1-pair coupled groups)
                // within `tol` first so coupled meanders preserve P/N skew balance.
                let mut intra_handled = BTreeSet::new();
                for &(nid, _, _) in &vals {
                    let Some(&p_idx) = pair_by_net.get(&nid) else {
                        continue;
                    };
                    if !intra_handled.insert(p_idx) {
                        continue;
                    }
                    let pair = &plan.pairs[p_idx];
                    let mp = measure_net_fromto(eng, pair.pos_net, false)?.ok_or_else(|| {
                        Failure::new(Status::Unroutable, "pos leg disconnected in group")
                    })?;
                    let mn = measure_net_fromto(eng, pair.neg_net, false)?.ok_or_else(|| {
                        Failure::new(Status::Unroutable, "neg leg disconnected in group")
                    })?;
                    if (mp.length_nm - mn.length_nm).abs() > tol {
                        let pair_modifiable = !plan.protected_nets.contains(&pair.pos_net)
                            && !plan.protected_nets.contains(&pair.neg_net)
                            && !eng
                                .copper
                                .input()
                                .iter()
                                .any(|t| t.net == pair.pos_net || t.net == pair.neg_net);
                        if !pair_modifiable {
                            return Err(Failure::new(
                                Status::Unroutable,
                                format!(
                                    "protected/existing diff pair {} skew exceeds group tolerance {tol}",
                                    pair.stem
                                ),
                            ));
                        }
                        apply_intra_pair_match(eng, pair, tol, group.amplitude_nm)?;
                    }
                }
                let mut refreshed_max = i64::MIN;
                for &nid in &group.members {
                    let m = measure_net_fromto(eng, nid, is_delay)?.ok_or_else(|| {
                        Failure::new(
                            Status::Unroutable,
                            format!("length-match group member net {nid} is disconnected"),
                        )
                    })?;
                    let v = if is_delay { m.delay_as } else { m.length_nm };
                    refreshed_max = refreshed_max.max(v);
                }
                target = refreshed_max;

                let mut handled_pairs = BTreeSet::new();
                for &(nid, v, modifiable) in &vals {
                    if let Some(&p_idx) = pair_by_net.get(&nid) {
                        if !handled_pairs.insert(p_idx) {
                            continue;
                        }
                        let pair = &plan.pairs[p_idx];
                        let pair_modifiable = !plan.protected_nets.contains(&pair.pos_net)
                            && !plan.protected_nets.contains(&pair.neg_net)
                            && !eng
                                .copper
                                .input()
                                .iter()
                                .any(|t| t.net == pair.pos_net || t.net == pair.neg_net);
                        if pair_modifiable {
                            apply_coupled_pair_meander(
                                eng,
                                pair,
                                target,
                                tol,
                                is_delay,
                                group.amplitude_nm,
                            )?;
                        }
                    } else if modifiable && v < target - tol {
                        apply_single_ended_meander(
                            eng,
                            nid,
                            target,
                            tol,
                            is_delay,
                            group.amplitude_nm,
                            group.spacing_widths,
                            false,
                            None,
                        )?;
                    }
                }
            } else {
                for &(nid, v, modifiable) in &vals {
                    if v < target - tol {
                        if !modifiable {
                            return Err(Failure::new(
                                Status::Unroutable,
                                format!(
                                    "protected/existing net {nid} cannot be meandered to match target {target}"
                                ),
                            ));
                        }
                        apply_single_ended_meander(
                            eng,
                            nid,
                            target,
                            tol,
                            is_delay,
                            group.amplitude_nm,
                            group.spacing_widths,
                            false,
                            None,
                        )?;
                    }
                }
            }

            // Independent final group skew verification
            let mut final_min = i64::MAX;
            let mut final_max = i64::MIN;
            for &nid in &group.members {
                let m = measure_net_fromto(eng, nid, is_delay)?.ok_or_else(|| {
                    Failure::new(
                        Status::Unroutable,
                        format!("length-match group member net {nid} disconnected after matching"),
                    )
                })?;
                let v = if is_delay { m.delay_as } else { m.length_nm };
                final_min = final_min.min(v);
                final_max = final_max.max(v);
            }
            if final_max - final_min > tol {
                return Err(Failure::new(
                    Status::Unroutable,
                    format!(
                        "length-match group skew {} exceeds tolerance {tol}",
                        final_max - final_min
                    ),
                ));
            }
        }
        Ok(())
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn measure_net_fromto_use_height_false_ignores_via_overrides_and_45_offcenter_overshoot() {
        let uid = |n: u64| format!("00000000-0000-4000-8000-{n:012}");
        let pro = serde_json::json!({
            "meta": {"version": 3},
            "board": {"design_settings": {"rules": {"use_height_for_length_calcs": false}}},
            "net_settings": {"classes": [{"name": "Default", "tuning_profile": "P1"}]},
            "tuning_profiles": {
                "tuning_profiles_impedance_geometric": [{
                    "profile_name": "P1",
                    "type": 0,
                    "enable_time_domain_tuning": true,
                    "via_prop_delay": 10_000_000,
                    "layer_entries": [
                        {"signal_layer": "F.Cu", "width": 250_000, "delay": 5_000_000},
                        {"signal_layer": "B.Cu", "width": 250_000, "delay": 8_000_000}
                    ],
                    "via_overrides": [{
                        "signal_layer_from": "F.Cu",
                        "signal_layer_to": "B.Cu",
                        "via_layer_from": "F.Cu",
                        "via_layer_to": "B.Cu",
                        "delay": 90_000_000
                    }]
                }]
            }
        })
        .to_string();
        let mut inp: Input = serde_json::from_value(serde_json::json!({
            "version": 1,
            "source_id": "e".repeat(64),
            "layer": "F.Cu",
            "layers": ["F.Cu", "B.Cu"],
            "bounds": {"min": [0, 0], "max": [50_000_000, 50_000_000]},
            "nets": [{"id": 0, "name": ""}, {"id": 1, "name": "SIG"}],
            "pads": [
                {"id": uid(1), "net": 1, "at": [10_000_000, 20_000_000], "shape": {"kind": "rect", "half_size_nm": [150_000, 150_000]}, "layers": ["F.Cu"]},
                {"id": uid(2), "net": 1, "at": [30_000_000, 20_550_000], "shape": {"kind": "rect", "half_size_nm": [150_000, 150_000]}, "layers": ["B.Cu"]}
            ],
            "tracks": [
                {"id": uid(10), "net": 1, "start": [10_000_000, 20_000_000], "end": [19_500_000, 20_000_000], "width_nm": 250_000, "locked": false, "layer": "F.Cu"},
                {"id": uid(11), "net": 1, "start": [19_500_000, 20_000_000], "end": [20_100_000, 20_600_000], "width_nm": 250_000, "locked": false, "layer": "F.Cu"},
                {"id": uid(12), "net": 1, "start": [19_900_000, 20_550_000], "end": [30_000_000, 20_550_000], "width_nm": 250_000, "locked": false, "layer": "B.Cu"}
            ],
            "vias": [
                {"id": uid(20), "net": 1, "at": [19_900_000, 20_550_000], "diameter_nm": 600_000, "drill_nm": 300_000, "locked": false, "start_layer": "F.Cu", "end_layer": "B.Cu", "kind": "through"}
            ],
            "rules": {
                "grid_nm": 100_000, "width_nm": 250_000, "clearance_nm": 200_000,
                "edge_clearance_nm": 200_000, "hole_clearance_nm": 200_000, "same_net_hole_clearance_nm": 200_000
            },
            "max_expansions": 50_000,
            "board_design_settings": {
                "use_height_for_length_calcs": false,
                "netclasses": [{"name": "Default", "tuning_profile": "P1"}]
            },
            "project_rules": pro,
            "stackup": [
                {"name": "F.Cu", "kind": "copper", "thickness_nm": 35_000},
                {"name": "di", "kind": "core", "thickness_nm": 1_530_000, "epsilon_r": 4.0},
                {"name": "B.Cu", "kind": "copper", "thickness_nm": 35_000}
            ]
        }))
        .unwrap();
        crate::rules::resolve_effective_rules(&mut inp).unwrap();
        let eng = Engine::new(inp).unwrap();
        let m = measure_net_fromto(&eng, 1, true).unwrap().unwrap();
        assert!(
            (m.length_nm - 20_291_128).abs() <= 50,
            "expected ~20291128 nm, got {}",
            m.length_nm
        );
        assert!(
            (m.delay_as - 131_755_640).abs() <= 5_000,
            "expected ~131755640 as with 0 via delay, got {}",
            m.delay_as
        );
    }

    fn ac_chain_base_input() -> Input {
        let uid = |n: u64| format!("00000000-0000-4000-8000-{n:012}");
        let mut inp: Input = serde_json::from_value(serde_json::json!({
            "version": 1,
            "source_id": "a".repeat(64),
            "layer": "F.Cu",
            "layers": ["F.Cu", "B.Cu"],
            "bounds": {"min": [0, 0], "max": [75_000_000, 20_000_000]},
            "nets": [
                {"id": 0, "name": ""},
                {"id": 1, "name": "DPA_P"},
                {"id": 2, "name": "DPA_N"},
                {"id": 3, "name": "DPB_P"},
                {"id": 4, "name": "DPB_N"},
                {"id": 5, "name": "SE"}
            ],
            "pads": [
                {"id": uid(1), "net": 1, "at": [5_000_000, 10_000_000], "shape": {"kind": "rect", "half_size_nm": [200_000, 200_000]}, "layers": ["F.Cu"], "footprint_ref": "J1", "pad_number": "1"},
                {"id": uid(2), "net": 2, "at": [5_000_000, 12_540_000], "shape": {"kind": "rect", "half_size_nm": [200_000, 200_000]}, "layers": ["F.Cu"], "footprint_ref": "J1", "pad_number": "2"},
                {"id": uid(3), "net": 1, "at": [34_520_000, 8_000_000], "shape": {"kind": "rect", "half_size_nm": [200_000, 200_000]}, "layers": ["F.Cu"], "footprint_ref": "C1", "pad_number": "1"},
                {"id": uid(4), "net": 3, "at": [35_480_000, 8_000_000], "shape": {"kind": "rect", "half_size_nm": [200_000, 200_000]}, "layers": ["F.Cu"], "footprint_ref": "C1", "pad_number": "2"},
                {"id": uid(5), "net": 2, "at": [34_520_000, 12_000_000], "shape": {"kind": "rect", "half_size_nm": [200_000, 200_000]}, "layers": ["F.Cu"], "footprint_ref": "C2", "pad_number": "1"},
                {"id": uid(6), "net": 4, "at": [35_480_000, 12_000_000], "shape": {"kind": "rect", "half_size_nm": [200_000, 200_000]}, "layers": ["F.Cu"], "footprint_ref": "C2", "pad_number": "2"},
                {"id": uid(7), "net": 3, "at": [65_000_000, 10_000_000], "shape": {"kind": "rect", "half_size_nm": [200_000, 200_000]}, "layers": ["F.Cu"], "footprint_ref": "J2", "pad_number": "1"},
                {"id": uid(8), "net": 4, "at": [65_000_000, 12_540_000], "shape": {"kind": "rect", "half_size_nm": [200_000, 200_000]}, "layers": ["F.Cu"], "footprint_ref": "J2", "pad_number": "2"}
            ],
            "tracks": [],
            "vias": [],
            "components": [
                {"ref_des": "J1", "pad_numbers": ["1", "2"], "net_ids": [1, 2]},
                {"ref_des": "J2", "pad_numbers": ["1", "2"], "net_ids": [3, 4]},
                {"ref_des": "C1", "pad_numbers": ["1", "2"], "net_ids": [1, 3]},
                {"ref_des": "C2", "pad_numbers": ["1", "2"], "net_ids": [2, 4]}
            ],
            "rules": {
                "grid_nm": 100_000, "width_nm": 150_000, "clearance_nm": 150_000,
                "edge_clearance_nm": 200_000, "hole_clearance_nm": 200_000, "same_net_hole_clearance_nm": 0
            },
            "max_expansions": 100_000,
            "board_design_settings": {
                "min_track_width_nm": 100_000,
                "min_clearance_nm": 150_000,
                "netclasses": [{
                    "name": "Default",
                    "track_width_nm": 150_000,
                    "clearance_nm": 150_000,
                    "diff_pair_width_nm": 150_000,
                    "diff_pair_gap_nm": 250_000
                }]
            },
            "routing_options": {
                "diff_pairs_mode": "auto",
                "pair_layers": ["F.Cu"],
                "ac_couple_chains": [["C1", "C2"]],
                "ac_match_tolerance_nm": 100_000,
                "ac_meander_amplitude_nm": 3_000_000
            }
        }))
        .unwrap();
        crate::rules::resolve_effective_rules(&mut inp).unwrap();
        inp
    }

    #[test]
    fn ac_chain_validation_refuses_invalid_topologies_and_flags() {
        let expect_invalid = |inp: Input, needle: &str| {
            let eng = Engine::new(inp).unwrap();
            let err = plan_diff_pairs(&eng).unwrap_err();
            assert_eq!(err.status, Status::Invalid, "{err:?}");
            assert!(
                err.reason.contains(needle),
                "expected {needle:?} in {:?}",
                err.reason
            );
        };

        let mut no_chains = ac_chain_base_input();
        no_chains
            .routing_options
            .as_mut()
            .unwrap()
            .ac_couple_chains
            .clear();
        expect_invalid(no_chains, "require --ac-couple-chain-json");

        let mut no_dp = ac_chain_base_input();
        no_dp.routing_options.as_mut().unwrap().diff_pairs_mode = None;
        expect_invalid(no_dp, "requires --diff-pairs");

        let mut one_ref = ac_chain_base_input();
        one_ref.routing_options.as_mut().unwrap().ac_couple_chains = vec![vec!["C1".into()]];
        expect_invalid(one_ref, "at least two");

        let mut dup_ref = ac_chain_base_input();
        dup_ref.routing_options.as_mut().unwrap().ac_couple_chains =
            vec![vec!["C1".into(), "C1".into()]];
        expect_invalid(dup_ref, "must not repeat");

        let mut missing_ref = ac_chain_base_input();
        missing_ref
            .routing_options
            .as_mut()
            .unwrap()
            .ac_couple_chains = vec![vec!["C1".into(), "C99".into()]];
        expect_invalid(missing_ref, "missing");

        let mut dnp_ref = ac_chain_base_input();
        dnp_ref
            .components
            .iter_mut()
            .find(|c| c.ref_des == "C1")
            .unwrap()
            .dnp = true;
        expect_invalid(dnp_ref, "DNP");

        let mut non_two_pad = ac_chain_base_input();
        non_two_pad.pads.push(
            serde_json::from_value(serde_json::json!({
                "id": "00000000-0000-4000-8000-000000000099",
                "net": 5, "at": [35_000_000, 8_500_000],
                "shape": {"kind": "rect", "half_size_nm": [100_000, 100_000]},
                "layers": ["F.Cu"], "footprint_ref": "C1", "pad_number": "3"
            }))
            .unwrap(),
        );
        expect_invalid(non_two_pad, "two native copper pads");

        let mut opp_pol = ac_chain_base_input();
        opp_pol
            .pads
            .iter_mut()
            .find(|p| {
                p.footprint_ref.as_deref() == Some("C1") && p.pad_number.as_deref() == Some("2")
            })
            .unwrap()
            .net = 4;
        opp_pol
            .pads
            .iter_mut()
            .find(|p| {
                p.footprint_ref.as_deref() == Some("C2") && p.pad_number.as_deref() == Some("2")
            })
            .unwrap()
            .net = 3;
        expect_invalid(opp_pol, "opposite polarities");

        let mut cont = ac_chain_base_input();
        cont.pads.push(
            serde_json::from_value(serde_json::json!({
                "id": "00000000-0000-4000-8000-000000000101",
                "net": 1, "at": [15_000_000, 5_000_000],
                "shape": {"kind": "rect", "half_size_nm": [100_000, 100_000]},
                "layers": ["F.Cu"], "footprint_ref": "R9", "pad_number": "1"
            }))
            .unwrap(),
        );
        cont.pads.push(
            serde_json::from_value(serde_json::json!({
                "id": "00000000-0000-4000-8000-000000000102",
                "net": 5, "at": [17_000_000, 5_000_000],
                "shape": {"kind": "rect", "half_size_nm": [100_000, 100_000]},
                "layers": ["F.Cu"], "footprint_ref": "R9", "pad_number": "2"
            }))
            .unwrap(),
        );
        cont.components.push(crate::model::ComponentInfo {
            ref_des: "R9".into(),
            dnp: false,
            exclude_from_bom: false,
            exclude_from_pos: false,
            pad_numbers: vec!["1".into(), "2".into()],
            net_ids: vec![1, 5],
        });
        expect_invalid(cont, "unlisted two-pad continuation");
    }

    #[test]
    fn ac_chain_matches_composed_skew_and_refuses_impossible_amplitude() {
        let mut inp = ac_chain_base_input();
        // Offset N terminal pads inward so N is ~2.0 mm shorter across the composed chain.
        for p in &mut inp.pads {
            if p.footprint_ref.as_deref() == Some("J1") && p.pad_number.as_deref() == Some("2") {
                p.at = [6_000_000, 12_540_000];
            } else if p.footprint_ref.as_deref() == Some("J2")
                && p.pad_number.as_deref() == Some("2")
            {
                p.at = [64_000_000, 12_540_000];
            } else if p.footprint_ref.as_deref() == Some("C1") {
                p.at[1] = 8_500_000;
            } else if p.footprint_ref.as_deref() == Some("C2") {
                p.at[1] = 12_000_000;
            }
        }
        let mut fail_inp = inp.clone();
        fail_inp
            .routing_options
            .as_mut()
            .unwrap()
            .ac_meander_amplitude_nm = Some(1_000);
        let fail_out = crate::run(fail_inp);
        assert_eq!(fail_out.status, Status::Unroutable, "{:?}", fail_out.reason);

        let out = crate::run(inp.clone());
        assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
        inp.tracks = out.added_tracks;
        inp.vias = out.added_vias;
        let eng = Engine::new(inp).unwrap();
        let m1 = measure_net_fromto(&eng, 1, false).unwrap().unwrap();
        let m2 = measure_net_fromto(&eng, 2, false).unwrap().unwrap();
        let m3 = measure_net_fromto(&eng, 3, false).unwrap().unwrap();
        let m4 = measure_net_fromto(&eng, 4, false).unwrap().unwrap();
        let sum_p = m1.length_nm + m3.length_nm;
        let sum_n = m2.length_nm + m4.length_nm;
        assert!(
            (sum_p - sum_n).abs() <= 100_000,
            "composed skew {} nm > 100_000 nm (P={sum_p}, N={sum_n})",
            (sum_p - sum_n).abs()
        );
    }
}
