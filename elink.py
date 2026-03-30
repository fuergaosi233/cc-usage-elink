#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "bleak>=0.22,<1.0",
#   "pillow>=12.1.1",
#   "click>=8.1",
#   "rich>=13.0",
# ]
# ///
"""
蓝签墨水屏 BLE 交互工具 + Claude Code 用量看板
设备: 4.2寸(400x300) 三色墨水屏 (黑/白/红)
"""

import asyncio
import json
import math
import subprocess
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import click
from bleak import BleakClient, BleakScanner
from PIL import Image, ImageDraw, ImageFont
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

# ── Constants ─────────────────────────────────────────────────────────────────

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID    = "0000ffe2-0000-1000-8000-00805f9b34fb"

TYPE_BLACK = 0x13  # bit=1 → 白, bit=0 → 黑
TYPE_RED   = 0x12  # bit=1 → 红

SCREEN_W = 400
SCREEN_H = 300
CHUNK    = 240  # 每包最多 240 字节

CONFIG_PATH = Path.home() / ".config" / "elink" / "config.json"

console = Console()

# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ── Protocol ──────────────────────────────────────────────────────────────────

def build_start(color_type: int) -> bytes:
    return bytes([color_type, 0x00, 0x00])


def build_end(color_type: int) -> bytes:
    return bytes([color_type, 0xFF, 0xFF])


def build_data_packets(color_type: int, data: bytes) -> list[bytes]:
    packets = []
    for i, offset in enumerate(range(0, len(data), CHUNK)):
        idx   = i + 1
        chunk = data[offset:offset + CHUNK]
        hi    = (idx >> 8) & 0xFF
        lo    = idx & 0xFF
        packets.append(bytes([color_type, hi, lo, len(chunk)]) + chunk)
    return packets


# ── Image conversion ──────────────────────────────────────────────────────────

def image_to_eink_bytes(image_path: str) -> tuple[bytes, bytes]:
    img = Image.open(image_path).convert("RGB").resize((SCREEN_W, SCREEN_H))
    black_data = bytearray()
    red_data   = bytearray()

    for y in range(SCREEN_H - 1, -1, -1):
        for byte_idx in range(math.ceil(SCREEN_W / 8)):
            b_black = 0
            b_red   = 0
            for bit in range(8):
                x = byte_idx * 8 + bit
                if x >= SCREEN_W:
                    b_black |= (1 << (7 - bit))
                    continue
                r, g, b = img.getpixel((x, y))
                is_red   = r > 150 and g < 100 and b < 100
                is_black = (r + g + b) < 200 and not is_red
                if not is_black:
                    b_black |= (1 << (7 - bit))
                if is_red:
                    b_red |= (1 << (7 - bit))
            black_data.append(b_black)
            red_data.append(b_red)

    return bytes(black_data), bytes(red_data)


# ── BLE ───────────────────────────────────────────────────────────────────────

async def scan_devices(timeout: float = 8.0) -> list:
    found = []

    def on_detect(device, adv):
        name  = device.name or ""
        uuids = [str(u).lower() for u in (adv.service_uuids or [])]
        if (
            "EDP" in name.upper()
            or SERVICE_UUID.lower() in uuids
            or "ffe0" in " ".join(uuids)
        ) and device not in found:
            found.append(device)

    async with BleakScanner(detection_callback=on_detect):
        await asyncio.sleep(timeout)

    return found


async def scan_until_found(timeout: float = 600.0) -> list:
    """扫描直到发现至少一台蓝签设备（或超时），找到后再等3s收集其他设备"""
    found = []
    ev    = asyncio.Event()
    start = time.monotonic()

    def on_detect(device, adv):
        name  = device.name or ""
        uuids = [str(u).lower() for u in (adv.service_uuids or [])]
        if (
            "EDP" in name.upper()
            or SERVICE_UUID.lower() in uuids
            or "ffe0" in " ".join(uuids)
        ) and device not in found:
            found.append(device)
            ev.set()

    async with BleakScanner(detection_callback=on_detect):
        live_task = asyncio.create_task(_scan_with_live(ev, start, timeout, None))
        try:
            await asyncio.wait_for(asyncio.shield(ev.wait()), timeout=timeout)
            await asyncio.sleep(3.0)  # 多等3s收集附近其他设备
        except asyncio.TimeoutError:
            raise RuntimeError(f"扫描 {timeout:.0f}s 未发现设备，请确认设备已开机")
        finally:
            live_task.cancel()
            try:
                await live_task
            except asyncio.CancelledError:
                pass

    return found


