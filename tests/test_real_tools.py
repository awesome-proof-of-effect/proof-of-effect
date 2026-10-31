"""Integration tests against real tools. Skipped when a tool is not on PATH/POE_TEST_TOOLPATH."""
import pytest

from conftest import have, tool_env


def _verdict(found, file, subject):
    assert (file, subject) in found, sorted(found)
    return found[(file, subject)]["verdict"]


@pytest.mark.skipif(not have("tsc"), reason="tsc not installed")
def test_tsconfig_misnested_option_is_a_no_op(repo):
    repo.write("src/index.ts", "export const x: number = 1;\n")
    repo.write("tsconfig.json", '{\n  // base\n  "compilerOptions": {"target": "es2020"},\n  "include": ["src"]\n}\n')
    repo.commit()
    repo.write("tsconfig.json", '{\n  "strict": true,\n  "compilerOptions": {"target": "es2020", '
                                '"noUncheckedIndexedAccess": true},\n  "include": ["src"],\n}\n')
    code, found = repo.check(env=tool_env())
    assert _verdict(found, "tsconfig.json", "strict") == "NO-OP"
    assert _verdict(found, "tsconfig.json", "compilerOptions.noUncheckedIndexedAccess") == "EFFECTIVE"
    assert code == 1


@pytest.mark.skipif(not have("tsc"), reason="tsc not installed")
def test_tsconfig_unknown_option_and_orphan_file(repo):
    repo.write("src/index.ts", "export const x = 1;\n")
    repo.write("tsconfig.json", '{"compilerOptions": {"target": "es2020"}, "include": ["src"]}\n')
    repo.commit()
    repo.write("tsconfig.json", '{"compilerOptions": {"target": "es2020", "strictt": true}, "include": ["src"]}\n')
    repo.write("tsconfig.build.json", '{"extends": "./tsconfig.json", "compilerOptions": {"sourceMap": true}}\n')
    _, found = repo.check(env=tool_env())
    assert _verdict(found, "tsconfig.json", "compilerOptions.strictt") == "UNKNOWN-KEY"
    assert _verdict(found, "tsconfig.build.json", "(whole file)") == "NOT-READ"


@pytest.mark.skipif(not have("tsc"), reason="tsc not installed")
def test_tsc_flags_use_the_full_option_list(repo):
    repo.write("package.json", '{"scripts": {"typecheck": "tsc --noEmit"}}\n')
    repo.commit()
    repo.write("package.json", '{"scripts": {"typecheck": '
                               '"tsc --noEmit --noUncheckedIndexedAccess --strictNullCheck"}}\n')
    code, found = repo.check("--only", "cli", env=tool_env())
    assert _verdict(found, "package.json", "tsc --noUncheckedIndexedAccess") == "EXISTS"
    assert _verdict(found, "package.json", "tsc --strictNullCheck") == "PHANTOM-FLAG"
    assert code == 1


@pytest.mark.skipif(not have("eslint"), reason="eslint not installed")
def test_eslint_legacy_file_is_ignored_in_flat_mode(repo):
    repo.write("eslint.config.mjs", 'export default [{ rules: { "no-console": "warn" } }];\n')
    repo.write(".eslintrc.json", '{"rules": {"no-debugger": "error"}}\n')
    repo.write("src/a.js", "console.log(1);\n")
    repo.commit()
    repo.write(".eslintrc.json", '{"rules": {"no-debugger": "error", "no-alert": "error"}}\n')
    repo.write("eslint.config.mjs", 'export default [{ rules: { "no-console": "error" } }];\n')
    code, found = repo.check("--only", "eslint", env=tool_env())
    assert _verdict(found, ".eslintrc.json", "rules.no-alert") == "NO-OP"
    assert "eslint.config.mjs" in found[(".eslintrc.json", "rules.no-alert")]["evidence"]
    assert _verdict(found, "eslint.config.mjs", "(whole file)") == "EFFECTIVE"
    assert code == 1


@pytest.mark.skipif(not have("jest"), reason="jest not installed")
def test_jest_unknown_and_effective_options(repo):
    repo.write("package.json", '{"name": "t", "version": "1.0.0"}\n')
    repo.write("jest.config.json", '{"testTimeout": 5000}\n')
    repo.commit()
    repo.write("jest.config.json", '{"testTimeout": 10000, "testTimeoutt": 1}\n')
    _, found = repo.check("--only", "jest", env=tool_env())
    assert _verdict(found, "jest.config.json", "testTimeout") == "EFFECTIVE"
    assert _verdict(found, "jest.config.json", "testTimeoutt") == "UNKNOWN-KEY"


