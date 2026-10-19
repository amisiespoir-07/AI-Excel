from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from .config import Settings
from .domain import ModelDefinition, ModelRegistryEntry, ModelRoute, ParameterExtraction


class LLMUnavailable(RuntimeError):
    pass


class LLMOutputInvalid(LLMUnavailable):
    pass


class DeepSeekClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _client(self):
        if not self.settings.deepseek_api_key:
            raise LLMUnavailable("未配置 DEEPSEEK_API_KEY")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMUnavailable("缺少 openai 依赖，请安装 requirements.txt") from exc
        return OpenAI(
            api_key=self.settings.deepseek_api_key,
            base_url=self.settings.deepseek_base_url,
            timeout=30.0,
            max_retries=2,
        )

    @staticmethod
    def _validate_json_response(content: str, schema, label: str):
        try:
            return schema.model_validate_json(content)
        except (ValidationError, ValueError) as exc:
            raise LLMOutputInvalid(f"DeepSeek 返回的{label}不是有效的结构化 JSON") from exc

    def route_model(
        self, user_message: str, candidates: list[ModelRegistryEntry]
    ) -> ModelRoute:
        # Build a whitelist payload rather than serializing any calculation definition.
        public_candidates = []
        for candidate in candidates:
            entry = {
                "id": candidate.id,
                "name": candidate.name,
                "description": candidate.description,
                "keywords": list(candidate.keywords),
                "category": candidate.category,
                "enabled": candidate.enabled,
            }
            # 参数提法（显示名 + 自然语言别名）能大幅提升判别力：
            # 「烟气量」这种只属于某一个模型的措辞，一看就知道该选谁。
            hints = list(getattr(candidate, "parameter_hints", ()) or ())
            if hints:
                entry["parameter_hints"] = hints
            public_candidates.append(entry)
        system_prompt = (
            "You are a calculation model router. Your only task is to select the calculation model "
            "that best matches the user's request. You may only select from the provided candidate "
            "model IDs. Never invent a model ID. Never select a workbook file. Never reveal spreadsheet "
            "tabs or coordinates. Never extract calculation parameters. "
            "A candidate may carry parameter_hints: the business parameter wordings that model accepts. "
            "Use them only to judge which model can accept what the user is describing; when the user's "
            "wording matches one candidate's hints, prefer that candidate. "
            "If the user's intent is ambiguous, return clarification_required and candidate model IDs. "
            "Return JSON only, with exactly the fields required by the route schema. For a selection use "
            '{"intent":"calculate","model_id":"..."}. For ambiguity use '
            '{"intent":"clarification_required","candidate_models":["..."],"clarification":"..."}.'
        )
        response = self._client().chat.completions.create(
            model=self.settings.deepseek_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"candidate_models": public_candidates, "user_message": user_message},
                        ensure_ascii=False,
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = response.choices[0].message.content or ""
        return self._validate_json_response(content, ModelRoute, "模型路由结果")

    def extract_parameters(
        self, model: ModelDefinition, user_message: str, current_values: dict[str, Any]
    ) -> ParameterExtraction:
        catalog = {
            key: {
                "description": spec.description,
                "display_name": spec.display_name,
                "input_aliases": spec.input.aliases,
                "type": spec.type,
                "unit": spec.unit,
                "required": spec.required,
                "minimum": spec.minimum,
                "maximum": spec.maximum,
                "missing_policy": spec.missing.policy,
            }
            for key, spec in model.parameters.items()
        }
        system_prompt = (
            "你是一个受限的参数提取器。根据允许参数的业务描述、同义表达和单位，"
            "把用户自然语言中的明确数值映射到 allowed_parameters 中的参数键。"
            "参数含义、同义表达和示例均以各参数的 description 为准。百分数必须遵循参数描述中指定的存储约定，"
            "不自行将15.3%换算为0.153。只提取本轮明确提及的值；不得猜测、补默认值、"
            "修改单位约定、输出单元格/工作簿路径或触发执行。仅允许使用给定参数键。"
            "无法确定某个数值对应哪个参数时不要猜，使用 clarification 提出简短中文问题。"
            "你只负责参数提取，不选择模型、Excel 文件或工作流。只返回 JSON 对象，且只允许 "
            "parameters 和 clarification 两个字段；clarification 无需澄清时为 null。"
        )
        user_payload = {
            "model_name": model.model.name,
            "model_description": model.model.description,
            "allowed_parameters": catalog,
            "previously_confirmed_session_values": current_values,
            "current_user_message": user_message,
        }
        response = self._client().chat.completions.create(
            model=self.settings.deepseek_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = response.choices[0].message.content or ""
        return self._validate_json_response(content, ParameterExtraction, "参数提取结果")

    def summarize_results(
        self,
        model: ModelDefinition,
        parameters: dict[str, Any],
        results: dict[str, Any],
    ) -> str:
        """只基于 Excel 实际结果生成说明；调用失败由报告层使用本地说明兜底。"""
        try:
            response = self._client().chat.completions.create(
                model=self.settings.deepseek_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "根据给定的 Excel 计算输入和输出，用中文写一段简洁结果说明。"
                            "数值只能照抄输出，不得重新计算、推断、补造事实或给保证；"
                            "明确说明结果以 Excel 计算为准。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"model": model.model_id, "parameters": parameters, "excel_results": results},
                            ensure_ascii=False,
                        ),
                    },
                ],
                temperature=0,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception:
            return "本报告数值来自本机 Microsoft Excel 对已确认参数的计算结果；请以表格中的结构化结果为准。"
