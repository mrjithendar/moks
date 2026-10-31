"""Amazon OpenSearch Service: every request is signed with SigV4 and the signature covers the body."""
import json

import httpx
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import ReadOnlyCredentials

from moks.connectors.base import ConnectorError
from moks.rag.backends import OpenSearchBackend, SigV4

URL = "https://search-moks.us-east-1.es.amazonaws.com"


def recomputed(request: httpx.Request, creds: ReadOnlyCredentials, body: bytes) -> str:
    """What a server computes from the request it received, using the date the client signed with."""
    headers = {"Host": request.url.netloc.decode(), "X-Amz-Date": request.headers["X-Amz-Date"]}
    if "Content-Type" in request.headers:
        headers["Content-Type"] = request.headers["Content-Type"]
    if "X-Amz-Security-Token" in request.headers:
        headers["X-Amz-Security-Token"] = request.headers["X-Amz-Security-Token"]
    aws = AWSRequest(method=request.method, url=str(request.url), data=body, headers=headers)
    signer = SigV4Auth(creds, "es", "us-east-1")
    aws.context["timestamp"] = request.headers["X-Amz-Date"]
    return f"{signer.signature(signer.string_to_sign(aws, signer.canonical_request(aws)), aws)}"


@pytest.fixture
def creds(monkeypatch):
    frozen = ReadOnlyCredentials("AKIDEXAMPLE", "not-a-real-secret", "session-token")
    monkeypatch.setattr(SigV4, "_credentials", lambda self: frozen)
    return frozen


def backend(handler):
    return OpenSearchBackend(URL, aws_region="us-east-1", transport=httpx.MockTransport(handler))


async def test_json_and_bulk_requests_are_signed_over_their_exact_bodies(creds):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"errors": False})
    os_ = backend(handler)
    await os_._req("POST", "/idx/_search", {"query": {"match_all": {}}})
    await os_._req("POST", "/_bulk?refresh=wait_for", ndjson='{"index":{}}\n{"a":1}\n')
    await os_._req("GET", "/_cluster/health")
    await os_.aclose()
    assert len(seen) == 3
    for request in seen:
        auth = request.headers["Authorization"]
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/") and "/us-east-1/es/aws4_request" in auth
        assert "host" in auth and request.headers["X-Amz-Security-Token"] == "session-token"
        assert auth.endswith("Signature=" + recomputed(request, creds, request.content))
    tampered = seen[0].content.replace(b"match_all", b"match_none")
    assert not seen[0].headers["Authorization"].endswith("Signature=" + recomputed(seen[0], creds, tampered))


async def test_ensure_index_keeps_its_vector_mapping_when_signed(creds):
    bodies = []

    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(404)
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={})
    os_ = backend(handler)
    await os_.ensure_index("moks-chunks-1", 768)
    await os_.aclose()
    mapping = bodies[0]["mappings"]["properties"]["embedding"]
    assert mapping["dimension"] == 768 and mapping["method"]["engine"] == "lucene" and bodies[0]["settings"]["index"]["knn"] is True


async def test_no_aws_credentials_is_a_clear_error_and_nothing_is_sent(monkeypatch):
    import botocore.session
    calls = []
    monkeypatch.setattr(botocore.session.Session, "get_credentials", lambda self: None)
    os_ = backend(lambda r: calls.append(r) or httpx.Response(200, json={}))
    with pytest.raises(ConnectorError) as err:
        await os_._req("GET", "/_cluster/health")
    await os_.aclose()
    assert err.value.kind == "config" and not calls


@pytest.mark.parametrize("url,kw", [(URL, {"username": "u", "password": "p"}), ("http://plain:9200", {})])
def test_signing_refuses_a_password_alongside_it_and_plain_http(url, kw):
    with pytest.raises(ConnectorError):
        OpenSearchBackend(url, aws_region="us-east-1", **kw)


async def test_self_hosted_opensearch_still_uses_basic_auth():
    seen = []
    os_ = OpenSearchBackend("http://os:9200", "u", "p", transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={})))
    await os_._req("GET", "/_cluster/health")
    await os_.aclose()
    assert seen[0].headers["Authorization"].startswith("Basic ")
