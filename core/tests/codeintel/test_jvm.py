"""The JVM convention adapter (M3-27): Maven/Gradle test and import conventions answer
`tests_for`, `dependents` and `impact` for Java and Kotlin with no binary and no index."""

import os
from pathlib import Path

import pytest

from agentic.codeintel import CodeIntel
from agentic.codeintel.jvm import JvmConventions, is_jvm_repo

MAIN = "core/src/main/java/com/acme/shop"
TEST = "core/src/test/java/com/acme/shop"


def write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def java(package: str, body: str = "", imports: tuple[str, ...] = ()) -> str:
    lines = [f"package {package};", *(f"import {name};" for name in imports), body]
    return "\n".join(lines) + "\n"


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    write(root, "pom.xml", "<project/>")
    write(root, f"{MAIN}/Cart.java", java("com.acme.shop", "class Cart {}"))
    write(root, f"{MAIN}/Price.java", java("com.acme.shop", "class Price { Cart cart; }"))
    write(
        root,
        "core/src/main/java/com/acme/web/CartController.java",
        java("com.acme.web", "class CartController {}", ("com.acme.shop.Cart",)),
    )
    write(root, "core/src/main/java/com/acme/web/Other.java", java("com.acme.web", "class O {}"))
    write(root, f"{TEST}/CartTest.java", java("com.acme.shop", "class CartTest {}"))
    write(root, f"{TEST}/CartIT.java", java("com.acme.shop", "class CartIT {}"))
    write(root, f"{TEST}/CartPricingTest.java", java("com.acme.shop", "class CartPricingTest {}"))
    write(root, f"{TEST}/CheckoutTest.java", java("com.acme.shop", "class T { Cart c; }"))
    write(root, f"{TEST}/UnrelatedTest.java", java("com.acme.shop", "class U { Price p; }"))
    write(
        root,
        "core/src/test/java/com/acme/web/CartControllerTests.java",
        java("com.acme.web", "class X {}", ("com.acme.shop.Cart",)),
    )
    return root


def test_build_files_mark_a_jvm_repo(tmp_path: Path):
    assert not is_jvm_repo(tmp_path)
    for name in ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle.kts"):
        folder = tmp_path / name.replace(".", "-")
        write(folder, name)
        assert is_jvm_repo(folder), name


def test_it_is_a_codeintel_adapter(shop: Path):
    adapter: CodeIntel = JvmConventions(shop)
    assert adapter.name == "jvm-conventions"
    assert adapter.supports == frozenset({"dependents", "impact", "tests_for"})


def test_tests_for_ranks_named_tests_then_prefix_then_mentions_then_importers(shop: Path):
    found = JvmConventions(shop).tests_for(f"{MAIN}/Cart.java")
    assert found.files == (
        f"{TEST}/CartIT.java",  # named, same package
        f"{TEST}/CartTest.java",
        f"{TEST}/CartPricingTest.java",  # same package, name starts with Cart
        f"{TEST}/CheckoutTest.java",  # same package, names Cart (no import needed)
        "core/src/test/java/com/acme/web/CartControllerTests.java",  # imports it
    )
    assert found.source == "jvm-conventions"


def test_a_named_test_in_another_package_still_counts(tmp_path: Path):
    write(tmp_path, "build.gradle")
    write(tmp_path, "src/main/java/a/Foo.java", java("a", "class Foo {}"))
    write(tmp_path, "src/test/java/b/FooTests.java", java("b", "class FooTests {}"))
    write(tmp_path, "src/test/java/b/TestFoo.java", java("b", "class TestFoo {}"))
    found = JvmConventions(tmp_path).tests_for("src/main/java/a/Foo.java").files
    assert found == ("src/test/java/b/FooTests.java", "src/test/java/b/TestFoo.java")


def test_kotlin_without_semicolons(tmp_path: Path):
    write(tmp_path, "build.gradle.kts")
    write(tmp_path, "src/main/kotlin/a/Repo.kt", "package a\n\nclass Repo\n")
    write(tmp_path, "src/test/kotlin/a/RepoTest.kt", "package a\n\nclass RepoTest\n")
    write(tmp_path, "src/test/kotlin/b/UsesIt.kt", "package b\nimport a.Repo\nclass UsesItTest\n")
    found = JvmConventions(tmp_path).tests_for("src/main/kotlin/a/Repo.kt").files
    assert found == ("src/test/kotlin/a/RepoTest.kt", "src/test/kotlin/b/UsesIt.kt")


