# Development targets for this checkout. Consumers call `kicad-route` directly.
KR := bin/kicad-route --out build/kicad-route

.PHONY: help install doctor update smoke evidence fixture
help:
	@echo "make install | doctor | update | smoke | evidence | fixture"
install:
	scripts/install.sh
doctor:
	$(KR) doctor
update:
	$(KR) update
smoke:
	$(KR) smoke
evidence:            ## smoke, then copy the structured evidence to reports/fixture for committing
	$(KR) smoke --publish reports/fixture
fixture:             ## regenerate the fixture board
	/usr/bin/python3 fixtures/route_demo/generate.py
