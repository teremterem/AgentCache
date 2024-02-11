# pylint: disable=too-many-arguments,too-many-instance-attributes,protected-access
"""
The Forum class is the main entry point for the agentforum library. It is used to create a forum, register agents in
it, and call agents. The Forum class is also responsible for storing messages in the forum (it uses ForumTrees
for that).
"""
import asyncio
import contextlib
import contextvars
import logging
import typing
from contextvars import ContextVar
from functools import cached_property
from typing import Optional, Union, Callable

from pydantic import BaseModel, ConfigDict, PrivateAttr, Field

from agentforum.conversations import ConversationTracker
from agentforum.errors import NoAskingAgentError
from agentforum.models import Immutable
from agentforum.promises import MessagePromise, AsyncMessageSequence, AgentCallMsgPromise
from agentforum.storage.trees import ForumTrees
from agentforum.storage.trees_impl import InMemoryTrees
from agentforum.utils import Sentinel, NO_VALUE, USER_ALIAS

if typing.TYPE_CHECKING:
    from agentforum.typing import AgentFunction, MessageType

logger = logging.getLogger(__name__)


class Forum(BaseModel):
    """
    A forum for agents to communicate. Messages in the forum assemble in a tree-like structure.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    forum_trees: ForumTrees = Field(default_factory=InMemoryTrees)
    _conversations: dict[str, ConversationTracker] = PrivateAttr(default_factory=dict)

    def agent(
        self,
        func: Optional["AgentFunction"] = None,
        alias: Optional[str] = None,
        description: Optional[str] = None,
        uppercase_func_name: bool = True,
        normalize_spaces_in_docstring: bool = True,
    ) -> Union["Agent", Callable[["AgentFunction"], "Agent"]]:
        """
        A decorator that registers an agent function in the forum.
        """
        if func is None:
            # the decorator `@forum.agent(...)` was used with arguments
            def _decorator(f: "AgentFunction") -> "Agent":
                return Agent(
                    self,
                    f,
                    alias=alias,
                    description=description,
                    uppercase_func_name=uppercase_func_name,
                    normalize_spaces_in_docstring=normalize_spaces_in_docstring,
                )

            return _decorator

        # the decorator `@forum.agent` was used either without arguments or as a direct function call
        return Agent(
            self,
            func,
            alias=alias,
            description=description,
            uppercase_func_name=uppercase_func_name,
            normalize_spaces_in_docstring=normalize_spaces_in_docstring,
        )

    def get_conversation(
        self, descriptor: Immutable, branch_from_if_new: Optional[Union[MessagePromise, Sentinel]] = None
    ) -> ConversationTracker:
        """
        Get a ConversationTracker object that tracks the tip of a conversation branch. If the conversation doesn't
        exist yet, it will be created. If branch_from_if_new is specified, the conversation will be branched off of
        that message promise (as long as the conversation doesn't exist yet). Descriptor is used to uniquely identify
        the conversation. It can be an arbitrary Immutable object - its hash_key will be used to identify the
        conversation.
        """
        conversation = self._conversations.get(descriptor.hash_key)
        if not conversation:
            conversation = ConversationTracker(forum=self, branch_from=branch_from_if_new)
            self._conversations[descriptor.hash_key] = conversation

        return conversation

    @cached_property
    def _user_agent(self) -> "Agent":
        """
        A special agent that represents the user. It is used to call other agents from the user's perspective.
        """
        return Agent(self, None, alias=USER_ALIAS)

    @cached_property
    def _user_interaction_context(self) -> "InteractionContext":
        """
        A special interaction context that represents the user. It is used to call other agents from the user's
        perspective.
        """
        return InteractionContext(
            forum=self,
            agent=self._user_agent,
            request_messages=None,
            response_producer=None,
        )


_CURRENT_FORUM: ContextVar[Optional["Forum"]] = ContextVar("_CURRENT_FORUM", default=None)


# noinspection PyProtectedMember
class Agent:
    """
    A wrapper for an agent function that allows calling the agent.
    """

    def __init__(
        self,
        forum: Forum,
        func: Optional["AgentFunction"],
        alias: Optional[str] = None,
        description: Optional[str] = None,
        uppercase_func_name: bool = True,
        normalize_spaces_in_docstring: bool = True,
    ) -> None:
        self.forum = forum
        self._func = func

        self.alias = alias
        if self.alias is None:
            self.alias = func.__name__
            if uppercase_func_name:
                self.alias = self.alias.upper()

        self.description = description
        if self.description is None:
            self.description = func.__doc__
            if self.description and normalize_spaces_in_docstring:
                self.description = " ".join(self.description.split())
        if self.description:
            # replace all {AGENT_ALIAS} entries in the description with the actual agent alias
            self.description = self.description.format(AGENT_ALIAS=self.alias)

        self.__name__ = self.alias
        self.__doc__ = self.description

    def ask(
        self,
        content: Optional["MessageType"] = None,
        override_sender_alias: Optional[str] = None,
        branch_from: Optional[MessagePromise] = None,
        conversation: Optional[ConversationTracker] = None,
        force_new_conversation: bool = False,
        **function_kwargs,
    ) -> "AsyncMessageSequence":
        """
        "Ask" the agent and immediately receive an AsyncMessageSequence object that can be used to obtain the agent's
        response(s). If force_new_conversation is False and conversation is not specified and pre-existing messages are
        passed as requests (for ex. messages that came from other agents), then this agent call will be automatically
        branched off of the conversation branch those pre-existing messages belong to (the history will be inherited
        from those messages, in other words).
        """
        return self._call(
            is_asking=True,
            content=content,
            override_sender_alias=override_sender_alias,
            branch_from=branch_from,
            conversation=conversation,
            force_new_conversation=force_new_conversation,
            **function_kwargs,
        )

    def start_asking(
        self,
        branch_from: Optional[MessagePromise] = None,
        conversation: Optional[ConversationTracker] = None,
        force_new_conversation: bool = False,
        **function_kwargs,
    ) -> "AgentCall":
        """
        Initiate the process of "asking" the agent. Returns an AgentCall object that can be used to send requests to
        the agent by calling `send_request()` zero or more times and receive its responses by calling
        `response_sequence()` at the end. If force_new_conversation is False and conversation is not specified and
        pre-existing messages are passed as requests (for ex. messages that came from other agents), then this agent
        call will be automatically branched off of the conversation branch those pre-existing messages belong to (the
        history will be inherited from those messages, in other words).
        """
        return self._start_call(
            is_asking=True,
            branch_from=branch_from,
            conversation=conversation,
            force_new_conversation=force_new_conversation,
            **function_kwargs,
        )

    def tell(
        self,
        content: Optional["MessageType"] = None,
        override_sender_alias: Optional[str] = None,
        branch_from: Optional[MessagePromise] = None,
        conversation: Optional[ConversationTracker] = None,
        force_new_conversation: bool = False,
        **function_kwargs,
    ) -> None:
        """
        "Tell" the agent. Does not return anything, because it's a one-way communication. If force_new_conversation
        is False and conversation is not specified and pre-existing messages are passed as requests (for ex. messages
        that came from other agents), then this agent call will be automatically branched off of the conversation
        branch those pre-existing messages belong to (the history will be inherited from those messages, in other
        words).
        """
        self._call(
            is_asking=False,
            content=content,
            override_sender_alias=override_sender_alias,
            branch_from=branch_from,
            conversation=conversation,
            force_new_conversation=force_new_conversation,
            **function_kwargs,
        )

    def start_telling(
        self,
        branch_from: Optional[MessagePromise] = None,
        conversation: Optional[ConversationTracker] = None,
        force_new_conversation: bool = False,
        **function_kwargs,
    ) -> "AgentCall":
        """
        Initiate the process of "telling" the agent. Returns an AgentCall object that can be used to send requests to
        the agent by calling `send_request()` zero or more times and calling `finish()` at the end. If
        force_new_conversation is False and conversation is not specified and pre-existing messages are passed as
        requests (for ex. messages that came from other agents), then this agent call will be automatically branched
        off of the conversation branch those pre-existing messages belong to (the history will be inherited from those
        messages, in other words).
        """
        return self._start_call(
            is_asking=False,
            branch_from=branch_from,
            conversation=conversation,
            force_new_conversation=force_new_conversation,
            **function_kwargs,
        )

    def _call(
        self,
        is_asking: bool,
        content: Optional["MessageType"] = None,
        override_sender_alias: Optional[str] = None,
        branch_from: Optional[MessagePromise] = None,
        conversation: Optional[ConversationTracker] = None,
        force_new_conversation: bool = False,
        **function_kwargs,
    ) -> Optional["AsyncMessageSequence"]:
        agent_call = self._start_call(
            is_asking=is_asking,
            branch_from=branch_from,
            conversation=conversation,
            force_new_conversation=force_new_conversation,
            **function_kwargs,
        )
        if content is not None:
            if override_sender_alias:
                agent_call.send_request(content, final_sender_alias=override_sender_alias)
            else:
                agent_call.send_request(content)

        if is_asking:
            return agent_call.response_sequence()
        agent_call.finish()
        return None

    def _start_call(
        self,
        is_asking: bool,
        branch_from: Optional[MessagePromise] = None,
        conversation: Optional[ConversationTracker] = None,
        force_new_conversation: bool = False,  # TODO TODO TODO Oleksandr: rename to just `new_conversation` ?
        **function_kwargs,
    ) -> "AgentCall":
        prev_forum_token = None
        try:
            if self.forum is not _CURRENT_FORUM.get():
                prev_forum_token = _CURRENT_FORUM.set(self.forum)

            if branch_from and conversation:
                raise ValueError(
                    "Cannot specify both `conversation` and `branch_from` in `Agent.call()` or `Agent.quick_call()`"
                )
            if branch_from:
                conversation = ConversationTracker(self.forum, branch_from=branch_from)

            if conversation:
                if conversation.has_prior_history and force_new_conversation:
                    raise ValueError(
                        "Cannot force a new conversation when there is prior history in `ConversationTracker`"
                    )
            else:
                # TODO TODO TODO TODO TODO
                conversation = ConversationTracker(self.forum, branch_from=NO_VALUE)

            agent_call = AgentCall(
                forum=self.forum,
                conversation=conversation,
                receiving_agent=self,
                is_asking=is_asking,
                do_not_forward_if_possible=not force_new_conversation,
                **function_kwargs,
            )
            agent_call._task = asyncio.create_task(
                self._acall_non_cached_agent_func(agent_call=agent_call, **function_kwargs)
            )
            parent_ctx = InteractionContext.get_current_context()  # TODO TODO TODO
            parent_ctx._child_agent_calls.append(agent_call)

            return agent_call
        finally:
            if prev_forum_token:
                _CURRENT_FORUM.reset(prev_forum_token)

    async def _acall_non_cached_agent_func(self, agent_call: "AgentCall", **function_kwargs) -> None:
        with agent_call._response_producer or contextlib.nullcontext():
            async with InteractionContext(
                forum=self.forum,
                agent=self,
                request_messages=agent_call._request_messages,
                response_producer=agent_call._response_producer,
            ) as ctx:
                try:
                    if self._func:
                        await self._func(ctx, **function_kwargs)
                except BaseException as exc:  # pylint: disable=broad-except
                    logger.debug("AGENT FUNCTION OF %s RAISED AN EXCEPTION:", self.alias, exc_info=True)
                    ctx.respond(exc)


# noinspection PyProtectedMember
class InteractionContext:
    """
    A context within which an agent is called. This is needed for things like looking up a sender alias for a message
    that is being created by the agent, so it can be populated in the message automatically (and other similar things).
    """

    _current_context: ContextVar[Optional["InteractionContext"]] = ContextVar("_current_context", default=None)

    def __init__(
        self,
        forum: "Forum",
        agent: Agent,
        request_messages: Optional[AsyncMessageSequence],
        response_producer: Optional["AsyncMessageSequence._MessageProducer"],
    ) -> None:
        self.forum = forum
        self.this_agent = agent
        self.request_messages = request_messages
        self.parent_context: Optional["InteractionContext"] = self._current_context.get()

        self._response_producer = response_producer
        self._child_agent_calls: list[AgentCall] = []
        self._previous_ctx_token: Optional[contextvars.Token] = None

    @property
    def was_asked(self) -> bool:
        """
        Check if this agent was "asked", and not just "told" (the calling agent is waiting for a response).
        """
        return bool(self._response_producer)

    def respond(self, content: "MessageType", **metadata) -> None:
        """
        Respond with a message or a sequence of messages.
        """
        if self.was_asked:
            self._response_producer.send_zero_or_more_messages(content, **metadata)
        else:
            self.get_asked_context().respond(content, **metadata)

    @classmethod
    def get_current_context(cls) -> Optional["InteractionContext"]:
        """Get the current InteractionContext object."""
        return cls._current_context.get() or _CURRENT_FORUM.get()._user_interaction_context

    def get_asked_context(self) -> "InteractionContext":
        """
        Get the InteractionContext object produced by an "asking" (rather than "telling") agent. If there is no asking
        agent, raises NoAskingAgentError.
        """
        ctx = self
        while ctx:
            if ctx.was_asked:
                return ctx
            ctx = ctx.parent_context
        raise NoAskingAgentError("There is no agent up the chain of parent contexts that is currently asking")

    @classmethod
    def get_current_sender_alias(cls) -> str:
        """
        Get the sender alias from the current InteractionContext object.
        """
        return cls.get_current_context().this_agent.alias

    async def __aenter__(self) -> "InteractionContext":
        """
        Set this context as the current context.
        """
        if self._previous_ctx_token:
            raise RuntimeError("InteractionContext is not reentrant")
        self._previous_ctx_token = self._current_context.set(self)  # <- this is the context switch
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        Restore the context that was current before this one.
        """
        for child_agent_call in self._child_agent_calls:
            # Just in case any of the child agent calls weren't explicitly finished, finish them now>
            # NOTE: "Finish" here doesn't mean finishing the agent function run, it means finishing the act of calling
            # the agent (i.e. the act of sending requests to the agent).
            child_agent_call.finish()
        # And here we wait for all the child agent functions to actually finish (before we let the parent context to
        # end).
        await asyncio.gather(
            *(child_agent_call._task for child_agent_call in self._child_agent_calls if child_agent_call._task),
            return_exceptions=True,  # this prevents waiting until the first exception and then giving up
        )
        self._current_context.reset(self._previous_ctx_token)
        self._previous_ctx_token = None


# noinspection PyProtectedMember
class AgentCall:
    """
    A call to an agent. This object is returned by Agent.call() method. It is used to send requests to the agent and
    receive its responses.
    """

    def __init__(
        self,
        forum: Forum,
        conversation: ConversationTracker,
        receiving_agent: Agent,
        is_asking: bool,
        do_not_forward_if_possible: bool = True,  # TODO TODO TODO Oleksandr: this concept should go away
        **function_kwargs,
    ) -> None:
        self.forum = forum
        self.receiving_agent = receiving_agent
        self.is_asking = is_asking

        # TODO TODO TODO Oleksandr: either explain this temporary_sub_conversation in a comment or refactor it
        #  completely when you get to implementing cached agent calls
        # TODO TODO TODO
        temporary_sub_conversation = ConversationTracker(forum=forum, branch_from=conversation._latest_msg_promise)

        self._request_messages = AsyncMessageSequence(
            temporary_sub_conversation,
            default_sender_alias=InteractionContext.get_current_sender_alias(),
            do_not_forward_if_possible=do_not_forward_if_possible,
        )
        self._request_producer = AsyncMessageSequence._MessageProducer(self._request_messages)

        self._task: Optional[asyncio.Task] = None

        agent_call_msg_promise = AgentCallMsgPromise(
            forum=self.forum,
            request_messages=self._request_messages,
            receiving_agent_alias=self.receiving_agent.alias,
            **function_kwargs,
        )
        # TODO TODO TODO
        conversation._latest_msg_promise = agent_call_msg_promise

        if is_asking:
            # TODO TODO TODO Oleksandr: switch to branch_from=NO_VALUE and employ reply_to (reply to AgentCallMsg) ?
            self._response_messages = AsyncMessageSequence(
                conversation, default_sender_alias=self.receiving_agent.alias
            )
            self._response_producer = AsyncMessageSequence._MessageProducer(self._response_messages)
        else:
            self._response_messages = None
            self._response_producer = None

    def send_request(self, content: "MessageType", **metadata) -> "AgentCall":
        """
        Send a request to the agent.
        """
        self._request_producer.send_zero_or_more_messages(content, **metadata)
        return self

    def response_sequence(self) -> "AsyncMessageSequence":
        """
        Finish the agent call and return the agent's response(s).

        NOTE: After this method is called it is not possible to send any more requests to this AgentCall object.
        """
        if not self.is_asking:
            raise NoAskingAgentError(
                "Cannot get response sequence for an agent call that is not asking, "
                "use ask()/start_asking() instead of tell()/start_telling()"
            )
        self.finish()
        return self._response_messages

    def finish(self) -> "AgentCall":
        """
        Finish the agent call.

        NOTE: After this method is called it is not possible to send any more requests to this AgentCall object.
        """
        self._request_producer.close()
        return self