def test_wildcard_and_static_imports_count_only_when_the_name_is_used(tmp_path: Path):
    write(tmp_path, "pom.xml")
    write(tmp_path, "src/main/java/a/Foo.java", java("a", "class Foo {}"))
    write(tmp_path, "src/test/java/b/WildTest.java", java("b", "Foo f;", ("a.*",)))
    write(tmp_path, "src/test/java/b/IdleTest.java", java("b", "Bar f;", ("a.*",)))
    write(tmp_path, "src/test/java/b/StaticTest.java", java("b", "x();", ("static a.Foo.x",)))
    found = JvmConventions(tmp_path).tests_for("src/main/java/a/Foo.java").files
    assert found == ("src/test/java/b/StaticTest.java", "src/test/java/b/WildTest.java")


def test_dependents_are_the_tests_then_the_sources_using_it(shop: Path):
    found = JvmConventions(shop).dependents(f"{MAIN}/Cart.java").files
    tests = JvmConventions(shop).tests_for(f"{MAIN}/Cart.java").files
    assert found[: len(tests)] == tests
    assert found[len(tests) :] == (
        "core/src/main/java/com/acme/web/CartController.java",  # imports it
        f"{MAIN}/Price.java",  # same package, names it
    )


def test_impact_is_the_changed_files_then_their_dependents(shop: Path):
    diff = f"--- a/{MAIN}/Price.java\n+++ b/{MAIN}/Price.java\n@@ -1 +1 @@\n"
    found = JvmConventions(shop).impact(diff).files
    assert found == (f"{MAIN}/Price.java", f"{TEST}/UnrelatedTest.java")


def test_impact_ignores_names_outside_the_tree_and_non_jvm_files(shop: Path):
    write(shop, "README.md", "# shop")
    diff = "--- a/../../etc/passwd\n+++ b/README.md\n"
    found = JvmConventions(shop).impact(diff)
    assert found.files == () and "no JVM source file" in found.note


def test_a_non_jvm_file_answers_nothing_with_a_note(shop: Path):
    write(shop, "scripts/build.py", "print(1)")
    found = JvmConventions(shop).tests_for("scripts/build.py")
    assert found.files == () and "not a JVM source file" in found.note


def test_a_test_file_has_no_tests_of_its_own(shop: Path):
    assert JvmConventions(shop).tests_for(f"{TEST}/CartTest.java").files == ()


def test_build_output_and_hidden_folders_are_skipped(shop: Path):
    write(shop, "core/target/generated/com/acme/shop/CartTest.java", java("com.acme.shop"))
    write(shop, "core/build/x/CartTests.java", java("com.acme.shop"))
    write(shop, ".idea/CartTest.java", java("com.acme.shop"))
    found = JvmConventions(shop).tests_for(f"{MAIN}/Cart.java").files
    assert not any(f.startswith(("core/target/", "core/build/", ".idea/")) for f in found)


