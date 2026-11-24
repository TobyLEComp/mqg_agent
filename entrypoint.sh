#!/bin/sh
# Generate shiro.ini from the environment at boot: credentials must never be
# baked into the image or committed. Fuseki reads $FUSEKI_BASE/shiro.ini.
#
# Auth is OPT-IN (decision 2026-07-09: run open for now, harden before real
# data - see roadmap "auth before real data"). Set FUSEKI_USER + FUSEKI_PASSWORD
# to enable Basic auth on every endpoint; leave unset to run anonymous.
set -eu

if [ -n "${FUSEKI_USER:-}" ] && [ -n "${FUSEKI_PASSWORD:-}" ]; then
    cat > "${FUSEKI_BASE}/shiro.ini" <<EOF
[main]
plainMatcher = org.apache.shiro.authc.credential.SimpleCredentialsMatcher
iniRealm.credentialsMatcher = \$plainMatcher

[users]
${FUSEKI_USER} = ${FUSEKI_PASSWORD}

[urls]
# /\$/ping stays anonymous so container healthchecks need no credentials. It is a
# timestamp and touches no dataset, so it reveals nothing.
#
# /kg/health is NOT listed here, and that absence is the point (2026-09-29).
#
# It is a second fuseki:query endpoint over the same dataset (config.ttl), which
# the liveness probe hits with ASK{} because /\$/ping is NOT sufficient: ActionPing
# returns a timestamp without touching the dataset, so it keeps returning 200 while
# every real query dies with "Maximum lock count exceeded" (Jena's shared read lock
# saturating in beginMultiMode) - exactly why that outage kept recurring
# undetected. A liveness probe has to run a REAL query to catch it.
#
# It used to be listed as anon, on the stated grounds that "Container Apps probes
# cannot send an Authorization header". THAT WAS FALSE - the probe takes a `header`
# block (infra/main.tf) - and the consequence was not theoretical: because Shiro
# matches only the servlet path + path-info (WebUtils.getPathWithinApplication) and
# never the query string, an anon /kg/health accepted ARBITRARY read-only SPARQL
# over the whole graph from anyone who could reach this app's PUBLIC ingress. The
# probe now authenticates like every other caller and the line is gone.
#
# Do not re-add it. If a future probe cannot carry credentials, the answer is a
# listener on a port that ingress does not publish, never an anon query endpoint:
# Shiro cannot filter by query string, so "anon" on a fuseki:query path always
# means "arbitrary read to the internet". The same trap applies to widening any
# rule to "/kg/sparql*" ('?' is an Ant single-char wildcard, so a query-string
# rule is dead config). Order matters here: first match wins.
/\$/ping = anon
/\$/**   = authcBasic
/**      = authcBasic
EOF
    chmod 600 "${FUSEKI_BASE}/shiro.ini"
    echo "fuseki: Basic auth ENABLED for user '${FUSEKI_USER}'"
else
    rm -f "${FUSEKI_BASE}/shiro.ini"
    echo "fuseki: WARNING - starting UNAUTHENTICATED (FUSEKI_USER/FUSEKI_PASSWORD unset)."
    echo "fuseki: WARNING - /kg/update accepts writes from anyone who can reach it."
fi

# Clear stale TDB2 lock left by an ungracefully-killed replica BEFORE the server
# opens the store. The TDB2 data lives on an Azure Files (SMB) share; in steady
# state Fuseki reads/writes it fine, but when a replica is killed without
# releasing its lock, the next replica finds a stale /fuseki/databases/kg/tdb.lock,
# fails to read the dead PID from it over SMB ("Failed to get a lock" /
# "Permission denied"), aborts dataset load, and returns 503 for every endpoint.
# This app is single-replica (min=max=1), so ANY tdb.lock present at boot is
# guaranteed stale — no other process holds it. Removing it here, before
# fuseki-server starts, lets TDB2 recreate a fresh lock and open the store
# cleanly. The lock is pure runtime coordination; the graph in Data-000N/ is
# untouched. Doing this at boot (not out-of-band) also avoids the SMB
# directory-cache race that an external delete against the live mount can hit.
find /fuseki/databases -name tdb.lock -type f -exec rm -f {} + 2>/dev/null || true

exec ./fuseki-server --port=3030 --config=/fuseki/config.ttl
