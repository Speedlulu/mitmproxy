from abc import abstractmethod
from queue import Queue
import time
from typing import Dict, Optional, Union

from aioquic.h3.connection import (
    H3Connection,
    ErrorCode as H3ErrorCode,
    FrameUnexpected as H3FrameUnexpected,
    Headers as H3Headers,
    HeadersState as H3HeadersState,
)
from aioquic.h3 import events as h3_events
from aioquic.quic import events as quic_events
from aioquic.quic.connection import stream_is_client_initiated, stream_is_unidirectional
from aioquic.quic.packet import QuicErrorCode

from mitmproxy import connection, http, version
from mitmproxy.net.http import status_codes
from mitmproxy.proxy import commands, context, events, layer
from mitmproxy.proxy.layers.quic import (
    OpenQuicStream,
    QuicStreamDataReceived,
    QuicStreamReset,
    ResetQuicStream,
    SendQuicStreamData,
    error_code_to_str,
    get_connection_error,
    set_connection_error,
)
from mitmproxy.proxy.utils import expect

from . import (
    RequestData,
    RequestEndOfMessage,
    RequestHeaders,
    RequestProtocolError,
    ResponseData,
    ResponseEndOfMessage,
    ResponseHeaders,
    RequestTrailers,
    ResponseTrailers,
    ResponseProtocolError,
)
from ._base import (
    HttpConnection,
    HttpEvent,
    ReceiveHttp,
    format_error,
)
from ._http2 import (
    format_h2_request_headers,
    format_h2_response_headers,
    parse_h2_request_headers,
    parse_h2_response_headers,
)


class MockQuic:
    """
    aioquic intermingles QUIC and HTTP/3. This is something we don't want to do because that makes testing much harder.
    Instead, we mock our QUIC connection object here and then take out the wire data to be sent.
    """

    def __init__(self, conn: connection.Connection) -> None:
        self.conn = conn
        self.pending_commands: Queue[commands.Command] = Queue()
        self.available_stream_ids: Queue[int] = Queue()

    def close(
        self,
        error_code: int = QuicErrorCode.NO_ERROR,
        frame_type: Optional[int] = None,
        reason_phrase: str = "",
    ) -> None:
        set_connection_error(self.conn, quic_events.ConnectionTerminated(
            error_code=error_code,
            frame_type=frame_type,
            reason_phrase=reason_phrase,
        ))
        self.pending_commands.put(commands.CloseConnection(self.conn))

    def get_next_available_stream_id(self, is_unidirectional=False) -> int:
        stream_id = self.available_stream_ids.get()
        assert is_unidirectional == stream_is_unidirectional(stream_id)
        return stream_id

    def send_stream_data(
        self, stream_id: int, data: bytes, end_stream: bool = False
    ) -> None:
        self.pending_commands.put(SendQuicStreamData(self.conn, stream_id, data, end_stream))


class LayeredH3Connection(H3Connection):
    def __init__(self, connection: connection.Connection) -> None:
        self._quic = MockQuic(connection)
        super().__init__(quic=self._quic, enable_webtransport=False)

    def start(self) -> layer.CommandGenerator[None]:
        # we need three unidirectional streams for `_init_connection`
        for _ in range(1, 3):
            self._quic.available_stream_ids.put((yield OpenQuicStream(self._quic.conn, is_unidirectional=False)))

    def create_webtransport_stream(self, session_id: int, is_unidirectional: bool = False) -> int:
        raise NotImplementedError()  # pragma: no cover

    def send_datagram(self, flow_id: int, data: bytes) -> None:
        raise NotImplementedError()  # pragma: no cover

    def send_push_promise(self, stream_id: int, headers: H3Headers) -> int:
        raise NotImplementedError()  # pragma: no cover

    def transmit(self) -> layer.CommandGenerator[None]:
        while self._quic.pending_commands:
            yield self._quic.pending_commands.get()