async def _scan_with_live(ev: asyncio.Event, start: float, scan_timeout: float, address: str | None):
    """扫描期间显示进度条（elapsed / timeout）"""
    addr_str = f" [cyan]{address}[/cyan]" if address else ""
    with Progress(
        SpinnerColumn(),
        TextColumn(f"[bold]扫描{addr_str}[/bold]"),
        BarColumn(bar_width=30),
        TextColumn("[dim]{task.completed:.0f}s / {task.total:.0f}s[/dim]"),
        console=console,
    ) as progress:
        task = progress.add_task("", total=scan_timeout)
        while not ev.is_set():
            elapsed = time.monotonic() - start
            progress.update(task, completed=min(elapsed, scan_timeout))
            await asyncio.sleep(0.25)


async def _try_direct_connect(address: str, attempts: int = 3) -> BleakClient | None:
    """跳过扫描，直接按地址连接（CoreBluetooth 缓存过的设备有效）"""
    for i in range(attempts):
        client = BleakClient(address)
        try:
            await client.connect(timeout=10.0)
            return client
        except Exception as e:
            console.print(f"  [dim]直连尝试 {i+1}/{attempts}: {e}[/dim]")
            if i < attempts - 1:
                await asyncio.sleep(1.0)
    return None


async def find_and_connect(address: str | None, scan_timeout: float = 60.0) -> BleakClient:
    # ── 快速路径：有地址先直连，无需等广播 ──────────────────────────
    if address:
        with console.status(f"[bold]直连 [cyan]{address}[/cyan]...[/bold]"):
            client = await _try_direct_connect(address)
        if client:
            console.print(f"[green]✓[/green] 直连成功")
            return client
        console.print("[yellow]直连失败，回退到扫描模式...[/yellow]")

    # ── 扫描模式（无地址 或 直连失败） ───────────────────────────────
    found = None
    ev    = asyncio.Event()

    def on_detect(device, adv):
        nonlocal found
        uuids = " ".join(str(u).lower() for u in (adv.service_uuids or []))
        if "ffe0" in uuids and (address is None or device.address == address) and not found:
            found = device
            ev.set()

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()

    start     = time.monotonic()
    live_task = asyncio.create_task(_scan_with_live(ev, start, scan_timeout, address))
    try:
        await asyncio.wait_for(ev.wait(), timeout=scan_timeout)
    except asyncio.TimeoutError:
        live_task.cancel()
        await scanner.stop()
        raise RuntimeError(f"扫描 {scan_timeout:.0f}s 未找到设备，请确认设备已开机并在附近")
    finally:
        live_task.cancel()
        try:
            await live_task
        except asyncio.CancelledError:
            pass

    elapsed = time.monotonic() - start
    console.print(f"[green]✓[/green] 找到: [cyan]{found.name or '?'}[/cyan]  [dim]{elapsed:.1f}s[/dim]")

    for attempt in range(5):
        await asyncio.sleep(0.3)
        client = BleakClient(found)
        try:
            await client.connect(timeout=20.0)
            await scanner.stop()
            return client
        except Exception as e:
            console.print(f"  [yellow]连接失败 (第{attempt+1}次): {e}，等2s重试...[/yellow]")
            await asyncio.sleep(2.0)

    await scanner.stop()
    raise RuntimeError("连接失败，已重试5次")