@pytest.mark.skipif(os.name == "nt", reason="file links need privileges on Windows")
def test_linked_files_and_folders_are_never_followed(shop: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    write(outside, "CartTest.java", java("com.acme.shop"))
    (shop / "core/src/test/java/linked").symlink_to(outside, target_is_directory=True)
    (shop / f"{TEST}/LinkedCartTest.java").symlink_to(outside / "CartTest.java")
    found = JvmConventions(shop).tests_for(f"{MAIN}/Cart.java").files
    assert not any("linked" in f.lower() for f in found)


def test_a_huge_file_is_read_only_up_to_the_cap(shop: Path, monkeypatch):
    from agentic.codeintel import jvm

    monkeypatch.setattr(jvm, "MAX_READ_BYTES", 64)
    write(shop, f"{TEST}/LateTest.java", java("com.acme.shop", " " * 200 + "Cart c;"))
    found = JvmConventions(shop).tests_for(f"{MAIN}/Cart.java").files
    assert f"{TEST}/LateTest.java" not in found


def test_too_many_files_says_the_answer_is_partial(shop: Path, monkeypatch):
    from agentic.codeintel import jvm

    monkeypatch.setattr(jvm, "MAX_FILES", 3)
    found = JvmConventions(shop).tests_for(f"{MAIN}/Cart.java")
    assert "only the first 3" in found.note


# ── review fixes ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rel", "test"),
    [
        ("src/commonMain/kotlin/a/Foo.kt", False),  # Kotlin Multiplatform sources
        ("src/jvmMain/kotlin/a/Foo.kt", False),
        ("src/jmh/java/a/FooBench.java", False),
        ("src/com/acme/Foo.java", False),  # Ant layout: by name
        ("src/com/acme/FooTest.java", True),
        ("src/jvmTest/kotlin/a/FooTest.kt", True),
        ("src/commonTest/kotlin/a/Foo.kt", True),
        ("src/testFixtures/java/a/Fixtures.java", True),
        ("src/integrationTest/java/a/Flow.java", True),
        ("src/androidTest/java/a/Ui.java", True),
    ],
)
def test_source_sets_are_tests_only_when_named_like_tests(rel, test):
    from agentic.codeintel.jvm import _is_test

    assert _is_test(rel) is test


def test_a_package_named_like_build_output_is_still_read(tmp_path: Path):
    write(tmp_path, "pom.xml")
    write(tmp_path, "src/main/java/org/x/build/Foo.java", java("org.x.build", "class Foo {}"))
    write(tmp_path, "src/test/java/org/x/build/FooTest.java", java("org.x.build", "class T {}"))
    write(tmp_path, "target/classes/org/x/build/FooTests.java", java("org.x.build"))
    found = JvmConventions(tmp_path).tests_for("src/main/java/org/x/build/Foo.java").files
    assert found == ("src/test/java/org/x/build/FooTest.java",)


def test_a_seed_past_the_file_cap_is_still_answered(shop: Path, monkeypatch):
    from agentic.codeintel import jvm

    monkeypatch.setattr(jvm, "MAX_FILES", 1)
    found = JvmConventions(shop).tests_for(f"{MAIN}/Price.java")
    assert "is not a file" not in found.note and "only the first 1" in found.note


def test_a_deep_tree_is_walked_without_recursion(tmp_path: Path, monkeypatch):
    import sys

    write(tmp_path, "pom.xml")
    deep = "/".join(["d"] * 60)
    write(tmp_path, f"{deep}/src/main/java/a/Foo.java", java("a", "class Foo {}"))
    write(tmp_path, f"{deep}/src/test/java/a/FooTest.java", java("a", "class T {}"))
    monkeypatch.setattr(sys, "getrecursionlimit", lambda: 20)  # documents the intent
    found = JvmConventions(tmp_path).tests_for(f"{deep}/src/main/java/a/Foo.java").files
    assert found == (f"{deep}/src/test/java/a/FooTest.java",)


def test_too_many_folder_entries_stop_the_walk_with_a_note(shop: Path, monkeypatch):
    from agentic.codeintel import jvm

    monkeypatch.setattr(jvm, "MAX_ENTRIES", 5)
    found = JvmConventions(shop).dependents(f"{MAIN}/Cart.java")
    assert "only the first" in found.note


def test_impact_expands_at_most_the_capped_number_of_files(shop: Path, monkeypatch):
    from agentic.codeintel import jvm

    monkeypatch.setattr(jvm, "MAX_IMPACT_SEEDS", 1)
    diff = "".join(f"+++ b/{rel}\n" for rel in (f"{MAIN}/Price.java", f"{MAIN}/Cart.java"))
    found = JvmConventions(shop).impact(diff)
    assert found.files[:2] == (f"{MAIN}/Price.java", f"{MAIN}/Cart.java")
    assert f"{TEST}/CartTest.java" not in found.files  # Cart was not expanded
    assert "first 1 changed file" in found.note


def test_a_build_file_one_level_down_marks_a_jvm_repo(tmp_path: Path):
    write(tmp_path, "orders/pom.xml")
    assert is_jvm_repo(tmp_path)
    write(tmp_path / "other", ".hidden/pom.xml")
    assert not is_jvm_repo(tmp_path / "other")
