# Licensed under LICENSE.md; also available at https://www.prefect.io/licenses/alpha-eula

"""
Environments are JSON-serializable objects that fully describe how to run a flow. Serialization
schemas are contained in `prefect.serialization.environment.py`.

Different Environment objects correspond to different computation environments -- for
example, a `LocalEnvironment` runs a flow in the local process; a `ContainerEnvironment`
runs a flow in a Docker container.

Some of the information that the environment requires to run a flow -- such as the flow
itself -- may not available when the Environment class is instantiated. Therefore, Environments
may be created with a subset of their (ultimate) required information, and the rest can be
provided when the environment's `build()` method is called.

The most basic Environment is a `LocalEnvironment`. This class stores a serialized version
of a Flow and deserializes it to run it. It is expected that most other Environments
will manipulate LocalEnvironments to actually run their flows. For example, the
`ContainerEnvironment` deploys a Docker container with all necessary dependencies installed
and also a serialized `LocalEnvironment`. When the `ContainerEnvironment` runs the
container, the container in turn runs the `LocalEnvironment`.
"""

import base64
import filecmp
import json
import logging
import os
import shutil
import tempfile
import textwrap
import uuid
from typing import Iterable

import cloudpickle
import docker
from cryptography.fernet import Fernet

import prefect


def from_file(path: str) -> "Environment":
    """
    Loads a serialized Environment class from a file

    Args:
        - path (str): the path of a file to deserialize as an Environment

    Returns:
        - Environment: an Environment object
    """
    schema = prefect.serialization.environment.EnvironmentSchema()
    with open(path, "r") as f:
        return schema.load(json.load(f))


class Environment:
    """
    Base class for Environments.

    An environment is an object that can be instantiated in a way that makes it possible to
    call `environment.run()` and run a flow.

    Because certain `__init__` parameters may not be known when the Environment is first
    created, including which Flow to run, Environments have a `build()` method that takes
    a `Flow` argument and returns an Environment with all `__init__` parameters specified.
    """

    def __init__(self) -> None:
        pass

    def build(self, flow: "prefect.Flow") -> "Environment":
        """
        Builds the environment for a specific flow. A new environment is returned.

        Args:
            - flow (prefect.Flow): the Flow for which the environment will be built

        Returns:
            - Environment: a new environment that can run the provided Flow.
        """
        raise NotImplementedError()

    def run(self, runner_kwargs: dict = None) -> "prefect.engine.state.State":
        """
        Runs the `Flow` represented by this environment.

        Args:
            - runner_kwargs (dict): Any arguments for `FlowRunner.run()`
        """
        raise NotImplementedError()

    def serialize(self) -> dict:
        """
        Returns a serialized version of the Environment

        Returns:
            - dict: the serialized Environment
        """
        schema = prefect.serialization.environment.EnvironmentSchema()
        return schema.dump(self)

    def to_file(self, path: str) -> None:
        """
        Serialize the environment to a file.

        Args:
            - path (str): the file path to which the environment will be written
        """
        with open(path, "w") as f:
            json.dump(self.serialize(), f)


class LocalEnvironment(Environment):
    """
    An environment for running a flow locally.

    Flows are serialized as pickles and encrypted. The encryption key is stored in the environment
    and is not meant to be secret, but rather to ensure that only this environment can run
    the serialized flow.

    Args:
        - encryption_key (bytes): an encryption key for this environment. If None, one will be
            generated automatically.
        - serialized_flow (bytes): a serialized flow. This is usually generated by calling `build()`.
    """

    def __init__(self, encryption_key: bytes = None, serialized_flow: bytes = None):
        if encryption_key is None:
            encryption_key = Fernet.generate_key()
        else:
            try:
                Fernet(encryption_key)
            except Exception:
                raise ValueError("Invalid encryption key.")

        self.encryption_key = encryption_key
        self.serialized_flow = serialized_flow

    def serialize_flow_to_bytes(self, flow: "prefect.Flow") -> bytes:
        """
        Serializes a Flow to binary.

        Args:
            - flow (Flow): the Flow to serialize

        Returns:
            - bytes: the serialized Flow
        """
        pickled_flow = cloudpickle.dumps(flow)
        encrypted_pickle = Fernet(self.encryption_key).encrypt(pickled_flow)
        encoded_pickle = base64.b64encode(encrypted_pickle)
        return encoded_pickle

    def deserialize_flow_from_bytes(self, serialized_flow: bytes) -> "prefect.Flow":
        """
        Deserializes a Flow to binary.

        Args:
            - flow (Flow): the Flow to serialize

        Returns:
            - bytes: the serialized Flow
        """
        decoded_pickle = base64.b64decode(serialized_flow)
        decrypted_pickle = Fernet(self.encryption_key).decrypt(decoded_pickle)
        flow = cloudpickle.loads(decrypted_pickle)
        return flow

    def build(self, flow: "prefect.Flow") -> "LocalEnvironment":
        """
        Build the LocalEnvironment. Returns a LocalEnvironment with a serialized flow attribute.

        Args:
            - flow (Flow): The prefect Flow object to build the environment for

        Returns:
            - LocalEnvironment: a LocalEnvironment with a serialized flow attribute
        """
        return LocalEnvironment(
            encryption_key=self.encryption_key,
            serialized_flow=self.serialize_flow_to_bytes(flow),
        )

    def run(self, runner_kwargs: dict = None) -> "prefect.engine.state.State":
        """
        Runs the `Flow` represented by this environment.

        Args:
            - runner_kwargs (dict): Any arguments for `FlowRunner.run()`
        """

        if not self.serialized_flow:
            raise ValueError(
                "No serialized flow found! Has this environment been built?"
            )
        flow = self.deserialize_flow_from_bytes(self.serialized_flow)
        runner_cls = prefect.engine.get_default_flow_runner_class()
        runner = runner_cls(flow=flow)
        return runner.run(**(runner_kwargs or {}))


