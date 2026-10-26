# Security status: historical archive

This repository is a **superseded public development snapshot**, not a
supported service or deployment template. The contemporary Open Source
Films editorial-retrieval work is maintained separately in
[opensourcefilms-erf](https://github.com/OpenSourceFilms/opensourcefilms-erf).

## Deployment warning

The original setup scripts used shared example passwords for PostgreSQL and
MinIO, an unauthenticated PostgreSQL fallback, fixed filesystem paths, and
service listeners whose network exposure depended on the host. These
assumptions are unsafe for a new installation.

To prevent accidental execution, the main historical bootstrap and startup
scripts now terminate immediately with **exit code 78** before running their
original bodies:

- `scripts/setup_infrastructure.sh`
- `scripts/setup_phase1.sh`
- `scripts/start_services.sh`
- `scripts/start_api.sh`

The bodies remain in the repository for engineering study. **Do not remove
the safety gate and use them as production installers.** No replacement
installation process is provided.

The application settings module also no longer embeds default database
and object-storage credentials. When an application imports that settings
class, its password-bearing fields must be explicitly supplied through
supported external configuration. This does not turn the old system into
a supported deployment.

## Other security limitations

This archive has not undergone a complete application security audit.
Other historical scripts, network defaults, authentication behaviour,
dependency versions, and licences may be unsuitable for present-day use.
Treat **all** execution scripts as untrusted reference material until
independently reviewed. Do not expose it to public traffic.

Historic git commits remain available and may show the old default values.
They were **common illustrative credentials, not evidence that a current
system uses them**. This update does not inspect, alter, rotate or revoke
any deployed credentials, and makes no claim about the configuration of
OSF's current private systems.

Only reuse selected ideas or independently reviewed code after supplying
current secure configuration, access controls, dependency audit and tests.
Do not treat the project's old filenames, API endpoints, or setup
instructions as evidence of a currently operated service.
