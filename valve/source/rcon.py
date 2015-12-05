# -*- coding: utf-8 -*-

"""Source Dedicated Server remote console (RCON) interface."""

from __future__ import (absolute_import,
                        unicode_literals, print_function, division)

import enum
import errno
import functools
import logging
import socket
import struct
import time

import six


log = logging.getLogger(__name__)


class RCONError(Exception):
    """Base exception for all RCON-related errors."""


class RCONCommunicationError(RCONError):
    """Used for propagating socket-related errors."""


class RCONTimeoutError(RCONError):
    """Raised when a timeout occurs waiting for a response."""


class RCONAuthenticationError(RCONError):
    """Raised for failed authentication.

    :ivar bool banned: signifies whether the authentication failed due to
        being banned or for merely providing the wrong password.
    """

    def __init__(self, banned=False):
        super().__init__("Banned" if banned else "Wrong password")
        self.banned = banned


class RCONMessageError(RCONError):
    """Raised for errors encoding or decoding RCON messages."""


class RCONMessage(object):
    """Represents a RCON request or response."""

    ENCODING = "ascii"

    class Type(enum.IntEnum):
        """Message types corresponding to ``SERVERDATA_`` constants."""

        RESPONSE_VALUE = 0
        AUTH_RESPONSE = 2
        EXECCOMMAND = 2
        AUTH = 3

    def __init__(self, id_, type_, body_or_text):
        self.id = int(id_)
        self.type = self.Type(type_)
        if isinstance(body_or_text, six.binary_type):
            self.body = body_or_text
        else:
            self.body = b""
            self.text = body_or_text

    def __repr__(self):
        return ("<{0.__class__.__name__} "
                "{0.id} {0.type.name} {1}B>").format(self, len(self.body))

    @property
    def text(self):
        """Get the body of the message as Unicode.

        :raises UnicodeDecodeError: if the body cannot be decoded as ASCII.

        :returns: the body of the message as a Unicode string.

        .. note::
            It has been reported that some servers may not return valid
            ASCII as they're documented to do so. Therefore you should
            always handle the potential :exc:`UnicodeDecodeError`.

            If the correct encoding is known you can manually decode
            :attr:`body` for your self.
        """
        return self.body.decode(self.ENCODING)

    @text.setter
    def text(self, text):
        """Set the body of the message as Unicode.

        This will attempt to encode the given text as ASCII and set it as the
        body of the message.

        :param str text: the Unicode string to set the body as.

        :raises UnicodeEncodeError: if the string cannot be encoded as ASCII.
        """
        self.body = text.encode(self.ENCODING)

    def encode(self):
        """Encode message to a bytestring."""
        terminated_body = self.body + b"\x00\x00"
        size = struct.calcsize("<ii") + len(terminated_body)
        return struct.pack("<iii", size, self.id, self.type) + terminated_body

    @classmethod
    def decode(cls, buffer_):
        """Decode a message from a bytestring.

        This will attempt to decode a single message from the start of the
        given buffer. If the buffer contains more than a single message then
        this must be called multiple times.

        :raises MessageError: if the buffer doesn't contain a valid message.

        :returns: a tuple containing the decoded :class:`RCONMessage` and
            the remnants of the buffer. If the buffer contained exactly one
            message then the remaning buffer will be empty.
        """
        size_field_length = struct.calcsize("<i")
        if len(buffer_) < size_field_length:
            raise RCONMessageError(
                "Need at least {} bytes; got "
                "{}".format(size_field_length, len(buffer_)))
        size_field, raw_message = \
            buffer_[:size_field_length], buffer_[size_field_length:]
        size = struct.unpack("<i", size_field)[0]
        if len(raw_message) < size:
            raise RCONMessageError(
                "Message is {} bytes long "
                "but got {}".format(size, len(raw_message)))
        message, remainder = raw_message[:size], raw_message[size:]
        fixed_fields_size = struct.calcsize("<ii")
        fixed_fields, body_and_terminators = \
            message[:fixed_fields_size], message[fixed_fields_size:]
        id_, type_ = struct.unpack("<ii", fixed_fields)
        body = body_and_terminators[:-2]
        return cls(id_, type_, body), remainder


