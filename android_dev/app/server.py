import asyncio
import contextlib
import hmac
import json
import logging
import re
import shutil
import uuid

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ImageContent, TextContent
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import adb
import jobs
import keystore
import setup
import process
from config import ANDROID_HOME, GRADLE, HOSTNAME, JOB_TIMEOUT, PROJECTS, TOKEN, TOKEN_VALID
from paths import project_file, project_path

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("android_dev")
server = MCPServer("android-dev", version="0.1.0")


def text_result(value):
    if not isinstance(value, str): value = json.dumps(value, indent=2, default=str)
    return [TextContent(type="text", text=value)]


@server.tool(description="Show SDK setup, Java, storage, connected devices, and current build job status.")
async def status() -> list:
    setup_state = dict(setup.state)
    try:
        java = await asyncio.to_thread(process.run, ["java", "-version"], timeout=10, max_bytes=1000)
        java_version = (java.stderr or java.stdout).strip()[:1000]
    except Exception as exc: java_version = str(exc)
    devices = await asyncio.to_thread(adb.devices)
    try: disk = shutil.disk_usage("/data").free
    except OSError: disk = None
    info = {**setup_state, "java_version": java_version, "sdk_path": str(ANDROID_HOME), "disk_free_bytes": disk,
            "adb_devices": devices, "running_job": jobs.CURRENT.get("id") if jobs.CURRENT and jobs.CURRENT.get("state") == "running" else None}
    return text_result(info)


@server.tool(description="Start or rerun verified Android SDK and Gradle setup in the background; poll status for progress. This requires the licence option to be enabled.")
async def sdk_setup() -> list:
    setup.start_setup()
    return text_result(dict(setup.state))


@server.tool(description="List Android SDK packages installed in the runtime SDK.")
async def sdk_list() -> list:
    return text_result(await asyncio.to_thread(setup.installed_packages))


@server.tool(description="Install Android SDK packages whose names use letters, digits, dot, underscore, semicolon, and dash.")
async def sdk_install(packages: list[str]) -> list:
    if any(not re.fullmatch(r"[A-Za-z0-9._;-]+", item) for item in packages): raise ValueError("Invalid package name")
    if not setup.LICENSE_ACCEPTED: raise ValueError("Enable accept_android_sdk_license before installing SDK packages")
    sdkmanager = ANDROID_HOME / "cmdline-tools/latest/bin/sdkmanager"
    if not sdkmanager.exists(): raise ValueError("SDK is not installed; run sdk_setup first")
    p = await asyncio.to_thread(process.run, [str(sdkmanager), *packages], timeout=600, max_bytes=12000)
    output = p.stdout + p.stderr
    return text_result({"exit_code": p.returncode, "output": output, "truncated": "earlier output truncated" in output})


@server.tool(description="List project directories under /share/android_dev/projects.")
async def list_projects() -> list:
    PROJECTS.mkdir(parents=True, exist_ok=True)
    names = sorted(p.name for p in PROJECTS.iterdir() if p.is_dir() and p.resolve().parent == PROJECTS.resolve())
    return text_result({"projects": names[:500], "truncated": len(names) > 500})


@server.tool(description="Create Gradle wrapper files using the pinned system Gradle installation.")
async def gradle_wrapper(project: str, gradle_version: str | None = None) -> list:
    path = project_path(project)
    command = [str(GRADLE), "wrapper"]
    if gradle_version:
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", gradle_version): raise ValueError("Invalid Gradle version")
        command += [f"--gradle-version={gradle_version}"]
    p = await asyncio.to_thread(process.run, ["nice", "-n", "10", *command], cwd=path, timeout=300, max_bytes=12000)
    output = p.stdout.splitlines()
    return text_result({"exit_code": p.returncode, "output": "\n".join(output[-100:]), "truncated": len(output) > 100 or any("earlier output truncated" in line for line in output)})


@server.tool(description="Run Gradle tasks asynchronously in a project; one job runs at a time.")
async def gradle_run(project: str, tasks: list[str], args: list[str] = [], signing_keystore: str | None = None) -> list:
    path = project_path(project)
    if not (path / "gradlew").is_file(): raise ValueError("Project has no ./gradlew; run gradle_wrapper first")
    if not tasks or any(t.startswith("-") for t in tasks): raise ValueError("Provide one or more Gradle task names")
    env = {}
    if signing_keystore:
        keyfile, metafile = keystore._files(signing_keystore)
        if not keyfile.is_file() or not metafile.is_file(): raise ValueError("Signing keystore not found")
        meta = json.loads(metafile.read_text())
        env = {"ANDROID_SIGNING_STORE_FILE": str(keyfile), "ANDROID_SIGNING_STORE_PASSWORD": meta["store_password"],
               "ANDROID_SIGNING_KEY_ALIAS": meta["alias"], "ANDROID_SIGNING_KEY_PASSWORD": meta["key_password"]}
    if any("ANDROID_SIGNING" in arg for arg in args): raise ValueError("Signing environment variables cannot be set through args")
    return text_result(jobs.start_job(project, ["sh", "./gradlew", *tasks, *args], env, JOB_TIMEOUT))


