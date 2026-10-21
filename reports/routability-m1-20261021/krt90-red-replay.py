import ast
import math
import subprocess
from kicad_route import campaign
source = subprocess.check_output(["git", "show", "557ae5758c06b050508dd8016f5c7b6abc2a83f8:src/kicad_route/campaign.py"], text=True)
node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "rank_key")
ns = {"math": math}
exec(compile(ast.Module(body=[node], type_ignores=[]), "baseline-campaign.py", "exec"), ns)
campaign.rank_key = ns["rank_key"]
