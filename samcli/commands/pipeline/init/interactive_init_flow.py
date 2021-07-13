"""
Interactive flow that prompts that users for pipeline template (cookiecutter template) and used it to generate
pipeline configuration file
"""
import logging
import os
from pathlib import Path
from textwrap import dedent
from typing import Dict, List, Tuple

import click

from samcli.cli.main import global_cfg
from samcli.commands.exceptions import PipelineTemplateCloneException, PipelineFileAlreadyExistsError
from samcli.lib.config.samconfig import SamConfig
from samcli.lib.cookiecutter.interactive_flow import InteractiveFlow
from samcli.lib.cookiecutter.interactive_flow_creator import InteractiveFlowCreator
from samcli.lib.cookiecutter.question import Choice
from samcli.lib.cookiecutter.template import Template
from samcli.lib.utils import osutils
from samcli.lib.utils.colors import Colored
from samcli.lib.utils.git_repo import GitRepo, CloneRepoException
from .pipeline_templates_manifest import Provider, PipelineTemplateMetadata, PipelineTemplatesManifest
from ..bootstrap.cli import (
    do_cli as do_bootstrap,
    PIPELINE_CONFIG_DIR,
    PIPELINE_CONFIG_FILENAME,
    _get_bootstrap_command_names,
)

LOG = logging.getLogger(__name__)
shared_path: Path = global_cfg.config_dir
APP_PIPELINE_TEMPLATES_REPO_URL = "https://github.com/aws/aws-sam-cli-pipeline-init-templates.git"
APP_PIPELINE_TEMPLATES_REPO_LOCAL_NAME = "aws-sam-cli-app-pipeline-templates"
CUSTOM_PIPELINE_TEMPLATE_REPO_LOCAL_NAME = "custom-pipeline-template"
SAM_PIPELINE_TEMPLATE_SOURCE = "AWS Quick Start Pipeline Templates"
CUSTOM_PIPELINE_TEMPLATE_SOURCE = "Custom Pipeline Template Location"


