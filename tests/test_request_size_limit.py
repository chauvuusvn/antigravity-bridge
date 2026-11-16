from bridge.server import AntigravityBridgeServer


def test_bridge_accepts_large_client_request_bodies(tmp_path):
    server = AntigravityBridgeServer(
        api_key="test-key",
        home=tmp_path,
    )

    assert server.app._client_max_size == 64 * 1024 * 1024
