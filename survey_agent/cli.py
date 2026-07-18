"""CLI entry point for the survey agent."""

from __future__ import annotations

import asyncio
import logging
import os

import typer
from rich.console import Console
from rich.table import Table

from .config import DATA_DIR, load_config, save_config
from .llm_provider import PROVIDERS
from .profile.categories import CATEGORIES
from .profile.store import ProfileStore

app = typer.Typer(
    name="survey-agent",
    help="Agent-driven survey automation system",
    no_args_is_help=True,
)
console = Console()


# ── Profile commands ──

profile_app = typer.Typer(help="Manage user profile")
app.add_typer(profile_app, name="profile")


@profile_app.command("setup")
def profile_setup():
    """Interactive profile setup interview."""
    store = ProfileStore(DATA_DIR / "profile.json")
    console.print("[bold]Survey Agent — Profile Setup[/bold]\n")
    console.print("Build your profile so surveys can be answered consistently.\n")

    for cat in CATEGORIES:
        console.print(f"\n[bold cyan]{cat.label}[/bold cyan] — {cat.description}")
        for field in cat.fields:
            current = store.get(f"{cat.key}.{field.key}")
            current_str = f" (current: {current})" if current is not None else ""
            prompt = f"  {field.label}{current_str}: "
            if field.options:
                prompt += f"\n    Options: {', '.join(field.options)}\n    "
            answer = typer.prompt(prompt, default=str(current) if current is not None else "")
            if answer:
                val = answer
                if field.field_type == "number":
                    try:
                        val = float(answer) if "." in answer else int(answer)
                    except ValueError:
                        console.print("  [red]Invalid number[/red]")
                        continue
                elif field.field_type == "boolean":
                    val = answer.lower() in ("yes", "y", "true", "1")
                elif field.field_type == "multi_select":
                    val = [a.strip() for a in answer.split(",")]
                store.set(f"{cat.key}.{field.key}", val, source="interview")

    console.print(f"\n[green]Profile complete! ({store.completion_pct():.0f}% filled)[/green]")


@profile_app.command("show")
def profile_show():
    """Show current profile."""
    store = ProfileStore(DATA_DIR / "profile.json")
    summary = store.summary()
    if summary == "(empty profile)":
        console.print("[yellow]Empty. Run: survey-agent profile setup[/yellow]")
    else:
        console.print(summary)
    console.print(f"\nCompletion: {store.completion_pct():.0f}%")


@profile_app.command("update")
def profile_update(
    key: str = typer.Argument(..., help="e.g. demographics.age"),
    value: str = typer.Argument(..., help="New value"),
):
    """Update a specific profile field."""
    store = ProfileStore(DATA_DIR / "profile.json")
    store.correct(key, value)
    console.print(f"[green]Updated {key} = {value}[/green]")


# ── Account commands ──