class InteractiveInitFlow:
    def __init__(self, allow_bootstrap: bool):
        self.allow_bootstrap = allow_bootstrap
        self.color = Colored()

    def do_interactive(self) -> None:
        """
        An interactive flow that prompts the user for pipeline template (cookiecutter template) location, downloads it,
        runs its specific questionnaire then generates the pipeline config file
        based on the template and user's responses
        """
        click.echo(
            dedent(
                """\

                sam pipeline init generates a pipeline config file that you can use to connect your
                AWS account(s) to your CI/CD system. We will guide you through the process to
                bootstrap resources for each stage, then walk through the details necessary for
                creating the pipeline config file.

                Please ensure you are in the root folder of your SAM application before you begin.
                """
            )
        )

        click.echo("Select a pipeline structure template to get started:")
        pipeline_template_source_question = Choice(
            key="pipeline-template-source",
            text="Select template",
            options=[SAM_PIPELINE_TEMPLATE_SOURCE, CUSTOM_PIPELINE_TEMPLATE_SOURCE],
            is_required=True,
        )
        source = pipeline_template_source_question.ask()
        if source == CUSTOM_PIPELINE_TEMPLATE_SOURCE:
            generated_files = self._generate_from_custom_location()
        else:
            generated_files = self._generate_from_app_pipeline_templates()
        click.secho(Colored().green("Successfully created the pipeline configuration file(s):"))
        for file in generated_files:
            click.secho(Colored().green(f"\t- {file}"))

    def _generate_from_app_pipeline_templates(
        self,
    ) -> List[str]:
        """
        Prompts the user to choose a pipeline template from SAM predefined set of pipeline templates hosted in the git
        repository: aws/aws-sam-cli-pipeline-init-templates.git
        downloads locally, then generates the pipeline config file from the selected pipeline template.
        Finally, return the list of generated files.
        """
        pipeline_templates_local_dir: Path = _clone_app_pipeline_templates()
        pipeline_templates_manifest: PipelineTemplatesManifest = _read_app_pipeline_templates_manifest(
            pipeline_templates_local_dir
        )
        # The manifest contains multiple pipeline-templates so select one
        selected_pipeline_template_metadata: PipelineTemplateMetadata = _prompt_pipeline_template(
            pipeline_templates_manifest
        )
        selected_pipeline_template_dir: Path = pipeline_templates_local_dir.joinpath(
            selected_pipeline_template_metadata.location
        )
        return self._generate_from_pipeline_template(selected_pipeline_template_dir)

    def _generate_from_custom_location(
        self,
    ) -> List[str]:
        """
        Prompts the user for a custom pipeline template location, downloads locally,
        then generates the pipeline config file and return the list of generated files
        """
        pipeline_template_git_location: str = click.prompt("Template Git location")
        if os.path.exists(pipeline_template_git_location):
            return self._generate_from_pipeline_template(Path(pipeline_template_git_location))

        with osutils.mkdir_temp(ignore_errors=True) as tempdir:
            tempdir_path = Path(tempdir)
            pipeline_template_local_dir: Path = _clone_pipeline_templates(
                pipeline_template_git_location, tempdir_path, CUSTOM_PIPELINE_TEMPLATE_REPO_LOCAL_NAME
            )
            return self._generate_from_pipeline_template(pipeline_template_local_dir)

    def _prompt_run_bootstrap_within_pipeline_init(self, env_names: List[str], required_env_number: int) -> bool:
        """
        Prompt bootstrap if `--bootstrap` flag is provided. Return True if bootstrap process is executed.
        """
        if not env_names:
            click.echo(Colored().yellow("No bootstrapped resources were detected."))
        else:
            click.echo(
                Colored().yellow(
                    f"Only {len(env_names)} bootstrapped stage(s) were detected, "
                    f"fewer than what the template requires: {required_env_number}."
                )
            )
        click.echo()

        if self.allow_bootstrap:
            if click.confirm(
                "Do you want to go through stage setup process now? If you choose no, "
                "you can still reference other bootstrapped resources."
            ):
                click.secho(
                    dedent(
                        """\

                        For each stage, we will ask for [1] stage definition, [2] account details, and [3]
                        reference application build resources in order to bootstrap these pipeline
                        resources. You can also add optional security parameters.

                        We recommend using an individual AWS account profiles for each stage in your
                        pipeline. You can set these profiles up using [little bit of info on how to do
                        this/docs].
                        """
                    )
                )

                click.echo(Colored().bold(f"\nStage {len(env_names) + 1} Setup\n"))
                do_bootstrap(
                    region=None,
                    profile=None,
                    interactive=True,
                    environment_name=None,
                    pipeline_user_arn=None,
                    pipeline_execution_role_arn=None,
                    cloudformation_execution_role_arn=None,
                    artifacts_bucket_arn=None,
                    create_image_repository=False,
                    image_repository_arn=None,
                    confirm_changeset=True,
                    config_file=None,
                    config_env=None,
                    standalone=False,
                )
                return True
        else:
            click.echo(
                Colored().yellow(
                    dedent(
                        f"""\
                        If you want to setup stages before proceed, please quit the process using Ctrl+C.
                        Then you can either run {Colored().bold('sam pipeline bootstrap')} to setup a stage
                        or re-run this command with option {Colored().bold('--bootstrap')} to enable stage setup.
                        """
                    )
                )
            )
        return False

    def _generate_from_pipeline_template(self, pipeline_template_dir: Path) -> List[str]:
        """
        Generates a pipeline config file from a given pipeline template local location
        and return the list of generated files.
        """
        pipeline_template: Template = _initialize_pipeline_template(pipeline_template_dir)
        required_env_number = 2  # TODO: read from template
        click.echo(f"You are using the {required_env_number}-stage pipeline template.")
        _draw_stage_diagram(required_env_number)
        while True:
            click.echo("Checking for bootstrapped resources...")
            env_names, bootstrap_context = _load_pipeline_bootstrap_resources()
            if len(env_names) < required_env_number and self._prompt_run_bootstrap_within_pipeline_init(
                env_names, required_env_number
            ):
                # the customers just went through the bootstrap process,
                # refresh the pipeline bootstrap resources and see whether bootstrap is still needed
                continue
            break

        context: Dict = pipeline_template.run_interactive_flows(bootstrap_context)
        with osutils.mkdir_temp() as generate_dir:
            LOG.debug("Generating pipeline files into %s", generate_dir)
            context["outputDir"] = "."  # prevent cookiecutter from generating a sub-folder
            pipeline_template.generate_project(context, generate_dir)
            return _copy_dir_contents_to_cwd_fail_on_exist(generate_dir)