class _ResponseBuffer(object):
    """Utility class to buffer RCON responses.

    This class strictly handles multi-part responses and rolls them up
    into a single response automatically. The end of a multi-part response
    is indicated by an empty ``RESPONSE_VALUE`` immediately followed by
    another with a body of ``0x00010000``. In order to prompt a server to
    send these terminators an empty ``RESPONSE_VALUE`` must be *sent*
    immediately after an ``EXECCOMMAND``.

    https://developer.valvesoftware.com/wiki/RCON#Multiple-packet_Responses

    .. note::
        Multi-part responses are only applicable to ``EXECCOMAND`` requests.

    In addition to handling multi-part responses transparently this class
    provides the ability to :meth:`discard` incoming messages. When a
    message is discarded it will be parsed from the buffer but then
    silently dropped, meaning it cannot be retrieved via :meth:`pop`.

    Message discarding works with multi-responses but it only applies to
    the complete response, not the constituent parts.
    """

    def __init__(self):
        self._buffer = b""
        self._responses = []
        self._partial_responses = []
        self._discard_count = 0

    def pop(self):
        """Pop first received message from the buffer.

        :raises RCONError: if there are no whole complete in the buffer.

        :returns: the oldest response in the buffer as a :class:`RCONMessage`.
        """
        if not self._responses:
            raise RCONError("Response buffer is empty")
        return self._responses.pop(0)

    def clear(self):
        """Clear the buffer.

        This clears the byte buffer, response buffer, partial response
        buffer and the discard counter.
        """
        log.debug(
            "Buffer cleared; %i bytes, %i messages, %i parts, %i discarded",
            len(self._buffer),
            len(self._responses),
            len(self._partial_responses),
            self._discard_count,
        )
        self._buffer = b""
        del self._responses[:]
        del self._partial_responses[:]
        self._discard_count = 0

    def _enqueue_or_discard(self, message):
        """Enqueue a message for retrieval or discard it.

        If the discard counter is zero then the message will be added to
        the complete responses buffer. Otherwise the message is dropped
        and the discard counter is decremented.
        """
        if self._discard_count == 0:
            log.debug("Enqueuing message %r", message)
            self._responses.append(message)
        else:
            log.debug("Discarding message %r", message)
            self._discard_count -= 1

    def _consume(self):
        """Attempt to parse buffer into responses.

        This may or may not consume part or the whole of the buffer.
        """
        while self._buffer:
            try:
                message, self._buffer = RCONMessage.decode(self._buffer)
            except RCONMessageError:
                return
            else:
                if message.type is message.Type.RESPONSE_VALUE:
                    log.debug("Recevied message part %r", message)
                    self._partial_responses.append(message)
                    if len(self._partial_responses) >= 2:
                        penultimate, last = self._partial_responses[-2:]
                        if (not penultimate.body
                                and last.body == b"\x00\x01\x00\x00"):
                            self._enqueue_or_discard(RCONMessage(
                                self._partial_responses[0].id,
                                RCONMessage.Type.RESPONSE_VALUE,
                                b"".join(part.body for part
                                         in self._partial_responses[:-2]),
                            ))
                            del self._partial_responses[:]
                else:
                    if self._partial_responses:
                        log.warning("Unexpected message %r", message)
                    self._enqueue_or_discard(message)

    def feed(self, bytes_):
        """Feed bytes into the buffer."""
        self._buffer += bytes_
        self._consume()

    def discard(self):
        """Discard the next message in the buffer.

        If there are already responses in the buffer then the leftmost
        one will be dropped from the buffer. However, if there's no
        responses currently in the buffer, as soon as one is received it
        will be immediately dropped.

        This can be called multiple times to discard multiple responses.
        """
        if self._responses:
            self._responses.pop(0)
        else:
            self._discard_count += 1


