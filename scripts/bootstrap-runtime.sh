#!/bin/sh
# Non-root startup. Never sources credentials or changes an existing profile.
set -eu
umask 077
mkdir -p "$HERMES_HOME/skills" "$TMPDIR"
if [ ! -e "$HERMES_HOME/skills/doer-loop" ]; then
    cp -r /opt/doer-skills/doer-loop "$HERMES_HOME/skills/"
fi
exec "$@"
