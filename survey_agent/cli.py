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
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Run the survey agent with dynamic browser control."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config()

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
    console.print(f"  Browser: {'headless' if headless else 'headed'}")
    console.print()

    if not headless:
        console.print("[dim]Browser window will open. First run: complete Google sign-in if prompted.[/dim]\n")

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


if __name__ == "__main__":
    app()
