//! Case-sensitive shell patterns used by the native board's --nets filter.
use crate::budget::WorkBudget;
use crate::model::{Failure, Input, Status};
use std::collections::BTreeSet;

#[derive(Debug)]
enum Token {
    Literal(char),
    Any,
    Star,
    Class {
        negated: bool,
        ranges: Vec<(char, char)>,
    },
}

impl Token {
    fn accepts(&self, c: char, work: &WorkBudget) -> Result<bool, Failure> {
        Ok(match self {
            Self::Literal(literal) => c == *literal,
            Self::Any => true,
            Self::Class { negated, ranges } => {
                for &(lo, hi) in ranges {
                    work.charge(1)?;
                    if lo <= c && c <= hi {
                        return Ok(!negated);
                    }
                }
                *negated
            }
            Self::Star => unreachable!("stars are handled by the matcher"),
        })
    }

    fn class(mut body: &[char]) -> Self {
        let mut negated = body.first() == Some(&'!');
        if negated {
            body = &body[1..];
        }
        let mut ranges = Vec::new();
        while let [lo, rest @ ..] = body {
            let end = match rest {
                ['-', hi, tail @ ..] => {
                    body = tail;
                    if hi < lo {
                        continue; // fnmatch drops descending ranges.
                    }
                    Some(*hi)
                }
                _ => {
                    body = rest;
                    None
                }
            };
            // Dropping a leading invalid range can expose a leading '!'.
            // fnmatch then treats it as negation, even in an otherwise empty class.
            if !negated && ranges.is_empty() && *lo == '!' {
                negated = true;
                if let Some(hi) = end {
                    ranges.extend([('-', '-'), (hi, hi)]);
                }
            } else {
                ranges.push((*lo, end.unwrap_or(*lo)));
            }
        }
        Self::Class { negated, ranges }
    }
}

struct NetPattern<'a> {
    source: &'a str,
    tokens: Vec<Token>,
    last_star: Option<usize>,
    match_leaf: bool,
}

impl<'a> NetPattern<'a> {
    fn new(source: &'a str, live: usize, work: &WorkBudget) -> Result<Self, Failure> {
        // The scans for wildcards, slash, closing bracket and class ranges are
        // linear: each class consumes its entire body before parsing resumes.
        work.charge(1 + source.len() as u64 * 4)?;
        let mut tokens = Vec::new();
        let mut last_star = None;
        if source.contains(['*', '?', '[']) {
            // At most one token or range per scalar, plus the temporary chars.
            work.search_storage(live + 2 * source.chars().count())?;
            let chars: Vec<_> = source.chars().collect();
            // An unmatched '[' is literal. Avoid repeatedly scanning the suffix
            // when a name contains a long run of unmatched opening brackets.
            let last_close = chars.iter().rposition(|&c| c == ']');
            let mut i = 0;
            while i < chars.len() {
                let c = chars[i];
                i += 1;
                let token = match c {
                    '*' if matches!(tokens.last(), Some(Token::Star)) => continue,
                    '*' => {
                        last_star = Some(tokens.len());
                        Token::Star
                    }
                    '?' => Token::Any,
                    '[' => {
                        let body_start = i;
                        let mut first_close = i + usize::from(chars.get(i) == Some(&'!'));
                        first_close += usize::from(chars.get(first_close) == Some(&']'));
                        if let Some(end) = last_close.filter(|&end| first_close <= end) {
                            let close = first_close
                                + chars[first_close..=end]
                                    .iter()
                                    .position(|&c| c == ']')
                                    .unwrap();
                            i = close + 1;
                            Token::class(&chars[body_start..close])
                        } else {
                            Token::Literal('[')
                        }
                    }
                    _ => Token::Literal(c),
                };
                tokens.push(token);
            }
        }
        Ok(Self {
            source,
            tokens,
            last_star,
            match_leaf: !source.contains('/'),
        })
    }

    fn full_match(&self, name: &str, work: &WorkBudget) -> Result<bool, Failure> {
        if self.tokens.is_empty() {
            if self.source.len() != name.len() {
                return Ok(false);
            }
            work.charge(name.len() as u64)?;
            return Ok(self.source == name);
        }
        let mut text = name.chars();
        // The suffix after the last '*' is anchored. Check it from the end
        // once, even if a nearly identical suffix occurs at every position.
        let tokens = if let Some(last_star) = self.last_star {
            for token in self.tokens[last_star + 1..].iter().rev() {
                work.charge(1)?;
                let Some(c) = text.next_back() else {
                    return Ok(false);
                };
                if !token.accepts(c, work)? {
                    return Ok(false);
                }
            }
            &self.tokens[..last_star]
        } else {
            &self.tokens[..]
        };
        let mut i = 0;
        let mut star = None;
        loop {
            work.charge(1)?;
            match tokens.get(i) {
                Some(Token::Star) => {
                    i += 1;
                    star = Some((i, text.clone()));
                    continue;
                }
                Some(token) => {
                    if let Some(c) = text.next() {
                        if token.accepts(c, work)? {
                            i += 1;
                            continue;
                        }
                    }
                }
                None if self.last_star.is_some() || text.as_str().is_empty() => return Ok(true),
                None => {}
            }
            // Retry the suffix with one more character absorbed by the last star.
            let Some((suffix, restart)) = &mut star else {
                return Ok(false);
            };
            if restart.next().is_none() {
                return Ok(false);
            }
            text = restart.clone();
            i = *suffix;
        }
    }

    fn matches(&self, name: &str, work: &WorkBudget) -> Result<bool, Failure> {
        work.charge(1)?;
        if self.full_match(name, work)? {
            return Ok(true);
        }
        if self.match_leaf {
            work.charge(name.len() as u64)?;
            if let Some((_, leaf)) = name.rsplit_once('/') {
                return self.full_match(leaf, work);
            }
        }
        Ok(false)
    }
}