def _load_pipeline_bootstrap_resources() -> Tuple[List[str], Dict[str, str]]:
    section = "parameters"
    context: Dict = {}

    config = SamConfig(PIPELINE_CONFIG_DIR, PIPELINE_CONFIG_FILENAME)
    if not config.exists():
        context[str(["environment_names_message"])] = ""
        return [], context

    # config.get_env_names() will return the list of
    # bootstrapped env names and "default" which is used to store shared values
    # we don't want to include "default" here.
    env_names = [env_name for env_name in config.get_env_names() if env_name != "default"]
    for env in env_names:
        for key, value in config.get_all(_get_bootstrap_command_names(), section, env).items():
            context[str([env, key])] = value

    # pre-load the list of env names detected from pipelineconfig.toml
    environment_names_message = (
        "Here are the environment names detected "
        + f"in {os.path.join(PIPELINE_CONFIG_DIR, PIPELINE_CONFIG_FILENAME)}:\n"
        + "\n".join([f"\t- {env_name}" for env_name in env_names])
    )
    context[str(["environment_names_message"])] = environment_names_message

    return env_names, context


def _copy_dir_contents_to_cwd_fail_on_exist(source_dir: str) -> List[str]:
    copied_file_paths: List[str] = []
    for root, _, files in os.walk(source_dir):
        for filename in files:
            file_path = Path(root, filename)
            target_file_path = Path(".").joinpath(file_path.relative_to(source_dir))
            LOG.debug("Verify %s does not exist", target_file_path)
            if target_file_path.exists():
                raise PipelineFileAlreadyExistsError(target_file_path)
            copied_file_paths.append(str(target_file_path))
    LOG.debug("Copy contents of %s to cwd", source_dir)
    osutils.copytree(source_dir, ".")
    return copied_file_paths


def _clone_app_pipeline_templates() -> Path:
    """
    clone aws/aws-sam-cli-pipeline-init-templates.git Git repo to the local machine in SAM shared directory.
    Returns:
        the local directory path where the repo is cloned.
    """
    try:
        return _clone_pipeline_templates(
            repo_url=APP_PIPELINE_TEMPLATES_REPO_URL,
            clone_dir=shared_path,
            clone_name=APP_PIPELINE_TEMPLATES_REPO_LOCAL_NAME,
        )
    except PipelineTemplateCloneException:
        # If can't clone app pipeline templates, try using an old clone from a previous run if already exist
        expected_previous_clone_local_path: Path = shared_path.joinpath(APP_PIPELINE_TEMPLATES_REPO_LOCAL_NAME)
        if expected_previous_clone_local_path.exists():
            click.echo("Unable to download updated app pipeline templates, using existing ones")
            return expected_previous_clone_local_path
        raise


def _clone_pipeline_templates(repo_url: str, clone_dir: Path, clone_name: str) -> Path:
    """
    clone a given pipeline templates' Git repo to the user machine inside the given clone_dir directory
    under the given clone name. For example, if clone_name is "custom-pipeline-template" then the location to clone
    to is "/clone/dir/path/custom-pipeline-template/"

    Parameters:
        repo_url: the URL of the Git repo to clone
        clone_dir: the local parent directory to clone to
        clone_name: The folder name to give to the created clone inside clone_dir

    Returns:
        Path to the local clone
    """
    try:
        repo: GitRepo = GitRepo(repo_url)
        clone_path: Path = repo.clone(clone_dir, clone_name, replace_existing=True)
        return clone_path
    except (OSError, CloneRepoException) as ex:
        raise PipelineTemplateCloneException(str(ex)) from ex


def _read_app_pipeline_templates_manifest(pipeline_templates_dir: Path) -> PipelineTemplatesManifest:
    """
    parse and return the manifest yaml file located in the root directory of the SAM pipeline templates folder:

    Parameters:
        pipeline_templates_dir: local directory of SAM pipeline templates

    Raises:
        AppPipelineTemplateManifestException if the manifest is not found, ill-formatted or missing required keys

    Returns:
        The manifest of the pipeline templates
    """
    manifest_path: Path = pipeline_templates_dir.joinpath("manifest.yaml")
    return PipelineTemplatesManifest(manifest_path)


