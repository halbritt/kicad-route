# Development targets for this checkout. Consumers call `kicad-route` directly.
KR := bin/kicad-route --out build/kicad-route

.PHONY: help install doctor update smoke evidence fixture state-manifest determinism
help:
	@echo "make install | doctor | update | state-manifest | determinism | smoke | evidence | fixture"
install:
	scripts/install.sh
doctor:
	$(KR) doctor
update:
	$(KR) update
state-manifest:      ## record what is installed under STATE in STATE/manifest.json (no downloads)
	$(KR) state-manifest
determinism:         ## route the fixture twice with KRT, compare, record in the state manifest and reports/
	$(KR) determinism --backend krt --record --publish reports/determinism
smoke:
	$(KR) smoke
evidence:            ## smoke, then copy the structured evidence to reports/fixture for committing
	$(KR) smoke --publish reports/fixture
fixture:             ## regenerate the fixture board
	/usr/bin/python3 fixtures/route_demo/generate.py

# Needs only the installed numeric routing environment and cached locked crates.
KERNEL_PYTHON ?= $(if $(KICAD_ROUTE_STATE),$(KICAD_ROUTE_STATE),.)/.venv-krt/bin/python
.PHONY: kernel
kernel:
	$(KERNEL_PYTHON) scripts/kernel-tests.py
