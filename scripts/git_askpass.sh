#!/bin/sh
# Answers git's username and password prompts from the environment, so the
# Bitbucket token is never on a command line, in a URL, in a file or in the
# job log. gather_topology.yml sets GIT_ASKPASS to this script for its git
# commands; the AWX Bitbucket credential sets the two variables.
case "$1" in
  Username*) printf '%s\n' "$BITBUCKET_USERNAME" ;;
  *) printf '%s\n' "$BITBUCKET_TOKEN" ;;
esac