@pytest.mark.skipif(not have("ruff"), reason="ruff not installed")
def test_ruff_settings_and_typo_table(repo):
    repo.write("pkg/mod.py", "x = 1\n")
    repo.write("pyproject.toml", '[project]\nname = "t"\nversion = "0"\n\n[tool.ruff]\nline-length = 88\n')
    repo.commit()
    repo.write("pyproject.toml", '[project]\nname = "t"\nversion = "0.1"\n\n[tool.ruff]\nline-length = 100\n\n'
                                 '[tool.ruf]\nselect = ["E"]\n')
    _, found = repo.check(env=tool_env())
    assert _verdict(found, "pyproject.toml", "tool.ruff.line-length") == "EFFECTIVE"
    assert _verdict(found, "pyproject.toml", "tool.ruf") == "NOT-READ"
    assert not any(subject.startswith("project") for _, subject in found)


@pytest.mark.skipif(not have("ruff"), reason="ruff not installed")
def test_ruff_unknown_field(repo):
    repo.write("pkg/mod.py", "x = 1\n")
    repo.write("ruff.toml", "line-length = 88\n")
    repo.commit()
    repo.write("ruff.toml", "line-length = 88\nline-lenght = 100\n")
    _, found = repo.check("--only", "ruff", env=tool_env())
    assert _verdict(found, "ruff.toml", "line-lenght") == "UNKNOWN-KEY"


@pytest.mark.skipif(not have("pytest"), reason="pytest not installed")
def test_pytest_ini_options(repo):
    repo.write("tests/test_a.py", "def test_a():\n    assert True\n")
    repo.write("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    repo.commit()
    repo.write("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests", "more"]\ntimeoutt = 5\n')
    _, found = repo.check("--only", "pytest", env=tool_env())
    assert _verdict(found, "pyproject.toml", "tool.pytest.ini_options.testpaths") == "EFFECTIVE"
    assert _verdict(found, "pyproject.toml", "tool.pytest.ini_options.timeoutt") == "UNKNOWN-KEY"


@pytest.mark.skipif(not have("pytest"), reason="pytest not installed")
def test_pytest_reads_a_different_file(repo):
    repo.write("tests/test_a.py", "def test_a():\n    assert True\n")
    repo.write("pytest.ini", "[pytest]\ntestpaths = tests\n")
    repo.write("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "-q"\n')
    repo.commit()
    repo.write("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "-x"\n')
    _, found = repo.check("--only", "pytest", env=tool_env())
    assert _verdict(found, "pyproject.toml", "tool.pytest.ini_options.addopts") == "NO-OP"
    assert "loads pytest.ini" in found[("pyproject.toml", "tool.pytest.ini_options.addopts")]["evidence"]


@pytest.mark.skipif(not have("prettier"), reason="prettier not installed")
def test_prettier_effective_and_unknown_option(repo):
    repo.write("src/a.ts", "export const x = 1\n")
    repo.write(".prettierrc.json", '{"semi": false, "tabWidth": 2}\n')
    repo.commit()
    repo.write(".prettierrc.json", '{"semi": false, "tabWidth": 4, "tabWidthh": 8}\n')
    _, found = repo.check("--only", "prettier", env=tool_env())
    assert _verdict(found, ".prettierrc.json", "tabWidth") == "EFFECTIVE"
    assert _verdict(found, ".prettierrc.json", "tabWidthh") == "UNKNOWN-KEY"


@pytest.mark.skipif(not have("prettier"), reason="prettier not installed")
def test_prettier_override_makes_a_key_effective(repo):
    repo.write("src/a.ts", "export const x = 1\n")
    repo.write(".prettierrc.json",
               '{"semi": true, "overrides": [{"files": "*.ts", "options": {"tabWidth": 2}}]}\n')
    repo.commit()
    # Changing the *.ts override's tabWidth changes the options Prettier resolves for src/a.ts.
    repo.write(".prettierrc.json",
               '{"semi": true, "overrides": [{"files": "*.ts", "options": {"tabWidth": 8}}]}\n')
    _, found = repo.check("--only", "prettier", env=tool_env())
    assert _verdict(found, ".prettierrc.json", "overrides[0].options.tabWidth") == "EFFECTIVE"


@pytest.mark.skipif(not have("prettier"), reason="prettier not installed")
def test_prettier_package_json_key_ignored_when_config_file_exists(repo):
    # Prettier searches upward from the file and uses the first config it finds: the closer
    # src/.prettierrc.json wins, so the root package.json "prettier" key never applies to src/a.ts.
    repo.write("src/a.ts", "export const x = 1\n")
    repo.write("src/.prettierrc.json", '{"semi": false}\n')
    repo.write("package.json", '{"name": "t", "version": "1.0.0", "prettier": {"tabWidth": 2}}\n')
    repo.commit()
    repo.write("package.json", '{"name": "t", "version": "1.0.0", "prettier": {"tabWidth": 8}}\n')
    _, found = repo.check("--only", "prettier", env=tool_env())
    assert _verdict(found, "package.json", "prettier.tabWidth") == "NO-OP"
    assert "src/.prettierrc.json" in found[("package.json", "prettier.tabWidth")]["evidence"]
