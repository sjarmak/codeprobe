"""CLI commands for managing authentication with external services.

Provides ``codeprobe auth sourcegraph`` (PAT prompt + cache),
``codeprobe auth status``, and ``codeprobe auth logout``.

ZFC compliant: pure IO (prompt, file read/write). No semantic judgment.
"""

from __future__ import annotations

import click


@click.group()
def auth() -> None:
    """Manage authentication for external services."""


@auth.command()
@click.option(
    "--endpoint",
    default="https://sourcegraph.com",
    help="Sourcegraph instance URL.",
)
def sourcegraph(endpoint: str) -> None:
    """Authenticate with Sourcegraph (paste a Personal Access Token).

    Creates a PAT cache at ~/.codeprobe/auth.json so subsequent
    commands can authenticate without prompting or requiring
    SRC_ACCESS_TOKEN in the environment.
    """
    from codeprobe.mining.sg_auth import CachedToken, save_cached_token

    pat = click.prompt(
        "Paste your Sourcegraph Personal Access Token",
        hide_input=True,
    )
    pat = pat.strip()
    if not pat:
        click.echo("Error: empty token", err=True)
        raise SystemExit(1)

    token = CachedToken(
        access_token=pat,
        refresh_token=None,
        expires_at=None,
        endpoint=endpoint,
    )
    save_cached_token(token)
    click.echo(f"Authenticated with {endpoint}")
    click.echo("  Token cached at ~/.codeprobe/auth.json")


@auth.command("logout")
@click.option(
    "--service",
    default="sourcegraph",
    help="Service to clear auth for.",
)
def logout(service: str) -> None:
    """Remove cached auth for a service."""
    from codeprobe.mining.sg_auth import clear_cached_token

    clear_cached_token(service=service)
    click.echo(f"Cleared {service} auth cache")


@auth.command("status")
def status() -> None:
    """Show cached auth status."""
    from codeprobe.mining.sg_auth import load_cached_token

    token = load_cached_token()
    if token is None:
        click.echo("Not authenticated. Run `codeprobe auth sourcegraph`.")
        return
    click.echo(f"Sourcegraph: {token.endpoint}")
    if token.expires_at is not None:
        click.echo(f"Expires: {token.expires_at.isoformat()}")
    else:
        click.echo("Expires: unknown (PAT, non-expiring)")