@server.tool(description="Get build job state and a bounded tail of its log.")
async def job_status(job_id: str, tail_lines: int = 100) -> list:
    return text_result(jobs.status(job_id, tail_lines))


@server.tool(description="Cancel a running Gradle build and its child process group.")
async def job_cancel(job_id: str) -> list:
    return text_result(jobs.cancel(job_id))


@server.tool(description="Find APKs, AABs, and test reports under project build output directories.")
async def list_artifacts(project: str) -> list:
    root = project_path(project)
    items = []
    candidates = list(root.glob("**/build/outputs/**/*")) + list(root.glob("**/build/reports/**/*"))
    for p in candidates:
        # One test run writes hundreds of report assets; only the entry pages are artifacts.
        if p.is_file() and (p.suffix in (".apk", ".aab") or p.name == "index.html"):
            stat = p.stat(); items.append({"path": str(p), "size": stat.st_size, "mtime": stat.st_mtime})
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return text_result({"artifacts": items[:500], "truncated": len(items) > 500})


@server.tool(description="Pair an Android phone using the wireless debugging pairing host:port and code.")
async def adb_pair(host_port: str, code: str) -> list: return text_result(await asyncio.to_thread(adb.pair, host_port, code))


@server.tool(description="Connect adb to a phone's wireless debugging connect host:port.")
async def adb_connect(host_port: str) -> list: return text_result(await asyncio.to_thread(adb.connect, host_port))


@server.tool(description="Disconnect adb from a wireless phone host:port.")
async def adb_disconnect(host_port: str) -> list:
    adb.validate_host_port(host_port)
    return text_result(await asyncio.to_thread(adb.run, ["disconnect", host_port]))


@server.tool(description="List adb devices.")
async def adb_devices() -> list: return text_result(await asyncio.to_thread(adb.devices))


@server.tool(description="Install an APK located inside the projects root on the selected phone.")
async def install_apk(path: str, device: str | None = None, replace: bool = True, grant_permissions: bool = False) -> list:
    apk = project_file(path)
    args = ["install", "-t"]
    if replace: args.append("-r")
    if grant_permissions: args.append("-g")
    args.append(str(apk))
    return text_result(await asyncio.to_thread(adb.device_run, device, args, 180))


def valid_package(value):
    if not adb.PKG.fullmatch(value): raise ValueError("Invalid Android package name")


@server.tool(description="Uninstall an Android package from a phone.")
async def uninstall(package: str, device: str | None = None) -> list:
    valid_package(package); return text_result(await asyncio.to_thread(adb.device_run, device, ["uninstall", package]))


