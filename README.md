[![CI](https://github.com/epics-containers/ec-nightly/actions/workflows/ci.yml/badge.svg)](https://github.com/epics-containers/ec-nightly/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

# ec-nightly

A small, generic container image that runs nightly **read-only** smoke checks
against a [blueapi](https://github.com/DiamondLightSource/blueapi) instance,
intended to run in-cluster as a Kubernetes CronJob.

Image: `ghcr.io/epics-containers/ec-nightly:<tag>`

Beamline specifics are data supplied through environment variables (e.g. from
Helm values), never code.

## Usage

```
ec-nightly --version
ec-nightly list                 # describe the available checks
ec-nightly run                  # run every check, in order
ec-nightly run health plans     # run selected checks
```

Exit code is 0 when no check failed, 1 when any check failed and 2 for a usage
or configuration error. Results are logged to stdout and, when `REPORT_DIR` is
set, written to `REPORT_DIR/<UTC timestamp>/<JOB_NAME>/report.json` and
`report.xml` (JUnit). The token and client secret are never logged or reported.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `BLUEAPI_URL` | (required) | blueapi base URL, e.g. `http://p47-blueapi.p47-beamline.svc.cluster.local:80` |
| `TOKEN_URL` | `https://identity.diamond.ac.uk/realms/dls/protocol/openid-connect/token` | OAuth2 token endpoint |
| `CLIENT_ID` | empty | OAuth2 client id (client_credentials grant) |
| `CLIENT_SECRET` | empty | OAuth2 client secret, sent as HTTP Basic (`client_secret_basic`); supply from a Kubernetes Secret |
| `INSTRUMENT` | empty | instrument name, recorded in reports |
| `JOB_NAME` | `local` | job name, used in the report path |
| `REPORT_DIR` | empty (no files) | directory to write reports into; must be writable |
| `EXPECTED_PLANS` | empty | comma separated plan names that must be present |
| `EXPECTED_DEVICES` | empty | comma separated device names that must be present |
| `ALLOWED_WORKER_STATES` | `IDLE,RUNNING,PAUSED,PAUSING` | worker states considered healthy |
| `HTTP_TIMEOUT` | `30` | per-request timeout in seconds |

With neither `CLIENT_ID` nor `CLIENT_SECRET` set, the `token` check is skipped
and blueapi is called without a bearer token (for blueapi without OIDC).

## Checks

All requests to blueapi are `GET`s.

| Check | Request | Passes when |
|---|---|---|
| `token` | `POST TOKEN_URL` (client_credentials) | an access token is returned |
| `health` | `GET /healthz` (no auth) | `{"status": "ok"}` |
| `environment` | `GET /api/v1/environment` (bearer) | authorised, `initialized` and no `error_message` |
| `plans` | `GET /api/v1/plans` (bearer) | non-empty and contains all `EXPECTED_PLANS` |
| `devices` | `GET /api/v1/devices` (bearer) | non-empty and contains all `EXPECTED_DEVICES` |
| `worker` | `GET /api/v1/worker/state` (bearer) | state is in `ALLOWED_WORKER_STATES` |

The image runs as uid 65534 so it suits `runAsNonRoot`.

## Development

Created from the [DLS python-copier-template](https://github.com/DiamondLightSource/python-copier-template).

```
uv sync
uv run tox -p
```
