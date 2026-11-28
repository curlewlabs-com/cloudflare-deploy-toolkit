#!/usr/bin/env python3
"""Suite for what a deploy run is asked to do, and what it refuses at startup.

A wrong SHA mislabels an immutable upload. A missing migration hook must fail
before the first change to the deployment.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

MODULE = pathlib.Path(__file__).resolve().parents[2] / "deploy-lib" / "deploy_config.py"

sys.path.insert(0, str(MODULE.parent))

import deploy_config

# The smallest environment that describes a deploy. Cases start from this and
# remove or replace one entry, so what each one is about is the diff.
COMPLETE = {
    "PREFLIGHT_CMD": "true",
    "DEPLOY_WRANGLER_ENV_STAGING": "staging",
    "MIGRATE_CMD": "true",
}


def staging(
    expected_serving_sha: str = "", **overrides: str
) -> deploy_config.DeployConfig:
    environ = {**COMPLETE, **overrides}
    return deploy_config.resolve(
        deploy_config.Invocation(
            env="staging",
            sha="abc1234",
            expected_serving_sha=expected_serving_sha,
        ),
        environ,
    )


class ArgumentParsing(unittest.TestCase):
    def test_the_environment_and_the_sha_come_off_argv(self) -> None:
        parsed = deploy_config.parse_args(["production", "--sha", "deadbee"])
        self.assertEqual(parsed.env, "production")
        self.assertEqual(parsed.sha, "deadbee")

    def test_the_expected_serving_commit_comes_off_argv(self) -> None:
        # An argument rather than a variable: a pipeline that predates the bind
        # refuses it as an unknown argument (the unknown-argument case below),
        # where it would ignore a variable and deploy unbound.
        full = "abc1234" + "0" * 33
        parsed = deploy_config.parse_args(
            ["production", "--sha", "abc1234", "--expected-serving-sha", full]
        )
        self.assertEqual(parsed.expected_serving_sha, full)
        self.assertEqual(
            deploy_config.parse_args(
                ["production", "--sha", "abc1234"]
            ).expected_serving_sha,
            "",
        )

    def test_an_expected_serving_flag_without_one_value_is_a_usage_error(
        self,
    ) -> None:
        # Read as "no expected build", each of these would deploy unbound.
        full = "abc1234" + "0" * 33
        for argv in (
            ["production", "--expected-serving-sha"],
            ["production", "--expected-serving-sha", ""],
            ["production", "--expected-serving-sha", full, "--expected-serving-sha"],
            [
                "production",
                "--expected-serving-sha",
                full,
                "--expected-serving-sha",
                full,
            ],
        ):
            with self.subTest(argv=argv), self.assertRaises(deploy_config.UsageError):
                deploy_config.parse_args(argv)

    def test_a_sha_flag_never_swallows_the_word_after_it(self) -> None:
        # A flag or an environment read as the sha drops it from the command
        # line: `--sha --expected-serving-sha X` would deploy unbound, and
        # `--sha production staging` would deploy staging.
        for argv in (
            ["production", "--sha", "--expected-serving-sha", "a" * 40],
            ["--sha", "production", "staging"],
            ["staging", "--sha", "-h"],
        ):
            with self.subTest(argv=argv), self.assertRaises(deploy_config.UsageError):
                deploy_config.parse_args(argv)

    def test_a_sha_flag_with_nothing_after_it_falls_back_to_the_default(self) -> None:
        # A form that instead consumed the next positional would silently deploy
        # the wrong environment.
        parsed = deploy_config.parse_args(["staging", "--sha"])
        self.assertEqual(parsed.env, "staging")
        self.assertTrue(parsed.sha)

    def test_an_unknown_argument_and_a_missing_environment_are_usage_errors(
        self,
    ) -> None:
        for argv in (["staging", "--wat"], ["staging", "--dry-run"], []):
            with self.subTest(argv=argv), self.assertRaises(deploy_config.UsageError):
                deploy_config.parse_args(argv)

    def test_help_is_a_usage_exit_rather_than_a_deploy(self) -> None:
        for flag in ("-h", "--help"):
            with self.subTest(flag=flag), self.assertRaises(deploy_config.UsageError):
                deploy_config.parse_args(["staging", flag])


class StartupRefusals(unittest.TestCase):
    def test_a_complete_environment_resolves(self) -> None:
        config = staging()
        self.assertEqual(config.wrangler_env, "staging")
        self.assertEqual(config.migrate_cmd, ("true",))

    def test_a_missing_migrate_command_refuses_before_anything_runs(self) -> None:
        environ = {
            key: value for key, value in COMPLETE.items() if key != "MIGRATE_CMD"
        }
        with self.assertRaises(deploy_config.ConfigError) as refusal:
            deploy_config.resolve(
                deploy_config.Invocation(env="staging", sha="abc1234"),
                environ,
            )
        self.assertIn("MIGRATE_CMD", str(refusal.exception))

    def test_config_for_the_other_environment_does_not_stand_in(self) -> None:
        # A production deploy reading the staging Worker environment is the failure
        # this refusal exists for.
        with self.assertRaises(deploy_config.ConfigError):
            deploy_config.resolve(
                deploy_config.Invocation(env="production", sha="abc1234"),
                COMPLETE,
            )

    def test_a_duration_the_pipeline_cannot_wait_refuses(self) -> None:
        for field, value in (
            ("DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS", "2 soon 15"),
            ("DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS", "2 -5 15"),
        ):
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(deploy_config.ConfigError) as refusal,
            ):
                staging(**{field: value})
            self.assertIn(field, str(refusal.exception))

    def test_an_empty_schedule_means_no_retries(self) -> None:
        self.assertEqual(
            staging(DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS=" ").retry_backoff, ()
        )

    def test_a_hook_path_may_carry_spaces_and_leading_arguments(self) -> None:
        # shlex rather than whitespace splitting, so a quoted path with a
        # space in it is expressible.
        config = staging(MIGRATE_CMD="'/opt/my tools/migrate.sh' --strict")
        self.assertEqual(config.migrate_cmd, ("/opt/my tools/migrate.sh", "--strict"))

    def test_a_missing_or_empty_preflight_refuses(self) -> None:
        # Preflight is the consumer's check that the live database can take
        # this deploy; a run without it would mutate unchecked.
        for empty in (None, ""):
            environ = dict(COMPLETE)
            if empty is None:
                del environ["PREFLIGHT_CMD"]
            else:
                environ["PREFLIGHT_CMD"] = empty
            with (
                self.subTest(empty=empty),
                self.assertRaises(deploy_config.ConfigError) as refusal,
            ):
                deploy_config.resolve(
                    deploy_config.Invocation("staging", "abcdef0"), environ
                )
            self.assertIn("PREFLIGHT_CMD", str(refusal.exception))

    def test_empty_postflight_skips_the_optional_hook(self) -> None:
        self.assertIsNone(staging(POSTFLIGHT_CMD="  ").postflight_cmd)

    def test_an_expected_serving_build_is_a_full_sha(self) -> None:
        # The bind requires the serving version's commit to equal this exactly,
        # so only a full lowercase SHA can ever match, and anything else is
        # refused at startup rather than at the bind.
        full = "abc1234" + "0" * 33
        self.assertEqual(staging(full).expected_serving_sha, full)
        self.assertEqual(staging().expected_serving_sha, "")
        for value in ("abc1234", full.upper()):
            with (
                self.subTest(value=value),
                self.assertRaises(deploy_config.ConfigError),
            ):
                staging(value)

    def test_the_commit_written_on_uploads_is_the_full_form_of_the_sha(
        self,
    ) -> None:
        # Every upload's message records the short form and the commit, so a
        # commit --sha does not abbreviate would leave the version
        # contradicting itself.
        full = "abc1234" + "0" * 33
        self.assertEqual(staging(DEPLOY_COMMIT_SHA=full).commit_sha, full)
        self.assertEqual(staging().commit_sha, "")
        for value in ("abc1234", full.upper(), "def5678" + "0" * 33):
            with (
                self.subTest(value=value),
                self.assertRaises(deploy_config.ConfigError),
            ):
                staging(DEPLOY_COMMIT_SHA=value)

    def test_an_expected_runtime_is_parsed_at_startup_and_unset_is_none(
        self,
    ) -> None:
        # Unset is the consumer that never asked for the check, and must make
        # no read. Set and empty is a consumer whose own read of its configured
        # runtime printed nothing, which must not pass for unset.
        self.assertIsNone(staging().expected_runtime)
        runtime = staging(
            DEPLOY_EXPECTED_RUNTIME=(
                '{"compatibility_date": "2025-01-01", "compatibility_flags": []}'
            )
        ).expected_runtime
        self.assertIsNotNone(runtime)
        assert runtime is not None
        self.assertEqual(runtime.compatibility_date, "2025-01-01")
        for value in ("", "2025-01-01", '{"compatibility_date": "2025-01-01"}'):
            with (
                self.subTest(value=value),
                self.assertRaises(deploy_config.ConfigError),
            ):
                staging(DEPLOY_EXPECTED_RUNTIME=value)


if __name__ == "__main__":
    unittest.main()