@app.command("account")
def account_setup(  # noqa: PLR0913
    google_email: str = typer.Option(None, "--google", "-g", help="Gmail for OAuth logins"),
    paypal_email: str = typer.Option(None, "--paypal", "-p", help="PayPal for withdrawals"),
    provider: str = typer.Option(
        None, "--provider", help="LLM provider (gemini, groq, mistral, openai-compatible, cerebras)"
    ),
    model: str = typer.Option(None, "--model", "-m", help="LLM model name"),
    api_key: str = typer.Option(None, "--api-key", "-k", help="API key for the LLM provider"),
    base_url: str = typer.Option(None, "--base-url", help="Base URL (for openai-compatible providers)"),
):
    """Configure accounts and LLM provider."""
    config = load_config()
    changed = False

    if google_email:
        config.account.google_email = google_email
        console.print(f"[green]Google: {google_email}[/green]")
        changed = True
    if paypal_email:
        config.account.paypal_email = paypal_email
        console.print(f"[green]PayPal: {paypal_email}[/green]")
        changed = True
    if provider:
        if provider not in PROVIDERS:
            console.print(f"[red]Unknown provider: {provider}[/red]")
            console.print(f"Available: {', '.join(PROVIDERS.keys())}")
            raise typer.Exit(1)
        config.llm_provider = provider
        console.print(f"[green]Provider: {provider}[/green]")
        changed = True
    if model:
        config.llm_model = model
        console.print(f"[green]Model: {model}[/green]")
        changed = True
    if api_key:
        config.llm_api_key = api_key
        console.print(f"[green]API key: {'*' * 8}{api_key[-4:]}[/green]")
        changed = True
    if base_url:
        config.llm_base_url = base_url
        console.print(f"[green]Base URL: {base_url}[/green]")
        changed = True

    if not changed:
        p = config.llm_provider
        pc = PROVIDERS.get(p, {})
        raw_key = pc.get("env_var", "")
        env_key = raw_key if isinstance(raw_key, str) else ""
        has_env = bool(os.environ.get(env_key, ""))
        has_config = bool(config.llm_api_key)

        console.print("[bold]Current config:[/bold]")
        console.print(f"  Google:      {config.account.google_email or '(not set)'}")
        console.print(f"  PayPal:      {config.account.paypal_email or '(not set)'}")
        console.print(f"  Provider:    {p} ({pc.get('name', '?')})")
        console.print(f"  Model:       {config.llm_model or pc.get('default_model', '(default)')}")
        console.print(f"  API key:     {'set' if has_config else f'(set {env_key} env var or use --api-key)'}")
        console.print(f"  Env {env_key}: {'found' if has_env else 'not set'}")
        if config.llm_base_url:
            console.print(f"  Base URL:    {config.llm_base_url}")
        console.print(f"\nAvailable providers: {', '.join(PROVIDERS.keys())}")
    else:
        save_config(config)


# ── Run commands ──


