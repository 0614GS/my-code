"""提示词片段的确定性解析与生命周期缓存。"""

from collections.abc import Iterable

from nano_code.model import PromptStability, ResolvedPromptSection, SystemPrompt
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

    def resolve(self) -> SystemPrompt:
        """生成本次 prompt；只有 request 片段会在每次解析时重新计算。"""

        return SystemPrompt(
            tuple(self._resolve_section(item) for item in self._sections)
        )

    def _resolve_section(self, section: PromptSection) -> ResolvedPromptSection:
        if section.stability is not PromptStability.REQUEST:
            cached = self._cache.get(section.key)
            if cached is not None:
                return cached
        resolved = ResolvedPromptSection(
            key=section.key,
            content=section.resolve(),
            stability=section.stability,
        )
        if section.stability is not PromptStability.REQUEST:
            self._cache[section.key] = resolved
        return resolved
