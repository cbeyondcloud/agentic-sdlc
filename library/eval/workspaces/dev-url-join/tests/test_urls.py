from urls import join_url


def test_plain_join():
    assert join_url("https://api.example.com", "v1/orders") == "https://api.example.com/v1/orders"


def test_no_double_slashes():
    assert join_url("https://api.example.com/", "/v1/orders") == "https://api.example.com/v1/orders"
    assert join_url("https://gw.example.com/acme/", "v1") == "https://gw.example.com/acme/v1"