async def send_channel(
    client: BleakClient,
    color_type: int,
    data: bytes,
    progress: Progress,
    task_id,
):
    packets = build_data_packets(color_type, data)

    await client.write_gatt_char(CHAR_UUID, build_start(color_type), response=False)
    await asyncio.sleep(3.0)

    for i, pkt in enumerate(packets):
        await client.write_gatt_char(CHAR_UUID, pkt, response=False)
        await asyncio.sleep(1.0 if i < 10 else 0.6)
        progress.update(task_id, advance=1)

    await client.write_gatt_char(CHAR_UUID, build_end(color_type), response=False)
    await asyncio.sleep(0.1)


def _make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    )


async def _do_send(address: str | None, black_bytes: bytes, red_bytes: bytes):
    n_black = math.ceil(len(black_bytes) / CHUNK)
    n_red   = math.ceil(len(red_bytes)   / CHUNK)

    client = await find_and_connect(address)
    console.print(f"[green]✓[/green] 已连接  预计约 {_est_seconds(n_black + n_red):.0f}s")

    try:
        with _make_progress() as progress:
            t1 = progress.add_task("[bold]黑色通道[/bold]", total=n_black)
            await send_channel(client, TYPE_BLACK, black_bytes, progress, t1)

            t2 = progress.add_task("[red]红色通道[/red]", total=n_red)
            await send_channel(client, TYPE_RED, red_bytes, progress, t2)

        console.print(Panel("[bold green]发送完毕，墨水屏正在刷新！[/bold green]", expand=False))
    finally:
        await client.disconnect()


def _est_seconds(total_packets: int) -> float:
    """估算发送耗时（秒）：起始3s + 前10包×1s + 剩余×0.6s，两通道各一次"""
    per_channel = 3.0 + min(total_packets // 2, 10) * 1.0 + max(total_packets // 2 - 10, 0) * 0.6
    return per_channel * 2


# ── Usage image ───────────────────────────────────────────────────────────────

def _detect_token_from_keychain() -> str | None:
    """尝试从 macOS Keychain 自动读取 OAuth Token"""
    raw = subprocess.run(
        ["security", "find-generic-password", "-s", "usage-elink-oauth", "-w"],
        capture_output=True, text=True,
    ).stdout.strip()
    if raw:
        return raw

    raw = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True, text=True,
    ).stdout.strip()
    if raw:
        try:
            return json.loads(raw)["claudeAiOauth"]["accessToken"]
        except Exception:
            pass
    return None


def get_oauth_token() -> str | None:
    # 优先级: config 文件 > Keychain
    return load_config().get("oauth_token") or _detect_token_from_keychain()


def fetch_usage(token: str) -> dict | None:
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        console.print(f"[yellow]API 请求失败: {e}[/yellow]")
        return None


def _fmt_resets(iso: str | None) -> str:
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso).astimezone()
    s  = (dt - datetime.now(timezone.utc).astimezone()).total_seconds()
    if s <= 0:
        return "Now"
    h, rem = divmod(int(s), 3600)
    m = rem // 60
    return f"{h}h {m}m" if h < 24 else dt.strftime("%a %-I%p").upper()


