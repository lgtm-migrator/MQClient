"""Back-end using RabbitMQ."""

import logging
import time
from functools import partial
from typing import Any, AsyncGenerator, Callable, Optional, Union

import pika  # type: ignore

from .. import broker_client_interface, log_msgs
from ..broker_client_interface import (
    RETRY_DELAY,
    TIMEOUT_MILLIS_DEFAULT,
    TRY_ATTEMPTS,
    AlreadyClosedException,
    ClosingFailedException,
    ConnectingFailedException,
    Message,
    Pub,
    RawQueue,
    Sub,
)

LOGGER = logging.getLogger("mqclient.rabbitmq")

AMQP_ADDRESS_PREFIX = "amqp://"


class RabbitMQ(RawQueue):
    """Base RabbitMQ wrapper.

    Extends:
        RawQueue
    """

    def __init__(self, address: str, queue: str) -> None:
        super().__init__()
        self.address = address
        if not self.address.startswith(AMQP_ADDRESS_PREFIX):
            self.address = AMQP_ADDRESS_PREFIX + self.address
        self.queue = queue
        self.connection: Optional[pika.BlockingConnection] = None
        self.channel: Optional[pika.adapters.blocking_connection.BlockingChannel] = None

    async def connect(self) -> None:
        """Set up connection and channel."""
        await super().connect()
        LOGGER.info(f"Connecting with address={self.address}")
        self.connection = pika.BlockingConnection(
            pika.connection.URLParameters(self.address)
        )
        self.channel = self.connection.channel()

    async def close(self) -> None:
        """Close connection."""
        await super().close()

        if not self.channel:
            raise ClosingFailedException("No channel to close.")
        if not self.connection:
            raise ClosingFailedException("No connection to close.")
        if self.connection.is_closed:
            raise AlreadyClosedException()

        try:
            self.connection.close()
        except Exception as e:
            raise ClosingFailedException() from e

        if self.channel.is_open:
            LOGGER.warning("Channel remains open after connection close.")


