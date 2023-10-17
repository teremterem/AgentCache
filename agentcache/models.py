"""Data models."""
import hashlib
import typing
from abc import ABC, abstractmethod
from typing import AsyncIterator, Iterator, Dict, Any, Literal, Type, Tuple, List, Optional

from pydantic import BaseModel, model_validator, PrivateAttr, ConfigDict

from agentcache.typing import MessageType
from agentcache.utils import Broadcastable

if typing.TYPE_CHECKING:
    from agentcache.message_tree import MessageTree

_PRIMITIVES_ALLOWED_IN_IMMUTABLE = (str, int, float, bool, type(None))


class Immutable(BaseModel):
    """
    A base class for immutable pydantic objects. It is frozen and has a git-style hash key that is calculated from the
    JSON representation of the object.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    ac_model_: str  # AgentCache model name

    @property
    def hash_key(self) -> str:
        """Get the hash key for this object. It is a hash of the JSON representation of the object."""
        if not hasattr(self, "_hash_key"):
            # pylint: disable=attribute-defined-outside-init
            # noinspection PyAttributeOutsideInit
            self._hash_key = hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()
        return self._hash_key

    # noinspection PyNestedDecorators
    @model_validator(mode="before")
    @classmethod
    def _validate_immutable_fields(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively make sure that the field values of the object are immutable."""
        for key, value in values.items():
            cls._validate_value(key, value)
        return values

    @classmethod
    def _validate_value(cls, key: str, value: Any) -> None:
        """Recursively make sure that the field value is immutable."""
        if isinstance(value, tuple):
            for sub_value in value:
                cls._validate_value(key, sub_value)
        elif not isinstance(value, cls._allowed_value_types()):
            raise ValueError(
                f"only {{{', '.join([t.__name__ for t in cls._allowed_value_types()])}}} "
                f"are allowed as field values in {cls.__name__}, got {type(value).__name__} in `{key}`"
            )

    @classmethod
    def _allowed_value_types(cls) -> Tuple[Type[Any], ...]:
        return _TYPES_ALLOWED_IN_IMMUTABLE


_TYPES_ALLOWED_IN_IMMUTABLE = *_PRIMITIVES_ALLOWED_IN_IMMUTABLE, Immutable


class Metadata(Immutable):
    """Metadata for a message. Supports arbitrary fields."""

    model_config = ConfigDict(extra="allow")
    ac_model_: Literal["metadata"] = "metadata"

    @classmethod
    def _allowed_value_types(cls) -> Tuple[Type[Any], ...]:
        return _TYPES_ALLOWED_IN_METADATA


_TYPES_ALLOWED_IN_METADATA = *_PRIMITIVES_ALLOWED_IN_IMMUTABLE, Metadata


class Message(Immutable):
    """A message."""

    ac_model_: Literal["message"] = "message"
    _message_tree: "MessageTree" = PrivateAttr()  # set by MessageTree.anew_message()
    content: str
    metadata: Metadata = Metadata()  # empty metadata by default
    prev_msg_hash_key: Optional[str] = None

    @property
    def message_tree(self) -> "MessageTree":
        """Get the message tree that this message belongs to."""
        return self._message_tree

    async def aget_previous_message(self) -> Optional["Message"]:
        """Get the previous message in the conversation."""
        if not self.prev_msg_hash_key:
            return None

        if not hasattr(self, "_prev_msg"):
            # pylint: disable=attribute-defined-outside-init
            # noinspection PyAttributeOutsideInit
            self._prev_msg = await self.message_tree.afind_message(self.prev_msg_hash_key)
        return self._prev_msg

    async def areply(self, content: str, metadata: Optional[Metadata] = None) -> "Message":
        """Reply to this message."""
        return await self.message_tree.anew_message(
            content=content, metadata=metadata, prev_msg_hash_key=self.hash_key
        )

    async def aget_full_chat(self) -> List["Message"]:
        """Get the full chat history for this message (including this message)."""
        # TODO Oleksandr: introduce a limit on the number of messages to fetch
        msg = self
        result = [msg]
        while msg := await msg.aget_previous_message():
            result.append(msg)
        result.reverse()
        return result


# TODO Oleksandr: introduce ErrorMessage for cases when something goes wrong (or maybe make it a part of Message ?)


class Token(Immutable):
    """
    A token. This class is used by StreamedMessage (when the message is streamed token by token instead of being
    returned all at once).
    """

    ac_model_: Literal["token"] = "token"
    text: str


class StreamedMessage(ABC):
    """A message that is streamed token by token instead of being returned all at once."""

    @abstractmethod
    def get_full_message(self) -> Message:
        """
        Get the full message. This method will "await" until all the tokens are received and then return the complete
        message.
        """

    @abstractmethod
    async def aget_full_message(self) -> Message:
        """
        Get the full message. This method will "await" until all the tokens are received and then return the complete
        message (async version).
        """

    @abstractmethod
    def __next__(self) -> Token:
        """Get the next token of a message that is being streamed."""

    @abstractmethod
    async def __anext__(self) -> Token:
        """Get the next token of a message that is being streamed (async version)."""

    def __aiter__(self) -> AsyncIterator[Token]:
        return self

    def __iter__(self) -> Iterator[Token]:
        return self


class MessageBundle(Broadcastable[MessageType]):
    """A bundle of messages. Used to group messages that are sent together."""

    def __init__(self, *args, bundle_metadata: Optional[Metadata] = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.bundle_metadata: Metadata = bundle_metadata or Metadata()
