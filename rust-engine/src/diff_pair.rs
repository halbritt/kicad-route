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

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum RouteUnitId {
    DiffPair(usize),
    SingleNet(u32),
}

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
pub struct DiffPairPlan {
    pub pairs: Vec<ResolvedDiffPair>,
    pub length_groups: Vec<ResolvedLengthGroup>,
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
        ProtectMode::Existing | ProtectMode::Human => {
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
        ProtectMode::Locked => {
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
    if prof.via_prop_delay_as_per_mm <= 0 && prof.via_overrides.is_empty() {
        return Err(Failure::new(
            Status::Invalid,
            format!("tuning_profile {prof_name:?} missing positive via delay"),
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

fn net_fromto_tokens(input: &Input, net_id: u32) -> Option<(String, String)> {
    let pads: Vec<_> = input.pads.iter().filter(|p| p.net == net_id).collect();
    if pads.len() != 2 {
        return None;
    }
    let tok = |p: &crate::model::Pad| -> Option<String> {
        let r = p.footprint_ref.as_deref()?;
        let n = p.pad_number.as_deref()?;
        Some(format!("{r}-{n}"))
    };
    Some((tok(pads[0])?, tok(pads[1])?))
}

#[allow(clippy::too_many_arguments)]
fn resolve_pair_dru_bounds(
    input: &Input,
    rules: &EffectiveRules,
    stem: &str,
    pos_net: u32,
    neg_net: u32,
    pos_name: &str,
    neg_name: &str,
    class_name: &str,
    class_members: &[String],
    layers: &[String],
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

    for (net_id, net_name) in [(pos_net, pos_name), (neg_net, neg_name)] {
        let ft_owned = net_fromto_tokens(input, net_id);
        let ft_ref = ft_owned.as_ref().map(|(a, b)| (a.as_str(), b.as_str()));
        let item = DruItem {
            net_id,
            net_name,
            net_class: class_name,
            class_members,
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
                if !eval_dru_expr_for_context(cond, &item, None, Some(stem), ft_ref) {
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
                        let pos_ft = net_fromto_tokens(input, pos_net);
                        let neg_ft = net_fromto_tokens(input, neg_net);
                        let pos_item = DruItem {
                            net_id: pos_net,
                            net_name: pos_name,
                            net_class: class_name,
                            class_members,
                            kind: DruItemKind::Track,
                            layers,
                        };
                        let neg_item = DruItem {
                            net_id: neg_net,
                            net_name: neg_name,
                            net_class: class_name,
                            class_members,
                            kind: DruItemKind::Track,
                            layers,
                        };
                        let both_match = eval_dru_expr_for_context(
                            cond,
                            &pos_item,
                            None,
                            Some(stem),
                            pos_ft.as_ref().map(|(a, b)| (a.as_str(), b.as_str())),
                        ) && eval_dru_expr_for_context(
                            cond,
                            &neg_item,
                            None,
                            Some(stem),
                            neg_ft.as_ref().map(|(a, b)| (a.as_str(), b.as_str())),
                        );
                        let any_other = input.nets.iter().any(|on| {
                            if on.id == 0 || on.id == pos_net || on.id == neg_net {
                                return false;
                            }
                            let nr = rules.net(on.id);
                            let oft = net_fromto_tokens(input, on.id);
                            let oitem = DruItem {
                                net_id: on.id,
                                net_name: &on.name,
                                net_class: &nr.class_name,
                                class_members: &nr.class_members,
                                kind: DruItemKind::Track,
                                layers,
                            };
                            eval_dru_expr_for_context(
                                cond,
                                &oitem,
                                None,
                                None,
                                oft.as_ref().map(|(a, b)| (a.as_str(), b.as_str())),
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
                format!("conflicting DRC diff_pair_gap bounds ({lo} > {hi}) for pair {stem:?}"),
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
        let (dru_gap_min, dru_gap_max, dru_uncoupled, dru_max_width, dru_skew_max_nm) =
            resolve_pair_dru_bounds(
                input,
                rules,
                &stem,
                pos_net,
                neg_net,
                &pos_name,
                &neg_name,
                &pr.class_name,
                &pr.class_members,
                &pair_layers,
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
                if na != nb {
                    return Err(Failure::new(
                        Status::Invalid,
                        format!(
                            "from_to entry {:?} connects different nets ({:?} vs {:?})",
                            pair_tok, name_by_id[&na], name_by_id[&nb]
                        ),
                    ));
                }
                if !ft_seen_nets.insert(na) {
                    return Err(Failure::new(
                        Status::Invalid,
                        format!("duplicate net {:?} in from_to group", name_by_id[&na]),
                    ));
                }
                from_to_nets.push(name_by_id[&na].to_owned());
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
        let group_amp = g.meander_amplitude_nm.unwrap_or_else(|| {
            opts.group_meander_amplitude_nm
                .unwrap_or(if any_pair_member {
                    opts.meander_amplitude_nm.unwrap_or(4_500_000)
                } else {
                    default_group_amp
                })
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
            let tol_nm = g
                .tolerance_nm
                .or(opts.group_match_tolerance_nm)
                .unwrap_or(100_000);
            if tol_nm < 0 {
                return Err(Failure::new(
                    Status::Invalid,
                    "length tolerance cannot be negative",
                ));
            }
            if any_pair_member {
                if let Some(intra_tol) = opts.length_match_tolerance_nm {
                    if intra_tol != tol_nm {
                        return Err(Failure::new(
                            Status::Invalid,
                            "coupled length-match groups and intra-pair tuning must declare identical controls",
                        ));
                    }
                }
                if let Some(intra_amp) = opts.meander_amplitude_nm {
                    if intra_amp != group_amp {
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

    Ok(DiffPairPlan {
        pairs,
        length_groups,
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

fn via_barrel_height_mm(input: &Input, layer_a: &str, layer_b: &str) -> f64 {
    if layer_a == layer_b || input.stackup.is_empty() {
        return 0.0;
    }
    let Some(idx_a) = input
        .stackup
        .iter()
        .position(|s| s.kind == StackupKind::Copper && s.name == layer_a)
    else {
        return 0.0;
    };
    let Some(idx_b) = input
        .stackup
        .iter()
        .position(|s| s.kind == StackupKind::Copper && s.name == layer_b)
    else {
        return 0.0;
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
    total_nm / 1_000_000.0
}

fn via_delay_between_layers_as(
    input: &Input,
    prof: &TuningProfile,
    via: &Via,
    layer_a: &str,
    layer_b: &str,
) -> f64 {
    if layer_a == layer_b {
        return 0.0;
    }
    for ov in &prof.via_overrides {
        let layer_match = (ov.start_layer == layer_a && ov.end_layer == layer_b)
            || (ov.start_layer == layer_b && ov.end_layer == layer_a);
        if layer_match && (ov.via_diameter_nm <= 0 || ov.via_diameter_nm == via.diameter_nm) {
            return ov.delay_as as f64;
        }
    }
    let h_mm = via_barrel_height_mm(input, layer_a, layer_b);
    h_mm * (prof.via_prop_delay_as_per_mm as f64)
}

#[derive(Clone, Copy, Debug)]
struct NetMeasure {
    length_nm: i64,
    delay_as: i64,
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

    // Node keys: (point, layer_idx), plus virtual pad nodes 0 (pad A) and 1 (pad B).
    let mut node_ids: BTreeMap<(Point, usize), usize> = BTreeMap::new();
    let mut nodes: Vec<(Point, usize)> = Vec::new();
    let get_node = |pt: Point,
                    l_idx: usize,
                    node_ids: &mut BTreeMap<(Point, usize), usize>,
                    nodes: &mut Vec<(Point, usize)>|
     -> usize {
        *node_ids.entry((pt, l_idx)).or_insert_with(|| {
            let id = nodes.len();
            nodes.push((pt, l_idx));
            id
        })
    };

    // Split segments at any interior T-junction point (track endpoints, via centers, pad centers).
    let mut layer_points: BTreeMap<usize, Vec<Point>> = BTreeMap::new();
    for t in &tracks {
        if let Some(li) = engine.layer_index(&t.layer) {
            let entry = layer_points.entry(li).or_default();
            entry.push(t.start);
            entry.push(t.end);
        }
    }
    for v in &vias {
        let s = engine.layer_index(&v.start_layer).unwrap_or(0);
        let e = engine.layer_index(&v.end_layer).unwrap_or(s);
        for li in s.min(e)..=s.max(e) {
            layer_points.entry(li).or_default().push(v.at);
        }
    }
    for p in &pads {
        let layers: Vec<usize> = if p.layers.is_empty() {
            (0..engine.layer_count()).collect()
        } else {
            p.layers
                .iter()
                .filter_map(|ln| engine.layer_index(ln))
                .collect()
        };
        for li in layers {
            layer_points.entry(li).or_default().push(p.at);
        }
    }

    #[derive(Clone, Copy)]
    struct Edge {
        to: usize,
        len_nm: f64,
        delay_as: f64,
        alive: bool,
        pair_rev: usize,
    }
    let mut edges: Vec<Edge> = Vec::new();
    let mut adj: Vec<Vec<usize>> = Vec::new();
    let add_edge = |u: usize,
                    v: usize,
                    len_nm: f64,
                    delay_as: f64,
                    nodes_len: usize,
                    edges: &mut Vec<Edge>,
                    adj: &mut Vec<Vec<usize>>| {
        if u == v {
            return;
        }
        if adj.len() < nodes_len {
            adj.resize_with(nodes_len, Vec::new);
        }
        let e1 = edges.len();
        let e2 = e1 + 1;
        edges.push(Edge {
            to: v,
            len_nm,
            delay_as,
            alive: true,
            pair_rev: e2,
        });
        edges.push(Edge {
            to: u,
            len_nm,
            delay_as,
            alive: true,
            pair_rev: e1,
        });
        adj[u].push(e1);
        adj[v].push(e2);
    };

    for t in &tracks {
        let Some(li) = engine.layer_index(&t.layer) else {
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
        let mut pts = vec![t.start, t.end];
        if let Some(cands) = layer_points.get(&li) {
            for &pt in cands {
                if pt != t.start
                    && pt != t.end
                    && geometry::point_near_segment(pt, t.start, t.end, 2, false)
                {
                    pts.push(pt);
                }
            }
        }
        let dx = (t.end[0] - t.start[0]) as f64;
        let dy = (t.end[1] - t.start[1]) as f64;
        let seg_len_sq = dx * dx + dy * dy;
        pts.sort_by(|a, b| {
            let ta = if seg_len_sq > 0.0 {
                ((a[0] - t.start[0]) as f64 * dx + (a[1] - t.start[1]) as f64 * dy) / seg_len_sq
            } else {
                0.0
            };
            let tb = if seg_len_sq > 0.0 {
                ((b[0] - t.start[0]) as f64 * dx + (b[1] - t.start[1]) as f64 * dy) / seg_len_sq
            } else {
                0.0
            };
            ta.partial_cmp(&tb).unwrap_or(std::cmp::Ordering::Equal)
        });
        pts.dedup();
        for win in pts.windows(2) {
            let a = win[0];
            let b = win[1];
            let d = (((b[0] - a[0]) as f64).hypot((b[1] - a[1]) as f64)).max(0.0);
            let del = (d / 1_000_000.0) * layer_delay_rate;
            let u = get_node(a, li, &mut node_ids, &mut nodes);
            let v = get_node(b, li, &mut node_ids, &mut nodes);
            add_edge(u, v, d, del, nodes.len(), &mut edges, &mut adj);
        }
    }

    // Via vertical edges between active copper layers at v.at
    for v in &vias {
        let s = engine.layer_index(&v.start_layer).unwrap_or(0);
        let e = engine.layer_index(&v.end_layer).unwrap_or(s);
        let mut active_layers: Vec<usize> = Vec::new();
        for li in s.min(e)..=s.max(e) {
            if node_ids.contains_key(&(v.at, li)) {
                active_layers.push(li);
            }
        }
        // Also connect pads overlapping the via center
        for p in &pads {
            if geometry::via_connects_pad(v, p) {
                let p_layers: Vec<usize> = if p.layers.is_empty() {
                    (s.min(e)..=s.max(e)).collect()
                } else {
                    p.layers
                        .iter()
                        .filter_map(|ln| engine.layer_index(ln))
                        .collect()
                };
                for li in p_layers {
                    if li >= s.min(e) && li <= s.max(e) {
                        let pn = get_node(p.at, li, &mut node_ids, &mut nodes);
                        let vn = get_node(v.at, li, &mut node_ids, &mut nodes);
                        if pn != vn {
                            let d = ((v.at[0] - p.at[0]) as f64).hypot((v.at[1] - p.at[1]) as f64);
                            add_edge(pn, vn, d, 0.0, nodes.len(), &mut edges, &mut adj);
                        }
                        if !active_layers.contains(&li) {
                            active_layers.push(li);
                        }
                    }
                }
            }
        }
        active_layers.sort_unstable();
        active_layers.dedup();
        for win in active_layers.windows(2) {
            let la = win[0];
            let lb = win[1];
            let u = get_node(v.at, la, &mut node_ids, &mut nodes);
            let w = get_node(v.at, lb, &mut node_ids, &mut nodes);
            let h_nm = if engine.effective_rules.use_height_for_length_calcs {
                via_barrel_height_mm(&engine.input, engine.layer_name(la), engine.layer_name(lb))
                    * 1_000_000.0
            } else {
                0.0
            };
            let del = if let Some(pr) = prof {
                via_delay_between_layers_as(
                    &engine.input,
                    pr,
                    v,
                    engine.layer_name(la),
                    engine.layer_name(lb),
                )
            } else {
                0.0
            };
            add_edge(u, w, h_nm, del, nodes.len(), &mut edges, &mut adj);
        }
    }

    // Pad super-nodes for pad 0 and pad 1
    let mut pad_nodes = [0usize; 2];
    for (p_idx, p) in pads.iter().take(2).enumerate() {
        let super_id = nodes.len();
        nodes.push((p.at, usize::MAX));
        adj.resize_with(nodes.len(), Vec::new);
        pad_nodes[p_idx] = super_id;
        let p_layers: Vec<usize> = if p.layers.is_empty() {
            (0..engine.layer_count()).collect()
        } else {
            p.layers
                .iter()
                .filter_map(|ln| engine.layer_index(ln))
                .collect()
        };
        let mut direct_pad_hit = false;
        for li in &p_layers {
            if let Some(&nid) = node_ids.get(&(p.at, *li)) {
                add_edge(super_id, nid, 0.0, 0.0, nodes.len(), &mut edges, &mut adj);
                direct_pad_hit = true;
            }
        }
        if !direct_pad_hit {
            for &li in &p_layers {
                for &(pt, pt_li) in node_ids.keys() {
                    if pt_li == li && pt != p.at && geometry::track_near_pad(pt, pt, 0, 0, p, false)
                    {
                        let nid = node_ids[&(pt, pt_li)];
                        add_edge(
                            super_id,
                            nid,
                            dist_f64(pt, p.at),
                            0.0,
                            nodes.len(),
                            &mut edges,
                            &mut adj,
                        );
                        direct_pad_hit = true;
                    }
                }
            }
        }
        if !direct_pad_hit {
            for t in &tracks {
                if let Some(li) = engine.layer_index(&t.layer) {
                    if pad_supports_layer(p, &t.layer) && geometry::track_connects_pad(t, p) {
                        let closer = if dist_f64(t.start, p.at) <= dist_f64(t.end, p.at) {
                            t.start
                        } else {
                            t.end
                        };
                        if let Some(&nid) = node_ids.get(&(closer, li)) {
                            add_edge(
                                super_id,
                                nid,
                                dist_f64(closer, p.at),
                                0.0,
                                nodes.len(),
                                &mut edges,
                                &mut adj,
                            );
                        }
                    }
                }
            }
        }
    }
    let s_node = pad_nodes[0];
    let t_node = pad_nodes[1];

    // Iteratively prune dead-end degree-1 vertices (excluding terminals s_node & t_node)
    let mut deg: Vec<usize> = adj.iter().map(|list| list.len()).collect();
    let mut queue: Vec<usize> = (0..nodes.len())
        .filter(|&u| u != s_node && u != t_node && deg[u] <= 1)
        .collect();
    while let Some(u) = queue.pop() {
        for &ei in &adj[u] {
            if !edges[ei].alive {
                continue;
            }
            edges[ei].alive = false;
            let rev = edges[ei].pair_rev;
            edges[rev].alive = false;
            let v = edges[ei].to;
            deg[u] = deg[u].saturating_sub(1);
            deg[v] = deg[v].saturating_sub(1);
            if v != s_node && v != t_node && deg[v] == 1 {
                queue.push(v);
            }
        }
    }

    // Shortest useful path from s_node to t_node on pruned graph (rejects disconnected, handles loops cleanly)
    #[derive(Clone, Copy)]
    struct State {
        cost: f64,
        len_nm: f64,
        delay_as: f64,
        node: usize,
    }
    impl PartialEq for State {
        fn eq(&self, other: &Self) -> bool {
            self.cost == other.cost && self.node == other.node
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
        }
    }
    impl PartialOrd for State {
        fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
            Some(self.cmp(other))
        }
    }

    let mut dist = vec![f64::INFINITY; nodes.len()];
    let mut heap = BinaryHeap::new();
    dist[s_node] = 0.0;
    heap.push(State {
        cost: 0.0,
        len_nm: 0.0,
        delay_as: 0.0,
        node: s_node,
    });
    while let Some(cur) = heap.pop() {
        if cur.node == t_node {
            return Ok(Some(NetMeasure {
                length_nm: cur.len_nm.round() as i64,
                delay_as: cur.delay_as.round() as i64,
            }));
        }
        if cur.cost > dist[cur.node] {
            continue;
        }
        for &ei in &adj[cur.node] {
            let e = edges[ei];
            if !e.alive {
                continue;
            }
            let step_cost = if require_delay { e.delay_as } else { e.len_nm };
            let nc = cur.cost + step_cost;
            if nc < dist[e.to] {
                dist[e.to] = nc;
                heap.push(State {
                    cost: nc,
                    len_nm: cur.len_nm + e.len_nm,
                    delay_as: cur.delay_as + e.delay_as,
                    node: e.to,
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

    let mask_pad_clr = if r.solder_mask_to_copper_clearance_nm > 0 {
        r.solder_mask_expansion_nm + r.solder_mask_to_copper_clearance_nm
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
                    if pad.layers.is_empty() || pad.layers.iter().any(|l| l == layer_name) {
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
    Ok(true)
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

fn point_over_reference_zones(engine: &Engine, pt: Point, imp: &ImpedancePlan) -> bool {
    imp.reference_layers.iter().all(|rl| {
        engine.input.zones.iter().any(|z| {
            z.net == imp.reference_net
                && z.layer_fills.iter().any(|lf| {
                    lf.layer == *rl
                        && lf
                            .polygons
                            .iter()
                            .any(|poly| point_in_poly_ray(pt, &poly.outer, &poly.holes))
                })
        })
    })
}

fn segment_over_reference_zones(engine: &Engine, a: Point, b: Point, imp: &ImpedancePlan) -> bool {
    let len = ((b[0] - a[0]) as f64).hypot((b[1] - a[1]) as f64);
    let steps = (len / 200_000.0).ceil().max(1.0) as usize;
    for k in 0..=steps {
        let t = (k as f64) / (steps as f64);
        let pt = [
            a[0] + ((b[0] - a[0]) as f64 * t).round() as i64,
            a[1] + ((b[1] - a[1]) as f64 * t).round() as i64,
        ];
        if !point_over_reference_zones(engine, pt, imp) {
            return false;
        }
    }
    true
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
    if let Some(imp) = &pair.impedance {
        if engine.layer_name(layer_idx) != imp.signal_layer {
            return Ok(false);
        }
        if !segment_over_reference_zones(engine, pa, pb, imp)
            || !segment_over_reference_zones(engine, na, nb, imp)
        {
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
fn trim_dangling_pad_tails(tracks: &mut Vec<Track>, pads: &[&crate::model::Pad]) {
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
                    pad_supports_layer(p, layer) && geometry::track_near_pad(pt, pt, 0, 0, p, false)
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
    vec![vec![a, elbow1, b], vec![a, elbow2, b], vec![a, b]]
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
    skew_nm: i64,
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

fn pad_supports_layer(pad: &crate::model::Pad, layer_name: &str) -> bool {
    pad.layers.is_empty() || pad.layers.iter().any(|l| l == layer_name)
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
    let offsets_nm: [i64; 7] = [
        0, 350_000, 700_000, 1_000_000, 1_500_000, 2_000_000, max_reach,
    ];

    let mut candidate_layers: Vec<usize> = allowed_layer_idxs
        .iter()
        .copied()
        .filter(|&li| {
            let lname = engine.layer_name(li);
            pad_supports_layer(p_pad, lname) && pad_supports_layer(n_pad, lname)
        })
        .collect();
    if candidate_layers.is_empty() {
        // SMD pads on a layer excluded from --pair-layers: escape on pad layer to coupled via
        for li in 0..engine.layer_count() {
            let lname = engine.layer_name(li);
            if pad_supports_layer(p_pad, lname) && pad_supports_layer(n_pad, lname) {
                candidate_layers.push(li);
            }
        }
    }

    for &li in &candidate_layers {
        for &(_, dir) in &ranked_dirs {
            let left = (-dir.1, dir.0);
            let pn_vec = (
                (p_pad.at[0] - n_pad.at[0]) as f64,
                (p_pad.at[1] - n_pad.at[1]) as f64,
            );
            let pos_on_left = (pn_vec.0 * left.0 + pn_vec.1 * left.1) >= 0.0;
            let sign = if pos_on_left { 1.0 } else { -1.0 };
            let step_sign = if is_end { -1.0 } else { 1.0 };

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
                if let Some(imp) = &pair.impedance {
                    if !point_over_reference_zones(engine, gp, imp)
                        || !point_over_reference_zones(engine, gn, imp)
                    {
                        continue;
                    }
                }

                'esc: for &ew in &widths_to_try {
                    let p_paths = octilinear_elbow_paths(p_pad.at, gp);
                    let n_paths = octilinear_elbow_paths(n_pad.at, gn);
                    for pp in &p_paths {
                        let p_legal = pp.windows(2).all(|seg| {
                            legal_edge_with_width(engine, pair.pos_net, li, seg[0], seg[1], ew)
                                .unwrap_or(false)
                        });
                        if !p_legal {
                            continue;
                        }
                        for np in &n_paths {
                            let n_legal = np.windows(2).all(|seg| {
                                legal_edge_with_width(engine, pair.neg_net, li, seg[0], seg[1], ew)
                                    .unwrap_or(false)
                            });
                            if !n_legal {
                                continue;
                            }
                            let min_esc_clr =
                                2 * i128::from(ew) + 2 * i128::from(pair.clearance_nm);
                            let mut cross_ok = true;
                            for ps in pp.windows(2) {
                                for ns in np.windows(2) {
                                    if geometry::segments_near(
                                        ps[0],
                                        ps[1],
                                        ns[0],
                                        ns[1],
                                        min_esc_clr,
                                        false,
                                    ) {
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
                            let uncoupled_nm = lp.max(ln).round() as i64;
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
                            out.push(EndFanout {
                                gather: g,
                                gp,
                                gn,
                                dir,
                                layer_idx: li,
                                pos_on_left,
                                pos_segs,
                                neg_segs,
                                uncoupled_nm,
                                skew_nm,
                            });
                            break 'esc;
                        }
                    }
                }
            }
        }
    }
    out.sort_by_key(|f| (f.uncoupled_nm, f.skew_nm));
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

fn validate_via_jog_pair(
    engine: &Engine,
    pair: &ResolvedDiffPair,
    layer_idx: usize,
    p_a: Point,
    p_b: Point,
    n_a: Point,
    n_b: Point,
) -> Result<Option<i64>, Failure> {
    if p_a != p_b {
        if !legal_edge_with_width(engine, pair.pos_net, layer_idx, p_a, p_b, pair.width_nm)? {
            return Ok(None);
        }
        if let Some(imp) = &pair.impedance {
            if engine.layer_name(layer_idx) != imp.signal_layer
                || !segment_over_reference_zones(engine, p_a, p_b, imp)
            {
                return Ok(None);
            }
        }
    }
    if n_a != n_b {
        if !legal_edge_with_width(engine, pair.neg_net, layer_idx, n_a, n_b, pair.width_nm)? {
            return Ok(None);
        }
        if let Some(imp) = &pair.impedance {
            if engine.layer_name(layer_idx) != imp.signal_layer
                || !segment_over_reference_zones(engine, n_a, n_b, imp)
            {
                return Ok(None);
            }
        }
    }
    if p_a != p_b || n_a != n_b {
        let min_clr2 = 2 * i128::from(pair.width_nm) + 2 * i128::from(pair.clearance_nm);
        if geometry::segments_near(p_a, p_b, n_a, n_b, min_clr2, false) {
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
    let pos_on_left = start_f.pos_on_left;
    let s_nm = pair.width_nm + pair.gap_nm;
    let mut tracks: Vec<Track> = Vec::new();
    let mut vias: Vec<Via> = Vec::new();
    let mut total_uncoupled = start_f.uncoupled_nm + end_f.uncoupled_nm;

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
        let Some(u1) =
            validate_via_jog_pair(engine, pair, start_f.layer_idx, p0[0], p_pt, n0[0], n_pt)?
        else {
            return Ok(None);
        };
        let Some(u2) =
            validate_via_jog_pair(engine, pair, first_run_layer, p_pt, p0[0], n_pt, n0[0])?
        else {
            return Ok(None);
        };
        total_uncoupled += u1.max(u2);
        if p0[0] != p_pt {
            tracks.push(Track {
                id: String::new(),
                net: pair.pos_net,
                start: p0[0],
                end: p_pt,
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(start_f.layer_idx).to_owned(),
            });
            tracks.push(Track {
                id: String::new(),
                net: pair.pos_net,
                start: p_pt,
                end: p0[0],
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(first_run_layer).to_owned(),
            });
        }
        if n0[0] != n_pt {
            tracks.push(Track {
                id: String::new(),
                net: pair.neg_net,
                start: n0[0],
                end: n_pt,
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(start_f.layer_idx).to_owned(),
            });
            tracks.push(Track {
                id: String::new(),
                net: pair.neg_net,
                start: n_pt,
                end: n0[0],
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(first_run_layer).to_owned(),
            });
        }
        vias.push(vp);
        vias.push(vn);
    }

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
                total_uncoupled += dist_f64(start_f.gp, p_poly[0])
                    .max(dist_f64(start_f.gn, n_poly[0]))
                    .round() as i64;
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
                tracks.push(Track {
                    id: String::new(),
                    net: pair.pos_net,
                    start: start_f.gp,
                    end: p_poly[0],
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(li).to_owned(),
                });
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
                tracks.push(Track {
                    id: String::new(),
                    net: pair.neg_net,
                    start: start_f.gn,
                    end: n_poly[0],
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(li).to_owned(),
                });
            }
        }
        for w in p_poly.windows(2) {
            if !legal_edge_with_width(engine, pair.pos_net, li, w[0], w[1], pair.width_nm)? {
                return Ok(None);
            }
            if let Some(imp) = &pair.impedance {
                if engine.layer_name(li) != imp.signal_layer
                    || !segment_over_reference_zones(engine, w[0], w[1], imp)
                {
                    return Ok(None);
                }
            }
            tracks.push(Track {
                id: String::new(),
                net: pair.pos_net,
                start: w[0],
                end: w[1],
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(li).to_owned(),
            });
        }
        for w in n_poly.windows(2) {
            if !legal_edge_with_width(engine, pair.neg_net, li, w[0], w[1], pair.width_nm)? {
                return Ok(None);
            }
            if let Some(imp) = &pair.impedance {
                if engine.layer_name(li) != imp.signal_layer
                    || !segment_over_reference_zones(engine, w[0], w[1], imp)
                {
                    return Ok(None);
                }
            }
            tracks.push(Track {
                id: String::new(),
                net: pair.neg_net,
                start: w[0],
                end: w[1],
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(li).to_owned(),
            });
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
                total_uncoupled += dist_f64(last_p, end_f.gp)
                    .max(dist_f64(last_n, end_f.gn))
                    .round() as i64;
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
                tracks.push(Track {
                    id: String::new(),
                    net: pair.pos_net,
                    start: last_p,
                    end: end_f.gp,
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(li).to_owned(),
                });
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
                tracks.push(Track {
                    id: String::new(),
                    net: pair.neg_net,
                    start: last_n,
                    end: end_f.gn,
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(li).to_owned(),
                });
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
            let Some(u1) = validate_via_jog_pair(engine, pair, li, p_end, p_pt, n_end, n_pt)?
            else {
                return Ok(None);
            };
            let Some(u2) =
                validate_via_jog_pair(engine, pair, next_li, p_pt, next_p[0], n_pt, next_n[0])?
            else {
                return Ok(None);
            };
            total_uncoupled += u1 + u2;
            if p_end != p_pt {
                tracks.push(Track {
                    id: String::new(),
                    net: pair.pos_net,
                    start: p_end,
                    end: p_pt,
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(li).to_owned(),
                });
            }
            if n_end != n_pt {
                tracks.push(Track {
                    id: String::new(),
                    net: pair.neg_net,
                    start: n_end,
                    end: n_pt,
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(li).to_owned(),
                });
            }
            if next_p[0] != p_pt {
                tracks.push(Track {
                    id: String::new(),
                    net: pair.pos_net,
                    start: p_pt,
                    end: next_p[0],
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(next_li).to_owned(),
                });
            }
            if next_n[0] != n_pt {
                tracks.push(Track {
                    id: String::new(),
                    net: pair.neg_net,
                    start: n_pt,
                    end: next_n[0],
                    width_nm: pair.width_nm,
                    locked: false,
                    layer: engine.layer_name(next_li).to_owned(),
                });
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
        let Some(u1) =
            validate_via_jog_pair(engine, pair, last_run_layer, p_last, p_pt, n_last, n_pt)?
        else {
            return Ok(None);
        };
        let Some(u2) =
            validate_via_jog_pair(engine, pair, end_f.layer_idx, p_pt, p_last, n_pt, n_last)?
        else {
            return Ok(None);
        };
        total_uncoupled += u1.max(u2);
        if p_last != p_pt {
            tracks.push(Track {
                id: String::new(),
                net: pair.pos_net,
                start: p_last,
                end: p_pt,
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(last_run_layer).to_owned(),
            });
            tracks.push(Track {
                id: String::new(),
                net: pair.pos_net,
                start: p_pt,
                end: p_last,
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(end_f.layer_idx).to_owned(),
            });
        }
        if n_last != n_pt {
            tracks.push(Track {
                id: String::new(),
                net: pair.neg_net,
                start: n_last,
                end: n_pt,
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(last_run_layer).to_owned(),
            });
            tracks.push(Track {
                id: String::new(),
                net: pair.neg_net,
                start: n_pt,
                end: n_last,
                width_nm: pair.width_nm,
                locked: false,
                layer: engine.layer_name(end_f.layer_idx).to_owned(),
            });
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

    if total_uncoupled > pair.uncoupled_max_nm {
        return Ok(None);
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
    trim_dangling_pad_tails(&mut p_tracks, &pos_pads);
    trim_dangling_pad_tails(&mut n_tracks, &neg_pads);
    tracks = p_tracks;
    tracks.extend(n_tracks);
    Ok(Some(CoupledSolution { tracks, vias }))
}
fn astar_coupled_centerline(
    engine: &Engine,
    pair: &ResolvedDiffPair,
    sf: &EndFanout,
    ef: &EndFanout,
    allowed_layer_idxs: &[usize],
    step_nm: i64,
    step_penalty: &dyn Fn(u8, Point) -> u64,
    via_penalty: &dyn Fn(Point, u8, u8) -> u64,
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

    let start_li = if allowed_layer_idxs.contains(&sf.layer_idx) {
        sf.layer_idx
    } else {
        allowed_layer_idxs[0]
    };
    let end_li = if allowed_layer_idxs.contains(&ef.layer_idx) {
        ef.layer_idx
    } else {
        allowed_layer_idxs[0]
    };

    let origin = sf.gather;
    let grid_pt = |gx: i32, gy: i32| -> Point {
        [
            origin[0] + i64::from(gx) * step_nm,
            origin[1] + i64::from(gy) * step_nm,
        ]
    };
    let heuristic = |pt: Point, li: usize| -> u64 {
        let d = dist_f64(pt, ef.gather).round() as u64;
        let v = if li == end_li { 0 } else { 400_000 };
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

    let start_key = Key {
        gx: 0,
        gy: 0,
        li: start_li as u8,
        dir: 8,
    };
    let mut best_g: BTreeMap<Key, u64> = BTreeMap::new();
    let mut parent: BTreeMap<Key, Key> = BTreeMap::new();
    let mut heap = BinaryHeap::new();
    best_g.insert(start_key, 0);
    heap.push(Node {
        f: heuristic(origin, start_li),
        g: 0,
        key: start_key,
    });

    let snap_radius = step_nm * 6;
    let diag_len = ((step_nm as f64) * std::f64::consts::SQRT_2).round() as u64;
    let via_base_cost = 600_000u64;
    let mut expansions = 0usize;
    let max_expansions = 35_000usize;

    let reconstruct_runs = |tail_key: Key,
                            tail_pts: &[Point],
                            tail_on_end_li: bool,
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
        if tail_on_end_li && runs.last().is_some_and(|(_, l)| *l != end_li) {
            let pivot = *runs.last().unwrap().0.last().unwrap();
            let mut tail_vec = vec![pivot];
            for &tp in tail_pts {
                if tail_vec.last().copied() != Some(tp) {
                    tail_vec.push(tp);
                }
            }
            runs.push((tail_vec, end_li));
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
            let mut tails = octilinear_elbow_paths(pt, ef.gather);
            if dist_f64(pt, ef.gather) >= 1_200_000.0 {
                let p_out = [
                    ef.gather[0] - (600_000.0 * ef.dir.0).round() as i64,
                    ef.gather[1] - (600_000.0 * ef.dir.1).round() as i64,
                ];
                for mut t in octilinear_elbow_paths(pt, p_out) {
                    t.push(ef.gather);
                    tails.push(t);
                }
            }
            for tail in &tails {
                let tail_end_li = li != end_li;
                let runs = reconstruct_runs(k, tail, tail_end_li, &parent);
                if let Some(sol) = validate_and_build_coupled_solution(engine, pair, sf, ef, &runs)?
                {
                    return Ok(Some(sol));
                }
            }
        }

        // 8 octilinear steps
        for (nd, &(dx, dy)) in DIRS.iter().enumerate() {
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
                pt[0] + (sign * half * left.0).round() as i64,
                pt[1] + (sign * half * left.1).round() as i64,
            ];
            let pb = [
                npt[0] + (sign * half * left.0).round() as i64,
                npt[1] + (sign * half * left.1).round() as i64,
            ];
            let na = [
                pt[0] - (sign * half * left.0).round() as i64,
                pt[1] - (sign * half * left.1).round() as i64,
            ];
            let nb = [
                npt[0] - (sign * half * left.0).round() as i64,
                npt[1] - (sign * half * left.1).round() as i64,
            ];
            if !legal_pair_coupled_step(engine, pair, li, pa, pb, na, nb, pair.width_nm)? {
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
        if k.dir < 8 && allowed_layer_idxs.len() > 1 {
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

fn route_single_diff_pair(
    engine: &mut Engine,
    pair: &ResolvedDiffPair,
    plan: &DiffPairPlan,
    step_penalty: &dyn Fn(u8, Point) -> u64,
    via_penalty: &dyn Fn(Point, u8, u8) -> u64,
) -> Result<(), Failure> {
    // Check if already connected & protected / pre-routed
    let p_conn = measure_net_fromto(engine, pair.pos_net, false)?.is_some();
    let n_conn = measure_net_fromto(engine, pair.neg_net, false)?.is_some();
    if p_conn && n_conn {
        return Ok(());
    }
    if plan.protected_nets.contains(&pair.pos_net) || plan.protected_nets.contains(&pair.neg_net) {
        return Err(Failure::new(
            Status::Unroutable,
            format!(
                "differential pair ({:?}, {:?}) is incomplete under --protect",
                pair.pos_name, pair.neg_name
            ),
        ));
    }

    let allowed_layer_idxs: Vec<usize> = plan
        .pair_layers
        .iter()
        .filter_map(|ln| engine.layer_index(ln))
        .collect();
    if allowed_layer_idxs.is_empty() {
        return Err(Failure::new(
            Status::Invalid,
            "no allowed copper layers available for differential pair routing",
        ));
    }

    let Some([(p_start, n_start), (p_end, n_end)]) =
        find_pad_pairs(&engine.input, pair.pos_net, pair.neg_net)
    else {
        return Err(Failure::new(
            Status::Unroutable,
            format!("unable to pair terminal pads for {}", pair.stem),
        ));
    };

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
    )?;
    let end_fanouts = enumerate_end_fanouts(
        engine,
        pair,
        p_end,
        n_end,
        start_mid,
        true,
        &allowed_layer_idxs,
    )?;

    let mut pairs_ranked: Vec<(i64, usize, usize)> = Vec::new();
    for (si, sf) in start_fanouts.iter().enumerate() {
        for (ei, ef) in end_fanouts.iter().enumerate() {
            if sf.pos_on_left != ef.pos_on_left {
                continue;
            }
            if sf.uncoupled_nm + ef.uncoupled_nm > pair.uncoupled_max_nm {
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
            let score = sf.uncoupled_nm
                + ef.uncoupled_nm
                + sf.skew_nm
                + ef.skew_nm
                + same_layer_bonus
                + align_pen;
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
                let runs = vec![(path, sf.layer_idx)];
                if let Some(sol) = validate_and_build_coupled_solution(engine, pair, sf, ef, &runs)?
                {
                    return commit_coupled_solution(engine, pair, sol);
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
            if let Some(sol) = astar_coupled_centerline(
                engine,
                pair,
                sf,
                ef,
                &allowed_layer_idxs,
                step_nm,
                step_penalty,
                via_penalty,
            )? {
                return commit_coupled_solution(engine, pair, sol);
            }
        }
    }

    Err(Failure::new(
        Status::Unroutable,
        format!(
            "coupled routing failed for differential pair ({:?}, {:?}); no single-ended fallback allowed",
            pair.pos_name, pair.neg_name
        ),
    ))
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
        let p_ok = measure_net_fromto(eng, pair.pos_net, false)?.is_some();
        let n_ok = measure_net_fromto(eng, pair.neg_net, false)?.is_some();
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

    if !plan.pairs.is_empty() {
        unwrap_tx(engine.transaction(|tx| {
            let eng = &mut *tx.engine;
            for pair in &plan.pairs {
                route_single_diff_pair(eng, pair, &plan, step_penalty, via_penalty)?;
            }
            Ok(())
        }))?;
    }

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
// ---------------------------------------------------------------------------
// Length & Delay Matching (Intra-Pair, Coupled Tandem & Single-Ended Meanders)
// ---------------------------------------------------------------------------

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
) -> (Vec<Point>, f64, f64) {
    let min_seg = (1.2 * width_nm).max(width_nm + 50_000.0);
    let c_try = (0.3 * spacing_nm).min(0.35 * amp_nm);
    let gap_try = (spacing_nm - 2.0 * c_try).max(0.5 * spacing_nm);
    let inner_miter_shrink = inner_offset_margin_nm * (2.0 - std::f64::consts::SQRT_2);
    let inner_top_shrink = 2.0 * inner_offset_margin_nm * (std::f64::consts::SQRT_2 - 1.0);
    let chamfer_ok = c_try - inner_miter_shrink >= min_seg
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
    let min_tri_h =
        (0.85 * width_nm + inner_offset_margin_nm * (std::f64::consts::SQRT_2 - 1.0)).max(10_000.0);
    let h_small = amp_nm.max(min_tri_h);
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

fn point_to_seg_dist_f64(p: [f64; 2], a: Point, b: Point) -> f64 {
    let vx = (b[0] - a[0]) as f64;
    let vy = (b[1] - a[1]) as f64;
    let wx = p[0] - a[0] as f64;
    let wy = p[1] - a[1] as f64;
    let len2 = vx * vx + vy * vy;
    if len2 <= 0.0 {
        return wx.hypot(wy);
    }
    let t = ((wx * vx + wy * vy) / len2).clamp(0.0, 1.0);
    let px = a[0] as f64 + t * vx - p[0];
    let py = a[1] as f64 + t * vy - p[1];
    px.hypot(py)
}

fn measure_pair_uncoupled_nm(engine: &Engine, pair: &ResolvedDiffPair) -> i64 {
    let p_tracks: Vec<&Track> = engine
        .copper
        .tracks()
        .filter(|t| t.net == pair.pos_net)
        .collect();
    let n_tracks: Vec<&Track> = engine
        .copper
        .tracks()
        .filter(|t| t.net == pair.neg_net)
        .collect();

    let leg_uncoupled = |own: &[&Track], other: &[&Track]| -> f64 {
        let mut total = 0.0_f64;
        for t in own {
            let len = dist_f64(t.start, t.end);
            if len <= 1.0 {
                continue;
            }
            let steps = (len / 50_000.0).ceil().max(1.0) as usize;
            let step_len = len / (steps as f64);
            for k in 0..steps {
                let frac = (k as f64 + 0.5) / (steps as f64);
                let mx = t.start[0] as f64 + (t.end[0] - t.start[0]) as f64 * frac;
                let my = t.start[1] as f64 + (t.end[1] - t.start[1]) as f64 * frac;
                let coupled = other.iter().any(|o| {
                    if o.layer != t.layer {
                        return false;
                    }
                    let half_w = 0.5 * (t.width_nm + o.width_nm) as f64;
                    let min_c = (half_w + pair.gap_min_nm as f64 - 15_000.0).max(0.0);
                    let max_c = half_w + pair.gap_max_nm as f64 + 15_000.0;
                    let d = point_to_seg_dist_f64([mx, my], o.start, o.end);
                    d >= min_c && d <= max_c
                });
                if !coupled {
                    total += step_len;
                }
            }
        }
        total
    };

    let u_p = leg_uncoupled(&p_tracks, &n_tracks);
    let u_n = leg_uncoupled(&n_tracks, &p_tracks);
    u_p.max(u_n).round() as i64
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
                    let Some((_, max_add)) = eval_added(n_waves, max_amp, nx, ny, alt, center_t)
                    else {
                        continue;
                    };
                    if max_add + (tol as f64) < needed_len_nm {
                        continue;
                    }
                    let mut lo = (0.85 * cand.width_nm as f64).min(max_amp).max(10_000.0);
                    let mut hi = max_amp;
                    for _ in 0..32 {
                        let mid = 0.5 * (lo + hi);
                        match eval_added(n_waves, mid, nx, ny, alt, center_t) {
                            Some((_, add)) if add < needed_len_nm => lo = mid,
                            _ => hi = mid,
                        }
                    }
                    let mid_amp = 0.5 * (lo + hi);
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
                                    && !segment_over_reference_zones(eng, w[0], w[1], imp)
                                {
                                    return Err(Failure::new(
                                        Status::Unroutable,
                                        "meander left impedance reference zone",
                                    ));
                                }
                            }
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
                    match attempt {
                        Ok(()) => return Ok(()),
                        Err(TransactionError::Limit(f)) => return Err(f),
                        Err(TransactionError::Operation(_)) => {}
                    }
                }
            }
        }
    }

    Err(Failure::new(
        Status::Unroutable,
        format!("unable to match net {net} to target {target_val} within tolerance {tol}"),
    ))
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
    .round();
    let prof = if is_delay {
        find_tuning_profile(&engine.effective_rules, pair.pos_net).cloned()
    } else {
        None
    };

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

        let eval_coupled = |n_waves: usize,
                            amp: f64,
                            nx: f64,
                            ny: f64,
                            alt: bool|
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
            let _ = existing_uncoupled;
            for &(nx, ny, alt) in &[
                (-uy, ux, true),
                (uy, -ux, true),
                (-uy, ux, false),
                (uy, -ux, false),
            ] {
                let Some((_, _, max_add)) = eval_coupled(n_waves, max_amp, nx, ny, alt) else {
                    continue;
                };
                if max_add + (tol as f64) < needed_len_nm {
                    continue;
                }
                let min_tri_h = 0.85 * (pair.width_nm as f64)
                    + 0.5 * (s_nm as f64) * (std::f64::consts::SQRT_2 - 1.0);
                let mut lo = min_tri_h.min(max_amp);
                let mut hi = max_amp;
                for _ in 0..32 {
                    let mid = 0.5 * (lo + hi);
                    match eval_coupled(n_waves, mid, nx, ny, alt) {
                        Some((_, _, add)) if add < needed_len_nm => lo = mid,
                        _ => hi = mid,
                    }
                }
                let mid_amp = 0.5 * (lo + hi);
                let best_coupled = [hi, mid_amp, lo]
                    .into_iter()
                    .filter_map(|a| eval_coupled(n_waves, a, nx, ny, alt))
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
                        if !legal_edge_with_width(eng, pair.pos_net, li, w[0], w[1], pair.width_nm)?
                        {
                            return Err(Failure::new(
                                Status::Unroutable,
                                "coupled meander pos illegal",
                            ));
                        }
                        if let Some(imp) = &pair.impedance {
                            if !segment_over_reference_zones(eng, w[0], w[1], imp) {
                                return Err(Failure::new(
                                    Status::Unroutable,
                                    "coupled meander outside ref zone",
                                ));
                            }
                        }
                    }
                    for w in n_poly.windows(2) {
                        if !legal_edge_with_width(eng, pair.neg_net, li, w[0], w[1], pair.width_nm)?
                        {
                            return Err(Failure::new(
                                Status::Unroutable,
                                "coupled meander neg illegal",
                            ));
                        }
                        if let Some(imp) = &pair.impedance {
                            if !segment_over_reference_zones(eng, w[0], w[1], imp) {
                                return Err(Failure::new(
                                    Status::Unroutable,
                                    "coupled meander outside ref zone",
                                ));
                            }
                        }
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
                    if (vp - target_val).abs() > tol || (vn - target_val).abs() > tol {
                        return Err(Failure::new(
                            Status::Unroutable,
                            "coupled meander missed tolerance",
                        ));
                    }
                    Ok(())
                });
                match attempt {
                    Ok(()) => return Ok(()),
                    Err(TransactionError::Limit(f)) => return Err(f),
                    Err(TransactionError::Operation(_)) => {}
                }
            }
        }
    }

    Err(Failure::new(
        Status::Unroutable,
        format!(
            "unable to match coupled pair {} to target {target_val}",
            pair.stem
        ),
    ))
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
    if measure_pair_uncoupled_nm(engine, pair) > pair.uncoupled_max_nm {
        return Err(Failure::new(
            Status::Unroutable,
            format!(
                "diff pair {} exceeds diff_pair_uncoupled limit before intra-pair match",
                pair.stem
            ),
        ));
    }
    let (short_net, long_net, long_len) = if mp.length_nm < mn.length_nm {
        (pair.pos_net, pair.neg_net, mn.length_nm)
    } else {
        (pair.neg_net, pair.pos_net, mp.length_nm)
    };
    let needed = (long_len - mp.length_nm.min(mn.length_nm)) as f64;
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
        .cloned()
        .collect();
    let max_h = amp_max_nm.min(pair.uncoupled_max_nm / 2) as f64;

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
        if max_h <= 10_000.0 || seg_len < ((3 * cand.width_nm).max(750_000)) as f64 {
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
            let (_, max_add) = eval_pad_bump(max_h, nx, ny);
            if max_add + (tol_nm as f64) < needed {
                continue;
            }
            let mut lo = (0.85 * cand.width_nm as f64).min(max_h).max(10_000.0);
            let mut hi = max_h;
            for _ in 0..32 {
                let mid_h = 0.5 * (lo + hi);
                if eval_pad_bump(mid_h, nx, ny).1 < needed {
                    lo = mid_h;
                } else {
                    hi = mid_h;
                }
            }
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
                            && !segment_over_reference_zones(eng, w[0], w[1], imp)
                        {
                            return Err(Failure::new(
                                Status::Unroutable,
                                "pad detour left impedance reference zone",
                            ));
                        }
                    }
                }
                if measure_pair_uncoupled_nm(eng, pair) > pair.uncoupled_max_nm {
                    return Err(Failure::new(
                        Status::Unroutable,
                        "pad detour exceeded diff_pair_uncoupled limit",
                    ));
                }
                let m = measure_net_fromto(eng, short_net, false)?
                    .ok_or_else(|| Failure::new(Status::Unroutable, "disconnected"))?;
                if (m.length_nm - long_len).abs() > tol_nm {
                    return Err(Failure::new(Status::Unroutable, "pad detour missed tol"));
                }
                Ok(())
            });
            match attempt {
                Ok(()) => return Ok(()),
                Err(TransactionError::Limit(f)) => return Err(f),
                Err(TransactionError::Operation(_)) => {}
            }
        }
    }

    let spacing_widths = 2.5;
    apply_single_ended_meander(
        engine,
        short_net,
        long_len,
        tol_nm,
        false,
        amp_max_nm.min(pair.uncoupled_max_nm / 2),
        spacing_widths,
        true,
        Some(pair),
    )
}

pub fn apply_length_matching_in_tx(
    engine: &mut Engine,
    state: &DiffPairRunState,
) -> Result<(), Failure> {
    let plan = &state.plan;
    let mut pair_by_net: BTreeMap<u32, usize> = BTreeMap::new();
    for (idx, p) in plan.pairs.iter().enumerate() {
        pair_by_net.insert(p.pos_net, idx);
        pair_by_net.insert(p.neg_net, idx);
    }

    unwrap_tx(engine.transaction(|tx| {
        let eng = &mut *tx.engine;

        for pair in &plan.pairs {
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
