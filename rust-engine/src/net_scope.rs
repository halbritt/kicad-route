//! Case-sensitive shell patterns used by the native board's --nets filter.
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
    fn accepts(&self, c: char) -> bool {
        match self {
            Self::Literal(literal) => c == *literal,
            Self::Any => true,
            Self::Class { negated, ranges } => {
                ranges.iter().any(|&(lo, hi)| lo <= c && c <= hi) != *negated
            }
            Self::Star => unreachable!("stars are handled by the matcher"),
        }
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
    match_leaf: bool,
}

impl<'a> NetPattern<'a> {
    fn new(source: &'a str) -> Self {
        let mut tokens = Vec::new();
        if source.contains(['*', '?', '[']) {
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
                    '*' => Token::Star,
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
        Self {
            source,
            tokens,
            match_leaf: !source.contains('/'),
        }
    }

    fn full_match(&self, name: &str) -> bool {
        if self.tokens.is_empty() {
            return self.source == name;
        }
        let mut text = name.chars();
        let mut i = 0;
        let mut star = None;
        loop {
            match self.tokens.get(i) {
                Some(Token::Star) => {
                    i += 1;
                    star = Some((i, text.clone()));
                    continue;
                }
                Some(token) if text.next().is_some_and(|c| token.accepts(c)) => {
                    i += 1;
                    continue;
                }
                None if text.as_str().is_empty() => return true,
                _ => {}
            }
            // Retry the suffix with one more character absorbed by the last star.
            let Some((suffix, restart)) = &mut star else {
                return false;
            };
            if restart.next().is_none() {
                return false;
            }
            text = restart.clone();
            i = *suffix;
        }
    }

    fn matches(&self, name: &str) -> bool {
        self.full_match(name)
            || (self.match_leaf
                && name
                    .rsplit_once('/')
                    .is_some_and(|(_, leaf)| self.full_match(leaf)))
    }
}

pub(crate) fn active_nets(input: &Input) -> Result<Option<BTreeSet<u32>>, Failure> {
    let Some(opts) = input
        .routing_options
        .as_ref()
        .filter(|opts| !opts.net_filters.is_empty())
    else {
        return Ok(None);
    };
    let mut pos = Vec::new();
    let mut neg = Vec::new();
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
        let pattern = NetPattern::new(if body.starts_with(r"\!") {
            &body[1..]
        } else {
            body
        });
        if exclusion {
            let names = || input.nets.iter().filter(|n| n.id != 0);
            if !names().any(|n| pattern.matches(&n.name)) {
                let literal = NetPattern::new(raw);
                if let Some(n) = names().find(|n| literal.matches(&n.name)) {
                    return Err(Failure::new(
                        Status::Invalid,
                        format!(
                            r"--nets {raw:?} excludes no nets and the board contains active-low net {:?}. Use '\{raw}' to select it or '!\{raw}' to exclude it",
                            n.name
                        ),
                    ));
                }
            }
            neg.push(pattern);
        } else {
            pos.push(pattern);
        }
    }
    let mut out = BTreeSet::new();
    for n in &input.nets {
        if n.id == 0 {
            continue;
        }
        let pos_ok = pos.is_empty() || pos.iter().any(|p| p.matches(&n.name));
        let neg_hit = neg.iter().any(|p| p.matches(&n.name));
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

#[cfg(test)]
mod tests {
    use super::*;

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
            assert_eq!(
                NetPattern::new(pat).full_match(text),
                expected,
                "{pat:?} {text:?}"
            );
        }
    }
}