@app.command("run")
def run_surveys(
    sites: list[str] = typer.Option([], "--site", "-s", help="Site slugs"),  # noqa: B008
    max_surveys: int = typer.Option(5, "--max", "-n", help="Max surveys"),
    headless: bool = typer.Option(False, "--headless/--no-headless"),
    backend: str = typer.Option(None, "--backend", "-b", help="Browser backend (browser-use, browser-harness)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    daemon: bool = typer.Option(False, "--daemon", "-d", help="Run indefinitely with pauses between cycles"),
):
    """Run the survey agent with dynamic browser control."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    config = load_config()
    if backend:
        config.browser_backend = backend

    # Resolve API key: config > env var
    pc = PROVIDERS.get(config.llm_provider, {})
    raw_key = pc.get("env_var", "")
    env_key = raw_key if isinstance(raw_key, str) else ""
    api_key = config.llm_api_key or os.environ.get(env_key, "")
    if not api_key:
        console.print(f"[red]No API key for {config.llm_provider}[/red]")
        console.print(f"Set with: survey-agent account --provider {config.llm_provider} --api-key YOUR_KEY")
        console.print(f"Or export: export {env_key}=your_key")
        raise typer.Exit(1)

    # Set env var for langchain
    if env_key and api_key:
        os.environ[env_key] = api_key

    if not config.account.google_email:
        console.print("[yellow]Warning: Google email not set[/yellow]")

    from .orchestrator import SessionOrchestrator  # noqa: PLC0415

    orchestrator = SessionOrchestrator(max_sessions=1, config=config, headless=headless)
    orchestrator.set_profile(str(DATA_DIR / "profile.json"))

    site_list = sites if sites else ["branded-surveys"]

    console.print("[bold]Survey Agent[/bold]")
    console.print(f"  Provider: {config.llm_provider} ({pc.get('name', '')})")
    console.print(f"  Model: {config.llm_model or pc.get('default_model', '')}")
    console.print(f"  Sites: {', '.join(site_list)}")
    console.print(f"  Backend: {config.browser_backend}")
    console.print(f"  Browser: {'headless' if headless else 'headed'}")
    console.print()

    if config.browser_backend == "browser-harness":
        console.print("[dim]Using your real Chrome browser. Make sure Chrome is open.[/dim]\n")
    elif not headless:
        console.print("[dim]Browser window will open. First run: complete Google sign-in if prompted.[/dim]\n")

    if daemon:
        console.print("[bold yellow]Daemon mode[/bold yellow]")
        console.print("[dim]Running indefinitely with 15-45 min pauses between cycles.[/dim]")
        console.print("[dim]Press Ctrl+C to stop after current cycle.[/dim]\n")
        asyncio.run(orchestrator.run_daemon(sites=site_list, max_surveys=max_surveys))
    else:
        results = asyncio.run(orchestrator.run(sites=site_list, max_surveys=max_surveys))

        for _i, result in enumerate(results):
            if "error" in result:
                console.print(f"[red]Error: {result['error']}[/red]")
            else:
                stats = result.get("stats", {})
                console.print(
                    f"[green]Done: {stats.get('surveys_completed', 0)} surveys, "
                    f"{stats.get('questions_answered', 0)} questions, "
                    f"{stats.get('errors', 0)} errors[/green]"
                )


@app.command("credentials")
def store_credentials(
    site: str = typer.Argument(help="Site slug (e.g. branded-surveys)"),
    email: str = typer.Option(..., "--email", "-e", help="Login email"),
    password: str = typer.Option(..., "--password", "-pw", help="Login password"),
):
    """Store site login credentials in macOS Keychain."""
    from .credentials import CredentialStore  # noqa: PLC0415

    store = CredentialStore()
    store.store_with_tracking(site, email, password)
    console.print(f"[green]Stored credentials for {site} ({email})[/green]")
    console.print("[dim]Credentials are saved in macOS Keychain (survey-agent/{site})[/dim]")


@app.command("login")
def login_site(
    sites: list[str] = typer.Option(["branded-surveys"], "--site", "-s", help="Site slugs"),  # noqa: B008
):
    """Open browser for manual Google sign-in. Run this FIRST before 'run'."""
    from .browser.playwright_engine import PlaywrightEngine  # noqa: PLC0415
    from .orchestrator import SITES  # noqa: PLC0415

    async def _login() -> None:
        engine = PlaywrightEngine(persistent=True)
        await engine.launch(headless=False)

        for site_slug in sites:
            if site_slug not in SITES:
                console.print(f"[red]Unknown site: {site_slug}[/red]")
                continue

            site = SITES[site_slug]
            console.print(f"\n[bold]Logging into {site['name']}[/bold]")
            console.print(f"  URL: {site['url']}")
            console.print()
            console.print("[yellow]1. A browser window will open[/yellow]")
            console.print("[yellow]2. Complete the Google sign-in manually[/yellow]")
            console.print("[yellow]3. Make sure you see the dashboard[/yellow]")
            console.print("[yellow]4. Come back here and press Enter[/yellow]")
            console.print()

            ctx = await engine.create_context()
            page_wrapper = await ctx.new_page()
            await page_wrapper.goto(site["url"])

            input("[dim]Press Enter here after you've logged in...[/dim]")
            console.print(f"[green]Session saved for {site['name']}[/green]")

        await engine.close()
        console.print("\n[green]Done! You can now run: survey-agent run[/green]")

    asyncio.run(_login())


email_app = typer.Typer(help="Email integration (Gmail IMAP for 2FA codes)")
app.add_typer(email_app, name="email")


@email_app.command("setup")
def email_setup(
    email: str = typer.Option(..., "--email", "-e", help="Gmail address"),
    password: str = typer.Option(..., "--password", "-p", help="Gmail App Password (NOT your regular password)"),
):
    """Store Gmail App Password for IMAP access (used for 2FA verification codes).

    Setup steps:
    1. Enable 2-Step Verification: https://myaccount.google.com/security
    2. Create App Password: https://myaccount.google.com/apppasswords
    3. Select 'Mail' and your device, then 'Generate'
    4. Run: survey-agent email setup --email you@gmail.com --password <16-char-app-password>

    The agent will use this to read verification codes from sites that require
    email-based 2FA (e.g., YouGov, Prime Opinion).
    """
    from .email.verification import store_gmail_credentials  # noqa: PLC0415

    store_gmail_credentials(email, password)
    console.print(f"[green]Stored Gmail credentials for {email}[/green]")
    console.print("[dim]Saved in macOS Keychain (survey-agent/gmail)[/dim]")
    console.print()
    console.print("[yellow]Test the connection with: survey-agent email test[/yellow]")


@email_app.command("test")
def email_test(
    sender: str = typer.Option(None, "--from", "-f", help="Filter by sender"),
    timeout: int = typer.Option(30, "--timeout", "-t", help="Seconds to wait for new email"),
):
    """Test Gmail connection and look for a recent verification code.

    Useful for verifying that the app password works.
    """
    from .email.verification import (  # noqa: PLC0415
        GmailVerificationReader,
        get_gmail_credentials,
    )

    creds = get_gmail_credentials()
    if not creds:
        console.print("[red]No Gmail credentials configured. Run: survey-agent email setup[/red]")
        raise typer.Exit(1)
    email_addr, app_password = creds
    console.print(f"[cyan]Connecting to Gmail as {email_addr}...[/cyan]")

    try:
        with GmailVerificationReader(email_addr, app_password) as reader:
            console.print("[green]Connected![/green]")
            console.print(f"[dim]Watching for verification codes (timeout: {timeout}s)...[/dim]")
            code = reader.find_verification_code(
                sender_pattern=sender,
                timeout=timeout,
                poll_interval=5,
            )
            if code:
                console.print(f"[green]Found verification code: {code}[/green]")
            else:
                console.print(f"[yellow]No verification code found within {timeout}s[/yellow]")
                console.print("[dim]Send yourself a test verification email and try again[/dim]")
    except Exception as e:
        console.print(f"[red]Gmail error: {e}[/red]")
        console.print("[dim]Verify your app password at https://myaccount.google.com/apppasswords[/dim]")


@email_app.command("poll")
def email_poll(
    sender: str = typer.Option(None, "--from", "-f", help="Filter by sender (substring match)"),
    subject: str = typer.Option(None, "--subject", "-s", help="Regex to match in subject"),
    timeout: int = typer.Option(60, "--timeout", "-t", help="Seconds to wait"),
):
    """Poll for a verification code (blocking). Used internally by the agent."""
    from .email.verification import (  # noqa: PLC0415
        GmailVerificationReader,
        get_gmail_credentials,
    )

    creds = get_gmail_credentials()
    if not creds:
        console.print("[red]No Gmail credentials configured[/red]")
        raise typer.Exit(1)
    email_addr, app_password = creds

    with GmailVerificationReader(email_addr, app_password) as reader:
        code = reader.find_verification_code(
            sender_pattern=sender,
            subject_pattern=subject,
            timeout=timeout,
            poll_interval=5,
        )
        if code:
            print(code)
        else:
            raise typer.Exit(1)


@app.command("providers")
def list_providers():
    """List available LLM providers."""
    table = Table(title="Available LLM Providers")
    table.add_column("Provider", style="cyan")
    table.add_column("Name")
    table.add_column("Default Model")
    table.add_column("Env Var")
    table.add_column("Status")

    for key, p in PROVIDERS.items():
        env_val = os.environ.get(p["env_var"], "")
        status = "[green]configured[/green]" if env_val else "[dim]not set[/dim]"
        table.add_row(key, p["name"], p["default_model"], p["env_var"], status)

    console.print(table)


# ── Brain commands ──

brain_app = typer.Typer(help="Brain foundation management")
app.add_typer(brain_app, name="brain")


@brain_app.command("status")
def brain_status(last: int = typer.Option(5, "--last", "-n", help="Show last N digest runs")):
    """Show recent digest runs and brain state."""
    import json as _json  # noqa: PLC0415

    brain_dir = DATA_DIR / "brain"
    if not brain_dir.exists():
        console.print("[dim]Brain not initialized.[/dim]")
        raise typer.Exit(code=0)

    from .brain.store import FailureStore  # noqa: PLC0415

    store = FailureStore(brain_dir)
    pending = store.pending_promotions()
    console.print(f"[bold]Pending promotions:[/bold] {len(pending)}")
    for p in pending:
        console.print(f"  - {p.site} / {p.event_type} (count={p.count})")

    runs_log = brain_dir / "digest_runs.jsonl"
    if runs_log.exists():
        console.print(f"\n[bold]Last {last} digest runs:[/bold]")
        lines = runs_log.read_text().splitlines()
        for line in lines[-last:]:
            try:
                entry = _json.loads(line)
                status_color = (
                    "green"
                    if entry.get("status") == "ok"
                    else "yellow"
                    if entry.get("status") == "skipped_no_signal"
                    else "red"
                )
                console.print(
                    f"  {entry.get('started_at', '?')} | "
                    f"{entry.get('trigger', '?'):>10} | "
                    f"[{status_color}]{entry.get('status', '?')}[/{status_color}] | "
                    f"rules+={entry.get('rules_added', 0)} "
                    f"persona~={entry.get('persona_fields_changed', 0)}"
                )
            except Exception:
                pass
    else:
        console.print("[dim]No digest runs yet.[/dim]")


@brain_app.command("pause")
def brain_pause():
    """Stop auto-triggers (writes data/brain/paused marker)."""
    from .brain._lock import atomic_write_bytes  # noqa: PLC0415

    brain_dir = DATA_DIR / "brain"
    brain_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(brain_dir / "paused", b"1")
    console.print("[yellow]Brain paused.[/yellow]")


@brain_app.command("resume")
def brain_resume():
    """Re-enable auto-triggers."""
    p = DATA_DIR / "brain" / "paused"
    if p.exists():
        p.unlink()
    console.print("[green]Brain resumed.[/green]")


@brain_app.command("inspect")
def brain_inspect(run_id: str = typer.Argument(help="Digest run ID")):
    """Show the diff + rationale for one digest run."""
    diff_path = DATA_DIR / "brain" / "diffs" / f"{run_id}.json"
    if not diff_path.exists():
        console.print(f"[red]Run {run_id} not found.[/red]")
        raise typer.Exit(code=1)
    console.print(diff_path.read_text())


@brain_app.command("rollback")
def brain_rollback(
    run_id: str = typer.Argument(help="Digest run ID to rollback to"),
):
    """Revert profile.json + rules.yaml + persona_hints.json to a specific run."""
    import json as _json  # noqa: PLC0415

    from .brain._lock import atomic_write_bytes  # noqa: PLC0415

    diff_path = DATA_DIR / "brain" / "diffs" / f"{run_id}.json"
    if not diff_path.exists():
        console.print(f"[red]Run {run_id} not found.[/red]")
        raise typer.Exit(code=1)

    payload = _json.loads(diff_path.read_text())
    before = payload["before"]
    atomic_write_bytes(DATA_DIR / "profile.json", before["profile.json"].encode("utf-8"))
    atomic_write_bytes(DATA_DIR / "brain" / "rules.yaml", before["rules.yaml"].encode("utf-8"))
    atomic_write_bytes(DATA_DIR / "brain" / "persona_hints.json", before["persona_hints.json"].encode("utf-8"))
    console.print(f"[green]Rolled back to {run_id}.[/green]")


if __name__ == "__main__":
    app()
