#!/usr/bin/env bash
# Decide the version for this build and whether it is a release.
#
# Inputs (environment):
#   EVENT     push | pull_request | workflow_dispatch | ...   (github.event_name)
#   REF_TYPE  branch | tag                                    (github.ref_type)
#   REF_NAME  branch or tag name                              (github.ref_name)
#   BUMP      auto | patch | minor | major | none             (manual-run override; default auto)
#   RUN       run number, for development versions
#   RELEASE_BRANCHES  space-separated branches that auto-release (default "main master")
#
# Rules on a release branch (auto):
#   - no earlier vX.Y.Z tag  -> release the base version in dbmigrator/__init__.py
#   - "!:" subject or "BREAKING CHANGE" in a body since the last tag -> major
#   - any "feat:" subject                                            -> minor
#   - any other subject except docs/ci/chore/test/style/build:       -> patch
#   - only those housekeeping types, or "[skip release]" in HEAD     -> no release
#
# Prints key=value lines (version, tag, release, bump, previous) for $GITHUB_OUTPUT.
set -euo pipefail

EVENT=${EVENT:-push}
REF_TYPE=${REF_TYPE:-branch}
REF_NAME=${REF_NAME:-$(git rev-parse --abbrev-ref HEAD)}
BUMP=${BUMP:-auto}
RUN=${RUN:-0}
RELEASE_BRANCHES=${RELEASE_BRANCHES:-main master}
SHA=$(git rev-parse --short=7 HEAD)

emit() {  # version release bump previous
  echo "version=$1"
  echo "tag=v$1"
  echo "release=$2"
  echo "bump=$3"
  echo "previous=$4"
}

# A pushed tag is a release of exactly that version.
if [[ $REF_TYPE == tag ]]; then
  emit "${REF_NAME#v}" true tag "$REF_NAME"
  exit 0
fi

previous=$(git tag --merged HEAD --list 'v*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n1 || true)
base=$(sed -nE 's/^__version__ = "([0-9]+\.[0-9]+\.[0-9]+).*"/\1/p' dbmigrator/__init__.py)

if [[ -z $previous ]]; then
  bump=initial
else
  subjects=$(git log --format=%s "$previous..HEAD")
  bodies=$(git log --format=%b "$previous..HEAD")
  if [[ -z $subjects ]]; then
    bump=none
  elif grep -qE '^[a-zA-Z]+(\([^)]*\))?!:' <<<"$subjects" || grep -q 'BREAKING CHANGE' <<<"$bodies"; then
    bump=major
  elif grep -qE '^feat(\([^)]*\))?:' <<<"$subjects"; then
    bump=minor
  elif grep -qvE '^(docs|ci|chore|test|style|build)(\([^)]*\))?:' <<<"$subjects"; then
    bump=patch
  else
    bump=none
  fi
fi

[[ $BUMP != auto ]] && bump=$BUMP
git log -1 --format=%B | grep -qi '\[skip release\]' && [[ $BUMP == auto ]] && bump=none

if [[ -z $previous ]]; then
  next=$base
else
  IFS=. read -r major minor patch <<<"${previous#v}"
  case $bump in
    major) next="$((major + 1)).0.0" ;;
    minor) next="$major.$((minor + 1)).0" ;;
    *)     next="$major.$minor.$((patch + 1))" ;;  # patch, and the preview number when not releasing
  esac
fi

release=false
if [[ $bump != none && " $RELEASE_BRANCHES " == *" $REF_NAME "* && ( $EVENT == push || $EVENT == workflow_dispatch ) ]]; then
  release=true
fi

if [[ $release == true ]]; then
  emit "$next" true "$bump" "${previous:-none}"
else
  emit "$next.dev$RUN+$SHA" false "$bump" "${previous:-none}"
fi
