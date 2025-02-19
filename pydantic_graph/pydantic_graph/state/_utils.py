from __future__ import annotations as _annotations

from collections.abc import Sequence
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Annotated, Any, Union

import pydantic
from pydantic_core import core_schema

from ..nodes import BaseNode

nodes_schema_var: ContextVar[Sequence[type[BaseNode[Any, Any, Any]]]] = ContextVar('nodes_var')


class CustomNodeSchema:
    def __get_pydantic_core_schema__(
        self, _source_type: Any, handler: pydantic.GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        try:
            nodes = nodes_schema_var.get()
        except LookupError as e:
            raise RuntimeError(
                'Unable to build a Pydantic schema for `NodeStep` without setting `nodes_schema_var`. '
                'You probably want to use TODO'
            ) from e
        if len(nodes) == 1:
            nodes_type = nodes[0]
        else:
            nodes_annotated = [Annotated[node, pydantic.Tag(node.get_id())] for node in nodes]
            nodes_type = Annotated[Union[tuple(nodes_annotated)], pydantic.Discriminator(self._node_discriminator)]

        schema = handler(nodes_type)
        schema['serialization'] = core_schema.wrap_serializer_function_ser_schema(
            function=self._node_serializer,
            return_schema=core_schema.dict_schema(core_schema.str_schema(), core_schema.any_schema()),
        )
        return schema

    @staticmethod
    def _node_discriminator(node_data: Any) -> str:
        return node_data.get('node_id')

    @staticmethod
    def _node_serializer(node: Any, handler: pydantic.SerializerFunctionWrapHandler) -> dict[str, Any]:
        node_dict = handler(node)
        node_dict['node_id'] = node.get_id()
        return node_dict


def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)
