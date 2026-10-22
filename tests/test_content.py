import shutil
from pathlib import Path

import pytest
import yaml

from poster.content_generator.generator import Generator, load_rows, load_words


@pytest.fixture
def content_root(tmp_path):
    root = tmp_path / "content"
    shutil.copytree(Path(__file__).parents[1] / "examples/content", root)
    return root


def test_seed_locale_and_product_description_binding(content_root):
    g = Generator(content_root)
    a = g.generate("italian_info", 123)
    assert a == g.generate("italian_info", 123)
    assert a.content_id == g.generate("italian_info", 123).content_id
    assert a.metadata["locale"] == "it"
    assert a.metadata["product"]["name"] in a.title
    assert a.metadata["description"] in a.body
    assert len(a.metadata["selected"]["cities"][0]) == 5
    assert len(set(a.metadata["selected"]["keywords"][0])) == 3
    fr = g.generate("french_info", 123)
    assert fr.metadata["locale"] == "fr"
    assert "Paracétamol" in fr.title
    assert "Roma" not in fr.body


def test_hot_dictionary_and_template_reload(content_root):
    g = Generator(content_root)
    before = g.generate("italian_info", 7)
    cities = content_root / "dictionaries/cities_it.txt"
    cities.write_text("New1\nNew2\nNew3\nNew4\nNew5\n", encoding="utf-8")
    template = content_root / "templates/italian_info.txt"
    template.write_text("CHANGED {{cities:5}} {{product.name}}", encoding="utf-8")
    after = g.generate("italian_info", 7)
    assert "CHANGED" in after.body and "New1" in after.body
    assert before.metadata["source_revision"] != after.metadata["source_revision"]
    assert before.body != after.body


def test_no_cross_product_fallback_and_insufficient_words(content_root):
    descriptions = content_root / "dictionaries/descriptions.csv"
    descriptions.write_text("product_id,description\nwrong,Unrelated\n")
    with pytest.raises(ValueError, match="Missing description"):
        Generator(content_root).generate("italian_info", 1)


def test_yaml_loaders_and_macro_limits(content_root):
    words = content_root / "words.yaml"
    words.write_text(yaml.safe_dump(["one", "two", "two"]))
    assert load_words(words) == ["one", "two"]
    rows = content_root / "rows.yaml"
    rows.write_text(yaml.safe_dump([{"id": "a", "name": "A"}]))
    assert load_rows(rows)[0]["id"] == "a"
    (content_root / "templates/italian_info.txt").write_text("{{cities:999}}")
    with pytest.raises(ValueError, match="Insufficient"):
        Generator(content_root).generate("italian_info", 1)


def test_content_ready_atomicity_and_invalid_revision_fallback(content_root):
    from poster.project import mark_ready

    mark_ready(content_root)
    generator = Generator(content_root)
    initial = generator.generate("italian_info", 1)
    template = content_root / "templates/italian_info.txt"
    template.write_text("NEW {{product.name}}")
    assert generator.generate("italian_info", 1) == initial
    mark_ready(content_root)
    revised = generator.generate("italian_info", 1)
    assert revised.body.startswith("NEW ")
    template.write_text("{{unknown}}")
    mark_ready(content_root)
    assert generator.generate("italian_info", 1) == revised


def test_optional_blocks_and_strict_macros(content_root):
    template = content_root / "templates/italian_info.txt"
    template.write_text("[[optional:0]]NO[[/optional]][[optional:1]]YES[[/optional]]")
    assert Generator(content_root).generate("italian_info", 1).body == "YES"
    template.write_text("{{unknown}}")
    with pytest.raises(ValueError, match="Unknown content macro"):
        Generator(content_root).generate("italian_info", 1)
