# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2017-2018 New Vector Ltd
# Copyright 2019 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import errno
import os
from collections import OrderedDict
from textwrap import dedent
from typing import Any, MutableMapping, Optional

from six import integer_types

import yaml


class ConfigError(Exception):
    pass


# We split these messages out to allow packages to override with package
# specific instructions.
MISSING_REPORT_STATS_CONFIG_INSTRUCTIONS = """\
Please opt in or out of reporting anonymized homeserver usage statistics, by
setting the `report_stats` key in your config file to either True or False.
"""

MISSING_REPORT_STATS_SPIEL = """\
We would really appreciate it if you could help our project out by reporting
anonymized usage statistics from your homeserver. Only very basic aggregate
data (e.g. number of users) will be reported, but it helps us to track the
growth of the Matrix community, and helps us to make Matrix a success, as well
as to convince other networks that they should peer with us.

Thank you.
"""

MISSING_SERVER_NAME = """\
Missing mandatory `server_name` config option.
"""


def path_exists(file_path):
    """Check if a file exists

    Unlike os.path.exists, this throws an exception if there is an error
    checking if the file exists (for example, if there is a perms error on
    the parent dir).

    Returns:
        bool: True if the file exists; False if not.
    """
    try:
        os.stat(file_path)
        return True
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise e
        return False


class Config(object):
    """
    A configuration section, containing configuration keys and values.

    Attributes:
        section (str): The section title of this config object, such as
            "tls" or "logger". This is used to refer to it on the root
            logger (for example, `config.tls.some_option`). Must be
            defined in subclasses.
    """

    section = None

    def __init__(self, root_config=None):
        self.root = root_config

    def __getattr__(self, item: str) -> Any:
        """
        Try and fetch a configuration option that does not exist on this class.

        This is so that existing configs that rely on `self.value`, where value
        is actually from a different config section, continue to work.
        """
        if item in ["generate_config_section", "read_config"]:
            raise AttributeError(item)

        if self.root is None:
            raise AttributeError(item)
        else:
            return self.root._get_unclassed_config(self.section, item)

    @staticmethod
    def parse_size(value):
        if isinstance(value, integer_types):
            return value
        sizes = {"K": 1024, "M": 1024 * 1024}
        size = 1
        suffix = value[-1]
        if suffix in sizes:
            value = value[:-1]
            size = sizes[suffix]
        return int(value) * size

    @staticmethod
    def parse_duration(value):
        if isinstance(value, integer_types):
            return value
        second = 1000
        minute = 60 * second
        hour = 60 * minute
        day = 24 * hour
        week = 7 * day
        year = 365 * day
        sizes = {"s": second, "m": minute, "h": hour, "d": day, "w": week, "y": year}
        size = 1
        suffix = value[-1]
        if suffix in sizes:
            value = value[:-1]
            size = sizes[suffix]
        return int(value) * size

    @staticmethod
    def abspath(file_path):
        return os.path.abspath(file_path) if file_path else file_path

    @classmethod
    def path_exists(cls, file_path):
        return path_exists(file_path)

    @classmethod
    def check_file(cls, file_path, config_name):
        if file_path is None:
            raise ConfigError("Missing config for %s." % (config_name,))
        try:
            os.stat(file_path)
        except OSError as e:
            raise ConfigError(
                "Error accessing file '%s' (config for %s): %s"
                % (file_path, config_name, e.strerror)
            )
        return cls.abspath(file_path)

    @classmethod
    def ensure_directory(cls, dir_path):
        dir_path = cls.abspath(dir_path)
        try:
            os.makedirs(dir_path)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
        if not os.path.isdir(dir_path):
            raise ConfigError("%s is not a directory" % (dir_path,))
        return dir_path

    @classmethod
    def read_file(cls, file_path, config_name):
        cls.check_file(file_path, config_name)
        with open(file_path) as file_stream:
            return file_stream.read()


