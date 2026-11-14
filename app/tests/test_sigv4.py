from lapcounter import sigv4


def test_aws_get_object_example():
    """AWS's documented example: GET /test.txt with Range, us-east-1, 2013-05-24."""
    headers = sigv4.sign(
        method="GET", host="examplebucket.s3.amazonaws.com", path="/test.txt",
        headers={"Range": "bytes=0-9"}, region="us-east-1", service="s3",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        date="20130524T000000Z", payload_hash=sigv4.EMPTY_SHA256,
    )
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def test_session_token_is_signed():
    headers = sigv4.sign(method="GET", host="b.s3.us-west-2.amazonaws.com", path="/k",
                         headers={"x-amz-security-token": "tok"}, region="us-west-2",
                         service="s3", access_key="A", secret_key="S", date="20261031T000000Z")
    assert "x-amz-security-token" in headers["authorization"]
    assert headers["x-amz-content-sha256"] == "UNSIGNED-PAYLOAD"


def test_canonical_path_encodes_once():
    assert sigv4.canonical_path("packs/lc-dev-1/a b.json") == "/packs/lc-dev-1/a%20b.json"
