# Provides updates for Klipper and Moonraker
#
# Copyright (C) 2020  Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import asyncio
import os
import pathlib
import logging
import shutil
import zipfile
import time
import tempfile
import re
import json
from ...utils import source_info
from ...thirdparty.packagekit import enums as PkEnum
from .common import AppType, Channel, get_base_configuration, get_app_type
from .base_deploy import BaseDeploy
from .app_deploy import AppDeploy
from .git_deploy import GitDeploy
from .zip_deploy import ZipDeploy

# Annotation imports
from typing import (
    TYPE_CHECKING,
    TypeVar,
    Any,
    Awaitable,
    Callable,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
    Dict,
    List,
    cast
)
if TYPE_CHECKING:
    from ...server import Server
    from ...confighelper import ConfigHelper
    from ...common import WebRequest
    from ...klippy_connection import KlippyConnection
    from ..shell_command import ShellCommandFactory as SCMDComp
    from ..database import MoonrakerDatabase as DBComp
    from ..database import NamespaceWrapper
    from ..dbus_manager import DbusManager
    from ..machine import Machine
    from ..http_client import HttpClient
    from ..file_manager.file_manager import FileManager
    from ...eventloop import FlexTimer
    from dbus_next import Variant
    from dbus_next.aio import ProxyInterface
    JsonType = Union[List[Any], Dict[str, Any]]
    _T = TypeVar("_T")

# Check To see if Updates are necessary each hour
UPDATE_REFRESH_INTERVAL = 3600.
# Perform auto refresh no later than 4am
MAX_UPDATE_HOUR = 4

def get_deploy_class(
    app_type: Union[AppType, str], default: _T
) -> Union[Type[BaseDeploy], _T]:
    key = AppType.from_string(app_type) if isinstance(app_type, str) else app_type
    _deployers = {
        AppType.WEB: WebClientDeploy,
        AppType.GIT_REPO: GitDeploy,
        AppType.ZIP: ZipDeploy
    }
    return _deployers.get(key, default)