class RootConfig(object):
    """
    Holder of an application's configuration.

    What configuration this object holds is defined by `config_classes`, a list
    of Config classes that will be instantiated and given the contents of a
    configuration file to read. They can then be accessed on this class by their
    section name, defined in the Config or dynamically set to be the name of the
    class, lower-cased and with "Config" removed.
    """

    config_classes = []

    def __init__(self):
        self._configs = OrderedDict()

        for config_class in self.config_classes:
            if config_class.section is None:
                raise ValueError("%r requires a section name" % (config_class,))

            try:
                conf = config_class(self)
            except Exception as e:
                raise Exception("Failed making %s: %r" % (config_class.section, e))
            self._configs[config_class.section] = conf

    def __getattr__(self, item: str) -> Any:
        """
        Redirect lookups on this object either to config objects, or values on
        config objects, so that `config.tls.blah` works, as well as legacy uses
        of things like `config.server_name`. It will first look up the config
        section name, and then values on those config classes.
        """
        if item in self._configs.keys():
            return self._configs[item]

        return self._get_unclassed_config(None, item)

    def _get_unclassed_config(self, asking_section: Optional[str], item: str):
        """
        Fetch a config value from one of the instantiated config classes that
        has not been fetched directly.

        Args:
            asking_section: If this check is coming from a Config child, which
                one? This section will not be asked if it has the value.
            item: The configuration value key.

        Raises:
            AttributeError if no config classes have the config key. The body
                will contain what sections were checked.
        """
        for key, val in self._configs.items():
            if key == asking_section:
                continue

            if item in dir(val):
                return getattr(val, item)

        raise AttributeError(item, "not found in %s" % (list(self._configs.keys()),))

    def invoke_all(self, func_name: str, *args, **kwargs) -> MutableMapping[str, Any]:
        """
        Invoke a function on all instantiated config objects this RootConfig is
        configured to use.

        Args:
            func_name: Name of function to invoke
            *args
            **kwargs
        Returns:
            ordered dictionary of config section name and the result of the
            function from it.
        """
        res = OrderedDict()

        for name, config in self._configs.items():
            if hasattr(config, func_name):
                res[name] = getattr(config, func_name)(*args, **kwargs)

        return res

    @classmethod
    def invoke_all_static(cls, func_name: str, *args, **kwargs):
        """
        Invoke a static function on config objects this RootConfig is
        configured to use.

        Args:
            func_name: Name of function to invoke
            *args
            **kwargs
        Returns:
            ordered dictionary of config section name and the result of the
            function from it.
        """
        for config in cls.config_classes:
            if hasattr(config, func_name):
                getattr(config, func_name)(*args, **kwargs)

    def generate_config(
        self,
        config_dir_path,
        data_dir_path,
        server_name,
        generate_secrets=False,
        report_stats=None,
        open_private_ports=False,
        listeners=None,
        database_conf=None,
        tls_certificate_path=None,
        tls_private_key_path=None,
        acme_domain=None,
    ):
        """
        Build a default configuration file

        This is used when the user explicitly asks us to generate a config file
        (eg with --generate_config).

        Args:
            config_dir_path (str): The path where the config files are kept. Used to
                create filenames for things like the log config and the signing key.

            data_dir_path (str): The path where the data files are kept. Used to create
                filenames for things like the database and media store.

            server_name (str): The server name. Used to initialise the server_name
                config param, but also used in the names of some of the config files.

            generate_secrets (bool): True if we should generate new secrets for things
                like the macaroon_secret_key. If False, these parameters will be left
                unset.

            report_stats (bool|None): Initial setting for the report_stats setting.
                If None, report_stats will be left unset.

            open_private_ports (bool): True to leave private ports (such as the non-TLS
                HTTP listener) open to the internet.

            listeners (list(dict)|None): A list of descriptions of the listeners
                synapse should start with each of which specifies a port (str), a list of
                resources (list(str)), tls (bool) and type (str). For example:
                [{
                    "port": 8448,
                    "resources": [{"names": ["federation"]}],
                    "tls": True,
                    "type": "http",
                },
                {
                    "port": 443,
                    "resources": [{"names": ["client"]}],
                    "tls": False,
                    "type": "http",
                }],


            database (str|None): The database type to configure, either `psycog2`
                or `sqlite3`.

            tls_certificate_path (str|None): The path to the tls certificate.

            tls_private_key_path (str|None): The path to the tls private key.

            acme_domain (str|None): The domain acme will try to validate. If
                specified acme will be enabled.

        Returns:
            str: the yaml config file
        """

        return "\n\n".join(
            dedent(conf)
            for conf in self.invoke_all(
                "generate_config_section",
                config_dir_path=config_dir_path,
                data_dir_path=data_dir_path,
                server_name=server_name,
                generate_secrets=generate_secrets,
                report_stats=report_stats,
                open_private_ports=open_private_ports,
                listeners=listeners,
                database_conf=database_conf,
                tls_certificate_path=tls_certificate_path,
                tls_private_key_path=tls_private_key_path,
                acme_domain=acme_domain,
            ).values()
        )

    @classmethod
    def load_config(cls, description, argv):
        """Parse the commandline and config files

        Doesn't support config-file-generation: used by the worker apps.

        Returns: Config object.
        """
        config_parser = argparse.ArgumentParser(description=description)
        cls.add_arguments_to_parser(config_parser)
        obj, _ = cls.load_config_with_parser(config_parser, argv)

        return obj

    @classmethod
    def add_arguments_to_parser(cls, config_parser):
        """Adds all the config flags to an ArgumentParser.

        Doesn't support config-file-generation: used by the worker apps.

        Used for workers where we want to add extra flags/subcommands.

        Args:
            config_parser (ArgumentParser): App description
        """

        config_parser.add_argument(
            "-c",
            "--config-path",
            action="append",
            metavar="CONFIG_FILE",
            help="Specify config file. Can be given multiple times and"
            " may specify directories containing *.yaml files.",
        )

        config_parser.add_argument(
            "--keys-directory",
            metavar="DIRECTORY",
            help="Where files such as certs and signing keys are stored when"
            " their location is not given explicitly in the config."
            " Defaults to the directory containing the last config file",
        )

        cls.invoke_all_static("add_arguments", config_parser)

    @classmethod
    def load_config_with_parser(cls, parser, argv):
        """Parse the commandline and config files with the given parser

        Doesn't support config-file-generation: used by the worker apps.

        Used for workers where we want to add extra flags/subcommands.

        Args:
            parser (ArgumentParser)
            argv (list[str])

        Returns:
            tuple[HomeServerConfig, argparse.Namespace]: Returns the parsed
            config object and the parsed argparse.Namespace object from
            `parser.parse_args(..)`
        """

        obj = cls()

        config_args = parser.parse_args(argv)

        config_files = find_config_files(search_paths=config_args.config_path)

        if not config_files:
            parser.error("Must supply a config file.")

        if config_args.keys_directory:
            config_dir_path = config_args.keys_directory
        else:
            config_dir_path = os.path.dirname(config_files[-1])
        config_dir_path = os.path.abspath(config_dir_path)
        data_dir_path = os.getcwd()

        config_dict = read_config_files(config_files)
        obj.parse_config_dict(
            config_dict, config_dir_path=config_dir_path, data_dir_path=data_dir_path
        )

        obj.invoke_all("read_arguments", config_args)

        return obj, config_args

    @classmethod
    def load_or_generate_config(cls, description, argv):
        """Parse the commandline and config files

        Supports generation of config files, so is used for the main homeserver app.

        Returns: Config object, or None if --generate-config or --generate-keys was set
        """
        config_parser = argparse.ArgumentParser(add_help=False)
        config_parser.add_argument(
            "-c",
            "--config-path",
            action="append",
            metavar="CONFIG_FILE",
            help="Specify config file. Can be given multiple times and"
            " may specify directories containing *.yaml files.",
        )

        generate_group = config_parser.add_argument_group("Config generation")
        generate_group.add_argument(
            "--generate-config",
            action="store_true",
            help="Generate a config file, then exit.",
        )
        generate_group.add_argument(
            "--generate-missing-configs",
            "--generate-keys",
            action="store_true",
            help="Generate any missing additional config files, then exit.",
        )
        generate_group.add_argument(
            "-H", "--server-name", help="The server name to generate a config file for."
        )
        generate_group.add_argument(
            "--report-stats",
            action="store",
            help="Whether the generated config reports anonymized usage statistics.",
            choices=["yes", "no"],
        )
        generate_group.add_argument(
            "--config-directory",
            "--keys-directory",
            metavar="DIRECTORY",
            help=(
                "Specify where additional config files such as signing keys and log"
                " config should be stored. Defaults to the same directory as the last"
                " config file."
            ),
        )
        generate_group.add_argument(
            "--data-directory",
            metavar="DIRECTORY",
            help=(
                "Specify where data such as the media store and database file should be"
                " stored. Defaults to the current working directory."
            ),
        )
        generate_group.add_argument(
            "--open-private-ports",
            action="store_true",
            help=(
                "Leave private ports (such as the non-TLS HTTP listener) open to the"
                " internet. Do not use this unless you know what you are doing."
            ),
        )

        config_args, remaining_args = config_parser.parse_known_args(argv)

        config_files = find_config_files(search_paths=config_args.config_path)

        if not config_files:
            config_parser.error(
                "Must supply a config file.\nA config file can be automatically"
                ' generated using "--generate-config -H SERVER_NAME'
                ' -c CONFIG-FILE"'
            )

        if config_args.config_directory:
            config_dir_path = config_args.config_directory
        else:
            config_dir_path = os.path.dirname(config_files[-1])
        config_dir_path = os.path.abspath(config_dir_path)
        data_dir_path = os.getcwd()

        generate_missing_configs = config_args.generate_missing_configs

        obj = cls()

        if config_args.generate_config:
            if config_args.report_stats is None:
                config_parser.error(
                    "Please specify either --report-stats=yes or --report-stats=no\n\n"
                    + MISSING_REPORT_STATS_SPIEL
                )

            (config_path,) = config_files
            if not path_exists(config_path):
                print("Generating config file %s" % (config_path,))

                if config_args.data_directory:
                    data_dir_path = config_args.data_directory
                else:
                    data_dir_path = os.getcwd()
                data_dir_path = os.path.abspath(data_dir_path)

                server_name = config_args.server_name
                if not server_name:
                    raise ConfigError(
                        "Must specify a server_name to a generate config for."
                        " Pass -H server.name."
                    )

                config_str = obj.generate_config(
                    config_dir_path=config_dir_path,
                    data_dir_path=data_dir_path,
                    server_name=server_name,
                    report_stats=(config_args.report_stats == "yes"),
                    generate_secrets=True,
                    open_private_ports=config_args.open_private_ports,
                )

                if not path_exists(config_dir_path):
                    os.makedirs(config_dir_path)
                with open(config_path, "w") as config_file:
                    config_file.write("# vim:ft=yaml\n\n")
                    config_file.write(config_str)

                config_dict = yaml.safe_load(config_str)
                obj.generate_missing_files(config_dict, config_dir_path)

                print(
                    (
                        "A config file has been generated in %r for server name"
                        " %r. Please review this file and customise it"
                        " to your needs."
                    )
                    % (config_path, server_name)
                )
                return
            else:
                print(
                    (
                        "Config file %r already exists. Generating any missing config"
                        " files."
                    )
                    % (config_path,)
                )
                generate_missing_configs = True

        parser = argparse.ArgumentParser(
            parents=[config_parser],
            description=description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

        obj.invoke_all_static("add_arguments", parser)
        args = parser.parse_args(remaining_args)

        config_dict = read_config_files(config_files)
        if generate_missing_configs:
            obj.generate_missing_files(config_dict, config_dir_path)
            return None

        obj.parse_config_dict(
            config_dict, config_dir_path=config_dir_path, data_dir_path=data_dir_path
        )
        obj.invoke_all("read_arguments", args)

        return obj

    def parse_config_dict(self, config_dict, config_dir_path=None, data_dir_path=None):
        """Read the information from the config dict into this Config object.

        Args:
            config_dict (dict): Configuration data, as read from the yaml

            config_dir_path (str): The path where the config files are kept. Used to
                create filenames for things like the log config and the signing key.

            data_dir_path (str): The path where the data files are kept. Used to create
                filenames for things like the database and media store.
        """
        self.invoke_all(
            "read_config",
            config_dict,
            config_dir_path=config_dir_path,
            data_dir_path=data_dir_path,
        )

    def generate_missing_files(self, config_dict, config_dir_path):
        self.invoke_all("generate_files", config_dict, config_dir_path)


def read_config_files(config_files):
    """Read the config files into a dict

    Args:
        config_files (iterable[str]): A list of the config files to read

    Returns: dict
    """
    specified_config = {}
    for config_file in config_files:
        with open(config_file) as file_stream:
            yaml_config = yaml.safe_load(file_stream)
        specified_config.update(yaml_config)

    if "server_name" not in specified_config:
        raise ConfigError(MISSING_SERVER_NAME)

    if "report_stats" not in specified_config:
        raise ConfigError(
            MISSING_REPORT_STATS_CONFIG_INSTRUCTIONS + "\n" + MISSING_REPORT_STATS_SPIEL
        )
    return specified_config


def find_config_files(search_paths):
    """Finds config files using a list of search paths. If a path is a file
    then that file path is added to the list. If a search path is a directory
    then all the "*.yaml" files in that directory are added to the list in
    sorted order.

    Args:
        search_paths(list(str)): A list of paths to search.

    Returns:
        list(str): A list of file paths.
    """

    config_files = []
    if search_paths:
        for config_path in search_paths:
            if os.path.isdir(config_path):
                # We accept specifying directories as config paths, we search
                # inside that directory for all files matching *.yaml, and then
                # we apply them in *sorted* order.
                files = []
                for entry in os.listdir(config_path):
                    entry_path = os.path.join(config_path, entry)
                    if not os.path.isfile(entry_path):
                        err = "Found subdirectory in config directory: %r. IGNORING."
                        print(err % (entry_path,))
                        continue

                    if not entry.endswith(".yaml"):
                        err = (
                            "Found file in config directory that does not end in "
                            "'.yaml': %r. IGNORING."
                        )
                        print(err % (entry_path,))
                        continue

                    files.append(entry_path)

                config_files.extend(sorted(files))
            else:
                config_files.append(config_path)
    return config_files


__all__ = ["Config", "RootConfig"]
