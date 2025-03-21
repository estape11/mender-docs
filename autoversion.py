#!/usr/bin/python3
# Copyright 2019 Northern.tech AS
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import argparse
import os
import re
import sys

UPDATE = 1
CHECK = 2
MODE = UPDATE

# What to update
COMPONENT = None
VERSION = None

# Just a way to warn if _nothing_ was updated
REPOS_CACHE = []

# Match version strings.
YOCTO_BRANCHES = r"(?:dora|daisy|dizzy|jethro|krogoth|morty|pyro|rocko|sumo|thud|warrior|zeus|dunfell|gatesgarth|kirkstone|langdale|mickledore|scarthgap)"
EXACT_VERSION_MATCH = r"(?<![0-9]\.)(?<![0-9])[1-9][0-9]*\.[0-9]+\.[x0-9]+(?:b[0-9]+)?(?:-build[0-9]+)?(?![0-9])(?!\.[0-9])(?:-rc\.[0-9]+)?(?![0-9])(?!\.[0-9])"
VERSION_MATCHER = r"(?:%s|(?:mender-%s)|(?<![a-z])(?:%s|master)(?![a-z]))" % (
    EXACT_VERSION_MATCH,
    EXACT_VERSION_MATCH,
    YOCTO_BRANCHES,
)
MINOR_VERSIONS_MATCHER = r"(?:(?<!\.)\s*\d+\.\d+[, ]?(?!\.\d))+"

ERRORS_FOUND = False


def walk_tree():
    exclude_dirs = [
        "node_modules",  # Several readme.md with version strings
        "03.Open-source-licenses",  # References to old versions
    ]
    for dirpath, dirs, filenames in os.walk(".", topdown=True):
        dirs[:] = list(filter(lambda x: not x in exclude_dirs, dirs))
        for file in filenames:
            if not file.endswith(".md") and not file.endswith(".markdown"):
                continue

            process_file(os.path.join(dirpath, file))


def process_file(file):
    if MODE == UPDATE:
        newname = "%s.new" % file
        new = open(newname, "w")
    else:
        new = None
    lineno = 0
    try:
        with open(file) as orig:
            tag_search = re.compile("^ *<!-- *AUTOVERSION *:")
            # When empty, signals that autoversioning is not active. When
            # filled, contains replacements to be made on the line.
            replacements = []

            in_code_block = False

            first_line = True

            in_page_header = False
            page_header_lines = []

            for line in orig.readlines():
                lineno += 1

                # Deal with page header which may have a following tag instead
                # of a preceding tag.
                if first_line:
                    first_line = False
                    if line.strip() == "---":
                        in_page_header = True
                        page_header_lines.append(line)
                        continue
                if in_page_header:
                    page_header_lines.append(line)
                    if line.strip() == "---":
                        in_page_header = False
                    continue

                # Deal with code blocks.
                if not in_code_block and tag_search.match(line):
                    replacements = parse_autoversion_tag(line)
                    # Apply replacing/checking to page header blocks.
                    if len(page_header_lines) > 0:
                        for ph_line in page_header_lines:
                            process_line(ph_line, replacements, new)
                        page_header_lines = []
                    if MODE == UPDATE:
                        new.write(line)
                    continue
                if line.startswith("```"):
                    if in_code_block:
                        in_code_block = False
                        replacements = []
                    else:
                        in_code_block = True

                # Apply replacing/checking to page header blocks.
                if len(page_header_lines) > 0:
                    for ph_line in page_header_lines:
                        process_line(ph_line, replacements, new)
                    page_header_lines = []

                # Actual replacing/checking of line.
                process_line(line, replacements, new)

                if not in_code_block and len(line.strip()) == 0:
                    # Outside code blocks we only keep replacement list for one
                    # paragraph, separated by empty line.
                    replacements = []

            # Output leftover page header lines. This could happen if the header
            # is the only thing in the file.
            if len(page_header_lines) > 0:
                for ph_line in page_header_lines:
                    process_line(ph_line, replacements, new)

        if MODE == UPDATE:
            new.close()
            os.rename(newname, file)
    except Exception as exc:
        if MODE == UPDATE:
            new.close()
            os.remove(newname)

        # A little hacky: Extend the error message with the filename.
        args = exc.args
        if not args:
            arg0 = ""
        else:
            arg0 = args[0]
        arg0 = "%s:%d: %s" % (file, lineno, arg0)
        exc.args = (arg0,) + args[1:]
        raise


