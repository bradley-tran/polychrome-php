"""Minimal FastCGI client used by integration tests and benchmarks (stdlib only)."""
import socket
import struct


def record(kind, data=b"", request_id=1):
    return struct.pack("!BBHHBB", 1, kind, request_id, len(data), 0, 0) + data


def encode_params(params):
    result = bytearray()
    for key, value in params.items():
        key, value = key.encode(), str(value).encode()
        for length in (len(key), len(value)):
            result.extend(bytes([length]) if length < 128 else struct.pack("!I", length | 0x80000000))
        result.extend(key)
        result.extend(value)
    return bytes(result)


def connect(endpoint, timeout=10):
    if isinstance(endpoint, str):
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(timeout)
        sock.connect(endpoint)
        return sock
    return socket.create_connection(endpoint, timeout=timeout)


def receive_exact(sock, length):
    result = bytearray()
    while len(result) < length:
        part = sock.recv(length - len(result))
        if not part:
            raise EOFError("FastCGI connection ended before END_REQUEST")
        result.extend(part)
    return bytes(result)


def receive_response(sock):
    output, errors = bytearray(), bytearray()
    while True:
        version, kind, request_id, length, padding, _ = struct.unpack("!BBHHBB", receive_exact(sock, 8))
        if version != 1 or request_id != 1:
            raise ValueError("Unexpected FastCGI record")
        data = receive_exact(sock, length)
        if padding:
            receive_exact(sock, padding)
        if kind == 6:
            output.extend(data)
        elif kind == 7:
            errors.extend(data)
        elif kind == 3:
            break
    raw_headers, _, body = bytes(output).partition(b"\r\n\r\n")
    headers = []
    status = 200
    for line in raw_headers.split(b"\r\n"):
        name, separator, value = line.partition(b":")
        if not separator:
            continue
        name, value = name.decode().lower(), value.decode().strip()
        if name == "status":
            status = int(value.split()[0])
        else:
            headers.append((name, value))
    return status, headers, body, bytes(errors)


def request(endpoint, filename, query="", method="GET", body=b"", params=None, keep=False, timeout=10):
    env = {
        "SCRIPT_FILENAME": str(filename), "SCRIPT_NAME": "/index.php",
        "REQUEST_URI": "/index.php" + ("?" + query if query else ""),
        "REQUEST_METHOD": method, "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)), "SERVER_PROTOCOL": "HTTP/1.1",
        "SERVER_NAME": "localhost", "SERVER_PORT": "80",
        "REMOTE_ADDR": "127.0.0.1", "REMOTE_PORT": "43210",
        "HTTP_HOST": "localhost", "GATEWAY_INTERFACE": "CGI/1.1",
        "DOCUMENT_ROOT": str(filename.parent),
    }
    env.update(params or {})
    with connect(endpoint, timeout=timeout) as sock:
        sock.sendall(record(1, struct.pack("!HB5x", 1, int(keep))))
        encoded = encode_params(env)
        for start in range(0, len(encoded), 65535):
            sock.sendall(record(4, encoded[start:start + 65535]))
        sock.sendall(record(4))
        for start in range(0, len(body), 65535):
            sock.sendall(record(5, body[start:start + 65535]))
        sock.sendall(record(5))
        response = receive_response(sock)
        if keep and sock.recv(1) != b"":
            raise AssertionError("one-request child left its connection open")
        return response
