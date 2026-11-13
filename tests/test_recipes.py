"""Engine recipes: the cost settings for a class of board reach the engine, and say so in the result."""
from helpers import run
from kicad_route import cli as route


def test_auto_picks_by_layer_count_and_a_name_is_taken_as_given():
    assert route.resolve_recipe("auto", 2) == "two-layer"
    assert route.resolve_recipe("auto", 4) == "default"
    assert route.resolve_recipe("auto", None) == "default"
    assert route.resolve_recipe("two-layer-bus", 4) == "two-layer-bus"
    # without the option a run chooses for the board, as with `auto`
    assert route.RECIPE_DEFAULT == "auto"
    assert route.resolve_recipe(None, 2) == "two-layer" and route.resolve_recipe(None, 4) == "default"
    assert route.resolve_recipe("default", 2) == "default"
    # a recipe only sets costs: nothing in one may relax a rule or change what is delivered
    allowed = {"--layer-costs", "--via-cost", "--direction-preference-cost", "--turn-cost"}
    for name, flags in route.RECIPES.items():
        assert {f for f in flags if f.startswith("--")} <= allowed, name


def engine_argv(result):
    return next(c["argv"] for c in result["process"]["commands"] if any(a.endswith("route.py") for a in c["argv"]))


def test_a_recipe_reaches_the_engine_before_backend_args_and_is_recorded():
    r, run_dir, result = run("run", "--backend", "builtin", "--budget", "120", "--default-clearance", "0.4",
                             "--recipe", "two-layer", "--backend-arg=--via-cost", "--backend-arg=300",
                             "--label", "smoke-recipe")
    assert r.returncode == 0, result["validation"]["reasons"]
    assert result["settings"]["recipe"] == "two-layer"
    argv = engine_argv(result)
    at = argv.index("--layer-costs")
    assert argv[at:at + 5] == ["--layer-costs", "1.0", "1.0", "--via-cost", "600"]
    # the caller's own value comes later on the command line, so the engine takes it
    assert [i for i, a in enumerate(argv) if a == "--via-cost"][-1] > at + 3
    assert argv[[i for i, a in enumerate(argv) if a == "--via-cost"][-1] + 1] == "300"
    assert result["routing"]["status"] == route.COMPLETE and result["validation"]["status"] == route.PASS


def test_recipe_default_gives_the_engine_its_own_costs():
    r, run_dir, result = run("run", "--backend", "builtin", "--budget", "120", "--default-clearance", "0.4",
                             "--recipe", "default", "--label", "smoke-no-recipe")
    assert "recipe" not in result["settings"]
    assert "--layer-costs" not in engine_argv(result)


def test_without_the_option_the_two_layer_fixture_gets_its_recipe():
    r, run_dir, result = run("run", "--backend", "builtin", "--budget", "120", "--default-clearance", "0.4",
                             "--label", "smoke-default-recipe")
    assert result["settings"]["recipe"] == "two-layer"
    assert "--layer-costs" in engine_argv(result)
    assert result["routing"]["status"] == route.COMPLETE and result["validation"]["status"] == route.PASS


def test_auto_gives_the_two_layer_fixture_its_recipe():
    r, run_dir, result = run("run", "--backend", "builtin", "--budget", "120", "--default-clearance", "0.4",
                             "--recipe", "auto", "--label", "smoke-auto-recipe")
    assert result["settings"]["recipe"] == "two-layer"
    assert "--layer-costs" in engine_argv(result)
    assert result["routing"]["status"] == route.COMPLETE and result["validation"]["status"] == route.PASS
