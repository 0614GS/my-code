"""提示词片段的确定性解析与生命周期缓存。"""

from collections.abc import Iterable

from nano_code.model.request import PromptStability, ResolvedPromptSection, SystemPrompt
from nano_code.prompts.models import PromptSection

_STABILITY_ORDER = {
    PromptStability.STATIC: 0,
    PromptStability.SESSION: 1,
    PromptStability.REQUEST: 2,
}


class PromptRegistry:
    """解析有序片段，并缓存本轮之前应保持稳定的内容。"""

    def __init__(self, sections: Iterable[PromptSection]) -> None:
        actual = tuple(sections)
        if not actual:
            raise ValueError("At least one prompt section is required")
        keys = [section.key for section in actual]
        if len(keys) != len(set(keys)):
            raise ValueError("Prompt section keys must be unique")
        order = [_STABILITY_ORDER[section.stability] for section in actual]
        if order != sorted(order):
            raise ValueError(
                "Prompt sections must be ordered static, session, then request"
            )
        self._sections = actual
        self._cache: dict[str, ResolvedPromptSection] = {}

    @property
    def sections(self) -> tuple[PromptSection, ...]:
        return self._sections

    def resolve(
        self,
        *,
        session_cache: dict[str, ResolvedPromptSection] | None = None,
    ) -> SystemPrompt:
        """Resolve with runtime-static and caller-owned session caches."""

        return SystemPrompt(
            tuple(self._resolve_section(item, session_cache) for item in self._sections)
        )

    def _resolve_section(
        self,
        section: PromptSection,
        session_cache: dict[str, ResolvedPromptSection] | None,
    ) -> ResolvedPromptSection:
        cache = (
            self._cache
            if section.stability is PromptStability.STATIC
            else session_cache
            if section.stability is PromptStability.SESSION
            else None
        )
        if cache is not None:
            cached = cache.get(section.key)
            if cached is not None:
                return cached
        resolved = ResolvedPromptSection(
            key=section.key,
            content=section.resolve(),
            stability=section.stability,
        )
        if cache is not None:
            cache[section.key] = resolved
        return resolved


__all__ = [
    "PromptRegistry",
]
