from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from .domain import (
    CalculationIntent,
    ModelDefinition,
    ParameterExtraction,
    normalize_legacy_parameter_values,
    validate_parameters,
)
from .llm import DeepSeekClient, LLMOutputInvalid
from .model_router import InvalidModelRoute, ModelRouter
from .model_registry import ModelDefinitionRegistry
from .storage import Storage


class NotFoundError(RuntimeError):
    pass


class ConflictError(RuntimeError):
    pass


_MODEL_SWITCH_LANGUAGE = re.compile(
    r"(?:切换(?:到|成|为)|改算|不要.{0,12}(?:了|算).{0,12}改算|"
    r"重新(?:开始|进行)(?:一项|一个)?(?:新的)?计算|现在.{0,12}(?:帮我)?(?:计算|算一下))"
)


class ParameterService:
    def __init__(
        self,
        storage: Storage,
        model: ModelDefinition | None,
        llm: DeepSeekClient,
        router: ModelRouter | None = None,
        resolver: ModelDefinitionRegistry | None = None,
    ):
        self.storage = storage
        self.model = model
        self.llm = llm
        self.router = router
        self.resolver = resolver

    def create_session(self, user_id: str) -> dict[str, Any]:
        return self.storage.create_session(
            user_id, self.model.model.id if self.model is not None else None
        )

    @staticmethod
    def _is_explicit_model_switch(message: str) -> bool:
        return bool(_MODEL_SWITCH_LANGUAGE.search(message))

    def _resolve_model(self, model_id: str) -> ModelDefinition:
        if self.resolver is not None:
            return self.resolver.get(model_id)
        if self.model is not None and self.model.model.id == model_id:
            return self.model
        raise InvalidModelRoute(f"无法解析计算模型：{model_id}")

    def _route(self, message: str):
        """问一次路由器。返回 (model_id, None) 或 (None, 需要澄清的路由结果)。"""
        if self.router is None:
            raise InvalidModelRoute("当前 Session 尚未绑定模型，无法进行模型路由")
        route = self.router.route(message)
        if route.intent == CalculationIntent.CLARIFICATION_REQUIRED:
            return None, route
        return route.model_id, None

    def _extract(self, model: ModelDefinition, message: str, session_parameters: dict[str, Any]) -> ParameterExtraction:
        try:
            extracted = self.llm.extract_parameters(model, message, session_parameters)
            return ParameterExtraction.model_validate(extracted)
        except ValidationError as exc:
            raise LLMOutputInvalid("DeepSeek 参数提取结果不符合规定的 JSON Schema") from exc

    def process_message(
        self,
        user_id: str,
        session_id: str,
        message: str,
        explicit_model_id: str | None = None,
    ) -> dict[str, Any]:
        session = self.storage.get_session(session_id, user_id)
        if session is None:
            raise NotFoundError("会话不存在")
        # A new message invalidates any prior confirmation, but leaves its values
        # untouched until routing and extraction both pass validation.
        self.storage.update_session(
            session_id, user_id, session["parameters"], pending=False
        )

        current_model_id = session.get("model_id")
        session_parameters = normalize_legacy_parameter_values({}, session["parameters"])
        parsed: ParameterExtraction | None = None
        clarification_route = None

        if explicit_model_id is not None:
            if self.router is not None:
                self.router.validate_explicit(explicit_model_id)
            selected_model_id = explicit_model_id
        elif not current_model_id or self._is_explicit_model_switch(message):
            selected_model_id, clarification_route = self._route(message)
        else:
            # 会话已绑定模型、又没有显式切换语：先用**当前模型**抽一次参数。
            #
            # 抽到东西 = 正常的参数跟进，保持现有的「会话模型锁定」行为；
            # 什么都没抽到 = 这句话很可能根本不属于当前模型（例如用户换了一种
            # 计算的提法），这时才值得再问一次路由器 —— 否则绑定过的会话永远
            # 只能识别当初那个模型，自然语言说不动它。
            #
            # 但绑定的模型本身可能已经不存在了（配置改名、下线模型都是常见原因）。
            # 那种情况绝不能直接抛错：会话会永久卡死——每条消息都死在解析这一步，
            # 用户看不出原因，连切换语都走不到（切换语在解析之前就判完了）。
            # 把悬空绑定当成「未绑定」，重新问一次路由器。
            try:
                current_model = self._resolve_model(current_model_id)
            except Exception:
                current_model = None
            if current_model is None:
                selected_model_id, clarification_route = self._route(message)
            else:
                parsed = self._extract(current_model, message, session_parameters)
                if parsed.parameters or self.router is None:
                    selected_model_id = current_model_id
                else:
                    selected_model_id, clarification_route = self._route(message)

        if clarification_route is not None:
            return {
                "session_id": session_id,
                "model_id": current_model_id,
                "parameters": normalize_legacy_parameter_values({}, session["parameters"]),
                "missing_required": [],
                "needs_confirmation": False,
                "clarification": clarification_route.clarification,
                "candidate_models": clarification_route.candidate_models,
            }

        if not selected_model_id:
            raise InvalidModelRoute("Session 尚未解析到有效 model_id")
        try:
            selected_model = self._resolve_model(selected_model_id)
        except Exception as exc:
            if isinstance(exc, InvalidModelRoute):
                raise
            raise InvalidModelRoute(f"无法加载计算模型：{selected_model_id}") from exc

        switched_model = current_model_id != selected_model_id
        if switched_model:
            if not self.storage.bind_session_model(
                session_id,
                user_id,
                selected_model_id,
                clear_parameters=True,
            ):
                raise NotFoundError("会话不存在")
            session["parameters"] = {}
            session_parameters = {}
            # 换了模型，用旧模型抽出来的结果作废，必须按新模型的参数目录重抽
            parsed = None
        self.model = selected_model

        if parsed is None:
            parsed = self._extract(self.model, message, session_parameters)

        # Old persisted values are migrated at this boundary; current config
        # aliases are reserved for natural-language wording, never Excel IDs.
        raw_updates = normalize_legacy_parameter_values(
            {"schema_version": self.model.schema_version}, parsed.parameters
        )
        updates, _ = validate_parameters(self.model, raw_updates, require_all=False)

        global_preferences, model_preferences = self.storage.get_preferences(
            user_id, self.model.model.id
        )
        # 逐层合并时记下每个值的来源：前端必须把「本轮你给的」和「沿用上次的」
        # 区分开，否则用户会对一个自己根本没提过的旧值点「确认」。
        sources: dict[str, str] = {}
        merged: dict[str, Any] = {}
        layers = (
            ("default", {
                key: spec.missing.value
                for key, spec in self.model.parameters.items()
                if spec.missing.policy == "default"
            }),
            ("preference", normalize_legacy_parameter_values({}, global_preferences)),
            ("preference", normalize_legacy_parameter_values({}, model_preferences)),
            ("session", session_parameters),
            ("input", updates),
        )
        for label, layer in layers:
            for key, value in layer.items():
                merged[key] = value
                sources[key] = label
        validated, _ = validate_parameters(self.model, merged, require_all=False)
        missing = [
            key for key, spec in self.model.parameters.items()
            if spec.missing.policy == "error" and key not in validated
        ]
        self.storage.update_session(session_id, user_id, validated, pending=not missing)

        if missing:
            questions = parsed.clarification or ""
            if not questions:
                questions = "请补充以下必填参数：" + "、".join(
                    f"{self.model.parameters[key].display_name}（{self.model.parameters[key].description}）"
                    for key in missing
                )
            return {
                "session_id": session_id,
                "model_id": self.model.model.id,
                "parameters": validated,
                "parameter_sources": sources,
                "missing_required": missing,
                "needs_confirmation": False,
                "clarification": questions,
            }
        return {
            "session_id": session_id,
            "model_id": self.model.model.id,
            "parameters": validated,
            "parameter_sources": sources,
            "missing_required": [],
            "needs_confirmation": True,
            "clarification": parsed.clarification or "",
            "confirmation_summary": [
                {
                    "key": key,
                    "description": self.model.parameters[key].display_name,
                    "value": value,
                    "unit": self.model.parameters[key].unit,
                    "source": sources.get(key, "session"),
                }
                for key, value in validated.items()
            ],
        }

    def confirm(self, user_id: str, session_id: str) -> dict[str, Any]:
        session = self.storage.get_session(session_id, user_id)
        if session is None:
            raise NotFoundError("会话不存在")
        if self.model is None or session.get("model_id") != self.model.model.id:
            raise ConflictError("会话模型与当前 Calculation Model Definition 不一致")
        if not session["pending_confirmation"]:
            raise ConflictError("当前会话没有待确认的完整参数")
        parameters, missing = validate_parameters(
            self.model,
            normalize_legacy_parameter_values({}, session["parameters"]),
            require_all=True,
        )
        if missing:
            raise ConflictError("必填参数尚未补齐，不能开始计算")
        snapshot = self.model.model_dump(mode="json")
        return self.storage.enqueue_job(
            user_id=user_id,
            session_id=session_id,
            model_id=self.model.model.id,
            model_snapshot=snapshot,
            parameters=parameters,
        )

    def save_preferences(
        self, user_id: str, values: dict[str, Any], model_id: str | None = None
    ) -> dict[str, Any]:
        if model_id not in (None, self.model.model.id):
            raise ValueError(
                "模型偏好只能写入当前注入的模型定义："
                f"期望 {self.model.model.id!r}，收到 {model_id!r}"
            )
        validated, _ = validate_parameters(self.model, values, require_all=False)
        self.storage.set_preferences(user_id, validated, model_id=model_id)
        return {"scope": "model" if model_id else "global", "values": validated}

    def get_preferences(self, user_id: str) -> dict[str, Any]:
        global_values, model_values = self.storage.get_preferences(user_id, self.model.model.id)
        return {"global": global_values, "model": model_values}
