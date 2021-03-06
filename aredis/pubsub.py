import asyncio
import threading
from asyncio.futures import CancelledError
import time
from aredis.exceptions import PubSubError, ConnectionError, TimeoutError
from aredis.utils import (list_or_args,
                          iteritems,
                          iterkeys,
                          nativestr)


class PubSub(object):
    """
    PubSub provides publish, subscribe and listen support to Redis channels.

    After subscribing to one or more channels, the listen() method will block
    until a message arrives on one of the subscribed channels. That message
    will be returned and it's safe to start listening again.
    """
    PUBLISH_MESSAGE_TYPES = ('message', 'pmessage')
    UNSUBSCRIBE_MESSAGE_TYPES = ('unsubscribe', 'punsubscribe')

    def __init__(self, connection_pool, ignore_subscribe_messages=False):
        self.connection_pool = connection_pool
        self.ignore_subscribe_messages = ignore_subscribe_messages
        self.connection = None
        # we need to know the encoding options for this connection in order
        # to lookup channel and pattern names for callback handlers.
        conn = connection_pool.get_connection('pubsub')
        try:
            self.encoding = conn.encoding
            self.decode_responses = conn.decode_responses
        finally:
            connection_pool.release(conn)
        self.reset()

    def __del__(self):
        try:
            # if this object went out of scope prior to shutting down
            # subscriptions, close the connection manually before
            # returning it to the connection pool
            self.reset()
        except Exception:
            pass

    def reset(self):
        if self.connection:

            self.connection.disconnect()
            self.connection.clear_connect_callbacks()
            self.connection_pool.release(self.connection)

            self.connection = None
        self.channels = {}
        self.patterns = {}

    def close(self):
        self.reset()

    async def on_connect(self, connection):
        "Re-subscribe to any channels and patterns previously subscribed to"
        # NOTE: for python3, we can't pass bytestrings as keyword arguments
        # so we need to decode channel/pattern names back to str strings
        # before passing them to [p]subscribe.
        if self.channels:
            channels = {}
            for k, v in iteritems(self.channels):
                if not self.decode_responses:
                    k = k.decode(self.encoding)
                channels[k] = v
            await self.subscribe(**channels)
        if self.patterns:
            patterns = {}
            for k, v in iteritems(self.patterns):
                if not self.decode_responses:
                    k = k.decode(self.encoding)
                patterns[k] = v
            await self.psubscribe(**patterns)

    def encode(self, value):
        """
        Encode the value so that it's identical to what we'll
        read off the connection
        """
        if self.decode_responses and isinstance(value, bytes):
            value = value.decode(self.encoding)
        elif not self.decode_responses and isinstance(value, str):
            value = value.encode(self.encoding)
        return value

    @property
    def subscribed(self):
        "Indicates if there are subscriptions to any channels or patterns"
        return bool(self.channels or self.patterns)

    async def execute_command(self, *args, **kwargs):
        "Execute a publish/subscribe command"

        # NOTE: don't parse the response in this function -- it could pull a
        # legitimate message off the stack if the connection is already
        # subscribed to one or more channels

        if self.connection is None:
            self.connection = self.connection_pool.get_connection()
            # register a callback that re-subscribes to any channels we
            # were listening to when we were disconnected
            self.connection.register_connect_callback(self.on_connect)
        connection = self.connection
        await self._execute(connection, connection.send_command, *args)

    async def _execute(self, connection, command, *args):
        try:
            return await command(*args)
        except CancelledError:
            # do not retry if coroutine is cancelled
            if await connection.can_read():
                # disconnect if buffer is not empty in case of error
                # when connection is reused
                connection.disconnect()
            return None
        except (ConnectionError, TimeoutError) as e:
            connection.disconnect()
            if not connection.retry_on_timeout and isinstance(e, TimeoutError):
                raise
            # Connect manually here. If the Redis server is down, this will
            # fail and raise a ConnectionError as desired.
            try:
                await connection.connect()
            except Exception as e:
                connection.clear_connect_callbacks()
                raise e
            # the ``on_connect`` callback should haven been called by the
            # connection to resubscribe us to any channels and patterns we were
            # previously listening to
            return await command(*args)

    async def parse_response(self, block=True, timeout=0):
        "Parse the response from a publish/subscribe command"
        connection = self.connection
        if connection is None:
            raise RuntimeError(
                'pubsub connection not set: '
                'did you forget to call subscribe() or psubscribe()?')
        coro = self._execute(connection, connection.read_response)
        if not block and timeout > 0:
            try:
                return await asyncio.wait_for(coro, timeout)
            except Exception:
                return None
        return await coro

    async def psubscribe(self, *args, **kwargs):
        """
        Subscribe to channel patterns. Patterns supplied as keyword arguments
        expect a pattern name as the key and a callable as the value. A
        pattern's callable will be invoked automatically when a message is
        received on that pattern rather than producing a message via
        ``listen()``.
        """
        if args:
            args = list_or_args(args[0], args[1:])
        new_patterns = {}
        new_patterns.update(dict.fromkeys(map(self.encode, args)))
        for pattern, handler in iteritems(kwargs):
            new_patterns[self.encode(pattern)] = handler
        ret_val = await self.execute_command('PSUBSCRIBE', *iterkeys(new_patterns))
        # update the patterns dict AFTER we send the command. we don't want to
        # subscribe twice to these patterns, once for the command and again
        # for the reconnection.
        self.patterns.update(new_patterns)
        return ret_val

    async def punsubscribe(self, *args):
        """
        Unsubscribe from the supplied patterns. If empy, unsubscribe from
        all patterns.
        """
        if args:
            args = list_or_args(args[0], args[1:])
        return await self.execute_command('PUNSUBSCRIBE', *args)

    async def subscribe(self, *args, **kwargs):
        """
        Subscribe to channels. Channels supplied as keyword arguments expect
        a channel name as the key and a callable as the value. A channel's
        callable will be invoked automatically when a message is received on
        that channel rather than producing a message via ``listen()`` or
        ``get_message()``.
        """
        if args:
            args = list_or_args(args[0], args[1:])
        new_channels = {}
        new_channels.update(dict.fromkeys(map(self.encode, args)))
        for channel, handler in iteritems(kwargs):
            new_channels[self.encode(channel)] = handler
        ret_val = await self.execute_command('SUBSCRIBE', *iterkeys(new_channels))
        # update the channels dict AFTER we send the command. we don't want to
        # subscribe twice to these channels, once for the command and again
        # for the reconnection.
        self.channels.update(new_channels)
        return ret_val

    async def unsubscribe(self, *args):
        """
        Unsubscribe from the supplied channels. If empty, unsubscribe from
        all channels
        """
        if args:
            args = list_or_args(args[0], args[1:])
        return await self.execute_command('UNSUBSCRIBE', *args)

    async def listen(self):
        "Listen for messages on channels this client has been subscribed to"
        if self.subscribed:
            return self.handle_message(await self.parse_response(block=True))

    async def get_message(self, ignore_subscribe_messages=False, timeout=0):
        """
        Get the next message if one is available, otherwise None.

        If timeout is specified, the system will wait for `timeout` seconds
        before returning. Timeout should be specified as a floating point
        number.
        """
        response = await self.parse_response(block=False, timeout=timeout)
        if response:
            return self.handle_message(response, ignore_subscribe_messages)
        return None

    def handle_message(self, response, ignore_subscribe_messages=False):
        """
        Parses a pub/sub message. If the channel or pattern was subscribed to
        with a message handler, the handler is invoked instead of a parsed
        message being returned.
        """
        message_type = nativestr(response[0])
        if message_type == 'pmessage':
            message = {
                'type': message_type,
                'pattern': response[1],
                'channel': response[2],
                'data': response[3]
            }
        else:
            message = {
                'type': message_type,
                'pattern': None,
                'channel': response[1],
                'data': response[2]
            }

        # if this is an unsubscribe message, remove it from memory
        if message_type in self.UNSUBSCRIBE_MESSAGE_TYPES:
            subscribed_dict = None
            if message_type == 'punsubscribe':
                subscribed_dict = self.patterns
            else:
                subscribed_dict = self.channels
            try:
                del subscribed_dict[message['channel']]
            except KeyError:
                pass

        if message_type in self.PUBLISH_MESSAGE_TYPES:
            # if there's a message handler, invoke it
            handler = None
            if message_type == 'pmessage':
                handler = self.patterns.get(message['pattern'], None)
            else:
                handler = self.channels.get(message['channel'], None)
            if handler:
                handler(message)
                return None
        else:
            # this is a subscribe/unsubscribe message. ignore if we don't
            # want them
            if ignore_subscribe_messages or self.ignore_subscribe_messages:
                return None

        return message

    def run_in_thread(self, daemon=False):
        for channel, handler in iteritems(self.channels):
            if handler is None:
                raise PubSubError("Channel: '{}' has no handler registered"
                                  .format(channel))
        for pattern, handler in iteritems(self.patterns):
            if handler is None:
                raise PubSubError("Pattern: '{}' has no handler registered"
                                  .format(pattern))
        thread = PubSubWorkerThread(self, daemon=daemon)
        thread.start()
        return thread


