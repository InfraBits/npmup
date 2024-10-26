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
import logging
import os
import time
from typing import Optional, List, Tuple, Dict

import jwt
import requests

logger: logging.Logger = logging.getLogger(__name__)


class GithubApp:
    _app_id: int
    _private_key: str
    _bearer_token: Optional[str] = None
    _bearer_token_expiry: Optional[int] = None

    def __init__(self, app_id: int, private_key: str) -> None:
        self._app_id = app_id
        self._private_key = private_key

    def _create_bearer_token(self) -> Tuple[str, int]:
        unix_now = int(time.time())
        unix_start = unix_now - 60  # Allow for clock drift
        unix_end = unix_now + 600
        return jwt.encode(
            payload={
                'iat': unix_start,
                'exp': unix_end,
                'iss': self._app_id
            },
            key=self._private_key,
            algorithm='RS256'
        ), unix_end

    def _get_bearer_token(self) -> str:
        if (
                self._bearer_token is None
                or self._bearer_token_expiry is None
                or self._bearer_token_expiry <= int(time.time())
        ):
            self._bearer_token, self._bearer_token_expiry = self._create_bearer_token()
        return self._bearer_token

    def _get_access_token(self, installation_id: int) -> str:
        r = requests.post(
            f'https://api.github.com/app/installations/{installation_id}/access_tokens',
            headers={
                'Authorization': f'Bearer {self._get_bearer_token()}',
            },
        )
        return str(r.json()['token'])

    def _get_installation_id(self, repository: str) -> Optional[int]:
        r = requests.get(
            f'https://api.github.com/repos/{repository}/installation',
            headers={
                'Authorization': f'Bearer {self._get_bearer_token()}',
            },
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return int(r.json()['id'])

    def get_access_token(self, repository: str) -> Optional[str]:
        installation_id = self._get_installation_id(repository)
        if installation_id is None:
            return None
        return self._get_access_token(installation_id)


class Git:
    def __init__(self, repository: str, branch_name: str, github_app: Optional[GithubApp]) -> None:
        self.repository = repository
        self._branch_name = branch_name
        self._github_app = github_app

    def _build_headers(self) -> Dict[str, str]:
        headers = {
            'Accept': 'application/vnd.github.v3+json'
        }
        if self._github_app is not None:
            access_token = self._github_app.get_access_token(self.repository)
            if access_token is not None:
                headers['Authorization'] = f'Bearer {access_token}'
        else:
            headers['Authorization'] = f'token {os.environ.get("GITHUB_TOKEN", "")}'
        return headers

    def get_default_branch(self) -> Tuple[str, str]:
        r = requests.get(f'https://api.github.com/repos/{self.repository}',
                         headers=self._build_headers())
        r.raise_for_status()
        data: Dict[str, Tuple[str, str]] = r.json()
        return data['default_branch']

    def get_head_ref(self) -> Tuple[Optional[str], Optional[str]]:
        r = requests.get('https://api.github.com/repos/'
                         f'{self.repository}/git/refs',
                         headers=self._build_headers())
        r.raise_for_status()

        default_branch = self.get_default_branch()
        for branch in r.json():
            if branch['ref'] == f'refs/heads/{default_branch}':
                return branch['ref'], branch['object']['sha']
        return None, None

    def create_branch(self, base_sha: str) -> str:
        r = requests.post('https://api.github.com/repos/'
                          f'{self.repository}/git/refs',
                          json={
                              'ref': f'refs/heads/{self._branch_name}',
                              'sha': base_sha,
                          },
                          headers=self._build_headers())
        r.raise_for_status()
        data: Dict[str, Dict[str, str]] = r.json()
        return data['object']['sha']

    def _create_blob(self, content: str) -> str:
        r = requests.post('https://api.github.com/repos/'
                          f'{self.repository}/git/blobs',
                          json={
                              'content': content,
                          },
                          headers=self._build_headers())
        r.raise_for_status()
        data: Dict[str, str] = r.json()
        return data['sha']

    def update_branch_files(self,
                            branch_sha: str,
                            files: Dict[str, str],
                            commit_summary: str,
                            commit_body: Optional[str]) -> None:
        r = requests.post('https://api.github.com/repos/'
                          f'{self.repository}/git/trees',
                          json={
                              'tree': [
                                  {
                                      'path': file,
                                      'mode': '100644',
                                      'type': 'blob',
                                      'sha': self._create_blob(contents)
                                  }
                                  for file, contents in files.items()
                              ],
                              'base_tree': branch_sha
                          },
                          headers=self._build_headers())
        r.raise_for_status()
        tree_data: Dict[str, str] = r.json()

        r = requests.post('https://api.github.com/repos/'
                          f'{self.repository}/git/commits',
                          json={
                              'tree': tree_data['sha'],
                              'message': (f'{commit_summary}\n{commit_body}'
                                          if commit_body else commit_summary),
                              'parents': [branch_sha]
                          },
                          headers=self._build_headers())
        r.raise_for_status()
        commit_data: Dict[str, str] = r.json()

        r = requests.patch('https://api.github.com/repos/'
                           f'{self.repository}/git/refs/heads/{self._branch_name}',
                           json={
                               'sha': commit_data['sha'],
                           },
                           headers=self._build_headers())
        r.raise_for_status()

    def create_pull_request(self, head_ref: str, summary: str, description: str) -> int:
        r = requests.post('https://api.github.com/repos/'
                          f'{self.repository}/pulls',
                          json={
                              'title': summary,
                              'body': description,
                              'head': self._branch_name,
                              'base': head_ref,
                          },
                          headers=self._build_headers())
        r.raise_for_status()
        data: Dict[str, int] = r.json()
        return data['number']

    def get_pull_request_actions(self, pull_request_id: int) -> Dict[str, str]:
        r = requests.get('https://api.github.com/repos/'
                         f'{self.repository}/actions/runs',
                         json={'branch': self._branch_name},
                         headers=self._build_headers())
        r.raise_for_status()
        return {
            action['name']: action['conclusion']
            for action in r.json()['workflow_runs']
            if action['conclusion']
            if action['event'] == 'pull_request'
            if any([pr['number'] == pull_request_id
                    for pr in action['pull_requests']])
        }

    def merge_pull_request(self, pull_request_id: int) -> None:
        r = requests.put(f'https://api.github.com/repos/'
                         f'{self.repository}/pulls/{pull_request_id}/merge',
                         json={'merge_method': 'rebase'},
                         headers=self._build_headers())
        r.raise_for_status()

    def create_issue_comment(self, issue_number: int, comment: str) -> None:
        r = requests.post(f'https://api.github.com/repos/'
                          f'{self.repository}/issues/{issue_number}/comments',
                          json={'body': comment},
                          headers=self._build_headers())
        r.raise_for_status()

    def delete_branch(self) -> None:
        r = requests.delete(f'https://api.github.com/repos/'
                            f'{self.repository}/git/refs/heads/{self._branch_name}',
                            headers=self._build_headers())
        r.raise_for_status()

    def wait_for_workflows(self, required_workflows: List[str], pull_request_id: int) -> bool:
        required_workflows = [required_workflow.lower()
                              for required_workflow in required_workflows]
        logger.info(f'Waiting for: {required_workflows}')

        while True:
            pull_request_actions = {
                k.lower(): v
                for k, v in self.get_pull_request_actions(pull_request_id).items()
            }
            logger.info(f'Found workflows: {pull_request_actions}')

            if all([
                workflow in pull_request_actions
                for workflow in required_workflows
            ]):
                logger.info('All required workflows have concluded')

                happy_bunny = True
                for required_workflow in required_workflows:
                    if pull_request_actions[required_workflow] != 'success':
                        logger.error(f'Workflow for {required_workflow} failed'
                                     f': {pull_request_actions[required_workflow]}')
                        happy_bunny = False
                return happy_bunny

            logger.info('Missing required workflows, waiting....')
            time.sleep(5)