class UpdateManager:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.event_loop = self.server.get_event_loop()
        self.kconn: KlippyConnection
        self.kconn = self.server.lookup_component("klippy_connection")
        self.app_config = get_base_configuration(config)
        auto_refresh_enabled = config.getboolean('enable_auto_refresh', False)
        self.cmd_helper = CommandHelper(config, self.get_updaters)
        self.updaters: Dict[str, BaseDeploy] = {}
        if config.getboolean('enable_system_updates', True):
            self.updaters['system'] = PackageDeploy(config, self.cmd_helper)
        mcfg = self.app_config["moonraker"]
        kcfg = self.app_config["klipper"]
        mclass = get_deploy_class(mcfg.get("type"), BaseDeploy)
        self.updaters['moonraker'] = mclass(mcfg, self.cmd_helper)
        kclass = BaseDeploy
        if (
            os.path.exists(kcfg.get("path")) and
            os.path.exists(kcfg.get("env"))
        ):
            kclass = get_deploy_class(kcfg.get("type"), BaseDeploy)
        self.updaters['klipper'] = kclass(kcfg, self.cmd_helper)

        # TODO: The below check may be removed when invalid config options
        # raise a config error.
        if (
            config.get("client_repo", None) is not None or
            config.get('client_path', None) is not None
        ):
            raise config.error(
                "The deprecated 'client_repo' and 'client_path' options\n"
                "have been removed.  See Moonraker's configuration docs\n"
                "for details on client configuration.")
        client_sections = config.get_prefix_sections("update_manager ")
        for section in client_sections:
            cfg = config[section]
            name = BaseDeploy.parse_name(cfg)
            if name in self.updaters:
                if name not in ["klipper", "moonraker"]:
                    self.server.add_warning(
                        f"[update_manager]: Extension {name} already added"
                    )
                continue
            try:
                client_type = cfg.get("type")
                deployer = get_deploy_class(client_type, None)
                if deployer is None:
                    self.server.add_warning(
                        f"Invalid type '{client_type}' for section [{section}]")
                else:
                    self.updaters[name] = deployer(cfg, self.cmd_helper)
            except Exception as e:
                self.server.add_warning(
                    f"[update_manager]: Failed to load extension {name}: {e}"
                )

        self.cmd_request_lock = asyncio.Lock()
        self.initial_refresh_complete: bool = False
        self.klippy_identified_evt: Optional[asyncio.Event] = None

        # Auto Status Refresh
        self.refresh_timer: Optional[FlexTimer] = None
        if auto_refresh_enabled:
            self.refresh_timer = self.event_loop.register_timer(
                self._handle_auto_refresh)

        self.server.register_endpoint(
            "/machine/update/moonraker", ["POST"],
            self._handle_update_request)
        self.server.register_endpoint(
            "/machine/update/klipper", ["POST"],
            self._handle_update_request)
        self.server.register_endpoint(
            "/machine/update/system", ["POST"],
            self._handle_update_request)
        self.server.register_endpoint(
            "/machine/update/client", ["POST"],
            self._handle_update_request)
        self.server.register_endpoint(
            "/machine/update/full", ["POST"],
            self._handle_full_update_request)
        self.server.register_endpoint(
            "/machine/update/status", ["GET"],
            self._handle_status_request)
        self.server.register_endpoint(
            "/machine/update/refresh", ["POST"],
            self._handle_refresh_request)
        self.server.register_endpoint(
            "/machine/update/recover", ["POST"],
            self._handle_repo_recovery)
        self.server.register_endpoint(
            "/machine/update/rollback", ["POST"],
            self._handle_rollback)
        self.server.register_notification("update_manager:update_response")
        self.server.register_notification("update_manager:update_refreshed")

        # Register Ready Event
        self.server.register_event_handler(
            "server:klippy_identified", self._set_klipper_repo)

    def get_updaters(self) -> Dict[str, BaseDeploy]:
        return self.updaters

    async def component_init(self) -> None:
        # Prune stale data from the database
        umdb = self.cmd_helper.get_umdb()
        db_keys = await umdb.keys()
        for key in db_keys:
            if key not in self.updaters:
                logging.info(f"Removing stale update_manager data: {key}")
                await umdb.pop(key, None)
        for updater in list(self.updaters.values()):
            await updater.initialize()
        if self.refresh_timer is not None:
            self.refresh_timer.start()
        else:
            self.event_loop.register_callback(
                self._handle_auto_refresh, self.event_loop.get_loop_time()
            )

    def _set_klipper_repo(self) -> None:
        if self.klippy_identified_evt is not None:
            self.klippy_identified_evt.set()
        kinfo = self.server.get_klippy_info()
        if not kinfo:
            logging.info("No valid klippy info received")
            return
        kpath: str = kinfo['klipper_path']
        executable: str = kinfo['python_path']
        kupdater = self.updaters.get('klipper')
        if (
            isinstance(kupdater, AppDeploy) and
            kupdater.check_same_paths(kpath, executable)
        ):
            # Current Klipper Updater is valid
            return
        # Update paths in the database
        db: DBComp = self.server.lookup_component('database')
        db.insert_item("moonraker", "update_manager.klipper_path", kpath)
        db.insert_item("moonraker", "update_manager.klipper_exec", executable)
        app_type = get_app_type(kpath)
        kcfg = self.app_config["klipper"]
        kcfg.set_option("path", kpath)
        kcfg.set_option("env", executable)
        kcfg.set_option("type", str(app_type))
        need_notification = not isinstance(kupdater, AppDeploy)
        kclass = get_deploy_class(app_type, BaseDeploy)
        self.updaters['klipper'] = kclass(kcfg, self.cmd_helper)
        coro = self._update_klipper_repo(need_notification)
        self.event_loop.create_task(coro)

    async def _update_klipper_repo(self, notify: bool) -> None:
        async with self.cmd_request_lock:
            umdb = self.cmd_helper.get_umdb()
            await umdb.pop('klipper', None)
            await self.updaters['klipper'].initialize()
            await self.updaters['klipper'].refresh()
        if notify:
            self.cmd_helper.notify_update_refreshed()

    async def _handle_auto_refresh(self, eventtime: float) -> float:
        cur_hour = time.localtime(time.time()).tm_hour
        log_remaining_time = True
        if self.initial_refresh_complete:
            log_remaining_time = False
            # Update when the local time is between 12AM and 5AM
            if cur_hour >= MAX_UPDATE_HOUR:
                return eventtime + UPDATE_REFRESH_INTERVAL
            if self.kconn.is_printing():
                # Don't Refresh during a print
                logging.info("Klippy is printing, auto refresh aborted")
                return eventtime + UPDATE_REFRESH_INTERVAL
        need_notify = False
        machine: Machine = self.server.lookup_component("machine")
        if machine.validation_enabled():
            logging.info(
                "update_manger: Install validation pending, bypassing "
                "initial refresh"
            )
            self.initial_refresh_complete = True
            return eventtime + UPDATE_REFRESH_INTERVAL
        async with self.cmd_request_lock:
            try:
                for name, updater in list(self.updaters.items()):
                    if updater.needs_refresh(log_remaining_time):
                        await updater.refresh()
                        need_notify = True
            except Exception:
                logging.exception("Unable to Refresh Status")
                return eventtime + UPDATE_REFRESH_INTERVAL
            finally:
                self.initial_refresh_complete = True
        if need_notify:
            self.cmd_helper.notify_update_refreshed()
        return eventtime + UPDATE_REFRESH_INTERVAL

    async def _handle_update_request(self,
                                     web_request: WebRequest
                                     ) -> str:
        if self.kconn.is_printing():
            raise self.server.error("Update Refused: Klippy is printing")
        app: str = web_request.get_endpoint().split("/")[-1]
        if app == "client":
            app = web_request.get_str('name')
        if self.cmd_helper.is_app_updating(app):
            return f"Object {app} is currently being updated"
        updater = self.updaters.get(app, None)
        if updater is None:
            raise self.server.error(f"Updater {app} not available", 404)
        async with self.cmd_request_lock:
            self.cmd_helper.set_update_info(app, id(web_request))
            try:
                await updater.update()
            except Exception as e:
                self.cmd_helper.notify_update_response(
                    f"Error updating {app}: {e}", is_complete=True)
                raise
            finally:
                self.cmd_helper.clear_update_info()
        return "ok"

    async def _handle_full_update_request(self,
                                          web_request: WebRequest
                                          ) -> str:
        async with self.cmd_request_lock:
            app_name = ""
            self.cmd_helper.set_update_info('full', id(web_request))
            self.cmd_helper.notify_update_response(
                "Preparing full software update...")
            try:
                # Perform system updates
                if 'system' in self.updaters:
                    app_name = 'system'
                    await self.updaters['system'].update()

                # Update clients
                for name, updater in self.updaters.items():
                    if name in ['klipper', 'moonraker', 'system']:
                        continue
                    app_name = name
                    await updater.update()

                # Update Klipper
                app_name = 'klipper'
                kupdater = self.updaters.get('klipper')
                if isinstance(kupdater, AppDeploy):
                    self.klippy_identified_evt = asyncio.Event()
                    check_restart = await kupdater.update()
                    if self.cmd_helper.needs_service_restart(app_name):
                        await kupdater.restart_service()
                        check_restart = True
                    if check_restart:
                        self.cmd_helper.notify_update_response(
                            "Waiting for Klippy to reconnect (this may take"
                            " up to 2 minutes)...")
                        try:
                            await asyncio.wait_for(
                                self.klippy_identified_evt.wait(), 120.)
                        except asyncio.TimeoutError:
                            self.cmd_helper.notify_update_response(
                                "Klippy reconnect timed out...")
                        else:
                            self.cmd_helper.notify_update_response(
                                "Klippy Reconnected")
                        self.klippy_identified_evt = None

                # Update Moonraker
                app_name = 'moonraker'
                moon_updater = cast(AppDeploy, self.updaters["moonraker"])
                await moon_updater.update()
                if self.cmd_helper.needs_service_restart(app_name):
                    await moon_updater.restart_service()
                self.cmd_helper.set_full_complete(True)
                self.cmd_helper.notify_update_response(
                    "Full Update Complete", is_complete=True)
            except Exception as e:
                self.cmd_helper.set_full_complete(True)
                self.cmd_helper.notify_update_response(
                    f"Error updating {app_name}: {e}", is_complete=True)
            finally:
                self.cmd_helper.clear_update_info()
            return "ok"

    async def _handle_status_request(self,
                                     web_request: WebRequest
                                     ) -> Dict[str, Any]:
        check_refresh = web_request.get_boolean('refresh', False)
        # Override a request to refresh if:
        #   - An update is in progress
        #   - Klippy is printing
        #   - Validation is pending
        machine: Machine = self.server.lookup_component("machine")
        if (
            machine.validation_enabled() or
            self.cmd_helper.is_update_busy() or
            self.kconn.is_printing() or
            not self.initial_refresh_complete
        ):
            if check_refresh:
                logging.info("update_manager: bypassing refresh request")
            check_refresh = False

        if check_refresh:
            # Acquire the command request lock if we want force a refresh
            await self.cmd_request_lock.acquire()
            # Now that we have acquired the lock reject attempts to spam
            # the refresh request.
            lrt = max([upd.get_last_refresh_time()
                       for upd in self.updaters.values()])
            if time.time() < lrt + 60.:
                logging.debug("update_manager: refresh bypassed due to spam")
                check_refresh = False
                self.cmd_request_lock.release()
        vinfo: Dict[str, Any] = {}
        try:
            for name, updater in list(self.updaters.items()):
                if check_refresh:
                    await updater.refresh()
                vinfo[name] = updater.get_update_status()
        except Exception:
            raise
        finally:
            if check_refresh:
                self.cmd_request_lock.release()
        ret = self.cmd_helper.get_rate_limit_stats()
        ret['version_info'] = vinfo
        ret['busy'] = self.cmd_helper.is_update_busy()
        if check_refresh:
            event_loop = self.server.get_event_loop()
            event_loop.delay_callback(
                .2, self.cmd_helper.notify_update_refreshed
            )
        return ret

    async def _handle_refresh_request(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:
        name: Optional[str] = web_request.get_str("name", None)
        if name is not None and name not in self.updaters:
            raise self.server.error(f"No updater registered for '{name}'")
        machine: Machine = self.server.lookup_component("machine")
        if (
            machine.validation_enabled() or
            self.cmd_helper.is_update_busy() or
            self.kconn.is_printing() or
            not self.initial_refresh_complete
        ):
            raise self.server.error(
                "Server is busy, cannot perform refresh", 503
            )
        async with self.cmd_request_lock:
            vinfo: Dict[str, Any] = {}
            for updater_name, updater in list(self.updaters.items()):
                if name is None or updater_name == name:
                    await updater.refresh()
                vinfo[updater_name] = updater.get_update_status()
            ret = self.cmd_helper.get_rate_limit_stats()
            ret['version_info'] = vinfo
            ret['busy'] = self.cmd_helper.is_update_busy()
            event_loop = self.server.get_event_loop()
            event_loop.delay_callback(
                .2, self.cmd_helper.notify_update_refreshed
            )
        return ret

    async def _handle_repo_recovery(self, web_request: WebRequest) -> str:
        if self.kconn.is_printing():
            raise self.server.error(
                "Recovery Attempt Refused: Klippy is printing")
        app: str = web_request.get_str('name')
        hard = web_request.get_boolean("hard", False)
        update_deps = web_request.get_boolean("update_deps", False)
        updater = self.updaters.get(app, None)
        if updater is None:
            raise self.server.error(f"Updater {app} not available", 404)
        elif not isinstance(updater, GitDeploy):
            raise self.server.error(f"Upater {app} is not a Git Repo Type")
        async with self.cmd_request_lock:
            self.cmd_helper.set_update_info(f"recover_{app}", id(web_request))
            try:
                await updater.recover(hard, update_deps)
            except Exception as e:
                self.cmd_helper.notify_update_response(
                    f"Error Recovering {app}")
                self.cmd_helper.notify_update_response(
                    str(e), is_complete=True)
                raise
            finally:
                self.cmd_helper.clear_update_info()
        return "ok"

    async def _handle_rollback(self, web_request: WebRequest) -> str:
        if self.kconn.is_printing():
            raise self.server.error("Rollback Attempt Refused: Klippy is printing")
        app: str = web_request.get_str('name')
        updater = self.updaters.get(app, None)
        if updater is None:
            raise self.server.error(f"Updater {app} not available", 404)
        async with self.cmd_request_lock:
            self.cmd_helper.set_update_info(f"rollback_{app}", id(web_request))
            try:
                await updater.rollback()
            except Exception as e:
                self.cmd_helper.notify_update_response(f"Error Rolling Back {app}")
                self.cmd_helper.notify_update_response(str(e), is_complete=True)
                raise
            finally:
                self.cmd_helper.clear_update_info()
        return "ok"

    async def close(self) -> None:
        if self.refresh_timer is not None:
            self.refresh_timer.stop()
        for updater in self.updaters.values():
            ret = updater.close()
            if ret is not None:
                await ret

class CommandHelper:
    def __init__(
        self,
        config: ConfigHelper,
        get_updater_cb: Callable[[], Dict[str, BaseDeploy]]
    ) -> None:
        self.server = config.get_server()
        self.get_updaters = get_updater_cb
        self.http_client: HttpClient
        self.http_client = self.server.lookup_component("http_client")
        config.getboolean('enable_repo_debug', False, deprecate=True)
        if self.server.is_debug_enabled():
            logging.warning("UPDATE MANAGER: REPO DEBUG ENABLED")
        shell_cmd: SCMDComp = self.server.lookup_component('shell_command')
        self.scmd_error = shell_cmd.error
        self.build_shell_command = shell_cmd.build_shell_command
        self.run_cmd_with_response = shell_cmd.exec_cmd
        self.pkg_updater: Optional[PackageDeploy] = None

        # database management
        db: DBComp = self.server.lookup_component('database')
        db.register_local_namespace("update_manager")
        self.umdb = db.wrap_namespace("update_manager")

        # Refresh Time Tracking (default is to refresh every 7 days)
        reresh_interval = config.getint('refresh_interval', 168)
        # Convert to seconds
        self.refresh_interval = reresh_interval * 60 * 60

        # GitHub API Rate Limit Tracking
        self.gh_rate_limit: Optional[int] = None
        self.gh_limit_remaining: Optional[int] = None
        self.gh_limit_reset_time: Optional[float] = None

        # Update In Progress Tracking
        self.cur_update_app: Optional[str] = None
        self.cur_update_id: Optional[int] = None
        self.full_update: bool = False
        self.full_complete: bool = False
        self.pending_service_restarts: Set[str] = set()

    def get_server(self) -> Server:
        return self.server

    def get_http_client(self) -> HttpClient:
        return self.http_client

    def get_refresh_interval(self) -> float:
        return self.refresh_interval

    def get_umdb(self) -> NamespaceWrapper:
        return self.umdb

    def set_update_info(self, app: str, uid: int) -> None:
        self.cur_update_app = app
        self.cur_update_id = uid
        self.full_update = app == "full"
        self.full_complete = not self.full_update
        self.pending_service_restarts.clear()

    def is_full_update(self) -> bool:
        return self.full_update

    def add_pending_restart(self, svc_name: str) -> None:
        self.pending_service_restarts.add(svc_name)

    def remove_pending_restart(self, svc_name: str) -> None:
        if svc_name in self.pending_service_restarts:
            self.pending_service_restarts.remove(svc_name)

    def set_full_complete(self, complete: bool = False):
        self.full_complete = complete

    def clear_update_info(self) -> None:
        self.cur_update_app = self.cur_update_id = None
        self.full_update = False
        self.full_complete = False
        self.pending_service_restarts.clear()

    def needs_service_restart(self, svc_name: str) -> bool:
        return svc_name in self.pending_service_restarts

    def is_app_updating(self, app_name: str) -> bool:
        return self.cur_update_app == app_name

    def is_update_busy(self) -> bool:
        return self.cur_update_app is not None

    def set_package_updater(self, updater: PackageDeploy) -> None:
        self.pkg_updater = updater

    async def run_cmd(
        self,
        cmd: str,
        timeout: float = 20.,
        notify: bool = False,
        retries: int = 1,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        sig_idx: int = 1
    ) -> None:
        cb = self.notify_update_response if notify else None
        scmd = self.build_shell_command(cmd, callback=cb, env=env, cwd=cwd)
        for _ in range(retries):
            if await scmd.run(timeout=timeout, sig_idx=sig_idx):
                break
        else:
            raise self.server.error("Shell Command Error")

    def notify_update_refreshed(self) -> None:
        vinfo: Dict[str, Any] = {}
        for name, updater in self.get_updaters().items():
            vinfo[name] = updater.get_update_status()
        uinfo = self.get_rate_limit_stats()
        uinfo['version_info'] = vinfo
        uinfo['busy'] = self.is_update_busy()
        self.server.send_event("update_manager:update_refreshed", uinfo)

    def notify_update_response(
        self, resp: Union[str, bytes], is_complete: bool = False
    ) -> None:
        if self.cur_update_app is None:
            return
        resp = resp.strip()
        if isinstance(resp, bytes):
            resp = resp.decode()
        done = is_complete
        if self.full_update:
            done &= self.full_complete
        notification = {
            'message': resp,
            'application': self.cur_update_app,
            'proc_id': self.cur_update_id,
            'complete': done}
        self.server.send_event(
            "update_manager:update_response", notification)

    async def install_packages(
        self, package_list: List[str], **kwargs
    ) -> None:
        if self.pkg_updater is None:
            return
        await self.pkg_updater.install_packages(package_list, **kwargs)

    def get_rate_limit_stats(self) -> Dict[str, Any]:
        return self.http_client.github_api_stats()

    def on_download_progress(
        self, progress: int, download_size: int, downloaded: int
    ) -> None:
        totals = (
            f"{downloaded // 1024} KiB / "
            f"{download_size // 1024} KiB"
        )
        self.notify_update_response(
            f"Downloading {self.cur_update_app}: {totals} [{progress}%]")

    async def create_tempdir(
        self, suffix: Optional[str] = None, prefix: Optional[str] = None
    ) -> tempfile.TemporaryDirectory[str]:
        def _createdir(sfx, pfx):
            return tempfile.TemporaryDirectory(suffix=sfx, prefix=pfx)

        eventloop = self.server.get_event_loop()
        return await eventloop.run_in_thread(_createdir, suffix, prefix)

class PackageDeploy(BaseDeploy):
    def __init__(self,
                 config: ConfigHelper,
                 cmd_helper: CommandHelper
                 ) -> None:
        super().__init__(config, cmd_helper, "system", "", "")
        cmd_helper.set_package_updater(self)
        self.use_packagekit = config.getboolean("enable_packagekit", True)
        self.available_packages: List[str] = []

    async def initialize(self) -> Dict[str, Any]:
        storage = await super().initialize()
        self.available_packages = storage.get('packages', [])
        provider: BasePackageProvider
        try_fallback = True
        if self.use_packagekit:
            try:
                provider = PackageKitProvider(self.cmd_helper)
                await provider.initialize()
            except Exception:
                pass
            else:
                self.log_info("PackageDeploy: PackageKit Provider Configured")
                self.prefix = "PackageKit: "
                try_fallback = False
        if try_fallback:
            # Check to see of the apt command is available
            fallback = await self._get_fallback_provider()
            if fallback is None:
                provider = BasePackageProvider(self.cmd_helper)
                machine: Machine = self.server.lookup_component("machine")
                dist_info = machine.get_system_info()['distribution']
                dist_id: str = dist_info['id'].lower()
                self.server.add_warning(
                    "Unable to initialize System Update Provider for "
                    f"distribution: {dist_id}")
            else:
                self.log_info("PackageDeploy: Using APT CLI Provider")
                self.prefix = "Package Manager APT: "
                provider = fallback
        self.provider = provider
        return storage

    async def _get_fallback_provider(self) -> Optional[BasePackageProvider]:
        # Currently only the API Fallback provider is available
        shell_cmd: SCMDComp
        shell_cmd = self.server.lookup_component("shell_command")
        cmd = shell_cmd.build_shell_command("sh -c 'command -v apt'")
        try:
            ret = await cmd.run_with_response()
        except shell_cmd.error:
            return None
        # APT Command found should be available
        self.log_debug(f"APT package manager detected: {ret}")
        provider = AptCliProvider(self.cmd_helper)
        try:
            await provider.initialize()
        except Exception:
            return None
        return provider

    async def refresh(self) -> None:
        try:
            # Do not force a refresh until the server has started
            if self.server.is_running():
                await self._update_package_cache(force=True)
            self.available_packages = await self.provider.get_packages()
            pkg_msg = "\n".join(self.available_packages)
            self.log_info(
                f"Detected {len(self.available_packages)} package updates:"
                f"\n{pkg_msg}"
            )
        except Exception:
            self.log_exc("Error Refreshing System Packages")
        # Update Persistent Storage
        self._save_state()

    def get_persistent_data(self) -> Dict[str, Any]:
        storage = super().get_persistent_data()
        storage['packages'] = self.available_packages
        return storage

    async def update(self) -> bool:
        if not self.available_packages:
            return False
        self.cmd_helper.notify_update_response("Updating packages...")
        try:
            await self._update_package_cache(force=True, notify=True)
            await self.provider.upgrade_system()
        except Exception:
            raise self.server.error("Error updating system packages")
        self.available_packages = []
        self._save_state()
        self.cmd_helper.notify_update_response(
            "Package update finished...", is_complete=True)
        return True

    async def _update_package_cache(self,
                                    force: bool = False,
                                    notify: bool = False
                                    ) -> None:
        curtime = time.time()
        if force or curtime > self.last_refresh_time + 3600.:
            # Don't update if a request was done within the last hour
            await self.provider.refresh_packages(notify)

    async def install_packages(self,
                               package_list: List[str],
                               **kwargs
                               ) -> None:
        await self.provider.install_packages(package_list, **kwargs)

    def get_update_status(self) -> Dict[str, Any]:
        return {
            'package_count': len(self.available_packages),
            'package_list': self.available_packages
        }

class BasePackageProvider:
    def __init__(self, cmd_helper: CommandHelper) -> None:
        self.server = cmd_helper.get_server()
        self.cmd_helper = cmd_helper

    async def initialize(self) -> None:
        pass

    async def refresh_packages(self, notify: bool = False) -> None:
        raise self.server.error("Cannot refresh packages, no provider set")

    async def get_packages(self) -> List[str]:
        raise self.server.error("Cannot retrieve packages, no provider set")

    async def install_packages(self,
                               package_list: List[str],
                               **kwargs
                               ) -> None:
        raise self.server.error("Cannot install packages, no provider set")

    async def upgrade_system(self) -> None:
        raise self.server.error("Cannot upgrade packages, no provider set")

class AptCliProvider(BasePackageProvider):
    APT_CMD = "sudo DEBIAN_FRONTEND=noninteractive apt-get"

    async def refresh_packages(self, notify: bool = False) -> None:
        await self.cmd_helper.run_cmd(
            f"{self.APT_CMD} update", timeout=600., notify=notify)

    async def get_packages(self) -> List[str]:
        res = await self.cmd_helper.run_cmd_with_response(
            "apt list --upgradable", timeout=60.)
        pkg_list = [p.strip() for p in res.split("\n") if p.strip()]
        if pkg_list:
            pkg_list = pkg_list[2:]
            return [p.split("/", maxsplit=1)[0] for p in pkg_list]
        return []

    async def resolve_packages(self, package_list: List[str]) -> List[str]:
        self.cmd_helper.notify_update_response("Resolving packages...")
        search_regex = "|".join([f"^{pkg}$" for pkg in package_list])
        cmd = f"apt-cache search --names-only \"{search_regex}\""
        ret = await self.cmd_helper.run_cmd_with_response(cmd, timeout=600.)
        resolved = [
            pkg.strip().split()[0] for pkg in ret.split("\n") if pkg.strip()
        ]
        return [avail for avail in package_list if avail in resolved]

    async def install_packages(self,
                               package_list: List[str],
                               **kwargs
                               ) -> None:
        timeout: float = kwargs.get('timeout', 300.)
        retries: int = kwargs.get('retries', 3)
        notify: bool = kwargs.get('notify', False)
        await self.refresh_packages(notify=notify)
        resolved = await self.resolve_packages(package_list)
        if not resolved:
            self.cmd_helper.notify_update_response("No packages detected")
            return
        logging.debug(f"Resolved packages: {resolved}")
        pkgs = " ".join(resolved)
        await self.cmd_helper.run_cmd(
            f"{self.APT_CMD} install --yes {pkgs}", timeout=timeout,
            retries=retries, notify=notify)

    async def upgrade_system(self) -> None:
        await self.cmd_helper.run_cmd(
            f"{self.APT_CMD} upgrade --yes", timeout=3600.,
            notify=True)

class PackageKitProvider(BasePackageProvider):
    def __init__(self, cmd_helper: CommandHelper) -> None:
        super().__init__(cmd_helper)
        dbus_mgr: DbusManager = self.server.lookup_component("dbus_manager")
        self.dbus_mgr = dbus_mgr
        self.pkgkit: Optional[ProxyInterface] = None

    async def initialize(self) -> None:
        if not self.dbus_mgr.is_connected():
            raise self.server.error("DBus Connection Not available")
        # Check for PolicyKit permissions
        await self.dbus_mgr.check_permission(
            "org.freedesktop.packagekit.system-sources-refresh",
            "The Update Manager will fail to fetch package updates")
        await self.dbus_mgr.check_permission(
            "org.freedesktop.packagekit.package-install",
            "The Update Manager will fail to install packages")
        await self.dbus_mgr.check_permission(
            "org.freedesktop.packagekit.system-update",
            "The Update Manager will fail to update packages"
        )
        # Fetch the PackageKit DBus Inteface
        self.pkgkit = await self.dbus_mgr.get_interface(
            "org.freedesktop.PackageKit",
            "/org/freedesktop/PackageKit",
            "org.freedesktop.PackageKit")

    async def refresh_packages(self, notify: bool = False) -> None:
        await self.run_transaction("refresh_cache", False, notify=notify)

    async def get_packages(self) -> List[str]:
        flags = PkEnum.Filter.NONE
        pkgs = await self.run_transaction("get_updates", flags.value)
        pkg_ids = [info['package_id'] for info in pkgs if 'package_id' in info]
        return [pkg_id.split(";")[0] for pkg_id in pkg_ids]

    async def install_packages(self,
                               package_list: List[str],
                               **kwargs
                               ) -> None:
        notify: bool = kwargs.get('notify', False)
        await self.refresh_packages(notify=notify)
        flags = (
            PkEnum.Filter.NEWEST | PkEnum.Filter.NOT_INSTALLED |
            PkEnum.Filter.BASENAME | PkEnum.Filter.ARCH
        )
        pkgs = await self.run_transaction("resolve", flags.value, package_list)
        pkg_ids = [info['package_id'] for info in pkgs if 'package_id' in info]
        if pkg_ids:
            logging.debug(f"Installing Packages: {pkg_ids}")
            tflag = PkEnum.TransactionFlag.ONLY_TRUSTED
            await self.run_transaction("install_packages", tflag.value,
                                       pkg_ids, notify=notify)

    async def upgrade_system(self) -> None:
        # Get Updates, Install Packages
        flags = PkEnum.Filter.NONE
        pkgs = await self.run_transaction("get_updates", flags.value)
        pkg_ids = [info['package_id'] for info in pkgs if 'package_id' in info]
        if pkg_ids:
            logging.debug(f"Upgrading Packages: {pkg_ids}")
            tflag = PkEnum.TransactionFlag.ONLY_TRUSTED
            await self.run_transaction("update_packages", tflag.value,
                                       pkg_ids, notify=True)

    def create_transaction(self) -> PackageKitTransaction:
        if self.pkgkit is None:
            raise self.server.error("PackageKit Interface Not Available")
        return PackageKitTransaction(self.dbus_mgr, self.pkgkit,
                                     self.cmd_helper)

    async def run_transaction(self,
                              method: str,
                              *args,
                              notify: bool = False
                              ) -> Any:
        transaction = self.create_transaction()
        return await transaction.run(method, *args, notify=notify)

class PackageKitTransaction:
    GET_PKG_ROLES = (
        PkEnum.Role.RESOLVE | PkEnum.Role.GET_PACKAGES |
        PkEnum.Role.GET_UPDATES
    )
    QUERY_ROLES = GET_PKG_ROLES | PkEnum.Role.GET_REPO_LIST
    PROGRESS_STATUS = (
        PkEnum.Status.RUNNING | PkEnum.Status.INSTALL |
        PkEnum.Status.UPDATE
    )

    def __init__(self,
                 dbus_mgr: DbusManager,
                 pkgkit: ProxyInterface,
                 cmd_helper: CommandHelper
                 ) -> None:
        self.server = cmd_helper.get_server()
        self.eventloop = self.server.get_event_loop()
        self.cmd_helper = cmd_helper
        self.dbus_mgr = dbus_mgr
        self.pkgkit = pkgkit
        # Transaction Properties
        self.notify = False
        self._status = PkEnum.Status.UNKNOWN
        self._role = PkEnum.Role.UNKNOWN
        self._tflags = PkEnum.TransactionFlag.NONE
        self._percentage = 101
        self._dl_remaining = 0
        self.speed = 0
        self.elapsed_time = 0
        self.remaining_time = 0
        self.caller_active = False
        self.allow_cancel = True
        self.uid = 0
        # Transaction data tracking
        self.tfut: Optional[asyncio.Future] = None
        self.last_progress_notify_time: float = 0.
        self.result: List[Dict[str, Any]] = []
        self.err_msg: str = ""

    def run(self,
            method: str,
            *args,
            notify: bool = False
            ) -> Awaitable:
        if self.tfut is not None:
            raise self.server.error(
                "PackageKit transaction can only be used once")
        self.notify = notify
        self.tfut = self.eventloop.create_future()
        coro = self._start_transaction(method, *args)
        self.eventloop.create_task(coro)
        return self.tfut

    async def _start_transaction(self,
                                 method: str,
                                 *args
                                 ) -> None:
        assert self.tfut is not None
        try:
            # Create Transaction
            tid = await self.pkgkit.call_create_transaction()  # type: ignore
            transaction, props = await self.dbus_mgr.get_interfaces(
                "org.freedesktop.PackageKit", tid,
                ["org.freedesktop.PackageKit.Transaction",
                 "org.freedesktop.DBus.Properties"])
            # Set interface callbacks
            transaction.on_package(self._on_package_signal)    # type: ignore
            transaction.on_repo_detail(                        # type: ignore
                self._on_repo_detail_signal)
            transaction.on_item_progress(                      # type: ignore
                self._on_item_progress_signal)
            transaction.on_error_code(self._on_error_signal)   # type: ignore
            transaction.on_finished(self._on_finished_signal)  # type: ignore
            props.on_properties_changed(                       # type: ignore
                self._on_properties_changed)
            # Run method
            logging.debug(f"PackageKit: Running transaction call_{method}")
            func = getattr(transaction, f"call_{method}")
            await func(*args)
        except Exception as e:
            self.tfut.set_exception(e)

    def _on_package_signal(self,
                           info_code: int,
                           package_id: str,
                           summary: str
                           ) -> None:
        info = PkEnum.Info.from_index(info_code)
        if self._role in self.GET_PKG_ROLES:
            pkg_data = {
                'package_id': package_id,
                'info': info.desc,
                'summary': summary
            }
            self.result.append(pkg_data)
        else:
            self._notify_package(info, package_id)

    def _on_repo_detail_signal(self,
                               repo_id: str,
                               description: str,
                               enabled: bool
                               ) -> None:
        if self._role == PkEnum.Role.GET_REPO_LIST:
            repo_data = {
                "repo_id": repo_id,
                "description": description,
                "enabled": enabled
            }
            self.result.append(repo_data)
        else:
            self._notify_repo(repo_id, description)

    def _on_item_progress_signal(self,
                                 item_id: str,
                                 status_code: int,
                                 percent_complete: int
                                 ) -> None:
        status = PkEnum.Status.from_index(status_code)
        # NOTE: This signal doesn't seem to fire predictably,
        # nor does it seem to provide a consistent "percent complete"
        # parameter.
        # logging.debug(
        #    f"Role {self._role.name}: Item Progress Signal Received\n"
        #    f"Item ID: {item_id}\n"
        #    f"Percent Complete: {percent_complete}\n"
        #    f"Status: {status.desc}")

    def _on_error_signal(self,
                         error_code: int,
                         details: str
                         ) -> None:
        err = PkEnum.Error.from_index(error_code)
        self.err_msg = f"{err.name}: {details}"

    def _on_finished_signal(self, exit_code: int, run_time: int) -> None:
        if self.tfut is None:
            return
        ext = PkEnum.Exit.from_index(exit_code)
        secs = run_time / 1000.
        if ext == PkEnum.Exit.SUCCESS:
            self.tfut.set_result(self.result)
        else:
            err = self.err_msg or ext.desc
            server = self.cmd_helper.get_server()
            self.tfut.set_exception(server.error(err))
        msg = f"Transaction {self._role.desc}: Exit {ext.desc}, " \
              f"Run time: {secs:.2f} seconds"
        if self.notify:
            self.cmd_helper.notify_update_response(msg)
        logging.debug(msg)

    def _on_properties_changed(self,
                               iface_name: str,
                               changed_props: Dict[str, Variant],
                               invalid_props: Dict[str, Variant]
                               ) -> None:
        for name, var in changed_props.items():
            formatted = re.sub(r"(\w)([A-Z])", r"\g<1>_\g<2>", name).lower()
            setattr(self, formatted, var.value)

    def _notify_package(self, info: PkEnum.Info, package_id: str) -> None:
        if self.notify:
            if info == PkEnum.Info.FINISHED:
                return
            pkg_parts = package_id.split(";")
            msg = f"{info.desc}: {pkg_parts[0]} ({pkg_parts[1]})"
            self.cmd_helper.notify_update_response(msg)

    def _notify_repo(self, repo_id: str, description: str) -> None:
        if self.notify:
            if not repo_id.strip():
                repo_id = description
            # TODO: May want to eliminate dups
            msg = f"GET: {repo_id}"
            self.cmd_helper.notify_update_response(msg)

    def _notify_progress(self) -> None:
        if self.notify and self._percentage <= 100:
            msg = f"{self._status.desc}...{self._percentage}%"
            if self._status == PkEnum.Status.DOWNLOAD and self._dl_remaining:
                if self._dl_remaining < 1024:
                    msg += f", Remaining: {self._dl_remaining} B"
                elif self._dl_remaining < 1048576:
                    msg += f", Remaining: {self._dl_remaining // 1024} KiB"
                else:
                    msg += f", Remaining: {self._dl_remaining // 1048576} MiB"
                if self.speed:
                    speed = self.speed // 8
                    if speed < 1024:
                        msg += f", Speed: {speed} B/s"
                    elif speed < 1048576:
                        msg += f", Speed: {speed // 1024} KiB/s"
                    else:
                        msg += f", Speed: {speed // 1048576} MiB/s"
            self.cmd_helper.notify_update_response(msg)

    @property
    def role(self) -> PkEnum.Role:
        return self._role

    @role.setter
    def role(self, role_code: int) -> None:
        self._role = PkEnum.Role.from_index(role_code)
        if self._role in self.QUERY_ROLES:
            # Never Notify Queries
            self.notify = False
        if self.notify:
            msg = f"Transaction {self._role.desc} started..."
            self.cmd_helper.notify_update_response(msg)
        logging.debug(f"PackageKit: Current Role: {self._role.desc}")

    @property
    def status(self) -> PkEnum.Status:
        return self._status

    @status.setter
    def status(self, status_code: int) -> None:
        self._status = PkEnum.Status.from_index(status_code)
        self._percentage = 101
        self.speed = 0
        logging.debug(f"PackageKit: Current Status: {self._status.desc}")

    @property
    def transaction_flags(self) -> PkEnum.TransactionFlag:
        return self._tflags

    @transaction_flags.setter
    def transaction_flags(self, bits: int) -> None:
        self._tflags = PkEnum.TransactionFlag(bits)

    @property
    def percentage(self) -> int:
        return self._percentage

    @percentage.setter
    def percentage(self, percent: int) -> None:
        self._percentage = percent
        if self._status in self.PROGRESS_STATUS:
            self._notify_progress()

    @property
    def download_size_remaining(self) -> int:
        return self._dl_remaining

    @download_size_remaining.setter
    def download_size_remaining(self, bytes_remaining: int) -> None:
        self._dl_remaining = bytes_remaining
        self._notify_progress()


class WebClientDeploy(BaseDeploy):
    def __init__(self,
                 config: ConfigHelper,
                 cmd_helper: CommandHelper
                 ) -> None:
        super().__init__(config, cmd_helper, prefix="Web Client")
        self.repo = config.get('repo').strip().strip("/")
        self.owner, self.project_name = self.repo.split("/", 1)
        self.path = pathlib.Path(config.get("path")).expanduser().resolve()
        self.type = AppType.from_string(config.get('type'))
        self.channel = Channel.from_string(config.get("channel", "stable"))
        if self.channel == Channel.DEV:
            self.server.add_warning(
                f"Invalid Channel '{self.channel}' for config "
                f"section [{config.get_name()}], type: {self.type}. "
                f"Must be one of the following: stable, beta. "
                f"Falling back to beta channel"
            )
            self.channel = Channel.BETA
        self.info_tags: List[str] = config.getlist("info_tags", [])
        self.persistent_files: List[str] = []
        self.warnings: List[str] = []
        self.version: str = "?"
        pfiles = config.getlist('persistent_files', None)
        if pfiles is not None:
            self.persistent_files = [pf.strip("/") for pf in pfiles]
            if ".version" in self.persistent_files:
                raise config.error(
                    "Invalid value for option 'persistent_files': "
                    "'.version' can not be persistent")
        self._valid: bool = True
        self._is_prerelease: bool = False
        self._is_fallback: bool = False
        self._path_writable: bool = False

    async def _validate_client_info(self) -> None:
        self._valid = False
        self._is_fallback = False
        eventloop = self.server.get_event_loop()
        self.warnings.clear()
        repo_parent = source_info.find_git_repo(self.path)
        homedir = pathlib.Path("~").expanduser()
        if not self._path_writable:
            self.warnings.append(
                f"Location at option 'path: {self.path}' is not writable."
            )
        elif not self.path.is_dir():
            self.warnings.append(
                f"Location at option 'path: {self.path}' is not a directory."
            )
        elif repo_parent is not None and repo_parent != homedir:
            self.warnings.append(
                f"Location at option 'path: {self.path}' is within a git repo. Found "
                f".git folder at '{repo_parent.joinpath('.git')}'"
            )
        else:
            rinfo = self.path.joinpath("release_info.json")
            if rinfo.is_file():
                try:
                    data = await eventloop.run_in_thread(rinfo.read_text)
                    uinfo: Dict[str, str] = json.loads(data)
                    project_name = uinfo["project_name"]
                    owner = uinfo["project_owner"]
                    self.version = uinfo["version"]
                except Exception:
                    logging.exception("Failed to load release_info.json.")
                else:
                    self._valid = True
                    detected_repo = f"{owner}/{project_name}"
                    if self.repo.lower() != detected_repo.lower():
                        self.warnings.append(
                            f"Value at option 'repo: {self.repo}' does not match "
                            f"detected repo '{detected_repo}', falling back to "
                            "detected version."
                        )
                        self.repo = detected_repo
                        self.owner = owner
                        self.project_name = project_name
            else:
                version_path = self.path.joinpath(".version")
                if version_path.is_file():
                    version = await eventloop.run_in_thread(version_path.read_text)
                    self.version = version.strip()
                self._valid = await self._detect_fallback()
        if not self._valid:
            self.warnings.append("Failed to validate client installation")
            if self.server.is_debug_enabled():
                self.log_info("Debug Enabled, overriding validity checks")

    async def _detect_fallback(self) -> bool:
        fallback_defs = {
            "mainsail": "mainsail-crew",
            "fluidd": "fluidd-core"
        }
        for fname in ("manifest.json", "manifest.webmanifest"):
            manifest = self.path.joinpath(fname)
            eventloop = self.server.get_event_loop()
            if manifest.is_file():
                try:
                    mtext = await eventloop.run_in_thread(manifest.read_text)
                    mdata: Dict[str, Any] = json.loads(mtext)
                    proj_name: str = mdata["name"].lower()
                except Exception:
                    self.log_exc(f"Failed to load json from {manifest}")
                    continue
                if proj_name in fallback_defs:
                    owner = fallback_defs[proj_name]
                    detected_repo = f"{owner}/{proj_name}"
                    if detected_repo != self.repo.lower():
                        self.warnings.append(
                            f"Value at option 'repo: {self.repo}' does not match "
                            f"detected repo '{detected_repo}', falling back to "
                            "detected version."
                        )
                        self.repo = detected_repo
                        self.owner = owner
                        self.project_name = proj_name
                    self._is_fallback = True
                    return True
        return False

    async def initialize(self) -> Dict[str, Any]:
        fm: FileManager = self.server.lookup_component("file_manager")
        self._path_writable = not fm.check_reserved_path(
            self.path, need_write=True, raise_error=False
        )
        if self._path_writable:
            fm.add_reserved_path(f"update_manager {self.name}", self.path)
        await self._validate_client_info()
        storage = await super().initialize()
        if self.version == "?":
            self.version = storage.get("version", "?")
        self.remote_version: str = storage.get('remote_version', "?")
        self.rollback_version: str = storage.get('rollback_version', self.version)
        self.rollback_repo: str = storage.get(
            'rollback_repo', self.repo if self._valid else "?"
        )
        self.last_error: str = storage.get('last_error', "")
        dl_info: List[Any] = storage.get('dl_info', ["?", "?", 0])
        self.dl_info: Tuple[str, str, int] = cast(
            Tuple[str, str, int], tuple(dl_info))
        if not self.needs_refresh():
            self._log_client_info()
        return storage

    def _log_client_info(self) -> None:
        warn_str = ""
        if self.warnings:
            warn_str = "\nWarnings:\n"
            warn_str += "\n".join([f" {item}" for item in self.warnings])
        dl_url, content_type, size = self.dl_info
        logging.info(
            f"Web Client {self.name} Detected:\n"
            f"Repo: {self.repo}\n"
            f"Channel: {self.channel}\n"
            f"Path: {self.path}\n"
            f"Local Version: {self.version}\n"
            f"Remote Version: {self.remote_version}\n"
            f"Valid: {self._valid}\n"
            f"Fallback Client Detected: {self._is_fallback}\n"
            f"Pre-release: {self._is_prerelease}\n"
            f"Download Url: {dl_url}\n"
            f"Download Size: {size}\n"
            f"Content Type: {content_type}\n"
            f"Rollback Version: {self.rollback_version}\n"
            f"Rollback Repo: {self.rollback_repo}"
            f"{warn_str}"
        )

    async def refresh(self) -> None:
        try:
            if not self._valid:
                await self._validate_client_info()
            await self._get_remote_version()
        except Exception:
            logging.exception("Error Refreshing Client")
        self._log_client_info()
        self._save_state()

    async def _fetch_github_version(
        self, repo: Optional[str] = None, tag: Optional[str] = None
    ) -> Dict[str, Any]:
        if repo is None:
            if not self._valid:
                self.log_info("Invalid Web Installation, aborting remote refresh")
                return {}
            repo = self.repo
        if tag is not None:
            resource = f"repos/{repo}/releases/tags/{tag}"
        elif self.channel == Channel.STABLE:
            resource = f"repos/{repo}/releases/latest"
        else:
            resource = f"repos/{repo}/releases?per_page=1"
        client = self.cmd_helper.get_http_client()
        resp = await client.github_api_request(
            resource, attempts=3, retry_pause_time=.5
        )
        release: Union[List[Any], Dict[str, Any]] = {}
        if resp.status_code == 304:
            if resp.content:
                # Not modified, however we need to restore state from
                # cached content
                release = resp.json()
            else:
                # Either not necessary or not possible to restore from cache
                return {}
        elif resp.has_error():
            self.log_info(f"Github Request Error - {resp.error}")
            self.last_error = str(resp.error)
            return {}
        else:
            release = resp.json()
        result: Dict[str, Any] = {}
        if isinstance(release, list):
            if release:
                result = release[0]
        else:
            result = release
        self.last_error = ""
        return result

    async def _get_remote_version(self) -> None:
        result = await self._fetch_github_version()
        if not result:
            return
        self.remote_version = result.get('name', "?")
        release_asset: Dict[str, Any] = result.get('assets', [{}])[0]
        dl_url: str = release_asset.get('browser_download_url', "?")
        content_type: str = release_asset.get('content_type', "?")
        size: int = release_asset.get('size', 0)
        self.dl_info = (dl_url, content_type, size)
        self._is_prerelease = result.get('prerelease', False)

    def get_persistent_data(self) -> Dict[str, Any]:
        storage = super().get_persistent_data()
        storage['version'] = self.version
        storage['remote_version'] = self.remote_version
        storage['rollback_version'] = self.rollback_version
        storage['rollback_repo'] = self.rollback_repo
        storage['dl_info'] = list(self.dl_info)
        storage['last_error'] = self.last_error
        return storage

    async def update(
        self, rollback_info: Optional[Tuple[str, str, int]] = None
    ) -> bool:
        if not self._valid:
            raise self.server.error(
                f"Web Client {self.name}: Invalid install detected, aborting update"
            )
        if rollback_info is not None:
            dl_url, content_type, size = rollback_info
            start_msg = f"Rolling Back Web Client {self.name}..."
        else:
            if self.remote_version == "?":
                await self._get_remote_version()
                if self.remote_version == "?":
                    raise self.server.error(
                        f"Client {self.repo}: Unable to locate update"
                    )
            dl_url, content_type, size = self.dl_info
            if self.version == self.remote_version:
                # Already up to date
                return False
            start_msg = f"Updating Web Client {self.name}..."
        if dl_url == "?":
            raise self.server.error(f"Client {self.repo}: Invalid download url")
        current_version = self.version
        event_loop = self.server.get_event_loop()
        self.cmd_helper.notify_update_response(start_msg)
        self.cmd_helper.notify_update_response(f"Downloading Client: {self.name}")
        td = await self.cmd_helper.create_tempdir(self.name, "client")
        try:
            tempdir = pathlib.Path(td.name)
            temp_download_file = tempdir.joinpath(f"{self.name}.zip")
            temp_persist_dir = tempdir.joinpath(self.name)
            client = self.cmd_helper.get_http_client()
            await client.download_file(
                dl_url, content_type, temp_download_file, size,
                self.cmd_helper.on_download_progress)
            self.cmd_helper.notify_update_response(
                f"Download Complete, extracting release to '{self.path}'")
            await event_loop.run_in_thread(
                self._extract_release, temp_persist_dir,
                temp_download_file)
        finally:
            await event_loop.run_in_thread(td.cleanup)
        self.version = self.remote_version
        await self._validate_client_info()
        if self._valid and rollback_info is None:
            self.rollback_version = current_version
            self.rollback_repo = self.repo
        msg = f"Client Update Finished: {self.name}"
        if rollback_info is not None:
            msg = f"Rollback Complete: {self.name}"
        self.cmd_helper.notify_update_response(msg, is_complete=True)
        self._log_client_info()
        self._save_state()
        return True

    async def rollback(self) -> bool:
        if self.rollback_version == "?" or self.rollback_repo == "?":
            raise self.server.error("Incomplete Rollback Data")
        if self.rollback_version == self.version:
            return False
        result = await self._fetch_github_version(
            self.rollback_repo, self.rollback_version
        )
        if not result:
            raise self.server.error("Failed to retrieve release asset data")
        release_asset: Dict[str, Any] = result.get('assets', [{}])[0]
        dl_url: str = release_asset.get('browser_download_url', "?")
        content_type: str = release_asset.get('content_type', "?")
        size: int = release_asset.get('size', 0)
        dl_info = (dl_url, content_type, size)
        return await self.update(dl_info)

    def _extract_release(self,
                         persist_dir: pathlib.Path,
                         release_file: pathlib.Path
                         ) -> None:
        if not persist_dir.exists():
            os.mkdir(persist_dir)
        if self.path.is_dir():
            # find and move persistent files
            for fname in os.listdir(self.path):
                src_path = self.path.joinpath(fname)
                if fname in self.persistent_files:
                    dest_dir = persist_dir.joinpath(fname).parent
                    os.makedirs(dest_dir, exist_ok=True)
                    shutil.move(str(src_path), str(dest_dir))
            shutil.rmtree(self.path)
        os.mkdir(self.path)
        with zipfile.ZipFile(release_file) as zf:
            zf.extractall(self.path)
        # Move temporary files back into
        for fname in os.listdir(persist_dir):
            src_path = persist_dir.joinpath(fname)
            dest_dir = self.path.joinpath(fname).parent
            os.makedirs(dest_dir, exist_ok=True)
            shutil.move(str(src_path), str(dest_dir))

    def get_update_status(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'owner': self.owner,
            'version': self.version,
            'remote_version': self.remote_version,
            'rollback_version': self.rollback_version,
            'configured_type': str(self.type),
            'channel': str(self.channel),
            'info_tags': self.info_tags,
            'last_error': self.last_error,
            'is_valid': self._valid,
            'warnings': self.warnings
        }

def load_component(config: ConfigHelper) -> UpdateManager:
    return UpdateManager(config)
