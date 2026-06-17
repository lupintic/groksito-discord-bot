#!/usr/bin/env python3
"""
Groksito - Configuración Segura Interactiva (estilo Hermes Agent)

Ejecuta esto para crear o reparar tu archivo .env de forma profesional y segura.

Uso:
    python scripts/configure_env.py

Características:
- 100% seguro de ejecutar muchas veces (idempotente).
- Si no existe .env → copia .env.example como base (estructura + comentarios bonitos).
- Modo "Actualización Segura": respeta todo lo existente, solo pide lo que falta.
- Nunca duplica claves (usa el escritor unificado de env_utils).
- Maneja ALLOWED_GUILD_IDS correctamente como array JSON.
- Puede lanzar el flujo de login OAuth directamente desde aquí.
- Siempre hace backup antes de cualquier cambio.
- Ofrece "Limpiar Duplicados" y "Empezar de Cero" con confirmaciones fuertes.

Este es el método recomendado (y final) para configurar el bot.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Try rich for nice UX (same as the bot). Graceful fallback to plain input/print.
try:
    from rich.console import Console
    from rich.prompt import Prompt, Confirm
    from rich.panel import Panel
    RICH = True
    console = Console()
except Exception:
    RICH = False
    console = None

ENV_FILE = Path(".env")

# =============================================================================
# Unified safe .env logic (single source of truth)
# We prefer to import the shared implementation from src/groksito_discord/env_utils.
# If that fails (e.g. running configure_env.py before the package is installed),
# we fall back to a minimal local copy so the script remains an "always works" recovery tool.
# =============================================================================

def _import_shared_env_utils():
    """Try to pull the real implementations from the package."""
    try:
        # Make src importable (same pattern the web dashboard uses)
        src_dir = Path(__file__).resolve().parent.parent / "src"
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
        from groksito_discord.utils.env_utils import (
            safe_write_env as _safe_write,
            parse_env_file as _parse_file,
            parse_env_lines as _parse_lines,
            backup_env as _backup,
            deduplicate_env_file as _dedup,
            _format_env_value as _fmt_val,
            _format_list_for_display as _fmt_list,
            _get_ci as _get_ci,
            create_fresh_env_from_template as _fresh,
            CRITICAL_KEYS as _CRIT,
            PROTECTED_KEYS as _PROT,
        )
        return {
            "safe_write_env": _safe_write,
            "parse_env_file": _parse_file,
            "parse_env_lines": _parse_lines,
            "backup_env": _backup,
            "deduplicate_env_file": _dedup,
            "_format_env_value": _fmt_val,
            "_format_list_for_display": _fmt_list,
            "_get_ci": _get_ci,
            "create_fresh_env_from_template": _fresh,
            "CRITICAL_KEYS": _CRIT,
            "PROTECTED_KEYS": _PROT,
        }
    except Exception:
        return None

_SHARED = _import_shared_env_utils()

if _SHARED:
    safe_write_env = _SHARED["safe_write_env"]
    parse_env_file = _SHARED["parse_env_file"]
    parse_env_lines = _SHARED["parse_env_lines"]
    backup_env = _SHARED["backup_env"]
    deduplicate_env_file = _SHARED["deduplicate_env_file"]
    _format_env_value = _SHARED["_format_env_value"]
    _format_list_for_display = _SHARED["_format_list_for_display"]
    _get_ci = _SHARED["_get_ci"]
    create_fresh_env_from_template = _SHARED["create_fresh_env_from_template"]
    CRITICAL_KEYS = _SHARED["CRITICAL_KEYS"]
    PROTECTED_KEYS = _SHARED["PROTECTED_KEYS"]
else:
    # --- Minimal fallback implementations (kept in sync with env_utils.py) ---
    # Only used if the import above completely fails. Keeps configure_env.py usable
    # as a last-resort recovery tool even in broken checkouts.
    import re
    import shutil
    from datetime import datetime

    ENV_LINE_RE = re.compile(
        r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s#]*))?\s*(?:#\s*(.*))?$',
        re.IGNORECASE,
    )
    CRITICAL_KEYS = {"DISCORD_BOT_TOKEN", "XAI_API_KEY"}
    PROTECTED_KEYS = CRITICAL_KEYS | {"GROK_AUTH_MODE", "GROK_OAUTH_PORT", "GROK_OAUTH_TOKEN_FILE"}

    def _format_env_value(val: Any) -> str:
        if isinstance(val, (list, tuple)):
            return json.dumps(val, separators=(",", ":"))
        if val is None or str(val).strip() == "":
            return '""'
        val = str(val)
        if re.match(r"^[A-Za-z0-9_./:@%+=-]+$", val) and not val[0] in ("-", "+", "="):
            return val
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _format_list_for_display(val: Any) -> str:
        if val is None:
            return ""
        if isinstance(val, (list, tuple)):
            return ",".join(str(x) for x in val)
        if isinstance(val, str):
            v = val.strip()
            if not v:
                return ""
            if v.startswith("[") and v.endswith("]"):
                try:
                    lst = json.loads(v)
                    if isinstance(lst, (list, tuple)):
                        return ",".join(str(x) for x in lst)
                except Exception:
                    pass
            try:
                parts = [p.strip().strip('"').strip("'") for p in v.split(",") if p.strip()]
                return ",".join(p for p in parts if p)
            except Exception:
                return v
        return str(val)

    def _get_ci(d: dict[str, str], key: str, default: str = "") -> str:
        if not key:
            return default
        klower = key.lower()
        for dk, dv in d.items():
            if dk.lower() == klower:
                return dv
        return os.getenv(key) or os.getenv(key.upper()) or os.getenv(key.lower()) or default

    def parse_env_file(path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        if not path.exists():
            return values
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                m = ENV_LINE_RE.match(line)
                if m:
                    key = m.group(1)
                    val = m.group(2) or m.group(3) or m.group(4) or ""
                    values[key] = val
        except Exception:
            pass
        return values

    def parse_env_lines(path: Path) -> list[str]:
        if not path.exists():
            return []
        try:
            return path.read_text(encoding="utf-8").splitlines(keepends=True)
        except Exception:
            return []

    def backup_env(path: Path) -> Path | None:
        if not path.exists():
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            ts_bak = path.with_name(f"{path.name}.backup-{ts}")
            latest = path.with_name(f"{path.name}.backup")
            shutil.copy2(path, ts_bak)
            shutil.copy2(path, latest)
            return ts_bak
        except Exception:
            return None

    def deduplicate_env_file(path: Path, *, keep: str = "last", make_backup: bool = True) -> bool:
        if not path.exists():
            return False
        lines = parse_env_lines(path)
        changed = False
        kept_lines: list[str] = []
        if keep == "last":
            last_idx: dict[str, int] = {}
            for i, line in enumerate(lines):
                m = ENV_LINE_RE.match(line)
                if m:
                    last_idx[m.group(1).lower().strip()] = i
            for i, line in enumerate(lines):
                m = ENV_LINE_RE.match(line)
                if m:
                    lk = m.group(1).lower().strip()
                    if i != last_idx[lk]:
                        changed = True
                        continue
                kept_lines.append(line)
        else:
            seen: set[str] = set()
            for line in lines:
                m = ENV_LINE_RE.match(line)
                if m:
                    lk = m.group(1).lower().strip()
                    if lk in seen:
                        changed = True
                        continue
                    seen.add(lk)
                kept_lines.append(line)
        if changed:
            if make_backup:
                backup_env(path)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text("".join(kept_lines), encoding="utf-8")
            os.replace(tmp, path)
        return changed

    def safe_write_env(path: Path, updates: dict[str, Any], *, force_backup: bool = True) -> tuple[bool, str, Path | None]:
        backup_path = None
        if path.exists() and force_backup:
            backup_path = backup_env(path)
        pre_values = parse_env_file(path) if path.exists() else {}
        lines = parse_env_lines(path)
        update_map: dict[str, Any] = {}
        caller_casing: dict[str, str] = {}
        for k, v in updates.items():
            lk = k.lower().strip()
            if lk and lk not in update_map:
                update_map[lk] = v
                caller_casing[lk] = k
        new_lines: list[str] = []
        updated_lowers: set[str] = set()
        for line in lines:
            m = ENV_LINE_RE.match(line)
            if m:
                orig_key = m.group(1)
                lower = orig_key.lower().strip()
                if lower in update_map:
                    if lower not in updated_lowers:
                        val = update_map[lower]
                        formatted = _format_env_value(val)
                        comment = m.group(5) or ""
                        new_line = f"{orig_key}={formatted}"
                        if comment:
                            new_line += f"  # {comment}"
                        new_line += "\n"
                        new_lines.append(new_line)
                        updated_lowers.add(lower)
                    continue
            new_lines.append(line)
        for lower_key, val in update_map.items():
            if lower_key not in updated_lowers:
                key_to_use = caller_casing.get(lower_key, lower_key)
                formatted = _format_env_value(val)
                new_lines.append(f"{key_to_use}={formatted}\n")
        try:
            if not path.parent.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text("".join(new_lines), encoding="utf-8")
            try:
                with open(tmp, "rb") as f:
                    os.fsync(f.fileno())
            except Exception:
                pass
            os.replace(tmp, path)
        except Exception as e:
            if backup_path and backup_path.exists():
                try:
                    shutil.copy2(backup_path, path)
                except Exception:
                    pass
            return False, f"Write failed: {e}. Backup restored if possible.", backup_path
        post = parse_env_file(path)
        for ck in CRITICAL_KEYS:
            if _get_ci(pre_values, ck) and not _get_ci(post, ck):
                if backup_path and backup_path.exists():
                    shutil.copy2(backup_path, path)
                return False, f"SAFETY: {ck} disappeared after write. File restored from backup.", backup_path
        return True, "", backup_path

    def create_fresh_env_from_template(target: Path, template_path: Path | None = None, overrides: dict[str, Any] | None = None) -> tuple[bool, str, Path | None]:
        """
        Fallback version (cuando no se pudo importar el módulo compartido).
        Intenta copiar .env.example para tener la estructura bonita.
        """
        backup_path = backup_env(target) if target.exists() else None

        # Intentar cargar el template real aunque estemos en fallback
        template_lines: list[str] = []
        candidates = []
        if template_path:
            candidates.append(template_path)
        candidates.extend([Path(".env.example"), Path("env.example"), Path(".env.template")])

        for cand in candidates:
            if cand.exists():
                try:
                    template_lines = cand.read_text(encoding="utf-8").splitlines(keepends=True)
                    break
                except Exception:
                    pass

        if not template_lines:
            template_lines = [
                "# Groksito Discord Bot - Config (fallback mínimo)\n",
                "# Recomendado: copia .env.example manualmente o instala el paquete.\n\n",
                "DISCORD_BOT_TOKEN=\n",
                "XAI_API_KEY=\n\n",
                "GROK_AUTH_MODE=api_key\n",
                "ALLOWED_GUILD_IDS=\n",
            ]

        try:
            if not target.parent.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text("".join(template_lines), encoding="utf-8")
            os.replace(tmp, target)
        except Exception as e:
            if backup_path and backup_path.exists():
                try:
                    import shutil
                    shutil.copy2(backup_path, target)
                except Exception:
                    pass
            return False, f"Failed to write template: {e}", backup_path

        if overrides:
            return safe_write_env(target, overrides, force_backup=False)
        return True, "", backup_path

# --- UI helpers ---

def say(msg: str, style: str | None = None) -> None:
    if RICH:
        console.print(msg, style=style)
    else:
        print(msg)

def ask(prompt: str, default: str | None = None) -> str:
    if RICH:
        return Prompt.ask(prompt, default=default, show_default=True) or ""
    else:
        if default:
            p = f"{prompt} [{default}]: "
        else:
            p = f"{prompt}: "
        val = input(p).strip()
        return val or (default or "")

def confirm(prompt: str, default: bool = False) -> bool:
    if RICH:
        return Confirm.ask(prompt, default=default)
    else:
        d = "Y/n" if default else "y/N"
        val = input(f"{prompt} ({d}): ").strip().lower()
        if not val:
            return default
        return val in ("y", "yes")

def show_panel(title: str, content: str) -> None:
    if RICH:
        console.print(Panel(content, title=title, border_style="cyan"))
    else:
        print(f"\n=== {title} ===")
        print(content)
        print("=" * (len(title) + 8))


def _try_run_oauth_login() -> bool:
    """
    Intenta lanzar el flujo interactivo de OAuth directamente desde setup.
    Devuelve True si tuvo éxito.
    """
    say("\n--- Login OAuth (SuperGrok / X Premium+) ---")
    try:
        # Asegurar que podemos importar desde src
        src_dir = Path(__file__).resolve().parent.parent / "src"
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))

        from groksito_discord.core.grok_oauth import login_oauth_interactive
    except Exception as e:
        say(f"No se pudo cargar el módulo de OAuth: {e}", "red")
        say("Puedes ejecutarlo manualmente después:")
        say("    python -m groksito_discord --login-oauth")
        say("    (o con --no-browser / --print-url-only según tu caso)")
        return False

    say("Se abrirá el flujo de login en el navegador (o te dará instrucciones).")
    say("Esto es seguro y solo se hace una vez (guarda tokens en ./oauth/).")

    if not confirm("¿Quieres iniciar el login OAuth ahora?", default=True):
        say("Omitido. Puedes correrlo más tarde con el comando de arriba.")
        return False

    try:
        # En setup normalmente queremos que el usuario vea el navegador.
        # Si está en Docker el propio login_oauth_interactive ya fuerza no-browser.
        success = login_oauth_interactive(no_browser=False)
        if success:
            say("✅ Login OAuth completado con éxito. Los tokens se guardaron.", "bold green")
            return True
        else:
            say("El login OAuth no se completó (puede que hayas cancelado o haya habido un error).", "yellow")
            say("Puedes reintentarlo después con: python -m groksito_discord --login-oauth")
            return False
    except KeyboardInterrupt:
        say("\nLogin interrumpido por el usuario.", "yellow")
        return False
    except Exception as e:
        say(f"Error durante el login OAuth: {e}", "red")
        return False


def _bootstrap_from_template() -> bool:
    """
    Si no existe .env, copia .env.example (o un esqueleto mínimo) para tener
    estructura, secciones y comentarios bonitos desde el principio.
    Esto es clave para que el resultado final se vea profesional y no "plano".
    Usa el helper del escritor compartido cuando está disponible.
    """
    if ENV_FILE.exists():
        return True

    say("No se encontró .env. Creando uno limpio a partir de .env.example...", "green")

    try:
        ok, msg, bak = create_fresh_env_from_template(ENV_FILE)
        if ok:
            say("✅ .env creado a partir del template (estructura + comentarios preservados).", "green")
            return True
        else:
            say(f"No se pudo crear desde template: {msg}. Usando mínimo...", "yellow")
    except Exception as e:
        say(f"Error usando create_fresh: {e}. Intentando copia directa...", "yellow")

    # Fallback manual (copia directa del .env.example si existe)
    candidates = [Path(".env.example"), Path("env.example")]
    for cand in candidates:
        if cand.exists():
            try:
                backup_env(ENV_FILE) if ENV_FILE.exists() else None
                if not ENV_FILE.parent.exists():
                    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy2(cand, ENV_FILE)
                say(f"✅ Copiado {cand} → .env", "green")
                return True
            except Exception as ex:
                say(f"Falló copia directa: {ex}", "red")

    # Último recurso: esqueleto mínimo
    try:
        if not ENV_FILE.parent.exists():
            ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        skeleton = (
            "# Groksito Discord Bot - Configuración mínima generada\n"
            "# Ejecuta `python scripts/configure_env.py` de nuevo para completarla.\n\n"
            "DISCORD_BOT_TOKEN=\n"
            "XAI_API_KEY=\n"
            "GROK_AUTH_MODE=api_key\n"
            "ALLOWED_GUILD_IDS=\n"
        )
        ENV_FILE.write_text(skeleton, encoding="utf-8")
        say("Se creó un .env mínimo (sin template).", "yellow")
        return True
    except Exception as e:
        say(f"Error crítico creando .env: {e}", "red")
        return False


# --- The interactive flow ---

def main() -> None:
    say("🚀 Groksito - Configuración Segura Interactiva", "bold cyan")
    say("Esta herramienta es segura de ejecutar las veces que quieras.")
    say("Usa .env.example como base cuando no hay .env, nunca duplica claves,")
    say("y puede lanzar el login de OAuth directamente.\n")

    # 1. Si no hay .env, bootstrap desde el template ANTES de todo.
    had_env_before = ENV_FILE.exists()
    if not had_env_before:
        if not _bootstrap_from_template():
            say("No se pudo crear .env. Abortando.", "red")
            return

    existing = parse_env_file(ENV_FILE)
    had_env = bool(existing) or ENV_FILE.exists()

    if had_env:
        say(f"Encontrado {ENV_FILE} con {len(existing)} claves.", "yellow")
        show_panel(
            "Vista actual (segura)",
            "\n".join(
                f"  {k} = {'<configurado>' if v else '<vacío>'}"
                for k, v in list(existing.items())[:12]
            )
            + ("\n  ... (más claves)" if len(existing) > 12 else ""),
        )
    else:
        say("No se encontró .env. Se creará uno nuevo.", "green")

    # Modo de operación
    choice = "1"
    if had_env_before:  # Solo mostramos menú completo si ya existía algo antes
        say("\nOpciones (la 1 - Actualización Segura es la recomendada):")
        say("  1) Actualización Segura (por defecto): respeta TODO lo que ya existe.")
        say("     Solo pide los críticos que faltan y te permite cambiar ajustes seguros.")
        say("  2) Limpiar Duplicados solamente (elimina duplicados, se queda con el último valor, hace backup y sale).")
        say("  3) Revisar y cambiar valores específicos (con confirmaciones una por una).")
        say("  4) Empezar de CERO (hace backup, escribe .env limpio desde .env.example y vuelve a preguntar).")

        choice = ask("Elige 1/2/3/4", default="1").strip() or "1"

    updates: dict[str, Any] = {}
    backup_made: Path | None = None

    if choice == "2":
        say("\nEjecutando limpieza de duplicados (se queda con la última ocurrencia de cada clave)...")
        changed = deduplicate_env_file(ENV_FILE, keep="last", make_backup=True)
        if changed:
            say("✅ Duplicados eliminados. Se crearon backups (.env.backup y con timestamp).", "green")
            say("   Vuelve a correr `python scripts/configure_env.py` si también quieres rellenar valores que faltan.")
        else:
            say("No se encontraron claves duplicadas. Tu .env ya está limpio.")
        say("\nListo.")
        return

    if choice == "4":
        if had_env:
            if not confirm("Esto hará BACKUP de tu .env actual y creará uno completamente nuevo desde el template limpio. ¿Continuar?", default=False):
                say("Abortado.", "red")
                return
            confirm_word = ask("Escribe la palabra CERO en mayúsculas para confirmar el reinicio total")
            if confirm_word != "CERO":
                say("La palabra de confirmación no coincide. Abortado.", "red")
                return
            backup_made = backup_env(ENV_FILE)
            say(f"Backup realizado: {backup_made}", "green")
        else:
            say("Creando .env fresco desde el template limpio...")

        ok, msg, bak = create_fresh_env_from_template(ENV_FILE, overrides=None)
        if bak:
            backup_made = bak
        if not ok:
            say(f"No se pudo escribir el template limpio: {msg}", "red")
            return

        say("Template limpio escrito (con comentarios y secciones). Releyendo...")
        existing = parse_env_file(ENV_FILE)
        choice = "1"  # continuamos como actualización segura sobre el template bonito

    # ============================================================
    # RECOLECCIÓN DE VALORES (siempre segura: solo lo que el usuario confirma)
    # ============================================================

    # 1. Discord (crítico)
    current_disc = _get_ci(existing, "DISCORD_BOT_TOKEN")
    if current_disc:
        say(f"\nDiscord Bot Token: ya está configurado (longitud ~{len(current_disc)}).")
        if confirm("¿Quieres cambiar el Discord Bot Token?", default=False):
            new = ask("Pega el nuevo DISCORD_BOT_TOKEN")
            if new:
                updates["DISCORD_BOT_TOKEN"] = new.strip()
    else:
        say("\n¡DISCORD_BOT_TOKEN es REQUERIDO!")
        token = ask("Pega tu Discord Bot Token (de https://discord.com/developers/applications)")
        if token:
            updates["DISCORD_BOT_TOKEN"] = token.strip()

    # 2. Autenticación
    current_mode = _get_ci(existing, "GROK_AUTH_MODE") or "api_key"
    current_xai = _get_ci(existing, "XAI_API_KEY")

    say("\n--- Autenticación ---")
    say("Puedes usar una XAI_API_KEY clásica (estable) o el flujo experimental de OAuth (SuperGrok / X Premium+).")
    say("Recomendado: pon una key, o usa GROK_AUTH_MODE=auto + haz login una vez.")

    if current_mode or current_xai:
        say(f"Modo actual: {current_mode}")
        if current_xai:
            say("XAI_API_KEY presente.")
        if confirm("¿Cambiar configuración de autenticación?", default=False):
            pass  # seguimos para preguntar
        else:
            if current_mode and current_mode.lower() not in ("", "none"):
                updates["GROK_AUTH_MODE"] = current_mode
            # No tocamos el valor de la key secreta a menos que el usuario lo pida

    auth_choice = ask(
        "Método de auth: 1=XAI_API_KEY (estable y recomendado)  2=OAuth (sin key, experimental)  3=auto (prefiere OAuth si hay token) [1]",
        default="1",
    ).strip()

    will_need_oauth = False

    if auth_choice in ("1", ""):
        if not current_xai or confirm("¿Reemplazar XAI_API_KEY?", default=False):
            key = ask("Pega tu XAI_API_KEY (de https://console.x.ai)")
            if key:
                updates["XAI_API_KEY"] = key.strip()
        updates["GROK_AUTH_MODE"] = "api_key"
    elif auth_choice == "2":
        updates["GROK_AUTH_MODE"] = "oauth"
        will_need_oauth = True
        say("OAuth seleccionado. Al final del setup te ofreceré lanzar el login directamente.")
    else:
        updates["GROK_AUTH_MODE"] = "auto"
        will_need_oauth = True
        if not current_xai or confirm("¿También poner/actualizar una XAI_API_KEY de respaldo (muy recomendado)?", default=True):
            key = ask("Pega XAI_API_KEY de respaldo (o deja vacío)")
            if key:
                updates["XAI_API_KEY"] = key.strip()
        say("GROK_AUTH_MODE=auto. Te recomiendo hacer --login-oauth una vez.")

    # 3. Allowed Guilds (seguridad) - se escribe como JSON array
    current_guilds_raw = _get_ci(existing, "ALLOWED_GUILD_IDS") or _get_ci(existing, "allowed_guild_ids")
    current_guilds = _format_list_for_display(current_guilds_raw)
    say("\n--- Seguridad ---")
    say("ALLOWED_GUILD_IDS es muy recomendado (seguridad). Vacío = el bot puede ser invitado a cualquier servidor.")
    guilds = ask("IDs de Guild permitidos (separados por coma, o vacío para todos)", default=current_guilds or "")
    if guilds or (current_guilds_raw and confirm("¿Borrar ALLOWED_GUILD_IDS?", default=False)):
        gstr = guilds.strip()
        try:
            if not gstr:
                guild_list: list[int] = []
            elif gstr.startswith("[") and gstr.endswith("]"):
                guild_list = json.loads(gstr)
            else:
                guild_list = [int(x.strip()) for x in gstr.split(",") if x.strip()]
            updates["allowed_guild_ids"] = guild_list
        except Exception as e:
            say(f"Aviso: no pude parsear como lista ({e}). Guardando como texto crudo.")
            updates["allowed_guild_ids"] = gstr

    # 4. TTS (ajustes seguros, expuestos en web)
    say("\n--- TTS / Audio ---")
    voices = ["eve", "ara", "rex", "sal", "leo"]

    cur_voice = _get_ci(existing, "tts_default_voice") or "eve"
    say(f"Voz TTS actual: {cur_voice}")
    if confirm(f"¿Cambiar voz TTS por defecto? (actual: {cur_voice})", default=False):
        say("Voces disponibles: " + ", ".join(voices))
        v = ask("Voz", default=cur_voice)
        if v:
            updates["tts_default_voice"] = v.strip().lower()

    cur_lang = _get_ci(existing, "tts_default_language") or "es"
    say(f"Idioma TTS actual: {cur_lang}")
    if confirm(f"¿Cambiar idioma TTS por defecto? (actual: {cur_lang})", default=False):
        say("Comunes: es, es-ES, es-MX, en, auto, fr, de, pt-BR ...")
        l = ask("Código de idioma", default=cur_lang)
        if l:
            updates["tts_default_language"] = l.strip()

    # 5. Otros ajustes comunes (solo si el usuario quiere)
    if confirm("¿Configurar algunos ajustes adicionales (modelo, nivel de log, video)?", default=True):
        cur_model = _get_ci(existing, "grok_model") or "grok-4.3"
        m = ask("Modelo Grok", default=cur_model)
        if m:
            updates["grok_model"] = m.strip()

        cur_log = _get_ci(existing, "log_level") or "INFO"
        l = ask("Nivel de log (INFO, DEBUG, WARNING...)", default=cur_log)
        if l:
            updates["log_level"] = l.strip().upper()

        cur_video = _get_ci(existing, "enable_video_generation") or "true"
        if confirm(f"¿Habilitar generación de video? (actual {cur_video})", default=cur_video.lower() != "false"):
            updates["enable_video_generation"] = "true"
        else:
            updates["enable_video_generation"] = "false"

    # === ESCRITURA (siempre a través del escritor seguro unificado) ===
    if not updates:
        say("\nNo se solicitaron cambios. No hay nada que hacer.")
        # Aún así podemos ofrecer OAuth si corresponde
        if will_need_oauth or (_get_ci(existing, "GROK_AUTH_MODE") or "").lower() in ("oauth", "auto"):
            if confirm("¿Quieres lanzar el login OAuth ahora?", default=True):
                _try_run_oauth_login()
        say("\nTip: puedes volver a ejecutar `python scripts/configure_env.py` cuando quieras.")
        return

    say(f"\nA punto de actualizar de forma segura {len(updates)} valor(es): {', '.join(sorted(updates.keys()))}")
    say("El escritor va a: actualizar en su lugar (case-insensitive), colapsar duplicados antiguos,")
    say("preservar comentarios y orden, solo agregar claves realmente nuevas, hacer backup y verificar críticos.")
    if not confirm("¿Aplicar estos cambios al .env ahora?", default=True):
        say("Abortado por el usuario.")
        return

    ok, msg, bak = safe_write_env(ENV_FILE, updates)
    if ok:
        say(f"\n✅ .env actualizado correctamente (sin duplicados).", "bold green")
        if bak:
            say(f"   Backup guardado en: {bak}")
    else:
        say(f"\n❌ Falló la actualización: {msg}", "bold red")
        if bak:
            say(f"   Existe un backup en: {bak}")
        sys.exit(1)

    # ============================================================
    # PASOS FINALES + OFERTA DE OAUTH
    # ============================================================
    say("\n--- Pasos recomendados ---")
    say("  1. Valida la configuración:")
    say("       python -m groksito_discord --status")
    say("       python -m groksito_discord --check")
    say("  2. Si cambiaste algo desde el web dashboard, reinicia el bot de Discord.")
    say("  3. Para iniciar el bot:")
    say("       python -m groksito_discord")

    current_final_mode = _get_ci(parse_env_file(ENV_FILE), "GROK_AUTH_MODE") or updates.get("GROK_AUTH_MODE", "")
    if current_final_mode.lower() in ("oauth", "auto") or will_need_oauth:
        say("\n--- OAuth ---")
        if confirm("¿Quieres ejecutar el login OAuth ahora (recomendado si elegiste oauth/auto)?", default=True):
            _try_run_oauth_login()

    say("\nListo. Puedes volver a correr `python scripts/configure_env.py` cuantas veces quieras. Es idempotente y seguro.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nInterrumpido.", "yellow")
        sys.exit(130)
    except Exception as e:
        say(f"\nError inesperado: {e}", "red")
        if "--debug" in sys.argv:
            import traceback
            traceback.print_exc()
        sys.exit(1)