class Http3Connection(HttpConnection):
    h3_conn: Optional[LayeredH3Connection] = None

    ReceiveData: type[Union[RequestData, ResponseData]]
    ReceiveEndOfMessage: type[Union[RequestEndOfMessage, ResponseEndOfMessage]]
    ReceiveProtocolError: type[Union[RequestProtocolError, ResponseProtocolError]]
    ReceiveTrailers: type[Union[RequestTrailers, ResponseTrailers]]

    @abstractmethod
    def parse_headers(
        self, event: h3_events.HeadersReceived
    ) -> Union[RequestHeaders, ResponseHeaders]:
        pass  # pragma: no cover

    def postprocess_outgoing_event(self, event: HttpEvent) -> HttpEvent:
        return event

    def preprocess_incoming_event(self, event: HttpEvent) -> layer.CommandGenerator[HttpEvent]:
        yield from ()
        return event

    @abstractmethod
    def send_protocol_error(
        self, event: Union[RequestProtocolError, ResponseProtocolError]
    ) -> None:
        pass  # pragma: no cover

    @expect(HttpEvent, QuicStreamDataReceived, QuicStreamReset, events.ConnectionClosed)
    def state_done(self, _) -> layer.CommandGenerator[None]:
        yield from ()

    @expect(HttpEvent, QuicStreamDataReceived, QuicStreamReset, events.ConnectionClosed)
    def state_ready(self, event: events.Event) -> layer.CommandGenerator[None]:
        assert self.h3_conn is not None

        # send mitmproxy HTTP events over the H3 connection
        if isinstance(event, HttpEvent):
            event = yield from self.preprocess_incoming_event(event)
            try:

                if isinstance(event, (RequestData, ResponseData)):
                    self.h3_conn.send_data(
                        stream_id=event.stream_id, data=event.data, end_stream=False
                    )
                elif isinstance(event, (RequestHeaders, ResponseHeaders)):
                    self.h3_conn.send_headers(
                        stream_id=event.stream_id,
                        headers=(
                            yield from (
                                format_h2_request_headers(self.context, event)
                                if isinstance(event, RequestHeaders)
                                else format_h2_response_headers(self.context, event)
                            )
                        ),
                        end_stream=event.end_stream,
                    )
                    if event.end_stream:
                        # this will prevent any further headers or data from being sent
                        self.h3_conn._stream[
                            event.stream_id
                        ].headers_send_state = H3HeadersState.AFTER_TRAILERS
                elif isinstance(event, (RequestTrailers, ResponseTrailers)):
                    self.h3_conn.send_headers(
                        stream_id=event.stream_id,
                        headers=[*event.trailers.fields],
                        end_stream=True,
                    )
                elif isinstance(event, (RequestEndOfMessage, ResponseEndOfMessage)):
                    self.h3_conn.send_data(
                        stream_id=event.stream_id, data=b"", end_stream=True
                    )
                elif isinstance(event, (RequestProtocolError, ResponseProtocolError)):
                    self.send_protocol_error(event)
                else:
                    raise AssertionError(f"Unexpected event: {event!r}")

            except H3FrameUnexpected:
                # Http2Connection also ignores HttpEvents that violate the current stream state
                pass

            else:
                # transmit buffered data and re-arm timer
                yield from self.h3_conn.transmit()

        elif isinstance(event, QuicStreamReset):
            if (
                stream_is_client_initiated(event.stream_id)
                and event.stream_id in self.h3_conn._stream
                and not self.h3_conn._stream[event.stream_id].ended
            ):
                # mark the receiving part of the stream as ended
                # (H3Connection alas doesn't handle StreamReset)
                self.h3_conn._stream[event.stream_id] = True

                # report the protocol error (doing the same error code mingling as H2)
                code = (
                    status_codes.CLIENT_CLOSED_REQUEST
                    if event.error_code == H3ErrorCode.H3_REQUEST_CANCELLED
                    else self.ReceiveProtocolError.code
                )
                yield ReceiveHttp(
                    self.postprocess_outgoing_event(
                        self.ReceiveProtocolError(
                            stream_id=event.stream_id,
                            message=f"stream reset by client ({error_code_to_str(event.error_code)})",
                            code=code,
                        )
                    )
                )

        elif isinstance(event, QuicStreamDataReceived):
            yield commands.Log(f"recvd data: {event=}")
            # and convert back...
            e = quic_events.StreamDataReceived(data=event.data, end_stream=event.end_stream, stream_id=event.stream_id)
            for h3_event in self.h3_conn.handle_event(e):
                yield commands.Log(f"{h3_event=}")

                # report received data
                if (
                    isinstance(h3_event, h3_events.DataReceived)
                    and stream_is_client_initiated(h3_event.stream_id)
                ):
                    yield ReceiveHttp(
                        self.postprocess_outgoing_event(
                            self.ReceiveData(
                                stream_id=h3_event.stream_id, data=h3_event.data
                            )
                        )
                    )
                    if h3_event.stream_ended:
                        yield ReceiveHttp(
                            self.postprocess_outgoing_event(
                                self.ReceiveEndOfMessage(stream_id=h3_event.stream_id)
                            )
                        )

                # report headers and trailers
                elif (
                    isinstance(h3_event, h3_events.HeadersReceived)
                    and stream_is_client_initiated(h3_event.stream_id)
                ):
                    if self.h3_conn._stream[h3_event.stream_id].headers_recv_state is H3HeadersState.AFTER_TRAILERS:
                        yield ReceiveHttp(
                            self.postprocess_outgoing_event(
                                self.ReceiveTrailers(
                                    stream_id=h3_event.stream_id,
                                    trailers=http.Headers(h3_event.headers),
                                )
                            )
                        )
                    else:
                        try:
                            receive_event = self.parse_headers(h3_event)
                        except ValueError as e:
                            # this will result in a ConnectionClosed event
                            set_connection_error(self.conn, quic_events.ConnectionTerminated(
                                error_code=H3ErrorCode.H3_GENERAL_PROTOCOL_ERROR,
                                frame_type=None,
                                reason_phrase=f"Invalid HTTP/3 request headers: {e}",
                            ))
                            yield commands.CloseConnection(self.conn)
                        else:
                            yield ReceiveHttp(
                                self.postprocess_outgoing_event(receive_event)
                            )

                    # always report an EndOfMessage if the stream has ended
                    if h3_event.stream_ended:
                        yield ReceiveHttp(
                            self.postprocess_outgoing_event(
                                self.ReceiveEndOfMessage(stream_id=h3_event.stream_id)
                            )
                        )

                # we don't support push, web transport, etc.
                else:
                    yield commands.Log(
                        f"Ignored unsupported H3 event: {h3_event!r}",
                        level=(
                            "info"
                            if stream_is_client_initiated(h3_event.stream_id) else
                            "debug"
                        )
                    )

        # report a protocol error for all remaining open streams when a connection is closed
        elif isinstance(event, events.ConnectionClosed):
            for stream in self.h3_conn._stream.values():
                if stream_is_client_initiated(stream.stream_id) and not stream.ended:
                    close_event = get_connection_error(self.conn)
                    yield ReceiveHttp(
                        self.postprocess_outgoing_event(
                            self.ReceiveProtocolError(
                                stream_id=stream.stream_id,
                                message=(
                                    "Connection closed."
                                    if close_event is None else
                                    close_event.reason_phrase
                                ),
                                code=(
                                    QuicErrorCode.APPLICATION_ERROR
                                    if close_event is None else
                                    close_event.error_code
                                ),
                            )
                        )
                    )
            self._handle_event = self.state_done

        else:
            raise AssertionError(f"Unexpected event: {event!r}")

    @expect(events.Start)
    def state_start(self, event: events.Event) -> layer.CommandGenerator[None]:
        assert self.h3_conn is None

        self.h3_conn = LayeredH3Connection(self.conn)
        self._handle_event = self.state_ready
        yield from self.h3_conn.start()

    _handle_event = state_start


