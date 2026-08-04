"""Login command body: one-time Telegram session authentication."""
from __future__ import annotations

import click
from rich.console import Console

from .client import make_client

console = Console()


async def login_run(session, phone, password):
    """Authenticate the Telegram session (one-time).

    Telegram sends a login code to your phone/other client; enter it when prompted.
    If your account has 2FA enabled you'll also be asked for the cloud password. The
    authorized session is saved to the data dir so every other tgf command reuses it.
    """
    client = make_client(session)
    effective_phone = phone
    effective_password = password
    if not effective_phone:
        effective_phone = click.prompt("📱 Phone number (e.g. +1234567890)", default="", type=str).strip()
    if not effective_phone:
        console.print("[red]A phone number is required to log in.[/red]")
        raise SystemExit(1)
    try:
        try:
            await client.start(phone=effective_phone, password=effective_password or None)
        except Exception as e:
            msg = str(e).lower()
            if "password" in msg or "2fa" in msg or "two-step" in msg:
                pw = click.prompt("🔒 Cloud password (2FA)", default="", type=str, hide_input=True).strip()
                if pw:
                    await client.start(phone=effective_phone, password=pw)
                else:
                    raise
            elif "api_id_invalid" in msg or ("api_id" in msg and "invalid" in msg):
                # Creds exist but Telegram rejects them — almost always expired/revoked
                # values from my.telegram.org. This is the real blocker behind the
                # "phone code" wall.
                console.print(
                    "[red]❌ Telegram rejected your TELEGRAM_API_ID / TELEGRAM_API_HASH.[/red]\n"
                    "   They are present but INVALID. Generate fresh ones at:\n"
                    "   https://my.telegram.org → API development tools → create app\n"
                    "   then update them in your .env file."
                )
                raise SystemExit(1)
            else:
                raise
        me = await client.get_me()
        console.print(
            f"[bold green]✅ Logged in as[/bold green] "
            f"{getattr(me, 'username', None) or getattr(me, 'first_name', None)} "
            f"(id={me.id})"
        )
        console.print(f"[dim]session saved at: {client.session.filename}[/dim]")
    except Exception as e:
        console.print(f"[red]❌ Login failed:[/red] {type(e).__name__}: {e}")
        raise SystemExit(1)
    finally:
        await client.disconnect()