class PubSubWorkerThread(threading.Thread):
    def __init__(self, pubsub, daemon=False):
        super(PubSubWorkerThread, self).__init__()
        self.daemon = daemon
        self.pubsub = pubsub
        self._running = False
        # Make sure we have the current thread loop before we
        # fork into the new thread. If not loop has been set on the connection
        # pool use the current default event loop.
        self.loop = pubsub.connection_pool.loop or asyncio.get_event_loop()

    async def _run(self):
        pubsub = self.pubsub
        while pubsub.subscribed:
            await pubsub.get_message(ignore_subscribe_messages=True)
        pubsub.close()
        self._running = False

    def run(self):
        if self._running:
            return
        self._running = True
        future = asyncio.run_coroutine_threadsafe(self._run(), self.loop)
        return future.result()

    def stop(self):
        # stopping simply unsubscribes from all channels and patterns.
        # the unsubscribe responses that are generated will short circuit
        # the loop in run(), calling pubsub.close() to clean up the connection
        if self.loop:
            unsubscribed = asyncio.run_coroutine_threadsafe(self.pubsub.unsubscribe(), self.loop)
            punsubscribed = asyncio.run_coroutine_threadsafe(self.pubsub.punsubscribe(), self.loop)
            asyncio.wait(
                [unsubscribed, punsubscribed],
                loop=self.loop
            )


