from __future__ import annotations

from collections.abc import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import clear


class Terminal:
    """Prompt wrapper with history and cross-platform key bindings.

    prompt_toolkit normalizes console input on Windows and POSIX terminals into
    symbolic keys such as "c-c", "c-l" and "up". This keeps shortcut handling
    out of platform-specific msvcrt/termios branches.
    """

    def __init__(
        self,
        history_file: str,
        on_toggle_hex: Callable[[], None],
        on_toggle_terminator: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        self.bindings = KeyBindings()
        self._on_toggle_hex = on_toggle_hex
        self._on_toggle_terminator = on_toggle_terminator
        self._on_exit = on_exit

        @self.bindings.add("c-h", eager=True)
        def _toggle_hex(event) -> None:  # pragma: no cover - interactive
            # Some terminals map Ctrl+H to Backspace. When that happens users
            # can still use :hex / :ascii; on terminals that distinguish it,
            # this binding toggles HEX/ASCII immediately.
            self._on_toggle_hex()

        @self.bindings.add("c-l", eager=True)
        def _clear(event) -> None:  # pragma: no cover - interactive
            clear()

        @self.bindings.add("c-e", eager=True)
        def _toggle_term(event) -> None:  # pragma: no cover - interactive
            self._on_toggle_terminator()

        @self.bindings.add("c-c", eager=True)
        @self.bindings.add("c-d", eager=True)
        def _exit(event) -> None:  # pragma: no cover - interactive
            self._on_exit()
            event.app.exit(result=":quit")

        self.session: PromptSession[str] = PromptSession(
            history=FileHistory(history_file),
            key_bindings=self.bindings,
        )

    async def prompt(self, message: str) -> str:
        with patch_stdout(raw=True):
            return await self.session.prompt_async(HTML(message))