class Http3Server(Http3Connection):
    ReceiveData = RequestData
    ReceiveEndOfMessage = RequestEndOfMessage
    ReceiveProtocolError = RequestProtocolError
    ReceiveTrailers = RequestTrailers

    def __init__(self, context: context.Context):
        super().__init__(context, context.client)

    def parse_headers(self, event: h3_events.HeadersReceived) -> Union[RequestHeaders, ResponseHeaders]:
        # same as HTTP/2
        (
            host,
            port,
            method,
            scheme,
            authority,
            path,
            headers,
        ) = parse_h2_request_headers(event.headers)
        request = http.Request(
            host=host,
            port=port,
            method=method,
            scheme=scheme,
            authority=authority,
            path=path,
            http_version=b"HTTP/3",
            headers=headers,
            content=None,
            trailers=None,
            timestamp_start=time.time(),
            timestamp_end=None,
        )
        return RequestHeaders(stream_id=event.stream_id, request=request, end_stream=event.stream_ended)

    def send_protocol_error(self, event: Union[RequestProtocolError, ResponseProtocolError]) -> None:
        assert self.h3_conn is not None
        assert isinstance(event, ResponseProtocolError)

        # same as HTTP/2
        code = event.code
        if code != status_codes.CLIENT_CLOSED_REQUEST:
            code = status_codes.INTERNAL_SERVER_ERROR
        self.h3_conn.send_headers(
            stream_id=event.stream_id,
            headers=[
                (b":status", b"%d" % code),
                (b"server", version.MITMPROXY.encode()),
                (b"content-type", b"text/html"),
            ],
        )
        self.h3_conn.send_data(
            stream_id=event.stream_id,
            data=format_error(code, event.message),
            end_stream=True,
        )