class ClusterPubSub(PubSub):
    """
    Wrapper for PubSub class.
    """
    reconnect_closed_coro = None
    def __init__(self, *args, **kwargs):
        super(ClusterPubSub, self).__init__(*args, **kwargs)

        self.last_pong = None

    def reset(self):
        if self.reconnect_closed_coro:
            self.reconnect_closed_coro.cancel()
        return super(ClusterPubSub, self).reset()

    def handle_message(self, response, ignore_subscribe_messages=False):
        if response[0] == "pong":
            self.last_pong = time.time()
            return
        elif response[0] not in ['message', 'subscribe']:
            print("response not recegnized {}".format(response))
        else:
            return super(ClusterPubSub, self).handle_message(response, ignore_subscribe_messages)

    async def ping_handler(self):
        while self.connection is not None:
            try:
                await self.connection.send_command('ping')
            except:
                print("unable to ping cluster connection")
                import traceback; traceback.print_exc()
            await asyncio.sleep(2)


    async def reconnect_closed(self):
        ping_coro = asyncio.ensure_future(self.ping_handler())
        while self.connection is not None:
            print(self.connection)
            try:
                if self.last_pong:
                    if time.time() - self.last_pong > 20:
                        connection = self.connection
                        print("no pong in 30 seconds")
                        #await self.connection_pool.nodes.increment_reinitialize_counter()
                        # for task in self.connection._connect_callbacks:
                        #     task.cancel()

                        self.connection.clear_connect_callbacks()
                        self.connection_pool.release(connection)
                        # self.connection.disconnect()
                        self.connection_pool.disconnect()
                        self.connection_pool.reset()
                        await self.connection_pool.nodes.initialize()
                        connection  = self.connection_pool.get_connection('pubsub')
                        del self.connection
                        self.connection = connection
                        self.connection.register_connect_callback(self.on_connect)
                        await asyncio.sleep(5)
                await asyncio.sleep(3)
            except Exception as e:
                print("exception in reconnect pubsub handler")
                import traceback
                traceback.print_exc()
        ping_coro.cancel()

    # async def _execute(self, connection, command, *args):
    #     if 'send_command' in str(command):
    #         print("execute args command: {} args: {}".format(command, args))
    #     if "send_command" in str(command) and len(args) >1:
    #
    #         ttl = 26
    #         while ttl > 0:
    #             try:
    #                 return await super(ClusterPubSub, self)._execute(connection, command, *args)
    #             except Exception as e:
    #                 await asyncio.sleep(0.2)
    #                 await self.connection_pool.nodes.increment_reinitialize_counter()
    #                 print("error executing pubsub command, resetting connection {} args: ".format(e. args))
    #                 connection = self.connection = self.connection_pool.get_connection(
    #                     'pubsub',
    #                     channel=args[1],
    #                 )
    #                 # register a callback that re-subscribes to any channels we
    #                 # were listening to when we were disconnected
    #                 self.connection.register_connect_callback(self.on_connect)
    #             ttl-=1
    #         raise ConnectionError("Cluster TTL exceeded for Pubsub")
    #     else:
    #         try:
    #             return await super(ClusterPubSub, self)._execute(connection, command, *args)
    #         except Exception as e:
    #             print("exception in _execute")
    #             import traceback; traceback.print_exc()

    async def execute_command(self, *args, tries=0, **kwargs):
        """
        Execute a publish/subscribe command.

        Taken code from redis-py and tweak to make it work within a cluster.
        """
        # NOTE: don't parse the response in this function -- it could pull a
        # legitimate message off the stack if the connection is already
        # subscribed to one or more channels
        await self.connection_pool.initialize()

        if self.connection is None:


            connection = self.connection = self.connection_pool.get_connection(
                'pubsub',
                channel=args[1],
            )
            self.last_pong = time.time()

            # register a callback that re-subscribes to any channels we
            # were listening to when we were disconnected
            self.connection.register_connect_callback(self.on_connect)
            if self.reconnect_closed_coro is None:
                self.reconnect_closed_coro = asyncio.ensure_future(self.reconnect_closed())

        try:
            connection = self.connection
            await self._execute(connection, connection.send_command, *args)
        except Exception as e:
            if tries > 25:
                print("too many pubsub retries, giving up")
                raise e
            tries+=1
            await self.connection_pool.nodes.increment_reinitialize_counter()
            self.connection=None
            await asyncio.sleep(tries+1)
            print("Redis pubsub _execute exception {}".format(e))
            import traceback; traceback.print_exc()
            await self.execute_command(*args, tries=tries,  **kwargs)
