import tempfile
import unittest
from pathlib import Path

from agent_wrapper.server import (
    add_recent_folder,
    build_argv,
    load_recent_folders,
    parse_ollama_tags,
    parse_opencode_models,
    remember_folder,
)


class ParseOpencodeModelsTests(unittest.TestCase):
    def test_groups_models_by_source(self):
        stdout = "opencode/big-pickle\nollama/gemma4:e4b\nopenai/gpt-5.6-terra\nopenai/gpt-5.6-terra-fast\n"

        result = parse_opencode_models(stdout)

        self.assertEqual(
            result,
            {
                "opencode": ["big-pickle"],
                "ollama": ["gemma4:e4b"],
                "openai": ["gpt-5.6-terra", "gpt-5.6-terra-fast"],
            },
        )

    def test_ignores_lines_without_slash(self):
        stdout = "some warning line\nopenai/gpt-5.6-terra\n"

        result = parse_opencode_models(stdout)

        self.assertEqual(result, {"openai": ["gpt-5.6-terra"]})

    def test_empty_output_yields_empty_dict(self):
        self.assertEqual(parse_opencode_models(""), {})


class ParseOllamaTagsTests(unittest.TestCase):
    def test_extracts_names(self):
        payload = {"models": [{"name": "qwen3.8:27b"}, {"name": "gemma4:e4b"}]}

        self.assertEqual(parse_ollama_tags(payload), ["qwen3.8:27b", "gemma4:e4b"])

    def test_missing_models_key_yields_empty_list(self):
        self.assertEqual(parse_ollama_tags({}), [])


class BuildArgvTests(unittest.TestCase):
    def test_claude_without_model(self):
        self.assertEqual(build_argv("claude", "", ""), ["claude"])

    def test_claude_with_model(self):
        self.assertEqual(
            build_argv("claude", "anthropic", "sonnet"), ["claude", "--model", "sonnet"]
        )

    def test_codex_with_model(self):
        self.assertEqual(
            build_argv("codex", "openai", "gpt-5.2"), ["codex", "-m", "gpt-5.2"]
        )

    def test_opencode_with_source_and_model(self):
        result = build_argv(
            "opencode", "ollama", "gemma4:e4b", opencode_exe="opencode.exe"
        )

        self.assertEqual(result, ["opencode.exe", "-m", "ollama/gemma4:e4b"])

    def test_opencode_without_model_uses_bare_exe(self):
        result = build_argv("opencode", "", "", opencode_exe="opencode.exe")

        self.assertEqual(result, ["opencode.exe"])

    def test_unknown_harness_raises(self):
        with self.assertRaises(ValueError):
            build_argv("unknown", "", "")


class AddRecentFolderTests(unittest.TestCase):
    def test_new_folder_is_prepended(self):
        result = add_recent_folder(["C:\\a", "C:\\b"], "C:\\c")

        self.assertEqual(result, ["C:\\c", "C:\\a", "C:\\b"])

    def test_existing_folder_moves_to_front(self):
        result = add_recent_folder(["C:\\a", "C:\\b"], "C:\\b")

        self.assertEqual(result, ["C:\\b", "C:\\a"])

    def test_truncates_to_limit(self):
        result = add_recent_folder(["C:\\a", "C:\\b"], "C:\\c", limit=2)

        self.assertEqual(result, ["C:\\c", "C:\\a"])


class RecentFoldersPersistenceTests(unittest.TestCase):
    def test_load_missing_file_returns_empty_list(self):
        path = Path(tempfile.mkdtemp()) / "does-not-exist" / "recent_folders.json"

        self.assertEqual(load_recent_folders(path), [])

    def test_remember_folder_persists_and_dedupes(self):
        path = Path(tempfile.mkdtemp()) / "recent_folders.json"

        remember_folder("C:\\project-a", path)
        result = remember_folder("C:\\project-b", path)
        result = remember_folder("C:\\project-a", path)

        self.assertEqual(result, ["C:\\project-a", "C:\\project-b"])
        self.assertEqual(load_recent_folders(path), result)


if __name__ == "__main__":
    unittest.main()
