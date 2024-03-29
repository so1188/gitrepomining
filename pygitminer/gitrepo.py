# Copyright 2018 Safa Omri


import logging
import os
from pathlib import Path
from threading import Lock
from typing import List, Dict, Set, Generator

from git import Git, Repo, GitCommandError, Commit as GitCommit

from pygitminer.gitcommits import Commit, ModificationType, Modification
from pygitminer.configuration import Conf

logger = logging.getLogger(__name__)

# this class helps obtaining the list of commits, checkout, reset, etc.
class GitRepository:
    
    def __init__(self, path: str, conf=None):
        self.path = Path(path)
        self.project_name = self.path.name
        self.lock = Lock()
        self._git = None
        self._repo = None

        if conf is None:
            conf = Conf({
                "path_to_repo": str(self.path),
                "git_repo": self
            })

        self._conf = conf
        self._conf.set_value("main_branch", None)  # init main_branch to None

    @property
    # GitPython object Git.
    def git(self):
        if self._git is None:
            self._open_git()
        return self._git

    @property
    # GitPython object Repo.
    def repo(self):
        if self._repo is None:
            self._open_repository()
        return self._repo

    def _open_git(self):
        self._git = Git(str(self.path))

    def clear(self):
        if self._git:
            self.git.clear_cache()
        if self._repo:
            self.repo.git.clear_cache()

    def _open_repository(self):
        self._repo = Repo(str(self.path))
        self._repo.config_writer().set_value("blame", "markUnblamableLines", "true").release()
        if self._conf.get("main_branch") is None:
            self._main_branch(self._repo)

    def _main_branch(self, repo):
        try:
            self._conf.set_value("main_branch", repo.active_branch.name)
        except TypeError:
            logger.info("HEAD is a detached symbolic reference, setting main branch to empty string")
            self._conf.set_value("main_branch", '')

    def get_head(self) -> Commit:
        head_commit = self.repo.head.commit
        return Commit(head_commit, self._conf)

    def list_of_commits(self, rev='HEAD', **kwargs) -> Generator[Commit, None, None]:    
        if 'reverse' not in kwargs:
            kwargs['reverse'] = True

        for commit in self.repo.iter_commits(rev=rev, **kwargs):
            yield self.get_commit_from_gitpython(commit)

    def get_commit(self, commit_id: str) -> Commit:
        gp_commit = self.repo.commit(commit_id)
        return Commit(gp_commit, self._conf)


    # Build a pygitminer commit object from a GitPython commit object.
    def get_commit_from_gitpython(self, commit: GitCommit) -> Commit:
        # GitCommit commit: GitPython commit
        return Commit(commit, self._conf)  #Commit commit: pygitminer commit

    def checkout(self, _hash: str) -> None:
        with self.lock:
            self._delete_tmp_branch()
            self.git.checkout('-f', _hash, b='_PD')

    def _delete_tmp_branch(self) -> None:
        try:
            if self.repo.active_branch.name == '_PD':
                self.git.checkout('-f', self._conf.get("main_branch"))
            self.repo.delete_head('_PD', force=True)
        except GitCommandError:
            logger.debug("Branch _PD not found")

    def files(self) -> List[str]:
        _all = []
        for path, _, files in os.walk(str(self.path)):
            if '.git' in path:
                continue
            for name in files:
                _all.append(os.path.join(path, name))
        return _all

    def reset(self) -> None:
        with self.lock:
            self.git.checkout('-f', self._conf.get("main_branch"))
            self._delete_tmp_branch()

    # Calculate total number of commits.
    def total_commits(self) -> int:
        return len(list(self.list_of_commits()))

    def get_commit_from_tag(self, tag: str) -> Commit:
        try:
            selected_tag = self.repo.tags[tag]
            return self.get_commit(selected_tag.commit.hexsha)
        except (IndexError, AttributeError):
            logger.debug('Tag %s not found', tag)
            raise

    def get_tagged_commits(self):
        tags = []
        for tag in self.repo.tags:
            if tag.commit:
                tags.append(tag.commit.hexsha)
        return tags

    def get_last_modified_lines(self, commit: Commit,                      # the commit to analyze
                                        modification: Modification = None,         # single modification to analyze
                                        hashes_to_ignore_path: str = None) \
            -> Dict[str, Set[str]]:

        if modification is not None:
            modifications = [modification]
        else:
            modifications = commit.modifications

        return self._last_commits_calculate(commit, modifications,
                                            hashes_to_ignore_path)        # return: the set containing all the bug inducing commits

    def _last_commits_calculate(self, commit: Commit,
                                modifications: List[Modification],
                                hashes_to_ignore_path: str = None) \
            -> Dict[str, Set[str]]:

        commits = {} # type: Dict[str, Set[str]]

        for mod in modifications:
            path = mod.new_path
            if mod.change_type == ModificationType.RENAME or mod.change_type == ModificationType.DELETE:
                path = mod.old_path
            deleted_lines = mod.diff_parsed['deleted']

            try:
                blame = self._get_blame(commit.hash, path, hashes_to_ignore_path)
                for num_line, line in deleted_lines:
                    if not self._line_useless(line.strip()):
                        buggy_commit = blame[num_line - 1].split(' ')[0].replace('^', '')

                        if buggy_commit.startswith("*"):
                            continue

                        if mod.change_type == ModificationType.RENAME:
                            path = mod.new_path

                        commits.setdefault(path, set()).add(self.get_commit(buggy_commit).hash)
            except GitCommandError:
                logger.debug(
                    "Could not found file %s in commit %s. Probably a double "
                    "rename!", mod.filename, commit.hash)

        return commits

    def _get_blame(self, commit_hash: str, path: str, hashes_to_ignore_path: str = None):
        args = ['-w', commit_hash + '^']
        if hashes_to_ignore_path is not None:
            if self.git.version_info >= (2, 23):
                args += ["--ignore-revs-file", hashes_to_ignore_path]
            else:
                logger.info("'--ignore-revs-file' is only available from git v2.23")
        return self.git.blame(*args, '--', path).split('\n')

    @staticmethod
    def _line_useless(line: str):
        return not line or \
               line.startswith('//') or \
               line.startswith('#') or \
               line.startswith("/*") or \
               line.startswith("'''") or \
               line.startswith('"""') or \
               line.startswith("*")

    def get_modified_file(self, filepath: str) -> List[str]:      # str filepath: path to the file
        
        path = str(Path(filepath))

        commits = []
        try:
            commits = self.git.log("--follow", "--format=%H", path).split('\n')
        except GitCommandError:
            logger.debug("Could not find information of file %s", path)

        return commits        # return: the list of commits' hash

    def _delete(self):
        self.clear()
