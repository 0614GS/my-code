"""确定性组装 system prompt 的领域组件。"""

from collections.abc import Iterable

from nano_code.context.models import PromptSection


class PromptAssembler:
    """保留片段顺序，并拒绝会破坏缓存语义的重复身份。"""

    def __init__(self, sections: Iterable[PromptSection]) -> None:
        actual = tuple(sections)
        keys = [section.key for section in actual]
        if len(keys) != len(set(keys)):
            raise ValueError("Prompt section keys must be unique")
        if not actual:
            raise ValueError("At least one prompt section is required")
        self._sections = actual

    @property
    def sections(self) -> tuple[PromptSection, ...]:
        return self._sections

    def render(self) -> str:
        """按声明顺序生成稳定的 provider-neutral system prompt。"""

        return "\n\n".join(section.content for section in self._sections)