class Http3Client(Http3Connection):
    ReceiveData = ResponseData
    ReceiveEndOfMessage = ResponseEndOfMessage
    ReceiveProtocolError = ResponseProtocolError
    ReceiveTrailers = ResponseTrailers

    def __init__(self, context: context.Context):
        super().__init__(context, context.server)
        self._event_to_quic: Dict[int, int] = {}
        self._quic_to_event: Dict[int, int] = {}

    def parse_headers(self, event: h3_events.HeadersReceived) -> Union[RequestHeaders, ResponseHeaders]:
        # same as HTTP/2
        status_code, headers = parse_h2_response_headers(event.headers)
        response = http.Response(
            http_version=b"HTTP/3",
            status_code=status_code,
            reason=b"",
            headers=headers,
            content=None,
            trailers=None,
            timestamp_start=time.time(),
            timestamp_end=None,
        )
        return ResponseHeaders(stream_id=event.stream_id, response=response, end_stream=event.stream_ended)

    def postprocess_outgoing_event(self, event: HttpEvent) -> HttpEvent:
        event.stream_id = self._quic_to_event[event.stream_id]
        return event

    def preprocess_incoming_event(self, event: HttpEvent) -> layer.CommandGenerator[HttpEvent]:
        if event.stream_id in self._event_to_quic:
            event.stream_id = self._event_to_quic[event.stream_id]
        else:
            # QUIC and HTTP/3 would actually allow for direct stream ID mapping, but since we want
            # to support H2<->H3, we need to translate IDs.
            # NOTE: We always create bidirectional streams, as we can't safely infer unidirectionality.
            stream_id = yield OpenQuicStream(self.conn)
            self._event_to_quic[event.stream_id] = stream_id
            self._quic_to_event[stream_id] = event.stream_id
            event.stream_id = stream_id
        return event

    def send_protocol_error(self, event: Union[RequestProtocolError, ResponseProtocolError]) -> None:
        assert isinstance(event, RequestProtocolError)

        # same as HTTP/2
        code = event.code
        if code != H3ErrorCode.H3_REQUEST_CANCELLED:
            code = H3ErrorCode.H3_INTERNAL_ERROR
        yield ResetQuicStream(
            connection=self.conn,
            stream_id=event.stream_id,
            error_code=code,
        )


__all__ = [
    "Http3Client",
    "Http3Server",
]
