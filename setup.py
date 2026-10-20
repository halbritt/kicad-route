"""Ship public fixture/schema resources without treating checkout tests as runtime."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class RuntimeResources(build_py):
    def run(self):
        super().run()
        root = Path(__file__).resolve().parent
        destination = Path(self.build_lib) / "kicad_route" / "resources"
        for name in ("fixtures", "schemas"):
            shutil.copytree(root / name, destination / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


setup(cmdclass={"build_py": RuntimeResources})
