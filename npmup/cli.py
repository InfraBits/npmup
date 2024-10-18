'''
npmup - Simple packages updater

MIT License

Copyright (c) 2024 Infra Bits

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
'''
import base64
import json
import logging
import subprocess
import sys
import uuid
from pathlib import PosixPath
from typing import Optional, Tuple, Dict

import click

from .git import GithubApp, Git
from .settings import Settings

logger: logging.Logger = logging.getLogger(__name__)


def _update(path: PosixPath) -> Tuple[Dict[str, str], Optional[str], Optional[str]]:
    base_path = PosixPath(path)
    packages_path = base_path / 'package.json'
    if not packages_path.is_file():
        logger.info(f'Could not find packages ({packages_path})')
        return {}, None, None

    package_lock_path = base_path / 'package-lock.json'
    if not package_lock_path.is_file():
        logger.info(f'Could not find lock ({package_lock_path})')
        return {}, None, None

    logger.info('Calling ncu')
    stdout = subprocess.check_output(['ncu', '-u', '--jsonUpgraded'],
                                     cwd=base_path.as_posix())
    try:
        updates = json.loads(stdout.decode('utf-8'))
    except ValueError:
        logger.info(f'Failed to decode ncu output: {stdout!r}')
        return {}, None, None

    if updates:
        logger.info('Calling npm install')
        subprocess.check_call(['npm', 'install', '--package-lock-only'],
                              cwd=base_path.as_posix())

    with packages_path.open('r') as fh:
        packages_data = fh.read()

    with package_lock_path.open('r') as fh:
        package_lock_data = fh.read()

    return updates, packages_data, package_lock_data


def _merge(settings: Settings,
           repository: str,
           github_app: Optional[GithubApp],
           updated_packages: Dict[str, str],
           packages: str,
           package_lock: str) -> None:
    branch_name = f'npmup-{uuid.uuid4()}'
    logger.info(f'Merging updated packages files using {branch_name}')

    # Handle the merging logic as required
    git = Git(repository, branch_name, github_app)
    head_ref, head_sha = git.get_head_ref()
    if not head_ref or not head_sha:
        logger.error('Failed to get head ref')
        return

    branch_sha = git.create_branch(head_sha)

    commit_summary = f'npmup ({len(updated_packages)} updates)'
    commit_description = ''
    for name, version in updated_packages.items():
        commit_description += f'`{name}`: `{version}`\n'

    logger.info(f'Using commit summary: {commit_summary}')
    logger.info(f'Using commit description: {commit_description}')
    git.update_branch_files(
        branch_sha,
        {
            'package.json': packages,
            'package-lock.json': package_lock
        },
        commit_summary.strip(),
        commit_description.strip()
    )

    logger.info(f'Creating pull request for {branch_name}')
    assert branch_sha is not None
    if pull_request_id := git.create_pull_request(head_ref,
                                                  commit_summary.strip(),
                                                  commit_description.strip()):
        logger.info(f'Waiting for workflows to complete on {branch_name}')
        if git.wait_for_workflows(settings.workflows, pull_request_id):
            logger.info(f'Merging pull request {pull_request_id}')
            git.merge_pull_request(pull_request_id)
        else:
            logger.info(f'Closing failed pull request {pull_request_id}')
            try:
                git.create_issue_comment(
                    pull_request_id,
                    f'Expected workflow ({", ".join(settings.workflows)}) failed'
                )
            except Exception as e:
                logger.exception('Failed to create commit comment', e)
            git.delete_branch()


@click.command()
@click.option('--debug', is_flag=True, help='Increase logging level to debug')
@click.option('--merge', is_flag=True, help='Merge changes into a GitHub repo')
@click.option('--repository', help='Name of the GitHub repo these files belong to')
@click.option('--github-app-id', type=int, help='GitHub app id')
@click.option('--github-app-key', type=str, help='GitHub app private key')
@click.option('--path', help='Path to update', type=PosixPath, default=PosixPath.cwd())
def cli(debug: bool,
        path: PosixPath,
        merge: bool,
        repository: str,
        github_app_id: Optional[int] = None,
        github_app_key: Optional[str] = None) -> None:
    '''npmup - Simple packages updater.'''
    logging.basicConfig(stream=sys.stderr,
                        level=(logging.DEBUG if debug else logging.INFO),
                        format='%(asctime)-15s %(message)s')

    if merge and not repository:
        click.echo("--merge requires --repository")
        return

    # Load the settings for our runtime
    settings = Settings.load(path)
    logger.info(f'Using settings: {settings}')

    # Setup a GithubApp if needed
    github_app: Optional[GithubApp] = None
    if github_app_id and github_app_key:
        github_app = GithubApp(github_app_id, base64.b64decode(github_app_key).decode('utf-8'))

    # Perform the actual updates
    updates, packages, package_locks = _update(path)
    logger.info(f'Found {len(updates)} updates')

    # Create a pull request if required & we have changes
    if merge and updates and packages and package_locks:
        _merge(settings, repository, github_app,
               updates, packages, package_locks)


if __name__ == '__main__':
    cli()