def parse_autoversion_tag(tag):
    # Returns a structure like this:
    # [
    #     {
    #         "search": For example: "-b %" to match -b parameter with version.
    #         "repo": Git repository whose version should be substituted.
    #     },
    #     ...
    # ]

    # Match a string like:
    # <!--AUTOVERSION: "-b %"/integration "integration-%"/integration-->
    # and allow escaped double quotes in the match string (inside double quotes
    # in example).
    tag_match = re.match("^ *<!-- *AUTOVERSION *: *(.*)--> *$", tag)
    if not tag_match:
        raise Exception("Malformed AUTOVERSION tag:\n%s" % tag)
    end_of_whole_tag = tag_match.end(1)

    matcher = re.compile(r'"((?:[^"]|\\")*)"/([-a-z]+) *')
    last_end = -1
    parsed = []
    pos = tag_match.start(1)
    while True:
        match = matcher.match(tag, pos=pos, endpos=end_of_whole_tag)
        if not match:
            break
        pos = match.end()
        last_end = pos
        expr = match.group(1).replace('\\"', '"')
        repo = match.group(2)

        if "%" not in expr:
            raise Exception(
                "Search string \"%s\" doesn't contain at least one '%%'" % expr
            )
        parsed.append({"search": expr, "repo": repo})
    if last_end != end_of_whole_tag:
        raise Exception(
            (
                "AUTOVERSION tag not parsed correctly:\n%s" + "Example of valid tag:\n"
                '<!--AUTOVERSION: "git clone -b %%"/integration "Mender Client %%"/mender "docker version %%"/ignore-->'
            )
            % tag
        )
    return parsed


def process_line(line, replacements, fd):
    # Process a line using the given replacements, optionally writing to a file
    # if it is not None.

    # First run a pass over the line, where we remove all replacements, and then
    # check if there are any "version-looking" strings left, which there should
    # not be.
    all_removed = do_replacements(line, replacements, just_remove=True)
    match = re.search(VERSION_MATCHER, all_removed)
    if match:
        sep = "-------------------------------------------------------------------------------"
        end = "==============================================================================="
        print(
            (
                'ERROR: Found version-looking string "%s" in documentation line, not covered by any AUTOVERSION expression. '
                + "Original line:\n\n%s\n%s%s\n\n"
                + "AUTOVERSION expressions in effect:\n%s\n\n"
                + "Line after removing all AUTOVERSION matched sections:\n\n%s\n%s%s\n\n"
                + "See README-autoversion.markdown for more information.\n\n%s"
            )
            % (
                match.group(0),
                sep,
                line,
                sep,
                "None"
                if len(replacements) == 0
                else "\n".join(
                    [
                        '"%s"/%s' % (repl["search"], repl["repo"])
                        for repl in replacements
                    ]
                ),
                sep,
                all_removed,
                sep,
                end,
            )
        )
        global ERRORS_FOUND
        ERRORS_FOUND = True

    # If we were not given a file, then we are just doing checking and are done.
    if fd is None:
        return None

    # Now do the replacement and write that.
    all_replaced = do_replacements(line, replacements, just_remove=False)
    fd.write(all_replaced)


def do_replacements(line, replacements, just_remove):
    all_replaced = line
    for search, repo, in [(repl["search"], repl["repo"]) for repl in replacements]:
        if repo != "ignore" and repo not in REPOS_CACHE:
            REPOS_CACHE.append(repo)
        if len(search.strip()) <= 2:
            raise Exception(
                "Search string needs to be longer/more specific than just '%s'" % search
            )
        escaped = re.escape(search)
        regex = escaped.replace(r"%", VERSION_MATCHER)
        if just_remove:
            repl = search.replace(r"%", "")
        else:
            if repo == "ignore" or repo != COMPONENT:
                continue
            repl = search.replace("%", VERSION)
        all_replaced = re.sub(regex, repl, all_replaced)
    return all_replaced


DESCRIPTION = """With `--update`, updates the version references for COMPONENT to VERSION.

Examples:
    autoversion.py --update --component mender-artifact --version 1.2.3
    autoversion.py --update --component mender-connect --version 4.5.6

With `--check` it verifies that there are no version references with a missing AUTOVERSION tag.
"""


def main():
    global MODE
    global ERRORS_FOUND
    global COMPONENT
    global VERSION
    global REPOS_CACHE

    parser = argparse.ArgumentParser(
        description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check that there are no dangling version references",
    )
    parser.add_argument(
        "--update", action="store_true", help="Update all version references"
    )
    parser.add_argument(
        "--component", help="Component to update, it matches the Git repository name",
    )
    parser.add_argument(
        "--version", help="Version to update to",
    )

    parser.add_argument(
        "--poky-version",
        help="poky version to update to (usually a branch). This is an special case which ignores --component and --version flags",
    )

    args = parser.parse_args()

    if args.update and args.check:
        raise Exception("--check and --update are mutually exclusive")
    elif args.update:
        if args.component is None and args.version is None:
            raise Exception(
                "--component and --version are require to --update something"
            )
        MODE = UPDATE
        COMPONENT = args.component
        VERSION = args.version

    elif args.check:
        MODE = CHECK
    else:
        raise Exception("Either --check or --update must be given")

    walk_tree()

    if args.update and args.component not in REPOS_CACHE:
        print(
            f"Component '{args.component}' was not found anywhere in the docs content."
        )
        sys.exit(1)

    if ERRORS_FOUND:
        print("Errors found. See printed messages.")
        sys.exit(1)

    if args.check:
        print("All good. List of components found: " + ", ".join(REPOS_CACHE))


if __name__ == "__main__":
    main()