class RabbitMQPub(RabbitMQ, Pub):
    """Wrapper around queue with delivery-confirm mode in the channel.

    Extends:
        RabbitMQ
        Pub
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        LOGGER.debug(f"{log_msgs.INIT_PUB} ({args}; {kwargs})")
        super().__init__(*args, **kwargs)

    async def connect(self) -> None:
        """Set up connection, channel, and queue.

        Turn on delivery confirmations.
        """
        LOGGER.debug(log_msgs.CONNECTING_PUB)
        await super().connect()

        if not self.channel:
            raise ConnectingFailedException("No channel to configure connection.")

        self.channel.queue_declare(queue=self.queue, durable=False)
        self.channel.confirm_delivery()

        LOGGER.debug(log_msgs.CONNECTED_PUB)

    async def close(self) -> None:
        """Close connection."""
        LOGGER.debug(log_msgs.CLOSING_PUB)
        await super().close()
        LOGGER.debug(log_msgs.CLOSED_PUB)

    async def send_message(self, msg: bytes) -> None:
        """Send a message on a queue.

        Args:
            address (str): address of queue
            name (str): name of queue on address

        Returns:
            RawQueue: queue
        """
        LOGGER.debug(log_msgs.SENDING_MESSAGE)
        if not self.channel:
            raise RuntimeError("queue is not connected")

        await try_call(
            self,
            partial(
                self.channel.basic_publish,
                exchange="",
                routing_key=self.queue,
                body=msg,
            ),
        )
        LOGGER.debug(log_msgs.SENT_MESSAGE)


class RabbitMQSub(RabbitMQ, Sub):
    """Wrapper around queue with prefetch-queue QoS.

    Extends:
        RabbitMQ
        Sub
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        LOGGER.debug(f"{log_msgs.INIT_SUB} ({args}; {kwargs})")
        super().__init__(*args, **kwargs)
        self.consumer_id = None
        self.prefetch = 1

    async def connect(self) -> None:
        """Set up connection, channel, and queue.

        Turn on prefetching.
        """
        LOGGER.debug(log_msgs.CONNECTING_SUB)
        await super().connect()

        if not self.channel:
            raise ConnectingFailedException("No channel to configure connection.")

        self.channel.queue_declare(queue=self.queue, durable=False)
        self.channel.basic_qos(prefetch_count=self.prefetch, global_qos=True)

        LOGGER.debug(log_msgs.CONNECTED_SUB)

    async def close(self) -> None:
        """Close connection.

        Also, channel will be canceled (rejects all pending ackable
        messages).
        """
        LOGGER.debug(log_msgs.CLOSING_SUB)
        await super().close()
        LOGGER.debug(log_msgs.CLOSED_SUB)

    @staticmethod
    def _to_message(  # type: ignore[override]  # noqa: F821 # pylint: disable=W0221
        method_frame: Optional[pika.spec.Basic.GetOk], body: Optional[Union[str, bytes]]
    ) -> Optional[Message]:
        """Transform RabbitMQ-Message to Message type."""
        if not method_frame or body is None:
            return None

        if isinstance(body, str):
            return Message(method_frame.delivery_tag, body.encode())
        else:
            return Message(method_frame.delivery_tag, body)

    async def get_message(
        self, timeout_millis: Optional[int] = TIMEOUT_MILLIS_DEFAULT
    ) -> Optional[Message]:
        """Get a message from a queue.

        NOTE - `timeout_millis` is not used.
        """
        LOGGER.debug(log_msgs.GETMSG_RECEIVE_MESSAGE)
        if not self.channel:
            raise RuntimeError("queue is not connected")

        method_frame, _, body = await try_call(
            self, partial(self.channel.basic_get, self.queue)
        )
        msg = RabbitMQSub._to_message(method_frame, body)

        if msg:
            LOGGER.debug(f"{log_msgs.GETMSG_RECEIVED_MESSAGE} ({msg.msg_id!r}).")
            return msg
        else:
            LOGGER.debug(log_msgs.GETMSG_NO_MESSAGE)
            return None

    async def ack_message(self, msg: Message) -> None:
        """Ack a message from the queue.

        Note that RabbitMQ acks messages in-order, so acking message 3
        of 3 in-progress messages will ack them all.
        """
        LOGGER.debug(log_msgs.ACKING_MESSAGE)
        if not self.channel:
            raise RuntimeError("queue is not connected")

        await try_call(self, partial(self.channel.basic_ack, msg.msg_id))
        LOGGER.debug(f"{log_msgs.ACKED_MESSAGE} ({msg.msg_id!r}).")

    async def reject_message(self, msg: Message) -> None:
        """Reject (nack) a message from the queue.

        Note that RabbitMQ acks messages in-order, so nacking message 3
        of 3 in-progress messages will nack them all.
        """
        LOGGER.debug(log_msgs.NACKING_MESSAGE)
        if not self.channel:
            raise RuntimeError("queue is not connected")

        await try_call(self, partial(self.channel.basic_nack, msg.msg_id))
        LOGGER.debug(f"{log_msgs.NACKED_MESSAGE} ({msg.msg_id!r}).")

    async def message_generator(
        self, timeout: int = 60, propagate_error: bool = True
    ) -> AsyncGenerator[Optional[Message], None]:
        """Yield Messages.

        Generate messages with variable timeout.
        Yield `None` on `throw()`.

        Keyword Arguments:
            timeout {int} -- timeout in seconds for inactivity (default: {60})
            propagate_error -- should errors from downstream kill the generator? (default: {True})
        """
        LOGGER.debug(log_msgs.MSGGEN_ENTERED)
        if not self.channel:
            raise RuntimeError("queue is not connected")

        msg = None
        try:
            gen = partial(self.channel.consume, self.queue, inactivity_timeout=timeout)

            async for method_frame, _, body in try_yield(self, gen):
                # get message
                msg = RabbitMQSub._to_message(method_frame, body)
                LOGGER.debug(log_msgs.MSGGEN_GET_NEW_MESSAGE)
                if not msg:
                    LOGGER.info(log_msgs.MSGGEN_NO_MESSAGE_LOOK_BACK_IN_QUEUE)
                    break

                # yield message to consumer
                try:
                    LOGGER.debug(f"{log_msgs.MSGGEN_YIELDING_MESSAGE} [{msg}]")
                    yield msg
                # consumer throws Exception...
                except Exception as e:  # pylint: disable=W0703
                    LOGGER.debug(log_msgs.MSGGEN_DOWNSTREAM_ERROR)
                    if propagate_error:
                        LOGGER.debug(log_msgs.MSGGEN_PROPAGATING_ERROR)
                        raise
                    LOGGER.warning(
                        f"{log_msgs.MSGGEN_EXCEPTED_DOWNSTREAM_ERROR} {e}.",
                        exc_info=True,
                    )
                    yield None  # hand back to consumer
                # consumer requests again, aka next()
                else:
                    pass

        # Garbage Collection (or explicit close(), or break in consumer's loop)
        except GeneratorExit:
            LOGGER.debug(log_msgs.MSGGEN_GENERATOR_EXITING)
            LOGGER.debug(log_msgs.MSGGEN_GENERATOR_EXITED)


