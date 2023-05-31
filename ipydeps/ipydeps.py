# vim: expandtab tabstop=4 shiftwidth=4

from importlib import invalidate_caches as importlib_invalidate_caches
from os import environ
from time import sleep
from typing import Callable, Dict, Sequence, Set, Union
from urllib.error import HTTPError
from urllib.request import urlopen

import json
import pkg_resources
import re
import subprocess
import sys

from pypki3 import loader as pki_loader
from pypki3 import NamedTemporaryKeyCertPaths, ssl_context
from temppath import TemporaryPathContext

from .config import config_dir, load_config
from .logger import logger
from .utils import (
    combine_key_and_cert,
    normalize_package_names,
    stdlib_packages,
)

package_name_pattern = re.compile(r'([A-Za-z][A-Za-z0-9_\-]+((<|>|<=|>=|==)[0-9]+\.[0-9]+(\.[0-9]+)*)?)')
pip_run_args = [sys.executable, '-m', 'pip']

def run_pip(
    packages: Sequence,
    use_pki: bool,
    verbose: bool,
    pip_config_path: Optional[Path],
):
    args = ['install']

    if verbose:
        args.append('-vvv')

    env = dict(environ)

    if pip_config_path:
        env['PIP_CONFIG_FILE'] = str(pip_config_path)

    if use_pki:
        with NamedTemporaryKeyCertPaths() as key_cert_paths:
            key_path = key_cert_paths[0]
            cert_path = key_cert_paths[1]
            ca_path = pki_loader.ca_path()

            with TemporaryPathContext() as combined_key_cert_path:
                combine_key_and_cert(combined_key_cert_path, key_path, cert_path)
                args.append(f'--client-cert={combined_key_cert_path}')
                args.append(f'--cert={ca_path}')
                return run_get_stderr(pip_run_args+args+packages, env=env)

    return run_get_stderr(pip_run_args+args+packages, env=env)

def invalidate_cache():
    '''
    Invalidates the import cache so the next attempt to import a package
    will look for new import locations.
    '''
    importlib_invalidate_caches()
    sleep(2)

def refresh_available_packages():
    '''
    Forces a rescan of available packages in pip's vendored pkg_resources
    and the main pkg_resources package, also used by pbr.
    '''
    for entry in sys.path:
        pkg_resources.working_set.add_entry(entry)

def valid_pkg_names(s: str):
    '''
    Finds potential package names using a regex
    so weird strings that might contain code
    don't get through.  This also allows version
    specifiers.
    '''
    return [x[0] for x in package_name_pattern.findall(s)]

def get_pkg_names(x: Union[str, Sequence]) -> Set:
    if isinstance(x, (list, tuple)):
        x = ' '.join(x)

    packages = (p.strip() for p in valid_pkg_names(x))
    return set(p for p in packages if len(p) > 0)

def py_name_micro():
    return f'python-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}'

def py_name_minor():
    return f'python-{sys.version_info.major}.{sys.version_info.minor}'

def py_name_major():
    return f'python-{sys.version_info.major}'

def case_insensitive_dependencies_json(dep_json):
    lowercased = {}

    for version in dep_json:
        lowercased[version] = {}
        packages = dep_json[version]

        for pkg in packages:
            pkg = pkg.lower()
            cmds = packages[pkg]

            if pkg in lowercased[version]:
                logger.warning('Duplicate package name %s in dependencies JSON.  Package names are case-insensitive.  Overwriting!', pkg)

            lowercased[version][pkg] = cmds

    return lowercased

def get_dependencies_link_urlopener(config: Config) -> Callable:
    if config.dependencies_link_requires_pki:
        ctx = ssl_context()
        return lambda url: urlopen(url, context=ctx)

    return urlopen

def read_dependencies_json(config):
    urlopener = get_dependencies_link_urlopener(config)

    try:
        resp = urlopener(config.dependencies_link)
    except HTTPError as e:
        logger.error(str(e.read(), encoding='utf8'))
        return {}

    d = str(resp.read(), encoding='utf8')

    try:
        j = json.loads(d)
    except json.decoder.JSONDecodeError as e:
        logger.error(str(e))
        return {}

    return case_insensitive_dependencies_json(j)

def find_overrides(packages: Set, config: Config) -> Dict[str, Sequence[str]]:
    if len(packages) == 0:
        return {}

    dep_json = read_dependencies_json(config)
    major = py_name_major()
    minor = py_name_minor()
    micro = py_name_micro()

    overrides = {}

    # Check major, then minor, then micro so the most
    # specific override is used for a particular package.
    if major in dep_json:
        for pkg in packages:
            if pkg in dep_json[major]:
                overrides[pkg] = dep_json[major][pkg]

    if minor in dep_json:
        for pkg in packages:
            if pkg in dep_json[minor]:
                overrides[pkg] = dep_json[minor][pkg]

    if micro in dep_json:
        for pkg in packages:
            if pkg in dep_json[micro]:
                overrides[pkg] = dep_json[micro][pkg]

    return overrides

