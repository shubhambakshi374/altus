"""The Textual application shell."""

from __future__ import annotations

from textual.app import App

from altus import __version__
from altus.agent import build_tool_context, build_workspace
from altus.config import load_config, resolve_profile, save_config, sessions_dir
from altus.config.models import Config, Profile
from altus.core.session import Session
from altus.core.types import ModelInfo
from altus.providers import create_provider
from altus.providers.base import BaseProvider
from altus.storage.sessions import SessionStore
from altus.tools import ToolContext, ToolRegistry, default_registry
from altus.tools.approval import AllowAll, SessionApprovals
from altus.tui.commands import CommandRegistry, build_registry
from altus.tui.screens.chat import ChatScreen
from altus.tui.widgets.approval import InteractiveApproval


class AltusApp(App[None]):
    CSS_PATH = "altus.tcss"
    TITLE = "Altus"
    # Ctrl+P is the model picker. Phase 1 has no commands worth a palette;
    # revisit once the workflow designer has actions to expose.
    ENABLE_COMMAND_PALETTE = False

    def __init__(
        self,
        *,
        profile_name: str | None = None,
        resume: str | None = None,
        model_override: str | None = None,
        provider_override: str | None = None,
        no_tools: bool = False,
        extra_roots: tuple[str, ...] = (),
        auto_approve: bool = False,
        config: Config | None = None,
        provider: BaseProvider | None = None,
        workspace_root: str | None = None,
    ) -> None:
        super().__init__()
        self.config = config or load_config()
        _, profile = resolve_profile(self.config, profile_name)
        if provider_override:
            profile = profile.model_copy(update={"provider": provider_override})
        if model_override:
            profile = profile.model_copy(update={"model": model_override})
        self.profile = profile
        self.workspace = build_workspace(self.config, root=workspace_root, extra_roots=extra_roots)
        self.registry: ToolRegistry = default_registry(
            cloud=self.config.cloud, mcp_settings=self.config.mcp, git=self.config.tools.git
        )
        self.approvals = SessionApprovals(AllowAll() if auto_approve else InteractiveApproval(self))
        self.tool_ctx: ToolContext = build_tool_context(
            self.config, self.workspace, approvals=self.approvals, registry=self.registry
        )
        self.tools_enabled = self.config.tools.enabled and not no_tools
        self.commands: CommandRegistry = build_registry()
        self.store = SessionStore(sessions_dir())
        self.session = self._load_or_create(resume)
        # An injected provider keeps the app testable without any network.
        self._injected_provider = provider
        self.provider: BaseProvider = provider or create_provider(
            profile.provider, self.config, profile=profile
        )
        self.sub_title = f"v{__version__}"

    def _load_or_create(self, resume: str | None) -> Session:
        if resume:
            session_id = self.store.latest_id() if resume == "last" else resume
            if session_id:
                try:
                    return self.store.load(session_id)
                except FileNotFoundError, ValueError:
                    pass
        return self._new_session()

    def _new_session(self) -> Session:
        session = Session(
            provider=self.profile.provider,
            model=self.profile.model,
            system=self.profile.system,
            max_tokens=self.profile.max_tokens,
            temperature=self.profile.temperature,
            base_url=self.profile.base_url,
            model_supports_tools=self.profile.supports_tools is not False,
            workspace_root=str(self.workspace.root),
            tools_enabled=self.tools_enabled,
        )
        self.store.create(session)
        return session

    def start_new_session(self) -> None:
        self.session = self._new_session()

    async def switch_model(self, model: ModelInfo) -> None:
        """Swap provider and/or model, keeping the current transcript."""
        if model.provider != self.session.provider:
            if self._injected_provider is None:
                await self.provider.close()
                self.provider = create_provider(model.provider, self.config)
            self.session.provider = model.provider
        self.session.model = model.id
        if model.max_output_tokens:
            self.session.max_tokens = min(self.session.max_tokens, model.max_output_tokens)
        # Capability travels with the model, so switching to one without tools
        # disables them rather than failing on the next turn.
        self.session.model_supports_tools = model.supports_tools
        self.store.update_header(self.session)

    # ------------------------------------------------ slash-command surface

    async def switch_provider(self, name: str, profile: Profile | None = None) -> None:
        """Swap provider, keeping the transcript, and pick a sensible model."""
        from altus.providers.registry import catalog_for

        if profile is not None:
            self.profile = profile
        if self._injected_provider is None:
            await self.provider.close()
            self.provider = create_provider(name, self.config, profile=profile or self.profile)
        self.session.provider = name
        self.session.base_url = (profile or self.profile).base_url if profile else ""
        catalog = catalog_for(name)
        if catalog and not any(m.id == self.session.model for m in catalog):
            self.session.model = catalog[0].id
        self.store.update_header(self.session)

    def set_kube_context(self, name: str) -> None:
        """Record the context for Altus only; ~/.kube/config is never touched."""
        self.config.cloud.kube_context = name
        save_config(self.config)

    def add_kubeconfig(self, path: str) -> None:
        if path not in self.config.cloud.kubeconfigs:
            self.config.cloud.kubeconfigs.append(path)
            save_config(self.config)

    async def use_profile(self, name: str) -> str:
        """Switch to a named profile, endpoint and all."""
        profile = self.config.profiles.get(name)
        if profile is None:
            raise KeyError(name)
        self.config.default_profile = name
        save_config(self.config)
        await self.switch_provider(profile.provider, profile)
        self.session.model = profile.model
        self.session.max_tokens = profile.max_tokens
        self.session.system = profile.system
        self.session.model_supports_tools = profile.supports_tools is not False
        self.store.update_header(self.session)
        return name

    async def apply_setup(self, provider: str, model: str, *, base_url: str = "") -> None:
        """Adopt what the wizard chose, for this session and the next."""
        from altus.config.loader import resolve_profile as _resolve

        name, existing = _resolve(self.config, None)
        updated = existing.model_copy(
            update={"provider": provider, "model": model, "base_url": base_url}
        )
        self.config.profiles[name] = updated
        save_config(self.config)
        await self.switch_provider(provider, updated)
        self.session.model = model
        self.session.base_url = base_url
        self.store.update_header(self.session)

    @property
    def configured_providers(self) -> list[str]:
        """Usable right now. For `local` that means a profile names an
        endpoint, not that a key exists --- there is no key."""
        from altus.config.secrets import credential_status
        from altus.providers import PROVIDER_NAMES
        from altus.providers.local import LocalProvider, configured_endpoints

        ready = [n for n in PROVIDER_NAMES if credential_status(n).available]
        if configured_endpoints(self.config):
            ready.append(LocalProvider.name)
        return ready

    def open_setup(self, provider: str | None = None) -> None:
        from altus.tui.widgets.setup import SetupWizard

        self.push_screen(SetupWizard(provider))

    def open_model_picker(self) -> None:
        screen = self.screen
        if isinstance(screen, ChatScreen):
            screen.action_pick_model()

    async def new_session_from_command(self) -> None:
        screen = self.screen
        if isinstance(screen, ChatScreen):
            await screen.action_new_session()

    async def ask_from_command(self, text: str) -> bool:
        """Start a turn with ``text`` as if the user had typed it.

        How ``/workflow new`` hands the conversation over to the model. False
        when there is no chat screen to hand it to.
        """
        screen = self.screen
        if not isinstance(screen, ChatScreen):
            return False
        await screen.ask(text)
        return True

    def on_mount(self) -> None:
        self.theme = self.config.ui.theme
        self.push_screen(ChatScreen())

    async def on_unmount(self) -> None:
        if self._injected_provider is None:
            await self.provider.close()
