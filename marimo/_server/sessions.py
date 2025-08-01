# Copyright 2024 Marimo. All rights reserved.
"""Client session management

This module encapsulates session management: each client gets a unique session,
and each session wraps a Python kernel and a websocket connection through which
the kernel can send messages to the frontend. Sessions do not share kernels or
websockets.

In run mode, in which we may have many clients connected to the server, a
session is closed as soon as its websocket connection is severed. In edit mode,
in which we have at most one connected client, a session may be kept around
even if its socket is closed.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import os
import queue
import signal
import sys
import threading
import time
from multiprocessing import connection
from multiprocessing.queues import Queue as MPQueue
from pathlib import Path
from typing import Any, Optional, Union
from uuid import uuid4

from marimo import _loggers
from marimo._ast.cell import CellConfig
from marimo._cli.print import red
from marimo._config.manager import (
    MarimoConfigManager,
    MarimoConfigReader,
    ScriptConfigManager,
)
from marimo._config.settings import GLOBAL_SETTINGS
from marimo._messaging.ops import (
    FocusCell,
    MessageOperation,
    Reload,
    UpdateCellCodes,
    UpdateCellIdsRequest,
)
from marimo._messaging.types import KernelMessage
from marimo._output.formatters.formatters import register_formatters
from marimo._runtime import requests, runtime
from marimo._runtime.requests import (
    AppMetadata,
    CreationRequest,
    ExecuteMultipleRequest,
    ExecutionRequest,
    HTTPRequest,
    SerializedCLIArgs,
    SerializedQueryParams,
    SetUIElementValueRequest,
)
from marimo._server.exceptions import InvalidSessionException
from marimo._server.file_manager import AppFileManager
from marimo._server.file_router import AppFileRouter, MarimoFileKey
from marimo._server.lsp import LspServer
from marimo._server.model import ConnectionState, SessionConsumer, SessionMode
from marimo._server.models.models import InstantiateRequest
from marimo._server.recents import RecentFilesManager
from marimo._server.session.serialize import (
    SessionCacheKey,
    SessionCacheManager,
)
from marimo._server.session.session_view import SessionView
from marimo._server.tokens import AuthToken, SkewProtectionToken
from marimo._server.types import QueueType
from marimo._server.utils import print_, print_tabbed
from marimo._types.ids import CellId_t, ConsumerId, SessionId
from marimo._utils.disposable import Disposable
from marimo._utils.distributor import (
    ConnectionDistributor,
    QueueDistributor,
)
from marimo._utils.file_watcher import FileWatcherManager
from marimo._utils.repr import format_repr
from marimo._utils.typed_connection import TypedConnection

LOGGER = _loggers.marimo_logger()


class QueueManager:
    """Manages queues for a session."""

    def __init__(self, use_multiprocessing: bool):
        context = mp.get_context("spawn") if use_multiprocessing else None

        # Control messages for the kernel (run, set UI element, set config, etc
        # ) are sent through the control queue
        self.control_queue: QueueType[requests.ControlRequest] = (
            context.Queue() if context is not None else queue.Queue()
        )

        # Set UI element queues are stored in both the control queue and
        # this queue, so that the backend can merge/batch set-ui-element
        # requests.
        self.set_ui_element_queue: QueueType[
            requests.SetUIElementValueRequest
        ] = context.Queue() if context is not None else queue.Queue()

        # Code completion requests are sent through a separate queue
        self.completion_queue: QueueType[requests.CodeCompletionRequest] = (
            context.Queue() if context is not None else queue.Queue()
        )

        self.win32_interrupt_queue: QueueType[bool] | None
        if sys.platform == "win32":
            self.win32_interrupt_queue = (
                context.Queue() if context is not None else queue.Queue()
            )
        else:
            self.win32_interrupt_queue = None

        # Input messages for the user's Python code are sent through the
        # input queue
        self.input_queue: QueueType[str] = (
            context.Queue(maxsize=1)
            if context is not None
            else queue.Queue(maxsize=1)
        )
        self.stream_queue: Optional[
            queue.Queue[Union[KernelMessage, None]]
        ] = None
        if not use_multiprocessing:
            self.stream_queue = queue.Queue()

    def close_queues(self) -> None:
        if isinstance(self.control_queue, MPQueue):
            # cancel join thread because we don't care if the queues still have
            # things in it: don't want to make the child process wait for the
            # queues to empty
            self.control_queue.cancel_join_thread()
            self.control_queue.close()
        else:
            # kernel thread cleans up read/write conn and IOloop handler on
            # exit; we don't join the thread because we don't want to block
            self.control_queue.put(requests.StopRequest())

        if isinstance(self.set_ui_element_queue, MPQueue):
            self.set_ui_element_queue.cancel_join_thread()
            self.set_ui_element_queue.close()

        if isinstance(self.input_queue, MPQueue):
            # again, don't make the child process wait for the queues to empty
            self.input_queue.cancel_join_thread()
            self.input_queue.close()

        if isinstance(self.completion_queue, MPQueue):
            self.completion_queue.cancel_join_thread()
            self.completion_queue.close()

        if isinstance(self.win32_interrupt_queue, MPQueue):
            self.win32_interrupt_queue.cancel_join_thread()
            self.win32_interrupt_queue.close()


class KernelManager:
    def __init__(
        self,
        queue_manager: QueueManager,
        mode: SessionMode,
        configs: dict[CellId_t, CellConfig],
        app_metadata: AppMetadata,
        config_manager: MarimoConfigReader,
        virtual_files_supported: bool,
        redirect_console_to_browser: bool,
    ) -> None:
        self.kernel_task: Optional[threading.Thread | mp.Process] = None
        self.queue_manager = queue_manager
        self.mode = mode
        self.configs = configs
        self.app_metadata = app_metadata
        self.config_manager = config_manager
        self.redirect_console_to_browser = redirect_console_to_browser

        # Only used in edit mode
        self._read_conn: Optional[TypedConnection[KernelMessage]] = None
        self._virtual_files_supported = virtual_files_supported

    def start_kernel(self) -> None:
        # We use a process in edit mode so that we can interrupt the app
        # with a SIGINT; we don't mind the additional memory consumption,
        # since there's only one client sess
        is_edit_mode = self.mode == SessionMode.EDIT
        listener = None
        if is_edit_mode:
            # Need to use a socket for windows compatibility
            listener = connection.Listener(family="AF_INET")
            self.kernel_task = mp.Process(
                target=runtime.launch_kernel,
                args=(
                    self.queue_manager.control_queue,
                    self.queue_manager.set_ui_element_queue,
                    self.queue_manager.completion_queue,
                    self.queue_manager.input_queue,
                    # stream queue unused
                    None,
                    listener.address,
                    is_edit_mode,
                    self.configs,
                    self.app_metadata,
                    self.config_manager.get_config(hide_secrets=False),
                    self._virtual_files_supported,
                    self.redirect_console_to_browser,
                    self.queue_manager.win32_interrupt_queue,
                    self.profile_path,
                    GLOBAL_SETTINGS.LOG_LEVEL,
                ),
                # The process can't be a daemon, because daemonic processes
                # can't create children
                # https://docs.python.org/3/library/multiprocessing.html#multiprocessing.Process.daemon  # noqa: E501
                daemon=False,
            )
        else:
            # We use threads in run mode to minimize memory consumption;
            # launching a process would copy the entire program state,
            # which (as of writing) is around 150MB

            # We can't terminate threads, so we have to wait until they
            # naturally exit before cleaning up resources
            def launch_kernel_with_cleanup(*args: Any) -> None:
                runtime.launch_kernel(*args)

            # install formatter import hooks, which will be shared by all
            # threads (in edit mode, the single kernel process installs
            # formatters ...)
            register_formatters(theme=self.config_manager.theme)

            assert self.queue_manager.stream_queue is not None
            # Make threads daemons so killing the server immediately brings
            # down all client sessions
            self.kernel_task = threading.Thread(
                target=launch_kernel_with_cleanup,
                args=(
                    self.queue_manager.control_queue,
                    self.queue_manager.set_ui_element_queue,
                    self.queue_manager.completion_queue,
                    self.queue_manager.input_queue,
                    self.queue_manager.stream_queue,
                    # IPC not used in run mode
                    None,
                    is_edit_mode,
                    self.configs,
                    self.app_metadata,
                    self.config_manager.get_config(hide_secrets=False),
                    self._virtual_files_supported,
                    self.redirect_console_to_browser,
                    # win32 interrupt queue
                    None,
                    # profile path
                    None,
                    # log level
                    GLOBAL_SETTINGS.LOG_LEVEL,
                ),
                # daemon threads can create child processes, unlike
                # daemon processes
                daemon=True,
            )

        self.kernel_task.start()  # type: ignore
        if listener is not None:
            # First thing kernel does is connect to the socket, so it's safe to
            # call accept
            self._read_conn = TypedConnection[KernelMessage].of(
                listener.accept()
            )

    @property
    def profile_path(self) -> str | None:
        self._profile_path: str | None

        if hasattr(self, "_profile_path"):
            return self._profile_path

        profile_dir = GLOBAL_SETTINGS.PROFILE_DIR
        if profile_dir is not None:
            self._profile_path = os.path.join(
                profile_dir,
                (
                    os.path.basename(self.app_metadata.filename) + str(uuid4())
                    if self.app_metadata.filename is not None
                    else str(uuid4())
                ),
            )
        else:
            self._profile_path = None
        return self._profile_path

    def is_alive(self) -> bool:
        return self.kernel_task is not None and self.kernel_task.is_alive()

    def interrupt_kernel(self) -> None:
        if (
            isinstance(self.kernel_task, mp.Process)
            and self.kernel_task.pid is not None
        ):
            q = self.queue_manager.win32_interrupt_queue
            if sys.platform == "win32" and q is not None:
                LOGGER.debug("Queueing interrupt request for kernel.")
                q.put_nowait(True)
            else:
                LOGGER.debug("Sending SIGINT to kernel")
                os.kill(self.kernel_task.pid, signal.SIGINT)

    def close_kernel(self) -> None:
        assert self.kernel_task is not None, "kernel not started"

        if isinstance(self.kernel_task, mp.Process):
            if self.profile_path is not None and self.kernel_task.is_alive():
                self.queue_manager.control_queue.put(requests.StopRequest())
                # Hack: Wait for kernel to exit and write out profile;
                # joining the process hangs, but not sure why.
                print_(
                    "\tWriting profile statistics to",
                    self.profile_path,
                    " ...",
                )
                while not os.path.exists(self.profile_path):
                    time.sleep(0.1)
                time.sleep(1)

            self.queue_manager.close_queues()
            if self.kernel_task.is_alive():
                self.kernel_task.terminate()
            if self._read_conn is not None:
                self._read_conn.close()
        elif self.kernel_task.is_alive():
            # We don't join the kernel thread because we don't want to server
            # to block on it finishing
            self.queue_manager.control_queue.put(requests.StopRequest())

    @property
    def kernel_connection(self) -> TypedConnection[KernelMessage]:
        assert self._read_conn is not None, "connection not started"
        return self._read_conn


class Room:
    """
    A room is a collection of SessionConsumers
    that can be used to broadcast messages to all
    of them.
    """

    def __init__(self) -> None:
        self.main_consumer: Optional[SessionConsumer] = None
        self.consumers: dict[SessionConsumer, ConsumerId] = {}
        self.disposables: dict[SessionConsumer, Disposable] = {}

    @property
    def size(self) -> int:
        return len(self.consumers)

    def add_consumer(
        self,
        consumer: SessionConsumer,
        dispose: Disposable,
        consumer_id: ConsumerId,
        # Whether the consumer is the main session consumer
        # We only allow one main consumer, the rest are kiosk consumers
        main: bool,
    ) -> None:
        self.consumers[consumer] = consumer_id
        self.disposables[consumer] = dispose
        if main:
            assert self.main_consumer is None, (
                "Main session consumer already exists"
            )
            self.main_consumer = consumer

    def remove_consumer(self, consumer: SessionConsumer) -> None:
        if consumer not in self.consumers:
            LOGGER.debug(
                "Attempted to remove a consumer that was not in room."
            )
            return

        if consumer == self.main_consumer:
            self.main_consumer = None
        self.consumers.pop(consumer)
        disposable = self.disposables.pop(consumer)
        try:
            consumer.on_stop()
        finally:
            disposable.dispose()

    def broadcast(
        self,
        operation: MessageOperation,
        except_consumer: Optional[ConsumerId],
    ) -> None:
        for consumer in self.consumers:
            if consumer.consumer_id == except_consumer:
                continue
            if consumer.connection_state() == ConnectionState.OPEN:
                consumer.write_operation(operation)

    def close(self) -> None:
        for consumer in self.consumers:
            disposable = self.disposables.pop(consumer)
            consumer.on_stop()
            disposable.dispose()
        self.consumers = {}
        self.main_consumer = None


_DEFAULT_TTL_SECONDS = 120


class Session:
    """A client session.

    Each session has its own Python kernel, for editing and running the app,
    and its own websocket, for sending messages to the client.
    """

    SESSION_CACHE_INTERVAL_SECONDS = 2

    @classmethod
    def create(
        cls,
        *,
        initialization_id: str,
        session_consumer: SessionConsumer,
        mode: SessionMode,
        app_metadata: AppMetadata,
        app_file_manager: AppFileManager,
        config_manager: MarimoConfigManager,
        virtual_files_supported: bool,
        redirect_console_to_browser: bool,
        ttl_seconds: Optional[int],
    ) -> Session:
        """
        Create a new session.
        """
        # Inherit config from the session manager
        # and override with any script-level config
        config_manager = config_manager.with_overrides(
            ScriptConfigManager(app_file_manager.path).get_config()
        )

        configs = app_file_manager.app.cell_manager.config_map()
        use_multiprocessing = mode == SessionMode.EDIT
        queue_manager = QueueManager(use_multiprocessing)
        kernel_manager = KernelManager(
            queue_manager,
            mode,
            configs,
            app_metadata,
            config_manager,
            virtual_files_supported=virtual_files_supported,
            redirect_console_to_browser=redirect_console_to_browser,
        )

        return cls(
            initialization_id=initialization_id,
            session_consumer=session_consumer,
            queue_manager=queue_manager,
            kernel_manager=kernel_manager,
            app_file_manager=app_file_manager,
            config_manager=config_manager,
            ttl_seconds=ttl_seconds,
        )

    def __init__(
        self,
        initialization_id: str,
        session_consumer: SessionConsumer,
        queue_manager: QueueManager,
        kernel_manager: KernelManager,
        app_file_manager: AppFileManager,
        config_manager: MarimoConfigManager,
        ttl_seconds: Optional[int],
    ) -> None:
        """Initialize kernel and client connection to it."""
        # This is some unique ID that we can use to identify the session
        # in edit mode. We don't use the session_id because this can change if
        # the session is resumed
        self.initialization_id = initialization_id
        self.app_file_manager = app_file_manager
        self.room = Room()
        self._queue_manager = queue_manager
        self.kernel_manager = kernel_manager
        self.ttl_seconds = (
            ttl_seconds if ttl_seconds is not None else _DEFAULT_TTL_SECONDS
        )
        self.session_view = SessionView()
        self.session_cache_manager: SessionCacheManager | None = None
        self.config_manager = config_manager
        self.kernel_manager.start_kernel()
        # Reads from the kernel connection and distributes the
        # messages to each subscriber.
        self.message_distributor: (
            ConnectionDistributor[KernelMessage]
            | QueueDistributor[KernelMessage]
        )
        if self.kernel_manager.mode == SessionMode.EDIT:
            self.message_distributor = ConnectionDistributor[KernelMessage](
                self.kernel_manager.kernel_connection
            )
        else:
            q = self._queue_manager.stream_queue
            assert q is not None
            self.message_distributor = QueueDistributor[KernelMessage](queue=q)

        self.message_distributor.add_consumer(
            lambda msg: self.session_view.add_raw_operation(msg[1])
        )
        self.connect_consumer(session_consumer, main=True)
        self.message_distributor.start()

        self.heartbeat_task: Optional[asyncio.Task[Any]] = None
        self._start_heartbeat()
        self._closed = False

    def _start_heartbeat(self) -> None:
        def _check_alive() -> None:
            if not self.kernel_manager.is_alive():
                LOGGER.debug(
                    "Closing session %s because kernel died",
                    self.initialization_id,
                )
                self.close()
                print_()
                print_tabbed(
                    red(
                        "The Python kernel for file "
                        f"{self.app_file_manager.filename} died unexpectedly."
                    )
                )
                print_()
                self.close()

        # Start a heartbeat task, which checks if the kernel is alive
        # every second

        async def _heartbeat() -> None:
            while True:
                await asyncio.sleep(1)
                _check_alive()

        try:
            loop = asyncio.get_event_loop()
            self.heartbeat_task = loop.create_task(_heartbeat())
        except RuntimeError:
            # This can happen if there is no event loop running
            self.heartbeat_task = None

    def try_interrupt(self) -> None:
        """Try to interrupt the kernel."""
        self.kernel_manager.interrupt_kernel()

    def put_control_request(
        self,
        request: requests.ControlRequest,
        from_consumer_id: Optional[ConsumerId],
    ) -> None:
        """Put a control request in the control queue."""
        self._queue_manager.control_queue.put(request)
        if isinstance(request, SetUIElementValueRequest):
            self._queue_manager.set_ui_element_queue.put(request)
        # Propagate the control request to the room
        if isinstance(request, ExecuteMultipleRequest):
            self.room.broadcast(
                UpdateCellCodes(
                    cell_ids=request.cell_ids,
                    codes=request.codes,
                    # Not stale because we just ran the code
                    code_is_stale=False,
                ),
                except_consumer=from_consumer_id,
            )
            if len(request.cell_ids) == 1:
                self.room.broadcast(
                    FocusCell(cell_id=request.cell_ids[0]),
                    except_consumer=from_consumer_id,
                )
        self.session_view.add_control_request(request)

    def put_completion_request(
        self, request: requests.CodeCompletionRequest
    ) -> None:
        """Put a code completion request in the completion queue."""
        self._queue_manager.completion_queue.put(request)

    def put_input(self, text: str) -> None:
        """Put an input() request in the input queue."""
        self._queue_manager.input_queue.put(text)
        self.session_view.add_stdin(text)

    def disconnect_consumer(self, session_consumer: SessionConsumer) -> None:
        """
        Stop the session consumer but keep the kernel running.

        This will disconnect the main session consumer,
        or a kiosk consumer.
        """
        self.room.remove_consumer(session_consumer)

    def maybe_disconnect_consumer(self) -> None:
        """
        Disconnect the main session consumer if it connected.
        """
        if self.room.main_consumer is not None:
            self.disconnect_consumer(self.room.main_consumer)

    def connect_consumer(
        self, session_consumer: SessionConsumer, *, main: bool
    ) -> None:
        """
        Connect or resume the session with a new consumer.

        If its the main consumer and one already exists,
        an exception is raised.
        """
        subscribe = session_consumer.on_start()
        unsubscribe_consumer = self.message_distributor.add_consumer(subscribe)
        self.room.add_consumer(
            session_consumer,
            unsubscribe_consumer,
            session_consumer.consumer_id,
            main=main,
        )

    def get_current_state(self) -> SessionView:
        """Return the current state of the session."""
        return self.session_view

    def connection_state(self) -> ConnectionState:
        """Return the connection state of the session."""
        if self._closed:
            return ConnectionState.CLOSED
        if self.room.main_consumer is None:
            return ConnectionState.ORPHANED
        return self.room.main_consumer.connection_state()

    def write_operation(
        self,
        operation: MessageOperation,
        from_consumer_id: Optional[ConsumerId],
    ) -> None:
        """Write an operation to the session consumer and the session view."""
        self.session_view.add_operation(operation)
        self.room.broadcast(operation, except_consumer=from_consumer_id)

    def close(self) -> None:
        """
        Close the session.

        This will close the session consumer, kernel, and all kiosk consumers.
        """
        if self._closed:
            return

        self._closed = True
        # Close the room
        self.room.close()
        # Close the kernel
        self.message_distributor.stop()
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
        if self.session_cache_manager:
            self.session_cache_manager.stop()
        self.kernel_manager.close_kernel()

    def instantiate(
        self,
        request: InstantiateRequest,
        *,
        http_request: Optional[HTTPRequest],
    ) -> None:
        """Instantiate the app."""
        execution_requests = tuple(
            ExecutionRequest(
                cell_id=cell_data.cell_id,
                code=cell_data.code,
                request=http_request,
            )
            for cell_data in self.app_file_manager.app.cell_manager.cell_data()
        )

        self.put_control_request(
            CreationRequest(
                execution_requests=execution_requests,
                set_ui_element_value_request=SetUIElementValueRequest(
                    object_ids=request.object_ids,
                    values=request.values,
                    token=str(uuid4()),
                    request=http_request,
                ),
                auto_run=request.auto_run,
                request=http_request,
            ),
            from_consumer_id=None,
        )

    def sync_session_view_from_cache(self) -> None:
        """Sync the session view from a file.

        Overwrites the existing session view.
        Mutates the existing session.
        """
        from marimo._version import __version__

        LOGGER.debug("Syncing session view from cache")
        self.session_cache_manager = SessionCacheManager(
            session_view=self.session_view,
            path=self.app_file_manager.path,
            interval=self.SESSION_CACHE_INTERVAL_SECONDS,
        )

        app = self.app_file_manager.app
        codes = tuple(
            cell_data.code for cell_data in app.cell_manager.cell_data()
        )
        key = SessionCacheKey(codes=codes, marimo_version=__version__)
        self.session_view = self.session_cache_manager.read_session_view(key)
        self.session_cache_manager.start()

    def __repr__(self) -> str:
        return format_repr(
            self,
            {
                "connection_state": self.connection_state(),
                "room": self.room,
            },
        )


class SessionManager:
    """Mapping from client session IDs to sessions.

    Maintains a mapping from client session IDs to client sessions;
    there is exactly one session per client.

    The SessionManager also encapsulates state common to all sessions:
    - the app filename
    - the app mode (edit or run)
    - the auth token
    - the skew-protection token
    """

    def __init__(
        self,
        *,
        file_router: AppFileRouter,
        mode: SessionMode,
        development_mode: bool,
        quiet: bool,
        include_code: bool,
        lsp_server: LspServer,
        config_manager: MarimoConfigManager,
        cli_args: SerializedCLIArgs,
        argv: list[str] | None,
        auth_token: Optional[AuthToken],
        redirect_console_to_browser: bool,
        ttl_seconds: Optional[int],
        watch: bool = False,
    ) -> None:
        self.file_router = file_router
        self.mode = mode
        self.development_mode = development_mode
        self.quiet = quiet
        self.sessions: dict[SessionId, Session] = {}
        self.include_code = include_code
        self.ttl_seconds = ttl_seconds
        self.lsp_server = lsp_server
        self.watcher_manager = FileWatcherManager()
        self.watch = watch
        self.recents = RecentFilesManager()
        self.cli_args = cli_args
        self.argv = argv
        self.redirect_console_to_browser = redirect_console_to_browser

        # We should access the config_manager from the session if possible
        # since this will contain config-level overrides
        self._config_manager = config_manager

        def _get_code() -> str:
            app = file_router.get_single_app_file_manager(
                default_width=self._config_manager.default_width,
                default_auto_download=self._config_manager.default_auto_download,
                default_sql_output=self._config_manager.default_sql_output,
            ).app
            return "".join(code for code in app.cell_manager.codes())

        # Auth token and Skew-protection token
        if mode == SessionMode.EDIT:
            # In edit mode, if no auth token is provided,
            # generate a random token
            self.auth_token = (
                AuthToken.random() if auth_token is None else auth_token
            )
            self.skew_protection_token = SkewProtectionToken.random()
        else:
            source_code = _get_code()
            # Because run-mode is read-only and we could have multiple
            # servers for the same app (going to sleep or autoscaling),
            # we default to a token based on the app's code
            self.auth_token = (
                AuthToken.from_code(source_code)
                if auth_token is None
                else auth_token
            )
            self.skew_protection_token = SkewProtectionToken.from_code(
                source_code
            )

    def app_manager(self, key: MarimoFileKey) -> AppFileManager:
        """
        Get the app manager for the given key.
        """
        return self.file_router.get_file_manager(
            key,
            default_width=self._config_manager.default_width,
            default_auto_download=self._config_manager.default_auto_download,
            default_sql_output=self._config_manager.default_sql_output,
        )

    def create_session(
        self,
        session_id: SessionId,
        session_consumer: SessionConsumer,
        query_params: SerializedQueryParams,
        file_key: MarimoFileKey,
    ) -> Session:
        """Create a new session"""
        LOGGER.debug("Creating new session for id %s", session_id)
        if session_id not in self.sessions:
            app_file_manager = self.file_router.get_file_manager(
                file_key,
                default_width=self._config_manager.default_width,
                default_auto_download=self._config_manager.default_auto_download,
                default_sql_output=self._config_manager.default_sql_output,
            )

            if app_file_manager.path:
                self.recents.touch(app_file_manager.path)

            session = Session.create(
                initialization_id=file_key,
                session_consumer=session_consumer,
                mode=self.mode,
                app_metadata=AppMetadata(
                    query_params=query_params,
                    filename=app_file_manager.path,
                    cli_args=self.cli_args,
                    argv=self.argv,
                    app_config=app_file_manager.app.config,
                ),
                app_file_manager=app_file_manager,
                config_manager=self._config_manager,
                virtual_files_supported=True,
                redirect_console_to_browser=self.redirect_console_to_browser,
                ttl_seconds=self.ttl_seconds,
            )
            self.sessions[session_id] = session

            # Start file watcher if enabled
            if self.watch and app_file_manager.path:
                self._start_file_watcher_for_session(session)

        return self.sessions[session_id]

    def _start_file_watcher_for_session(self, session: Session) -> None:
        """Start a file watcher for a session."""
        if not session.app_file_manager.path:
            return

        async def on_file_changed(path: Path) -> None:
            LOGGER.debug(f"{path} was modified")
            # Skip if the session does not relate to the file
            if session.app_file_manager.path != os.path.abspath(path):
                return

            # Reload the file manager to get the latest code
            try:
                changed_cell_ids = session.app_file_manager.reload()
            except Exception as e:
                # If there are syntax errors, we just skip
                # and don't send the changes
                LOGGER.error(f"Error loading file: {e}")
                return
            # In run, we just call Reload()
            if self.mode == SessionMode.RUN:
                session.write_operation(Reload(), from_consumer_id=None)
                return

            # Get the latest codes
            codes = list(session.app_file_manager.app.cell_manager.codes())
            cell_ids = list(
                session.app_file_manager.app.cell_manager.cell_ids()
            )
            # Send the updated cell ids and codes to the frontend
            session.write_operation(
                UpdateCellIdsRequest(cell_ids=cell_ids),
                from_consumer_id=None,
            )

            # Check if we should auto-run cells based on config
            should_autorun = (
                self._config_manager.get_config()["runtime"]["watcher_on_save"]
                == "autorun"
            )

            # Auto-run cells if configured
            if should_autorun:
                changed_cell_ids_list = list(changed_cell_ids)
                cell_ids_to_idx = {
                    cell_id: idx for idx, cell_id in enumerate(cell_ids)
                }
                changed_codes = [
                    codes[cell_ids_to_idx[cell_id]]
                    for cell_id in changed_cell_ids_list
                ]

                # This runs the request and also runs UpdateCellCodes
                session.put_control_request(
                    ExecuteMultipleRequest(
                        cell_ids=changed_cell_ids_list,
                        codes=changed_codes,
                        request=None,
                    ),
                    from_consumer_id=None,
                )
            else:
                session.write_operation(
                    UpdateCellCodes(
                        cell_ids=cell_ids,
                        codes=codes,
                        code_is_stale=True,
                    ),
                    from_consumer_id=None,
                )

        session._unsubscribe_file_watcher_ = on_file_changed  # type: ignore

        self.watcher_manager.add_callback(
            Path(session.app_file_manager.path), on_file_changed
        )

    def handle_file_rename_for_watch(
        self, session_id: SessionId, prev_path: Optional[str], new_path: str
    ) -> tuple[bool, Optional[str]]:
        """Handle renaming a file for a session.

        Returns:
            tuple[bool, Optional[str]]: (success, error_message)
        """
        session = self.get_session(session_id)
        if not session:
            return False, "Session not found"

        if not os.path.exists(new_path):
            return False, f"File {new_path} does not exist"

        if not session.app_file_manager.path:
            return False, "Session has no associated file"

        # Handle rename for session cache
        if session.session_cache_manager:
            session.session_cache_manager.rename_path(new_path)

        try:
            if self.watch:
                # Remove the old file watcher if it exists
                if prev_path:
                    self.watcher_manager.remove_callback(
                        Path(prev_path),
                        session._unsubscribe_file_watcher_,  # type: ignore
                    )

                # Add a watcher for the new path if needed
                self._start_file_watcher_for_session(session)

            return True, None

        except Exception as e:
            LOGGER.error(f"Error handling file rename: {e}")

            if self.watch:
                self._start_file_watcher_for_session(session)
            return False, str(e)

    def get_session(self, session_id: SessionId) -> Optional[Session]:
        session = self.sessions.get(session_id)
        if session:
            return session

        # Search for kiosk sessions
        for session in self.sessions.values():
            if ConsumerId(session_id) in session.room.consumers.values():
                return session

        return None

    def get_session_by_file_key(
        self, file_key: MarimoFileKey
    ) -> Optional[Session]:
        for session in self.sessions.values():
            if (
                session.initialization_id == file_key
                or session.app_file_manager.path == os.path.abspath(file_key)
            ):
                return session
        return None

    def maybe_resume_session(
        self, new_session_id: SessionId, file_key: MarimoFileKey
    ) -> Optional[Session]:
        """
        Try to resume a session if one is resumable.
        If it is resumable, return the session and update the session id.
        """

        # If in run mode, only resume the session if it is orphaned and has
        # the same session id, otherwise we want to create a new session
        if self.mode == SessionMode.RUN:
            maybe_session = self.get_session(new_session_id)
            if (
                maybe_session
                and maybe_session.connection_state()
                == ConnectionState.ORPHANED
            ):
                LOGGER.debug(
                    "Found a resumable RUN session: prev_id=%s",
                    new_session_id,
                )
                return maybe_session
            return None

        # Cleanup sessions with dead kernels; materializing as a list because
        # close_sessions mutates self.sessions
        for session_id, session in list(self.sessions.items()):
            task = session.kernel_manager.kernel_task
            if task is not None and not task.is_alive():
                self.close_session(session_id)

        # Should only return an orphaned session
        sessions_with_the_same_file: dict[SessionId, Session] = {
            session_id: session
            for session_id, session in self.sessions.items()
            if session.app_file_manager.path == os.path.abspath(file_key)
        }

        if len(sessions_with_the_same_file) == 0:
            return None
        if len(sessions_with_the_same_file) > 1:
            raise InvalidSessionException(
                "Only one session should exist while editing"
            )

        (session_id, session) = next(iter(sessions_with_the_same_file.items()))
        connection_state = session.connection_state()
        if connection_state == ConnectionState.ORPHANED:
            LOGGER.debug(
                f"Found a resumable EDIT session: prev_id={session_id}"
            )
            # Set new session and remove old session
            self.sessions[new_session_id] = session
            # If the ID is the same, we don't need to delete the old session
            if new_session_id != session_id and session_id in self.sessions:
                del self.sessions[session_id]
            return session

        LOGGER.debug(
            "Session is not resumable, current state: %s",
            connection_state,
        )
        return None

    def any_clients_connected(self, key: MarimoFileKey) -> bool:
        """Returns True if at least one client has an open socket."""
        if key.startswith(AppFileRouter.NEW_FILE):
            return False

        for session in self.sessions.values():
            if session.connection_state() == ConnectionState.OPEN and (
                session.app_file_manager.path == os.path.abspath(key)
            ):
                return True
        return False

    async def start_lsp_server(self) -> None:
        """Starts the lsp server if it is not already started.

        Doesn't start in run mode.
        """
        if self.mode == SessionMode.RUN:
            LOGGER.warning("Cannot start LSP server in run mode")
            return

        alert = self.lsp_server.start()

        if alert is not None:
            for session in self.sessions.values():
                session.write_operation(alert, from_consumer_id=None)
            return

    def close_session(self, session_id: SessionId) -> bool:
        """Close a session and remove its file watcher if it has one."""
        LOGGER.debug("Closing session %s", session_id)
        session = self.get_session(session_id)
        if session is None:
            return False

        # Remove the file watcher callback for this session
        if session.app_file_manager.path and self.watch:
            self.watcher_manager.remove_callback(
                Path(session.app_file_manager.path),
                session._unsubscribe_file_watcher_,  # type: ignore
            )

        session.close()
        if session_id in self.sessions:
            del self.sessions[session_id]
        return True

    def close_all_sessions(self) -> None:
        LOGGER.debug("Closing all sessions (sessions: %s)", self.sessions)
        for session in self.sessions.values():
            session.close()
        LOGGER.debug("Closed all sessions.")
        self.sessions = {}

    def shutdown(self) -> None:
        """Shutdown the session manager and stop all file watchers."""
        LOGGER.debug("Shutting down")
        self.close_all_sessions()
        self.lsp_server.stop()
        self.watcher_manager.stop_all()

    def should_send_code_to_frontend(self) -> bool:
        """Returns True if the server can send messages to the frontend."""
        return self.mode == SessionMode.EDIT or self.include_code

    def get_active_connection_count(self) -> int:
        return len(
            [
                session
                for session in self.sessions.values()
                if session.connection_state() == ConnectionState.OPEN
            ]
        )


def send_message_to_consumer(
    session: Session,
    operation: MessageOperation,
    consumer_id: Optional[ConsumerId],
) -> None:
    if session.connection_state() == ConnectionState.OPEN:
        for consumer, c_id in session.room.consumers.items():
            if c_id == consumer_id:
                consumer.write_operation(operation)