def render_usage_image(out: str = "/tmp/usage_display.png") -> str:
    # ── Colors ────────────────────────────────────────────────────────
    BG    = (252, 252, 250)
    BLACK = (8,   8,   8)
    WHITE = (255, 255, 255)
    GRAY  = (115, 115, 115)
    LGRAY = (215, 215, 215)
    RED   = (190, 30,  30)

    # ── Data ──────────────────────────────────────────────────────────
    token = get_oauth_token()
    api   = fetch_usage(token) if token else None
    if not token:
        console.print("[yellow]未找到 OAuth Token（用 elink setup 配置）[/yellow]")

    fh   = (api or {}).get("five_hour")       or {}
    sd   = (api or {}).get("seven_day")        or {}
    sd_s = (api or {}).get("seven_day_sonnet") or {}

    # ── Canvas: render at ½ res, pixel-double with NEAREST ───────────
    # 低分辨率渲染后 nearest-neighbor 放大，得到像素块风格（匹配墨水屏美学）
    W, H  = 400, 300
    rW, rH = W // 2, H // 2   # 200 × 150 canvas
    P     = 7
    BAR_H = 17

    img = Image.new("RGB", (rW, rH), BG)
    d   = ImageDraw.Draw(img)

    # ── Fonts (half apparent size; after 2× they look correct) ────────
    def mono(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        candidates = [
            ("/System/Library/Fonts/Menlo.ttc",        1 if bold else 0),
            ("/System/Library/Fonts/Monaco.ttf",        0),
            ("/System/Library/Fonts/Courier.ttc",       1 if bold else 0),
            ("/System/Library/Fonts/HelveticaNeue.ttc", 1 if bold else 0),
        ]
        for path, idx in candidates:
            try:
                return ImageFont.truetype(path, size, index=idx)
            except Exception:
                pass
        return ImageFont.load_default()

    # ── Header ────────────────────────────────────────────────────────
    HDR_H = 29
    d.rectangle([0, 0, rW, HDR_H], fill=BLACK)
    d.text((P, HDR_H // 2), "CC  USAGE",
           font=mono(17, bold=True), fill=WHITE, anchor="lm")
    d.text((rW - P, HDR_H // 2 - 5),
           datetime.now().strftime("%H:%M"),
           font=mono(9), fill=(175, 175, 175), anchor="rm")
    d.text((rW - P, HDR_H // 2 + 5),
           date.today().strftime("%-d %b"),
           font=mono(7), fill=(150, 150, 150), anchor="rm")

    # ── Helpers ───────────────────────────────────────────────────────
    def dashed(y: int, x1: int, x2: int):
        x = x1
        while x < x2:
            d.line([x, y, min(x + 2, x2), y], fill=GRAY, width=1)
            x += 4

    def row_header(y: int, label: str, reset_str: str) -> int:
        lf = mono(8, bold=True)
        rf = mono(7)
        lw = int(d.textlength(label, font=lf))
        rw = int(d.textlength(reset_str, font=rf))
        d.text((P, y), label, font=lf, fill=BLACK)
        dashed(y + 5, P + lw + 3, rW - P - rw - 3)
        d.text((rW - P, y), reset_str, font=rf, fill=GRAY, anchor="rt")
        return y + 12

    def bar_row(y: int, label: str, pct: float, pct_str: str,
                fill_color=BLACK) -> int:
        LW = 18
        PW = 26
        bx = P + LW + 3
        bw = rW - P - LW - PW - 6

        d.text((P, y + (BAR_H - 10) // 2),
               label, font=mono(10, bold=True), fill=BLACK)

        d.rectangle([bx, y, bx + bw, y + BAR_H],
                    outline=BLACK, width=1, fill=LGRAY)
        if pct > 0.001:
            fw = max(int(pct * (bw - 2)), 1)
            d.rectangle([bx + 1, y + 1, bx + fw, y + BAR_H - 1],
                        fill=fill_color)

        d.text((rW - P, y + (BAR_H - 10) // 2),
               pct_str, font=mono(10, bold=True), fill=fill_color, anchor="rt")

        return y + BAR_H + 4

    # ── Sections ──────────────────────────────────────────────────────
    y = HDR_H + 5

    fh_pct = (fh.get("utilization") or 0) / 100
    fh_col = RED if fh_pct >= 0.8 else BLACK
    fh_str = f"{fh.get('utilization', 0):.0f}%" if fh else "—"
    fh_rst = _fmt_resets(fh.get("resets_at")) if fh else "No data"

    y = row_header(y, "SESSION", f"Resets {fh_rst}")
    y = bar_row(y, "5H", fh_pct, fh_str, fh_col)
    y += 5

    sd_pct = (sd.get("utilization") or 0) / 100
    sd_str = f"{sd.get('utilization', 0):.0f}%" if sd else "—"
    sd_rst = _fmt_resets(sd.get("resets_at")) if sd else "—"

    y = row_header(y, "ALL MODELS", f"Resets {sd_rst}")
    y = bar_row(y, "7D", sd_pct, sd_str)
    y += 5

    ss_pct = (sd_s.get("utilization") or 0) / 100
    ss_str = f"{sd_s.get('utilization', 0):.0f}%" if sd_s else "—"
    ss_rst = _fmt_resets(sd_s.get("resets_at")) if sd_s else "—"

    y = row_header(y, "SONNET", f"Resets {ss_rst}")
    bar_row(y, "7D", ss_pct, ss_str)

    # ── Pixel-double to 400×300 (nearest-neighbor = hard pixel edges) ─
    img = img.resize((W, H), Image.NEAREST)
    img.save(out)
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """蓝签墨水屏 CLI · Claude Code 用量看板"""


# ─ setup ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--force", is_flag=True, help="强制重新配置（覆盖现有设置）")
def setup(force: bool):
    """全自动初始配置（首次使用）"""
    cfg = load_config()

    console.print(Panel("[bold cyan]elink 初始化配置[/bold cyan]", expand=False))

    # ── 步骤 1: 绑定设备 ──────────────────────────────────────────────
    if cfg.get("device_address") and not force:
        console.print(f"[dim]✓ 设备已绑定: {cfg['device_address']}（用 --force 重新配置）[/dim]")
    else:
        console.print("\n[bold]步骤 1/2[/bold]  绑定墨水屏设备  [dim](最多等待10分钟，Ctrl+C 跳过)[/dim]")
        try:
            devices = asyncio.run(scan_until_found(600.0))
        except (RuntimeError, KeyboardInterrupt) as e:
            if isinstance(e, RuntimeError):
                console.print(f"[red]{e}[/red]")
            else:
                console.print("[dim]跳过设备绑定[/dim]")
            devices = []

        if devices:
            table = Table(header_style="bold cyan")
            table.add_column("#", style="dim", width=3)
            table.add_column("设备名", min_width=20)
            table.add_column("地址", style="cyan")
            for i, dev in enumerate(devices, 1):
                table.add_row(str(i), dev.name or "—", dev.address)
            console.print(table)

            if len(devices) == 1:
                selected = devices[0]
                console.print(f"自动选择唯一设备: [cyan]{selected.name or selected.address}[/cyan]")
            else:
                idx      = click.prompt("选择设备编号", type=click.IntRange(1, len(devices)))
                selected = devices[idx - 1]

            cfg["device_address"] = selected.address
            save_config(cfg)
            console.print(f"[green]✓[/green] 设备已绑定: [cyan]{selected.address}[/cyan]")

    # ── 步骤 2: OAuth Token ───────────────────────────────────────────
    if cfg.get("oauth_token") and not force:
        console.print(f"[dim]✓ OAuth Token 已配置（用 --force 重新配置）[/dim]")
    else:
        console.print("\n[bold]步骤 2/2[/bold]  配置 OAuth Token")
        with console.status("[bold]检测 Keychain...[/bold]"):
            auto_token = _detect_token_from_keychain()

        need_manual = True
        if auto_token:
            console.print(
                f"[green]✓[/green] 从 Keychain 读取到 Token: [dim]{auto_token[:16]}…[/dim]"
            )
            if click.confirm("使用此 Token?", default=True):
                cfg["oauth_token"] = auto_token
                save_config(cfg)
                console.print("[green]✓[/green] Token 已保存")
                need_manual = False
            else:
                console.print("[dim]已跳过，请手动输入[/dim]")
        else:
            console.print("[yellow]Keychain 中未找到 Token[/yellow]")

        if need_manual:
            console.print(
                "[dim]获取方式: Claude.ai DevTools → Application → Cookies → sessionKey[/dim]\n"
                "[dim]或: macOS Keychain → 搜索 usage-elink-oauth[/dim]"
            )
            token = click.prompt("粘贴 OAuth Token", hide_input=True)
            cfg["oauth_token"] = token
            save_config(cfg)
            console.print("[green]✓[/green] Token 已保存")

    # ── 完成 ─────────────────────────────────────────────────────────
    console.print(Panel(
        f"[bold green]配置完成！[/bold green]\n\n"
        f"设备地址: [cyan]{cfg.get('device_address', '[red]未配置[/red]')}[/cyan]\n"
        f"OAuth Token: [dim]{'已配置' if cfg.get('oauth_token') else '[yellow]未配置[/yellow]'}[/dim]\n\n"
        "[dim]立即推送:    [bold]uv run elink.py push[/bold][/dim]\n"
        "[dim]后台定时推送: [bold]uv run elink.py watch[/bold][/dim]",
        expand=False,
    ))


# ─ scan ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--timeout", "-t", default=8.0, show_default=True, help="扫描超时（秒）")
def scan(timeout: float):
    """扫描附近的蓝签设备"""
    with console.status(f"[bold]扫描中 ({timeout:.0f}s)...[/bold]"):
        devices = asyncio.run(scan_devices(timeout))

    if not devices:
        console.print("[red]未发现蓝签设备[/red]")
        return

    table = Table(title="发现的蓝签设备", header_style="bold cyan")
    table.add_column("#", style="dim", width=3)
    table.add_column("设备名", min_width=20)
    table.add_column("地址", style="cyan")
    for i, dev in enumerate(devices, 1):
        table.add_row(str(i), dev.name or "—", dev.address)
    console.print(table)


# ─ bind ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("address", required=False)
@click.option("--timeout", "-t", default=8.0, show_default=True, help="扫描超时（秒）")
def bind(address: str | None, timeout: float):
    """绑定默认设备（扫描后选择，或直接指定地址）"""
    if address:
        cfg = load_config()
        cfg["device_address"] = address
        save_config(cfg)
        console.print(f"[green]✓[/green] 已绑定: [cyan]{address}[/cyan]")
        return

    with console.status(f"[bold]扫描中 ({timeout:.0f}s)...[/bold]"):
        devices = asyncio.run(scan_devices(timeout))

    if not devices:
        console.print("[red]未发现蓝签设备[/red]")
        return

    table = Table(title="发现的蓝签设备", header_style="bold cyan")
    table.add_column("#", style="dim", width=3)
    table.add_column("设备名", min_width=20)
    table.add_column("地址", style="cyan")
    for i, dev in enumerate(devices, 1):
        table.add_row(str(i), dev.name or "—", dev.address)
    console.print(table)

    if len(devices) == 1:
        if not click.confirm(f"绑定 [{devices[0].name or devices[0].address}]?", default=True):
            return
        selected = devices[0]
    else:
        idx      = click.prompt("选择设备编号", type=click.IntRange(1, len(devices)))
        selected = devices[idx - 1]

    cfg = load_config()
    cfg["device_address"] = selected.address
    save_config(cfg)
    console.print(f"[green]✓[/green] 已绑定: [cyan]{selected.name or '?'}[/cyan]  {selected.address}")


# ─ send ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("image_path", type=click.Path(exists=True))
@click.argument("address", required=False)
def send(image_path: str, address: str | None):
    """发送图片到墨水屏"""
    if not address:
        address = load_config().get("device_address")

    with console.status("[bold]转换图片...[/bold]"):
        black_bytes, red_bytes = image_to_eink_bytes(image_path)
    console.print(
        f"[green]✓[/green] 图片转换完成  "
        f"黑色: {len(black_bytes)}B  红色: {len(red_bytes)}B"
    )

    asyncio.run(_do_send(address, black_bytes, red_bytes))


# ─ clear ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("address", required=False)
def clear(address: str | None):
    """清屏（全白）"""
    if not address:
        address = load_config().get("device_address")

    total       = SCREEN_H * math.ceil(SCREEN_W / 8)
    black_bytes = bytes([0xFF] * total)
    red_bytes   = bytes([0x00] * total)

    asyncio.run(_do_send(address, black_bytes, red_bytes))


# ─ push ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("address", required=False)
@click.option("--out", default="/tmp/usage_display.png", show_default=True, help="图片输出路径")
@click.option("--dry-run", is_flag=True, help="只生成图片，不发送")
def push(address: str | None, out: str, dry_run: bool):
    """一条龙：生成 CC 用量图 → 发送到墨水屏"""
    if not address:
        address = load_config().get("device_address")

    with console.status("[bold]获取 Claude Code 用量数据...[/bold]"):
        image_path = render_usage_image(out)
    console.print(f"[green]✓[/green] 用量图已生成: {image_path}")

    if dry_run:
        console.print("[dim]--dry-run: 跳过发送[/dim]")
        return

    with console.status("[bold]转换图片...[/bold]"):
        black_bytes, red_bytes = image_to_eink_bytes(image_path)

    asyncio.run(_do_send(address, black_bytes, red_bytes))


# ─ watch ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("address", required=False)
@click.option("--interval", "-i", default=5, show_default=True, help="刷新间隔（分钟）")
@click.option("--out", default="/tmp/usage_display.png", show_default=True, help="图片缓存路径")
def watch(address: str | None, interval: int, out: str):
    """后台模式：定时推送用量到墨水屏（Ctrl+C 退出）"""
    if not address:
        address = load_config().get("device_address")

    if not address:
        console.print(
            "[red]未绑定设备[/red]  先运行 [bold]elink setup[/bold] 或 [bold]elink bind[/bold]"
        )
        raise SystemExit(1)

    console.print(Panel(
        f"[bold]后台模式[/bold]  每 [cyan]{interval}[/cyan] 分钟刷新一次\n"
        f"设备: [cyan]{address}[/cyan]\n"
        "[dim]Ctrl+C 退出[/dim]",
        title="[bold cyan]elink watch[/bold cyan]",
        expand=False,
    ))

    run_count = 0
    while True:
        run_count += 1
        console.rule(f"[bold]第 {run_count} 次  {datetime.now().strftime('%H:%M:%S')}[/bold]")

        try:
            with console.status("[bold]获取用量数据...[/bold]"):
                image_path = render_usage_image(out)
            console.print(f"[green]✓[/green] 用量图已生成")

            with console.status("[bold]转换图片...[/bold]"):
                black_bytes, red_bytes = image_to_eink_bytes(image_path)

            asyncio.run(_do_send(address, black_bytes, red_bytes))

        except KeyboardInterrupt:
            raise
        except Exception as e:
            console.print(f"[red]推送失败: {e}[/red]  将在 {interval} 分钟后重试")

        # ── 倒计时 ────────────────────────────────────────────────────
        seconds = interval * 60
        try:
            with Live(console=console, refresh_per_second=2) as live:
                for remaining in range(seconds, 0, -1):
                    m, s = divmod(remaining, 60)
                    live.update(Text(f"  下次推送: {m:02d}:{s:02d}", style="dim"))
                    time.sleep(1)
        except KeyboardInterrupt:
            console.print("\n[yellow]已停止[/yellow]")
            break


# ─ config ────────────────────────────────────────────────────────────────────

@cli.group()
def config():
    """配置管理"""


@config.command("set-token")
@click.option("--token", prompt="OAuth Token", hide_input=True, help="Claude OAuth Token")
def config_set_token(token: str):
    """设置 OAuth Token"""
    cfg = load_config()
    cfg["oauth_token"] = token
    save_config(cfg)
    console.print("[green]✓[/green] OAuth Token 已保存")


@config.command("clear-token")
def config_clear_token():
    """删除已保存的 OAuth Token（回退到 Keychain 自动检测）"""
    cfg = load_config()
    cfg.pop("oauth_token", None)
    save_config(cfg)
    console.print("[green]✓[/green] Token 已清除，将回退到 Keychain")


@config.command("show")
def config_show():
    """显示当前配置"""
    cfg = load_config()

    table = Table(title="当前配置", header_style="bold cyan")
    table.add_column("Key",   style="cyan", min_width=18)
    table.add_column("Value")

    addr  = cfg.get("device_address") or "[dim]未绑定[/dim]"
    token = cfg.get("oauth_token")
    token_display = f"{token[:16]}…" if token else "[dim]未设置（自动检测 Keychain）[/dim]"

    table.add_row("device_address", addr)
    table.add_row("oauth_token",    token_display)
    table.add_row("config_path",    str(CONFIG_PATH))
    console.print(table)


if __name__ == "__main__":
    cli()