async def try_call(queue: RabbitMQ, func: Callable[..., Any]) -> Any:
    """Try to call `func` and return value.

    Try up to `TRY_ATTEMPTS` times, for connection-related errors.
    """
    for i in range(TRY_ATTEMPTS):
        if i > 0:
            LOGGER.debug(
                f"{log_msgs.TRYCALL_CONNECTION_ERROR_TRY_AGAIN} (attempt #{i+1})..."
            )

        try:
            return func()
        except pika.exceptions.ConnectionClosedByBroker:
            LOGGER.debug(log_msgs.TRYCALL_CONNECTION_CLOSED_BY_BROKER)
        # Do not recover on channel errors
        except pika.exceptions.AMQPChannelError as err:
            LOGGER.error(f"{log_msgs.TRYCALL_RAISE_AMQP_CHANNEL_ERROR} {err}.")
            raise
        # Recover on all other connection errors
        except pika.exceptions.AMQPConnectionError:
            LOGGER.debug(log_msgs.TRYCALL_AMQP_CONNECTION_ERROR)

        await queue.close()
        time.sleep(RETRY_DELAY)
        await queue.connect()

    LOGGER.debug(log_msgs.TRYCALL_CONNECTION_ERROR_MAX_RETRIES)
    raise Exception("RabbitMQ connection error")


async def try_yield(
    queue: RabbitMQ, func: Callable[..., Any]
) -> AsyncGenerator[Any, None]:
    """Try to call `func` and yield value(s).

    Try up to `TRY_ATTEMPTS` times, for connection-related errors.
    """
    for i in range(TRY_ATTEMPTS):
        if i > 0:
            LOGGER.debug(
                f"{log_msgs.TRYYIELD_CONNECTION_ERROR_TRY_AGAIN} (attempt #{i+1})..."
            )

        try:
            for x in func():  # pylint: disable=invalid-name
                yield x
        except pika.exceptions.ConnectionClosedByBroker:
            LOGGER.debug(log_msgs.TRYYIELD_CONNECTION_CLOSED_BY_BROKER)
        # Do not recover on channel errors
        except pika.exceptions.AMQPChannelError as err:
            LOGGER.error(f"{log_msgs.TRYYIELD_RAISE_AMQP_CHANNEL_ERROR} {err}.")
            raise
        # Recover on all other connection errors
        except pika.exceptions.AMQPConnectionError:
            LOGGER.debug(log_msgs.TRYYIELD_AMQP_CONNECTION_ERROR)

        await queue.close()
        time.sleep(RETRY_DELAY)
        await queue.connect()

    LOGGER.debug(log_msgs.TRYYIELD_CONNECTION_ERROR_MAX_RETRIES)
    raise Exception("RabbitMQ connection error")


class BrokerClient(broker_client_interface.BrokerClient):
    """RabbitMQ Pub-Sub BrokerClient Factory.

    Extends:
        BrokerClient
    """

    NAME = "rabbitmq"

    @staticmethod
    async def create_pub_queue(
        address: str, name: str, auth_token: str = ""
    ) -> RabbitMQPub:
        """Create a publishing queue.

        # NOTE - `auth_token` is not used currently

        Args:
            address (str): address of queue
            name (str): name of queue on address

        Returns:
            RawQueue: queue
        """
        q = RabbitMQPub(address, name)  # pylint: disable=invalid-name
        await q.connect()
        return q

    @staticmethod
    async def create_sub_queue(
        address: str, name: str, prefetch: int = 1, auth_token: str = ""
    ) -> RabbitMQSub:
        """Create a subscription queue.

        # NOTE - `auth_token` is not used currently

        Args:
            address (str): address of queue
            name (str): name of queue on address

        Returns:
            RawQueue: queue
        """
        q = RabbitMQSub(address, name)  # pylint: disable=invalid-name
        q.prefetch = prefetch
        await q.connect()
        return q
