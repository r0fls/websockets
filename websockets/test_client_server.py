import os
import ssl
import unittest
from unittest.mock import patch

import asyncio

from .client import *
from .exceptions import InvalidHandshake
from .http import read_response
from .server import *


testcert = os.path.join(os.path.dirname(__file__), 'testcert.pem')


@asyncio.coroutine
def handler(ws, path):
    if path == '/attributes':
        yield from ws.send(repr((ws.host, ws.port, ws.secure)))
    else:
        yield from ws.send((yield from ws.recv()))


class ClientServerTests(unittest.TestCase):

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.start_server()

    def tearDown(self):
        self.stop_server()
        self.loop.close()

    def start_server(self):
        server = serve(handler, 'localhost', 8642)
        self.server = self.loop.run_until_complete(server)

    def start_client(self):
        client = connect('ws://localhost:8642/')
        self.client = self.loop.run_until_complete(client)

    def stop_client(self):
        self.loop.run_until_complete(self.client.worker)

    def stop_server(self):
        self.server.close()
        self.loop.run_until_complete(self.server.wait_closed())

    def test_basic(self):
        self.start_client()
        self.loop.run_until_complete(self.client.send("Hello!"))
        reply = self.loop.run_until_complete(self.client.recv())
        self.assertEqual(reply, "Hello!")
        self.stop_client()

    def test_protocol_attributes(self):
        client = connect('ws://localhost:8642/attributes')
        client = self.loop.run_until_complete(client)
        try:
            expected_attrs = repr(('localhost', 8642, False))
            client_attrs = repr((client.host, client.port, client.secure))
            self.assertEqual(client_attrs, expected_attrs)
            server_attrs = self.loop.run_until_complete(client.recv())
            self.assertEqual(server_attrs, expected_attrs)
        finally:
            self.loop.run_until_complete(client.worker)

    @patch('websockets.server.read_request')
    def test_server_receives_malformed_request(self, _read_request):
        _read_request.side_effect = ValueError("read_request failed")

        with self.assertRaises(InvalidHandshake):
            self.start_client()

    @patch('websockets.client.read_response')
    def test_client_receives_malformed_response(self, _read_response):
        _read_response.side_effect = ValueError("read_response failed")

        with self.assertRaises(InvalidHandshake):
            self.start_client()

        # Now the server believes the connection is open. Run the event loop
        # once to make it notice the connection was closed. Interesting hack.
        self.loop.run_until_complete(asyncio.sleep(0))

    @patch('websockets.client.build_request')
    def test_client_sends_invalid_handshake_request(self, _build_request):
        def wrong_build_request(set_header):
            return '42'
        _build_request.side_effect = wrong_build_request

        with self.assertRaises(InvalidHandshake):
            self.start_client()

    @patch('websockets.server.build_response')
    def test_server_sends_invalid_handshake_response(self, _build_response):
        def wrong_build_response(set_header, key):
            return build_response(set_header, '42')
        _build_response.side_effect = wrong_build_response

        with self.assertRaises(InvalidHandshake):
            self.start_client()

    @patch('websockets.client.read_response')
    def test_server_does_not_switch_protocols(self, _read_response):
        @asyncio.coroutine
        def wrong_read_response(stream):
            code, headers = yield from read_response(stream)
            return 400, headers
        _read_response.side_effect = wrong_read_response

        with self.assertRaises(InvalidHandshake):
            self.start_client()

        # Now the server believes the connection is open. Run the event loop
        # once to make it notice the connection was closed. Interesting hack.
        self.loop.run_until_complete(asyncio.sleep(0))

    @patch('websockets.server.WebSocketServerProtocol.send')
    def test_server_handler_crashes(self, send):
        send.side_effect = ValueError("send failed")

        self.start_client()
        self.loop.run_until_complete(self.client.send("Hello!"))
        reply = self.loop.run_until_complete(self.client.recv())
        self.assertEqual(reply, None)
        self.stop_client()

        # Connection ends with an unexpected error.
        self.assertEqual(self.client.close_code, 1011)

    @patch('websockets.server.WebSocketServerProtocol.close')
    def test_server_close_crashes(self, close):
        close.side_effect = ValueError("close failed")

        self.start_client()
        self.loop.run_until_complete(self.client.send("Hello!"))
        reply = self.loop.run_until_complete(self.client.recv())
        self.assertEqual(reply, "Hello!")
        self.stop_client()

        # Connection ends with a protocol error.
        self.assertEqual(self.client.close_code, 1002)


@unittest.skipUnless(os.path.exists(testcert), "test certificate is missing")
class SSLClientServerTests(ClientServerTests):

    @property
    def server_context(self):
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLSv1)
        ssl_context.load_cert_chain(testcert)
        return ssl_context

    @property
    def client_context(self):
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLSv1)
        ssl_context.load_verify_locations(testcert)
        ssl_context.verify_mode = ssl.CERT_REQUIRED
        return ssl_context

    def start_server(self):
        server = serve(handler, 'localhost', 8642, ssl=self.server_context)
        self.server = self.loop.run_until_complete(server)

    def start_client(self):
        client = connect('wss://localhost:8642/', ssl=self.client_context)
        self.client = self.loop.run_until_complete(client)

    def test_protocol_attributes(self):
        client = connect('wss://localhost:8642/attributes',
                         ssl=self.client_context)
        client = self.loop.run_until_complete(client)
        try:
            expected_attrs = repr(('localhost', 8642, True))
            client_attrs = repr((client.host, client.port, client.secure))
            self.assertEqual(client_attrs, expected_attrs)
            server_attrs = self.loop.run_until_complete(client.recv())
            self.assertEqual(server_attrs, expected_attrs)
        finally:
            self.loop.run_until_complete(client.worker)


class ClientServerOriginTests(unittest.TestCase):

    def test_checking_origin_succeeds(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        server = loop.run_until_complete(
            serve(handler, 'localhost', 8642, origins=['http://localhost']))
        client = loop.run_until_complete(
            connect('ws://localhost:8642/', origin='http://localhost'))

        loop.run_until_complete(client.send("Hello!"))
        self.assertEqual(loop.run_until_complete(client.recv()), "Hello!")

        server.close()
        loop.run_until_complete(server.wait_closed())
        loop.run_until_complete(client.worker)
        loop.close()

    def test_checking_origin_fails(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        server = loop.run_until_complete(
            serve(handler, 'localhost', 8642, origins=['http://localhost']))
        with self.assertRaises(InvalidHandshake):
            loop.run_until_complete(
                connect('ws://localhost:8642/', origin='http://otherhost'))

        server.close()
        loop.run_until_complete(server.wait_closed())
        loop.close()

    def test_checking_lack_of_origin_succeeds(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        server = loop.run_until_complete(
            serve(handler, 'localhost', 8642, origins=['']))
        client = loop.run_until_complete(connect('ws://localhost:8642/'))

        loop.run_until_complete(client.send("Hello!"))
        self.assertEqual(loop.run_until_complete(client.recv()), "Hello!")

        server.close()
        loop.run_until_complete(server.wait_closed())
        loop.run_until_complete(client.worker)
        loop.close()