def _prompt_pipeline_template(pipeline_templates_manifest: PipelineTemplatesManifest) -> PipelineTemplateMetadata:
    """
    Prompts the user a list of the available CI/CD systems along with associated app pipeline templates to choose
    one of them

    Parameters:
        pipeline_templates_manifest: A manifest file lists the available providers and the associated pipeline templates

    Returns:
         The manifest (A section in the pipeline_templates_manifest) of the chosen pipeline template;
    """
    provider = _prompt_cicd_provider(pipeline_templates_manifest.providers)
    provider_pipeline_templates: List[PipelineTemplateMetadata] = [
        t for t in pipeline_templates_manifest.templates if t.provider == provider.id
    ]
    selected_template_manifest: PipelineTemplateMetadata = _prompt_provider_pipeline_template(
        provider_pipeline_templates
    )
    return selected_template_manifest


def _prompt_cicd_provider(available_providers: List[Provider]) -> Provider:
    """
    Prompts the user a list of the available CI/CD systems to choose from

    Parameters:
        available_providers: List of available CI/CD systems such as Jenkins, Gitlab and CircleCI

    Returns:
        The chosen provider
    """
    if len(available_providers) == 1:
        return available_providers[0]

    question_to_choose_provider = Choice(
        key="provider", text="CI/CD system", options=[p.display_name for p in available_providers], is_required=True
    )
    chosen_provider_display_name = question_to_choose_provider.ask()
    return next(p for p in available_providers if p.display_name == chosen_provider_display_name)


def _prompt_provider_pipeline_template(
    provider_available_pipeline_templates_metadata: List[PipelineTemplateMetadata],
) -> PipelineTemplateMetadata:
    """
    Prompts the user a list of the available pipeline templates to choose from

    Parameters:
        provider_available_pipeline_templates_metadata: List of available pipeline templates manifests

    Returns:
        The chosen pipeline template manifest
    """
    if len(provider_available_pipeline_templates_metadata) == 1:
        return provider_available_pipeline_templates_metadata[0]
    question_to_choose_pipeline_template = Choice(
        key="pipeline-template",
        text="Which pipeline template would you like to use?",
        options=[t.display_name for t in provider_available_pipeline_templates_metadata],
    )
    chosen_pipeline_template_display_name = question_to_choose_pipeline_template.ask()
    return next(
        t
        for t in provider_available_pipeline_templates_metadata
        if t.display_name == chosen_pipeline_template_display_name
    )


def _initialize_pipeline_template(pipeline_template_dir: Path) -> Template:
    """
    Initialize a pipeline template from a given pipeline template (cookiecutter template) location

    Parameters:
        pipeline_template_dir: The local location of the pipeline cookiecutter template

    Returns:
        The initialized pipeline's cookiecutter template
    """
    interactive_flow = _get_pipeline_template_interactive_flow(pipeline_template_dir)
    return Template(location=str(pipeline_template_dir), interactive_flows=[interactive_flow])


def _get_pipeline_template_interactive_flow(pipeline_template_dir: Path) -> InteractiveFlow:
    """
    A pipeline template defines its own interactive flow (questionnaire) in a JSON file named questions.json located
    in the root directory of the template. This questionnaire defines a set of questions to prompt to the user and
    use the responses as the cookiecutter context

    Parameters:
        pipeline_template_dir: The local location of the pipeline cookiecutter template

    Raises:
        QuestionsNotFoundException: if the pipeline template is missing questions.json file.
        QuestionsFailedParsingException: if questions.json file is ill-formatted or missing required keys.

    Returns:
         The interactive flow
    """
    flow_definition_path: Path = pipeline_template_dir.joinpath("questions.json")
    return InteractiveFlowCreator.create_flow(str(flow_definition_path))


def _lines_for_stage(stage_index: int) -> List[str]:
    return [
        " _________ ",
        "|         |",
        f"| Stage {stage_index} |",
        "|_________|",
    ]


def _draw_stage_diagram(number_of_stages: int) -> None:
    delimiters = ["  ", "  ", "->", "  "]
    stage_lines = [_lines_for_stage(i + 1) for i in range(number_of_stages)]
    for i, delimiter in enumerate(delimiters):
        click.echo(delimiter.join([stage_lines[stage_i][i] for stage_i in range(number_of_stages)]))