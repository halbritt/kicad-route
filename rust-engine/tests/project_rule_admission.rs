use kicad_route_engine::model::{Input, Status};
use kicad_route_engine::{run, Engine};
use serde_json::json;

fn board(rules: &str) -> Input {
    serde_json::from_value(json!({
        "version": 1, "source_id": "a".repeat(64), "layer": "F.Cu",
        "bounds": {"min":[0,0], "max":[10000000,5000000]},
        "nets":[{"id":1,"name":"SIG"}],
        "pads":[
          {"id":"00000000-0000-4000-8000-000000000001","net":1,"at":[1000000,2400000],"shape":{"kind":"circle","radius_nm":200000}},
          {"id":"00000000-0000-4000-8000-000000000002","net":1,"at":[9000000,2400000],"shape":{"kind":"circle","radius_nm":200000}}
        ], "tracks": [], "max_expansions":100000,
        "project_rules":json!({"meta":{"version":1}, "net_settings":{"meta":{"version":5},
            "classes":[{"name":"Default","track_width":0.2,"clearance":0.2,"via_diameter":0.6,"via_drill":0.3}]}}).to_string(),
        "custom_rules":format!("(version 1) {rules}")
    })).unwrap()
}

#[test]
fn absolute_length_counts_pad_to_die_and_rolls_back_at_the_boundary() {
    for (max, succeeds) in [("8.25", true), ("8.249999", false)] {
        let mut input = board(&format!(
            r#"(rule "SIG maximum" (condition "A.NetName == 'SIG'") (constraint length (max {max}mm)))"#
        ));
        input.board_design_settings = Some(
            serde_json::from_value(json!({
                "pad_to_die_length_nm":{"00000000-0000-4000-8000-000000000002":250000}
            }))
            .unwrap(),
        );
        let mut engine = Engine::new(input.clone()).unwrap();
        if succeeds {
            engine.route(1, 100000).unwrap();
            assert!(engine.copper().tracks().count() > 0);
        } else {
            let fail = engine.route(1, 100000).unwrap_err();
            assert_eq!(fail.status, Status::Unroutable, "{}", fail.reason);
            assert!(fail.reason.contains("8250000 nm"), "{}", fail.reason);
            assert_eq!(engine.copper().tracks().count(), 0);
            let all = run(input);
            assert_eq!(all.status, Status::Unroutable);
            assert!(all.added_tracks.is_empty());
        }
    }
}

#[test]
fn scope_precedence_and_ignore_follow_the_applicable_whole_net_rule() {
    let early = r#"(rule "early" (constraint length (max 1mm)))"#;
    let later =
        r#"(rule "SIG allowed" (condition "A.NetName == 'SIG'") (constraint length (max 8mm)))"#;
    let other =
        r#"(rule "other" (condition "A.NetName == 'OTHER'") (constraint length (max 1mm)))"#;
    let ignore = r#"(rule "ignored" (severity ignore) (constraint length (max 1mm)))"#;
    for rules in [
        format!("{early} {later}"),
        other.into(),
        format!("{early} {ignore}"),
    ] {
        let out = run(board(&rules));
        assert_eq!(out.status, Status::Complete, "{:?}", out.reason);
    }
}

#[test]
fn missing_rule_context_is_reported_before_routing() {
    for (selector, constraint, diagnostic) in [
        (
            "!(A.inDiffPair('*') && B.inDiffPair('USB'))",
            "clearance (min 0.3mm)",
            "inDiffPair",
        ),
        (
            "B.fromTo('J1-1','J2-1')",
            "hole_clearance (min 0.3mm)",
            "fromTo",
        ),
        (
            "A.fromTo('J1-1','J2-1')",
            "track_width (min 0.3mm)",
            "fromTo",
        ),
        ("A.Type == 'Via'", "length (max 0.1mm)", "whole-net maximum"),
        (
            "A.NetName == 'SIG'",
            "length (min 1mm)",
            "whole-net maximum",
        ),
        (
            "A.NetName == 'SIG'",
            "length (max 4ps)",
            "whole-net maximum",
        ),
    ] {
        let input = board(&format!(
            r#"(rule "control" (condition "{selector}") (constraint {constraint}))"#
        ));
        let out = run(input);
        assert_eq!(out.status, Status::Unsupported, "{:?}", out.reason);
        let why = out.reason.unwrap();
        assert!(why.contains("control") && why.contains(diagnostic), "{why}");
        assert_eq!(out.expanded_states, 0);
        assert!(out.added_tracks.is_empty());
    }
}

#[test]
fn netclass_pattern_cross_checks_native_members_and_reports_disagreement() {
    let mut input = board("");
    let mut p: serde_json::Value =
        serde_json::from_str(input.project_rules.as_ref().unwrap()).unwrap();
    p["net_settings"]["classes"]
        .as_array_mut()
        .unwrap()
        .push(json!({"name":"Wide","priority":0,"track_width":0.5}));
    p["net_settings"]["netclass_patterns"] = json!([{"pattern":"SIG?","netclass":"Wide"}]);
    input.project_rules = Some(p.to_string());
    let ok = run(input.clone());
    assert_eq!(ok.status, Status::Complete, "{:?}", ok.reason);
    assert!(!ok.added_tracks.is_empty());
    assert!(ok.added_tracks.iter().all(|t| t.width_nm == 500000));
    input.board_design_settings =
        Some(serde_json::from_value(json!({"netclass_assignments":{"SIG":["Default"]}})).unwrap());
    let mismatch = run(input);
    assert_eq!(mismatch.status, Status::Unsupported);
    assert!(mismatch.reason.unwrap().contains("SIG?"));
    assert!(mismatch.added_tracks.is_empty());
}

#[test]
fn whole_net_maximum_keeps_existing_branch_copper_in_the_length() {
    let mut input = board(r#"(rule "net maximum" (constraint length (max 8.9mm)))"#);
    assert_eq!(run(input.clone()).status, Status::Complete);
    input.tracks.push(
        serde_json::from_value(json!({
            "id":"00000000-0000-4000-8000-000000000003","net":1,
            "start":[5000000,2400000],"end":[5000000,3400000],"width_nm":200000,"locked":true
        }))
        .unwrap(),
    );
    let out = run(input);
    assert_eq!(out.status, Status::Unroutable, "{:?}", out.reason);
    assert!(out.reason.unwrap().contains("net maximum"));
    assert!(out.added_tracks.is_empty());
}
