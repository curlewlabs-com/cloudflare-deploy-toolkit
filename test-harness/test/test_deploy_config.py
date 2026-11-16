#!/usr/bin/env python3
"""Suite for what a deploy run is asked to do, and what it refuses at startup.

The classes of case, and the second is the one worth having. Parsing argv
decides the sha stamped into every uploaded version's immutable annotations, so
a form that silently produces the wrong one is a version nobody can attribute
later. The refusals decide whether a misconfigured run stops before the first
wrangler call or part-way through a maintenance window - and the migrate hook is
the case that earns the class: the step that runs it is reached, on the windowed
body, only after the maintenance version is serving and the database is locked,
so a refusal deferred to that step costs a full three-layer recovery.
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
    "DEPLOY_DB_STAGING": "example-db-staging",
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
        self.assertEqual(config.mode, deploy_config.WINDOWED)
        self.assertEqual(config.db_name, "example-db-staging")
        self.assertEqual(config.wrangler_env, "staging")
        self.assertEqual(config.migrate_cmd, ("true",))

    def test_a_missing_migrate_command_refuses_before_anything_runs(self) -> None:
        # The whole reason this check is at startup: on the windowed body the
        # step that would have caught it runs after the maintenance version is
        # serving, so the consumer pays a three-layer recovery to learn it.
        environ = {
            key: value for key, value in COMPLETE.items() if key != "MIGRATE_CMD"
        }
        with self.assertRaises(deploy_config.ConfigError) as refusal:
            deploy_config.resolve(
                deploy_config.Invocation(env="staging", sha="abc1234"),
                environ,
            )
        self.assertIn("MIGRATE_CMD", str(refusal.exception))

    def test_an_unrecognized_mode_refuses_rather_than_choosing_one(self) -> None:
        with self.assertRaises(deploy_config.ConfigError) as refusal:
            staging(DEPLOY_PIPELINE_MODE="windowlss")
        self.assertIn("unrecognized DEPLOY_PIPELINE_MODE", str(refusal.exception))

    def test_an_unset_mode_is_the_windowed_body(self) -> None:
        # The conservative default: every protection on for a consumer that has
        # not opted into routing.
        self.assertEqual(staging().mode, deploy_config.WINDOWED)

    def test_config_for_the_other_environment_does_not_stand_in(self) -> None:
        # A production deploy reading the staging database name is the failure
        # this refusal exists for.
        with self.assertRaises(deploy_config.ConfigError):
            deploy_config.resolve(
                deploy_config.Invocation(env="production", sha="abc1234"),
                COMPLETE,
            )

    def test_a_duration_the_pipeline_cannot_wait_refuses(self) -> None:
        # The shell passed each of these to `sleep`, which failed at the moment
        # it was used - minutes into a deploy, on the windowed body after the
        # maintenance version is already serving. Negative counts as unwaitable
        # alongside non-numeric: a schedule the pipeline silently read as zero
        # is a retry budget nobody has.
        for field, value in (
            ("DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS", "2 soon 15"),
            ("DEPLOY_WRANGLER_RETRY_BACKOFF_SECONDS", "2 -5 15"),
            ("DEPLOY_VERIFY_BACKOFF_SECONDS", "1 2 later"),
            ("DEPLOY_DRAIN_SECONDS", "quickly"),
            ("DEPLOY_DRAIN_SECONDS", "-1"),
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

    def test_the_optional_hooks_are_absent_rather_than_empty(self) -> None:
        config = staging(PREFLIGHT_CMD="   ")
        self.assertIsNone(config.preflight_cmd)
        self.assertIsNone(config.postflight_cmd)

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


# What the relocation body adds to COMPLETE.
RELOCATING = {
    "DEPLOY_PIPELINE_MODE": "relocation",
    "DEPLOY_RELOCATION_SOURCE_DB_STAGING": "example-db-source",
    "RELOCATE_COPY_CMD": "scripts/copy.sh --exact",
}


class RelocationRefusals(unittest.TestCase):
    """The relocation body's config is refused at startup for the migrate hook's
    reason: its copy hook is reached only once the source is fenced, so a run
    that finds it missing there has paused writes for nothing."""

    def test_a_complete_relocation_resolves(self) -> None:
        config = staging(**RELOCATING)
        self.assertEqual(config.mode, deploy_config.RELOCATION)
        self.assertEqual(config.db_name, "example-db-staging")
        self.assertEqual(config.relocation_source_db, "example-db-source")
        self.assertEqual(config.relocate_copy_cmd, ("scripts/copy.sh", "--exact"))

    def test_each_missing_piece_refuses(self) -> None:
        for missing in ("DEPLOY_RELOCATION_SOURCE_DB_STAGING", "RELOCATE_COPY_CMD"):
            with self.subTest(missing=missing):
                environ = {k: v for k, v in RELOCATING.items() if k != missing}
                with self.assertRaisesRegex(deploy_config.ConfigError, missing):
                    staging(**environ)

    def test_a_source_that_is_the_target_refuses(self) -> None:
        # Fenced as the source and then opened as the target, by a run that
        # copied nothing: a relocation that reports success and moved nothing.
        with self.assertRaisesRegex(deploy_config.ConfigError, "not its target"):
            staging(
                **{
                    **RELOCATING,
                    "DEPLOY_RELOCATION_SOURCE_DB_STAGING": "example-db-staging",
                }
            )

    def test_a_resume_is_the_literal_true_and_only_on_this_body(self) -> None:
        # It lets a run continue from a source somebody else locked, so nothing
        # but the documented value opts in, and an ordinary deploy never can.
        self.assertTrue(
            staging(**RELOCATING, DEPLOY_RELOCATION_RESUME="true").relocation_resume
        )
        for value in ("TRUE", "1", "yes", ""):
            with self.subTest(value=value):
                self.assertFalse(
                    staging(
                        **RELOCATING, DEPLOY_RELOCATION_RESUME=value
                    ).relocation_resume
                )
        self.assertFalse(staging(DEPLOY_RELOCATION_RESUME="true").relocation_resume)

    def test_the_pause_budget_has_a_default_and_refuses_what_it_cannot_honor(
        self,
    ) -> None:
        self.assertEqual(
            staging(**RELOCATING).relocation_max_pause_seconds,
            deploy_config.DEFAULT_RELOCATION_MAX_PAUSE_SECONDS,
        )
        self.assertEqual(
            staging(
                **RELOCATING, DEPLOY_RELOCATION_MAX_PAUSE_SECONDS="90"
            ).relocation_max_pause_seconds,
            90,
        )
        # Zero would stop every fenced step the instant the fence closed: a
        # relocation that pauses writes and can never do anything else.
        for value in ("0", "-5", "soon"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    deploy_config.ConfigError, "DEPLOY_RELOCATION_MAX_PAUSE_SECONDS"
                ),
            ):
                staging(**RELOCATING, DEPLOY_RELOCATION_MAX_PAUSE_SECONDS=value)

    def test_the_other_bodies_take_no_relocation_config(self) -> None:
        # A source named for some later relocation must not change what an
        # ordinary deploy of the same checkout resolves to.
        config = staging(DEPLOY_RELOCATION_SOURCE_DB_STAGING="example-db-source")
        self.assertEqual(config.mode, deploy_config.WINDOWED)
        self.assertEqual(config.relocation_source_db, "")
        self.assertIsNone(config.relocate_copy_cmd)
        self.assertEqual(config.relocation_max_pause_seconds, 0)


if __name__ == "__main__":
    unittest.main()
