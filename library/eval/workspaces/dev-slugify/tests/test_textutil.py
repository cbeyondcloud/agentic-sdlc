from textutil import slugify


def test_slugify_basic():
    assert slugify("Hello, World!") == "hello-world"


def test_slugify_strips_accents():
    assert slugify("Crème Brûlée Recipe") == "creme-brulee-recipe"