pub(crate) fn active_nets(
    input: &Input,
    work: &WorkBudget,
) -> Result<Option<BTreeSet<u32>>, Failure> {
    let Some(opts) = input
        .routing_options
        .as_ref()
        .filter(|opts| !opts.net_filters.is_empty())
    else {
        return Ok(None);
    };
    work.search_storage(input.nets.len())?;
    work.charge(input.nets.len() as u64)?;
    // Resolve one pattern at a time. Exclusion wins regardless of token order.
    let mut selected = vec![None; input.nets.len()];
    let mut has_inclusion = false;
    for raw in &opts.net_filters {
        let exclusion = raw.starts_with('!');
        let body = raw.strip_prefix('!').unwrap_or(raw);
        if body.is_empty() {
            let reason = if exclusion {
                "--nets exclusion pattern cannot be empty"
            } else {
                "--nets filter pattern cannot be empty"
            };
            return Err(Failure::new(Status::Invalid, reason));
        }
        let pat = if body.starts_with(r"\!") {
            &body[1..]
        } else {
            body
        };
        let pattern = NetPattern::new(pat, selected.len(), work)?;
        has_inclusion |= !exclusion;
        let mut hit = false;
        for (net, choice) in input.nets.iter().zip(&mut selected) {
            work.charge(1)?;
            if net.id != 0 && pattern.matches(&net.name, work)? {
                hit = true;
                if exclusion || choice.is_none() {
                    *choice = Some(!exclusion);
                }
            }
        }
        if exclusion && !hit {
            drop(pattern);
            let literal = NetPattern::new(raw, selected.len(), work)?;
            for net in &input.nets {
                work.charge(1)?;
                if net.id != 0 && literal.matches(&net.name, work)? {
                    return Err(Failure::new(
                        Status::Invalid,
                        format!(
                            r"--nets {raw:?} excludes no nets and the board contains active-low net {:?}. Use '\{raw}' to select it or '!\{raw}' to exclude it",
                            net.name
                        ),
                    ));
                }
            }
        }
    }
    let mut out = BTreeSet::new();
    for (net, choice) in input.nets.iter().zip(&selected) {
        work.charge(1)?;
        if net.id != 0 && choice.unwrap_or(!has_inclusion) {
            work.search_storage(selected.len() + out.len() + 1)?;
            out.insert(net.id);
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn long_near_matches_stop_at_the_work_limit_and_brackets_use_linear_storage() {
        use crate::model::Limits;
        let work = WorkBudget::new(Limits {
            max_work: 4_096,
            ..Default::default()
        });
        let suffix = format!("*{}z", "a".repeat(128));
        let pat = NetPattern::new(&suffix, 0, &work).unwrap();
        let before = work.used();
        assert!(!pat.full_match(&"a".repeat(1024), &work).unwrap());
        assert_eq!(work.used() - before, 1);

        let interior = format!("{suffix}*");
        let partial = NetPattern::new(&interior, 0, &work).unwrap();
        let error = partial.full_match(&"a".repeat(1024), &work).unwrap_err();
        assert_eq!(error.status, Status::WorkExhausted);
        assert_eq!(error.work_used, 4_096);

        let brackets = "[".repeat(1024);
        let work = WorkBudget::new(Limits {
            max_search_entries: 2048,
            ..Default::default()
        });
        assert!(NetPattern::new(&brackets, 0, &work)
            .unwrap()
            .full_match(&brackets, &work)
            .unwrap());
        assert_eq!(work.used(), 4 * 1024 + 1 + 1024 + 1);
        let error = NetPattern::new(&brackets, 1, &work).err().unwrap();
        assert_eq!(error.status, Status::AdmissionExceeded);
    }

    #[test]
    fn shell_classes_and_literals_match_case_and_unicode_scalars() {
        let cases = [
            ("SIG_[A-C]", "SIG_B", true),
            ("SIG_[A-C]", "SIG_a", false),
            ("[!a-c]", "d", true),
            ("[!a-c]", "b", false),
            ("[^a]", "^", true),
            ("[^a]", "b", false),
            ("[]-]", "]", true),
            ("[-a]", "-", true),
            ("[!]]", "]", false),
            ("[!]]", "[", true),
            ("[[]0]", "[0]", true),
            ("[0]", "[0]", false),
            ("[?][*]", "?*", true),
            (r"[\]", r"\", true),
            (r"\*", r"\abc", true),
            (r"\*", "*", false),
            ("[a", "[a", true),
            ("[!]", "[!]", true),
            ("[]", "]", false),
            ("[c-a]", "b", false),
            ("[!c-a]", "b", true),
            ("[c-a!]", "x", true),
            ("[c-a!-b]", "a", true),
            ("[c-a!-b]", "-", false),
            ("[c-a!-b]", "b", false),
            ("[a-c-e]", "-", true),
            ("[a-c-e]", "d", false),
            ("[β-δ]?", "γ🐈", true),
            ("?", "a\u{301}", false),
            ("?**?a*?", "🔌\nabaβ", true),
            ("a*b?c", "abxxc", false),
            ("*aba?c", "abaxabazc", true),
            ("*", "", true),
            ("", "", true),
            ("*", "\n", true),
        ];
        for (pat, text, expected) in cases {
            let work = WorkBudget::new(Default::default());
            assert_eq!(
                NetPattern::new(pat, 0, &work)
                    .unwrap()
                    .full_match(text, &work)
                    .unwrap(),
                expected,
                "{pat:?} {text:?}"
            );
        }
    }
}