class ContainerEnvironment(Environment):
    """
    Container class used to represent a Docker container.

    Args:
        - base_image (str): The image to pull that is used as a base for the Docker container
        *Note*: Images must include Python 3.4+ and `pip`.
        - registry_url (str, optional): The registry to push the image to
        - python_dependencies (list, optional): The list of pip installable python packages
        that will be installed on build of the Docker container
        - image_name (str, optional): A name for the image (usually provided by `build()`)
        - image_tag (str, optional): A tag for the image (usually provided by `build()`)
        - env_vars (dict, optional): an optional dictionary mapping environment variables to their values (e.g., `{SHELL="bash"}`) to be
            included in the Dockerfile
        - files (dict, optional): an optional dictionary mapping local file names to file names in the Docker container; file names should be
            _absolute paths_.  Note that the COPY directive will be used for these files, so please read the associated Docker documentation.

    Raises:
        - ValueError: if provided `files` contain non-absolute paths
    """

    def __init__(
        self,
        base_image: str,
        registry_url: str,
        python_dependencies: list = None,
        image_name: str = None,
        image_tag: str = None,
        env_vars: dict = None,
        files: dict = None,
    ):
        self.base_image = base_image
        self.registry_url = registry_url
        self.image_name = image_name
        self.image_tag = image_tag
        self.python_dependencies = python_dependencies or []
        self.env_vars = env_vars or {}
        self.files = files or {}
        not_absolute = [
            file_path for file_path in self.files if not os.path.isabs(file_path)
        ]
        if not_absolute:
            raise ValueError(
                "Provided paths {} are not absolute file paths, please provide absolute paths only.".format(
                    ", ".join(not_absolute)
                )
            )

    def _parse_generator_output(self, generator: Iterable):
        """
        Parses and writes a Docker command's output to stdout
        """
        for item in generator:
            item = item.decode("utf-8")
            for line in item.split("\n"):
                if line:
                    output = json.loads(line).get("stream")
                    if output and output != "\n":
                        print(output.strip("\n"))

    def build(
        self, flow: "prefect.Flow", push: bool = True
    ) -> "prefect.environments.ContainerEnvironment":
        """
        Build the Docker container. Returns a Container Environment with the appropriate
        image_name and image_tag set.

        Args:
            - flow (prefect.Flow): Flow to be placed in container
            - push (bool): Whether or not to push to registry after build

        Returns:
            - ContainerEnvironment: a ContainerEnvironment that represents the provided flow.
        """

        image_name = str(uuid.uuid4())
        image_tag = str(uuid.uuid4())

        with tempfile.TemporaryDirectory() as tempdir:

            self.pull_image()

            self.create_dockerfile(flow=flow, directory=tempdir)

            client = docker.APIClient(base_url="unix://var/run/docker.sock")

            full_name = os.path.join(self.registry_url, image_name)

            logging.info("Building the flow's container environment...")
            output = client.build(
                path=tempdir, tag="{}:{}".format(full_name, image_tag), forcerm=True
            )
            self._parse_generator_output(output)

            if push:
                self.push(full_name, image_tag)

            # Remove the image locally after being pushed
            client.remove_image(image="{}:{}".format(full_name, image_tag), force=True)

            return ContainerEnvironment(
                base_image=self.base_image,
                registry_url=self.registry_url,
                image_name=image_name,
                image_tag=image_tag,
                python_dependencies=self.python_dependencies,
            )

    def run(self, runner_kwargs: dict = None) -> None:
        """
        Runs the `Flow` represented by this environment.

        Args:
            - runner_kwargs (dict): Any arguments for `FlowRunner.run()`
        """

        client = docker.APIClient(base_url="unix://var/run/docker.sock")

        running_container = client.create_container(
            image="{}:{}".format(
                os.path.join(self.registry_url, self.image_name), self.image_tag
            ),
            command='bash -c "prefect run $PREFECT_ENVIRONMENT_FILE"',
            detach=True,
        )

        return running_container

    def push(self, image_name: str, image_tag: str) -> None:
        """Push this environment to a registry

        Args:
            - image_name (str): Name for the image
            - image_tag (str): Tag for the image

        Returns:
            - None
        """
        client = docker.APIClient(base_url="unix://var/run/docker.sock")

        logging.info("Pushing image to the registry...")

        # This could be adjusted to use something like tqdm for progress bar output
        output = client.push(image_name, tag=image_tag, stream=True, decode=True)
        for line in output:
            if line.get("progress"):
                print(line.get("status"), line.get("progress"))

    def pull_image(self) -> None:
        """Pull the image specified so it can be built.

        In order for the docker python library to use a base image it must be pulled
        from either the main docker registry or a separate registry that must be set in
        the environment variables.
        """
        client = docker.APIClient(base_url="unix://var/run/docker.sock")

        output = client.pull(self.base_image, stream=True, decode=True)
        for line in output:
            if line.get("progress"):
                print(line.get("status"), line.get("progress"))

    def create_dockerfile(self, flow: "prefect.Flow", directory: str = None) -> None:
        """Creates a dockerfile to use as the container.

        In order for the docker python library to build a container it needs a
        Dockerfile that it can use to define the container. This function takes the
        image and python_dependencies then writes them to a file called Dockerfile.

        *Note*: if `files` are added to this container, they will be copied to this directory as well.

        Args:
            - flow (Flow): the flow that the container will run
            - directory (str, optional): A directory where the Dockerfile will be created,
                if no directory is specified is will be created in the current working directory

        Returns:
            - None
        """

        with open(os.path.join(directory, "Dockerfile"), "w+") as dockerfile:

            # Generate RUN pip install commands for python dependencies
            pip_installs = ""
            if self.python_dependencies:
                for dependency in self.python_dependencies:
                    pip_installs += "RUN pip install {}\n".format(dependency)

            env_vars = ""
            if self.env_vars:
                white_space = " " * 20
                env_vars = "ENV " + " \ \n{}".format(white_space).join(
                    "{k}={v}".format(k=k, v=v) for k, v in self.env_vars.items()
                )

            copy_files = ""
            if self.files:
                for src, dest in self.files.items():
                    fname = os.path.basename(src)
                    full_fname = os.path.join(directory, fname)
                    if (
                        os.path.exists(full_fname)
                        and filecmp.cmp(src, full_fname) is False
                    ):
                        raise ValueError(
                            "File {fname} already exists in {directory}".format(
                                fname=full_fname, directory=directory
                            )
                        )
                    else:
                        shutil.copy2(src, full_fname)
                    copy_files += "COPY {fname} {dest}\n".format(fname=fname, dest=dest)

            # Create a LocalEnvironment to run the flow
            # the local environment will be placed in the container and run when the container
            # runs
            local_environment = LocalEnvironment().build(flow=flow)
            flow_path = os.path.join(directory, "flow_env.prefect")
            local_environment.to_file(flow_path)

            # Due to prefect being a private repo it currently will require a
            # personal access token. Once pip installable this will change and there won't
            # be a need for the personal access token or git anymore.
            # *Note*: this currently prevents alpine images from being used

            file_contents = textwrap.dedent(
                """\
                FROM {base_image}

                RUN apt-get -qq -y update && apt-get -qq -y install --no-install-recommends --no-install-suggests git

                RUN pip install pip --upgrade
                RUN pip install wheel
                {pip_installs}

                RUN mkdir /root/.prefect/
                COPY flow_env.prefect /root/.prefect/flow_env.prefect
                {copy_files}

                ENV PREFECT_ENVIRONMENT_FILE="/root/.prefect/flow_env.prefect"
                ENV PREFECT__USER_CONFIG_PATH="/root/.prefect/config.toml"
                {env_vars}

                RUN git clone https://{access_token}@github.com/PrefectHQ/prefect.git
                RUN pip install ./prefect
                """.format(
                    base_image=self.base_image,
                    pip_installs=pip_installs,
                    copy_files=copy_files,
                    env_vars=env_vars,
                    access_token=os.getenv("PERSONAL_ACCESS_TOKEN"),
                )
            )

            dockerfile.write(file_contents)