def run_get_stderr(cmd, env=environ) -> Tuple[int, Optional[str]]:
    returncode = 0
    err = None

    try:
        subprocess.check_output(cmd, stderr=subprocess.PIPE, env=env)
    except subprocess.CalledProcessError as e:
        returncode = e.returncode
        err = str(e.stderr, encoding='utf8')

    return returncode, err

def get_freeze_package_name(info):
    name, _ = info.split('==')
    return name.strip()

def process_pip_freeze_output(pkgs) -> list:
    pkgs = str(pkgs, encoding='utf8').split('\n')
    pkgs = [p for p in pkgs if len(p) > 0 and '==' in p]
    pkgs = [get_freeze_package_name(p) for p in pkgs]
    return pkgs

def pip_freeze_packages():
    pkgs = subprocess.check_output(pip_run_args + ['freeze','--all'])
    return process_pip_freeze_output(pkgs)

def currently_installed() -> Set:
    pr = set([pkg.project_name for pkg in pkg_resources.working_set])
    pf = set(pip_freeze_packages())
    return {p.lower() for p in pr|pf}

def subtract_installed(already_installed: Set, requested: Set) -> Set:
    requested_packages = set((p.lower() for p in requested))  # removes duplicates
    return packages - already_installed

def subtract_stdlib(packages: Set) -> Set:
    '''
    Understandably, some users request to install packages
    that are actually in the standard library, so log it
    and remove it from the list.
    '''
    stdlib = stdlib_packages()

    for package in packages & stdlib:
        logger.warning('%s is part of the Python standard library and will be skipped.  Remove it from the list to remove this warning.', package)

    return packages - stdlib

def run_and_log_error(cmd):
    returncode, err = run_get_stderr(cmd)

    if returncode != 0 and err is not None:
        logger.error(err)

def run_overrides(overrides):
    for name, cmds in overrides.items():
        logger.info('Executing overrides for %s', name)

        for command in cmds:
            if len(command) > 0:
                logger.debug(' '.join(command))
                run_and_log_error(command)

def log_currently_installed(before: Set, requested: Set) -> None:
    already_installed = before & requested

    if len(already_installed) > 0:
        logger.info('Packages currently installed: %s', ', '.join(sorted(list(already_installed))))

def log_before_after(before: Set, after: Set) -> None:
    new_packages = after - before

    if len(new_packages) == 0:
        logger.warning('No new packages installed')
    elif len(new_packages) > 0:
        logger.info('New packages installed: {0}'.format(', '.join(sorted(list(new_packages)))))

def find_pip_config_path(config_name: Optional[str], configs_path: Path):
    if configs_path is None:
        return None

    return configs_path / config_name

def pip_config_found(config_name: Optional[str], pip_config_path: Optional[Path]) -> bool:
    if config_name is None:
        return True

    if pip_config_path.exists():
        return True

    logger.error('Could not find pip config named %s at %s', config_name, pip_config_path)
    return False

def pip(
    requested_packages: Union[str, Sequence],
    verbose: bool=False,
    use_pki: bool=False,
    use_overrides: bool=True,
    config: Optional[str]=None,
) -> None:
    configs_path = config_dir(environ)
    ipydeps_config = load_config(configs_path)
    pip_config_path = find_pip_config_path(config, configs_path)

    if not pip_config_found(config, pip_config_path):
        return

    packages_before_install = currently_installed()

    requested_packages = get_pkg_names(requested_packages)
    requested_packages = normalize_package_names(requested_packages)
    requested_packages = subtract_stdlib(requested_packages)

    # ignore items that have already been installed
    log_currently_installed(packages_before_install, requested_packages)
    requested_packages = subtract_installed(packages_before_install, requested_packages)

    if use_overrides:
        run_overrides(find_overrides(requested_packages, ipydeps_config))

    # now that overrides have run, calculate and subtract what's installed again
    refresh_available_packages()
    packages_to_install = list(subtract_installed(currently_installed(), requested_packages))

    if len(packages) > 0:
        logger.debug('Running pip to install %s', ', '.join(sorted(packages)))
        returncode, err = run_pip(packages_to_install, use_pki, verbose, pip_config_path)

        if returncode != 0 and err is not None:
            logger.error(err)

        invalidate_cache()
        refresh_available_packages()

    packages_after_install = currently_installed()
    log_before_after(packages_before_install, packages_after_install)
    logger.debug('Done')