@server.tool(description="Launch an Android package, optionally specifying its activity.")
async def launch(package: str, activity: str | None = None, device: str | None = None) -> list:
    valid_package(package)
    if activity and not re.fullmatch(r"(?:\.[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*|[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*)", activity):
        raise ValueError("Invalid Android activity name")
    component = package if not activity else f"{package}/{activity}"
    return text_result(await asyncio.to_thread(adb.device_run, device, ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"] if not activity else ["shell", "am", "start", "-n", component]))


@server.tool(description="Force stop an Android package.")
async def force_stop(package: str, device: str | None = None) -> list:
    valid_package(package); return text_result(await asyncio.to_thread(adb.device_run, device, ["shell", "am", "force-stop", package]))


@server.tool(description="Read bounded Android logcat output, optionally filtered to a running package process.")
async def logcat(device: str | None = None, lines: int = 300, package: str | None = None, min_priority: str = "V") -> list:
    args = ["logcat", "-d", "-v", "threadtime"]
    if package:
        valid_package(package)
        pid = await asyncio.to_thread(adb.device_run, device, ["shell", "pidof", package], 15, False)
        if not pid.strip(): return text_result({"error": f"Package {package} is not running"})
        args += ["--pid", pid.split()[0]]
    if min_priority not in tuple("VDIWEFS"): raise ValueError("Invalid log priority")
    args += ["*:" + min_priority]
    out = await asyncio.to_thread(adb.device_run, device, args, 45, False)
    rows = out.splitlines(); count = max(1, min(lines, 5000))
    return text_result({"output": "\n".join(rows[-count:]), "truncated": len(rows) > count})


@server.tool(description="Save a full-resolution phone screenshot and return a preview image resized to at most 1280 pixels.")
async def screenshot(device: str | None = None) -> list:
    result = await asyncio.to_thread(adb.screenshot, device)
    return [TextContent(type="text", text=f"Screenshot: {result['path']}"), ImageContent(type="image", data=result["data"], mime_type="image/png")]


@server.tool(description="Return bounded uiautomator XML for the phone's current screen.")
async def ui_dump(device: str | None = None) -> list:
    def capture():
        remote = f"/sdcard/android_dev_{uuid.uuid4().hex}.xml"
        try:
            dumped = adb.device_run(device, ["shell", "uiautomator", "dump", remote], 30, False)
            if "error" in dumped.lower(): return {"xml": dumped[-4000:], "truncated": False, "exit_code": 1}
            result = adb.device_subprocess(device, ["shell", "cat", remote], 30, max_bytes=50000)
            return {"xml": result.stdout[-50000:], "truncated": "earlier output truncated" in result.stdout, "exit_code": result.returncode}
        finally:
            try: adb.device_run(device, ["shell", "rm", "-f", remote], 10, False)
            except Exception: pass
    return text_result(await asyncio.to_thread(capture))


@server.tool(description="Tap screen coordinates.")
async def tap(x: int, y: int, device: str | None = None) -> list:
    return text_result(await asyncio.to_thread(adb.device_run, device, ["shell", "input", "tap", str(x), str(y)]))


@server.tool(description="Swipe between screen coordinates.")
async def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300, device: str | None = None) -> list:
    if not 1 <= duration_ms <= 60000: raise ValueError("duration_ms must be 1..60000")
    return text_result(await asyncio.to_thread(adb.device_run, device, ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)]))


@server.tool(description="Type text into the focused Android field.")
async def input_text(text: str, device: str | None = None) -> list:
    return text_result(await asyncio.to_thread(adb.device_run, device, ["shell", "input", "text", adb.input_escape(text)]))


@server.tool(description="Send an Android key event by key code or documented key name.")
async def key_event(key: str, device: str | None = None) -> list:
    if not re.fullmatch(r"[A-Za-z0-9_]+", key): raise ValueError("Invalid key event")
    return text_result(await asyncio.to_thread(adb.device_run, device, ["shell", "input", "keyevent", key]))


@server.tool(description="Run an explicitly requested command in the phone's shell.")
async def adb_shell(command: str, device: str | None = None, timeout_seconds: int = 30) -> list:
    if not 1 <= timeout_seconds <= 300: raise ValueError("timeout_seconds must be 1..300")
    p = await asyncio.to_thread(adb.device_subprocess, device, ["shell", command], timeout_seconds, max_bytes=12000)
    output = p.stdout.splitlines()
    return text_result({"exit_code": p.returncode, "output": "\n".join(output[-200:]), "truncated": len(output) > 200 or any("earlier output truncated" in line for line in output)})


@server.tool(description="Create a release signing keystore. Passwords are generated privately and never returned.")
async def keystore_create(name: str, alias: str = "release", dname: str | None = None) -> list:
    return text_result(await asyncio.to_thread(keystore.create, name, alias, dname))


@server.tool(description="List signing keystore names and SHA-256 fingerprints.")
async def keystore_list() -> list:
    items = await asyncio.to_thread(keystore.listing)
    return text_result({"keystores": items[:100], "truncated": len(items) > 100})


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path != "/health":
            if not TOKEN_VALID:
                log.error("MCP request refused: configure mcp_token with at least 32 characters")
                return JSONResponse({"error": "MCP unavailable: configure mcp_token with at least 32 characters"}, status_code=503)
            supplied = request.headers.get("authorization", "")
            candidate = supplied[7:] if supplied.startswith("Bearer ") else ""
            if not hmac.compare_digest(candidate.encode(), TOKEN.encode()):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


async def health(_request): return JSONResponse({"status": "ok"})


app = server.streamable_http_app(
    streamable_http_path="/mcp", json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{HOSTNAME}:8200", f"{HOSTNAME}:*", "localhost:*"]),
    host="0.0.0.0")
app.add_middleware(AuthMiddleware)
app.router.routes.append(Route("/health", health, methods=["GET"]))
_mcp_lifespan = app.router.lifespan_context


@contextlib.asynccontextmanager
async def lifespan(application):
    jobs.gradle_properties()
    setup.start_setup()
    async with _mcp_lifespan(application) as state:
        yield state


app.router.lifespan_context = lifespan


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8200)
