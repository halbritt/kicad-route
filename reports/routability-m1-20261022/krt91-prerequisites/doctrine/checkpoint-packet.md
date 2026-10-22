# Doctrine evidence packet

- Packet: pkt-f2738561084bd181 · Corpus: corpus-2026-07-12-a11702cc9217
- Question: How should kicad-route bind native experiment evidence and explicitly permitted differences so routing comparisons refuse incomplete or mismatched policy without expanding solver scope?
- Role: coding-agent · Task: implementation · Languages: Python · Risk: correctness · Signals: native board and footprint rule-area evidence; versioned experiment sidecar and strict comparison boundary
- Detail: compact · View: compact · Selection cost: 4775/5000 relative-routing-cost-unit

Precedence: explicit human authorization and stop conditions, then accepted repository contracts, tests, decisions, and current runtime facts, precede all doctrine below. This packet is retrieved guidance: it can justify a question, investigation, proposal, or bounded action; retrieval never creates authority to select, execute, verify, or accept a change.

## Activated concepts (14)
- **universal-minimize-simultaneous-uncertainty** — Organize investigation and change so one uncertain behavior, dependency, design choice, or performance hypothesis is tested at a time with close feedback.
- **universal-evidence-before-intervention** — An engineering intervention is earned only by evidence capable of establishing the claimed problem and distinguishing the proposed response from no change or a lower-cost response.
- **testing-test-first-feedback** — Writing a failing behavioral test before implementation is valuable when it clarifies an accepted contract, reproduces a defect, or creates a close preservation gate; it is not proof that the resulting design is coherent.
- **python-repository-shaped-idiom** — Pythonic form is contextual interoperability with the repository's supported interpreter, dependencies, tooling, neighboring code, and public behavior, not permission for style-only modernization.
- **universal-repository-contract-precedence** — Explicit task authority, accepted repository decisions, executable contracts, and current runtime facts govern the work before any generic engineering school or source preference.
- **implementation-repository-language-conformance** — A language idiom or mechanism is appropriate only when current repository versions, tooling, neighboring code, public compatibility, and runtime semantics support it and it improves the authorized task.
- **python-structured-cleanup** — Files, locks, transactions, temporary state, and asynchronous resources should use with, async with, contextlib, or an equivalent structured owner so setup and cleanup remain paired across success, exceptions, and cancellation.
- **python-text-bytes-boundary** — Decode bytes to text at a known ingress and encode text at egress with an explicit codec, error, normalization, and loss policy; leave opaque binary payloads uninterpreted.
- **universal-local-reasoning** — Place knowledge, state, invariants, and behavior so a maintainer can understand and change one responsibility without reconstructing distant hidden state or call order.
- **universal-separate-semantic-structural-change** — Feature behavior, defect repair, refactoring, architecture, migration, optimization, cleanup, deletion, and dependency changes remain distinct checkpoints even when one task needs several.
- **python-mutable-ownership** — Because Python assignment and calls share object references, every stored or mutated input needs an explicit borrowed, consumed, copied, shared, or owned contract.
- **universal-preserve-behavior-by-default** — Existing observable behavior remains protected unless an accepted contract explicitly identifies what may change and who can accept it.
- **agent-conduct-authority-bounded-action** — Observation, diagnosis, recommendation, selection, execution, verification, and acceptance are distinct authority levels; technical access and sufficient evidence at one level do not grant the next.
- **universal-no-change-option** — Leave code or architecture unchanged when demonstrated pressure and expected benefit do not exceed intervention, migration, indirection, learning, and operational cost.

