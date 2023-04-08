import re
from typing import Iterable, List, NamedTuple, Optional, Set, Tuple

from ggshield.core.filter import is_filepath_excluded
from ggshield.core.git_shell import git
from ggshield.core.text_utils import STYLE, format_text
from ggshield.core.utils import REGEX_HEADER_INFO, Filemode

from .scannable import Files, Scannable, StringScannable


_RX_HEADER_LINE_SEPARATOR = re.compile("[\n\0]:", re.MULTILINE)


class PatchParseError(Exception):
    """
    Raised by Commit.get_files() if it fails to parse its patch.
    """

    pass


def _parse_patch(patch: str, exclusion_regexes: Set[re.Pattern]) -> Iterable[Scannable]:
    """
    Parse the patch generated with `git show` (or `git diff`)

    If the patch represents a merge commit, then `patch` actually contains multiple
    commits, one per parent, because we call `git show` with the `-m` option to force it
    to generate one single-parent commit per parent. This makes later code simpler and
    ensures we see *all* the changes.
    """
    for commit in patch.split("\0commit "):
        tokens = commit.split("\0diff ", 1)
        if len(tokens) == 1:
            # No diff, carry on to next commit
            continue
        header, rest = tokens

        names_and_modes = _parse_patch_header(header)

        diffs = re.split(r"^diff ", rest, flags=re.MULTILINE)
        for (filename, filemode), diff in zip(names_and_modes, diffs):
            if is_filepath_excluded(filename, exclusion_regexes):
                continue

            # extract document from diff: we must skip diff extended headers
            # (lines like "old mode 100644", "--- a/foo", "+++ b/foo"...)
            try:
                end_of_headers = diff.index("\n@@")
            except ValueError:
                # No content
                continue
            # +1 because we searched for the '\n'
            content = diff[end_of_headers + 1 :]

            yield StringScannable(filename, content, filemode=filemode)


def _parse_patch_header(header: str) -> Iterable[Tuple[str, Filemode]]:
    """
    Parse the header of a raw patch, generated with -z --raw
    """

    if header[0] == ":":
        # If the patch has been generated by `git diff` and not by `git show` then
        # there is no commit info and message, add a blank line to simulate commit info
        # otherwise the split below is going to skip the first file of the patch.
        header = "\n" + header

    # First item returned by split() contains commit info and message, skip it
    for line in _RX_HEADER_LINE_SEPARATOR.split(header)[1:]:
        yield _parse_patch_header_line(f":{line}")


class CommitInformation(NamedTuple):
    author: str
    email: str
    date: str


class Commit(Files):
    """
    Commit represents a commit which is a list of commit files.
    """

    def __init__(
        self,
        sha: Optional[str] = None,
        exclusion_regexes: Optional[Set[re.Pattern]] = None,
    ):
        super().__init__([])
        self.sha = sha
        self._patch: Optional[str] = None
        self.exclusion_regexes: Set[re.Pattern] = exclusion_regexes or set()
        self._info: Optional[CommitInformation] = None

    @property
    def info(self) -> CommitInformation:
        if self._info is None:
            m = REGEX_HEADER_INFO.search(self.patch)

            if m is None:
                self._info = CommitInformation("unknown", "", "")
            else:
                self._info = CommitInformation(**m.groupdict())

        return self._info

    @property
    def optional_header(self) -> str:
        """Return the formatted patch."""
        return (
            format_text(f"\ncommit {self.sha}\n", STYLE["commit_info"])
            + f"Author: {self.info.author} <{self.info.email}>\n"
            + f"Date: {self.info.date}\n"
        )

    @property
    def patch(self) -> str:
        """Get the change patch for the commit."""
        if self._patch is None:
            common_args = [
                "--raw",  # shows a header with the files touched by the commit
                "-z",  # separate file names in the raw header with \0
                "--patch",  # force output of the diff (--raw disables it)
                "-m",  # split multi-parent (aka merge) commits into several one-parent commits
            ]
            if self.sha:
                self._patch = git(["show", self.sha] + common_args)
            else:
                self._patch = git(["diff", "--cached"] + common_args)

        return self._patch

    @property
    def files(self) -> List[Scannable]:
        if not self._files:
            self._files = list(self.get_files())

        return self._files

    def get_files(self) -> Iterable[Scannable]:
        """
        Parse the patch into files and extract the changes for each one of them.

        See tests/data/patches for examples
        """
        try:
            yield from _parse_patch(self.patch, self.exclusion_regexes)
        except Exception as exc:
            raise PatchParseError(f"Could not parse patch (sha: {self.sha}): {exc}")

    def __repr__(self) -> str:
        return f"<Commit sha={self.sha} files={self.files}>"


def _parse_patch_header_line(line: str) -> Tuple[str, Filemode]:
    """
    Parse a file line in the raw patch header, returns a tuple of filename, filemode

    See https://github.com/git/git/blob/master/Documentation/diff-format.txt for details
    on the format.
    """

    prefix, name, *rest = line.rstrip("\0").split("\0")

    if rest:
        # If the line has a new name, we want to use it
        name = rest[0]

    # for a non-merge commit, prefix is
    # :old_perm new_perm old_sha new_sha status_and_score
    #
    # for a 2 parent commit, prefix is
    # ::old_perm1 old_perm2 new_perm old_sha1 old_sha2 new_sha status_and_score
    #
    # We can ignore most of it, because we only care about the status.
    #
    # status_and_score is one or more status letters, followed by an optional numerical
    # score. We can ignore the score, but we need to check the status letters.
    status = prefix.rsplit(" ", 1)[-1].rstrip("0123456789")

    # There is one status letter per commit parent. In the case of a non-merge commit
    # the situation is simple: there is only one letter.
    # In the case of a merge commit we must look at all letters: if one parent is marked
    # as D(eleted) and the other as M(odified) then we use MODIFY as filemode because
    # the end result contains modifications. To ensure this, the order of the `if` below
    # matters.

    if "M" in status:  # modify
        return name, Filemode.MODIFY
    elif "C" in status:  # copy
        return name, Filemode.NEW
    elif "A" in status:  # add
        return name, Filemode.NEW
    elif "T" in status:  # type change
        return name, Filemode.NEW
    elif "R" in status:  # rename
        return name, Filemode.RENAME
    elif "D" in status:  # delete
        return name, Filemode.DELETE
    else:
        raise ValueError(f"Can't parse header line {line}: unknown status {status}")
