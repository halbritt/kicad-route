// Independent exact geometry for small axis-aligned oracle worlds. Represent
// each body as a closed box plus circular radius (doubled to retain odd widths).
// Distance between two axis-aligned segments/boxes is their box-gap distance.
use crate::model::{Pad, Point, Shape, Track};
#[derive(Clone, Copy, Debug)]
pub struct Body {
    pub lo: Point,
    pub hi: Point,
    pub radius2: i64,
    is_track: bool,
}
impl Body {
    pub fn pad(p: &Pad) -> Self {
        match p.shape {
            Shape::Circle { radius_nm: r } => Self {
                lo: p.at,
                hi: p.at,
                is_track: false,
                radius2: 2 * r,
            },
            Shape::RoundRect {
                half_size_nm: h,
                radius_nm: r,
            } => Self {
                lo: [p.at[0] - h[0] + r, p.at[1] - h[1] + r],
                hi: [p.at[0] + h[0] - r, p.at[1] + h[1] - r],
                is_track: false,
                radius2: 2 * r,
            },
            Shape::Rect { half_size_nm: h } => Self {
                lo: [p.at[0] - h[0], p.at[1] - h[1]],
                hi: [p.at[0] + h[0], p.at[1] + h[1]],
                is_track: false,
                radius2: 0,
            },
            Shape::Union { .. }
            | Shape::BoxExact { .. }
            | Shape::Capsule { .. }
            | Shape::ConvexPolygon { .. } => {
                unimplemented!("use 2x/polygon oracle for M3.1 shapes")
            }
        }
    }
    pub fn track(t: &Track) -> Self {
        assert!(t.start[0] == t.end[0] || t.start[1] == t.end[1]);
        Self::segment(t.start, t.end, t.width_nm)
    }
    pub fn segment(a: Point, b: Point, radius2: i64) -> Self {
        Self {
            lo: [a[0].min(b[0]), a[1].min(b[1])],
            hi: [a[0].max(b[0]), a[1].max(b[1])],
            radius2,
            is_track: true,
        }
    }
    pub fn distance4(self, other: Self) -> i64 {
        let mut d = 0;
        for k in 0..2 {
            let gap = (self.lo[k] - other.hi[k])
                .max(other.lo[k] - self.hi[k])
                .max(0);
            d += gap * gap;
        }
        4 * d
    }
    pub fn contacts(self, other: Self) -> bool {
        if self.radius2 == 0 && other.radius2 == 0 {
            return (0..2).all(|k| self.hi[k].min(other.hi[k]) - self.lo[k].max(other.lo[k]) >= 1);
        }
        let radius2 = 2 * (self.radius2 / 2 + other.radius2 / 2 - 1);
        radius2 >= 0 && self.distance4(other) <= radius2 * radius2
    }
    /// For a rounded box, the minimum total span equals the shorter
    /// core dimension plus the diameter. The cap must penetrate by the
    /// smaller feature radius. All distances below are doubled.
    pub fn accepts_landing(self, cap: Self) -> bool {
        let distance4 = self.distance4(cap);
        let own = 2 * (self.radius2 / 2);
        let incoming = 2 * (cap.radius2 / 2);
        let span = if self.is_track {
            0
        } else {
            (self.hi[0] - self.lo[0]).min(self.hi[1] - self.lo[1])
        };
        let required = (own + span).min(incoming).max(2);
        let reach = own + incoming - required;
        reach >= 0 && distance4 <= reach * reach && self.contacts(cap)
    }
    pub fn accepts_clipped(self, cap: Self) -> bool {
        if self.is_track {
            self.accepts_landing(cap)
        } else {
            self.contacts(cap)
        }
    }
    pub fn violates(self, other: Self, clearance: i64) -> bool {
        let radius2 = self.radius2 + other.radius2 + 2 * clearance;
        self.distance4(other) < radius2 * radius2
    }
}