## Demoted doctrine (stubs: 15)
- **implementation-concurrency-progress-contract** — Concurrent correctness requires progress as well as race freedom: designs must account for deadlock, livelock, starvation, lock ordering, and fairness across every contended resource. (books/concurrency-in-go/chapters/005-chapter-1-an-introduction-to-concurrency.md :: Deadlocks, Livelocks, and Starvation)
- **implementation-readiness** — Consequential coding should begin only after the intended outcome, affected contracts, placement constraints, failure and resource policy, verification route, and integration path are understood at a depth proportional to consequence and uncertainty. (books/code-complete-2nd-edition-v413hav/chapters/008-chapter-3-measure-twice-cut-once-upstream-prerequisites.md :: cc2e.com/0386 Checklist: Upstream Prerequisites)
- **implementation-risk-driven-tests** — Tests and characterization should protect observable contracts, invariants, boundary values, state transitions, failure and resource paths, and repaired regressions at the smallest level that can observe them without binding to irrelevant internals. (books/100-go-mistakes-and-how-to-avoid-them-teiva-harsanyi-bibis-ir/chapters/015-chapter-11-testing.md :: 11.2 #83: Not enabling the -race flag)
- **operations-credential-custody** — Production credentials are high-value secrets requiring dedicated custody - kept out of source control, images, and installation directories; readable only by the owning account or held in a vault whose decryption keys live elsewhere; expunged from process memory promptly; and excluded from core dumps and support bundles. (books/release-it-design-and-deploy-production-ready-software-pdfdrive/chapters/014-chapter-8-processes-on-machines.md :: Configuration)
- **refactoring-behavior-preservation** — Unless change is authorized, externally observable behavior remains invariant across structural work. (books/programming-working-effectively-with-legacy-code/chapters/008-chapter-1-changing-software.md :: Improving Design)
- **refactoring-change-type-classification** — A change label is valid only when its purpose and allowable observable delta match the actual work. (books/programming-working-effectively-with-legacy-code/chapters/008-chapter-1-changing-software.md :: Four Reasons to Change Software)
- **refactoring-stop-backtrack-escalate** — The agent must stop when it can no longer state what changed, what remains invariant, why a check failed, or whether it has authority. (books/programming-working-effectively-with-legacy-code/chapters/021-chapter-13-i-need-to-make-a-change-but-i-don-t-know-what-tests-to-write.md :: When You Find Bugs)
- **repository-assessment-metrics-not-performance** — Commit, LOC, ownership, defect-attribution, hotspot, and knowledge-map data must never be converted into individual productivity or performance judgments. (books/dokumen-pub-software-design-x-rays-fix-technical-debt-with-behavioral-code-analysis-1nbsped-1680502727-978-1680502725/chapters/017-appendix-a1-the-hazards-of-productivity-and-performance-metrics.md :: Adaptive Behavior and the Destruction of a Data Source)
- **testing-integration-scope-selection** — Keep integration tests few and targeted - cover as many edge cases as possible with unit tests, then use integration tests for the longest happy path that touches all out-of-process dependencies plus only those edge cases unit tests cannot exercise, letting fail-fast crashes stand in for tests of wiring bugs that manifest immediately without corrupting state. (books/manning-unit-testing-principles-practices-and-patterns-2020-1/chapters/015-chapter-8-why-integration-testing.md :: 8.1 What is an integration test?)
- **testing-suite-net-value** — A test belongs in the suite only while its regression-protection and design value clearly exceeds its recurring costs of refactoring alongside the code, execution on every change, false-alarm triage, and reading, because test code is itself a liability. (books/manning-unit-testing-principles-practices-and-patterns-2020-1/chapters/006-chapter-1-the-goal-of-unit-testing.md :: 1.2 The goal of unit testing)
- **testing-unit-of-behavior** — A unit test should verify one unit of observable behavior meaningful in the problem domain - which may span a method, a class, or several classes - rather than one unit of code per class. (books/manning-unit-testing-principles-practices-and-patterns-2020-1/chapters/007-chapter-2-what-is-a-unit-test.md :: 2.3.1 Unit testing one class at a time)
- **universal-change-amplification** — Structure is costly when one conceptual change repeatedly forces disproportionate, risky, or cross-owner edits; restructuring is justified only when it reduces that demonstrated amplification. (books/clean-architecture-a-craftsman-guide-to-software-structure-and-design/chapters/023-chapter-13-component-cohesion.md :: THE COMMON CLOSURE PRINCIPLE)
- **universal-earned-abstraction** — Introduce or retain an abstraction only when it hides coherent knowledge or mechanism and reduces total caller reasoning, coordinated change, or substitution cost more than its interface and indirection add. (books/100-go-mistakes-and-how-to-avoid-them-teiva-harsanyi-bibis-ir/chapters/006-chapter-2-code-and-project-organization.md :: 2.5 #5: Interface pollution)
- **universal-measure-claimed-improvement** — A claim that work improves performance, maintainability, reliability, architecture fitness, or process needs a preserved baseline and evidence on that same dimension. (books/dokumen-pub-software-design-x-rays-fix-technical-debt-with-behavioral-code-analysis-1nbsped-1680502727-978-1680502725/chapters/007-chapter-2-identify-code-with-high-interest-rates.md :: Follow Up on Improvements to Rails)
- **universal-reversible-reviewable-change** — Choose the smallest coherent step that can be independently understood, verified, and reversed or safely advanced. (books/dokumen-pub-software-engineering-at-google-lessons-learned-from-programming-over-time-1nbsped-1492082791-9781492082798/chapters/017-chapter-9-code-review.md :: Write Small Changes)

## Operational layers
- Procedures: proc-assess-authority-to-act, proc-determine-abstraction-earned, proc-determine-tests-characterization, proc-place-new-behavior, proc-plan-implementation
- Lenses: lens-python
- Evidence classes: evidence-explicit-user-requirements, evidence-repository-contracts, evidence-static-source-structure, evidence-tests
- Change types: change-feature-implementation
- Authority ceiling: execute

## Unmet evidence obligations by concept (23)
- implementation-repository-language-conformance: CI and build matrix | formatter and static-tool configuration
- python-mutable-ownership: concurrency | identity and equality contract | lifetime and size | nested object graph
- python-repository-shaped-idiom: formatter linter and type-checker configuration
- python-structured-cleanup: acquisition and release paths | exception and cancellation behavior | nesting order | owner and lifetime | resource ownership and failure policy
- python-text-bytes-boundary: external format and domain text contract | normalization requirements | protocol or file metadata | representative non-ASCII data | round-trip and failure tests
- testing-test-first-feedback: design uncertainty assessment
- universal-no-change-option: actual current cost/risk or absence within a stated interval | expected future change from accepted plans | intervention cost and uncertainty | latent security, safety, data, durability, and compatibility check | proc-decide-leave-code-alone

## Conflicts (4)
- conflict-deep-modules-vs-small-units — deep-module: Concentrate related complexity behind a small stable interface so callers reason locally. vs. small-unit: Extract small cohesive routines or classes so each operation has one clear purpose and can be changed or tested independently. Selection rule: Compare caller simplification and hidden benefit against interface, navigation, change, and review cost; select the smallest number of cohesive units that minimizes total reasoning and change amplification, not a target size.
- conflict-refactor-now-vs-leave-alone — refactor-opportunistically: Improve nearby structure while understanding is fresh to prevent compounding design decay. vs. leave-stable-code: Avoid regression and opportunity cost where unattractive code is stable or outside the authorized change surface. Selection rule: Refactor only when demonstrated pressure and near-term benefit clear protection, integration, and opportunity costs; otherwise record the signal and revisit trigger without intervention.
- conflict-uniformity-vs-context-sensitive-design — repository-uniformity: Consistency lowers cognitive load, tool cost, and accidental variation. vs. local-fit: Different quality, domain, runtime, or language forces may require intentionally different structures. Selection rule: Conform by default; introduce a documented local exception or migration only when distinct forces produce material benefit exceeding ecosystem and review cost.
- conflict-upfront-vs-evolutionary-architecture — decide-up-front: Resolve high-cost, safety-critical, procurement, data, or topology decisions before implementation makes them expensive or impossible. vs. evolve-from-feedback: Delay reversible decisions and evolve structure through implementation, domain, and runtime feedback. Selection rule: Classify decisions by reversibility, consequence, information value, and decision deadline; decide only the irreversible/high-consequence minimum early and establish feedback plus revisit triggers for the rest.

## Prohibition triggers (15)
- prohibit-authority-inference-from-access: The requested transition exceeds the explicit task authority or lacks the designated decision owner.
- prohibit-boundary-without-force: No independent policy, invariant, quality, trust, volatility, release, deployment, ownership, or semantic force exists.
- prohibit-concurrency-first-optimization: The sequential path has not been simplified/profiled or coordination, ordering, ownership, cancellation, and downstream capacity are unmeasured.
- prohibit-coverage-as-adequacy-proof: The claim lacks contract, boundary, state, failure, data-flow, mutation, or risk analysis.
- prohibit-generic-doctrine-over-local-contract: The applicable local instruction, accepted decision, executable contract, or current observation conflicts with generic doctrine.
- prohibit-go-error-discard: The operation can fail and no explicit best-effort or loss policy owns the consequence.
- prohibit-implicit-resource-lifecycle: A resource can escape, block, leak, outlive its owner, or be acquired along multiple paths.
- prohibit-indirection-by-slogan: The proposal names no hidden decision, substitute behavior, independent consumer, volatility, ownership, deployment, or policy boundary.
- prohibit-mutable-default-state: The default can be mutated or escape the call.
- prohibit-python-hints-as-runtime-validation: Values originate outside trusted typed code or correctness depends on value-level constraints.
- prohibit-size-only-decomposition: Size is the only stated concern; no independently evolving responsibility, change coupling, interface leverage, or review problem is demonstrated.
- prohibit-syntax-only-abstraction: Shared knowledge, authority, semantics, consumers, or reasons to change have not been established.
- prohibit-test-change-without-contract-reconciliation: The test expresses supported behavior or its authority is unresolved.
- prohibit-unknown-consequential-implementation: One missing input can plausibly change the chosen behavior or create irreversible impact.
- prohibit-unspecified-performance-optimization: Operation, workload, boundary, environment, resource/statistic, correctness precondition, or target is missing.