class RCON(object):
    """Represents an RCON connection."""

    def __init__(self, address, password, timeout=None):
        self._address = address
        self._password = password
        self._timeout = timeout if timeout else None
        self._authenticated = False
        self._socket = None
        self._closed = False
        self._responses = _ResponseBuffer()

    def __enter__(self):
        self.connect()
        self.authenticate()
        return self

    def __exit__(self, value, type_, traceback):
        self.close()

    def __call__(self, command):
        """Invoke a command.

        This is a higher-level version of :meth:`execute` that always blocks
        and only returns the response body.

        :raises RCONMEssageError: if the response body couldn't be decoded
            into a Unicode string.

        :returns: the response to the command as a Unicode string.
        """
        try:
            return self.execute(command).text
        except UnicodeDecodeError as exc:
            raise RCONMessageError("Couldn't decode response: {}".format(exc))

    @property
    def connected(self):
        """Determine if a connection has been made.

        .. note::
            Strictly speaking this does not guarantee that any subsequent
            attempt to execute a command will succeed as the underlying
            socket may be closed by the server at any time. It merely
            indicates that a previous call to :meth:`connect` was
            successful.
        """
        return bool(self._socket)

    @property
    def authenticated(self):
        """Determine if the connection is authenticated."""
        return self._authenticated

    @property
    def closed(self):
        """Determine if the connection has been closed."""
        return self._closed

    def _request(self, type_, body):
        """Send a request to the server.

        This sends an encoded message with the given type and body to the
        server. The sent message will have an ID of zero.

        :param RCONMessage.Type type_: the type of message to send.
        :param body: the body of the message to send as either a bytestring
            or Unicode string.
        """
        request = RCONMessage(0, type_, body)
        self._socket.sendall(request.encode())

    def _read(self):
        """Read bytes from the socket into the response buffer.

        :raises RCONCommunicationError: if the socket is closed by the
            server or for any other unexpected socket-related error. In
            such cases the connection will also be closed.
        """
        try:
            i_bytes = self._socket.recv(4096)
        except socket.error:
            self.close()
            raise RCONCommunicationError
        if not i_bytes:
            self.close()
            raise RCONCommunicationError
        self._responses.feed(i_bytes)

    def _receive(self, count=1):
        """Receive messages from the server.

        This will wait up to the configured timeout for the given number of
        messages to be recieved from the server. If there is any kind of
        communication error or the timeout is reached then the connection is
        closed.

        :param int count: the number of messages to wait for.

        :raises RCONCommunicationError: if the socket is closed by the
            server or for any other unexpected socket-related error.
        :raises RCONTimeoutError: if the desired number of messages are
            not recieved in the configured timeout.

        :returns: a tuple containing ``count`` number of :class:`RCONMessage`
            that were received.
        """
        responses = []
        time_start = time.monotonic()
        while (self._timeout is None
                or time.monotonic() - time_start < self._timeout):
            self._read()
            try:
                response = self._responses.pop()
            except RCONError:
                continue
            else:
                responses.append(response)
                if len(responses) == count:
                    return tuple(responses)
        self.close()
        raise RCONTimeoutError

    def _ensure(state, value=True):
        """Decorator to ensure a connection is in a specific state.

        Use this to wrap a method so that it'll only be executed when
        certain attributes are set to ``True`` or ``False``. The returned
        function will raise :exc:`RCONError` if the condition is not met.

        Additionally, this decorator will modify the docstring of the
        wrapped function to include a sphinx-style ``:raises:`` directive
        documenting the valid state for the call.

        :param str state: the state attribute to check.
        :param bool value: the required value for the attribute.
        """

        def decorator(function):

            @functools.wraps(function)
            def wrapper(instance, *args, **kwargs):
                if not getattr(instance, state) is value:
                    raise RCONError("Must {} {}".format(
                        "be" if value else "not be", state))
                return function(instance, *args, **kwargs)

            if not wrapper.__doc__.endswith("\n"):
                wrapper.__doc__ += "\n"
            wrapper.__doc__ += ("\n:raises RCONError: {} {}.".format(
                "if" if value else "if not", state))
            return wrapper

        return decorator

    @_ensure('connected', False)
    @_ensure('closed', False)
    def connect(self):
        """Create a connection to a server."""
        log.debug("Connecting to %s", self._address)
        self._socket = socket.socket(
            socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        self._socket.connect(self._address)

    @_ensure('connected')
    @_ensure('closed', False)
    def authenticate(self):
        """Authenticate with the server.

        This sends an authentication message to the connected server
        containing the password. If the password is correct the server
        sends back an acknowledgement and will allow all subsequent
        commands to be executed.

        However, if the password is wrong the server will either notify
        the client or immediately drop the connection depending on whether
        the client IP has been banned or not. In either case, the client
        connection will be closed and an exception raised.

        .. note::
            Client banning IP banning happens automatically after a few
            failed attempts at authentication. Assuming you can direct
            access to the server's console you can unban the client IP
            using the ``removeip`` command::

                Banning xxx.xxx.xxx.xx for rcon hacking attempts
                ] removeip xxx.xxx.xxx.xxx
                removeip:  filter removed for xxx.xxx.xxx.xxx

        :raises RCONAuthenticationError: if authentication failed, either
            due to being banned or providing the wrong password.
        """
        self._request(RCONMessage.Type.AUTH, self._password)
        try:
            response = self._receive(1)[0]
        except RCONCommunicationError:
            raise RCONAuthenticationError(True)
        else:
            # TODO: Understand why two responses -- the first being
            #       completely empty (all zero) -- are sent.
            self._responses.clear()
            if response.id == -1:
                self.close()
                raise RCONAuthenticationError
            self._authenticated = True

    @_ensure('connected')
    def close(self):
        """Close connection to a server."""
        self._socket.close()
        self._closed = True
        self._socket = None

    @_ensure('authenticated')
    def execute(self, command, block=True):
        """Invoke a command.

        Invokes the given command on the conncted server. By default this
        will block (up to the timeout) for a response. This can be disabled
        if you don't care about the response.

        :param str command: the command to execute.
        :param bool block: whether or not to wait for a response.

        :raises RCONCommunicationError: if the socket is closed or in any
            other erroneous state whilst issuing the request or receiving
            the response.
        :raises RCONTimeoutError: if the timeout is reached waiting for a
            response.

        :returns: the response to the command as a :class:`RCONMessage` or
            ``None`` depending on whether ``block`` was ``True`` or not.
        """
        self._request(RCONMessage.Type.EXECCOMMAND, command)
        self._request(RCONMessage.Type.RESPONSE_VALUE, "")
        if block:
            return self._receive(1)[0]
        else:
            self._responses.discard()
            self._read()

    del _ensure


def shell(rcon=None):
    """A simple interactive RCON shell.

    An existing, connected and authenticated :class:`RCON` object can be
    given otherwise the shell will prompt for connection details.

    Once connected the shell simply dispatches commands and prints the
    response to stdout.

    :param rcon: the :class:`RCON` object to use for issuing commands
        or ``None``.
    """
